"""静态加密（encryption at rest）的回归测试，零 Qt 依赖。

两条主线：

1. **底座**（``mchat/core/atrest.py``）：密钥怎么存、文本/文件怎么加解密、
   老明文怎么兼容、密钥不对时是不是**明确失败**（而不是静默当明文）；
2. **落地**（本地历史库 ‵mchat/core/db.py`` / 令牌缓存 ``mchat/core/session.py``）：
   磁盘上到底是不是密文 —— 这里直接拿 sqlite3 / json 读原始文件来断言，
   而不是"读回来是明文"（那只能证明我解密对了，证明不了它存的是密文）。

用法：``.venv/Scripts/python.exe -m pytest tests/test_atrest.py -q``
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mchat.core import atrest                                              # noqa: E402
from mchat.core.atrest import (FILE_MAGIC, TEXT_PREFIX, AtRestError,      # noqa: E402
                               StorageCipher, cleanup_view_cache, is_encrypted_file,
                               materialize, open_storage, seal_file_in_place,
                               set_active_cipher)
from mchat.core.db import ChatLog                                          # noqa: E402
from mchat.core.models import Attachment, Message                          # noqa: E402
from mchat.core.session import Session, SessionStore                       # noqa: E402

SECRET = "周五下午三点在楼下等我，别告诉别人"
SECRET_BYTES = "这是附件里的字节".encode("utf-8")


def _msg(mid: str, text: str, ts: float = 1.0, *, conv: str = "c1",
         attachment: Attachment | None = None) -> Message:
    return Message(id=mid, conversation_id=conv, sender_id="u_1", text=text, ts=ts,
                   outgoing=False, attachment=attachment)


def _raw_column(db_path: str, sql: str, params=()):
    """绕过 ChatLog，直接读数据库里的原始值（要证明的就是"磁盘上是密文"）。"""
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


# =========================================================================== 底座
def test_key_is_created_once_and_reused(tmp_path):
    folder = str(tmp_path / "data")
    first = open_storage(folder)
    assert first.available, first.reason
    assert os.path.isfile(os.path.join(folder, atrest.KEY_FILE_NAME))
    # 同一目录再开一次：拿到的必须是同一把钥匙（否则之前存的数据全成乱码）
    second = open_storage(folder)
    assert second.available
    sealed = first.encrypt_text(SECRET)
    assert second.decrypt_text(sealed) == SECRET
    # 另一个目录是另一把钥匙：解不开（但会**明确报错**，不返回垃圾）
    other = open_storage(str(tmp_path / "elsewhere"))
    assert other.available
    with pytest.raises(AtRestError):
        other.decrypt_text(sealed)


def test_key_file_does_not_contain_the_key(tmp_path):
    """密钥文件里不该有明文密钥 —— Windows 上应当是 DPAPI 包过的一坨。"""
    folder = str(tmp_path / "data")
    cipher = open_storage(folder)
    assert cipher.available
    with open(os.path.join(folder, atrest.KEY_FILE_NAME), "rb") as handle:
        raw = handle.read()
    record = json.loads(raw.decode("utf-8"))
    assert record["how"] in ("dpapi", "file")
    if os.name == "nt":
        assert record["how"] == "dpapi", "Windows 上必须用 DPAPI 包住，不能裸放密钥"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 只在 Windows 上")
def test_dpapi_round_trip():
    sealed = atrest._dpapi(True, b"some-secret-bytes")
    assert sealed != b"some-secret-bytes"          # 真的封过了
    assert atrest._dpapi(False, sealed) == b"some-secret-bytes"


def test_broken_key_file_is_reported_and_not_overwritten(tmp_path):
    """密钥坏了就**如实上报**，绝不能悄悄重建一把（那等于把老数据全扔了）。"""
    folder = str(tmp_path / "data")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, atrest.KEY_FILE_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ 这不是合法的 JSON")
    cipher = open_storage(folder)
    assert not cipher.available
    assert "密钥" in cipher.reason or "不可读写" in cipher.reason
    with open(path, "r", encoding="utf-8") as handle:
        assert handle.read() == "{ 这不是合法的 JSON"      # 原文件没被覆盖


def test_text_and_bytes_round_trip():
    cipher = StorageCipher(b"k" * 32)
    assert cipher.available
    for text in (SECRET, "a", "emoji 👍🇨🇳", ""):
        sealed = cipher.encrypt_text(text)
        if text == "":
            assert sealed == ""
        else:
            assert sealed.startswith(TEXT_PREFIX)
            assert SECRET not in sealed
            assert cipher.decrypt_text(sealed) == text
    assert cipher.encrypt_text(None) is None
    blob = cipher.encrypt_bytes(SECRET_BYTES)
    assert blob.startswith(FILE_MAGIC) and SECRET_BYTES not in blob
    assert cipher.decrypt_bytes(blob) == SECRET_BYTES
    # 同一段明文两次加密结果不同（随机 nonce）
    assert cipher.encrypt_text(SECRET) != cipher.encrypt_text(SECRET)


def test_legacy_plaintext_is_passed_through():
    """老数据没有前缀 → 当明文读（升级不能把人的历史读没了）。"""
    cipher = StorageCipher(b"k" * 32)
    assert cipher.decrypt_text("老消息，明文存的") == "老消息，明文存的"
    assert cipher.decrypt_bytes("明文附件".encode("utf-8")) == "明文附件".encode("utf-8")


def test_tampered_or_wrong_key_fails_loudly():
    cipher = StorageCipher(b"k" * 32)
    sealed = cipher.encrypt_text(SECRET)
    # 换一把钥匙
    with pytest.raises(AtRestError):
        StorageCipher(b"x" * 32).decrypt_text(sealed)
    # 改一个字节
    body = sealed[len(TEXT_PREFIX):]
    broken = TEXT_PREFIX + body[:-4] + ("AAAA" if not body.endswith("AAAA") else "BBBB")
    with pytest.raises(AtRestError):
        cipher.decrypt_text(broken)
    # 拿一段密文冒充另一类内容（AAD 不同）→ 也要失败
    with pytest.raises(AtRestError):
        cipher.decrypt_bytes(FILE_MAGIC + __import__("base64").b64decode(body)[:200])


def test_unavailable_cipher_degrades_to_plaintext(tmp_path):
    """没有加密后端/没钥匙时：不加密，但**state 要说得清楚**。"""
    cipher = StorageCipher(None, reason="测试：故意不给钥匙")
    assert not cipher.available
    assert cipher.encrypt_text(SECRET) == SECRET          # 原样落盘
    assert not cipher.is_encrypted_text(cipher.encrypt_text(SECRET))
    assert "未加密" in cipher.describe()


def test_materialize_decrypts_to_a_temp_copy(tmp_path):
    cipher = StorageCipher(b"k" * 32)
    set_active_cipher(cipher)
    try:
        folder = str(tmp_path / "cache")
        os.makedirs(folder, exist_ok=True)
        enc_path = os.path.join(folder, "f_1-图纸.png")
        with open(enc_path, "wb") as handle:
            handle.write(cipher.encrypt_bytes(SECRET_BYTES))
        assert is_encrypted_file(enc_path)
        view = str(tmp_path / "view")
        plain = materialize(enc_path, cipher, folder=view)
        assert plain != enc_path and os.path.isfile(plain)
        with open(plain, "rb") as handle:
            assert handle.read() == SECRET_BYTES
        assert plain.endswith("图纸.png"), "后缀要留给系统挑打开方式"
        # 再调一次：复用（不重复解密）
        assert materialize(enc_path, cipher, folder=view) == plain
        # 明文文件本身：原样返回，不复制
        plain_file = os.path.join(folder, "普通.txt")
        with open(plain_file, "wb") as handle:
            handle.write(b"plain")
        assert materialize(plain_file, cipher, folder=view) == plain_file
    finally:
        set_active_cipher(None)


def test_seal_file_in_place_and_cleanup(tmp_path):
    cipher = StorageCipher(b"k" * 32)
    set_active_cipher(cipher)
    try:
        path = str(tmp_path / "粘贴的图片.png")
        with open(path, "wb") as handle:
            handle.write(SECRET_BYTES)
        assert seal_file_in_place(path, cipher) is True
        assert is_encrypted_file(path)
        assert seal_file_in_place(path, cipher) is False      # 已经加密了，不重复盖
        folder = str(tmp_path / "view2")
        assert cleanup_view_cache() >= 0                      # 没东西也不该炸
    finally:
        set_active_cipher(None)


# =========================================================================== 本地历史库
def test_history_db_stores_ciphertext(tmp_path):
    cipher = StorageCipher(b"k" * 32)
    db_path = str(tmp_path / "mchat-alice.db")
    attachment = Attachment(path="C:/cache/f_1-评审材料.pdf", name="评审材料.pdf", size=10)
    log = ChatLog(db_path, cipher=cipher)
    log.save_message(_msg("m1", SECRET, attachment=attachment))
    log.set_draft("c1", "没发出去的一句话")

    stored = _raw_column(db_path, "SELECT text FROM messages WHERE id='m1'")
    assert stored.startswith(TEXT_PREFIX), "磁盘上还是明文！"
    assert SECRET not in stored
    assert str(_raw_column(db_path, "SELECT file_name FROM messages WHERE id='m1'")) \
        .startswith(TEXT_PREFIX)
    assert str(_raw_column(db_path, "SELECT attachment FROM messages WHERE id='m1'")) \
        .startswith(TEXT_PREFIX)
    assert str(_raw_column(db_path, "SELECT text FROM drafts")) .startswith(TEXT_PREFIX)
    # 整个库文件里也不该出现明文（WAL 一起查）
    for suffix in ("", "-wal"):
        path = db_path + suffix
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                assert SECRET.encode("utf-8") not in handle.read(), path

    # 读回来必须是原文
    back = log.load("c1")[0]
    assert back.text == SECRET
    assert back.attachment is not None and back.attachment.name == "评审材料.pdf"
    assert log.draft("c1") == "没发出去的一句话"
    assert log.encrypted and log.stats()["encrypted"] is True
    log.close()


def test_legacy_plaintext_rows_are_converted_on_open(tmp_path):
    """老库（明文）用加密打开一次 → 就地转成密文，且内容不变。"""
    db_path = str(tmp_path / "mchat-bob.db")
    plain = ChatLog(db_path)                       # 先当没加密写进去
    plain.save_message(_msg("m1", SECRET))
    plain.close()
    assert _raw_column(db_path, "SELECT text FROM messages") == SECRET

    cipher = StorageCipher(b"k" * 32)
    log = ChatLog(db_path, cipher=cipher)
    assert str(_raw_column(db_path, "SELECT text FROM messages")).startswith(TEXT_PREFIX)
    assert log.load("c1")[0].text == SECRET
    log.close()


def test_search_still_works_when_encrypted(tmp_path):
    cipher = StorageCipher(b"k" * 32)
    log = ChatLog(str(tmp_path / "mchat-c.db"), cipher=cipher)
    log.save_message(_msg("m1", "这周要去日本出差"))
    log.save_message(_msg("m2", "晚上吃什么", 2.0))
    hits = log.search("日本")                        # 短词（走不到 FTS）也要能搜到
    assert [hit.message_id for hit in hits] == ["m1"]
    assert "日本" in hits[0].snippet
    assert log.search("日本", conversation_id="other") == []
    assert log.search("") == []
    log.close()


def test_unreadable_rows_do_not_crash_the_ui(tmp_path):
    """密钥不对/记录被改过：读回来是一句提示，而不是异常炸到界面。"""
    db_path = str(tmp_path / "mchat-d.db")
    with_key = ChatLog(db_path, cipher=StorageCipher(b"k" * 32))
    with_key.save_message(_msg("m1", SECRET))
    with_key.close()
    wrong = ChatLog(db_path, cipher=StorageCipher(b"z" * 32))
    message = wrong.load("c1")[0]
    assert "解密失败" in message.text
    assert wrong.search("日本") == []               # 搜不出东西，但也不炸
    wrong.close()


# =========================================================================== 令牌缓存
def test_token_is_not_plaintext_on_disk(tmp_path):
    cipher = StorageCipher(b"k" * 32)
    path = str(tmp_path / "account.json")
    store = SessionStore(path, cipher=cipher)
    store.save(Session(server="http://127.0.0.1:8765", token="tok-abcdef123456",
                       user_id="u_1", username="alice", display_name="爱丽丝"))
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    record = next(iter(raw["accounts"].values()))
    assert record["token"].startswith(TEXT_PREFIX), "令牌还是明文躺在文件里！"
    assert "tok-abcdef123456" not in json.dumps(raw)
    # 读回来还是能用的明文令牌
    assert store.last().token == "tok-abcdef123456"
    assert store.get("http://127.0.0.1:8765", "alice").token == "tok-abcdef123456"


def test_legacy_plaintext_token_still_works(tmp_path):
    path = str(tmp_path / "account.json")
    legacy = {"last": "http://127.0.0.1:8765|alice",
              "accounts": {"http://127.0.0.1:8765|alice": {
                  "server": "http://127.0.0.1:8765", "token": "老明文令牌",
                  "user_id": "u_1", "username": "alice"}}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(legacy, handle, ensure_ascii=False)
    store = SessionStore(path, cipher=StorageCipher(b"k" * 32))
    assert store.last().token == "老明文令牌"
    store.save(store.last())                      # 再存一次 → 自动变密文
    with open(path, "r", encoding="utf-8") as handle:
        assert str(json.load(handle)["accounts"][
            "http://127.0.0.1:8765|alice"]["token"]).startswith(TEXT_PREFIX)


def test_forget_does_not_write_other_tokens_in_plaintext(tmp_path):
    """退出一个账号时，别的账号的令牌重写回去也必须是密文（读写循环的坑）。"""
    cipher = StorageCipher(b"k" * 32)
    path = str(tmp_path / "account.json")
    store = SessionStore(path, cipher=cipher)
    store.save(Session(server="http://h:1", token="tok-alice", user_id="u_1",
                       username="alice"))
    store.save(Session(server="http://h:1", token="tok-bob", user_id="u_2",
                       username="bob"))
    store.forget("http://h:1", "bob")
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.dumps(json.load(handle))
    assert "tok-alice" not in raw
    assert SessionStore(path, cipher=cipher).get("http://h:1", "alice").token == "tok-alice"


def test_unreadable_token_forces_relogin_instead_of_garbage(tmp_path):
    path = str(tmp_path / "account.json")
    other_key = StorageCipher(b"z" * 32)
    store = SessionStore(path, cipher=other_key)
    store.save(Session(server="http://h:1", token="tok-secret", user_id="u_1",
                       username="alice"))
    # 换一把钥匙（模拟"数据被拷到别的机器/账号"）
    fresh = SessionStore(path, cipher=StorageCipher(b"k" * 32))
    session = fresh.last()
    assert session is not None and session.token == "", "解不开就该当没有令牌（要求重新登录）"
