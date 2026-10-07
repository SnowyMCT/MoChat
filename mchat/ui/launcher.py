"""启动器：**先账号，后界面** —— 程序第一屏到底是登录窗口还是聊天主窗口，由它决定。

流程（和 Telegram 一致）
------------------------
    启动 → 本地缓存里有登录令牌吗？
       ├─ 有 → 拿它问一句 /api/me
       │       ├─ 通了      → 直接开主窗口（**不会**闪一下登录界面）
       │       ├─ 401 失效  → 清掉这个账号的缓存，开登录窗口（带一句说明）
       │       └─ 连不上    → 照常开主窗口（离线：历史还在本地库，联网自动补）
       └─ 没有 → 开登录窗口

主窗口里的「退出登录」也回到这里：清掉缓存 → 关主窗口 → 回登录窗口。

为什么窗口之间的切换要这么小心
------------------------------
两个窗口交替时会有"一个窗口都没有"的瞬间，``QApplication`` 默认会**直接退出**，
所以这里把 ``quitOnLastWindowClosed`` 关掉，改成"**用户自己关掉窗口才算退出**"。
正因如此，退出这件事必须显式做干净（``quit()`` + 兜底定时器，见 :meth:`Launcher._quit_app`）——
否则窗口是没了、进程还挂在任务管理器里。
"""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from ..core.api import ApiClient, ApiError
from ..core.crashlog import default_crash_log_path, install_crash_log
from ..core.atrest import cleanup_view_cache, open_storage, set_active_cipher
from ..core.db import ChatLog
from ..core.session import Session, SessionStore, default_data_dir, log_path_for
from .async_call import AsyncCall
from .login_window import LoginWindow
from .theme import set_font_scale, clamp_font_scale

__all__ = ["Launcher", "main"]

SETTINGS_ORG = "MChat"
SETTINGS_APP = "MChat"
SCOPE_LOGIN = "login"
SCOPE_MAIN = "main"
# 用户点 ✕ 之后，事件循环最多再转这么久；到点还没退出来就**强制结束进程**。
# 为什么要有这一条：用户在意的是"关了窗口进程就得没" —— 只要哪里还挂着
# （嵌套事件循环、某个窗口没关干净……），资源管理器里就会留下一个 python，
# 攒几个就成了"关了软件还一堆 python 在跑"。正常路径是毫秒级退出，
# 这个定时器根本轮不到触发，它只是"绝不留僵尸"的保险。
QUIT_WATCHDOG_MS = 3000
QUIT_LOG_NAME = "quit.log"


