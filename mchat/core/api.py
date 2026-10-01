"""服务端 HTTP 客户端 + 线上 JSON → 领域模型的转换。

这一层是**唯一知道协议细节**的地方（协议见 ``server/PROTOCOL.md``）：
界面与传输层都只跟本模块返回的模型/字典打交道，所以协议变了只改这里。

* 只用标准库 ``urllib.request``：客户端不必因为"要联网"就多背一个 HTTP 库；
* 全部方法是**同步阻塞**的，调用方（传输层的后台线程 / 界面里的异步包装）自己决定
  在哪个线程里跑 —— 界面线程绝不要直接调（长轮询会挂住界面）；
* 异常只有两种：:class:`ApiError`（服务端明确回了个错误码）和
  :class:`NetworkError`（压根没连上/超时/返回不是 JSON）。界面据此说不同的话：
  "密码不对" 和 "连不上服务器" 是两件事。

本模块零 Qt 依赖，可脱离界面单测（``tests/test_server.py`` 就是这么做的：
真起一个服务端，用这里的客户端跑完整流程）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .models import (Attachment, ChatKind, Conversation, Friend, FriendRequest,
                     ME_ID, MergedItem, Message, PinRef, Reaction, ReplyRef, User)
from .atrest import active_cipher, is_encrypted_file
from .paths import is_inside, safe_local_name

__all__ = ["ApiError", "NetworkError", "ApiClient", "SendResult", "user_from_json",
           "conversation_from_json", "message_from_json", "users_from_json",
           "conversations_from_json", "messages_from_json", "pins_from_json",
           "friends_from_json", "friend_requests_from_json"]

DEFAULT_TIMEOUT = 15.0
SYNC_TIMEOUT_SLACK = 10.0        # 长轮询的 HTTP 超时 = 服务端等待时间 + 这点余量
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
REMOTE_SCHEME = Attachment.REMOTE_SCHEME     # 附件"还没下载到本地"时路径用的是它


class ApiError(Exception):
    """服务端**明确**回了错误（HTTP 4xx/5xx + ``{"error": {...}}``）。"""

    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code
        self.status = status

    @property
    def friendly(self) -> str:
        """人话（界面上直接显示这句）：错误码 → 一句用户看得懂的话。"""
        return {
            "bad_credentials": "用户名或密码不对",
            "unauthorized": "登录已失效，请重新登录",
            "username_taken": "这个用户名已经被注册了",
            "invalid_username": "用户名只能是 3–24 位的字母、数字、下划线、点或减号",
            "weak_password": "密码至少 6 位",
            "not_found": "找不到这个人或这个会话",
            "not_member": "你不在这个会话里",
            "too_large": "文件太大，服务器不收",
            "duplicate_id": "消息 id 冲突，请重发",
            "invalid_request": "请求不合法（可能是客户端版本和服务端对不上）",
            "invalid_invite": "邀请码不对（问一下服务器管理员）",
            "registration_disabled": "这台服务器已关闭注册",
            "too_many_requests": "试得太频繁了，等一下再试",
            "message_too_long": "这条消息太长了",
            "quota_exceeded": "你的附件空间满了（服务器配额）",
        }.get(self.code, self.message)

    @property
    def is_auth(self) -> bool:
        """是不是"登录态不认了"（缓存失效，要回登录界面）。"""
        return self.code == "unauthorized" or self.status == 401


class NetworkError(Exception):
    """连不上 / 超时 / 返回的不是 JSON —— 网络层面的问题，不代表账号有问题。"""

    def __init__(self, message: str = "", *, status: int = 0, url: str = ""):
        super().__init__(message or "网络不可用")
        self.message = message or "网络不可用"
        self.status = status
        self.url = url

    @property
    def friendly(self) -> str:
        return f"连不上服务器：{self.message}"


class SendResult(dict):
    """``send_message()`` 的返回值：**既是那条消息，也可能是一次被拦下的好友申请**。

    为什么是 ``dict`` 子类而不是一个新对象：调用方（传输层 / 测试 / 界面）一直把
    ``send_message()`` 的返回值当消息字典用（``sent["text"]`` / ``sent["id"]``，
    连裸 HTTP 的测试也是这个形状），换掉会把所有老调用点一起打碎。这里只在字典上多挂
    一个 ``friend_request`` 属性：

    * 正常投递 —— 它就是那条消息的字典，``friend_request`` 是 None；
    * 被好友门禁拦下 —— 服务端**没有落库这条消息**（它变成了对方的一条好友申请），
      所以字典是空的，但 ``friend_request`` 非空，调用方据此把本地那条乐观显示的消息
      标成"已发送好友申请"，而**不能**把同一条再重发一遍。

    两种结果都是协议里**正常**的结果，不抛 :class:`ApiError`。
    """

    def __init__(self, message=None, friend_request=None):
        super().__init__(dict(message or {}))
        self.friend_request = (dict(friend_request)
                               if isinstance(friend_request, dict) else None)

    @property
    def intercepted(self) -> bool:
        """这条消息是不是被好友门禁拦下、变成了好友申请。"""
        return self.friend_request is not None


# ---------------------------------------------------------------------------
# 线上 JSON -> 模型
# ---------------------------------------------------------------------------
def user_from_json(data, *, me_id: str = "") -> User:
    """``{"id","username","display_name","online"}`` -> :class:`User`。

    ``me_id`` 是"我自己的服务端 id"：命中的那一个会被换成 :data:`ME_ID`，
    这样界面里那套 ``member.is_me`` / ``peer_name``（"对方"是谁）不用改一行代码。
    """
    if not isinstance(data, dict):
        return User(ME_ID, "未知用户")
    raw_id = str(data.get("id") or "")
    is_me = bool(me_id) and raw_id == me_id
    name = str(data.get("display_name") or data.get("username") or "未知用户")
    return User(ME_ID if is_me else raw_id, name, online=bool(data.get("online")),
                username=str(data.get("username") or ""))


def users_from_json(items, *, me_id: str = "") -> list[User]:
    return [user_from_json(item, me_id=me_id) for item in (items or [])]


def attachment_from_json(data) -> Attachment | None:
    """服务端附件 -> 本地附件对象。

    还没有把文件下回来时，路径写成 ``remote://<id>`` 这个**标记**：
    界面照常显示名字和大小，"点开"的时候再按这个 id 去下载（见 ``Attachment.is_remote``）。
    """
    if not isinstance(data, dict):
        return None
    file_id = str(data.get("id") or "")
    if not file_id:
        return None
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return Attachment(path=REMOTE_SCHEME + file_id,
                      name=str(data.get("name") or file_id), size=size)


def reply_from_json(data) -> ReplyRef | None:
    if not isinstance(data, dict):
        return None
    message_id = str(data.get("id") or "")
    if not message_id:
        return None
    return ReplyRef(message_id, str(data.get("author") or ""),
                    str(data.get("preview") or ""))


def merged_from_json(items) -> tuple[MergedItem, ...]:
    found = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        found.append(MergedItem(str(item.get("author") or ""), str(item.get("text") or ""),
                                str(item.get("attachment") or ""), str(item.get("clock") or "")))
    return tuple(found)


def message_from_json(data, *, me_id: str = "", outgoing: bool | None = None) -> Message | None:
    """一条线上消息 -> :class:`Message`（坏数据返回 None，别炸界面）。

    ``outgoing`` 不给就按 ``sender_id == me_id`` 推断 —— 所以"同一账号的另一个设备
    发出来的消息"在这边也正确地落在右边。
    """
    if not isinstance(data, dict):
        return None
    message_id = str(data.get("id") or "")
    conversation_id = str(data.get("conversation_id") or "")
    if not message_id or not conversation_id:
        return None
    sender_id = str(data.get("sender_id") or "")
    mine = (sender_id == me_id) if outgoing is None else bool(outgoing)
    try:
        ts = float(data.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return Message(
        id=message_id,
        conversation_id=conversation_id,
        sender_id=ME_ID if mine else sender_id,
        text=str(data.get("text") or ""),
        ts=ts,
        outgoing=mine,
        status=str(data.get("status") or "sent"),
        attachment=attachment_from_json(data.get("attachment")),
        reply_to=reply_from_json(data.get("reply_to")),
        forwarded_from=str(data.get("forwarded_from") or ""),
        merged_items=merged_from_json(data.get("merged")),
        reactions=reactions_from_json(data.get("reactions")),
    )


def messages_from_json(items, *, me_id: str = "") -> list[Message]:
    found = [message_from_json(item, me_id=me_id) for item in (items or [])]
    return [message for message in found if message is not None]


def reactions_from_json(items) -> tuple:
    """线上的 ``reactions`` -> ``(Reaction, ...)``（计数与"我点过"都由服务端给）。"""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        emoji = str(item.get("emoji") or "")
        if not emoji:
            continue
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        out.append(Reaction(emoji, count, bool(item.get("mine"))))
    return tuple(out)


def pins_from_json(items, *, me_id: str = "") -> tuple[list, list]:
    """服务端的 ``pins`` -> ``(PinRef 列表, 快照消息列表)``。

    服务端只下发**双方可见**的置顶（"仅我可见"是本机自己的事），
    而 ``Conversation.pins`` 里带的 ``for_both`` 正好表示这件事。
    快照消息要单独返回：对方可能还没把那批历史拉到本地，没有快照就画不出置顶横幅。
    """
    refs: list = []
    snaps: list = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("message_id") or "")
        if not message_id:
            continue
        refs.append(PinRef(message_id, True))
        snapshot = message_from_json(item.get("message"), me_id=me_id)
        if snapshot is not None:
            snaps.append(snapshot)
    return refs, snaps


def conversation_from_json(data, *, me_id: str = "") -> Conversation | None:
    """一个线上会话 -> :class:`Conversation`。

    ``messages`` 里先放两样东西：最后一条（列表预览要用）和**被置顶那几条的快照**
    （横幅要用，哪怕那条消息在更早的历史里、本地还没有）。
    """
    if not isinstance(data, dict):
        return None
    conversation_id = str(data.get("id") or "")
    if not conversation_id:
        return None
    kind = ChatKind.GROUP if str(data.get("kind")) == "group" else ChatKind.DIRECT
    members = users_from_json(data.get("members"), me_id=me_id)
    conversation = Conversation(id=conversation_id, title=str(data.get("title") or "会话"),
                               kind=kind, members=members)
    snapshots: list = []
    last = message_from_json(data.get("last_message"), me_id=me_id)
    if last is not None:
        snapshots.append(last)
    refs, pinned_snaps = pins_from_json(data.get("pins"), me_id=me_id)
    conversation.pins = tuple(refs)
    known = {message.id for message in snapshots}
    snapshots.extend(snap for snap in pinned_snaps if snap.id not in known)
    conversation.messages = sorted(snapshots, key=lambda message: message.ts)
    return conversation


def conversations_from_json(items, *, me_id: str = "") -> list[Conversation]:
    found = [conversation_from_json(item, me_id=me_id) for item in (items or [])]
    return [conv for conv in found if conv is not None]


# ---------------------------------------------------------------------------
# 好友
# ---------------------------------------------------------------------------
def _friend_user(item, *, me_id: str) -> User | None:
    """好友/申请条目里的 ``user`` —— 缺失或不是人就返回 None（坏数据别炸界面）。"""
    if not isinstance(item, dict):
        return None
    user = item.get("user")
    if not isinstance(user, dict):
        return None
    return user_from_json(user, me_id=me_id)


def _friend_since(item) -> float:
    try:
        return float((item or {}).get("since") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def friends_from_json(items, *, me_id: str = "") -> list[Friend]:
    """``GET /api/friends`` 的 ``friends`` 数组 -> ``[Friend, ...]``。"""
    found = []
    for item in items or []:
        user = _friend_user(item, me_id=me_id)
        if user is None:
            continue
        found.append(Friend(user, _friend_since(item)))
    return found


def friend_requests_from_json(items, *, me_id: str = "") -> list[FriendRequest]:
    """``incoming`` / ``outgoing`` 数组 -> ``[FriendRequest, ...]``（两者形状一样）。"""
    found = []
    for item in items or []:
        user = _friend_user(item, me_id=me_id)
        if user is None:
            continue
        try:
            ts = float((item or {}).get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        found.append(FriendRequest(user, str((item or {}).get("note") or ""), ts))
    return found


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class ApiClient:
    """MChat 服务端客户端。一个实例对应"一个账号 + 一个服务器"。"""

    def __init__(self, base_url: str, token: str = "", *, timeout: float = DEFAULT_TIMEOUT,
                 user_agent: str = "MChat/1.0"):
        from .session import normalize_server          # 放这里避免循环 import
        self.base_url = normalize_server(base_url)
        if not self.base_url:
            raise ValueError("服务器地址不能为空")
        self.token = token or ""
        self.timeout = float(timeout)
        self.user_agent = user_agent

    # ------------------------------------------------------------------ 底层
    def _url(self, path: str, query: dict | None = None) -> str:
        # 路径里的非 ASCII 必须百分号编码：urllib 拼请求行时用的是 ascii 编解码，
        # 直接把中文拼进 URL 会在 putrequest 里抛 UnicodeEncodeError（实测踩过）
        url = self.base_url + urllib.parse.quote(path, safe="/")
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _request(self, method: str, path: str, *, body=None, raw: bytes | None = None,
                 query: dict | None = None, timeout: float | None = None,
                 auth: bool = True) -> bytes:
        url = self._url(path, query)
        data = None
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:            # 服务端明确回了错误
            payload = b""
            try:
                payload = error.read()
            except OSError:
                pass
            code, message = _error_from_body(payload, error.code)
            raise ApiError(code, message, error.code) from None
        except urllib.error.URLError as error:             # 连不上/超时/DNS
            raise NetworkError(str(getattr(error, "reason", error)), url=url) from None
        except (TimeoutError, OSError) as error:
            raise NetworkError(str(error), url=url) from None

    def _json(self, method: str, path: str, **kwargs) -> dict:
        payload = self._request(method, path, **kwargs)
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise NetworkError("服务端返回的不是 JSON（地址填错了？）",
                               url=kwargs.get("url", path)) from None
        if not isinstance(data, dict):
            raise NetworkError("服务端返回的 JSON 结构不对", url=path)
        return data

    # ------------------------------------------------------------------ 账号
    def health(self, *, timeout: float = 6.0) -> dict:
        """探活（免鉴权）：登录界面的「测试连接」用它。"""
        return self._json("GET", "/api/health", timeout=timeout, auth=False)

    def register(self, username: str, password: str, display_name: str = "",
                 invite: str = "") -> dict:
        """注册并直接登录，返回 ``{"token", "user", ...}``。

        服务器默认会要求邀请码（没显式配置时它自己生成一个打在启动日志里），
        ``invite`` 就放在 body 的 ``invite`` 字段；服务器不要求邀请码时多传也会被忽略。
        """
        body = {"username": username, "password": password, "display_name": display_name}
        if invite:
            body["invite"] = invite
        return self._json("POST", "/api/register", auth=False, body=body)

    def login(self, username: str, password: str) -> dict:
        return self._json("POST", "/api/login", auth=False,
                          body={"username": username, "password": password})

    def me(self) -> dict:
        return self._json("GET", "/api/me", timeout=8.0)

    def logout(self) -> dict:
        return self._json("POST", "/api/logout", body={})

    # ------------------------------------------------------------------ 找人
    def search_users(self, query: str = "") -> list[dict]:
        data = self._json("GET", "/api/users", query={"query": query or None})
        return list(data.get("users") or [])

    def conversations(self) -> list[dict]:
        return list(self._json("GET", "/api/conversations").get("conversations") or [])

    def create_direct(self, username: str) -> dict:
        data = self._json("POST", "/api/conversations/direct",
                          body={"username": username})
        return dict(data.get("conversation") or {})

    def create_group(self, title: str, usernames) -> dict:
        data = self._json("POST", "/api/conversations/group",
                          body={"title": title, "usernames": list(usernames)})
        return dict(data.get("conversation") or {})

    def friends(self) -> dict:
        """好友 / 别人申请加我 / 我申请加别人（``GET /api/friends``，原样返回三段）。"""
        data = self._json("GET", "/api/friends", timeout=15.0)
        return {"friends": list(data.get("friends") or []),
                "incoming": list(data.get("incoming") or []),
                "outgoing": list(data.get("outgoing") or [])}

    def friend_request(self, username: str, note: str = "") -> dict:
        """申请加某人好友。

        返回 ``{"status": ...}``：``pending``（申请已发出）/ ``accepted``（对方先申请过
        我 → 直接互为好友，附带 ``conversation``）/ ``already_friends``。**这三种都是
        正常结果**，不抛异常（只有"没这个人"才是 404 :class:`ApiError`）。
        """
        body = {"username": username}
        if note:
            body["note"] = note
        return self._json("POST", "/api/friends/request", body=body, timeout=15.0)

    def friend_accept(self, username: str) -> dict:
        """同意对方发来的好友申请；会顺带把对方的留言落成会话第一条消息。"""
        return self._json("POST", "/api/friends/accept", body={"username": username},
                          timeout=15.0)

    def friend_reject(self, username: str) -> dict:
        return self._json("POST", "/api/friends/reject", body={"username": username},
                          timeout=15.0)

    def friend_remove(self, username: str) -> dict:
        """删除好友。**幂等**：本来就不是好友也回 ok；历史会话与消息一律保留。"""
        return self._json("POST", "/api/friends/remove", body={"username": username},
                          timeout=15.0)

    # ------------------------------------------------------------------ 消息
    def messages(self, conversation_id: str, *, before: float | None = None,
                 limit: int = 50) -> dict:
        return self._json("GET", f"/api/conversations/{conversation_id}/messages",
                          query={"before": before, "limit": int(limit)})

    def send_message(self, conversation_id: str, client_id: str, text: str, *,
                     attachment_id: str = "", reply_to: ReplyRef | None = None,
                     forwarded_from: str = "", merged=()) -> SendResult:
        """发一条消息，返回 :class:`SendResult`。

        单聊里对方还不是我的好友时，服务端会把这条消息**变成一条好友申请**
        （``{"message": null, "friend_request": {...}}``）—— 那是一种正常结果，
        不是错误；调用方检查 ``result.intercepted`` / ``result.friend_request``。
        """
        body = {
            "client_id": client_id,
            "text": text,
            "attachment_id": attachment_id or None,
            "reply_to": reply_to.to_json() if reply_to is not None else None,
            "forwarded_from": forwarded_from or "",
            "merged": [item.to_json() for item in merged],
        }
        data = self._json("POST", f"/api/conversations/{conversation_id}/messages",
                          body=body, timeout=20.0)
        return SendResult(data.get("message"), data.get("friend_request"))

    def mark_read(self, conversation_id: str, up_to_ts: float) -> dict:
        return self._json("POST", f"/api/conversations/{conversation_id}/read",
                          body={"up_to_ts": float(up_to_ts)})

    def set_reaction(self, conversation_id: str, message_id: str, emoji: str,
                     added: bool | None = None) -> dict:
        """给一条消息加/撤一个表情回应（服务端算计数，然后推给会话里所有人）。

        ``added=None`` 让服务端自己开关；一般传明确值（重试时幂等）。
        """
        body = {"message_id": message_id, "emoji": emoji}
        if added is not None:
            body["added"] = bool(added)
        return self._json("POST", f"/api/conversations/{conversation_id}/reactions",
                          body=body)

    def pin_message(self, conversation_id: str, message_id: str,
                    pinned: bool = True) -> dict:
        """会话内置顶（"双方置顶"）。只有"也为对方置顶"才该调它。

        "仅我可见"是纯客户端的事：只写本地库，不告诉服务端 ——
        否则对面会看到一条本该只有自己知道的置顶。
        """
        return self._json("POST", f"/api/conversations/{conversation_id}/pin",
                          body={"message_id": message_id, "pinned": bool(pinned)})

    def typing(self, conversation_id: str, typing: bool = True) -> dict:
        return self._json("POST", f"/api/conversations/{conversation_id}/typing",
                          body={"typing": bool(typing)}, timeout=6.0)

    def sync(self, cursor: int, *, timeout: float = 20.0) -> dict:
        """长轮询：最多挂 ``timeout`` 秒，拿到事件就返回。"""
        return self._json("GET", "/api/sync", query={"cursor": int(cursor),
                                                     "timeout": timeout},
                          timeout=timeout + SYNC_TIMEOUT_SLACK)

    # ------------------------------------------------------------------ 附件
    def upload(self, path: str, *, name: str | None = None) -> dict:
        """上传一个本地文件，返回 ``{"id","name","size"}``。

        本地附件缓存是**加密落盘**的，所以发之前先按需解密 —— 否则传上去的是密文，
        对面下回来打不开（客户端侧这块一直是透明的：界面只给路径）。
        """
        with open(path, "rb") as handle:
            payload = handle.read()
        cipher = active_cipher()
        if cipher is not None and cipher.available and is_encrypted_file(path):
            payload = cipher.decrypt_bytes(payload)
        if len(payload) > MAX_UPLOAD_BYTES:
            raise ApiError("too_large", "文件超过 64 MB，客户端就没打算发", 413)
        filename = name or os.path.basename(path)
        data = self._json("POST", "/api/upload", raw=payload, timeout=120.0,
                          query={"name": filename})
        return dict(data.get("attachment") or {})

    def download(self, attachment_id: str, dest_path: str, *, root: str = "") -> str:
        """把附件下到 ``dest_path``（父目录不存在会自动建）。返回落地的路径。

        落地路径必须由 :func:`mchat.core.paths.resolve_attachment_path` 算出来
        （它保证落在下载目录里）。``root`` 给了就再校验一次"确实在 root 里面"：
        路径字符串自己是分不出好坏的（见 core/paths.py 里的说明），但**调用方知道
        自己想把文件写到哪个目录**，所以就由调用方把它交进来。
        """
        dest_path = os.path.abspath(dest_path)
        if root and not is_inside(root, dest_path):
            raise NetworkError("附件落地路径不在下载目录里，已拒绝写入", url=dest_path)
        leaf = safe_local_name(os.path.basename(dest_path))
        if leaf != os.path.basename(dest_path):
            raise NetworkError("附件落地路径的文件名不合法，已拒绝写入", url=dest_path)
        folder = os.path.dirname(dest_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        payload = self._request("GET", f"/api/files/{attachment_id}", timeout=120.0)
        cipher = active_cipher()
        if cipher is not None and cipher.available:
            # 落盘即密文：附件缓存里不该留明文（要看清它得先过 materialize）
            payload = cipher.encrypt_bytes(payload)
        tmp = dest_path + ".part"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, dest_path)                # 半截文件绝不留在目标路径上
        return dest_path

    # ------------------------------------------------------------------ 辅助
    def with_token(self, token: str) -> "ApiClient":
        return ApiClient(self.base_url, token, timeout=self.timeout,
                         user_agent=self.user_agent)


def _error_from_body(payload: bytes, status: int) -> tuple[str, str]:
    """从错误响应里抠出 ``(code, message)``；body 不是预期格式就按状态码兜底。"""
    fallback = {
        400: ("invalid_request", "请求不合法"),
        401: ("unauthorized", "登录已失效"),
        403: ("not_member", "没有权限"),
        404: ("not_found", "没找到"),
        409: ("duplicate_id", "冲突"),
        413: ("too_large", "太大"),
        500: ("server_error", "服务端出错了"),
    }.get(status, ("server_error", f"HTTP {status}"))
    try:
        data = json.loads(payload.decode("utf-8"))
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return (str(error.get("code") or fallback[0]),
                    str(error.get("message") or fallback[1]))
        if isinstance(error, str):                      # 有的实现在这里放字符串
            return fallback[0], error
    except (ValueError, UnicodeDecodeError):
        pass
    return fallback
