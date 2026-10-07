"""全局消息搜索：查的是本地历史库的 FTS5 索引（原生 C），点结果跳到那条消息。

为什么是独立窗口
----------------
搜索要"跨会话看结果、点进去跳转"，塞进侧栏会把列表挤变形；做成和工具箱一样的
**非模态窗口**最省事（关掉不影响聊天，重复打开复用同一个）。

性能
----
* 查询本身由 SQLite 完成（trigram 倒排索引，实测 2 万条里 13ms）；
* 输入**防抖 180ms**：打字过程中不查询，停下来才查一次，避免每敲一个字母就查库；
* 结果最多 60 行，行控件复用同一套样式；国旗用的是仓库里的本地小图（不联网）。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..core.models import format_list_time
from . import anim
from . import flag_text
from .theme import build_qss
from .widgets import FloatingLabelInput, SegmentedControl, install_app_chrome

__all__ = ["SearchDialog"]

DEBOUNCE_MS = 180
MAX_ROWS = 60
SCOPE_CURRENT = "仅当前会话"
SCOPE_ALL = "全部会话"


class ResultRow(QFrame):
    """一条搜索结果：会话名 + 时间 + 命中片段（点/回车都算选中）。"""

    picked = pyqtSignal(str, str)                # (conversation_id, message_id)

    def __init__(self, hit, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.setObjectName("searchRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.conversation_id = hit.conversation_id
        self.message_id = hit.message_id

        self.title_label = QLabel(f"{title}　·　{subtitle}", self)
        self.title_label.setObjectName("searchTitle")
        self.time_label = QLabel(format_list_time(hit.ts), self)
        self.time_label.setObjectName("searchTime")
        self._plain_text = hit.snippet.strip() or "（空）"
        self.snippet_label = QLabel(self)
        self.snippet_label.setObjectName("searchSnippet")
        self.snippet_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                         QSizePolicy.Policy.Preferred)
        # 命中片段也是 QTextDocument 渲染的文本：国旗要换成 <img>（否则只有 "JP" 字母）
        payload, rich = flag_text.to_display(self._plain_text,
                                             size=flag_text.PREVIEW_IMAGE_SIZE)
        self.snippet_label.setText(payload)
        self.snippet_label.setTextFormat(Qt.TextFormat.RichText if rich
                                         else Qt.TextFormat.PlainText)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.title_label)
        top.addStretch(1)
        top.addWidget(self.time_label)

        column = QVBoxLayout(self)
        column.setContentsMargins(10, 7, 10, 7)
        column.setSpacing(2)
        column.addLayout(top)
        column.addWidget(self.snippet_label)

    def mousePressEvent(self, event) -> None:            # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.conversation_id, self.message_id)
        super().mousePressEvent(event)


class SearchDialog(QDialog):
    """搜索窗口。

    ``log`` 只需有 ``search(query, limit, conversation_id)``；会话名由 ``titles`` 提供。

    **范围**：默认"仅当前会话" —— 从哪个会话点进来就搜哪个会话；顶部的分段控件可以
    随时切到"全部会话"（切回来看当前会话时范围会跟着走，见 :meth:`set_scope`）。
    """

    messageSelected = pyqtSignal(str, str)        # (conversation_id, message_id)
    scopeModeChanged = pyqtSignal(str)            # "current" / "all"

    def __init__(self, log, titles: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("搜索全部消息")
        self.setObjectName("searchRoot")
        self.setMinimumSize(520, 420)
        self.resize(560, 460)
        self._log = log
        self._titles = dict(titles)
        self._rows: list[ResultRow] = []
        self._scope_id: str = ""                  # 空 = 不限会话
        self._scope_mode = "all"
        self._current_id: str = ""                # 主窗口当前正开着的会话

        self.search_field = FloatingLabelInput(
            "搜索全部消息（中文或英文，例如 日本 / telegram）", field_height=40)
        self.search_field.textChanged.connect(self._on_text)

        self.scope_control = SegmentedControl([SCOPE_CURRENT, SCOPE_ALL], height=30,
                                              compact=True)
        self.scope_control.valueChanged.connect(self._on_scope_control)

        self.hint = QLabel("输入关键词开始搜索：跨全部会话，命中本地历史库的索引", self)
        self.hint.setObjectName("searchHint")
        self.hint.setWordWrap(True)

        self.list_host = QWidget(self)
        self.list_host.setObjectName("searchList")
        self._list_layout = QVBoxLayout(self.list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch(1)

        area = QScrollArea(self)
        area.setObjectName("searchArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self.list_host)
        area.viewport().setAutoFillBackground(False)
        self._area = area

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.scope_control)
        top.addStretch(1)

        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(8)
        box.addWidget(self.search_field)
        box.addLayout(top)
        box.addWidget(self.hint)
        box.addWidget(area, 1)

        # 无边框 + 自绘 ✕（系统标题栏那套不再出现）
        self.chrome = install_app_chrome(self, title=self.windowTitle())

        # 防抖：打字时只重置计时器，停手 180ms 才真的查库
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self.run_search)

    # ------------------------------------------------------------------ 对外
    def set_scope(self, conversation_id: str | None, *, mode: str | None = None) -> None:
        """设定搜索范围。``mode`` 省略时：给了会话就是"仅当前会话"，没给就是"全部会话"。

        主窗口在切换会话时会调用它（只在"仅当前会话"模式下跟着走），所以从任何会话
        打开/切过去，搜的都是"你现在看的这个会话"。
        """
        if conversation_id:
            self._current_id = conversation_id
        self._scope_mode = mode or ("current" if conversation_id else "all")
        self._scope_id = self._current_id if self._scope_mode == "current" else ""
        index = 0 if self._scope_mode == "current" else 1
        self.scope_control.set_index(index, notify=False)
        self.scope_control.setEnabled(bool(self._current_id))
        self._apply_scope_labels()
        if self.query().strip():
            self.run_search()                     # 范围变了，结果立刻重算
        else:
            self._show_idle_hint()
        self.scopeModeChanged.emit(self._scope_mode)

    def set_current(self, conversation_id: str) -> None:
        """主窗口换了会话：跟着挪的只有"仅当前会话"模式（"全部会话"模式不受影响）。"""
        self._current_id = conversation_id or ""
        self.scope_control.setEnabled(bool(self._current_id))
        if self._scope_mode == "current":
            self.set_scope(self._current_id, mode="current")

    def scope_mode(self) -> str:
        return self._scope_mode

    def scope_id(self) -> str:
        return self._scope_id

    def scope_title(self) -> str:
        return self._titles.get(self._scope_id, self._scope_id) if self._scope_id else "全部会话"

    def set_titles(self, titles: dict[str, str]) -> None:
        """会话名有变化（比如清空历史后重铺）时刷新一下。"""
        self._titles = dict(titles)
        self._apply_scope_labels()

    def set_query(self, text: str) -> None:
        self.search_field.setText(text)               # textChanged 会带出搜索

    def query(self) -> str:
        return self.search_field.text()

    def results(self) -> list[tuple[str, str]]:
        """当前结果 ``[(conversation_id, message_id), …]``（测试与跳转都用它）。"""
        return [(row.conversation_id, row.message_id) for row in self._rows]

    def row_count(self) -> int:
        return len(self._rows)

    def run_search(self) -> int:
        """真的查一次库（测试里可以直接调，不用等防抖）。"""
        self._timer.stop()
        query = self.query().strip()
        for row in self._rows:
            anim.cancel(row)                   # 先停动画再删（行上的淡入可能还没跑完）
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        if not query:
            self._show_idle_hint()
            return 0
        hits = self._log.search(query, limit=MAX_ROWS,
                                conversation_id=self._scope_id or None)
        for hit in hits:
            title = self._titles.get(hit.conversation_id, hit.conversation_id)
            sender = "我" if hit.outgoing else "对方"
            row = ResultRow(hit, title, sender, self.list_host)
            row.picked.connect(self._on_picked)
            row.show()                                 # 运行时新建的控件要显式显示
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._rows.append(row)
        anim.stagger_fade_in(self._rows)                # 结果行错峰淡入，一次刷新别"啪"地全出来
        if not hits:
            if self._scope_id:
                self.hint.setText(f"「{self.scope_title()}」里没有包含「{query}」的消息"
                                  "　·　切到「全部会话」再找找")
            else:
                self.hint.setText(f"没有找到包含「{query}」的消息")
            return 0
        more = f"（只显示最近 {MAX_ROWS} 条）" if len(hits) >= MAX_ROWS else ""
        where = f"在「{self.scope_title()}」" if self._scope_id else ""
        self.hint.setText(f"「{query}」{where}命中 {len(hits)} 条{more}　·　点一行跳过去")
        return len(hits)

    def pick_first(self) -> bool:
        """选中第一条结果（回车/测试用）。"""
        if not self._rows:
            return False
        self._on_picked(self._rows[0].conversation_id, self._rows[0].message_id)
        return True

    def apply_palette(self, palette) -> None:
        self.setStyleSheet(build_qss(palette))
        self.search_field.apply_palette(palette)
        self.scope_control.apply_palette(palette)
        self.chrome.apply_palette(palette)

    # ------------------------------------------------------------------ 内部
    def _show_idle_hint(self) -> None:
        if self._scope_id:
            self.hint.setText(f"在「{self.scope_title()}」里搜索：输入关键词开始")
        else:
            self.hint.setText("输入关键词开始搜索")

    def _apply_scope_labels(self) -> None:
        self.setWindowTitle(f"搜索 · {self.scope_title()}")
        self.search_field.set_label(
            f"在「{self.scope_title()}」里搜索消息" if self._scope_id
            else "搜索全部消息")

    def _on_scope_control(self, value: str) -> None:
        """分段控件：切到"仅当前会话"就收窄到主窗口开着的会话。"""
        mode = "current" if value == SCOPE_CURRENT else "all"
        if mode == "current" and not self._current_id:
            self.scope_control.set_index(1, notify=False)
            return
        self.set_scope(self._current_id if mode == "current" else None, mode=mode)

    def _on_text(self, _text: str) -> None:
        self._timer.start()                            # 重新计时（防抖）

    def _on_picked(self, conversation_id: str, message_id: str) -> None:
        self.messageSelected.emit(conversation_id, message_id)

    def keyPressEvent(self, event) -> None:            # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.pick_first()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:                # noqa: N802
        super().showEvent(event)
        self.search_field.focus_field()
