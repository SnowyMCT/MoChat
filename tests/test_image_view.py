"""图片查看器（点图片看大图）的用例：组件本身 + 接进聊天窗口之后的整条链路。

覆盖：

* 查看器：比例（适应窗口 / 原始大小 / 滚轮缩放）、画布尺寸不超视口、翻页到头禁用、
  文件不在/还在下载时的降级文案、主题样式跟着走；
* 接进来之后：**点气泡里的图片**（真的发一次鼠标事件）打开查看器、`‹ ›` 翻的是
  本会话里的图、图片不在本地时给提示而不是静默失败、右键菜单多了「查看图片」、
  还在服务器上的图会先下载再打开、查看器里的「另存为」把**明文**复制出去、
  主窗口关闭时一起关。

用法：``.venv/Scripts/python.exe tests/test_image_view.py``（也能被 ``pytest tests`` 收集）。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QPoint, QSettings, Qt                          # noqa: E402
from PyQt6.QtGui import QPixmap                                          # noqa: E402
from PyQt6.QtTest import QTest                                           # noqa: E402
from PyQt6.QtWidgets import QApplication                                 # noqa: E402

from mchat.core import assets as core_assets                             # noqa: E402
import seed                                                              # noqa: E402
from mchat.core.db import ChatLog                                        # noqa: E402
from mchat.core.models import Attachment, Message                        # noqa: E402
from mchat.core.transport import LocalTransport                          # noqa: E402
from mchat.ui import theme as theme_module                               # noqa: E402
from mchat.ui.image_view import ImageViewerDialog                        # noqa: E402
from mchat.ui.main_window import MainWindow                              # noqa: E402
from mchat.ui.theme import THEME_DARK, THEME_LIGHT                       # noqa: E402


# --------------------------------------------------------------------- 工具
@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def pump(application, ms=180) -> None:
    """推进事件循环（异步回调和动画都靠它）。"""
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


def wait_for(application, predicate, timeout=10.0, step=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        application.processEvents()
        time.sleep(step)
    return bool(predicate())


@pytest.fixture()
def sample_images(tmp_path):
    """两张真实的 PNG（用仓库里的演示图复制出来，别动素材原件）。"""
    source = core_assets.demo_path(core_assets.DEMO_PHOTO)
    assert os.path.isfile(source), "演示图片素材不见了：%s" % source
    paths = []
    for name in ("风景 1.png", "风景 2.png"):
        target = tmp_path / name
        shutil.copyfile(source, target)
        paths.append(str(target))
    return paths


def build_viewer(app, paths, *, palette=None) -> ImageViewerDialog:
    viewer = ImageViewerDialog()
    viewer.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    viewer.set_images([("m_%d" % index, path, os.path.basename(path))
                       for index, path in enumerate(paths)])
    viewer.apply_palette(palette or theme_module.LIGHT)
    viewer.resize(760, 520)
    viewer.show()
    pump(app, 120)
    return viewer


@pytest.fixture()
def window(app, monkeypatch, tmp_path):
    """一个真窗口（演示数据 + 本地传输层 + 内存库），和 test_ui.py 同一套接法。"""
    settings = QSettings("MChatImageViewTest", "MChatImageViewTest")
    settings.clear()
    log = ChatLog(":memory:")
    demo = seed.build_demo()
    ticks = [time.time()]
    transport = LocalTransport(demo.scripts, clock=lambda: ticks[0])
    saved: list[str] = []
    save_dir = tmp_path / "saved"
    save_dir.mkdir()

    def fake_save(suggested: str = "", parent=None) -> str:
        """替掉「另存为」对话框：固定存到 tmp 下（模态窗口在无头环境会一直等）。"""
        saved.append(suggested)
        return str(save_dir / (suggested or "out.png"))

    win = MainWindow(demo=demo, transport=transport, settings=settings, log=log,
                     confirm=lambda **kw: True, filedialog=lambda kind, parent=None: "",
                     savedialog=fake_save)
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    QApplication.setActiveWindow(win)
    pump(app, 320)
    win._test_saved = saved               # 断言"另存为"被问过
    win._test_saved_dir = str(save_dir)
    yield win
    win.close()
    win.deleteLater()
    pump(app, 60)


def add_image_message(win, path: str, *, message_id: str, name: str = "",
                     conversation_id: str = seed.CHAT_LIN, outgoing: bool = False) -> Message:
    """往仓库里塞一条"带图片"的消息（``path`` 可以是 ``remote://`` 标记）。"""
    if path.startswith(Attachment.REMOTE_SCHEME):
        attachment = Attachment.remote(path[len(Attachment.REMOTE_SCHEME):],
                                       name or "还在服务器上.png", 100)
    else:
        attachment = Attachment(path=os.path.abspath(path),
                                name=name or os.path.basename(path))
    message = Message(id=message_id, conversation_id=conversation_id, sender_id="peer",
                      text="", ts=time.time(), outgoing=outgoing, attachment=attachment)
    win.store.append(message)
    return message


