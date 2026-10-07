"""动效组件库。

组件库，前四个用 QPropertyAnimation 驱动（Qt 自带插值，稳定 60fps）：

    FloatingLabelInput  悬浮标签输入框  —— Test17 的核心动效，这里通用化
    SegmentedControl    分段切换控件    —— 带滑块动画的"胶囊"选择器
    Toast               提示气泡        —— 淡入淡出 + 上滑，不挤占布局
    CollapsibleSection  折叠容器        —— 高度 + 透明度双动画展开/收起
    SplitHandle         左右分界线      —— 拖动改变左右区域宽度（自绘，无动画）

性能取舍
--------
* 组件内部不用嵌套布局定位标签/滑块，几何全部手算，避免动画期间布局反复回流。
* 逐帧只改字号与 QPalette 颜色（微秒级），不逐帧 setStyleSheet（要重新解析 QSS）。
* 不用 QGraphicsEffect 做阴影，避免每帧离屏合成；层次靠描边和底色区分。
"""
from __future__ import annotations

from PyQt6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation,
                          QRect, QSize, QTimer, Qt, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import QColor, QPen, QFont, QFontMetrics, QPainter, QPalette
from PyQt6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

from . import anim
from . import theme
from .icons import IconButton

__all__ = ["lerp", "mix_color", "FloatingLabelInput", "SegmentedControl", "ToggleSwitch",
           "Toast", "CollapsibleSection", "SplitHandle", "confirm_destructive",
           "WindowChrome", "install_app_chrome", "FramelessResizer", "CHROME_H",
           "CHROME_CLOSE", "CHROME_WINDOW"]


def confirm_destructive(parent, palette, *, title: str, text: str,
                        ok_text: str = "确定删除", detail: str = "") -> bool:
    """危险操作的二次确认：弹窗 + 红色按钮（删消息、清空历史这类不可撤销的动作）。

    用 `QMessageBox` 而不是自绘对话框：它有平台标准的按键/回车/Esc 行为，
    这里只把它按主题上个色，并把确认键染红。
    """
    from PyQt6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        # 批量操作时把"要删的到底是哪几条"摆出来（Qt 的"显示详情"折叠区）
        box.setDetailedText(detail)
    confirm = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(box.buttons()[-1])                # 默认停在"取消"上，别手滑
    box.setStyleSheet(f"""
        QMessageBox {{ background-color: {palette.card}; }}
        QMessageBox QLabel {{ color: {palette.text}; font-size: 13px; }}
        QPushButton {{
            background-color: {palette.track}; color: {palette.text};
            border: none; border-radius: 8px; padding: 6px 16px; font-size: 13px;
        }}
        QPushButton:hover {{ background-color: {palette.border}; }}
        QPushButton:default, QPushButton:focus {{
            background-color: {palette.danger}; color: #FFFFFF; font-weight: 600;
        }}
    """)
    box.exec()
    return box.clickedButton() is confirm


def lerp(a: float, b: float, t: float) -> float:
    """线性插值。"""
    return a + (b - a) * t


def mix_color(start: QColor, end: QColor, t: float) -> QColor:
    """两个颜色之间按 t 插值。"""
    return QColor(
        round(lerp(start.red(), end.red(), t)),
        round(lerp(start.green(), end.green(), t)),
        round(lerp(start.blue(), end.blue(), t)),
    )


# 字重插值只能取枚举档位（Qt6 的 QFont.Weight 不接受任意整数）
_WEIGHTS = (QFont.Weight.Normal, QFont.Weight.Medium, QFont.Weight.DemiBold)


