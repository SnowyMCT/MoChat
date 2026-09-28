"""动效工具箱：淡入 / 展开 / 交叉淡入 / 错峰淡入，外加"正在输入"的跳动小点。

为什么集中在一处
----------------
气泡入场、搜索结果、未读徽标、主题切换四处都要做同类动效。各写一遍必然出现四种时长、
四种缓动、以及"动画跑完了没把 QGraphicsOpacityEffect 摘掉"这类小 bug（留着它等于给
控件常驻一层离屏合成，长列表会明显变卡，也破坏像素级断言）。所以这里统一：

* 时长与缓动只有一份（``FAST/NORMAL/SLOW`` + ``OutCubic``）；
* **动画结束一律摘掉效果**（``setGraphicsEffect(None)``），稳态零开销；
* ``set_enabled(False)`` 一键全关 —— 测试要像素确定性、或将来做"减少动效"偏好时用；
* 关掉时不是"什么都不做"，而是**直接落到终态**，所以关掉动画不会改变界面语义。

两条硬坑（都踩过）
------------------
* ``QGraphicsOpacityEffect`` **别往 QScrollArea 上套**：视口会走离屏合成，滚动时出现
  拖影。要淡入就套在里面的具体子控件上（消息区因此只给新气泡做入场，不给整块列表做）。
* 在布局里动 ``pos`` 会被布局立刻改回去。想让控件"长出来"就动 ``maximumHeight``，
  并且**动画结束必须把上限恢复**成 ``UNLIMITED``，否则换行/缩放会被卡在旧高度。
"""
from __future__ import annotations

import math

from PyQt6.QtCore import (QEasingCurve, QPropertyAnimation, QTimer, Qt,
                          pyqtProperty)
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget

__all__ = ["FAST_MS", "NORMAL_MS", "SLOW_MS", "UNLIMITED", "set_enabled", "is_enabled",
           "fade_in", "stagger_fade_in", "cross_fade", "cancel",
           "TypingDots", "SnapshotOverlay"]

FAST_MS = 140          # 小东西（胶囊、结果行）
NORMAL_MS = 200        # 常规（气泡入场）
SLOW_MS = 280          # 大动作（主题交叉淡入）
EASING = QEasingCurve.Type.OutCubic
UNLIMITED = 16777215   # Qt 的 QWIDGETSIZE_MAX：maximumHeight 的"不限制"

_enabled = True


def set_enabled(enabled: bool) -> None:
    """总开关。关掉后各动效函数直接落到终态（界面语义不变，只是不再有过渡）。"""
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled


# ---------------------------------------------------------------------------
# 淡入
# ---------------------------------------------------------------------------
def _effect_for(widget: QWidget) -> QGraphicsOpacityEffect:
    effect = widget.graphicsEffect()
    if isinstance(effect, QGraphicsOpacityEffect):
        return effect                                   # 复用，别反复建
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    return effect


_FADE_ATTR = "_mchat_fade_anim"


def _detach_effect(widget: QWidget) -> None:
    """摘掉透明效果（稳态不留离屏合成）。"""
    try:
        if widget.graphicsEffect() is not None:
            widget.setGraphicsEffect(None)
    except RuntimeError:
        pass                                   # 控件已经被销毁（定时器晚了一步）


def _defer_detach(widget: QWidget) -> None:
    """推迟到下一轮事件循环再摘效果（别在动画回调里当场删）。

    实测踩过的坑，两层：

    1. 在 ``finished`` 槽里**当场** ``setGraphicsEffect(None)``，效果被立即 delete，
       Qt 转眼又进到已释放的对象上 —— Windows 上是 access violation；
    2. **``QTimer.singleShot(0, 上下文对象, lambda)`` 这个三参数重载本身就崩**
       （PyQt6 + lambda，同一条路径下 0 延时也一样崩；换成两参数版就没事）。

    所以这里用两参数 ``singleShot`` + 回调里 try/except 兜住"控件已经没了"。
    """
    QTimer.singleShot(0, lambda: _detach_effect(widget))


