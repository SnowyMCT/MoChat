"""Telegram 风格的表情面板：分类图标条 + 表情网格 + 搜索 + 最近使用。

表情全在本地（见 :mod:`mchat.ui.emoji`），不需要联网：文字类表情用系统彩色字体画，
旗帜用本地精灵图切图（见 :mod:`mchat.ui.flags`）—— Windows 的字体不画国旗。

结构（自上而下，和 Telegram 一样）
----------------------------------
    搜索框  —— 输入"笑 / cat / ❤️"都能搜
    网格    —— 可滚动，9 列；当前分类或搜索结果
    分类条  —— 最近 / 表情 / 动物自然 / 食物饮品 / 活动运动 / 旅行地点 / 物品 / 符号

职责边界
--------
* 本组件只发信号：``emojiChosen(str)``。"插到输入框哪个位置"由聊天区决定。
* 用过的表情进"最近"（最多 24 个，新的排前面），列表由本组件维护，
  **持久化交给外部**（主窗口往 QSettings 写），这样组件不碰配置。
* 展开/收起用自绘的 ``maximumHeight`` 动画，不挤占消息区的"存在感"
  （面板由聊天区布局托管，展开时消息区自然被顶上去）。

交互细节
--------
* 表情格子是 ``FocusPolicy.NoFocus`` 的 QToolButton —— 点它不会把焦点从
  输入框/搜索框抢走，可以连着插好几个表情。
* 搜索框有关键词时显示搜索结果，清空后回到原来的分类。
"""
from __future__ import annotations

from PyQt6.QtCore import (QEasingCurve, QPropertyAnimation, QSize, Qt,
                          pyqtSignal)
from PyQt6.QtGui import QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QScrollArea, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

from . import emoji as emoji_data
from . import flags as flag_data
from .widgets import FloatingLabelInput

__all__ = ["EmojiPicker", "EMOJI_FONT_FAMILIES", "emoji_font", "flag_icon"]

# Windows 上是 Segoe UI Emoji（彩色），其余是给跨平台的兜底
EMOJI_FONT_FAMILIES = ("Segoe UI Emoji", "Apple Color Emoji",
                       "Noto Color Emoji", "Segoe UI Symbol")


def emoji_font(size: float) -> QFont:
    """表情专用字体：能列一族就列一族，别让中文正文字体把表情顶成线条图。"""
    font = QFont()
    font.setFamilies(list(EMOJI_FONT_FAMILIES))
    font.setPointSizeF(size)
    return font


_SPRITE: QPixmap | None = None
_SPRITE_LOADED = False


def _sprite() -> QPixmap | None:
    """懒加载旗帜精灵图（整个进程只读一次盘；素材缺失时返回 None，面板降级画字符）。"""
    global _SPRITE, _SPRITE_LOADED
    if not _SPRITE_LOADED:
        _SPRITE_LOADED = True
        if flag_data.sprite_exists():
            _SPRITE = QPixmap(flag_data.sprite_path())
    return _SPRITE


def flag_icon(char: str) -> QIcon | None:
    """从精灵图里切出这面旗的图标；拿不到就返回 None。"""
    sheet = _sprite()
    index = flag_data.INDEX.get(char)
    if sheet is None or index is None:
        return None
    x, y, w, h = flag_data.cell_rect(index)
    return QIcon(sheet.copy(x, y, w, h))


