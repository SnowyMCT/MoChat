#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MChat 服务端 —— 零第三方依赖的 HTTP + JSON 聊天后端。

为什么零依赖
------------
* 目标机器上常常连 pip 都用不了（内网、没装 python3-pip、没有 PyPI 出口），
  而这个服务只需要标准库里的 http.server / socketserver / sqlite3 / hashlib /
  secrets / json / threading 就能完整跑起来，所以刻意一个第三方包都不引。
* 好处：拷过去 `python3 mchat_server.py` 直接跑；没有 requirements.txt，
  不会有依赖版本冲突、不会有“装不上 cryptography 所以起不来”这类事故。
* 代价：实时推送用长轮询（HTTP 挂住请求）而不是 WebSocket —— 见 PROTOCOL.md §3.5，
  客户端一次拿到事件就立刻再发起下一次连接，延迟和体验都够用，且能过任何反代。

怎么跑
------
    python3 mchat_server.py                              # 本机 127.0.0.1:8765
    python3 mchat_server.py --host 0.0.0.0 --port 8765   # 对外提供服务
    python3 mchat_server.py --data /var/lib/mchat        # 生产环境建议显式指定
    python3 mchat_server.py --help                       # 全部参数

数据落在哪
----------
默认是**本文件旁边的 `data/` 目录**（即 `server/data/`，可用 `--data` 覆盖）：

    <data>/mchat.db         SQLite 数据库（账号 / 会话 / 消息 / token / 附件元信息）
    <data>/mchat.db-wal     SQLite 预写日志（运行时存在，正常退出会合并）
    <data>/mchat.db-shm
    <data>/files/<id>       上传的附件原始字节

**备份只需要备份整个 data 目录**（或者停服后只备份 mchat.db 和 files/）。

线程与稳定性
------------
* `ThreadingHTTPServer` + `daemon_threads = True`：每个请求一个线程，长轮询线程
  在 `threading.Condition` 上睡着等唤醒，不忙等、不空转 CPU。
* SQLite 每个线程一条独立连接（`threading.local()`），写操作再套一把全局锁串行化，
  不会出现 "SQLite objects created in a thread can only be used in that same thread"。
* Ctrl+C / SIGTERM 优雅退出：关监听 socket、落盘、打印一行日志（给 systemd stop 用）。

实现严格对照 server/PROTOCOL.md v1。协议要改的话，三处一起改：本文件、PROTOCOL.md、
客户端 mchat/core/api.py。
"""

import argparse
import base64
import collections
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
SERVER_NAME = "mchat-server"
PBKDF2_ROUNDS = 200000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,24}$")
MSG_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{6,128}$")
ONLINE_WINDOW = 60.0        # last_seen 在这个秒数内算“在线”
SEEN_TOUCH_INTERVAL = 10.0  # 每个用户最多每 10 秒写一次 last_seen
JSON_BODY_LIMIT = 4 * 1024 * 1024
DRAIN_LIMIT = 512 * 1024 * 1024  # 超限上传最多丢弃这么多字节再回 413

# --- 部署加固用的上限（都是"防着有人拿它当 DoS 工具/撑爆磁盘"的闸门） ---
MAX_FILENAME_CHARS = 120    # 附件名长度上限（超长名字会撑爆响应头，也会把客户端磁盘折腾坏）
MAX_MERGED_ITEMS = 50       # 一条消息里最多合并几条转发
MAX_MERGED_TEXT = 2000      # 合并转发里单条正文的上限（超了截断）
MAX_REPLY_PREVIEW = 300     # 引用预览文字上限（超了截断）
MAX_FORWARDED_FROM = 128    # “转发自 XX”那串会话名的上限
LOGIN_WINDOW = 60.0         # 登录/注册限流的窗口长度（秒）

# --- 第二道防线：单条消息/单个会话上"能被无限堆的东西"都要有顶 ---
MAX_REACTIONS_PER_USER = 20   # 一个人在同一条消息上最多挂几个不同的表情
MAX_PINS_PER_CONV = 50        # 一个会话最多同时置顶几条
MAX_GROUP_MEMBERS = 100       # 一个群最多几个人（含自己）
MAX_FRIEND_NOTE = 500         # 好友申请留言的上限（门禁把消息正文当留言时截断到这）

# ---------------------------------------------------------------------------
# 错误码（严格按 PROTOCOL.md §1；末尾几条是本实现的部署加固开关，属于额外扩展）
# ---------------------------------------------------------------------------
ERRORS = {
    "invalid_request": (400, "请求格式不对"),
    "invalid_username": (400, "用户名不合法（3-24 位，字母数字下划线点减号）"),
    "weak_password": (400, "密码太短（至少 6 位）"),
    "username_taken": (409, "用户名已被注册"),
    "bad_credentials": (401, "用户名或密码不对"),
    "unauthorized": (401, "登录状态已失效，请重新登录"),
    "not_found": (404, "对象不存在"),
    "not_member": (403, "你不在这个会话里"),
    "duplicate_id": (409, "这条消息 id 已被占用"),
    "too_large": (413, "文件超过服务端限制"),
    "server_error": (500, "服务端内部错误"),
    # --- 部署加固用（不在 PROTOCOL.md 的错误表里，但格式一致） ---
    "registration_disabled": (403, "本服务已关闭注册"),
    "invalid_invite": (403, "邀请码不对"),
    "too_many_requests": (429, "操作太频繁，请稍后再试"),
    "message_too_long": (413, "消息太长"),
    "quota_exceeded": (413, "附件配额已满"),
}


class ApiError(Exception):
    def __init__(self, code, message=None, status=None):
        self.code = code
        base_status, base_message = ERRORS.get(code, (500, "服务端内部错误"))
        self.status = status if status is not None else base_status
        self.message = message if message else base_message
        Exception.__init__(self, "%s: %s" % (self.code, self.message))


def fail(code, message=None):
    raise ApiError(code, message)


def now_ts():
    return time.time()


# ---------------------------------------------------------------------------
# 安全相关的小工具
# ---------------------------------------------------------------------------
def safe_filename(name, fallback="file"):
    """把外部送来的文件名收拾成"只剩一个文件名"的安全形态。

    为什么必须有它：附件名是**发送者**给的，而它会出现在两个危险的地方 ——
    ``Content-Disposition`` 响应头（带 CR/LF 就能做响应拆分，实测过）和
    客户端本地落地路径（带 ``..`` / 分隔符就能写到下载目录外，实测过）。
    所以这里一次把三件事做掉：只留最后一段（去掉两种分隔符）、
    去掉控制字符（含 CR/LF/NUL）、限制长度。

    注意：这不是"防注入"的全部 —— 客户端那边还会再按同样的规则清洗一次，
    并且把落地路径钉死在下载目录里（见 mchat/core/paths.py）。
    """
    text = str(name or "")
    # 反斜杠先换成正斜杠，然后只取最后一段：C:\a\b.txt / ../../etc/passwd 都变 b.txt / passwd
    text = text.replace("\\", "/").split("/")[-1]
    text = "".join(ch for ch in text if ch.isprintable() and ch not in "\r\n\x00\x1b")
    text = text.strip()
    if len(text) > MAX_FILENAME_CHARS:
        # 截断时尽量保住后缀：界面/系统靠它认文件类型
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            text = stem[:MAX_FILENAME_CHARS - len(ext) - 1] + "." + ext
        else:
            text = text[:MAX_FILENAME_CHARS]
    if text in ("", ".", ".."):
        return fallback
    return text


def clipping(text, limit):
    """把字符串砍到 limit 个字符（用于引用预览、合并转发这类展示字段）。"""
    value = str(text or "")
    return value if len(value) <= limit else value[:limit]


class RateLimiter(object):
    """登录/注册的闸门。

    两层（都只放在内存里，重启即清零 —— 这类限流本来就不需要持久化）：
    * **每 IP 滑动窗口**：``--login-rate`` 次 / ``LOGIN_WINDOW`` 秒，超了回 429。
      挡的是"换着账号名撞密码"和一键注册刷号；
    * **每账号连续失败锁定**：连着错 ``--lock-after`` 次就锁 ``--lock-seconds`` 秒。
      挡的是"盯住一个账号慢慢撞"，同时不给攻击者机会用别人的账号互相锁死太久。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._hits = {}        # ip -> deque(时间戳)
        self._fails = {}       # username -> [连续失败次数, 锁定到什么时候]

    def allow(self, ip):
        """这一 IP 现在还能不能发登录/注册请求。"""
        limit = self.cfg.login_rate
        if limit <= 0:
            return True
        now = now_ts()
        with self._lock:
            hits = self._hits.setdefault(ip, collections.deque())
            while hits and now - hits[0] > LOGIN_WINDOW:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            # 顺手清理这张表：IPv6 客户端能换着源地址打，只按"空桶"清是不够的
            if len(self._hits) > 4096:
                for key, bucket in list(self._hits.items()):
                    if not bucket or now - bucket[-1] > LOGIN_WINDOW:
                        self._hits.pop(key, None)
            return True

    def locked_for(self, username):
        """这个账号还剩多少秒处于锁定状态（0 = 没锁）。"""
        if self.cfg.lock_after <= 0:
            return 0.0
        with self._lock:
            state = self._fails.get(username)
            if not state:
                return 0.0
            remaining = state[1] - now_ts()
            return remaining if remaining > 0 else 0.0

    def note_failure(self, username):
        if self.cfg.lock_after <= 0 or not username:
            return
        with self._lock:
            state = self._fails.get(username) or [0, 0.0]
            state[0] += 1
            if state[0] >= self.cfg.lock_after:
                state[1] = now_ts() + self.cfg.lock_seconds
                state[0] = 0
            self._fails[username] = state

    def note_success(self, username):
        if username:
            with self._lock:
                self._fails.pop(username, None)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class Config(object):
    """运行参数。**默认值就是按"能安全地放出去"选的**：注册要邀请码、登录有闸门、
    连接数和正文长度都有上限 —— 出厂的默认状态不该是一个对全世界敞开、还能被当
    DoS 工具用的服务端。"""

    def __init__(self, args):
        self.host = args.host
        self.port = int(args.port)
        self.data_dir = os.path.abspath(args.data)
        self.db_path = os.path.join(self.data_dir, "mchat.db")
        self.files_dir = os.path.join(self.data_dir, "files")
        self.token_ttl_days = float(args.token_ttl_days)     # <= 0 = 永不过期（存 NULL）
        self.max_upload_mb = float(args.max_upload_mb)
        self.user_quota_mb = float(args.user_quota_mb)
        self.orphan_days = float(args.orphan_days)
        self.allow_register = not args.no_register
        self.invite_code = (args.invite_code or "").strip()
        self.invite_generated = False
        if self.allow_register and not self.invite_code and not args.open_register:
            # 既没说关注册、也没给邀请码、也没明说"开放注册" → 自己生成一个，
            # 启动时打在日志里。默认状态因此不是"全世界都能注册"，运维也一眼能拿到码。
            self.invite_code = secrets.token_urlsafe(9)
            self.invite_generated = True
        self.login_rate = int(args.login_rate)
        self.lock_after = int(args.lock_after)
        self.lock_seconds = float(args.lock_seconds)
        # 磁盘加密：给了密钥文件才开。**读不出来就抛**（起不来好过"以为加密了其实没有"）
        self.storage = StorageCrypto.load(getattr(args, "storage_key_file", ""))
        self.trust_proxy = bool(args.trust_proxy)
        self.max_connections = int(args.max_connections)
        self.max_text_chars = int(args.max_text_chars)
        self.max_merged = int(args.max_merged)
        self.max_reactions = int(args.max_reactions)
        self.max_pins = int(args.max_pins)
        self.max_group_members = int(args.max_group_members)
        self.typing_interval = float(args.typing_interval)
        self.long_poll_max = int(args.long_poll_max)
        self.online_window = float(args.online_window)
        self.presence_interval = float(args.presence_interval)
        self.verbose = bool(args.verbose)


# ---------------------------------------------------------------------------
# 磁盘加密（可选）：消息正文 / 附件文件名 / 附件字节在磁盘上不留明文
# ---------------------------------------------------------------------------
TEXT_PREFIX = "enc1:"                # 文本字段的版本前缀（和客户端同一套约定）
FILE_MAGIC = b"MCHSRV1"              # 服务端写的加密附件头
STORAGE_KEY_ENV = "MCHAT_STORAGE_KEY_FILE"


def hash_token(token):
    """令牌只存哈希：库/备份被人拿走也不能直接拿去冒充某个账号。

    令牌本身是 32 字节随机（``token_urlsafe(32)``），不是人选的密码，所以不需要加盐慢哈希
    —— 直接 SHA-256 就够，省得每次请求都跑一遍 PBKDF2。
    """
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


class StorageKeyError(Exception):
    """磁盘加密的配置有问题。**故意让服务端起不来**：不能"以为加密了其实没有"。"""


