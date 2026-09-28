"""矢量图标：全部用 QPainter 现画，不引入图片资源。

好处是图标颜色能跟着主题变（重新生成 QIcon 即可），也不会因为缩放变糊：
每个图标都在"单位方格"（0..1）里定义几何，再按目标尺寸缩放绘制。
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QToolButton

__all__ = ["make_icon", "IconButton"]

# 描边宽度（单位方格里的比例，乘上尺寸就是像素）
STROKE = 0.09


def _pen(painter: QPainter, color: QColor, width: float = STROKE) -> None:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _dot(painter: QPainter, x: float, y: float, r: float) -> None:
    painter.drawEllipse(QPointF(x, y), r, r)


def _search(painter: QPainter, color: QColor) -> None:
    _pen(painter, color)
    painter.drawEllipse(QPointF(0.44, 0.44), 0.28, 0.28)
    painter.drawLine(QPointF(0.65, 0.65), QPointF(0.87, 0.87))


def _send(painter: QPainter, color: QColor) -> None:
    """纸飞机：两片机翼拼成的箭头。"""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    wing_top = QPainterPath()
    wing_top.moveTo(0.06, 0.50)
    wing_top.lineTo(0.94, 0.08)
    wing_top.lineTo(0.66, 0.50)
    wing_top.closeSubpath()
    wing_bottom = QPainterPath()
    wing_bottom.moveTo(0.06, 0.50)
    wing_bottom.lineTo(0.94, 0.92)
    wing_bottom.lineTo(0.66, 0.50)
    wing_bottom.closeSubpath()
    painter.drawPath(wing_top)
    painter.drawPath(wing_bottom)


def _apps(painter: QPainter, color: QColor) -> None:
    """2×2 圆角方块：代表"工具集合"。"""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for x, y in ((0.13, 0.13), (0.55, 0.13), (0.13, 0.55), (0.55, 0.55)):
        painter.drawRoundedRect(QRectF(x, y, 0.32, 0.32), 0.09, 0.09)


def _more(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for y in (0.18, 0.5, 0.82):
        _dot(painter, 0.5, y, 0.085)


def _plus(painter: QPainter, color: QColor) -> None:
    _pen(painter, color, 0.11)
    painter.drawLine(QPointF(0.16, 0.5), QPointF(0.84, 0.5))
    painter.drawLine(QPointF(0.5, 0.16), QPointF(0.5, 0.84))


def _smile(painter: QPainter, color: QColor) -> None:
    _pen(painter, color, 0.08)
    painter.drawEllipse(QPointF(0.5, 0.5), 0.39, 0.39)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    _dot(painter, 0.36, 0.41, 0.065)
    _dot(painter, 0.64, 0.41, 0.065)
    _pen(painter, color, 0.075)
    painter.drawArc(QRectF(0.30, 0.34, 0.40, 0.36), 200 * 16, 140 * 16)


def _check(painter: QPainter, color: QColor) -> None:
    _pen(painter, color, 0.105)
    painter.drawLine(QPointF(0.16, 0.54), QPointF(0.38, 0.76))
    painter.drawLine(QPointF(0.38, 0.76), QPointF(0.84, 0.24))


def _double_check(painter: QPainter, color: QColor) -> None:
    _pen(painter, color, 0.105)
    painter.drawLine(QPointF(0.06, 0.54), QPointF(0.28, 0.76))
    painter.drawLine(QPointF(0.28, 0.76), QPointF(0.72, 0.24))
    painter.drawLine(QPointF(0.36, 0.54), QPointF(0.58, 0.76))
    painter.drawLine(QPointF(0.58, 0.76), QPointF(0.96, 0.32))


_PAINTERS = {
    "search": _search,
    "send": _send,
    "apps": _apps,
    "more": _more,
    "plus": _plus,
    "smile": _smile,
    "check": _check,
    "double_check": _double_check,
}


def make_icon(kind: str, color: str | QColor, size: int = 20, *,
              ratio: float = 2.0) -> QIcon:
    """画一个图标。``ratio`` 是超采样倍数，高分屏下不发虚。"""
    draw = _PAINTERS.get(kind)
    if draw is None:
        return QIcon()
    px = int(size * ratio)
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(px, px)                      # 之后都在 0..1 的单位方格作画
    draw(painter, QColor(color))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


class IconButton(QToolButton):
    """纯图标按钮：颜色随主题重新生成图标。"""

    def __init__(self, kind: str, parent=None, *, size: int = 20, box: int = 34,
                 tooltip: str = "", object_name: str = "iconBtn",
                 color: str = "#000000", fixed_box: bool = True):
        super().__init__(parent)
        self._kind = kind
        self._size = size
        self._color = color
        self.setObjectName(object_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setIconSize(QSize(size, size))
        if fixed_box:
            self.setFixedSize(box, box)
        if tooltip:
            self.setToolTip(tooltip)
        self.set_icon_color(color)

    def set_icon_color(self, color: str) -> None:
        self._color = color
        self.setIcon(make_icon(self._kind, color, self._size))

    def icon_kind(self) -> str:
        return self._kind
