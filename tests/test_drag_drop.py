"""拖拽 / 粘贴文件进窗口 → 待发送条（无头、不弹窗、不碰真实剪贴板）。

覆盖：窗口级拖放的"接"与"不接"（本地文件接、纯文字不接）、落点提示的显隐与居中、
输入框（QTextEdit 默认会吃掉拖放）把文件转发成附件而不是插成一串路径、
一次拖多个只带第一个并明说、拖到不存在的路径要提示而不是静默、
资源管理器里复制文件再 Ctrl+V（走 urls 那条路），
外加一条回归：粘贴图片这条老路没被这次改动弄坏。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                       # noqa: E402
from PyQt6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl   # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication                            # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed                                                         # noqa: E402
from mchat.core import assets as core_assets                        # noqa: E402
from mchat.core.db import ChatLog                                   # noqa: E402
from mchat.core.transport import LocalTransport                     # noqa: E402
from mchat.ui.main_window import MainWindow                         # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(application, ms=140) -> None:
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def files(tmp_path):
    """拖进来用的真实文件：一张真 PNG + 一个文本文件（都放 tmp，别动素材原件）。"""
    png = tmp_path / "风景.png"
    shutil.copyfile(core_assets.demo_path(core_assets.DEMO_PHOTO), png)
    note = tmp_path / "笔记.txt"
    note.write_text("hello", encoding="utf-8")
    return str(png), str(note)


@pytest.fixture()
def window(app):
    settings = QSettings("MChatDragDropTest", "MChatDragDropTest")
    settings.clear()
    demo = seed.build_demo()
    ticks = [time.time()]
    transport = LocalTransport(demo.scripts, clock=lambda: ticks[0])
    win = MainWindow(demo=demo, transport=transport, settings=settings,
                     log=ChatLog(":memory:"), confirm=lambda **kw: True,
                     filedialog=lambda kind, parent=None: "")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    QApplication.setActiveWindow(win)
    pump(app, 260)
    toasts: list[tuple] = []
    win.toast.show_message = lambda *args, **kwargs: toasts.append(args)
    win._test_toasts = toasts
    yield win
    win.close()
    win.deleteLater()
    pump(app, 40)


def mime_with_files(*paths) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    return mime


def drag_enter(widget, mime) -> QDragEnterEvent:
    """走 sendEvent 而不是直接调 handler：想验的是"这条事件真能被接住"。

    **drop 必须先经过 drag_enter**：Qt 把 Drop 投给"上一次 DragEnter 认下的那个目标"，
    单独发一个 Drop 不会落到窗口上（真机上也永远是 enter → move → drop 这个顺序）。
    """
    event = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)
    return event


def drop(widget, mime) -> QDropEvent:
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)
    return event


def toasts_of(window) -> list[str]:
    return [str(args[0]) for args in window._test_toasts]


# ------------------------------------------------------------------ 窗口级拖放
def test_window_accepts_local_file_and_shows_centered_hint(app, window, files):
    png, _ = files
    event = drag_enter(window, mime_with_files(png))
    assert event.isAccepted(), "拖本地文件进窗口，窗口应该接住"
    hint = window._drop_hint
    assert hint.isVisible(), "拖着的时候要给一个落点提示"
    assert abs(hint.x() - (window.width() - hint.width()) // 2) <= 1, "提示要居中"
    assert abs(hint.y() - (window.height() - hint.height()) // 2) <= 1


def test_text_drag_is_left_to_qt(app, window):
    mime = QMimeData()
    mime.setText("一段普通文字")
    event = drag_enter(window, mime)
    assert not event.isAccepted(), "拖文字过来不该被当成附件"
    assert not window._drop_hint.isVisible()


def test_drop_file_lands_in_pending_tray(app, window, files):
    png, _ = files
    drag_enter(window, mime_with_files(png))
    drop(window, mime_with_files(png))
    assert not window._drop_hint.isVisible(), "松手之后提示要收掉"
    assert window.chat_view.has_pending_attachment(), "拖进来的文件要挂到待发送条"
    attachment = window.chat_view.pending_attachment()
    assert attachment is not None and os.path.samefile(attachment.path, png)
    assert attachment.name == "风景.png"


def test_drag_leave_hides_hint(app, window, files):
    png, _ = files
    drag_enter(window, mime_with_files(png))
    assert window._drop_hint.isVisible()
    QApplication.sendEvent(window, QDragLeaveEvent())          # 拖出去 / 按 Esc 取消
    assert not window._drop_hint.isVisible()


def test_multiple_files_take_the_first_and_say_so(app, window, files):
    png, note = files
    drag_enter(window, mime_with_files(png, note))
    drop(window, mime_with_files(png, note))
    attachment = window.chat_view.pending_attachment()
    assert attachment is not None and os.path.samefile(attachment.path, png)
    assert any("一次只能带一个" in text for text in toasts_of(window)), toasts_of(window)


def test_missing_file_warns_instead_of_silence(app, window, tmp_path):
    window.attach_paths([str(tmp_path / "根本没有这个文件.txt")])
    assert not window.chat_view.has_pending_attachment()
    assert any("不是本地文件" in text for text in toasts_of(window)), toasts_of(window)


def test_empty_drop_paths_warns(app, window):
    assert window.attach_paths([]) is None
    assert any("不是本地文件" in text for text in toasts_of(window)), toasts_of(window)


# ------------------------------------------------------- 输入框（QTextEdit）那条路
def test_composer_drop_forwards_to_window(app, window, files):
    """QTextEdit 默认自己接受拖放，不拦的话拖到输入框会被它吃掉（或插成一串路径）。"""
    png, _ = files
    edit = window.chat_view.composer.edit
    assert drag_enter(edit, mime_with_files(png)).isAccepted()
    drop(edit, mime_with_files(png))
    assert window.chat_view.has_pending_attachment(), "输入框上的拖放要转发成附件"
    assert window.chat_view.input_text() == "", "不该把文件路径当文字插进输入框"


def test_composer_text_drop_still_inserts_text(app, window):
    """拖文字进来照旧插文字（别把默认行为一起拦掉）。"""
    mime = QMimeData()
    mime.setText("拖进来的文字")
    window.chat_view.composer.edit.insertFromMimeData(mime)
    assert window.chat_view.input_text() == "拖进来的文字"


def test_paste_files_from_explorer_becomes_attachment(app, window, files):
    """资源管理器里 Ctrl+C 一个文件 → 输入框 Ctrl+V = 当附件发（QQ/Telegram 同款）。"""
    _, note = files
    window.chat_view.composer.edit.insertFromMimeData(mime_with_files(note))
    attachment = window.chat_view.pending_attachment()
    assert attachment is not None and attachment.name == "笔记.txt"
    assert window.chat_view.input_text() == ""


def test_paste_image_regression(app, window):
    """老路回归：剪贴板里是图片 → 落成 PNG 当附件（这次改动不能把它弄丢）。"""
    mime = QMimeData()
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(0xFF3390EC)
    mime.setImageData(image)
    window.chat_view.composer.edit.insertFromMimeData(mime)
    attachment = window.chat_view.pending_attachment()
    assert attachment is not None and attachment.kind == "image"
    assert os.path.isfile(attachment.path), "粘贴的图片要先落成真正的文件"
