"""附件的界面部件：输入栏上方的待发送条、气泡里的图片缩略图与文件卡片。

三层职责分清楚（和项目其它地方一致）：

* **数据层**（``core.models.Attachment``）只管路径/名字/大小，不认识 Qt；
* **本模块**只负责"把附件画出来、把点击抛成信号"，不改数据、不碰文件系统的副作用；
* 真正的动作（打开文件、打开所在文件夹、把剪贴板图片落成文件）都在主窗口里做 ——
  那里有提示气泡、能统一判断"文件还在不在"。

图片缩略图带一份小缓存：切会话会整块重画消息区，同一张图会被反复请求，
每次都重新解码几百 KB 的图很浪费。
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPixmap
from PyQt6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QSizePolicy,
                             QToolButton, QVBoxLayout, QWidget)

from ..core.atrest import AtRestError, materialize
from ..core.models import Attachment
from .icons import make_icon

__all__ = ["THUMB", "IMAGE_MAX_WIDTH", "IMAGE_MAX_HEIGHT", "CARD_WIDTH",
           "IMAGE_FILTER", "FILE_FILTER", "pick_file", "scaled_preview",
           "attachment_meta", "AttachmentTray", "ImageAttachmentView",
           "FileAttachmentView"]

THUMB = 64                    # 待发送条里缩略图的边长
IMAGE_MAX_WIDTH = 300         # 气泡里图片的最大显示尺寸（等比缩放，不裁切）
IMAGE_MAX_HEIGHT = 300
CARD_WIDTH = 236              # 气泡里文件卡片的固定宽度

IMAGE_FILTER = ("图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tif *.tiff *.ico)"
                ";;所有文件 (*)")
FILE_FILTER = "所有文件 (*.*)"

_PREVIEW_CACHE: dict[tuple, QPixmap] = {}
_PREVIEW_CACHE_MAX = 24


# --------------------------------------------------------------------- 工具
def pick_file(kind: str = "file", parent=None) -> str:
    """弹系统文件对话框，返回选中的路径（取消返回空串）。

    **测试别走这里**：``QFileDialog`` 是模态窗口，无头环境会卡住。
    ``MainWindow`` 的 ``filedialog`` 参数可以注入一个假选择器，覆盖的是同一条代码路径。
    """
    image = kind == "image"
    path, _ = QFileDialog.getOpenFileName(
        parent, "选择图片" if image else "选择文件", "",
        IMAGE_FILTER if image else FILE_FILTER)
    return path or ""


def scaled_preview(path: str, *, max_width: int = IMAGE_MAX_WIDTH,
                   max_height: int = IMAGE_MAX_HEIGHT) -> QPixmap | None:
    """把图片等比缩到上限内；文件不在或读不出来时返回 ``None``（调用方降级处理）。"""
    key = (path, int(max_width), int(max_height))
    cached = _PREVIEW_CACHE.get(key)
    if cached is not None:
        return cached
    # 附件缓存是加密落盘的 → 先解出临时明文再交给 Qt 读（没加密时这一步是空操作）
    try:
        pixmap = QPixmap(materialize(path))
    except AtRestError:
        return None
    if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        return None
    scaled = pixmap.scaled(max_width, max_height,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    if len(_PREVIEW_CACHE) >= _PREVIEW_CACHE_MAX:      # 简单先进先出，够用
        _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    _PREVIEW_CACHE[key] = scaled
    return scaled


def attachment_meta(attachment: Attachment) -> str:
    """卡片/待发送条上的第二行：``PNG 图片 · 1.2 MB`` / ``PDF 文件 · 865 B``。"""
    ext = attachment.ext
    if attachment.kind == "image":
        kind_text = f"{ext} 图片" if ext else "图片"
    else:
        kind_text = f"{ext} 文件" if ext else "文件"
    return f"{kind_text} · {attachment.size_text}" if attachment.size_text else kind_text


def attachment_tooltip(attachment: Attachment) -> str:
    lines = [attachment.name]
    meta = attachment_meta(attachment).replace(" · ", "\n")
    lines.append(meta)
    lines.append(attachment.path)
    lines.append("点击打开（右键可打开所在文件夹）" if attachment.exists
                 else "⚠ 文件已不在本地")
    return "\n".join(lines)


def _file_icon_pixmap(palette, size: int = 30, *, missing: bool = False) -> QPixmap:
    color = getattr(palette, "muted" if missing else "accent", "#3390EC")
    return make_icon("file", color, size).pixmap(QSize(size, size))


# ------------------------------------------------------------ 待发送的附件条
class AttachmentTray(QWidget):
    """输入栏上方的"待发送附件"条：缩略图 + 名字/类型大小 + ✕ 移除。

    只是"待发送"的中转站：附件怎么选、怎么发、发到哪，都是主窗口的事。
    没有附件时整条隐藏（高度自然收回去，输入栏不变形）。
    """

    removed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("attachTray")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._attachment: Attachment | None = None
        self._palette = None

        self.thumb = QLabel(self)
        self.thumb.setObjectName("attachThumb")
        self.thumb.setFixedSize(THUMB, THUMB)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.name_label = QLabel("", self)
        self.name_label.setObjectName("attachName")
        self.meta_label = QLabel("", self)
        self.meta_label.setObjectName("attachMeta")

        self.remove_btn = QToolButton(self)
        self.remove_btn.setObjectName("closeBtn")
        self.remove_btn.setText("✕")
        self.remove_btn.setFixedSize(26, 26)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.remove_btn.setToolTip("移除附件（不发送）")
        self.remove_btn.clicked.connect(self._on_remove)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        text_column.addWidget(self.name_label)
        text_column.addWidget(self.meta_label)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 6)
        row.setSpacing(10)
        row.addWidget(self.thumb)
        row.addLayout(text_column, 1)
        row.addWidget(self.remove_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.setVisible(False)

    # ------------------------------------------------------------------ 对外
    def set_attachment(self, attachment: Attachment | None) -> None:
        self._attachment = attachment
        if attachment is None:
            self.clear()
            return
        self.name_label.setText(f"待发送：{attachment.name}")
        self.meta_label.setText(attachment_meta(attachment))
        self.thumb.setPixmap(self._thumb_pixmap())
        self.thumb.setToolTip(attachment_tooltip(attachment))
        self.setVisible(True)                 # 运行时显隐必须显式设置

    def clear(self) -> None:
        self._attachment = None
        self.thumb.clear()
        self.name_label.clear()
        self.meta_label.clear()
        self.setVisible(False)

    def attachment(self) -> Attachment | None:
        return self._attachment

    def has_attachment(self) -> bool:
        return self._attachment is not None

    def apply_palette(self, palette) -> None:
        self._palette = palette
        if self._attachment is not None:
            self.thumb.setPixmap(self._thumb_pixmap())

    # ------------------------------------------------------------------ 内部
    def _thumb_pixmap(self) -> QPixmap:
        attachment = self._attachment
        if attachment is None:
            return QPixmap()
        if attachment.kind == "image":
            pixmap = scaled_preview(attachment.path, max_width=THUMB, max_height=THUMB)
            if pixmap is not None:
                return pixmap
        # 非图片（或图片读不出来）：画个文件图标顶上
        return _file_icon_pixmap(self._palette, 34, missing=not attachment.exists)

    def _on_remove(self) -> None:
        self.clear()
        self.removed.emit()


# -------------------------------------------------------- 气泡里的图片
class ImageAttachmentView(QLabel):
    """气泡里的图片缩略图；点一下让系统看图器打开（"打开"动作由主窗口执行）。"""

    activated = pyqtSignal(str)              # action: "open"
    menuRequested = pyqtSignal(object)       # 右键：把坐标抛给气泡去弹菜单

    def __init__(self, attachment: Attachment, parent=None):
        super().__init__(parent)
        self.attachment = attachment
        self.setObjectName("imagePreview")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(attachment_tooltip(attachment))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        pixmap = scaled_preview(attachment.path)
        self._has_image = pixmap is not None
        if self._has_image:
            self.setPixmap(pixmap)
            # +2 是给 QSS 的 1px 边框留位置，否则边框会盖住图像最外一圈像素
            self.setFixedSize(pixmap.width() + 2, pixmap.height() + 2)
        else:
            self.setText(f"图片不在本地了\n{attachment.name}")
            self.setMinimumWidth(180)
            self.setWordWrap(True)

    # ------------------------------------------------------------------ 对外
    def has_image(self) -> bool:
        """图片真的画出来了吗（文件不在了就是 False，气泡上是一行提示）。"""
        return self._has_image

    def preview_size(self) -> QSize:
        """画出来的缩略图尺寸（测试用来断言"等比 + 不超上限"）。"""
        pixmap = self.pixmap()
        return QSize() if pixmap is None else pixmap.size()

    def apply_palette(self, palette) -> None:      # 图片本身不换色，接口与其它部件对齐
        """（图片由像素构成，主题切换不需要重画；留这个方法是为了调用方统一处理。）"""

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event) -> None:      # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit("open")
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:     # noqa: N802
        self.menuRequested.emit(event.globalPos())


# -------------------------------------------------------- 气泡里的文件卡片
class FileAttachmentView(QFrame):
    """气泡里的文件卡片：图标 + 文件名 + "类型 · 大小"；点一下用默认程序打开。"""

    activated = pyqtSignal(str)
    menuRequested = pyqtSignal(object)

    def __init__(self, attachment: Attachment, parent=None, *, palette=None):
        super().__init__(parent)
        self.attachment = attachment
        self.setObjectName("fileCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(CARD_WIDTH)
        self.setToolTip(attachment_tooltip(attachment))

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(38, 38)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.name_label = QLabel(attachment.name, self)
        self.name_label.setObjectName("fileName")
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        self.meta_label = QLabel(attachment_meta(attachment), self)
        self.meta_label.setObjectName("fileMeta")
        if not attachment.exists:
            self.meta_label.setText(f"{self.meta_label.text()} · 已不在本地")

        text_column = QVBoxLayout()
        text_column.setSpacing(3)
        text_column.addWidget(self.name_label)
        text_column.addWidget(self.meta_label)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 9, 12, 9)
        row.setSpacing(10)
        row.addWidget(self.icon_label)
        row.addLayout(text_column, 1)

        self._palette = None
        self.apply_palette(palette)
        self._elide_name()

    # ------------------------------------------------------------------ 对外
    def name_text(self) -> str:
        return self.attachment.name

    def meta_text(self) -> str:
        return self.meta_label.text()

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.icon_label.setPixmap(_file_icon_pixmap(
            palette, 28, missing=not self.attachment.exists))

    # ------------------------------------------------------------------ 内部
    def resizeEvent(self, event) -> None:           # noqa: N802
        super().resizeEvent(event)
        self._elide_name()

    def _elide_name(self) -> None:
        """文件名按卡片宽度截断（名字可能很长，硬切会露出半截字）。"""
        metrics = QFontMetrics(self.name_label.font())
        width = max(60, self.name_label.width())
        self.name_label.setText(
            metrics.elidedText(self.attachment.name, Qt.TextElideMode.ElideMiddle, width))

    def mousePressEvent(self, event) -> None:       # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit("open")
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:      # noqa: N802
        self.menuRequested.emit(event.globalPos())
