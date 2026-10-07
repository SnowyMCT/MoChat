"""关窗要**快**，退了就得**没** —— 用户报的那两个现象，钉在这儿。

用法：``.venv/Scripts/python.exe tests/test_shutdown.py``（也能 ``pytest tests/test_shutdown.py``）

用户原话：点 ✕ 之后窗口要等几秒才关（看着像程序出错）；关了软件资源管理器里还有
一堆 python 在跑。前者的根是**收消息那条线程平时正挂在 20 秒的长轮询里**，
关窗时去 join 它就只能等满超时（实测 3.0 秒起，掉线时更久）；后者是"没人保证
进程一定退"。这里四个用例分别钉住：

    abort()          —— 挂在服务器上的长轮询能被当场叫醒（关窗不卡的前提）
    transport.stop() —— 不等网络，几十毫秒收工，线程也收干净
    window.shutdown()—— 整个收尾（含关库/存草稿）不等网络，< 0.5 秒
    launcher         —— "到点强制结束进程"的保险只在真入口装、关窗时会上膛
    真进程里试一次   —— 事件循环真的退不出来时，保险**确实**把进程结束了（不是嘴上说说）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest                                                            # noqa: E402
from PyQt6.QtCore import QObject, QSettings, Qt, pyqtSignal              # noqa: E402
from PyQt6.QtWidgets import QApplication                                 # noqa: E402

from mchat.core.api import ApiClient, NetworkError                       # noqa: E402
from mchat.core.db import ChatLog                                        # noqa: E402
from mchat.core.net_transport import NetworkTransport                    # noqa: E402
from mchat.core.session import Session, SessionStore, log_path_for       # noqa: E402
from mchat.ui.main_window import MainWindow                              # noqa: E402
from server_fixture import running_server                                # noqa: E402

PW = "secret123"
# "关窗不能等网络"的量化判据。修之前是 3.0 秒起（join 到超时），留一点余量给慢机器。
CLOSE_BUDGET = 0.6


@pytest.fixture(scope="module")
def server():
    with running_server() as process:
        yield process


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for_condition(predicate, timeout=15.0, step=0.02) -> bool:
    """不依赖 Qt 的等待（传输层那条线程跟事件循环无关）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def pump(app, ms=120) -> None:
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, predicate, timeout=15.0, step=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(step)
    return bool(predicate())


def account(base_url: str, username: str):
    """注册一个新账号，返回 ``(带令牌的 ApiClient, Session)``。"""
    boot = ApiClient(base_url)
    payload = boot.register(username, PW, username)
    user = dict(payload.get("user") or {})
    session = Session(server=base_url, token=str(payload.get("token") or ""),
                      user_id=str(user.get("id") or ""),
                      username=str(user.get("username") or username),
                      display_name=str(user.get("display_name") or username))
    return ApiClient(session.server, session.token), session


# --------------------------------------------------------------------------- 1
def test_abort_wakes_a_hanging_long_poll(server):
    """长轮询挂在服务器上时，``abort()`` 必须**当场**把它弄醒（不是等超时）。

    这是"关窗不卡"的根：``stop()`` 就是靠它才敢 join。
    """
    api, _session = account(server.base_url, "sd_abort")
    outcome: dict = {}

    def worker():
        start = time.time()
        try:
            api.sync(0, timeout=20.0)
            outcome["error"] = None
        except NetworkError as error:
            outcome["error"] = error
        except BaseException as error:                       # noqa: BLE001
            outcome["error"] = error
        outcome["elapsed"] = time.time() - start

    thread = threading.Thread(target=worker, name="sd-long-poll", daemon=True)
    thread.start()
    try:
        assert wait_for_condition(lambda: api.live_requests() >= 1, timeout=10), \
            "长轮询没挂上去，这条用例就白测了"
        assert thread.is_alive()
        start = time.time()
        aborted = api.abort()
        thread.join(timeout=5)
        elapsed = time.time() - start
        assert aborted >= 1, "abort() 说一条都没掐断"
        assert not thread.is_alive(), "abort() 没把阻塞在长轮询里的线程叫醒"
        assert elapsed < 1.0, f"abort() 之后长轮询还等了 {elapsed:.2f} 秒才回来"
        assert outcome["elapsed"] < 2.0, outcome
        assert isinstance(outcome["error"], NetworkError), outcome
        assert api.live_requests() == 0
        # 掐完还得能用：关窗那条路会顺手掐一下，别把客户端本身弄坏了
        assert api.health().get("ok") is True
        assert isinstance(api.sync(0, timeout=1.0), dict)
    finally:
        thread.join(timeout=1)


