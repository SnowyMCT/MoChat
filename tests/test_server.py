"""端到端：**起一个真的服务端**，让客户端（连界面一起）真的连上去聊天。

用法：``.venv/Scripts/python.exe tests/test_server.py``（要跑十几秒）

这个文件回答的是"它到底能不能用"：
    账号     —— 注册/重名/弱密码/登录/错密码/令牌失效/退出登录
    两个账号 —— 单聊建会话、发消息、对方长轮询收到、未读、已读回执、正在输入
    附件     —— 上传 → 对方能下回来（字节一致）
    群聊     —— 建群、群里发消息、非成员看不到
    多设备   —— 同一个账号的两条连接都能收到自己发的消息
    界面     —— 两个真实的 MainWindow（各自登录一个账号）互发消息，
               另一个窗口真的收得到、会话列表/未读/已读都跟着变
    在线状态 —— 对方上线/下线要真的画到界面（小绿点 + 聊天头部第二行）
    退出     —— 点 ✕ 关掉主窗口，整个进程真的退出（不能在任务管理器里继续挂着）
    登录     —— 登录窗口 → 写本地缓存 → 下次启动自动登录（不再出现登录界面）
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

from PyQt6.QtCore import QSettings, Qt                               # noqa: E402
from PyQt6.QtWidgets import QApplication                              # noqa: E402

from mchat.core.api import ApiClient, ApiError                        # noqa: E402
from mchat.core.db import ChatLog                                     # noqa: E402
from mchat.core.models import ME_ID                                    # noqa: E402
from mchat.core.session import Session, SessionStore, log_path_for    # noqa: E402
from mchat.ui.main_window import MainWindow                           # noqa: E402
from server_fixture import make_friends, running_server               # noqa: E402

try:                                                                  # noqa: E402
    import pytest
except ImportError:              # 直接用 python 跑本文件时不需要 pytest
    pytest = None

if pytest is not None:
    @pytest.fixture(scope="module")
    def server():
        """pytest 用它注入服务端；`python tests/test_server.py` 走文件末尾的 ``_run_all()``。

        两种跑法必须都能用：``pytest tests`` 是仓库的规范命令，而逐套直接跑
        （不依赖 pytest）是这套测试一贯的兜底方式。
        """
        with running_server() as process:
            yield process


ALICE_PW = "secret123"
BOB_PW = "secret456"
# 一张真正合法的 1×1 PNG：附件测试要走到"气泡里画缩略图"，喂假字节会让 Qt 刷 libpng 报错
PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


# --------------------------------------------------------------------------- 工具
def pump(app, ms=120):
    """推进事件循环（动画/异步回调都靠它）。"""
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app, predicate, timeout=15.0, step=0.05):
    """一边转事件循环一边等条件成立（不靠"睡固定时长"）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(step)
    return bool(predicate())


def prime(api: ApiClient) -> int:
    """先把事件游标"落到现在"。协议里 cursor=0 表示"从现在起"，所以想收到后面发生的事件，
    必须先同步一次拿到当前游标（真客户端也是这么做的：连上就长轮询一次）。"""
    return int(api.sync(0, timeout=1).get("cursor") or 0)


def register(base_url: str, username: str, password: str, display: str = "") -> ApiClient:
    api = ApiClient(base_url)
    payload = api.register(username, password, display or username)
    api.token = str(payload.get("token") or "")
    return api


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


