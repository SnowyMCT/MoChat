"""算法层单测。

不依赖 pytest：直接 ``python tests/test_crypto.py`` 就能跑；
装了 pytest 也可以 ``pytest tests``。

重点验证三件事：
1. Base64 / AES 自身的往返正确；
2. 与 Test16.py 的双向兼容（旧格式密文能解，旧程序能解我们的兼容密文）；
3. 异常路径给出的是可读错误，而不是静默返回错误值。
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Crypto.Cipher import AES                                    # noqa: E402
from Crypto.Util.Padding import pad, unpad                       # noqa: E402

from mchat.core import crypto                                    # noqa: E402


# ---------------------------------------------------------------------------
# Test16.py / Test15.py 的原版实现（照抄来做参照物，用来做交叉验证）
#
# 注意命名：不要叫 test*，否则 pytest 会把它们当用例收集，看到带参数就报
# "fixture 'text' not found"。
# ---------------------------------------------------------------------------
TEST16_IV = b"1234567890123456"


def ref16_encrypt(text: str, secret_key: str) -> str:
    key = hashlib.sha256(secret_key.encode("utf-8")).digest()
    cipher = AES.new(key, AES.MODE_CBC, TEST16_IV)
    raw = pad(text.encode("utf-8"), AES.block_size)
    return base64.b64encode(cipher.encrypt(raw)).decode("utf-8")


def ref16_decrypt(text: str, secret_key: str) -> str:
    key = hashlib.sha256(secret_key.encode("utf-8")).digest()
    raw = base64.b64decode(text.encode("utf-8"))
    cipher = AES.new(key, AES.MODE_CBC, TEST16_IV)
    return unpad(cipher.decrypt(raw), AES.block_size).decode("utf-8")


def ref15_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def ref15_decode(text: str) -> str:
    return base64.b64decode(text.encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Base64
# ---------------------------------------------------------------------------
def test_base64_roundtrip():
    for text in ["hello", "中文 with 🎉 emoji", " 前后有空格 ", "a" * 500]:
        assert crypto.decode_base64(crypto.encode_base64(text)) == text


def test_base64_matches_test15():
    text = "MChat 合并原型"
    assert crypto.encode_base64(text) == ref15_encode(text)
    assert crypto.decode_base64(ref15_encode(text)) == ref15_decode(ref15_encode(text))


def test_base64_tolerates_whitespace_and_missing_padding():
    raw = crypto.encode_base64("宽松解析")
    assert crypto.decode_base64(f"  {raw[:4]}\n{raw[4:]}  ") == "宽松解析"
    unpadded = raw.rstrip("=")
    assert crypto.decode_base64(unpadded) == "宽松解析"


def test_base64_rejects_garbage():
    try:
        crypto.decode_base64("这显然不是 Base64!!!")
    except crypto.CryptoError as exc:
        assert "Base64" in str(exc)
    else:
        raise AssertionError("畸形输入应当抛 CryptoError")


def test_base64_rejects_non_utf8_bytes():
    # 0xFF 0xFE 是合法的 Base64 载荷，但不是合法 UTF-8 文本
    payload = base64.b64encode(b"\xff\xfe").decode()
    try:
        crypto.decode_base64(payload)
    except crypto.CryptoError as exc:
        assert "UTF-8" in str(exc)
    else:
        raise AssertionError("非 UTF-8 载荷应当抛 CryptoError")


# ---------------------------------------------------------------------------
# AES
# ---------------------------------------------------------------------------
def test_aes_roundtrip_various():
    for text in ["secret", "中文密文测试", "x" * 4096, "多行\n文本\n换行"]:
        cipher = crypto.aes_encrypt(text, "口令-123")
        assert crypto.aes_decrypt(cipher, "口令-123") == text


def test_aes_random_iv_is_actually_random():
    a = crypto.aes_encrypt("同一段明文", "同样密钥")
    b = crypto.aes_encrypt("同一段明文", "同样密钥")
    assert a != b, "两次加密结果相同，说明 IV 没有随机化"
    assert crypto.aes_decrypt(a, "同样密钥") == crypto.aes_decrypt(b, "同样密钥") == "同一段明文"


def test_aes_wrong_key_fails_loudly():
    cipher = crypto.aes_encrypt("机密", "正确口令")
    try:
        crypto.aes_decrypt(cipher, "错误口令")
    except crypto.CryptoError:
        pass
    else:
        raise AssertionError("错误密钥应当抛 CryptoError")


def test_aes_interop_with_test16():
    """双向兼容：解旧格式 + 产出旧格式。"""
    key = "shared-secret"
    old_cipher = ref16_encrypt("跨版本互通", key)
    assert crypto.aes_decrypt(old_cipher, key) == "跨版本互通"

    new_legacy = crypto.aes_encrypt("跨版本互通", key, legacy_iv=True)
    assert ref16_decrypt(new_legacy, key) == "跨版本互通"


def test_aes_legacy_self_roundtrip():
    """兼容格式也要能自己解自己（曾经因为把常量 IV 写进密文而失败）。"""
    legacy = crypto.aes_encrypt("兼容格式自检", "k", legacy_iv=True)
    assert crypto.aes_decrypt(legacy, "k") == "兼容格式自检"
    # 旧格式长度必须正好是 16 的整数倍：多出来的字节说明混进了头部
    assert len(base64.b64decode(legacy)) % 16 == 0, "旧格式密文里混入了额外字节"
    plain_len = len("兼容格式自检".encode())
    assert len(base64.b64decode(legacy)) == (plain_len // 16 + 1) * 16


def test_aes_format_detection():
    key = "k"
    assert crypto.detect_format(crypto.aes_encrypt("x", key)).name == "MChat v1 格式"
    assert crypto.detect_format(crypto.aes_encrypt("x", key, legacy_iv=True)).name == "Test16 兼容格式"
    assert crypto.detect_format("这不是密文") is None


def test_aes_rejects_truncated_ciphertext():
    cipher = crypto.aes_encrypt("完整密文", "k")
    try:
        crypto.aes_decrypt(cipher[:-12], "k")
    except crypto.CryptoError:
        pass
    else:
        raise AssertionError("截断的密文应当抛 CryptoError")


def test_empty_inputs_rejected():
    for call in (
        lambda: crypto.encode_base64(""),
        lambda: crypto.decode_base64("   "),
        lambda: crypto.aes_encrypt("", "k"),
        lambda: crypto.aes_decrypt("", "k"),
        lambda: crypto.aes_encrypt("x", ""),
        lambda: crypto.aes_decrypt("x", ""),
    ):
        try:
            call()
        except crypto.CryptoError:
            continue
        raise AssertionError("空输入应当抛 CryptoError")


# ---------------------------------------------------------------------------
# 独立运行器
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
