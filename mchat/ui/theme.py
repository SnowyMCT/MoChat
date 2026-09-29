"""主题令牌与全局样式表。

性能约定
--------
* 静态样式（背景、边框、按钮、滚动条…）由 :func:`build_qss` 一次性生成，
  只在切换主题时重建 —— 运行期不再重复解析 QSS。
* 需要逐帧变化的颜色（悬浮标签、分段控件高亮…）走 QPalette 或组件局部
  QSS，由组件自己管理，不进全局表。

配色取向参考 Telegram：亮色是蓝白 + 浅绿发出的气泡，暗色是 #17212B
侧栏 + #0E1621 聊天区 + #2B5278 发出的气泡。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication

FONT_STACK = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

THEME_LIGHT = "亮色"
THEME_DARK = "暗色"
THEME_SYSTEM = "跟随系统"
THEME_OPTIONS = (THEME_LIGHT, THEME_DARK, THEME_SYSTEM)


# ---------------------------------------------------------------------------
# 文字大小（全局字号缩放）
# ---------------------------------------------------------------------------
# 界面上的字号几乎都写在 QSS 的 font-size 里（气泡、会话列表、按钮、菜单、对话框…），
# 所以"文字大小"这一项做成一件事就够：**把生成好的样式表里每个 font-size 乘一个系数**。
# 一处实现 = 全局一致，不会出现"气泡变大了、侧栏没变"这种半吊子效果；
# 少数写死在代码里的字号（悬浮标签的磅值、预览文字）另外按同一个系数算。
FONT_SCALES: tuple[tuple[str, float], ...] = (
    ("小", 0.88), ("标准", 1.0), ("大", 1.14), ("特大", 1.28),
)
FONT_SCALE_LABELS = tuple(label for label, _ in FONT_SCALES)
FONT_SCALE_MIN = min(scale for _, scale in FONT_SCALES)
FONT_SCALE_MAX = max(scale for _, scale in FONT_SCALES)

_FONT_PX = re.compile(r"font-size:\s*([0-9]*\.?[0-9]+)px")
_font_scale = 1.0


def clamp_font_scale(scale) -> float:
    """把任意输入夹到允许范围内（坏值回落成 1.0），设置项不可信。"""
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value != value:                      # NaN
        return 1.0
    return max(FONT_SCALE_MIN, min(FONT_SCALE_MAX, value))


def set_font_scale(scale) -> float:
    """设当前字号系数，返回实际生效的值（各部件下次重建样式表/应用调色板时生效）。"""
    global _font_scale
    _font_scale = clamp_font_scale(scale)
    return _font_scale


def font_scale() -> float:
    return _font_scale


def scale_of(label: str) -> float:
    """档位名（"大"）-> 系数；认不出来就是标准。"""
    for name, value in FONT_SCALES:
        if name == label:
            return value
    return 1.0


def label_of(scale) -> str:
    """系数 -> 最近的档位名（存的是数字，界面要显示档位）。"""
    value = clamp_font_scale(scale)
    return min(FONT_SCALES, key=lambda item: abs(item[1] - value))[0]


def scale_font_sizes(qss: str, scale) -> str:
    """把样式表里所有 ``font-size: Npx`` 乘上系数。

    系数是 1 就原样返回（别做无谓的字符串重写，GSS 文本好比对、测试好断言）。
    """
    factor = clamp_font_scale(scale)
    if abs(factor - 1.0) < 1e-9:
        return qss

    def replace(match: re.Match) -> str:
        size = float(match.group(1)) * factor
        text = f"{size:.2f}".rstrip("0").rstrip(".")
        return f"font-size: {text}px"

    return _FONT_PX.sub(replace, qss)


@dataclass(frozen=True)
class Palette:
    """一套完整的颜色令牌。

    前半段是通用令牌（沿用原转换器的命名），后半段是聊天界面新增的：
    ``surface``（消息区/对话框底）、``bubble_in`` / ``bubble_out``（气泡）。
    """

    key: str            # light / dark
    label: str          # 界面显示名
    window: str         # 窗口 / 侧栏底色
    sidebar: str
    sidebar_hover: str
    item_selected: str
    surface: str        # 消息区、对话框底色
    card: str           # 卡片 / 顶栏 / 输入栏底色
    card_alt: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_on: str      # 铺在主题色上的文字色
    accent_soft: str
    success: str
    danger: str
    warning: str
    track: str          # 分段控件底槽 / 悬停底色
    bubble_in: str
    bubble_in_text: str
    bubble_out: str
    bubble_out_text: str
    toast_bg: str
    toast_text: str
    scroll: str


LIGHT = Palette(
    key="light", label=THEME_LIGHT,
    window="#FFFFFF", sidebar="#FFFFFF", sidebar_hover="#F1F4F7",
    item_selected="#E8F3FE", surface="#E9EEF4",
    card="#FFFFFF", card_alt="#F1F5F9",
    border="#E4E9F0", text="#17212B", muted="#7B8794",
    accent="#3390EC", accent_hover="#2B82D4", accent_pressed="#2570B8",
    accent_on="#FFFFFF", accent_soft="#E8F3FE",
    success="#12A150", danger="#E5484D", warning="#DE8B00",
    track="#EEF2F7",
    bubble_in="#FFFFFF", bubble_in_text="#17212B",
    bubble_out="#EEFFDE", bubble_out_text="#17212B",
    toast_bg="#202634", toast_text="#F6F8FC", scroll="#C7CEDC",
)

DARK = Palette(
    key="dark", label=THEME_DARK,
    window="#17212B", sidebar="#17212B", sidebar_hover="#202B36",
    item_selected="#2B5278", surface="#0E1621",
    card="#1E2A38", card_alt="#24303D",
    border="#2A3947", text="#E9EDF1", muted="#8A9AA9",
    accent="#5EABF0", accent_hover="#7CBCF5", accent_pressed="#4A96DC",
    accent_on="#0B1220", accent_soft="#223A4D",
    success="#3DD68C", danger="#FF6B6B", warning="#F5B544",
    track="#202B36",
    bubble_in="#182533", bubble_in_text="#E9EDF1",
    bubble_out="#2B5278", bubble_out_text="#EAF3FB",
    toast_bg="#E9EDF5", toast_text="#131820", scroll="#3A4453",
)

PALETTES = {p.key: p for p in (LIGHT, DARK)}


def system_palette() -> Palette:
    """读取系统配色（Qt 6.5+ 的 styleHints().colorScheme()，取不到就当亮色）。"""
    hints = QGuiApplication.styleHints()
    getter = getattr(hints, "colorScheme", None)
    if callable(getter):
        try:
            if getter() == Qt.ColorScheme.Dark:
                return DARK
        except Exception:            # pragma: no cover - 平台差异兜底
            pass
    return LIGHT


def resolve_palette(name: str) -> Palette:
    """主题名 -> 令牌。未知名字与"跟随系统"都回落到系统配色。"""
    if name == THEME_DARK:
        return DARK
    if name == THEME_LIGHT:
        return LIGHT
    return system_palette()


def watch_system_palette(callback) -> None:
    """系统配色变化时回调（Qt 6.5+ 才有这个信号，没有就静默跳过）。"""
    signal = getattr(QGuiApplication.styleHints(), "colorSchemeChanged", None)
    if signal is not None:
        signal.connect(lambda *_: callback())


def menu_qss(p: Palette, *, scale: float | None = None) -> str:
    """右键/下拉菜单的样式（菜单是独立顶层控件，不吃主窗口的样式表）。"""
    qss = f"""
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
    return scale_font_sizes(qss, font_scale() if scale is None else scale)