class Launcher(QObject):
    """账号 → 窗口的总调度。测试里可以整体驱动：``start()`` / ``on_logged_in()`` / ``logout()``。"""

    windowChanged = pyqtSignal(str)             # "login" / "main" / ""（都关了）

    def __init__(self, app: QApplication, *, session_store: SessionStore | None = None,
                 api_factory=None, settings=None, main_factory=None, parent=None,
                 quit_watchdog_ms: int = 0):
        super().__init__(parent)
        self.app = app
        # 静态加密的钥匙：先开出来，再让"当前进程"记住它 —— 附件预览、点开文件、
        # 本地库、令牌缓存都靠它（见 mchat/core/atrest.py 的说明）。
        self.cipher = open_storage(default_data_dir())
        set_active_cipher(self.cipher)
        self.sessions = (session_store if session_store is not None
                         else SessionStore(cipher=self.cipher))
        self._api_factory = api_factory or (lambda url: ApiClient(url))
        self._settings = settings if settings is not None else QSettings(SETTINGS_ORG,
                                                                        SETTINGS_APP)
        self._main_factory = main_factory           # 测试注入用：造一个假的主窗口
        self._login: LoginWindow | None = None
        self._main = None
        self._session: Session | None = None
        self._api: ApiClient | None = None
        self._pending_session: Session | None = None
        self._pending_api: ApiClient | None = None
        # 0 = 不装保险（测试里直接建 Launcher 的场合：装了就可能在别的进程里
        # 3 秒后强制 os._exit，把测试进程带走）。真入口 main() 会传一个正数。
        self._quit_watchdog_ms = int(quit_watchdog_ms or 0)
        self._quit_armed = False
        self._quit_reason = ""          # 只为了日志里写清楚"是关哪个窗口触发的"
        self._probe = AsyncCall(self)
        self._probe.finished.connect(self._on_probe_ok)
        self._probe.failed.connect(self._on_probe_failed)

    # ================================================================== 状态
    @property
    def theme_name(self) -> str:
        from .theme import THEME_OPTIONS
        saved = str(self._settings.value("theme", THEME_OPTIONS[0]))
        return saved if saved in THEME_OPTIONS else THEME_OPTIONS[0]

    def apply_saved_preferences(self) -> None:
        """文字大小是全局状态，登录窗口也得跟着它（主题则各自 apply）。"""
        self._apply_font_scale()

    def _apply_font_scale(self) -> float:
        return set_font_scale(clamp_font_scale(self._settings.value("text_scale", 1.0)))

    def login_window(self) -> LoginWindow | None:
        return self._login

    def main_window(self):
        return self._main

    def session(self) -> Session | None:
        return self._session

    def api(self) -> ApiClient | None:
        return self._api

    def scope(self) -> str:
        """现在屏幕上是谁：``login`` / ``main`` / ``""``（都没有）。"""
        if self._main is not None:
            return SCOPE_MAIN
        if self._login is not None:
            return SCOPE_LOGIN
        return ""

    # ================================================================== 启动
    def start(self) -> str:
        """决定第一屏。返回 ``"login"`` 或 ``"main"``。"""
        self.apply_saved_preferences()
        cached = self.sessions.last()
        if cached is None:
            self.show_login()
            return SCOPE_LOGIN
        if not cached.token:
            # 上次是"不记住我"：只记了账号名，密码没存，还是得重新登录
            self.show_login(notice=f"上次用的账号是 {cached.username}，请输入密码")
            return SCOPE_LOGIN
        self._probe_session(cached)
        return SCOPE_LOGIN           # 探测期间先显示登录窗；通了会立刻被主窗口替换

    def _probe_session(self, session: Session) -> None:
        """拿缓存里的令牌问一句"我还登录着吗"（后台线程，别卡住第一屏）。"""
        api = self._api_factory(session.server)
        api.token = session.token

        def work():
            return api.me()

        self._pending_api = api
        self._pending_session = session
        if not self._probe.run(work):
            return
        if self._login is None:                  # 探测期间总得有点东西在屏幕上
            self.show_login(notice=f"正在用上次的登录连接 {session.host} …")

    def _on_probe_ok(self, result) -> None:
        session = self._pending_session
        user = dict((result or {}).get("user") or {})
        if user:
            from dataclasses import replace
            session = replace(session,
                              user_id=str(user.get("id") or session.user_id),
                              username=str(user.get("username") or session.username),
                              display_name=str(user.get("display_name")
                                               or session.display_name))
        self.sessions.save(session)               # 昵称可能在别处改过
        self._close_login()
        self.open_main(session, self._pending_api)

    def _on_probe_failed(self, error) -> None:
        session = self._pending_session
        if isinstance(error, ApiError) and error.is_auth:
            self.sessions.forget(session.server, session.username)
            self.show_login(notice=f"{session.username} 的登录已失效，请重新登录")
            return
        # 网络不通：不要把缓存扔了 —— 照常进主界面，离线也能看历史，联网自动补
        self._close_login()
        self.open_main(session, self._pending_api, offline=True)

    # ================================================================== 登录窗口
    def show_login(self, *, notice: str = "") -> LoginWindow:
        if self._login is None:
            window = LoginWindow(session_store=self.sessions,
                                 api_factory=self._api_factory,
                                 theme_name=self.theme_name, notice=notice)
            window.loggedIn.connect(self.on_logged_in)
            window.closed.connect(self._on_login_closed)
            window.apply_theme(self.theme_name)
            self._login = window
        elif notice:
            self._login.set_status(notice, "info")
        window = self._login
        window.show()
        window.raise_()
        window.activateWindow()
        window.focus_first()
        self.windowChanged.emit(SCOPE_LOGIN)
        return window

    def _close_login(self) -> None:
        window, self._login = self._login, None
        if window is not None:
            window.hide()
            window.deleteLater()

    def _on_login_closed(self) -> None:
        """用户自己把登录窗口关了（没登录）→ 整个程序退出。"""
        self._login = None
        if self._main is None:
            self.windowChanged.emit("")
            self._quit_app("关了登录窗口")

    def on_logged_in(self, session: Session) -> None:
        """登录窗口报喜：开主窗口（缓存已由登录窗口写好）。"""
        self.apply_saved_preferences()
        api = self._api_factory(session.server)
        api.token = session.token
        self._close_login()
        self.open_main(session, api)

    # ================================================================== 主窗口
    def open_main(self, session: Session, api: ApiClient, *, offline: bool = False):
        from .main_window import MainWindow
        self._session, self._api = session, api
        data_dir = default_data_dir()
        log = ChatLog(log_path_for(session, data_dir), cipher=self.cipher)   # 库按账号分开存
        if self._main_factory is not None:
            window = self._main_factory(session=session, api=api, log=log,
                                        settings=self._settings)
        else:
            window = MainWindow(session=session, api=api, log=log,
                                settings=self._settings)
        window.logoutRequested.connect(self.logout)
        window.closed.connect(self._on_main_closed)
        self._main = window
        window.show()
        window.raise_()
        window.activateWindow()
        if offline:
            window.toast.show_message(
                f"{session.host}无法连接，切换到离线模式", "warning", 4200)
        self.windowChanged.emit(SCOPE_MAIN)
        return window

    def logout(self) -> bool:
        """退出登录：清掉这个账号的缓存 → 关掉主窗口 → 回登录窗口。"""
        session = self._session
        if session is not None:
            self.sessions.forget(session.server, session.username)
        window, self._main = self._main, None
        self._session, self._api = None, None
        if window is not None:
            window.shutdown()          # 停轮询/关库（不等 deleteLater）
            window.hide()              # hide() 不触发 closeEvent，所以不会连带退出程序
            window.deleteLater()
        self.show_login(notice="已退出登录" if session is not None else "")
        return True

    def _on_main_closed(self) -> None:
        """用户点 ✕ 把主窗口关了 → 整个程序退出。

        为什么非要显式做这件事：为了登录窗口/主窗口交替，``quitOnLastWindowClosed``
        是关掉的（见 :func:`main`），于是"最后一个窗口关掉"不会让 QApplication 自己
        退出 —— 表现就是**窗口没了、进程还在资源管理器里占着**（长轮询线程、数据库
        连接、内存都还挂着）。登录窗口那条路有 ``_on_login_closed``，主窗口原来缺一个。

        退出登录走的是 ``logout()``（``hide()`` 不触发 ``closeEvent``），不会误伤。
        """
        self._main = None
        self._session, self._api = None, None
        if self._login is None:            # 屏幕上没有别的窗口了：收工
            self.windowChanged.emit("")
            self._quit_app("关了主窗口")

    # ================================================================== 收工
    def _quit_app(self, why: str) -> None:
        """退出事件循环；顺带装上"绝不留僵尸"的保险。

        正常情况 ``quit()`` 一调，事件循环立刻返回、``main()`` 返回、进程结束。
        但只要有嵌套事件循环（``QMessageBox.exec()`` 之类）或别的什么把 exec()
        拖着，``quit()`` 就退不出来 —— 表现就是**窗口没了、任务管理器里的 python
        还在**。所以这里再挂一个定时器兜底：到点还没退就强制结束进程。
        """
        self.app.quit()
        self._quit_reason = why
        if self._quit_watchdog_ms <= 0 or self._quit_armed:
            return
        self._quit_armed = True
        # 两参数的 singleShot（PyQt6 里带上下文对象的三参数重载 + lambda 会崩进程）
        QTimer.singleShot(self._quit_watchdog_ms, self._force_quit)

    def _force_quit(self) -> None:
        """定时器真触发了 = 事件循环没退出来。留一笔日志，然后强制结束进程。"""
        _note_forced_quit(self._quit_reason)
        try:
            cleanup_view_cache()
        except Exception:                            # noqa: BLE001 —— 都要走了
            pass
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:                        # noqa: BLE001
                pass
        os._exit(0)                                  # 不再等任何线程/析构


