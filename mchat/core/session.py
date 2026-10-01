"""账号会话：**登录信息的本地缓存**（Telegram 那种"登一次就够了"）。

要解决的事
----------
真联网之后每次打开程序都重新输密码是没法用的 —— 账号态必须在本地留一份，
下次启动直接拿它去跟服务端确认"我还登录着"，确认通过就跳过登录界面。

缓存的**不是密码**，而是服务端发的**令牌（token）**
--------------------------------------------------
* 密码只在你第一次登录的那一次网络上出现，之后永不落地，也不进任何本地文件；
* 落地的只有 ``token`` + 服务器地址 + 账号显示信息（够界面上显示"当前是谁"）；
* 服务端那边 token 可以随时吊销（退出登录就吊销），所以偷到文件也只能用到吊销为止；
* 文件权限写成 0600（Windows 上 chmod 作用有限，但至少语义明确）。

一个文件存**多个账号**（``accounts`` 字典），并记住"上次用的是哪个"
------------------------------------------------------------
形如 ``{"last": "http://host:8765|alice", "accounts": {"<key>": {...}}}``。
这样换账号回来不用重敲用户名；``logout`` 只摘掉那一个账号的 token，别的账号不受影响。

本模块**零 Qt 依赖**（路径自己算，和 QStandardPaths 的 AppDataLocation 对齐），
所以整条"缓存 → 读取 → 失效清理"的逻辑都能在没有界面时单测。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace

from .atrest import AtRestError, StorageCipher

__all__ = ["Session", "SessionStore", "DEFAULT_SERVER", "default_data_dir",
           "default_session_path", "account_key", "normalize_server", "safe_name"]

DEFAULT_SERVER = "http://127.0.0.1:8765"      # 本机自测用；真部署改成你的服务器地址
SESSION_FILENAME = "account.json"


def default_data_dir() -> str:
    """本地数据目录（**和界面层的 QStandardPaths.AppDataLocation 同一个位置**）。

    Windows: ``%APPDATA%\\MChat\\MChat``；其它平台: ``~/.config/MChat``。
    库文件（mchat-*.db）、剪贴板图片、这份账号缓存都住在这里，卸载就是删这一个目录。
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "MChat", "MChat")
    base = (os.environ.get("XDG_CONFIG_HOME")
            or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "MChat")


def default_session_path() -> str:
    return os.path.join(default_data_dir(), SESSION_FILENAME)


