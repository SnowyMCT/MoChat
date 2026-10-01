"""账号系统（纯逻辑层）单测：本地登录缓存 + 线上 JSON → 模型的转换。

用法：``.venv/Scripts/python.exe tests/test_account.py``

覆盖：
    路径与清理   —— 数据目录、缓存文件位置、服务器地址归一化、用户名当文件名的清洗
    登录缓存     —— 存/读/多账号/上次账号/"不记住我"不留令牌/退出登录/坏文件降级/原子写
    账号隔离     —— 每个账号的本地历史库是不同文件（换账号不会串记录）
    ApiError     —— 错误码 → 人话、401 判定（决定"要不要退回登录界面"）
    模型转换     —— user/conversation/message 的线上字段 -> 领域模型
                   （我自己的 id 映射、outgoing 推断、附件 remote:// 标记、引用/转发/合并转发）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.core.api import (ApiClient, ApiError, NetworkError,        # noqa: E402
                            conversation_from_json, message_from_json,
                            user_from_json)
from mchat.core.models import ME_ID, ChatKind, Message                 # noqa: E402
from mchat.core.session import (DEFAULT_SERVER, Session, SessionStore,  # noqa: E402
                                account_key, default_data_dir, log_path_for,
                                normalize_server, safe_name)

NOW = 1759123456.0


def _temp_store():
    folder = tempfile.mkdtemp(prefix="mchat-account-")
    return SessionStore(os.path.join(folder, "account.json")), folder


def _session(username="alice", **kwargs):
    base = dict(server="http://127.0.0.1:8765", token="tok-" + username,
                user_id="u_" + username, username=username,
                display_name="爱丽丝", saved_at=NOW, remember=True)
    base.update(kwargs)
    return Session(**base)


# --------------------------------------------------------------------------- 路径
def test_default_data_dir_and_session_path():
    folder = default_data_dir()
    assert os.path.isabs(folder), folder
    assert folder.endswith(os.path.join("MChat", "MChat")) or folder.endswith("MChat")
    assert SessionStore().path.endswith("account.json")


def test_normalize_server():
    assert normalize_server("example.com:8765") == "http://example.com:8765"
    assert normalize_server("  https://chat.example.com/  ") == "https://chat.example.com"
    assert normalize_server("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert normalize_server("") == ""
    assert normalize_server(None) == ""


def test_safe_name_and_log_path_per_account():
    assert safe_name("alice") == "alice"
    assert safe_name("a/b:c*d") == "a_b_c_d"
    assert safe_name("") == "default"
    one = log_path_for(_session("alice"), "C:/data")
    two = log_path_for(_session("bob", display_name="鲍勃"), "C:/data")
    assert one != two, "不同账号必须用不同的库文件，否则会看到别人的聊天记录"
    assert one.endswith(".db") and "alice" in one


def test_account_key_is_case_insensitive_on_username():
    assert account_key("http://h:1/", "Alice") == account_key("http://h:1", "alice")
    assert account_key("http://h:1", "alice") != account_key("http://h:2", "alice")


# --------------------------------------------------------------------------- 缓存
def test_store_empty_then_save_and_load():
    store, folder = _temp_store()
    assert store.last() is None and not store.has_any()
    store.save(_session())
    loaded = store.last()
    assert loaded is not None
    assert (loaded.server, loaded.token, loaded.username) == (
        "http://127.0.0.1:8765", "tok-alice", "alice")
    assert loaded.display_name == "爱丽丝"
    assert store.get("http://127.0.0.1:8765", "alice") is not None
    assert store.get("http://127.0.0.1:8765", "bob") is None


def test_store_keeps_multiple_accounts_and_last_wins():
    store, _ = _temp_store()
    store.save(_session("alice"))
    store.save(_session("bob"))
    assert store.last().username == "bob"
    assert {s.username for s in store.sessions()} == {"alice", "bob"}
    store.forget("http://127.0.0.1:8765", "bob")
    assert store.last().username == "alice", "退出一个账号不该影响别的账号"


def test_store_forget_only_removes_that_account():
    store, _ = _temp_store()
    store.save(_session("alice"))
    store.save(_session("bob"))
    assert store.forget("http://127.0.0.1:8765", "bob") is True
    assert store.forget("http://127.0.0.1:8765", "bob") is False       # 第二次就没有了
    assert store.get("http://127.0.0.1:8765", "alice") is not None
    assert store.last().username == "alice"


def test_not_remembering_drops_the_token():
    """没勾「记住我」：只记账号名方便下次填，**令牌不落盘**。"""
    store, _ = _temp_store()
    store.save(_session("alice", remember=False))
    raw = json.load(open(store.path, encoding="utf-8"))
    stored = next(iter(raw["accounts"].values()))
    assert stored["token"] == "", stored
    assert raw["last"] == account_key("http://127.0.0.1:8765", "alice")
    assert store.last().username == "alice"      # 账号还在（只是没令牌）


def test_cache_never_contains_a_password():
    """缓存文件里不能出现任何密码字段（我们从头到尾就没存过密码）。"""
    store, _ = _temp_store()
    store.save(_session("alice"))
    text = open(store.path, encoding="utf-8").read()
    assert "password" not in text.lower()
    assert "secret123" not in text


def test_store_survives_corrupt_file():
    store, folder = _temp_store()
    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write("{这不是 JSON")
    assert store.last() is None
    store.save(_session())                        # 坏文件不该让它以后都写不进去
    assert store.last().username == "alice"
    assert not os.path.exists(store.path + ".tmp"), "原子写不该留临时文件"


def test_store_reads_legacy_single_session_file():
    """兼容第一版格式：整个文件就是一个 session。"""
    store, folder = _temp_store()
    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump(_session().to_json(), handle, ensure_ascii=False)
    assert store.last().username == "alice"


def test_session_json_roundtrip_and_bad_data():
    session = _session()
    assert Session.from_json(session.to_json()) == session
    for bad in (None, {}, {"server": "", "token": "t", "username": "a"},
                {"server": "http://h", "token": "t", "username": ""}, "不是字典"):
        assert Session.from_json(bad) is None, bad
    # 没令牌（"不记住我"）也是合法的一条账号记录：只是下次要重新输密码
    tokenless = Session.from_json({"server": "http://h:1", "token": "",
                                   "username": "alice"})
    assert tokenless is not None and tokenless.token == "" and tokenless.username == "alice"
    assert session.name == "爱丽丝" and session.host == "127.0.0.1:8765"
    assert session.label == "爱丽丝 @ 127.0.0.1:8765"
    assert session.with_token("new").token == "new"


def test_clear_removes_the_file():
    store, folder = _temp_store()
    store.save(_session())
    assert os.path.isfile(store.path)
    store.clear()
    assert not os.path.isfile(store.path)
    assert store.last() is None


def test_default_server_constant_is_localhost():
    assert DEFAULT_SERVER.startswith("http://") and "8765" in DEFAULT_SERVER


# --------------------------------------------------------------------------- 错误
def test_api_error_friendly_and_auth_flag():
    assert ApiError("bad_credentials", "", 401).friendly == "用户名或密码不对"
    assert ApiError("username_taken", "", 409).friendly == "这个用户名已经被注册了"
    assert ApiError("unauthorized", "", 401).is_auth is True
    assert ApiError("not_found", "", 404).is_auth is False
    assert ApiError("weird_thing", "服务端加了新错误码", 500).friendly == "服务端加了新错误码"


def test_network_error_is_not_an_account_problem():
    error = NetworkError("Connection refused", url="http://h/api/sync")
    assert error.friendly.startswith("连不上服务器")
    assert not isinstance(error, ApiError)


def test_client_requires_a_server_address():
    try:
        ApiClient("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("空地址应该直接报错，而不是等到发请求才炸")
    client = ApiClient("chat.example.com:9000", "tok")
    assert client.base_url == "http://chat.example.com:9000"
    assert client.token == "tok"
    # 带 token 的直链**故意没有了**：URL 会进反代日志/浏览器历史，令牌不该出现在里面。
    # 服务端那边也只认 Authorization 头（见 tests/test_hardening.py）。
    assert not hasattr(client, "file_url"), "别再把它加回来：token 不进 URL"


def test_register_can_carry_an_invite_code():
    """服务器要求邀请码时，客户端要能把 invite 放进 body；不填就不带这个字段。"""
    sent = {}

    class Recorder(ApiClient):
        def _json(self, method, path, **kwargs):
            sent.update({"method": method, "path": path, "body": kwargs.get("body")})
            return {"token": "t", "user": {"id": "u_1", "username": "alice"}}

    client = Recorder("http://127.0.0.1:8765")
    client.register("alice", "secret123", "爱丽丝", "let-me-in")
    assert sent["path"] == "/api/register" and sent["method"] == "POST"
    assert sent["body"]["invite"] == "let-me-in"
    client.register("alice", "secret123", "爱丽丝")
    assert "invite" not in sent["body"]


# --------------------------------------------------------------------------- 转换
def test_user_from_json_remaps_me():
    me = user_from_json({"id": "u_me", "username": "alice", "display_name": "爱丽丝",
                         "online": True}, me_id="u_me")
    assert me.id == ME_ID and me.is_me and me.name == "爱丽丝"
    other = user_from_json({"id": "u_b", "username": "bob"}, me_id="u_me")
    assert other.id == "u_b" and not other.is_me and other.name == "bob"


def test_message_from_json_full_payload():
    message = message_from_json({
        "id": "m1", "conversation_id": "c1", "sender_id": "u_me", "sender_name": "我",
        "text": "在吗", "ts": NOW, "status": "read",
        "attachment": {"id": "f1", "name": "图.png", "size": 1234},
        "reply_to": {"id": "m0", "author": "鲍勃", "preview": "早"},
        "forwarded_from": "另一个群",
        "merged": [{"author": "鲍勃", "text": "第一条", "attachment": "", "clock": "09:01"}],
    }, me_id="u_me")
    assert message.outgoing is True and message.sender_id == ME_ID
    assert message.is_read is True
    assert message.attachment.is_remote and message.attachment.remote_id == "f1"
    assert message.attachment.name == "图.png" and message.attachment.size == 1234
    assert message.attachment.exists is False, "还没下回来的附件不能当成在本地"
    assert message.reply_to.text == "鲍勃：早"
    assert message.merged_count == 1 and message.merged_line(0) == "鲍勃：第一条"


def test_message_from_json_is_lenient():
    assert message_from_json(None) is None
    assert message_from_json({"id": "", "conversation_id": "c"}) is None
    loose = message_from_json({"id": "m2", "conversation_id": "c1", "text": "嗨",
                               "ts": "不是数字"}, me_id="u_me")
    assert loose.ts == 0.0 and loose.outgoing is False
    assert loose.attachment is None and loose.merged_items == ()


def test_conversation_from_json_builds_members_and_preview():
    conversation = conversation_from_json({
        "id": "c1", "kind": "group", "title": "评审组",
        "members": [{"id": "u_me", "username": "alice", "display_name": "爱丽丝"},
                    {"id": "u_b", "username": "bob", "display_name": "鲍勃"}],
        "last_message": {"id": "m9", "conversation_id": "c1", "sender_id": "u_b",
                         "text": "下午两点", "ts": NOW},
        "unread": 2,
    }, me_id="u_me")
    assert conversation.kind is ChatKind.GROUP and conversation.title == "评审组"
    assert conversation.member(ME_ID) is not None
    assert conversation.peer_name == "鲍勃", "「对方」不能被认成我自己"
    assert len(conversation.messages) == 1
    assert conversation.preview_text() == "鲍勃: 下午两点"
    assert conversation_from_json({"id": ""}) is None


def test_conversation_pins_come_from_the_server_with_snapshots():
    """服务端下发的"双方置顶"要能变成 PinRef + 把快照消息带进来（横幅要用）。"""
    conversation = conversation_from_json({
        "id": "c1", "kind": "direct", "title": "鲍勃",
        "members": [{"id": "u_me", "username": "alice", "display_name": "爱丽丝"},
                    {"id": "u_b", "username": "bob", "display_name": "鲍勃"}],
        "last_message": {"id": "m2", "conversation_id": "c1", "sender_id": "u_b",
                         "text": "晚安", "ts": NOW},
        "pins": [{"message_id": "m1", "pinned_by": "u_me", "pinned_by_name": "爱丽丝",
                  "pinned_at": NOW - 10, "for_both": True,
                  "message": {"id": "m1", "conversation_id": "c1", "sender_id": "u_me",
                              "text": "记得带伞", "ts": NOW - 60}},
                 {"message_id": "", "for_both": True}],
    }, me_id="u_me")
    assert len(conversation.pins) == 1, "空 message_id 的那条要丢掉"
    pin = conversation.pins[0]
    assert pin.message_id == "m1" and pin.for_both is True
    assert pin.label == "双方可见"
    ids = [message.id for message in conversation.messages]
    assert ids == ["m1", "m2"], ids                      # 快照 + 最后一条，按时间排
    assert conversation.messages[0].text == "记得带伞"
    assert conversation_from_json({"id": "c1", "pins": None}).pins == ()


def test_message_send_text_keeps_merged_payload():
    merged = message_from_json({
        "id": "m3", "conversation_id": "c1", "sender_id": "u_me", "text": "",
        "ts": NOW,
        "merged": [{"author": "鲍勃", "text": "a", "attachment": "", "clock": "09:01"},
                   {"author": "我", "text": "b", "attachment": "", "clock": "09:02"}],
    }, me_id="u_me")
    assert merged.is_merged and merged.send_text == "【合并转发】2 条消息"
    plain = Message(id="m4", conversation_id="c1", sender_id="u_me", text="  ", ts=NOW)
    assert plain.send_text == "", "纯附件/空消息不该被塞进一句假正文"


def test_error_body_fallback_from_api_module():
    from mchat.core.api import _error_from_body
    payload = '{"error": {"code": "not_member", "message": "不在群里"}}'.encode("utf-8")
    assert _error_from_body(payload, 403) == ("not_member", "不在群里")
    assert _error_from_body(b"<html>500</html>", 500)[0] == "server_error"
    assert _error_from_body(b"", 401)[0] == "unauthorized"


def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"  ok    {name}")
        except Exception as exc:                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
