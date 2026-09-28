"""算法层：Base64 编解码 + AES-256-CBC 加解密。

设计约定
--------
* 纯函数，不依赖任何 GUI，方便离线单测。
* 出错一律抛 :class:`CryptoError`，消息直接面向用户；界面层自己决定怎么展示。
  不再沿用 Test15/Test16 里返回 ``"ERROR_xxx"`` 魔法字符串的老做法 —— 那样
  既容易和真实数据混淆，也无法携带"到底错在哪"的信息。
* AES 默认用 **随机 IV**，并把版本魔术头 + IV 一起写进密文，因此同一条明文
  每次加密结果都不同（语义安全的基本要求）。同时保留读取 Test16 旧格式
  （固定 IV = b"1234567890123456"，无头部）的能力，老密文照样解得开。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad

__all__ = [
    "CryptoError",
    "CipherFormat",
    "MAGIC",
    "LEGACY_IV",
    "derive_key",
    "encode_base64",
    "decode_base64",
    "aes_encrypt",
    "aes_decrypt",
    "detect_format",
]

BLOCK_SIZE = AES.block_size          # 16
MAGIC = b"MCH1"                      # 新格式魔术头（4 字节）
LEGACY_IV = b"1234567890123456"      # Test16 使用的固定 IV
_HEADER_SIZE = len(MAGIC) + BLOCK_SIZE  # 魔术头 + IV = 20


class CryptoError(Exception):
    """算法层统一异常，``str(exc)`` 即用户可读的中文提示。"""


@dataclass(frozen=True)
class CipherFormat:
    """密文格式描述，用于界面提示。"""

    name: str
    detail: str


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def _b64_bytes(text: str) -> bytes:
    """把（可能带换行/空格的）Base64 文本还原成字节。"""
    compact = "".join(text.split())
    if not compact:
        raise CryptoError("没有可处理的 Base64 内容")
    padded = compact + "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("这不是合法的 Base64 文本") from exc


def _decrypt_cbc(key: bytes, iv: bytes, body: bytes) -> bytes:
    if not body or len(body) % BLOCK_SIZE:
        raise CryptoError("密文长度不是 16 字节的整数倍，可能已损坏")
    try:
        return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(body), BLOCK_SIZE)
    except (ValueError, KeyError) as exc:
        raise CryptoError("解密失败：密钥错误或密文已损坏") from exc


def _friendly_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CryptoError("结果不是合法的 UTF-8 文本（原文可能不是文字）") from exc


# ---------------------------------------------------------------------------
# Base64（Test15 的能力，做了更严格的校验）
# ---------------------------------------------------------------------------
def encode_base64(text: str) -> str:
    """UTF-8 文本 -> Base64 字符串。"""
    if not text:
        raise CryptoError("没有可编码的文本")
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_base64(text: str) -> str:
    """Base64 字符串 -> UTF-8 文本（自动忽略换行与空格，自动补 '='）。"""
    if not text or not text.strip():
        raise CryptoError("没有可解码的内容")
    return _friendly_utf8(_b64_bytes(text))


# ---------------------------------------------------------------------------
# AES-256-CBC（Test16 的能力 + 随机 IV）
# ---------------------------------------------------------------------------
def derive_key(secret_key: str) -> bytes:
    """SHA-256 把任意长度口令摊成 32 字节 AES-256 密钥。"""
    if not secret_key:
        raise CryptoError("密钥不能为空")
    return hashlib.sha256(secret_key.encode("utf-8")).digest()


def aes_encrypt(text: str, secret_key: str, *, legacy_iv: bool = False) -> str:
    """AES-256-CBC + PKCS7 加密，返回 Base64。

    ``legacy_iv=True`` 时退回 Test16 的固定 IV 且不写头部，
    产物可被老程序解开（代价是同口令同明文密文相同）。
    """
    if not text:
        raise CryptoError("没有可加密的文本")
    key = derive_key(secret_key)
    if legacy_iv:
        # 旧格式：IV 是双方约定的常量，不能写进密文
        iv, prefix = LEGACY_IV, b""
    else:
        # 新格式：随机 IV 随密文一起传递，前面再挂 4 字节版本头
        iv = get_random_bytes(BLOCK_SIZE)
        prefix = MAGIC + iv
    body = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(text.encode("utf-8"), BLOCK_SIZE))
    return base64.b64encode(prefix + body).decode("ascii")


def aes_decrypt(text: str, secret_key: str) -> str:
    """解密 ``aes_encrypt`` 的产物，并自动兼容 Test16 旧格式。"""
    if not text or not text.strip():
        raise CryptoError("没有可解密的密文")
    key = derive_key(secret_key)
    raw = _b64_bytes(text)
    if raw.startswith(MAGIC):
        if len(raw) < _HEADER_SIZE + BLOCK_SIZE:
            raise CryptoError("密文长度不合法，数据不完整")
        return _friendly_utf8(_decrypt_cbc(key, raw[4:_HEADER_SIZE], raw[_HEADER_SIZE:]))
    return _friendly_utf8(_decrypt_cbc(key, LEGACY_IV, raw))


def detect_format(text: str) -> CipherFormat | None:
    """判断一段密文是哪种格式（仅看 Base64 头部，不解密）。"""
    try:
        raw = _b64_bytes(text)
    except CryptoError:
        return None
    if raw.startswith(MAGIC):
        return CipherFormat("MChat v1 格式", "随机 IV + 版本头，每次密文都不同")
    if raw and len(raw) % BLOCK_SIZE == 0:
        return CipherFormat("Test16 兼容格式", "固定 IV，可与旧程序互通")
    return None
