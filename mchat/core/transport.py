"""传输层：把"消息怎么送到对方"这件事抽象成接口。

界面只依赖三个动作：``start()`` / ``send()`` / ``poll()``，所以换实现界面一行都不用改：

    transport = NetworkTransport(api)      # 真联网（mchat/core/net_transport.py）
    transport = LocalTransport(scripts)    # 本机演示/测试（脚本自动回复）

``NetworkTransport`` 是真实使用的那一个：``send()`` 把消息丢进发件箱由后台线程发出，
``poll()`` 把后台长轮询收到的事件倒给界面 —— 形状和本文件里这个假实现完全一样。

附件（图片/文件）跟着消息一起走：:class:`Outgoing` 与 :class:`TransportEvent` 各带一个
``attachment``。联网时由传输层负责"先上传拿到远端 id → 再发消息"，对界面而言
附件始终是"一个路径 + 名字 + 大小"，这一层的形状没有变。
"""
from __future__ import annotations

import abc
import heapq
import itertools
import time
from dataclasses import dataclass, field

from .models import Attachment, Conversation, Message, ReplyRef

__all__ = ["Outgoing", "TransportEvent", "ChatTransport", "ReplyScript",
           "LocalTransport", "NullTransport",
           "EVENT_MESSAGE", "EVENT_TYPING", "EVENT_READ", "EVENT_CONVERSATION",
           "EVENT_RESYNC"]

EVENT_MESSAGE = "message"
EVENT_TYPING = "typing"
EVENT_READ = "read"
EVENT_CONVERSATION = "conversation"     # 有人拉我进了新会话（联网时服务端推的）
EVENT_RESYNC = "resync"                 # 事件流水断了：界面重新拉一次会话列表


@dataclass(frozen=True)
class Outgoing:
    """一条要发出去的消息（``attachment`` 为 None 就是纯文字）。

    ``message_id`` 就是本地那条消息的 id：联网时它会被当成服务端的消息 id
    （协议里的 ``client_id``），所以**重发同一条不会变成两条**（幂等）。
    引用 / 转发 / 合并转发这三个"附加信息"也要一起送出去，否则对面收到的
    就只剩一句光秃秃的话。
    """

    conversation_id: str
    sender_id: str
    text: str
    message_id: str = ""
    attachment: Attachment | None = None
    reply_to: ReplyRef | None = None
    forwarded_from: str = ""
    merged_items: tuple = ()


@dataclass(frozen=True)
class TransportEvent:
    """从传输层收到的异步事件。

    ``message`` / ``conversation`` 是**联网时才会带上**的完整对象：服务端已经给全了
    （id、时间戳、引用、转发…），界面直接用，不必自己再拼一个。本机演示那条路
    仍然只给 ``text`` 这种"散装"字段，界面两种都认。
    """

    kind: str
    conversation_id: str = ""      # 有默认值：像 resync 这种"全局"事件不需要会话 id
    sender_id: str = ""
    text: str = ""
    ts: float = 0.0
    typing: bool = False
    message_id: str = ""
    attachment: Attachment | None = None
    message: Message | None = None
    conversation: Conversation | None = None


@dataclass
class ReplyScript:
    """单机演示用：某个会话由谁、按什么台词、延迟多久回复。

    ``attachments`` 是"对方会陆续发过来的文件"：第 n 次回复带上第 n 个附件，
    用完就不再带 —— 这样"收到别人发来的图片/文件"这条路径也能在本机演示里跑通，
    不用手写假事件。
    """

    sender_id: str
    lines: tuple[str, ...]
    latency: float = 1.2
    typing_lead: float = 0.3
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)

    def line(self, index: int) -> str:
        """按序取台词（循环使用，超出部分回到开头）。"""
        if not self.lines:
            return ""
        return self.lines[index % len(self.lines)]

    def attachment(self, index: int) -> Attachment | None:
        """第 ``index`` 次回复附带的文件（没有就是 None）。"""
        return self.attachments[index] if index < len(self.attachments) else None


class ChatTransport(abc.ABC):
    """传输层接口。"""

    name = "abstract"
    online = False

    def start(self) -> None:
        """连接/启动。单机演示直接置位。"""

    def stop(self) -> None:
        """断开/停止。"""

    @abc.abstractmethod
    def send(self, message: Outgoing) -> None:
        """发送一条消息（不必立刻送达）。"""

    @abc.abstractmethod
    def poll(self, now: float | None = None) -> list[TransportEvent]:
        """取出所有"已经到了"的事件；没消息时返回空列表。"""


class LocalTransport(ChatTransport):
    """单机演示传输层。

    * ``send()`` 不联网，而是按 :class:`ReplyScript` 排一个"对方正在输入…
      -> 回复"的事件序列；
    * ``poll(now)`` 把到期的事件倒出来（用时间戳判断，不依赖真实等待，
      所以单测里可以直接传 ``now + 10`` 拿到结果）。
    """

    name = "本机演示"
    READ_DELAY = 0.6          # 发出去多久后"对方已读"

    def __init__(self, scripts: dict[str, ReplyScript] | None = None,
                 *, clock=time.time):
        self._scripts: dict[str, ReplyScript] = dict(scripts or {})
        self._clock = clock
        self._queue: list[tuple[float, int, TransportEvent]] = []
        self._counter = itertools.count()
        self._replies_sent: dict[str, int] = {}
        self.online = True

    # ------------------------------------------------------------------ 接口
    def send(self, message: Outgoing) -> None:
        script = self._scripts.get(message.conversation_id)
        if script is None:
            return
        # 空消息不回：既没文字也没附件的调用不该触发一轮"对方正在输入…"
        if not message.text.strip() and message.attachment is None:
            return
        now = self._clock()
        index = self._replies_sent.get(message.conversation_id, 0)
        self._replies_sent[message.conversation_id] = index + 1
        self._push(now + script.typing_lead, TransportEvent(
            kind=EVENT_TYPING, conversation_id=message.conversation_id,
            sender_id=script.sender_id, typing=True, ts=now + script.typing_lead,
        ))
        self._push(now + script.latency, TransportEvent(
            kind=EVENT_MESSAGE, conversation_id=message.conversation_id,
            sender_id=script.sender_id, text=script.line(index),
            ts=now + script.latency, typing=False,
            attachment=script.attachment(index),
        ))
        if message.message_id:
            # 顺手模拟一次"对方已读"，气泡上的单勾会变双勾
            self._push(now + self.READ_DELAY, TransportEvent(
                kind=EVENT_READ, conversation_id=message.conversation_id,
                message_id=message.message_id, ts=now + self.READ_DELAY,
            ))

    def poll(self, now: float | None = None) -> list[TransportEvent]:
        now = self._clock() if now is None else now
        ready: list[TransportEvent] = []
        while self._queue and self._queue[0][0] <= now:
            ready.append(heapq.heappop(self._queue)[2])
        ready.sort(key=lambda event: event.ts)
        return ready

    # ------------------------------------------------------------------ 辅助
    def pending(self) -> int:
        """还没到期的待投递事件数（测试与调试用）。"""
        return len(self._queue)

    def _push(self, when: float, event: TransportEvent) -> None:
        heapq.heappush(self._queue, (when, next(self._counter), event))


class NullTransport(ChatTransport):
    """什么都不做的传输层：没登录/没服务器时占位用，界面照常能打开（历史还在本地）。

    "离线"是明确的状态（``online = False``），而不是把界面搞崩。
    """

    name = "未连接"
    online = False

    def send(self, message: Outgoing) -> None:
        return None

    def poll(self, now: float | None = None) -> list[TransportEvent]:
        return []
