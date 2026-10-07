"""聊天领域模型。

这一层只有数据与纯函数，不 import 任何 Qt —— 因此整套业务规则都能在
没有图形界面的环境里跑单测，也让未来的网络层可以直接复用。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

__all__ = [
    "ChatKind", "User", "Message", "Conversation", "Attachment", "ME_ID",
    "ReplyRef", "MergedItem", "PinRef", "Friend", "FriendRequest",
    "reply_to_json", "reply_from_json",
    "pins_to_json", "pins_from_json",
    "merged_to_json", "merged_from_json",
    "IMAGE_SUFFIXES", "human_size",
    "format_clock", "format_day_label", "format_list_time",
    "same_day", "day_label_key",
    "strip_partial_flag", "ellipsize",
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
# 附件（图片 / 文件）
# ---------------------------------------------------------------------------
# 图片后缀：用来决定"气泡里画缩略图"还是"画文件卡片"。这里按后缀判断，不读文件内容 ——
# 数据层不导入 Qt，也不想为了判断类型去解码整张图。
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp",
                            ".webp", ".tif", ".tiff", ".ico"})


def strip_partial_flag(text: str) -> str:
    """去掉结尾**落单的半个旗帜**（按字符/像素截断时的收尾处理）。

    为什么需要它：旗帜是**两个** regional indicator 拼起来的（🇯🇵 = J + P），
    英格兰/海盗旗那种还带零宽连接符（ZWJ）。按字符数截断、或按像素宽度省略号截断，
    都可能正好切在中间 —— 剩下那一半在字体里会被画成一个孤立的字母方块（看着像乱码）。

    这里只是**纯文本层面**的收尾（把没配成对的尾巴去掉），和"旗子要不要画成图片"
    无关 —— 那是界面层 ``ui/flag_text.py`` 的事。
    """
    while text and text[-1] == "\u200d":            # 尾巴上挂着的零宽连接符
        text = text[:-1]
    run = 0                                         # 结尾这一串 regional indicator 有多长
    while run < len(text) and 0x1F1E6 <= ord(text[-1 - run]) <= 0x1F1FF:
        run += 1
    if run % 2:                                     # 两面旗是成对的：奇数说明落了半面
        text = text[:-1]
    return text


def ellipsize(text: str, limit: int, *, ellipsis: str = "…") -> str:
    """按字符数截断（``limit`` 是含省略号的总长），且**不把一面旗帜切成两半**。

    摘要、回复引用、聊天记录卡片都是"取前面几十个字"，直接切片很容易切出半面旗，
    统一走这里（会话列表按像素省略号截断时也用它收尾，见 ``strip_partial_flag``）。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return strip_partial_flag(text[:max(0, limit - len(ellipsis))]) + ellipsis


def human_size(size: int) -> str:
    """字节数 -> ``1.2 MB`` / ``234 KB`` / ``512 B``（文件卡片上显示的那个）。"""
    try:
        value = float(max(0, int(size)))
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            text = f"{value:.0f}" if unit == "B" else f"{value:.1f}"
            return f"{text.replace('.0', '')} {unit}"
        value /= 1024
    return f"{value:.0f} GB"          # pragma: no cover - 循环里已经 return


