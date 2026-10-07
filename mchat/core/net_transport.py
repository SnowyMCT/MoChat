"""联网传输层：真·发消息 / 收消息（长轮询），界面层完全不知道网络的存在。

界面只调三个方法
----------------
``start()`` 起一条后台线程；``send(Outgoing)`` 把消息丢进发件箱立刻返回（**绝不卡界面**）；
``poll()`` 把后台收到的事件倒出来交给 :class:`~mchat.ui.main_window.MainWindow` ——
和本机演示那个 :class:`~mchat.core.transport.LocalTransport` 一模一样的形状，
所以主窗口里那段 ``tick()`` 的代码不用为联网改写。

为什么所有 HTTP 都挤在后台那一条线程里
--------------------------------------
* 界面线程绝不能被网络阻塞（长轮询一挂就是 20 秒）；
* 一条线程 = 一件一件地发，天然保证"消息顺序不会乱"；
* 不用给 ``urllib`` 加锁，也不会出现两个线程同时写同一个连接池的问题。
* 但**收发必须分开两条线程**：长轮询一次要挂十几秒，如果发送也在同一条线程上排队，
  用户敲完一句"在吗"要等到下一次长轮询返回才发得出去（实测就是这样，消息卡在发件箱里
  最多 20 秒）。所以：一条线程专门长轮询收事件，另一条专门发（发出去后被唤醒，立刻发）。
  两条线程各用各的 HTTP 连接，互不干扰；发件箱仍在发送线程内串行，顺序不乱。

三件必须做对的事
----------------
1. **掉线自动重连**：连不上就退避重试（1s → 2s → 5s），发件箱里的消息**不丢**；
   重发用的是同一个消息 id，服务端幂等，所以不会变成两条（这是协议里刻意设计的）。
2. **自己的消息也会被服务端推回来**（同一账号的其它设备要看到），传输层照实交给界面，
   由界面按 id 去重 —— 不能在这里偷偷丢掉，否则多设备同步就废了。
3. **附件先上传再发**：``send`` 时若附件还在本地，先 ``/api/upload`` 拿到 id 再发消息；
   收到带附件的消息时立刻下到本地缓存目录，下载失败就保留 ``remote://`` 标记，
   界面会显示成"文件不在本地了"并且允许点一下重试下载。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

from .api import (ApiClient, ApiError, NetworkError, conversation_from_json,
                  message_from_json, reactions_from_json, user_from_json)
from .models import Attachment, Conversation, Message
from .paths import resolve_attachment_path
from .transport import (EVENT_ATTACHMENT_SENT, EVENT_CONVERSATION, EVENT_FRIEND_ACCEPT,
                        EVENT_FRIEND_REJECT, EVENT_FRIEND_REMOVE,
                        EVENT_FRIEND_REQUEST, EVENT_MESSAGE, EVENT_PIN,
                        EVENT_PRESENCE, EVENT_REACTION, EVENT_READ, EVENT_RESYNC,
                        EVENT_SEND_BLOCKED, EVENT_TYPING, ChatTransport, Outgoing,
                        TransportEvent)

__all__ = ["NetworkTransport"]

DEFAULT_SYNC_TIMEOUT = 20.0          # 一次长轮询最多挂多久（服务端上限 30）
BACKOFF_STEPS = (1.0, 2.0, 5.0)      # 连不上时的退避阶梯，到顶就一直用最后一档
IDLE_WAIT = 0.3                      # 每轮之间的最小间隔（有活干时会被立刻唤醒）
EXECUTOR_TICK = 0.05                 # 后台线程被唤醒时的轮询粒度
FIRST_SYNC_TIMEOUT = 2.0             # 第一次同步的短超时（见 _long_poll 里的说明）
# 多大的附件才"自动"下：比这大的只留一个"点击下载"的卡片。
# 之前是来者不拒地全自动下载 —— 对端反复发 64MB 的文件就能把磁盘和带宽吃掉，
# 而且用户没有任何机会说"我不要"。16MB 够覆盖日常的图片/文档。
AUTO_DOWNLOAD_MAX = 16 * 1024 * 1024


class NetworkTransport(ChatTransport):
    """走 HTTP 的传输层。``api`` 是 :class:`~mchat.core.api.ApiClient`。"""

    name = "联网"

    def __init__(self, api: ApiClient, *, me_id: str = "", download_dir: str = "",
                 sync_timeout: float = DEFAULT_SYNC_TIMEOUT, clock=time.time,
                 auto_download_max: int = AUTO_DOWNLOAD_MAX, autostart: bool = True):
        self.api = api
        self.me_id = me_id                       # 我自己的服务端 id（判断"这条是不是我发的"）
        self.download_dir = download_dir         # 收到附件先落这里
        self.sync_timeout = float(sync_timeout)
        self.auto_download_max = int(auto_download_max or 0)   # 0 = 多大都自动下
        self._clock = clock

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._outbox: list[Outgoing] = []        # 待发送（掉线时攒在这里，顺序不变）
        self._tasks: list[tuple] = []            # 已读回执 / 正在输入这类"顺路做一下"的小活
        self._events: list[TransportEvent] = []  # 待界面取走的事件
        self._uploaded: dict[tuple, str] = {}    # (路径, 大小) -> 服务器附件 id（同一张图别重复上传）
        self._cursor = 0
        self._primed = False                     # 第一次同步（游标从"现在"起）做过没有
        self._error = ""                         # 最近一次网络错误（界面上显示"离线 · 原因"）
        self._auth_error = ""                    # 登录失效了："缓存也不认了"，界面该退回登录窗
        self._online = False
        self._sent = 0                           # 成功发出去的条数（自检/测试用）
        self._received = 0
        if autostart:
            self.start()

    # ================================================================== 状态
    @property
    def online(self) -> bool:
        return self._online

    @property
    def auth_error(self) -> str:
        """非空 = token 已失效（界面据此退回登录界面）。会读一次就清掉。"""
        with self._lock:
            return self._auth_error

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    def status(self) -> dict:
        with self._lock:
            return {"online": self._online, "pending": len(self._outbox),
                    "cursor": self._cursor, "error": self._error,
                    "sent": self._sent, "received": self._received}

    def pending(self) -> int:
        """还没发出去的消息条数（侧栏底部那行"离线 · N 条待发"用的就是它）。"""
        with self._lock:
            return len(self._outbox)

    def last_error(self) -> str:
        with self._lock:
            return self._error

    def describe(self) -> str:
        """侧栏底部那行小字：连上了就说连上了，掉线了说清楚在等什么。"""
        host = self.api.base_url.split("://")[-1]
        if self._online:
            return f"已连接 · {host}"
        if not self._primed:
            return f"正在连接 {host} …"          # 刚打开程序、第一轮长轮询还没回来
        waiting = self.pending()
        tail = f"，{waiting} 条待发" if waiting else ""
        return f"离线{tail} · 正在重连 {host}"

    # ================================================================== 生命周期
    def start(self) -> None:
        """起两条后台线程：一条收发事件（长轮询），一条专管发出去（发件箱 + 已读回执）。"""
        if self.is_running():
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._poll_loop, name="mchat-net-poll", daemon=True),
            threading.Thread(target=self._send_loop, name="mchat-net-send", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, *, timeout: float = 0.8) -> None:
        """停掉后台线程（关窗口时调）。**发件箱不清空**，只是不再发。

        ``join`` 之前先 ``api.abort()``：收的那条线程通常正挂在服务器上的长轮询里
        （最长 20 秒），不掐断就只能 join 到超时 —— 用户点 ✕ 之后窗口要卡好几秒
        （实测 3.0 秒起，掉线时更久），像是程序卡死了。掐断之后线程几十毫秒内就
        从 HTTP 里出来，``join`` 基本立刻返回。
        """
        self._stop.set()
        self._wake.set()
        abort = getattr(self.api, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:                            # noqa: BLE001 —— 收尾阶段不能再抛
                pass
        for thread in list(self._threads):
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=timeout)
        self._threads = []

    def is_running(self) -> bool:
        """后台线程还在跑吗（界面据此禁用按钮 + 显示"正在连接…"）。"""
        return any(thread.is_alive() for thread in self._threads)

    # ================================================================== 发
    def send(self, message: Outgoing) -> None:
        """入队即返回。真正的发送在后台线程里做，失败会自动重试。"""
        with self._lock:
            self._outbox.append(message)
        self._wake.set()

    def mark_read(self, conversation_id: str, up_to_ts: float) -> None:
        """告诉服务端"这个会话我看到哪了"（对方的气泡会变成双勾）。"""
        with self._lock:
            self._tasks.append(("read", conversation_id, float(up_to_ts)))
        self._wake.set()

    def send_typing(self, conversation_id: str, typing: bool = True) -> None:
        with self._lock:
            self._tasks.append(("typing", conversation_id, bool(typing)))
        self._wake.set()

    def flush(self, *, wait: float = 5.0) -> bool:
        """等发件箱清空（测试与"退出前把话说出去"用）。返回是否真的清空了。"""
        deadline = self._clock() + wait
        while self._clock() < deadline:
            if self.pending() == 0:
                return True
            self._wake.set()
            time.sleep(EXECUTOR_TICK)
        return self.pending() == 0

    # ================================================================== 收
    def poll(self, now: float | None = None) -> list[TransportEvent]:
        """取出后台攒下的所有事件（界面每 400ms 调一次，形状同本机演示）。"""
        with self._lock:
            events, self._events = self._events, []
        return events

    # ================================================================== 附件
    def localize(self, attachment: Attachment | None) -> Attachment | None:
        """把"还在服务器上"的附件下到本地缓存目录；失败就原样返回（界面会降级显示）。

        超过 ``auto_download_max`` 的附件**不自动下**（保持 ``remote://`` 标记，
        界面显示成"点一下再下"）：对面拿大文件刷屏时，磁盘和带宽不该由他说了算。
        """
        if attachment is None or not attachment.is_remote or not self.download_dir:
            return attachment
        if self.auto_download_max and attachment.size > self.auto_download_max:
            return attachment
        file_id = attachment.source_id
        dest = self.cache_path(attachment)
        try:
            if not os.path.isfile(dest):
                self.api.download(file_id, dest, root=self.download_dir)
        except (ApiError, NetworkError, OSError) as error:
            # 保持 remote:// 标记（界面显示"文件不在本地"，点一下能重试）。
            # 但**别一声不响**：被拒的原因要留在错误栏里，否则排查时只看到"附件一直没下来"。
            with self._lock:
                self._error = f"附件没下来：{error}"
            return attachment
        return Attachment.from_path(dest)

    def cache_path(self, attachment: Attachment) -> str:
        """收到的附件在本地的落地路径（``<附件id>-<安全文件名>``，重名也不会互相覆盖）。

        名字是**发送者**给的，所以这里必须过
        :func:`mchat.core.paths.resolve_attachment_path` —— 落地路径只在那一个地方算，
        也正是"附件名里带 ..\\ 就能写到下载目录外"这个洞的唯一补丁点。
        """
        folder = self.download_dir or tempfile.gettempdir()
        return resolve_attachment_path(folder, attachment.source_id, attachment.name)

    # ================================================================== 后台线程
    def _poll_loop(self) -> None:
        """长轮询收事件：拿到东西就立刻再来一轮（这就是"实时"）。"""
        attempt = 0
        while not self._stop.is_set():
            try:
                self._long_poll()
                self._set_online(True)
                attempt = 0
            except ApiError as error:
                attempt = self._handle_api_error(error, attempt)
            except NetworkError as error:
                attempt = self._handle_network_error(error, attempt)
            except Exception as error:                 # 兜底：后台线程绝不能静默死掉
                self._note_internal_error(error)
                self._stop.wait(2.0)

    def _send_loop(self) -> None:
        """发件箱 + 已读回执/正在输入这类小活。

        它**独立于长轮询**：`send()` 一入队就唤醒它，消息立刻发出去，
        不必等那条挂在半空的长轮询请求回来（否则发一句话可能要等十几秒）。
        """
        attempt = 0
        while not self._stop.is_set():
            try:
                before = self._sent
                self._run_tasks()
                self._flush_outbox()
                if self._sent > before:            # 真的发出去东西了 = 网络是通的
                    self._set_online(True)
                attempt = 0
            except ApiError as error:
                attempt = self._handle_api_error(error, attempt)
            except NetworkError as error:
                attempt = self._handle_network_error(error, attempt)
            except Exception as error:
                self._note_internal_error(error)
                self._stop.wait(2.0)
            self._wake.wait(IDLE_WAIT)          # 有新活时立刻返回
            self._wake.clear()

    def _handle_api_error(self, error: ApiError, attempt: int) -> int:
        """服务端明确回了错误：登录失效要上报界面，其余退避重试。"""
        if error.is_auth:
            with self._lock:
                self._auth_error = error.friendly
            self._set_online(False)
            self._stop.wait(2.0)                 # 等界面把它收走（退回登录界面）
            return attempt
        with self._lock:
            self._error = error.friendly
        self._set_online(False)
        self._stop.wait(BACKOFF_STEPS[min(attempt, len(BACKOFF_STEPS) - 1)])
        return attempt + 1

    def _handle_network_error(self, error: NetworkError, attempt: int) -> int:
        with self._lock:
            self._error = error.friendly
        self._set_online(False)
        self._stop.wait(BACKOFF_STEPS[min(attempt, len(BACKOFF_STEPS) - 1)])
        return attempt + 1

    def _note_internal_error(self, error: BaseException) -> None:
        with self._lock:
            self._error = f"内部错误：{type(error).__name__}: {error}"

    def _run_tasks(self) -> None:
        while True:
            with self._lock:
                if not self._tasks:
                    return
                task = self._tasks.pop(0)
            kind = task[0]
            if kind == "read":
                self.api.mark_read(task[1], task[2])
            elif kind == "typing":
                self.api.typing(task[1], task[2])

    def _flush_outbox(self) -> None:
        """按顺序发出去；只要有一条没成功就退出，下次接着从这条发（顺序不会乱）。"""
        while True:
            with self._lock:
                if not self._outbox:
                    return
                item = self._outbox[0]
            try:
                self._send_one(item)
            except ApiError as error:
                if error.code == "duplicate_id":
                    pass                            # 服务端说这条 id 已经在别处用过了：跳过它
                else:
                    raise
            with self._lock:
                if self._outbox and self._outbox[0] is item:
                    self._outbox.pop(0)
                self._sent += 1

    def _send_one(self, outgoing: Outgoing) -> None:
        attachment_id = ""
        attachment = outgoing.attachment
        if attachment is not None and not attachment.is_remote:
            attachment_id = self._upload(attachment)
        if attachment_id:
            # 把"服务端那份附件叫什么 id"交给界面记下来（并落库）：用户事后删掉本地文件时，
            # 消息才不至于只会说"不在本地了"—— 服务器上还有那份，能按这个 id 取回来。
            event = TransportEvent(
                kind=EVENT_ATTACHMENT_SENT, conversation_id=outgoing.conversation_id,
                message_id=outgoing.message_id, attachment_id=attachment_id)
            with self._lock:
                self._events.append(event)
        result = self.api.send_message(
            outgoing.conversation_id, outgoing.message_id or _fallback_id(outgoing),
            outgoing.text, attachment_id=attachment_id, reply_to=outgoing.reply_to,
            forwarded_from=outgoing.forwarded_from, merged=outgoing.merged_items)
        # 好友门禁：这条消息**没有被投递**，而是变成了对方的一条好友申请
        # （服务端响应里 message 是 null）。它是一次正常结果，绝不能拿原来那个
        # client_id 再发一遍 —— 那只会把同一条申请反复刷给对方。
        request = getattr(result, "friend_request", None)
        if request:
            self._note_blocked(outgoing, request)

    def _note_blocked(self, outgoing: Outgoing, request: dict) -> None:
        """把"消息被好友门禁拦成了申请"这件事交给界面（本地那条要换个样子）。"""
        user = user_from_json(request.get("user"), me_id=self.me_id)
        note = str(request.get("note") or "")
        try:
            ts = float(request.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        event = TransportEvent(
            kind=EVENT_SEND_BLOCKED, conversation_id=outgoing.conversation_id,
            message_id=outgoing.message_id, sender_id=user.id, user=user,
            text=note, note=note, ts=ts)
        with self._lock:
            self._events.append(event)

    def _upload(self, attachment: Attachment) -> str:
        """上传一次就记住（同一个文件改个名再发不算——按路径+大小认）。"""
        key = (attachment.path, attachment.size)
        with self._lock:
            cached = self._uploaded.get(key)
        if cached:
            return cached
        info = self.api.upload(attachment.path, name=attachment.name)
        file_id = str(info.get("id") or "")
        if file_id:
            with self._lock:
                self._uploaded[key] = file_id
        return file_id

    def _long_poll(self) -> None:
        first = not self._primed
        # 第一次同步用短超时：服务端对"游标 0"的处理是"从现在的最大序号起"，
        # 对一个还没有任何事件的新账号，它会一直等到有事件才返回（最长一个 timeout）。
        # 那样界面开头十几秒都停在"正在连接…"，看着像连不上 —— 短超时先把手握做了，
        # 之后就一直用正常的 20 秒长轮询。
        timeout = self.sync_timeout if self._primed else min(self.sync_timeout,
                                                             FIRST_SYNC_TIMEOUT)
        data = self.api.sync(self._cursor, timeout=timeout)
        translated: list[TransportEvent] = []
        if first:
            # 第一次连上（或刚重连上）：游标是从"现在"开始的，中间那段时间发生的事
            # 服务端不会重放 —— 提醒界面重新对一次账（重拉会话列表 + 当前会话历史）。
            # 顺带也覆盖了"服务端说我离线太久、事件流水已经断了"那种情况。
            translated.append(TransportEvent(kind=EVENT_RESYNC))
        for raw in data.get("events") or []:
            try:
                event = self._translate(raw)
            except Exception as error:                # 单条坏事件不能把整个流掐断
                with self._lock:
                    self._error = f"解析事件失败：{type(error).__name__}: {error}"
                event = None
            if event is not None:
                translated.append(event)
        # 翻译在锁外做（附件下载是网络操作，别占着锁）；最后一次性入队
        with self._lock:
            self._cursor = int(data.get("cursor", self._cursor) or self._cursor)
            self._primed = True
            self._events.extend(translated)
            self._received += len(translated)

    def _translate(self, raw) -> TransportEvent | None:
        """线上事件 -> 界面事件（认不出来的事件类型直接忽略：向前兼容）。"""
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "")
        conversation_id = str(raw.get("conversation_id") or "")
        if kind == EVENT_MESSAGE:
            message = self._message(raw.get("message"))
            if message is None:
                return None
            return TransportEvent(
                kind=EVENT_MESSAGE, conversation_id=message.conversation_id,
                sender_id=message.sender_id, text=message.text, ts=message.ts,
                message_id=message.id, attachment=message.attachment, message=message)
        if kind == EVENT_READ:
            return TransportEvent(kind=EVENT_READ, conversation_id=conversation_id,
                                  message_id=str(raw.get("message_id") or ""),
                                  sender_id=str(raw.get("reader_id") or ""),
                                  ts=float(raw.get("ts") or 0.0))
        if kind == EVENT_TYPING:
            sender = str(raw.get("user_id") or "")
            if sender and sender == self.me_id:
                return None                       # 别的设备替我打的字，别在我这儿显示
            return TransportEvent(kind=EVENT_TYPING, conversation_id=conversation_id,
                                  sender_id=sender, typing=bool(raw.get("typing")))
        if kind == EVENT_CONVERSATION:
            conversation = self._conversation(raw.get("conversation"))
            if conversation is None:
                return None
            return TransportEvent(kind=EVENT_CONVERSATION,
                                  conversation_id=conversation.id,
                                  conversation=conversation)
        if kind == EVENT_PIN:
            message_id = str(raw.get("message_id") or "")
            message = self._message(raw.get("message")) if raw.get("message") else None
            if not message_id and message is not None:
                message_id = message.id
            if not message_id:
                return None
            return TransportEvent(kind=EVENT_PIN, conversation_id=conversation_id,
                                  message_id=message_id, message=message,
                                  pinned=bool(raw.get("pinned", True)),
                                  sender_id=str(raw.get("pinned_by") or ""),
                                  pinned_by_name=str(raw.get("pinned_by_name") or ""))
        if kind == EVENT_REACTION:
            message_id = str(raw.get("message_id") or "")
            if not message_id:
                return None
            return TransportEvent(kind=EVENT_REACTION, conversation_id=conversation_id,
                                  message_id=message_id,
                                  emoji=str(raw.get("emoji") or ""),
                                  added=bool(raw.get("added", True)),
                                  reactions=reactions_from_json(raw.get("reactions")),
                                  sender_id=str(raw.get("by_id") or ""),
                                  pinned_by_name=str(raw.get("by_name") or ""))

        if kind == EVENT_PRESENCE:
            user_id = str(raw.get("user_id") or "")
            if not user_id or user_id == self.me_id:
                return None
            return TransportEvent(kind=EVENT_PRESENCE, conversation_id="",
                                  sender_id=user_id, online=bool(raw.get("online")))

        if kind in (EVENT_FRIEND_REQUEST, EVENT_FRIEND_ACCEPT,
                    EVENT_FRIEND_REJECT, EVENT_FRIEND_REMOVE):
            # 好友事件只带一个 ``user``（申请/同意的"另一个人"），没有会话。
            # 我自己的 id 会被换成 ME_ID，界面据此认出"这是我在别处的动作"。
            user = user_from_json(raw.get("user"), me_id=self.me_id)
            note = str(raw.get("note") or "")
            try:
                ts = float(raw.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            return TransportEvent(kind=kind, conversation_id="",
                                  sender_id=user.id, user=user, note=note,
                                  text=note, ts=ts)

        if kind == EVENT_RESYNC:
            return TransportEvent(kind=EVENT_RESYNC, conversation_id=conversation_id)
        return None

    def _message(self, raw) -> Message | None:
        from .api import message_from_json
        message = message_from_json(raw, me_id=self.me_id)
        if message is None:
            return None
        # 附件：能下就下下来，下不了就保留 remote:// 标记
        if message.attachment is not None and message.attachment.is_remote:
            message.attachment = self.localize(message.attachment)
        return message

    def _conversation(self, raw) -> Conversation | None:
        from .api import conversation_from_json
        return conversation_from_json(raw, me_id=self.me_id)

    def _set_online(self, value: bool) -> None:
        with self._lock:
            if value and not self._online:
                self._error = ""
            self._online = bool(value)


def _fallback_id(outgoing: Outgoing) -> str:
    """没给 id 的调用（老代码/测试）兜一个出来：服务端要求 client_id 非空。"""
    import uuid
    return uuid.uuid4().hex
