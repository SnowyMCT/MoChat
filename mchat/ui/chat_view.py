"""右侧聊天区：消息气泡、日期分隔条、输入栏，以及"没选会话"的空态。"""
from __future__ import annotations

from PyQt6.QtCore import (QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import (QColor, QPainter, QPainterPath, QPalette, QPen,
                         QTextCharFormat)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMenu,
                             QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
                             QTextEdit, QToolButton, QVBoxLayout, QWidget)

from ..core.models import Attachment, Conversation, Message, day_label_key, format_day_label
from ..core.store import ChatStore
from .attach import AttachmentTray, FileAttachmentView, ImageAttachmentView
from .avatar import Avatar
from . import anim
from .emoji_panel import EmojiPicker
from . import flag_text
from . import flags as flag_data
from .icons import IconButton, make_icon
from .theme import menu_qss

__all__ = ["ChatView", "MessageList", "MessageBubble", "Composer",
           "ReactionChip", "QUICK_REACTIONS", "ReplyBar", "SelectionBar",
           "SelectionCheck", "HitLayer", "QuoteBox", "MergedCard", "PinBanner"]

MAX_BUBBLE_WIDTH = 520
MIN_INPUT_LINES = 1
MAX_INPUT_LINES = 5
QUICK_REACTIONS = ("👍", "❤️", "😂", "🎉", "😮", "😢")   # 右键菜单里那一排
FLASH_MS = 1400                                          # 跳到某条消息时高亮多久
SWEEP_SLOP = 6                                           # 左键划多远才算"扫选"（不算点击）
ATTACH_IMAGE = "image"                                   # 附件选择器的两种模式
ATTACH_FILE = "file"