def normalize_server(url: str) -> str:
    """把用户敲的服务器地址收拾干净：补协议、去尾部斜杠、去空白。

    ``example.com:8765`` -> ``http://example.com:8765``（少写 http:// 是最常见的输入）。
    """
    text = (url or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def safe_name(text: str) -> str:
    """把用户名/主机名收拾成能当文件名的一段（库文件按账号分开存要用）。"""
    keep = []
    for char in str(text or ""):
        keep.append(char if (char.isalnum() or char in "._-@") else "_")
    return "".join(keep).strip("._") or "default"


def account_key(server: str, username: str) -> str:
    """一个账号的稳定键：``http://host:8765|alice``（同服务器同用户名才算同一个）。"""
    return f"{normalize_server(server)}|{str(username or '').strip().lower()}"


@dataclass(frozen=True)
class Session:
    """一次登录的账号态。``token`` 是唯一能证明"我是谁"的东西。"""

    server: str
    token: str
    user_id: str
    username: str
    display_name: str = ""
    saved_at: float = 0.0
    remember: bool = True

    # ---------------------------------------------------------------- 便捷
    @property
    def key(self) -> str:
        return account_key(self.server, self.username)

    @property
    def name(self) -> str:
        """界面上显示的名字（昵称优先）。"""
        return self.display_name or self.username

    @property
    def host(self) -> str:
        """服务器地址里"主机:端口"那一段（侧栏底部那行小字用）。"""
        return self.server.split("://")[-1]

    @property
    def label(self) -> str:
        return f"{self.name} @ {self.host}"

    def with_token(self, token: str) -> "Session":
        return replace(self, token=token, saved_at=time.time())

    # ---------------------------------------------------------------- 序列化
    def to_json(self) -> dict:
        return {"server": self.server, "token": self.token, "user_id": self.user_id,
                "username": self.username, "display_name": self.display_name,
                "saved_at": self.saved_at, "remember": self.remember}

    @classmethod
    def from_json(cls, data) -> "Session | None":
        """坏数据一律返回 None（手改坏的缓存不该让程序起不来）。

        ``token`` 允许为空：勾了"不记住我"的账号只留个名字方便下次填，
        读回来仍然是这个账号（只是要重新输密码，见 :meth:`SessionStore.last`）。
        """
        if not isinstance(data, dict):
            return None
        server = normalize_server(str(data.get("server") or ""))
        token = str(data.get("token") or "")
        username = str(data.get("username") or "")
        if not server or not username:
            return None
        try:
            saved_at = float(data.get("saved_at") or 0.0)
        except (TypeError, ValueError):
            saved_at = 0.0
        return cls(server=server, token=token, user_id=str(data.get("user_id") or ""),
                   username=username,
                   display_name=str(data.get("display_name") or ""),
                   saved_at=saved_at, remember=bool(data.get("remember", True)))


class SessionStore:
    """账号缓存的读写（JSON 文件）。``path=None`` 就用系统默认位置。

    ``cipher`` 给了就**把令牌加密后再落盘**（见 :mod:`mchat.core.atrest`）：文件里不再
    明文躺着那个"能直接冒充我"的令牌。老文件里的明文令牌照读（没有 ``enc1:`` 前缀就当
    明文），下次保存时自动换成密文。
    """

    def __init__(self, path: str | None = None, *, cipher: StorageCipher | None = None):
        self.path = path or default_session_path()
        self._cipher = cipher or StorageCipher(None, reason="没有配置静态加密")

    # ---------------------------------------------------------------- 加解密
    def _seal_token(self, token: str) -> str:
        return self._cipher.encrypt_text(token) if self._cipher.available else token

    def _unseal_token(self, token: str) -> str:
        """解不开就当"没有令牌"（顶多要求重新登录一次），绝不把密文当令牌用。"""
        if not token or not isinstance(token, str):
            return ""
        try:
            return self._cipher.decrypt_text(token) or ""
        except AtRestError:
            return ""

    # ---------------------------------------------------------------- 读
    def _read(self) -> dict:
        """读文件。**加密/解密一律在这一对方法里做**，别处只管明文。

        放这里而不是 ``save``：``forget``/``save`` 都是"读回来改一改再写回去"的
        读写循环，只要有一处忘了加密，别人的令牌就被明文写回去了。
        """
        return self._unseal_accounts(self._read_raw())

    def _read_raw(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        accounts = data.get("accounts")
        if not isinstance(accounts, dict) or not accounts:
            # 兼容第一版格式：整个文件就是一个 session
            single = Session.from_json(data)
            if single is not None:
                return {"accounts": {single.key: single.to_json()}, "last": single.key}
            if isinstance(accounts, dict):
                data["accounts"] = accounts
                return data
            return {}
        return data

    def sessions(self) -> list[Session]:
        """缓存里的全部账号（按上次使用时间倒序）。"""
        found = []
        for raw in self._read().get("accounts", {}).values():
            session = Session.from_json(raw)
            if session is not None:
                found.append(session)
        found.sort(key=lambda s: s.saved_at, reverse=True)
        return found

    def last(self) -> Session | None:
        """上次用的那个账号（自动登录就靠它）；没有就 None。

        令牌为空（"不记住我"）时也照常返回 —— 调用方看 ``session.token`` 决定
        自己能不能自动登录，而不是让它以为"这个账号不存在"。
        """
        data = self._read()
        key = str(data.get("last") or "")
        raw = data.get("accounts", {}).get(key)
        session = Session.from_json(raw)
        if session is not None:
            return session
        sessions = self.sessions()
        return sessions[0] if sessions else None

    def get(self, server: str, username: str) -> Session | None:
        raw = self._read().get("accounts", {}).get(account_key(server, username))
        return Session.from_json(raw)

    def has_any(self) -> bool:
        return self.last() is not None

    # ---------------------------------------------------------------- 写
    def save(self, session: Session) -> str:
        """记下一个账号的登录态（``remember=False`` 就只记住账号名、不留 token）。"""
        data = self._read()
        accounts = dict(data.get("accounts") or {})
        stored = session if session.remember else replace(session, token="")
        accounts[session.key] = stored.to_json()
        data["accounts"] = accounts
        data["last"] = session.key
        self._write(data)
        return self.path

    def forget(self, server: str, username: str) -> bool:
        """退出登录：摘掉这个账号（缓存里别的账号不动）。"""
        data = self._read()
        accounts = dict(data.get("accounts") or {})
        key = account_key(server, username)
        existed = accounts.pop(key, None) is not None
        data["accounts"] = accounts
        if str(data.get("last") or "") == key:
            data["last"] = next(iter(accounts), "")
        self._write(data)
        return existed

    def clear(self) -> None:
        """把整份缓存删掉（测试与"彻底退出"用）。"""
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write(self, data: dict) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._seal_accounts(data), handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)               # 原子替换：断电也不会留半截文件
        try:
            os.chmod(self.path, 0o600)           # 令牌别给别人读
        except OSError:
            pass

    # ---------------------------------------------------------------- 令牌加解密
    def _seal_accounts(self, data: dict) -> dict:
        """写盘前：每个账号的令牌换成密文。"""
        if not self._cipher.available:
            return data
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return data
        sealed = {}
        for key, record in accounts.items():
            if isinstance(record, dict):
                record = dict(record)
                record["token"] = self._seal_token(str(record.get("token") or ""))
            sealed[key] = record
        out = dict(data)
        out["accounts"] = sealed
        return out

    def _unseal_accounts(self, data: dict) -> dict:
        """读盘后：每个账号的令牌还原成明文（解不开就当空令牌 = 重新登录）。"""
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return data
        opened = {}
        for key, record in accounts.items():
            if isinstance(record, dict):
                record = dict(record)
                record["token"] = self._unseal_token(str(record.get("token") or ""))
            opened[key] = record
        out = dict(data)
        out["accounts"] = opened
        return out


def log_path_for(session: Session, folder: str | None = None) -> str:
    """这个账号的本地历史库路径 —— **按账号分开存**。

    换账号登录不会看到上一个账号的聊天记录（同一个库文件混着才是真的隐私问题）。
    """
    folder = folder or default_data_dir()
    return os.path.join(folder, f"mchat-{safe_name(session.username)}"
                               f"@{safe_name(session.host)}.db")
