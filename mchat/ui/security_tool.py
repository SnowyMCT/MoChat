"""安全工具箱：原来那套 Base64 / AES-256 文本转换器。

从"整个应用"降级为"聊天软件里的一个按钮"，但功能与交互原样保留：
两种算法 × 两个方向、悬浮标签动效、密钥区折叠、复制、快捷键、错误提示。
它只认主题色（:meth:`apply_palette`），自己不管主题切换 —— 主题归主窗口管。
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame,
                             QHBoxLayout, QLabel, QPushButton, QToolButton,
                             QVBoxLayout, QWidget)

from ..core import crypto
from .theme import Palette, build_qss
from .widgets import (CollapsibleSection, FloatingLabelInput, SegmentedControl,
                      Toast)

__all__ = ["ALGORITHM_BASE64", "ALGORITHM_AES", "SecurityToolWidget",
           "SecurityToolDialog"]

ALGORITHM_BASE64 = "Base64 编解码"
ALGORITHM_AES = "AES-256 加解密"
MODE_TEXT_LABELS = ("编码", "解码")
MODE_AES_LABELS = ("加密", "解密")

# 每种「算法 × 模式」组合的界面文案，集中一处便于维护
SPEC = {
    (ALGORITHM_BASE64, "编码"): {
        "button": "执 行 编 码",
        "label": "待编码文本",
        "hint": "UTF-8 文本 → Base64 字符串",
    },
    (ALGORITHM_BASE64, "解码"): {
        "button": "执 行 解 码",
        "label": "Base64 密文",
        "hint": "Base64 字符串 → UTF-8 文本（自动忽略换行、自动补 '='）",
    },
    (ALGORITHM_AES, "加密"): {
        "button": "执 行 加 密",
        "label": "待加密明文",
        "hint": "AES-256-CBC · SHA-256 派生密钥 · PKCS7 填充",
    },
    (ALGORITHM_AES, "解密"): {
        "button": "执 行 解 密",
        "label": "待解密密文",
        "hint": "自动识别 MChat v1 格式与 Test16 旧格式",
    },
}


class SecurityToolWidget(QWidget):
    """转换器本体（无窗口边框，可直接嵌进对话框或测试）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("toolRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(QSize(620, 760))
        # 让面板自己能接住鼠标点击（点空白处好让输入框失焦）
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self.toast = Toast(self)
        self._initializing = True
        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()
        # 启动时不抢焦点：标签安静地落在框里当占位符，用户点一下才浮起
        self._on_algorithm_changed(self.algo_seg.value())
        self._initializing = False

    # ======================================================================
    # 界面搭建
    # ======================================================================
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 20)
        root.setSpacing(12)

        root.addLayout(self._build_header())
        root.addLayout(self._build_pickers())
        root.addWidget(self._build_input_card(), 1)

        self.action_btn = QPushButton("执 行 编 码")
        self.action_btn.setObjectName("primary")
        self.action_btn.setMinimumHeight(46)
        self.action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_btn.setToolTip("执行（Ctrl+Enter）")
        root.addWidget(self.action_btn)
        root.addWidget(self._build_output_card(), 1)

    def _build_header(self) -> QHBoxLayout:
        title = QLabel("安全工具箱")
        title.setObjectName("title")
        subtitle = QLabel("Base64 编码 · AES-256 加密 · 全程本地运算")
        subtitle.setObjectName("subtitle")

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(title)
        text_col.addWidget(subtitle)

        hint = QLabel("关掉本窗口即可回到聊天")
        hint.setObjectName("hint")

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addLayout(text_col)
        head.addStretch(1)
        head.addWidget(hint, 0, Qt.AlignmentFlag.AlignVCenter)
        return head

    def _build_pickers(self) -> QVBoxLayout:
        self.algo_seg = SegmentedControl((ALGORITHM_BASE64, ALGORITHM_AES), height=40)
        self.algo_seg.setToolTip("选择算法")
        self.mode_seg = SegmentedControl(MODE_TEXT_LABELS, height=40)
        self.mode_seg.setToolTip("选择方向")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.algo_seg, 3)
        row.addWidget(self.mode_seg, 2)

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hint")

        box = QVBoxLayout()
        box.setSpacing(7)
        box.addLayout(row)
        box.addWidget(self.hint_label)
        return box

    def _build_input_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 13, 16, 15)
        lay.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        head_title = QLabel("输入")
        head_title.setObjectName("cardTitle")
        self.counter_label = QLabel("")
        self.counter_label.setObjectName("hint")
        self.clear_btn = QToolButton()
        self.clear_btn.setObjectName("ghost")
        self.clear_btn.setText("清空")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setToolTip("清空输入与输出（Esc 只清输出）")
        header.addWidget(head_title)
        header.addWidget(self.counter_label)
        header.addStretch(1)
        header.addWidget(self.clear_btn)
        lay.addLayout(header)

        self.input_field = FloatingLabelInput(
            SPEC[(ALGORITHM_BASE64, "编码")]["label"], multiline=True,
            field_height=104, radius=12,
        )
        lay.addWidget(self.input_field, 1)

        self.key_section = CollapsibleSection()
        self.key_field = FloatingLabelInput("加密密钥（口令）", password=True,
                                            field_height=44)
        self.compat_check = QCheckBox("兼容 Test16 旧格式（固定 IV，老程序也能解开）")
        self.compat_check.setToolTip(
            "勾选后使用固定初始向量且不写版本头，密文可被 Test16 直接解开；\n"
            "代价是同口令同明文每次结果相同。解密时两种格式都会自动识别。"
        )
        self.key_section.content_layout().addWidget(self.key_field)
        self.key_section.content_layout().addWidget(self.compat_check)
        lay.addWidget(self.key_section)
        return card

    def _build_output_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 13, 16, 15)
        lay.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        head_title = QLabel("输出")
        head_title.setObjectName("cardTitle")
        self.output_hint = QLabel("")
        self.output_hint.setObjectName("hint")
        self.copy_btn = QToolButton()
        self.copy_btn.setObjectName("ghost")
        self.copy_btn.setText("复制")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.copy_btn.setToolTip("复制结果到剪贴板（Ctrl+Shift+C）")
        header.addWidget(head_title)
        header.addWidget(self.output_hint)
        header.addStretch(1)
        header.addWidget(self.copy_btn)
        lay.addLayout(header)

        self.output_field = FloatingLabelInput(
            "结果会显示在这里", multiline=True, read_only=True,
            field_height=104, radius=12,
        )
        lay.addWidget(self.output_field, 1)
        return card

    def _connect_signals(self) -> None:
        self.algo_seg.valueChanged.connect(self._on_algorithm_changed)
        self.mode_seg.valueChanged.connect(self._on_mode_changed)
        self.input_field.textChanged.connect(self._on_input_changed)
        self.input_field.submitted.connect(self.execute)
        self.key_field.submitted.connect(self.execute)
        self.action_btn.clicked.connect(self.execute)
        self.copy_btn.clicked.connect(self.copy_output)
        self.clear_btn.clicked.connect(self.clear_all)
        self.compat_check.toggled.connect(lambda _: self.key_section.refresh_height())

    def _install_shortcuts(self) -> None:
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(seq), self, activated=self.execute)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_output)
        QShortcut(QKeySequence("Escape"), self, activated=self.clear_output)

    # ======================================================================
    # 状态与文案
    # ======================================================================
    @property
    def algorithm(self) -> str:
        return self.algo_seg.value()

    @property
    def mode(self) -> str:
        return self.mode_seg.value()

    def _spec(self) -> dict:
        return SPEC[(self.algorithm, self.mode)]

    def _on_algorithm_changed(self, name: str) -> None:
        is_aes = name == ALGORITHM_AES
        self.mode_seg.set_options(MODE_AES_LABELS if is_aes else MODE_TEXT_LABELS)
        self.key_section.set_expanded(is_aes)
        self._refresh_texts()

    def _on_mode_changed(self, _mode: str) -> None:
        self._refresh_texts()
        self.input_field.clear()
        self.clear_output()
        if not self._initializing:
            self.input_field.focus_field()

    def _refresh_texts(self) -> None:
        spec = self._spec()
        self.action_btn.setText(spec["button"])
        self.input_field.set_label(spec["label"])
        self.hint_label.setText(spec["hint"])
        is_aes = self.algorithm == ALGORITHM_AES
        encrypting = self.mode in ("编码", "加密")
        if is_aes:
            self.key_field.set_label("加密密钥（口令）" if encrypting else "解密密钥（口令）")
        self.compat_check.setVisible(is_aes and encrypting)
        self.key_section.refresh_height()

    def _on_input_changed(self, text: str) -> None:
        self.counter_label.setText(f"{len(text)} 字符" if text else "")

    # ======================================================================
    # 业务动作
    # ======================================================================
    def execute(self) -> None:
        """按当前算法 + 模式转换输入内容。"""
        text = self.input_field.text()
        if not text.strip():
            self._fail("输入内容不能为空", self.input_field)
            return
        algorithm, mode = self.algorithm, self.mode
        key = self.key_field.text()
        if algorithm == ALGORITHM_AES and not key:
            self._fail("密钥不能为空", self.key_field)
            return

        heavy = len(text) > 20_000
        if heavy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            detail = self._compute(algorithm, mode, text, key)
        except crypto.CryptoError as exc:
            self._fail(str(exc), self.input_field)
            return
        except Exception as exc:          # 兜底：算法层异常绝不让窗口崩掉
            self._fail(f"处理失败：{exc}", self.input_field)
            return
        finally:
            if heavy:
                QApplication.restoreOverrideCursor()

        self.output_field.setText(detail["text"])
        self.output_field.set_label("输出结果")
        self.output_hint.setText(detail.get("note", ""))
        self.toast.show_message(detail["message"], detail["kind"])

    def _compute(self, algorithm: str, mode: str, text: str, key: str) -> dict:
        if algorithm == ALGORITHM_BASE64:
            if mode == "解码":
                return {"text": crypto.decode_base64(text), "message": "Base64 解码完成",
                        "kind": "success", "note": "Base64 → 文本"}
            return {"text": crypto.encode_base64(text), "message": "Base64 编码完成",
                    "kind": "success", "note": "文本 → Base64"}
        if mode == "加密":
            legacy = self.compat_check.isChecked()
            return {
                "text": crypto.aes_encrypt(text, key, legacy_iv=legacy),
                "message": "AES-256 加密完成" + ("（Test16 兼容格式）" if legacy else "（随机 IV）"),
                "kind": "success",
                "note": "Test16 兼容格式" if legacy else "MChat v1 格式",
            }
        fmt = crypto.detect_format(text)
        return {
            "text": crypto.aes_decrypt(text, key),
            "message": "AES-256 解密完成" + (f" · {fmt.name}" if fmt else ""),
            "kind": "success",
            "note": fmt.detail if fmt else "",
        }

    def copy_output(self) -> None:
        text = self.output_field.text()
        if not text:
            self.toast.show_message("还没有可复制的结果", "warning")
            return
        QApplication.clipboard().setText(text)
        self.toast.show_message(f"已复制 {len(text)} 个字符到剪贴板", "success")

    def clear_all(self) -> None:
        self.input_field.clear()
        self.key_field.clear()
        self.clear_output()
        self.toast.show_message("已清空", "info", 1500)

    def clear_output(self) -> None:
        self.output_field.clear()
        self.output_field.set_label("结果会显示在这里")
        self.output_hint.setText("")

    def _fail(self, message: str, field: FloatingLabelInput) -> None:
        field.flash_error()
        field.focus_field()
        self.toast.show_message(message, "error", 2800)

    # ======================================================================
    # 主题（由外部驱动）
    # ======================================================================
    def apply_palette(self, p: Palette) -> None:
        self.setStyleSheet(build_qss(p))
        for widget in (self.algo_seg, self.mode_seg, self.input_field,
                       self.key_field, self.output_field, self.toast):
            widget.apply_palette(p)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(p.surface))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
        pal.setColor(QPalette.ColorRole.Text, QColor(p.text))
        pal.setColor(QPalette.ColorRole.Base, QColor(p.card))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_on))
        self.setPalette(pal)

    # ======================================================================
    # 便捷接口（也方便脚本化与测试）
    # ======================================================================
    def select_algorithm(self, index: int) -> None:
        self.algo_seg.set_index(index, animate=False)

    def select_mode(self, index: int) -> None:
        self.mode_seg.set_index(index, animate=False)

    def set_input(self, text: str, key: str | None = None) -> None:
        self.input_field.setText(text)
        if key is not None:
            self.key_field.setText(key)

    def output_text(self) -> str:
        return self.output_field.text()

    def status_text(self) -> str:
        return self.toast.message()

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self.toast.reposition()

    def mousePressEvent(self, event) -> None:            # noqa: N802
        """点空白处：把焦点从输入框收回，悬浮标签随动画落回框内。

        点击落在输入框/按钮上时由它们自己处理，不会走到这里（事件不冒泡）。
        """
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)


class SecurityToolDialog(QDialog):
    """把工具箱装进一个非模态窗口：关掉不影响聊天，重复打开复用同一个实例。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("toolDialog")
        self.setWindowTitle("安全工具箱 · MChat")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setSizeGripEnabled(False)
        self.tool = SecurityToolWidget(self)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.tool)
        self.setFixedSize(self.tool.width(), self.tool.height())

    def __getattr__(self, name: str):
        """没定义的属性转发给内部的转换器本体。

        测试与调用方因此可以直接 ``dialog.set_input(...)`` /
        ``dialog.execute()``，不必在这里写一长串薄包装。
        """
        tool = self.__dict__.get("tool")
        if tool is not None and hasattr(tool, name):
            return getattr(tool, name)
        raise AttributeError(name)

    def apply_palette(self, p: Palette) -> None:
        """对话框是顶层窗口，不继承主窗口的样式表，得自己设一份。"""
        self.tool.apply_palette(p)
        self.setStyleSheet(build_qss(p))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(p.surface))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
        self.setPalette(pal)
