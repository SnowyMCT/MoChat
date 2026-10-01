"""静态加密（encryption at rest）：本地存的东西一律不落明文。

要解决什么
----------
本地这份数据以前是**明文**的：历史库里的每句话、附件缓存里的每个字节、令牌缓存里的
那个令牌，谁拿到文件谁就能看（备份、云同步目录、同机器的另一个账号、被拆走的硬盘）。
这个模块就是"把它们变成密文"的唯一入口。

密钥怎么管（这是整件事的关键，光加密不管密钥等于没加密）
------------------------------------------------------
* 数据密钥（DEK）是 32 字节随机数，**只生成一次**，存在数据目录的 ``storage.key``；
* Windows 上用 **DPAPI**（``CryptProtectData``，当前用户 + 本机为界）把它包起来再落盘
  —— 换句话说：把文件拷到别的机器、别的 Windows 账号下，**解不开**；
  （DPAPI 走 ctypes 调系统 API，不引第三方包。）
* 其它平台退化成"密钥文件 + 0600 权限"，和以前保护令牌的方式同一档，但至少
  **单独泄漏一个库文件**（备份、误传、同步到网盘）时里面是密文；
* 密钥解不开（换了机器/账号）时**不静默重建**：抛 :class:`AtRestError`，由界面明确告诉
  用户"这份本地历史打不开了"，而不是假装没事去覆盖。

算法
----
AES-256-GCM（PyCryptodome，客户端本来就依赖它做工具箱），每条各用 12 字节随机 nonce，
带 AAD 把用途绑进密文 —— 拿历史库里的密文去冒充附件内容会直接校验失败。
密文一律带版本前缀，**旧明文照样能读**：

* 文本字段：``enc1:<base64(nonce‖密文)>``；没有这个前缀的当明文放行（老库兼容）；
* 文件：头部 ``MCHATENC1`` + 12 字节 nonce + 密文。

给界面用的那一步
----------------
加密的文件要交给系统默认程序打开，就必须先还原成明文 —— :func:`materialize` 负责这件事：
解密到 ``<临时目录>/mchat-view/`` 下（同名保留，0600），源文件变了会重新解。
**这一步必然在临时目录留下明文**（任何"加密存储 + 外部程序打开"的产品都是这样），
退出时会尽量清掉（见 :func:`cleanup_view_cache`）。

本模块能脱离界面单测（``tests/test_atrest.py``）。
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import tempfile

__all__ = ["AtRestError", "StorageCipher", "open_storage", "materialize",
           "cleanup_view_cache", "is_encrypted_file", "set_active_cipher",
           "active_cipher", "view_cache_dir", "seal_file_in_place", "TEXT_PREFIX",
           "FILE_MAGIC", "KEY_FILE_NAME", "VIEW_DIR_NAME"]

TEXT_PREFIX = "enc1:"            # 文本字段的版本前缀
FILE_MAGIC = b"MCHATENC1"        # 加密文件的头部魔术字
KEY_FILE_NAME = "storage.key"
VIEW_DIR_NAME = "mchat-view"     # 解密出来的临时明文放这儿（交给系统程序打开用）
KEY_BYTES = 32                   # AES-256
NONCE_BYTES = 12                 # GCM 标准 nonce
_DPAPI_ENTROPY = b"MChat at-rest v1"
_TEXT_AAD = b"mchat-field"
_FILE_AAD = b"mchat-file"


class AtRestError(Exception):
    """静态加密相关的错误，``str(exc)`` 即给用户看的中文说明。"""


# ---------------------------------------------------------------------------
# 密钥存储：Windows 走 DPAPI，其它平台退化成一个 0600 的密钥文件
# ---------------------------------------------------------------------------
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> "_DataBlob":
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi(protect: bool, data: bytes) -> bytes:
    """调系统 DPAPI 加/解封一段数据（只在 Windows 上用）。

    ``CryptProtectData`` / ``CryptUnprotectData`` 的签名一模一样（第 2 个参数在解密时
    是"谁加的密"，传 NULL 就行），所以这里一套参数用两次。
    """
    if os.name != "nt":
        raise AtRestError("DPAPI 只在 Windows 上可用")
    crypt32 = ctypes.windll.crypt32
    function = getattr(crypt32, "CryptProtectData" if protect else "CryptUnprotectData")
    source, entropy = _blob(data), _blob(_DPAPI_ENTROPY)
    result = _DataBlob()
    ok = function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0,
                  ctypes.byref(result))
    if not ok:
        raise AtRestError("系统拒绝%s这份密钥（换了 Windows 账号或换了机器？）"
                          % ("保护" if protect else "解开"))
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _wrap_key(raw: bytes) -> dict:
    """把数据密钥变成"可以落盘的一坨"。"""
    if os.name == "nt":
        blob = _dpapi(True, raw)
        return {"v": 1, "how": "dpapi", "key": base64.b64encode(blob).decode("ascii"),
                "check": _check_word(raw)}
    return {"v": 1, "how": "file", "key": base64.b64encode(raw).decode("ascii"),
            "check": _check_word(raw)}


def _unwrap_key(record: dict) -> bytes:
    how = str(record.get("how") or "")
    try:
        blob = base64.b64decode(str(record.get("key") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AtRestError("密钥文件坏了（不是合法的 base64）") from exc
    if how == "dpapi":
        raw = _dpapi(False, blob)
    elif how == "file":
        raw = blob
    else:
        raise AtRestError("认不出的密钥格式：%r" % how)
    if len(raw) != KEY_BYTES:
        raise AtRestError("密钥长度不对（期望 %d 字节，实际 %d）" % (KEY_BYTES, len(raw)))
    check = str(record.get("check") or "")
    if check and check != _check_word(raw):
        raise AtRestError("密钥不对（校验字对不上）—— 这份数据可能不是这台机器上存的")
    return raw


def _check_word(raw: bytes) -> str:
    """密钥的自校验字：用来区分"密钥文件被换了"和"密文坏了"。"""
    return hmac.new(raw, b"mchat-key-check", hashlib.sha256).hexdigest()[:16]


def _write_private(path: str, blob: bytes) -> None:
    """原子写 + 尽量收紧权限（Windows 上靠目录 ACL，POSIX 上是 0600）。"""
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(blob)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------
class StorageCipher:
    """一条账号（或一份库）对应一个实例：拿着数据密钥做加解密。

    ``available=False`` 时所有 encrypt 都退化成"原样返回"（= 不加密），
    这样"没装 PyCryptodome"不会让程序起不来，只会让它降级并**明确告诉你**。
    """

    def __init__(self, key: bytes | None, *, reason: str = ""):
        self.key = key
        self.reason = reason                 # 不可用时说明为什么
        self._aes = None
        if key:
            try:
                from Crypto.Cipher import AES          # PyCryptodome（客户端本来就依赖）
            except ImportError as exc:                 # pragma: no cover - 环境问题
                self.key = None
                self.reason = "没有可用的加密后端（PyCryptodome 没装上）：%s" % exc
            else:
                self._aes = AES

    # ---------------------------------------------------------------- 状态
    @property
    def available(self) -> bool:
        return self._aes is not None and self.key is not None

    def describe(self) -> str:
        """界面上/日志里那行字：现在到底是加密还是明文。"""
        if self.available:
            how = "Windows DPAPI（绑定当前账号与本机）" if os.name == "nt" else "密钥文件（0600）"
            return "本地存储已加密（AES-256-GCM，密钥由%s保护）" % how
        return "本地存储**未加密**：%s" % (self.reason or "没有密钥")

    # ---------------------------------------------------------------- 文本字段
    def encrypt_text(self, text: str | None) -> str | None:
        """把一个文本字段变成密文字符串（空值原样返回）。"""
        if text is None or text == "":
            return text
        if not self.available:
            return text
        return TEXT_PREFIX + self._seal(str(text).encode("utf-8"), _TEXT_AAD)

    def decrypt_text(self, text: str | None) -> str | None:
        """把密文字段还原；**没有前缀的当明文**（老库、以及没开加密时存的行）。"""
        if not text or not isinstance(text, str) or not text.startswith(TEXT_PREFIX):
            return text
        if not self.available:
            raise AtRestError("这条记录是加密的，但当前没有可用的密钥/加密后端")
        raw = self._open(text[len(TEXT_PREFIX):], _TEXT_AAD)
        return raw.decode("utf-8")

    # ---------------------------------------------------------------- 字节（附件）
    def encrypt_bytes(self, blob: bytes) -> bytes:
        if not self.available:
            return blob
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._aes.new(self.key, self._aes.MODE_GCM, nonce=nonce)
        sealed.update(_FILE_AAD)
        ciphertext, tag = sealed.encrypt_and_digest(blob)
        return FILE_MAGIC + nonce + tag + ciphertext

    def decrypt_bytes(self, blob: bytes) -> bytes:
        if not blob.startswith(FILE_MAGIC):
            return blob                       # 明文（老缓存 / 没开加密）
        if not self.available:
            raise AtRestError("这个文件是加密的，但当前没有可用的密钥/加密后端")
        body = blob[len(FILE_MAGIC):]
        if len(body) < NONCE_BYTES + 16:
            raise AtRestError("加密文件不完整")
        nonce, rest = body[:NONCE_BYTES], body[NONCE_BYTES:]
        tag, ciphertext = rest[:16], rest[16:]
        sealed = self._aes.new(self.key, self._aes.MODE_GCM, nonce=nonce)
        sealed.update(_FILE_AAD)
        try:
            return sealed.decrypt_and_verify(ciphertext, tag)
        except ValueError as exc:
            raise AtRestError("加密文件校验失败（密钥不对或文件被改过）") from exc

    def is_encrypted_text(self, value) -> bool:
        return isinstance(value, str) and value.startswith(TEXT_PREFIX)

    # ---------------------------------------------------------------- 内部
    def _seal(self, raw: bytes, aad: bytes) -> str:
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._aes.new(self.key, self._aes.MODE_GCM, nonce=nonce)
        sealed.update(aad)
        ciphertext, tag = sealed.encrypt_and_digest(raw)
        return base64.b64encode(nonce + tag + ciphertext).decode("ascii")

    def _open(self, encoded: str, aad: bytes) -> bytes:
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AtRestError("密文不是合法的 base64（文件被截断？）") from exc
        if len(blob) < NONCE_BYTES + 16:
            raise AtRestError("密文不完整")
        nonce, rest = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        tag, ciphertext = rest[:16], rest[16:]
        sealed = self._aes.new(self.key, self._aes.MODE_GCM, nonce=nonce)
        sealed.update(aad)
        try:
            return sealed.decrypt_and_verify(ciphertext, tag)
        except ValueError as exc:
            raise AtRestError("解密失败（密钥不对，或这条记录被改过）") from exc


def open_storage(data_dir: str, *, enabled: bool = True) -> StorageCipher:
    """打开（必要时创建）数据目录里的密钥，返回可用的 :class:`StorageCipher`。

    ``enabled=False`` 或环境变量 ``MCHAT_NO_AT_REST=1`` 时返回一个"不可用"的实例 ——
    那就是不加密（供排障/性能对照用），但 :meth:`StorageCipher.describe` 会说清楚。
    """
    if not enabled or os.environ.get("MCHAT_NO_AT_REST") == "1":
        return StorageCipher(None, reason="按配置关闭了静态加密")
    path = os.path.join(data_dir, KEY_FILE_NAME)
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
            if not isinstance(record, dict):
                raise AtRestError("密钥文件格式不对")
            return StorageCipher(_unwrap_key(record))
        key = secrets.token_bytes(KEY_BYTES)
        os.makedirs(data_dir, exist_ok=True)
        _write_private(path, _json_bytes(_wrap_key(key)))
        return StorageCipher(key)
    except AtRestError as exc:
        # 钥匙打不开（换了机器/账号、文件被改过）：**不重建**，如实上报让界面说清楚
        return StorageCipher(None, reason=str(exc))
    except (OSError, ValueError) as exc:
        return StorageCipher(None, reason="密钥文件不可读写：%s" % exc)


def _json_bytes(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 给界面用：把加密的附件还原成"能交给系统程序打开"的明文
# ---------------------------------------------------------------------------
def is_encrypted_file(path: str) -> bool:
    """看文件头部是不是我们写的加密格式（只读 9 个字节，不留明文）。"""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(FILE_MAGIC)) == FILE_MAGIC
    except OSError:
        return False


# 当前进程用的那一把钥匙。界面（图片预览、点开附件、打开所在文件夹）都要用它，
# 与其把 cipher 一路传到每个控件，不如在这里留一个"当前生效"的引用 ——
# 单进程桌面程序，一个进程只会登一个账号，一个引用是够的，也省下十几处参数。
_ACTIVE: "StorageCipher | None" = None


def set_active_cipher(cipher: "StorageCipher | None") -> None:
    """由启动器在登录后设置；登出/换账号时会跟着换。"""
    global _ACTIVE
    _ACTIVE = cipher


def active_cipher() -> "StorageCipher | None":
    return _ACTIVE


def view_cache_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), VIEW_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def materialize(path: str, cipher: StorageCipher | None = None, *,
                folder: str | None = None) -> str:
    """返回一个**明文**文件路径（加密的就解密到临时目录，没加密的直接返回原路径）。

    名字里带上原文件名（后缀要留住，系统靠它挑打开方式）；前面挂一段短的路径哈希，
    避免两个同名文件互相覆盖。``cipher`` 不传就用当前生效的那把（见
    :func:`set_active_cipher`）。
    """
    cipher = cipher or active_cipher()
    if not path or not os.path.isfile(path):
        return path
    if cipher is None or not cipher.available:
        return path
    if not is_encrypted_file(path):
        return path                          # 老缓存里的明文文件：照旧用
    target_dir = folder or view_cache_dir()
    os.makedirs(target_dir, exist_ok=True)
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
    out = os.path.join(target_dir, "%s-%s" % (digest, os.path.basename(path)))
    try:
        if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out                       # 已经解过一次，直接用
        with open(path, "rb") as handle:
            blob = handle.read()
        plain = cipher.decrypt_bytes(blob)
        _write_private(out, plain)
    except (OSError, AtRestError):
        raise
    return out


def seal_file_in_place(path: str, cipher: StorageCipher | None = None) -> bool:
    """把一个**已经写好的明文文件**就地在原路径上换成密文（返回是否真的换了）。

    给"程序自己刚生成的中间文件"用（比如粘进来的图片）：写的时候没法直接加密
    （Qt 的 ``QImage.save`` 只认路径），那就写完之后立刻盖上。
    """
    cipher = cipher or active_cipher()
    if not path or cipher is None or not cipher.available or not os.path.isfile(path):
        return False
    if is_encrypted_file(path):
        return False
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
        _write_private(path, cipher.encrypt_bytes(blob))
    except OSError:
        return False
    return True


def cleanup_view_cache() -> int:
    """退出时清掉临时明文（best effort）。返回删掉几个文件。"""
    folder = os.path.join(tempfile.gettempdir(), VIEW_DIR_NAME)
    removed = 0
    try:
        for name in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    except OSError:
        return 0
    return removed