def build_qss(p: Palette, *, scale: float | None = None) -> str:
    """生成整套静态样式表（字号按当前"文字大小"设置整体缩放）。"""
    qss = f"""
    QWidget {{ font-family: {FONT_STACK}; }}
    QWidget#root, QWidget#toolRoot, QDialog {{ color: {p.text}; }}

    /* ---------------------------------------------------------- 骨架 -- */
    QWidget#root {{ background-color: {p.surface}; }}
    /* 侧栏与聊天区之间的竖线不在这里画：由 SplitHandle 自绘，
       这样拖动时线能跟着高亮/变粗，也不用让两个控件各自画半条 */
    QWidget#sidebar {{
        background-color: {p.sidebar};
    }}
    QWidget#chatHeader {{
        background-color: {p.card};
        border-bottom: 1px solid {p.border};
    }}
    QWidget#composer {{
        background-color: {p.card};
        border-top: 1px solid {p.border};
    }}
    QScrollArea#messageArea {{ background: transparent; border: none; }}
    QScrollArea#messageArea > QWidget > QWidget {{ background-color: {p.surface}; }}

    /* ------------------------------------------------------ 会话列表 -- */
    QLabel#appTitle {{ font-size: 17px; font-weight: 700; color: {p.text}; }}
    QFrame#convItem {{ background: transparent; border-radius: 10px; }}
    QFrame#convItem:hover {{ background-color: {p.sidebar_hover}; }}
    QFrame#convItem[selected="true"] {{ background-color: {p.item_selected}; }}
    QLabel#convName {{ font-size: 14px; font-weight: 600; color: {p.text}; }}
    QLabel#convPreview {{ font-size: 12px; color: {p.muted}; }}
    QLabel#convTime {{ font-size: 11px; color: {p.muted}; }}
    QLabel#badge {{
        background-color: {p.accent}; color: {p.accent_on};
        font-size: 11px; font-weight: 700;
        border-radius: 10px; padding: 1px 7px;
    }}
    QLabel#convMark {{
        font-size: 11px; padding: 0 2px;
    }}
    QLabel#sideSectionTitle {{
        font-size: 11px; font-weight: 700; color: {p.muted};
        padding: 2px 4px;
    }}

    /* ------------------------------------------------------ 聊天区域 -- */
    QLabel#chatName {{ font-size: 15px; font-weight: 700; color: {p.text}; }}
    QLabel#chatStatus {{ font-size: 12px; color: {p.muted}; }}
    QLabel#chatStatus[typing="true"] {{ color: {p.accent}; }}
    QLabel#dateChip {{
        background-color: {p.track}; color: {p.muted};
        font-size: 11.5px; font-weight: 600;
        border-radius: 11px; padding: 3px 12px;
    }}
    QFrame#bubbleIn {{
        background-color: {p.bubble_in};
        border: 1px solid {p.border};
        border-radius: 14px;
        border-bottom-left-radius: 5px;
    }}
    QFrame#bubbleOut {{
        background-color: {p.bubble_out};
        border: none;
        border-radius: 14px;
        border-bottom-right-radius: 5px;
    }}
    QFrame#bubbleIn QLabel#msgText {{ color: {p.bubble_in_text}; }}
    QFrame#bubbleOut QLabel#msgText {{ color: {p.bubble_out_text}; }}
    QLabel#msgText {{ font-size: 13.5px; }}
    QLabel#msgMeta {{ font-size: 10.5px; color: {p.muted}; }}
    QFrame#bubbleOut QLabel#msgMeta {{ color: {p.bubble_out_text}; }}
    QFrame#bubbleOut QLabel#readMark {{ color: {p.accent}; font-size: 11px; }}
    QLabel#senderName {{ font-size: 12px; font-weight: 700; color: {p.accent}; }}
    QLabel#emptyTitle {{ font-size: 16px; font-weight: 700; color: {p.text}; }}
    QLabel#emptyText {{ font-size: 12.5px; color: {p.muted}; }}

    /* 表情回应：点一下加/撤，自己点过的高亮 */
    QLabel#reactionChip {{
        background-color: {p.track};
        border: 1px solid transparent;
        border-radius: 10px;
        font-size: 11.5px;
        padding: 1px 7px;
    }}
    QLabel#reactionChip:hover {{ border-color: {p.accent}; }}
    QLabel#reactionChip[mine="true"] {{
        background-color: {p.accent_soft};
        border-color: {p.accent};
    }}
    /* 跳到某条消息时闪一下边框（动态属性 + repolish） */
    QFrame#bubbleIn[flash="true"], QFrame#bubbleOut[flash="true"] {{
        border: 2px solid {p.accent};
    }}
    /* 会话列表里未发出去的草稿 */
    QLabel#convPreview[draft="true"] {{ color: {p.danger}; font-weight: 600; }}

    /* ---------------------------------------------------- 搜索窗口 -- */
    QWidget#searchRoot {{ background-color: {p.surface}; }}
    /* 滚动区不写这两行的话，视口会退回系统默认底色（暗色主题下露出一片浅灰） */
    QScrollArea#searchArea {{ background: transparent; border: none; }}
    QScrollArea#searchArea > QWidget > QWidget {{ background-color: {p.surface}; }}
    QWidget#searchRow {{ background-color: {p.card}; border-radius: 10px; }}
    QWidget#searchRow[selected="true"] {{ background-color: {p.item_selected}; }}
    QLabel#searchTitle {{ font-size: 12.5px; font-weight: 700; color: {p.text}; }}
    QLabel#searchSnippet {{ font-size: 12px; color: {p.muted}; }}
    QLabel#searchTime {{ font-size: 11px; color: {p.muted}; }}
    QLabel#searchHint {{ font-size: 12px; color: {p.muted}; }}

    /* ------------------------------------------------------ 输入栏 -- */
    /* color 必须写：只写背景的话，QSS 会退回系统默认调色板，
       暗色主题下输入框里的字就是黑的，落在深色底上根本看不见 */
    QTextEdit#composerInput {{
        background: transparent;
        border: none;
        color: {p.text};
        selection-background-color: {p.accent_soft};
        selection-color: {p.text};
        font-size: 13.5px;
        padding: 0px;
    }}

    /* -------------------------------------------------------- 附件 -- */
    /* 待发送条（输入栏上方）：缩略图/文件图标 + 名字 + 大小 + ✕ */
    QWidget#attachTray {{
        background-color: {p.card_alt};
        border-top: 1px solid {p.border};
    }}
    QLabel#attachThumb {{
        border: 1px solid {p.border};
        border-radius: 8px;
        background-color: {p.card};
    }}
    QLabel#attachName {{ font-size: 12.5px; font-weight: 600; color: {p.text}; }}
    QLabel#attachMeta {{ font-size: 11px; color: {p.muted}; }}
    /* 气泡里的图片：等比缩放后的缩略图，点一下用系统看图器打开 */
    QLabel#imagePreview {{
        border: 1px solid {p.border};
        border-radius: 10px;
        background-color: {p.card_alt};
        color: {p.muted};
        font-size: 12px;
        padding: 10px;
    }}
    /* 气泡里的文件卡片：图标 + 文件名 + 类型/大小 */
    QFrame#fileCard {{
        background-color: {p.card_alt};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}
    /* 自己发出去的气泡底色本身就是主题色/浅绿，卡片改用半透明白，两种主题都压得住 */
    QFrame#bubbleOut QFrame#fileCard {{
        background-color: rgba(255, 255, 255, 0.42);
        border: 1px solid rgba(255, 255, 255, 0.55);
    }}
    QLabel#fileName {{ font-size: 12.5px; font-weight: 600; color: {p.text}; }}
    QLabel#fileMeta {{ font-size: 11px; color: {p.muted}; }}
    QFrame#bubbleOut QLabel#fileName {{ color: {p.bubble_out_text}; }}
    QFrame#bubbleOut QLabel#fileMeta {{ color: {p.bubble_out_text}; }}

    /* ---------------------------------------------------- 表情面板 -- */
    QWidget#emojiPanel {{
        background-color: {p.card};
        border-top: 1px solid {p.border};
    }}
    QScrollArea#emojiArea {{ background: transparent; border: none; }}
    QScrollArea#emojiArea > QWidget > QWidget {{ background-color: {p.card}; }}
    QToolButton#emojiCell {{
        background: transparent;
        border: none;
        border-radius: 9px;
        color: {p.text};
        padding: 0px;
    }}
    QToolButton#emojiCell:hover {{ background-color: {p.track}; }}
    QToolButton#emojiCell:pressed {{ background-color: {p.border}; }}
    QToolButton#emojiCatBtn {{
        background: transparent;
        border: none;
        border-radius: 8px;
        color: {p.muted};
        padding: 0px;
    }}
    QToolButton#emojiCatBtn:hover {{ background-color: {p.track}; }}
    QToolButton#emojiCatBtn:checked {{ background-color: {p.accent_soft}; }}
    QLabel#emojiHint {{ font-size: 12.5px; color: {p.muted}; }}

    /* ------------------------------------------------------ 工具箱 -- */
    QWidget#toolRoot {{ background-color: {p.surface}; }}
    QFrame#card {{
        background-color: {p.card};
        border: 1px solid {p.border};
        border-radius: 14px;
    }}
    QLabel#title {{ font-size: 22px; font-weight: 700; color: {p.text}; }}
    QLabel#subtitle {{ font-size: 12px; color: {p.muted}; }}
    QLabel#cardTitle {{ font-size: 13px; font-weight: 700; color: {p.text}; }}
    QLabel#hint {{ font-size: 11px; color: {p.muted}; }}

    /* ---------------------------------------------------- 登录窗口 -- */
    /* 状态行 / 用户行（新建会话）—— 登录窗口整窗用 build_qss，自带卡片与主按钮样式 */
    QLabel#loginStatus {{ font-size: 12px; color: {p.muted}; }}
    QWidget#userRow {{ background-color: {p.card}; border-radius: 10px; }}
    QWidget#userRow:hover {{ background-color: {p.item_selected}; }}
    QWidget#userRow[selected="true"] {{ background-color: {p.accent_soft}; }}
    QLabel#userName {{ font-size: 13.5px; font-weight: 600; color: {p.text}; }}
    QLabel#userHandle {{ font-size: 11.5px; color: {p.muted}; }}
    QLabel#dialogHint {{ font-size: 12px; color: {p.muted}; }}
    QLabel#dialogStatus {{ font-size: 12px; color: {p.muted}; }}

    /* -------------------------------------------------------- 按钮 -- */
    QPushButton#primary {{
        background-color: {p.accent};
        color: {p.accent_on};
        border: none;
        border-radius: 12px;
        font-size: 15px;
        font-weight: 700;
    }}
    QPushButton#primary:hover {{ background-color: {p.accent_hover}; }}
    QPushButton#primary:pressed {{ background-color: {p.accent_pressed}; }}
    QPushButton#primary:disabled {{ background-color: {p.track}; color: {p.muted}; }}

    QToolButton#ghost {{
        background: transparent;
        border: none;
        border-radius: 8px;
        color: {p.muted};
        font-size: 12px;
        padding: 3px 9px;
    }}
    QToolButton#ghost:hover {{ background-color: {p.track}; color: {p.text}; }}
    QToolButton#ghost:pressed {{ background-color: {p.border}; }}

    QToolButton#iconBtn {{
        background: transparent;
        border: none;
        border-radius: 9px;
    }}
    QToolButton#iconBtn:hover {{ background-color: {p.track}; }}
    QToolButton#iconBtn:pressed {{ background-color: {p.border}; }}
    QToolButton#iconBtn:checked {{ background-color: {p.accent_soft}; }}

    QToolButton#toolBtn {{
        background-color: {p.accent_soft};
        border: 1px solid {p.accent_soft};
        border-radius: 10px;
        color: {p.accent};
        font-size: 12.5px;
        font-weight: 600;
        padding: 4px 11px;
    }}
    QToolButton#toolBtn:hover {{ border-color: {p.accent}; }}
    QToolButton#toolBtn:pressed {{ background-color: {p.item_selected}; }}

    QToolButton#sendBtn {{
        background-color: {p.accent};
        border: none;
        border-radius: 21px;
    }}
    QToolButton#sendBtn:hover {{ background-color: {p.accent_hover}; }}
    QToolButton#sendBtn:pressed {{ background-color: {p.accent_pressed}; }}

    QToolButton#eye {{
        background: transparent;
        border: none;
        border-radius: 6px;
        color: {p.muted};
        font-size: 11px;
        padding: 0;
    }}
    QToolButton#eye:hover {{ background-color: {p.track}; color: {p.accent}; }}
    QToolButton#eye:checked {{ color: {p.accent}; }}

    QToolButton#closeBtn {{
        background: transparent;
        border: none;
        border-radius: 9px;
        color: {p.muted};
        font-size: 15px;
        font-weight: 700;
    }}
    QToolButton#closeBtn:hover {{ background-color: {p.track}; color: {p.danger}; }}

    QCheckBox {{ color: {p.muted}; font-size: 12px; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 2px solid {p.border};
        border-radius: 5px;
        background-color: {p.card};
    }}
    QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox::indicator:checked {{ background-color: {p.accent}; border-color: {p.accent}; }}

    /* ---------------------------------------------------- 滚动条等 -- */
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 6px 3px 6px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {p.scroll}; border-radius: 4px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.accent}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

    QToolTip {{
        background-color: {p.toast_bg};
        color: {p.toast_text};
        border: none;
        padding: 6px 9px;
        font-family: {FONT_STACK};
        font-size: 12px;
    }}

    /* -------------------------------------------- 回复 / 转发 / 置顶 -- */
    /* 气泡里的引用块（回复）：左边一根竖条 + 两行小字，整块可以点，跳到原消息 */
    QFrame#quoteBox {{
        background-color: {p.track};
        border: none;
        border-left: 3px solid {p.accent};
        border-radius: 6px;
    }}
    QFrame#bubbleOut QFrame#quoteBox {{ background-color: rgba(255, 255, 255, 0.42); }}
    QLabel#quoteAuthor {{ font-size: 11.5px; font-weight: 700; color: {p.accent}; }}
    QLabel#quoteText {{ font-size: 11.5px; color: {p.muted}; }}
    QFrame#bubbleOut QLabel#quoteText {{ color: {p.bubble_out_text}; }}
    /* "转发自 X" 的小字（Telegram 的 Forwarded from） */
    QLabel#forwardCaption {{ font-size: 11.5px; font-weight: 600; color: {p.accent}; }}
    /* 合并转发：一张卡片，列出前几条，点一下展开全部 */
    QFrame#mergedCard {{
        background-color: {p.track};
        border: none;
        border-left: 3px solid {p.accent};
        border-radius: 8px;
    }}
    QFrame#bubbleOut QFrame#mergedCard {{ background-color: rgba(255, 255, 255, 0.42); }}
    QLabel#mergeTitle {{ font-size: 12.5px; font-weight: 700; color: {p.accent}; }}
    QLabel#mergePreview {{ font-size: 12px; color: {p.text}; }}
    QFrame#bubbleOut QLabel#mergePreview {{ color: {p.bubble_out_text}; }}
    QLabel#mergeFooter {{ font-size: 11px; color: {p.muted}; }}
    /* "聊天记录"查看窗口（合并转发点开之后） */
    QDialog#mergedViewerRoot {{ background-color: {p.surface}; }}
    QLabel#mergedViewTitle {{ font-size: 15px; font-weight: 700; color: {p.text}; }}
    QLabel#mergedViewSubtitle {{ font-size: 12px; color: {p.muted}; }}
    QWidget#mergedViewHost, QScrollArea#mergedViewArea {{ background: transparent; }}
    QWidget#mergedRow {{ background: transparent; }}
    QLabel#mergedNick {{ font-size: 12px; color: {p.accent}; font-weight: 600; }}
    QLabel#mergedClock {{ font-size: 11px; color: {p.muted}; }}
    QFrame#mergedBubble {{
        background-color: {p.card};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}
    QLabel#mergedBody {{ font-size: 12.5px; color: {p.text}; }}
    QLabel#mergedAttach {{ font-size: 12px; color: {p.accent}; }}
    /* 选中态：整条气泡描一圈主题色 */
    QFrame#bubbleIn[selected="true"], QFrame#bubbleOut[selected="true"] {{
        border: 1px solid {p.accent};
    }}
    /* 批量操作条：贴在输入栏上方 */
    QWidget#selectBar {{
        background-color: {p.card};
        border-top: 1px solid {p.border};
    }}
    QLabel#selectCount {{ font-size: 13px; font-weight: 700; color: {p.accent}; }}
    QLabel#selectHint {{ font-size: 11.5px; color: {p.muted}; }}
    QPushButton#dangerBtn {{
        background-color: {p.danger};
        border: none;
        border-radius: 10px;
        color: #FFFFFF;
        font-size: 12.5px;
        font-weight: 700;
        padding: 6px 12px;
    }}
    QPushButton#dangerBtn:hover {{ background-color: {p.danger}; color: #FFFFFF; border: 1px solid #FFFFFF; }}
    QPushButton#dangerBtn:disabled {{ background-color: {p.track}; color: {p.muted}; }}
    /* 会话内置顶横幅：贴在聊天头部下面 */
    QFrame#pinBanner {{
        background-color: {p.card};
        border: none;
        border-bottom: 1px solid {p.border};
    }}
    QLabel#pinTitle {{ font-size: 11.5px; font-weight: 700; color: {p.accent}; }}
    QLabel#pinText {{ font-size: 12px; color: {p.text}; }}
    QLabel#pinCounter {{ font-size: 11px; color: {p.muted}; }}
    QLabel#pinScope {{ font-size: 10.5px; color: {p.accent}; background-color: {p.accent_soft};
        border-radius: 6px; padding: 1px 5px; }}
    QToolButton#pinNav {{
        border: none; border-radius: 6px; color: {p.muted}; font-size: 14px;
        background: transparent; padding: 0px;
    }}
    QToolButton#pinNav:hover {{ background-color: {p.card}; color: {p.text}; }}
    QFrame#pinBanner:hover {{ background-color: {p.card_alt}; }}
    /* 输入栏上方的"回复 XX"条 */
    QFrame#replyBar {{
        background-color: {p.card};
        border: none;
        border-left: 3px solid {p.accent};
        border-radius: 8px;
    }}
    QLabel#replyTitle {{ font-size: 11.5px; font-weight: 700; color: {p.accent}; }}
    QLabel#replyText {{ font-size: 12px; color: {p.muted}; }}
    /* 转发窗口 */
    QDialog#forwardRoot {{ background-color: {p.surface}; }}
    QLabel#forwardTitle {{ font-size: 18px; font-weight: 700; color: {p.text}; }}
    QLabel#forwardSubtitle {{ font-size: 12px; color: {p.muted}; }}
    QPushButton#convRow {{
        background-color: {p.card};
        border: 1px solid {p.border};
        border-radius: 12px;
        color: {p.text};
        font-size: 13px;
        font-weight: 600;
        padding: 9px 12px;
        text-align: left;
    }}
    QPushButton#convRow:hover {{ border-color: {p.accent}; color: {p.accent}; }}
    QPushButton#convRow:pressed {{ background-color: {p.track}; }}
    QLabel#forwardFoot {{ font-size: 11.5px; color: {p.muted}; }}

    /* ---------------------------------------------------------- 设置 -- */
    QDialog#settingsRoot {{ background-color: {p.surface}; }}
    QLabel#settingsTitle {{ font-size: 19px; font-weight: 700; color: {p.text}; }}
    QLabel#settingsSubtitle {{ font-size: 12px; color: {p.muted}; }}
    QLabel#settingName {{ font-size: 13.5px; font-weight: 600; color: {p.text}; }}
    QLabel#settingHint {{ font-size: 11.5px; color: {p.muted}; }}
    QLabel#settingValue {{ font-size: 12px; font-weight: 600; color: {p.accent}; }}
    /* 字号预览：改档位时这里的字号会动画过渡（所见即所得） */
    QLabel#settingsPreview {{
        background-color: {p.card_alt};
        border: 1px solid {p.border};
        border-radius: 10px;
        color: {p.text};
        padding: 10px 12px;
    }}
    QLabel#settingsFoot {{ font-size: 11px; color: {p.muted}; }}
    /* 设置里的次要按钮（"打开数据目录"） */
    QPushButton#ghostBtn {{
        background-color: {p.card_alt};
        border: 1px solid {p.border};
        border-radius: 10px;
        color: {p.text};
        font-size: 12.5px;
        font-weight: 600;
        padding: 6px 12px;
    }}
    QPushButton#ghostBtn:hover {{ border-color: {p.accent}; color: {p.accent}; }}
    QPushButton#ghostBtn:pressed {{ background-color: {p.track}; }}
    """
    return scale_font_sizes(qss, font_scale() if scale is None else scale)