def _pick_cipher_backend():
    """挑一个 AEAD 后端。服务端本体零依赖，所以这个功能是"要用就装一个"。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # noqa: F401
    except ImportError:
        AESGCM = None
    if AESGCM is not None:
        return "cryptography", AESGCM
    try:
        from Crypto.Cipher import AES                                   # noqa: F401
    except ImportError as exc:
        raise StorageKeyError(
            "配了 --storage-key-file，但这台机器上没有任何加密后端。请二选一：\n"
            "  * pip install cryptography   （或者 pycryptodome），然后重启；\n"
            "  * 改回不用 --storage-key-file，改用整盘加密（Linux: LUKS / Windows: BitLocker）\n"
            "原始错误：%s" % exc)
    return "pycryptodome", AES


class StorageCrypto(object):
    """可选的磁盘加密。

    **只有运维显式给了密钥才启用。**为什么不"自己生成一把放在数据目录里"：密钥和数据
    放在一起等于没加密 —— 拿到备份的人两样都拿到了。所以密钥放在数据目录**外面**
    （systemd 可以用 ``LoadCredential=`` 喂进来），这样"只泄漏数据目录/备份"时里面
    的东西仍然是密文。

    没启用时是明文存储，启动日志里会**明说**这一点（不制造"我加密了"的错觉）。
    """

    def __init__(self, key=None, *, reason="", backend=None):
        self.key = key
        self.reason = reason
        self._backend = backend
        self._cryptography_aead = None
        self._pycrypto_aes = None
        if key and backend is not None:
            name, module = backend
            if name == "cryptography":
                self._cryptography_aead = module
            else:
                self._pycrypto_aes = module

    # ---------------------------------------------------------------- 构造
    @classmethod
    def disabled(cls, reason="没有配置磁盘加密密钥"):
        return cls(None, reason=reason)

    @classmethod
    def load(cls, key_file):
        """从密钥文件读密钥；没给路径就是"不加密"。**读不出来直接抛。**"""
        path = (key_file or os.environ.get(STORAGE_KEY_ENV) or "").strip()
        if not path:
            return cls.disabled()
        if not os.path.isfile(path):
            raise StorageKeyError("找不到密钥文件：%s" % path)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise StorageKeyError("密钥文件读不了：%s（%s）" % (path, exc))
        key = _decode_key(raw)
        if key is None:
            raise StorageKeyError(
                "密钥文件内容不合法：需要 32 字节随机（原始字节，或一行 base64/hex）。"
                "生成一个： head -c 32 /dev/urandom > /etc/mchat/storage.key && chmod 600 它")
        return cls(key, backend=_pick_cipher_backend())

    # ---------------------------------------------------------------- 状态
    @property
    def available(self):
        return self.key is not None and (self._cryptography_aead is not None
                                         or self._pycrypto_aes is not None)

    def describe(self):
        return ("磁盘加密：开（AES-256-GCM，密钥来自外部密钥文件）" if self.available
                else "磁盘加密：**关** —— 消息与附件在磁盘上是明文（%s）" % self.reason)

    # ---------------------------------------------------------------- 文本
    def encrypt_text(self, text):
        if text is None or text == "" or not self.available:
            return text
        return TEXT_PREFIX + base64.b64encode(self._aead_seal(str(text).encode("utf-8"))).decode("ascii")

    def decrypt_text(self, value):
        if not isinstance(value, str) or not value.startswith(TEXT_PREFIX):
            return value
        return self._aead_open(base64.b64decode(value[len(TEXT_PREFIX):])).decode("utf-8")

    def is_encrypted_text(self, value):
        return isinstance(value, str) and value.startswith(TEXT_PREFIX)

    # ---------------------------------------------------------------- 字节
    def encrypt_bytes(self, blob):
        if not self.available:
            return blob
        return FILE_MAGIC + self._aead_seal(blob)

    def decrypt_bytes(self, blob):
        if not blob.startswith(FILE_MAGIC):
            return blob                      # 明文（历史文件 / 没开加密）
        if not self.available:
            raise StorageKeyError("这个文件是加密的，但现在没配 --storage-key-file")
        return self._aead_open(blob[len(FILE_MAGIC):])

    # ---------------------------------------------------------------- AEAD 细节
    # 统一成"nonce ‖ tag ‖ 密文"一种打包方式，两个后端都往里塞。
    def _aead_seal(self, data):
        nonce = secrets.token_bytes(12)
        if self._cryptography_aead is not None:
            sealed = self._cryptography_aead(self.key).encrypt(nonce, data, AAD)
            return nonce + sealed                # cryptography 的密文尾部已带 tag
        sealed = self._pycrypto_aes.new(self.key, self._pycrypto_aes.MODE_GCM, nonce=nonce)
        sealed.update(AAD)
        ciphertext, tag = sealed.encrypt_and_digest(data)
        return nonce + tag + ciphertext

    def _aead_open(self, blob):
        if len(blob) < 12 + 16:
            raise StorageKeyError("密文不完整（文件被截断？）")
        nonce, body = blob[:12], blob[12:]
        try:
            if self._cryptography_aead is not None:
                return self._cryptography_aead(self.key).decrypt(nonce, body, AAD)
            tag, ciphertext = body[:16], body[16:]
            sealed = self._pycrypto_aes.new(self.key, self._pycrypto_aes.MODE_GCM, nonce=nonce)
            sealed.update(AAD)
            return sealed.decrypt_and_verify(ciphertext, tag)
        except Exception as exc:                 # 两个后端的异常类型不一样，一起收
            raise StorageKeyError("解密失败（密钥不对，或内容被改过）：%s" % exc)


AAD = b"mchat-server-storage"


def _decode_key(raw):
    """密钥文件可以是原始 32 字节，也可以是一行 base64/hex 文本。"""
    if len(raw) == 32:
        return raw
    text = raw.decode("ascii", "ignore").strip()
    for decode in (base64.b64decode, bytes.fromhex):
        try:
            candidate = decode(text)
        except Exception:
            continue
        if len(candidate) == 32:
            return candidate
    return None


# ---------------------------------------------------------------------------
# 存储层：每线程一条 SQLite 连接 + 写操作全局串行化
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  display_name TEXT NOT NULL,
  pw_hash BLOB NOT NULL,
  salt BLOB NOT NULL,
  created_at REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);

-- 令牌**只存哈希**：库文件/备份被别人拿走，也不能直接拿里面的东西冒充某个账号登录。
-- （令牌是 32 字节随机串，不是人选的口令，所以直接 SHA-256 就够，不用慢哈希。）
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  last_used REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  creator_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);

CREATE TABLE IF NOT EXISTS conv_members (
  conversation_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  joined_at REAL NOT NULL,
  last_read_ts REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (conversation_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON conv_members(user_id);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL,
  attachment_id TEXT,
  reply_to TEXT,
  forwarded_from TEXT NOT NULL DEFAULT '',
  merged TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, ts);

CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  name TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at REAL NOT NULL
);

-- 消息回应（表情）。一人一条记录，计数由服务端 GROUP BY 算出来 ——
-- 客户端不许自己加一减一，否则两个人同时点就会算错。
CREATE TABLE IF NOT EXISTS reactions (
  conversation_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  emoji TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (message_id, user_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id);

-- 会话内置顶（"双方置顶"）。只存**双方可见**的那些置顶：
-- "仅我可见"是纯客户端的事，服务端不该知道（否则用户会发现"我明明只给自己置顶"）。
-- 一个会话可以置顶多条（客户端横幅能用 ‹ › 翻），所以主键是（会话, 消息）。
CREATE TABLE IF NOT EXISTS conv_pins (
  conversation_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  pinned_by TEXT NOT NULL,
  pinned_at REAL NOT NULL,
  PRIMARY KEY (conversation_id, message_id)
);

-- 好友关系：只存**已经同意**的。约定互为好友时插两行（a→b 和 b→a），
-- 所以"谁是我的好友"一句 SELECT 就够，"我们是不是好友"也只是一次存在性判断。
CREATE TABLE IF NOT EXISTS friends (
  user_id TEXT NOT NULL,
  friend_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (user_id, friend_id)
);
CREATE INDEX IF NOT EXISTS idx_friends_user ON friends(user_id);

-- 好友申请：单向（requester 申请 target）。同一对只留一行（后一次覆盖留言和时间），
-- 反复发消息不该在库里堆出一串申请（见 post_message 的门禁）。
CREATE TABLE IF NOT EXISTS friend_requests (
  requester_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (requester_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_friend_requests_target ON friend_requests(target_id);
"""


class Database(object):
    """每线程一条连接的 SQLite 封装。所有写操作走 write()，内部用全局锁串行化。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._conns = []
        self._conns_lock = threading.Lock()
        self._setup()

    # -- 连接管理 ---------------------------------------------------------
    def _new_connection(self):
        conn = sqlite3.connect(self.cfg.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
        # 开了磁盘加密之后，删掉/改写过的旧明文不该还能从空闲页里翻出来
        conn.execute("PRAGMA secure_delete=ON")
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._new_connection()
            self._local.conn = c
        return c

    def _setup(self):
        conn = self.conn
        conn.executescript(SCHEMA)
        self._migrate_tokens(conn)
        # 必须在**建表之后、开始服务之前**跑：门禁上线后，老库里已有的单聊
        # 如果没被追认成好友，两个人会在升级后突然发不出消息。
        self.migrated_friend_pairs = self._migrate_friends(conn)

    def _migrate_friends(self, conn):
        """把**存量单聊**的双方一次性追认成互为好友（INSERT OR IGNORE，幂等）。

        为什么要有这一步：发消息门禁只认 friends 表 —— 老库里已经存在的单聊，
        当年根本没有"申请/同意"这回事（那时候还没有好友功能），不补一行的话
        升级之后这对人直接发不出消息。这不是他们该承受的，所以这里把
        "已经开过的单聊"补成好友关系。

        为什么用 meta 标记位而不是每次启动都扫一遍：用户**主动删掉**的好友关系，
        不该在服务端重启后又自己长回来（删除是明确的意愿）。标记位让这段迁移
        只对"还没有这个键的老库"生效一次。

        返回追认了多少对。
        """
        if self.get_meta("friends_migrated"):
            return 0
        stamp = now_ts()
        pairs = set()          # 一对人只算一次（同一对的两个方向在下面各插一行）
        for row in conn.execute("SELECT id FROM conversations WHERE kind='direct'"):
            members = [m["user_id"] for m in conn.execute(
                "SELECT user_id FROM conv_members WHERE conversation_id=?", (row["id"],))]
            for index, a in enumerate(members):
                for b in members[index + 1:]:
                    pairs.add((a, b) if a <= b else (b, a))
        for a, b in pairs:
            conn.execute(
                "INSERT OR IGNORE INTO friends(user_id, friend_id, created_at) VALUES(?,?,?)",
                (a, b, stamp),
            )
            conn.execute(
                "INSERT OR IGNORE INTO friends(user_id, friend_id, created_at) VALUES(?,?,?)",
                (b, a, stamp),
            )
        self.set_meta("friends_migrated", "1")
        return len(pairs)

    def _migrate_tokens(self, conn):
        """老库的 ``tokens`` 表存的是**明文令牌** → 换成哈希表（所有人需要重登一次）。

        为什么不做"原地转换"：哈希是单向的，没法把明文行改写成它自己的哈希
        （客户端手里那串才是凭据，服务端算不出对应的明文）。所以宁可让老令牌全部失效，
        也不能把一张明文令牌表继续留在磁盘上。

        返回是否真的迁移过（给启动日志用）。
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_xinfo(tokens)")}
        if "token_hash" in columns:
            return False
        conn.execute("DROP TABLE IF EXISTS tokens")
        conn.executescript(SCHEMA)
        return True

    # -- 读 ---------------------------------------------------------------
    def query(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def scalar(self, sql, args=(), default=None):
        row = self.query_one(sql, args)
        if row is None:
            return default
        return row[0]

    # -- 写 ---------------------------------------------------------------
    @contextlib.contextmanager
    def write(self):
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql, args=()):
        """单条写的语法糖。"""
        with self.write() as conn:
            return conn.execute(sql, args)

    # -- meta -------------------------------------------------------------
    def get_meta(self, key, default=None):
        row = self.query_one("SELECT v FROM meta WHERE k=?", (key,))
        return row["v"] if row else default

    def set_meta(self, key, value):
        with self.write() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, str(value)))

    # -- 关停 -------------------------------------------------------------
    def flush(self):
        with self._write_lock:
            for conn in list(self._conns):
                try:
                    conn.commit()
                except Exception:
                    pass

    def close_all(self):
        with self._conns_lock:
            conns = list(self._conns)
            self._conns = []
        for conn in conns:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()


