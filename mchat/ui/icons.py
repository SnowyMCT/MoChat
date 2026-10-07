"""矢量图标：全部用 QPainter 现画，不引入图片资源。

好处是图标颜色能跟着主题变（重新生成 QIcon 即可），也不会因为缩放变糊：
每个图标都在"单位方格"（0..1）里定义几何，再按目标尺寸缩放绘制。
"""
from __future__ import annotations

import math

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


def _plus(painter: QPainter, color: QColor) -> None:
    _pen(painter, color, 0.11)
    painter.drawLine(QPointF(0.16, 0.5), QPointF(0.84, 0.5))
    painter.drawLine(QPointF(0.5, 0.16), QPointF(0.5, 0.84))


def _exit(painter: QPainter, color: QColor) -> None:
    """退出登录：一扇门 + 一支往外走的箭头（比一个"电源"图标更像"退出账号"）。"""
    _pen(painter, color, 0.095)
    path = QPainterPath()                      # 门框：左边、上边、下边（右侧开口）
    path.moveTo(0.54, 0.14)
    path.lineTo(0.20, 0.14)
    path.lineTo(0.20, 0.86)
    path.lineTo(0.54, 0.86)
    painter.drawPath(path)
    painter.drawLine(QPointF(0.40, 0.50), QPointF(0.86, 0.50))     # 箭头杆
    painter.drawLine(QPointF(0.68, 0.32), QPointF(0.86, 0.50))     # 箭头两撇
    painter.drawLine(QPointF(0.68, 0.68), QPointF(0.86, 0.50))


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


def _clip(painter: QPainter, color: QColor) -> None:
    """回形针：外圈一条"长 U" + 里侧一条短勾（附件按钮）。"""
    _pen(painter, color, 0.085)
    path = QPainterPath()
    path.moveTo(0.30, 0.72)                    # 左下开口
    path.lineTo(0.30, 0.32)
    path.arcTo(QRectF(0.30, 0.12, 0.40, 0.40), 180.0, -180.0)   # 顶端半圆到 (0.70, 0.32)
    path.lineTo(0.70, 0.74)
    path.arcTo(QRectF(0.46, 0.56, 0.24, 0.36), 0.0, -180.0)     # 底部小回勾到 (0.46, 0.56)
    path.lineTo(0.46, 0.30)
    painter.drawPath(path)


def _file(painter: QPainter, color: QColor) -> None:
    """文档：右上角折角 + 两条正文线（文件卡片上的图标）。"""
    _pen(painter, color, 0.078)
    body = QPainterPath()
    body.moveTo(0.24, 0.12)
    body.lineTo(0.60, 0.12)
    body.lineTo(0.78, 0.32)
    body.lineTo(0.78, 0.88)
    body.lineTo(0.24, 0.88)
    body.closeSubpath()
    painter.drawPath(body)
    painter.drawLine(QPointF(0.58, 0.12), QPointF(0.58, 0.34))    # 折角
    painter.drawLine(QPointF(0.58, 0.34), QPointF(0.78, 0.34))
    _pen(painter, color, 0.062)                                   # 正文线
    painter.drawLine(QPointF(0.36, 0.52), QPointF(0.66, 0.52))
    painter.drawLine(QPointF(0.36, 0.68), QPointF(0.58, 0.68))


def _image(painter: QPainter, color: QColor) -> None:
    """相框 + 太阳 + 山（图片消息 / "选图片"那个菜单项）。"""
    _pen(painter, color, 0.078)
    painter.drawRoundedRect(QRectF(0.12, 0.18, 0.76, 0.64), 0.10, 0.10)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    _dot(painter, 0.34, 0.37, 0.055)
    hills = QPainterPath()
    hills.moveTo(0.16, 0.76)
    hills.lineTo(0.40, 0.48)
    hills.lineTo(0.58, 0.68)
    hills.lineTo(0.68, 0.58)
    hills.lineTo(0.84, 0.76)
    hills.closeSubpath()
    painter.drawPath(hills)


