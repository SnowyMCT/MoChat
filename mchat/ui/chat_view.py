"""右侧聊天区：消息气泡、日期分隔条、输入栏，以及"没选会话"的空态。"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPalette, QTextCharFormat
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea,
                             QSizePolicy, QStackedWidget, QTextEdit,
                             QToolButton, QVBoxLayout, QWidget)

from ..core.models import Conversation, Message, day_label_key, format_day_label
from ..core.store import ChatStore
from .avatar import Avatar
from . import anim
from .emoji_panel import EmojiPicker
from . import flag_text
from . import flags as flag_data
from .icons import IconButton, make_icon
from .theme import menu_qss

__all__ = ["ChatView", "MessageList", "MessageBubble", "Composer",
           "ReactionChip", "QUICK_REACTIONS"]

MAX_BUBBLE_WIDTH = 520
MIN_INPUT_LINES = 1
MAX_INPUT_LINES = 5
QUICK_REACTIONS = ("👍", "❤️", "😂", "🎉", "😮", "😢")   # 右键菜单里那一排
FLASH_MS = 1400                                          # 跳到某条消息时高亮多久


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
    """一条消息的气泡：左侧（收到的）白/深底，右侧（发出的）主题浅色底。"""

    menuRequested = pyqtSignal(str, object)     # (message_id, 全局坐标)
    reactionToggled = pyqtSignal(str, str)      # (message_id, emoji)

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

        self.setMaximumWidth(MAX_BUBBLE_WIDTH)
        self.set_meta(message)

    def set_meta(self, message: Message) -> None:
        """只刷新时间与已读标记（消息内容不变，不重建控件）。"""
        self.time_label.setText(message.time_text)
        if self.outgoing:
            self.read_label.setText("✓✓" if message.is_read else "✓")

    def text(self) -> str:
        """消息原文（富文本时 ``text_label.text()`` 是 HTML，所以这里存了原串）。"""
        return self._plain_text

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
    """

    submitted = pyqtSignal()

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
        """粘贴走同一条路：粘进来的旗帜也应该是图片。"""
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


class Composer(QWidget):
    """底部输入栏：表情、输入框、发送按钮。"""

    messageSubmitted = pyqtSignal(str)
    emojiPanelToggled = pyqtSignal(bool)     # 表情按钮自己只管状态，开哪个面板由聊天区决定

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.smile_btn = IconButton("smile", self, size=20, box=36,
                                    tooltip="表情（Esc 关闭）")
        self.smile_btn.setCheckable(True)
        self.smile_btn.toggled.connect(self.emojiPanelToggled)
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
        text = self.edit.message_text().strip()
        if text:
            self.edit.clear()
        self.messageSubmitted.emit(text)

    def text(self) -> str:
        return self.edit.message_text()

    def apply_palette(self, palette) -> None:
        self.smile_btn.set_icon_color(palette.muted)
        self.send_btn.set_icon_color(palette.accent_on)
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


class MessageList(QScrollArea):
    """消息区：按日期插分隔条，新消息追加后自动滚到底。"""

    FOLLOW_SLACK = 24            # 贴底判定的容差（滚轮抖一下也算贴底）

    menuRequested = pyqtSignal(str, object)
    reactionToggled = pyqtSignal(str, str)

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
                anim.cancel(widget)      # 先停动画：删除是 deleteLater，回调可能才刚起来
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
                               show_sender=self._group)
        bubble.menuRequested.connect(self.menuRequested)
        bubble.reactionToggled.connect(self.reactionToggled)
        self._add_row(bubble, center=False, outgoing=message.outgoing)
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

    def remove_message(self, message_id: str) -> bool:
        """从列表里拿掉一条消息（撤回）：气泡和它所在的那一行一起撤。"""
        bubble = self._bubbles.pop(message_id, None)
        if bubble is None:
            return False
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

    def focus_message(self, message_id: str, *, flash: bool = True) -> bool:
        """滚到某条消息并闪一下（搜索结果跳转用）。"""
        bubble = self._bubbles.get(message_id)
        if bubble is None:
            return False
        self.ensureWidgetVisible(bubble, 0, 80)
        if flash:
            bubble.flash()
        return True

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
    """聊天面板：头部（头像/名字/状态/更多）+ 消息区 + 输入栏。"""

    messageSubmitted = pyqtSignal(str)
    moreRequested = pyqtSignal()
    searchRequested = pyqtSignal()           # 聊天头部的放大镜：搜这个会话
    emojiUsed = pyqtSignal(str)              # 用过某个表情（主窗口拿去存"最近"）
    reactionPicked = pyqtSignal(str, str)    # (message_id, emoji) 点回应
    messageDeleted = pyqtSignal(str)         # 右键菜单里选了"删除这条消息"
    reactionModeChanged = pyqtSignal(bool)   # 进入/退出"选表情当回应"模式

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

        self.more_btn = IconButton("more", header, tooltip="更多操作")
        self.more_btn.clicked.connect(self.moreRequested)
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
        # 点消息区空白处收面板：过滤器装在视口上，就只对"消息区里的点击"生效
        self.message_list.viewport().installEventFilter(self)
        self.message_list.menuRequested.connect(self._show_bubble_menu)
        self.message_list.reactionToggled.connect(self.reactionPicked)
        self.composer = Composer(page)
        self.composer.messageSubmitted.connect(self.messageSubmitted)
        # 表情面板夹在消息区和输入栏之间：展开时把消息区顶上去（Telegram 的做法），
        # 而不是浮在上层遮住消息。
        self.emoji_picker = EmojiPicker(page, recents=self._emoji_recents)
        self.emoji_picker.emojiChosen.connect(self._on_emoji_chosen)
        self.composer.emojiPanelToggled.connect(self.emoji_picker.set_open)

        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self.message_list, 1)
        box.addWidget(self.emoji_picker)
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
        self.typing_dots.hide()
        self.typing_dots.stop()
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

    def _show_bubble_menu(self, message_id: str, global_pos) -> None:
        """气泡右键菜单：一排常用回应 + 更多表情 + 删除（只对自己的消息）。"""
        bubble = self.message_list.bubbles().get(message_id)
        if bubble is None:
            return
        menu = QMenu(self)
        if self._palette is not None:
            menu.setStyleSheet(menu_qss(self._palette))
        for emoji in QUICK_REACTIONS:
            menu.addAction(f"{emoji}　回应", lambda _=False, e=emoji:
                           self.reactionPicked.emit(message_id, e))
        menu.addAction("更多表情…", lambda: self.begin_reaction(message_id))
        if bubble.outgoing:
            menu.addSeparator()
            menu.addAction("⚠ 删除这条消息", lambda: self.messageDeleted.emit(message_id))
        menu.exec(global_pos)

    def set_input_text(self, text: str) -> None:
        """整段替换输入框内容（草稿恢复用）。"""
        self.composer.edit.set_message_text(text)

    def set_smile_checked(self, checked: bool) -> None:
        self.composer.set_smile_checked(checked)

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.more_btn.set_icon_color(palette.muted)
        self.search_btn.set_icon_color(palette.muted)
        self.typing_dots.set_color(palette.muted)
        self.header_avatar.apply_palette(palette)
        self.composer.apply_palette(palette)
        self.emoji_picker.apply_palette(palette)
