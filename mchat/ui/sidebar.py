"""左侧会话列表：标题栏（含工具箱入口）、搜索框、会话条目、主题切换。"""
from __future__ import annotations

from PyQt6.QtCore import (QEasingCurve, QPropertyAnimation, QSize, Qt,
                          pyqtProperty, pyqtSignal)
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea,
                             QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..core.models import Conversation
from ..core.store import ChatStore
from . import anim
from .avatar import Avatar
from . import flag_text
from .icons import IconButton, make_icon
from .theme import THEME_OPTIONS
from .widgets import FloatingLabelInput, SegmentedControl

__all__ = ["Sidebar", "ConversationItem", "PopBadge", "SIDEBAR_WIDTH",
           "SIDEBAR_MIN_WIDTH", "SIDEBAR_MAX_WIDTH"]

SIDEBAR_WIDTH = 296          # 默认宽度
SIDEBAR_MIN_WIDTH = 220      # 再窄摘要就只剩两个字了
SIDEBAR_MAX_WIDTH = 460      # 再宽聊天区就不够用
ITEM_HEIGHT = 66


class PopBadge(QLabel):
    """未读徽标：数字变化时"弹"一下。

    自绘而不靠 QSS：只有自己画，才能既不触发重新布局、又能把缩放做在绘制阶段
    （给布局里的控件动 geometry 会被布局立刻改回去）。缩放中心在正中，
    所以"弹"的时候不会把旁边的摘要挤走。
    """

    POP_FROM = 0.62           # 从多小弹起
    POP_MS = 320

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scale = 1.0
        self._palette = None
        self._bg = QColor("#3390EC")
        self._fg = QColor("#FFFFFF")
        self._anim = QPropertyAnimation(self, b"scale", self)
        self._anim.setDuration(self.POP_MS)
        self._anim.setStartValue(self.POP_FROM)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)

    def get_scale(self) -> float:
        return self._scale

    def set_scale(self, value: float) -> None:
        self._scale = float(value)
        self.update()

    scale = pyqtProperty(float, fget=get_scale, fset=set_scale)

    def pop(self) -> None:
        """数字变多时弹一下（动画关掉时就是直接停在 1.0）。"""
        self._anim.stop()
        if not anim.is_enabled():
            self.set_scale(1.0)
            return
        self.set_scale(self.POP_FROM)
        self._anim.start()

    def is_popping(self) -> bool:
        return self._anim.state() == QPropertyAnimation.State.Running

    def apply_palette(self, palette) -> None:
        """记住调色板；具体颜色看 ``muted`` 属性（免打扰的徽标是灰的）。"""
        self._palette = palette
        self.refresh_colors()

    def refresh_colors(self) -> None:
        """按当前调色板 + ``muted`` 属性重取颜色（自绘，所以颜色得自己拿）。"""
        if self._palette is None:
            return
        muted = self.property("muted") == "true"
        background = self._palette.muted if muted else self._palette.accent
        self.apply_colors(background, self._palette.accent_on)

    def apply_colors(self, background: str, foreground: str) -> None:
        self._bg = QColor(background)
        self._fg = QColor(foreground)
        self.update()

    def paintEvent(self, event) -> None:                 # noqa: N802
        base = QPainter(self)
        base.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width, height = self.width(), self.height()
        base.translate(width / 2, height / 2)
        base.scale(self._scale, self._scale)             # 以中心缩放：不挤走邻居
        base.translate(-width / 2, -height / 2)
        radius = min(width, height) / 2
        base.setPen(Qt.PenStyle.NoPen)
        base.setBrush(self._bg)
        base.drawRoundedRect(self.rect(), radius, radius)
        base.setPen(self._fg)
        base.setFont(self.font())
        base.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        base.end()


