"""应用内窗口统一外壳：无边框 + 自绘标题条（系统那套标题栏连它的 ✕ 一个都不露）。

要守住四件事，缺一件这活儿就算白干：

* **每个应用窗口**都是无边框，右上角那个 ✕ 是自绘的、且**真能关窗**（不是画着好看）；
  普通对话框只有 ✕，最小化/最大化是主窗口专享（对话框本来就是固定尺寸）。
* **主窗口三个按钮**各自到位：✕ 走的还是原来那条 ``closeEvent``（"收进托盘"开关
  照样拦得住）、─ 最小化、□ 最大化/还原且图标与提示跟着换。
* 无边框之后**补回来的八边缩放**判定正确：哪条边 → 哪个光标 → 交给系统原生缩放。
* 鼠标移动**绝不抛异常**。踩过一次：``cursor_for()`` 里写了 ``int(Qt.Edge.LeftEdge)``，
  PyQt6 的 Qt.Edge 成员不吃 ``int()``，而那个过滤器跑在**全应用的每一次鼠标移动**上，
  于是满屏 traceback（见 ``test_mouse_move_at_the_edge_never_raises``）。

真窗口上不做 ``startSystemMove()`` / ``startSystemResize()`` —— 那是系统原生循环，
测试里真调进去就挂住了。这两条用 ``FakeWindow`` 验"有没有按约定发号施令"。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest                                                       # noqa: E402
from PyQt6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect,      # noqa: E402
                          QSettings, Qt)
from PyQt6.QtGui import QCursor, QMouseEvent                        # noqa: E402
from PyQt6.QtTest import QTest                                      # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget                   # noqa: E402

import seed                                                         # noqa: E402
from mchat.core.db import ChatLog                                   # noqa: E402
from mchat.core.session import SessionStore                         # noqa: E402
from mchat.core.transport import LocalTransport                     # noqa: E402
from mchat.ui.forward_dialog import FORWARD_HEIGHT                  # noqa: E402
from mchat.ui.forward_dialog import ForwardDialog                   # noqa: E402
from mchat.ui.forward_view import MergedViewerDialog                # noqa: E402
from mchat.ui.friends_dialog import DIALOG_H as FRIENDS_H           # noqa: E402
from mchat.ui.friends_dialog import FriendsDialog                   # noqa: E402
from mchat.ui.image_view import ImageViewerDialog                   # noqa: E402
from mchat.ui.login_window import LoginWindow                       # noqa: E402
from mchat.ui.main_window import MainWindow                         # noqa: E402
from mchat.ui.new_chat_dialog import DIALOG_H as NEW_CHAT_H         # noqa: E402
from mchat.ui.new_chat_dialog import NewChatDialog                  # noqa: E402
from mchat.ui.pin_dialog import PinDialog                           # noqa: E402
from mchat.ui.search_dialog import SearchDialog                     # noqa: E402
from mchat.ui.security_tool import SecurityToolDialog                # noqa: E402
from mchat.ui.settings_dialog import SettingsDialog                 # noqa: E402
from mchat.ui.theme import DARK, LIGHT                              # noqa: E402
from mchat.ui.widgets import (CHROME_H, CHROME_WINDOW, EDGE_BOTTOM,  # noqa: E402
                              EDGE_LEFT, EDGE_RIGHT, EDGE_TOP,
                              FramelessResizer, WindowChrome, _edge_flags)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(application=None, ms: int = 120) -> None:
    application = application or QApplication.instance()
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


class FakeClipboard:
    """假剪贴板：安全工具箱的"复制"不许碰真剪贴板（真碰过一次）。"""

    def __init__(self) -> None:
        self.text_value = ""

    def setText(self, text) -> None:                    # noqa: N802
        self.text_value = str(text)

    def text(self) -> str:
        return self.text_value


@pytest.fixture()
def window(app):
    """真主窗口（无头、不弹窗），带示例数据。"""
    settings = QSettings("MChatChromeTest", "MChatChromeTest")
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
    pump(app, 200)
    yield win
    win.shutdown()
    win.deleteLater()
    pump(app, 60)


def show(app, widget):
    """把窗口摆上屏（无头），好让 chrome 真的被布局/尺寸算进去。"""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    pump(app, 80)
    return widget


def window_factories(tmp_path):
    """应用内的每一个窗口。新增窗口忘了装外壳时，这条用例会红。"""
    return [
        ("设置", lambda: SettingsDialog()),
        ("好友", lambda: FriendsDialog()),
        ("搜索", lambda: SearchDialog(ChatLog(":memory:"), {})),
        ("转发到", lambda: ForwardDialog()),
        ("聊天记录", lambda: MergedViewerDialog()),
        ("图片", lambda: ImageViewerDialog()),
        ("新建会话", lambda: NewChatDialog()),
        ("安全工具箱", lambda: SecurityToolDialog(clipboard=FakeClipboard())),
        ("置顶消息", lambda: PinDialog(peer_label="小明", for_both=True)),
        ("登录", lambda: LoginWindow(
            session_store=SessionStore(str(tmp_path / "sessions.json")))),
    ]


# ============================================================== 每个窗口都装上了
def test_every_app_window_is_frameless_with_its_own_close(app, tmp_path):
    """无边框 + 自绘 ✕，且点它真能把窗口关掉（对话框不该有最小化/最大化）。"""
    problems: list[str] = []
    for label, factory in window_factories(tmp_path):
        target = show(app, factory())
        try:
            if not (target.windowFlags() & Qt.WindowType.FramelessWindowHint):
                problems.append(f"{label}: 还带着系统标题栏（没有 FramelessWindowHint）")
            chrome = getattr(target, "chrome", None)
            if chrome is None:
                problems.append(f"{label}: 没装自绘标题条")
                continue
            if chrome.button("close") is None:
                problems.append(f"{label}: 没有自绘的关闭按钮")
                continue
            for extra in ("minimize", "maximize"):
                if chrome.button(extra) is not None:
                    problems.append(f"{label}: 对话框不该有 {extra} 按钮")
            if chrome.title_text() not in ("", target.windowTitle()):
                problems.append(f"{label}: 标题条写的是 {chrome.title_text()!r}")
            chrome.button("close").click()
            if target.isVisible():
                problems.append(f"{label}: 点了自绘 ✕ 却没关")
        finally:
            target.hide()
            target.deleteLater()
            pump(app, 20)
    assert not problems, "\n".join(problems)


def test_chrome_spans_the_top_of_the_window(app, window):
    """标题条铺满窗口整宽，内容排在它**下面**（不会压在它底下、也不会被它盖住）。"""
    outer = window.layout()
    assert outer.itemAt(0).widget() is window.chrome, "标题条该是根布局的第一格"
    assert window.chrome.geometry() == QRect(0, 0, window.width(), CHROME_H)
    content = outer.itemAt(1).widget()
    assert content is not None and content is not window.chrome
    assert content.geometry().top() >= CHROME_H
    # 内容容器里装的还是原来那套（侧栏 / 分界线 / 聊天区），留白没被吃掉
    assert (window.sidebar.parent() is content or content.isAncestorOf(window.sidebar)), \
        "侧栏没跟着布局一起搬进内容容器"
    window.resize(1180, 760)
    pump(app, 120)
    assert window.chrome.geometry() == QRect(0, 0, window.width(), CHROME_H)
    assert outer.itemAt(1).widget().geometry().top() >= CHROME_H


def test_fixed_size_dialogs_leave_room_for_the_chrome(app, tmp_path):
    """固定尺寸的窗口：高度 = 原来的高度 + 标题条那一条（否则内容会被裁掉）。"""
    friends = show(app, FriendsDialog())
    forward = show(app, ForwardDialog())
    new_chat = show(app, NewChatDialog())
    tool = show(app, SecurityToolDialog(clipboard=FakeClipboard()))
    try:
        assert friends.height() == FRIENDS_H + CHROME_H
        assert forward.height() == FORWARD_HEIGHT + CHROME_H
        assert new_chat.height() == NEW_CHAT_H + CHROME_H
        assert tool.height() == tool.tool.height() + CHROME_H
        assert tool.width() == tool.tool.width()          # 宽度不变，只长高一条
        for target in (friends, forward, new_chat, tool):
            assert target.chrome.geometry().bottom() < target.height()
    finally:
        for target in (friends, forward, new_chat, tool):
            target.hide()
            target.deleteLater()
        pump(app, 20)


# ============================================================== 主窗口的三个按钮
def test_main_window_has_all_three_buttons(app, window):
    chrome = window.chrome
    assert chrome.title_text() == window.windowTitle()
    assert [name for name in ("minimize", "maximize", "close") if chrome.button(name)] \
        == ["minimize", "maximize", "close"]


def test_close_button_respects_the_tray_switch(app, window):
    """自绘 ✕ 走的就是原来那条 closeEvent：开关关着 = 退出，开着 = 收进托盘。"""
    assert window.close_to_tray() is False
    fired: list[bool] = []
    window.closed.connect(lambda: fired.append(True))
    window.chrome.button("close").click()
    pump(app, 240)
    assert fired == [True], "默认（不收托盘）点 ✕ 应该关掉并通知启动器"

    window.show()
    pump(app, 80)
    window.set_close_to_tray(True)
    window.chrome.button("close").click()
    pump(app, 240)
    assert not window.isVisible(), "开了收托盘，点 ✕ 应该把窗口收起来"
    assert window._quitting is False, "收托盘不该被当成退出程序"
    window.set_close_to_tray(False)


def test_chrome_takes_theme_colors(app, window):
    for palette in (LIGHT, DARK):
        window.chrome.apply_palette(palette)
        assert window.chrome._palette is palette
        for button in window.chrome.buttons.values():
            assert not button.icon().isNull(), "上色之后图标不该是空的"


# ============================================================== 假窗口：按钮与拖动
class FakeWindow(QObject):
    """假窗口：只记下 chrome 对它做了什么。

    真窗口上 ``startSystemMove()`` 会进系统拖动循环、``showMaximized()`` 会真最大化，
    测试里不能那么干 —— 这里验的是"chrome 有没有按约定发号施令"。
    """

    def __init__(self, *, maximized: bool = False):
        super().__init__()
        self.maximized = maximized
        self.calls: list[str] = []
        self._cursor = QCursor(Qt.CursorShape.ArrowCursor)
        self._rect = QRect(0, 0, 900, 600)
        self._visible = True

    # --- chrome 要用的
    def installEventFilter(self, obj) -> None:          # noqa: N802
        self.calls.append("filter")

    def width(self) -> int:
        return self._rect.width()

    def height(self) -> int:
        return self._rect.height()

    def setGeometry(self, *args) -> None:               # noqa: N802
        self.calls.append("geometry")

    def raise_(self) -> None:
        pass

    def close(self) -> None:
        self.calls.append("close")

    def showMinimized(self) -> None:                    # noqa: N802
        self.calls.append("minimize")

    def isMaximized(self) -> bool:                      # noqa: N802
        return self.maximized

    def showNormal(self) -> None:                       # noqa: N802
        self.calls.append("normal")
        self.maximized = False

    def showMaximized(self) -> None:                    # noqa: N802
        self.calls.append("maximize")
        self.maximized = True

    def windowHandle(self):                             # noqa: N802
        return self

    def startSystemMove(self) -> None:                  # noqa: N802
        self.calls.append("move")

    # --- resizer 要用的
    def isVisible(self) -> bool:                        # noqa: N802
        return self._visible

    def rect(self) -> QRect:
        return self._rect

    def mapFromGlobal(self, point):                     # noqa: N802
        return point

    def setCursor(self, cursor) -> None:                # noqa: N802
        self._cursor = cursor if isinstance(cursor, QCursor) else QCursor(cursor)

    def unsetCursor(self) -> None:                      # noqa: N802
        self._cursor = QCursor(Qt.CursorShape.ArrowCursor)

    def cursor(self) -> QCursor:
        return self._cursor


def test_chrome_buttons_dispatch_to_the_window(app):
    holder = show(app, QWidget())
    fake = FakeWindow()
    chrome = WindowChrome(fake, parent=holder, title="MChat — 测试",
                          controls=("minimize", "maximize", "close"))
    try:
        chrome.trigger("minimize")
        chrome.trigger("maximize")
        assert fake.maximized is True and fake.calls[-2:] == ["minimize", "maximize"]
        assert chrome.button("maximize").icon_kind() == "win_restore"
        assert chrome.button("maximize").toolTip() == "还原"
        chrome.trigger("maximize")                      # 再来一次 = 还原
        assert fake.maximized is False and fake.calls[-1] == "normal"
        assert chrome.button("maximize").icon_kind() == "win_maximize"
        assert chrome.button("maximize").toolTip() == "最大化"
        chrome.trigger("close")
        assert fake.calls[-1] == "close"
    finally:
        chrome.deleteLater()
        holder.hide()
        holder.deleteLater()
        pump(app, 20)


def test_chrome_drag_uses_the_native_move(app):
    holder = show(app, QWidget())
    fake = FakeWindow()
    chrome = WindowChrome(fake, parent=holder, controls=CHROME_WINDOW)
    try:
        QTest.mousePress(chrome, Qt.MouseButton.LeftButton, pos=QPoint(60, 10))
        QTest.mouseRelease(chrome, Qt.MouseButton.LeftButton, pos=QPoint(60, 10))
        assert fake.calls.count("move") == 1, "按住标题条该交给系统原生拖动"

        QTest.mouseDClick(chrome, Qt.MouseButton.LeftButton, pos=QPoint(60, 10))
        assert fake.maximized is True, "双击标题条 = 最大化（和系统标题栏一致）"
    finally:
        chrome.deleteLater()
        holder.hide()
        holder.deleteLater()
        pump(app, 20)


# ============================================================== 八边缩放
def test_resize_edges_and_cursors(app, window):
    resizer = FramelessResizer(window, margin=6)
    width, height = window.width(), window.height()
    assert resizer.edges_at(QPoint(2, 2)) == EDGE_LEFT | EDGE_TOP
    assert resizer.edges_at(QPoint(width - 2, height - 2)) == EDGE_RIGHT | EDGE_BOTTOM
    assert resizer.edges_at(QPoint(width - 2, 2)) == EDGE_RIGHT | EDGE_TOP
    assert resizer.edges_at(QPoint(width // 2, 3)) == EDGE_TOP
    assert resizer.edges_at(QPoint(3, height // 2)) == EDGE_LEFT
    assert resizer.edges_at(QPoint(width // 2, height // 2)) == 0
    assert resizer.edges_at(QPoint(-5, 10)) == 0            # 窗口外
    assert resizer.cursor_for(EDGE_LEFT | EDGE_TOP) == Qt.CursorShape.SizeFDiagCursor
    assert resizer.cursor_for(EDGE_RIGHT | EDGE_TOP) == Qt.CursorShape.SizeBDiagCursor
    assert resizer.cursor_for(EDGE_LEFT) == Qt.CursorShape.SizeHorCursor
    assert resizer.cursor_for(EDGE_BOTTOM) == Qt.CursorShape.SizeVerCursor
    assert resizer.cursor_for(0) is None


def test_edge_flags_round_trip():
    """内部一律用纯 int 位标记，只有交给 Qt 的时候才转回 Qt.Edge。"""
    assert _edge_flags(EDGE_LEFT) == Qt.Edge.LeftEdge
    assert _edge_flags(EDGE_LEFT | EDGE_BOTTOM) == Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
    assert _edge_flags(0) == Qt.Edge(0)


def _move_event(target, local: QPoint) -> QMouseEvent:
    return QMouseEvent(QEvent.Type.MouseMove, QPointF(local),
                       QPointF(target.mapToGlobal(local)),
                       Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


def test_mouse_move_at_the_edge_never_raises(app, window):
    """回归：``int(Qt.Edge.LeftEdge)`` 会 TypeError，而这段跑在每次鼠标移动上。

    无头环境里 ``QApplication.widgetAt()`` 认不出窗口，所以把"鼠标底下是谁"换个
    假答案 —— 要验的是边缘判定与光标那两行，不是 widgetAt 本身。
    用的是主窗口自己那个（已 attach 的）缩放器：没 attach 的会直接早退，测不到那两行。
    """
    resizer = window._resizer
    assert resizer.is_attached()
    original = resizer._widget_under
    resizer._widget_under = lambda point: window
    try:
        assert resizer.eventFilter(window, _move_event(window, QPoint(3, 3))) is False
        assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
        assert resizer.eventFilter(window, _move_event(
            window, QPoint(window.width() // 2, window.height() // 2))) is False
        assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
    finally:
        resizer._widget_under = original
        window.unsetCursor()


def test_edge_press_starts_system_resize_and_is_swallowed(app):
    """边缘按下：交给系统原生缩放，并且**吞掉这一下**（否则标题条会同时开始拖动）。"""
    fake = FakeWindow()
    resizer = FramelessResizer(fake)
    resizer.attach()                     # 没 attach 的缩放器会直接早退（测不到那几行）
    resizer._widget_under = lambda point: fake
    resized: list[int] = []
    fake.startSystemResize = lambda edges: resized.append(edges)   # noqa: E731

    inside = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(QPoint(450, 300)),
                         QPointF(QPoint(450, 300)), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert resizer.eventFilter(fake, inside) is False      # 窗口中间：不插手

    corner = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(QPoint(2, 2)),
                         QPointF(QPoint(2, 2)), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    try:
        assert resizer.eventFilter(fake, corner) is True
        assert resized == [Qt.Edge.LeftEdge | Qt.Edge.TopEdge]
    finally:
        resizer.detach()
