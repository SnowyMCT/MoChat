"""左侧会话列表：标题栏（含工具箱入口）、搜索框、会话条目、主题切换。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea,
                             QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..core.models import Conversation
from ..core.store import ChatStore
from .avatar import Avatar
from .icons import IconButton, make_icon
from .theme import THEME_OPTIONS
from .widgets import FloatingLabelInput, SegmentedControl

__all__ = ["Sidebar", "ConversationItem", "SIDEBAR_WIDTH"]

SIDEBAR_WIDTH = 296
ITEM_HEIGHT = 66


class ConversationItem(QFrame):
    """一条会话：头像 + 名字/摘要 + 时间/未读徽标。"""

    clicked = pyqtSignal(str)

    def __init__(self, conversation: Conversation, unread: int = 0, parent=None):
        super().__init__(parent)
        self.setObjectName("convItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ITEM_HEIGHT)
        self._selected = False
        self._preview_text = ""

        self.avatar = Avatar(self, size=46, name=conversation.title)
        self.name_label = QLabel(conversation.title, self)
        self.name_label.setObjectName("convName")
        self.time_label = QLabel("", self)
        self.time_label.setObjectName("convTime")
        self.preview_label = QLabel("", self)
        self.preview_label.setObjectName("convPreview")
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                         QSizePolicy.Policy.Preferred)
        self.badge = QLabel("", self)
        self.badge.setObjectName("badge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.hide()

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.name_label)
        top.addStretch(1)
        top.addWidget(self.time_label)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        bottom.addWidget(self.preview_label, 1)
        bottom.addWidget(self.badge)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        text_col.addLayout(top)
        text_col.addLayout(bottom)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(10)
        row.addWidget(self.avatar)
        row.addLayout(text_col, 1)

        self.refresh(conversation, unread)

    # ------------------------------------------------------------------ 对外
    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    def refresh(self, conversation: Conversation, unread: int,
                typing: bool = False) -> None:
        """用最新的会话数据刷新文案（列表重建与增量刷新都走这里，只有一份实现）。"""
        self._conversation_id = conversation.id
        self.name_label.setText(conversation.title)
        self.time_label.setText(conversation.preview_time())
        self._preview_text = "正在输入…" if typing else conversation.preview_text()
        self._elide_preview()
        others = [m for m in conversation.members if not m.is_me]
        self.avatar.set_name(conversation.title,
                             online=bool(others and others[0].online))
        if unread > 0:
            self.badge.setText(str(unread) if unread < 100 else "99+")
            self.badge.show()
        else:
            self.badge.hide()

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        # 动态属性改了必须让样式重新生效，否则 QSS 的选择器不会刷新
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def apply_palette(self, palette) -> None:
        self.avatar.apply_palette(palette)

    def preview_text(self) -> str:
        """未截断的摘要原文（测试与调试用）。"""
        return self._preview_text

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self._elide_preview()

    def _elide_preview(self) -> None:
        """按当前宽度把摘要截成"…"结尾，避免半截字被硬切掉。"""
        metrics = QFontMetrics(self.preview_label.font())
        width = max(40, self.preview_label.width())
        self.preview_label.setText(
            metrics.elidedText(self._preview_text, Qt.TextElideMode.ElideRight, width))

    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._conversation_id)
        super().mousePressEvent(event)


class Sidebar(QWidget):
    """整个左栏。所有交互都通过信号抛给主窗口，自己不改数据。"""

    conversationSelected = pyqtSignal(str)
    toolRequested = pyqtSignal()
    menuRequested = pyqtSignal()
    themeChanged = pyqtSignal(str)

    def __init__(self, store: ChatStore, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._store = store
        self._items: dict[str, ConversationItem] = {}
        self._keyword = ""
        self._selected_id: str = ""
        self._typing: set[str] = set()
        self._palette = None
        self._empty_hint: QLabel | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 12, 10, 12)
        root.setSpacing(10)
        root.addLayout(self._build_header())
        root.addWidget(self._build_search())
        root.addWidget(self._build_list(), 1)
        root.addWidget(self._build_footer())

    # ================================================================== 搭建
    def _build_header(self) -> QHBoxLayout:
        self.title = QLabel("MChat", self)
        self.title.setObjectName("appTitle")
        self.total_badge = QLabel("", self)
        self.total_badge.setObjectName("badge")
        self.total_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total_badge.hide()

        self.tool_btn = QToolButton(self)
        self.tool_btn.setObjectName("toolBtn")
        self.tool_btn.setText("工具箱")
        self.tool_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.tool_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tool_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tool_btn.setToolTip("打开安全工具箱：Base64 / AES-256（Ctrl+T）")
        self.tool_btn.setMinimumHeight(30)
        self.tool_btn.clicked.connect(self.toolRequested)

        self.more_btn = IconButton("more", self, tooltip="更多")
        self.more_btn.clicked.connect(self.menuRequested)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(2, 0, 0, 0)
        row.addWidget(self.title)
        row.addWidget(self.total_badge)
        row.addStretch(1)
        row.addWidget(self.tool_btn)
        row.addWidget(self.more_btn)
        return row

    def _build_search(self) -> QWidget:
        self.search_field = FloatingLabelInput("搜索会话或消息", field_height=38,
                                               radius=10)
        self.search_field.textChanged.connect(self._on_search)
        return self.search_field

    def _build_list(self) -> QScrollArea:
        self._list_host = QWidget()
        self._list_host.setObjectName("convList")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch(1)

        area = QScrollArea(self)
        area.setObjectName("convArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self._list_host)
        area.viewport().setAutoFillBackground(False)
        self._list_host.setAutoFillBackground(False)
        return area

    def _build_footer(self) -> QWidget:
        self.theme_seg = SegmentedControl(THEME_OPTIONS, height=32)
        self.theme_seg.setToolTip("界面主题")
        self.theme_seg.valueChanged.connect(self.themeChanged)
        self.mode_hint = QLabel("本机演示 · 未联网", self)
        self.mode_hint.setObjectName("sideSectionTitle")
        self.mode_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(self.theme_seg)
        box.addWidget(self.mode_hint)
        return self._wrap(box)

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ================================================================== 数据
    def refresh(self, *, keyword: str | None = None) -> None:
        """按当前搜索词重建列表（会话数量很少，重建比逐个 diff 更简单可靠）。"""
        if keyword is not None:
            self._keyword = keyword
        conversations = self._store.search(self._keyword)
        for item in self._items.values():
            item.hide()
            item.setParent(None)
            item.deleteLater()
        self._items.clear()

        for conv in conversations:
            item = ConversationItem(conv, self._store.unread(conv.id), self._list_host)
            item.clicked.connect(self.conversationSelected)
            item.set_selected(conv.id == self._selected_id)
            item.show()                     # 后建的子控件不会自动显示
            self._list_layout.insertWidget(self._list_layout.count() - 1, item)
            self._items[conv.id] = item
            if self._palette is not None:
                item.apply_palette(self._palette)
        self.refresh_total()
        if not conversations:
            if self._empty_hint is None:
                self._empty_hint = QLabel("没有匹配的会话", self._list_host)
                self._empty_hint.setObjectName("sideSectionTitle")
                self._list_layout.insertWidget(0, self._empty_hint)
                self._empty_hint.show()
        elif self._empty_hint is not None:
            self._empty_hint.hide()
            self._empty_hint.setParent(None)
            self._empty_hint.deleteLater()
            self._empty_hint = None

    def refresh_item(self, conversation_id: str) -> None:
        item = self._items.get(conversation_id)
        conv = self._store.get(conversation_id)
        if item is None or conv is None:
            self.refresh()
            return
        item.refresh(conv, self._store.unread(conversation_id),
                     typing=conversation_id in self._typing)
        self.refresh_total()

    def refresh_total(self) -> None:
        total = self._store.total_unread()
        if total > 0:
            self.total_badge.setText(str(total) if total < 100 else "99+")
            self.total_badge.show()
        else:
            self.total_badge.hide()

    def set_current(self, conversation_id: str) -> None:
        self._selected_id = conversation_id
        for cid, item in self._items.items():
            item.set_selected(cid == conversation_id)

    def set_typing(self, conversation_id: str, typing: bool) -> None:
        if typing:
            self._typing.add(conversation_id)
        else:
            self._typing.discard(conversation_id)
        self.refresh_item(conversation_id)

    # ================================================================== 皮肤
    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.search_field.apply_palette(palette)
        self.theme_seg.apply_palette(palette)
        self.tool_btn.setIcon(make_icon("apps", palette.accent, 16))
        self.more_btn.set_icon_color(palette.muted)
        for item in self._items.values():
            item.apply_palette(palette)

    # ================================================================== 内部
    def set_theme_name(self, name: str) -> None:
        if name in THEME_OPTIONS:
            self.theme_seg.set_index(THEME_OPTIONS.index(name), notify=False,
                                     animate=False)

    def _on_search(self, text: str) -> None:
        self.refresh(keyword=text)

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self.search_field.raise_()