@dataclass(frozen=True)
class Attachment:
    """消息里的附件。

    联网之后一个附件有**两种状态**，靠 ``path`` 区分：

    * 已经在本地磁盘上 —— ``path`` 是真实路径，点开就用系统程序打开；
    * 还在服务器上、没下回来 —— ``path`` 是 ``remote://<附件id>`` 这个**标记**
      （见 :attr:`is_remote`）。气泡照样显示名字和大小，"点开"时再按 id 去下载。

    为什么用"标记路径"而不是加一个字段：数据层（库、传输层、气泡）全都只认
    ``path/name/size`` 这三样，加标记就不用动数据库结构，也不用动任何一层的形状 ——
    "下载到本地"这一步做完以后，附件就和"本机选的图片"完全一样了。

    ``name`` 和 ``size`` 都可以从路径推出来，但显式存下来更稳：文件被移走/改名以后，
    气泡上的文件卡片还能显示原来的名字和大小（不会变成"未知"）。
    """

    path: str
    name: str = ""
    size: int = 0
    #: 我发出去的附件在**服务器上的 id**（发成功后记下来）。本地那份被删掉以后，
    #: 消息不该只说"不在本地了" —— 服务器上还有，能按这个 id 一键取回。
    server_id: str = ""
    REMOTE_SCHEME = "remote://"          # 远程附件在 ``path`` 里的标记前缀

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", os.path.basename(self.path) or "附件")
        if not self.size:
            try:                       # 文件不在就当大小未知（不是错误，别让它抛）
                object.__setattr__(self, "size", os.path.getsize(self.path))
            except OSError:
                pass

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_path(cls, path: str) -> "Attachment":
        """从本地路径造一个附件（绝对路径：存进库里的路径要经得起换 cwd）。"""
        return cls(path=os.path.abspath(str(path)))

    @classmethod
    def remote(cls, file_id: str, name: str = "", size: int = 0) -> "Attachment":
        """造一个"还在服务器上"的附件（下载完请用 :meth:`from_path` 换掉它）。"""
        return cls(path=cls.REMOTE_SCHEME + str(file_id), name=name, size=size)

    # ---------------------------------------------------------------- 查询
    @property
    def is_remote(self) -> bool:
        """是不是"还没下载到本地"（气泡点开时要先去服务端拿）。"""
        return self.path.startswith(self.REMOTE_SCHEME)

    @property
    def remote_id(self) -> str:
        """服务器上的附件 id；本地附件就是空串。"""
        return self.path[len(self.REMOTE_SCHEME):] if self.is_remote else ""

    @property
    def source_id(self) -> str:
        """服务器上那份附件叫什么 id（两种情形：还没下载的看路径标记，自己发过的看
        :attr:`server_id`）。是"去服务器取字节"唯一该用的 id。"""
        return self.remote_id or self.server_id

    @property
    def recoverable(self) -> bool:
        """本地这份没了，但**服务器上还有一份**（能取回来）。

        典型场景：自己发出去的图，事后把本地文件删了 —— 服务器上那份是这条消息引用的
        附件，一直在；界面不该说"图片不在本地了"就完事，而该说"点一下取回"。
        """
        return bool(self.server_id) and not self.is_remote and not self.exists

    def localized_at(self, path: str) -> "Attachment":
        """下载/取回之后的新形态：换成真实路径，但**继续带着服务器那份 id**
        （这样以后再删掉本地文件，还是能一键取回）。"""
        return Attachment(path=os.path.abspath(str(path)), name=self.name,
                          size=self.size, server_id=self.source_id)

    @property
    def suffix(self) -> str:
        """``.pdf`` / ``.png``（全是小写，没有后缀就是空串）。"""
        return os.path.splitext(self.name)[1].lower()

    @property
    def ext(self) -> str:
        """``PDF`` / ``PNG``（文件卡片上那一行用）。"""
        return self.suffix.lstrip(".").upper()

    @property
    def is_image(self) -> bool:
        return self.suffix in IMAGE_SUFFIXES

    @property
    def kind(self) -> str:
        """``image`` 或 ``file`` —— 界面据此决定画缩略图还是文件卡片。"""
        return "image" if self.is_image else "file"

    @property
    def label(self) -> str:
        """没有配文时消息的摘要（会话列表 / 通知里用）。"""
        return "📷 照片" if self.is_image else f"📎 {self.name}"

    @property
    def size_text(self) -> str:
        """``1.2 MB``；大小未知时是空串（别显示 "0 B" 骗人）。"""
        return human_size(self.size) if self.size > 0 else ""

    @property
    def exists(self) -> bool:
        """文件还在不在本地（被删/被移走时界面要能优雅降级）。

        远程附件**恒为 False**：界面会走"点一下去下载"的分支，而不是"打开文件"。
        """
        return bool(self.path) and not self.is_remote and os.path.isfile(self.path)

    # ---------------------------------------------------------------- 序列化
    def to_json(self) -> dict:
        return {"path": self.path, "name": self.name, "size": self.size,
                "server_id": self.server_id}

    @classmethod
    def from_json(cls, data) -> "Attachment | None":
        """从落库的字典还原；字段乱七八糟时返回 ``None``（别让坏数据炸开界面）。"""
        if not isinstance(data, dict):
            return None
        path = str(data.get("path") or "")
        if not path:
            return None
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(path=path, name=str(data.get("name") or ""), size=size,
                   server_id=str(data.get("server_id") or ""))

    def dump(self) -> str:
        """给 SQLite 用的 JSON 字符串（中文名不转义，库文件里看着就是原名）。"""
        return json.dumps(self.to_json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReplyRef:
    """被引用（回复）的那条消息的快照。

    为什么连作者和摘要一起存，而不是只存一个 message_id 去查：被引用的那条可能被
    撤回、或者整个会话被清空，那时候气泡上的引用块还得照常显示 —— 快照存的是
    "我当时回的是哪句话"。
    """

    message_id: str
    author: str
    preview: str

    def to_json(self) -> dict:
        return {"id": self.message_id, "author": self.author, "preview": self.preview}

    @classmethod
    def from_json(cls, data) -> "ReplyRef | None":
        """坏数据降级成 None（老库、手改过的库都不该把界面搞崩）。"""
        if not isinstance(data, dict):
            return None
        message_id = str(data.get("id", "")).strip()
        if not message_id:
            return None
        return cls(message_id, str(data.get("author", "")), str(data.get("preview", "")))

    @property
    def text(self) -> str:
        """一行字：``作者：摘要``（群聊里一眼能看出回的是谁）。"""
        return f"{self.author}：{self.preview}" if self.author else self.preview


def reply_to_json(reply: "ReplyRef | None") -> str | None:
    """存库用：没有引用就存 NULL（而不是空 JSON，便于用 IS NULL 判断）。"""
    return json.dumps(reply.to_json(), ensure_ascii=False) if reply is not None else None


def reply_from_json(data) -> "ReplyRef | None":
    if data in (None, "", b""):
        return None
    try:
        return ReplyRef.from_json(json.loads(data))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PinRef:
    """一条会话内置顶：**哪条消息** + **是不是"对方也置顶"**（Telegram 的两个选项）。

    ``for_both=False`` 就是"仅为我置顶"：只有我这边看得到这条置顶横幅，对方那边什么都没有。
    """

    message_id: str
    for_both: bool = False

    def to_json(self) -> dict:
        return {"id": self.message_id, "both": self.for_both}

    @classmethod
    def from_json(cls, data) -> "PinRef | None":
        """兼容三种存法：``{"id":..,"both":..}`` / 纯 id 字符串 / 直接一个 id。"""
        if isinstance(data, dict):
            message_id = str(data.get("id", "")).strip()
            if not message_id:
                return None
            return cls(message_id, bool(data.get("both", False)))
        if isinstance(data, str) and data.strip():
            return cls(data.strip(), False)
        return None

    @property
    def label(self) -> str:
        """界面上那一小行说明（横幅上会显示）。"""
        return "双方可见" if self.for_both else "仅我可见"


def pins_to_json(pins: Iterable["PinRef"]) -> str:
    pins = tuple(pins)
    if not pins:
        return ""
    return json.dumps([pin.to_json() for pin in pins], ensure_ascii=False)


def pins_from_json(data) -> tuple["PinRef", ...]:
    """坏数据降级成空元组；老库里的"单个 id / 纯 id 数组"也认。"""
    if not data:
        return ()
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ()
        if not text.startswith("["):
            return (PinRef(text, False),)          # v3 第一版：就存了一个 id
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError):
            return ()
    else:
        loaded = data
    if not isinstance(loaded, (list, tuple)):
        return ()
    found = (PinRef.from_json(entry) for entry in loaded)
    return tuple(pin for pin in found if pin is not None)


