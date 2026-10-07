"""「自己发出去的附件，本地删了还能从服务器取回」的用例。

用户报的问题：图片发出去以后把本地文件删了，消息只说"图片不在本地了" —— 可服务器上
明明还留着那份（那条消息引用的附件，只要消息还在就不会被孤儿回收）。根因是客户端**没有
记住上传返回的附件 id**，于是既不是"还没下载"、也没有 id 可取。

覆盖：模型字段与老库兼容、传输层报出 id、界面文案与取回动作、查看器翻页取回、
真服务端端到端（删掉本地文件后按记下的 id 取回来，字节一致）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QSettings, Qt                              # noqa: E402
from PyQt6.QtWidgets import QApplication                            # noqa: E402

from mchat.core import assets as core_assets                        # noqa: E402
import seed                                                         # noqa: E402
from mchat.core.api import ApiClient                                # noqa: E402
from mchat.core.db import ChatLog                                   # noqa: E402
from mchat.core.models import Attachment, Message                   # noqa: E402
from mchat.core.net_transport import NetworkTransport               # noqa: E402
from mchat.core.transport import (EVENT_ATTACHMENT_SENT, LocalTransport,   # noqa: E402
                                  Outgoing)
from mchat.ui.image_view import ImageViewerDialog                   # noqa: E402
from mchat.ui.main_window import MainWindow                         # noqa: E402
from server_fixture import make_friends, running_server             # noqa: E402

ME = "u_me"


def pump(application, ms=160) -> None:
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


# --------------------------------------------------------------------- 模型
def test_attachment_remembers_the_server_id(tmp_path):
    """server_id 能落库、能读回；老记录（没这一项）读成空串，不会炸。"""
    attachment = Attachment.from_path(str(tmp_path / "图.png"))
    assert attachment.server_id == "" and attachment.source_id == ""

    remote = Attachment.remote("f_abc", "图.png", 10)
    assert remote.source_id == "f_abc"                 # 还没下载：看路径标记

    sent = Attachment(path=str(tmp_path / "图.png"), name="图.png", size=10,
                      server_id="f_xyz")
    assert sent.source_id == "f_xyz"

    data = sent.to_json()
    assert data["server_id"] == "f_xyz"
    assert Attachment.from_json(data).server_id == "f_xyz"
    # 老库里的 JSON 没有 server_id：读得出来，就是空串
    old = {"path": sent.path, "name": sent.name, "size": sent.size}
    assert Attachment.from_json(old).server_id == ""


def test_recoverable_and_localized_at(tmp_path):
    """本地文件删了但记得服务器 id ⇒ 可取回；取回之后仍然记得那个 id。"""
    path = tmp_path / "照片.png"
    path.write_bytes(b"PNG")
    attachment = Attachment(path=str(path), name="照片.png", size=3, server_id="f_1")
    assert not attachment.recoverable, "文件还在本地，谈不上取回"

    os.remove(path)
    assert attachment.recoverable and attachment.source_id == "f_1"

    fetched = tmp_path / "取回的.png"
    fetched.write_bytes(b"PNG")
    restored = attachment.localized_at(str(fetched))
    assert not restored.recoverable and restored.exists
    assert restored.server_id == "f_1", "取回后还得记得服务器那份 id"
    assert restored.name == "照片.png", "名字不能被取回动作抹掉"

    # 没有 server_id 的老数据：删了就是删了（保持老行为，不改口）
    plain = Attachment(path=str(tmp_path / "没有的.png"), name="没有的.png")
    assert not plain.recoverable


# ----------------------------------------------------------------- 传输层
class _FakeApi(object):
    """只够跑通"上传 → 发消息"这条路的假客户端。"""

    def __init__(self):
        self.sent = []
        self.uploads = []

    def upload(self, path, name=None, on_progress=None):
        # 真客户端会传 on_progress=（附件上传进度），假客户端收下不用，但签名得对得上
        self.uploads.append((path, name))
        return {"id": "f_uploaded_1", "name": name or os.path.basename(path), "size": 3}

    def send_message(self, conversation_id, client_id, text, **kwargs):
        self.sent.append({"id": client_id, "text": text, **kwargs})
        return {"id": client_id, "text": text, "attachment": kwargs.get("attachment_id")}


def test_transport_reports_the_server_side_attachment_id(tmp_path):
    """发出去的附件：传输层要把"服务端那份的 id"作为事件交给界面（否则记不下来）。"""
    local = tmp_path / "图.png"
    local.write_bytes(b"PNG")
    fake = _FakeApi()
    transport = NetworkTransport(fake, me_id=ME, autostart=False)
    try:
        transport.send(Outgoing("c1", ME, "", "msg-1",
                                attachment=Attachment.from_path(str(local))))
        transport._flush_outbox()
        assert fake.uploads, "本地附件要先上传"
        events = [event for event in transport.poll()
                  if event.kind == EVENT_ATTACHMENT_SENT]
        assert len(events) == 1, "应该报一条「附件已上传」事件"
        assert events[0].message_id == "msg-1"
        assert events[0].attachment_id == "f_uploaded_1"
        assert fake.sent[0]["attachment_id"] == "f_uploaded_1"
    finally:
        transport.stop()


# ------------------------------------------------------------------- 界面
@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def window(app):
    settings = QSettings("MChatRecoverTest", "MChatRecoverTest")
    settings.clear()
    log = ChatLog(":memory:")
    demo = seed.build_demo()
    transport = LocalTransport(demo.scripts, clock=lambda: time.time())
    win = MainWindow(demo=demo, transport=transport, settings=settings, log=log,
                     confirm=lambda **kw: True, filedialog=lambda kind, parent=None: "",
                     savedialog=lambda suggested="", parent=None: "")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    # **不要** setActiveWindow：那是 Qt 的全局状态，而"别人发消息要不要弹通知"
    # 正是按 isActiveWindow() 判的（见 test_ui_friends_notify）—— 在这里设一次，
    # 后面那套用例就会误判成"用户正在看这个会话"，通知断言随之变红。
    pump(app, 300)
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 120)
    yield win
    win.close()
    win.deleteLater()
    pump(app, 60)


def add_sent_image(window, folder: str, *, message_id: str, server_id: str = "") -> Message:
    """造一条"我发出去的图片"消息（本地文件真的存在，测试再按需删掉它）。"""
    path = os.path.join(folder, f"{message_id}.png")
    shutil.copyfile(core_assets.demo_path(core_assets.DEMO_PHOTO), path)
    attachment = Attachment(path=path, name=f"{message_id}.png", server_id=server_id)
    message = Message(id=message_id, conversation_id=seed.CHAT_LIN, sender_id=ME,
                      text="", ts=time.time(), outgoing=True, attachment=attachment)
    window.store.append(message)
    window.chat_view.show_conversation(window.store.get(seed.CHAT_LIN))
    return message


def test_sent_attachment_records_the_id_and_stores_it(window):
    """界面收到"附件已上传"事件后：内存里记住 id，库里也存下来（重启后还在）。"""
    folder = tempfile.mkdtemp()
    try:
        message = add_sent_image(window, folder, message_id="img-send")
        event = type("E", (), {"message_id": message.id, "attachment_id": "f_777"})()
        window._apply_attachment_sent(event)
        assert window.store.message(message.id).attachment.server_id == "f_777"
        rows = window._log.load(seed.CHAT_LIN, limit=100)
        same = [item for item in rows if item.id == message.id]
        assert same and same[0].attachment.server_id == "f_777", "必须落库，否则重启就忘了"
        # 同一条事件重复到达（重发/多设备）：不重复写库、也不改坏
        window._apply_attachment_sent(event)
        assert window.store.message(message.id).attachment.server_id == "f_777"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_deleting_the_local_file_says_it_can_be_fetched(window, app):
    """本地那份删掉之后：文案说清"服务器上还有、点一下取回"，点击触发取回而不是报错。"""
    folder = tempfile.mkdtemp()
    try:
        message = add_sent_image(window, folder, message_id="img-gone-sent",
                                 server_id="f_888")
        os.remove(message.attachment.path)
        # 缩略图有一层内存缓存（切会话重画时不重复解码）：先清掉，模拟"重启后重新画"
        from mchat.ui import attach as attach_view
        attach_view._PREVIEW_CACHE.clear()
        window.chat_view.show_conversation(window.store.get(seed.CHAT_LIN))
        pump(app, 160)

        bubble = window.chat_view.message_list.bubbles()["img-gone-sent"]
        assert bubble.image_view is not None
        assert bubble.image_view.has_image() is False
        text = bubble.image_view.text()
        assert "服务器上还有" in text and "取回" in text, text
        assert "取回" in bubble.image_view.toolTip(), bubble.image_view.toolTip()

        # 点一下 = 去服务器取回（不是弹"文件已经不在这个位置了"）
        calls = []
        original = window.download_attachment
        window.download_attachment = lambda mid, **kw: calls.append((mid, kw)) or True
        try:
            assert window.attachment_action("img-gone-sent", "view") is True
            assert calls == [("img-gone-sent", {"view_after": True})], calls
            assert "不在这个位置" not in window.status_text(), window.status_text()
            # "打开所在文件夹"这时只能先提示"先取回来"
            assert window.attachment_action("img-gone-sent", "folder") is False
            assert "取回来" in window.status_text(), window.status_text()
        finally:
            window.download_attachment = original
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_fetching_restores_the_bubble_and_keeps_the_server_id(window, app):
    """取回完成后：气泡恢复成图片、附件仍带着服务器 id（下次删了还能再取）。"""
    folder = tempfile.mkdtemp()
    try:
        message = add_sent_image(window, folder, message_id="img-fetch",
                                 server_id="f_999")
        os.remove(message.attachment.path)
        fetched = os.path.join(folder, "取回来的.png")
        shutil.copyfile(core_assets.demo_path(core_assets.DEMO_PHOTO), fetched)

        window._on_action_done({"kind": "download", "message_id": "img-fetch",
                                "path": fetched, "view": True})
        pump(app, 220)
        restored = window.store.message("img-fetch").attachment
        assert restored.exists and restored.server_id == "f_999"
        assert restored.name == "img-fetch.png"
        viewer = window.image_viewer()
        assert viewer is not None and viewer.current_path() == fetched
        bubble = window.chat_view.message_list.bubbles()["img-fetch"]
        assert bubble.image_view is not None and bubble.image_view.has_image()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_viewer_navigation_fetches_missing_images_once(window, app):
    """查看器翻到"本地没有、服务器上还有"的图：自动取回一次，不会反复触发。"""
    folder = tempfile.mkdtemp()
    viewer = ImageViewerDialog()
    try:
        viewer.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window._image_viewer = viewer
        viewer.imageChanged.connect(window._on_viewer_image_changed)
        message = add_sent_image(window, folder, message_id="img-nav", server_id="f_555")
        os.remove(message.attachment.path)

        calls = []
        original = window.download_attachment
        window.download_attachment = lambda mid, **kw: calls.append((mid, kw)) or True
        try:
            window._on_viewer_image_changed("img-nav")
            window._on_viewer_image_changed("img-nav")      # 已经在取回中 → 不重复触发
            assert calls == [("img-nav", {"view_after": True})], calls
            # 取回失败（比如没网）时标记要放回去，允许下次重试
            window._fetching.clear()
            window.download_attachment = lambda mid, **kw: calls.append((mid, kw)) or False
            window._on_viewer_image_changed("img-nav")
            assert "img-nav" not in window._fetching, "失败之后要允许重试"
        finally:
            window.download_attachment = original
    finally:
        viewer.close()
        shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------- 真服务端
def test_recovery_against_a_real_server(tmp_path):
    """真服务端：上传并发出引用它的消息 → 删掉本地那份 → 按记下的 id 取回，字节一致。"""
    payload = b"REAL-IMAGE-BYTES" * 32
    local = tmp_path / "本地那份.png"
    local.write_bytes(payload)
    with running_server() as live:
        alice = ApiClient(live.base_url)
        alice.token = str(alice.register("recover_alice", "secret123", "爱丽丝")["token"])
        bob = ApiClient(live.base_url)
        bob.token = str(bob.register("recover_bob", "secret123", "鲍勃")["token"])
        cid = alice.create_direct("recover_bob")["id"]
        make_friends(alice, bob)

        info = alice.upload(str(local), name="风景.png")      # 客户端发附件时干的事
        alice.send_message(cid, "a" * 32, "看这张", attachment_id=info["id"])

        # 消息里记住的"服务器那份 id"（界面记下来的就是它）
        attachment = Attachment(path=str(local), name="风景.png", size=len(payload),
                                server_id=info["id"])
        os.remove(local)                                      # 用户把本地那份删了
        assert attachment.recoverable

        dest = tmp_path / "取回来.png"
        alice.download(attachment.source_id, str(dest))
        assert dest.read_bytes() == payload, "服务器上那份必须还在、还能取回"
        # 取回到本地之后，再删一次也还能再取（id 一直在）
        fetched = attachment.localized_at(str(dest))
        os.remove(dest)
        assert fetched.recoverable and fetched.source_id == info["id"]
