"""右侧聊天区：消息气泡、日期分隔条、输入栏，以及"没选会话"的空态。"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                             QScrollArea, QSizePolicy, QStackedWidget,
                             QToolButton, QVBoxLayout, QWidget)

from ..core.models import Conversation, Message, day_label_key, format_day_label
from ..core.store import ChatStore
from .avatar import Avatar
from .icons import IconButton, make_icon

__all__ = ["ChatView", "MessageList", "MessageBubble", "Composer"]

MAX_BUBBLE_WIDTH = 520
MIN_INPUT_LINES = 1
MAX_INPUT_LINES = 5


class DateChip(QLabel):
    """消息列表里的日期分隔条（今天 / 昨天 / 9月25日）。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("dateChip")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    @property
    def label(self) -> str:
        return self.text()


class MessageBubble(QFrame):
    """一条消息的气泡：左侧（收到的）白/深底，右侧（发出的）主题浅色底。"""

    def __init__(self, message: Message, *, sender_name: str = "",
                 show_sender: bool = False, parent=None):
        super().__init__(parent)
        self.message_id = message.id
        self.outgoing = message.outgoing
        self.setObjectName("bubbleOut" if message.outgoing else "bubbleIn")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        column = QVBoxLayout(self)
        column.setContentsMargins(11, 7, 11, 6)
        column.setSpacing(2)

        if show_sender and not message.outgoing and sender_name:
            name = QLabel(sender_name, self)
            name.setObjectName("senderName")
            column.addWidget(name)

        self.text_label = QLabel(message.text, self)
        self.text_label.setObjectName("msgText")
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)   # 消息里带 < > 也不当 HTML
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumWidth(MAX_BUBBLE_WIDTH - 26)
        self.text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.text_label)

        meta = QHBoxLayout()
        meta.setSpacing(4)
        self.time_label = QLabel(message.time_text, self)
        self.time_label.setObjectName("msgMeta")
        self.read_label = QLabel("", self)
        self.read_label.setObjectName("readMark")
        meta.addStretch(1)
        meta.addWidget(self.time_label)
        if message.outgoing:
            meta.addWidget(self.read_label)
        column.addLayout(meta)

        self.setMaximumWidth(MAX_BUBBLE_WIDTH)
        self.set_meta(message)

    def set_meta(self, message: Message) -> None:
        """只刷新时间与已读标记（消息内容不变，不重建控件）。"""
        self.time_label.setText(message.time_text)
        if self.outgoing:
            self.read_label.setText("✓✓" if message.is_read else "✓")

    def text(self) -> str:
        return self.text_label.text()


class MessageEdit(QPlainTextEdit):
    """聊天输入框：自增高 1~5 行，Enter 发送、Shift+Enter 换行。"""

    submitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composerInput")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setPlaceholderText("输入消息，Enter 发送 · Shift+Enter 换行")
        self.textChanged.connect(self._sync_height)
        self._sync_height()

    def keyPressEvent(self, event) -> None:              # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)             # 换行
            else:
                self.submitted.emit()
            return
        super().keyPressEvent(event)

    def _sync_height(self) -> None:
        line = self.fontMetrics().lineSpacing()
        lines = max(1, min(MAX_INPUT_LINES, self.document().blockCount()))
        self.setFixedHeight(lines * line + 12)
        if lines < MAX_INPUT_LINES:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def sizeHint(self) -> QSize:
        return QSize(200, self.height())


