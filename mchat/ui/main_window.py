"""主窗口：把侧栏、聊天区、工具箱和传输层拼起来。

职责边界
--------
* :class:`~mchat.core.store.ChatStore`   —— 数据与未读规则（不依赖 Qt）
* :class:`~mchat.core.net_transport.NetworkTransport` —— 消息怎么送达（不依赖 Qt）
* :class:`~mchat.ui.sidebar.Sidebar` / :class:`~mchat.ui.chat_view.ChatView`
  —— 只负责显示与交互，通过信号把动作抛给本窗口
* 本文件 —— 唯一"知道全部"的地方：接线、主题、事件轮询、跟服务端对账

联网版和本机演示版的差别**只在这里**：登录之后会话列表和聊天记录都从服务端来
（``load_conversations`` / ``_ensure_history``），发出去的消息走
``NetworkTransport``（后台线程 + 掉线重发），未读与已读回执跟服务端对账。
测试仍然可以注入 ``demo=`` + ``LocalTransport`` 把这一层换成"脚本自动回复"，
下面的逻辑对两者是同一套代码。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from dataclasses import replace

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QDesktopServices, QGuiApplication, QKeySequence,
                         QPalette, QShortcut)
from PyQt6.QtWidgets import QHBoxLayout, QMenu, QWidget

from ..core.api import (ApiError, NetworkError, friend_requests_from_json,
                        friends_from_json)
from ..core.atrest import (AtRestError, active_cipher, materialize,
                           seal_file_in_place)
from ..core.db import DEFAULT_LIMIT, ChatLog
from ..core.models import (ME_ID, Attachment, ChatKind, Conversation, Message, PinRef,
                          User)
from ..core.paths import resolve_attachment_path
from ..core.store import FRIENDS, MESSAGE, PIN, RESET, ChatStore, StoreEvent
from ..core.transport import (EVENT_ATTACHMENT_SENT, EVENT_CONVERSATION, EVENT_FRIEND_REQUEST,
                              EVENT_MESSAGE, EVENT_PIN, EVENT_PRESENCE,
                              EVENT_REACTION, EVENT_READ, EVENT_RESYNC,
                              EVENT_SEND_BLOCKED, EVENT_TYPING,
                              FRIEND_EVENT_KINDS, LocalTransport,
                              NullTransport, Outgoing)
from ..core.net_transport import NetworkTransport
from ..core.session import Session
from .attach import pick_file, pick_save_path
from .chat_view import ATTACH_FILE, ChatView
from . import anim
from .async_call import AsyncCall
from .forward_dialog import ForwardDialog
from .forward_view import MergedViewerDialog
from .friends_dialog import BLOCKED_NOTICE, FriendsDialog
from .image_view import ImageViewerDialog
from .new_chat_dialog import NewChatDialog
from .notify import Notifier
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
ATTACHMENT_DIRNAME = "attachments"           # 别人发来的附件下到这里
HISTORY_LIMIT = 50                           # 打开一个会话先拉多少条历史
TYPING_TTL = 6.0                             # "正在输入…"最多显示几秒（对面可能就没下文了）


def default_log_path() -> str:
    """本地历史库的位置：跟着系统"应用数据"目录走（Windows 是 %APPDATA%\\MChat）。"""
    from PyQt6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)
    if not base:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "mchat.db")


class MainWindow(QWidget):
    """MChat 聊天窗口。

    构造参数里 ``demo`` 和 ``transport`` 是**测试用的注入口**：给一个
    :class:`~mchat.core.seed.Demo` 就回到"本机演示"那条路（会话与自动回复都现成），
    真机上走的是 ``session`` + ``api``（联网）。两条路共用下面所有逻辑。
    """

    logoutRequested = pyqtSignal()          # 侧栏底部「退出登录」→ 启动器负责换窗口
    closed = pyqtSignal()                   # 用户点 ✕ 把窗口关了 → 启动器负责退出程序

    def __init__(self, parent: QWidget | None = None, *, demo=None,
                 transport=None, settings=None, log=None, confirm=None,
                 filedialog=None, savedialog=None, ask_pin=None, clipboard=None,
                 session: Session | None = None, api=None):
        super().__init__(parent)
        # 收尾闸门：**放在最前面**（构造中途也可能派发事件，收尾/回调里都会读它）
        self._shutdown_done = False
        self.setObjectName("root")
        name = session.name if session is not None else ""
        self.setWindowTitle(f"MChat — {name}" if name else "MChat")
        self.setMinimumSize(960, 600)
        self.resize(self._preferred_size())
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        # 设置可以注入（测试用独立命名空间，避免读到用户手动拖出来的宽度）
        self._settings = (settings if settings is not None
                          else QSettings(SETTINGS_ORG, SETTINGS_APP))
        self._session = session
        self._api = api
        self._user_id = str(getattr(session, "user_id", "") or "")
        # 没有 demo 注入就是**空仓库**：真实使用时会话列表由服务端给（load_conversations）
        if demo is not None:
            self._store: ChatStore = demo.store
            self._me = demo.me
            self._default_chat_id = demo.default_chat_id
        else:
            self._store = ChatStore()
            self._me = User(ME_ID, session.name if session is not None else "我",
                            online=True)
            self._default_chat_id = ""
        # 本地历史库可以注入（测试用临时文件/内存库，绝不碰用户的真实数据）
        self._log = log if log is not None else ChatLog(default_log_path(),
                                                        cipher=active_cipher())
        self._log.compact()                       # 启动整理一次：WAL 收回主库，文件更小
        # 传输层：注入 > 演示脚本 > 真联网 > 空实现（没登录也不会崩）
        if transport is not None:
            self._transport = transport
        elif demo is not None:
            self._transport = LocalTransport(demo.scripts)
        elif api is not None:
            self._transport = NetworkTransport(api, me_id=self._user_id,
                                               download_dir=self.attachment_dir(),
                                               auto_download_max=self._auto_download_max())
        else:
            self._transport = NullTransport()
        self._transport.start()
        self._current_id = ""
        self._sidebar_width = SIDEBAR_WIDTH
        self._tool_dialog: SecurityToolDialog | None = None
        self._search_dialog: SearchDialog | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._forward_dialog: ForwardDialog | None = None
        self._merged_viewer: MergedViewerDialog | None = None
        self._image_viewer: ImageViewerDialog | None = None
        self._new_chat_dialog: NewChatDialog | None = None
        self._friends_dialog: FriendsDialog | None = None
        # 桌面通知 + 提示音：构造**永不失败**（无托盘/无音频后端时内部静默降级），
        # 测试环境（offscreen 无托盘）自然什么都不弹。开关从设置里读，默认都开。
        self._notifier = Notifier(icon=None)
        self._notify_enabled = self._saved_notify()
        self._sound_enabled = self._saved_sound()
        self._notifier.set_enabled(self._notify_enabled)
        self._notifier.set_sound_enabled(self._sound_enabled)
        # 各会话"还没发出去的附件/待回复的那条"（和草稿一样按会话存，切换会话不会串台）
        self._pending_attachments: dict[str, Attachment] = {}
        self._pending_replies: dict[str, object] = {}
        self._fetching: set[str] = set()            # 正在从服务器取回的附件（别反复触发）
        self._forward_ids: list[str] = []          # 转发窗口里"这次要转发哪几条"
        # 联网用的三条后台调用通道（各自只干一类活，互不阻塞）
        self._load_call = AsyncCall(self)          # 拉会话列表
        self._history_call = AsyncCall(self)       # 拉某个会话的历史
        self._action_call = AsyncCall(self)        # 已读回执 / 正在输入 / 下载附件
        self._pin_call = AsyncCall(self)           # 会话内置顶同步（专用：不能被别人挡住）
        self._read_call = AsyncCall(self)          # 已读回执（专用：漏一条对方就少一个双勾）
        self._react_call = AsyncCall(self)         # 表情回应同步（专用）
        self._friends_call = AsyncCall(self)       # 拉好友 / 申请（GET /api/friends）
        self._load_call.finished.connect(self._on_conversations_loaded)
        self._history_call.finished.connect(self._on_history_loaded)
        self._action_call.finished.connect(self._on_action_done)
        self._pin_call.finished.connect(self._on_pin_sent)
        self._read_call.finished.connect(self._on_read_sent)
        self._react_call.finished.connect(self._on_reaction_sent)
        self._friends_call.finished.connect(self._on_friends_loaded)
        for call in (self._load_call, self._history_call, self._action_call,
                     self._pin_call, self._read_call, self._react_call,
                     self._friends_call):
            call.failed.connect(self._on_net_error)
        self._history_wanted = ""                  # 正在等历史回来的会话
        self._typing_sent_at = 0.0                 # "正在输入"的节流时间戳
        self._server_read_at: dict[str, float] = {}  # 已经告诉过服务端的已读位置
        self._history_loaded: set[str] = set()      # 已经从服务端拉过历史的会话
        self._auth_notified = False                 # "登录失效"只提示一次
        self._typing_until: dict[str, float] = {}    # 对面"正在输入…"的过期时间
        self._pending_pins: dict[tuple, bool] = {}    # 还没同步给服务端的"双方置顶"
        self._pending_reads: dict[str, float] = {}    # 还没告诉服务端的"我看到哪了"
        self._pending_reactions: dict[tuple, bool] = {}   # 还没同步出去的表情回应

        # 危险操作的确认弹窗也可以注入（测试里不弹窗、走固定答案）
        self.ask_pin = ask_pin or ask_pin_dialog
        # 剪贴板也是可注入的：测试里换成假实现，免得跑一次测试就把用户
        # 剪贴板里的东西顶掉（真机上"复制"当然还是写系统剪贴板）
        self._clipboard = clipboard if clipboard is not None else QGuiApplication.clipboard()
        self._confirm = confirm or (lambda **kw: confirm_destructive(
            self, self.current_palette(), **kw))
        # 文件选择框同样可以注入（QFileDialog 是模态窗口，无头测试里会卡住）
        self._pick = filedialog if filedialog is not None else pick_file
        # 「另存为」也走注入：模态文件对话框在无头测试里会一直等下去
        self._save_pick = savedialog if savedialog is not None else pick_save_path
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

        self.select_conversation(self._default_chat_id)
        self._poller = QTimer(self)
        self._poller.setInterval(POLL_INTERVAL_MS)
        self._poller.timeout.connect(self.tick)
        self._poller.start()
        if self._api is not None:
            self.load_conversations()               # 联网：开场就去服务端要会话列表

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
        # 一个会话都没有（刚注册的账号就是这样）：右侧先说清楚"去哪开第一个会话"，
        # 省的看到一个没名字的头像和一个空白面板发懵
        if not self._store.conversations():
            self.chat_view.show_empty()

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
        self.chat_view.composer.edit.textChanged.connect(self.notify_typing)
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
        self.sidebar.newChatRequested.connect(self.open_new_chat)
        self.sidebar.friendsRequested.connect(self.open_friends)
        self.sidebar.logoutRequested.connect(self.logout)
        self.sidebar.conversationMenuRequested.connect(self._show_conversation_menu)
        self._store.subscribe(self._on_store_event)
        self.sidebar.refresh()
        self.sidebar.refresh_friends_badge()

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
    # 联网：跟服务端对账（会话列表 / 历史 / 已读 / 正在输入 / 附件下载）
    # ======================================================================
    def account(self) -> Session | None:
        """当前登录的账号（没登录就是 None）。"""
        return self._session

    def user_name(self) -> str:
        return self._me.name

    def my_user_id(self) -> str:
        """我在服务端的 id（判断"这条消息是不是我发的"用；空串 = 本机演示）。"""
        return self._user_id

    def attachment_dir(self) -> str:
        """别人发来的附件在本地落脚的地方（和库文件放一块，删账号就一起删）。"""
        path = self._log.path
        folder = (os.path.dirname(os.path.abspath(path))
                  if path and path != ":memory:" else tempfile.gettempdir())
        return os.path.join(folder, ATTACHMENT_DIRNAME)

    def _auto_download_max(self) -> int:
        """多大以下的附件才自动下载（字节；0 = 多大都自动下）。

        默认 16MB。这个开关是**防着对面**的：以前来者不拒地全自动下，对端随便发几个
        大文件就能把磁盘和带宽吃掉；现在超过这个大小的只显示"点一下再下"。
        想调就在设置里写 ``auto_download_mb``（QSettings，没做界面 —— 属于少数人才改的项）。
        """
        raw = self._settings.value("auto_download_mb", 16)
        try:
            return max(0, int(float(raw) * 1024 * 1024))
        except (TypeError, ValueError):
            return 16 * 1024 * 1024

    def load_conversations(self) -> bool:
        """去服务端要会话列表（开窗口、登录后、以及服务端说"重新同步"时调）。"""
        if self._api is None:
            return False

        def work():
            return {"items": self._api.conversations()}

        if not self._load_call.run(work):
            return False
        return True

    def _on_conversations_loaded(self, payload) -> None:
        if self._shutdown_done:
            return          # 窗口已经关了、库也关了：迟到的后台结果别再往库里写
        self._apply_conversations((payload or {}).get("items") or [])

    def _apply_conversations(self, items) -> int:
        """把服务端的会话列表并进仓库：新增的建出来，已有的换标题/成员/未读。"""
        from ..core.api import conversation_from_json

        added = 0
        cleared = self._log.cleared_map()
        flags = self._log.flags()
        pins = self._log.pinned_refs()
        for raw in items:
            conversation = conversation_from_json(raw, me_id=self._user_id)
            if conversation is None:
                continue
            conversation_id = conversation.id
            names = flags.get(conversation_id, set())
            conversation.pinned = "pinned" in names
            conversation.muted = "muted" in names
            unread = int(raw.get("unread") or 0)
            existing = self._store.get(conversation_id)
            if existing is None:
                self._store.add_conversation(conversation, unread=unread)
                # 上次退出前的历史先在本地捞回来（离线也能看），再由服务端补新的
                self._store.hydrate(conversation_id,
                                    self._log.load(conversation_id, limit=DEFAULT_LIMIT))
                added += 1
            else:
                existing.title = conversation.title
                existing.kind = conversation.kind
                existing.members = conversation.members
                existing.pinned = conversation.pinned
                existing.muted = conversation.muted
                if conversation.messages:
                    self._persist_reactions(conversation.messages)
                    self._store.hydrate(conversation_id, conversation.messages)
                self._store.set_unread(conversation_id, unread)
            # 置顶要合一份：服务端知道"双方可见"的那些，本地知道"仅我可见"的那些
            self._merge_pins(conversation, pins.get(conversation_id, ()))
            if cleared.get(conversation_id) and unread:
                # 本地清空过这个会话，但服务端还记着未读：把旧的未读顺手抹掉
                self._sync_read_to_server(conversation_id)
        self.sidebar.refresh()
        self._sync_connection_hint()
        if self._current_id == "" and added:
            self.select_conversation(self._store.conversations()[0].id)
        return added

    def _merge_pins(self, conversation, local_pins) -> tuple:
        """合成这个会话的置顶列表：**服务的"双方可见" + 本地的"仅我可见"**。

        为什么必须合：`pins` 在两边各有一半 ——
        * 服务端（``Conversation.pins``）只下发"双方可见"的那些，而且它是**权威**：
          对方取消了我这边也要跟着消失，所以不能只信本地库；
        * "仅我可见"服务端压根不知道（那是刻意不发请求的），只能从本地库来。

        按 message_id 去重、双方可见的优先；合完把结果写回本地库，
        这样离线打开也能看到上次那份（含对方置顶的那些）。
        """
        shared = tuple(conversation.pins)                    # 服务端下发，都是双方可见
        mine = tuple(pin for pin in (local_pins or ()) if not pin.for_both)
        known = {pin.message_id for pin in shared}
        merged = shared + tuple(pin for pin in mine if pin.message_id not in known)
        conversation.pins = merged
        if merged != tuple(local_pins or ()):
            self._log.set_pinned_refs(conversation.id, merged)
        if conversation.id == self._current_id:
            self._sync_pin_banner()
        return merged

    def _ensure_history(self, conversation_id: str) -> bool:
        """打开会话时补历史：界面里只有最近几十条，更早的要向服务端要。"""
        if self._api is None or not conversation_id:
            return False
        if conversation_id in self._history_loaded or self._history_call.is_running():
            return False

        def work():
            data = self._api.messages(conversation_id, limit=HISTORY_LIMIT)
            return {"cid": conversation_id, "messages": list(data.get("messages") or [])}

        if not self._history_call.run(work):
            return False
        self._history_wanted = conversation_id
        return True

    def _on_history_loaded(self, payload) -> None:
        from ..core.api import messages_from_json

        if self._shutdown_done:
            return          # 同上：库已关，迟到的历史回填直接丢掉
        payload = dict(payload or {})
        conversation_id = str(payload.get("cid") or "")
        messages = messages_from_json(payload.get("messages"), me_id=self._user_id)
        messages = [message for message in messages
                    if message.ts > self._log.cleared_at(conversation_id)]
        if messages:
            for message in messages:
                self._log.save_message(message)     # 服务端历史也落本地：离线能翻
            self._persist_reactions(messages)        # 别人点过的表情也要落本地
            self._store.hydrate(conversation_id, messages)
        self._history_loaded.add(conversation_id)
        if conversation_id == self._current_id:
            conversation = self._store.get(conversation_id)
            if conversation is not None:
                self.chat_view.show_conversation(conversation)
                self._sync_reactions(conversation_id)
                self._sync_pin_banner()
        self.sidebar.refresh_item(conversation_id)
        # 拉的过程中用户又切了会话：把新的那个补上
        if self._history_wanted != conversation_id:
            self._ensure_history(self._history_wanted)

    def _sync_read_to_server(self, conversation_id: str, up_to: float | None = None) -> None:
        """告诉服务端"这个会话我看到哪了"（对方那边的单勾才会变双勾）。"""
        if self._api is None or not conversation_id:
            return
        stamp = time.time() if up_to is None else float(up_to)
        if self._server_read_at.get(conversation_id, 0.0) >= stamp:
            return
        self._server_read_at[conversation_id] = stamp
        self._pending_reads[conversation_id] = max(
            stamp, self._pending_reads.get(conversation_id, 0.0))
        self._flush_reads()

    def _persist_reactions(self, messages) -> None:
        """把服务端给的消息回应写进本地缓存（下次离线打开也能看到别人点过的表情）。

        服务端给的清单（哪怕是空的）才是权威：所以本地"刚点的那一下"最终会被
        服务端的回显覆盖成它算出来的数字 —— 两个人同时点也不会算重。
        """
        for message in messages or ():
            reactions = getattr(message, "reactions", None)
            if reactions is None:
                continue
            self._log.set_reactions(
                message.id,
                {item.emoji: item.count for item in reactions},
                {item.emoji for item in reactions if item.mine},
            )

    def _flush_reads(self) -> bool:
        """把"我看到哪了"发给服务端；通道忙就等下一次（tick 会再叫它）。

        为什么要排队：之前共用 ``_action_call``，它同时在跑别的活时 ``run()`` 直接返回
        False —— 已读回执就被静默吞掉了，对方永远等不到双勾。
        """
        api = self._api
        if api is None or self._read_call.is_running() or not self._pending_reads:
            return False
        conversation_id = next(iter(self._pending_reads))
        stamp = self._pending_reads[conversation_id]

        def work():
            api.mark_read(conversation_id, stamp)
            return {"kind": "read", "cid": conversation_id, "ts": stamp}

        return self._read_call.run(work)

    def _on_read_sent(self, payload) -> None:
        payload = dict(payload or {})
        if payload.get("kind") != "read":
            return
        conversation_id = payload.get("cid")
        if self._pending_reads.get(conversation_id) == payload.get("ts"):
            self._pending_reads.pop(conversation_id, None)
        self._flush_reads()

    def notify_typing(self) -> None:
        """"我正在输入" —— 3 秒一次就够（每敲一个字都发会把服务器淹掉）。

        输入框清空（或者消息刚发出去）时发一条 `typing=false`，让对方那边立刻停掉
        "正在输入…" —— 否则对面会一直看着那三个字，直到下一条消息到达才消失。
        """
        conversation_id = self._current_id
        if self._api is None or not conversation_id:
            return
        send = getattr(self._transport, "send_typing", None)
        if send is None:
            return
        has_content = bool(self.chat_view.input_text().strip()) or \
            bool(self.chat_view.has_pending_attachment())
        now = time.time()
        if not has_content:
            if self._typing_sent_at:
                self._typing_sent_at = 0.0
                send(conversation_id, False)
            return
        if now - self._typing_sent_at < 3.0:
            return
        self._typing_sent_at = now
        send(conversation_id, True)

    def _on_action_done(self, payload) -> None:
        payload = dict(payload or {})
        if payload.get("kind") != "download":
            return
        if self._shutdown_done:
            return          # 关窗之后才下完的附件：库已经关了，别写（下次打开重下）
        message_id = str(payload.get("message_id") or "")
        path = str(payload.get("path") or "")
        message = self._store.message(message_id)
        if message is None or not path or not os.path.isfile(path):
            self.toast.show_message("附件下载失败", "warning", 2200)
            return
        previous = message.attachment                  # 取回前的形态（带着服务器那份 id）
        message.attachment = previous.localized_at(path) if previous is not None \
            else Attachment.from_path(path)      # 换成本地路径，但**保住服务器那份 id**
        self._fetching.discard(message_id)
        self._log.save_message(message)             # 记下来：下次不用再下
        conversation = self._store.get(message.conversation_id)
        if conversation is not None and message.conversation_id == self._current_id:
            # 附件那一块要从"还没下"变成真图片/文件卡片：整条会话重画一次最省事
            # （只有下载这类罕见路径会走到，不必为它给气泡加一套增量更新）
            self.chat_view.show_conversation(conversation)
            self._sync_reactions(message.conversation_id)
        self._sync_connection_hint()
        if payload.get("view"):
            # 点图片 → 先下载 → 下完直接在查看器里打开（中间不再弹"已下载"那一句）
            self.view_image(message_id)
        elif payload.get("open"):
            self.attachment_action(message_id, "open")
        else:
            self.toast.show_message("附件已下载", "success", 1600)

    def download_attachment(self, message_id: str, *, open_after: bool = False,
                            view_after: bool = False) -> bool:
        """把还在服务器上的附件下到本地（点开它时走这条路）。

        ``view_after`` = 下完之后在**图片查看器**里打开（点图片那条路）；
        ``open_after`` = 下完之后交给系统程序打开（右键菜单「打开」那条路）。
        """
        message = self._store.message(message_id)
        attachment = message.attachment if message is not None else None
        if message is None or attachment is None or not (attachment.is_remote
                                                        or attachment.recoverable):
            return False
        api, transport = self._api, self._transport
        if api is None:
            return False
        dest = (transport.cache_path(attachment)
                if hasattr(transport, "cache_path")
                else resolve_attachment_path(self.attachment_dir(),
                                             attachment.remote_id, attachment.name))

        def work():
            api.download(attachment.source_id, dest, root=self.attachment_dir())
            return {"kind": "download", "message_id": message_id, "path": dest,
                    "open": open_after, "view": view_after}

        if not self._action_call.run(work):
            return False
        self.toast.show_message(f"正在下载 {attachment.name} …", "info", 1600)
        return True

    def _on_net_error(self, error) -> None:
        """后台调用失败：不弹窗、不打断，用提示条说清楚；登录失效就退回登录界面。"""
        if self._shutdown_done:
            # 关窗时收尾会先把在飞的请求全掐断（api.abort），那些请求会以
            # NetworkError/ApiError 的形式回来 —— 窗口已经在关了，别再动界面。
            return
        if isinstance(error, ApiError):
            if error.is_auth and not self._auth_notified:
                self._auth_notified = True
                self.toast.show_message("登录已失效，请重新登录", "warning", 2600)
                self.logoutRequested.emit()
                return
            if self._pending_pins:
                # 服务端明确说"不行"（比如那条消息已经被删了）：别再排队重试了，
                # 否则每次 tick 都会拿同一条去撞墙
                dropped = next(iter(self._pending_pins))
                self._pending_pins.pop(dropped, None)
                self.toast.show_message(f"置顶没同步成功：{error.friendly}", "warning", 2400)
                self._flush_pins()
            return
        if isinstance(error, NetworkError):
            self._sync_connection_hint()
            return

    def _sync_connection_hint(self) -> None:
        """侧栏底部那行小字：连着/离线/待发几条 + 当前账号。"""
        describe = getattr(self._transport, "describe", None)
        if describe is None:
            self.sidebar.set_mode_hint(self._transport.name)
            return
        name = self._session.name if self._session is not None else ""
        self.sidebar.set_mode_hint(f"{name} · {describe()}" if name else describe())

    # ======================================================================
    # 新建群聊（和某人开聊走「好友」窗口：申请 -> 对方同意 -> 服务端建单聊）
    # ======================================================================
    def open_new_chat(self) -> NewChatDialog:
        """左栏那个「＋」：打开新建群聊窗口（没有服务器时明确说一句，别装作能建）。"""
        if self._api is None:
            self.toast.show_message("没连上服务器，建不了新会话", "warning", 2200)
            raise RuntimeError("没有服务器连接：请先登录")
        if self._new_chat_dialog is None:
            dialog = NewChatDialog(self, api=self._api, theme_name=self.theme_name())
            dialog.conversationCreated.connect(self.on_conversation_created)
            self._new_chat_dialog = dialog
        dialog = self._new_chat_dialog
        dialog.apply_palette(self.current_palette())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def new_chat_dialog(self) -> NewChatDialog | None:
        return self._new_chat_dialog

    def on_conversation_created(self, data) -> Conversation | None:
        """服务端建好了会话（群聊走这里；单聊是"同意好友"时服务端建的）：加进列表、直接打开。"""
        from ..core.api import conversation_from_json

        conversation = conversation_from_json(data, me_id=self._user_id)
        if conversation is None:
            self.toast.show_message("服务端没返回会话信息", "warning", 2200)
            return None
        if self._store.get(conversation.id) is None:
            self._store.add_conversation(conversation)
            self._store.hydrate(conversation.id,
                                self._log.load(conversation.id, limit=DEFAULT_LIMIT))
        self._history_loaded.add(conversation.id)        # 刚建的空会话，不用再拉历史
        self.select_conversation(conversation.id)
        self.toast.show_message(f"已打开「{conversation.title}」", "success", 1800)
        return conversation

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
        """点一下加、再点撤。本地先动（界面零延迟），同时把这次变化同步给服务端，
        由它算总数再推给所有人（含自己的其它设备）—— 计数绝不在客户端自己加减。

        **仅本机演示模式**（没有 api）没有服务端，就退回"纯本地计数"那套。
        """
        counts = self._log.toggle_reaction(message_id, emoji)
        mine = self._log.mine(message_id)
        bubble = self.chat_view.message_list.bubbles().get(message_id)
        if bubble is not None:
            bubble.set_reactions(counts, mine)
        message = self._store.message(message_id)
        if message is not None and self._api is not None:
            self._react_on_server(message.conversation_id, message_id, emoji,
                                  emoji in mine)
        return counts

    def _react_on_server(self, conversation_id: str, message_id: str, emoji: str,
                         added: bool) -> bool:
        """把这次回应同步给服务端；连不上就排队，等连上自动补（见 tick）。"""
        if self._api is None:
            return False
        self._pending_reactions[(conversation_id, message_id, emoji)] = bool(added)
        return self._flush_reactions()

    def _flush_reactions(self) -> bool:
        api = self._api
        if api is None or self._react_call.is_running() or not self._pending_reactions:
            return False
        (conversation_id, message_id, emoji), added = next(iter(self._pending_reactions.items()))

        def work():
            api.set_reaction(conversation_id, message_id, emoji, added)
            return {"kind": "reaction", "cid": conversation_id, "mid": message_id,
                    "emoji": emoji}

        return self._react_call.run(work)

    def _on_reaction_sent(self, payload) -> None:
        payload = dict(payload or {})
        if payload.get("kind") != "reaction":
            return
        self._pending_reactions.pop(
            (payload.get("cid"), payload.get("mid"), payload.get("emoji")), None)
        self._flush_reactions()

    def pending_reactions(self) -> dict:
        """还没同步出去的回应（测试/自检用）。"""
        return dict(self._pending_reactions)

    def _apply_reaction(self, event) -> None:
        """别人（或我的另一台设备）给某条消息加了/撤了表情：按服务端给的清单覆盖本地。"""
        message = self._store.message(event.message_id)
        if message is None:
            return
        counts = {item.emoji: item.count for item in event.reactions}
        mine = {item.emoji for item in event.reactions if item.mine}
        self._log.set_reactions(event.message_id, counts, mine)
        message.reactions = tuple(event.reactions)
        if event.conversation_id == self._current_id:
            bubble = self.chat_view.message_list.bubbles().get(event.message_id)
            if bubble is not None:
                bubble.set_reactions(counts, mine)
        self.sidebar.refresh_item(event.conversation_id)
        if event.sender_id and event.sender_id != self._user_id:
            who = event.pinned_by_name or "对方"
            action = "加了" if event.added else "撤了"
            self.toast.show_message(f"{who} {action} {event.emoji} 回应", "info", 2000)

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
        # "双方可见"才告诉服务端（它会推给会话里所有人）；"仅我可见"是本机的事，
        # 服务端根本不该知道 —— 否则对面会看到一条本该只有自己知道的置顶。
        if after is not None and after.for_both:
            self._pin_on_server(conversation.id, message_id, True)
        elif after is None and before is not None and before.for_both:
            self._pin_on_server(conversation.id, message_id, False)
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
        if not target:
            return False
        was_shared = bool(self._store.pin_ref(self._current_id, target)
                          and self._store.pin_ref(self._current_id, target).for_both)
        if not self._store.unpin_message(target):
            return False
        conversation, _ = self._store.locate(target)
        if conversation is not None:
            self._log.set_pinned_refs(conversation.id, conversation.pins)
            if was_shared:                     # 取消的是"双方可见"那条：对面也要跟着撤
                self._pin_on_server(conversation.id, target, False)
        self._sync_pin_banner()
        self.toast.show_message("已取消这一条的置顶（其余的留着）", "info", 1800)
        return True

    def _pin_on_server(self, conversation_id: str, message_id: str,
                       pinned: bool) -> bool:
        """把"双方置顶"同步给服务端。连不上就**排队**，等连上了自动补（见 tick）。"""
        if self._api is None:
            return False
        self._pending_pins[(conversation_id, message_id)] = bool(pinned)
        return self._flush_pins()

    def _flush_pins(self) -> bool:
        """发出一条待同步的置顶（一次一条，成功的回调里再发下一条）。"""
        api = self._api
        if api is None or self._pin_call.is_running() or not self._pending_pins:
            return False
        (conversation_id, message_id), pinned = next(iter(self._pending_pins.items()))

        def work():
            api.pin_message(conversation_id, message_id, pinned)
            return {"kind": "pin", "cid": conversation_id, "mid": message_id}

        return self._pin_call.run(work)

    def _on_pin_sent(self, payload) -> None:
        payload = dict(payload or {})
        if payload.get("kind") != "pin":
            return
        self._pending_pins.pop((payload.get("cid"), payload.get("mid")), None)
        self._flush_pins()                     # 队列里还有就接着发

    def pending_pins(self) -> dict:
        """还没同步出去的置顶（测试/自检用）。"""
        return dict(self._pending_pins)

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
                                          message.id, attachment=message.attachment,
                                          forwarded_from=message.forwarded_from,
                                          merged_items=message.merged_items))
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
                      "和草稿，**其它会话不受影响**，而且无法撤销。"
                      "（这是清空**你这边**的记录，对方的记录还在。）")):
            return False
        stamp = time.time()
        removed = self._log.clear_conversation(conversation_id)      # 库（并打 cleared 旗标）
        self._store.clear_messages(conversation_id)                 # 内存
        self._pending_attachments.pop(conversation_id, None)        # 待发送的附件也一并清掉
        self._pending_replies.pop(conversation_id, None)            # 待回复的那条同理
        self._store.get(conversation_id).pins = ()                  # 库里已被 clear_conversation 清掉
        self._log.set_flag(conversation_id, "cleared", True)
        # 记住"清到哪一刻"：往后打开这个会话时，服务端再把这些旧消息发回来也不会冒出来
        self._log.set_cleared_at(conversation_id, stamp)
        self._sync_read_to_server(conversation_id, stamp)           # 旧消息也不该再算未读
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
        self._ensure_history(conversation_id)       # 联网：向服务端补这个会话的历史
        self._sync_read_to_server(conversation_id)  # 打开了就算读过（对方看到双勾）
        self._sync_connection_hint()
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
            # id 用完整 uuid：它同时就是"服务端那条消息的 id"（协议里的 client_id），
            # 重发/重试靠它去重，所以不能短到有碰撞风险
            id=uuid.uuid4().hex,
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
        result = self._transport.send(Outgoing(conversation_id, ME_ID, text, message.id,
                                               attachment=attachment, reply_to=reply))
        self._log.set_draft(conversation_id, "")        # 发出去了，草稿作废
        self._sync_drafts()
        self._typing_sent_at = 0.0                      # 下一句话重新计"正在输入"
        # 传输层同步回一个"被好友门禁拦下"的结果（联网那层是异步的，走
        # EVENT_SEND_BLOCKED 事件）。无论哪条路：本地那条标成 blocked，绝不重发。
        if getattr(result, "intercepted", False):
            self._apply_blocked(conversation_id, message.id)
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
            seal_file_in_place(path)             # 贴进来的图也别在磁盘上留明文
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
        """附件上的动作：看大图（``"view"``）/ 用系统程序打开（``"open"``）/ 打开所在文件夹。

        点气泡里的图片、右键菜单、通知都汇到这里 —— 行为只有一处定义。
        """
        message = self._store.message(message_id)
        attachment = message.attachment if message is not None else None
        if attachment is None:
            return False
        if action == "view":
            return self.view_image(message_id)
        if attachment.is_remote or attachment.recoverable:
            # 还在服务器上（没下载过 / 或者本地那份被删了）：先取回来，别让用户看着"打不开"发愣
            if action == "folder":
                self.toast.show_message("文件还没取回来，先打开一次再找它所在的文件夹",
                                        "info", 2400)
                return False
            return self.download_attachment(message_id, open_after=True)
        if action == "folder":
            folder = os.path.dirname(os.path.abspath(materialize(attachment.path)))
            if not os.path.isdir(folder):
                self.toast.show_message(f"文件夹不见了：{folder}", "warning", 2400)
                return False
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            return True
        if not attachment.exists:
            self.toast.show_message(f"文件已经不在这个位置了：{attachment.name}",
                                    "warning", 2600)
            return False
        try:
            # 加密存储的附件要先解出明文才能交给系统程序（解到临时目录，退出时清）
            plain = materialize(attachment.path)
        except AtRestError as error:
            self.toast.show_message(f"这个附件打不开：{error}", "warning", 3200)
            return False
        QDesktopServices.openUrl(QUrl.fromLocalFile(plain))
        return True

    # ---- 图片查看器（点图片看大图，QQ 那种） ----
    def view_image(self, message_id: str) -> bool:
        """点图片：在**应用内的查看器**里看大图（不是交给系统看图器）。

        还在服务器上的图片会先下载，下完自动打开查看器 —— 点一下就有反应，
        不用让用户先"下载"再"打开"。
        """
        message = self._store.message(message_id)
        attachment = message.attachment if message is not None else None
        if message is None or attachment is None or attachment.kind != "image":
            return False
        if attachment.is_remote or attachment.recoverable:
            return self.download_attachment(message_id, view_after=True)
        if not attachment.exists:
            self.toast.show_message(f"图片已经不在这个位置了：{attachment.name}",
                                    "warning", 2600)
            return False
        return self.open_image_view(message_id) is not None

    def open_image_view(self, message_id: str) -> ImageViewerDialog | None:
        """打开（或复用）图片查看器，定位到这条消息的图片。

        查看器里的 ``‹ ›`` 翻的是**当前会话里所有能看的图片**（本地已有的那些），
        和 QQ 一样：点开一张就能顺手往下看。
        """
        _, message = self._store.locate(message_id)
        attachment = message.attachment if message is not None else None
        if message is None or attachment is None or attachment.kind != "image":
            return None
        items = self._conversation_images(message.conversation_id)
        if not items:
            return None
        index = next((position for position, item in enumerate(items)
                      if item[0] == message_id), None)
        if index is None:
            # 这张图没能进列表（不在本地了 / 不是图片）→ **不许顺手打开会话里别的图**，
            # 否则用户点一张"已经丢了的图"却看到另一张，比直接报错还糟
            return None
        if self._image_viewer is None:
            viewer = ImageViewerDialog(self)
            viewer.revealRequested.connect(self._reveal_image)
            viewer.saveRequested.connect(self._save_image)
            viewer.imageChanged.connect(self._on_viewer_image_changed)
            self._image_viewer = viewer
        viewer = self._image_viewer
        viewer.set_images(items, index)
        viewer.apply_palette(self.current_palette())
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()
        return viewer

    def image_viewer(self) -> ImageViewerDialog | None:
        return self._image_viewer

    def _conversation_images(self, conversation_id: str) -> list[tuple[str, str, str]]:
        """会话里"能直接看"的图片：``[(message_id, 路径, 文件名), …]``（按时间顺序）。"""
        conversation = self._store.get(conversation_id)
        messages = conversation.messages if conversation is not None else []
        items: list[tuple[str, str, str]] = []
        for message in messages:
            attachment = message.attachment
            if attachment is not None and attachment.kind == "image" and (
                    attachment.exists or attachment.source_id):
                # 本地没有但服务器上还有的（自己发出去又删了本地那份）也要进列表：
                # 翻到它时查看器显示"正在下载"，主窗口顺手把它取回来
                items.append((message.id, attachment.path if attachment.exists else "",
                              attachment.name))
        return items

    def _reveal_image(self, message_id: str) -> None:
        """查看器里的「在文件夹中显示」→ 复用附件那条老路（统一判断文件还在不在）。"""
        self.attachment_action(message_id, "folder")

    def _save_image(self, message_id: str) -> bool:
        """查看器里的「另存为」：把图**明文**复制到用户选的位置（附件本体是加密落盘的）。"""
        message = self._store.message(message_id)
        attachment = message.attachment if message is not None else None
        if message is None or attachment is None or not attachment.exists:
            self.toast.show_message("这张图已经不在本地了，没法另存", "warning", 2400)
            return False
        target = self._save_pick(attachment.name, parent=self)
        if not target:
            return False                              # 用户取消了：什么都不做、也不提示
        try:
            source = materialize(attachment.path)     # 加密落盘的附件先解成明文再拷
            target = os.path.abspath(str(target))
            if os.path.abspath(source) == target:
                self.toast.show_message("已经保存到这个位置了", "info", 2000)
                return True
            with open(source, "rb") as reader, open(target, "wb") as writer:
                shutil.copyfileobj(reader, writer)
        except (AtRestError, OSError) as error:
            self.toast.show_message(f"保存失败：{error}", "warning", 3000)
            return False
        self.toast.show_message(f"已保存到 {target}", "success", 2400)
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
                self._apply_incoming(event)
            elif event.kind == EVENT_TYPING:
                self.sidebar.set_typing(event.conversation_id, event.typing)
                if event.conversation_id == self._current_id:
                    self.chat_view.set_typing(event.typing)
                # 对面可能敲一半就走了、"不打字了"那条事件永远收不到：给个过期时间，
                # 免得"正在输入…"在侧栏挂到天荒地老（Telegram 也是几秒后自己消失）
                self._typing_until[event.conversation_id] = (
                    time.time() + TYPING_TTL if event.typing else 0.0)
            elif event.kind == EVENT_READ:
                self._store.set_read(event.conversation_id, event.message_id)
            elif event.kind == EVENT_PIN:
                self._apply_pin(event)
            elif event.kind == EVENT_REACTION:
                self._apply_reaction(event)
            elif event.kind == EVENT_PRESENCE:
                self._apply_presence(event)
            elif event.kind == EVENT_CONVERSATION:
                self._apply_new_conversation(event)
            elif event.kind == EVENT_SEND_BLOCKED:
                # 我发的消息被好友门禁拦成了申请：本地那条要换个样子，别一直像发出了
                self._apply_blocked(event.conversation_id, event.message_id)
            elif event.kind == EVENT_ATTACHMENT_SENT:
                # 我发出去的附件在服务端叫什么 id：记下来（本地那份删了还能取回）
                self._apply_attachment_sent(event)
            elif event.kind in FRIEND_EVENT_KINDS:
                self._apply_friend_event(event)
            elif event.kind == EVENT_RESYNC:
                self.resync()
        # 服务端不认这个令牌了：退回登录界面（启动器负责换窗口）
        auth_error = str(getattr(self._transport, "auth_error", "") or "")
        if auth_error and not self._auth_notified:
            self._auth_notified = True
            self.toast.show_message(f"{auth_error}，请重新登录", "warning", 3200)
            self.logoutRequested.emit()
        self._expire_typing()
        # 离线时排队的"双方置顶 / 已读 / 回应"：一联上就补发（服务端都幂等）
        if getattr(self._transport, "online", False):
            if self._pending_pins:
                self._flush_pins()
            if self._pending_reactions:
                self._flush_reactions()
        if self._pending_reads:
            self._flush_reads()
        self._sync_connection_hint()
        return len(events)

    def _expire_typing(self) -> None:
        """过期的"正在输入…"清掉（见 tick 里那段注释）。"""
        now = time.time()
        for conversation_id, until in list(self._typing_until.items()):
            if until and until > now:
                continue
            self._typing_until.pop(conversation_id, None)
            self.sidebar.set_typing(conversation_id, False)
            if conversation_id == self._current_id:
                self.chat_view.set_typing(False)

    def _apply_incoming(self, event) -> None:
        """收到一条消息：去重 → 清理整成"我这边的样子" → 入库 + 未读。"""
        message = event.message
        if message is None:
            # 本机演示那条路（LocalTransport）只给"散装"字段，这里照旧拼一条
            message = Message(
                id=f"in-{uuid.uuid4().hex[:8]}",
                conversation_id=event.conversation_id,
                sender_id=event.sender_id,
                text=event.text,
                ts=event.ts or time.time(),
                outgoing=False,
                attachment=event.attachment,
            )
        elif self._store.message(message.id) is not None:
            return                       # 自己的回显 / 重复事件：已经有了就不再来一条
        elif self._log.cleared_at(message.conversation_id) >= message.ts:
            return                       # 这个会话本地清空过，比清空点还早的先不显示
        if message.sender_id == self._user_id and self._user_id:
            message.outgoing = True       # 同一个账号在别的设备上发的
            message.sender_id = ME_ID
        if self._store.get(message.conversation_id) is None:
            return                        # 会话还没同步下来（等 conversation 事件/下次拉取）
        # 消息到达即结束"正在输入…"
        self.sidebar.set_typing(message.conversation_id, False)
        if message.conversation_id == self._current_id:
            self.chat_view.set_typing(False)
        self._store.receive(message)
        self._notify_incoming(message)      # 没在看这个会话就弹一条桌面通知

    def _apply_new_conversation(self, event) -> None:
        """服务端推来一个新会话（别人拉我开了单聊/进了群）：加进列表并说一声。"""
        conversation = event.conversation
        if conversation is None or self._store.get(conversation.id) is not None:
            return
        flags = self._log.flags().get(conversation.id, set())
        conversation.pinned = "pinned" in flags
        conversation.muted = "muted" in flags
        self._store.add_conversation(conversation)
        self._store.hydrate(conversation.id,
                            self._log.load(conversation.id, limit=DEFAULT_LIMIT))
        self.toast.show_message(f"新会话：{conversation.title}", "info", 2600)

    def _apply_presence(self, event) -> None:
        """某人上线/下线了：把他所有会话里的成员状态改掉，并刷新看得见的地方。

        以前只有"重新拉会话列表"才会更新在线状态 —— 对方挂了半小时，我这边小绿点
        还亮着。现在服务端一变化就推过来，立刻改。

        改状态走 :meth:`~mchat.core.store.ChatStore.set_user_online`（内部用
        ``replace()`` 换成员对象）：``User`` 是 frozen dataclass，直接给它赋值会抛
        ``FrozenInstanceError``，而这里跑在 ``tick()`` 里 —— 异常一冒整条事件轮询
        就断掉，于是"好友上线了还是显示离线"（实测就栽在这行）。
        """
        user_id = event.sender_id
        if not user_id:
            return
        touched = self._store.set_user_online(user_id, bool(event.online))
        if not touched:
            return
        for conversation_id in touched:
            self.sidebar.refresh_item(conversation_id)
        if self._current_id in touched:
            conversation = self._store.get(self._current_id)
            others = [m for m in conversation.members if not m.is_me] if conversation else []
            self.chat_view.header_avatar.set_online(bool(others and others[0].online))
            self.chat_view.refresh_status()

    def _apply_pin(self, event) -> None:
        """对方置顶/取消置顶了一条消息（"双方可见"那种）：跟着变，并且说一声。

        快照消息要并进仓库：对方置顶的那条可能在更早的历史里、我本地还没有 ——
        只记一个 id 的话横幅画不出来（横幅要显示"谁置顶了哪句话"）。
        """
        conversation = self._store.get(event.conversation_id)
        if conversation is None:
            return
        message_id = event.message_id or (event.message.id if event.message else "")
        if not message_id:
            return
        who = event.pinned_by_name or "对方"
        mine = bool(event.sender_id) and event.sender_id == self._user_id
        if event.pinned:
            if event.message is not None and self._store.message(message_id) is None:
                self._log.save_message(event.message)
                self._store.hydrate(event.conversation_id, [event.message])
            # 注意：**不能**直接调 store.pin_message —— 它是个"开关"（同一条再点一次 =
            # 取消）。服务端会把置顶事件也推回给发起人自己，于是"我自己置顶的"会被这条
            # 回显又取消掉（实测：置顶横幅 300ms 后就没了）。这里要的是幂等的"确保置顶"。
            current = self._store.pin_ref(event.conversation_id, message_id)
            if current is None or not current.for_both:
                if not self._store.pin_message(message_id, for_both=True):
                    return
            self._log.set_pinned_refs(event.conversation_id, conversation.pins)
            note = (f"已在双方置顶一条消息" if mine
                    else f"{who} 置顶了一条消息（双方可见）")
        else:
            if not self._store.unpin_message(message_id):
                return
            self._log.set_pinned_refs(event.conversation_id, conversation.pins)
            note = ("已取消这条置顶（对方那边也撤了）" if mine
                    else f"{who} 取消了一条置顶")
        if event.conversation_id == self._current_id:
            self._sync_pin_banner()
        self.sidebar.refresh_item(event.conversation_id)
        if not mine:
            # 自己发起的那条不用再弹一次：点下去的时候已经提示过"已置顶（双方可见）"了，
            # 服务端把事件推回来只是为了让多设备同步，不该在同一个屏幕上再说一遍
            self.toast.show_message(note, "info", 2200)

    def resync(self) -> bool:
        """重新对一次账：会话列表 + 当前会话的历史。

        什么时候用：刚连上（游标是从"现在"起的，中间那段的消息服务端不会重放）、
        或者离线太久、服务端说事件流水已经断了。对一次账就补回来了。
        """
        started = self.load_conversations()
        if self._current_id:
            self._history_loaded.discard(self._current_id)
            started = self._ensure_history(self._current_id) or started
        return started

    def _on_store_event(self, event: StoreEvent) -> None:
        if event.kind == RESET:
            self.sidebar.refresh()
            return
        if event.kind == PIN:
            self._sync_pin_banner()                     # 置顶变了：横幅跟着刷
            self.sidebar.refresh_item(event.conversation_id)
            return
        if event.kind == FRIENDS:
            # 好友 / 申请变了：侧栏那个申请角标要立刻跟上，开着的好友窗口也要重画
            self.sidebar.refresh_friends_badge()
            if self._friends_dialog is not None:
                self._friends_dialog.refresh()
            return
        if event.kind == MESSAGE and event.message is not None:
            message = event.message
            self._log.save_message(message)             # 写穿：幂等 upsert，一条一条也不慢
            self._persist_reactions([message])
            self.chat_view.sync_message(message)
            # 正开着的会话不留未读 —— 而且**要立刻告诉服务端**：
            # 只标本地的话，对方那边要等我切走再切回来才看到双勾
            if message.conversation_id == self._current_id and not message.outgoing:
                self._store.mark_read(message.conversation_id)
                self._sync_read_to_server(message.conversation_id, message.ts)
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
            dialog.notifyToggled.connect(self.set_notifications)
            dialog.soundToggled.connect(self.set_sound_enabled)
            dialog.openDataDirRequested.connect(self.open_data_dir)
            self._settings_dialog = dialog
        dialog = self._settings_dialog
        dialog.sync(theme_name=self.theme_name(), font_scale=self.text_scale(),
                    animations=self.animations_enabled(), stats=self._log.stats(),
                    notifications=self.notifications_enabled(),
                    sound=self.sound_enabled(),
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
                              animations: bool | None = None,
                              notifications: bool | None = None,
                              sound: bool | None = None) -> None:
        """别处（菜单/侧栏）改了设置时，把设置窗口里的控件同步过去（不回抛信号）。"""
        if self._settings_dialog is None:
            return
        self._settings_dialog.sync(theme_name=theme_name, font_scale=font_scale,
                                   animations=animations,
                                   notifications=notifications, sound=sound)

    # ======================================================================
    # 桌面通知 / 提示音
    # ======================================================================
    def _saved_notify(self) -> bool:
        """桌面通知开关（默认开；关掉后不再弹系统通知）。"""
        return str(self._settings.value("notify", "true")).lower() != "false"

    def _saved_sound(self) -> bool:
        """提示音开关（默认开）。"""
        return str(self._settings.value("notify_sound", "true")).lower() != "false"

    def notifications_enabled(self) -> bool:
        return self._notify_enabled

    def sound_enabled(self) -> bool:
        return self._sound_enabled

    def notifier(self) -> Notifier:
        """桌面包（测试里替换 ``message`` 记录调用）。"""
        return self._notifier

    def set_notifications(self, enabled: bool) -> None:
        """开/关桌面通知（设置里的那个开关，选择会记住）。"""
        self._notify_enabled = bool(enabled)
        self._settings.setValue("notify", "true" if enabled else "false")
        self._notifier.set_enabled(self._notify_enabled)
        self._sync_settings_dialog(notifications=self._notify_enabled)
        self.toast.show_message("桌面通知已开启" if enabled else "桌面通知已关闭",
                                "info", 1500)

    def set_sound_enabled(self, enabled: bool) -> None:
        """开/关提示音（桌面通知关掉时它不再响；这里管的是单独那一个开关）。"""
        self._sound_enabled = bool(enabled)
        self._settings.setValue("notify_sound", "true" if enabled else "false")
        self._notifier.set_sound_enabled(self._sound_enabled)
        self._sync_settings_dialog(sound=self._sound_enabled)
        self.toast.show_message("提示音已开启" if enabled else "提示音已关闭",
                                "info", 1500)

    def _sender_name(self, message) -> str:
        """这条消息是谁发的（群聊里是发言人，单聊里就是对方）。"""
        conversation = self._store.get(message.conversation_id)
        if conversation is None:
            return ""
        for member in conversation.members:
            if member.id == message.sender_id:
                return member.name
        others = [m for m in conversation.members if not m.is_me]
        return others[0].name if others else ""

    def _notify_incoming(self, message) -> None:
        """收到别人消息且我没在看那个会话 → 弹桌面通知（+ 提示音）。

        三道必须守住的线：
        * **自己发的绝不通知自己**（多设备回显也会走这里，先按 outgoing 排掉）；
        * 窗口是活的、看的又正是这个会话 —— 用户就在看，不用再打扰；
        * 整段包在 try/except 里：通知坏掉了也绝不能把\"收消息\"带崩。
        """
        if not self._notify_enabled or self._notifier is None:
            return
        if message.outgoing:
            return
        conversation_id = message.conversation_id
        if conversation_id == self._current_id and self.isActiveWindow():
            return
        conversation = self._store.get(conversation_id)
        title = conversation.title if conversation is not None else "新消息"
        sender = self._sender_name(message)
        if sender and sender != title:
            title = sender
        body = (message.text or "").strip()
        if message.attachment is not None:
            extra = f"[{message.attachment.name}]"
            body = f"{body} {extra}".strip() if body else extra
        if not body:
            body = "发来一条新消息"
        try:
            self._notifier.message(title, body)
        except Exception:                            # noqa: BLE001 —— 通知不许影响收消息
            pass

    def _apply_attachment_sent(self, event) -> None:
        """记下"我这条消息的附件在服务端是哪一份"，并存进本地库。

        为什么必须记：用户事后把本地那份图片/文件删了，消息不该只会说"不在本地了" ——
        服务器上那份一直留着（只要这条消息还在就不会被孤儿回收），按这个 id 能一键取回。
        """
        message = self._store.message(event.message_id)
        attachment = message.attachment if message is not None else None
        if message is None or attachment is None or not event.attachment_id:
            return
        if attachment.server_id == event.attachment_id:
            return
        message.attachment = replace(attachment, server_id=event.attachment_id)
        if not self._shutdown_done:
            self._log.save_message(message)

    def _on_viewer_image_changed(self, message_id: str) -> None:
        """查看器翻到一张"本地没有、服务器上还有"的图：自动去取回来。

        取回途中查看器显示"正在下载 …"，下完再刷一次（同一张图不会重复触发）。
        """
        message = self._store.message(message_id)
        attachment = message.attachment if message is not None else None
        if attachment is None or attachment.exists or not attachment.source_id:
            return
        if message_id in self._fetching:
            return
        self._fetching.add(message_id)
        if not self.download_attachment(message_id, view_after=True):
            self._fetching.discard(message_id)

    def _apply_blocked(self, conversation_id: str, message_id: str) -> None:
        """消息被好友门禁拦成申请：本地那条标成 blocked（**不重发**）并明确提示。"""
        if conversation_id and message_id:
            self._store.set_message_status(conversation_id, message_id, "blocked")
        self.toast.show_message(BLOCKED_NOTICE, "warning", 3200)

    # ======================================================================
    # 好友 / 申请（服务端权威；界面做完事只让主窗口去对账）
    # ======================================================================
    def open_friends(self) -> FriendsDialog:
        """打开好友窗口（非模态、复用同一个实例）：列表 / 申请 / 搜索加好友。"""
        if self._friends_dialog is None:
            dialog = FriendsDialog(self, api=self._api, store=self._store,
                                   theme_name=self.theme_name())
            dialog.changed.connect(self._on_friends_changed)
            self._friends_dialog = dialog
        dialog = self._friends_dialog
        dialog.apply_palette(self.current_palette())
        dialog.refresh()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def friends_dialog(self) -> FriendsDialog | None:
        return self._friends_dialog

    def _on_friends_changed(self) -> None:
        """好友窗口里做完一次操作（加 / 同意 / 拒绝 / 删）：去服务端对一次账。"""
        self.refresh_friends()
        self.sidebar.refresh()      # 同意/删好友可能让会话列表跟着变

    def refresh_friends(self) -> bool:
        """拉一次 ``GET /api/friends`` 落进 store（服务端权威）；没联网就只刷角标。"""
        if self._api is None:
            self.sidebar.refresh_friends_badge()
            return False

        def work():
            return {"data": self._api.friends()}

        if not self._friends_call.run(work):
            return False
        return True

    def _on_friends_loaded(self, payload) -> None:
        if self._shutdown_done:
            return
        data = dict((payload or {}).get("data") or {})
        self._store.set_friends(
            friends_from_json(data.get("friends"), me_id=self._user_id),
            friend_requests_from_json(data.get("incoming"), me_id=self._user_id),
            friend_requests_from_json(data.get("outgoing"), me_id=self._user_id))
        self.sidebar.refresh_friends_badge()
        if self._friends_dialog is not None:
            self._friends_dialog.refresh()

    def _apply_friend_event(self, event) -> None:
        """服务端推来的好友事件：能就地改的就改，剩下的拉一次权威清单对账。"""
        if event.user is not None:
            self._store.apply_friend_event(event.kind, event.user,
                                           note=event.note, ts=event.ts)
        self.sidebar.refresh_friends_badge()
        if self._friends_dialog is not None:
            self._friends_dialog.refresh()
        if event.kind == EVENT_FRIEND_REQUEST:
            # 服务端也会把这条事件回推给**申请方自己的其它设备**（多设备同步），那种情况下
            # event.user 就是"我"。要是不排掉，自己发起的申请会在这里弹出一句
            # "XX 申请加你为好友"（XX 还是自己），既莫名其妙又误导。
            user = event.user
            if user is not None and not getattr(user, "is_me", False):
                self.toast.show_message(f"{user.name} 申请加你为好友", "info", 2600)
        # 申请方 / 被申请方的事件长得一样，方向只有服务端知道 —— 拉一次让状态变权威
        self.refresh_friends()

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
        if self._image_viewer is not None:
            self._image_viewer.apply_palette(p)
        if self._new_chat_dialog is not None:
            self._new_chat_dialog.apply_palette(p)
        if self._friends_dialog is not None:
            self._friends_dialog.apply_palette(p)
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

    def connection_text(self) -> str:
        """侧栏底部那行小字（连着/离线/几条待发 + 账号）。"""
        return self.sidebar.mode_hint.text()

    def transport_status(self) -> dict:
        """传输层的状态快照（诊断与测试用：在线吗、几条待发、最后一条错误是什么）。

        用例失败时把"最后一条错误"打出来，比"断言为假"有用得多 ——
        联网这套的很多失败都是"网络层悄悄咽掉了"，光看断言看不出为什么。
        """
        status = getattr(self._transport, "status", None)
        return dict(status()) if callable(status) else {"name": self._transport.name}

    def logout(self) -> bool:
        """侧栏底部「退出登录」：二次确认后交给启动器换回登录窗口。

        清掉的是**这台电脑上的登录信息**（令牌缓存），本地聊天记录留在库里 ——
        下次登录同一个账号还能接着看，不用重新下载历史。
        """
        name = self._session.name if self._session is not None else ""
        if not self._confirm(
                title="退出登录", ok_text="退出",
                text=(f"会清掉这台电脑上 {name} 的登录信息，下次打开要重新登录。"
                      "本地的聊天记录不会被删。")):
            return False
        self.logoutRequested.emit()
        return True

    def shutdown(self) -> None:
        """收尾：停轮询、停后台网络、关掉所有子窗口、存草稿、关库。

        退出登录换窗口时**必须**显式调它（``deleteLater`` 之后不一定还会走到
        ``closeEvent``），否则那条长轮询线程会一直挂着占住服务器连接。
        也正因为如此，关窗之后再有后台回调回来就不能碰库了（见各 ``_on_*`` 里的
        ``_shutdown_done`` 判断）—— 库里已经关了，再写就是
        ``sqlite3.ProgrammingError: Cannot operate on a closed database``。
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._poller.stop()
        # 关窗前把还压在发件箱里的消息尽量送出去（有上限，别让"关窗口"卡住）。
        # 现在关窗＝整个进程立刻退出，不给发件箱一点时间的话，刚敲完的那句
        # 就只留在本地库里、永远发不出去（本地演示/离线传输层没有 flush，跳过）。
        # **只在真有东西待发时才等**：平时根本不进这个分支（零耗时），
        # 否则为了"最后一句"让关窗慢一下也值（上限 1 秒，原来 1.5 秒）。
        flush = getattr(self._transport, "flush", None)
        pending = getattr(self._transport, "pending", None)
        if callable(flush) and (pending() if callable(pending) else 0):
            try:
                flush(wait=1.0)
            except Exception:                            # noqa: BLE001 —— 收尾阶段不能再抛
                pass
        # stop() 里会先 api.abort() 把挂在半空的长轮询/上传掐断，所以这里不等网络。
        self._transport.stop()
        self._notifier.stop()                            # 收掉托盘 / 停掉提示音（幂等）
        for dialog in (self._search_dialog, self._settings_dialog, self._forward_dialog,
                       self._merged_viewer, self._image_viewer, self._new_chat_dialog,
                       self._friends_dialog):
            if dialog is not None:
                dialog.close()
        self._save_draft(self._current_id)               # 关窗前把草稿存住
        self._log.close()

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self.toast.reposition()

    def closeEvent(self, event) -> None:                 # noqa: N802
        self.shutdown()
        super().closeEvent(event)
        # 收尾**之后**再告诉启动器"用户把窗口关了 → 程序该退了"。
        # 必须显式发：为了登录窗口/主窗口交替，``quitOnLastWindowClosed`` 是关着的，
        # 没有这一句事件循环就永远转下去 —— 窗口是没了，进程还挂在资源管理器里。
        # （「退出登录」那条路走的是 ``logoutRequested`` + ``hide()``，不会走到这里。）
        self.closed.emit()


def main(argv: list[str] | None = None) -> int:
    """程序入口：``python main.py``。

    真正的启动流程（先账号后界面：缓存自动登录 / 登录窗口 / 主窗口）在
    :mod:`mchat.ui.launcher` 里，这里只是转出去，保证 ``mchat.app.main`` 这类
    老写法继续有效。
    """
    from .launcher import main as launcher_main

    return launcher_main(argv)
