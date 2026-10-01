"""设置区域（独立非模态窗口）：主题 / 文字大小 / 界面动效 / 数据。

设计取向
--------
* **即时生效，没有"确定/取消"**：改一下就能看见效果，才敢放心调（Telegram 就是这么做的）。
  所有选择都写进 QSettings，下次打开还在。
* **这一区域本身也带动画**（需求里明确要的）：
    1. 打开：整窗淡入 + 从下方 18px 滑上来，里面的卡片**自上而下错峰**浮现；
    2. 每张卡片：鼠标进来/出去时底色与描边**渐变过渡**（自绘插值，不是 QSS 的硬切）；
    3. 动效开关：旋钮滑过去 + 轨道颜色渐变（``widgets.ToggleSwitch``）；
    4. 主题/字号的分段控件：滑块本来就有 220ms 动画；
    5. 改文字大小：预览行的字号**动画过渡**到新档位（所见即所得）；
    6. 关闭：淡出。
* **一律尊重全局动效开关**：关掉动效时上面这些全部直接落终态（界面语义不变），
  这也是无头测试里做像素断言的开关。
"""
from __future__ import annotations

from PyQt6.QtCore import (QEasingCurve, QPropertyAnimation, QRect, Qt, pyqtProperty,
                          pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from . import anim
from . import theme
from .widgets import SegmentedControl, ToggleSwitch, mix_color

__all__ = ["SettingsDialog", "SettingsCard"]

CARD_RADIUS = 14
PREVIEW_MS = 220            # 预览文字换档位时的字号过渡
ROW_STEP_MS = 45            # 打开时各卡片的错峰间隔


class SettingsCard(QFrame):
    """一张设置卡片：标题 + 说明 +（可选）右侧控件。

    ``hoverProgress`` 从 0 渐变到 1（进入 160ms），绘制时用它插值底色与描边 ——
    所以是"渐变"而不是 QSS ``:hover`` 那种一帧到位。曲线和时长跟全局动效一套。
    """

    HOVER_MS = 160

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("settingCard")
        # 自绘背景，所以**不要**开 WA_StyledBackground（否则 QSS 会把我们画的盖掉）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._hover = 0.0
        self._palette = None
        self._anim = QPropertyAnimation(self, b"hoverProgress", self)
        self._anim.setDuration(self.HOVER_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("settingName")
        self.hint_label = QLabel(hint, self)
        self.hint_label.setObjectName("settingHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(bool(hint))

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.title_label)
        header.addStretch(1)

        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(14, 12, 14, 13)
        self._column.setSpacing(8)
        self._column.addLayout(header)
        self._column.addWidget(self.hint_label)

        self._control = QWidget(self)
        self._control_column = QVBoxLayout(self._control)
        self._control_column.setContentsMargins(0, 0, 0, 0)
        self._control_column.setSpacing(8)
        self._control.setVisible(False)
        self._column.addWidget(self._control)

    # ------------------------------------------------------------------ 内容
    def add_control(self, widget: QWidget, *, stretch: int = 0) -> None:
        """往下加一行控件（一张卡片里多数是竖着排的）。"""
        self._control_column.addWidget(widget, stretch)
        self._control.setVisible(True)

    def add_control_row(self, *widgets: QWidget, hint_stretch: int = 1) -> None:
        """一行里并排放几个控件（开关 + 说明这种）。"""
        row = QHBoxLayout()
        row.setSpacing(10)
        for index, widget in enumerate(widgets):
            row.addWidget(widget, hint_stretch if index == len(widgets) - 1 else 0)
        self._control_column.addLayout(row)
        self._control.setVisible(True)

    def add_trailing(self, widget: QWidget) -> None:
        """放到标题行右侧（比如当前值的文字）。"""
        header = self._column.itemAt(0)
        layout = header.layout()
        layout.addWidget(widget)

    def control(self) -> QWidget:
        return self._control

    # ------------------------------------------------------------------ 动画
    def hover_progress(self) -> float:
        return self._hover

    def set_hover_progress(self, value: float) -> None:
        self._hover = max(0.0, min(1.0, float(value)))
        self.update()

    hoverProgress = pyqtProperty(float, fget=hover_progress, fset=set_hover_progress)

    def is_animating(self) -> bool:
        return self._anim.state() == QPropertyAnimation.State.Running

    def _animate_hover(self, target: float) -> None:
        if not anim.is_enabled() or not self.isVisible():
            self._anim.stop()
            self.set_hover_progress(target)
            return
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, event) -> None:                 # noqa: N802
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:                 # noqa: N802
        self._animate_hover(0.0)
        super().leaveEvent(event)

    # ------------------------------------------------------------------ 绘制
    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.update()

    def background_color(self) -> QColor:
        """当前底色（测试断言"hover 真的改了颜色"用，不靠截图）。"""
        if self._palette is None:
            return QColor("#FFFFFF")
        return mix_color(QColor(self._palette.card), QColor(self._palette.track),
                         self._hover)

    def border_color(self) -> QColor:
        if self._palette is None:
            return QColor("#E4E9F0")
        return mix_color(QColor(self._palette.border),
                         QColor(self._palette.accent_soft), self._hover)

    def paintEvent(self, event) -> None:                 # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self.border_color())
        painter.setBrush(self.background_color())
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.drawRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        painter.end()


class SettingsDialog(QDialog):
    """设置窗口：把"能调的东西"集中在一处，改完立刻生效并记住。"""

    themeChanged = pyqtSignal(str)
    fontScaleChanged = pyqtSignal(float)
    animationsToggled = pyqtSignal(bool)
    notifyToggled = pyqtSignal(bool)
    soundToggled = pyqtSignal(bool)
    openDataDirRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("settingsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("设置")
        # 独立窗口（和工具箱一致）：不挡着聊天，边调边看效果
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(False)
        self.resize(440, 560)
        self._palette = None
        self._preview_px = 14.0
        self._preview_anim = QPropertyAnimation(self, b"previewFontPx", self)
        self._preview_anim.setDuration(PREVIEW_MS)
        self._preview_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)
        root.addLayout(self._build_header())
        self.cards: list[SettingsCard] = []
        for card in self._build_cards():
            self.cards.append(card)
            root.addWidget(card)
        root.addStretch(1)
        self.foot = QLabel("所有设置即时生效", self)
        self.foot.setObjectName("settingsFoot")
        root.addWidget(self.foot)

    # ================================================================== 搭建
    def _build_header(self) -> QVBoxLayout:
        self.title = QLabel("设置", self)
        self.title.setObjectName("settingsTitle")
        self.subtitle = QLabel("外观、文字大小、动效", self)
        self.subtitle.setObjectName("settingsSubtitle")
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(self.title)
        box.addWidget(self.subtitle)
        return box

    def _build_cards(self) -> list[SettingsCard]:
        # ---- 外观：主题 + 文字大小 + 预览 ----
        look = SettingsCard("外观", "主题与文字大小在全窗口立即生效")
        self.theme_control = SegmentedControl(theme.THEME_OPTIONS, height=34)
        self.theme_control.valueChanged.connect(self.themeChanged)
        self.theme_label = QLabel("", self)
        self.theme_label.setObjectName("settingValue")
        look.add_trailing(self.theme_label)
        look.add_control(self.theme_control)
        self.font_control = SegmentedControl(theme.FONT_SCALE_LABELS, height=34)
        self.font_control.valueChanged.connect(
            lambda label: self.fontScaleChanged.emit(theme.scale_of(label)))
        self.font_label = QLabel("", self)
        self.font_label.setObjectName("settingValue")
        look.add_trailing(self.font_label)
        look.add_control(self.font_control)
        self.preview = QLabel("Hello User！", self)
        self.preview.setObjectName("settingsPreview")
        self.preview.setWordWrap(True)
        look.add_control(self.preview)
        self.preview_hint = QLabel("预览", self)
        self.preview_hint.setObjectName("settingHint")
        look.add_control(self.preview_hint)

        # ---- 动效：开关 ----
        motion = SettingsCard("界面动效", "消息入场、主题交叉淡入、面板展开…")
        self.motion_switch = ToggleSwitch(True, self)
        self.motion_switch.toggled.connect(self._on_switch_toggled)
        self.motion_value = QLabel("", self)
        self.motion_value.setObjectName("settingValue")
        motion.add_trailing(self.motion_value)
        self.motion_hint = QLabel("", self)
        self.motion_hint.setObjectName("settingHint")
        self.motion_hint.setWordWrap(True)
        motion.add_control_row(self.motion_switch, self.motion_hint)

        # ---- 数据：统计 + 打开数据目录 ----
        data = SettingsCard("数据", "消息与附件记录都在这一个 SQLite 文件里，程序不联网")
        self.stats_label = QLabel("", self)
        self.stats_label.setObjectName("settingHint")
        data.add_control(self.stats_label)
        self.open_dir_btn = QPushButton("打开数据目录", self)
        self.open_dir_btn.setObjectName("ghostBtn")
        self.open_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_dir_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.open_dir_btn.clicked.connect(self.openDataDirRequested)
        self.data_path_label = QLabel("", self)
        self.data_path_label.setObjectName("settingHint")
        self.data_path_label.setWordWrap(True)
        data.add_control(self.open_dir_btn)
        data.add_control(self.data_path_label, stretch=1)

        # ---- 关于 ----
        about = SettingsCard("关于", "MChat")
        self.about_label = QLabel("", self)
        self.about_label.setObjectName("settingHint")
        self.about_label.setWordWrap(True)
        about.add_control(self.about_label)

        # ---- 消息通知：桌面通知 + 提示音 ----
        # 放在最后（而不是夹在中间）：卡片下标是既有测试的锚点，加新卡片不动老下标。
        notice = SettingsCard("消息通知", "收到新消息时弹一条系统通知 / 响一声提示音")
        self.notify_switch = ToggleSwitch(True, self)
        self.notify_switch.toggled.connect(self._on_notify_toggled)
        self.notify_value = QLabel("", self)
        self.notify_value.setObjectName("settingValue")
        notice.add_trailing(self.notify_value)
        self.notify_hint = QLabel("", self)
        self.notify_hint.setObjectName("settingHint")
        self.notify_hint.setWordWrap(True)
        notice.add_control_row(self.notify_switch, self.notify_hint)
        self.sound_switch = ToggleSwitch(True, self)
        self.sound_switch.toggled.connect(self._on_sound_toggled)
        self.sound_value = QLabel("", self)
        self.sound_value.setObjectName("settingValue")
        notice.add_trailing(self.sound_value)
        self.sound_hint = QLabel("", self)
        self.sound_hint.setObjectName("settingHint")
        self.sound_hint.setWordWrap(True)
        notice.add_control_row(self.sound_switch, self.sound_hint)
        return [look, motion, data, about, notice]

    # ================================================================== 状态同步
    def sync(self, *, theme_name: str | None = None, font_scale: float | None = None,
             animations: bool | None = None, stats: dict | None = None,
             notifications: bool | None = None, sound: bool | None = None,
             data_path: str = "", transport: str = "") -> None:
        """把主窗口的当前状态贴到控件上（都走 ``notify=False``，不回抛信号）。"""
        if theme_name is not None:
            self.theme_control.set_index(
                theme.THEME_OPTIONS.index(theme_name)
                if theme_name in theme.THEME_OPTIONS else 0,
                notify=False, animate=False)
            self.theme_label.setText(theme_name)
        if font_scale is not None:
            label = theme.label_of(font_scale)
            self.font_control.set_index(theme.FONT_SCALE_LABELS.index(label),
                                        notify=False, animate=False)
            self.font_label.setText(f"{label}（{theme.font_scale():.2f}×）"
                                    if label != "标准" else "标准（1×）")
            self._animate_preview(14.0 * theme.font_scale())
        if animations is not None:
            self.motion_switch.set_checked(animations, animate=False, notify=False)
            self.motion_value.setText("开启" if animations else "已关闭")
            self.motion_hint.setText(" "
                                     if animations else
                                     "关闭")
        if notifications is not None:
            self.notify_switch.set_checked(notifications, animate=False, notify=False)
            self.notify_value.setText("开启" if notifications else "已关闭")
            self.notify_hint.setText("新消息会弹系统通知（不看这个窗口时）"
                                     if notifications else
                                     "已关闭：收到消息不再弹系统通知")
        if sound is not None:
            self.sound_switch.set_checked(sound, animate=False, notify=False)
            self.sound_value.setText("开启" if sound else "已关闭")
            self.sound_hint.setText("新消息会响一声提示音"
                                    if sound else
                                    "已关闭：收到消息不响提示音")
        if stats is not None:
            self.stats_label.setText(
                f"消息 {stats.get('messages', 0)} 条 · 附件 {stats.get('attachments', 0)} 个 · "
                f"回应 {stats.get('reactions', 0)} 个 · "
                f"占用 {stats.get('bytes', 0) / 1024:.0f} KB")
        if data_path:
            self.data_path_label.setText(f"库文件：{data_path}")
        if transport:
            self.about_label.setText(
                f"传输层：{transport}（未联网）\n"
                "快捷键：Ctrl+, 设置 · Ctrl+T 工具箱 · Ctrl+E 表情 · Ctrl+Shift+F 搜索")

    def theme_name(self) -> str:
        return self.theme_control.value()

    def font_scale_label(self) -> str:
        return self.font_control.value()

    def animations_enabled(self) -> bool:
        return self.motion_switch.is_checked()

    def notifications_enabled(self) -> bool:
        return self.notify_switch.is_checked()

    def sound_enabled(self) -> bool:
        return self.sound_switch.is_checked()

    def card_count(self) -> int:
        return len(self.cards)

    # ================================================================== 动画
    def preview_font_px(self) -> float:
        return self._preview_px

    def set_preview_font_px(self, value: float) -> None:
        self._preview_px = float(value)
        font = QFont(self.preview.font())
        font.setPixelSize(max(8, int(round(self._preview_px))))
        self.preview.setFont(font)
        self._fit_preview()

    def _fit_preview(self) -> None:
        """把预览框撑到刚好装下内容。

        字号是代码里改的，布局不一定认账（Qt 只在 polish 时量一次），而且这段预览文字
        在这个宽度下会**换行**，所以按"换行后的真实高度"算，不要猜行数 —— 第一版直接
        设了一个"一行 + padding"的最小高度，截图里两行文字上下都被裁掉了。
        """
        metrics = QFontMetrics(self.preview.font())
        width = max(80, self.preview.width() - 24)      # 减掉 QSS 的左右 padding
        box = metrics.boundingRect(QRect(0, 0, width, 10000),
                                   int(Qt.TextFlag.TextWordWrap), self.preview.text())
        self.preview.setMinimumHeight(box.height() + 24)   # +24 = 上下 padding
        self.preview.updateGeometry()

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self._fit_preview()                             # 宽度变了要重新按换行量高度

    previewFontPx = pyqtProperty(float, fget=preview_font_px, fset=set_preview_font_px)

    def _animate_preview(self, target_px: float) -> None:
        """预览文字的字号过渡到目标值（不尊重动效开关的话就直接落终态）。"""
        if not anim.is_enabled():
            self._preview_anim.stop()
            self.set_preview_font_px(target_px)
            return
        self._preview_anim.stop()
        self._preview_anim.setStartValue(self._preview_px)
        self._preview_anim.setEndValue(target_px)
        self._preview_anim.start()

    def is_preview_animating(self) -> bool:
        return self._preview_anim.state() == QPropertyAnimation.State.Running

    def play_entrance(self) -> None:
        """打开时的入场：整窗淡入 + 上滑，卡片自上而下错峰浮现。"""
        anim.slide_in(self, offset=18, ms=anim.SLOW_MS)
        anim.stagger_fade_in(self.cards, ms=anim.NORMAL_MS, step=ROW_STEP_MS,
                             limit=len(self.cards))

    def is_entering(self) -> bool:
        """整窗的入场动画还在跑吗（测试用；结束应当摘掉透明效果）。"""
        return anim.is_animating(self)

    def close_animated(self) -> None:
        """淡出后收起来（``hide()`` 只是收尾，用户看不到中间过程）。"""
        if not self.isVisible():
            return
        anim.fade_out(self, ms=anim.FAST_MS, on_done=self.hide)

    def showEvent(self, event) -> None:                  # noqa: N802
        super().showEvent(event)
        self.play_entrance()

    def _on_switch_toggled(self, enabled: bool) -> None:
        self.motion_value.setText("已开启" if enabled else "已关闭")
        self.motion_hint.setText("消息入场、主题交叉淡入、面板展开都有过渡"
                                 if enabled else
                                 "已关掉：所有动效直接落终态（省电、也便于截图对比）")
        self.animationsToggled.emit(enabled)

    def _on_notify_toggled(self, enabled: bool) -> None:
        self.notify_value.setText("开启" if enabled else "已关闭")
        self.notify_hint.setText("新消息会弹系统通知（不看这个窗口时）"
                                 if enabled else
                                 "已关闭：收到消息不再弹系统通知")
        self.notifyToggled.emit(enabled)

    def _on_sound_toggled(self, enabled: bool) -> None:
        self.sound_value.setText("开启" if enabled else "已关闭")
        self.sound_hint.setText("新消息会响一声提示音"
                                if enabled else
                                "已关闭：收到消息不响提示音")
        self.soundToggled.emit(enabled)

    # ================================================================== 皮肤
    def apply_palette(self, palette) -> None:
        """整套样式表 + 各控件重上色（字号也在这里跟着 theme.font_scale() 走）。"""
        self._palette = palette
        self.setStyleSheet(theme.build_qss(palette))
        self.theme_control.apply_palette(palette)
        self.font_control.apply_palette(palette)
        self.motion_switch.apply_palette(palette)
        self.notify_switch.apply_palette(palette)
        self.sound_switch.apply_palette(palette)
        for card in self.cards:
            card.apply_palette(palette)
        for label in (self.foot, self.data_path_label):
            label.setStyleSheet(f"color: {palette.muted};")
