"""会话仓库：内存数据 + 查询 + 变更通知。

为什么要有这一层
----------------
界面（列表、消息区、未读徽标）需要的是"按会话组织好的消息"，而不是散落的
列表操作。把增删查、排序、未读、搜索集中在这里，界面只订阅变更事件，
于是：

* 换传输层（单机演示 -> 真网络）时界面一行都不用改，新消息 append 进来即可；
* 未读计数、排序规则等规则只有一处实现，不会在几个控件里各写一遍。

本模块同样不依赖 Qt：订阅回调是普通函数，界面层把它接到自绘控件上。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import ChatKind, Conversation, Message

__all__ = ["StoreEvent", "ChatStore"]

MESSAGE = "message"
READ = "read"
RESET = "reset"


@dataclass(frozen=True)
class StoreEvent:
    """仓库变更事件：``kind`` 决定界面刷新哪一块。"""

    kind: str
    conversation_id: str = ""
    message: Message | None = None


class ChatStore:
    """会话与消息的内存仓库。"""

    def __init__(self, conversations: Iterable[Conversation] = ()):
        # 网络层将来会在别的线程里 append，加一把锁最省事（递归锁，事件回调里
        # 允许再次读仓库，不会自锁）
        self._lock = threading.RLock()
        self._conversations: dict[str, Conversation] = {}
        self._order: list[str] = []
        self._unread: dict[str, int] = {}
        self._listeners: list[Callable[[StoreEvent], None]] = []
        for conv in conversations:
            self.add_conversation(conv)

    # ==================================================================
    # 读取
    # ==================================================================
    def conversations(self) -> list[Conversation]:
        """所有会话：置顶优先，其余按最近消息时间倒序。"""
        with self._lock:
            items = [self._conversations[cid] for cid in self._order
                     if cid in self._conversations]
            items.sort(key=lambda c: (not c.pinned, -c.last_ts))
            return items

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    def unread(self, conversation_id: str) -> int:
        with self._lock:
            return self._unread.get(conversation_id, 0)

    def total_unread(self) -> int:
        """所有未读之和 —— **免打扰的会话不计**（顶部徽标只提醒"需要你看的"）。"""
        with self._lock:
            total = 0
            for conversation_id, count in self._unread.items():
                conversation = self._conversations.get(conversation_id)
                if conversation is not None and conversation.muted:
                    continue
                total += count
            return total

    def search(self, keyword: str) -> list[Conversation]:
        """按会话名或消息内容过滤（空关键词返回全部）。"""
        keyword = (keyword or "").strip().lower()
        if not keyword:
            return self.conversations()
        hits = []
        for conv in self.conversations():
            if keyword in conv.title.lower() or any(
                    keyword in msg.text.lower() for msg in conv.messages):
                hits.append(conv)
        return hits

    # ==================================================================
    # 写入
    # ==================================================================
    def add_conversation(self, conv: Conversation, *, unread: int = 0) -> None:
        with self._lock:
            if conv.id not in self._conversations:
                self._order.append(conv.id)
            self._conversations[conv.id] = conv
            self._unread[conv.id] = unread
        self._emit(StoreEvent(RESET, conv.id))

    def append(self, message: Message) -> None:
        """追加一条消息（按时间插入，收到乱序消息也不会错位）。"""
        with self._lock:
            conv = self._conversations.get(message.conversation_id)
            if conv is None:
                return
            msgs = conv.messages
            if msgs and message.ts < msgs[-1].ts:
                index = len(msgs)
                while index > 0 and msgs[index - 1].ts > message.ts:
                    index -= 1
                msgs.insert(index, message)
            else:
                msgs.append(message)
        self._emit(StoreEvent(MESSAGE, message.conversation_id, message))

    def hydrate(self, conversation_id: str, messages: Iterable[Message]) -> int:
        """把本地历史并进内存（启动时回填）。

        按 id 去重后整体按时间排序 —— 演示数据每次启动都会用"相对现在"的时间戳重建，
        历史里那些同样的 id 会被新的盖掉，用户自己发过的消息则原样留下，两者都不会重复。
        """
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return 0
            merged = {msg.id: msg for msg in conv.messages}
            added = 0
            for message in messages:
                if message.id not in merged:
                    added += 1
                elif message.ts <= merged[message.id].ts:
                    continue                      # 演示数据更新鲜，保留它
                merged[message.id] = message
            conv.messages[:] = sorted(merged.values(), key=lambda m: m.ts)
        self._emit(StoreEvent(RESET, conversation_id))
        return added

    def remove(self, message_id: str) -> bool:
        """删掉一条消息（撤回自己的消息用）。"""
        with self._lock:
            for conv in self._conversations.values():
                for index, message in enumerate(conv.messages):
                    if message.id == message_id:
                        del conv.messages[index]
                        conversation_id = conv.id
                        break
                else:
                    continue
                break
            else:
                return False
        self._emit(StoreEvent(RESET, conversation_id))
        return True

    def set_pinned(self, conversation_id: str, pinned: bool) -> bool:
        """置顶/取消置顶。列表顺序跟着变，所以发 RESET 让侧栏整个重建。"""
        return self.set_flag(conversation_id, "pinned", pinned)

    def set_muted(self, conversation_id: str, muted: bool) -> bool:
        """免打扰：未读徽标不再计入总数、也不再弹跳（Telegram 的做法）。"""
        return self.set_flag(conversation_id, "muted", muted)

    def set_flag(self, conversation_id: str, name: str, value: bool) -> bool:
        """改会话上的布尔旗标（pinned / muted），返回是否真的变了。"""
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or getattr(conversation, name) == bool(value):
                return False
            setattr(conversation, name, bool(value))
        self._emit(StoreEvent(RESET, conversation_id))
        return True

    def clear_messages(self, conversation_id: str) -> int:
        """清空**某一个**会话的消息（会话本身留着），返回删掉几条。"""
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return 0
            removed = len(conversation.messages)
            conversation.messages.clear()
            self._unread[conversation_id] = 0
        self._emit(StoreEvent(RESET, conversation_id))
        return removed

    def receive(self, message: Message) -> None:
        """收到一条消息：先累加未读，再入库。

        顺序有讲究：入库会通知界面刷新，而界面发现"这条会话正开着"就会立刻
        调 :meth:`mark_read` 把它归零 —— 先加后清才对；反过来的话未读会残留。
        """
        if not message.outgoing:
            self.bump_unread(message.conversation_id)
        self.append(message)

    def mark_read(self, conversation_id: str) -> bool:
        """把会话标为已读，返回是否真的发生了变化。"""
        with self._lock:
            if not self._unread.get(conversation_id):
                return False
            self._unread[conversation_id] = 0
        self._emit(StoreEvent(READ, conversation_id))
        return True

    def bump_unread(self, conversation_id: str) -> None:
        with self._lock:
            self._unread[conversation_id] = self._unread.get(conversation_id, 0) + 1
        self._emit(StoreEvent(READ, conversation_id))

    def set_unread(self, conversation_id: str, count: int) -> None:
        """直接设置未读数（演示数据的"开局状态"用；正常路径请用 bump/mark_read）。"""
        with self._lock:
            self._unread[conversation_id] = max(0, int(count))
        self._emit(StoreEvent(READ, conversation_id))

    def set_read(self, conversation_id: str, message_id: str) -> None:
        """把发出去的消息标记为已读（气泡上的对勾变双勾），并通知界面刷新它。"""
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return
            target = None
            for msg in conv.messages:
                if msg.id == message_id:
                    msg.status = "read"
                    target = msg
                    break
        if target is not None:
            self._emit(StoreEvent(MESSAGE, conversation_id, target))

    # ==================================================================
    # 变更订阅
    # ==================================================================
    def subscribe(self, callback: Callable[[StoreEvent], None]) -> Callable[[], None]:
        """注册回调，返回取消订阅的函数。"""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, event: StoreEvent) -> None:
        # 在锁外回调：监听者可以安全地反过来读仓库，也不会把界面拖进锁里
        for callback in list(self._listeners):
            callback(event)