# ------------------------------------------------------------------ 查看器
def test_viewer_shows_the_image_and_fits_it(app, sample_images):
    viewer = build_viewer(app, sample_images)
    try:
        assert viewer.has_image(), viewer.note_text()
        assert viewer.count() == 2 and viewer.index() == 0
        assert viewer.title_text() == "风景 1.png"
        assert "风景 1.png" in viewer.info_text()

        pixmap = QPixmap(sample_images[0])
        # 适应窗口：塞得进视口、而且不放大超过 100%（小图放大只会更糊）
        assert viewer.in_fit_mode()
        assert viewer.zoom_percent() <= 100
        size = viewer.canvas_size()
        viewport = viewer.area.viewport().size()
        assert size.width() <= viewport.width() and size.height() <= viewport.height()
        # 等比：缩放前后宽高比一致（不裁切、也不拉伸）
        assert abs(size.width() / size.height()
                   - pixmap.width() / pixmap.height()) < 0.01
    finally:
        viewer.close()


def test_viewer_zoom_and_fit_toggle(app, sample_images):
    viewer = build_viewer(app, sample_images)
    try:
        viewer.actual_size()
        assert viewer.zoom_percent() == 100 and not viewer.in_fit_mode()
        viewer.zoom_in()
        assert viewer.zoom_percent() > 100
        viewer.zoom_out()
        assert viewer.zoom_percent() <= 100
        viewer.set_zoom(5000)                    # 上限夹取
        assert viewer.zoom_percent() <= 800
        viewer.set_zoom(1)                       # 下限夹取
        assert viewer.zoom_percent() >= 10
        viewer.fit_to_window()
        assert viewer.in_fit_mode()
        viewer.toggle_fit()                      # 双击 = 切换
        assert not viewer.in_fit_mode() and viewer.zoom_percent() == 100
        viewer.toggle_fit()
        assert viewer.in_fit_mode()
    finally:
        viewer.close()


def test_viewer_navigation_disables_at_the_ends(app, sample_images):
    viewer = build_viewer(app, sample_images)
    try:
        assert viewer.nav_state() == (False, True)          # 第一张：没有上一张
        assert viewer.next_image() is True
        assert viewer.index() == 1 and viewer.current_path() == sample_images[1]
        assert viewer.title_text() == "风景 2.png"
        assert "第 2/2 张" in viewer.info_text()
        assert viewer.nav_state() == (True, False)          # 最后一张：没有下一张
        assert viewer.next_image() is False
        assert viewer.prev_image() is True and viewer.index() == 0
        assert viewer.prev_image() is False
    finally:
        viewer.close()


def test_viewer_single_image_has_no_paging(app, sample_images):
    viewer = build_viewer(app, sample_images[:1])
    try:
        assert viewer.count() == 1
        assert viewer.nav_state() == (False, False)
        assert "第" not in viewer.info_text()
    finally:
        viewer.close()


def test_viewer_degrades_when_the_file_is_gone(app, sample_images, tmp_path):
    missing = str(tmp_path / "已经被删掉的图.png")
    viewer = build_viewer(app, [missing])
    try:
        assert viewer.has_image() is False
        assert "打不开" in viewer.note_text()
        assert not viewer.nav_state()[1]
    finally:
        viewer.close()


def test_viewer_shows_downloading_state(app, sample_images):
    viewer = ImageViewerDialog()
    try:
        viewer.apply_palette(theme_module.DARK)
        viewer.set_loading("还没下完的图.png", message_id="m_x")
        assert viewer.has_image() is False
        assert viewer.current_path() == ""
        assert "正在下载" in viewer.info_text()
    finally:
        viewer.close()


def test_viewer_follows_the_theme(app, sample_images):
    viewer = build_viewer(app, sample_images, palette=theme_module.LIGHT)
    try:
        light_qss = viewer.styleSheet()
        viewer.apply_palette(theme_module.DARK)
        dark_qss = viewer.styleSheet()
        assert theme_module.LIGHT.surface in light_qss
        assert theme_module.DARK.surface in dark_qss and dark_qss != light_qss
        # 画布底色是固定的深色（不跟主题变白），按钮/信息条走主题
        assert theme_module.IMAGE_CANVAS in dark_qss and theme_module.DARK.card in dark_qss
        assert "QPushButton#viewerBtn" in dark_qss
    finally:
        viewer.close()


