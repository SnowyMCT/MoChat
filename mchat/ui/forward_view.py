"""「<来源> 的聊天记录」查看窗口 —— 合并转发点开之后看到的东西（QQ 那种）。

设计取向
--------
* 气泡里**只放一张紧凑卡片**（标题 + 一行摘要 + "查看 N 条转发消息"），
  内容不塞进去 —— 塞进去又长又难读，这正是这一版要改掉的。
* 真正的内容在这个独立窗口里，**按聊天气泡的样子逐条列**：头像 + 昵称 + 内容 + 时间，
  一眼就能看出"谁说了什么、什么时候说的"。
* 非模态、复用同一个窗口（和搜索/设置/工具箱/转发一致）：开着它还能继续用主窗口。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from .avatar import Avatar
from .theme import build_qss

__all__ = ["MergedViewerDialog"]


class MergedRow(QWidget):
    """一条记录：头像 + 昵称/时间 + 气泡（内容 + 可选附件行）。"""

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("mergedRow")
        self.avatar = Avatar(self, size=34, name=item.author or "?")

        self.nick_label = QLabel(item.author or "未知", self)
        self.nick_label.setObjectName("mergedNick")
        self.clock_label = QLabel(item.clock, self)
        self.clock_label.setObjectName("mergedClock")

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 2)
        head.setSpacing(6)
        head.addWidget(self.nick_label)
        head.addWidget(self.clock_label)
        head.addStretch(1)

        self.body_label = QLabel(item.text if item.text.strip() else "", self)
        self.body_label.setObjectName("mergedBody")
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)      # 能选中复制，像真的聊天记录

        bubble = QFrame(self)
        bubble.setObjectName("mergedBubble")
        bubble.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(bubble)
        column.setContentsMargins(10, 7, 10, 7)
        column.setSpacing(4)
        if item.text.strip():
            column.addWidget(self.body_label)
        if item.attachment:
            self.attach_label = QLabel(f"📎 {item.attachment}", bubble)
            self.attach_label.setObjectName("mergedAttach")
            column.addWidget(self.attach_label)
        if not item.text.strip() and not item.attachment:
            empty = QLabel("（空消息）", bubble)
            empty.setObjectName("mergedBody")
            column.addWidget(empty)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(2)
        right.addLayout(head)
        right.addWidget(bubble)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        row.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(right, 1)

    def apply_palette(self, palette) -> None:
        self.avatar.apply_palette(palette)

    def texts(self) -> tuple[str, str, str]:
        return (self.nick_label.text(), self.clock_label.text(), self.body_label.text())


class MergedViewerDialog(QDialog):
    """合并转发的"聊天记录"窗口：``set_message(message)`` 重建内容。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mergedViewerRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("聊天记录")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(False)
        self.resize(460, 580)

        self._palette = None
        self._rows: list[MergedRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        self.title_label = QLabel("聊天记录", self)
        self.title_label.setObjectName("mergedViewTitle")
        self.subtitle_label = QLabel("", self)
        self.subtitle_label.setObjectName("mergedViewSubtitle")
        root.addWidget(self.title_label)
        root.addWidget(self.subtitle_label)

        self.area = QScrollArea(self)
        self.area.setObjectName("mergedViewArea")
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("mergedViewHost")
        host.setAutoFillBackground(False)
        self.list_layout = QVBoxLayout(host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(12)
        self.list_layout.addStretch(1)
        self.area.setWidget(host)
        self.area.viewport().setAutoFillBackground(False)
        root.addWidget(self.area, 1)

        self.close_btn = QPushButton("关闭", self)
        self.close_btn.setObjectName("ghostBtn")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(self.close_btn)
        root.addLayout(foot)

    # ------------------------------------------------------------------ 接口
    def set_message(self, message) -> None:
        """按一条"合并转发"消息重建列表。"""
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        source = message.forwarded_from or "聊天"
        self.title_label.setText(f"{source} 的聊天记录")
        self.subtitle_label.setText(f"共 {message.merged_count} 条消息")
        for entry in message.merged_items:
            row = MergedRow(entry, self.area.widget())
            self._rows.append(row)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        if self._palette is not None:
            self.apply_palette(self._palette)
        self.area.verticalScrollBar().setValue(0)      # 打开总是从最早那条看起

    def row_count(self) -> int:
        return len(self._rows)

    def row_texts(self) -> list[tuple[str, str, str]]:
        return [row.texts() for row in self._rows]

    def body_texts(self) -> list[str]:
        return [row.body_label.text() for row in self._rows]

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        for row in self._rows:
            row.apply_palette(palette)