# ---------------------------------------------------------------------------
# 悬浮标签输入框
# ---------------------------------------------------------------------------
class FloatingLabelInput(QWidget):
    """Material / Google 风格的悬浮标签输入框。

    标签有"框内占位"与"浮到边框线上"两种状态，切换时同时插值三件事：
    纵坐标、字号（14 -> 11.5）、颜色（灰 -> 主题色）——所以看起来是"变形"
    而不是"跳变"。浮起后标签宽度收成文字宽度，正好在边框上切出一个缺口。

    参数
    ----
    multiline    True 时内部用 QPlainTextEdit（多行）
    password     True 时右侧带"显示/隐藏"按钮，默认掩码显示
    read_only    只读（用于输出框）
    field_height 输入框的最小高度
    """

    textChanged = pyqtSignal(str)
    submitted = pyqtSignal()

    GAP = 9             # 顶部留白 = 悬浮标签高度的一半，让标签能骑在边框线上
    LABEL_H = 18
    INSIDE_PT = 14.0
    FLOAT_PT = 11.5
    LABEL_PAD = 6       # 标签左右内边距（缺口宽度）
    LABEL_X = 8         # 框内状态：8 + 6(pad) = 14，与输入框文字内边距对齐
    LABEL_X_FLOAT = 10  # 悬浮状态：让出圆角
    ANIM_MS = 170

    def __init__(self, label: str, parent: QWidget | None = None, *,
                 multiline: bool = False, password: bool = False,
                 read_only: bool = False, field_height: int = 48, radius: int = 10):
        super().__init__(parent)
        # 构造期 Qt 也会派发事件（setCursor -> CursorChange、setMinimumHeight -> resize），
        # 处理函数里若用到尚未创建的属性，会在 C++ 虚函数里抛异常并直接 abort。
        # 所以先立一个 ready 闸门，构造结束再放行。
        self._ready = False
        self._palette = None
        self._label_text = label
        self._multiline = multiline
        self._password = password
        self._min_field_h = field_height
        self._radius = radius
        self._progress = 0.0
        self._focused = False
        self._error = False
        self._label_bg: str | None = None
        self._eye: QToolButton | None = None
        self._float_w = 60

        # ---- 真正的输入控件 ----
        if multiline:
            field: QLineEdit | QPlainTextEdit = QPlainTextEdit(self)
            field.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            field.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            field.setTabChangesFocus(True)
            field.textChanged.connect(self._on_multiline_changed)
        else:
            field = QLineEdit(self)
            field.textChanged.connect(self.textChanged)
            field.returnPressed.connect(self.submitted)
            if password:
                field.setEchoMode(QLineEdit.EchoMode.Password)
                self._eye = QToolButton(self)
                self._eye.setObjectName("eye")
                self._eye.setText("显示")
                self._eye.setCheckable(True)
                self._eye.setCursor(Qt.CursorShape.PointingHandCursor)
                self._eye.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                self._eye.setFixedSize(44, 24)
                self._eye.toggled.connect(self._toggle_echo)
        self._field = field  # 必须在装事件过滤器之前就位
        field.setReadOnly(read_only)
        field.installEventFilter(self)
        field.setCursor(Qt.CursorShape.IBeamCursor)
        self._read_only = read_only

        # ---- 悬浮标签 ----
        self._label = QLabel(label, self)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # ---- 动画 ----
        self._anim = QPropertyAnimation(self, b"labelProgress", self)
        self._anim.setDuration(self.ANIM_MS)
        # InOutCubic：起步慢、中间快、收尾慢 —— 两头都不"撞墙"
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding if multiline else QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(self._min_field_h + self.GAP)
        self._recompute_float_width()
        self._ready = True
        self._relayout()  # 补一次构造期的几何计算

    # ------------------------------------------------------------------ 属性
    def get_label_progress(self) -> float:
        return self._progress

    def set_label_progress(self, value: float) -> None:
        self._progress = float(value)
        self._apply_progress()

    labelProgress = pyqtProperty(float, fget=get_label_progress, fset=set_label_progress)

    # ------------------------------------------------------------------ 布局
    def sizeHint(self) -> QSize:
        return QSize(220, self._min_field_h + self.GAP)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._ready:
            self._relayout()

    def _relayout(self) -> None:
        if not self._ready:
            return
        w, h = self.width(), self.height()
        fh = max(self._min_field_h, h - self.GAP)
        self._field.setGeometry(0, self.GAP, w, fh)
        if self._eye is not None:
            self._eye.move(w - self._eye.width() - 8, self.GAP + (fh - self._eye.height()) // 2)
            self._eye.raise_()
        self._apply_progress()

    def _recompute_float_width(self) -> None:
        font = QFont(self.font())
        font.setPointSizeF(self.FLOAT_PT * self.font_scale())
        font.setWeight(QFont.Weight.DemiBold)
        self._float_w = QFontMetrics(font).horizontalAdvance(self._label_text) + self.LABEL_PAD * 2 + 4

    def font_scale(self) -> float:
        """当前"文字大小"系数。

        界面上的字号大多写在 QSS 里、由 ``theme.scale_font_sizes()`` 统一缩放，只有
        这个悬浮标签的磅值是写死在代码里的两档（14 → 11.5），所以它得自己跟着走 ——
        否则"文字大小"调大以后，标签会显得比框里的字小一号。
        """
        return theme.font_scale()

    def refresh_fonts(self) -> None:
        """字号设置变了：重算标签宽度（浮起后的缺口宽度是按字号量出来的）并重排。"""
        self._recompute_float_width()
        self._apply_progress()

    def _apply_progress(self) -> None:
        """按当前进度摆放标签：位置 + 字号 + 字重 + 颜色，全部插值。"""
        t = self._progress
        rect = self._field.geometry()
        inside_y = rect.top() + (rect.height() - self.LABEL_H) // 2
        y = round(lerp(inside_y, 0, t))
        full_w = max(60, rect.width() - self.LABEL_X - 14)
        w = round(lerp(full_w, self._float_w, t))
        x = round(lerp(rect.left() + self.LABEL_X, rect.left() + self.LABEL_X_FLOAT, t))
        self._label.setGeometry(x, y, w, self.LABEL_H)

        font = QFont(self._label.font())
        font.setPointSizeF(lerp(self.INSIDE_PT, self.FLOAT_PT, t) * self.font_scale())
        font.setWeight(_WEIGHTS[min(2, int(t * 3))])
        self._label.setFont(font)

        pal = self._label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, self._label_color(t))
        self._label.setPalette(pal)
        self._update_label_bg(t)

    def _update_label_bg(self, t: float) -> None:
        """标签底色必须与它身后那层背景一致，否则会露出一条色带。

        框内状态（t < 0.5）标签压在输入区上 -> 用输入区底色；
        浮起到边框线上时它身后是卡片 -> 用卡片底色（正好"切断"边框线）。
        只在真的需要换色时才动样式表，所以一次动画最多两次 QSS 解析。
        """
        p = self._palette
        if p is None:
            return
        fill = p.card_alt if self._read_only else p.card
        wanted = fill if t < 0.5 else p.card
        if wanted == self._label_bg:
            return
        self._label_bg = wanted
        self._label.setStyleSheet(
            f"QLabel {{ background-color: {wanted}; border-radius: 4px;"
            f" padding: 0 {self.LABEL_PAD}px; }}"
        )

    def _label_color(self, t: float) -> QColor:
        if self._palette is None:  # 主题还没注入时的安全兜底
            return QColor("#767F92")
        base = QColor(self._palette.muted)
        if self._error:
            return mix_color(base, QColor(self._palette.danger), t)
        target = QColor(self._palette.accent if self._focused else self._palette.muted)
        return mix_color(base, target, t)

    # ------------------------------------------------------------------ 交互
    def eventFilter(self, obj, event) -> bool:
        if self._ready and obj is self._field:
            if event.type() == QEvent.Type.FocusIn:
                self._focused = True
                self._sync_state()
            elif event.type() == QEvent.Type.FocusOut:
                self._focused = False
                self._sync_state()
        return super().eventFilter(obj, event)

    def _sync_state(self, animate: bool = True) -> None:
        """聚焦或已有内容 -> 标签浮起，否则落回框内。"""
        target = 1.0 if (self._focused or self.has_text()) else 0.0
        if abs(target - self._progress) < 0.001:
            return
        self._anim.stop()
        if animate:
            self._anim.setStartValue(self._progress)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self.set_label_progress(target)

    def _on_multiline_changed(self) -> None:
        self.textChanged.emit(self.text())
        self._sync_state()

    def _toggle_echo(self, shown: bool) -> None:
        self._field.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self._eye.setText("隐藏" if shown else "显示")

    # ------------------------------------------------------------------ 对外
    def text(self) -> str:
        return self._field.toPlainText() if self._multiline else self._field.text()

    def setText(self, text: str) -> None:
        if self._multiline:
            self._field.setPlainText(text)
        else:
            self._field.setText(text)
        self._sync_state()

    def clear(self) -> None:
        self.setText("")

    def has_text(self) -> bool:
        return bool(self.text())

    def set_label(self, text: str) -> None:
        if text == self._label_text:
            return
        self._label_text = text
        self._label.setText(text)
        self._recompute_float_width()
        self._apply_progress()

    def label_text(self) -> str:
        return self._label_text

    def focus_field(self) -> None:
        self._field.setFocus(Qt.FocusReason.OtherFocusReason)

    def flash_error(self, ms: int = 750) -> None:
        """出错反馈：边框闪一下危险色（比抖动更稳定，不受布局回流影响）。"""
        self._error = True
        self._apply_field_style()
        self._apply_progress()
        QTimer.singleShot(ms, self._clear_error)

    def _clear_error(self) -> None:
        self._error = False
        self._apply_field_style()
        self._apply_progress()

    # ------------------------------------------------------------------ 皮肤
    def apply_palette(self, p) -> None:
        self._palette = p
        self._apply_field_style()
        self._label_bg = None  # 主题变了，强制重算标签底色
        self._update_label_bg(self._progress)
        if self._eye is not None:
            # 眼睛按钮用全局 QSS 上色，主题变了要让样式重新解析一次
            self._eye.style().unpolish(self._eye)
            self._eye.style().polish(self._eye)
        self.refresh_fonts()   # 字号设置也可能刚变过（两个设置都是在这里统一生效）

    def _apply_field_style(self) -> None:
        p = self._palette
        if p is None:
            return
        focus_border = p.danger if self._error else p.accent
        border = p.danger if self._error else p.border
        background = p.card_alt if self._read_only else p.card
        kind = "QPlainTextEdit" if self._multiline else "QLineEdit"
        self._field.setStyleSheet(f"""
            {kind} {{
                background-color: {background};
                border: 2px solid {border};
                border-radius: {self._radius}px;
                padding: {4 if self._multiline else 0}px 12px;
                color: {p.text};
                font-size: 14px;
                selection-background-color: {p.accent_soft};
                selection-color: {p.text};
            }}
            {kind}:focus {{ border-color: {focus_border}; }}
            {kind}[readOnly="true"] {{ background-color: {p.card_alt}; }}
        """)