# ------------------------------------------------------------ 接进聊天窗口
def test_clicking_the_image_bubble_opens_the_viewer(app, window, sample_images):
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 160)
    message = add_image_message(window, sample_images[0], message_id="img-a")
    window.chat_view.show_conversation(window.store.get(seed.CHAT_LIN))
    pump(app, 160)

    bubble = window.chat_view.message_list.bubbles()["img-a"]
    assert bubble.image_view is not None and bubble.image_view.has_image()
    assert "查看大图" in bubble.image_view.toolTip(), bubble.image_view.toolTip()

    QTest.mouseClick(bubble.image_view, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(4, 4))
    pump(app, 200)

    viewer = window.image_viewer()
    assert viewer is not None, "点图片应该打开查看器"
    assert viewer.has_image() and viewer.current_path() == sample_images[0]
    assert viewer.count() == 1 and viewer.index() == 0
    assert message.attachment.path == sample_images[0]


def test_viewer_pages_through_the_conversation(app, window, sample_images):
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 160)
    add_image_message(window, sample_images[0], message_id="img-1")
    add_image_message(window, sample_images[1], message_id="img-2")
    pump(app, 120)

    assert window.open_image_view("img-2") is not None
    viewer = window.image_viewer()
    assert viewer.count() == 2 and viewer.index() == 1, "点第二张就该从第二张看起"
    assert viewer.nav_state() == (True, False)
    viewer.prev_image()
    assert viewer.current_path() == sample_images[0]
    # 会话里只有一张图时不该出现翻页暗示
    assert window.open_image_view("img-1") is not None
    assert viewer.count() == 2


def test_image_menu_and_missing_file(app, window, sample_images, tmp_path):
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 160)
    add_image_message(window, sample_images[0], message_id="img-ok")
    gone = str(tmp_path / "早就没有了的图.png")
    add_image_message(window, gone, message_id="img-gone")
    window.chat_view.show_conversation(window.store.get(seed.CHAT_LIN))
    pump(app, 160)

    menu = window.chat_view._build_bubble_menu("img-ok")
    labels = [action.text() for action in menu.actions()]
    assert "查看图片" in labels, labels
    assert any(text.startswith("打开「") for text in labels)
    assert "打开所在文件夹" in labels

    # 图片不在本地：给提示，别静默失败
    assert window.attachment_action("img-gone", "view") is False
    assert "不在这个位置" in window.status_text(), window.status_text()
    assert window.open_image_view("img-gone") is None
    # 只有本地存在的图会进查看器的翻页列表
    assert [item[0] for item in window._conversation_images(seed.CHAT_LIN)] == ["img-ok"]
    # 对着不是图片的消息点"看大图"：安静地返回 False
    other = window.store.get(seed.CHAT_LIN).messages[0]
    assert window.attachment_action(other.id, "view") is False


def test_remote_image_downloads_first_then_opens(app, window, sample_images):
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 120)
    message = add_image_message(window, Attachment.REMOTE_SCHEME + "f_remote",
                                message_id="img-remote", name="服务器上的图.png")
    assert message.attachment.is_remote

    calls = []
    original = window.download_attachment

    def record(message_id, **kwargs):
        calls.append((message_id, kwargs))
        return True

    window.download_attachment = record
    try:
        assert window.view_image("img-remote") is True
        assert calls == [("img-remote", {"view_after": True})], calls
    finally:
        window.download_attachment = original

    # 下完之后（回调那一层）：附件换成本地文件、直接在查看器里打开
    window._on_action_done({"kind": "download", "message_id": "img-remote",
                            "path": sample_images[0], "view": True})
    pump(app, 200)
    assert not window._store.message("img-remote").attachment.is_remote
    viewer = window.image_viewer()
    assert viewer is not None and viewer.current_path() == sample_images[0]


def test_save_as_writes_the_plain_file(app, window, sample_images):
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 120)
    add_image_message(window, sample_images[0], message_id="img-save")

    assert window._save_image("img-save") is True
    assert window._test_saved == ["风景 1.png"], window._test_saved
    target = os.path.join(window._test_saved_dir, "风景 1.png")
    assert os.path.isfile(target), target
    assert open(target, "rb").read() == open(sample_images[0], "rb").read()
    assert "已保存" in window.status_text(), window.status_text()
    # 用户取消（假的对话框返回空串）→ 什么都不做、也不提示
    window._save_pick = lambda suggested="", parent=None: ""
    assert window._save_image("img-save") is False


def test_viewer_survives_theme_switch_and_closes_with_the_window(app, window, sample_images):
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 120)
    add_image_message(window, sample_images[0], message_id="img-theme")
    assert window.open_image_view("img-theme") is not None
    viewer = window.image_viewer()
    assert theme_module.DARK.surface in viewer.styleSheet() or \
        theme_module.LIGHT.surface in viewer.styleSheet()

    window.set_theme(THEME_DARK)
    pump(app, 400)
    assert theme_module.DARK.surface in viewer.styleSheet(), "切主题时查看器要跟着换"

    window.shutdown()
    pump(app, 120)
    assert not viewer.isVisible(), "关主窗口时查看器要一起关"