def _gear(painter: QPainter, color: QColor) -> None:
    """齿轮：一个圆环 + 八颗朝外的齿（设置入口）。

    比"真齿轮轮廓"省事得多，20px 下反而更清楚：圆环是齿根，短齿只要够粗就不会糊。
    """
    _pen(painter, color, 0.085)
    center = QPointF(0.5, 0.5)
    ring = 0.27
    painter.drawEllipse(center, ring, ring)
    for index in range(8):
        angle = index * math.pi / 4
        cos, sin = math.cos(angle), math.sin(angle)
        painter.drawLine(QPointF(center.x() + (ring - 0.03) * cos,
                                 center.y() + (ring - 0.03) * sin),
                         QPointF(center.x() + (ring + 0.13) * cos,
                                 center.y() + (ring + 0.13) * sin))


def _double_check(painter: QPainter, color: QColor) -> None:
    _pen(painter, color, 0.105)
    painter.drawLine(QPointF(0.06, 0.54), QPointF(0.28, 0.76))
    painter.drawLine(QPointF(0.28, 0.76), QPointF(0.72, 0.24))
    painter.drawLine(QPointF(0.36, 0.54), QPointF(0.58, 0.76))
    painter.drawLine(QPointF(0.58, 0.76), QPointF(0.96, 0.32))


def _people(painter: QPainter, color: QColor) -> None:
    """两个人（前面一个大、后面一个小的头 + 肩）—— 好友入口。

    20px 下双色不好表达，所以靠"前后错位"区分：后面的头小一点、往右上偏，
    前面的身体盖住它一部分，视觉上就是两个人而不是两个圆圈。
    """
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    _dot(painter, 0.70, 0.28, 0.135)                       # 后面那个人的头
    behind = QPainterPath()
    behind.moveTo(0.44, 0.88)
    behind.lineTo(0.44, 0.76)
    behind.arcTo(QRectF(0.44, 0.52, 0.52, 0.48), 180.0, -180.0)
    behind.lineTo(0.96, 0.88)
    behind.closeSubpath()
    painter.drawPath(behind)
    _dot(painter, 0.38, 0.32, 0.185)                       # 前面那个人的头
    front = QPainterPath()
    front.moveTo(0.06, 0.90)
    front.lineTo(0.06, 0.76)
    front.arcTo(QRectF(0.06, 0.46, 0.64, 0.60), 180.0, -180.0)
    front.lineTo(0.70, 0.90)
    front.closeSubpath()
    painter.drawPath(front)


def _win_close(painter: QPainter, color: QColor) -> None:
    """关闭：一个 ✕。"""
    _pen(painter, color, 0.10)
    painter.drawLine(QPointF(0.30, 0.30), QPointF(0.70, 0.70))
    painter.drawLine(QPointF(0.70, 0.30), QPointF(0.30, 0.70))


def _win_minimize(painter: QPainter, color: QColor) -> None:
    """最小化：一条横线。"""
    _pen(painter, color, 0.10)
    painter.drawLine(QPointF(0.28, 0.50), QPointF(0.72, 0.50))


def _win_maximize(painter: QPainter, color: QColor) -> None:
    """最大化：一个空心方框。"""
    _pen(painter, color, 0.09)
    painter.drawRect(QRectF(0.30, 0.30, 0.40, 0.40))


def _win_restore(painter: QPainter, color: QColor) -> None:
    """还原（已经最大化时）：两个错位的小方框。"""
    _pen(painter, color, 0.09)
    painter.drawRect(QRectF(0.26, 0.36, 0.36, 0.36))
    path = QPainterPath()
    path.moveTo(0.38, 0.28)                # 后面那个框只画露出来的两条边
    path.lineTo(0.74, 0.28)
    path.lineTo(0.74, 0.64)
    painter.drawPath(path)


_PAINTERS = {
    "search": _search,
    "send": _send,
    "apps": _apps,
    "plus": _plus,
    "exit": _exit,
    "smile": _smile,
    "check": _check,
    "double_check": _double_check,
    "clip": _clip,
    "file": _file,
    "image": _image,
    "gear": _gear,
    "people": _people,
    "win_close": _win_close,
    "win_minimize": _win_minimize,
    "win_maximize": _win_maximize,
    "win_restore": _win_restore,
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

    def set_icon_kind(self, kind: str) -> None:
        """换一个图标（同一个按钮在不同状态下画不同的东西：最大化 ↔ 还原）。"""
        self._kind = kind
        self.setIcon(make_icon(kind, self._color, self._size))

    def icon_kind(self) -> str:
        return self._kind