# --------------------------------------------------------------------------- 2
def test_transport_stop_does_not_wait_for_the_network(server):
    """``NetworkTransport.stop()`` 不等那条长轮询（不然关窗就得卡几秒）。"""
    api, session = account(server.base_url, "sd_stop")
    transport = NetworkTransport(api, me_id=session.user_id, autostart=True)
    try:
        assert wait_for_condition(lambda: transport.online, timeout=20), transport.describe()
        assert wait_for_condition(lambda: api.live_requests() >= 1, timeout=5), \
            "长轮询没挂上，停起来当然快 —— 这条断言是前提，不是结论"
        start = time.time()
        transport.stop()
        elapsed = time.time() - start
        assert elapsed < CLOSE_BUDGET, f"stop() 等了 {elapsed:.2f} 秒（又在 join 长轮询？）"
        assert transport.is_running() is False
        assert api.live_requests() == 0
        assert not [t for t in threading.enumerate() if t.name.startswith("mchat-net")], \
            "后台线程没停干净"
    finally:
        transport.stop()


# --------------------------------------------------------------------------- 3
def test_window_shutdown_does_not_wait_for_the_network(server):
    """整个窗口收尾（停轮询/存草稿/关库）都不等网络：实测要 < 0.5 秒。"""
    app = _app()
    folder = tempfile.mkdtemp(prefix="mchat-shutdown-")
    settings = QSettings("MChatShutdown", "MChatShutdown")
    settings.clear()
    _api, session = account(server.base_url, "sd_window")
    client = ApiClient(session.server, session.token)
    log = ChatLog(log_path_for(session, folder))
    window = MainWindow(session=session, api=client, log=log, settings=settings)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    try:
        transport = window.transport
        assert wait_for(app, lambda: transport.online, timeout=20)
        assert wait_for(app, lambda: client.live_requests() >= 1, timeout=5), \
            "长轮询没挂上，关得再快也说明不了问题"
        pump(app, 200)

        start = time.time()
        window.shutdown()
        elapsed = time.time() - start
        assert elapsed < CLOSE_BUDGET, f"关窗收尾卡了 {elapsed:.2f} 秒（在等网络？）"
        assert transport.is_running() is False
        assert client.live_requests() == 0

        # 幂等：再调一次必须立刻返回（closeEvent 和 logout 两条路都会调）
        start = time.time()
        window.shutdown()
        assert time.time() - start < 0.2
    finally:
        window.hide()
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- 4
class _FakeMainWindow(QObject):
    """只测启动流程时的轻量替身（契约和真窗口一样：两个信号 + show/raise_）。"""

    logoutRequested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, *, session=None, api=None, log=None, settings=None):
        super().__init__()
        self.session, self.api, self.log, self.settings = session, api, log, settings

    def show(self) -> None:
        pass

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        pass