class Composer(QWidget):
    """底部输入栏：表情、输入框、发送按钮。"""

    messageSubmitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.smile_btn = IconButton("smile", self, size=20, box=36,
                                    tooltip="插入表情")
        self.smile_btn.clicked.connect(self._insert_smiley)
        self.edit = MessageEdit(self)
        self.edit.submitted.connect(self.submit)
        self.send_btn = IconButton("send", self, size=18, box=42,
                                   tooltip="发送（Enter）", object_name="sendBtn")
        self.send_btn.clicked.connect(self.submit)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 10)
        row.setSpacing(8)
        row.addWidget(self.smile_btn, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.edit, 1)
        row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

    # ------------------------------------------------------------------ 对外
    def submit(self) -> None:
        """把输入框内容抛给主窗口（空内容也抛，由主窗口统一提示，避免两处判断）。"""
        text = self.edit.toPlainText().strip()
        if text:
            self.edit.clear()
        self.messageSubmitted.emit(text)

    def text(self) -> str:
        return self.edit.toPlainText()

    def apply_palette(self, palette) -> None:
        self.smile_btn.set_icon_color(palette.muted)
        self.send_btn.set_icon_color(palette.accent_on)
        pal = self.edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor(palette.text))
        pal.setColor(QPalette.ColorRole.Base, QColor(palette.card))
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.muted))
        self.edit.setPalette(pal)

    def _insert_smiley(self) -> None:
        self.edit.insertPlainText("🙂")
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)