class ConversationItem(QFrame):
    """一条会话：头像 + 名字/摘要 + 时间/未读徽标。"""

    clicked = pyqtSignal(str)
    menuRequested = pyqtSignal(str, object)      # (会话id, 全局坐标) —— 右键菜单交给主窗口

    def __init__(self, conversation: Conversation, unread: int = 0, parent=None):
        super().__init__(parent)
        self._muted = False
        self.setObjectName("convItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ITEM_HEIGHT)
        self._selected = False
        self._preview_text = ""
        self._draft = ""

        self.avatar = Avatar(self, size=46, name=conversation.title)
        self.name_label = QLabel(conversation.title, self)
        self.name_label.setObjectName("convName")
        self.time_label = QLabel("", self)
        self.time_label.setObjectName("convTime")
        self.preview_label = QLabel("", self)
        self.preview_label.setObjectName("convPreview")
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                         QSizePolicy.Policy.Preferred)
        # 置顶/免打扰的小标记：放在名字和时间的中间，空着就不显示
        self.mark_label = QLabel("", self)
        self.mark_label.setObjectName("convMark")
        self.badge = PopBadge(self)
        self.badge.setObjectName("badge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.hide()
        self._unread_shown = 0           # 上一次显示的数字，用来判断"是不是变多了"

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.name_label)
        top.addStretch(1)
        top.addWidget(self.mark_label)
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
        if typing:
            self._preview_text = "正在输入…"
        elif self._draft:                    # 草稿优先于最后一条消息（Telegram 的做法）
            self._preview_text = f"草稿：{self._draft}"
        else:
            self._preview_text = conversation.preview_text()
        self._elide_preview()
        others = [m for m in conversation.members if not m.is_me]
        self.avatar.set_name(conversation.title,
                             online=bool(others and others[0].online))
        # 置顶/免打扰标记：📌 表示置顶，🔕 表示免打扰（两个都有就都显示）
        marks = ("📌" if conversation.pinned else "") + ("🔕" if conversation.muted else "")
        self.mark_label.setText(marks)
        self.mark_label.setToolTip("置顶" if conversation.pinned else "")
        if conversation.muted != self._muted:
            self._muted = conversation.muted
            # 免打扰的未读徽标是灰的（Telegram 同款），也不再弹跳，别来抢注意力
            self.badge.setProperty("muted", "true" if self._muted else "false")
            self.badge.refresh_colors()
        if unread > 0:
            grew = self.isVisible() and unread > self._unread_shown
            self.badge.setText(str(unread) if unread < 100 else "99+")
            self.badge.show()
            self._unread_shown = unread
            if grew and not conversation.muted:
                self.badge.pop()             # 数字变多才弹；首次铺列表不弹，免得太吵
        else:
            self.badge.hide()
            self._unread_shown = 0

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        # 动态属性改了必须让样式重新生效，否则 QSS 的选择器不会刷新
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def set_draft(self, text: str) -> None:
        """这一行是不是还留着没发出去的草稿（会话列表里标出来）。"""
        text = (text or "").strip()
        if text == self._draft:
            return
        self._draft = text
        self.preview_label.setProperty("draft", "true" if text else "false")
        style = self.preview_label.style()
        style.unpolish(self.preview_label)
        style.polish(self.preview_label)

    def draft(self) -> str:
        return self._draft

    def apply_palette(self, palette) -> None:
        self.avatar.apply_palette(palette)
        self._palette = palette
        self.mark_label.setStyleSheet(f"color: {palette.muted};")
        self.badge.apply_palette(palette)

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
        text = self._preview_text
        if self._draft and not text.startswith("草稿："):
            text = f"草稿：{self._draft}"
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)
        # 先按纯文本截断（截断只认字符），再把旗帜换成小图
        payload, rich = flag_text.to_display(elided, size=flag_text.PREVIEW_IMAGE_SIZE)
        self.preview_label.setTextFormat(Qt.TextFormat.RichText if rich
                                        else Qt.TextFormat.PlainText)
        self.preview_label.setText(payload)

    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._conversation_id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:           # noqa: N802
        """右键这一行 → 交给主窗口弹菜单（置顶/免打扰/标记未读/清空这个会话）。"""
        self.menuRequested.emit(self._conversation_id, event.globalPos())


class Sidebar(QWidget):
    """整个左栏。所有交互都通过信号抛给主窗口，自己不改数据。"""

    conversationSelected = pyqtSignal(str)
    conversationMenuRequested = pyqtSignal(str, object)   # (会话id, 全局坐标)
    toolRequested = pyqtSignal()
    menuRequested = pyqtSignal()
    themeChanged = pyqtSignal(str)

    def __init__(self, store: ChatStore, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 宽度由主窗口经 setFixedWidth 控制（可拖动分界线改），这里只给默认值
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._store = store
        self._items: dict[str, ConversationItem] = {}
        self._keyword = ""
        self._selected_id: str = ""
        self._typing: set[str] = set()
        self._drafts: dict[str, str] = {}
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
            item.menuRequested.connect(self.conversationMenuRequested)
            item.set_draft(self._drafts.get(conv.id, ""))
            item.set_selected(conv.id == self._selected_id)
            item.refresh(conv, self._store.unread(conv.id), typing=conv.id in self._typing)
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
        total = self._store.total_unread()      # 免打扰的会话不计（在 store 里算，只有一份逻辑）
        if total > 0:
            self.total_badge.setText(str(total) if total < 100 else "99+")
            self.total_badge.show()
        else:
            self.total_badge.hide()

    def set_drafts(self, drafts: dict[str, str]) -> None:
        """草稿表 ``{会话id: 草稿文本}``（主窗口在切会话时更新）。"""
        self._drafts = dict(drafts)
        for conversation_id, item in self._items.items():
            conversation = self._store.get(conversation_id)
            if conversation is None:
                continue
            item.set_draft(self._drafts.get(conversation_id, ""))
            item.refresh(conversation, self._store.unread(conversation_id),
                         typing=conversation_id in self._typing)

    def drafts(self) -> dict[str, str]:
        return dict(self._drafts)

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
