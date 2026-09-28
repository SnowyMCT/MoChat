"""圆形头像：渐变底 + 名字首字（Telegram 的做法），在线状态用小绿点。"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget

__all__ = ["Avatar", "avatar_colors"]

# 七组 Telegram 风格的渐变，按名字稳定取色，同一个人每次都是同一套
GRADIENTS: tuple[tuple[str, str], ...] = (
    ("#FF885E", "#FF516A"),
    ("#FFCD6A", "#FFA85C"),
    ("#82B1FF", "#665FFF"),
    ("#A0DE7E", "#54CB68"),
    ("#53EDD6", "#28C9B7"),
    ("#72D5FD", "#2A9EF1"),
    ("#E0A2F3", "#D669ED"),
)


def avatar_colors(seed: str) -> tuple[str, str]:
    """由名字稳定地挑一组渐变色（不随机，列表刷新不会变色）。"""
    text = (seed or "?").strip() or "?"
    index = sum(ord(ch) for ch in text) % len(GRADIENTS)
    return GRADIENTS[index]


class Avatar(QWidget):
    """固定尺寸的圆形头像控件。"""

    def __init__(self, parent=None, *, size: int = 46, name: str = "",
                 online: bool = False):
        super().__init__(parent)
        self._size = size
        self._name = name
        self._online = online
        self._online_color = "#4FD16F"
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------------ 对外
    def set_name(self, name: str, online: bool | None = None) -> None:
        self._name = name
        if online is not None:
            self._online = online
        self.update()

    def set_online(self, online: bool) -> None:
        self._online = online
        self.update()

    def name(self) -> str:
        return self._name

    def apply_palette(self, palette) -> None:
        self._online_color = palette.success
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:                 # noqa: N802 (Qt 命名)
        size = self._size
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        first, second = avatar_colors(self._name)
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0.0, QColor(first))
        gradient.setColorAt(1.0, QColor(second))

        path = QPainterPath()
        path.addEllipse(QRectF(0.5, 0.5, size - 1.0, size - 1.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(path)

        initial = (self._name.strip()[:1] or "?").upper()
        font = QFont(self.font())
        font.setPixelSize(max(10, int(size * 0.42)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, initial)

        if self._online:
            dot = max(8.0, size * 0.24)
            center = QPointF(size - dot / 2 - 1.0, size - dot / 2 - 1.0)
            painter.setBrush(QColor(self._online_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, dot / 2, dot / 2)
        painter.end()