def _note_forced_quit(reason: str = "") -> None:
    """记一笔"是被强制结束的"（``<数据目录>/logs/quit.log``），排障时一眼看得见。

    日志写失败绝不能反过来把退出流程带崩 —— 整段包在 try 里。
    """
    try:
        import time
        folder = os.path.join(default_data_dir(), "logs")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, QUIT_LOG_NAME), "a", encoding="utf-8") as handle:
            handle.write("%s %s后 %d ms 事件循环仍未退出，已强制结束进程\n"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), reason or "关窗",
                            QUIT_WATCHDOG_MS))
    except Exception:                                # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    """程序入口：``python main.py``。

    ``quitOnLastWindowClosed`` 必须关掉：登录窗口和主窗口交替时会有"一个窗口都没有"
    的瞬间，默认行为会在那一刻把程序退出。退出改由"用户关掉登录窗口"（见
    :meth:`Launcher._on_login_closed`）显式触发。
    """
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(SETTINGS_APP)
    app.setOrganizationName(SETTINGS_ORG)
    app.setQuitOnLastWindowClosed(False)
    install_crash_log(default_crash_log_path(os.path.join(default_data_dir(), "logs")))
    launcher = Launcher(app, quit_watchdog_ms=QUIT_WATCHDOG_MS)
    app._mchat_launcher = launcher        # 别让启动器被回收（窗口切换全靠它）
    launcher.start()
    try:
        return app.exec()
    finally:
        # 退出时清掉"点开附件"时解密出来的临时明文（best effort）
        cleanup_view_cache()
