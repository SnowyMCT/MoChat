"""主窗口：把侧栏、聊天区、工具箱和传输层拼起来。

职责边界
--------
* :class:`~mchat.core.store.ChatStore`   —— 数据与未读规则（不依赖 Qt）
* :class:`~mchat.core.transport.LocalTransport` —— 消息怎么送达（不依赖 Qt）
* :class:`~mchat.ui.sidebar.Sidebar` / :class:`~mchat.ui.chat_view.ChatView`
  —— 只负责显示与交互，通过信号把动作抛给本窗口
* 本文件 —— 唯一"知道全部"的地方：接线、主题、事件轮询、打开工具箱

所以想换成真联网时，只需要在 ``__init__`` 里把 ``LocalTransport`` 换成
socket 版实现，其余代码不用改。
"""
from __future__ import annotations

import sys
import time
import uuid
import os

from PyQt6.QtCore import QPoint, QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QMenu, QWidget

from ..core import seed
from ..core.db import DEFAULT_LIMIT, ChatLog
from ..core.models import ME_ID, Message
from ..core.store import MESSAGE, RESET, ChatStore, StoreEvent
from ..core.transport import (EVENT_MESSAGE, EVENT_READ, EVENT_TYPING,
                              LocalTransport, Outgoing)
from .chat_view import ChatView
from . import anim
from .search_dialog import SearchDialog
from .security_tool import SecurityToolDialog
from .sidebar import (SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_WIDTH,
                      Sidebar)
from .theme import (THEME_OPTIONS, THEME_SYSTEM, Palette, build_qss,
                    menu_qss, resolve_palette, watch_system_palette)
from .widgets import SplitHandle, Toast, confirm_destructive

__all__ = ["MainWindow", "SETTINGS_ORG", "SETTINGS_APP"]

SETTINGS_ORG = "MChat"
SETTINGS_APP = "MChat"
POLL_INTERVAL_MS = 400                      # 事件轮询间隔（真联网时同理）
CHAT_MIN_WIDTH = 420                        # 无论怎么拖，聊天区至少留这么宽


def default_log_path() -> str:
    """本地历史库的位置：跟着系统"应用数据"目录走（Windows 是 %APPDATA%\\MChat）。"""
    from PyQt6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)
    if not base:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "mchat.db")


