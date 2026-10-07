"""登录 / 注册窗口 —— 打开程序时的第一屏。

像 Telegram 一样"登一次就够了"是怎么落地的
------------------------------------------
1. 启动时先看本地缓存（:mod:`mchat.core.session`）：有令牌就拿它去 ``/api/me`` 问一句
   "我还登录着吗"，问通了直接进主界面 —— **这一屏根本不会出现**；
2. 只有"没缓存 / 令牌已被吊销 / 用户点了退出登录"才显示这一屏；
3. 勾了「记住我」（默认勾上）登录成功就把令牌写进缓存，下次开程序直接进主界面。

界面上刻意只做几件事：服务器地址、用户名、密码、（注册时）昵称与确认密码、记住我、
测试连接。其余的错误都落在下方那行状态文字里，并且闪一下对应的输入框边框
（红色闪边框比弹窗轻，也不会打断输入）。

网络调用全部走 :class:`~mchat.ui.async_call.AsyncCall`：后台线程发请求、结果回到
主线程 —— 界面在这一秒钟里仍然能拖动，按钮变成灰的并显示"正在登录…"。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QToolButton, QVBoxLayout, QWidget)

from ..core.api import ApiClient, ApiError, NetworkError
from ..core.session import DEFAULT_SERVER, Session, SessionStore, normalize_server
from .async_call import AsyncCall
from .theme import Palette, build_qss, resolve_palette
from .widgets import (FloatingLabelInput, SegmentedControl, ToggleSwitch,
                      install_app_chrome)

__all__ = ["LoginWindow", "MODE_LOGIN", "MODE_REGISTER", "plain_http_warning",
           "host_of", "username_problem", "password_problem"]

MODE_LOGIN = "登录"
MODE_REGISTER = "注册"
CARD_WIDTH = 384
OUTER_MARGIN = 24               # 窗口边距（卡片外边留一圈）
FIELD_HEIGHT = 46               # 输入框/主按钮要的高度（输入框自身还会再要一点）
FIELD_GAP = 8                   # 输入框之间
STATUS_LINES = 2                # 状态文字预留几行（提醒/报错最多两行，多了就挤）
CHIP_MAX_WIDTH = 118            # "上次：xxx" 标签的宽度上限（和"测试连接"同一行）
SIZE_SLACK = 20                 # 量高度时的富余（见 _lock_size 的说明）
MIN_USERNAME = 3
MIN_PASSWORD = 6

# "本机"地址：这些走明文 HTTP 也没什么大不了（流量不出机器）
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "localhost.localdomain"}


def host_of(server: str) -> str:
    """从服务器地址里抠出主机名（``http://user@1.2.3.4:8765/x`` -> ``1.2.3.4``）。"""
    text = (server or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/")[0].split("@")[-1]
    if text.startswith("["):                      # IPv6 字面量
        return text[1:].split("]")[0]
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


def plain_http_warning(server: str) -> str:
    """明文 HTTP 且不是本机 → 返回一句警告；否则空串。

    为什么要在登录窗口就提醒：密码和 token 都是明文走 HTTP 的，链路上谁都看得见。
    用户敲 ``chat.example.com`` 时我们**替他补**了 ``http://``（那是方便），
    但"补出来的明文"不能就这么悄悄地把密码送出去 —— 至少得让他看见。
    """
    text = normalize_server(server)
    if not text or text.startswith("https://"):
        return ""
    host = host_of(text).lower()
    if host in LOCAL_HOSTS or not host:
        return ""
    return ("⚠ 明文 HTTP：密码和登录令牌在网络上明着传。"
            "公网请让服务端配好 https:// 再登录。")


def username_problem(username: str) -> str:
    """用户名规则（和服务端一致，**先在本地拦一道**：白等一趟网络很烦）。"""
    text = (username or "").strip()
    if not text:
        return "请填用户名"
    if not (MIN_USERNAME <= len(text) <= 24):
        return f"用户名要 {MIN_USERNAME}–24 位"
    if not all(char.isalnum() or char in "._-" for char in text):
        return "用户名只能用字母、数字、下划线、点或减号"
    return ""


def password_problem(password: str) -> str:
    if not password:
        return "请填密码"
    if len(password) < MIN_PASSWORD:
        return f"密码至少 {MIN_PASSWORD} 位"
    return ""


class LoginWindow(QWidget):
    """登录窗口。``loggedIn`` 携带 :class:`~mchat.core.session.Session` 抛给启动器。"""

    loggedIn = pyqtSignal(object)
    closed = pyqtSignal()          # 用户自己把这扇窗关了（没登录）—— 启动器据此退出程序

    def __init__(self, parent: QWidget | None = None, *, api_factory=None,
                 session_store: SessionStore | None = None, theme_name: str = "",
                 notice: str = ""):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle("MChat · 登录")
        # 尺寸在 _lock_size() 里**定死**（量最高的那种配置），这里不预设
        self._store = session_store if session_store is not None else SessionStore()
        self._api_factory = api_factory or (lambda url: ApiClient(url))
        # 注意：这里叫 _client 而不是 _api —— 本类还有一个 _api() 方法，
        # 同名实例属性会把它盖掉，调用时就成了 "NoneType object is not callable"（踩过）
        self._client: ApiClient | None = None
        self._theme_name = theme_name
        self._palette: Palette | None = None
        self._busy = False
        self._call = AsyncCall(self)
        # 信号连的是"活在主线程的 QObject 的绑定方法"，所以槽一定在主线程里跑
        self._call.finished.connect(self._on_done)
        self._call.failed.connect(self._on_failed)
        self._build_ui()
        self._lock_size()                     # 窗口尺寸定死（见 _lock_size 的说明）
        self.apply_theme(theme_name)          # 单独用它（不在启动器里）也要有皮肤
        self._prefill()
        if notice:
            self.set_status(notice, "info")

    # ================================================================== 搭建
    def _build_ui(self) -> None:
        self.title = QLabel("MChat", self)
        self.title.setObjectName("title")
        self.subtitle = QLabel("登录账号", self)
        self.subtitle.setObjectName("subtitle")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.mode_seg = SegmentedControl([MODE_LOGIN, MODE_REGISTER], height=36)
        self.mode_seg.valueChanged.connect(self._on_mode_changed)

        self.server_field = FloatingLabelInput("服务器地址", field_height=FIELD_HEIGHT)
        self.server_field.setToolTip("形如 http://1.2.3.4:8765；本机自测填 "
                                     "http://127.0.0.1:8765")
        self.username_field = FloatingLabelInput("用户名", field_height=FIELD_HEIGHT)
        self.username_field.setText("")
        # 昵称排在第 2 格（紧跟在用户名后面）：注册时它属于"这个账号是谁"，
        # 夹在密码和确认密码中间是纯添乱 —— 密码那两格必须挨着。
        self.display_field = FloatingLabelInput("昵称（可留空）", field_height=FIELD_HEIGHT)
        self.password_field = FloatingLabelInput("密码", password=True, field_height=FIELD_HEIGHT)
        self.confirm_field = FloatingLabelInput("确认密码", password=True, field_height=FIELD_HEIGHT)
        # 服务器默认要求邀请码（没显式配置时它自己生成一个打在启动日志里），
        # 客户端不要求填 —— 服务器说要，登录时那一行红字会告诉他 why。
        self.invite_field = FloatingLabelInput("邀请码（服务器要求时填）", field_height=FIELD_HEIGHT)
        # 从上到下的填写顺序：地址 → 账号（用户名、昵称）→ 密码（密码、确认）→ 邀请码
        self._fields = (self.server_field, self.username_field, self.display_field,
                        self.password_field, self.confirm_field, self.invite_field)
        # 回车往下走一格：**跳过当前模式藏起来的那几格** —— 登录模式没有昵称/确认密码，
        # 用户名按回车必须直接落进密码框（用 isHidden() 判，窗口还没显示时 isVisible()
        # 对谁都返回 False，会把"跳过"判错）。
        for field in self._fields[:-1]:
            field.submitted.connect(lambda f=field: self._focus_next(f))
        self.invite_field.submitted.connect(self.submit)
        # 服务器地址一变就检查是不是明文 HTTP（填本机地址时不吵）
        self.server_field.textChanged.connect(self._on_server_changed)

        self.remember_switch = ToggleSwitch(True)
        self.remember_label = QLabel("在这台电脑上记住我", self)
        self.remember_label.setObjectName("hint")

        self.submit_btn = QPushButton(MODE_LOGIN, self)
        self.submit_btn.setObjectName("primary")
        self.submit_btn.setFixedHeight(FIELD_HEIGHT)
        self.submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_btn.setDefault(True)
        self.submit_btn.clicked.connect(self.submit)

        self.test_btn = QToolButton(self)
        self.test_btn.setObjectName("ghost")
        self.test_btn.setText("测试连接")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.test_btn.clicked.connect(self.test_connection)

        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(6)
        # 包一层容器：注册模式下整排藏起来（那会儿"上次登录的账号"是噪声，
        # 也省得一堆小按钮把 384 宽的卡片撑变形）
        self.recent_box = QWidget(self)
        self.recent_box.setLayout(self.recent_row)
        self.recent_box.setVisible(False)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("loginStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 三行的高度先占住：状态文字出现/消失、一行的错误换成长提醒，布局都不许跳
        self.status_label.setMinimumHeight(self._status_height())

        card = QFrame(self)
        card.setObjectName("card")
        card.setFixedWidth(CARD_WIDTH)
        # 竖向**不参与拉伸**：卡片永远贴合自己的内容（字段少就矮、字段多就高），
        # 多出来的高度全给窗口背景 —— 这样字段间距才是恒定的，不会被布局抽走几像素。
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        box = QVBoxLayout(card)
        box.setContentsMargins(22, 18, 22, 16)
        box.setSpacing(FIELD_GAP)
        box.addWidget(self.title)
        box.addWidget(self.subtitle)
        box.addWidget(self.mode_seg)
        # 卡片**贴合内容**（不再被拉满窗口）：登录模式字段少，卡片自然矮一点、居中显示；
        # 注册模式字段全出来，卡片正好把窗口占满。间距因此恒定，不会出现"上面空一大块、
        # 下面挤成一条"的怪异节奏（那种是卡片被拉伸 + 弹簧分配不均造成的）。
        for field in self._fields:
            box.addWidget(field)
        remember = QHBoxLayout()
        remember.setSpacing(8)
        remember.addWidget(self.remember_switch)
        remember.addWidget(self.remember_label, 1)
        box.addLayout(remember)
        box.addWidget(self.submit_btn)
        box.addWidget(self.status_label)
        # 底部一行：左边"测试连接"、右边"上次登录的账号"（都算"辅助动作"，挤一行省高度）
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        bottom.addWidget(self.test_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.recent_box)
        box.addLayout(bottom)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(OUTER_MARGIN, OUTER_MARGIN, OUTER_MARGIN, OUTER_MARGIN)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)              # 卡片宽度定死 → 用两侧的弹簧把它居中
        row.addStretch(1)
        outer.addStretch(1)              # 上下也居中：窗口尺寸是定死的（见 _lock_size）
        outer.addLayout(row)
        outer.addStretch(1)

        # 无边框 + 自绘 ✕。必须在 _lock_size() 之前装：它会把根布局的上边距加高一条
        # 标题条，随后 sizeHint() 量出来的高度才是"含标题条"的那一份
        self.chrome = install_app_chrome(self, title=self.windowTitle())

        self._on_mode_changed(MODE_LOGIN)

    # ================================================================== 尺寸
    def _status_height(self) -> int:
        """状态文字预留两行的高度（一行错误换成长提醒时布局不许跳）。"""
        return self.status_label.fontMetrics().lineSpacing() * STATUS_LINES + 6

    def _apply_field_visibility(self, mode: str) -> None:
        """哪种模式显示哪几格 —— **只有这里说了算**。

        ``_lock_size()`` 量完尺寸后也调它还原，所以不会出现"量一次把字段全藏了"这种事
        （踩过：量之前用 ``isVisible()`` 记快照，而窗口还没 show，全是 False）。
        """
        registering = mode == MODE_REGISTER
        for field in (self.display_field, self.confirm_field, self.invite_field):
            field.setVisible(registering)
        # 注册模式不显示"上次登录的账号"：那会儿它是噪声，也省得挤版面
        self.recent_box.setVisible(not registering)

    def _lock_size(self) -> None:
        """把窗口尺寸**定死** —— 这个窗口不该能被缩放（用户明确要求）。

        量的是"最高的那种配置"：**六个输入框全摆出来 + 状态文字留两行 + 两个最近账号
        快捷标签**。两种模式因此共用同一个窗口尺寸，切换时既不跳、也拉不动。
        尺寸随文字缩放而变，所以每次换主题/字号都重新量一遍（见 ``apply_palette``）。
        """
        for field in self._fields:
            field.setVisible(True)                     # 先按最坏情况摆开
        self.recent_box.setVisible(True)
        self.status_label.setMinimumHeight(self._status_height())
        hint = self.sizeHint()
        # 留一点富余：QLabel 带 wordWrap 时 Qt 的 sizeHint 会算得偏乐观几像素，
        # 高出来的那几像素会从**字段间距**里扣（注册页六格会被压成 5px，肉眼看得出来）。
        self.setFixedSize(max(hint.width(), CARD_WIDTH + 2 * OUTER_MARGIN),
                          hint.height() + SIZE_SLACK)
        self._apply_field_visibility(self.mode())       # 量完按当前模式还原

    def is_fixed_size(self) -> bool:
        """窗口是不是被定死了（和设置对话框同一个约定，测试用）。"""
        return self.minimumSize() == self.maximumSize()

    def _prefill(self) -> None:
        """有缓存就把服务器地址和用户名填上（密码永远不填 —— 我们也没存过）。"""
        last = self._store.last()
        self.server_field.setText(last.server if last is not None else DEFAULT_SERVER)
        if last is not None:
            self.username_field.setText(last.username)
        sessions = self._store.sessions()
        while self.recent_row.count():
            item = self.recent_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for session in sessions[:2]:          # 最多两个：再多就把 384 宽的卡片挤变形了
            if session.server != self.server_field.text():
                continue
            btn = QToolButton(self)
            btn.setObjectName("ghost")
            name = session.username
            short = name if len(name) <= 10 else name[:10] + "…"
            btn.setText(f"上次：{short}")
            btn.setToolTip(f"用 {name} 登录（账号记在这台电脑上）")
            btn.setMaximumWidth(CHIP_MAX_WIDTH)   # 长用户名也只占这么多，不撑版面
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _=False, name=session.username:
                                self.prefill_account(name))
            btn.show()
            self.recent_row.insertWidget(self.recent_row.count() - 1, btn)

    def prefill_account(self, username: str) -> None:
        """点"上次登录的账号"：填上用户名，并把焦点丢到密码框。"""
        cached = self._store.get(self.server_field.text(), username)
        if cached is not None:
            self.server_field.setText(cached.server)
        self.username_field.setText(username)
        self.set_mode(MODE_LOGIN)
        self.password_field.focus_field()
        self.set_status(f"输入 {username} 的密码继续", "info")

    def _focus_next(self, field) -> None:
        """回车：焦点交给后面第一个**当前可见**的格子；后面没有了就提交。"""
        order = list(self._fields)
        for candidate in order[order.index(field) + 1:]:
            if not candidate.isHidden():
                candidate.focus_field()
                return
        self.submit()

    # ================================================================== 状态
    def mode(self) -> str:
        return self.mode_seg.value()

    def set_mode(self, mode: str) -> None:
        if mode in (MODE_LOGIN, MODE_REGISTER):
            self.mode_seg.set_index([MODE_LOGIN, MODE_REGISTER].index(mode),
                                    notify=False)

    def _on_mode_changed(self, mode: str) -> None:
        registering = mode == MODE_REGISTER
        self._apply_field_visibility(mode)
        self.submit_btn.setText(MODE_REGISTER if registering else MODE_LOGIN)
        self.subtitle.setText("创建一个新账号" if registering
                              else "登录账号")
        warning = plain_http_warning(self.server_field.text())
        self.set_status(warning if (registering and warning) else "", "info")

    def _on_server_changed(self, text: str) -> None:
        """地址一变就重算"明文 HTTP"这条提醒（登录窗口里最值得说的一句话）。"""
        if self._busy:
            return
        warning = plain_http_warning(text)
        if warning:
            self.set_status(warning, "error")
        elif self.status_kind() == "error":
            # 之前那条提醒就是它，清掉；别的错误消息（比如"密码不对"）不动
            self.set_status("", "info")

    def is_busy(self) -> bool:
        return self._busy

    def status_text(self) -> str:
        return self.status_label.text()

    def status_kind(self) -> str:
        return str(self.status_label.property("kind") or "")

    def set_status(self, text: str, kind: str = "info") -> None:
        """下方那行状态文字。``kind`` 是 error / success / info。"""
        self.status_label.setText(text)
        self.status_label.setProperty("kind", kind)
        colors = {"info": self._palette.muted if self._palette else "#8A8F98",
                  "error": self._palette.danger if self._palette else "#D64545",
                  "success": self._palette.success if self._palette else "#2E9E5B"}
        self.status_label.setStyleSheet(
            f"color: {colors.get(kind, colors['info'])}; font-size: 12px;")

    def _set_busy(self, busy: bool, note: str = "") -> None:
        self._busy = bool(busy)
        self.submit_btn.setEnabled(not busy)
        if busy:
            self.submit_btn.setText("正在连接…")
            self.set_status(note, "info")
        else:
            self.submit_btn.setText(MODE_REGISTER if self.mode() == MODE_REGISTER
                                    else MODE_LOGIN)

    # ================================================================== 动作
    def current_server(self) -> str:
        return normalize_server(self.server_field.text())

    def remember_enabled(self) -> bool:
        return bool(self.remember_switch.is_checked())

    def submit(self) -> bool:
        """按当前模式登录 / 注册。返回是否真的发起了请求（本地校验不过就 False）。"""
        if self._busy:
            return False
        server = self.current_server()
        if not server:
            self.set_status("请填服务器地址", "error")
            self.server_field.flash_error()
            return False
        username = self.username_field.text().strip()
        problem = username_problem(username)
        if problem:
            self.set_status(problem, "error")
            self.username_field.flash_error()
            return False
        password = self.password_field.text()
        problem = password_problem(password)
        if problem:
            self.set_status(problem, "error")
            self.password_field.flash_error()
            return False
        display = self.display_field.text().strip()
        if self.mode() == MODE_REGISTER:
            if self.confirm_field.text() != password:
                self.set_status("两次输入的密码不一样", "error")
                self.confirm_field.flash_error()
                return False
            invite = self.invite_field.text().strip()
            work = lambda: self._api().register(username, password, display, invite)  # noqa: E731
            note = f"正在注册 {username}…"
        else:
            work = lambda: self._api().login(username, password)             # noqa: E731
            note = f"正在登录 {username}…"
        if not self._call.run(work):
            return False
        self._set_busy(True, note)
        return True

    def test_connection(self) -> bool:
        """「测试连接」：只打 ``/api/health``，验一下地址通不通（不提交登录）。"""
        server = self.current_server()
        if not server:
            self.set_status("先填服务器地址", "error")
            self.server_field.flash_error()
            return False
        if self._busy:
            return False

        def work():
            info = ApiClient(server, timeout=6.0).health()
            return {"health": info}

        if not self._call.run(work):
            return False
        self._set_busy(True, f"正在探测 {server} …")
        return True

    def _api(self) -> ApiClient:
        """当前服务器地址对应的客户端（地址改了就地重建，令牌由调用方设置）。"""
        server = self.current_server()
        if self._client is None or self._client.base_url != server:
            self._client = self._api_factory(server)
        return self._client

    # ================================================================== 回调
    def _on_done(self, result) -> None:
        self._set_busy(False)
        if isinstance(result, dict) and "health" in result:
            info = result["health"] or {}
            self.set_status(
                f"连接正常 · {info.get('name', 'mchat-server')} "
                f"{info.get('version', '')} · {info.get('users', '?')} 个账号",
                "success")
            return
        payload = dict(result or {})
        token = str(payload.get("token") or "")
        user = dict(payload.get("user") or {})
        if not token or not user:
            self.set_status("服务端返回的数据不完整", "error")
            return
        server = self.current_server()
        session = Session(
            server=server, token=token, user_id=str(user.get("id") or ""),
            username=str(user.get("username") or self.username_field.text().strip()),
            display_name=str(user.get("display_name") or ""),
            remember=self.remember_enabled())
        self._store.save(session)
        self.password_field.setText("")            # 密码用完就从内存里抹掉
        self.set_status(f"欢迎回来，{session.name}", "success")
        self.loggedIn.emit(session)

    def _on_failed(self, error) -> None:
        self._set_busy(False)
        if isinstance(error, ApiError):
            self.set_status(error.friendly, "error")
            if error.code in ("bad_credentials",):
                self.password_field.flash_error()
                self.password_field.focus_field()
            elif error.code in ("username_taken", "invalid_username"):
                self.username_field.flash_error()
            elif error.code == "weak_password":
                self.password_field.flash_error()
            elif error.code == "invalid_invite":
                # 注册要邀请码：把光标直接丢到那一格，别让他猜该填哪儿
                self.invite_field.flash_error()
                self.invite_field.focus_field()
            elif error.code == "too_many_requests":
                self.password_field.flash_error()
        elif isinstance(error, NetworkError):
            self.set_status(f"{error.friendly}（请检查地址）", "error")
            self.server_field.flash_error()
        else:
            self.set_status(f"出错了：{type(error).__name__}: {error}", "error")

    # ================================================================== 皮肤
    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        for field in (self.server_field, self.username_field, self.password_field,
                      self.display_field, self.confirm_field, self.invite_field):
            field.apply_palette(palette)
        self.mode_seg.apply_palette(palette)
        self.remember_switch.apply_palette(palette)
        self.chrome.apply_palette(palette)
        self.remember_label.setStyleSheet(f"color: {palette.muted};")
        self.subtitle.setStyleSheet(f"color: {palette.muted};")
        # 状态文字的颜色由 kind 决定：重放一次当前状态，把颜色换到新调色板上
        self.set_status(self.status_label.text(), self.status_kind() or "info")
        self._lock_size()        # 字号可能变了（设置里的文字大小）→ 重新量一次尺寸

    def apply_theme(self, name: str) -> None:
        self._theme_name = name
        self.apply_palette(resolve_palette(name))

    def focus_first(self) -> None:
        """打开这一屏时把光标放到最该填的那一格（用户名没填就放用户名）。"""
        QTimer.singleShot(0, self.username_field.focus_field
                          if not self.username_field.text().strip()
                          else self.password_field.focus_field)

    def set_invite(self, code: str) -> None:
        """把邀请码填进去（测试和"管理员给我码了我直接粘"都用得上）。"""
        self.invite_field.setText(code or "")

    def closeEvent(self, event) -> None:                 # noqa: N802
        super().closeEvent(event)
        self.closed.emit()