# ---------------------------------------------------------------------------
# 事件总线：内存事件队列 + Condition 唤醒（长轮询的核心）
# ---------------------------------------------------------------------------
class EventBus(object):
    """每个用户一条单调递增的事件序号流。

    cursor 语义（PROTOCOL.md §3.5）：
    * cursor 缺省 / 0 → 只拿“从现在起”的新事件（把游标推到当前最大序号）；
    * 事件已被清理、或服务端重启导致序号对不上 → 回一个 resync 事件，让客户端
      重新拉一次 /api/conversations；
    * 没有事件就在 Condition 上睡最多 timeout 秒，被 push() 唤醒后立刻返回，
      不忙等、不占 CPU。
    """

    MAX_PER_USER = 500
    PERSIST_EVERY = 50

    def __init__(self, db):
        self.db = db
        self.cond = threading.Condition()
        self.log = {}
        self._dirty = 0
        raw = db.get_meta("next_seq")
        try:
            self.next_seq = max(1, int(raw))
        except (TypeError, ValueError):
            self.next_seq = 1

    def _state(self, user_id):
        st = self.log.get(user_id)
        if st is None:
            st = {"items": collections.deque(maxlen=self.MAX_PER_USER), "max_seq": 0}
            self.log[user_id] = st
        return st

    def push(self, user_id, event):
        need_persist = False
        with self.cond:
            st = self._state(user_id)
            seq = self.next_seq
            self.next_seq += 1
            st["max_seq"] = seq
            st["items"].append((seq, event))
            self._dirty += 1
            if self._dirty >= self.PERSIST_EVERY:
                self._dirty = 0
                need_persist = True
            self.cond.notify_all()
        # DB 写放在锁外，避免 cond → db 之外再出现 db → cond 的锁序
        if need_persist:
            self.flush()

    def flush(self):
        try:
            self.db.set_meta("next_seq", self.next_seq)
        except Exception:
            if self.db.cfg.verbose:
                traceback.print_exc()

    def sync(self, user_id, cursor, timeout):
        """返回 (events, new_cursor)。"""
        if cursor is None:
            cursor = 0
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            fail("invalid_request", "cursor 必须是整数")
        if cursor < 0:
            fail("invalid_request", "cursor 不能是负数")

        deadline = now_ts() + max(0.0, float(timeout))
        with self.cond:
            st = self._state(user_id)
            if cursor == 0:
                # 首次连接：不要历史事件，从“现在”开始
                cursor = st["max_seq"]
            while True:
                events = [ev for (seq, ev) in st["items"] if seq > cursor]
                if events:
                    return events, st["max_seq"]
                # 没有新事件：判断游标是不是已经落到保留窗口之外
                if cursor > st["max_seq"]:
                    return [{"kind": "resync"}], st["max_seq"]
                if st["items"]:
                    oldest = st["items"][0][0]
                    if cursor < oldest - 1:
                        return [{"kind": "resync"}], st["max_seq"]
                elif cursor < st["max_seq"]:
                    return [{"kind": "resync"}], st["max_seq"]
                remaining = deadline - now_ts()
                if remaining <= 0:
                    return [], st["max_seq"]
                self.cond.wait(remaining)


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------
class App(object):
    def __init__(self, db, bus, cfg):
        self.db = db
        self.bus = bus
        self.cfg = cfg
        self._seen_lock = threading.Lock()
        self._seen_cache = {}
        # 「正在输入」的节流表：(会话, 用户) -> 上次推送时间
        self._typing_lock = threading.Lock()
        self._typing_seen = {}
        # 登录/注册的闸门（每 IP 频率 + 每账号连续失败锁定）
        self.limiter = RateLimiter(cfg)
        # 磁盘加密（没配密钥时是个"不可用"的对象，所有加解密都是原样返回）
        self.storage = cfg.storage

    # ---------------- 磁盘加密 ----------------
    def _seal(self, value):
        """写库前：内容字段加密（没开磁盘加密就是原样返回）。"""
        return self.storage.encrypt_text(value) if self.storage.available else value

    def _unseal(self, value, fallback=None):
        """读库后：解密。**解不开要看得见**，不能假装有内容。"""
        if not self.storage.is_encrypted_text(value):
            return value
        try:
            return self.storage.decrypt_text(value)
        except StorageKeyError as exc:
            return fallback if fallback is not None else "⚠️无法解密（密钥不对或数据被改过）"

    def reencrypt_plaintext(self):
        """把库里/磁盘上还留着的**明文**内容改写成密文（启用磁盘加密后的第一次启动）。

        和客户端同一个思路：老数据不能读不出来，但也不能继续明文躺在磁盘上。
        改完 VACUUM 一次 —— 老明文页不然还留在文件里。返回改动处数。
        """
        storage = self.storage
        if not storage.available:
            return 0
        changed = 0
        with self.db.write() as conn:
            for row in self.db.query("SELECT id, text, reply_to, merged, forwarded_from FROM messages"):
                if storage.is_encrypted_text(row["text"]):
                    continue
                conn.execute(
                    "UPDATE messages SET text=?, reply_to=?, merged=?, forwarded_from=? WHERE id=?",
                    (storage.encrypt_text(row["text"]), storage.encrypt_text(row["reply_to"]),
                     storage.encrypt_text(row["merged"]),
                     storage.encrypt_text(row["forwarded_from"]), row["id"]))
                changed += 1
            for row in self.db.query("SELECT id, name FROM attachments"):
                if storage.is_encrypted_text(row["name"]):
                    continue
                conn.execute("UPDATE attachments SET name=? WHERE id=?",
                             (storage.encrypt_text(row["name"]), row["id"]))
                changed += 1
        for row in self.db.query("SELECT id FROM attachments"):
            path = os.path.join(self.cfg.files_dir, row["id"])
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                blob = handle.read()
            if blob.startswith(FILE_MAGIC):
                continue
            tmp = path + ".enc"
            with open(tmp, "wb") as handle:
                handle.write(storage.encrypt_bytes(blob))
            os.replace(tmp, path)
            changed += 1
        if changed:
            # VACUUM **不能开在事务里**（sqlite 会直接报 "cannot VACUUM from within a
            # transaction"）。连接本身是 autocommit 的，所以这里绕开 write() 直接执行。
            try:
                self.db.conn.execute("VACUUM")
            except sqlite3.Error:
                if self.cfg.verbose:
                    traceback.print_exc()
        return changed

    # ---------------- 小工具 ----------------
    def _new_id(self, prefix, table):
        # 8 字节随机（16 位 hex）：附件/会话 id 会出现在 URL 里，短 id 是能被枚举的
        # （4 字节只有 2^32 种可能 —— 见 post_message 里"附件必须是自己的"那道校验）
        for _ in range(12):
            candidate = prefix + secrets.token_hex(8)
            if self.db.query_one("SELECT 1 FROM %s WHERE id=?" % table, (candidate,)) is None:
                return candidate
        fail("server_error", "生成 id 失败")

    def _touch(self, user_id):
        now = now_ts()
        with self._seen_lock:
            last = self._seen_cache.get(user_id, 0.0)
            if now - last < SEEN_TOUCH_INTERVAL:
                return
            self._seen_cache[user_id] = now
        self.db.execute("UPDATE users SET last_seen=? WHERE id=?", (now, user_id))

    def user_by_username(self, username):
        if not isinstance(username, str):
            return None
        row = self.db.query_one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username.strip(),))
        return row

    def user_by_id(self, user_id):
        return self.db.query_one("SELECT * FROM users WHERE id=?", (user_id,))

    def is_online(self, row):
        try:
            return (now_ts() - float(row["last_seen"])) <= self.cfg.online_window
        except Exception:
            return False

    def user_dict(self, row):
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "online": self.is_online(row),
        }

    # ---------------- 鉴权 ----------------
    def auth(self, token, touch=True):
        if not token or not isinstance(token, str):
            fail("unauthorized")
        # 库里存的是哈希：拿请求里的令牌算一遍哈希再比（明文不落库、也不进日志）
        digest = hash_token(token)
        row = self.db.query_one("SELECT user_id, expires_at FROM tokens WHERE token_hash=?",
                                (digest,))
        if row is None:
            fail("unauthorized")
        expires_at = row["expires_at"]
        if expires_at is not None and float(expires_at) <= now_ts():
            self.db.execute("DELETE FROM tokens WHERE token_hash=?", (digest,))
            fail("unauthorized")
        user = self.user_by_id(row["user_id"])
        if user is None:
            self.db.execute("DELETE FROM tokens WHERE token_hash=?", (digest,))
            fail("unauthorized")
        if touch:
            self._touch(user["id"])
        return user

    def _issue_token(self, user_id):
        token = secrets.token_urlsafe(32)
        now = now_ts()
        # --token-ttl-days <= 0 → 存 NULL（永不过期）。janitor 只清 expires_at 非 NULL 的，
        # 所以"设成 0 想做永不过期"这件事现在是**成立**的（以前会立刻失效，是个坑）。
        expires_at = None
        if self.cfg.token_ttl_days > 0:
            expires_at = now + self.cfg.token_ttl_days * 86400.0
        self.db.execute(
            "INSERT INTO tokens(token_hash, user_id, created_at, expires_at, last_used) "
            "VALUES(?,?,?,?,?)",
            (hash_token(token), user_id, now, expires_at, now),
        )
        return token

    # ---------------- 账号 ----------------
    def register(self, body):
        if not self.cfg.allow_register:
            fail("registration_disabled")
        if self.cfg.invite_code:
            invite = body.get("invite")
            if not isinstance(invite, str) or not hmac.compare_digest(invite, self.cfg.invite_code):
                fail("invalid_invite", "注册需要正确的邀请码（body 里加 \"invite\" 字段）")
        username = body.get("username")
        password = body.get("password")
        display_name = body.get("display_name")
        if not isinstance(username, str) or not isinstance(password, str):
            fail("invalid_request", "需要 username 和 password（字符串）")
        if display_name is not None and not isinstance(display_name, str):
            fail("invalid_request", "display_name 必须是字符串")
        username = username.strip()
        if not USERNAME_RE.match(username):
            fail("invalid_username")
        if len(password) < 6:
            fail("weak_password")
        if self.db.query_one("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)) is not None:
            fail("username_taken")

        salt = secrets.token_bytes(16)
        pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
        user_id = self._new_id("u_", "users")
        now = now_ts()
        shown = (display_name or "").strip() or username
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO users(id, username, display_name, pw_hash, salt, created_at, last_seen) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (user_id, username, shown, pw_hash, salt, now, now),
                )
        except sqlite3.IntegrityError:
            fail("username_taken")
        token = self._issue_token(user_id)
        user = self.user_by_id(user_id)
        return {"token": token, "user": self.user_dict(user), "server_time": now_ts()}

    def login(self, body):
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            fail("invalid_request", "需要 username 和 password（字符串）")
        user = self.user_by_username(username.strip())
        if user is None:
            # 不要泄露“用户不存在”和“密码错”的区别
            hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"mchat-dummy-salt", PBKDF2_ROUNDS)
            fail("bad_credentials")
        pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), user["salt"], PBKDF2_ROUNDS)
        if not hmac.compare_digest(pw_hash, user["pw_hash"]):
            fail("bad_credentials")
        self._touch(user["id"])
        token = self._issue_token(user["id"])
        return {"token": token, "user": self.user_dict(user), "server_time": now_ts()}

    def logout(self, token):
        self.db.execute("DELETE FROM tokens WHERE token_hash=?", (hash_token(token),))
        return {"ok": True}

    # ---------------- 会话 ----------------
    def members_of(self, cid):
        rows = self.db.query(
            "SELECT u.* FROM conv_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.conversation_id=? ORDER BY m.joined_at ASC, u.username ASC",
            (cid,),
        )
        return rows

    def member_ids(self, cid):
        rows = self.db.query("SELECT user_id FROM conv_members WHERE conversation_id=?", (cid,))
        return [r["user_id"] for r in rows]

    def ensure_member(self, cid, user_id):
        conv = self.db.query_one("SELECT * FROM conversations WHERE id=?", (cid,))
        if conv is None:
            fail("not_found", "会话不存在")
        if self.db.query_one(
            "SELECT 1 FROM conv_members WHERE conversation_id=? AND user_id=?", (cid, user_id)
        ) is None:
            fail("not_member")
        return conv

    def last_read_ts(self, cid, user_id):
        row = self.db.query_one(
            "SELECT last_read_ts FROM conv_members WHERE conversation_id=? AND user_id=?", (cid, user_id)
        )
        return float(row["last_read_ts"]) if row else 0.0

    def conversation_dict(self, cid, viewer_id):
        conv = self.db.query_one("SELECT * FROM conversations WHERE id=?", (cid,))
        if conv is None:
            return None
        members = self.members_of(cid)
        if conv["kind"] == "direct":
            title = None
            for m in members:
                if m["id"] != viewer_id:
                    title = m["display_name"] or m["username"]
                    break
            if not title:
                me = self.user_by_id(viewer_id)
                title = (me["display_name"] or me["username"]) if me else "对话"
        else:
            title = conv["title"] or "群聊"
        read_mark = self.last_read_ts(cid, viewer_id)
        last = self.db.query_one(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY ts DESC, id DESC LIMIT 1", (cid,)
        )
        unread = self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND sender_id<>? AND ts>?",
            (cid, viewer_id, read_mark),
            default=0,
        )
        return {
            "id": conv["id"],
            "kind": conv["kind"],
            "title": title,
            "members": [self.user_dict(m) for m in members],
            "last_message": self.message_dict(last, viewer_id) if last is not None else None,
            "last_ts": float(last["ts"]) if last is not None else 0.0,
            "unread": int(unread or 0),
            "updated_at": float(conv["updated_at"]),
            "pins": self.pins_of(cid, viewer_id),
        }

    def pins_of(self, cid, viewer_id):
        """这个会话里"双方可见"的置顶（新的排前面）。

        每条带上被置顶那条消息的**快照**：对方可能还没把那批历史拉到本地，
        光给一个 id 的话他画不出横幅（横幅要显示"谁置顶了哪句话"）。
        """
        rows = self.db.query(
            "SELECT p.message_id, p.pinned_by, p.pinned_at FROM conv_pins p "
            "JOIN messages m ON m.id = p.message_id AND m.conversation_id = p.conversation_id "
            "WHERE p.conversation_id=? ORDER BY p.pinned_at DESC",
            (cid,),
        )
        out = []
        for row in rows:
            message = self.db.query_one("SELECT * FROM messages WHERE id=?", (row["message_id"],))
            if message is None:
                continue
            pinner = self.user_by_id(row["pinned_by"])
            out.append({
                "message_id": row["message_id"],
                "pinned_by": row["pinned_by"],
                "pinned_by_name": ((pinner["display_name"] or pinner["username"])
                                   if pinner is not None else ""),
                "pinned_at": float(row["pinned_at"]),
                "for_both": True,
                "message": self.message_dict(message, viewer_id),
            })
        return out

    def set_pin(self, me, cid, message_id, pinned):
        """置顶/取消置顶一条消息，**并推给会话里的所有人**（包括自己的其它设备）。

        只有这一个入口动 ``conv_pins``：客户端点「也为 XX 置顶」时才调它；
        「仅我可见」那个选项根本不该发请求（本机记着就行）。
        """
        self.ensure_member(cid, me["id"])
        message_id = str(message_id or "").strip()
        if not message_id:
            fail("invalid_request", "需要 message_id")
        if pinned is None:
            pinned = True
        if not isinstance(pinned, bool):
            fail("invalid_request", "pinned 必须是布尔值")
        row = self.db.query_one(
            "SELECT * FROM messages WHERE id=? AND conversation_id=?", (message_id, cid)
        )
        if row is None:
            fail("not_found", "这条消息不在这个会话里")
        if pinned:
            # 一个会话能置顶的条数要有顶：每次置顶都给全体成员推事件（还带消息快照），
            # 而且客户端的置顶横幅是"‹ › 一条条翻"的用法，堆多了也没意义。
            already = self.db.query_one(
                "SELECT 1 FROM conv_pins WHERE conversation_id=? AND message_id=?", (cid, message_id))
            if already is None:
                count = self.db.scalar(
                    "SELECT COUNT(*) FROM conv_pins WHERE conversation_id=?", (cid,), default=0)
                if int(count or 0) >= self.cfg.max_pins:
                    fail("invalid_request", "这个会话最多同时置顶 %d 条" % self.cfg.max_pins)
        stamp = now_ts()
        with self.db.write() as conn:
            if pinned:
                conn.execute(
                    "INSERT INTO conv_pins(conversation_id, message_id, pinned_by, pinned_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(conversation_id, message_id) DO UPDATE SET "
                    "pinned_by=excluded.pinned_by, pinned_at=excluded.pinned_at",
                    (cid, message_id, me["id"], stamp),
                )
            else:
                conn.execute(
                    "DELETE FROM conv_pins WHERE conversation_id=? AND message_id=?",
                    (cid, message_id),
                )
        name = me["display_name"] or me["username"]
        for uid in self.member_ids(cid):
            self.bus.push(uid, {
                "kind": "pin",
                "conversation_id": cid,
                "message_id": message_id,
                "message": self.message_dict(row, uid),
                "pinned": bool(pinned),
                "pinned_by": me["id"],
                "pinned_by_name": name,
                "ts": stamp,
            })
        return {"ok": True, "pinned": bool(pinned), "message_id": message_id}

    def presence_sweep(self, seen, announce=True):
        """扫一遍所有用户的在线状态，变化的人通知"跟他有共同会话的人"。返回变化人数。

        ``seen`` 由调用方持有（上一轮的判定结果），``announce=False`` 只建立基线。
        """
        rows = self.db.query("SELECT id, last_seen FROM users")
        changed = []
        for row in rows:
            now_online = self.is_online(row)
            if seen.get(row["id"]) == now_online:
                continue
            seen[row["id"]] = now_online
            changed.append((row["id"], now_online))
        if not changed:
            return 0
        stamp = now_ts()
        for user_id, now_online in changed:
            peers = self.peer_ids(user_id)
            for peer in peers:
                if not announce:
                    continue
                self.bus.push(peer, {
                    "kind": "presence",
                    "user_id": user_id,
                    "online": bool(now_online),
                    "ts": stamp,
                })
        return len(changed)

    def peer_ids(self, user_id):
        """跟这个人在同一个会话里的所有人（他自己除外）—— 在线状态只需要推给他们。"""
        rows = self.db.query(
            "SELECT DISTINCT other.user_id AS uid FROM conv_members mine "
            "JOIN conv_members other ON other.conversation_id = mine.conversation_id "
            "WHERE mine.user_id=? AND other.user_id<>?",
            (user_id, user_id),
        )
        return [row["uid"] for row in rows]

    def list_conversations(self, user_id):
        rows = self.db.query(
            "SELECT c.* FROM conversations c JOIN conv_members m ON m.conversation_id = c.id "
            "WHERE m.user_id=?",
            (user_id,),
        )
        out = []
        for row in rows:
            item = self.conversation_dict(row["id"], user_id)
            if item is not None:
                out.append(item)
        out.sort(key=lambda c: (-c["last_ts"], -c["updated_at"], c["id"]))
        return out

    def create_direct(self, me, username):
        other = self.user_by_username(username.strip()) if isinstance(username, str) else None
        if other is None:
            fail("not_found", "没有这个用户")
        if other["id"] == me["id"]:
            fail("invalid_request", "不能和自己建单聊")
        existing = self.db.query_one(
            "SELECT c.id FROM conversations c WHERE c.kind='direct' "
            "AND (SELECT COUNT(*) FROM conv_members WHERE conversation_id=c.id)=2 "
            "AND EXISTS(SELECT 1 FROM conv_members WHERE conversation_id=c.id AND user_id=?) "
            "AND EXISTS(SELECT 1 FROM conv_members WHERE conversation_id=c.id AND user_id=?) LIMIT 1",
            (me["id"], other["id"]),
        )
        if existing is not None:
            return self.conversation_dict(existing["id"], me["id"]), False
        cid = self._new_id("c_", "conversations")
        now = now_ts()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO conversations(id, kind, title, creator_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (cid, "direct", "", me["id"], now, now),
            )
            conn.execute(
                "INSERT INTO conv_members(conversation_id, user_id, joined_at, last_read_ts) VALUES(?,?,?,?)",
                (cid, me["id"], now, 0.0),
            )
            conn.execute(
                "INSERT INTO conv_members(conversation_id, user_id, joined_at, last_read_ts) VALUES(?,?,?,?)",
                (cid, other["id"], now, 0.0),
            )
        # 「新建会话」事件：对方（以及自己的其它设备）立刻能在列表里看到这个单聊
        for uid in (me["id"], other["id"]):
            self.bus.push(uid, {
                "kind": "conversation",
                "conversation_id": cid,
                "conversation": self.conversation_dict(cid, uid),
            })
        return self.conversation_dict(cid, me["id"]), True

    def create_group(self, me, title, usernames):
        if usernames is None:
            usernames = []
        if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
            fail("invalid_request", "usernames 必须是字符串数组")
        if title is not None and not isinstance(title, str):
            fail("invalid_request", "title 必须是字符串")
        # 人数上限**先**卡：不卡的话一个 4MB 的 usernames 数组会让服务端为几千个人各查一次库、
        # 各写一行成员、各推一条事件 —— 建群就是最省事的骚扰/放大接口。
        if len(usernames) + 1 > self.cfg.max_group_members:
            fail("invalid_request", "一个群最多 %d 人（含自己）" % self.cfg.max_group_members)
        members = []
        seen = set([me["id"]])
        for name in usernames:
            row = self.user_by_username(name.strip())
            if row is None:
                fail("not_found", "没有这个用户：%s" % name)
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            members.append(row)
        if not members:
            fail("invalid_request", "群里至少要有一个人")
        clean_title = (title or "").strip()
        if not clean_title:
            names = [me["display_name"] or me["username"]] + [m["display_name"] or m["username"] for m in members]
            clean_title = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
        cid = self._new_id("c_", "conversations")
        now = now_ts()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO conversations(id, kind, title, creator_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (cid, "group", clean_title[:64], me["id"], now, now),
            )
            for uid in [me["id"]] + [m["id"] for m in members]:
                conn.execute(
                    "INSERT INTO conv_members(conversation_id, user_id, joined_at, last_read_ts) VALUES(?,?,?,?)",
                    (cid, uid, now, 0.0),
                )
        # 通知所有成员（含建群者自己的其它设备）
        for uid in [me["id"]] + [m["id"] for m in members]:
            self.bus.push(
                uid,
                {
                    "kind": "conversation",
                    "conversation_id": cid,
                    "conversation": self.conversation_dict(cid, uid),
                },
            )
        return self.conversation_dict(cid, me["id"])

    # ---------------- 好友 ----------------
    def are_friends(self, user_id, other_id):
        """两人是不是好友。关系是双向存的，看一行就够。"""
        return self.db.query_one(
            "SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (user_id, other_id)
        ) is not None

    def peer_in_direct(self, me_id, cid):
        """单聊里的"对方" = 成员里除我之外的那个人；不是单聊就返回 None。

        用成员表算而不是让客户端传：客户端说"对方是谁"是可以伪造的，
        而"这是不是单聊、另一个成员是谁"是服务端事实。
        """
        kind = self.db.scalar("SELECT kind FROM conversations WHERE id=?", (cid,))
        if kind != "direct":
            return None
        for uid in self.member_ids(cid):
            if uid != me_id:
                return uid
        return None

    def _push_friend_event(self, kind, actor, target_id=None, note=None, ts=None):
        """好友事件只推给"相关的那个人"和发起者自己的其它设备 —— 复用现有 EventBus。

        推给发起者自己是为了多设备同步（手机上加完好友，电脑上也得跟着变），
        这跟 message/reaction 事件的惯例一致。
        """
        stamp = now_ts() if ts is None else ts
        event = {"kind": kind, "user": self.user_dict(actor), "ts": stamp}
        if note is not None:
            event["note"] = note
        for uid in (actor["id"], target_id):
            if uid:
                self.bus.push(uid, event)

    def list_friends(self, me):
        """好友 / 收到的好友申请 / 我发出去还没被回应的申请。"""
        friends = [
            {"user": self.user_dict(row), "since": float(row["since"])}
            for row in self.db.query(
                "SELECT u.*, f.created_at AS since FROM friends f JOIN users u ON u.id=f.friend_id "
                "WHERE f.user_id=? ORDER BY f.created_at ASC, u.username ASC",
                (me["id"],),
            )
        ]
        # incoming / outgoing 都要剔掉"其实已经是好友了"的那种：申请和同意可能交错
        # 发生，同意了之后旧申请行不该继续挂在列表里当噪音。
        incoming = [
            {"user": self.user_dict(row), "note": self._unseal(row["note"], "") or "",
             "ts": float(row["ts"])}
            for row in self.db.query(
                "SELECT u.*, r.note AS note, r.updated_at AS ts FROM friend_requests r "
                "JOIN users u ON u.id=r.requester_id WHERE r.target_id=? AND NOT EXISTS("
                "  SELECT 1 FROM friends f WHERE f.user_id=? AND f.friend_id=r.requester_id"
                ") ORDER BY r.updated_at ASC",
                (me["id"], me["id"]),
            )
        ]
        outgoing = [
            {"user": self.user_dict(row), "note": self._unseal(row["note"], "") or "",
             "ts": float(row["ts"])}
            for row in self.db.query(
                "SELECT u.*, r.note AS note, r.updated_at AS ts FROM friend_requests r "
                "JOIN users u ON u.id=r.target_id WHERE r.requester_id=? AND NOT EXISTS("
                "  SELECT 1 FROM friends f WHERE f.user_id=? AND f.friend_id=r.target_id"
                ") ORDER BY r.updated_at ASC",
                (me["id"], me["id"]),
            )
        ]
        return {"friends": friends, "incoming": incoming, "outgoing": outgoing}

    def request_friend(self, me, username, note=None):
        """申请加好友。对方**已经申请过我**时直接互为好友（两个人都伸手了，别再互相等）。"""
        other = self.user_by_username(username) if isinstance(username, str) else None
        if other is None:
            fail("not_found", "没有这个用户")
        if other["id"] == me["id"]:
            fail("invalid_request", "不能加自己为好友")
        if note is not None and not isinstance(note, str):
            fail("invalid_request", "note 必须是字符串")
        note = clipping(note or "", MAX_FRIEND_NOTE)
        if self.are_friends(me["id"], other["id"]):
            return {"status": "already_friends"}
        pending = self.db.query_one(
            "SELECT * FROM friend_requests WHERE requester_id=? AND target_id=?",
            (other["id"], me["id"]),
        )
        if pending is not None:
            stamp = now_ts()
            friend, conv = self._accept_friendship(
                me, other, self._unseal(pending["note"], "") or "", stamp)
            self._push_friend_event("friend_accept", me, other["id"], ts=stamp)
            return {"status": "accepted", "friend": friend, "conversation": conv}
        stamp = now_ts()
        self._upsert_friend_request(me["id"], other["id"], note, stamp)
        self._push_friend_event("friend_request", me, other["id"], note=note, ts=stamp)
        return {"status": "pending",
                "request": {"user": self.user_dict(other), "note": note, "ts": stamp}}

    def _upsert_friend_request(self, requester_id, target_id, note, stamp):
        """同一对只留一行：后来的留言覆盖前面的，created_at 保持第一次的时间。"""
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO friend_requests(requester_id, target_id, note, created_at, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(requester_id, target_id) DO UPDATE SET "
                "note=excluded.note, updated_at=excluded.updated_at",
                (requester_id, target_id, self._seal(note), stamp, stamp),
            )

    def _accept_friendship(self, acceptor, requester, note, stamp):
        """互相写成好友 + 确保有单聊 + 把申请里的留言落成会话第一条消息。

        留言是**申请人**说的，所以消息的 sender 是 requester（这样两边看到的
        气泡方向才对）。返回 (friend 对象, Conversation 对象)。
        """
        with self.db.write() as conn:
            conn.execute("DELETE FROM friend_requests WHERE requester_id=? AND target_id=?",
                         (requester["id"], acceptor["id"]))
            conn.execute("DELETE FROM friend_requests WHERE requester_id=? AND target_id=?",
                         (acceptor["id"], requester["id"]))
            conn.execute("INSERT OR IGNORE INTO friends(user_id, friend_id, created_at) VALUES(?,?,?)",
                         (acceptor["id"], requester["id"], stamp))
            conn.execute("INSERT OR IGNORE INTO friends(user_id, friend_id, created_at) VALUES(?,?,?)",
                         (requester["id"], acceptor["id"], stamp))
        # 单聊已经存在（门禁那次就在这个会话里发的）就复用，没有才新建 ——
        # 复用走的还是既有的 create_direct，行为（含 conversation 事件）不变。
        conv, _created = self.create_direct(acceptor, requester["username"])
        if note:
            self._materialize_note(acceptor, requester, conv["id"], note, stamp)
            conv = self.conversation_dict(conv["id"], acceptor["id"])
        since = self.db.scalar(
            "SELECT created_at FROM friends WHERE user_id=? AND friend_id=?",
            (acceptor["id"], requester["id"]), default=stamp)
        friend = {"user": self.user_dict(requester), "since": float(since or stamp)}
        return friend, conv

    def _materialize_note(self, acceptor, requester, cid, note, stamp):
        """把好友申请的留言写成会话里的一条消息（申请人发出），并推 message 事件。

        这条消息没有客户端生成的 client_id（它不是"某人打的字"，是服务端补的），
        所以用服务端的 id 规则现造一个。
        """
        text = clipping(note, self.cfg.max_text_chars)
        msg_id = self._new_id("m_", "messages")
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO messages(id, conversation_id, sender_id, text, ts, attachment_id, "
                "reply_to, forwarded_from, merged, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (msg_id, cid, requester["id"], self._seal(text), stamp, None, None,
                 self._seal(""), self._seal("[]"), stamp),
            )
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, cid))
        saved = self.db.query_one("SELECT * FROM messages WHERE id=?", (msg_id,))
        for uid in self.member_ids(cid):
            self.bus.push(uid, {
                "kind": "message",
                "conversation_id": cid,
                "message": self.message_dict(saved, uid),
            })

    def accept_friend(self, me, username):
        other = self.user_by_username(username) if isinstance(username, str) else None
        if other is None:
            fail("not_found", "没有这个用户")
        row = self.db.query_one(
            "SELECT * FROM friend_requests WHERE requester_id=? AND target_id=?",
            (other["id"], me["id"]),
        )
        if row is None:
            fail("not_found", "没有对方发给你的好友申请")
        stamp = now_ts()
        friend, conv = self._accept_friendship(
            me, other, self._unseal(row["note"], "") or "", stamp)
        self._push_friend_event("friend_accept", me, other["id"], ts=stamp)
        return {"status": "accepted", "friend": friend, "conversation": conv}

    def reject_friend(self, me, username):
        other = self.user_by_username(username) if isinstance(username, str) else None
        if other is None:
            fail("not_found", "没有这个用户")
        row = self.db.query_one(
            "SELECT * FROM friend_requests WHERE requester_id=? AND target_id=?",
            (other["id"], me["id"]),
        )
        if row is None:
            fail("not_found", "没有对方发给你的好友申请")
        self.db.execute("DELETE FROM friend_requests WHERE requester_id=? AND target_id=?",
                        (other["id"], me["id"]))
        # 申请人那边可能只剩一个**空单聊**（他就是在里面发消息被拦成申请的）：
        # 既然被拒了，就把它收掉，别让人家留一个点进去什么都发不出去的会话。
        self._drop_empty_direct(me["id"], other["id"])
        self._push_friend_event("friend_reject", me, other["id"])
        return {"ok": True}

    def _drop_empty_direct(self, a_id, b_id):
        """删掉两人之间"一条消息都没有"的单聊（连同成员行）。有历史的会话一律保留。"""
        row = self.db.query_one(
            "SELECT c.id FROM conversations c WHERE c.kind='direct' "
            "AND (SELECT COUNT(*) FROM conv_members WHERE conversation_id=c.id)=2 "
            "AND EXISTS(SELECT 1 FROM conv_members WHERE conversation_id=c.id AND user_id=?) "
            "AND EXISTS(SELECT 1 FROM conv_members WHERE conversation_id=c.id AND user_id=?) "
            "AND NOT EXISTS(SELECT 1 FROM messages WHERE conversation_id=c.id) LIMIT 1",
            (a_id, b_id),
        )
        if row is None:
            return False
        with self.db.write() as conn:
            conn.execute("DELETE FROM conv_members WHERE conversation_id=?", (row["id"],))
            conn.execute("DELETE FROM conversations WHERE id=?", (row["id"],))
        return True

    def remove_friend(self, me, username):
        """删好友：只删关系，历史会话和消息一律留着（幂等，删不存在的也是 ok）。"""
        other = self.user_by_username(username) if isinstance(username, str) else None
        if other is None:
            fail("not_found", "没有这个用户")
        if other["id"] == me["id"]:
            fail("invalid_request", "不能删自己")
        with self.db.write() as conn:
            conn.execute("DELETE FROM friends WHERE user_id=? AND friend_id=?",
                         (me["id"], other["id"]))
            conn.execute("DELETE FROM friends WHERE user_id=? AND friend_id=?",
                         (other["id"], me["id"]))
        self._push_friend_event("friend_remove", me, other["id"])
        return {"ok": True}

    def _friend_gate(self, me, cid, text):
        """单聊门禁：对方还不是我的好友 → 这条消息**不落库**，改成一条好友申请。

        返回 None 表示放行（是好友、或不是单聊）；否则返回给客户端的那份
        friend_request 对象。留言就是这条消息的正文（截到 MAX_FRIEND_NOTE）。
        """
        peer_id = self.peer_in_direct(me["id"], cid)
        if peer_id is None or self.are_friends(me["id"], peer_id):
            return None
        peer = self.user_by_id(peer_id)
        if peer is None:
            return None
        note = clipping(text, MAX_FRIEND_NOTE)
        stamp = now_ts()
        self._upsert_friend_request(me["id"], peer_id, note, stamp)
        self._push_friend_event("friend_request", me, peer_id, note=note, ts=stamp)
        return {"user": self.user_dict(peer), "note": note, "ts": stamp}

    # ---------------- 消息 ----------------
    def _attachment_dict(self, attachment_id):
        if not attachment_id:
            return None
        row = self.db.query_one("SELECT id, name, size FROM attachments WHERE id=?", (attachment_id,))
        if row is None:
            return None
        # 文件名也是内容（可能是"2026年体检报告.pdf"这种），一样解密
        name = self._unseal(row["name"], "附件")
        return {"id": row["id"], "name": name, "size": int(row["size"])}

    def reactions_of(self, message_id, viewer_id):
        """一条消息上的回应：``[{"emoji": "👍", "count": 2, "mine": true}, …]``。

        ``mine`` 是按**看的人**算的（同一条消息，两个人看到的 mine 不一样），
        所以每条事件都要按人各发一份 —— 跟消息的 status 一个道理。
        """
        rows = self.db.query(
            "SELECT user_id, emoji FROM reactions WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
        buckets = {}
        for row in rows:
            bucket = buckets.get(row["emoji"])
            if bucket is None:
                bucket = {"emoji": row["emoji"], "count": 0, "mine": False}
                buckets[row["emoji"]] = bucket
            bucket["count"] += 1
            if row["user_id"] == viewer_id:
                bucket["mine"] = True
        return sorted(buckets.values(), key=lambda item: (-item["count"], item["emoji"]))

    def toggle_reaction(self, me, cid, message_id, emoji, added=None):
        """点一下/撤一下某条消息的表情，**推给会话里所有人**（含自己的其它设备）。

        ``added`` 不传就是"开关"（客户端一般传明确值，重试才幂等）。
        """
        self.ensure_member(cid, me["id"])
        message_id = str(message_id or "").strip()
        emoji = str(emoji or "")
        if not message_id:
            fail("invalid_request", "需要 message_id")
        if not emoji or len(emoji) > 16 or any(ch.isspace() for ch in emoji):
            fail("invalid_request", "emoji 不合法")
        row = self.db.query_one(
            "SELECT * FROM messages WHERE id=? AND conversation_id=?", (message_id, cid)
        )
        if row is None:
            fail("not_found", "这条消息不在这个会话里")
        existing = self.db.query_one(
            "SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
            (message_id, me["id"], emoji),
        )
        if added is None:
            added = existing is None
        if not isinstance(added, bool):
            fail("invalid_request", "added 必须是布尔值")
        if added and existing is None:
            # 一条消息上"一个人挂的表情"要有上限：每加一个不同表情都会给全体成员推一次
            # 事件（还带整份 reactions 清单），不设顶就是一个现成的放大/刷屏手段。
            used = self.db.scalar(
                "SELECT COUNT(DISTINCT emoji) FROM reactions WHERE message_id=? AND user_id=?",
                (message_id, me["id"]), default=0)
            if int(used or 0) >= self.cfg.max_reactions:
                fail("invalid_request", "一条消息上最多挂 %d 个表情" % self.cfg.max_reactions)
        stamp = now_ts()
        with self.db.write() as conn:
            if added:
                conn.execute(
                    "INSERT INTO reactions(conversation_id, message_id, user_id, emoji, created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(message_id, user_id, emoji) DO NOTHING",
                    (cid, message_id, me["id"], emoji, stamp),
                )
            else:
                conn.execute(
                    "DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                    (message_id, me["id"], emoji),
                )
        name = me["display_name"] or me["username"]
        for uid in self.member_ids(cid):
            self.bus.push(uid, {
                "kind": "reaction",
                "conversation_id": cid,
                "message_id": message_id,
                "emoji": emoji,
                "added": bool(added),
                "by_id": me["id"],
                "by_name": name,
                "reactions": self.reactions_of(message_id, uid),
                "ts": stamp,
            })
        return {"ok": True, "message_id": message_id, "emoji": emoji,
                "added": bool(added), "reactions": self.reactions_of(message_id, me["id"])}

    def message_dict(self, row, viewer_id, senders=None, attachments=None):
        if row is None:
            return None
        if senders is None:
            senders = {}
        if attachments is None:
            attachments = {}
        sender_name = senders.get(row["sender_id"])
        if sender_name is None:
            sender = self.user_by_id(row["sender_id"])
            sender_name = (sender["display_name"] or sender["username"]) if sender else "未知用户"
            senders[row["sender_id"]] = sender_name

        att = None
        if row["attachment_id"]:
            if row["attachment_id"] in attachments:
                att = attachments[row["attachment_id"]]
            else:
                att = self._attachment_dict(row["attachment_id"])
                attachments[row["attachment_id"]] = att

        reply_to = None
        if row["reply_to"]:
            try:
                reply_to = json.loads(self._unseal(row["reply_to"], "") or "null")
            except ValueError:
                reply_to = None
        merged = []
        if row["merged"]:
            try:
                merged = json.loads(self._unseal(row["merged"], "[]") or "[]") or []
            except ValueError:
                merged = []

        if row["sender_id"] == viewer_id:
            readers = self.db.scalar(
                "SELECT COUNT(*) FROM conv_members WHERE conversation_id=? AND user_id<>? AND last_read_ts>=?",
                (row["conversation_id"], viewer_id, row["ts"]),
                default=0,
            )
            status = "read" if readers else "sent"
        else:
            status = "read" if self.last_read_ts(row["conversation_id"], viewer_id) >= float(row["ts"]) else "sent"

        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "sender_id": row["sender_id"],
            "sender_name": sender_name,
            "text": self._unseal(row["text"], ""),
            "ts": float(row["ts"]),
            "attachment": att,
            "reply_to": reply_to,
            "forwarded_from": self._unseal(row["forwarded_from"], "") or "",
            "merged": merged,
            "status": status,
            "reactions": self.reactions_of(row["id"], viewer_id),
        }

    def list_messages(self, viewer_id, cid, before, limit):
        if limit is None:
            limit = 50
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            fail("invalid_request", "limit 必须是整数")
        limit = max(1, min(200, limit))
        if before is not None:
            try:
                before = float(before)
            except (TypeError, ValueError):
                fail("invalid_request", "before 必须是时间戳")
            rows = self.db.query(
                "SELECT * FROM messages WHERE conversation_id=? AND ts<? ORDER BY ts DESC, id DESC LIMIT ?",
                (cid, before, limit + 1),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                (cid, limit + 1),
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        rows = list(reversed(rows))
        senders = {}
        attachments = {}
        return [self.message_dict(r, viewer_id, senders, attachments) for r in rows], has_more

    def post_message(self, me, cid, body):
        self.ensure_member(cid, me["id"])
        client_id = body.get("client_id")
        if not isinstance(client_id, str) or not MSG_ID_RE.match(client_id):
            fail("invalid_request", "client_id 必填（客户端生成的 32 位 hex）")
        text = body.get("text")
        if text is None:
            text = ""
        if not isinstance(text, str):
            fail("invalid_request", "text 必须是字符串")
        # 单条正文也得上限：JSON 体上限是 4MB，但"能塞进来"不等于"该让它进来" ——
        # 一条 4MB 的消息会把所有人的本地库和推送一起拖垮。
        if len(text) > self.cfg.max_text_chars:
            fail("message_too_long", "这条消息太长了（上限 %d 字符）" % self.cfg.max_text_chars)
        attachment_id = body.get("attachment_id")
        if attachment_id is not None and not isinstance(attachment_id, str):
            fail("invalid_request", "attachment_id 必须是字符串")
        if attachment_id:
            owner = self.db.query_one("SELECT owner_id FROM attachments WHERE id=?", (attachment_id,))
            if owner is None:
                fail("not_found", "附件不存在")
            # **只能引用自己上传的附件**。以前只查"这个 id 存不存在"，于是
            # "猜一个别人的附件 id，再在自己会话里发一条引用它的消息"就能绕开下载鉴权
            # （下载那边的规则是"引用它的消息所在会话的成员都能下"）—— 典型的 IDOR。
            if owner["owner_id"] != me["id"]:
                fail("not_member", "这不是你上传的附件，不能引用")
        if not text and not attachment_id:
            fail("invalid_request", "消息内容不能为空")

        reply_to = body.get("reply_to")
        if reply_to is not None:
            if not isinstance(reply_to, dict):
                fail("invalid_request", "reply_to 必须是对象或 null")
            # 引用是"展示用"的元数据：写不下就截断，不必为了它把整条消息顶回去
            reply_to = {
                "id": clipping(reply_to.get("id"), 128),
                "author": clipping(reply_to.get("author"), 64),
                "preview": clipping(reply_to.get("preview"), MAX_REPLY_PREVIEW),
            }
        merged = body.get("merged")
        if merged is None:
            merged = []
        if not isinstance(merged, list):
            fail("invalid_request", "merged 必须是数组")
        if len(merged) > self.cfg.max_merged:
            fail("invalid_request", "合并转发最多 %d 条" % self.cfg.max_merged)
        # 逐条只留协议里约定的四个字符串字段：否则客户端能往库里塞任意结构、任意大小
        merged = [
            {
                "author": clipping(item.get("author"), 64),
                "text": clipping(item.get("text"), MAX_MERGED_TEXT),
                "attachment": clipping(item.get("attachment"), MAX_FILENAME_CHARS),
                "clock": clipping(item.get("clock"), 32),
            }
            for item in merged if isinstance(item, dict)
        ]
        forwarded_from = body.get("forwarded_from") or ""
        if not isinstance(forwarded_from, str):
            fail("invalid_request", "forwarded_from 必须是字符串")
        forwarded_from = clipping(forwarded_from, MAX_FORWARDED_FROM)

        # 好友门禁：单聊里对方还不是我的好友 → 这条消息**不写库**，改成一条好友申请。
        # 放在所有字段校验之后：参数本身有问题（太长、附件不是自己的、坏的 JSON）
        # 仍然按老规矩报错，别让"发不出去"把真正的格式错误盖掉。
        gate = self._friend_gate(me, cid, text)
        if gate is not None:
            return None, False, gate

        # 幂等：同一条 client_id 重复 POST 只入库一次
        existing = self.db.query_one("SELECT * FROM messages WHERE id=?", (client_id,))
        if existing is not None:
            if existing["conversation_id"] != cid or existing["sender_id"] != me["id"]:
                fail("duplicate_id")
            return self.message_dict(existing, me["id"]), False, None

        ts = now_ts()
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO messages(id, conversation_id, sender_id, text, ts, attachment_id, reply_to, "
                    "forwarded_from, merged, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        client_id, cid, me["id"], self._seal(text), ts, attachment_id or None,
                        self._seal(json.dumps(reply_to, ensure_ascii=False)) if reply_to else None,
                        self._seal(forwarded_from),
                        self._seal(json.dumps(merged, ensure_ascii=False)), ts,
                    ),
                )
                conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, cid))
        except sqlite3.IntegrityError:
            existing = self.db.query_one("SELECT * FROM messages WHERE id=?", (client_id,))
            if existing is not None and existing["conversation_id"] == cid and existing["sender_id"] == me["id"]:
                return self.message_dict(existing, me["id"]), False, None
            fail("duplicate_id")

        saved = self.db.query_one("SELECT * FROM messages WHERE id=?", (client_id,))
        for uid in self.member_ids(cid):
            self.bus.push(uid, {
                "kind": "message",
                "conversation_id": cid,
                "message": self.message_dict(saved, uid),
            })
        return self.message_dict(saved, me["id"]), True, None

    def mark_read(self, me, cid, up_to_ts):
        self.ensure_member(cid, me["id"])
        if up_to_ts is None:
            fail("invalid_request", "需要 up_to_ts")
        try:
            up_to_ts = float(up_to_ts)
        except (TypeError, ValueError):
            fail("invalid_request", "up_to_ts 必须是时间戳")
        previous = self.last_read_ts(cid, me["id"])
        if up_to_ts <= previous:
            # 已经在"看到这儿"了：不改库、也不推事件。
            # 以前每次都把最近 200 条重新推一遍 —— 一个成员循环调这个接口就能
            # 给每个发过消息的人各推 200 条 read 事件（放大手段，且纯属白干）。
            return {"ok": True, "unchanged": True}
        with self.db.write() as conn:
            conn.execute(
                "UPDATE conv_members SET last_read_ts=? WHERE conversation_id=? AND user_id=? AND last_read_ts<?",
                (up_to_ts, cid, me["id"], up_to_ts),
            )
        rows = self.db.query(
            "SELECT id, sender_id FROM messages WHERE conversation_id=? AND sender_id<>? "
            "AND ts>? AND ts<=? ORDER BY ts ASC LIMIT 200",
            (cid, me["id"], previous, up_to_ts),
        )
        stamp = now_ts()
        for row in rows:
            self.bus.push(row["sender_id"], {
                "kind": "read",
                "conversation_id": cid,
                "message_id": row["id"],
                "reader_id": me["id"],
                "ts": stamp,
            })
        return {"ok": True}

    def send_typing(self, me, cid, typing):
        self.ensure_member(cid, me["id"])
        if typing is None:
            typing = True
        if not isinstance(typing, bool):
            fail("invalid_request", "typing 必须是布尔值")
        # 节流：同一个人在同一个会话里，最多每 --typing-interval 秒推一次「正在输入」。
        # 没有这道闸，一个成员用死循环刷 typing 就能把整群人的长轮询全部吵醒（每次 push
        # 都是 notify_all），既费服务端 CPU 也是现成的骚扰/放大手段。
        # **两个方向都节流**：以前只闸 typing=true、把 false 放行 —— 那是个洞，循环发
        # false 一样能无限推。但只要**状态变了**就立刻放行，所以"我不打字了"不会被压掉
        #（否则对方会一直看着"正在输入…"）。
        if self.cfg.typing_interval > 0:
            key = (cid, me["id"])
            now = now_ts()
            with self._typing_lock:
                last_ts, last_value = self._typing_seen.get(key, (0.0, None))
                if last_value == typing and now - last_ts < self.cfg.typing_interval:
                    return {"ok": True, "throttled": True}
                self._typing_seen[key] = (now, typing)
                if len(self._typing_seen) > 8192:       # 兜底清理，别让它无限长大
                    for stale in [k for k, v in list(self._typing_seen.items())
                                  if now - v[0] > 3600.0]:
                        self._typing_seen.pop(stale, None)
        name = me["display_name"] or me["username"]
        for uid in self.member_ids(cid):
            if uid == me["id"]:
                continue
            self.bus.push(uid, {
                "kind": "typing",
                "conversation_id": cid,
                "user_id": me["id"],
                "user_name": name,
                "typing": typing,
            })
        return {"ok": True}

    # ---------------- 用户搜索 ----------------
    def search_users(self, me, query):
        if query is None or not query.strip():
            rows = self.db.query(
                "SELECT * FROM users WHERE id<>? ORDER BY last_seen DESC LIMIT 20", (me["id"],)
            )
            return [self.user_dict(r) for r in rows]
        q = query.strip().lower()
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.db.query(
            "SELECT * FROM users WHERE id<>? AND (lower(username) LIKE ? ESCAPE '\\' "
            "OR lower(display_name) LIKE ? ESCAPE '\\') ORDER BY last_seen DESC LIMIT 20",
            (me["id"], like, like),
        )
        return [self.user_dict(r) for r in rows]

    # ---------------- 附件 ----------------
    def used_bytes(self, user_id):
        """这个账号的附件一共占了多少字节。"""
        return int(self.db.scalar(
            "SELECT COALESCE(SUM(size), 0) FROM attachments WHERE owner_id=?", (user_id,),
            default=0) or 0)

    def save_upload(self, me, name, blob):
        # 名字是发送者给的，会原样进 Content-Disposition 和客户端的落地路径 —— 先洗干净
        name = safe_filename(name)
        quota = int(self.cfg.user_quota_mb * 1048576)
        if quota > 0:
            used = self.used_bytes(me["id"])
            if used + len(blob) > quota:
                fail("quota_exceeded", "你的附件配额已满（上限 %.0f MB，已用 %.1f MB）"
                     % (self.cfg.user_quota_mb, used / 1048576.0))
        att_id = self._new_id("f_", "attachments")
        path = os.path.join(self.cfg.files_dir, att_id)
        tmp = path + ".part"
        # 附件字节也加密落盘（size 列存的仍是原始大小，客户端看到的"文件多大"不变）
        payload = self.storage.encrypt_bytes(blob) if self.storage.available else blob
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        now = now_ts()
        self.db.execute(
            "INSERT INTO attachments(id, owner_id, name, size, created_at) VALUES(?,?,?,?,?)",
            (att_id, me["id"], self._seal(name), len(blob), now),
        )
        return {"id": att_id, "name": name, "size": len(blob)}

    def cleanup_orphan_attachments(self, days, limit=500):
        """清掉"上传了但没有任何消息引用"且放了超过 N 天的附件（数据库行 + 磁盘字节）。

        为什么需要：上传接口先落地文件、再由发消息引用；中途失败（客户端崩了、用户
        取消了）就会留下永远不会被引用的孤儿。没有回收的话磁盘只涨不跌。
        返回清掉了几个。
        """
        if days <= 0:
            return 0
        deadline = now_ts() - days * 86400.0
        rows = self.db.query(
            "SELECT id FROM attachments WHERE created_at < ? AND NOT EXISTS "
            "(SELECT 1 FROM messages m WHERE m.attachment_id = attachments.id) LIMIT ?",
            (deadline, int(limit)),
        )
        removed = 0
        for row in rows:
            self.db.execute("DELETE FROM attachments WHERE id=?", (row["id"],))
            try:
                os.remove(os.path.join(self.cfg.files_dir, row["id"]))
            except OSError:
                pass
            removed += 1
        return removed

    def attachment_path(self, me, att_id):
        row = self.db.query_one("SELECT * FROM attachments WHERE id=?", (att_id,))
        if row is None:
            fail("not_found", "附件不存在")
        if row["owner_id"] != me["id"]:
            allowed = self.db.query_one(
                "SELECT 1 FROM messages m JOIN conv_members cm ON cm.conversation_id=m.conversation_id "
                "WHERE m.attachment_id=? AND cm.user_id=? LIMIT 1",
                (att_id, me["id"]),
            )
            if allowed is None:
                fail("not_member", "你无权下载这个附件")
        path = os.path.join(self.cfg.files_dir, att_id)
        if not os.path.isfile(path):
            fail("not_found", "附件文件已丢失")
        return row, path


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mchat-server/" + VERSION
    sys_version = ""
    timeout = 120  # 空闲连接超时，避免线程被吊死

    # ---- 日志 ----
    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[mchat] %s - %s\n" % (self.address_string(), fmt % args))
            sys.stderr.flush()

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)

    # ---- 入口 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # ---- 响应工具 ----
    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _bytes(self, status, blob, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", self._header_safe(content_type))
        self.send_header("Content-Length", str(len(blob)))
        for key, value in (extra_headers or []):
            self.send_header(key, self._header_safe(value))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(blob)

    @staticmethod
    def _header_safe(value):
        """响应头里绝不允许出现 CR/LF —— 一旦漏进去就是响应拆分（实测过：附件名带 CRLF
        能在下载响应里塞进 Set-Cookie 和第二个 Content-Length）。这里是最后一道兜底，
        所有拼进头部的值都过一遍。"""
        return str(value).replace("\r", "").replace("\n", "").replace("\x00", "")

    # ---- 请求体 ----
    def _content_length(self):
        raw = self.headers.get("Content-Length")
        if raw is None:
            fail("invalid_request", "缺少 Content-Length")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            fail("invalid_request", "Content-Length 不合法")
        if length < 0:
            fail("invalid_request", "Content-Length 不合法")
        return length

    def _drain(self, length):
        left = length
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            left -= len(chunk)

    def _read_json(self):
        length = self._content_length()
        if length > JSON_BODY_LIMIT:
            self._drain(min(length, DRAIN_LIMIT))
            fail("too_large", "请求体太大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            fail("invalid_request", "JSON 解析失败")
        if data is None:
            return {}
        if not isinstance(data, dict):
            fail("invalid_request", "请求体必须是 JSON 对象")
        return data

    def _read_raw(self, limit):
        length = self._content_length()
        if length > limit:
            self._drain(min(length, DRAIN_LIMIT))
            fail("too_large", "文件超过 --max-upload-mb 限制（%.1f MB）" % (limit / 1048576.0))
        blob = b""
        left = length
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                fail("invalid_request", "上传中断")
            blob += chunk
            left -= len(chunk)
        return blob

    # ---- 鉴权 ----
    def _token_from_header(self):
        """**只**从 Authorization 头取 token。

        刻意不再支持 ``?token=``：URL 会进反代的 access log、浏览器历史、报错日志，
        把一个长效令牌放进 URL 等于到处撒一份（实测过 --verbose 时整串 token 会落进日志）。
        真需要"把链接直接扔给浏览器"的场合，正确做法是服务端签发一次性短链，
        而不是把会话令牌复用成 URL 参数。
        """
        raw = self.headers.get("Authorization") or ""
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return None

    def _require(self):
        return self.server.app.auth(self._token_from_header())

    def _client_ip(self):
        """这次请求算哪个 IP 发来的（限流用）。

        默认只认 TCP 对端地址：直接暴露在公网时，伪造 X-Forwarded-For 就能白拿
        一份新的限流配额。放在 nginx/caddy 后面时用 ``--trust-proxy`` 显式打开。
        """
        if self.server.app.cfg.trust_proxy:
            for key in ("X-Real-IP", "X-Forwarded-For"):
                value = self.headers.get(key)
                if value:
                    return value.split(",")[0].strip()
        return self.client_address[0]

    # ---- 登录/注册的闸门 ----
    def _guard_auth_attempt(self, username):
        """动手验证密码之前的两道闸：每 IP 的频率 + 每账号的连续失败锁定。"""
        limiter = self.server.app.limiter
        if not limiter.allow(self._client_ip()):
            fail("too_many_requests", "尝试太频繁，请等一会儿再试")
        if username:
            remaining = limiter.locked_for(username)
            if remaining > 0:
                fail("too_many_requests",
                     "这个账号连续登录失败次数过多，请 %.0f 秒后再试" % remaining)

    def _guarded_login(self, app):
        body = self._read_json()
        raw_name = body.get("username")
        username = raw_name.strip().lower() if isinstance(raw_name, str) else ""
        self._guard_auth_attempt(username)
        try:
            result = app.login(body)
        except ApiError as exc:
            if exc.code == "bad_credentials":
                app.limiter.note_failure(username)
            raise
        app.limiter.note_success(username)
        return result

    def _guarded_register(self, app):
        # 注册只挡频率（每 IP 每窗口几次）：账号还不存在，没有"连续失败"可谈
        self._guard_auth_attempt("")
        return app.register(self._read_json())

    # ---- 路由 ----
    def _dispatch(self, method):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            while len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            parts = [p for p in path.split("/") if p]
            self._route(method, parts, query)
        except ApiError as exc:
            self._safe_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让一个请求打死线程
            traceback.print_exc()
            # **不要**把 str(exc) 回给客户端：那是内部实现细节（路径、SQL、异常类型），
            # 运维看服务端日志就够了，客户端只需要知道"服务端出错了"。
            self._safe_json(500, {"error": {"code": "server_error",
                                            "message": "服务端内部错误"}})

    def _safe_json(self, status, payload):
        try:
            self._json(status, payload)
        except Exception:
            self.close_connection = True

    def _route(self, method, parts, query):
        app = self.server.app
        # /api/health
        if parts == ["api", "health"] and method in ("GET", "HEAD"):
            users = app.db.scalar("SELECT COUNT(*) FROM users", (), 0)
            messages = app.db.scalar("SELECT COUNT(*) FROM messages", (), 0)
            return self._json(200, {
                "ok": True,
                "name": SERVER_NAME,
                "version": VERSION,
                "time": now_ts(),
                "users": int(users or 0),
                "messages": int(messages or 0),
            })

        if parts[:1] != ["api"]:
            fail("not_found", "没有这个接口")

        # /api/files/<id>
        if len(parts) == 3 and parts[1] == "files" and method in ("GET", "HEAD"):
            me = self._require()
            row, path = app.attachment_path(me, parts[2])
            with open(path, "rb") as fh:
                blob = fh.read()
            try:
                # 磁盘上是密文 → 交给客户端之前还原成原始字节
                blob = app.storage.decrypt_bytes(blob)
            except StorageKeyError as exc:
                fail("server_error", "附件解不开（磁盘加密的密钥不对？）：%s" % exc)
            # 再洗一次：加固之前入库的名字可能带 CR/LF 或路径成分，响应头不能直接用它
            name = safe_filename(app._unseal(row["name"], "附件"))
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            ascii_name = (name.encode("ascii", "replace").decode("ascii")
                          .replace('"', "_").replace("\\", "_"))
            quoted = urllib.parse.quote(name)
            headers = [(
                "Content-Disposition",
                'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, quoted),
            )]
            return self._bytes(200, blob, ctype, headers)

        if method == "POST":
            if parts == ["api", "register"]:
                return self._json(200, self._guarded_register(app))
            if parts == ["api", "login"]:
                return self._json(200, self._guarded_login(app))
            if parts == ["api", "logout"]:
                app.auth(self._token_from_header())
                return self._json(200, app.logout(self._token_from_header()))
            if parts == ["api", "upload"]:
                me = self._require()
                names = query.get("name")
                name = names[0] if names else "file"
                blob = self._read_raw(int(app.cfg.max_upload_mb * 1048576))
                return self._json(200, {"attachment": app.save_upload(me, name, blob)})
            if parts == ["api", "conversations", "direct"]:
                me = self._require()
                body = self._read_json()
                username = body.get("username")
                if not isinstance(username, str) or not username.strip():
                    fail("invalid_request", "需要 username")
                conv, _created = app.create_direct(me, username)
                return self._json(200, {"conversation": conv})
            if parts == ["api", "conversations", "group"]:
                me = self._require()
                body = self._read_json()
                conv = app.create_group(me, body.get("title"), body.get("usernames"))
                return self._json(200, {"conversation": conv})
            if parts == ["api", "friends", "request"]:
                me = self._require()
                body = self._read_json()
                return self._json(200, app.request_friend(me, body.get("username"), body.get("note")))
            if parts == ["api", "friends", "accept"]:
                me = self._require()
                body = self._read_json()
                return self._json(200, app.accept_friend(me, body.get("username")))
            if parts == ["api", "friends", "reject"]:
                me = self._require()
                body = self._read_json()
                return self._json(200, app.reject_friend(me, body.get("username")))
            if parts == ["api", "friends", "remove"]:
                me = self._require()
                body = self._read_json()
                return self._json(200, app.remove_friend(me, body.get("username")))
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "messages":
                me = self._require()
                body = self._read_json()
                # 单聊里对方不是好友时 message 是 null、多一个 friend_request（见 PROTOCOL.md §3.4）
                message, _created, friend_request = app.post_message(me, parts[2], body)
                payload = {"message": message}
                if friend_request is not None:
                    payload["friend_request"] = friend_request
                return self._json(200, payload)
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "read":
                me = self._require()
                body = self._read_json()
                return self._json(200, app.mark_read(me, parts[2], body.get("up_to_ts")))
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "typing":
                me = self._require()
                body = self._read_json()
                return self._json(200, app.send_typing(me, parts[2], body.get("typing")))
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "reactions":
                # 消息回应（表情）：服务端算计数，然后推给所有人
                me = self._require()
                body = self._read_json()
                return self._json(200, app.toggle_reaction(me, parts[2], body.get("message_id"),
                                                          body.get("emoji"), body.get("added")))
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "pin":
                # 会话内置顶（"双方置顶"）：服务端记下来并推给所有人
                me = self._require()
                body = self._read_json()
                return self._json(200, app.set_pin(me, parts[2], body.get("message_id"),
                                                   body.get("pinned")))
            fail("not_found", "没有这个接口")

        if method in ("GET", "HEAD"):
            if parts == ["api", "me"]:
                me = self._require()
                return self._json(200, {"user": app.user_dict(me), "server_time": now_ts()})
            if parts == ["api", "users"]:
                me = self._require()
                values = query.get("query")
                users = app.search_users(me, values[0] if values else "")
                return self._json(200, {"users": users})
            if parts == ["api", "conversations"]:
                me = self._require()
                return self._json(200, {"conversations": app.list_conversations(me["id"])})
            if parts == ["api", "friends"]:
                me = self._require()
                return self._json(200, app.list_friends(me))
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "messages":
                me = self._require()
                app.ensure_member(parts[2], me["id"])
                before = query.get("before")
                limit = query.get("limit")
                messages, has_more = app.list_messages(
                    me["id"],
                    parts[2],
                    before[0] if before else None,
                    limit[0] if limit else None,
                )
                return self._json(200, {"messages": messages, "has_more": has_more})
            if parts == ["api", "sync"]:
                me = self._require()
                cursor_values = query.get("cursor")
                cursor = cursor_values[0] if cursor_values else 0
                timeout_values = query.get("timeout")
                timeout = 20.0
                if timeout_values:
                    try:
                        timeout = float(timeout_values[0])
                    except (TypeError, ValueError):
                        fail("invalid_request", "timeout 必须是数字")
                timeout = max(0.0, min(float(app.cfg.long_poll_max), timeout))
                events, new_cursor = app.bus.sync(me["id"], cursor, timeout)
                return self._json(200, {
                    "events": events,
                    "cursor": new_cursor,
                    "server_time": now_ts(),
                })
            fail("not_found", "没有这个接口")

        fail("not_found", "没有这个接口")


def _build_busy_response():
    """连接数超限时回的那一坨字节（503 + 统一错误格式）。"""
    body = json.dumps(
        {"error": {"code": "too_many_requests", "message": "服务器忙，请稍后再试"}},
        ensure_ascii=False).encode("utf-8")
    head = ("HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n" % len(body)).encode("latin-1")
    return head + body


class Server(ThreadingHTTPServer):
    """线程池无上限的 HTTPServer 会被几十个慢连接拖死，所以在这里加一道闸：
    同时在处理的连接数超过 ``--max-connections`` 就立刻回 503，不做任何等待。

    这不是"完美抗 DDoS"（真要抗得靠反代/防火墙），而是让"一个脚本开几千个 socket
    挂在那里"这种最常见的低成本玩法打不穿正常用户。
    """

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128
    # 连接数满了时的固定回应（Content-Length 由代码算出来，别手数）
    BUSY_RESPONSE = _build_busy_response()

    def __init__(self, addr, handler, app):
        self.app = app
        self.verbose = app.cfg.verbose
        limit = int(app.cfg.max_connections)
        self._slots = threading.BoundedSemaphore(limit) if limit > 0 else None
        ThreadingHTTPServer.__init__(self, addr, handler)

    def process_request(self, request, client_address):
        if self._slots is not None and not self._slots.acquire(blocking=False):
            try:
                # 先把这个连接上已经到达的字节读掉再关：带着"未读数据"直接 close 会让
                # 对端收到 RST，客户端可能连 503 都读不到。给 50ms 就够（正常客户端
                # 请求早就到了；慢连接本来就是要被丢掉的）。
                request.settimeout(0.05)
                request.recv(65536)
            except OSError:
                pass
            try:
                request.sendall(self.BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except BaseException:
            self._release_slot()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self._release_slot()

    def _release_slot(self):
        if self._slots is None:
            return
        try:
            self._slots.release()
        except ValueError:              # 多放了一次也不该把服务端搞崩
            pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        sys.stderr.write("[mchat] 处理请求出错 %s: %r\n" % (client_address, exc))
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# 后台维护线程
# ---------------------------------------------------------------------------
def _presence_watcher(app, stop_event, interval=5.0):
    """每几秒扫一遍"谁上线/下线"，推给跟他有共同会话的人。

    为什么不能只在拉会话列表时算一次：那样对方的状态要等下一次重拉才更新
    （表现就是"在线/离线不变，还是刚登录时那个状态"）。
    第一次扫只用来建立基线，不推 —— 免得服务端一重启就群发一轮"全体在线"。
    """
    seen = {}
    baseline = True
    while not stop_event.wait(interval):
        try:
            changed = app.presence_sweep(seen, announce=not baseline)
            baseline = False
            if changed and app.cfg.verbose:
                sys.stderr.write("[mchat] 在线状态变化：%d 人\n" % changed)
                sys.stderr.flush()
        except Exception:
            traceback.print_exc()


def _janitor(app, stop_event, interval=300.0):
    """每 5 分钟收拾一次：过期 token + 没人引用的孤儿附件。"""
    db = app.db
    while not stop_event.wait(interval):
        try:
            with db.write() as conn:
                cur = conn.execute("DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at <= ?", (now_ts(),))
                removed = cur.rowcount
            orphans = app.cleanup_orphan_attachments(app.cfg.orphan_days)
            if (removed or orphans) and db.cfg.verbose:
                sys.stderr.write("[mchat] 清理：过期 token %d 条，孤儿附件 %d 个\n" % (removed, orphans))
                sys.stderr.flush()
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="mchat_server.py",
        description="MChat 服务端：零第三方依赖的 HTTP + JSON 聊天后端（协议见 PROTOCOL.md）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python3 mchat_server.py --host 0.0.0.0 --port 8765 --data /var/lib/mchat",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（对外服务用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                        help="数据目录（数据库 + 附件），默认是本文件旁边的 data/")
    parser.add_argument("--token-ttl-days", type=float, default=30.0,
                        help="登录 token 有效期（天），默认 30；<= 0 表示永不过期")
    parser.add_argument("--max-upload-mb", type=float, default=64.0,
                        help="单个附件大小上限（MB），默认 64（和客户端自带的 64MB 上限对齐）")
    parser.add_argument("--user-quota-mb", type=float, default=512.0,
                        help="每个账号的附件总容量上限（MB），默认 512；超了回 413 quota_exceeded")
    parser.add_argument("--orphan-days", type=float, default=7.0,
                        help="没被任何消息引用的附件保留几天后清掉，默认 7；0 = 不清理")
    parser.add_argument("--max-text-chars", type=int, default=20000,
                        help="单条消息正文长度上限，默认 20000 字符")
    parser.add_argument("--max-merged", type=int, default=MAX_MERGED_ITEMS,
                        help="合并转发一条消息最多带几条，默认 %d" % MAX_MERGED_ITEMS)
    parser.add_argument("--max-reactions", type=int, default=MAX_REACTIONS_PER_USER,
                        help="一个人在同一条消息上最多挂几个表情，默认 %d" % MAX_REACTIONS_PER_USER)
    parser.add_argument("--max-pins", type=int, default=MAX_PINS_PER_CONV,
                        help="一个会话最多同时置顶几条，默认 %d" % MAX_PINS_PER_CONV)
    parser.add_argument("--max-group-members", type=int, default=MAX_GROUP_MEMBERS,
                        help="一个群最多几个人（含自己），默认 %d" % MAX_GROUP_MEMBERS)
    parser.add_argument("--typing-interval", type=float, default=1.0,
                        help="同一会话里「正在输入」事件的最小间隔（秒），默认 1；0 = 不节流")
    parser.add_argument("--storage-key-file", default="",
                        help="**磁盘加密**的密钥文件（32 字节随机；要放在数据目录**外面**）。"
                             "给了它，消息正文/附件文件名/附件字节在磁盘上就是密文；"
                             "不给就是明文（启动日志里会明说）。也可以用环境变量 "
                             "MCHAT_STORAGE_KEY_FILE 传。需要 cryptography 或 pycryptodome 后端")
    parser.add_argument("--long-poll-max", type=int, default=30, help="/api/sync 允许的最长挂起秒数，默认 30")
    parser.add_argument("--no-register", action="store_true", help="关闭自助注册（只留已有账号登录）")
    parser.add_argument("--invite-code", default="", help="注册邀请码，设置后 /api/register 必须带 invite 字段")
    parser.add_argument("--open-register", action="store_true",
                        help="明确允许任何人自助注册（不设邀请码）。都不指定时服务端会自己生成一个"
                             "邀请码并打在启动日志里 —— 也就是默认不对外开放注册")
    parser.add_argument("--login-rate", type=int, default=12,
                        help="每 IP 每 %d 秒允许的登录/注册次数，默认 12；0 = 不限流" % int(LOGIN_WINDOW))
    parser.add_argument("--lock-after", type=int, default=5,
                        help="同一账号连续登录失败几次后锁定，默认 5；0 = 不锁")
    parser.add_argument("--lock-seconds", type=float, default=60.0,
                        help="账号锁定时长（秒），默认 60")
    parser.add_argument("--trust-proxy", action="store_true",
                        help="放在 nginx/caddy 后面时打开：按 X-Real-IP / X-Forwarded-For 认客户端 IP。"
                             "直接暴露到公网时**不要**开（否则伪造这个头就能绕过限流）")
    parser.add_argument("--max-connections", type=int, default=200,
                        help="同时在处理的连接数上限，默认 200；超了立刻回 503"
                             "（挡住慢连接把线程耗光）；0 = 不限")
    parser.add_argument("--online-window", type=float, default=60.0,
                        help="last_seen 在这个秒数内算「在线」，默认 60（要大于客户端心跳间隔）")
    parser.add_argument("--presence-interval", type=float, default=5.0,
                        help="每隔几秒扫一次「谁上线/下线」并推给相关的人，默认 5；0 = 关闭推送")
    parser.add_argument("--verbose", action="store_true", help="打印每条请求的访问日志")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = Config(args)
    except StorageKeyError as exc:
        # 配了磁盘加密却用不了：**起不来**比"以为加密了其实没加密"安全
        sys.stderr.write("[mchat] 启动失败（磁盘加密配置有问题）：\n%s\n" % exc)
        return 2
    os.makedirs(cfg.files_dir, exist_ok=True)

    db = Database(cfg)
    bus = EventBus(db)
    app = App(db, bus, cfg)
    # 启用磁盘加密后的第一次启动：把以前明文存下来的内容就地改写成密文
    try:
        converted = app.reencrypt_plaintext()
    except Exception:
        traceback.print_exc()
        converted = 0

    # 自动生成的邀请码要落库：不然服务端一重启码就变了，先拿到码的人反而注册不了
    if cfg.invite_generated:
        stored = db.get_meta("invite_code")
        if stored:
            cfg.invite_code = stored
        else:
            db.set_meta("invite_code", cfg.invite_code)

    try:
        httpd = Server((cfg.host, cfg.port), Handler, app)
    except OSError as exc:
        sys.stderr.write("[mchat] 启动失败：无法监听 %s:%d —— %s\n" % (cfg.host, cfg.port, exc))
        return 2

    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    users = db.scalar("SELECT COUNT(*) FROM users", (), 0)
    print("[mchat] %s v%s 已启动  http://%s:%d" % (SERVER_NAME, VERSION, bound_host, bound_port), flush=True)
    print("[mchat] 数据目录 %s（数据库 %s）" % (cfg.data_dir, os.path.basename(cfg.db_path)), flush=True)
    print("[mchat] 已有账号 %s 个，注册%s" % (
        users,
        "已开放（--open-register，谁都能注册！）" if cfg.allow_register and not cfg.invite_code else (
            "需邀请码" if cfg.invite_code else "已关闭（--no-register）"),
    ), flush=True)
    if cfg.invite_code:
        print("[mchat] ★ 注册邀请码：%s   （客户端注册时填在「邀请码」里；%s）" % (
            cfg.invite_code,
            "本次自动生成，已存在库里，重启不变" if cfg.invite_generated else "来自 --invite-code"),
            flush=True)
    print("[mchat] 登录 token 有效期 %s；每 IP 每 %d 秒最多 %s 次登录/注册；" % (
        "%.0f 天" % cfg.token_ttl_days if cfg.token_ttl_days > 0 else "永不过期",
        int(LOGIN_WINDOW),
        "不限" if cfg.login_rate <= 0 else str(cfg.login_rate)), flush=True)
    print("[mchat] 同时连接上限 %s；单条消息上限 %d 字符；每账号附件配额 %.0f MB；typing 节流 %.1fs" % (
        "不限" if cfg.max_connections <= 0 else str(cfg.max_connections),
        cfg.max_text_chars, cfg.user_quota_mb, cfg.typing_interval), flush=True)
    print("[mchat] 单条消息上限：%d 字符 / 合并转发 %d 条 / 单条消息表情 %d 个；"
          "每个会话最多置顶 %d 条；每个群最多 %d 人" % (
              cfg.max_text_chars, cfg.max_merged, cfg.max_reactions,
              cfg.max_pins, cfg.max_group_members), flush=True)
    # 上传是"整段读进内存"再落盘的：并发上限 × 单文件上限就是最坏情况的内存占用。
    # 默认 200 × 64MB = 12.8GB，机器小的话必须调（这条提示比事后 OOM 便宜多了）。
    worst_case_mb = cfg.max_connections * cfg.max_upload_mb if cfg.max_connections > 0 else 0
    if worst_case_mb > 2048:
        print("[mchat] ⚠ 内存提醒：--max-connections %d × --max-upload-mb %.0f = 最坏 %.1f GB "
              "（上传是整段进内存的）。小内存机器请调小其中一个。" % (
                  cfg.max_connections, cfg.max_upload_mb, worst_case_mb / 1024.0), flush=True)
    print("[mchat] %s" % cfg.storage.describe(), flush=True)
    if converted:
        print("[mchat] 已把 %d 处旧的明文内容改写成密文（并 VACUUM 收干净）" % converted,
              flush=True)
    print("[mchat] 令牌只存哈希（库里没有可用的明文令牌）", flush=True)
    migrated_pairs = int(getattr(db, "migrated_friend_pairs", 0) or 0)
    if migrated_pairs:
        print("[mchat] 已把 %d 对存量单聊追认为好友（新门禁不会冻结老会话）" % migrated_pairs,
              flush=True)

    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        if stop_event.is_set():
            return
        stop_event.set()
        print("[mchat] 收到退出信号（%s），正在优雅关闭…" % _signal_name(signum), flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError, RuntimeError):
            pass

    janitor = threading.Thread(target=_janitor, args=(app, stop_event), name="mchat-janitor")
    janitor.daemon = True
    janitor.start()

    if cfg.presence_interval > 0:
        presence = threading.Thread(target=_presence_watcher,
                                    args=(app, stop_event, cfg.presence_interval),
                                    name="mchat-presence")
        presence.daemon = True
        presence.start()

    exit_code = 0
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        stop_event.set()
    except Exception:
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        stop_event.set()
        bus.flush()
        db.flush()
        db.close_all()
        print("[mchat] 已退出，数据已落盘（%s）" % cfg.data_dir, flush=True)
    return exit_code


def _signal_name(signum):
    for name in dir(signal):
        if name.startswith("SIG") and getattr(signal, name) == signum:
            return name
    return str(signum)


if __name__ == "__main__":
    sys.exit(main())
