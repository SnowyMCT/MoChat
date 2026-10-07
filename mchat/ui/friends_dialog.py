"""好友界面：好友列表 / 待处理申请 / 加好友 / 删好友。

为什么单独一个窗口（而不是塞进侧栏）：好友关系是**服务端权威**的，这一页要能一次
看清三件事 —— 我的好友、别人申请加我（要同意/拒绝）、我申请加别人（等回应）——
再叠上\"搜索用户发申请\"，信息量放不进 296px 的侧栏。所以侧栏只留一个入口按钮 +
红点角标，真正的操作都在这个固定尺寸的小窗里（和新建群聊窗口一致的写法）。

几条铁律在这个文件里的落点：
* **固定尺寸**：``setFixedSize`` + 原生不可拉伸标志（只设前者 Windows 上边框还会出现双向箭头）。
* **删除是破坏性操作**：红色警示按钮 + 确认弹窗，文案必须说清\"只解除好友关系、
  历史聊天记录保留、对方以后要重新申请\"（见 :data:`REMOVE_WARNING`）。
* **数据来自 store**：本窗口不自己存好友名单，一律读 :class:`~mchat.core.store.ChatStore`
  （服务端回来时主窗口会 ``set_friends`` 落进去）。做完一次操作只发 ``changed``
  信号，让主窗口去 ``GET /api/friends`` 对账 —— 客户端不自己猜结果。
* **网络调用全在后台**：走 :class:`~mchat.ui.async_call.AsyncCall`，窗口全程可拖动。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                            QScrollArea, QSizePolicy, QToolButton, QVBoxLayout,
                            QWidget)

from .async_call import AsyncCall
from .avatar import Avatar
from .theme import Palette, build_qss, resolve_palette
from .widgets import (CHROME_H, FloatingLabelInput, SegmentedControl,
                      confirm_destructive, install_app_chrome)

__all__ = ["FriendsDialog", "PersonRow", "MODE_FRIENDS", "MODE_REQUESTS",
           "REMOVE_WARNING", "BLOCKED_NOTICE"]

MODE_FRIENDS = "好友"
MODE_REQUESTS = "申请"
DIALOG_W = 460
DIALOG_H = 560
ROW_H = 56
RESULTS_H = 150
SEARCH_DEBOUNCE_MS = 250

#: 删除好友确认框里必须说清楚的话（用户亲口定的文案方向）。
REMOVE_WARNING = ("删除好友只会解除好友关系，历史聊天记录会保留；"
                  "对方以后需要重新申请才能再聊。")

#: 被好友门禁拦下的消息给用户的明确提示（配合气泡上的\"已发送好友申请\"）。
BLOCKED_NOTICE = "对方还不是你的好友，已发送好友申请，等对方同意后即可聊天。"


def _handle(user) -> str:
    """行里那行 ``@用户名``；拿不到就留空（别显示一个孤零零的 @）。

    协议里的 User 是带 username 的，但演示/本地数据可能没有 —— 所以这里兜一下。
    """
    username = str(getattr(user, "username", "") or "")
    return "@" + username if username else ""


class PersonRow(QFrame):
    """一行\"人\"：头像 + 昵称 + @用户名（+ 可选附言），右侧可挂按钮。

    整行高度固定、说明文字走 ``Ignored`` 尺寸策略：长附言只会被行宽截断，
    绝不会把整行撑宽（铁律：行文字截断不溢出）。
    """

    def __init__(self, name: str, handle: str = "", note: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("userRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(ROW_H)
        self.avatar = Avatar(self, size=38, name=name)
        self.name_label = QLabel(name, self)
        self.name_label.setObjectName("userName")
        self.handle_label = QLabel(handle, self)
        self.handle_label.setObjectName("userHandle")
        self.handle_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        self.note_label = QLabel(note, self)
        self.note_label.setObjectName("dialogHint")
        self.note_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(bool(note))

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.name_label)
        text.addWidget(self.handle_label)
        if note:
            text.addWidget(self.note_label)

        self.trailing = QHBoxLayout()
        self.trailing.setSpacing(6)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 10, 6)
        row.setSpacing(10)
        row.addWidget(self.avatar)
        row.addLayout(text, 1)
        row.addLayout(self.trailing)

    def add_button(self, text: str, *, danger: bool = False,
                   primary: bool = False) -> QPushButton:
        """右侧挂一个按钮（危险操作走 ``dangerBtn``：红底，视觉上先拦一下）。"""
        button = QPushButton(text, self)
        button.setObjectName("dangerBtn" if danger else
                             ("primary" if primary else "ghostBtn"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedHeight(30)
        self.trailing.addWidget(button)
        return button

    def add_note_widget(self, widget: QWidget) -> None:
        self.trailing.addWidget(widget)

    def apply_palette(self, palette: Palette) -> None:
        self.avatar.apply_palette(palette)


class FriendsDialog(QDialog):
    """好友 / 申请 / 加好友 / 删好友。``changed`` 通知主窗口去服务端对账。"""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, api=None, store=None,
                 confirm=None, theme_name: str = ""):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle("好友")
        self.setFixedSize(DIALOG_W, DIALOG_H)
        # 只 setFixedSize 的话 Windows 边框仍会出现双向箭头；这一条才是原生\"拉不动\"
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, True)
        self._api = api
        self._store = store
        self._palette: Palette | None = None
        # 确认弹窗可注入（测试里走固定答案，不真弹窗）
        self._confirm = confirm if confirm is not None else (
            lambda **kw: confirm_destructive(
                self, self._palette or resolve_palette(""), **kw))
        self._friends_rows: list[PersonRow] = []
        self._incoming_rows: list[PersonRow] = []
        self._outgoing_rows: list[PersonRow] = []
        self._result_rows: list[PersonRow] = []
        # 三条独立通道：搜索 / 好友操作 / 互不阻塞（和新建群聊窗口一个路子）
        self._search_call = AsyncCall(self)
        self._search_call.finished.connect(self._on_search_done)
        self._search_call.failed.connect(self._on_failed)
        self._act_call = AsyncCall(self)
        self._act_call.finished.connect(self._on_action_done)
        self._act_call.failed.connect(self._on_failed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.run_search)
        self._build_ui()
        if theme_name:
            self.apply_palette(resolve_palette(theme_name))
        self.refresh()

    # ================================================================== 搭建
    def _build_ui(self) -> None:
        self.title = QLabel("好友", self)
        self.title.setObjectName("title")
        self.subtitle = QLabel("管理好友与申请；群聊不受好友关系影响", self)
        self.subtitle.setObjectName("subtitle")

        self.mode_seg = SegmentedControl([MODE_FRIENDS, MODE_REQUESTS], height=34)
        self.mode_seg.valueChanged.connect(self._on_mode_changed)

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

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("dialogStatus")
        self.status_label.setWordWrap(True)

        self.friends_page = self._build_friends_page()
        self.requests_page = self._build_requests_page()

        box = QVBoxLayout(self)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addLayout(head)
        box.addWidget(self.subtitle)
        box.addWidget(self.mode_seg)
        box.addWidget(self.friends_page, 1)
        box.addWidget(self.requests_page, 1)
        box.addWidget(self.status_label)
        self._on_mode_changed(MODE_FRIENDS)

        # 无边框 + 自绘 ✕；窗口是固定尺寸的，高度要把标题条那一条算进去
        self.chrome = install_app_chrome(self, title=self.windowTitle())
        self.setFixedSize(DIALOG_W, DIALOG_H + CHROME_H)

    def _make_scroll(self, host: QWidget) -> QScrollArea:
        area = QScrollArea(self)
        area.setObjectName("convArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(host)
        return area

    def _build_friends_page(self) -> QWidget:
        self.search_field = FloatingLabelInput("搜索用户名或昵称", field_height=40)
        self.search_field.textChanged.connect(self._on_keyword)
        self.search_field.submitted.connect(self.run_search)
        self.note_field = FloatingLabelInput("申请附言（可选）", field_height=40)

        self.results_host = QWidget()
        self.results_layout = QVBoxLayout(self.results_host)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(4)
        self.results_layout.addStretch(1)
        self.results_area = self._make_scroll(self.results_host)
        self.results_area.setFixedHeight(RESULTS_H)

        self.friends_title = QLabel("我的好友", self)
        self.friends_title.setObjectName("sideSectionTitle")

        self.friends_host = QWidget()
        self.friends_layout = QVBoxLayout(self.friends_host)
        self.friends_layout.setContentsMargins(0, 0, 0, 0)
        self.friends_layout.setSpacing(4)
        self.friends_layout.addStretch(1)
        self.friends_area = self._make_scroll(self.friends_host)

        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        box.addWidget(self.search_field)
        box.addWidget(self.note_field)
        box.addWidget(self.results_area)
        box.addWidget(self.friends_title)
        box.addWidget(self.friends_area, 1)
        return page

    def _build_requests_page(self) -> QWidget:
        self.requests_host = QWidget()
        self.requests_layout = QVBoxLayout(self.requests_host)
        self.requests_layout.setContentsMargins(0, 0, 0, 0)
        self.requests_layout.setSpacing(4)
        self.requests_layout.addStretch(1)
        self.requests_area = self._make_scroll(self.requests_host)

        # 两个分区标题 + 两个子容器都塞进同一个滚动区，靠 _reset_host 重建
        self.incoming_head = QLabel("待处理申请", self.requests_host)
        self.incoming_head.setObjectName("sideSectionTitle")
        self.incoming_host = QWidget(self.requests_host)
        self.incoming_layout = QVBoxLayout(self.incoming_host)
        self.incoming_layout.setContentsMargins(0, 0, 0, 0)
        self.incoming_layout.setSpacing(4)
        self.incoming_layout.addStretch(1)
        self.outgoing_head = QLabel("我发出的申请", self.requests_host)
        self.outgoing_head.setObjectName("sideSectionTitle")
        self.outgoing_host = QWidget(self.requests_host)
        self.outgoing_layout = QVBoxLayout(self.outgoing_host)
        self.outgoing_layout.setContentsMargins(0, 0, 0, 0)
        self.outgoing_layout.setSpacing(4)
        self.outgoing_layout.addStretch(1)

        self.requests_layout.insertWidget(0, self.incoming_head)
        self.requests_layout.insertWidget(1, self.incoming_host)
        self.requests_layout.insertWidget(2, self.outgoing_head)
        self.requests_layout.insertWidget(3, self.outgoing_host)

        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.requests_area, 1)
        return page

    # ================================================================== 状态
    def mode(self) -> str:
        return self.mode_seg.value()

    def set_mode(self, mode: str) -> None:
        if mode in (MODE_FRIENDS, MODE_REQUESTS):
            self.mode_seg.set_index([MODE_FRIENDS, MODE_REQUESTS].index(mode),
                                    notify=False)

    def _on_mode_changed(self, mode: str) -> None:
        self.friends_page.setVisible(mode == MODE_FRIENDS)
        self.requests_page.setVisible(mode == MODE_REQUESTS)

    def status_text(self) -> str:
        return self.status_label.text()

    def set_status(self, text: str, kind: str = "info") -> None:
        self.status_label.setText(str(text))
        color = None
        if self._palette is not None:
            color = {"warning": self._palette.warning,
                     "error": self._palette.danger}.get(kind)
        if color:
            self.status_label.setStyleSheet(f"color: {color};")
        elif self._palette is not None:
            self.status_label.setStyleSheet(f"color: {self._palette.muted};")

    # ---- 计数（测试与主窗口角标用） --------------------------------------
    def friend_count(self) -> int:
        return len(self._friends_rows)

    def incoming_count(self) -> int:
        return len(self._incoming_rows)

    def outgoing_count(self) -> int:
        return len(self._outgoing_rows)

    def friend_names(self) -> list[str]:
        return [row.name_label.text() for row in self._friends_rows]

    def incoming_names(self) -> list[str]:
        return [row.name_label.text() for row in self._incoming_rows]

    # ================================================================== 渲染
    def _reset_host(self, layout) -> None:
        """清空一个行容器（重建比逐个 diff 简单，行数很少）。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        layout.addStretch(1)

    def _insert(self, layout, widget: QWidget) -> None:
        layout.insertWidget(layout.count() - 1, widget)
        widget.show()

    def _empty_hint(self, parent: QWidget, text: str) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("dialogHint")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _clear_results(self) -> None:
        self._reset_host(self.results_layout)
        self._result_rows = []

    def refresh(self) -> None:
        """按 store 里的\"当前这一份\"重建三张名单（服务端是权威）。"""
        friends = list(self._store.friends()) if self._store is not None else []
        incoming = list(self._store.incoming_requests()) if self._store is not None else []
        outgoing = list(self._store.outgoing_requests()) if self._store is not None else []
        self._fill_friends(friends)
        self._fill_incoming(incoming)
        self._fill_outgoing(outgoing)
        self.incoming_head.setText(
            f"待处理申请{('（' + str(len(incoming)) + '）') if incoming else ''}")
        self.outgoing_head.setText(
            f"我发出的申请{('（' + str(len(outgoing)) + '）') if outgoing else ''}")

    def _fill_friends(self, friends) -> None:
        self._reset_host(self.friends_layout)
        self._friends_rows = []
        if not friends:
            self._insert(self.friends_layout,
                         self._empty_hint(self.friends_host,
                                          "还没有好友。在上面搜个人发个申请吧。"))
            return
        for friend in friends:
            row = PersonRow(friend.name, _handle(friend.user), parent=self.friends_host)
            button = row.add_button("删除", danger=True)
            button.clicked.connect(
                lambda _checked=False, u=friend.user.username, r=row:
                self.remove_friend(u, name=r.name_label.text()))
            self._insert(self.friends_layout, row)
            self._friends_rows.append(row)

    def _fill_incoming(self, requests) -> None:
        self._reset_host(self.incoming_layout)
        self._incoming_rows = []
        if not requests:
            self._insert(self.incoming_layout,
                         self._empty_hint(self.incoming_host, "没有待处理的申请。"))
            return
        for request in requests:
            note = request.note.strip()
            row = PersonRow(request.name, _handle(request.user),
                            f"附言：{note}" if note else "", self.incoming_host)
            accept = row.add_button("同意", primary=True)
            accept.clicked.connect(
                lambda _checked=False, u=request.user.username: self.accept_request(u))
            reject = row.add_button("拒绝")
            reject.clicked.connect(
                lambda _checked=False, u=request.user.username: self.reject_request(u))
            self._insert(self.incoming_layout, row)
            self._incoming_rows.append(row)

    def _fill_outgoing(self, requests) -> None:
        self._reset_host(self.outgoing_layout)
        self._outgoing_rows = []
        if not requests:
            self._insert(self.outgoing_layout,
                         self._empty_hint(self.outgoing_host, "没有等待回应的申请。"))
            return
        for request in requests:
            row = PersonRow(request.name, _handle(request.user), "", self.outgoing_host)
            waiting = QLabel("等待对方同意", self.outgoing_host)
            waiting.setObjectName("dialogHint")
            row.add_note_widget(waiting)
            self._insert(self.outgoing_layout, row)
            self._outgoing_rows.append(row)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(build_qss(palette))
        self.chrome.apply_palette(palette)
        self.mode_seg.apply_palette(palette)
        self.search_field.apply_palette(palette)
        self.note_field.apply_palette(palette)
        for host in (self.friends_host, self.incoming_host,
                     self.outgoing_host, self.results_host):
            for row in host.findChildren(PersonRow):
                row.apply_palette(palette)
        for label in (self.subtitle, self.status_label):
            label.setStyleSheet(label.styleSheet())
        self.set_status(self.status_label.text())

    # ================================================================== 搜索
    def _on_keyword(self, _text: str) -> None:
        self._debounce.start()

    def set_keyword(self, text: str) -> None:
        self.search_field.setText(text)

    def run_search(self) -> bool:
        """搜人（复用服务端的用户搜索）。没接服务端时什么都不做。"""
        self._debounce.stop()
        if self._api is None:
            self.set_status("未连接服务端，无法搜索用户", "warning")
            return False
        keyword = self.search_field.text().strip()

        def work():
            return {"users": self._api.search_users(keyword)}

        self._search_call.run(work)
        return True

    def is_searching(self) -> bool:
        return self._search_call.is_running()

    def _on_search_done(self, result) -> None:
        users = list((result or {}).get("users") or [])
        self._clear_results()
        if not users:
            self._insert(self.results_layout,
                         self._empty_hint(self.results_host, "没搜到人。"))
            self.set_status("没搜到匹配的用户", "warning")
            return
        for user in users:
            username = str((user or {}).get("username") or "")
            if not username:
                continue
            display = str((user or {}).get("display_name") or username)
            row = PersonRow(display, f"@{username}", parent=self.results_host)
            button = row.add_button("加好友", primary=True)
            button.clicked.connect(
                lambda _checked=False, u=username: self.send_request(u))
            self._insert(self.results_layout, row)
            self._result_rows.append(row)
        self.set_status(f"搜到 {len(users)} 个用户")

    # ================================================================== 操作
    def _busy(self) -> bool:
        if self._api is None:
            self.set_status("未连接服务端，操作不可用", "warning")
            return True
        if self._act_call.is_running():
            return True
        return False

    def send_request(self, username: str) -> bool:
        """给某人发好友申请（附言取输入框，可选）。"""
        if self._busy():
            return False
        note = self.note_field.text().strip()

        def work():
            return {"result": self._api.friend_request(username, note)}

        self.set_status(f"已向 @{username} 发送好友申请")
        self._act_call.run(work)
        return True

    def accept_request(self, username: str) -> bool:
        if self._busy():
            return False

        def work():
            return {"result": self._api.friend_accept(username)}

        self.set_status(f"已同意 @{username} 的申请")
        self._act_call.run(work)
        return True

    def reject_request(self, username: str) -> bool:
        if self._busy():
            return False

        def work():
            return {"result": self._api.friend_reject(username)}

        self.set_status(f"已拒绝 @{username} 的申请")
        self._act_call.run(work)
        return True

    def remove_friend(self, username: str, *, name: str = "") -> bool:
        """删除好友：**先红色警示二次确认**，确认后调服务端（历史记录服务端保留）。"""
        if self._api is None:
            self.set_status("未连接服务端，操作不可用", "warning")
            return False
        who = name or username
        if not self._confirm(
                title="删除好友", ok_text="删除好友",
                text=f"确定要删除好友「{who}」吗？\n\n{REMOVE_WARNING}"):
            return False
        if self._act_call.is_running():
            return False

        def work():
            return {"result": self._api.friend_remove(username)}

        self.set_status(f"已删除好友 @{username}")
        self._act_call.run(work)
        return True

    def _on_action_done(self, _result) -> None:
        # 操作落地了：让主窗口去服务端对账（自己不改 store，服务端是权威）
        self.changed.emit()

    def _on_failed(self, error) -> None:
        text = getattr(error, "friendly", "") or str(error) or "操作失败"
        self.set_status(text, "error")
