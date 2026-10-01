"""网络传输层单测（不需要真服务器：用一个"假的 ApiClient"）。

用法：``.venv/Scripts/python.exe tests/test_net.py``

这里验的是**传输层自己的那点逻辑**，不涉及协议本身对不对（那是
``tests/test_server.py`` 起真服务端干的事）：

    接口一致性 —— 形状和本机演示那个 LocalTransport 一样（start/send/poll/stop）
    发件箱     —— 发出去立刻返回、失败重试不丢、附件只上传一次、id 幂等传递
    收件箱     —— 四种事件翻译成界面事件；自己打的字不显示给自己
    长轮询     —— 光标前进、掉线后自动重连、登录失效上报给界面
    附件       —— 收到就下到本地缓存，下不了保留 remote:// 标记
    状态文案   —— 侧栏底部那行"已连接/离线 · N 条待发"
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.core.api import ApiError, NetworkError, SendResult             # noqa: E402
from mchat.core.models import Attachment                                   # noqa: E402
from mchat.core.net_transport import NetworkTransport                      # noqa: E402
from mchat.core.transport import (EVENT_CONVERSATION, EVENT_MESSAGE,       # noqa: E402
                                 EVENT_PIN, EVENT_READ, EVENT_RESYNC,
                                 EVENT_SEND_BLOCKED, EVENT_TYPING,
                                 ChatTransport, Outgoing)

ME = "u_me"


def _message_json(message_id="m1", text="在吗", sender="u_bob", ts=None, **extra):
    payload = {"id": message_id, "conversation_id": "c1", "sender_id": sender,
               "sender_name": "鲍勃", "text": text, "ts": ts or time.time(),
               "status": "sent", "attachment": None, "reply_to": None,
               "forwarded_from": "", "merged": []}
    payload.update(extra)
    return payload


class FakeApi:
    """一个可编程的假客户端：想让它返回什么事件、想让它失败几次都行。"""

    def __init__(self, *, uploads=True):
        self.base_url = "http://127.0.0.1:9999"
        self.token = "tok"
        self.sent: list[dict] = []
        self.reads: list[tuple] = []
        self.typings: list[tuple] = []
        self.uploads: list[str] = []
        self.downloads: list[str] = []
        self.cursors: list[int] = []
        self.events: list[list] = []          # 每次 sync 依次弹出
        self.intercept: dict | None = None    # 非 None 时 send_message 回"被门禁拦下"
        self.intercepted: list[dict] = []     # 被拦下的发送尝试（只该尝试一次）
        self.fail_sync = 0
        self.fail_send = 0
        self.fail_send_with: ApiError | None = None
        self.fail_download = False
        self.upload_id = "f1" if uploads else ""
        self.file_bytes = b"\x89PNG-fake"

    # ---- 传输层用到的接口 ----
    def sync(self, cursor, timeout=20.0):
        self.cursors.append(int(cursor))
        if self.fail_sync > 0:
            self.fail_sync -= 1
            raise NetworkError("Connection refused", url=self.base_url + "/api/sync")
        batch = self.events.pop(0) if self.events else []
        head = 100 + len(self.sent) + len(self.cursors)
        return {"events": batch, "cursor": max(int(cursor), head), "server_time": time.time()}

    def send_message(self, conversation_id, client_id, text, **kwargs):
        if self.fail_send > 0:
            self.fail_send -= 1
            raise NetworkError("掉了", url=self.base_url)
        if self.fail_send_with is not None:
            error, self.fail_send_with = self.fail_send_with, None
            raise error
        if self.intercept is not None:
            # 好友门禁：服务端没落库这条消息，只在响应里回一个 friend_request
            self.intercepted.append({"cid": conversation_id, "id": client_id, "text": text})
            return SendResult(None, self.intercept)
        self.sent.append({"cid": conversation_id, "id": client_id, "text": text, **kwargs})
        return _message_json(message_id=client_id, text=text, sender=ME)

    def upload(self, path, name=None):
        self.uploads.append(name or os.path.basename(path))
        return {"id": self.upload_id, "name": name or os.path.basename(path),
                "size": os.path.getsize(path)}

    def download(self, attachment_id, dest_path, **kwargs):
        # 真客户端会传 root=（"必须落在这个目录里"），假客户端收下不用，但签名得对得上
        self.downloads.append(attachment_id)
        if self.fail_download:
            raise NetworkError("下不动", url=self.base_url)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with open(dest_path, "wb") as handle:
            handle.write(self.file_bytes)
        return dest_path

    def mark_read(self, conversation_id, up_to_ts):
        self.reads.append((conversation_id, up_to_ts))
        return {"ok": True}

    def typing(self, conversation_id, typing=True):
        self.typings.append((conversation_id, typing))
        return {"ok": True}


# --------------------------------------------------------------------- 接口一致
def test_transport_matches_the_interface():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    assert isinstance(transport, ChatTransport), "必须还是那个传输层接口"
    assert transport.name == "联网" and transport.online is False
    assert transport.poll() == []
    transport.stop()                                   # 没启动过也不能炸


def test_send_is_queued_and_never_blocks():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    transport.send(Outgoing("c1", ME, "在吗", "id1"))
    assert transport.pending() == 1, "send 只入队，真正的发送在后台"
    assert fake.sent == []
    transport._flush_outbox()
    assert transport.pending() == 0 and fake.sent[0]["id"] == "id1"
    assert fake.sent[0]["text"] == "在吗"


def test_outbox_keeps_message_when_offline_then_resends():
    fake = FakeApi()
    fake.fail_send = 1
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    transport.send(Outgoing("c1", ME, "第一条", "id1"))
    try:
        transport._flush_outbox()
    except NetworkError:
        pass
    assert transport.pending() == 1, "掉线时消息必须留在发件箱里"
    transport._flush_outbox()                          # 网络回来了
    assert transport.pending() == 0 and fake.sent[0]["id"] == "id1"
    assert len(fake.sent) == 1


def test_send_order_is_preserved():
    fake = FakeApi()
    fake.fail_send = 2                                 # 头两条都失败一次
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    for index in range(3):
        transport.send(Outgoing("c1", ME, f"第{index}条", f"id{index}"))
    for _ in range(5):
        try:
            transport._flush_outbox()
        except NetworkError:
            continue
    assert [item["text"] for item in fake.sent] == ["第0条", "第1条", "第2条"], fake.sent


def test_attachment_is_uploaded_only_once():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(b"x" * 32)
        path = handle.name
    try:
        attachment = Attachment.from_path(path)
        for _ in range(3):
            transport.send(Outgoing("c1", ME, "", f"id{_}", attachment=attachment))
        transport._flush_outbox()
        assert fake.uploads == [attachment.name], f"同一个文件只该上传一次：{fake.uploads}"
        assert [item["attachment_id"] for item in fake.sent] == ["f1"] * 3
    finally:
        os.remove(path)


def test_duplicate_id_from_server_is_not_fatal():
    fake = FakeApi()
    fake.fail_send_with = ApiError("duplicate_id", "这条 id 有人用了", 409)
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    transport.send(Outgoing("c1", ME, "重复的", "id1"))
    transport._flush_outbox()
    assert transport.pending() == 0, "服务端说 id 冲突就该跳过它，别把发件箱堵死"


# --------------------------------------------------------------------- 收事件
def test_translate_message_event():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    fake.events = [[{"kind": "message", "conversation_id": "c1",
                     "message": _message_json()}]]
    transport._long_poll()
    events = [event for event in transport.poll() if event.kind == EVENT_MESSAGE]
    assert len(events) == 1, events
    message = events[0].message
    assert message.text == "在吗" and message.outgoing is False
    assert message.sender_id == "u_bob" and events[0].message_id == "m1"
    assert transport.poll() == [], "取过一次就该空了"


def test_translate_own_echo_and_other_events():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    fake.events = [[
        {"kind": "message", "conversation_id": "c1",
         "message": _message_json("m2", "我发的", sender=ME)},
        {"kind": "read", "conversation_id": "c1", "message_id": "m2",
         "reader_id": "u_bob", "ts": 1.0},
        {"kind": "typing", "conversation_id": "c1", "user_id": "u_bob", "typing": True},
        {"kind": "typing", "conversation_id": "c1", "user_id": ME, "typing": True},
        {"kind": "conversation", "conversation_id": "c9",
         "conversation": {"id": "c9", "kind": "direct", "title": "鲍勃",
                          "members": [{"id": ME, "username": "alice"},
                                      {"id": "u_bob", "username": "bob"}],
                          "last_message": None, "unread": 0}},
        {"kind": "resync"},
        {"kind": "未来才有的新事件"},
    ]]
    transport._long_poll()
    events = transport.poll()
    kinds = [event.kind for event in events]
    # 第一个 resync 是"刚连上"要界面重新对账（游标从"现在"起，服务端不重放），
    # 最后一个 resync 是服务端自己说"流水断了"——两个都要能认出来
    assert kinds == [EVENT_RESYNC, EVENT_MESSAGE, EVENT_READ, EVENT_TYPING,
                     EVENT_CONVERSATION, EVENT_RESYNC], kinds
    events = events[1:]
    echo = events[0].message
    assert echo.outgoing is True, "同一账号在别的设备发的消息也要落在右边"
    assert echo.sender_id == "me"
    assert events[2].typing is True and events[2].sender_id == "u_bob"
    created = events[3].conversation
    assert created.id == "c9" and created.title == "鲍勃"
    assert created.peer_name == "bob", "对方是 bob，不能认成我自己"
    assert transport.status()["received"] == len(events) + 1  # 多的那条是刚连上的 resync


def test_translate_pin_event():
    """置顶事件：带快照消息 + pinned 真假 + 谁置顶的。"""
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    fake.events = [[
        {"kind": "pin", "conversation_id": "c1", "message_id": "m1",
         "message": _message_json("m1", "这条要置顶", sender="u_bob"),
         "pinned": True, "pinned_by": "u_bob", "pinned_by_name": "鲍勃", "ts": 1.0},
        {"kind": "pin", "conversation_id": "c1", "message_id": "m2",
         "message": _message_json("m2", "取消的那条", sender="u_bob"),
         "pinned": False, "pinned_by": "u_bob", "pinned_by_name": "鲍勃", "ts": 2.0},
    ]]
    transport._long_poll()
    events = [event for event in transport.poll() if event.kind == EVENT_PIN]
    assert len(events) == 2, events
    assert events[0].pinned is True and events[0].pinned_by_name == "鲍勃"
    assert events[0].message_id == "m1" and events[0].message.text == "这条要置顶"
    assert events[1].pinned is False and events[1].message_id == "m2"


def test_cursor_is_passed_and_advanced():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    transport._long_poll()
    first = transport.cursor
    transport._long_poll()
    assert fake.cursors == [0, first], fake.cursors
    assert transport.cursor > 0


def test_read_and_typing_go_through_the_queue():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    transport.mark_read("c1", 123.0)
    transport.send_typing("c1", True)
    assert fake.reads == [] and fake.typings == [], "这两个也走后台线程，别在界面线程发"
    transport._run_tasks()
    assert fake.reads == [("c1", 123.0)] and fake.typings == [("c1", True)]


# --------------------------------------------------------------------- 附件
def test_received_attachment_is_downloaded_to_cache():
    fake = FakeApi()
    with tempfile.TemporaryDirectory() as folder:
        transport = NetworkTransport(fake, me_id=ME, download_dir=folder, autostart=False)
        fake.events = [[{"kind": "message", "conversation_id": "c1",
                         "message": _message_json(
                             attachment={"id": "f9", "name": "图.png", "size": 9})}]]
        transport._long_poll()
        attachment = [event for event in transport.poll() if event.attachment][0].attachment
        assert attachment.is_remote is False, "能下就该下下来"
        assert os.path.isfile(attachment.path)
        assert open(attachment.path, "rb").read() == fake.file_bytes
        assert fake.downloads == ["f9"]


def test_failed_download_keeps_remote_marker():
    fake = FakeApi()
    fake.fail_download = True
    with tempfile.TemporaryDirectory() as folder:
        transport = NetworkTransport(fake, me_id=ME, download_dir=folder, autostart=False)
        fake.events = [[{"kind": "message", "conversation_id": "c1",
                         "message": _message_json(
                             attachment={"id": "f9", "name": "图.png", "size": 9})}]]
        transport._long_poll()
        attachment = [event for event in transport.poll() if event.attachment][0].attachment
        assert attachment.is_remote and attachment.remote_id == "f9"
        assert attachment.name == "图.png", "下不了也得让界面显示得出文件名"
        assert transport.cache_path(attachment).endswith("f9-图.png")


# --------------------------------------------------------------------- 状态
def test_describe_text_for_online_offline_and_backlog():
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    assert transport.describe().startswith("正在连接"), "还没连上时该说正在连接"
    transport._set_online(True)
    assert transport.describe() == "已连接 · 127.0.0.1:9999"
    transport._primed = True                     # 已经同步过一次（白盒设一下）
    transport._set_online(False)
    transport.send(Outgoing("c1", ME, "排队中", "id1"))
    text = transport.describe()
    assert text.startswith("离线，1 条待发"), text
    assert "正在重连" in text
    assert transport.online is False


# --------------------------------------------------------------------- 后台线程
def test_background_thread_receives_and_stops():
    fake = FakeApi()
    fake.events = [[{"kind": "message", "conversation_id": "c1",
                     "message": _message_json("m7", "后台收到的")}]]
    with tempfile.TemporaryDirectory() as folder:
        transport = NetworkTransport(fake, me_id=ME, download_dir=folder)
        deadline = time.time() + 5.0
        got = []
        while time.time() < deadline and not got:
            got = transport.poll()
            time.sleep(0.05)
        assert got, "后台线程应该把事件收进来"
        assert transport.online is True
        assert transport.is_running()
    transport.stop()
    assert not transport.is_running(), "stop 之后线程必须退出（否则会一直占着服务器连接）"


def test_auth_failure_is_reported_to_the_ui():
    fake = FakeApi()
    fake.fail_sync = 1

    def explode(*_args, **_kwargs):
        raise ApiError("unauthorized", "登录已失效", 401)

    fake.sync = explode
    transport = NetworkTransport(fake, me_id=ME)
    deadline = time.time() + 5.0
    while time.time() < deadline and not transport.auth_error:
        time.sleep(0.05)
    assert transport.auth_error.startswith("登录已失效"), transport.status()
    transport.stop()


def test_wake_up_on_new_message_is_fast():
    """有消息要发时别等满一个轮询周期（否则发一条消息要等一两秒才出去）。"""
    fake = FakeApi()
    transport = NetworkTransport(fake, me_id=ME, sync_timeout=0.2)
    try:
        transport.send(Outgoing("c1", ME, "急件", "id1"))
        deadline = time.time() + 3.0
        while time.time() < deadline and not fake.sent:
            time.sleep(0.02)
        assert fake.sent, "后台线程应该被唤醒并立刻把消息发出去"
    finally:
        transport.stop()


def test_intercepted_send_is_reported_and_never_retried():
    """被好友门禁拦下的发送：产出一条 send_blocked 事件，而且**不重发**。

    为什么必须验"不重试"：这条消息在服务端根本没落库，它变成了对方的一条好友申请。
    要是还按老规矩留在发件箱里重发，就会把同一条申请一遍遍刷给对方。
    """
    fake = FakeApi()
    fake.intercept = {"user": {"id": "u_bob", "username": "bob",
                               "display_name": "鲍勃", "online": True},
                      "note": "在吗", "ts": time.time()}
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    transport.send(Outgoing("c1", ME, "在吗", "id1"))
    transport._flush_outbox()

    assert len(fake.intercepted) == 1, "只该尝试发一次（没有重试）"
    assert transport.pending() == 0, "被拦下的发送不能留在发件箱里等重发"
    assert fake.sent == [], "被拦下的消息服务端没落库，不该记成已发送"

    blocked = [event for event in transport.poll() if event.kind == EVENT_SEND_BLOCKED]
    assert len(blocked) == 1, blocked
    assert blocked[0].conversation_id == "c1"
    assert blocked[0].message_id == "id1", "要能对上本地那条乐观显示的消息"
    assert blocked[0].note == "在吗" and blocked[0].text == "在吗"
    assert blocked[0].user is not None and blocked[0].user.name == "鲍勃"

    # 再来一次 flush 也不该冒出第二条：发件箱里已经什么都没有了
    transport._flush_outbox()
    assert len(fake.intercepted) == 1
    assert [event for event in transport.poll()
            if event.kind == EVENT_SEND_BLOCKED] == []


def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"  ok    {name}")
        except Exception as exc:                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