def cancel(widget: QWidget) -> None:
    """把控件身上的动画全停掉 —— 控件要被删/要重建之前调用。

    为什么要专门有这个：消息列表清空、搜索结果刷新都是"先删掉一批控件再重建"，
    而删除是 ``deleteLater``（下一轮才真的释放），这中间动画的 ``finished`` 回调
    可能才刚跑起来 —— 回调里再碰那个控件，PyQt6 会把 ``RuntimeError`` 直接升级成
    进程级致命错误（实测就是这么崩的）。所以删之前先把动画停干净。
    """
    cached = getattr(widget, _FADE_ATTR, None)
    if cached is not None:
        try:
            cached[1].stop()
        except RuntimeError:
            pass
        setattr(widget, _FADE_ATTR, None)
    try:
        for child in widget.findChildren(QPropertyAnimation):
            child.stop()
    except RuntimeError:
        pass
    try:
        widget.setMaximumHeight(UNLIMITED)
    except RuntimeError:
        pass


def fade_in(widget: QWidget, *, ms: int = NORMAL_MS, on_done=None):
    """淡入到完全不透明，结束后**摘掉效果**（稳态不留离屏合成）。"""
    if not _enabled:
        _detach_effect(widget)
        if on_done is not None:
            on_done()
        return None
    effect = _effect_for(widget)
    # 动画对象挂在控件上反复用（不设 DeleteWhenStopped：那会连带踩到刚删掉的效果）
    cached = getattr(widget, _FADE_ATTR, None)
    if cached is None or cached[0] is not effect:
        animation = QPropertyAnimation(effect, b"opacity", widget)
        animation.setEasingCurve(EASING)
        setattr(widget, _FADE_ATTR, (effect, animation))
    else:
        animation = cached[1]
    animation.stop()
    try:
        animation.finished.disconnect()                 # 只留本次这一个回调
    except TypeError:
        pass
    animation.setDuration(ms)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.finished.connect(lambda: (_defer_detach(widget),
                                        on_done() if on_done is not None else None))
    animation.start()
    return animation


def slide_scroll(scrollbar, offset: int, *, ms: int = NORMAL_MS + 40):
    """把滚动条从"目标值 - offset"滑到目标值 —— 让新内容**从下往上滑进视野**。

    为什么用滚动条而不是动控件几何：动高度会每帧触发一次整块列表的重新布局，
    消息还会在行里被重新居中（实测 y 在 616~628 之间来回抖），收尾恢复高度时
    再整体跳一下 —— 观感就是"卡一下"。滚动条只改一个整数，绘制层直接平移，
    全程不做布局，所以稳。

    没有可滚的余量（内容还没满一屏）时**什么都不做**：那时新消息本来就在视野里，
    硬滚反而会突然跳一下。
    """
    if scrollbar is None or not _enabled:
        return None
    target = scrollbar.maximum()
    start = target - max(1, int(offset))
    if start <= 0 or target <= 0:
        return None                                      # 没余量：不滑，直接淡入
    animation = QPropertyAnimation(scrollbar, b"value", scrollbar)
    animation.setDuration(ms)
    animation.setStartValue(start)
    animation.setEndValue(target)
    animation.setEasingCurve(EASING)

    def on_range(_minimum: int, maximum: int) -> None:
        # 追加的那一瞬间滚动范围还是旧值（布局要一拍才认出新增的高度），
        # 所以终点要跟着范围走，否则会停在"差一条消息"的位置上。
        animation.setEndValue(maximum)

    def finished() -> None:
        try:
            scrollbar.rangeChanged.disconnect(on_range)
        except TypeError:                                # pragma: no cover
            pass
        scrollbar.setValue(scrollbar.maximum())          # 收尾对齐到底（通常已经对齐了）

    scrollbar.rangeChanged.connect(on_range)
    animation.finished.connect(finished)
    scrollbar.setValue(start)
    animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


def stagger_fade_in(widgets, *, ms: int = FAST_MS, step: int = 16, limit: int = 12) -> None:
    """一排小控件依次淡入（错峰）。延迟累加上限 ``limit`` 个，60 行结果也不会等半天。"""
    if not _enabled:
        return
    for index, widget in enumerate(widgets):
        delay = min(index, limit) * step
        if delay:
            QTimer.singleShot(delay, lambda w=widget: fade_in(w, ms=ms))
        else:
            fade_in(widget, ms=ms)


