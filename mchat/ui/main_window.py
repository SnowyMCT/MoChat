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
import tempfile
import time
import uuid
import os

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (QColor, QDesktopServices, QGuiApplication, QKeySequence,
                         QPalette, QShortcut)
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QMenu, QWidget

from ..core import seed
from ..core.crashlog import default_crash_log_path, install_crash_log
from ..core.db import DEFAULT_LIMIT, ChatLog
from ..core.models import ME_ID, Attachment, ChatKind, Message
from ..core.store import MESSAGE, PIN, RESET, ChatStore, StoreEvent
from ..core.transport import (EVENT_MESSAGE, EVENT_READ, EVENT_TYPING,
                              LocalTransport, Outgoing)
from .attach import pick_file
from .chat_view import ATTACH_FILE, ChatView
from . import anim
from .forward_dialog import ForwardDialog
from .forward_view import MergedViewerDialog
from .pin_dialog import ask_pin as ask_pin_dialog
from .search_dialog import SearchDialog
from .security_tool import SecurityToolDialog
from .settings_dialog import SettingsDialog
from ..core.store import confirmation_preview
from .sidebar import (SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_WIDTH,
                      Sidebar)
from .theme import (THEME_OPTIONS, THEME_SYSTEM, Palette, build_qss,
                    clamp_font_scale, font_scale, label_of, menu_qss,
                    resolve_palette, set_font_scale, watch_system_palette)
from .widgets import SplitHandle, Toast, confirm_destructive

__all__ = ["MainWindow", "SETTINGS_ORG", "SETTINGS_APP"]