@dataclass(frozen=True)
class MergedItem:
    """"合并转发"里的一条：作者 + 内容（正文或附件名）+ 时间。

    ``clock`` 是转发时把**原消息的时间**抄下来的（"14:05"）：查看窗口按 QQ 的样子
    逐条列出来，每条配个时间才像聊天记录。老数据没有这个字段就留空。
    """

    author: str
    text: str
    attachment: str = ""
    clock: str = ""

    def to_json(self) -> dict:
        return {"author": self.author, "text": self.text,
                "attachment": self.attachment, "clock": self.clock}

    @classmethod
    def from_json(cls, data) -> "MergedItem | None":
        if not isinstance(data, dict):
            return None
        return cls(str(data.get("author", "")), str(data.get("text", "")),
                   str(data.get("attachment", "")), str(data.get("clock", "")))

    @property
    def preview(self) -> str:
        """这一条在摘要里长什么样。"""
        if self.text.strip():
            return self.text
        return f"📎 {self.attachment}" if self.attachment else "（空消息）"


def merged_to_json(items: Iterable["MergedItem"]) -> str:
    items = tuple(items)
    if not items:
        return ""
    return json.dumps([item.to_json() for item in items], ensure_ascii=False)


def merged_from_json(data) -> tuple["MergedItem", ...]:
    """坏数据降级成空元组：宁可当成一条普通消息，也不要让界面炸掉。"""
    if not data:
        return ()
    try:
        loaded = json.loads(data)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(item for item in (MergedItem.from_json(entry) for entry in loaded)
                 if item is not None)