# ---------------------------------------------------------------------------
# 主题交叉淡入
# ---------------------------------------------------------------------------
class SnapshotOverlay(QWidget):
    """"换主题前那一刻"的截图，盖在窗口上淡出 —— 视觉上就是两个主题的交叉淡入。"""

    def __init__(self, parent: QWidget, pixmap: QPixmap):
        super().__init__(parent)
        self._pixmap = pixmap
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(parent.rect())
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(SLOW_MS)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.finished.connect(self._finished)

    def is_fading(self) -> bool:
        return self._anim.state() == QPropertyAnimation.State.Running

    def fade_out(self, ms: int | None = None) -> None:
        if ms is not None:
            self._anim.setDuration(ms)
        self.show()
        self.raise_()
        self._anim.start()

    def _finished(self) -> None:
        # 同样别在动画回调里当场删效果/删控件，推到下一轮（见 _defer_detach 的注释）
        QTimer.singleShot(0, self._teardown)

    def _teardown(self) -> None:
        try:
            self.setGraphicsEffect(None)
            self.hide()
            self.deleteLater()
        except RuntimeError:
            pass

    def paintEvent(self, event) -> None:                 # noqa: N802
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)


def cross_fade(parent: QWidget, *, ms: int = SLOW_MS):
    """给"整块要变样"的操作做交叉淡入（主题切换）。返回叠层（测试可查），不需要时返回 None。"""
    if not _enabled or parent is None or not parent.isVisible():
        return None
    overlay = SnapshotOverlay(parent, parent.grab())
    overlay.fade_out(ms)
    return overlay


# ---------------------------------------------------------------------------
# 打字指示器
# ---------------------------------------------------------------------------
class TypingDots(QWidget):
    """三个上下跳动的小点（对端"正在输入…"）。

    自绘 + 定时器：只有可见时才跑定时器，隐藏就停 —— 聊天窗口大部分时间它是不动的，
    不该为此常驻一个 30fps 的回调。
    """

    TICK_MS = 45                  # 刷新间隔（≈22fps，够顺滑又不费电）
    PHASE_MS = 260                # 相邻两点的相位差
    DOT = 6
    GAP = 4
    LIFT = 3                      # 跳动幅度（像素）

    def __init__(self, parent=None, *, color: str = "#8A8A8A"):
        super().__init__(parent)
        self._color = QColor(color)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(self.dot_count() * self.DOT + (self.dot_count() - 1) * self.GAP,
                          self.DOT + self.LIFT)

    @staticmethod
    def dot_count() -> int:
        return 3

    # ------------------------------------------------------------------ 对外
    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def is_running(self) -> bool:
        return self._timer.isActive()

    def set_color(self, color: str | QColor) -> None:
        self._color = QColor(color)
        self.update()

    def dot_offsets(self) -> list[float]:
        """三个点当前各自的上移量（测试用：能看到它们在动）。"""
        return [self._lift_for(index) for index in range(self.dot_count())]

    def advance(self, ms: int | None = None) -> None:
        """手动推进相位（无头测试里也可以直接调，不必等定时器）。"""
        self._tick(ms or self.TICK_MS)

    # ------------------------------------------------------------------ 内部
    def get_phase(self) -> float:
        return self._phase

    def set_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()

    phase = pyqtProperty(float, fget=get_phase, fset=set_phase)

    def _lift_for(self, index: int) -> float:
        share = (self._phase + index * self.PHASE_MS / 1000.0) % 1.0
        wave = max(0.0, math.sin(share * math.pi))       # 0→1→0 的一跳
        return wave * self.LIFT

    def _tick(self, ms: int | None = None) -> None:
        step = (ms or self.TICK_MS) / 1000.0
        self._phase = (self._phase + step) % 1.0
        self.update()

    def hideEvent(self, event) -> None:                  # noqa: N802
        self.stop()                                      # 看不见就别烧 CPU
        super().hideEvent(event)

    def sizeHint(self):                                  # noqa: N802
        return self.size()

    def paintEvent(self, event) -> None:                 # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = self.DOT / 2
        for index in range(self.dot_count()):
            color = QColor(self._color)
            lift = self._lift_for(index)
            # 抬得越高越实，落回去时更淡 —— 三个点就有了"追逐"的感觉
            color.setAlpha(int(120 + 135 * (lift / self.LIFT)) if self.LIFT else 255)
            painter.setBrush(color)
            x = index * (self.DOT + self.GAP)
            painter.drawEllipse(int(x), int(self.LIFT - lift), self.DOT, self.DOT)
        painter.end()