SETTINGS_ORG = "MChat"
SETTINGS_APP = "MChat"
POLL_INTERVAL_MS = 400                      # 事件轮询间隔（真联网时同理）
CHAT_MIN_WIDTH = 420                        # 无论怎么拖，聊天区至少留这么宽
CLIPBOARD_DIRNAME = "clipboard"              # 粘进来的图片落在这个子目录里


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
                 transport=None, settings=None, log=None, confirm=None,
                 filedialog=None, ask_pin=None, clipboard=None):
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
        self._settings_dialog: SettingsDialog | None = None
        self._forward_dialog: ForwardDialog | None = None
        self._merged_viewer: MergedViewerDialog | None = None
        # 各会话"还没发出去的附件/待回复的那条"（和草稿一样按会话存，切换会话不会串台）
        self._pending_attachments: dict[str, Attachment] = {}
        self._pending_replies: dict[str, object] = {}
        self._forward_ids: list[str] = []          # 转发窗口里"这次要转发哪几条"
        # 本地历史库可以注入（测试用临时文件/内存库，绝不碰用户的真实数据）
        self._log = log if log is not None else ChatLog(default_log_path())
        self._log.compact()                       # 启动整理一次：WAL 收回主库，文件更小
        # 危险操作的确认弹窗也可以注入（测试里不弹窗、走固定答案）
        self.ask_pin = ask_pin or ask_pin_dialog
        # 剪贴板也是可注入的：测试里换成假实现，免得跑一次测试就把用户
        # 剪贴板里的东西顶掉（真机上"复制"当然还是写系统剪贴板）
        self._clipboard = clipboard if clipboard is not None else QGuiApplication.clipboard()
        self._confirm = confirm or (lambda **kw: confirm_destructive(
            self, self.current_palette(), **kw))
        # 文件选择框同样可以注入（QFileDialog 是模态窗口，无头测试里会卡住）
        self._pick = filedialog if filedialog is not None else pick_file
        self._hydrated = self._hydrate_history()

        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()

        saved = str(self._settings.value("theme", THEME_OPTIONS[0]))
        self.sidebar.set_theme_name(saved if saved in THEME_OPTIONS else THEME_OPTIONS[0])
        # 动效开关（默认开；菜单/设置里能关，也记得住）
        anim.set_enabled(str(self._settings.value("animations", "true")).lower() != "false")
        # 文字大小（全局字号系数，默认标准；设置里能改，也记得住）
        set_font_scale(self._saved_text_scale())
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
        self.sidebar.themeChanged.connect(self.set_theme)
        self.split_handle.moved.connect(self.set_sidebar_width)
        self.split_handle.resetRequested.connect(self.reset_sidebar_width)
        self.chat_view.messageSubmitted.connect(self.send_message)
        self.chat_view.searchRequested.connect(self.open_search)
        self.chat_view.emojiUsed.connect(self._remember_emoji)
        self.chat_view.reactionPicked.connect(self.toggle_reaction)
        self.chat_view.messageDeleted.connect(self.delete_message)
        self.chat_view.reactionModeChanged.connect(self._on_reaction_mode)
        # 附件：选文件 / 打开附件 / 输入框里粘进来一张图
        self.chat_view.attachRequested.connect(self.attach_file)
        self.chat_view.attachmentRequested.connect(self.attachment_action)
        self.chat_view.imagePasted.connect(self.attach_pasted_image)
        # 气泡右键菜单：回复 / 复制 / 置顶 / 转发 / 批量选择
        self.chat_view.replyRequested.connect(self.start_reply)
        self.chat_view.copyRequested.connect(self.copy_message)
        self.chat_view.pinRequested.connect(self.toggle_pin_message)
        self.chat_view.unpinRequested.connect(self.unpin_current)
        self.chat_view.forwardRequested.connect(self.open_forward)
        self.chat_view.deleteRequested.connect(self.delete_messages)
        self.chat_view.quoteTapped.connect(self.jump_to_quoted)
        self.chat_view.mergedOpened.connect(self.open_merged_view)
        self.chat_view.replyCleared.connect(self._on_reply_cleared)
        self.sidebar.settingsRequested.connect(self.open_settings)
        self.sidebar.conversationMenuRequested.connect(self._show_conversation_menu)
        self._store.subscribe(self._on_store_event)
        self.sidebar.refresh()

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self.open_tool)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+E"), self,
                  activated=self.chat_view.toggle_emoji_panel)
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, activated=self.open_search)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape)

    def _on_escape(self) -> None:
        """Esc 一层一层退：先退批量选择，再收表情面板，最后收提示气泡。"""
        if self.chat_view.selection_mode():
            self.chat_view.end_selection()
            return
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

    def _stash_attachment(self, conversation_id: str) -> None:
        """把当前挂着的待发送附件记到 ``conversation_id`` 名下。

        和草稿同一个道理：附件属于"那个会话里正在组织的一条消息"，切到别的会话
        必须是干净的（否则会在另一个会话里误发出去）。
        """
        if not conversation_id:
            return
        attachment = self.chat_view.pending_attachment()
        if attachment is None:
            self._pending_attachments.pop(conversation_id, None)
        else:
            self._pending_attachments[conversation_id] = attachment

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
        """删一条消息 —— **本地删除，不是撤回**：别人发的也能删，对方设备上照样看得到。"""
        if not self._confirm(title="删除这条消息", ok_text="删除",
                             text="只从你这边的本地历史里删掉，**不是撤回** —— "
                                  "对方设备上仍然看得到。删掉之后无法撤销。"):
            return False
        self._log.delete_message(message_id)
        self._store.remove(message_id)              # 会发 RESET -> 会话列表跟着刷新
        self.chat_view.remove_message(message_id)
        self._sync_pin_banner()                     # 删掉的正好是被置顶的那条时收起横幅
        self.toast.show_message("消息已删除", "success", 1500)
        return True

    # ======================================================================
    # 气泡右键菜单：回复 / 复制 / 置顶 / 转发 / 批量删除
    # ======================================================================
    def start_reply(self, message_id: str) -> object:
        """右键"回复"：把那一条挂到输入栏上方（按会话存，切走再切回来还在）。"""
        _, message = self._store.locate(message_id)
        if message is None:
            return None
        reply = self._store.reply_ref(message)
        self._pending_replies[self._current_id] = reply
        self.chat_view.set_reply_target(reply)
        self.chat_view.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.toast.show_message(f"回复 {reply.author}", "info", 1500)
        return reply

    def reply_target(self) -> object:
        return self.chat_view.reply_target()

    def clear_reply(self) -> None:
        """取消回复（✕ 那一下也走这里，保证"界面 + 按会话存的记录"一起清）。"""
        self._pending_replies.pop(self._current_id, None)
        self.chat_view.clear_reply()

    def _on_reply_cleared(self) -> None:
        self._pending_replies.pop(self._current_id, None)

    def copy_message(self, message_id: str) -> str:
        """右键"复制"：复制消息正文；纯附件消息就复制文件名（Telegram 也是这样）。

        没有可复制的内容时明确提示，而不是静默失败 —— 用户会以为剪贴板里有东西。
        """
        _, message = self._store.locate(message_id)
        if message is None:
            return ""
        text = message.text.strip()
        if not text and message.attachment is not None:
            text = message.attachment.name
        if not text:
            self.toast.show_message("这条消息没有可复制的文字", "warning", 1800)
            return ""
        self._clipboard.setText(text)
        self.toast.show_message(f"已复制 {len(text)} 个字符", "success", 1500)
        return text

    def toggle_pin_message(self, message_id: str, for_both: bool | None = None) -> bool:
        """右键「置顶」：先弹**置顶小窗**问一句"要不要也给对方置顶"。

        * 已经置顶的那条 → 走"取消置顶"，**不问**（和 Telegram 一致）；
        * ``for_both=None``（默认）→ 弹窗；``True/False`` → 直接按这个范围置顶
          （测试和"改范围"这类内部调用用得上，省得再弹一次窗）；
        * 小窗里点取消 → 什么都不做，返回 False。
        """
        conversation, message = self._store.locate(message_id)
        if conversation is None or message is None:
            return False
        before = self._store.pin_ref(conversation.id, message_id)
        if before is None and for_both is None:
            answer = self.ask_pin(self, self.current_palette(),
                                  peer_label=self.pin_option_label(conversation))
            if answer is None:                      # 用户点了取消
                return False
            for_both = answer
        if not self._store.pin_message(message_id, for_both=bool(for_both)):
            return False
        after = self._store.pin_ref(conversation.id, message_id)
        self._log.set_pinned_refs(conversation.id, conversation.pins)
        self._sync_pin_banner()
        total = len(conversation.pins)
        if after is None:
            note = "已取消这条消息的置顶"
        elif before is not None:
            note = f"已改成「{after.label}」"
        else:
            note = f"已置顶（{after.label}）" + (f"，这个会话共 {total} 条" if total > 1 else "")
        self.toast.show_message(note, "info", 1900)
        return True

    def pin_option_label(self, conversation) -> str:
        """置顶小窗那个勾选项的文案：单聊写对方名字，群聊说"所有群成员"。"""
        if conversation.kind == ChatKind.GROUP:
            return "也为所有群成员置顶"
        peer = conversation.peer_name or conversation.title
        return f"也为 {peer} 置顶"

    def unpin_current(self, message_id: str = "") -> bool:
        """取消某一条置顶（横幅上的 ✕ 带的就是当前显示那条的 id）；不传就取消显示的这条。"""
        target = message_id or self.chat_view.pinned_message_id()
        if not target or not self._store.unpin_message(target):
            return False
        conversation, _ = self._store.locate(target)
        if conversation is not None:
            self._log.set_pinned_refs(conversation.id, conversation.pins)
        self._sync_pin_banner()
        self.toast.show_message("已取消这一条的置顶（其余的留着）", "info", 1800)
        return True

    def _sync_pin_banner(self) -> None:
        """把当前会话的置顶整串交给横幅（可能多条，每条带"仅我可见 / 双方可见"）。"""
        conversation = self._store.get(self._current_id) if self._current_id else None
        pins = []
        if conversation is not None:
            for message, pin in self._store.pinned_entries(conversation.id):
                pins.append((message, self._store.author_name(message), pin))
        self.chat_view.set_pinned(pins)

    def jump_to_quoted(self, message_id: str) -> bool:
        """点引用块/置顶横幅：跳到那条消息（可能要先切会话）。"""
        conversation, message = self._store.locate(message_id)
        if conversation is None or message is None:
            self.toast.show_message("那条消息已经不在了", "warning", 1800)
            return False
        if conversation.id != self._current_id:
            self.select_conversation(conversation.id)
        return self.jump_to_message(conversation.id, message_id)

    # ---- 转发 ----
    def open_forward(self, message_ids) -> ForwardDialog | None:
        """打开转发窗口（复用同一个）：列会话 + 选"逐条 / 合并"。"""
        ids = [message_id for message_id in message_ids
               if self._store.message(message_id) is not None]
        if not ids:
            self.toast.show_message("没有可转发的消息", "warning", 1800)
            return None
        self._forward_ids = ids
        if self._forward_dialog is None:
            dialog = ForwardDialog(self)
            dialog.forwardRequested.connect(self.forward_messages)
            self._forward_dialog = dialog
        dialog = self._forward_dialog
        dialog.set_targets(self._forward_targets())
        dialog.set_count(len(ids))
        dialog.apply_palette(self.current_palette())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def forward_dialog(self) -> ForwardDialog | None:
        return self._forward_dialog

    # ---- 合并转发的"聊天记录"窗口 ----
    def open_merged_view(self, message_id: str) -> MergedViewerDialog | None:
        """点开一条合并转发：在独立窗口里按 QQ 那种聊天记录逐条看（非模态、复用同一个）。"""
        _, message = self._store.locate(message_id)
        if message is None or not message.is_merged:
            return None
        if self._merged_viewer is None:
            self._merged_viewer = MergedViewerDialog(self)
        viewer = self._merged_viewer
        viewer.set_message(message)
        viewer.apply_palette(self.current_palette())
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()
        return viewer

    def merged_viewer(self) -> MergedViewerDialog | None:
        return self._merged_viewer

    def _forward_targets(self) -> list[tuple[object, str]]:
        """转发窗口里列哪些会话：全部（含当前会话），副标题用最后一条消息的摘要。"""
        targets = []
        for conversation in self._store.conversations():
            last = conversation.last_message
            targets.append((conversation,
                            "还没有消息" if last is None else conversation.preview_text()))
        return targets

    def forward_messages(self, target_id: str, merge: bool) -> list[Message]:
        """真转发：逐条转发 or 合成一条"合并转发"，落库并跳到目标会话看结果。

        转发出去的每一条都**跟手打发送走同一条路**（`store.forward` 造消息 +
        `transport.send` 交给传输层）：这样才会收到"对方已读"回执（气泡从单勾变双勾）、
        对面也会按脚本回一句。不调 `transport.send` 的话转发消息会永远停在单勾、
        对面像没收到一样 —— 这正是之前漏掉的一步。
        """
        ids = [message_id for message_id in self._forward_ids]
        created = self._store.forward(ids, target_id, merge=merge)
        if not created:
            self.toast.show_message("转发失败：消息已经不在了", "warning", 1800)
            return []
        for message in created:
            self._transport.send(Outgoing(target_id, ME_ID, message.send_text,
                                          message.id, attachment=message.attachment))
        self._forward_ids = []
        target = self._store.get(target_id)
        title = target.title if target is not None else "会话"
        self.chat_view.end_selection()               # 转完退出批量选择
        self.select_conversation(target_id)          # 跳过去看看结果（Telegram 也这样）
        self.toast.show_message(
            f"已转发 {len(ids)} 条到「{title}」" + ("（合并成一条）" if merge else ""),
            "success", 2200)
        return created

    # ---- 批量删除 ----
    def delete_messages(self, message_ids) -> int:
        """批量删除选中的消息（**本地删除，不是撤回**：谁发的都能删，对方那边不受影响）。"""
        picked = [message for message in (self._store.message(message_id)
                                          for message_id in message_ids)
                  if message is not None]
        if not picked:
            self.toast.show_message("选中的消息都不在了", "warning", 2000)
            return 0
        question = (f"删除选中的 {len(picked)} 条消息？只会从你这边的本地历史里删掉，"
                    "**不是撤回** —— 对方设备上仍然看得到。")
        if not self._confirm(title="删除选中的消息", ok_text="删除", text=question,
                             detail=confirmation_preview(picked)):
            return 0
        for message in picked:                       # 先删库（本地历史）再删内存
            self._log.delete_message(message.id)
        removed = self._store.delete_messages([message.id for message in picked])
        for message in picked:
            self.chat_view.remove_message(message.id)
        self.chat_view.end_selection()
        self._sync_pin_banner()
        self.toast.show_message(f"已删除 {removed} 条消息（仅本地）", "success", 1800)
        return removed

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
        self._pending_attachments.pop(conversation_id, None)        # 待发送的附件也一并清掉
        self._pending_replies.pop(conversation_id, None)            # 待回复的那条同理
        self._store.get(conversation_id).pins = ()                  # 库里已被 clear_conversation 清掉
        self._log.set_flag(conversation_id, "cleared", True)
        if conversation_id == self._current_id:
            self.chat_view.show_conversation(self._store.get(conversation_id))
            self.chat_view.set_input_text("")                       # 草稿也一起没了
            self.chat_view.clear_attachment()
            self._sync_pin_banner()
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
        否则上次的清空会白清（``cleared`` 旗标就是为这个存的）。会话内置顶的那条
        也在这里贴回来（置顶的可能是用户自己发的消息，历史回填之后才认得出来）。
        """
        for conversation_id, names in self._log.flags().items():
            conversation = self._store.get(conversation_id)
            if conversation is None:
                continue
            conversation.pinned = "pinned" in names
            conversation.muted = "muted" in names
            if "cleared" in names:
                conversation.messages.clear()
        for conversation_id, refs in self._log.pinned_refs().items():
            conversation = self._store.get(conversation_id)
            if conversation is None:
                continue
            # 指向的那些消息里，不在了的（被删/被清空）直接剔掉，顺手把库里也写干净
            alive = tuple(pin for pin in refs
                          if self._store.message(pin.message_id) is not None)
            if len(alive) != len(refs):
                self._log.set_pinned_refs(conversation_id, alive)
            conversation.pins = alive

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
            self._stash_attachment(self._current_id)    # 附件同理：不跟着人跑
        self._current_id = conversation_id
        self.chat_view.show_conversation(conversation)
        self.sidebar.set_current(conversation_id)
        self._store.mark_read(conversation_id)
        if changed:
            self.chat_view.set_input_text(self._log.draft(conversation_id))
            self.chat_view.set_attachment(self._pending_attachments.get(conversation_id))
            self.chat_view.set_reply_target(self._pending_replies.get(conversation_id))
        self._sync_pin_banner()                     # 置顶横幅按新会话重刷
        self._sync_drafts()
        self._sync_reactions(conversation_id)
        self._sync_search_scope(conversation_id)    # 搜索范围跟着"我现在看的会话"走
        return True

    def send_message(self, text: str) -> bool:
        """把输入框里的文字（和挂着的附件）发给当前会话。

        纯附件消息是合法的（空文字 + 附件），所以"空消息"的判据是**两样都没有**。
        """
        text = text.strip()
        attachment = self.chat_view.pending_attachment()
        if not text and attachment is None:
            self.toast.show_message("空消息不能发送", "warning", 1800)
            return False
        conversation_id = self._current_id
        if not conversation_id:
            self.toast.show_message("先选择一个会话", "warning", 1800)
            return False
        if attachment is not None and not attachment.exists:
            # 选进来以后文件被删/移走了：当场说清楚，别发一条点不开的消息
            self.toast.show_message(f"附件不在这个位置了：{attachment.name}", "warning", 2400)
            self.chat_view.clear_attachment()
            return False
        reply = self.chat_view.reply_target()
        message = Message(
            id=f"out-{uuid.uuid4().hex[:8]}",
            conversation_id=conversation_id,
            sender_id=ME_ID,
            text=text,
            ts=time.time(),
            outgoing=True,
            attachment=attachment,
            reply_to=reply,                          # 回复：带上被引用那条的快照
        )
        self.chat_view.clear_attachment()               # 附件跟着这条消息走了
        self._pending_attachments.pop(conversation_id, None)
        self._pending_replies.pop(conversation_id, None)
        self.chat_view.clear_reply()                    # 回复条一起收掉（引用已经进消息了）
        self._store.append(message)
        self._transport.send(Outgoing(conversation_id, ME_ID, text, message.id,
                                      attachment=attachment))
        self._log.set_draft(conversation_id, "")        # 发出去了，草稿作废
        self._sync_drafts()
        return True

    # ======================================================================
    # 附件：图片 / 文件
    # ======================================================================
    def attach_file(self, kind: str = ATTACH_FILE) -> Attachment | None:
        """点"图片/文件"：弹选择框，选中的文件挂到输入栏上方等发送（不是立刻发）。

        真的发送走 :meth:`send_message` —— 用户可以补一句配文再一起发出去。
        """
        path = self._pick(kind, self)
        if not path:
            return None
        if not os.path.isfile(path):
            self.toast.show_message("这个文件找不到了", "warning", 1800)
            return None
        attachment = Attachment.from_path(path)
        self.chat_view.set_attachment(attachment)
        self.chat_view.focus_input()
        return attachment

    def attach_pasted_image(self, image) -> Attachment | None:
        """输入框里粘进来一张图：先落成 PNG（在本地库旁边的 clipboard/ 下），再当附件。

        为什么不直接拿 QImage 发：消息里要存的是"一个路径"，库、传输层、附件的显示
        都只认路径；落地之后这件事就和"选了一个图片文件"完全一样，只有一条路径要维护。
        """
        if image is None or image.isNull():
            return None
        folder = self.clipboard_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            name = time.strftime("粘贴的图片-%Y%m%d-%H%M%S") + ".png"
            path = os.path.join(folder, name)
            if not image.save(path, "PNG"):
                raise OSError("QImage.save 失败")
        except OSError as error:                 # 权限/磁盘满之类：说清楚而不是静默
            self.toast.show_message(f"这张图片存不下来：{error}", "warning", 2400)
            return None
        attachment = Attachment.from_path(path)
        self.chat_view.set_attachment(attachment)
        self.toast.show_message("图片已粘贴，Enter 发送", "info", 1600)
        return attachment

    def clipboard_dir(self) -> str:
        """剪贴板图片的落地目录：和本地库放一起（内存库则退到系统临时目录）。"""
        path = self._log.path
        if path and path != ":memory:":
            return os.path.join(os.path.dirname(os.path.abspath(path)),
                                CLIPBOARD_DIRNAME)
        return os.path.join(tempfile.gettempdir(), "mchat-clipboard")

    def attachment_action(self, message_id: str, action: str = "open") -> bool:
        """打开附件本身 / 打开它所在的文件夹（点气泡或右键菜单都会走到这里）。"""
        message = self._store.message(message_id)
        attachment = message.attachment if message is not None else None
        if attachment is None:
            return False
        if action == "folder":
            folder = os.path.dirname(os.path.abspath(attachment.path))
            if not os.path.isdir(folder):
                self.toast.show_message(f"文件夹不见了：{folder}", "warning", 2400)
                return False
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            return True
        if not attachment.exists:
            self.toast.show_message(f"文件已经不在这个位置了：{attachment.name}",
                                    "warning", 2600)
            return False
        QDesktopServices.openUrl(QUrl.fromLocalFile(attachment.path))
        return True

    def pending_attachment(self) -> Attachment | None:
        return self.chat_view.pending_attachment()

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
                    attachment=event.attachment,          # 对方发来的图片/文件
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
        if event.kind == PIN:
            self._sync_pin_banner()                     # 置顶变了：横幅跟着刷
            self.sidebar.refresh_item(event.conversation_id)
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
    # 设置区域（主题 / 文字大小 / 界面动效 / 数据）
    # ======================================================================
    def _saved_text_scale(self) -> float:
        return clamp_font_scale(self._settings.value("text_scale", 1.0))

    def text_scale(self) -> float:
        """当前文字大小系数（1.0 = 标准）。"""
        return font_scale()

    def set_text_scale(self, scale) -> float:
        """改文字大小：重建样式表（字号在里面统一缩放）并让各部件重上色。

        重建之后**画出来**才会变，所以这里直接走一次 ``_apply_theme`` ——
        主题没换，但样式表要重新生成（字号变了），各对话框也会拿到新的样式表。
        """
        value = set_font_scale(scale)
        self._settings.setValue("text_scale", value)
        self._apply_theme(animate=False)
        # 同步设置窗口：一是档位/当前值要跟上（比如从别处调用），
        # 二是**预览文字要动画过渡**到新字号 —— 这个动效在 sync 里发起来
        self._sync_settings_dialog(font_scale=value)
        self.toast.show_message(f"文字大小：{label_of(value)}", "info", 1400)
        return value

    def open_settings(self) -> SettingsDialog:
        """打开设置区域（非模态、复用同一个窗口）：打开时整窗淡入上滑 + 卡片错峰浮现。"""
        if self._settings_dialog is None:
            dialog = SettingsDialog(self)
            dialog.themeChanged.connect(self.set_theme)
            dialog.fontScaleChanged.connect(self.set_text_scale)
            dialog.animationsToggled.connect(self.set_animations)
            dialog.openDataDirRequested.connect(self.open_data_dir)
            self._settings_dialog = dialog
        dialog = self._settings_dialog
        dialog.sync(theme_name=self.theme_name(), font_scale=self.text_scale(),
                    animations=self.animations_enabled(), stats=self._log.stats(),
                    data_path=self._log.path, transport=self._transport.name)
        dialog.apply_palette(self.current_palette())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def settings_dialog(self) -> SettingsDialog | None:
        return self._settings_dialog

    def _sync_settings_dialog(self, *, theme_name: str | None = None,
                              font_scale: float | None = None,
                              animations: bool | None = None) -> None:
        """别处（菜单/侧栏）改了设置时，把设置窗口里的控件同步过去（不回抛信号）。"""
        if self._settings_dialog is None:
            return
        self._settings_dialog.sync(theme_name=theme_name, font_scale=font_scale,
                                   animations=animations)

    # ======================================================================
    # 主题
    # ======================================================================
    def set_theme(self, name: str) -> None:
        if name not in THEME_OPTIONS:
            return
        self._settings.setValue("theme", name)
        self.sidebar.set_theme_name(name)      # 让分段控件与主题保持一致
        self._sync_settings_dialog(theme_name=name)
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
        if self._settings_dialog is not None:
            self._settings_dialog.apply_palette(p)
        if self._forward_dialog is not None:
            self._forward_dialog.apply_palette(p)
        if self._merged_viewer is not None:
            self._merged_viewer.apply_palette(p)
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
        """开/关界面动效（菜单与设置里的那个开关，选择会记住）。"""
        anim.set_enabled(bool(enabled))
        self._settings.setValue("animations", "true" if enabled else "false")
        self._sync_settings_dialog(animations=bool(enabled))
        self.toast.show_message("界面动效已开启" if enabled else "界面动效已关闭",
                                "info", 1500)

    def animations_enabled(self) -> bool:
        return anim.is_enabled()

    # ======================================================================
    # 工具箱 / 菜单
    # ======================================================================
    def open_tool(self, prefill: str | None = None) -> SecurityToolDialog:
        """打开安全工具箱（非模态、复用同一个窗口）。

        没显式给文字时**自动带上聊天输入框里的内容**，而且只在工具箱还是空的时候带 ——
        免得顶掉用户已经敲进去的内容。原先把输入框文字送进工具箱是"更多"菜单里的一个
        动作，菜单删掉后这条能力并进了工具箱按钮本身："把这段拿去处理"本来就是点这个
        按钮最常见的意图，不必再绕一层菜单。
        """
        if self._tool_dialog is None:
            self._tool_dialog = SecurityToolDialog(self, clipboard=self._clipboard)
        dialog = self._tool_dialog
        dialog.apply_palette(self.current_palette())
        text, auto = prefill, False
        if text is None:                       # 没指定 => 从聊天输入框带
            text, auto = self.chat_view.input_text(), True
        text = text.strip()
        if text and not (auto and dialog.input_field.text().strip()):
            dialog.select_algorithm(0)
            dialog.set_input(text)
            if auto:
                self.toast.show_message("已把输入框的文字带进工具箱", "success", 1800)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.tool.input_field.focus_field()
        return dialog

    def tool_dialog(self) -> SecurityToolDialog | None:
        return self._tool_dialog

    def _menu_qss(self) -> str:
        return menu_qss(self.current_palette())

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
        if self._settings_dialog is not None:
            self._settings_dialog.close()
        if self._forward_dialog is not None:
            self._forward_dialog.close()
        if self._merged_viewer is not None:
            self._merged_viewer.close()
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
    # 崩溃留痕：偶发的"窗口直接消失"（段错误）下次能留下栈，写进 %APPDATA% 的 crash.log
    install_crash_log(default_crash_log_path(os.path.dirname(default_log_path())))
    window = MainWindow()
    window.show()
    return app.exec()
