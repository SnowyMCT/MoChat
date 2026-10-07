"""附件传输进度 + 失败态（上传 / 下载对称，数据层 → 传输层 → 界面层三层，无头）。

覆盖：
* ``ApiClient.upload(on_progress=...)`` 真的**分块发**（进度单调到 100%，服务端收到的
  长度和文件一模一样 —— 分块发送必须自己写 Content-Length，写错这里就会露馅）；
* ``ApiClient.download(on_progress=...)`` 真的**分块收**（进度单调到 100%、下下来的字节
  和上传的一模一样、Content-Length 等于明文大小）；
* 传输层把上传进度转成事件、**按百分比节流**、同一个文件第二次命中缓存也补报 100%；
* 上传失败（比如本地文件没了）会发一条 ``failed`` 事件，而且异常照旧抛出去走退避重试；
* 界面上：自己发的带附件消息有"上传"线、别人发来还没取回的有"下载"线（措辞不同：
  上传"正在重试"因为会自动重发，下载"点一下重试"因为得用户再点），
  下载进度从**后台线程经 Qt 信号**搬回气泡，失败原因也露出来。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                       # noqa: E402
from PyQt6.QtCore import QSettings, Qt                               # noqa: E402
from PyQt6.QtWidgets import QApplication                             # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed                                                         # noqa: E402
from server_fixture import running_server                            # noqa: E402
from ui_probe import wait_for                                        # noqa: E402
from mchat.core.api import ApiClient                                 # noqa: E402
from mchat.core.atrest import materialize                             # noqa: E402
from mchat.core.db import ChatLog                                    # noqa: E402
from mchat.core.models import Attachment, Message                    # noqa: E402
from mchat.core.net_transport import NetworkTransport                # noqa: E402
from mchat.core.transport import (EVENT_UPLOAD_PROGRESS, LocalTransport,   # noqa: E402
                                  Outgoing, TransportEvent)
from mchat.ui.main_window import MainWindow                          # noqa: E402

BIG = 400 * 1024                     # 400 KB：比 64KB 的分块大，一定会有多档进度


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(application=None, ms=140) -> None:
    application = application or QApplication.instance()
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def big_file(tmp_path):
    path = tmp_path / "大文件.bin"
    path.write_bytes(bytes(range(256)) * (BIG // 256))
    return str(path)


# ------------------------------------------------------------ 数据层：真服务端
def test_upload_streams_in_chunks_and_server_receives_everything(big_file):
    with running_server() as server:
        api = ApiClient(server.base_url)
        payload = api.register("alice", "secret123", "爱丽丝")
        api.token = str(payload.get("token") or "")      # register 不会自己存令牌
        seen: list[tuple[int, int]] = []
        info = api.upload(big_file, name="大文件.bin",
                          on_progress=lambda done, total: seen.append((done, total)))
        assert info.get("id"), info
        assert int(info.get("size") or 0) == BIG, "服务端收到的长度不对（Content-Length 写错了？）"
        assert seen, "一块进度都没报"
        done = [value for value, _ in seen]
        assert done == sorted(done), "进度必须单调不减"
        assert all(total == BIG for _, total in seen)
        assert done[-1] == BIG, "最后一次进度要正好等于总长"


# ---------------------------------------------------------- 传输层：事件与节流
def test_transport_turns_progress_into_throttled_events(big_file):
    with running_server() as server:
        api = ApiClient(server.base_url)
        payload = api.register("alice", "secret123", "爱丽丝")
        api.token = str(payload.get("token") or "")
        transport = NetworkTransport(api, autostart=False)
        attachment = Attachment(path=big_file, name="大文件.bin")
        outgoing = Outgoing(conversation_id="c1", sender_id="u1", text="",
                            message_id="m_up", attachment=attachment)

        file_id = transport._upload(
            attachment, on_progress=lambda d, t: transport._note_upload_progress(outgoing, d, t))
        assert file_id

        events = [event for event in transport.poll()
                  if event.kind == EVENT_UPLOAD_PROGRESS]
        values = [event.progress for event in events]
        assert values, "一条进度事件都没有"
        assert values == sorted(values), "进度事件必须递增"
        assert values[-1] == 1.0, "最后要报满"
        assert len(values) <= 100, f"按百分比节流之后不该超过 100 条，实际 {len(values)}"
        assert all(event.message_id == "m_up" for event in events)

        # 同一个文件再发一次：命中上传缓存，也要补报一次 100%（否则进度条停在 0 上）
        transport._upload(
            attachment, on_progress=lambda d, t: transport._note_upload_progress(outgoing, d, t))
        again = [event for event in transport.poll() if event.kind == EVENT_UPLOAD_PROGRESS]
        assert [event.progress for event in again] == [1.0], again


def test_upload_failure_emits_event_and_still_raises(tmp_path):
    """本地文件没了 = 上传失败：界面上要说"正在重试"，而且异常要照旧抛给发件箱。"""
    api = ApiClient("http://127.0.0.1:9")             # 不会真的连（文件先开就失败了）
    transport = NetworkTransport(api, autostart=False)
    attachment = Attachment(path=str(tmp_path / "早就没有了.bin"), name="没有.bin")
    outgoing = Outgoing(conversation_id="c1", sender_id="u1", text="",
                        message_id="m_gone", attachment=attachment)

    with pytest.raises(Exception):
        transport._send_one(outgoing)

    events = [event for event in transport.poll() if event.kind == EVENT_UPLOAD_PROGRESS]
    assert len(events) == 1, events
    assert events[0].failed and events[0].message_id == "m_gone"
    assert events[0].error, "失败原因要带上，界面那行小字靠它"


# ------------------------------------------------------------------ 界面层
@pytest.fixture()
def window(app):
    settings = QSettings("MChatUploadProgressTest", "MChatUploadProgressTest")
    settings.clear()
    demo = seed.build_demo()
    transport = LocalTransport(demo.scripts, clock=time.time)
    win = MainWindow(demo=demo, transport=transport, settings=settings,
                     log=ChatLog(":memory:"), confirm=lambda **kw: True,
                     filedialog=lambda kind, parent=None: "")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    QApplication.setActiveWindow(win)
    pump(app, 240)
    yield win
    win.close()
    win.deleteLater()
    pump(app, 40)


def add_attachment_message(win, path: str, *, message_id: str,
                           outgoing: bool = True) -> Message:
    message = Message(id=message_id, conversation_id=seed.CHAT_LIN, sender_id="u1",
                      text="", ts=time.time(), outgoing=outgoing,
                      attachment=Attachment(path=os.path.abspath(path),
                                            name=os.path.basename(path)))
    win.store.append(message)
    win.chat_view.show_conversation(win.store.get(seed.CHAT_LIN))
    pump()
    return message


def add_remote_attachment_message(win, *, message_id: str,
                                  name: str = "对方发的.pdf") -> Message:
    """塞一条"别人发来、还没取回来"的附件消息（气泡上该有一条下载线）。

    走 ``select_conversation`` 而不是直接 ``show_conversation``：取回完成之后主窗口要
    把这条会话**重画**（附件从"没下"变成真图片/文件卡片），而那条分支认的是
    ``_current_id`` —— 不选中会话的话它不会走，测试就看不到"那条线收掉"。
    """
    attachment = Attachment.remote("f_remote_1", name, 4096)
    message = Message(id=message_id, conversation_id=seed.CHAT_LIN, sender_id="u_bob",
                      text="", ts=time.time(), outgoing=False, attachment=attachment)
    win.store.append(message)
    win.select_conversation(seed.CHAT_LIN)
    pump()
    return message


def feed(win, event: TransportEvent) -> None:
    """走真实的 tick() 分发（不是直接调 handler）：验的是"事件真能落到气泡上"。"""
    win._transport.poll = lambda now=None: [event]
    win.tick()


def test_progress_event_lands_on_my_bubble(app, window, tmp_path):
    path = tmp_path / "报告.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    add_attachment_message(window, str(path), message_id="m_mine")
    bubble = window.chat_view.message_list.bubbles()["m_mine"]
    assert bubble.upload is not None, "自己发的带附件消息该有进度条"
    assert not bubble.upload.isVisible(), "没在上传时不该占地方"

    feed(window, TransportEvent(kind=EVENT_UPLOAD_PROGRESS, conversation_id=seed.CHAT_LIN,
                                message_id="m_mine", progress=0.42))
    pump(app, 80)

    assert bubble.upload.isVisible()
    assert abs(bubble.upload.progress_value() - 0.42) < 0.001
    assert bubble.upload_text() == "上传中 42%", bubble.upload_text()
    assert window.chat_view.upload_text("m_mine") == "上传中 42%"


def test_failure_event_says_retrying(app, window, tmp_path):
    path = tmp_path / "报告.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    add_attachment_message(window, str(path), message_id="m_mine")
    bubble = window.chat_view.message_list.bubbles()["m_mine"]

    feed(window, TransportEvent(kind=EVENT_UPLOAD_PROGRESS, conversation_id=seed.CHAT_LIN,
                                message_id="m_mine", failed=True, error="连接被重置"))
    pump(app, 80)

    assert bubble.upload.is_failed()
    text = bubble.upload_text()
    assert "上传失败" in text and "正在重试" in text, text
    assert "连接被重置" in text, "失败原因要露出来，不然用户只看到「卡住了」"


def test_other_peoples_attachment_has_no_progress_bar(app, window):
    message = Message(id="m_peer", conversation_id=seed.CHAT_LIN, sender_id="u_bob",
                      text="", ts=time.time(), outgoing=False,
                      attachment=Attachment.remote("f_peer", "对方发的.pdf", 2048))
    window.store.append(message)
    window.chat_view.show_conversation(window.store.get(seed.CHAT_LIN))
    pump(app, 160)
    bubble = window.chat_view.message_list.bubbles()["m_peer"]
    assert bubble.upload is None, "别人发来的附件不经过我这条上传链路，不该有进度条"
    assert window.chat_view.set_upload_progress("m_peer", 0.5) is False


def test_progress_for_unknown_message_is_ignored(app, window):
    feed(window, TransportEvent(kind=EVENT_UPLOAD_PROGRESS, conversation_id=seed.CHAT_LIN,
                                message_id="m_不存在", progress=0.5))
    pump(app, 40)                                     # 只要求"不炸"：这条不在当前会话里


# ------------------------------------------------------ 下载（另一端的那条线）
def test_download_streams_in_chunks_and_writes_the_same_bytes(tmp_path):
    with running_server() as server:
        api = ApiClient(server.base_url)
        api.token = str(api.register("alice", "secret123", "爱丽丝").get("token") or "")
        source = tmp_path / "给对方的.bin"
        source.write_bytes(bytes(range(256)) * (BIG // 256))
        info = api.upload(str(source), name="给对方的.bin")

        dest = tmp_path / "下载" / "给对方的.bin"
        seen: list[tuple[int, int]] = []
        api.download(str(info["id"]), str(dest), root=str(tmp_path),
                     on_progress=lambda done, total: seen.append((done, total)))

        # 落盘**可能是密文**：静态加密一旦在当前进程里生效（比如整套测试里先跑过
        # test_atrest），下载下来的东西就是 MCHATENC1 开头的密文 —— 所以必须走
        # materialize() 拿明文来比，直接 dest.read_bytes() 会在全量跑里红。
        assert Path(materialize(str(dest))).read_bytes() == source.read_bytes(), \
            "下下来的字节要和传上去的一模一样（明文）"
        done = [value for value, _ in seen]
        assert done, "一块进度都没报"
        assert done == sorted(done), "进度必须单调不减"
        assert done[-1] == BIG, "最后一次要正好等于文件大小"
        assert all(total == BIG for _, total in seen), \
            f"Content-Length 该等于明文大小，实测首档 {seen[0]}"


class _SlowFakeApi:
    """只实现 ``download``：先报一半、卡住等测试断言，再报满并落盘。

    要验的是"后台线程 → Qt 信号 → 气泡"这条链路（真 ApiClient 会打网络，慢且不稳）。
    """

    def __init__(self):
        self.reached_half = threading.Event()
        self.release = threading.Event()

    def download(self, attachment_id, dest, *, root="", on_progress=None):
        if on_progress is not None:
            on_progress(50, 100)
        self.reached_half.set()
        self.release.wait(timeout=6)
        if on_progress is not None:
            on_progress(100, 100)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"downloaded")
        return dest


def test_download_progress_reaches_the_bubble(app, window):
    window._api = _SlowFakeApi()
    message = add_remote_attachment_message(window, message_id="m_fetch")
    bubble = window.chat_view.message_list.bubbles()["m_fetch"]
    assert bubble.download is not None, "别人发来还没取回的附件该有一条下载线"
    assert not bubble.download.isVisible(), "没在下载时不该占地方"

    assert window.download_attachment(message.id) is True
    assert window._api.reached_half.wait(timeout=5)
    assert wait_for(app, lambda: window.chat_view.download_text("m_fetch") == "下载中 50%",
                    timeout_ms=4000), window.chat_view.download_text("m_fetch")
    assert bubble.download.isVisible()

    window._api.release.set()
    assert wait_for(app, lambda: not window.chat_view.download_text("m_fetch"),
                    timeout_ms=6000), "下完之后那条线该收掉（附件已经是本地的了）"


class _FailingApi:
    def download(self, attachment_id, dest, *, root="", on_progress=None):
        raise OSError("连不上服务器")


def test_download_failure_says_click_to_retry(app, window):
    window._api = _FailingApi()
    message = add_remote_attachment_message(window, message_id="m_fetch_fail")
    assert window.download_attachment(message.id) is True
    assert wait_for(app, lambda: "下载失败" in window.chat_view.download_text("m_fetch_fail"),
                    timeout_ms=5000), window.chat_view.download_text("m_fetch_fail")
    text = window.chat_view.download_text("m_fetch_fail")
    assert "点一下重试" in text, text
    assert "连不上服务器" in text, "失败原因要露出来，不然用户只看到「没下来」"


def test_upload_and_download_wording_differs(app, window, tmp_path):
    """同一条线两种措辞：上传失败会自动重发，下载失败得用户再点一下。"""
    path = tmp_path / "我发的.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    add_attachment_message(window, str(path), message_id="m_mine")
    mine = window.chat_view.message_list.bubbles()["m_mine"]
    theirs = add_remote_attachment_message(window, message_id="m_theirs")
    their_bubble = window.chat_view.message_list.bubbles()[theirs.id]

    mine.set_upload_failed("网络断了")
    their_bubble.set_download_failed("网络断了")
    assert "上传失败，正在重试" in mine.upload_text()
    assert "下载失败，点一下重试" in their_bubble.download_text()
