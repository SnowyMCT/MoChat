"""新建会话窗口：找到人开聊 / 拉个群。

真实的聊天软件不能只有"演示里那几个现成的会话" —— 用户得能自己找人。这个窗口给两条路：

* **找人聊天**：按用户名或昵称搜（服务端 ``/api/users``），点一下就在服务端建一个单聊
  （幂等：之前聊过就带你回原来的那个会话，不会在两边各留一个空壳）；
* **新建群聊**：填群名 + 勾几个人（同样靠搜索），一次建好，自己是群主。

窗口是**固定尺寸**的小窗口（和项目里其它小窗一致：``setFixedSize`` + 原生
"不可拉伸"窗口标志，只设前者 Windows 上边框仍会出现双向箭头）。搜索走防抖 250ms，
所有网络调用都在后台线程，窗口全程可拖动。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QToolButton, QVBoxLayout,
                             QWidget)

from ..core.api import ApiError, NetworkError
from .async_call import AsyncCall
from .avatar import Avatar
from .theme import Palette, build_qss
from .widgets import FloatingLabelInput, SegmentedControl

__all__ = ["NewChatDialog", "UserRow", "MODE_DIRECT", "MODE_GROUP"]

MODE_DIRECT = "找人聊天"
MODE_GROUP = "新建群聊"
DIALOG_W = 440
DIALOG_H = 540
ROW_H = 52
SEARCH_DEBOUNCE_MS = 250


class UserRow(QFrame):
    """搜索结果里的一行：头像 + 昵称 + @用户名。整行可点（Telegram 的习惯）。"""

    clicked = pyqtSignal(str)                  # 用户名

    def __init__(self, user: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("userRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(ROW_H)
        self.username = str(user.get("username") or "")
        self.display_name = str(user.get("display_name") or self.username)
        self.online = bool(user.get("online"))
        self._selected = False

        self.avatar = Avatar(self, size=38, name=self.display_name)
        self.name_label = QLabel(self.display_name, self)
        self.name_label.setObjectName("userName")
        self.handle_label = QLabel(f"@{self.username}"
                                   + (" · 在线" if self.online else ""), self)
        self.handle_label.setObjectName("userHandle")
        self.mark = QLabel("", self)
        self.mark.setObjectName("dialogHint")

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.name_label)
        text.addWidget(self.handle_label)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 10, 6)
        row.setSpacing(10)
        row.addWidget(self.avatar)
        row.addLayout(text, 1)
        row.addWidget(self.mark)

    def set_selected(self, selected: bool) -> None:
        """群聊模式下勾选（单聊模式下永远是 False，点了直接开聊）。"""
        if selected == self._selected:
            return
        self._selected = bool(selected)
        self.setProperty("selected", "true" if self._selected else "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.mark.setText("✓ 已选" if self._selected else "")

    def is_selected(self) -> bool:
        return self._selected

    def apply_palette(self, palette: Palette) -> None:
        self.avatar.apply_palette(palette)

    def mousePressEvent(self, event) -> None:               # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.username)
        super().mousePressEvent(event)


class NewChatDialog(QDialog):
    """找到人就能开聊的窗口。``conversationCreated`` 携带服务端返回的会话。"""

    conversationCreated = pyqtSignal(object)

    def __init__(self, parent=None, *, api=None, theme_name: str = ""):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle("新建会话")
        self.setFixedSize(DIALOG_W, DIALOG_H)
        # 只 setFixedSize 的话 Windows 边框还会给出双向箭头；这一条才是原生"拉不动"
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, True)
        self._api = api
        self._palette: Palette | None = None
        self._rows: dict[str, UserRow] = {}
        self._selected: list[str] = []
        # 两条独立的通道：搜索和"建会话"分开 —— 共用一条的话，搜索还在飞的时候
        # 点一行会被 `is_running()` 静默挡掉（用户会以为点了没反应）
        self._search_call = AsyncCall(self)
        self._search_call.finished.connect(self._on_search_done)
        self._search_call.failed.connect(self._on_failed)
        self._act_call = AsyncCall(self)
        self._act_call.finished.connect(self._on_done)
        self._act_call.failed.connect(self._on_failed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.run_search)
        self._build_ui()
        self.apply_theme(theme_name)

    # ================================================================== 搭建
    def _build_ui(self) -> None:
        self.title = QLabel("新建会话", self)
        self.title.setObjectName("title")
        self.subtitle = QLabel("搜到人就能开聊；群里可以一次选好几个人", self)
        self.subtitle.setObjectName("subtitle")

        self.mode_seg = SegmentedControl([MODE_DIRECT, MODE_GROUP], height=34)
        self.mode_seg.valueChanged.connect(self._on_mode_changed)

        self.search_field = FloatingLabelInput("搜索用户名或昵称", field_height=42)
        self.search_field.textChanged.connect(self._on_keyword)
        self.search_field.submitted.connect(self.run_search)

        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        self.rows_layout.addStretch(1)
        self.rows_area = QScrollArea(self)
        self.rows_area.setObjectName("convArea")
        self.rows_area.setWidgetResizable(True)
        self.rows_area.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_area.setWidget(self.rows_host)

        self.hint_label = QLabel("输入几个字就开始搜（用户名、昵称都能搜）", self)
        self.hint_label.setObjectName("dialogHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)

        # ---- 建群那几件东西（切到「找人聊天」时整块藏起来）----
        self.title_field = FloatingLabelInput("群名称", field_height=42)
        self.picked_label = QLabel("还没选人", self)
        self.picked_label.setObjectName("dialogHint")
        self.create_btn = QPushButton("创建群聊", self)
        self.create_btn.setObjectName("primary")
        self.create_btn.setFixedHeight(40)
        self.create_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_btn.clicked.connect(self.create_group)
        self.group_box = QWidget(self)
        group = QVBoxLayout(self.group_box)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(8)
        group.addWidget(self.title_field)
        group.addWidget(self.picked_label)
        group.addWidget(self.create_btn)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("dialogStatus")
        self.status_label.setWordWrap(True)

        self.close_btn = QToolButton(self)
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setText("✕")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.clicked.connect(self.close)

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.close_btn)

        box = QVBoxLayout(self)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addLayout(head)
        box.addWidget(self.subtitle)
        box.addWidget(self.mode_seg)
        box.addWidget(self.group_box)
        box.addWidget(self.search_field)
        box.addWidget(self.hint_label)
        box.addWidget(self.rows_area, 1)
        box.addWidget(self.status_label)
        self._on_mode_changed(MODE_DIRECT)

    # ================================================================== 状态
    def mode(self) -> str:
        return self.mode_seg.value()

    def set_mode(self, mode: str) -> None:
        if mode in (MODE_DIRECT, MODE_GROUP):
            self.mode_seg.set_index([MODE_DIRECT, MODE_GROUP].index(mode), notify=False)
            self._on_mode_changed(mode)

    def _on_mode_changed(self, mode: str) -> None:
        building = mode == MODE_GROUP
        self.group_box.setVisible(building)
        self.hint_label.setText(
            "勾几个人（可多选），填个群名，点「创建群聊」" if building
            else "输入几个字就开始搜（用户名、昵称都能搜）")
        for row in self._rows.values():
            row.set_selected(row.username in self._selected if building else False)
        self.set_status("", "info")

    def status_text(self) -> str:
        return self.status_label.text()

    def set_status(self, text: str, kind: str = "info") -> None:
        self.status_label.setText(text)
        colors = {"info": self._palette.muted if self._palette else "#8A8F98",
                  "error": self._palette.danger if self._palette else "#D64545",
                  "success": self._palette.success if self._palette else "#2E9E5B"}
        self.status_label.setStyleSheet(
            f"color: {colors.get(kind, colors['info'])}; font-size: 12px;")

    def rows(self) -> dict[str, UserRow]:
        return dict(self._rows)

    def selected_members(self) -> list[str]:
        return list(self._selected)

    def is_busy(self) -> bool:
        return self._search_call.is_running() or self._act_call.is_running()

    def is_searching(self) -> bool:
        return self._search_call.is_running()

    # ================================================================== 搜索
    def _on_keyword(self, _text: str) -> None:
        self._debounce.start()                # 防抖：边敲边搜会白打一堆请求

    def set_keyword(self, text: str) -> None:
        """程序化设置搜索词（测试用；也会照常触发防抖）。"""
        self.search_field.setText(text)

    def run_search(self) -> bool:
        keyword = self.search_field.text().strip()
        if self._api is None:
            self.set_status("没有连上服务器", "error")
            return False

        def work():
            return {"users": self._api.search_users(keyword)}

        if not self._search_call.run(work):
            return False
        self.set_status(f"正在找「{keyword}」…" if keyword else "正在加载最近活跃的人…",
                        "info")
        return True

    def _clear_rows(self) -> None:
        for row in self._rows.values():
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

    def _fill_rows(self, users: list) -> None:
        self._clear_rows()
        for user in users:
            username = str((user or {}).get("username") or "")
            if not username:
                continue
            row = UserRow(user, self.rows_host)
            row.clicked.connect(self.choose_user)
            row.set_selected(username in self._selected and self.mode() == MODE_GROUP)
            if self._palette is not None:
                row.apply_palette(self._palette)
            row.show()
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            self._rows[username] = row

    # ================================================================== 动作
    def choose_user(self, username: str) -> None:
        """点一行：群聊模式下是勾选，单聊模式下是直接开聊。"""
        if self.mode() == MODE_GROUP:
            self.toggle_member(username)
            return
        if self._api is None or self._act_call.is_running():
            return

        def work():
            return {"conversation": self._api.create_direct(username)}

        if not self._act_call.run(work):
            return
        self.set_status(f"正在打开和 {username} 的会话…", "info")

    def toggle_member(self, username: str) -> bool:
        if username in self._selected:
            self._selected.remove(username)
            picked = False
        else:
            self._selected.append(username)
            picked = True
        row = self._rows.get(username)
        if row is not None:
            row.set_selected(picked)
        self._sync_picked()
        return picked

    def _sync_picked(self) -> None:
        if self._selected:
            self.picked_label.setText("已选：" + "、".join(self._selected))
        else:
            self.picked_label.setText("还没选人")

    def create_group(self) -> bool:
        title = self.title_field.text().strip()
        if not title:
            self.set_status("给群起个名字", "error")
            self.title_field.flash_error()
            return False
        if not self._selected:
            self.set_status("至少选一个人", "error")
            return False
        if self._api is None or self._act_call.is_running():
            return False

        def work():
            return {"conversation": self._api.create_group(title, list(self._selected))}

        if not self._act_call.run(work):
            return False
        self.set_status(f"正在创建「{title}」…", "info")
        return True

    # ================================================================== 回调
    def _on_search_done(self, result) -> None:
        """搜索回来了：铺结果行（``_fill_rows`` 会按当前模式决定要不要勾选）。"""
        payload = dict(result or {})
        users = list(payload.get("users") or [])
        self._fill_rows(users)
        keyword = self.search_field.text().strip()
        if users:
            self.set_status(f"找到 {len(users)} 个人"
                            + ("" if self.mode() == MODE_GROUP else "，点一下开聊"),
                            "success")
        else:
            self.set_status(f"没找到「{keyword}」——用户名要完全一样的几个字符", "info")

    def _on_done(self, result) -> None:
        """建会话回来了：把服务端给的会话抛出去、关窗。"""
        conversation = dict((result or {}).get("conversation") or {})
        if not conversation.get("id"):
            self.set_status("服务端没返回会话", "error")
            return
        self.set_status("好了", "success")
        self.conversationCreated.emit(conversation)
        self.close()

    def _on_failed(self, error) -> None:
        if isinstance(error, ApiError):
            self.set_status(error.friendly, "error")
        elif isinstance(error, NetworkError):
            self.set_status(f"{error.friendly}（检查一下网络/服务器）", "error")
        else:
            self.set_status(f"出错了：{type(error).__name__}: {error}", "error")

    # ================================================================== 皮肤
    def apply_theme(self, name: str) -> None:
        from .theme import resolve_palette
        self.apply_palette(resolve_palette(name) if name else
                           resolve_palette("亮色"))

    def apply_palette(self, palette: Palette) -> None:
        """``QDialog`` 是顶层窗口，不继承主窗口样式，必须自己上一份。"""
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        self.search_field.apply_palette(palette)
        self.title_field.apply_palette(palette)
        self.mode_seg.apply_palette(palette)
        self.subtitle.setStyleSheet(f"color: {palette.muted};")
        for row in self._rows.values():
            row.apply_palette(palette)
        self.set_status(self.status_label.text(), "info")
