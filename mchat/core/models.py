"""聊天领域模型。

这一层只有数据与纯函数，不 import 任何 Qt —— 因此整套业务规则都能在
没有图形界面的环境里跑单测，也让未来的网络层可以直接复用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "ChatKind", "User", "Message", "Conversation", "ME_ID",
    "format_clock", "format_day_label", "format_list_time",
    "same_day", "day_label_key",
]

ME_ID = "me"          # 当前用户的固定 id（单机演示没有账号体系）


class ChatKind(str, Enum):
    """会话类型。"""

    DIRECT = "direct"      # 单聊
    GROUP = "group"        # 群聊


# ---------------------------------------------------------------------------
# 时间文案（聊天界面里到处都是，抽成纯函数避免各控件各写一份）
# ---------------------------------------------------------------------------
def format_clock(ts: float) -> str:
    """时间戳 -> ``14:05``"""
    return time.strftime("%H:%M", time.localtime(ts))


def same_day(a: float, b: float) -> bool:
    """两个时间戳是否落在同一个本地自然日。"""
    return time.strftime("%Y%m%d", time.localtime(a)) == time.strftime(
        "%Y%m%d", time.localtime(b))


def day_label_key(ts: float) -> str:
    """自然日分组键（用于消息列表里判断要不要插日期分隔条）。"""
    return time.strftime("%Y%m%d", time.localtime(ts))


def format_day_label(ts: float, now: float | None = None) -> str:
    """日期分隔条上的文字：今天 / 昨天 / 8月30日 / 2025年8月30日。"""
    now = time.time() if now is None else now
    if same_day(ts, now):
        return "今天"
    if same_day(ts, now - 86400):
        return "昨天"
    text = time.strftime("%m月%d日", time.localtime(ts))
    if time.localtime(ts).tm_year != time.localtime(now).tm_year:
        text = f"{time.localtime(ts).tm_year}年{text}"
    return text


def format_list_time(ts: float, now: float | None = None) -> str:
    """会话列表右侧的时间：今天显示时刻，昨天显示"昨天"，更早显示日期。"""
    now = time.time() if now is None else now
    if same_day(ts, now):
        return format_clock(ts)
    if same_day(ts, now - 86400):
        return "昨天"
    return time.strftime("%m/%d", time.localtime(ts))


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class User:
    """参与者。单机演示里没有账号体系，``id`` 就是稳定标识。"""

    id: str
    name: str
    online: bool = False
    is_bot: bool = False

    @property
    def initial(self) -> str:
        """头像上的首字：中文取首字，英文取首字母大写。"""
        name = self.name.strip()
        return name[:1].upper() if name else "?"

    @property
    def status_text(self) -> str:
        if self.is_bot:
            return "机器人"
        return "在线" if self.online else "离线"

    @property
    def is_me(self) -> bool:
        """是不是"我自己"（决定消息靠右、不发未读）。"""
        return self.id == ME_ID


@dataclass
class Message:
    """一条消息。``outgoing`` 表示由"我"发出（决定气泡靠哪边、什么颜色）。"""

    id: str
    conversation_id: str
    sender_id: str
    text: str
    ts: float
    outgoing: bool = False
    status: str = "sent"          # sent / read，供气泡上的对勾使用

    @property
    def time_text(self) -> str:
        return format_clock(self.ts)

    @property
    def is_read(self) -> bool:
        return self.status == "read"


@dataclass
class Conversation:
    """一个会话（单聊或群聊）。"""

    id: str
    title: str
    kind: ChatKind = ChatKind.DIRECT
    members: list[User] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    pinned: bool = False

    # ---------------------------------------------------------------- 查询
    @property
    def last_message(self) -> Message | None:
        return self.messages[-1] if self.messages else None

    @property
    def last_ts(self) -> float:
        last = self.last_message
        return last.ts if last else 0.0

    def member(self, user_id: str) -> User | None:
        for user in self.members:
            if user.id == user_id:
                return user
        return None

    def sender_name(self, message: Message) -> str:
        user = self.member(message.sender_id)
        return user.name if user else "未知用户"

    @property
    def initial(self) -> str:
        return self.title.strip()[:1].upper() if self.title.strip() else "?"

    @property
    def is_group(self) -> bool:
        return self.kind is ChatKind.GROUP

    def subtitle(self, typing: bool = False) -> str:
        """聊天头部第二行：优先显示"正在输入…"，其次是在线状态/成员数。"""
        if typing:
            return "正在输入…"
        if self.is_group:
            return f"{len(self.members)} 位成员"
        others = [m for m in self.members if not m.is_me]
        if others:
            return others[0].status_text
        return "私聊"

    # ---------------------------------------------------------- 列表预览文案
    def preview_text(self) -> str:
        """会话列表里的最后一条摘要；群聊带发送者前缀（Telegram 的做法）。"""
        last = self.last_message
        if last is None:
            return "还没有消息"
        text = " ".join(last.text.split())
        if self.is_group and not last.outgoing:
            text = f"{self.sender_name(last)}: {text}"
        elif last.outgoing:
            text = f"我: {text}"
        return text if len(text) <= 42 else text[:41] + "…"

    def preview_time(self, now: float | None = None) -> str:
        last = self.last_message
        return format_list_time(last.ts, now) if last else ""