class EmojiPicker(QWidget):
    """表情面板本体。"""

    emojiChosen = pyqtSignal(str)

    COLUMNS = 9                 # 默认每行几个表情（实际按面板宽度自动排满）
    MIN_COLUMNS = 6
    MAX_COLUMNS = 24
    CELL = 34                   # 格子边长
    PANEL_HEIGHT = 292          # 展开后的高度
    MAX_RECENT = 24
    ANIM_MS = 170

    def __init__(self, parent: QWidget | None = None, *, recents=(),
                 max_recent: int = MAX_RECENT):
        super().__init__(parent)
        self.setObjectName("emojiPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._ready = False
        self._recents: list[str] = [c for c in recents if c in emoji_data.ALL_EMOJI]
        self._max_recent = max_recent
        self._category = emoji_data.RECENT
        self._keyword = ""
        self._columns = self.COLUMNS
        self._cells: list[QToolButton] = []
        self._cat_buttons: dict[str, QToolButton] = {}
        self._open = False
        self._palette = None

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMaximumHeight(0)          # 初始收起（高度动画的起点）
        self.setMinimumHeight(0)
        self.setVisible(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(7)
        root.addWidget(self._build_search())
        root.addWidget(self._build_grid(), 1)
        root.addLayout(self._build_category_row())

        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._ready = True
        self._refresh()

    # ==================================================================== 搭建
    def _build_search(self) -> QWidget:
        self.search_field = FloatingLabelInput(
            "搜索表情（中文或英文，例如 笑 / cat）", field_height=34, radius=10)
        self.search_field.textChanged.connect(self._on_search)
        return self.search_field

    def _build_grid(self) -> QScrollArea:
        self._grid_host = QWidget()
        self._grid_host.setObjectName("emojiGrid")
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self._hint = QLabel("", self._grid_host)
        self._hint.setObjectName("emojiHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.hide()

        area = QScrollArea(self)
        area.setObjectName("emojiArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self._grid_host)
        area.viewport().setAutoFillBackground(False)
        self._grid_host.setAutoFillBackground(False)
        self._area = area
        return area

    def _build_category_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(2)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        for key, label, icon in emoji_data.CATEGORIES:
            btn = QToolButton(self)
            btn.setObjectName("emojiCatBtn")
            btn.setText(icon)
            btn.setFont(emoji_font(13))
            btn.setToolTip(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)          # 同一父控件内互斥，等于单选
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedSize(30, 26)
            btn.setProperty("categoryKey", key)
            btn.clicked.connect(lambda _=False, k=key: self.select_category(k))
            btn.show()                          # 父控件已显示时后建的子控件不会自动显示
            self._cat_buttons[key] = btn
            row.addWidget(btn)
        row.addStretch(1)
        return row

    # ==================================================================== 数据
    def categories(self) -> list[str]:
        return [key for key, _label, _icon in emoji_data.CATEGORIES]

    def current_category(self) -> str:
        """当前分类（搜索时仍然是选中的那个分类）。"""
        return self._category

    def search_text(self) -> str:
        return self.search_field.text()

    def visible_chars(self) -> list[str]:
        """网格里当前真的摆着的表情（测试与调试用，不依赖控件内部状态）。"""
        return [btn.property("emojiChar") for btn in self._cells]

    def cell_at(self, char: str) -> QToolButton | None:
        for btn in self._cells:
            if btn.property("emojiChar") == char:
                return btn
        return None

    def recents(self) -> list[str]:
        return list(self._recents)

    def hint_text(self) -> str:
        """网格为空时显示的那句提示（没显示就返回空串）。"""
        return self._hint.text() if self._hint.isVisible() else ""

    def select_category(self, key: str) -> None:
        if key not in self.categories() or key == self._category:
            self._sync_category_buttons()
            return
        self._category = key
        # 切分类等于放弃搜索：否则用户会疑惑"为什么点了分类还是搜索结果"
        if self._keyword:
            self.search_field.setText("")
        self._refresh()

    def set_search_text(self, text: str) -> None:
        """搜索框写入（``textChanged`` 会带着刷新，这里只是给测试/外部一个入口）。"""
        self.search_field.setText(text)

    def choose(self, char: str) -> None:
        """选中一个表情：记进"最近"并发信号。"""
        if char not in emoji_data.ALL_EMOJI:
            return
        self._remember(char)
        self.emojiChosen.emit(char)
        # 故意不刷新网格：正在点的格子会当场换位，手速快时很别扭。
        # "最近"的顺序等下次展开面板（或重新点分类）时再体现。

    def _remember(self, char: str) -> None:
        if char in self._recents:
            self._recents.remove(char)
        self._recents.insert(0, char)
        del self._recents[self._max_recent:]

    # ==================================================================== 展开
    def is_open(self) -> bool:
        return self._open

    def set_open(self, open_: bool, *, animate: bool = True) -> None:
        if open_ == self._open:
            return
        self._open = open_
        if open_:
            self.setVisible(True)
            if not self._keyword and self._category == emoji_data.RECENT:
                self._refresh()                 # 展开时把"最近"的最新顺序摆出来
        target = self.PANEL_HEIGHT if open_ else 0
        self._anim.stop()
        if not animate:
            self.setMaximumHeight(target)
            self.setVisible(open_)
            return
        self._anim.setStartValue(max(0, self.maximumHeight()))
        self._anim.setEndValue(target)
        self._anim.start()
        if not open_:
            # 收完再隐藏，否则会在动画中途"啪"地消失
            self._anim.finished.connect(self._on_collapsed)

    def _on_collapsed(self) -> None:
        try:
            self._anim.finished.disconnect(self._on_collapsed)
        except TypeError:                        # pragma: no cover - 没连上就算了
            pass
        if not self._open:
            self.setVisible(False)

    # ==================================================================== 内部
    def _on_search(self, text: str) -> None:
        self._keyword = text.strip()
        self._refresh()

    def _refresh(self) -> None:
        if not self._ready:
            return
        if self._keyword:
            chars = emoji_data.search(self._keyword)
            empty_text = f"没有匹配「{self._keyword}」的表情"
        elif self._category == emoji_data.RECENT:
            chars = list(self._recents)
            empty_text = "还没有用过的表情，点几个试试"
        else:
            chars = [char for char, _words in emoji_data.items(self._category)]
            empty_text = "这个分类是空的"
        self._fill_grid(chars, empty_text)
        self._sync_category_buttons()

    def _fill_grid(self, chars: list[str], empty_text: str) -> None:
        for btn in self._cells:
            # 先脱离层级再排队析构，否则旧格子会和新格子叠着画
            btn.hide()
            btn.setParent(None)
            btn.deleteLater()
        self._cells.clear()

        self._columns = self._columns_for_width()
        for index, char in enumerate(chars):
            btn = QToolButton(self._grid_host)
            btn.setObjectName("emojiCell")
            btn.setFixedSize(self.CELL, self.CELL)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # 别把焦点从输入框抢走
            btn.setProperty("emojiChar", char)
            words = emoji_data.keywords(char)
            if emoji_data.is_flag(char):
                # 旗帜：用精灵图里的那一格当图标（系统字体画不了国旗）；
                # 素材缺失时 icon 为 None，退回画字符（Windows 上就是两个字母，凑合能认）
                icon = flag_icon(char)
                if icon is not None:
                    btn.setIcon(icon)
                    btn.setIconSize(QSize(self.CELL - 8, self.CELL - 8))
                else:
                    btn.setText(char)
                    btn.setFont(emoji_font(13))
                btn.setToolTip(flag_data.code_of(char).upper() or "旗帜")
            else:
                btn.setText(char)
                btn.setFont(emoji_font(15))
                btn.setToolTip(char + ("　" + " ".join(words[:3]) if words else ""))
            btn.clicked.connect(lambda _=False, c=char: self.choose(c))
            btn.show()
            self._cells.append(btn)
            self._grid.addWidget(btn, index // self._columns, index % self._columns)

        if chars:
            self._hint.hide()
        else:
            self._hint.setText(empty_text)
            self._hint.setGeometry(0, 8, max(200, self._area.viewport().width()), 40)
            self._hint.show()

    def _columns_for_width(self) -> int:
        """按面板当前宽度决定每行几个格子（Telegram 那样排满，不在右边留一大条空白）。"""
        width = self._area.viewport().width() or self.width()
        step = self.CELL + self._grid.spacing()
        return max(self.MIN_COLUMNS, min(self.MAX_COLUMNS, int(width // step)))

    def _reflow(self) -> None:
        """宽度变了就重排格子（只是把已有控件换到新的行列，不重建控件）。"""
        columns = self._columns_for_width()
        if columns == self._columns or not self._cells:
            self._columns = columns
            return
        self._columns = columns
        for index, btn in enumerate(self._cells):
            self._grid.addWidget(btn, index // columns, index % columns)

    def _sync_category_buttons(self) -> None:
        """把"当前分类"标出来；搜索时一个都不标（此时网格是搜索结果，不属于任何分类）。

        ``autoExclusive`` 下程序化取消勾选会被 Qt 挡掉，得先临时关掉互斥。
        """
        for key, btn in self._cat_buttons.items():
            wanted = key == self._category and not self._keyword
            if wanted == btn.isChecked():
                continue
            if wanted:
                btn.setChecked(True)
            else:
                btn.setAutoExclusive(False)
                btn.setChecked(False)
                btn.setAutoExclusive(True)

    # ==================================================================== 皮肤
    def apply_palette(self, p) -> None:
        self._palette = p
        self.search_field.apply_palette(p)

    def resizeEvent(self, event) -> None:        # noqa: N802
        super().resizeEvent(event)
        if self._ready:
            self._reflow()                       # 面板变宽/变窄 -> 每行格子数跟着变
        self.search_field.raise_()
        if self._hint.isVisible():
            self._hint.setGeometry(0, 8, max(200, self._area.viewport().width()), 40)

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.PANEL_HEIGHT)