class MainWindow(QWidget):
    """MChat 聊天窗口。"""

    def __init__(self, parent: QWidget | None = None, *, demo=None,
                 transport=None, settings=None, log=None, confirm=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle("MChat")
        self.setMinimumSize(960, 600)
        self.resize(self._preferred_size())
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        # 设置可以注入（测试用独立命名空间，避免读到用户手动拖出来的宽度）
        self._settings = (settings if settings is not None
                          else QSettings(SETTINGS_ORG, SETTINGS_APP))
        demo = demo if demo is not None else seed.build_demo()
        self._store: ChatStore = demo.store
        self._me = demo.me
        # 传输层可以注入（测试用假时钟，将来换 socket 实现也走这里）
        self._transport = transport if transport is not None else LocalTransport(
            demo.scripts)
        self._transport.start()
        self._current_id = ""
        self._sidebar_width = SIDEBAR_WIDTH
        self._tool_dialog: SecurityToolDialog | None = None
        self._search_dialog: SearchDialog | None = None
        # 本地历史库可以注入（测试用临时文件/内存库，绝不碰用户的真实数据）
        self._log = log if log is not None else ChatLog(default_log_path())
        self._log.compact()                       # 启动整理一次：WAL 收回主库，文件更小
        # 危险操作的确认弹窗也可以注入（测试里不弹窗、走固定答案）
        self._confirm = confirm or (lambda **kw: confirm_destructive(
            self, self.current_palette(), **kw))
        self._hydrated = self._hydrate_history()

        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()

        saved = str(self._settings.value("theme", THEME_OPTIONS[0]))
        self.sidebar.set_theme_name(saved if saved in THEME_OPTIONS else THEME_OPTIONS[0])
        # 动效开关（默认开；菜单里能关，也记得住）
        anim.set_enabled(str(self._settings.value("animations", "true")).lower() != "false")
        self.set_sidebar_width(self._saved_sidebar_width(), persist=False)
        watch_system_palette(self._on_system_palette_changed)
        self._apply_theme()

        self.select_conversation(demo.default_chat_id)
        self._poller = QTimer(self)
        self._poller.setInterval(POLL_INTERVAL_MS)
        self._poller.timeout.connect(self.tick)
        self._poller.start()

    # ======================================================================
    # 搭建
    # ======================================================================
    def _preferred_size(self):
        screen = QGuiApplication.primaryScreen()
        wanted = QSize(1180, 760)
        if screen is not None:
            available = screen.availableGeometry().size()
            return QSize(min(wanted.width(), available.width() - 60),
                         min(wanted.height(), available.height() - 60))
        return wanted

    def _build_ui(self) -> None:
        self.sidebar = Sidebar(self._store, self)
        self.split_handle = SplitHandle(
            self, sidebar_width=SIDEBAR_WIDTH,
            min_width=SIDEBAR_MIN_WIDTH, max_width=SIDEBAR_MAX_WIDTH)
        self.chat_view = ChatView(self._store, self,
                                  emoji_recents=self._saved_emoji_recents())

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.sidebar)
        row.addWidget(self.split_handle)
        row.addWidget(self.chat_view, 1)

        self.toast = Toast(self, bottom_offset=70)   # 抬到输入栏上方，别挡住正在打的消息

    def _connect_signals(self) -> None:
        self.sidebar.conversationSelected.connect(self.select_conversation)
        self.sidebar.toolRequested.connect(self.open_tool)
        self.sidebar.menuRequested.connect(self._show_menu)
        self.sidebar.themeChanged.connect(self.set_theme)
        self.split_handle.moved.connect(self.set_sidebar_width)
        self.split_handle.resetRequested.connect(self.reset_sidebar_width)
        self.chat_view.messageSubmitted.connect(self.send_message)
        self.chat_view.moreRequested.connect(self._show_menu)
        self.chat_view.searchRequested.connect(self.open_search)
        self.chat_view.emojiUsed.connect(self._remember_emoji)
        self.chat_view.reactionPicked.connect(self.toggle_reaction)
        self.chat_view.messageDeleted.connect(self.delete_message)
        self.chat_view.reactionModeChanged.connect(self._on_reaction_mode)
        self.sidebar.conversationMenuRequested.connect(self._show_conversation_menu)
        self._store.subscribe(self._on_store_event)
        self.sidebar.refresh()

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self.open_tool)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+E"), self,
                  activated=self.chat_view.toggle_emoji_panel)
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, activated=self.open_search)
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape)

    def _on_escape(self) -> None:
        """Esc 先收表情面板，再收提示气泡 —— 一层一层退，别一次全关。"""
        if not self.chat_view.hide_emoji_panel():
            self.toast.hide_message()

    # ======================================================================
    # 本地历史（SQLite + FTS5）
    # ======================================================================
    @property
    def log(self) -> ChatLog:
        return self._log

    def _hydrate_history(self) -> int:
        """启动时把上次的消息并回内存（界面只画最近 ``DEFAULT_LIMIT`` 条）。

        放在订阅界面事件之前调用：回填会发 RESET 事件，此时还没有监听者，
        不会白刷一遍列表。
        """
        self._apply_saved_flags()      # 先按库里的旗标调整演示数据（清空过的别再"活过来"）
        added = 0
        for conversation in self._store.conversations():
            added += self._store.hydrate(
                conversation.id, self._log.load(conversation.id, limit=DEFAULT_LIMIT))
        # 再把当前内存里的整批写回一次（演示数据 + 回填），库才是完整日志 ——
        # 这样全局搜索连"几周前的开场白"也能搜到。upsert 幂等，不会写重。
        self._log.save_messages(
            [message for conversation in self._store.conversations()
             for message in conversation.messages])
        return added

    def hydrated_count(self) -> int:
        """这次启动从本地库捞回来多少条消息（自检/测试用）。"""
        return self._hydrated

    # ======================================================================
    # 左右分界（会话列表宽度）
    # ======================================================================
    def sidebar_width(self) -> int:
        return self._sidebar_width

    def sidebar_width_limit(self) -> tuple[int, int]:
        """(最小, 最大)：上限随窗口一起收缩，保证聊天区不会被挤没。"""
        return SIDEBAR_MIN_WIDTH, max(
            SIDEBAR_MIN_WIDTH,
            min(SIDEBAR_MAX_WIDTH, self.width() - CHAT_MIN_WIDTH))

    def set_sidebar_width(self, width: int, *, persist: bool = True) -> int:
        """把会话列表宽度设成 ``width``（会夹取到允许范围内），返回实际值。

        用 ``setFixedWidth`` 而不是 ``setMinimumWidth``：拖动时布局只剩聊天区
        在伸缩，宽度就是所见即所得，不会因为窗口略窄而被布局"匀"回去。
        """
        low, high = self.sidebar_width_limit()
        width = max(low, min(high, int(width)))
        self._sidebar_width = width
        self.sidebar.setFixedWidth(width)
        self.split_handle.set_sidebar_width(width)
        if persist:
            self._settings.setValue("sidebar_width", width)
        return width

    def reset_sidebar_width(self) -> int:
        """双击分界线：回到默认宽度。"""
        return self.set_sidebar_width(SIDEBAR_WIDTH)

    def _saved_sidebar_width(self) -> int:
        raw = self._settings.value("sidebar_width", SIDEBAR_WIDTH)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return SIDEBAR_WIDTH

    # ======================================================================
    # 表情面板的"最近使用"
    # ======================================================================
    def _saved_emoji_recents(self) -> list[str]:
        """设置里存的是空格分隔的表情串（表情本身不含空格，拆回来很安全）。"""
        raw = str(self._settings.value("emoji_recent", "") or "")
        return [char for char in raw.split(" ") if char]

    def _remember_emoji(self, char: str) -> None:
        recents = self.chat_view.emoji_recents()
        self._settings.setValue("emoji_recent", " ".join(recents))

    # ======================================================================
    # 草稿（存在本地库里，切会话时自动存取）
    # ======================================================================
    def _save_draft(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        self._log.set_draft(conversation_id, self.chat_view.input_text())

    def _sync_drafts(self) -> None:
        self.sidebar.set_drafts(self._log.drafts())

    def drafts(self) -> dict[str, str]:
        return self._log.drafts()

    # ======================================================================
    # 表情回应（"点一下加、再点撤"，落在本地库里）
    # ======================================================================
    def toggle_reaction(self, message_id: str, emoji: str) -> dict[str, int]:
        counts = self._log.toggle_reaction(message_id, emoji)
        bubble = self.chat_view.message_list.bubbles().get(message_id)
        if bubble is not None:
            bubble.set_reactions(counts, self._log.mine(message_id))
        return counts

    def reactions(self, message_id: str) -> dict[str, int]:
        return self._log.reactions(message_id)

    def _sync_reactions(self, conversation_id: str) -> None:
        self.chat_view.set_reactions(self._log.reactions_for(conversation_id),
                                     self._log.mine_for(conversation_id))

    def _on_reaction_mode(self, active: bool) -> None:
        if active:
            self.toast.show_message("回应模式：点表情加到那条消息上，关掉面板退出",
                                    "info", 2400)

    # ======================================================================
    # 删除消息 / 清空历史（危险操作，一律红色二次确认）
    # ======================================================================
    def delete_message(self, message_id: str) -> bool:
        if not self._confirm(title="删除消息", ok_text="删除",
                             text="删除这条消息？本地历史里也会一起删掉，无法撤销。"):
            return False
        self._log.delete_message(message_id)
        self._store.remove(message_id)              # 会发 RESET -> 会话列表跟着刷新
        self.chat_view.remove_message(message_id)
        self.toast.show_message("消息已删除", "success", 1500)
        return True

    # ======================================================================
    # 会话的右键操作：置顶 / 免打扰 / 标记未读 / 清空这个会话
    # ======================================================================
    def _show_conversation_menu(self, conversation_id: str, global_pos) -> None:
        self._build_conversation_menu(conversation_id).exec(global_pos)

    def _build_conversation_menu(self, conversation_id: str) -> QMenu:
        """侧栏会话的右键菜单。

        单独成一个方法：``exec()`` 是阻塞的，测试没法真弹菜单，但可以直接拿到这个
        :class:`QMenu` 对象、按文字点名动作再 ``trigger()`` —— 覆盖的是同一条代码路径。
        """
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_qss())
        conversation = self._store.get(conversation_id)
        if conversation is None:
            return menu
        title = menu.addAction(conversation.title, lambda: None)
        title.setEnabled(False)
        menu.addSeparator()
        menu.addAction("取消置顶" if conversation.pinned else "置顶会话",
                       lambda: self.toggle_pin(conversation_id))
        menu.addAction("取消免打扰" if conversation.muted else "免打扰",
                       lambda: self.toggle_mute(conversation_id))
        unread = self._store.unread(conversation_id)
        mark = menu.addAction("标记为未读", lambda: self.mark_unread(conversation_id))
        mark.setEnabled(unread == 0)             # 已经有未读了就没必要再标
        if unread:
            mark.setToolTip(f"这个会话已经有 {unread} 条未读")
        menu.addSeparator()
        # 危险项加"⚠"：真正的兜底是那句红色二次确认（这里只会清掉这一个会话）
        menu.addAction("⚠ 清空这个会话的历史…",
                       lambda: self.clear_conversation_history(conversation_id))
        return menu

    def toggle_pin(self, conversation_id: str) -> bool:
        return self._toggle_flag(conversation_id, "pinned")

    def toggle_mute(self, conversation_id: str) -> bool:
        return self._toggle_flag(conversation_id, "muted")

    def _toggle_flag(self, conversation_id: str, name: str) -> bool:
        """置顶/免打扰共用的开关：改内存 → 落库 → 提示（写两遍容易漏其中一步）。"""
        conversation = self._store.get(conversation_id)
        if conversation is None:
            return False
        value = not getattr(conversation, name)
        self._store.set_flag(conversation_id, name, value)
        self._log.set_flag(conversation_id, name, value)
        labels = {
            "pinned": ("已置顶", "已取消置顶"),
            "muted": ("已开启免打扰：未读不计入总数、徽标也不再跳动", "已取消免打扰"),
        }[name]
        self.toast.show_message(labels[0] if value else labels[1], "success", 1800)
        return value

    def mark_unread(self, conversation_id: str) -> bool:
        """标记为未读：徽标立刻出来，等再点开这个会话才消。

        未读数是"这一会儿的阅读状态"，不进本地库（和 Telegram 一致：重启后不保留）。
        """
        if self._store.get(conversation_id) is None:
            return False
        if self._store.unread(conversation_id) > 0:
            return False
        self._store.set_unread(conversation_id, 1)
        return True

    def clear_conversation_history(self, conversation_id: str) -> bool:
        """清空**这一个**会话的本地历史（消息 + 回应 + 草稿），要二次确认。"""
        conversation = self._store.get(conversation_id)
        if conversation is None:
            return False
        count = len(conversation.messages)
        if not self._confirm(
                title="清空这个会话的历史", ok_text="清空",
                text=(f"会删掉「{conversation.title}」的 {count} 条本地消息、相关的表情回应"
                      "和草稿，**其它会话不受影响**，而且无法撤销。")):
            return False
        removed = self._log.clear_conversation(conversation_id)      # 库（并打 cleared 旗标）
        self._store.clear_messages(conversation_id)                 # 内存
        self._log.set_flag(conversation_id, "cleared", True)
        if conversation_id == self._current_id:
            self.chat_view.show_conversation(self._store.get(conversation_id))
            self.chat_view.set_input_text("")                       # 草稿也一起没了
            self.chat_view.set_reactions(
                self._log.reactions_for(conversation_id),
                self._log.mine_for(conversation_id))
        self._sync_drafts()
        self.sidebar.refresh_item(conversation_id)
        self.toast.show_message(
            f"「{conversation.title}」的历史已清空（{removed} 条）", "success", 2000)
        return True

    def _apply_saved_flags(self) -> None:
        """启动时把库里的会话旗标贴回演示数据。

        演示数据每次启动都会重新生成，所以"清空过的会话"必须在这里把种子消息丢掉，
        否则上次的清空会白清（``cleared`` 旗标就是为这个存的）。
        """
        for conversation_id, names in self._log.flags().items():
            conversation = self._store.get(conversation_id)
            if conversation is None:
                continue
            conversation.pinned = "pinned" in names
            conversation.muted = "muted" in names
            if "cleared" in names:
                conversation.messages.clear()

    def open_data_dir(self) -> None:
        """打开库文件所在目录（用系统文件管理器）。"""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        folder = os.path.dirname(os.path.abspath(self._log.path))
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def log_stats(self) -> dict:
        return self._log.stats()

    # ======================================================================
    # 全局搜索
    # ======================================================================
    def open_search(self) -> SearchDialog:
        """打开"搜索当前会话消息"（非模态、复用同一个；查的是本地历史库的 FTS5 索引）。

        范围默认是**你现在开着的这个会话**：从哪个会话点进来就搜哪个会话。
        """
        return self._open_search(scope="current")

    def open_search_all(self) -> SearchDialog:
        """打开搜索窗口，范围=全部会话（想跨会话找时才用）。"""
        return self._open_search(scope="all")

    def _open_search(self, *, scope: str) -> SearchDialog:
        if self._search_dialog is None:
            self._search_dialog = SearchDialog(self._log, self._conversation_titles(), self)
            self._search_dialog.messageSelected.connect(self.jump_to_message)
        dialog = self._search_dialog
        dialog.set_titles(self._conversation_titles())
        dialog.set_scope(self._current_id, mode=scope)
        dialog.apply_palette(self.current_palette())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.search_field.focus_field()
        return dialog

    def search_dialog(self) -> SearchDialog | None:
        return self._search_dialog

    def _sync_search_scope(self, conversation_id: str) -> None:
        """"仅当前会话"模式下，搜索范围跟着会话走（"全部会话"模式不动它）。"""
        if self._search_dialog is not None:
            self._search_dialog.set_current(conversation_id)

    def _conversation_titles(self) -> dict[str, str]:
        return {conv.id: conv.title for conv in self._store.conversations()}

    def jump_to_message(self, conversation_id: str, message_id: str) -> bool:
        """跳到某条消息：切会话 -> 滚过去闪一下；超出"最近一批"就从库里补出来。"""
        if conversation_id != self._current_id and not self.select_conversation(conversation_id):
            return False
        if self.chat_view.flash_message(message_id):
            return True
        conversation = self._store.get(conversation_id)
        if conversation is None:
            return False
        # 界面里只画了最近 DEFAULT_LIMIT 条，这条更早：向库里多要一批再重画
        self._store.hydrate(conversation_id,
                            self._log.load(conversation_id, limit=DEFAULT_LIMIT * 5))
        self.chat_view.show_conversation(conversation)
        self._sync_reactions(conversation_id)
        return self.chat_view.flash_message(message_id)

    # ======================================================================
    # 会话
    # ======================================================================
    @property
    def store(self) -> ChatStore:
        return self._store

    @property
    def transport(self):
        return self._transport

    def current_conversation_id(self) -> str:
        return self._current_id

    def select_conversation(self, conversation_id: str) -> bool:
        conversation = self._store.get(conversation_id)
        if conversation is None:
            return False
        changed = conversation_id != self._current_id
        if changed:
            self._save_draft(self._current_id)          # 切走前把没发出去的存起来
        self._current_id = conversation_id
        self.chat_view.show_conversation(conversation)
        self.sidebar.set_current(conversation_id)
        self._store.mark_read(conversation_id)
        if changed:
            self.chat_view.set_input_text(self._log.draft(conversation_id))
        self._sync_drafts()
        self._sync_reactions(conversation_id)
        self._sync_search_scope(conversation_id)    # 搜索范围跟着"我现在看的会话"走
        return True

    def send_message(self, text: str) -> bool:
        """把输入框里的文字发给当前会话。"""
        text = text.strip()
        if not text:
            self.toast.show_message("空消息不能发送", "warning", 1800)
            return False
        conversation_id = self._current_id
        if not conversation_id:
            self.toast.show_message("先选择一个会话", "warning", 1800)
            return False
        message = Message(
            id=f"out-{uuid.uuid4().hex[:8]}",
            conversation_id=conversation_id,
            sender_id=ME_ID,
            text=text,
            ts=time.time(),
            outgoing=True,
        )
        self._store.append(message)
        self._transport.send(Outgoing(conversation_id, ME_ID, text, message.id))
        self._log.set_draft(conversation_id, "")        # 发出去了，草稿作废
        self._sync_drafts()
        return True

    # ======================================================================
    # 传输层事件轮询
    # ======================================================================
    def tick(self, now: float | None = None) -> int:
        """把传输层里"已经到了"的事件应用到仓库上，返回处理条数。

        ``now`` 留空就用传输层自己的时钟（真联网时由它决定，测试里可注入假时钟）。
        """
        events = self._transport.poll(now)
        for event in events:
            if event.kind == EVENT_MESSAGE:
                # 回复到达即结束"正在输入…"
                self.sidebar.set_typing(event.conversation_id, False)
                if event.conversation_id == self._current_id:
                    self.chat_view.set_typing(False)
                self._store.receive(Message(
                    id=f"in-{uuid.uuid4().hex[:8]}",
                    conversation_id=event.conversation_id,
                    sender_id=event.sender_id,
                    text=event.text,
                    ts=event.ts or time.time(),
                    outgoing=False,
                ))
            elif event.kind == EVENT_TYPING:
                self.sidebar.set_typing(event.conversation_id, event.typing)
                if event.conversation_id == self._current_id:
                    self.chat_view.set_typing(event.typing)
            elif event.kind == EVENT_READ:
                self._store.set_read(event.conversation_id, event.message_id)
        return len(events)

    def _on_store_event(self, event: StoreEvent) -> None:
        if event.kind == RESET:
            self.sidebar.refresh()
            return
        if event.kind == MESSAGE and event.message is not None:
            message = event.message
            self._log.save_message(message)             # 写穿：幂等 upsert，一条一条也不慢
            self.chat_view.sync_message(message)
            # 正开着的会话不留未读
            if message.conversation_id == self._current_id and not message.outgoing:
                self._store.mark_read(message.conversation_id)
        self.sidebar.refresh_item(event.conversation_id)

    # ======================================================================
    # 主题
    # ======================================================================
    def set_theme(self, name: str) -> None:
        if name not in THEME_OPTIONS:
            return
        self._settings.setValue("theme", name)
        self.sidebar.set_theme_name(name)      # 让分段控件与主题保持一致
        self._apply_theme(animate=True)        # 用户点的一下：交叉淡入，别硬切

    def theme_name(self) -> str:
        return self.sidebar.theme_seg.value()

    def current_palette(self) -> Palette:
        return resolve_palette(self.theme_name())

    def _on_system_palette_changed(self) -> None:
        if self.theme_name() == THEME_SYSTEM:
            self._apply_theme(animate=True)

    def _apply_theme(self, *, animate: bool = False) -> None:
        # 换主题前先把"旧样子"抓成一张图盖上，换完再让它淡出 —— 视觉上就是交叉淡入
        overlay = anim.cross_fade(self) if animate else None
        p = self.current_palette()
        self.setStyleSheet(build_qss(p))
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(p.window))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
        pal.setColor(QPalette.ColorRole.Text, QColor(p.text))
        pal.setColor(QPalette.ColorRole.Base, QColor(p.card))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_on))
        self.setPalette(pal)

        self.sidebar.apply_palette(p)
        self.split_handle.apply_palette(p)
        self.chat_view.apply_palette(p)
        self.toast.apply_palette(p)
        if self._tool_dialog is not None:
            self._tool_dialog.apply_palette(p)
        if self._search_dialog is not None:
            self._search_dialog.apply_palette(p)
        if overlay is not None:
            self.toast.raise_()             # 提示气泡别被快照盖住

    def theme_overlay(self):
        """正在做主题交叉淡入时返回那个叠层（测试用；没在淡入就是 None）。"""
        for child in self.children():
            if isinstance(child, anim.SnapshotOverlay) and child.isVisible():
                return child
        return None

    # ======================================================================
    # 动效开关
    # ======================================================================
    def set_animations(self, enabled: bool) -> None:
        """开/关界面动效（菜单里那个勾选项，选择会记住）。"""
        anim.set_enabled(bool(enabled))
        self._settings.setValue("animations", "true" if enabled else "false")
        self.toast.show_message("界面动效已开启" if enabled else "界面动效已关闭",
                                "info", 1500)

    def animations_enabled(self) -> bool:
        return anim.is_enabled()

    # ======================================================================
    # 工具箱 / 菜单
    # ======================================================================
    def open_tool(self, prefill: str = "") -> SecurityToolDialog:
        """打开安全工具箱（非模态、复用同一个窗口）。"""
        if self._tool_dialog is None:
            self._tool_dialog = SecurityToolDialog(self)
        dialog = self._tool_dialog
        dialog.apply_palette(self.current_palette())
        if prefill:
            dialog.select_algorithm(0)
            dialog.set_input(prefill)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.tool.input_field.focus_field()
        return dialog

    def tool_dialog(self) -> SecurityToolDialog | None:
        return self._tool_dialog

    def _show_menu(self) -> None:
        """侧栏/聊天头部共用的"更多"菜单。"""
        self._build_menu().exec(self.mapToGlobal(QPoint(self.width() // 2, 60)))

    def _build_menu(self) -> QMenu:
        """构造"更多"菜单（拆出来是为了测试能点名动作，不用真的弹出来）。

        注意这里**不再**有"标记当前会话为已读"和"清空本地历史"：前者已删，
        后者挪到了侧栏会话的右键菜单里，而且只清那一个会话。
        """
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_qss())
        menu.addAction("安全工具箱（Ctrl+T）", self.open_tool)
        menu.addAction("表情面板（Ctrl+E）", self.chat_view.toggle_emoji_panel)
        menu.addAction("搜索当前会话消息（Ctrl+Shift+F）", self.open_search)
        menu.addAction("在所有会话中搜索…", self.open_search_all)
        pending = self.chat_view.input_text().strip()
        action = menu.addAction("把输入框的文字送进工具箱", self._send_input_to_tool)
        action.setEnabled(bool(pending))
        if not pending:
            action.setToolTip("输入框为空")
        menu.addSeparator()
        stats = self._log.stats()
        info = menu.addAction(
            f"本地历史：{stats['messages']} 条 · {stats['reactions']} 个回应 · "
            f"{stats['bytes'] / 1024:.0f} KB", lambda: None)
        info.setEnabled(False)
        menu.addAction("打开数据目录", self.open_data_dir)
        effects = menu.addAction("界面动效")
        effects.setCheckable(True)
        effects.setChecked(anim.is_enabled())
        effects.toggled.connect(self.set_animations)
        menu.addAction(f"当前传输层：{self._transport.name}（未联网）", lambda: None).setEnabled(False)
        return menu

    def _menu_qss(self) -> str:
        return menu_qss(self.current_palette())

    def _send_input_to_tool(self) -> None:
        text = self.chat_view.input_text().strip()
        if not text:
            self.toast.show_message("输入框是空的", "warning", 1600)
            return
        self.open_tool(prefill=text)
        self.toast.show_message("已把输入框的文字送进工具箱", "success", 1800)

    def _focus_search(self) -> None:
        self.sidebar.search_field.focus_field()

    # ======================================================================
    # 窗口杂项
    # ======================================================================
    def status_text(self) -> str:
        return self.toast.message()

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self.toast.reposition()

    def closeEvent(self, event) -> None:                 # noqa: N802
        self._poller.stop()
        self._transport.stop()
        if self._search_dialog is not None:
            self._search_dialog.close()
        self._save_draft(self._current_id)               # 关窗前把草稿存住
        self._log.close()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    """程序入口：python main.py"""
    # 高分屏按真实缩放比渲染，避免汉字被截断
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(SETTINGS_APP)
    app.setOrganizationName(SETTINGS_ORG)
    window = MainWindow()
    window.show()
    return app.exec()