def test_quit_watchdog_is_armed_on_close_only_when_asked():
    """"到点强制结束进程"的保险：真入口装（``quit_watchdog_ms``），关窗时上膛。

    为什么要有它：用户在意的是"关了窗口进程就得没"。``quit()`` 正常路径是毫秒级
    返回，但只要有嵌套事件循环之类的把 ``exec()`` 拖着，窗口没了、任务管理器里
    那个 python 还在。装上这个定时器就再也不会留僵尸。

    这里用**很大的**超时（1 小时）做断言：既验"会上膛"，又不可能真在测试进程里触发。
    """
    from mchat.ui import launcher as launcher_module

    folder = tempfile.mkdtemp(prefix="mchat-quit-watchdog-")
    real_data_dir = launcher_module.default_data_dir
    launcher_module.default_data_dir = lambda: folder       # 别往真数据目录里写库
    app = _app()
    app.setQuitOnLastWindowClosed(False)
    try:
        for watchdog_ms, expected in ((0, False), (3600_000, True)):
            store = SessionStore(os.path.join(folder, "account.json"))
            launcher = launcher_module.Launcher(
                app, session_store=store,
                settings=QSettings("MChatShutdown", "MChatShutdown"),
                main_factory=lambda **kwargs: _FakeMainWindow(**kwargs),
                quit_watchdog_ms=watchdog_ms)
            session = Session(server="http://127.0.0.1:1", token="t", user_id="u1",
                              username="u", display_name="U")
            window = launcher.open_main(session, ApiClient("http://127.0.0.1:1", "t"))
            assert launcher._quit_armed is False, "还没关窗呢，保险不该上膛"
            window.closed.emit()                            # 用户点了 ✕
            assert launcher._quit_armed is expected, \
                f"watchdog_ms={watchdog_ms} 时上膛状态应该是 {expected}"

        # 保险真触发时的表现：留一笔日志（排障时一眼看得出"是被强制结束的"）
        assert not os.path.exists(os.path.join(folder, "logs", "quit.log"))
        launcher_module._note_forced_quit()
        quit_log = os.path.join(folder, "logs", "quit.log")
        assert os.path.isfile(quit_log)
        with open(quit_log, encoding="utf-8") as handle:
            assert "强制结束进程" in handle.read()
    finally:
        launcher_module.default_data_dir = real_data_dir
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- 5
_WATCHDOG_PROBE = '''
# 故意让事件循环**退不出来**（一个永不收手的 QTimer 把 exec() 拖着），
# 看"关窗兜底保险"会不会真的把进程结束掉 —— 判据是进程退出且没走到 exec 返回。
import os
import sys

sys.path.insert(0, r"{root}")
sys.path.insert(0, r"{tests}")

from PyQt6.QtCore import QObject, QSettings, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from mchat.core.api import ApiClient
from mchat.core.session import Session, SessionStore
from mchat.ui import launcher as launcher_module


class _Fake(QObject):
    logoutRequested = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, **kwargs):
        super().__init__()

    def show(self):
        pass

    def raise_(self):
        pass

    def activateWindow(self):
        pass


def main() -> int:
    folder = sys.argv[1]
    launcher_module.default_data_dir = lambda: folder        # 别写进真数据目录
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    store = SessionStore(os.path.join(folder, "account.json"))
    launcher = launcher_module.Launcher(
        app, session_store=store, settings=QSettings("MChatWatchdog", "MChatWatchdog"),
        main_factory=lambda **kwargs: _Fake(**kwargs), quit_watchdog_ms=800)
    session = Session(server="http://127.0.0.1:1", token="t", user_id="u1",
                      username="u", display_name="U")
    window = launcher.open_main(session, ApiClient("http://127.0.0.1:1", "t"))

    # 模拟"``quit()`` 没把事件循环退出来"（真出现过的样子：关窗时正好套着一层嵌套
    # 事件循环 —— 模态确认框之类）。判据是进程**有没有**被收掉，所以只在这儿动手脚，
    # 让 quit() 变成一句废话：正常路径本来就是毫秒级退出，根本轮不到兜底保险。
    app.quit = lambda *args, **kwargs: print("QUIT IGNORED", flush=True)

    QTimer.singleShot(200, window.closed.emit)               # 200ms 后"用户点了 ✕"
    code = app.exec()
    print("EXEC RETURNED", code, flush=True)                 # 这行**不该**出现
    return int(code)


raise SystemExit(main())
'''


def test_forced_quit_watchdog_really_ends_the_process(tmp_path=None):
    """事件循环真的退不出来时，保险会把进程**结束掉**（在真子进程里验）。

    这是"关了窗口进程就得没"的最后一道保证：只有 os._exit 这种事才救得回来
    —— 所以判据只能是"进程没了"，只能开子进程跑。
    """
    import subprocess

    folder = tempfile.mkdtemp(prefix="mchat-watchdog-")
    probe = os.path.join(folder, "watchdog_probe.py")
    with open(probe, "w", encoding="utf-8") as handle:
        handle.write(_WATCHDOG_PROBE.replace("{root}", ROOT)
                     .replace("{tests}", os.path.dirname(os.path.abspath(__file__))))
    try:
        started = time.time()
        done = subprocess.run([sys.executable, probe, folder], capture_output=True, text=True,
                              timeout=60, cwd=ROOT)
        elapsed = time.time() - started
        assert "QUIT IGNORED" in done.stdout, (done.stdout, done.stderr)
        assert "EXEC RETURNED" not in done.stdout, \
            f"事件循环居然自己退出来了，这条用例就没意义了：{done.stdout}"
        assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
        assert elapsed < 15, f"兜底保险拖了 {elapsed:.1f}s 才结束进程"
        quit_log = os.path.join(folder, "logs", "quit.log")
        assert os.path.isfile(quit_log), "强制结束该留一笔日志"
        with open(quit_log, encoding="utf-8") as handle:
            text = handle.read()
        assert "强制结束进程" in text and "关了主窗口" in text, text
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- 驱动
def _run_all() -> int:
    import test_shutdown as _self
    tests = [(name, getattr(_self, name)) for name in (
        "test_abort_wakes_a_hanging_long_poll",
        "test_transport_stop_does_not_wait_for_the_network",
        "test_window_shutdown_does_not_wait_for_the_network",
        "test_quit_watchdog_is_armed_on_close_only_when_asked",
        "test_forced_quit_watchdog_really_ends_the_process")]
    failed = 0
    with running_server() as server:
        for name, func in tests:
            try:
                if func.__code__.co_argcount:
                    func(server)
                else:
                    func()
                print(f"  ok    {name}")
            except Exception as exc:                         # noqa: BLE001
                failed += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
