"""主窗口：把侧栏、聊天区、工具箱和传输层拼起来。

职责边界
--------
* :class:`~mchat.core.store.ChatStore`   —— 数据与未读规则（不依赖 Qt）
* :class:`~mchat.core.transport.LocalTransport` —— 消息怎么送达（不依赖 Qt）
* :class:`~mchat.ui.sidebar.Sidebar` / :class:`~mchat.ui.chat_view.ChatView`
  —— 只负责显示与交互，通过信号把动作抛给本窗口
* 本文件 —— 唯一"知道全部"的地方：接线、主题、事件轮询、打开工具箱

所以想换成真联网时，只需要在 ``__init__`` 里把 ``LocalTransport`` 换成
socket 版实现，其余代码不用改。
"""
from __future__ import annotations

import sys
import time
import uuid

from PyQt6.QtCore import QPoint, QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QMenu, QWidget

from ..core import seed
from ..core.models import ME_ID, Message
from ..core.store import MESSAGE, RESET, ChatStore, StoreEvent
from ..core.transport import (EVENT_MESSAGE, EVENT_READ, EVENT_TYPING,
                              LocalTransport, Outgoing)
from .chat_view import ChatView
from .security_tool import SecurityToolDialog
from .sidebar import Sidebar
from .theme import (THEME_OPTIONS, THEME_SYSTEM, Palette, build_qss,
                    resolve_palette, watch_system_palette)
from .widgets import Toast

__all__ = ["MainWindow", "SETTINGS_ORG", "SETTINGS_APP"]

SETTINGS_ORG = "MChat"
SETTINGS_APP = "MChat"
POLL_INTERVAL_MS = 400                      # 事件轮询间隔（真联网时同理）