# ---------------------------------------------------------------------------
# 分段切换控件
# ---------------------------------------------------------------------------
class SegmentedControl(QWidget):
    """胶囊式分段控件，选中态由一个带 QPropertyAnimation 的滑块指示器承载。

    几何同样手算（等分宽度），所以滑块动画只动一个控件，代价可以忽略。
    """

    valueChanged = pyqtSignal(str)

    def __init__(self, options, parent: QWidget | None = None, *,
                 height: int = 38, compact: bool = False):
        super().__init__(parent)
        self._ready = False
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("segTrack")
        self._palette = None
        self._height = height
        self._compact = compact
        self._options: list[str] = []
        self._buttons: list[QToolButton] = []
        self._index = 0
        self.setFixedHeight(height)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed if compact else QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.setMinimumWidth(150)

        self._indicator = QFrame(self)
        self._indicator.setObjectName("indicator")
        self._anim = QPropertyAnimation(self._indicator, b"geometry", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.set_options(list(options), notify=False)
        self._ready = True
        self._relayout(animate=False)

    # ------------------------------------------------------------------ 数据
    def options(self) -> list[str]:
        return list(self._options)

    def value(self) -> str:
        return self._options[self._index] if self._options else ""

    def index(self) -> int:
        return self._index

    def set_options(self, values, *, notify: bool = True) -> None:
        for btn in self._buttons:
            # 立即脱离层级再排队析构：deleteLater 要等事件循环处理 DeferredDelete，
            # 在那之前旧按钮还会画在原处，和新按钮的文字叠成一片。
            btn.hide()
            btn.setParent(None)
            btn.deleteLater()
        self._buttons.clear()
        self._options = [str(v) for v in values]
        self._index = 0
        for i, text in enumerate(self._options):
            btn = QToolButton(self)
            btn.setText(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            btn.clicked.connect(lambda _=False, idx=i: self.set_index(idx))
            btn.show()  # 父控件已经显示时，后建的子控件不会自动显示
            self._buttons.append(btn)
        self._apply_palette_impl()
        self._relayout(animate=False)
        if notify and self._options:
            self.valueChanged.emit(self.value())

    def set_index(self, index: int, *, notify: bool = True, animate: bool = True) -> None:
        index = max(0, min(index, len(self._options) - 1))
        if index == self._index:
            return
        self._index = index
        self._relayout(animate=animate)
        self.set_selected_styles()
        if notify:
            self.valueChanged.emit(self.value())

    # ------------------------------------------------------------------ 布局
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._ready:
            self._relayout(animate=False)

    def _slot_rect(self, index: int) -> QRect:
        count = max(1, len(self._options))
        slot_w = self.width() / count
        return QRect(round(slot_w * index), 0, round(slot_w), self._height)

    def _relayout(self, *, animate: bool) -> None:
        if not self._ready:
            return
        for i, btn in enumerate(self._buttons):
            btn.setGeometry(self._slot_rect(i))
        if not self._buttons:
            return
        slot = self._slot_rect(self._index)
        target = slot.adjusted(3, 3, -3, -3)
        self._anim.stop()
        if animate and self._indicator.geometry().width() > 0:
            self._anim.setStartValue(self._indicator.geometry())
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._indicator.setGeometry(target)
        self._indicator.lower()  # 滑块永远垫在文字下面
        for btn in self._buttons:
            btn.raise_()

    # ------------------------------------------------------------------ 皮肤
    def apply_palette(self, p) -> None:
        self._palette = p
        self._apply_palette_impl()
        self.set_selected_styles()

    def _apply_palette_impl(self) -> None:
        p = self._palette
        if p is None:
            return
        radius = max(6, (self._height - 6) // 2)
        self.setStyleSheet(
            f"#segTrack {{ background-color: {p.track};"
            f" border-radius: {self._height // 2}px; }}"
        )
        self._indicator.setStyleSheet(
            f"#indicator {{ background-color: {p.accent}; border-radius: {radius}px; }}"
        )
        self.set_selected_styles()

    def _button_qss(self, selected: bool) -> str:
        p = self._palette
        if p is None:
            return ""
        size = 11 if self._compact else 13
        if selected:
            color, weight = p.accent_on, 700
        else:
            color, weight = p.muted, 500
        return (
            f"QToolButton {{ background: transparent; border: none; color: {color};"
            f" font-size: {size}px; font-weight: {weight}; }}"
            f"QToolButton:hover {{ color: {p.text if not selected else p.accent_on}; }}"
        )

    def set_selected_styles(self) -> None:
        for i, btn in enumerate(self._buttons):
            btn.setStyleSheet(self._button_qss(i == self._index))


# ---------------------------------------------------------------------------
# 提示气泡
# ---------------------------------------------------------------------------
class ToggleSwitch(QWidget):
    """iOS 风格的开关：旋钮滑过去 + 轨道颜色渐变，两件事都走动画。

    为什么自绘、不用 QSS 的 ``:checked`` 静态样式：用户点下去的那一下要有"跟手"的过渡，
    静态换色会显得很硬；而且自绘才能让颜色跟着主题走（QSS 的勾选态没法插值）。
    关掉全局动效时直接落终态 —— 界面语义不变，只是没有过渡。
    """

    toggled = pyqtSignal(bool)

    TRACK_W = 46
    TRACK_H = 25
    KNOB_PAD = 3
    ANIM_MS = 260          # 原来 180ms + OutCubic，切换"啪"一下就完了，看着生硬
    STRETCH = 5.0          # 滑动途中旋钮最多拉长几像素（iOS 的手感就在这儿）

    def __init__(self, checked: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("toggleSwitch")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # 点完别把焦点从输入框抢走
        self.setFixedSize(self.TRACK_W, self.TRACK_H)
        self._checked = bool(checked)
        self._progress = 1.0 if self._checked else 0.0
        self._palette = None
        self._anim = QPropertyAnimation(self, b"progressValue", self)
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setToolTip("开" if self._checked else "关")

    # ------------------------------------------------------------------ 状态
    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool, *, animate: bool = True,
                    notify: bool = False) -> None:
        """设状态；``notify=False`` 用于"别处改了、这边跟着同步"（避免信号回环）。"""
        value = bool(checked)
        changed = value != self._checked
        self._checked = value
        self.setToolTip("开" if value else "关")
        if not animate or not anim.is_enabled():
            self._anim.stop()
            self._progress = 1.0 if value else 0.0
            self.update()
        else:
            self._anim.stop()
            self._anim.setStartValue(self._progress)
            self._anim.setEndValue(1.0 if value else 0.0)
            self._anim.start()
        if changed and notify:
            self.toggled.emit(value)

    def toggle(self) -> None:
        """用户点了一下：翻转状态并**发信号**（同步用的 set_checked 默认不发）。"""
        self.set_checked(not self._checked, notify=True)

    def progress(self) -> float:
        """0 = 关，1 = 开（动画期间是中间值，测试看它有没有真的在动）。"""
        return self._progress

    def set_progress_value(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    progressValue = pyqtProperty(float, fget=progress, fset=set_progress_value)

    def is_animating(self) -> bool:
        return self._anim.state() == QPropertyAnimation.State.Running

    def phase(self) -> float:
        """这一趟动画走到哪儿了（0→1；没在动就是 0）。"""
        if not self.is_animating():
            return 0.0
        duration = max(1, self._anim.duration())
        return max(0.0, min(1.0, self._anim.currentTime() / duration))

    def knob_stretch(self) -> float:
        """旋钮当前该拉长多少像素：正弦包络，中段最大、两头归零。

        这样画出来是"起步鼓一下、落定前收回去"，而不是从头到尾一个胖椭圆。
        """
        import math

        return self.STRETCH * math.sin(math.pi * self.phase())

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.update()

    # ------------------------------------------------------------------ 绘制
    def track_color(self) -> QColor:
        """轨道当前颜色（关=底槽色，开=主题色，动画期间是插值）。

        关态不能直接用 ``palette.track``：它在亮色下和窗口底色几乎一样
        （实测 track=#e8edf3、surface=#E9EEF4，只差 1 个亮度），开关就剩一个白圆浮在那儿。
        所以往 ``muted``（文字灰）靠 22% 再描一圈边（见 :meth:`paintEvent`），
        两种主题下都立得住。
        """
        p = self._palette
        base = QColor(p.track if p else "#EEF2F7")
        if p is not None:
            base = mix_color(base, QColor(p.muted), 0.22)
        on = QColor(p.accent if p else "#3390EC")
        return mix_color(base, on, self._progress)

    def track_border(self) -> QColor:
        """关态时轨道的那圈描边颜色（开态不描，颜色本身就够醒目）。"""
        p = self._palette
        if p is None:
            return QColor("#D7DEE7")
        return QColor(p.border)

    def knob_color(self) -> QColor:
        """旋钮恒为白色（和 iPhone 一样）。

        第一版是"关态用卡片色"，结果暗色主题下旋钮（#1E2A38）和轨道（#202B36）几乎同一个
        颜色，开关看起来就是一块实心胶囊 —— 截图里一眼就看出来了。白色 + 关态加一圈描边，
        两种主题下都清楚。
        """
        return QColor("#FFFFFF")

    def paintEvent(self, event) -> None:                 # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = self.height() / 2
        if self._progress < 0.99:                        # 关态：轨道描一圈边
            painter.setPen(QPen(self.track_border(), 1))
            painter.setBrush(self.track_color())
            painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), radius, radius)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.track_color())
            painter.drawRoundedRect(self.rect(), radius, radius)

        diameter = self.height() - self.KNOB_PAD * 2
        left = self.KNOB_PAD
        right = self.width() - self.KNOB_PAD - diameter
        # 旋钮以"中心"定位，滑动途中沿运动方向拉长（拉长量对称摊在两侧）
        width = diameter + self.knob_stretch()
        center = lerp(left + diameter / 2, right + diameter / 2, self._progress)
        x = int(round(center - width / 2))
        painter.setBrush(self.knob_color())
        if self._palette is not None and self._progress < 0.99:
            painter.setPen(QColor(self._palette.border))     # 关态给旋钮一圈描边
        painter.drawEllipse(x, self.KNOB_PAD, int(round(width)), diameter)
        painter.end()

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.toggle()
        super().mouseReleaseEvent(event)

    def sizeHint(self) -> QSize:                         # noqa: N802
        return QSize(self.TRACK_W, self.TRACK_H)


class Toast(QFrame):
    """浮在窗口底部的提示气泡：淡入 + 上滑，超时自动消失，不参与布局。"""

    ICONS = {"success": "✓", "error": "✕", "warning": "⚠", "info": "ℹ"}
    FADE_MS = 170

    def __init__(self, parent: QWidget, *, bottom_offset: int = 0):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._palette = None
        self._message = ""
        self._kind = "info"
        # 贴底时再抬高多少像素（聊天主窗口用它避开发送栏，别盖住正在输入的字）
        self._bottom_offset = bottom_offset

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 9, 18, 9)
        row.setSpacing(8)
        self._icon = QLabel("", self)
        self._text = QLabel("", self)
        row.addWidget(self._icon)
        row.addWidget(self._text)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(self.FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(self.FADE_MS + 40)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_message)
        self._fade.finished.connect(self._on_faded)   # 只连一次，避免反复 connect/disconnect
        self._hiding = False
        self._style_key: tuple[str, str] | None = None
        self.hide()

    # ------------------------------------------------------------------ 对外
    def message(self) -> str:
        return self._message

    def show_message(self, text: str, kind: str = "info", duration: int = 2400) -> None:
        self._message = text
        self._kind = kind
        self._apply_style()
        self._icon.setText(self.ICONS.get(kind, "ℹ"))
        self._text.setText(text)

        self._hiding = False
        self._timer.stop()
        self.adjustSize()
        target = self._target_pos()
        self.move(target)
        self.show()
        self.raise_()
        self._fade.stop()
        self._slide.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(1.0)
        self._slide.setStartValue(target + _shift(0, 10))
        self._slide.setEndValue(target)
        self._fade.start()
        self._slide.start()
        self._timer.start(duration)

    def hide_message(self) -> None:
        if self._hiding or not self.isVisible():
            return
        self._hiding = True
        self._timer.stop()
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_faded(self) -> None:
        if self._hiding:
            self._hiding = False
            self.hide()

    def reposition(self) -> None:
        """窗口尺寸变化时重新贴底居中。"""
        if not self.isVisible():
            return
        if self._hiding:
            self._hiding = False
            self._timer.stop()
            self.raise_()
        self.move(self._target_pos())

    # ------------------------------------------------------------------ 内部
    def _target_pos(self):
        parent = self.parentWidget()
        self.adjustSize()
        return _shift(
            (parent.width() - self.width()) // 2,
            parent.height() - self.height() - 22 - self._bottom_offset,
        )

    def apply_palette(self, p) -> None:
        self._palette = p
        self._style_key = None  # 主题变了，样式需要重算
        if self.isVisible():
            self._apply_style()

    def _apply_style(self) -> None:
        """同一种提示反复出现时不必重复解析 QSS。"""
        p = self._palette
        if p is None:
            return
        key = (self._kind, p.key)
        if key == self._style_key:
            return
        self._style_key = key
        accent = {"success": p.success, "error": p.danger,
                  "warning": p.warning, "info": p.accent}.get(self._kind, p.accent)
        self._icon.setStyleSheet(
            f"color: {accent}; font-size: 13px; font-weight: 700; background: transparent;"
        )
        self._text.setStyleSheet(
            f"color: {p.toast_text}; font-size: 12.5px; font-weight: 600; background: transparent;"
        )
        self.setStyleSheet(f"#toast {{ background-color: {p.toast_bg}; border-radius: 12px; }}")