@dataclass(frozen=True)
class User:
    """参与者。单机演示里没有账号体系，``id`` 就是稳定标识。"""

    id: str
    name: str
    online: bool = False
    is_bot: bool = False
    # 登录用的 @handle。协议里的 User 一直带着它（username），但客户端模型过去只留了
    # display_name —— 结果好友窗口拿它去调"同意/删除"时直接 AttributeError。
    # 放在最后并且有默认值，是为了不动任何按位置构造 User(...) 的老代码。
    username: str = ""

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


@dataclass(frozen=True)
class Friend:
    """一个好友：``user`` 是对方，``since`` 是**服务端给的**成为好友的时间。

    ``since`` 一律用服务端下发的值，客户端不自己造时间 —— 两台设备上看到的
    "什么时候成为好友的"才一致。
    """

    user: User
    since: float = 0.0

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def name(self) -> str:
        return self.user.name


@dataclass(frozen=True)
class FriendRequest:
    """一条待处理的好友申请。

    ``incoming``（别人申请加我）里 ``user`` 是**申请人**；``outgoing``（我申请加别人）
    里 ``user`` 是**被申请人**。``note`` 是申请留言 —— 被好友门禁拦下来的申请，
    留言就是那条发不出去的消息正文。
    """

    user: User
    note: str = ""
    ts: float = 0.0

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def name(self) -> str:
        return self.user.name


@dataclass
@dataclass(frozen=True)
class Reaction:
    """一条消息上的一个回应（表情）聚合。

    ``count`` 是**服务端算的**总数，``mine`` 是"看的人有没有点"——
    客户端不自己加一减一，两个人同时点才不会被算错。
    """

    emoji: str
    count: int = 1
    mine: bool = False

    def __str__(self) -> str:
        return f"{self.emoji}{self.count}"