class SelectionCheck(QWidget):
    """批量选择模式下的圆勾（自绘 + 自己收鼠标）。

    自绘而不是放个 ``QCheckBox``：要的就是"一个圆 + 一个勾"，尺寸固定 22px。

    它就是个**小按钮**：点勾本身也要能勾上/取消这一条。这里曾经挂的是
    ``WA_TransparentForMouseEvents``（想着"点在勾上也算点在气泡上"），可勾虽然是挂在"行"上的，
    气泡那一层接收层只盖气泡自己 —— 于是点在勾上谁也收不到，点了没反应（用户报的就是这个）。
    现在它自己处理点击，并且按真实按钮的规矩：**按下 + 松手时还在它身上（而且没拖远）**
    才算一次点击 —— 这样"扫选"时鼠标从它身上划过去不会顺手翻一下。

    三个状态都要画：空圈（未选）/ 实心主题色 + 白勾（已选）/ 悬停时空圈变主题色。
    """

    SIZE = 22
    CLICK_SLOP = 6            # 拖出这么多像素就不算点击了（扫选会从勾上划过去）

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("selectionCheck")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("勾选这条消息")
        self._checked = False
        self._palette = None
        self._hover = False
        self._press_pos: QPoint | None = None

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.update()

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.update()

    def is_hovered(self) -> bool:
        return self._hover

    # ------------------------------------------------------------ 点击
    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            event.accept()                               # 接下这一次按下，松手才会回到这里
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        start = self._press_pos
        self._press_pos = None
        point = event.position().toPoint()
        if (event.button() == Qt.MouseButton.LeftButton and start is not None
                and self.rect().contains(point)
                and (point - start).manhattanLength() <= self.CLICK_SLOP):
            self.clicked.emit()                          # 只报"被点了"，勾上还是取消由消息列表决定
        event.accept()

    def enterEvent(self, event) -> None:                 # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:                 # noqa: N802
        self._hover = False
        self.update()

    def paintEvent(self, event) -> None:                 # noqa: N802
        if self._palette is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        inset = 1.5
        box = QRectF(inset, inset, self.width() - inset * 2, self.height() - inset * 2)
        if self._checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self._palette.accent))
            painter.drawEllipse(box)
            pen = QPen(QColor("#FFFFFF"), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(self.width() * 0.28, self.height() * 0.53)
            path.lineTo(self.width() * 0.45, self.height() * 0.69)
            path.lineTo(self.width() * 0.73, self.height() * 0.33)
            painter.drawPath(path)
        else:
            # 悬停时圈也上主题色：鼠标扫过去时能看出"这个圈是可以点的"
            color = self._palette.accent if self._hover else self._palette.muted
            painter.setPen(QPen(QColor(color), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(box)
        painter.end()


class HitLayer(QWidget):
    """盖在气泡上的透明接收层：批量选择时"点气泡任意位置"都算点到这一条。

    为什么不直接用气泡自己的 ``mousePressEvent``：气泡里还有文字标签、图片、文件卡片
    这些子控件，它们会先吃掉点击（而且文字还能被拖选）。盖一层就只剩一个入口，行为稳定。

    只认"按下 + 原地松手"：**扫选**（按住左键在消息区划过去）时鼠标会从这条气泡上划过，
    那时松手落在它身上不该再翻一次勾（否则划选完最后那条会被顺手取消掉）。
    """

    CLICK_SLOP = 6

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos: QPoint | None = None

    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        event.accept()                                   # 别让下面的文字被拖选

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        start = self._press_pos
        self._press_pos = None
        point = event.position().toPoint()
        if (event.button() == Qt.MouseButton.LeftButton and start is not None
                and self.rect().contains(point)
                and (point - start).manhattanLength() <= self.CLICK_SLOP):
            self.clicked.emit()
        event.accept()


class QuoteBox(QFrame):
    """气泡里的引用块（"回复 XX"）：左边一根竖条 + 作者 + 摘要，点一下跳到原消息。"""

    tapped = pyqtSignal()

    def __init__(self, reply, parent=None):
        super().__init__(parent)
        self.setObjectName("quoteBox")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("跳到这条消息")
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 4, 8, 4)
        column.setSpacing(0)
        self.author_label = QLabel(reply.author or "消息", self)
        self.author_label.setObjectName("quoteAuthor")
        column.addWidget(self.author_label)
        self._plain_text = reply.preview
        self.text_label = QLabel(self)
        self.text_label.setObjectName("quoteText")
        # 引用块也是 QTextDocument 渲染的文本：国旗要换成 <img>，否则 Windows 只肯画成
        # "JP" 两个字母（气泡正文、置顶横幅都换成图了，这里漏了）
        payload, rich = flag_text.to_display(reply.preview,
                                             size=flag_text.PREVIEW_IMAGE_SIZE)
        self.text_label.setText(payload)
        self.text_label.setTextFormat(Qt.TextFormat.RichText if rich
                                      else Qt.TextFormat.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumWidth(MAX_BUBBLE_WIDTH - 60)
        column.addWidget(self.text_label)

    def text(self) -> str:
        """引用原文（富文本时 ``text_label.text()`` 是 HTML，所以这里存了原串）。"""
        return self._plain_text

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.tapped.emit()
        super().mouseReleaseEvent(event)


class MergedCard(QFrame):
    """气泡里的「聊天记录」卡片（QQ 那种）。

    一张卡片只说三件事：**这是谁的聊天记录**、**头一句是什么**、**一共几条**。
    内容不往这张小卡片里塞 —— 塞进去又长又难读（上一版就是这么做的），
    点它打开 :class:`~mchat.ui.forward_view.MergedViewerDialog` 逐条看。
    """

    opened = pyqtSignal()                                # 点卡片（主窗口负责开查看窗口）

    def __init__(self, message: Message, parent=None):
        super().__init__(parent)
        self.setObjectName("mergedCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("打开聊天记录")
        self._message = message

        column = QVBoxLayout(self)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(3)
        self.title_label = QLabel(f"{message.forwarded_from or '聊天'} 的聊天记录", self)
        self.title_label.setObjectName("mergeTitle")
        column.addWidget(self.title_label)
        self._plain_preview = message.merged_preview()
        self.preview_label = QLabel(self)
        self.preview_label.setObjectName("mergePreview")
        self.preview_label.setMaximumWidth(MAX_BUBBLE_WIDTH - 60)
        # 合并转发里是"别人的原话"：一条带 <b> 的消息不该变成富文本 —— to_display 会先
        # 转义再插图，正好两件事一起办；国旗仍要换成 <img>（否则只有 "JP" 字母）
        payload, rich = flag_text.to_display(self._plain_preview,
                                             size=flag_text.PREVIEW_IMAGE_SIZE)
        self.preview_label.setText(payload)
        self.preview_label.setTextFormat(Qt.TextFormat.RichText if rich
                                        else Qt.TextFormat.PlainText)
        column.addWidget(self.preview_label)
        self.footer_label = QLabel(f"查看 {message.merged_count} 条转发消息 ›", self)
        self.footer_label.setObjectName("mergeFooter")
        column.addWidget(self.footer_label)

    def count_text(self) -> str:
        return self.footer_label.text()

    def preview_text(self) -> str:
        """卡片那一行的原文（富文本时 ``preview_label.text()`` 是 HTML，这里存原串）。"""
        return self._plain_preview

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.opened.emit()
        super().mouseReleaseEvent(event)


class ReactionChip(QLabel):
    """气泡下方的一个表情回应（点一下加/撤）。"""

    clicked = pyqtSignal(str)

    def __init__(self, emoji: str, count: int, mine: bool, parent=None):
        super().__init__(f"{emoji} {count}" if count > 1 else emoji, parent)
        self.emoji = emoji
        self.setObjectName("reactionChip")
        self.setProperty("mine", "true" if mine else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("取消回应" if mine else "加上回应")

    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.emoji)
        super().mousePressEvent(event)


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
    """一条消息的气泡：左侧（收到的）白/深底，右侧（发出的）主题浅色底。

    带附件的消息在气泡里先画附件（图片缩略图 / 文件卡片），配文画在下面；
    纯附件消息不建那个空的文字标签（否则气泡里会多出一行空白）。
    回复的消息最上面加一条引用块；转发来的消息加一行"转发自 X"；
    合并转发用一张卡片代替正文（免得几十条消息糊成一大坨文字）。
    """

    menuRequested = pyqtSignal(str, object)     # (message_id, 全局坐标)
    reactionToggled = pyqtSignal(str, str)      # (message_id, emoji)
    attachmentRequested = pyqtSignal(str, str)  # (message_id, "view" / "open" / "folder")
    quoteTapped = pyqtSignal(str)               # 点了引用块：跳到被引用的那条
    mergedOpened = pyqtSignal(str)              # 点了合并转发卡片（开"聊天记录"窗口）
    selectionToggled = pyqtSignal(str)          # 批量选择模式下点了这条

    def __init__(self, message: Message, *, sender_name: str = "",
                 show_sender: bool = False, palette=None, parent=None):
        super().__init__(parent)
        self.message_id = message.id
        self.outgoing = message.outgoing
        self.attachment: Attachment | None = message.attachment
        self.setObjectName("bubbleOut" if message.outgoing else "bubbleIn")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        column = QVBoxLayout(self)
        column.setContentsMargins(11, 7, 11, 6)
        column.setSpacing(2)

        if show_sender and not message.outgoing and sender_name:
            name = QLabel(sender_name, self)
            name.setObjectName("senderName")
            column.addWidget(name)

        if message.is_forwarded:                 # "➡ 转发自 王尼玛"
            caption = QLabel(f"➡ 转发自 {message.forwarded_from}", self)
            caption.setObjectName("forwardCaption")
            column.addWidget(caption)

        self.quote: QuoteBox | None = None
        if message.reply_to is not None:         # 回复：最上面那条引用块
            self.quote = QuoteBox(message.reply_to, self)
            self.quote.tapped.connect(lambda: self.quoteTapped.emit(self.message_id))
            column.addWidget(self.quote)

        # 附件：图片画缩略图，其余画文件卡片（点一下打开，右键交给气泡菜单）
        self.image_view: ImageAttachmentView | None = None
        self.file_card: FileAttachmentView | None = None
        if self.attachment is not None:
            column.addWidget(self._build_attachment(self.attachment, palette))

        self.merged_card: MergedCard | None = None
        if message.is_merged:                    # 合并转发：用卡片代替正文
            self.merged_card = MergedCard(message, self)
            self.merged_card.opened.connect(
                lambda: self.mergedOpened.emit(self.message_id))
            column.addWidget(self.merged_card)

        self.text_label = QLabel(self)
        self.text_label.setObjectName("msgText")
        # 含旗帜的消息走富文本（旗帜换成 <img>）；其余仍是纯文本，消息里带 < > 也不当 HTML
        self._plain_text = message.text
        payload, rich = flag_text.to_display(message.text)
        self.text_label.setText(payload)
        self.text_label.setTextFormat(Qt.TextFormat.RichText if rich
                                     else Qt.TextFormat.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumWidth(MAX_BUBBLE_WIDTH - 26)
        self.text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        if message.text:                      # 纯附件消息不用这一个空标签
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

        # 表情回应：一行小胶囊，空着的时候整行隐藏
        self.reactions_host = QWidget(self)
        self.reactions_row = QHBoxLayout(self.reactions_host)
        self.reactions_row.setContentsMargins(0, 3, 0, 0)
        self.reactions_row.setSpacing(4)
        self.reactions_host.hide()
        column.addWidget(self.reactions_host)
        self._chips: dict[str, ReactionChip] = {}
        self.reactionCounts: dict[str, int] = {}
        self.myReactions: set[str] = set()

        # 批量选择：圆勾（由消息列表插到行里）+ 盖在气泡上的透明接收层
        self.check = SelectionCheck(self)
        self.check.apply_palette(palette)     # 自绘控件：先给调色板，不然勾画不出来
        self.check.hide()
        self._select_mode = False
        self._selected = False
        self._hit: HitLayer | None = None
        # 圆勾自己收鼠标：点勾 = 勾上/取消这一条（和点气泡走同一条路，都是 selectionToggled）
        self.check.clicked.connect(
            lambda: self.selectionToggled.emit(self.message_id))

        self.setMaximumWidth(MAX_BUBBLE_WIDTH)
        self.set_meta(message)

    # ------------------------------------------------------------ 引用 / 转发
    def quote_text(self) -> str:
        """引用块的文案（没有引用就是空串）。"""
        if self.quote is None:
            return ""
        return f"{self.quote.author_label.text()}：{self.quote.text()}"

    # ---------------------------------------------------------------- 选择
    def is_selected(self) -> bool:
        return self._selected

    def selection_mode(self) -> bool:
        return self._select_mode

    def set_selected(self, selected: bool) -> None:
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self.check.set_checked(selected)
        # 动态属性 + repolish（和 flash 一个路子，别逐帧 setStyleSheet）
        self.setProperty("selected", "true" if selected else "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def set_selection_mode(self, active: bool) -> None:
        """进出批量选择模式：显示圆勾 + 盖一层接收层（点哪儿都算点这条）。"""
        active = bool(active)
        if active == self._select_mode:
            return
        self._select_mode = active
        if active:
            self.check.show()
            self._hit = HitLayer(self)
            self._hit.setObjectName("bubbleHit")
            self._hit.setGeometry(self.rect())
            self._hit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._hit.customContextMenuRequested.connect(
                lambda pos: self.menuRequested.emit(
                    self.message_id, self._hit.mapToGlobal(pos)))
            self._hit.clicked.connect(lambda: self.selectionToggled.emit(self.message_id))
            self._hit.show()
            self._hit.raise_()
        else:
            self.check.hide()
            if self._hit is not None:
                self._hit.hide()
                self._hit.setParent(None)
                self._hit.deleteLater()
                self._hit = None
            self.set_selected(False)

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        if self._hit is not None:                        # 接收层始终盖满整条气泡
            self._hit.setGeometry(self.rect())

    def set_meta(self, message: Message) -> None:
        """只刷新时间与已读标记（消息内容不变，不重建控件）。"""
        self.time_label.setText(message.time_text)
        if self.outgoing:
            self.read_label.setText("✓✓" if message.is_read else "✓")

    def text(self) -> str:
        """消息原文（富文本时 ``text_label.text()`` 是 HTML，所以这里存了原串）。"""
        return self._plain_text

    # ------------------------------------------------------------------ 附件
    def _build_attachment(self, attachment: Attachment,
                          palette) -> QWidget:
        """按类型造图片视图或文件卡片，并把它的点击/右键接到气泡自己的信号上。"""
        if attachment.kind == "image":
            view = ImageAttachmentView(attachment, self)
            self.image_view = view
        else:
            view = FileAttachmentView(attachment, self, palette=palette)
            self.file_card = view
        view.activated.connect(
            lambda action, m=self.message_id: self.attachmentRequested.emit(m, action))
        view.menuRequested.connect(
            lambda pos, m=self.message_id: self.menuRequested.emit(m, pos))
        return view

    def has_attachment(self) -> bool:
        return self.attachment is not None

    def apply_palette(self, palette) -> None:
        """换主题时重画附件里"带颜色"的部分（文件图标）；图片是像素，不用动。"""
        if self.file_card is not None and palette is not None:
            self.file_card.apply_palette(palette)
        self.check.apply_palette(palette)        # 圆勾是自绘的，也得跟着换色

    # ------------------------------------------------------------------ 回应
    def set_reactions(self, counts: dict[str, int], mine: set[str]) -> None:
        """按 ``{表情: 数量}`` 重建胶囊（数量很少，重建比逐个 diff 简单可靠）。"""
        self.reactionCounts = dict(counts)
        self.myReactions = set(mine)
        previous = set(self._chips)             # 记住原来有哪些：新加的才做弹出动画
        for chip in self._chips.values():
            chip.hide()
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()
        for emoji, count in self.reactionCounts.items():
            chip = ReactionChip(emoji, count, emoji in self.myReactions, self.reactions_host)
            chip.clicked.connect(lambda e, m=self.message_id: self.reactionToggled.emit(m, e))
            chip.show()                      # 运行时新建的控件必须显式显示
            self.reactions_row.addWidget(chip)
            self._chips[emoji] = chip
            if emoji not in previous:
                anim.fade_in(chip, ms=anim.FAST_MS)   # 新加的回应"弹"出来
        self.reactions_host.setVisible(bool(self._chips))

    def chip(self, emoji: str) -> ReactionChip | None:
        return self._chips.get(emoji)

    def reaction_emojis(self) -> list[str]:
        return list(self._chips)

    def has_reactions(self) -> bool:
        return bool(self._chips)

    # ------------------------------------------------------------------ 高亮
    def flash(self, ms: int = FLASH_MS) -> None:
        """跳到这条消息时闪一下边框（动态属性 + repolish，别逐帧 setStyleSheet）。"""
        self.setProperty("flash", "true")
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        QTimer.singleShot(ms, self._unflash)

    def is_flashing(self) -> bool:
        return self.property("flash") == "true"

    def _unflash(self) -> None:
        self.setProperty("flash", "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    # ------------------------------------------------------------------ 入场
    def animate_in(self, ms: int = anim.NORMAL_MS) -> None:
        """新消息入场：**只淡入**，气泡一上来就占最终大小。

        以前是"高度 0 长到自然高度 + 淡入"，实测量出来三个毛病：开头 30ms 停着不动、
        长个儿的时候气泡在行里被反复居中（y 在 616~628 之间抖）、收尾恢复高度时整体
        跳 48px —— 观感就是"卡一下"。根因是**动了几何**：每帧都要重排整块列表。
        所以改成只动透明度，几何全程不变；"从下面滑上来"那点意思交给滚动条
        （见 :func:`anim.slide_scroll`，只在真有滚动余量时才做）。
        """
        anim.fade_in(self, ms=ms)

    def is_animating(self) -> bool:
        """还在入场动画里吗（测试用：动画结束应当摘掉透明效果、几何自始至终不变）。"""
        return self.graphicsEffect() is not None

    def contextMenuEvent(self, event) -> None:           # noqa: N802
        self.menuRequested.emit(self.message_id, event.globalPos())


class MessageEdit(QTextEdit):
    """聊天输入框：自增高 1~5 行，Enter 发送、Shift+Enter 换行，旗帜显示成图片。

    为什么是 QTextEdit：只有 QTextDocument 能内嵌图片。``setAcceptRichText(False)``
    把"外部粘进来的富文本"降级成纯文本（免得带进外来的字体/颜色/链接），
    而程序化 ``insertHtml`` 插入的旗帜图片不受影响。

    **取文本一定要用** :meth:`message_text`：``toPlainText()`` 会把图片变成
    ``U+FFFC``（对象替换符），直接发出去消息内容就废了。

    剪贴板里是**图片**（截图工具、从浏览器复制的图）时不往文档里塞，而是发
    :attr:`imagePasted` 信号 —— 图片要当成"附件"发出去，走的是和选文件同一条路。
    """

    submitted = pyqtSignal()
    imagePasted = pyqtSignal(object)         # QImage：从剪贴板粘进来的图片

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composerInput")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptRichText(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setPlaceholderText("输入消息，Enter 发送 · Shift+Enter 换行")
        self.textChanged.connect(self._sync_height)
        self._sync_height()

    # ---------------------------------------------------------------- 文本
    def message_text(self) -> str:
        """取回纯文本：把嵌进去的旗帜图片还原成字符。

        **不能只看 ``charFormat().isImageFormat()``**：在图片后面紧跟着插入的文字
        （甚至 ``setPlainText``）会继承那个图片格式 —— 实测一个装着 "<b>加粗</b>"
        的片段会报 ``isImageFormat() == True``。判据以 Qt 自己的行为为准：
        真正的图片在 ``toPlainText()`` 里是 ``U+FFFC``（对象替换符），
        所以先拿 ``toPlainText()``，再把其中的 ``U+FFFC`` 按文档顺序换成旗帜字符。
        """
        plain = self.toPlainText()
        if "\ufffc" not in plain:
            return plain
        urls: list[str] = []
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.text() == "\ufffc":
                    fmt = fragment.charFormat()
                    urls.append(fmt.toImageFormat().name() if fmt.isImageFormat() else "")
                iterator += 1
            block = block.next()
        out: list[str] = []
        slot = 0
        for char in plain:
            if char == "\ufffc":
                url = urls[slot] if slot < len(urls) else ""
                out.append(flag_data.char_for_url(url))
                slot += 1
            else:
                out.append(char)
        return "".join(out)

    def set_message_text(self, text: str) -> None:
        """整段换成 ``text``（旗帜仍以字符传入，显示时才变图）。"""
        self.clear()
        if text:
            self.insert_text(text)

    def insert_text(self, text: str) -> None:
        """插入文本：遇到旗帜就插图，其余照常敲字。"""
        cursor = self.textCursor()
        for piece, is_flag in flag_text.split_flags(text):
            if is_flag:
                url = flag_data.png_url(piece)
                if url:
                    cursor.insertHtml(
                        f'<img src="{url}" width="{flag_text.TEXT_IMAGE_SIZE}"'
                        f' height="{flag_text.TEXT_IMAGE_SIZE}">')
                    # 插图会把"当前字符格式"留成图片格式，不抹掉的话接着敲的字
                    # 会带着它（连 setPlainText 都会被带偏），取文本时就会读错
                    cursor.setCharFormat(QTextCharFormat())
                    continue
            cursor.insertText(piece, QTextCharFormat())
        self.setTextCursor(cursor)

    def insertFromMimeData(self, source) -> None:        # noqa: N802
        """粘贴走同一条路：粘进来的旗帜也应该是图片。

        **图片优先**：剪贴板里是一张图（截图、复制的图）时不当文字处理，交给
        :attr:`imagePasted` —— 由上层落成一个 PNG 文件再当附件发（Telegram 同款行为）。
        """
        if source.hasImage():
            image = source.imageData()
            if image is not None and not image.isNull():
                self.imagePasted.emit(image)
                return
        if source.hasText():
            self.insert_text(source.text())
            return
        super().insertFromMimeData(source)

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


class ReplyBar(QFrame):
    """输入栏上方的"回复 王尼玛：…"条 —— 点 ✕ 取消回复。"""

    removed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("replyBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._reply = None
        self._plain_text = ""                    # 待回复那条的原文（见 text()）

        self.title_label = QLabel("", self)
        self.title_label.setObjectName("replyTitle")
        self.text_label = QLabel("", self)
        self.text_label.setObjectName("replyText")
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(self.title_label)
        texts.addWidget(self.text_label)

        self.remove_btn = QToolButton(self)
        self.remove_btn.setObjectName("closeBtn")
        self.remove_btn.setText("✕")
        self.remove_btn.setFixedSize(26, 26)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.remove_btn.setToolTip("取消回复")
        self.remove_btn.clicked.connect(self.clear)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 5, 6, 5)
        outer.setSpacing(6)
        outer.addLayout(texts, 1)
        outer.addWidget(self.remove_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()

    def reply(self):
        return self._reply

    def set_reply(self, reply) -> None:
        """挂上一条回复（``ReplyRef``）；传 None 就是收起这一条。"""
        self._reply = reply
        if reply is None:
            self.hide()
            return
        self.title_label.setText(f"回复 {reply.author}")
        # 回复条也是 QTextDocument 渲染的文本：国旗要换成 <img>（否则只有 "JP" 字母）
        self._plain_text = reply.preview
        payload, rich = flag_text.to_display(reply.preview,
                                             size=flag_text.PREVIEW_IMAGE_SIZE)
        self.text_label.setText(payload)
        self.text_label.setTextFormat(Qt.TextFormat.RichText if rich
                                      else Qt.TextFormat.PlainText)
        self.show()

    def text(self) -> str:
        """待回复那条的原文（富文本时 ``text_label.text()`` 是 HTML，这里存原串）。"""
        return self._plain_text

    def clear(self) -> None:
        """✕ / 发完消息之后收起（发信号让主窗口把"待回复"也清掉）。"""
        if self._reply is None:
            return
        self._reply = None
        self.hide()
        self.removed.emit()


class Composer(QWidget):
    """底部输入栏：附件、表情、输入框、发送按钮；上方是"待发送附件"条。"""

    messageSubmitted = pyqtSignal(str)
    emojiPanelToggled = pyqtSignal(bool)     # 表情按钮自己只管状态，开哪个面板由聊天区决定
    attachRequested = pyqtSignal(str)        # 点了"图片"/"文件"：由主窗口弹文件选择框
    attachmentRemoved = pyqtSignal()         # 点了待发送条上的 ✕
    imagePasted = pyqtSignal(object)         # 输入框里粘进来一张图
    replyRemoved = pyqtSignal()              # 取消回复（点了回复条上的 ✕）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._palette = None

        # 回复条在最上面，其次是待发送附件条，都没有的时候整条隐藏（高度收回去）
        self.reply_bar = ReplyBar(self)
        self.reply_bar.removed.connect(self.replyRemoved)
        self.tray = AttachmentTray(self)
        self.tray.removed.connect(self.attachmentRemoved)

        self.clip_btn = IconButton("clip", self, size=20, box=36,
                                   tooltip="发送图片或文件（Ctrl+V 也能直接粘截图）")
        self.clip_btn.clicked.connect(self.show_attach_menu)
        self.smile_btn = IconButton("smile", self, size=20, box=36,
                                    tooltip="表情（Esc 关闭）")
        self.smile_btn.setCheckable(True)
        self.smile_btn.toggled.connect(self.emojiPanelToggled)
        self.edit = MessageEdit(self)
        self.edit.submitted.connect(self.submit)
        self.edit.imagePasted.connect(self.imagePasted)
        self.send_btn = IconButton("send", self, size=18, box=42,
                                   tooltip="发送（Enter）", object_name="sendBtn")
        self.send_btn.clicked.connect(self.submit)

        row = QHBoxLayout()
        row.setContentsMargins(12, 8, 12, 10)
        row.setSpacing(8)
        row.addWidget(self.clip_btn, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.smile_btn, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.edit, 1)
        row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.reply_bar)
        root.addWidget(self.tray)
        root.addLayout(row)

    # ------------------------------------------------------------------ 回复
    def set_reply(self, reply) -> None:
        self.reply_bar.set_reply(reply)

    def reply(self):
        return self.reply_bar.reply()

    def clear_reply(self) -> None:
        self.reply_bar.clear()

    # ------------------------------------------------------------------ 附件
    def build_attach_menu(self) -> QMenu:
        """附件按钮的下拉菜单（拆出来是为了测试能点名动作，不用真的弹出来）。"""
        menu = QMenu(self)
        if self._palette is not None:
            menu.setStyleSheet(menu_qss(self._palette))
        menu.addAction("🖼　图片…", lambda: self.attachRequested.emit(ATTACH_IMAGE))
        menu.addAction("📎　文件…", lambda: self.attachRequested.emit(ATTACH_FILE))
        return menu

    def show_attach_menu(self) -> None:
        menu = self.build_attach_menu()
        # 往上弹：输入栏贴着窗口底边，往下弹会有一半在屏幕外
        menu.exec(self.clip_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))

    def set_attachment(self, attachment: Attachment | None) -> None:
        """挂一个待发送附件（图片/文件都走这里，选文件和粘剪贴板最后都汇到这里）。"""
        self.tray.set_attachment(attachment)

    def attachment(self) -> Attachment | None:
        return self.tray.attachment()

    def has_attachment(self) -> bool:
        return self.tray.has_attachment()

    def clear_attachment(self) -> None:
        if self.tray.has_attachment():
            self.tray.clear()

    # ------------------------------------------------------------------ 对外
    def submit(self) -> None:
        """把输入框内容抛给主窗口（空内容也抛，由主窗口统一提示，避免两处判断）。"""
        text = self.edit.message_text().strip()
        if text:
            self.edit.clear()
        self.messageSubmitted.emit(text)

    def text(self) -> str:
        return self.edit.message_text()

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.clip_btn.set_icon_color(palette.muted)
        self.smile_btn.set_icon_color(palette.muted)
        self.send_btn.set_icon_color(palette.accent_on)
        self.tray.apply_palette(palette)
        pal = self.edit.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor(palette.text))
        pal.setColor(QPalette.ColorRole.Base, QColor(palette.card))
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.muted))
        self.edit.setPalette(pal)

    def _insert_smiley(self) -> None:
        """（保留给"快速插入一个默认表情"的调用方；界面上的按钮已经改成开面板。）"""
        self.insert_text("🙂")

    def insert_text(self, text: str) -> None:
        """把文字插到光标处（表情面板点选后走这里）。"""
        self.edit.insert_text(text)
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_smile_checked(self, checked: bool) -> None:
        """由聊天区回写按钮状态（程序化关面板时用）。"""
        self.smile_btn.setChecked(checked)


class SelectionBar(QFrame):
    """批量选择条：已选 N 条 + 转发 / 删除 / 取消（贴在输入栏上方）。"""

    forwardRequested = pyqtSignal()
    deleteRequested = pyqtSignal()
    cancelRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("selectBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.count_label = QLabel("已选 0 条", self)
        self.count_label.setObjectName("selectCount")
        self.hint_label = QLabel("点气泡勾选，可批量转发或删除", self)
        self.hint_label.setObjectName("selectHint")
        self.forward_btn = QPushButton("转发", self)
        self.forward_btn.setObjectName("ghostBtn")
        self.forward_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forward_btn.clicked.connect(self.forwardRequested)
        self.delete_btn = QPushButton("删除", self)
        self.delete_btn.setObjectName("dangerBtn")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(self.deleteRequested)
        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.setObjectName("ghostBtn")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.cancelRequested)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(8)
        row.addWidget(self.count_label)
        row.addWidget(self.hint_label)
        row.addStretch(1)
        row.addWidget(self.forward_btn)
        row.addWidget(self.delete_btn)
        row.addWidget(self.cancel_btn)
        self.set_count(0)
        self.hide()

    def set_count(self, count: int) -> None:
        self.count_label.setText(f"已选 {count} 条")
        for button in (self.forward_btn, self.delete_btn):
            button.setEnabled(count > 0)

    def count_text(self) -> str:
        return self.count_label.text()


class PinBanner(QFrame):
    """置顶横幅：**可以置顶多条**，这条横幅显示当前那一条。

    左边是序号「1/3」（多条时才出现），‹ › 翻页，点文字跳到当前这条，✕ 只取消当前这条
    （别的置顶留着）。翻到第几条是**界面自己的状态**，主窗口每次把整串置顶交过来。
    """

    jumped = pyqtSignal()
    cycleRequested = pyqtSignal(int)     # -1 / +1
    unpinRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pinBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("跳到这条消息")
        self._pins: list[tuple[object, str]] = []
        self._index = 0

        self.title_label = QLabel("📌 置顶消息", self)
        self.title_label.setObjectName("pinTitle")
        self.counter_label = QLabel("", self)
        self.counter_label.setObjectName("pinCounter")
        self.scope_label = QLabel("", self)
        self.scope_label.setObjectName("pinScope")
        self.text_label = QLabel("", self)
        self.text_label.setObjectName("pinText")
        self.text_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)

        self.prev_btn = self._nav_button("‹", "上一条置顶")
        self.next_btn = self._nav_button("›", "下一条置顶")
        self.prev_btn.clicked.connect(lambda: self.cycleRequested.emit(-1))
        self.next_btn.clicked.connect(lambda: self.cycleRequested.emit(1))

        self.unpin_btn = QToolButton(self)
        self.unpin_btn.setObjectName("closeBtn")
        self.unpin_btn.setText("✕")
        self.unpin_btn.setFixedSize(26, 26)
        self.unpin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unpin_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.unpin_btn.setToolTip("取消这一条的置顶（其余几条留着）")
        self.unpin_btn.clicked.connect(self.unpinRequested)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 5, 10, 5)
        row.setSpacing(6)
        row.addWidget(self.title_label)
        row.addWidget(self.counter_label)
        row.addWidget(self.scope_label)
        row.addWidget(self.prev_btn)
        row.addWidget(self.next_btn)
        row.addWidget(self.text_label, 1)
        row.addWidget(self.unpin_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()

    def _nav_button(self, text: str, tip: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("pinNav")
        button.setText(text)
        button.setFixedSize(20, 20)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tip)
        button.hide()
        return button

    # ------------------------------------------------------------------ 对外
    def set_pins(self, pins) -> None:
        """整串置顶交过来：``[(message, 作者, PinRef), ...]``（最前面的是最近置顶的）。"""
        self._pins = list(pins)
        if not self._pins:
            self._index = 0
            self.hide()
            return
        self._index = max(0, min(self._index, len(self._pins) - 1))
        self._sync()
        self.show()

    def index(self) -> int:
        return self._index

    def total(self) -> int:
        return len(self._pins)

    def message_id(self) -> str:
        return getattr(self.current()[0], "id", "") if self._pins else ""

    def pin_ref(self):
        """当前显示这条的置顶信息（PinRef；没有就 None）。"""
        return self.current()[2] if self._pins else None

    def scope_text(self) -> str:
        return self.scope_label.text()

    def current(self):
        return self._pins[self._index] if self._pins else (None, "", None)

    def counter_text(self) -> str:
        return self.counter_label.text()

    def text(self) -> str:
        return self.text_label.text()

    def cycle(self, step: int) -> None:
        """翻到上/下一条（到头就绕回去）。"""
        if len(self._pins) < 2:
            return
        self._index = (self._index + step) % len(self._pins)
        self._sync()

    def _sync(self) -> None:
        message, author, pin = self.current()
        preview = " ".join(getattr(message, "preview_text", "").split()) or "（空消息）"
        # 截断走 flag_text.truncate：按字符切很容易切在两字符国旗的中间，
        # 剩下半面会渲染成一个字母方块
        preview = flag_text.truncate(preview, 42)
        line = f"{author}：{preview}" if author else preview
        # 横幅也是 QTextDocument 渲染的文本：国旗要换成 <img>，不然 Windows 只肯
        # 画成 "CN" 两个字母（气泡里能显示成图，这里一直漏了）
        payload, rich = flag_text.to_display(line)
        self.text_label.setText(payload)
        self.text_label.setTextFormat(Qt.TextFormat.RichText if rich
                                      else Qt.TextFormat.PlainText)
        self.scope_label.setText(getattr(pin, "label", ""))     # 仅我可见 / 双方可见
        many = len(self._pins) > 1
        self.counter_label.setText(f"{self._index + 1}/{len(self._pins)}" if many else "")
        self.counter_label.setVisible(many)
        self.prev_btn.setVisible(many)
        self.next_btn.setVisible(many)

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.jumped.emit()
        super().mouseReleaseEvent(event)


class MessageList(QScrollArea):
    """消息区：按日期插分隔条，新消息追加后自动滚到底。"""

    FOLLOW_SLACK = 24            # 贴底判定的容差（滚轮抖一下也算贴底）

    menuRequested = pyqtSignal(str, object)
    reactionToggled = pyqtSignal(str, str)
    attachmentRequested = pyqtSignal(str, str)
    quoteTapped = pyqtSignal(str)
    mergedOpened = pyqtSignal(str)
    selectionModeChanged = pyqtSignal(bool)
    selectionChanged = pyqtSignal(int)          # 选了几条

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
        self._palette = None
        self._selection_mode = False
        self._selected: set[str] = set()
        # 扫选（按住左键在消息区划过去 = Telegram 那种拖动多选）：
        # 顶层装一个**只看不吞**的过滤器 —— 事件照常送到原来的控件（文字照样能选、
        # 引用块/卡片照样能点），我们只是借道记下"按在哪儿、划过了谁"。
        self._sweep: dict | None = None
        self._swept: set[str] = set()
        self._sweep_paint = True
        # 观察者只在"看得见"的时候挂着（show/hide 成对进出）：隐藏着还收全应用的事件纯属浪费，
        # 而且窗口一关（hideEvent）就自动摘掉 —— 不留一个指向已删对象的野指针在 QApplication 上。
        self._application = QApplication.instance()
        self._filter_installed = False
        self.destroyed.connect(lambda *_: self._detach_sweep_filter())

    # ------------------------------------------------------------------ 构建
    def clear_messages(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            widget = item.widget()
            if widget is not None:
                anim.cancel(widget)      # 先停动画：删除是 deleteLater，回调可能才刚起来
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._bubbles.clear()
        self._chips.clear()
        self._selected.clear()
        self._last_day = ""

    def set_conversation(self, conversation: Conversation) -> None:
        self.clear_messages()
        self._group = conversation.is_group
        for message in conversation.messages:
            self.append(message, conversation.sender_name(message), animate=False)
        self.scroll_to_bottom()

    def append(self, message: Message, sender_name: str = "",
               animate: bool = True) -> MessageBubble:
        key = day_label_key(message.ts)
        if key != self._last_day:
            self._last_day = key
            label = format_day_label(message.ts)
            self._chips.append(label)
            self._add_row(DateChip(label), center=True)
        bubble = MessageBubble(message, sender_name=sender_name,
                               show_sender=self._group, palette=self._palette)
        bubble.menuRequested.connect(self.menuRequested)
        bubble.reactionToggled.connect(self.reactionToggled)
        bubble.attachmentRequested.connect(self.attachmentRequested)
        bubble.quoteTapped.connect(self.quoteTapped)
        bubble.mergedOpened.connect(self.mergedOpened)
        bubble.selectionToggled.connect(self._on_bubble_clicked)
        if self._selection_mode:            # 选择模式下新到的消息也能被勾
            bubble.set_selection_mode(True)
        self._add_row(bubble, center=False, outgoing=message.outgoing, check=bubble.check)
        self._bubbles[message.id] = bubble
        # 新消息入场 = 淡入 + （有滚动余量时）从下往上滑进视野。几何全程不动，
        # 所以不需要"动画结束再补滚动"那套，也不会抖。
        # 注意：滑动期间**不能**再调 ``_autoscroll`` —— 它一跳到底就把滑动顶没了。
        if animate:
            bubble.animate_in()
            if not self._slide_new_bubble(bubble):
                self._autoscroll()          # 没有可滑的余量：一次贴底收工
        else:
            self._autoscroll()
        return bubble

    def _slide_new_bubble(self, bubble: MessageBubble) -> bool:
        """把新气泡"滑"进视野（只动滚动条）。没有滚动余量就不滑。"""
        height = max(1, bubble.sizeHint().height())
        return anim.slide_scroll(self.verticalScrollBar(), height) is not None

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

    def bubbles(self) -> dict[str, MessageBubble]:
        return dict(self._bubbles)

    def apply_palette(self, palette) -> None:
        """记下调色板（新气泡要按它画文件图标），并把已有的气泡重刷一遍。"""
        self._palette = palette
        for bubble in self._bubbles.values():
            bubble.apply_palette(palette)

    def remove_message(self, message_id: str) -> bool:
        """从列表里拿掉一条消息（撤回）：气泡和它所在的那一行一起撤。"""
        bubble = self._bubbles.pop(message_id, None)
        if bubble is None:
            return False
        if message_id in self._selected:
            self._selected.discard(message_id)
            self._emit_selection()
        row = bubble.parentWidget()
        anim.cancel(bubble)
        bubble.hide()
        bubble.setParent(None)
        bubble.deleteLater()
        if row is not None and row is not self._host:
            row.hide()
            row.setParent(None)
            row.deleteLater()
        return True

    # ------------------------------------------------------------------ 批量选择
    def set_selection_mode(self, active: bool) -> None:
        """进/出批量选择模式（进去后每条气泡带圆勾，点气泡就勾上/取消）。"""
        active = bool(active)
        if active == self._selection_mode:
            return
        self._selection_mode = active
        if not active:
            self._selected.clear()
        for bubble in self._bubbles.values():
            bubble.set_selection_mode(active)
        self.selectionModeChanged.emit(active)
        self._emit_selection()

    def selection_mode(self) -> bool:
        return self._selection_mode

    def selected_ids(self) -> list[str]:
        """选中的消息 id（按消息在会话里的先后顺序，转发/删除都按这个顺序来）。"""
        return [message_id for message_id in self._bubbles if message_id in self._selected]

    def selected_count(self) -> int:
        return len(self._selected)

    def clear_selection(self) -> None:
        if not self._selected:
            return
        for message_id in list(self._selected):
            bubble = self._bubbles.get(message_id)
            if bubble is not None:
                bubble.set_selected(False)
        self._selected.clear()
        self._emit_selection()

    def select_message(self, message_id: str, selected: bool) -> bool:
        """把一条设成选中/未选中，返回是否真的变了。

        扫选要用"同一个状态刷一串"（从没选中的那条开始划 = 一路选上，从已选中的那条
        开始划 = 一路取消），所以这里要的是幂等的"设成"，而不是 :meth:`toggle_selection`。
        """
        bubble = self._bubbles.get(message_id)
        if bubble is None:
            return False
        selected = bool(selected)
        if selected == (message_id in self._selected):
            return False
        if selected:
            self._selected.add(message_id)
        else:
            self._selected.discard(message_id)
        bubble.set_selected(selected)
        return True

    def toggle_selection(self, message_id: str) -> bool:
        """勾上/取消一条，返回它现在的状态。"""
        if message_id not in self._bubbles:
            return False
        self.select_message(message_id, message_id not in self._selected)
        self._emit_selection()
        return message_id in self._selected

    def _on_bubble_clicked(self, message_id: str) -> None:
        if self._selection_mode:
            self.toggle_selection(message_id)

    def _emit_selection(self) -> None:
        self.selectionChanged.emit(len(self._selected))

    def focus_message(self, message_id: str, *, flash: bool = True) -> bool:
        """滚到某条消息并闪一下（搜索结果跳转用）。"""
        bubble = self._bubbles.get(message_id)
        if bubble is None:
            return False
        self.ensureWidgetVisible(bubble, 0, 80)
        if flash:
            bubble.flash()
        return True

    # ------------------------------------------------------------------ 扫选
    def _attach_sweep_filter(self) -> None:
        """把"划选观察者"挂到顶层（只挂一次）。"""
        if self._filter_installed or self._application is None:
            return
        self._application.installEventFilter(self)
        self._filter_installed = True

    def _detach_sweep_filter(self) -> None:
        """摘掉顶层观察者。

        ``destroyed`` 里调它会拿到"已经删掉的 C++ 对象"（PyQt 抛 RuntimeError）——
        那时候应用正在收摊，吞掉就行（正常路径由 hideEvent 先摘干净了）。
        """
        if not self._filter_installed:
            return
        try:
            self._application.removeEventFilter(self)
        except RuntimeError:                 # pragma: no cover - 只在销毁路径上出现
            pass
        self._filter_installed = False

    def showEvent(self, event) -> None:                  # noqa: N802
        super().showEvent(event)
        self._attach_sweep_filter()

    def hideEvent(self, event) -> None:                  # noqa: N802
        self._detach_sweep_filter()
        super().hideEvent(event)

    def is_sweeping(self) -> bool:
        """正在按住左键划选吗（拖动多选的手势里）。"""
        return bool(self._sweep and self._sweep.get("dragging"))

    def eventFilter(self, obj, event) -> bool:           # noqa: N802
        """顶层过滤器：只观察消息区里的左键按下 / 移动 / 松开，**从不吞事件**。

        为什么非得在顶层装：按下可能落在文字标签、附件缩略图、引用块这些子控件上，
        它们会各自处理（选中文字、点开卡片），消息列表这一层根本收不到；而"划选"又必须
        看得到从按下到松开的每一步。装在顶层 + 只认自己视口里的目标，就既能全看到，
        又不改任何既有行为（事件返回 False，照常往下发）。
        """
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self._sweep_press(obj, event)
        elif kind == QEvent.Type.MouseMove:
            self._sweep_move(event)
        elif kind == QEvent.Type.MouseButtonRelease:
            self._sweep_release(event)
        return False

    def _sweep_target(self, obj) -> bool:
        """这个事件的目标在自己这片消息区里吗。"""
        return (obj is self.viewport()
                or (isinstance(obj, QWidget) and self.viewport().isAncestorOf(obj)))

    def _sweep_press(self, obj, event) -> None:
        """左键按下：记下起点和"要刷成什么状态"（从没选中的那条划起=一路选上）。"""
        if (self._sweep is not None or not self._sweep_target(obj)
                or event.button() != Qt.MouseButton.LeftButton):
            return
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            return                      # Alt + 拖 = 老规矩：用鼠标选中文字
        bubble = self._bubble_at(obj)
        self._sweep = {
            "start": event.globalPosition().toPoint(),
            "dragging": False,
            "label": obj if self._text_selectable(obj) else None,
            "anchor": bubble.message_id if bubble is not None else "",
        }
        self._sweep_paint = True if bubble is None else not bubble.is_selected()
        self._swept.clear()

    def _sweep_move(self, event) -> None:
        gesture = self._sweep
        if gesture is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        now = event.globalPosition().toPoint()
        if not gesture["dragging"]:
            if (now - gesture["start"]).manhattanLength() < SWEEP_SLOP:
                return
            gesture["dragging"] = True
            if not self._selection_mode:
                self.set_selection_mode(True)      # 划一下就等于进了多选模式
        self._drop_text_selection(gesture)
        self._sweep_over(gesture, now)

    def _sweep_release(self, event) -> None:
        gesture = self._sweep
        if gesture is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self._sweep = None
        self._drop_text_selection(gesture)
        if not gesture["dragging"]:
            return                 # 原地点一下：还是交给气泡 / 圆勾自己那条路
        if self._selection_mode and not self._selected:
            self.set_selection_mode(False)     # 划了半天一条没划到：别留下一个空的多选模式

    def _sweep_over(self, gesture, now: QPoint) -> None:
        """把"从按下处划到这里"扫过的气泡刷成同一个状态（一条只刷一次，划回去不翻回来）。"""
        viewport = self.viewport()
        changed = False
        # 按下的那一条**永远算划中**：进多选模式这一下会把布局顶一下（批量条出现、
        # 气泡右移 28px、消息区矮 48px），按下的那个点很可能已经不在"划过的矩形"里了。
        anchor = gesture.get("anchor")
        if anchor and anchor not in self._swept:
            self._swept.add(anchor)
            changed = self.select_message(anchor, self._sweep_paint)
        rect = QRect(viewport.mapFromGlobal(gesture["start"]),
                     viewport.mapFromGlobal(now)).normalized()
        rect = rect.intersected(viewport.rect())        # 划出消息区的部分不算数
        if rect.isEmpty():
            if changed:
                self._emit_selection()
            return
        for message_id, bubble in self._bubbles.items():
            if message_id in self._swept:
                continue
            top_left = bubble.mapTo(viewport, QPoint(0, 0))
            if not rect.intersects(QRect(top_left, bubble.size())):
                continue
            self._swept.add(message_id)
            changed = self.select_message(message_id, self._sweep_paint) or changed
        if changed:
            self._emit_selection()

    def _bubble_at(self, widget) -> MessageBubble | None:
        """落点那个控件属于哪条气泡（空白处没有；圆勾挂在行上，所以还要往行里找一下）。"""
        node = widget if isinstance(widget, QWidget) else None
        while node is not None and node is not self.viewport():
            if isinstance(node, MessageBubble):
                return node
            for child in node.findChildren(
                    MessageBubble, options=Qt.FindChildOption.FindDirectChildrenOnly):
                return child
            node = node.parentWidget()
        return None

    @staticmethod
    def _text_selectable(widget) -> bool:
        """这是个"能用鼠标选文字"的标签吗（那种拖动本来是留给它选字的）。"""
        if not isinstance(widget, QLabel):
            return False
        return bool(widget.textInteractionFlags()
                    & Qt.TextInteractionFlag.TextSelectableByMouse)

    @staticmethod
    def _drop_text_selection(gesture) -> None:
        """划选期间把"顺手选中的文字"收干净 —— 划选是选消息，不是选文字。"""
        label = gesture.get("label")
        if label is None:
            return
        try:
            if label.hasSelectedText():
                label.setSelection(0, 0)
        except RuntimeError:                 # 控件可能已经随消息删掉了
            pass

    # ------------------------------------------------------------------ 内部
    def _add_row(self, widget: QWidget, *, center: bool,
                 outgoing: bool = False, check: QWidget | None = None,
                 ) -> None:
        row = QWidget(self._host)
        row.setObjectName("bubbleRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 8, 0)
        line.setSpacing(6)
        if center:
            line.addStretch(1)
            line.addWidget(widget)
            line.addStretch(1)
        elif outgoing:
            line.addStretch(1)
            line.addWidget(widget)
            if check is not None:            # 圆勾放在气泡外侧（和 Telegram 一样）
                line.addWidget(check, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            if check is not None:
                line.addWidget(check, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(widget)
            line.addStretch(1)
        row.show()                       # 运行时新建的控件必须显式显示
        self._layout.addWidget(row)

    def _autoscroll(self) -> None:
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def is_at_bottom(self, slack: int = 24) -> bool:
        """滚动条是不是已经贴着底（留一点余量，滚轮抖一下也算贴底）。"""
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - slack

    def scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        QTimer.singleShot(30, lambda: bar.setValue(bar.maximum()))

    def follow_bottom(self, ms: int = 600) -> None:
        """把视图"钉"在底部 ``ms`` 毫秒 —— 期间滚动范围一变就重新贴回底部。

        为什么光滚一次不够：入场动画那 260ms 里气泡高度是 0，动画结束恢复高度后，
        父布局还要再重算一两次，滚动范围就跟着变大两次；中间只要没跟上，最新那条
        就只露出大半（实测差 80px，正好是最后一条气泡的高度）。

        跟随期间用户自己往上滚了（离底超过 ``FOLLOW_SLACK``）就自动放弃跟随，
        不会把人从历史里硬拽回来。
        """
        bar = self.verticalScrollBar()
        state = {"max": bar.maximum()}
        bar.setValue(state["max"])

        def on_range(_minimum: int, maximum: int) -> None:
            still_pinned = bar.value() >= state["max"] - self.FOLLOW_SLACK
            state["max"] = maximum
            if still_pinned:
                bar.setValue(maximum)

        bar.rangeChanged.connect(on_range)

        def release() -> None:
            try:
                bar.rangeChanged.disconnect(on_range)
            except TypeError:                    # pragma: no cover - 已经断开
                pass

        QTimer.singleShot(ms, release)


class ChatView(QWidget):
    """聊天面板：头部（头像/名字/状态/搜索）+ 消息区 + 输入栏。"""

    messageSubmitted = pyqtSignal(str)
    searchRequested = pyqtSignal()           # 聊天头部的放大镜：搜这个会话
    emojiUsed = pyqtSignal(str)              # 用过某个表情（主窗口拿去存"最近"）
    reactionPicked = pyqtSignal(str, str)    # (message_id, emoji) 点回应
    messageDeleted = pyqtSignal(str)         # 右键菜单里选了"删除这条消息"
    reactionModeChanged = pyqtSignal(bool)   # 进入/退出"选表情当回应"模式
    attachRequested = pyqtSignal(str)        # 想选图片/文件（"image" / "file"）
    attachmentRequested = pyqtSignal(str, str)   # (message_id, "open" / "folder")
    imagePasted = pyqtSignal(object)         # 输入框里粘进来一张图（QImage）
    replyRequested = pyqtSignal(str)         # 右键菜单"回复"
    copyRequested = pyqtSignal(str)          # 右键菜单"复制"
    pinRequested = pyqtSignal(str)           # 右键菜单「置顶/取消置顶」
    unpinRequested = pyqtSignal(str)         # 点了置顶横幅上的 ✕（带 message_id）
    forwardRequested = pyqtSignal(object)    # 要转发这些消息（list[str]）
    deleteRequested = pyqtSignal(object)     # 要批量删除这些消息（list[str]）
    quoteTapped = pyqtSignal(str)            # 点了引用块（主窗口负责跳过去）
    selectionModeChanged = pyqtSignal(bool)  # 进出批量选择模式
    replyCleared = pyqtSignal()              # 用户点了回复条上的 ✕
    mergedOpened = pyqtSignal(str)           # 点了合并转发卡片：开"聊天记录"窗口

    def __init__(self, store: ChatStore, parent=None, *, emoji_recents=()):
        super().__init__(parent)
        self._store = store
        self._conversation: Conversation | None = None
        self._typing = False
        self._emoji_recents = list(emoji_recents)
        self._palette = None
        self._reaction_target: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        self.pin_banner = PinBanner(self)          # 置顶消息横幅：跟着头部
        root.addWidget(self.pin_banner)

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
        # "正在输入"的三个跳动点：紧跟状态文字，只在打字时出现
        self.typing_dots = anim.TypingDots(header)
        self.typing_dots.hide()

        status_row = QHBoxLayout()
        status_row.setSpacing(5)
        status_row.addWidget(self.status_label)
        status_row.addWidget(self.typing_dots)
        status_row.addStretch(1)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        text_col.addWidget(self.name_label)
        text_col.addLayout(status_row)

        # 单个会话的搜索入口：就在聊天头部，点开只搜这个会话（Ctrl+Shift+F 同效）
        self.search_btn = IconButton("search", header, tooltip="搜索这个会话的消息")
        self.search_btn.clicked.connect(self.searchRequested)

        row = QHBoxLayout(header)
        row.setContentsMargins(16, 8, 14, 8)
        row.setSpacing(10)
        row.addWidget(self.header_avatar)
        row.addLayout(text_col)
        row.addStretch(1)
        row.addWidget(self.search_btn)
        return header

    def _build_empty_page(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("emptyPage")
        icon = QLabel(page)
        icon.setPixmap(make_icon("smile", "#9AA7B4", 64).pixmap(QSize(64, 64)))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("还没有说话的对象", page)
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("点左栏的「＋」搜人开聊、建个群；对方先给你发消息，会话也会自己出现",
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
        # 点消息区空白处收面板：过滤器装在视口上，就只对"消息区里的点击"生效
        self.message_list.viewport().installEventFilter(self)
        self.message_list.menuRequested.connect(self._show_bubble_menu)
        self.message_list.reactionToggled.connect(self.reactionPicked)
        self.message_list.attachmentRequested.connect(self.attachmentRequested)
        self.message_list.quoteTapped.connect(self.quoteTapped)
        self.message_list.mergedOpened.connect(self.mergedOpened)
        self.message_list.selectionModeChanged.connect(self._on_selection_mode)
        self.message_list.selectionChanged.connect(self._on_selection_changed)
        self.composer = Composer(page)
        self.composer.messageSubmitted.connect(self.messageSubmitted)
        self.composer.attachRequested.connect(self.attachRequested)
        self.composer.imagePasted.connect(self.imagePasted)
        # 待发送条上的 ✕：只清附件，不动输入框里的字
        self.composer.attachmentRemoved.connect(self._on_attachment_removed)
        self.composer.replyRemoved.connect(self.replyCleared)   # 回复条的 ✕ 直接转给主窗口
        self.pin_banner.jumped.connect(self._on_pin_tapped)
        self.pin_banner.cycleRequested.connect(self._on_pin_cycle)
        self.pin_banner.unpinRequested.connect(self._on_pin_unpin)
        # 表情面板夹在消息区和输入栏之间：展开时把消息区顶上去（Telegram 的做法），
        # 而不是浮在上层遮住消息。
        self.emoji_picker = EmojiPicker(page, recents=self._emoji_recents)
        self.emoji_picker.emojiChosen.connect(self._on_emoji_chosen)
        self.composer.emojiPanelToggled.connect(self.emoji_picker.set_open)
        # 批量选择条：夹在消息区/表情面板和输入栏之间
        self.select_bar = SelectionBar(page)
        self.select_bar.forwardRequested.connect(self._request_forward)
        self.select_bar.deleteRequested.connect(self._request_delete)
        self.select_bar.cancelRequested.connect(self.end_selection)

        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self.message_list, 1)
        box.addWidget(self.emoji_picker)
        box.addWidget(self.select_bar)
        box.addWidget(self.composer)
        return page

    # ================================================================== 对外
    @property
    def conversation(self) -> Conversation | None:
        return self._conversation

    def show_conversation(self, conversation: Conversation) -> None:
        self._conversation = conversation
        self._typing = False
        # 切会话时把"批量选择 / 待回复 / 置顶横幅"都收干净；主窗口随后会按新会话
        # 把待回复和置顶再挂回来（set_reply_target / set_pinned，不触发信号，不会串台）
        self.end_selection()
        self.set_reply_target(None)
        self.set_pinned([])
        self.header_avatar.set_name(conversation.title)
        self.name_label.setText(conversation.title)
        others = [m for m in conversation.members if not m.is_me]
        self.header_avatar.set_online(bool(others and others[0].online))
        self.status_label.setText(conversation.subtitle())
        self.status_label.setProperty("typing", "false")
        self.typing_dots.hide()
        self.typing_dots.stop()
        self.message_list.set_conversation(conversation)
        self.stack.setCurrentIndex(1)
        self.header_avatar.show()            # 空态时收起来的头像，进来会话要放出来
        self.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_empty(self) -> None:
        """没有任何会话时的样子：收掉头像、把标题换成应用名（别留一个红色的"?"）。"""
        self._conversation = None
        self.stack.setCurrentIndex(0)
        self.header_avatar.hide()
        self.name_label.setText("MChat")
        self.status_label.setText("还没有会话")
        self.status_label.setProperty("typing", "false")

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

    def refresh_status(self) -> None:
        """重画头部第二行（在线状态变化时用：成员的 online 变了，这行要跟着变）。"""
        conversation = self._conversation
        if conversation is None:
            return
        if self._typing:
            return                                  # 正在输入…优先，别把它盖掉
        self.status_label.setText(conversation.subtitle())

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
            # 打字时省掉省略号：那三个点就承担了"…"的职责
            text = conversation.subtitle(typing)
            self.status_label.setText(text[:-1] if typing and text.endswith("…") else text)
        self.typing_dots.setVisible(typing)              # 定时器跟着显隐走，隐藏即停
        if typing:
            self.typing_dots.start()
        else:
            self.typing_dots.stop()

    def focus_input(self) -> None:
        if self.stack.currentIndex() == 1:
            self.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def input_text(self) -> str:
        return self.composer.text()

    def sent_bubbles(self) -> int:
        return self.message_list.bubble_count()

    # ------------------------------------------------------------ 表情面板
    def emoji_recents(self) -> list[str]:
        return self.emoji_picker.recents()

    def emoji_panel_open(self) -> bool:
        return self.emoji_picker.is_open()

    def set_emoji_panel_open(self, open_: bool, *, animate: bool = True) -> None:
        """按钮状态是唯一真源：改按钮会经 ``toggled`` 带动面板。"""
        was_at_bottom = self.message_list.is_at_bottom()
        self.composer.smile_btn.setChecked(open_)
        if open_ != self.emoji_picker.is_open():      # 状态已一致时上面那句不发信号
            self.emoji_picker.set_open(open_, animate=animate)
        if not open_:
            self.end_reaction()                       # 关掉面板就退出回应模式
        if was_at_bottom:
            # 面板展开/收起会改变消息区高度，原来贴底的位置就"错位"了；
            # 等面板铺开（或收完）之后把最新消息滚回视野。本来没贴底就别动人家。
            QTimer.singleShot(self.emoji_picker.ANIM_MS + 40,
                              lambda: self.message_list.follow_bottom(360))

    def eventFilter(self, obj, event) -> bool:        # noqa: N802
        """点消息区空白处 = 收起表情面板（工具箱那边是点空白收焦点，这里同理）。

        过滤器只装在消息区的视口上：面板自己是别的控件，点它不会走这里；
        而且**不吞掉这次点击**（返回 False），该选文字、该点气泡照常。
        """
        if (event.type() == QEvent.Type.MouseButtonPress
                and obj is self.message_list.viewport()
                and self.emoji_picker.is_open()):
            self.set_emoji_panel_open(False)
        return super().eventFilter(obj, event)

    def hide_emoji_panel(self) -> bool:
        """收起面板，返回"原本是否开着"（Esc 之类的地方好判断要不要继续处理）。"""
        if not self.emoji_picker.is_open():
            return False
        self.set_emoji_panel_open(False)
        return True

    def toggle_emoji_panel(self) -> bool:
        opened = not self.emoji_picker.is_open()
        self.set_emoji_panel_open(opened)
        return opened

    def _on_emoji_chosen(self, char: str) -> None:
        if self._reaction_target:               # 回应模式：加到那条消息上，不改输入框
            self.reactionPicked.emit(self._reaction_target, char)
        else:
            self.composer.edit.insert_text(char)
        self.emojiUsed.emit(char)

    # ------------------------------------------------------------ 回应 / 撤回
    def set_reactions(self, counts: dict[str, dict[str, int]],
                      mine: dict[str, set[str]]) -> None:
        """把库里读到的回应贴到气泡上（主窗口在切会话后调一次）。"""
        for message_id, bubble in self.message_list.bubbles().items():
            bubble.set_reactions(counts.get(message_id, {}), mine.get(message_id, set()))

    def reaction_mode_target(self) -> str | None:
        return self._reaction_target

    def begin_reaction(self, message_id: str) -> None:
        """进入回应模式：接着点面板里的任意表情，都会加到这条消息上。"""
        self._reaction_target = message_id
        self.set_emoji_panel_open(True)
        self.reactionModeChanged.emit(True)

    def end_reaction(self) -> None:
        if self._reaction_target is None:
            return
        self._reaction_target = None
        self.reactionModeChanged.emit(False)

    def remove_message(self, message_id: str) -> bool:
        return self.message_list.remove_message(message_id)

    def flash_message(self, message_id: str) -> bool:
        """滚到某条消息并闪一下（全局搜索跳转用）。"""
        return self.message_list.focus_message(message_id)

    # ================================================================== 回复 / 置顶 / 批量选择
    def set_reply_target(self, reply) -> None:
        """把"待回复"的引用挂到输入栏上方（主窗口按会话存，切回来还在）。"""
        self.composer.set_reply(reply)

    def reply_target(self):
        return self.composer.reply()

    def clear_reply(self) -> None:
        self.composer.clear_reply()

    def set_pinned(self, pins) -> None:
        """把整串置顶交给横幅（``[]`` 就收起）；``[(message, 作者), ...]``。"""
        self.pin_banner.set_pins(pins)

    def pinned_message_id(self) -> str:
        return self.pin_banner.message_id()

    def is_pinned(self, message_id: str) -> bool:
        return self._pin_ref(message_id) is not None

    def _pin_ref(self, message_id: str):
        """这条消息在当前会话里的置顶信息（PinRef / None）。"""
        conversation = self._conversation
        if conversation is None:
            return None
        return next((pin for pin in conversation.pins if pin.message_id == message_id), None)

    def _on_pin_tapped(self) -> None:
        """点置顶横幅：复用"跳到某条消息"这条路（主窗口统一处理跨会话/加载历史）。"""
        message_id = self.pin_banner.message_id()
        if message_id:
            self.quoteTapped.emit(message_id)

    def _on_pin_cycle(self, step: int) -> None:
        self.pin_banner.cycle(step)

    def _on_pin_unpin(self) -> None:
        message_id = self.pin_banner.message_id()
        if message_id:
            self.unpinRequested.emit(message_id)

    # ---- 批量选择 ----
    def begin_selection(self, message_id: str | None = None) -> None:
        """进入批量选择模式；给了 ``message_id`` 就顺手把这一条也勾上。"""
        self.message_list.set_selection_mode(True)
        if message_id:
            self.message_list.toggle_selection(message_id)

    def end_selection(self) -> None:
        self.message_list.set_selection_mode(False)

    def selection_mode(self) -> bool:
        return self.message_list.selection_mode()

    def selected_ids(self) -> list[str]:
        return self.message_list.selected_ids()

    def selected_count(self) -> int:
        return self.message_list.selected_count()

    def _on_selection_mode(self, active: bool) -> None:
        if active:
            # 表情面板和批量条会挤在一起，进选择模式先把它收掉（按钮状态也跟着回弹）
            self.emoji_picker.set_open(False)
            self.composer.set_smile_checked(False)
        self.select_bar.setVisible(active)
        if active:
            anim.fade_in(self.select_bar, ms=anim.FAST_MS)
        self.selectionModeChanged.emit(active)

    def _on_selection_changed(self, count: int) -> None:
        self.select_bar.set_count(count)

    def _request_forward(self) -> None:
        ids = self.message_list.selected_ids()
        if ids:
            self.forwardRequested.emit(ids)

    def _request_delete(self) -> None:
        ids = self.message_list.selected_ids()
        if ids:
            self.deleteRequested.emit(ids)

    def _show_bubble_menu(self, message_id: str, global_pos) -> None:
        """气泡右键菜单：回应一排 + 回复/复制/置顶/转发/选择 + 附件操作 + 删除。"""
        menu = self._build_bubble_menu(message_id)
        if menu is not None:
            menu.exec(global_pos)

    def _build_bubble_menu(self, message_id: str) -> QMenu | None:
        """构造气泡菜单（拆出来是为了测试能点名动作，不用真的弹出来）。

        * 选择模式下**不弹菜单**：那时点/右键气泡都是"勾选/取消"。
        * "删除"照旧只给自己的消息（别人的消息 Telegram 里也只是"删除会话内副本"，
          这里不做，免得误删别人的话）。
        """
        bubble = self.message_list.bubbles().get(message_id)
        if bubble is None or self.selection_mode():
            return None
        menu = QMenu(self)
        if self._palette is not None:
            menu.setStyleSheet(menu_qss(self._palette))
        for emoji in QUICK_REACTIONS:
            menu.addAction(f"{emoji}　回应", lambda _=False, e=emoji:
                           self.reactionPicked.emit(message_id, e))
        menu.addAction("更多表情…", lambda: self.begin_reaction(message_id))
        menu.addSeparator()
        menu.addAction("回复", lambda: self.replyRequested.emit(message_id))
        menu.addAction("复制", lambda: self.copyRequested.emit(message_id))
        pin = self._pin_ref(message_id)
        if pin is not None:
            # 取消置顶不问（Telegram 也一样），直接摘掉这一条
            menu.addAction(f"取消置顶（{pin.label}）",
                           lambda: self.pinRequested.emit(message_id))
        else:
            # 点「置顶」→ 主窗口弹一个小窗问"要不要也给对方置顶"
            menu.addAction("置顶", lambda: self.pinRequested.emit(message_id))
        menu.addAction("转发…", lambda: self.forwardRequested.emit([message_id]))
        menu.addAction("选择", lambda: self.begin_selection(message_id))
        if bubble.has_attachment():
            menu.addSeparator()
            attachment = bubble.attachment
            name = attachment.name if attachment else "附件"
            if attachment is not None and attachment.kind == "image":
                # 图片给两条路：**在应用里看大图**（点缩略图就是这个，QQ 那种）和
                # **交给系统看图器**。还在服务器上的图也能先点（会先下载、下完自动打开）。
                view = menu.addAction("查看图片",
                                      lambda: self.attachmentRequested.emit(message_id, "view"))
                view.setEnabled(bool(attachment.exists or attachment.is_remote))
            opened = menu.addAction(f"打开「{name}」",
                                    lambda: self.attachmentRequested.emit(message_id, "open"))
            opened.setEnabled(bool(bubble.attachment and bubble.attachment.exists))
            menu.addAction("打开所在文件夹",
                           lambda: self.attachmentRequested.emit(message_id, "folder"))
        # 删除是**本地删除**（不是撤回）：别人的消息一样能删，对方设备上仍然看得到。
        # 因为语义上容易误会成"撤回"，这里用 tooltip 和确认框把话说明白。
        menu.addSeparator()
        remove = menu.addAction("⚠ 删除这条消息", lambda: self.messageDeleted.emit(message_id))
        remove.setToolTip("只删本机这条记录，对方设备上仍然看得到（不是撤回）")
        return menu

    def set_input_text(self, text: str) -> None:
        """整段替换输入框内容（草稿恢复用）。"""
        self.composer.edit.set_message_text(text)

    def set_smile_checked(self, checked: bool) -> None:
        self.composer.set_smile_checked(checked)

    # ------------------------------------------------------------ 附件
    def set_attachment(self, attachment: Attachment | None) -> None:
        """挂上待发送的附件（选文件、粘剪贴板图片最后都汇到这里）。"""
        self.composer.set_attachment(attachment)

    def pending_attachment(self) -> Attachment | None:
        """当前待发送的附件（发送前由主窗口取一次）。"""
        return self.composer.attachment()

    def has_pending_attachment(self) -> bool:
        return self.composer.has_attachment()

    def clear_attachment(self) -> None:
        self.composer.clear_attachment()

    def attachment_tray(self) -> AttachmentTray:
        return self.composer.tray

    def _on_attachment_removed(self) -> None:
        """✕ 移除附件后把焦点还给输入框，接着打字不用再点一次。"""
        self.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.search_btn.set_icon_color(palette.muted)
        self.typing_dots.set_color(palette.muted)
        self.header_avatar.apply_palette(palette)
        self.composer.apply_palette(palette)
        self.emoji_picker.apply_palette(palette)
        self.message_list.apply_palette(palette)