class MessageList(QScrollArea):
    """消息区：按日期插分隔条，新消息追加后自动滚到底。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("messageArea")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._host = QWidget()
        self._host.setObjectName("messageHost")
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(14, 12, 6, 12)
        self._layout.setSpacing(6)
        # 顶部留一根弹簧：消息少的时候贴着底部排（Telegram 的做法）
        self._layout.addStretch(1)
        self.setWidget(self._host)
        self.viewport().setAutoFillBackground(False)
        self._host.setAutoFillBackground(False)

        self._bubbles: dict[str, MessageBubble] = {}
        self._chips: list[str] = []
        self._last_day = ""
        self._group = False

    # ------------------------------------------------------------------ 构建
    def clear_messages(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._bubbles.clear()
        self._chips.clear()
        self._last_day = ""

    def set_conversation(self, conversation: Conversation) -> None:
        self.clear_messages()
        self._group = conversation.is_group
        for message in conversation.messages:
            self.append(message, conversation.sender_name(message))
        self.scroll_to_bottom()

    def append(self, message: Message, sender_name: str = "") -> MessageBubble:
        key = day_label_key(message.ts)
        if key != self._last_day:
            self._last_day = key
            label = format_day_label(message.ts)
            self._chips.append(label)
            self._add_row(DateChip(label), center=True)
        bubble = MessageBubble(message, sender_name=sender_name,
                               show_sender=self._group)
        self._add_row(bubble, center=False, outgoing=message.outgoing)
        self._bubbles[message.id] = bubble
        self._autoscroll()
        return bubble

    def refresh_message(self, message: Message) -> None:
        bubble = self._bubbles.get(message.id)
        if bubble is not None:
            bubble.set_meta(message)

    def has_message(self, message_id: str) -> bool:
        return message_id in self._bubbles

    def bubble_count(self) -> int:
        return len(self._bubbles)

    def date_chips(self) -> list[str]:
        """日期分隔条的文案（按出现顺序）。"""
        return list(self._chips)

    # ------------------------------------------------------------------ 内部
    def _add_row(self, widget: QWidget, *, center: bool,
                 outgoing: bool = False) -> None:
        row = QWidget(self._host)
        row.setObjectName("bubbleRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 8, 0)
        line.setSpacing(0)
        if center:
            line.addStretch(1)
            line.addWidget(widget)
            line.addStretch(1)
        elif outgoing:
            line.addStretch(1)
            line.addWidget(widget)
        else:
            line.addWidget(widget)
            line.addStretch(1)
        row.show()                       # 运行时新建的控件必须显式显示
        self._layout.addWidget(row)

    def _autoscroll(self) -> None:
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        QTimer.singleShot(30, lambda: bar.setValue(bar.maximum()))


class ChatView(QWidget):
    """聊天面板：头部（头像/名字/状态/更多）+ 消息区 + 输入栏。"""

    messageSubmitted = pyqtSignal(str)
    moreRequested = pyqtSignal()

    def __init__(self, store: ChatStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._conversation: Conversation | None = None
        self._typing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_empty_page())
        self.stack.addWidget(self._build_chat_page())
        self.stack.setCurrentIndex(0)
        root.addWidget(self.stack, 1)

    # ================================================================== 搭建
    def _build_header(self) -> QWidget:
        header = QWidget(self)
        header.setObjectName("chatHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header.setFixedHeight(60)

        self.header_avatar = Avatar(header, size=42, name="")
        self.name_label = QLabel("", header)
        self.name_label.setObjectName("chatName")
        self.status_label = QLabel("", header)
        self.status_label.setObjectName("chatStatus")

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        text_col.addWidget(self.name_label)
        text_col.addWidget(self.status_label)

        self.more_btn = IconButton("more", header, tooltip="更多操作")
        self.more_btn.clicked.connect(self.moreRequested)

        row = QHBoxLayout(header)
        row.setContentsMargins(16, 8, 14, 8)
        row.setSpacing(10)
        row.addWidget(self.header_avatar)
        row.addLayout(text_col)
        row.addStretch(1)
        row.addWidget(self.more_btn)
        return header

    def _build_empty_page(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("emptyPage")
        icon = QLabel(page)
        icon.setPixmap(make_icon("smile", "#9AA7B4", 64).pixmap(QSize(64, 64)))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("选一个会话开始聊天", page)
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("左侧是会话列表；需要 Base64 / AES 工具时点「工具箱」（Ctrl+T）",
                      page)
        hint.setObjectName("emptyText")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box = QVBoxLayout(page)
        box.addStretch(1)
        box.addWidget(icon)
        box.addSpacing(10)
        box.addWidget(title)
        box.addWidget(hint)
        box.addStretch(1)
        return page

    def _build_chat_page(self) -> QWidget:
        page = QWidget(self)
        self.message_list = MessageList(page)
        self.composer = Composer(page)
        self.composer.messageSubmitted.connect(self.messageSubmitted)

        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self.message_list, 1)
        box.addWidget(self.composer)
        return page

    # ================================================================== 对外
    @property
    def conversation(self) -> Conversation | None:
        return self._conversation

    def show_conversation(self, conversation: Conversation) -> None:
        self._conversation = conversation
        self._typing = False
        self.header_avatar.set_name(conversation.title)
        self.name_label.setText(conversation.title)
        others = [m for m in conversation.members if not m.is_me]
        self.header_avatar.set_online(bool(others and others[0].online))
        self.status_label.setText(conversation.subtitle())
        self.status_label.setProperty("typing", "false")
        self.message_list.set_conversation(conversation)
        self.stack.setCurrentIndex(1)
        self.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_empty(self) -> None:
        self._conversation = None
        self.stack.setCurrentIndex(0)

    def append_message(self, message: Message) -> None:
        conversation = self._store.get(message.conversation_id)
        if self._conversation is None or conversation is None:
            return
        if message.conversation_id != self._conversation.id:
            return
        sender = conversation.sender_name(message)
        self.message_list.append(message, sender)

    def sync_message(self, message: Message) -> None:
        """消息状态变化（已读回执等）：列表里已有就刷新，没有就补上。"""
        if self._conversation is None or message.conversation_id != self._conversation.id:
            return
        if self.message_list.has_message(message.id):
            self.message_list.refresh_message(message)
        else:
            self.append_message(message)

    def set_typing(self, typing: bool) -> None:
        if self._typing == typing:
            return
        self._typing = typing
        self.status_label.setProperty("typing", "true" if typing else "false")
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)
        conversation = self._conversation
        if conversation is not None:
            self.status_label.setText(conversation.subtitle(typing))

    def focus_input(self) -> None:
        if self.stack.currentIndex() == 1:
            self.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def input_text(self) -> str:
        return self.composer.text()

    def sent_bubbles(self) -> int:
        return self.message_list.bubble_count()

    def apply_palette(self, palette) -> None:
        self.more_btn.set_icon_color(palette.muted)
        self.header_avatar.apply_palette(palette)
        self.composer.apply_palette(palette)