@dataclass
class Message:
    """一条消息。

    ``outgoing`` 表示由"我"发出（决定气泡靠哪边、什么颜色）；``attachment`` 是随消息
    一起发出去的图片/文件（可以是纯附件、也可以带一段配文 ``text``）。
    """

    id: str
    conversation_id: str
    sender_id: str
    text: str
    ts: float
    outgoing: bool = False
    status: str = "sent"          # sent / read，供气泡上的对勾使用
    attachment: Attachment | None = None
    reply_to: "ReplyRef | None" = None            # 回复：被引用那条的快照
    forwarded_from: str = ""                      # 转发自哪个会话（空 = 不是转发）
    merged_items: tuple[MergedItem, ...] = ()     # 合并转发的内容（非空 = 这是一条合并转发）
    reactions: tuple[Reaction, ...] = ()          # 回应（表情）：服务端算的计数 + 我点过的

    @property
    def time_text(self) -> str:
        return format_clock(self.ts)

    @property
    def is_read(self) -> bool:
        return self.status == "read"

    @property
    def blocked(self) -> bool:
        """这条是不是"撞上好友门禁、没发出去"的那条。

        服务端收到非好友的单聊消息**不落库**，而是把它变成一条好友申请；本地那条
        乐观显示的消息会停在 ``status="blocked"``，界面上标成"已发送好友申请"，
        而不是一直挂着"✓"（看着像发出去了）。
        """
        return self.status == "blocked"

    @property
    def is_reply(self) -> bool:
        return self.reply_to is not None

    def reaction_counts(self) -> dict:
        """``{表情: 数量}``（气泡直接要这个形状）。"""
        return {item.emoji: item.count for item in self.reactions}

    def my_reactions(self) -> set:
        return {item.emoji for item in self.reactions if item.mine}

    @property
    def send_text(self) -> str:
        """真正"发出去"的正文。

        合并转发自己没有 ``text``（内容在 ``merged_items`` 里），而传输层是拿 ``text``
        判"这是不是空消息"的 —— 空消息既不触发对方"正在输入…"，也不给已读回执。
        所以这里给它一句摘要当载荷，免得转发出去的东西在对面看来"什么都没送到"
        （纯附件消息仍然返回空串：那种情况由 ``attachment`` 证明它不是空的）。
        """
        if self.text.strip():
            return self.text
        if self.is_merged:
            return f"【合并转发】{self.merged_count} 条消息"
        return ""

    @property
    def is_forwarded(self) -> bool:
        return bool(self.forwarded_from) and not self.merged_items

    @property
    def is_merged(self) -> bool:
        """"合并转发"：一条气泡里装着好几条消息。"""
        return bool(self.merged_items)

    @property
    def merged_count(self) -> int:
        return len(self.merged_items)

    def merged_line(self, index: int) -> str:
        """"作者：内容" —— 查看窗口里的一行。"""
        item = self.merged_items[index]
        return f"{item.author}：{item.preview}" if item.author else item.preview

    def merged_preview(self, limit: int = 32) -> str:
        """卡片上那一行摘要：拿第一条的"作者：内容"（QQ 的聊天记录卡片就是这样）。

        只给一行 —— 内容都塞进卡片会又长又难读，想看全部点开查看窗口。
        """
        if not self.merged_items:
            return ""
        text = " ".join(self.merged_line(0).split())
        return ellipsize(text, limit)

    @property
    def kind(self) -> str:
        """``text`` / ``image`` / ``file`` —— 界面按这个选气泡内容。"""
        if self.attachment is None:
            return "text"
        return self.attachment.kind

    @property
    def has_attachment(self) -> bool:
        return self.attachment is not None

    @property
    def preview_text(self) -> str:
        """列表摘要：有配文就用配文，纯附件就用"📷 照片 / 📎 文件名"。"""
        if self.is_merged:
            return f"【合并转发】{self.merged_count} 条消息"
        if self.text.strip():
            return self.text
        return self.attachment.label if self.attachment is not None else ""


@dataclass
class Conversation:
    """一个会话（单聊或群聊）。"""

    id: str
    title: str
    kind: ChatKind = ChatKind.DIRECT
    members: list[User] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    pinned: bool = False
    muted: bool = False          # 免打扰：未读不计数、徽标变灰（侧栏右键菜单里切）
    # 会话内的置顶（**可以多条**）。排在最前的是最近置顶的：横幅先显示它，用 ‹ › 翻。
    # 每条是个 PinRef：带"仅我可见 / 双方可见"这个标记。
    pins: tuple[PinRef, ...] = ()

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

    @property
    def peer_name(self) -> str:
        """单聊里"对方"的名字（群聊没有具体的某个人，返回空串）。

        置顶小窗上那句「也为 XX 置顶」的 XX 就是它；群聊里换成"所有群成员"。
        """
        for member in self.members:
            if not member.is_me:
                return member.name
        return ""

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
        """会话列表里的最后一条摘要；群聊带发送者前缀（Telegram 的做法）。

        纯附件消息（没有配文）在这里显示成"📷 照片 / 📎 文件名"，所以取的是
        :attr:`Message.preview_text` 而不是裸的 ``text``。
        """
        last = self.last_message
        if last is None:
            return "还没有消息"
        text = " ".join(last.preview_text.split())
        if self.is_group and not last.outgoing:
            text = f"{self.sender_name(last)}: {text}"
        elif last.outgoing:
            text = f"我: {text}"
        return ellipsize(text, 42)

    def preview_time(self, now: float | None = None) -> str:
        last = self.last_message
        return format_list_time(last.ts, now) if last else ""
