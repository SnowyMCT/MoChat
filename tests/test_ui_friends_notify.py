"""好友界面 + 桌面通知的端到端回归。

前四个代理把功能写完了却没来得及写测试 —— 未测的接线正是最容易静默坏掉的地方，
所以这里把用户要的三件事钉住：

    加好友需同意 / 删除好友  → 好友窗口能开、能复用、数量对得上（服务端权威）
    被门禁拦下的消息          → 本地标成 blocked（**不重发**）并给出明确文案
    收到消息的桌面提示        → 别人发的、我又没在看 → 弹通知；自己发的不弹；开关关掉不弹

窗口用真服务端 + 真 MainWindow（``WA_DontShowOnScreen``），和 test_server.py 一个路子。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest                                                            # noqa: E402
from PyQt6.QtCore import QSettings, Qt                                   # noqa: E402
from PyQt6.QtWidgets import QApplication                                 # noqa: E402

from mchat.core.api import ApiClient                                     # noqa: E402
from mchat.core.db import ChatLog                                        # noqa: E402
from mchat.core.session import Session, log_path_for                     # noqa: E402
from mchat.ui.friends_dialog import BLOCKED_NOTICE, FriendsDialog        # noqa: E402
from mchat.ui.main_window import MainWindow                              # noqa: E402
from server_fixture import make_friends, running_server                  # noqa: E402

PW = "secret123"


@pytest.fixture(scope="module")
def server():
    with running_server() as process:
        yield process


def pump(app, ms=120):
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, predicate, timeout=15.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(step)
    return bool(predicate())


def register(base_url, username, display=""):
    api = ApiClient(base_url)
    payload = api.register(username, PW, display or username)
    api.token = str(payload.get("token") or "")
    return api


class Recorder:
    """顶掉真的 Notifier：只记下"弹了什么"，不起托盘、不出声。

    通知本身是好坏都不会抛异常的，所以这里要验证的是**调用与否**，
    而不是它内部怎么弹（那部分由 tests/test_notify.py 负责）。
    """

    def __init__(self):
        self.calls = []

    def message(self, title, body):
        self.calls.append((title, body))

    def set_enabled(self, value):
        pass

    def set_sound_enabled(self, value):
        pass

    def enabled(self):
        return True

    def sound_enabled(self):
        return True

    def available(self):
        return False

    def stop(self):
        pass


class UiCase:
    def __init__(self, base_url):
        self.base_url = base_url
        self.folder = tempfile.mkdtemp(prefix="mchat-fr-")
        self.settings = QSettings("MChatFriendsE2E", "MChatFriendsE2E")
        self.settings.clear()
        self.app = QApplication.instance() or QApplication([])
        self.windows = []

    def session(self, username, display=""):
        api = ApiClient(self.base_url)
        payload = api.login(username, PW)
        user = dict(payload.get("user") or {})
        return Session(server=self.base_url, token=str(payload.get("token") or ""),
                       user_id=str(user.get("id") or ""),
                       username=str(user.get("username") or username),
                       display_name=str(user.get("display_name") or display))

    def window(self, session):
        log = ChatLog(log_path_for(session, self.folder))
        window = MainWindow(session=session, api=ApiClient(session.server, session.token),
                            log=log, settings=self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        pump(self.app, 120)
        window._notifier = Recorder()       # 换成记录器（功能等价，不碰托盘）
        self.windows.append(window)
        return window

    def close(self):
        for window in self.windows:
            try:
                window.shutdown()
                window.hide()
            except RuntimeError:
                pass
        self.app.processEvents()
        shutil.rmtree(self.folder, ignore_errors=True)


def _open_pair(case, suffix, friends=True):
    """建两个账号、两个窗口、一条会话；默认先把两人变成好友。"""
    alice = register(case.base_url, "nw_alice_" + suffix, "爱丽丝")
    bob = register(case.base_url, "nw_bob_" + suffix, "鲍勃")
    if friends:
        make_friends(alice, bob)
    return alice, bob


# --------------------------------------------------------------------- 桌面通知
def test_incoming_message_notifies_but_own_message_does_not(server):
    """别人发来的、我又没在看 → 弹通知；自己发的绝不自扰。"""
    case = UiCase(server.base_url)
    try:
        alice_api, _ = _open_pair(case, "n1")
        alice = case.window(case.session("nw_alice_n1", "爱丽丝"))
        bob = case.window(case.session("nw_bob_n1", "鲍勃"))
        cid = alice_api.create_direct("nw_bob_n1")["id"]
        alice.load_conversations()
        bob.load_conversations()
        assert wait_for(case.app, lambda: bool(alice.store.get(cid))
                        and bool(bob.store.get(cid)), timeout=20)

        alice.select_conversation(cid)
        alice._notifier.calls.clear()
        bob._notifier.calls.clear()
        assert alice.send_message("通知测试") is True
        assert wait_for(case.app, lambda: any(m.text == "通知测试"
                                              for m in bob.store.get(cid).messages),
                        timeout=20), "bob 没收到消息"
        pump(case.app, 200)

        assert alice._notifier.calls == [], "自己发的消息不该通知自己"
        assert bob._notifier.calls, "别人发来的消息应该弹桌面通知"
        title, body = bob._notifier.calls[-1]
        assert "通知测试" in body, bob._notifier.calls
        assert title in ("爱丽丝", "鲍勃"), title     # 单聊用对方显示名当标题
    finally:
        case.close()


def test_notification_switch_off_silences_it(server):
    """设置里的「桌面通知」关掉之后，收到消息就不再打扰。"""
    case = UiCase(server.base_url)
    try:
        alice_api, _ = _open_pair(case, "n2")
        alice = case.window(case.session("nw_alice_n2", "爱丽丝"))
        bob = case.window(case.session("nw_bob_n2", "鲍勃"))
        cid = alice_api.create_direct("nw_bob_n2")["id"]
        alice.load_conversations()
        bob.load_conversations()
        assert wait_for(case.app, lambda: bool(bob.store.get(cid)), timeout=20)

        bob.set_notifications(False)
        assert bob.notifications_enabled() is False
        bob._notifier.calls.clear()
        alice.select_conversation(cid)
        assert alice.send_message("关掉通知后这条") is True
        assert wait_for(case.app, lambda: any(m.text == "关掉通知后这条"
                                              for m in bob.store.get(cid).messages),
                        timeout=20), "消息本身仍然要能收到"
        pump(case.app, 200)
        assert bob._notifier.calls == [], "开关关掉了还弹通知"
    finally:
        case.close()


# --------------------------------------------------------------------- 好友门禁
def test_message_to_non_friend_is_marked_blocked_with_notice(server):
    """给非好友发消息：本地那条标成 blocked（不再转圈、不重发）+ 明确文案。"""
    case = UiCase(server.base_url)
    try:
        alice_api, _ = _open_pair(case, "b1", friends=False)
        alice = case.window(case.session("nw_alice_b1", "爱丽丝"))
        cid = alice_api.create_direct("nw_bob_b1")["id"]
        alice.load_conversations()
        assert wait_for(case.app, lambda: bool(alice.store.get(cid)), timeout=20)
        alice.select_conversation(cid)

        toasts = []
        alice.toast.show_message = lambda *args, **kwargs: toasts.append(args)

        assert alice.send_message("你好，加个好友") is True
        assert wait_for(case.app, lambda: any(m.blocked for m in alice.store.get(cid).messages),
                        timeout=20), [m.status for m in alice.store.get(cid).messages]

        blocked = [m for m in alice.store.get(cid).messages if m.blocked]
        assert blocked and blocked[0].text == "你好，加个好友"
        assert blocked[0].status == "blocked"
        assert toasts, "要给用户一句明确提示"
        assert BLOCKED_NOTICE in [args[0] for args in toasts], toasts
        # 自己发起的申请会被服务端回推到自己这边：绝不能弹成"爱丽丝 申请加你为好友"
        assert not any("申请加你为好友" in str(args[0]) for args in toasts), toasts
        # 服务端那边一条都没落库：申请方自己翻历史也是空的
        assert [m for m in alice_api.messages(cid)["messages"] if m["text"] == "你好，加个好友"] == []
    finally:
        case.close()


# --------------------------------------------------------------------- 好友界面
def test_friends_dialog_opens_reuses_and_counts(server):
    """好友窗口：能从侧栏打开、复用同一实例、好友数随服务端变化。"""
    case = UiCase(server.base_url)
    try:
        alice_api, bob_api = _open_pair(case, "f1", friends=False)
        alice = case.window(case.session("nw_alice_f1", "爱丽丝"))

        # 侧栏入口是真的接着的：**还没有窗口时**发这个信号，就该把窗口开出来。
        # （不要用"先把 _friends_dialog 置空再造一个"的写法 —— 那会把一个还开着的
        #   窗口的 Python 引用丢掉，GC 撞上可见控件会直接把进程搞崩，是测试自己制造的坑。）
        alice.sidebar.friendsRequested.emit()
        pump(case.app, 150)
        dialog = alice.friends_dialog()
        assert isinstance(dialog, FriendsDialog), "侧栏「好友」入口没接上"
        assert alice.open_friends() is dialog, "必须复用同一个实例"
        assert not dialog.isModal(), "好友窗口不该挡住聊天"
        assert dialog.friend_count() == 0
        assert dialog.incoming_count() == 0

        # 让对方发一条 -> 变成我的待处理申请 -> 界面能看到
        # 注意：refresh_friends() 每次都会起一个后台线程去拉接口，所以**不能**把它塞进
        # wait_for 的判定函数里（那样 50ms 一次、线程爆炸直接把进程搞崩）——
        # 这里等事件驱动，必要时才补一次。
        bob_api.create_direct("nw_alice_f1")
        bob_api.send_message(bob_api.conversations()[0]["id"], "0" * 32, "我们加个好友")
        seen = False
        for _ in range(20):
            if alice.friends_dialog().incoming_count() == 1:
                seen = True
                break
            alice.refresh_friends()
            pump(case.app, 400)
        assert seen, "对方的申请没能出现在好友窗口里：%s" % alice.friends_dialog().incoming_count()

        # 同意之后：进好友列表，申请清空
        alice_api.friend_accept("nw_bob_f1")
        accepted = False
        for _ in range(20):
            alice.refresh_friends()
            pump(case.app, 400)
            if alice.friends_dialog().friend_count() == 1:
                accepted = True
                break
        assert accepted, alice.friends_dialog().friend_count()
        assert alice.friends_dialog().incoming_count() == 0
    finally:
        case.close()


def test_settings_has_notification_toggles(server):
    """设置里确实多了「消息通知」那张卡片，且默认两个开关都是开。"""
    case = UiCase(server.base_url)
    try:
        _open_pair(case, "s1")
        alice = case.window(case.session("nw_alice_s1", "爱丽丝"))
        assert alice.notifications_enabled() is True
        assert alice.sound_enabled() is True
        dialog = alice.open_settings()
        assert dialog.card_count() == 5, dialog.card_count()
        assert dialog.notifications_enabled() is True
        assert dialog.sound_enabled() is True

        alice.set_notifications(False)
        alice.set_sound_enabled(False)
        assert alice.notifications_enabled() is False and alice.sound_enabled() is False
        # 落到设置里了（重开一个窗口也该记得）
        assert str(alice._settings.value("notify")).lower() == "false"
    finally:
        case.close()
