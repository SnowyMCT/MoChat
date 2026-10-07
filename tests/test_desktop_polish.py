"""桌面端成套补齐的四件事：通知点击跳转 / 链接可点 / 未读分隔线 / 关窗到托盘。

都走真实部件与真实信号链（无头、WA_DontShowOnScreen、不弹窗、不碰真实剪贴板）：
* 链接：`flag_text` 一趟渲染出 `<a>`，气泡把它连成可点，主窗口交给系统浏览器
  （浏览器用记录器替换，真开会把测试机弹一脸窗口）；`file://` 之类一律不打开。
* 通知：点通知气泡 → 回到那条消息的会话（`QSystemTrayIcon.messageClicked` 那条路）。
* 未读分隔线：只插在**第一条未读**之前，位置用坐标断言（不是"存在即通过"）。
* 关窗：默认 ✕ = 退出；开了开关才收进托盘；托盘菜单「退出」在开关打开时仍必须真退。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                       # noqa: E402
from PyQt6.QtCore import QSettings, Qt                               # noqa: E402
from PyQt6.QtWidgets import QApplication                            # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed                                                         # noqa: E402
from mchat.core.db import ChatLog                                   # noqa: E402
from mchat.core.models import Message                               # noqa: E402
from mchat.core.transport import LocalTransport                     # noqa: E402
from mchat.ui import flag_text                                      # noqa: E402
from mchat.ui import main_window as main_window_module              # noqa: E402
from mchat.ui.main_window import MainWindow                         # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(application=None, ms=160) -> None:
    application = application or QApplication.instance()
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def window(app):
    settings = QSettings("MChatPolishTest", "MChatPolishTest")
    settings.clear()
    demo = seed.build_demo()
    transport = LocalTransport(demo.scripts, clock=time.time)
    win = MainWindow(demo=demo, transport=transport, settings=settings,
                     log=ChatLog(":memory:"), confirm=lambda **kw: True,
                     filedialog=lambda kind, parent=None: "")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    QApplication.setActiveWindow(win)
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 240)
    yield win
    win._quitting = True                 # 别让"收进托盘"把收尾卡住
    win.close()
    win.deleteLater()
    pump(app, 60)


def add_message(win, text: str, *, conversation_id: str = seed.CHAT_LIN,
                outgoing: bool = False, stamp: float = 0.0) -> Message:
    """往仓库里塞一条消息（当前会话的话，界面会立刻多出这个气泡）。"""
    message = Message(id=f"m-polish-{len(text)}-{int((stamp or time.time()) * 1000)}",
                      conversation_id=conversation_id, sender_id="u_bob",
                      text=text, ts=stamp or time.time(), outgoing=outgoing)
    win.store.append(message)
    win.chat_view.sync_message(message)
    pump()
    return message


class _FakeOpener:
    """替换 ``QDesktopServices`` 的记录器（真开浏览器会把测试机弹出窗口）。"""

    def __init__(self):
        self.urls: list[str] = []

    def openUrl(self, url) -> bool:                       # noqa: N802
        self.urls.append(url.toString())
        return True


# ------------------------------------------------------------------ 链接
def test_flag_and_link_are_rendered_in_one_pass(app):
    payload, rich = flag_text.to_display("国旗 🇨🇳 和链接 https://a.example/x?y=1")
    assert rich
    assert "<img" in payload and "048.png" in payload, payload
    assert '<a href="https://a.example/x?y=1">https://a.example/x?y=1</a>' in payload


def test_plain_text_stays_plain(app):
    assert flag_text.to_display("就是一句话，没有别的") == ("就是一句话，没有别的", False)
    assert flag_text.contains_link("没有链接") is False
    assert flag_text.contains_link("见 www.example.com") is True


def test_link_in_bubble_is_clickable_and_goes_to_the_browser(app, window, monkeypatch):
    message = add_message(window, "看这个 https://example.com/a?b=1 好吗")
    bubble = window.chat_view.message_list.bubbles()[message.id]
    assert "<a href=" in bubble.text_label.text(), bubble.text_label.text()
    assert bubble.text_label.textInteractionFlags() & \
        Qt.TextInteractionFlag.LinksAccessibleByMouse

    opener = _FakeOpener()
    monkeypatch.setattr(main_window_module, "QDesktopServices", opener)
    bubble.text_label.linkActivated.emit("https://example.com/a?b=1")   # 相当于用户点了链接
    assert opener.urls == ["https://example.com/a?b=1"]


def test_plain_bubble_has_no_link_affordance(app, window):
    message = add_message(window, "这条没有链接")
    bubble = window.chat_view.message_list.bubbles()[message.id]
    assert bubble.text_label.textFormat() == Qt.TextFormat.PlainText
    assert not (bubble.text_label.textInteractionFlags()
                & Qt.TextInteractionFlag.LinksAccessibleByMouse)


def test_link_handler_only_allows_http(app, window, monkeypatch):
    opener = _FakeOpener()
    monkeypatch.setattr(main_window_module, "QDesktopServices", opener)
    assert window.open_link("file:///C:/Windows/System32/calc.exe") is False
    assert window.open_link("javascript:alert(1)") is False
    assert window.open_link("") is False
    assert opener.urls == [], "只该放行 http(s)，别的 scheme 一个都不能开"
    assert window.open_link("https://example.com/") is True
    assert opener.urls == ["https://example.com/"]


# -------------------------------------------------------------- 通知点击
def test_notification_click_returns_its_conversation(app, window):
    seen: list[str] = []
    window._notifier.set_message_click_handler(seen.append)
    window._notifier.message("王尼玛", "在吗", conversation_id=seed.CHAT_LIN)
    window._notifier._on_message_clicked()          # 相当于用户点了那条通知
    assert seen == [seed.CHAT_LIN]
    window._notifier._on_message_clicked()          # 同一条只跳一次
    assert seen == [seed.CHAT_LIN]


def test_open_from_notification_shows_window_and_switches(app, window):
    window.select_conversation(seed.CHAT_DESIGN)
    window.hide()
    pump(app, 60)
    assert window.isVisible() is False
    assert window.open_from_notification(seed.CHAT_LIN) is True
    assert window.isVisible(), "点了通知得把窗口叫回来"
    assert window.chat_view.conversation.id == seed.CHAT_LIN
    assert window.open_from_notification("这个会话不存在") is False


# ---------------------------------------------------------- 未读分隔线
def test_unread_divider_sits_before_the_first_unread(app, window):
    window.select_conversation(seed.CHAT_DESIGN)     # 先去看别的会话
    before = [m.id for m in window.store.get(seed.CHAT_LIN).messages]
    added = []
    base = time.time()
    for index in range(3):
        message = add_message(window, f"第 {index} 条没看的", stamp=base + index)
        added.append(message.id)
        window.store.bump_unread(seed.CHAT_LIN)
    assert window.store.unread(seed.CHAT_LIN) == 3

    window.select_conversation(seed.CHAT_LIN)
    pump(app, 260)
    listing = window.chat_view.message_list
    assert listing.has_unread_divider(), "还有未读，打开时该有一条分界"
    assert listing.unread_divider_text() == "以下为 3 条新消息"
    assert listing.unread_anchor() == added[0]
    if before:
        divider_y = listing.unread_divider_y()
        assert divider_y > listing.bubble_y(before[-1]), "分界该在上一条已读之后"
        assert divider_y < listing.bubble_y(added[0]), "分界该在第一条未读之前"
        assert listing.bubble_y(added[-1]) > listing.bubble_y(added[0])


def test_no_divider_when_everything_is_read(app, window):
    window.select_conversation(seed.CHAT_DESIGN)
    window.select_conversation(seed.CHAT_LIN)
    pump(app, 160)
    assert not window.chat_view.message_list.has_unread_divider()
    assert window.chat_view.message_list.unread_anchor() == ""


# ------------------------------------------------------------ 关窗到托盘
def test_close_quits_by_default(app, window):
    assert window.close_to_tray() is False, "默认必须是老行为（✕ = 退出）"
    closed: list[int] = []
    window.closed.connect(lambda: closed.append(1))
    window.close()
    pump(app, 80)
    assert closed and window._shutdown_done


def test_close_to_tray_switch_hides_and_tray_quit_still_quits(app, window, monkeypatch):
    monkeypatch.setattr(window._notifier, "available", lambda: True)
    assert window.set_close_to_tray(True) is True
    closed: list[int] = []
    window.closed.connect(lambda: closed.append(1))

    window.close()                                   # 相当于点了 ✕
    pump(app, 100)
    assert not closed, "开了开关之后点 ✕ 不该退出程序"
    assert not window.isVisible(), "窗口该收进托盘"
    assert not window._shutdown_done, "收进托盘不是收尾，网络线程还得跑"

    window.quit_from_tray()                          # 托盘菜单「退出 MChat」
    pump(app, 100)
    assert closed and window._shutdown_done, "托盘退出必须真的退，不能被收托盘逻辑拦住"


def test_close_to_tray_refused_without_a_tray(app, window, monkeypatch):
    monkeypatch.setattr(window._notifier, "available", lambda: False)
    assert window.set_close_to_tray(True) is False, "没有托盘就不能收进托盘（窗口会找不回来）"
    assert window.close_to_tray() is False


def test_settings_dialog_has_the_toggle_and_it_reaches_the_window(app, window, monkeypatch):
    monkeypatch.setattr(window._notifier, "available", lambda: True)
    dialog = window.open_settings()
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    assert dialog.card_count() == 6, "新卡片加在最后，不动既有卡片下标"

    dialog.tray_switch.toggle()                      # 相当于用户点了一下开关 → 走真实信号链
    pump(app, 100)
    assert window.close_to_tray() is True
    assert str(window._settings.value("close_to_tray", "")).lower() == "true"
    assert dialog.close_to_tray() is True
    assert "收进托盘" in dialog.tray_value.text()

    dialog.tray_switch.toggle()                      # 关回去：别让后面的用例继承"收托盘"
    pump(app, 60)
    assert window.close_to_tray() is False
    window._quitting = True
    dialog.close()
