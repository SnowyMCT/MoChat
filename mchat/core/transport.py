"""传输层：把"消息怎么送到对方"这件事抽象成接口。

现在的实现是 :class:`LocalTransport` —— 单机演示：发的消息由脚本里的台词
自动回复，不联网。将来接真网络时，只需要再写一个实现同一组方法的
``SocketTransport``（内部用 socket/协议编解码），界面层一行都不用改：

    transport = LocalTransport(...)        # 现在
    transport = SocketTransport("host", 9000)   # 以后

界面只依赖三个动作：``start()`` / ``send()`` / ``poll()``。
"""
from __future__ import annotations

import abc
import heapq
import itertools
import time
from dataclasses import dataclass, field

__all__ = ["Outgoing", "TransportEvent", "ChatTransport", "ReplyScript",
           "LocalTransport", "EVENT_MESSAGE", "EVENT_TYPING", "EVENT_READ"]

EVENT_MESSAGE = "message"
EVENT_TYPING = "typing"
EVENT_READ = "read"


@dataclass(frozen=True)
class Outgoing:
    """一条要发出去的消息。"""

    conversation_id: str
    sender_id: str
    text: str
    message_id: str = ""


@dataclass(frozen=True)
class TransportEvent:
    """从传输层收到的异步事件。"""

    kind: str
    conversation_id: str
    sender_id: str = ""
    text: str = ""
    ts: float = 0.0
    typing: bool = False
    message_id: str = ""


@dataclass
class ReplyScript:
    """单机演示用：某个会话由谁、按什么台词、延迟多久回复。"""

    sender_id: str
    lines: tuple[str, ...]
    latency: float = 1.2
    typing_lead: float = 0.3

    def line(self, index: int) -> str:
        """按序取台词（循环使用，超出部分回到开头）。"""
        if not self.lines:
            return ""
        return self.lines[index % len(self.lines)]


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
        if script is None or not message.text.strip():
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
