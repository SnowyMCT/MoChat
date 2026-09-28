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

from PyQt6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect,
                          QSize, QTimer, Qt, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PyQt6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

__all__ = ["lerp", "mix_color", "FloatingLabelInput", "SegmentedControl", "Toast",
           "CollapsibleSection", "SplitHandle", "confirm_destructive"]


def confirm_destructive(parent, palette, *, title: str, text: str,
                        ok_text: str = "确定删除") -> bool:
    """危险操作的二次确认：弹窗 + 红色按钮（删消息、清空历史这类不可撤销的动作）。

    用 `QMessageBox` 而不是自绘对话框：它有平台标准的按键/回车/Esc 行为，
    这里只把它按主题上个色，并把确认键染红。
    """
    from PyQt6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
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
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

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
        font.setPointSizeF(self.FLOAT_PT)
        font.setWeight(QFont.Weight.DemiBold)
        self._float_w = QFontMetrics(font).horizontalAdvance(self._label_text) + self.LABEL_PAD * 2 + 4

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
        font.setPointSizeF(lerp(self.INSIDE_PT, self.FLOAT_PT, t))
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
        self._apply_progress()

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