def _shift(x: int, y: int):
    return QPoint(x, y)


# ---------------------------------------------------------------------------
# 折叠容器
# ---------------------------------------------------------------------------
class CollapsibleSection(QWidget):
    """以"高度 + 透明度"双动画展开/收起的容器（用于按算法显隐密钥区）。"""

    def __init__(self, parent: QWidget | None = None, *, duration: int = 200):
        super().__init__(parent)
        self._expanded = True
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._inner = QWidget(self)
        outer.addWidget(self._inner)

        self._inner_lay = QVBoxLayout(self._inner)
        self._inner_lay.setContentsMargins(0, 0, 0, 0)
        self._inner_lay.setSpacing(8)

        self._effect = QGraphicsOpacityEffect(self._inner)
        self._effect.setOpacity(1.0)
        self._inner.setGraphicsEffect(self._effect)

        self._height_anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._height_anim.setDuration(duration)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._height_anim.finished.connect(self._on_height_done)

        self._fade_anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_anim.setDuration(duration)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def content_layout(self) -> QVBoxLayout:
        return self._inner_lay

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        if expanded == self._expanded and (expanded or self.maximumHeight() == 0):
            return
        self._expanded = expanded
        target = self._inner.sizeHint().height() if expanded else 0
        if not animate:
            self._height_anim.stop()
            self._fade_anim.stop()
            self.setMaximumHeight(target)
            self._effect.setOpacity(1.0 if expanded else 0.0)
            self._inner.setVisible(expanded)
            return
        self._inner.setVisible(True)
        self._height_anim.stop()
        self._fade_anim.stop()
        start = self.maximumHeight() if self.maximumHeight() > 0 else 0
        self._height_anim.setStartValue(start)
        self._height_anim.setEndValue(target)
        self._fade_anim.setStartValue(self._effect.opacity())
        self._fade_anim.setEndValue(1.0 if expanded else 0.0)
        self._height_anim.start()
        self._fade_anim.start()

    def refresh_height(self) -> None:
        """内容尺寸变化（例如子控件显隐）后同步高度。"""
        if self._expanded:
            self.setMaximumHeight(self._inner.sizeHint().height())

    def _on_height_done(self) -> None:
        if not self._expanded:
            self._inner.setVisible(False)