class _UiCase:
    """一次性把两个账号 + 两个界面窗口准备好，用完收干净。"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.folder = tempfile.mkdtemp(prefix="mchat-e2e-")
        self.settings = QSettings("MChatE2E", "MChatE2E")
        self.settings.clear()
        self.app = _app()
        self.windows = []

    def session(self, username: str, password: str, display: str) -> Session:
        api = ApiClient(self.base_url)
        payload = api.login(username, password)
        user = dict(payload.get("user") or {})
        return Session(server=self.base_url, token=str(payload.get("token") or ""),
                       user_id=str(user.get("id") or ""),
                       username=str(user.get("username") or username),
                       display_name=str(user.get("display_name") or display))

    def window(self, session: Session) -> MainWindow:
        log = ChatLog(log_path_for(session, self.folder))
        api = ApiClient(session.server, session.token)
        window = MainWindow(session=session, api=api, log=log, settings=self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        pump(self.app, 120)
        self.windows.append(window)
        return window

    def close(self) -> None:
        for window in self.windows:
            try:
                window.shutdown()
                window.hide()
            except RuntimeError:
                pass
        self.app.processEvents()
        shutil.rmtree(self.folder, ignore_errors=True)


# --------------------------------------------------------------------------- 账号
def test_account_flow_over_http(server):
    api = ApiClient(server.base_url)
    health = api.health()
    assert health.get("ok") is True and health.get("name") == "mchat-server"

    payload = api.register("alice", ALICE_PW, "爱丽丝")
    token = payload.get("token")
    assert token and payload["user"]["username"] == "alice"
    api.token = token
    assert api.me()["user"]["display_name"] == "爱丽丝"

    # 重名 / 弱密码 / 非法用户名：三种错都要能明确区分（界面据此提示）
    try:
        ApiClient(server.base_url).register("alice", BOB_PW)
    except ApiError as error:
        assert error.code == "username_taken", error.code
    else:
        raise AssertionError("重名应该被拒")
    for username, password, code in (("bo", ALICE_PW, "invalid_username"),
                                     ("bob", "123", "weak_password"),
                                     ("bob bad", ALICE_PW, "invalid_username")):
        try:
            ApiClient(server.base_url).register(username, password)
        except ApiError as error:
            assert error.code == code, (username, error.code)
        else:
            raise AssertionError(f"{username} 应该被拒")
    try:
        ApiClient(server.base_url).login("alice", "错的密码")
    except ApiError as error:
        assert error.code == "bad_credentials" and error.friendly == "用户名或密码不对"
    else:
        raise AssertionError("错密码应该被拒")

    # 令牌失效：退出登录之后这个令牌就不认了（界面据此退回登录窗）
    api.logout()
    try:
        api.me()
    except ApiError as error:
        assert error.is_auth and error.code == "unauthorized"
    else:
        raise AssertionError("退出登录之后 /api/me 应该 401")


# --------------------------------------------------------------------------- 两个账号
def test_two_accounts_chat(server):
    alice = register(server.base_url, "chat_alice", ALICE_PW, "爱丽丝")
    bob = register(server.base_url, "chat_bob", BOB_PW, "鲍勃")
    make_friends(alice, bob)                  # 单聊要能发消息，两人得先是好友

    assert [user["username"] for user in bob.search_users("chat_al")] == ["chat_alice"]
    conversation = alice.create_direct("chat_bob")
    conversation_id = conversation["id"]
    assert conversation["kind"] == "direct"
    assert alice.create_direct("chat_bob")["id"] == conversation_id, "再建一次该拿到同一个会话"

    # alice 发一条，bob 用长轮询收（这一步串起了发送 → 事件 → 推送整条链）
    cursor = prime(bob)
    alice.send_message(conversation_id, "a" * 32, "在吗")
    deadline = time.time() + 15
    events = []
    while time.time() < deadline and not events:
        data = bob.sync(cursor, timeout=5)
        cursor = int(data.get("cursor") or cursor)
        events = [event for event in data.get("events") or []
                  if event.get("kind") == "message"]
    assert events, "bob 应该长轮询收到 alice 的消息"
    message = events[0]["message"]
    assert message["text"] == "在吗" and message["sender_name"] == "爱丽丝"

    # 未读 -> 读过 -> 对方收到已读回执
    conversations = {item["id"]: item for item in bob.conversations()}
    assert conversations[conversation_id]["unread"] == 1
    alice_cursor = prime(alice)
    bob.mark_read(conversation_id, time.time())
    assert {item["id"]: item for item in bob.conversations()}[conversation_id]["unread"] == 0
    deadline = time.time() + 15
    read_events = []
    while time.time() < deadline and not read_events:
        data = alice.sync(alice_cursor, timeout=5)
        alice_cursor = int(data.get("cursor") or alice_cursor)
        read_events = [event for event in data.get("events") or []
                       if event.get("kind") == "read"]
    assert read_events, "alice 应该收到「对方已读」回执"

    # 正在输入：节流由客户端做，服务端只负责转发
    bob.typing(conversation_id, True)
    deadline = time.time() + 15
    typing = []
    while time.time() < deadline and not typing:
        data = alice.sync(alice_cursor, timeout=5)
        alice_cursor = int(data.get("cursor") or alice_cursor)
        typing = [event for event in data.get("events") or []
                  if event.get("kind") == "typing"]
    assert typing and typing[0]["user_id"] == bob.me()["user"]["id"]

    # 幂等：同一个 client_id 重发两次只落一条
    same_id = "d" * 32
    first = alice.send_message(conversation_id, same_id, "重发测试")
    second = alice.send_message(conversation_id, same_id, "重发测试")
    assert first["id"] == second["id"] == same_id
    texts = [item["id"] for item in
             alice.messages(conversation_id, limit=50)["messages"]]
    assert texts.count(same_id) == 1, "同一条消息不该变成两条"

    # 历史分页（先多发几条，库里才有得翻）
    for index in range(4):
        alice.send_message(conversation_id, f"{index:032d}", f"补的第 {index} 条")
    history = bob.messages(conversation_id, limit=2)
    assert len(history["messages"]) <= 2
    assert history["has_more"] is True
    older = bob.messages(conversation_id, limit=2,
                         before=history["messages"][0]["ts"])
    assert all(item["ts"] < history["messages"][0]["ts"] for item in older["messages"])

    # 群聊 + 非成员访问
    carol = register(server.base_url, "chat_carol", "secret789", "卡罗尔")
    group = alice.create_group("评审组", ["chat_bob", "chat_carol"])
    assert group["kind"] == "group" and len(group["members"]) == 3
    alice.send_message(group["id"], "g" * 32, "群里第一条")
    assert len(bob.messages(group["id"])["messages"]) == 1
    carol_id = carol.me()["user"]["id"]
    assert [user["id"] for user in bob.search_users("chat_carol")] == [carol_id]
    try:
        carol.messages("c_不存在的会话")
    except ApiError as error:
        assert error.code in ("not_found", "not_member"), error.code

    # 附件：alice 上传 → bob 下回来（字节一致）
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(bytes(range(256)) * 4)
        path = handle.name
    try:
        info = alice.upload(path)
        assert info["size"] == 1024
        alice.send_message(conversation_id, "e" * 32, "给你一张图",
                           attachment_id=info["id"])
        messages = bob.messages(conversation_id, limit=10)["messages"]
        with_file = [item for item in messages if item.get("attachment")]
        assert with_file, "bob 应该看到一条带附件的消息"
        assert with_file[-1]["attachment"]["id"] == info["id"]
        assert with_file[-1]["attachment"]["name"].endswith(".png")
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "down.png")
            bob.download(info["id"], dest)
            assert open(dest, "rb").read() == open(path, "rb").read()
    finally:
        os.remove(path)


# --------------------------------------------------------------------------- 置顶
def test_pin_for_both_reaches_the_other_account(server):
    """"双方置顶"必须过服务端：对方要收到事件，而且两边 /api/conversations 里都看得到。"""
    alice = register(server.base_url, "pin_alice", ALICE_PW, "爱丽丝")
    bob = register(server.base_url, "pin_bob", BOB_PW, "鲍勃")
    carol = register(server.base_url, "pin_carol", "secret789", "卡罗尔")
    make_friends(alice, bob)                  # 置顶那条消息得先真的发出去
    cid = alice.create_direct("pin_bob")["id"]
    alice.send_message(cid, "p" * 32, "这条要置顶")
    message_id = alice.messages(cid)["messages"][-1]["id"]

    cursor = prime(bob)
    alice.pin_message(cid, message_id, True)

    events = []
    deadline = time.time() + 15
    while time.time() < deadline and not events:
        data = bob.sync(cursor, timeout=5)
        cursor = int(data.get("cursor") or cursor)
        events = [event for event in data.get("events") or []
                  if event.get("kind") == "pin"]
    assert events, "bob 应该收到置顶事件"
    event = events[0]
    assert event["pinned"] is True and event["pinned_by_name"] == "爱丽丝"
    assert event["message_id"] == message_id
    assert event["message"]["text"] == "这条要置顶", "事件要带被置顶那条的快照"

    # 两边拉会话列表都能看到这条"双方置顶"
    for api, who in ((alice, "alice"), (bob, "bob")):
        pins = {item["id"]: item["pins"] for item in api.conversations()}[cid]
        assert len(pins) == 1, (who, pins)
        assert pins[0]["message_id"] == message_id and pins[0]["for_both"] is True
        assert pins[0]["message"]["text"] == "这条要置顶"

    # 取消置顶：同样要推给对方
    alice.pin_message(cid, message_id, False)
    deadline = time.time() + 15
    unpinned = []
    while time.time() < deadline and not unpinned:
        data = bob.sync(cursor, timeout=5)
        cursor = int(data.get("cursor") or cursor)
        unpinned = [event for event in data.get("events") or []
                    if event.get("kind") == "pin"]
    assert unpinned and unpinned[0]["pinned"] is False, unpinned
    assert {item["id"]: item["pins"] for item in bob.conversations()}[cid] == []

    # 边界：非成员不能置顶；跨会话的消息也不能
    try:
        carol.pin_message(cid, message_id, True)
    except ApiError as error:
        assert error.code in ("not_member", "not_found"), error.code
    else:
        raise AssertionError("非成员不该能置顶")
    other = alice.create_direct("pin_carol")["id"]
    try:
        alice.pin_message(other, message_id, True)
    except ApiError as error:
        assert error.code == "not_found", error.code
    else:
        raise AssertionError("把别的会话里的消息置顶到这个会话应该被拒")


def _sync_until(api, cursor, kind, timeout=15.0, count=1):
    """一直长轮询，直到收到 ``count`` 条 ``kind`` 的事件（返回 ``(事件, 新游标)``）。"""
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline and len(events) < count:
        data = api.sync(cursor, timeout=5)
        cursor = int(data.get("cursor") or cursor)
        events.extend(event for event in (data.get("events") or [])
                      if event.get("kind") == kind)
    return events, cursor


def test_reactions_reach_the_other_account(server):
    """表情回应：计数由服务端算，两个人点同一个表情是 2 而不是各算各的。"""
    alice = register(server.base_url, "re_alice", ALICE_PW, "爱丽丝")
    bob = register(server.base_url, "re_bob", BOB_PW, "鲍勃")
    register(server.base_url, "re_carol", "secret789", "卡罗尔")
    make_friends(alice, bob)                  # 回应那条消息得先真的发出去
    cid = alice.create_direct("re_bob")["id"]
    alice.send_message(cid, "r" * 32, "这条有人回应吗")
    message_id = alice.messages(cid)["messages"][-1]["id"]
    cursor = prime(alice)
    prime(bob)

    result = bob.set_reaction(cid, message_id, "👍", True)
    assert result["added"] is True
    assert result["reactions"] == [{"emoji": "👍", "count": 1, "mine": True}], result

    events, cursor = _sync_until(alice, cursor, "reaction")
    assert events, "alice 应该收到回应事件"
    event = events[-1]
    assert event["emoji"] == "👍" and event["by_name"] == "鲍勃"
    assert event["reactions"] == [{"emoji": "👍", "count": 1, "mine": False}], \
        "同一条消息，alice 看到的 mine 必须是 False"

    # 两个人点同一个表情 → 服务端算 2（客户端绝不自己加一）
    mine_side = alice.set_reaction(cid, message_id, "👍", True)["reactions"]
    assert mine_side == [{"emoji": "👍", "count": 2, "mine": True}], mine_side
    listed = [m for m in bob.messages(cid)["messages"] if m["id"] == message_id][0]
    assert listed["reactions"] == [{"emoji": "👍", "count": 2, "mine": True}], listed

    # 撤掉 → 计数回落
    assert bob.set_reaction(cid, message_id, "👍", False)["reactions"] == \
        [{"emoji": "👍", "count": 1, "mine": False}]

    # 边界：非成员点不了；emoji 不合法直接拒
    try:
        ApiClient(server.base_url,
                  ApiClient(server.base_url).login("re_carol", "secret789")["token"]
                  ).set_reaction(cid, message_id, "👍", True)
    except ApiError as error:
        assert error.code in ("not_member", "not_found"), error.code
    else:
        raise AssertionError("非成员不该能回应")
    try:
        alice.set_reaction(cid, message_id, "  ", True)
    except ApiError as error:
        assert error.code == "invalid_request", error.code
    else:
        raise AssertionError("空白 emoji 应该被拒")


def test_presence_flips_reach_peers(_server=None):       # 这条自己起服务端，不用共用的那个
    """在线/离线变化要主动推给"跟我有共同会话的人"（不能只在我重拉列表时更新）。

    这里特意起一个窗口很短的服务端（2 秒没动静算离线、每秒扫一次），
    否则测一次要等一分钟。
    """
    with running_server(extra_args=["--online-window", "2", "--presence-interval", "1"]) as server:
        alice = register(server.base_url, "pr_alice", ALICE_PW, "爱丽丝")
        bob = register(server.base_url, "pr_bob", BOB_PW, "鲍勃")
        bob_id = [user["id"] for user in alice.search_users("pr_bob")][0]
        alice.create_direct("pr_bob")
        cursor = prime(alice)

        # bob 注册完就不动任何接口了 → 2 秒后算离线 → alice 应该收到一条离线的 presence
        events, cursor = _sync_until(alice, cursor, "presence", timeout=20)
        assert events, "bob 掉线应该主动告诉 alice"
        assert events[-1]["user_id"] == bob_id and events[-1]["online"] is False, events[-1]

        # bob 再露个面（等价于客户端重新长轮询）→ 变在线 → 再推一条
        bob.me()
        events, cursor = _sync_until(alice, cursor, "presence", timeout=20)
        assert events and events[-1]["online"] is True, events


# --------------------------------------------------------------------------- 界面
def test_ui_pin_for_both_reaches_the_other_window(server):
    """两个真实窗口：一方选「也为对方置顶」，另一方横幅真的出来了；取消也跟着撤。"""
    uip_alice = register(server.base_url, "uip_alice", ALICE_PW, "爱丽丝")
    uip_bob = register(server.base_url, "uip_bob", BOB_PW, "鲍勃")
    make_friends(uip_alice, uip_bob)          # 界面里要能发消息，两人得先是好友
    case = _UiCase(server.base_url)
    try:
        alice = case.window(case.session("uip_alice", ALICE_PW, "爱丽丝"))
        bob = case.window(case.session("uip_bob", BOB_PW, "鲍勃"))
        api = ApiClient(server.base_url, alice.account().token)
        cid = api.create_direct("uip_bob")["id"]
        alice.load_conversations()
        bob.load_conversations()
        assert wait_for(case.app, lambda: bool(alice.store.get(cid))
                        and bool(bob.store.get(cid)), timeout=20)
        alice.select_conversation(cid)
        bob.select_conversation(cid)
        alice.chat_view.set_input_text("这条要置顶")
        assert alice.send_message("这条要置顶") is True
        assert wait_for(case.app, lambda: any(m.text == "这条要置顶" for m in
                        bob.store.get(cid).messages), timeout=20)
        message_id = [m for m in alice.store.get(cid).messages if m.outgoing][-1].id

        # 「仅我可见」不该跑到对方那边
        assert alice.toggle_pin_message(message_id, for_both=False) is True
        assert alice.store.pin_ref(cid, message_id) is not None
        pump(case.app, 600)
        bob.tick()
        assert bob.store.pin_ref(cid, message_id) is None, "仅我可见的置顶不该出现在对方那里"
        assert alice.pending_pins() == {}, alice.pending_pins()
        alice.toggle_pin_message(message_id, for_both=False)          # 取消，回到干净状态

        # 「也为对方置顶」：对方那边横幅真的出来
        assert alice.toggle_pin_message(message_id, for_both=True) is True
        assert wait_for(case.app, lambda: bob.store.pin_ref(cid, message_id) is not None,
                        timeout=20), "对方也应该把这条置顶"
        assert bob.store.pin_ref(cid, message_id).for_both is True
        # 我自己这边更要还在：服务端会把置顶事件也推回给发起人（多设备同步），
        # 那条回显不能把"我刚置顶的"又取消掉（踩过：横幅 300ms 后就没了）
        pump(case.app, 800)
        assert alice.store.pin_ref(cid, message_id) is not None, \
            "自己发起的那条双方置顶被回显取消了"
        assert alice.chat_view.pinned_message_id() == message_id
        assert wait_for(case.app, lambda: bob.chat_view.pinned_message_id() == message_id,
                        timeout=10), "对方窗口的置顶横幅没出来"
        assert "双方" in bob.chat_view.pin_banner.scope_text(), \
            bob.chat_view.pin_banner.scope_text()
        assert bob.chat_view.is_pinned(message_id)

        # 对方取消置顶 -> 我这边也要跟着撤（服务端才是"双方可见"的权威）
        assert bob.unpin_current(message_id) is True
        assert wait_for(case.app, lambda: alice.store.pin_ref(cid, message_id) is None,
                        timeout=20), "取消置顶也该同步回来"
        assert wait_for(case.app, lambda: alice.chat_view.pinned_message_id() == "",
                        timeout=10)
    finally:
        case.close()


# --------------------------------------------------------------------------- 界面
def test_ui_read_receipt_and_reaction_reach_the_other_window(server):
    """两个真实窗口：对方读了 → 我这边双勾立刻打上；对方点表情 → 我这边立刻看到。

    这三件事以前都是"要等我切会话/重开程序才更新"：已读只标了本地、回应根本没过服务端。
    """
    uir_alice = register(server.base_url, "uir_alice", ALICE_PW, "爱丽丝")
    uir_bob = register(server.base_url, "uir_bob", BOB_PW, "鲍勃")
    make_friends(uir_alice, uir_bob)          # 界面里要能发消息，两人得先是好友
    case = _UiCase(server.base_url)
    try:
        alice = case.window(case.session("uir_alice", ALICE_PW, "爱丽丝"))
        bob = case.window(case.session("uir_bob", BOB_PW, "鲍勃"))
        api = ApiClient(server.base_url, alice.account().token)
        cid = api.create_direct("uir_bob")["id"]
        alice.load_conversations()
        bob.load_conversations()
        assert wait_for(case.app, lambda: bool(alice.store.get(cid))
                        and bool(bob.store.get(cid)), timeout=20)
        alice.select_conversation(cid)
        bob.select_conversation(cid)                  # 对方正开着这个会话
        alice.chat_view.set_input_text("看得见吗")
        assert alice.send_message("看得见吗") is True
        assert wait_for(case.app, lambda: any(m.text == "看得见吗" for m in
                        bob.store.get(cid).messages), timeout=20)
        message_id = [m for m in alice.store.get(cid).messages if m.outgoing][-1].id

        # 1) 已读：对方开着会话，消息一到就该回一句"我看到哪了" → 我这边双勾
        assert wait_for(case.app, lambda: alice.store.message(message_id).is_read,
                        timeout=20), ("双勾没及时打上",
                                      alice.store.message(message_id).status)
        assert alice.chat_view.message_list.bubbles()[message_id].read_label.text() == "✓✓"

        # 2) 回应：对方点 👍 → 我这边立刻看到（计数是服务端算的）
        counts = bob.toggle_reaction(message_id, "👍")
        assert counts == {"👍": 1}, counts
        assert wait_for(case.app, lambda: alice.reactions(message_id).get("👍") == 1,
                        timeout=20), ("对方的回应没过来", alice.reactions(message_id))
        assert alice.chat_view.message_list.bubbles()[message_id].reactionCounts == {"👍": 1}
        assert bob.pending_reactions() == {}, bob.pending_reactions()

        # 3) 我也点同一个 → 服务端算 2，两边都是 2（不各算各的）
        alice.toggle_reaction(message_id, "👍")
        assert wait_for(case.app, lambda: bob.reactions(message_id).get("👍") == 2,
                        timeout=20), ("第二个赞没同步", bob.reactions(message_id))
        assert alice.reactions(message_id).get("👍") == 2
    finally:
        case.close()


def test_ui_two_clients_exchange_messages(server):
    """两个**真实的界面窗口**各登一个账号，互发消息 —— 这条通了才叫真聊天软件。"""
    ui_alice = register(server.base_url, "ui_alice", ALICE_PW, "爱丽丝")
    ui_bob = register(server.base_url, "ui_bob", BOB_PW, "鲍勃")
    make_friends(ui_alice, ui_bob)            # 界面里要能发消息，两人得先是好友
    case = _UiCase(server.base_url)
    try:
        alice_window = case.window(case.session("ui_alice", ALICE_PW, "爱丽丝"))
        bob_window = case.window(case.session("ui_bob", BOB_PW, "鲍勃"))
        assert alice_window.account().username == "ui_alice"
        # 状态小字：刚打开的头一秒是"正在连接…"，连上后变成"账号 · 已连接 · host"
        assert wait_for(case.app, lambda: "已连接" in bob_window.connection_text(),
                        timeout=15), bob_window.connection_text()
        assert bob_window.connection_text().startswith("鲍勃 · "), bob_window.connection_text()

        # alice 建会话并打开（界面路径：+ 新建会话窗口 -> 服务端建单聊）
        dialog = alice_window.open_new_chat()
        dialog.search_field.setText("ui_bob")
        dialog.run_search()
        assert wait_for(case.app, lambda: "ui_bob" in dialog.rows(), timeout=15), \
            dialog.status_text()
        dialog.choose_user("ui_bob")
        assert wait_for(case.app, lambda: len(alice_window.store.conversations()) == 1,
                        timeout=15), "alice 那边应该出现和 bob 的会话"
        conversation_id = alice_window.store.conversations()[0].id

        # bob 那边应该被服务端"推"出来这个会话（不用手动刷新）
        assert wait_for(case.app, lambda: len(bob_window.store.conversations()) == 1,
                        timeout=15), "bob 那边应该收到新会话"

        # alice 发一条，bob 的窗口要真的收到
        alice_window.select_conversation(conversation_id)
        alice_window.chat_view.set_input_text("你好，我是爱丽丝")
        assert alice_window.send_message("你好，我是爱丽丝") is True
        bob_window.select_conversation(conversation_id)
        got = wait_for(case.app,
                       lambda: any(message.text == "你好，我是爱丽丝"
                                   for message in bob_window.store.get(
                                       conversation_id).messages),
                       timeout=20)
        assert got, "bob 的窗口应该收到这条消息"
        received = [m for m in bob_window.store.get(conversation_id).messages
                    if m.text == "你好，我是爱丽丝"][0]
        assert received.outgoing is False
        assert received.sender_id != ME_ID, "对方的消息不能算成自己发的"
        assert bob_window.chat_view.message_list.has_message(received.id), \
            "收到的消息要真的画出气泡"
        # 会话列表那行的摘要也要跟着变（列表刷新是事件驱动的，给它一点时间）
        assert wait_for(case.app,
                        lambda: "你好" in bob_window.sidebar._items[conversation_id]
                        .preview_text(), timeout=10), \
            bob_window.sidebar._items[conversation_id].preview_text()

        # 已读回执：bob 打开会话算已读 -> alice 的气泡变双勾
        bob_window.select_conversation(conversation_id)
        mine = [m for m in alice_window.store.get(conversation_id).messages
                if m.outgoing][-1]
        assert wait_for(case.app, lambda: alice_window.store.message(mine.id).is_read,
                        timeout=20), "alice 应该收到已读回执（气泡变双勾）"

        # bob 回一句，alice 也要收得到
        bob_window.chat_view.set_input_text("收到")
        assert bob_window.send_message("收到") is True
        assert wait_for(case.app,
                        lambda: any(m.text == "收到" for m in
                                    alice_window.store.get(conversation_id).messages),
                        timeout=20), "alice 应该收到 bob 的回复"

        # 附件：alice 发一张图，bob 那边应该是**已下载到本地**的
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(PNG_1PX * 40)
            picture = handle.name
        try:
            from mchat.core.models import Attachment
            alice_window.chat_view.set_attachment(Attachment.from_path(picture))
            alice_window.send_message("这是图片")
            downloaded = lambda: any(m.attachment is not None and m.attachment.exists
                                     for m in bob_window.store.get(conversation_id).messages)
            assert wait_for(case.app, downloaded, timeout=25), (
                "bob 那边应该把附件下到本地；现在的附件状态："
                + repr([(m.text, None if m.attachment is None else m.attachment.path,
                         None if m.attachment is None else m.attachment.is_remote)
                        for m in bob_window.store.get(conversation_id).messages])
                + "；传输层最后一条错误：" + str(bob_window.transport_status().get("error")))
            saved = [m for m in bob_window.store.get(conversation_id).messages
                     if m.attachment is not None and m.attachment.exists][0].attachment
            assert open(saved.path, "rb").read() == open(picture, "rb").read()
            assert os.path.basename(saved.path).endswith(".png")
        finally:
            os.remove(picture)
    finally:
        case.close()


def test_ui_presence_flips_show_up_in_the_other_window(_server=None):
    """好友上线/下线要**真的画到界面上**（小绿点 + 聊天头部第二行）。

    这条是 bug 回归：``_apply_presence`` 以前写 ``member.online = ...``，而 ``User``
    是 frozen dataclass —— 抛 ``FrozenInstanceError``（日志里抓到了这一行），
    异常发生在 ``tick()`` 这条事件轮询上，于是"对方上线了我这边还写着离线"。
    这里特意起一个窗口很短的服务端（2 秒算离线、每秒扫一次），不然要等一分钟。
    """
    with running_server(extra_args=["--online-window", "2", "--presence-interval", "1"]) as server:
        register(server.base_url, "upr_alice", ALICE_PW, "爱丽丝")
        bob = register(server.base_url, "upr_bob", BOB_PW, "鲍勃")
        bob_id = bob.me()["user"]["id"]     # 先记下来：轮询判据里**不能**再调 bob 的接口，
        #                                     否则每次都刷新 last_seen，他永远不会被判离线
        case = _UiCase(server.base_url)
        try:
            alice = case.window(case.session("upr_alice", ALICE_PW, "爱丽丝"))
            api = ApiClient(server.base_url, alice.account().token)
            cid = api.create_direct("upr_bob")["id"]
            alice.load_conversations()
            assert wait_for(case.app, lambda: alice.store.get(cid) is not None, timeout=20)
            alice.select_conversation(cid)

            # bob 这一轮不再调任何接口 → 2 秒后算离线 → 推送过来 → 界面立刻变
            assert wait_for(case.app,
                            lambda: alice.store.get(cid).member(bob_id).online is False,
                            timeout=25), "bob 超时该被判离线"
            assert alice.chat_view.status_label.text() == "离线", \
                alice.chat_view.status_label.text()
            assert alice.sidebar._items[cid].is_online() is False, "会话列表里的小绿点该灭掉"

            # bob 冒个泡（等价于另一端重新长轮询）→ 变在线 → 界面再变回来。
            # 要**反复**叫：服务端对 last_seen 是节流的（每 10 秒最多写一次），
            # 只叫一声很可能正好落在节流窗口里、压根没写进去。
            deadline = time.time() + 30
            while (time.time() < deadline and
                   alice.store.get(cid).member(bob_id).online is not True):
                bob.me()
                pump(case.app, 1200)
            assert alice.store.get(cid).member(bob_id).online is True, \
                "bob 上线了，alice 这边应该收到 presence 并改成在线"
            assert alice.chat_view.status_label.text() == "在线", \
                alice.chat_view.status_label.text()
            assert alice.sidebar._items[cid].is_online() is True, "会话列表里的小绿点该亮起来"
        finally:
            case.close()


# --------------------------------------------------------------------------- 退出
def test_closing_the_main_window_quits_the_process(server):
    """点 ✕ 关掉主窗口 → **整个进程真的退出**（不能在资源管理器里继续占着）。

    为什么要在子进程里验：这件事的判据就是"进程没了"。``quitOnLastWindowClosed``
    为了登录窗口/主窗口交替是关着的，所以最后一个窗口关掉之后谁都不退 ——
    原来的表现正是"窗口关了、进程还挂在任务管理器里"。这里跑一个真进程：
    自动登录 → 开主窗口 → 300ms 后点 ✕ → 看 ``app.exec()`` 是否返回、进程是否退出。
    """
    import subprocess

    folder = tempfile.mkdtemp(prefix="mchat-quit-")
    probe = os.path.join(folder, "quit_probe.py")
    session_file = os.path.join(folder, "account.json")
    with open(probe, "w", encoding="utf-8") as handle:
        handle.write(_QUIT_PROBE)
    command = [sys.executable, probe, server.base_url, session_file]
    started = time.time()
    done = subprocess.run(command, capture_output=True, text=True, timeout=60,
                          cwd=ROOT)
    elapsed = time.time() - started
    try:
        assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
        assert "EXIT 0" in done.stdout, (done.stdout, done.stderr)
        assert "MAIN OPEN" in done.stdout, (done.stdout, done.stderr)
        assert elapsed < 45, f"关窗之后进程拖了 {elapsed:.1f}s 才退出"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


_QUIT_PROBE = '''
# 真跑一遍"登录 → 主窗口 → 点 ✕ → 进程退出"，判据是进程自己退出。
import os
import sys

sys.path.insert(0, r"{root}")
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from mchat.core.api import ApiClient
from mchat.core.session import Session, SessionStore
from mchat.ui.launcher import Launcher


def main() -> int:
    base_url, session_file = sys.argv[1], sys.argv[2]
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)          # 和真入口一致（见 launcher.main）

    api = ApiClient(base_url)
    payload = api.register("quit_probe", "secret123", "探针")
    token = str(payload.get("token") or "")
    user = dict(payload.get("user") or {})
    session = Session(server=base_url, token=token, user_id=str(user.get("id") or ""),
                      username="quit_probe", display_name="探针")

    store = SessionStore(session_file)
    store.save(session)
    launcher = Launcher(app, session_store=store, api_factory=ApiClient)
    launcher.start()                               # 走真流程：拿缓存里的令牌自动进主窗口

    def click_x():
        window = launcher.main_window()
        if window is None:
            print("NO MAIN", flush=True)
            app.exit(9)
            return
        print("MAIN OPEN", flush=True)
        window.close()                             # 就是点右上角那个 ✕

    QTimer.singleShot(1200, click_x)
    QTimer.singleShot(20000, lambda: (print("TIMEOUT", flush=True), app.exit(7)))
    code = app.exec()
    print(f"EXIT {code}", flush=True)
    return int(code)


raise SystemExit(main())
'''.replace("{root}", ROOT)


# --------------------------------------------------------------------------- 登录 + 缓存
def test_login_window_writes_cache_and_launcher_auto_logs_in(server):
    from mchat.ui.launcher import Launcher
    from mchat.ui.login_window import LoginWindow

    folder = tempfile.mkdtemp(prefix="mchat-login-")
    try:
        store = SessionStore(os.path.join(folder, "account.json"))
        settings = QSettings("MChatE2E", "MChatE2E")
        settings.clear()
        app = _app()
        app.setQuitOnLastWindowClosed(False)

        window = LoginWindow(session_store=store, api_factory=ApiClient)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.server_field.setText(server.base_url)
        window.set_mode("注册")
        window.username_field.setText("login_alice")
        window.password_field.setText(ALICE_PW)
        window.confirm_field.setText(ALICE_PW)
        window.display_field.setText("爱丽丝")
        window.show()
        got = []
        window.loggedIn.connect(lambda session: got.append(session))
        assert window.submit() is True

        assert wait_for(app, lambda: bool(got), timeout=20), window.status_text()
        session = got[0]
        assert session.username == "login_alice" and session.token
        assert "欢迎" in window.status_text(), window.status_text()
        assert window.password_field.text() == "", "登录成功后密码要从内存里抹掉"
        assert store.last() is not None and store.last().token == session.token, \
            "登录成功必须把令牌写进本地缓存（下次就不用再登录了）"
        window.hide()

        # 再开一次：缓存里有令牌 -> 自动登录，登录窗口不该出现
        created = []
        launcher = Launcher(app, session_store=store, api_factory=ApiClient,
                            settings=settings,
                            main_factory=lambda **kwargs: _FakeMainWindow(**kwargs))
        launcher.start()
        assert wait_for(app, lambda: launcher.scope() == "main", timeout=20), \
            launcher.login_window().status_text() if launcher.login_window() else "?"
        assert launcher.session().username == "login_alice"
        assert launcher.main_window() is not None
        created.append(launcher.main_window())
        assert launcher.login_window() is None, "自动登录成功后不该还留着登录界面"

        # 退出登录：缓存清掉 + 回到登录界面
        assert launcher.logout() is True
        assert store.last() is None or store.last().token == "", "退出登录该清掉令牌"
        assert launcher.scope() == "login"
        assert launcher.login_window() is not None
        launcher.main_window()
    finally:
        for widget in _app().topLevelWidgets():
            widget.hide()
        shutil.rmtree(folder, ignore_errors=True)


class _FakeMainWindow:
    """只测启动流程时用的轻量替身：不建真的聊天窗口（省掉一堆库和网络线程）。"""

    def __init__(self, *, session=None, api=None, log=None, settings=None):
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Signals(QObject):
            logoutRequested = pyqtSignal()
            # 主窗口的契约：用户点 ✕ 关掉窗口时要抛这个信号（启动器据此退出程序）
            closed = pyqtSignal()

        self.session = session
        self.api = api
        self.log = log
        self.signals = _Signals()
        self.logoutRequested = self.signals.logoutRequested
        self.closed = self.signals.closed

    def show(self):
        return None

    def raise_(self):
        return None

    def activateWindow(self):
        return None

    def hide(self):
        return None

    def deleteLater(self):
        return None

    def shutdown(self):
        return None

    @property
    def toast(self):
        class _Toast:
            def show_message(self, *_args, **_kwargs):
                return None
        return _Toast()


# --------------------------------------------------------------------------- 驱动
def _run_all() -> int:
    tests = [("test_account_flow_over_http", test_account_flow_over_http),
             ("test_two_accounts_chat", test_two_accounts_chat),
             ("test_pin_for_both_reaches_the_other_account",
              test_pin_for_both_reaches_the_other_account),
             ("test_reactions_reach_the_other_account",
              test_reactions_reach_the_other_account),
             ("test_presence_flips_reach_peers", test_presence_flips_reach_peers),
             ("test_ui_read_receipt_and_reaction_reach_the_other_window",
              test_ui_read_receipt_and_reaction_reach_the_other_window),
             ("test_ui_pin_for_both_reaches_the_other_window",
              test_ui_pin_for_both_reaches_the_other_window),
             ("test_ui_two_clients_exchange_messages", test_ui_two_clients_exchange_messages),
             ("test_ui_presence_flips_show_up_in_the_other_window",
              test_ui_presence_flips_show_up_in_the_other_window),
             ("test_closing_the_main_window_quits_the_process",
              test_closing_the_main_window_quits_the_process),
             ("test_login_window_writes_cache_and_launcher_auto_logs_in",
              test_login_window_writes_cache_and_launcher_auto_logs_in)]
    failed = 0
    with running_server() as server:
        for name, func in tests:
            try:
                func(server)
                print(f"  ok    {name}")
            except Exception as exc:                 # noqa: BLE001
                failed += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