class MainWindow(QWidget):
    """MChat 聊天窗口。"""

    def __init__(self, parent: QWidget | None = None, *, demo=None,
                 transport=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle("MChat")
        self.setMinimumSize(960, 600)
        self.resize(self._preferred_size())
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        demo = demo if demo is not None else seed.build_demo()
        self._store: ChatStore = demo.store
        self._me = demo.me
        # 传输层可以注入（测试用假时钟，将来换 socket 实现也走这里）
        self._transport = transport if transport is not None else LocalTransport(
            demo.scripts)
        self._transport.start()
        self._current_id = ""
        self._tool_dialog: SecurityToolDialog | None = None

        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()

        saved = str(self._settings.value("theme", THEME_OPTIONS[0]))
        self.sidebar.set_theme_name(saved if saved in THEME_OPTIONS else THEME_OPTIONS[0])
        watch_system_palette(self._on_system_palette_changed)
        self._apply_theme()

        self.select_conversation(demo.default_chat_id)
        self._poller = QTimer(self)
        self._poller.setInterval(POLL_INTERVAL_MS)
        self._poller.timeout.connect(self.tick)
        self._poller.start()

    # ======================================================================
    # 搭建
    # ======================================================================
    def _preferred_size(self):
        screen = QGuiApplication.primaryScreen()
        wanted = QSize(1180, 760)
        if screen is not None:
            available = screen.availableGeometry().size()
            return QSize(min(wanted.width(), available.width() - 60),
                         min(wanted.height(), available.height() - 60))
        return wanted

    def _build_ui(self) -> None:
        self.sidebar = Sidebar(self._store, self)
        self.chat_view = ChatView(self._store, self)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.sidebar)
        row.addWidget(self.chat_view, 1)

        self.toast = Toast(self, bottom_offset=70)   # 抬到输入栏上方，别挡住正在打的消息

    def _connect_signals(self) -> None:
        self.sidebar.conversationSelected.connect(self.select_conversation)
        self.sidebar.toolRequested.connect(self.open_tool)
        self.sidebar.menuRequested.connect(self._show_menu)
        self.sidebar.themeChanged.connect(self.set_theme)
        self.chat_view.messageSubmitted.connect(self.send_message)
        self.chat_view.moreRequested.connect(self._show_menu)
        self._store.subscribe(self._on_store_event)
        self.sidebar.refresh()

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self.open_tool)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Escape"), self,
                  activated=lambda: self.toast.hide_message())

    # ======================================================================
    # 会话
    # ======================================================================
    @property
    def store(self) -> ChatStore:
        return self._store

    @property
    def transport(self):
        return self._transport

    def current_conversation_id(self) -> str:
        return self._current_id

    def select_conversation(self, conversation_id: str) -> bool:
        conversation = self._store.get(conversation_id)
        if conversation is None:
            return False
        self._current_id = conversation_id
        self.chat_view.show_conversation(conversation)
        self.sidebar.set_current(conversation_id)
        self._store.mark_read(conversation_id)
        return True

    def send_message(self, text: str) -> bool:
        """把输入框里的文字发给当前会话。"""
        text = text.strip()
        if not text:
            self.toast.show_message("空消息不能发送", "warning", 1800)
            return False
        conversation_id = self._current_id
        if not conversation_id:
            self.toast.show_message("先选择一个会话", "warning", 1800)
            return False
        message = Message(
            id=f"out-{uuid.uuid4().hex[:8]}",
            conversation_id=conversation_id,
            sender_id=ME_ID,
            text=text,
            ts=time.time(),
            outgoing=True,
        )
        self._store.append(message)
        self._transport.send(Outgoing(conversation_id, ME_ID, text, message.id))
        return True

    # ======================================================================
    # 传输层事件轮询
    # ======================================================================
    def tick(self, now: float | None = None) -> int:
        """把传输层里"已经到了"的事件应用到仓库上，返回处理条数。

        ``now`` 留空就用传输层自己的时钟（真联网时由它决定，测试里可注入假时钟）。
        """
        events = self._transport.poll(now)
        for event in events:
            if event.kind == EVENT_MESSAGE:
                # 回复到达即结束"正在输入…"
                self.sidebar.set_typing(event.conversation_id, False)
                if event.conversation_id == self._current_id:
                    self.chat_view.set_typing(False)
                self._store.receive(Message(
                    id=f"in-{uuid.uuid4().hex[:8]}",
                    conversation_id=event.conversation_id,
                    sender_id=event.sender_id,
                    text=event.text,
                    ts=event.ts or time.time(),
                    outgoing=False,
                ))
            elif event.kind == EVENT_TYPING:
                self.sidebar.set_typing(event.conversation_id, event.typing)
                if event.conversation_id == self._current_id:
                    self.chat_view.set_typing(event.typing)
            elif event.kind == EVENT_READ:
                self._store.set_read(event.conversation_id, event.message_id)
        return len(events)

    def _on_store_event(self, event: StoreEvent) -> None:
        if event.kind == RESET:
            self.sidebar.refresh()
            return
        if event.kind == MESSAGE and event.message is not None:
            message = event.message
            self.chat_view.sync_message(message)
            # 正开着的会话不留未读
            if message.conversation_id == self._current_id and not message.outgoing:
                self._store.mark_read(message.conversation_id)
        self.sidebar.refresh_item(event.conversation_id)

    # ======================================================================
    # 主题
    # ======================================================================
    def set_theme(self, name: str) -> None:
        if name not in THEME_OPTIONS:
            return
        self._settings.setValue("theme", name)
        self.sidebar.set_theme_name(name)      # 让分段控件与主题保持一致
        self._apply_theme()

    def theme_name(self) -> str:
        return self.sidebar.theme_seg.value()

    def current_palette(self) -> Palette:
        return resolve_palette(self.theme_name())

    def _on_system_palette_changed(self) -> None:
        if self.theme_name() == THEME_SYSTEM:
            self._apply_theme()

    def _apply_theme(self) -> None:
        p = self.current_palette()
        self.setStyleSheet(build_qss(p))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(p.window))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
        pal.setColor(QPalette.ColorRole.Text, QColor(p.text))
        pal.setColor(QPalette.ColorRole.Base, QColor(p.card))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_on))
        self.setPalette(pal)

        self.sidebar.apply_palette(p)
        self.chat_view.apply_palette(p)
        self.toast.apply_palette(p)
        if self._tool_dialog is not None:
            self._tool_dialog.apply_palette(p)

    # ======================================================================
    # 工具箱 / 菜单
    # ======================================================================
    def open_tool(self, prefill: str = "") -> SecurityToolDialog:
        """打开安全工具箱（非模态、复用同一个窗口）。"""
        if self._tool_dialog is None:
            self._tool_dialog = SecurityToolDialog(self)
        dialog = self._tool_dialog
        dialog.apply_palette(self.current_palette())
        if prefill:
            dialog.select_algorithm(0)
            dialog.set_input(prefill)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.tool.input_field.focus_field()
        return dialog

    def tool_dialog(self) -> SecurityToolDialog | None:
        return self._tool_dialog

    def _show_menu(self) -> None:
        """侧栏/聊天头部共用的"更多"菜单。"""
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_qss())
        menu.addAction("安全工具箱（Ctrl+T）", self.open_tool)
        pending = self.chat_view.input_text().strip()
        action = menu.addAction("把输入框的文字送进工具箱", self._send_input_to_tool)
        action.setEnabled(bool(pending))
        if not pending:
            action.setToolTip("输入框为空")
        menu.addSeparator()
        menu.addAction("标记当前会话为已读", self._mark_current_read)
        menu.addAction(f"当前传输层：{self._transport.name}（未联网）", lambda: None).setEnabled(False)
        menu.exec(self.mapToGlobal(QPoint(self.width() // 2, 60)))

    def _menu_qss(self) -> str:
        p = self.current_palette()
        return f"""
        QMenu {{
            background-color: {p.card};
            border: 1px solid {p.border};
            border-radius: 10px;
            padding: 6px;
        }}
        QMenu::item {{
            padding: 7px 18px 7px 14px;
            border-radius: 8px;
            color: {p.text};
            font-size: 12.5px;
        }}
        QMenu::item:selected {{ background-color: {p.accent_soft}; color: {p.accent}; }}
        QMenu::item:disabled {{ color: {p.muted}; }}
        QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}
        """

    def _send_input_to_tool(self) -> None:
        text = self.chat_view.input_text().strip()
        if not text:
            self.toast.show_message("输入框是空的", "warning", 1600)
            return
        self.open_tool(prefill=text)
        self.toast.show_message("已把输入框的文字送进工具箱", "success", 1800)

    def _mark_current_read(self) -> None:
        if self._store.mark_read(self._current_id):
            self.toast.show_message("已标记为已读", "success", 1500)
        else:
            self.toast.show_message("当前会话没有未读消息", "info", 1500)

    def _focus_search(self) -> None:
        self.sidebar.search_field.focus_field()

    # ======================================================================
    # 窗口杂项
    # ======================================================================
    def status_text(self) -> str:
        return self.toast.message()

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self.toast.reposition()

    def closeEvent(self, event) -> None:                 # noqa: N802
        self._poller.stop()
        self._transport.stop()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    """程序入口：python main.py"""
    # 高分屏按真实缩放比渲染，避免汉字被截断
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(SETTINGS_APP)
    app.setOrganizationName(SETTINGS_ORG)
    window = MainWindow()
    window.show()
    return app.exec()