# ---------------------------------------------------------------------------
# 窗口自绘外壳（无边框 + 自绘标题条 + 八边缩放）
# ---------------------------------------------------------------------------
CHROME_H = 32                      # 自绘标题条的高度

#: 普通对话框只留关闭；主窗口三个都要
CHROME_CLOSE = ("close",)
CHROME_WINDOW = ("minimize", "maximize", "close")

_CHROME_ICONS = {"close": "win_close", "minimize": "win_minimize",
                 "maximize": "win_maximize"}
_CHROME_TIPS = {"close": "关闭", "minimize": "最小化", "maximize": "最大化"}


class WindowChrome(QFrame):
    """窗口自绘标题条：一条能拖的横杠 + 右边一排"最小化 / 最大化 / 关闭"。

    为什么要它：应用内所有窗口都用**自绘的关闭按钮**，不要系统标题栏那套 ✕
    （观感统一，配色也跟着主题走）。窗口因此是**无边框**的（``FramelessWindowHint``），
    拖动和缩放得自己补：``startSystemMove()`` / ``startSystemResize()`` 在 Windows 上
    走系统原生实现 —— Aero 贴边、双击最大化、Win+方向键都还在，不是"自己搬窗口"。

    对话框不装最小化/最大化（它们本来就固定尺寸），只留一个 ✕。

    尺寸**完全交给布局**（由 :func:`install_app_chrome` 摆进根布局第一格）。
    注意这里**没有事件过滤器**跟窗口大小 —— 那是踩过坑的写法，见 install_app_chrome。
    """

    def __init__(self, window, *, title: str = "", controls=CHROME_CLOSE, parent=None):
        super().__init__(parent if parent is not None else window)
        self._window = window
        self.setObjectName("chrome")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(CHROME_H)
        self._palette = None
        self.moves = 0                 # 拖动次数（测试用：验"拖动走的是系统原生那条路"）

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 8, 0)
        row.setSpacing(6)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("chromeTitle")
        self.title_label.setVisible(bool(title))
        row.addWidget(self.title_label)
        row.addStretch(1)

        self.buttons: dict[str, IconButton] = {}
        for name in controls:
            button = IconButton(_CHROME_ICONS[name], self, size=11, box=28,
                                tooltip=_CHROME_TIPS[name], object_name="chromeBtn")
            button.clicked.connect(lambda _=False, action=name: self.trigger(action))
            row.addWidget(button)
            self.buttons[name] = button
        if "close" in self.buttons:
            self.buttons["close"].setObjectName("chromeCloseBtn")   # 悬停变色（见 QSS）

    # ------------------------------------------------------------------ 对外
    def title_text(self) -> str:
        return self.title_label.text()

    def button(self, name: str) -> IconButton | None:
        return self.buttons.get(name)

    def trigger(self, name: str) -> None:
        """按钮动作。关 = ``window.close()``（主窗口那套"收进托盘"逻辑照旧拦得住）。"""
        if name == "close":
            self._window.close()
        elif name == "minimize":
            self._window.showMinimized()
        elif name == "maximize":
            self.toggle_maximized()

    def toggle_maximized(self) -> None:
        window = self._window
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        self.sync_maximize_button()

    def sync_maximize_button(self) -> None:
        """最大化/还原：同一个按钮换图标 + 换提示（系统标题栏也是这么做的）。"""
        button = self.buttons.get("maximize")
        if button is None:
            return
        maximized = bool(self._window.isMaximized())
        button.set_icon_kind("win_restore" if maximized else "win_maximize")
        button.setToolTip("还原" if maximized else "最大化")

    def apply_palette(self, palette) -> None:
        self._palette = palette
        color = getattr(palette, "muted", "#8A93A3")
        for button in self.buttons.values():
            button.set_icon_color(color)
        self.sync_maximize_button()

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event) -> None:            # noqa: N802
        """按住标题条拖动：交给系统原生拖动（贴边/双击还原都还在）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                self.moves += 1
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:      # noqa: N802
        if "maximize" in self.buttons and event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def install_app_chrome(window, *, title: str = "", controls=CHROME_CLOSE) -> WindowChrome:
    """给窗口装上"无边框 + 自绘标题条"，返回那个 chrome（调用方留着上色、测试要用）。

    chrome 摆进**根布局的第一格**：根布局原有的留白（卡片边距）整块搬进一个内容容器，
    所以标题条能铺满窗口整宽、内容该留多少白还是多少白。

    为什么不是"chrome 浮在窗口顶上 + 事件过滤器跟着改大小"（那是第一版写法，能跑）：
    过滤器在窗口的 ``Resize``/``Show`` 事件**投递过程中**调 ``setGeometry()`` 会炸 ——
    连跑 20 轮"建一个图片查看器再销毁"，第 2 轮**硬崩**（没有 Python 栈、退出码还是
    莫名其妙的 127）。改成布局之后 20/20 轮干净通过。**别再改回过滤器那套。**
    """
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    chrome = WindowChrome(window, title=title, controls=controls)
    old = window.layout()
    if old is None:                        # 还没建根布局：先只把标题条摆上
        outer = QVBoxLayout(window)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(chrome)
        return chrome
    content = QWidget(window)              # 老布局整块搬进内容容器（Qt 允许这么搬家）
    content.setLayout(old)
    outer = QVBoxLayout(window)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    outer.addWidget(chrome)
    outer.addWidget(content, 1)
    return chrome


#: 哪几条边（纯 int 位标记）。**别拿 Qt.Edge 做算术**：PyQt6 的 Qt.Edge 成员不吃
#: ``int()``（``int(Qt.Edge.LeftEdge)`` 直接 TypeError），混着用必踩坑。
EDGE_LEFT, EDGE_RIGHT, EDGE_TOP, EDGE_BOTTOM = 1, 2, 4, 8

_EDGE_FLAGS = ((EDGE_LEFT, Qt.Edge.LeftEdge), (EDGE_RIGHT, Qt.Edge.RightEdge),
               (EDGE_TOP, Qt.Edge.TopEdge), (EDGE_BOTTOM, Qt.Edge.BottomEdge))


def _edge_flags(bits: int):
    """位标记 → ``Qt.Edge``（只有交给 ``startSystemResize()`` 时才需要转回去）。"""
    flags = Qt.Edge(0)
    for bit, flag in _EDGE_FLAGS:
        if bits & bit:
            flags |= flag
    return flags


class FramelessResizer(QObject):
    """无边框窗口的八边缩放（主窗口用）。

    无边框之后系统不再给缩放边框，所以：鼠标靠近窗口边缘 → 换成对应的缩放光标，
    按下 → ``startSystemResize(edges)`` 把缩放交回系统（原生、跟手）。
    对话框不需要它（固定尺寸），主窗口没有它就没法拉大小。

    为什么挂在 ``QApplication`` 上：鼠标压在子控件（侧栏、消息区）上时事件是发给那些
    子控件的，窗口自己收不到 MouseMove —— 想知道"光标离边缘多近"只能监听全应用的
    鼠标事件再换算（本仓库的批量扫选用的也是这一招）。
    """

    MARGIN = 6

    def __init__(self, window, *, margin: int = MARGIN):
        super().__init__(window)
        self.window = window
        self.margin = int(margin)
        self._app = None
        self._active = False

    # ------------------------------------------------------------------ 生命周期
    def attach(self) -> None:
        from PyQt6.QtWidgets import QApplication
        if self._app is None:
            self._app = QApplication.instance()
        if self._app is not None and not self._active:
            self._app.installEventFilter(self)
            self._active = True
            # 窗口销毁时**必须**摘掉这个全应用过滤器：留着就是个悬空指针，
            # 下一次有人给应用送鼠标/键盘事件时就崩（现场：随机崩在某个用例里，
            # 没有 Python 栈、退出码还莫名其妙）。
            try:
                self.window.destroyed.connect(self._on_window_destroyed)
            except (AttributeError, TypeError):
                pass

    def _on_window_destroyed(self, *_args) -> None:
        self.detach()

    def detach(self) -> None:
        if self._app is not None and self._active:
            try:
                self._app.removeEventFilter(self)
            except (RuntimeError, TypeError, AttributeError):
                pass                        # 窗口/应用已销毁：Qt 拒绝把死对象转成 C++ 指针
            self._active = False

    def is_attached(self) -> bool:
        return self._active

    # ------------------------------------------------------------------ 判定
    def edges_at(self, local: QPoint) -> int:
        """窗口内坐标 ``local`` 压在哪几条边上（位标记，0 = 不在边缘，不该改光标）。"""
        bits = 0
        rect = self.window.rect()
        if not rect.contains(local):
            return 0
        if local.x() <= self.margin:
            bits |= EDGE_LEFT
        if local.x() >= rect.width() - self.margin:
            bits |= EDGE_RIGHT
        if local.y() <= self.margin:
            bits |= EDGE_TOP
        if local.y() >= rect.height() - self.margin:
            bits |= EDGE_BOTTOM
        return bits

    def cursor_for(self, bits: int):
        """边角 ↔ 缩放光标（对角线用斜向的，四条边用横竖的）；不在边缘给 None。"""
        if not bits:
            return None
        if bits in (EDGE_LEFT | EDGE_TOP, EDGE_RIGHT | EDGE_BOTTOM):
            return Qt.CursorShape.SizeFDiagCursor            # ↖↘ 那条斜线
        if bits in (EDGE_RIGHT | EDGE_TOP, EDGE_LEFT | EDGE_BOTTOM):
            return Qt.CursorShape.SizeBDiagCursor            # ↗↙
        if bits & (EDGE_LEFT | EDGE_RIGHT):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    # ------------------------------------------------------------------ 事件
    def _widget_under(self, global_point):
        from PyQt6.QtWidgets import QApplication
        target = QApplication.widgetAt(global_point)
        return None if target is None else target.window()

    def eventFilter(self, obj, event) -> bool:           # noqa: N802
        """**只看不吞**（除边缘按下那一下，一律返回 False）：不影响点击、拖动、选文字。

        这个过滤器跑在**全应用的每一次鼠标移动**上，一旦抛异常就是刷屏 + 交互变卡，
        所以宁可吞掉（真出错由 tests/test_window_chrome.py 那组用例兜住）。
        """
        if not self._active:
            return False
        window = self.window
        try:
            if window is None or not window.isVisible():
                return False
            if self._widget_under(event.globalPosition().toPoint()) is not window:
                return False                    # 鼠标其实压在别的窗口（对话框）上
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                bits = self.edges_at(window.mapFromGlobal(
                    event.globalPosition().toPoint()))
                want = self.cursor_for(bits)
                current = window.cursor().shape()
                if want is None:
                    if current != Qt.CursorShape.ArrowCursor:
                        window.unsetCursor()
                elif current != want:
                    # **只在真的变了才设**：这个过滤器跑在每一次鼠标移动上，
                    # 每次都去改窗口状态是给自己找麻烦（Qt 的事件投递里改状态，
                    # 改重了就是硬崩 —— 同页 _follow_window 那个坑的教训）。
                    window.setCursor(want)
            elif (kind == QEvent.Type.MouseButtonPress
                  and event.button() == Qt.MouseButton.LeftButton):
                bits = self.edges_at(window.mapFromGlobal(
                    event.globalPosition().toPoint()))
                if bits:
                    handle = window.windowHandle()
                    if handle is not None:
                        handle.startSystemResize(_edge_flags(bits))
                        return True      # 这一下必须吞掉：不然标题条会同时开始拖动
        except Exception:                        # noqa: BLE001 —— 见 docstring
            return False

        return False


# ---------------------------------------------------------------------------
# 左右分界线
# ---------------------------------------------------------------------------
class SplitHandle(QWidget):
    """侧栏与聊天区之间的可拖动分界线（双击还原默认宽度）。

    自己画，不进全局 QSS：底色与侧栏一致（看着像侧栏一直延伸到分隔线），
    悬停时中缝变主题色、拖动时中缝加粗并铺一层浅主题底色。切换主题只要
    重新注入一次颜色令牌即可，不重建样式表。

    宽度只由外部（主窗口）决定：组件只把"光标期望的宽度"发出去，
    不做任何布局计算——这样夹取范围、持久化都集中在一处。
    """

    moved = pyqtSignal(int)         # 拖动中：希望的侧栏宽度（未夹取）
    resetRequested = pyqtSignal()   # 双击：还原默认宽度

    HANDLE_WIDTH = 7                # 可抓取的横向范围（细了不好点）

    def __init__(self, parent: QWidget | None = None, *, sidebar_width: int,
                 min_width: int, max_width: int):
        super().__init__(parent)
        self.setObjectName("splitHandle")
        self.setFixedWidth(self.HANDLE_WIDTH)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setToolTip("拖动可改变会话列表宽度（双击还原）")
        self._palette = None
        self._width = int(sidebar_width)
        self._min = int(min_width)
        self._max = int(max_width)
        self._dragging = False
        self._hover = False
        self._press_x = 0
        self._press_width = 0

    # ------------------------------------------------------------------ 对外
    def sidebar_width(self) -> int:
        return self._width

    def set_sidebar_width(self, width: int) -> None:
        """记录当前侧栏宽度（只用于下一次拖动的基准与绘制）。"""
        self._width = max(self._min, min(self._max, int(width)))
        self.update()

    def is_dragging(self) -> bool:
        return self._dragging

    def is_hovered(self) -> bool:
        return self._hover

    def apply_palette(self, p) -> None:
        self._palette = p
        self.update()

    # ------------------------------------------------------------------ 交互
    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # 记下按下时的全局横坐标与宽度：之后按位移增量算新宽度，
            # 不依赖事件本身落在分界线的哪一像素上，拖动不会有"跳一下"。
            self._dragging = True
            self._press_x = int(event.globalPosition().x())
            self._press_width = self._width
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:             # noqa: N802
        if self._dragging:
            delta = int(event.globalPosition().x()) - self._press_x
            self.moved.emit(self._press_width + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:      # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.resetRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event) -> None:                 # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:                 # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:                 # noqa: N802
        p = self._palette
        if p is None:
            return
        if self._dragging:
            background, line, thickness = p.accent_soft, p.accent, 2
        elif self._hover:
            background, line, thickness = p.sidebar_hover, p.accent, 1
        else:
            background, line, thickness = p.sidebar, p.border, 1

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(background))
        x = (self.width() - thickness) // 2      # 1px 时偏左一格，避免半像素发虚
        painter.fillRect(QRect(x, 0, thickness, self.height()), QColor(line))
