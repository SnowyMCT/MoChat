"""转发窗口：选一个目标会话，再决定"一条一条发"还是"合并成一条"。

设计取向
--------
* **非模态、复用同一个窗口**（和搜索、设置、工具箱一致）：关掉不影响聊天。
* **固定尺寸**（``min == max``）：这是个"点一行就发出去"的小窗口，能拉伸只会
  拉出一大片空白、或者把下面几行挤到窗口外面 —— 都不好看。所以干脆不给拉伸。
* 方式用顶部的分段控件选（``逐条转发 / 合并转发``），**先选方式再点会话** ——
  比"先点会话再弹第二个对话框问方式"少一次打断；只有一条消息时这个控件隐藏
  （一条消息没有"合并"可言，隐藏比禁用更少歧义）。
* 点会话那一行就是"确定"：不需要再点一次"转发"按钮。
* 会话行里的标题/摘要**按实际宽度截成省略号**：``QPushButton`` 的文字既不换行
  也不省略，长得没边的会话摘要会把行按钮撑得比视口还宽，整行从窗口右边探出去
  （横向滚动条是关掉的，探出去的部分直接看不见）。
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (QDialog, QFrame, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from .theme import build_qss
from .widgets import SegmentedControl, install_app_chrome, CHROME_H

__all__ = ["ForwardDialog", "MODE_SINGLE", "MODE_MERGE",
           "FORWARD_WIDTH", "FORWARD_HEIGHT", "ROW_HEIGHT"]

MODE_SINGLE = "逐条转发"
MODE_MERGE = "合并转发"
MODE_LABELS = (MODE_SINGLE, MODE_MERGE)

FORWARD_WIDTH = 420          # 固定尺寸：最小 == 最大，鼠标拖不出变化
FORWARD_HEIGHT = 520
ROW_HEIGHT = 54              # 一条会话：标题一行 + 摘要一行
ROW_PADDING = 12             # 与 QSS ``#convRow`` 的左右 padding 对齐
ROW_SAFETY = 6               # 再留点余量，别让"刚好塞满"的字被边缘切掉
LIST_RIGHT_MARGIN = 4        # 会话列表右侧留白（给滚动条留位置感）
ROOT_MARGIN = 18             # 根布局左右留白，和 _build 里保持一致
MIN_LAID_OUT_WIDTH = 120     # 视口比这还窄说明布局还没跑过，宽度不可信


class ForwardDialog(QDialog):
    """转发目标选择器：``forwardRequested(target_id, merge)``。"""

    forwardRequested = pyqtSignal(str, bool)         # (目标会话id, 是否合并)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("forwardRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("转发到")
        self.setWindowFlag(Qt.WindowType.Window, True)     # 独立窗口，不挡聊天
        self.setModal(False)
        # 固定尺寸 + 原生"不能拉伸"的对话框边框：鼠标移到边缘不再出现双向箭头
        self.setFixedSize(FORWARD_WIDTH, FORWARD_HEIGHT)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, True)

        self._palette = None
        self._count = 1
        self._rows: list[QPushButton] = []
        self._raw: list[tuple[str, str]] = []       # 与 _rows 一一对应的原文（标题, 摘要）
        self._syncing = False                       # 截断时改文字会触发事件，防重入

        root = QVBoxLayout(self)
        root.setContentsMargins(ROOT_MARGIN, 16, ROOT_MARGIN, 16)
        root.setSpacing(10)
        self.scope_control = SegmentedControl(MODE_LABELS, self, height=32)
        self.scope_control.setToolTip("多条消息时：合并成一条，或者一条一条发")
        self.scope_control.valueChanged.connect(lambda _=None: self.sync_header())
        root.addWidget(self._build_header())
        root.addWidget(self.scope_control)
        root.addWidget(self._build_list(), 1)
        self.sync_header()

        # 无边框 + 自绘 ✕；固定尺寸的窗口，高度要把标题条那一条算进去
        self.chrome = install_app_chrome(self, title=self.windowTitle())
        self.setFixedSize(FORWARD_WIDTH, FORWARD_HEIGHT + CHROME_H)

    # ================================================================== 搭建
    def _build_header(self) -> QWidget:
        host = QWidget(self)
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self.title_label = QLabel("转发到", host)
        self.title_label.setObjectName("forwardTitle")
        self.subtitle_label = QLabel("", host)
        self.subtitle_label.setObjectName("forwardSubtitle")
        column.addWidget(self.title_label)
        column.addWidget(self.subtitle_label)
        return host

    def _build_list(self) -> QWidget:
        self.area = QScrollArea(self)
        self.area.setObjectName("forwardArea")
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("forwardHost")
        self.list_layout = QVBoxLayout(host)
        self.list_layout.setContentsMargins(0, 0, LIST_RIGHT_MARGIN, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        self.area.setWidget(host)
        self.area.viewport().setAutoFillBackground(False)
        host.setAutoFillBackground(False)
        # 视口一变宽就重算一次截断（窗口固定了，这条主要在"首次布局"时起作用）
        self.area.viewport().installEventFilter(self)
        return self.area

    # ================================================================== 接口
    def set_targets(self, targets) -> None:
        """重建会话列表：``targets`` 是 ``(会话, 副标题)`` 的可迭代对象。"""
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        self._raw.clear()
        for conversation, subtitle in targets:
            button = QPushButton(self.area.widget())
            button.setObjectName("convRow")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"转发到「{conversation.title}」")
            button.setFixedHeight(ROW_HEIGHT)
            # 宽度由布局给满（Ignored = 不吃 sizeHint），这样行内容再长也撑不宽内层
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setMinimumWidth(0)
            button.clicked.connect(
                lambda _=False, cid=conversation.id: self._choose(cid))
            self._rows.append(button)
            self._raw.append((conversation.title, subtitle))
            self.list_layout.insertWidget(self.list_layout.count() - 1, button)
        self._sync_row_texts()
        if self._palette is not None:
            self.apply_palette(self._palette)

    def set_count(self, count: int) -> None:
        """要转发几条 —— 一条时隐藏"方式"控件（没有合并可言）。"""
        self._count = max(1, int(count))
        self.scope_control.setVisible(self._count > 1)
        self.sync_header()

    def sync_header(self) -> None:
        self.subtitle_label.setText(f"{self._count} 条消息")

    def mode(self) -> str:
        return MODE_MERGE if self.scope_control.value() == MODE_MERGE else MODE_SINGLE

    def is_merge(self) -> bool:
        return self.mode() == MODE_MERGE

    def is_fixed_size(self) -> bool:
        """窗口不可拉伸（最小尺寸 == 最大尺寸）。"""
        return self.minimumSize() == self.maximumSize()

    def row_titles(self) -> list[str]:
        """**原始**标题（不截断）—— 截断是显示层的事，调用方拿到的还是全名。"""
        return [title for title, _ in self._raw]

    def row_labels(self) -> list[str]:
        """界面上真正显示的文字（长的已经带省略号）。"""
        return [row.text() for row in self._rows]

    def row_count(self) -> int:
        return len(self._rows)

    def overflowing_rows(self) -> list[str]:
        """右/下边探出窗口外的行（正常应该永远是空的 —— 这正是要守住的东西）。"""
        escaped = []
        for row in self._rows:
            right = row.mapTo(self, row.rect().topRight()).x()
            bottom = row.mapTo(self, row.rect().bottomRight()).y()
            if right >= self.width() or bottom >= self.height():
                escaped.append(row.text().replace("\n", " / "))
        return escaped

    def choose(self, conversation_id: str) -> None:
        """按当前方式确认转发（测试直接调它，不用真的点按钮）。"""
        self.forwardRequested.emit(conversation_id, self.is_merge())

    def _choose(self, conversation_id: str) -> None:
        self.choose(conversation_id)

    # ================================================================== 文字
    def _row_text_width(self) -> int:
        """一行会话按钮里"文字能占多宽"（视口宽 - 右留白 - 左右 padding - 余量）。"""
        width = self.area.viewport().width()
        if width < MIN_LAID_OUT_WIDTH:           # 还没走过布局，退回按窗口宽估
            width = self.width() - 2 * ROOT_MARGIN
        return max(60, width - LIST_RIGHT_MARGIN - 2 * ROW_PADDING - ROW_SAFETY)

    def _sync_row_texts(self) -> None:
        """把每一行的标题/摘要按当前宽度截成省略号 —— 文字留在窗口里，行就不会探出去。"""
        if self._syncing:                        # setText 会回到这里，防重入
            return
        self._syncing = True
        try:
            width = self._row_text_width()
            for row, (title, subtitle) in zip(self._rows, self._raw):
                metrics = QFontMetrics(row.font())
                mode = Qt.TextElideMode.ElideRight
                row.setText(f"{metrics.elidedText(title, mode, width)}\n"
                            f"{metrics.elidedText(subtitle, mode, width)}")
        finally:
            self._syncing = False

    # ================================================================== 事件
    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self._sync_row_texts()

    def eventFilter(self, obj, event) -> bool:           # noqa: N802
        if obj is self.area.viewport() and event.type() == QEvent.Type.Resize:
            self._sync_row_texts()
        return super().eventFilter(obj, event)

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        self.scope_control.apply_palette(palette)
        self.chrome.apply_palette(palette)
        self._sync_row_texts()               # QSS 里的字号换了，截断宽度跟着重算
