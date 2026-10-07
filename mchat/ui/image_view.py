"""点开图片的那个大图窗口（QQ / Telegram 那种"看图"体验）。

为什么不是"交给系统看图器"
--------------------------
* 系统看图器是**另一个程序**：会抢焦点、会挡在最前面，而且要求附件先解成明文落在
  临时目录里（本地附件是加密落盘的）；
* 一个会话里往往连着好几张图，QQ 那种查看器能用 ``‹ ›`` 直接翻下一张，不用退出去
  再点一次；翻页、缩放、另存为、在文件夹中显示，这几件事放进自己的窗口里才是一套的；
* 明暗主题、文字大小才能跟着主窗口走。

职责边界（和 :mod:`mchat.ui.attach` 一致）：本模块只负责"把图画出来、把缩放和翻页
做对、把另存为 / 在文件夹中显示抛成信号"。真正的文件动作由主窗口执行 —— 那里有提示
气泡，也统一判断"文件还在不在"。

``path`` 为空 = 这张图还在服务器上：窗口显示"正在下载 …"，下完主窗口再调一次
``set_images`` 换成本地路径即可（不用为它另开一个"加载中"窗口）。
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..core.atrest import AtRestError, materialize
from .theme import build_qss
from .widgets import install_app_chrome

__all__ = ["ImageViewerDialog", "ZOOM_MIN", "ZOOM_MAX", "ZOOM_STEP"]

ZOOM_MIN = 10                       # 缩放百分比下限
ZOOM_MAX = 800                      # 上限（800% 够看像素级细节了）
ZOOM_STEP = 1.25                    # 滚轮 / ＋－ 一档的倍率
VIEWER_SIZE = (900, 640)            # 打开时的默认窗口大小
CANVAS_PADDING = 16                 # "适应窗口"时四周留的余量（像素）


def load_pixmap(path: str) -> QPixmap | None:
    """读一张图；读不出来返回 ``None``（调用方写一行提示，别抛）。

    附件是加密落盘的 → 先过 :func:`materialize` 解出临时明文，再交给 Qt 解码。
    和缩略图那条路同一个做法（见 ``ui/attach.py``）。
    """
    try:
        pixmap = QPixmap(materialize(path))
    except AtRestError:
        return None
    if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        return None
    return pixmap


class _Canvas(QLabel):
    """画布：双击在"适应窗口 / 原始大小"之间切，按住拖动可以平移大图。"""

    doubleClicked = pyqtSignal()
    dragged = pyqtSignal(int, int)          # (dx, dy)：由窗口去动滚动条

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press = None

    def mouseDoubleClickEvent(self, event) -> None:      # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:             # noqa: N802
        if self._press is not None:
            point = event.position().toPoint()
            self.dragged.emit(self._press.x() - point.x(), self._press.y() - point.y())
            self._press = point
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802
        self._press = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)


class ImageViewerDialog(QDialog):
    """图片查看器：``set_images(items, index)`` 换一组图（复用同一个窗口）。

    ``items`` 是 ``[(message_id, path, name), …]``：``path`` 为空表示那张图还在服务器上。
    """

    revealRequested = pyqtSignal(str)      # message_id：在文件夹中显示
    saveRequested = pyqtSignal(str)        # message_id：另存为…
    imageChanged = pyqtSignal(str)         # 换到了哪一张（缺本地文件的由主窗口去取回）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("imageViewerRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("图片")
        self.setWindowFlag(Qt.WindowType.Window, True)     # 独立窗口，不被父窗口裁掉
        self.setModal(False)
        self.resize(*VIEWER_SIZE)
        self.setMinimumSize(380, 300)

        self._palette = None
        self._items: list[tuple[str, str, str]] = []
        self._index = 0
        self._zoom = 1.0            # 1.0 = 100%（只在非"适应窗口"时生效）
        self._fit = True            # 默认适应窗口（QQ 打开也是先塞满窗口）
        self._pixmap: QPixmap | None = None
        self._note = ""             # 画不出图时那行字

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.area = QScrollArea(self)
        self.area.setObjectName("imageViewerScroll")
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setWidgetResizable(True)
        self.area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        host = QWidget()
        host.setObjectName("imageViewerHost")
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas = _Canvas(host)
        self.canvas.setObjectName("imageViewerCanvas")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.canvas.setWordWrap(True)
        self.canvas.doubleClicked.connect(self.toggle_fit)
        self.canvas.dragged.connect(self.pan_by)
        host_layout.addWidget(self.canvas)
        self.area.setWidget(host)
        self.area.viewport().setAutoFillBackground(False)
        root.addWidget(self.area, 1)

        self.bar = QFrame(self)
        self.bar.setObjectName("imageViewerBar")
        self.bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(10, 8, 10, 8)
        bar.setSpacing(8)

        self.prev_btn = self._nav_button("‹", "上一张（←）", self.prev_image)
        self.next_btn = self._nav_button("›", "下一张（→）", self.next_image)
        self.info_label = QLabel("", self.bar)
        self.info_label.setObjectName("imageViewerInfo")

        self.zoom_out_btn = self._bar_button("－", "缩小（-）", self.zoom_out)
        self.zoom_in_btn = self._bar_button("＋", "放大（+）", self.zoom_in)
        self.fit_btn = self._bar_button("适应窗口", "缩到整张图都看得见（0）", self.fit_to_window)
        self.actual_btn = self._bar_button("原始大小", "按 100% 显示（1）", self.actual_size)
        self.save_btn = self._bar_button("另存为…", "把这张图复制到别的位置",
                                        lambda: self._emit_current(self.saveRequested))
        self.reveal_btn = self._bar_button("在文件夹中显示", "打开它所在的文件夹",
                                          lambda: self._emit_current(self.revealRequested))
        self.close_btn = self._bar_button("关闭", "关掉这个窗口（Esc）", self.close)

        bar.addWidget(self.prev_btn)
        bar.addWidget(self.next_btn)
        bar.addWidget(self.info_label)
        bar.addStretch(1)
        bar.addWidget(self.zoom_out_btn)
        bar.addWidget(self.zoom_in_btn)
        bar.addWidget(self.fit_btn)
        bar.addWidget(self.actual_btn)
        bar.addWidget(self.save_btn)
        bar.addWidget(self.reveal_btn)
        bar.addWidget(self.close_btn)
        root.addWidget(self.bar)

        self._apply_display()

        # 无边框 + 自绘 ✕。标题留空：图名/第几张在自己那条信息栏里写着，别重复
        self.chrome = install_app_chrome(self, title="")

    # ------------------------------------------------------------------ 构造
    def _nav_button(self, text, tip, slot) -> QPushButton:
        button = QPushButton(text, self.bar)
        button.setObjectName("viewerNav")
        button.setToolTip(tip)
        button.setFixedSize(30, 28)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda _=False: slot())
        return button

    def _bar_button(self, text, tip, slot) -> QPushButton:
        button = QPushButton(text, self.bar)
        button.setObjectName("viewerBtn")
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda _=False: slot())
        return button

    # ------------------------------------------------------------------ 数据
    def set_images(self, items, index: int = 0) -> None:
        """换一组图。``items``：``[(message_id, path, name), …]``（``path`` 可为空串）。"""
        self._items = [tuple(item) for item in items]
        self._index = max(0, min(int(index), len(self._items) - 1)) if self._items else 0
        self._fit = True                       # 换图回到"适应窗口"，跟 QQ 一样
        self._apply_display()

    def set_image(self, path: str, name: str = "", *, message_id: str = "") -> None:
        """单张图（没有会话上下文时的便捷入口）。"""
        self.set_images([(message_id, path, name or "图片")], 0)

    def set_loading(self, name: str, *, message_id: str = "", total: int = 1,
                   index: int = 0) -> None:
        """"这张图还在下载"的占位形态（下完主窗口再 ``set_images`` 换成本地路径）。"""
        items = [(message_id, "", name)] if total <= 1 else \
            [(message_id, "", name)] + [("", "", "图片")] * (total - 1)
        self.set_images(items, index)

    def current(self) -> tuple[str, str, str] | None:
        if not self._items:
            return None
        return self._items[max(0, min(self._index, len(self._items) - 1))]

    def current_path(self) -> str:
        item = self.current()
        return "" if item is None else str(item[1])

    def current_name(self) -> str:
        item = self.current()
        return "" if item is None else str(item[2])

    def count(self) -> int:
        return len(self._items)

    def index(self) -> int:
        return self._index

    def has_image(self) -> bool:
        """当前这张真的画出来了吗（文件不在/还在下载就是 False）。"""
        return self._pixmap is not None

    # ------------------------------------------------------------------ 缩放
    def zoom_percent(self) -> int:
        """当前**实际**显示比例（适应窗口时是按视口算出来的那个数）。"""
        return int(round(self._visible_scale() * 100))

    def in_fit_mode(self) -> bool:
        return self._fit

    def canvas_size(self) -> QSize:
        """画出来的图多大（测试用来断言"适应窗口不超视口、不放大超过 100%"）。"""
        return QSize(self.canvas.width(), self.canvas.height())

    def set_zoom(self, percent: float) -> None:
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, float(percent))) / 100.0
        self._fit = False
        self._apply_display()

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom_percent() * ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom_percent() / ZOOM_STEP)

    def fit_to_window(self) -> None:
        self._fit = True
        self._apply_display()

    def actual_size(self) -> None:
        self.set_zoom(100)

    def toggle_fit(self) -> None:
        """双击切换：适应窗口 ↔ 原始大小。"""
        if self._fit:
            self.actual_size()
        else:
            self.fit_to_window()

    def pan_by(self, dx: int, dy: int) -> None:
        """拖动大图（内容比视口大时才动得了 —— 滚动条自己会夹取）。"""
        horizontal = self.area.horizontalScrollBar()
        vertical = self.area.verticalScrollBar()
        horizontal.setValue(horizontal.value() + int(dx))
        vertical.setValue(vertical.value() + int(dy))

    # ------------------------------------------------------------------ 翻页
    def next_image(self) -> bool:
        if self._index + 1 >= len(self._items):
            return False
        self._index += 1
        self._fit = True
        self._apply_display()
        return True

    def prev_image(self) -> bool:
        if self._index <= 0:
            return False
        self._index -= 1
        self._fit = True
        self._apply_display()
        return True

    def nav_state(self) -> tuple[bool, bool]:
        """``(上一张可用, 下一张可用)`` —— 到头就禁用（不绕回）。"""
        return (self.prev_btn.isEnabled(), self.next_btn.isEnabled())

    # ------------------------------------------------------------------ 呈现
    def title_text(self) -> str:
        return self.windowTitle()

    def info_text(self) -> str:
        return self.info_label.text()

    def note_text(self) -> str:
        return self._note

    def _emit_current(self, signal) -> None:
        item = self.current()
        if item is not None and str(item[0]):
            signal.emit(str(item[0]))

    def _apply_display(self) -> None:
        item = self.current()
        self._pixmap = None
        self._note = ""
        if item is None:
            self.setWindowTitle("图片")
            self._note = "没有图片"
        else:
            _, path, name = item
            self.setWindowTitle(name or "图片")
            if not path:
                self._note = f"正在下载 {name} …"
            else:
                self._pixmap = load_pixmap(path)
                if self._pixmap is None:
                    self._note = f"这张图打不开了\n{name}"
        if self._pixmap is None:
            self.canvas.setPixmap(QPixmap())
            self.canvas.setText(self._note)
            self.canvas.setFixedSize(max(320, self.area.viewport().width() - CANVAS_PADDING),
                                     max(200, self.area.viewport().height() - CANVAS_PADDING))
        else:
            size = self._display_size()
            self.canvas.setPixmap(self._pixmap.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self.canvas.setFixedSize(size)
        self._sync_controls()
        self._update_info()
        item = self.current()
        if item is not None and str(item[0]):
            # 告诉主窗口"现在看的是哪一张"：本地没有、服务器上还有的会被它取回来
            self.imageChanged.emit(str(item[0]))

    def _sync_controls(self) -> None:
        has_items = bool(self._items)
        for button in (self.zoom_in_btn, self.zoom_out_btn, self.fit_btn,
                       self.actual_btn, self.save_btn, self.reveal_btn):
            button.setEnabled(has_items)
        self.prev_btn.setEnabled(self._index > 0)
        # 下一张：后面还有**任何一张**（哪怕它还在下载）就该能翻
        self.next_btn.setEnabled(self._index + 1 < len(self._items))

    def _update_info(self) -> None:
        item = self.current()
        if item is None:
            self.info_label.setText("")
            return
        name = str(item[2])
        if self._pixmap is None:
            self.info_label.setText(self._note.replace("\n", " · "))
            return
        parts = [name, "%d×%d" % (self._pixmap.width(), self._pixmap.height()),
                 "%d%%" % self.zoom_percent()]
        if len(self._items) > 1:
            parts.append("第 %d/%d 张" % (self._index + 1, len(self._items)))
        self.info_label.setText(" · ".join(parts))

    def _fit_scale(self) -> float:
        """"适应窗口"的比例：塞得下、而且**不放大**超过 100%（小图放大只会更糊）。"""
        if self._pixmap is None or self._pixmap.width() <= 0 or self._pixmap.height() <= 0:
            return 1.0
        view = self.area.viewport().size()
        avail_w = max(64, view.width() - CANVAS_PADDING)
        avail_h = max(64, view.height() - CANVAS_PADDING)
        return min(1.0, avail_w / self._pixmap.width(), avail_h / self._pixmap.height())

    def _visible_scale(self) -> float:
        return self._fit_scale() if self._fit else self._zoom

    def _display_size(self) -> QSize:
        if self._pixmap is None:
            return QSize(1, 1)
        scale = self._visible_scale()
        return QSize(max(1, int(round(self._pixmap.width() * scale))),
                     max(1, int(round(self._pixmap.height() * scale))))

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        # 图片区整块是深色画布：标题条跟底部那条信息栏同色，别在顶上留一条浅灰
        self.chrome.setStyleSheet(
            f"QFrame#chrome {{ background-color: {palette.card}; border: none; }}")
        self.chrome.apply_palette(palette)

    # ------------------------------------------------------------------ 事件
    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        if self._fit:                                    # 适应窗口时跟着窗口重新算
            self._apply_display()
        else:
            self._update_info()

    def showEvent(self, event) -> None:                  # noqa: N802
        super().showEvent(event)
        if self._fit:
            self._apply_display()                        # 第一次显示才拿得到真实视口尺寸

    def wheelEvent(self, event) -> None:                 # noqa: N802
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom_in()
        elif delta < 0:
            self.zoom_out()
        event.accept()

    def keyPressEvent(self, event) -> None:              # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_Left:
            self.prev_image()
        elif key == Qt.Key.Key_Right:
            self.next_image()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
        elif key == Qt.Key.Key_Minus:
            self.zoom_out()
        elif key == Qt.Key.Key_0:
            self.fit_to_window()
        elif key == Qt.Key.Key_1:
            self.actual_size()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
