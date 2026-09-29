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
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import (ME_ID, ChatKind, Conversation, MergedItem, Message, PinRef,
                     ReplyRef, format_clock)

__all__ = ["StoreEvent", "ChatStore", "confirmation_preview"]

MESSAGE = "message"
READ = "read"
RESET = "reset"
PIN = "pin"                  # 会话内置顶变了（气泡右键菜单里的"置顶"）

CONFIRM_PREVIEW = 24         # 确认弹窗里一条消息最多展示几个字


def confirmation_preview(messages: Iterable[Message], limit: int = CONFIRM_PREVIEW) -> str:
    """批量操作确认弹窗里用的多行摘要（纯函数，方便单测）。"""
    lines = []
    for message in messages:
        text = " ".join(message.preview_text.split()) or "（空消息）"
        lines.append(f"· {text[:limit]}{'…' if len(text) > limit else ''}")
    return "\n".join(lines)


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

    def message(self, message_id: str) -> Message | None:
        """按 id 找一条消息（打开附件、撤回、右键操作都要"先找到这条"）。"""
        with self._lock:
            for conversation in self._conversations.values():
                for message in conversation.messages:
                    if message.id == message_id:
                        return message
        return None

    def locate(self, message_id: str) -> tuple[Conversation | None, Message | None]:
        """一次拿到"这条消息 + 它在哪个会话"（回复/转发都要两者，别扫两遍）。"""
        with self._lock:
            return self.locate_locked(message_id)

    def me_id(self) -> str:
        """当前用户的 id（从会话成员里认，认不出来就用模型里的常量）。"""
        with self._lock:
            for conversation in self._conversations.values():
                for member in conversation.members:
                    if member.is_me:
                        return member.id
        return ME_ID

    def author_name(self, message: Message) -> str:
        """这条消息在引用块/合并转发里该显示成谁发的（自己发的一律是"我"）。"""
        if message.outgoing or message.sender_id == self.me_id():
            return "我"
        conversation = self._conversations.get(message.conversation_id)
        if conversation is not None:
            return conversation.sender_name(message)
        return "对方"

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

    # ==================================================================
    # 回复 / 置顶 / 转发 / 批量删除（气泡右键菜单那一套）
    # ==================================================================
    def reply_ref(self, message: Message, *, limit: int = 42) -> ReplyRef:
        """给\"回复\"造一个引用快照：作者 + 一行摘要（太长就截断）。"""
        text = " ".join(message.preview_text.split()) or "（空消息）"
        if len(text) > limit:
            text = text[:limit - 1] + "…"
        return ReplyRef(message.id, self.author_name(message), text)

    def pin_message(self, message_id: str, *, for_both: bool = False) -> bool:
        """置顶一条：可选**对方也置顶**（``for_both=True``），默认只在自己这边。

        同一条再点一次（同一个可见范围）= 取消它；换了可见范围 = 就地改这一条的设置，
        别的置顶不动。新置顶的排最前，横幅先显示它。
        """
        conversation, message = self.locate(message_id)
        if conversation is None or message is None:
            return False
        for_both = bool(for_both)
        current = [pin for pin in conversation.pins if pin.message_id != message_id]
        existing = next((pin for pin in conversation.pins if pin.message_id == message_id),
                        None)
        if existing is None or existing.for_both != for_both:
            current.insert(0, PinRef(message_id, for_both))     # 新置顶 / 改可见范围
        with self._lock:
            conversation.pins = tuple(current)
        self._emit(StoreEvent(PIN, conversation.id))
        return True

    def unpin_message(self, message_id: str) -> bool:
        """取消某一条的置顶（横幅上的 ✕ 走这里）；它没被置顶就返回 False。"""
        conversation, message = self.locate(message_id)
        if conversation is None or message is None:
            return False
        if not any(pin.message_id == message_id for pin in conversation.pins):
            return False
        with self._lock:
            conversation.pins = tuple(pin for pin in conversation.pins
                                      if pin.message_id != message_id)
        self._emit(StoreEvent(PIN, conversation.id))
        return True

    def pin_ref(self, conversation_id: str, message_id: str) -> "PinRef | None":
        """这条消息在当前会话里的置顶信息（没置顶就是 None）。"""
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return None
        return next((pin for pin in conversation.pins if pin.message_id == message_id), None)

    def is_pinned(self, conversation_id: str, message_id: str) -> bool:
        return self.pin_ref(conversation_id, message_id) is not None

    def pinned_entries(self, conversation_id: str) -> list:
        """这个会话置顶的那几条：``[(消息, PinRef), ...]``（按置顶顺序，找不到的跳过）。"""
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return []
        entries = []
        for pin in conversation.pins:
            message = self.message(pin.message_id)
            if message is not None:
                entries.append((message, pin))
        return entries

    def forward(self, message_ids: Iterable[str], target_id: str, *,
                merge: bool = False, now: float | None = None) -> list[Message]:
        """把选中的消息转发到 ``target_id``：``merge=True`` 合成一条"合并转发"，
        否则一条一条地发（每条都带"转发自 X"）。

        转发出来的是**我发的新消息**（Telegram 也是这样：落在右边、不再触发未读），
        时间戳取"现在"；附件直接沿用原来的 :class:`Attachment`（只记路径，不复制文件）。
        """
        with self._lock:
            target = self._conversations.get(target_id)
            sources: list[tuple[Conversation, Message]] = []
            for message_id in message_ids:
                conversation, message = self.locate_locked(message_id)
                if message is not None:
                    sources.append((conversation, message))
            if target is None or not sources:
                return []
            stamp = time.time() if now is None else float(now)
            me = self.me_id()
            origin = sources[0][0].title
            if merge:
                items = tuple(
                    MergedItem(author=self.author_name(message), text=message.text,
                               attachment=(message.attachment.name
                                           if message.attachment is not None else ""),
                               clock=format_clock(message.ts))       # 原消息的时间一起带走
                    for _, message in sources)
                created = [Message(id=uuid.uuid4().hex, conversation_id=target_id,
                                   sender_id=me, text="", ts=stamp, outgoing=True,
                                   forwarded_from=origin, merged_items=items)]
            else:
                created = []
                for index, (conversation, message) in enumerate(sources):
                    created.append(Message(
                        id=uuid.uuid4().hex, conversation_id=target_id, sender_id=me,
                        text=message.text, ts=stamp + index * 0.001, outgoing=True,
                        forwarded_from=conversation.title, attachment=message.attachment))
        for message in created:          # 在锁外 append，让订阅者正常收到事件
            self.append(message)
        return created

    def delete_messages(self, message_ids: Iterable[str], *, only_mine: bool = False) -> int:
        """批量删除，返回真删掉几条。

        这是**本地删除、不是撤回**：谁发的都能删（``only_mine`` 默认 False），
        删的只是本机这份记录，对方设备上仍然看得到。置顶的那条要是被删了，
        顺手把置顶也清掉，否则下次进来会指一条不存在的消息。
        """
        wanted = {message_id for message_id in message_ids}
        removed = 0
        touched: list[str] = []
        unpinned: list[str] = []
        with self._lock:
            for conversation in self._conversations.values():
                hits = [m for m in conversation.messages
                        if m.id in wanted and (not only_mine or m.outgoing)]
                if not hits:
                    continue
                keep = [m for m in conversation.messages if m not in hits]
                conversation.messages[:] = keep
                removed += len(hits)
                touched.append(conversation.id)
                removed_ids = {m.id for m in hits}
                if any(pin.message_id in removed_ids for pin in conversation.pins):
                    conversation.pins = tuple(pin for pin in conversation.pins
                                              if pin.message_id not in removed_ids)
                    unpinned.append(conversation.id)
        for conversation_id in touched:
            self._emit(StoreEvent(RESET, conversation_id))
        for conversation_id in unpinned:
            self._emit(StoreEvent(PIN, conversation_id))
        return removed

    def locate_locked(self, message_id: str) -> tuple[Conversation | None, Message | None]:
        """:meth:`locate` 的内部版本：**调用方必须已经持锁**（避免重入加锁）。"""
        for conversation in self._conversations.values():
            for message in conversation.messages:
                if message.id == message_id:
                    return conversation, message
        return None, None

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
