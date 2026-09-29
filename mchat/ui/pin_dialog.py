"""置顶小窗"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout)

from . import theme
from .widgets import ToggleSwitch

__all__ = ["PinDialog", "ask_pin"]


class PinDialog(QDialog):
    """「置顶消息」小窗：一个勾选项 + 取消 / 置顶 两个按钮。"""

    def __init__(self, parent=None, *, peer_label: str = "", for_both: bool = False):
        super().__init__(parent)
        self.setObjectName("pinDialog")
        self.setModal(True)
        self.setFixedWidth(400)
        self._palette = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        self.title_label = QLabel("置顶这条消息", self)
        self.title_label.setObjectName("pinTitleText")
        root.addWidget(self.title_label)

        # 勾选项放在一张"卡片"里：和设置区域那几张卡片是同一套视觉语言
        card = QFrame(self)
        card.setObjectName("settingsCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.option_label = QLabel(peer_label or "也为对方置顶", card)
        self.option_label.setObjectName("pinOptionText")
        self.scope_hint = QLabel("", card)
        self.scope_hint.setObjectName("pinHintText")
        texts.addWidget(self.option_label)
        texts.addWidget(self.scope_hint)
        row.addLayout(texts, 1)
        self.checkbox = ToggleSwitch(bool(for_both), card)
        row.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(card)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.setObjectName("ghostBtn")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setMinimumWidth(84)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QPushButton("置顶", self)
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ok_btn.setMinimumWidth(84)
        self.ok_btn.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.ok_btn)
        root.addLayout(buttons)

        self.checkbox.toggled.connect(self._on_checkbox_changed)
        self._sync_scope_hint()

    # ------------------------------------------------------------------ 状态
    def for_both(self) -> bool:
        """勾了 = 对方也置顶。"""
        return self.checkbox.is_checked()

    def option_text(self) -> str:
        return self.option_label.text()

    def scope_text(self) -> str:
        return self.scope_hint.text()

    def _on_checkbox_changed(self, _checked: bool) -> None:
        self._sync_scope_hint()

    def _sync_scope_hint(self) -> None:
        """说清楚"不勾会怎样"，别让人猜（这是最容易被误解的地方）。"""
        if self.for_both():
            self.scope_hint.setText("为双方置顶")
        else:
            self.scope_hint.setText("为自己置顶")

    # ------------------------------------------------------------------ 外观
    def apply_palette(self, palette) -> None:
        """整份主题样式表 + 本窗口的排版规则（少一样都会露怯）。"""
        self._palette = palette
        if palette is None:
            return
        extra = (
            f"QDialog#pinDialog {{ background-color: {palette.surface}; }}"
            f"QLabel#pinTitleText {{ font-size: 15px; font-weight: 600;"
            f" color: {palette.text}; }}"
            f"QLabel#pinHintText {{ font-size: 12px; color: {palette.muted}; }}"
            f"QLabel#pinOptionText {{ font-size: 13.5px; color: {palette.text}; }}")
        self.setStyleSheet(theme.build_qss(palette) + extra)
        self.checkbox.apply_palette(palette)


def ask_pin(parent, palette, *, peer_label: str = "", for_both: bool = False):
    """弹小窗问一句，返回 ``True`` / ``False`` / ``None``（取消）。

    主窗口默认用这个实现；测试里注入假实现（模态窗在无头环境下会挂住）。
    """
    dialog = PinDialog(parent, peer_label=peer_label, for_both=for_both)
    dialog.apply_palette(palette)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.for_both()
