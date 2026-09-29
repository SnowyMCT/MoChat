"""本地历史库（SQLite + FTS5）的单测，零 Qt 依赖。

用法：``python tests/test_db.py``；装了 pytest 也可以 ``pytest tests``。

覆盖：
    落库与读取 —— 幂等 upsert、时间正序、limit 取最近 N 条、按会话隔离
    全文检索   —— 中文子串、英文子串、短词（<3 字符走 LIKE）、片段、上限、无命中
    附件       —— 附件随消息落库/读回、按附件文件名检索、附件计数
    库结构升级 —— v1 老库自动迁移到 v2（补列 + 重建索引 + 幂等）
    互动与草稿 —— 表情开关与计数、整会话一次取、草稿增删查
    维护操作   —— 删除消息连互动一起删、清空历史、stats
    效率基准   —— 2 万条批量落库耗时、检索耗时、文件占用（"占用少、效率高"的实测证据）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.core.db import (DEFAULT_LIMIT, SCHEMA_VERSION, SEARCH_LIMIT,  # noqa: E402
                           TRIGRAM_MIN, ChatLog)
from mchat.core.models import (Attachment, MergedItem, Message,       # noqa: E402
                               PinRef, ReplyRef, merged_from_json,
                               reply_from_json)

CHAT = "chat-a"
OTHER = "chat-b"


def _msg(mid: str, text: str, ts: float, *, conv: str = CHAT, outgoing: bool = False,
         sender: str = "lin", attachment: Attachment | None = None,
         reply_to: ReplyRef | None = None, forwarded_from: str = "",
         merged_items: tuple[MergedItem, ...] = ()) -> Message:
    return Message(id=mid, conversation_id=conv, sender_id=sender, text=text, ts=ts,
                   outgoing=outgoing, attachment=attachment, reply_to=reply_to,
                   forwarded_from=forwarded_from, merged_items=merged_items)


def _log(**kw) -> ChatLog:
    return ChatLog(kw.pop("path", ":memory:"))


def _write(path: str, size: int = 2048) -> str:
    """造一个真实文件（附件的大小取自磁盘，所以测试里得真写一个）。"""
    with open(path, "wb") as handle:
        handle.write(b"m" * size)
    return path


# ---------------------------------------------------------------------------
# 落库与读取
# ---------------------------------------------------------------------------
def test_save_and_load_roundtrip():
    log = _log()
    log.save_message(_msg("m1", "早上好", 100.0))
    log.save_message(_msg("m2", "收到", 101.0, outgoing=True))
    loaded = log.load(CHAT)
    assert [m.id for m in loaded] == ["m1", "m2"]
    assert loaded[0].text == "早上好"
    assert loaded[1].outgoing is True and loaded[1].status == "sent"
    log.close()


def test_upsert_is_idempotent_and_updates_status():
    log = _log()
    log.save_message(_msg("m1", "原文", 100.0))
    log.save_message(_msg("m1", "改过的", 100.0))                    # 同 id 覆盖
    log.set_status("m1", "read")
    assert log.count() == 1
    loaded = log.load(CHAT)[0]
    assert loaded.text == "改过的" and loaded.status == "read"
    log.close()


def test_load_returns_last_n_in_ascending_order():
    log = _log()
    log.save_messages([_msg(f"m{i}", f"第{i}条", 100.0 + i) for i in range(300)])
    loaded = log.load(CHAT, limit=10)
    assert len(loaded) == 10
    assert [m.id for m in loaded] == [f"m{i}" for i in range(290, 300)]
    assert DEFAULT_LIMIT >= 100, DEFAULT_LIMIT
    log.close()


def test_conversations_are_isolated():
    log = _log()
    log.save_messages([_msg("m1", "A 的消息", 1.0), _msg("m2", "B 的消息", 2.0, conv=OTHER)])
    assert [m.text for m in log.load(CHAT)] == ["A 的消息"]
    assert [m.text for m in log.load(OTHER)] == ["B 的消息"]
    assert log.count() == 2 and log.count(CHAT) == 1
    assert sorted(log.conversations()) == [CHAT, OTHER]
    log.close()


def test_persists_across_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sub", "mchat.db")     # 连目录都会自动建
        log = ChatLog(path)
        log.save_message(_msg("m1", "落盘的消息", 100.0))
        log.close()
        again = ChatLog(path)
        assert [m.text for m in again.load(CHAT)] == ["落盘的消息"]
        assert again.stats()["messages"] == 1
        again.close()


# ---------------------------------------------------------------------------
# 全文检索
# ---------------------------------------------------------------------------
def _search_fixture() -> ChatLog:
    log = _log()
    log.save_messages([
        _msg("m1", "这周要去日本和美国出差", 100.0),
        _msg("m2", "telegram 的表情面板做好了", 101.0),
        _msg("m3", "明天下午三点开会", 102.0, conv=OTHER),
        _msg("m4", "美国那边的时差记得算", 103.0),
    ])
    return log


def test_search_chinese_substring():
    """trigram 分词的价值所在：中文中间的子串也能命中。"""
    log = _search_fixture()
    hits = log.search("美国")
    assert {h.message_id for h in hits} == {"m1", "m4"}
    assert log.search("本和")[0].message_id == "m1"        # 词中间
    assert log.search("周要去")[0].conversation_id == CHAT
    log.close()


def test_search_english_substring_and_case():
    log = _search_fixture()
    assert [h.message_id for h in log.search("telegram")] == ["m2"]
    assert [h.message_id for h in log.search("TELEGRAM")] == ["m2"]
    assert [h.message_id for h in log.search("em")] == ["m2"] if TRIGRAM_MIN <= 2 else True
    log.close()


def test_search_short_query_uses_like():
    """不到 3 个字符的词 trigram 索引命不了，走 LIKE 兜底。"""
    log = _search_fixture()
    assert TRIGRAM_MIN == 3
    hits = log.search("开会")
    assert [h.message_id for h in hits] == ["m3"]
    assert [h.message_id for h in log.search("三")] == ["m3"]
    log.close()


def test_search_snippet_contains_the_hit():
    log = _search_fixture()
    hit = log.search("美国")[0]
    assert "美国" in hit.snippet
    assert hit.ts > 0 and hit.outgoing is False
    log.close()


def test_search_orders_by_time_desc_and_limits():
    log = _search_fixture()
    hits = log.search("美国")
    assert hits[0].ts >= hits[-1].ts
    log.save_messages([_msg(f"x{i}", "美国测试", 200.0 + i) for i in range(40)])
    assert len(log.search("美国", limit=5)) == 5
    assert SEARCH_LIMIT >= 20
    log.close()


def test_search_empty_and_miss_and_quotes():
    log = _search_fixture()
    assert log.search("") == [] and log.search("   ") == []
    assert log.search("这个词肯定没有") == []
    assert log.search('"quoted"') == []                     # 引号不会把 FTS 语法搞崩
    log.close()


def test_search_scoped_to_one_conversation():
    """按会话过滤：界面"从哪个会话进来就搜哪个会话"靠的就是这个参数。"""
    log = _search_fixture()
    scoped = log.search("美国", conversation_id=CHAT)
    assert {h.message_id for h in scoped} == {"m1", "m4"}
    assert all(h.conversation_id == CHAT for h in scoped)
    assert log.search("美国", conversation_id=OTHER) == []          # 那个会话里没有
    assert {h.message_id for h in log.search("美国")} == {"m1", "m4"}   # 不传=全部

    # 短词（LIKE 兜底那条路）也要能按会话过滤
    assert [h.message_id for h in log.search("三", conversation_id=OTHER)] == ["m3"]
    assert log.search("三", conversation_id=CHAT) == []

    # 过滤写在 SQL 里，上限照样生效
    log.save_messages([_msg(f"s{i}", "美国测试", 300.0 + i, conv=CHAT) for i in range(30)])
    assert len(log.search("美国", limit=5, conversation_id=CHAT)) == 5
    assert log.search("美国", limit=5, conversation_id=OTHER) == []
    log.close()


def test_search_survives_edit_and_delete():
    log = _search_fixture()
    log.save_message(_msg("m1", "改成去日本了", 100.0))        # 触发 FTS 重建索引
    assert log.search("美国") == [] or all(h.message_id != "m1" for h in log.search("美国"))
    assert [h.message_id for h in log.search("日本")] == ["m1"]
    assert log.delete_message("m1") is True
    assert log.search("日本") == []
    log.close()


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------
def test_attachment_round_trip():
    """附件跟着消息一起落库、一起读回；纯附件消息的正文是空串（预览另算）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Attachment.from_path(_write(os.path.join(tmp, "评审材料-v3.pdf"), 2048))
        png = Attachment.from_path(_write(os.path.join(tmp, "配色预览.png"), 4096))
        log = _log()
        log.save_message(_msg("m1", "文件在这，看下。", 100.0, attachment=pdf))
        log.save_message(_msg("m2", "", 101.0, attachment=png))
        log.save_message(_msg("m3", "没有附件的普通消息", 102.0))

        loaded = {m.id: m for m in log.load(CHAT)}
        assert loaded["m1"].attachment == pdf                 # frozen dataclass 直接比
        assert loaded["m1"].attachment.name == "评审材料-v3.pdf"
        assert loaded["m1"].attachment.size == 2048
        assert loaded["m1"].kind == "file" and loaded["m1"].text == "文件在这，看下。"
        assert loaded["m2"].kind == "image" and loaded["m2"].preview_text == "📷 照片"
        assert loaded["m3"].attachment is None and loaded["m3"].kind == "text"
        assert log.stats()["attachments"] == 2
        log.close()


def test_upsert_replaces_attachment_too():
    """同 id 再写一次：附件也要跟着换（不能只更新正文，留下上一条的附件）。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = Attachment.from_path(_write(os.path.join(tmp, "a.png"), 100))
        second = Attachment.from_path(_write(os.path.join(tmp, "b.pdf"), 200))
        log = _log()
        log.save_message(_msg("m1", "第一条", 100.0, attachment=first))
        log.save_message(_msg("m1", "改成第二条", 100.0, attachment=second))
        loaded = log.load(CHAT)[0]
        assert loaded.text == "改成第二条"
        assert loaded.attachment == second and loaded.attachment.name == "b.pdf"
        log.close()


def test_broken_attachment_json_is_ignored():
    """库里那一列是坏 JSON 时当作"没有附件"，不能让界面读崩。"""
    log = _log()
    log.save_message(_msg("m1", "正文", 100.0))
    with log._db:
        log._db.execute("UPDATE messages SET attachment = ? WHERE id = 'm1'", ("{坏数据",))
    assert log.load(CHAT)[0].attachment is None
    log.close()


def test_search_finds_attachment_file_name():
    """索引的是"正文 + 附件文件名"：只在文件名里出现过的词也要搜得到。"""
    with tempfile.TemporaryDirectory() as tmp:
        report = Attachment.from_path(_write(os.path.join(tmp, "季度复盘报告.pdf"), 111))
        photo = Attachment.from_path(_write(os.path.join(tmp, "秋天在山里拍的.png"), 222))
        log = _log()
        log.save_message(_msg("m1", "附件发你，收一下。", 100.0, attachment=report))
        log.save_message(_msg("m2", "", 101.0, attachment=photo))
        log.save_message(_msg("m3", "完全不相关的一句话", 102.0))

        assert [h.message_id for h in log.search("复盘报告")] == ["m1"]     # 4 字走 FTS
        assert [h.message_id for h in log.search("季度复盘")] == ["m1"]
        assert [h.message_id for h in log.search("复盘")] == ["m1"]         # 2 字走 LIKE
        assert [h.message_id for h in log.search("在山里")] == ["m2"]       # 纯附件消息
        assert "复盘报告" in log.search("复盘报告")[0].snippet
        assert log.search("表里没有这个词") == []
        # 正文照样能搜（索引串 = 正文 + 文件名，两者都在）
        assert [h.message_id for h in log.search("附件发你")] == ["m1"]
        log.close()


def test_clear_and_delete_keep_attachment_search_in_sync():
    """FTS 触发器对附件消息同样有效：改、删之后索引不能残留。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = Attachment.from_path(_write(os.path.join(tmp, "旧方案.pdf"), 10))
        second = Attachment.from_path(_write(os.path.join(tmp, "新方案.pdf"), 20))
        log = _log()
        log.save_message(_msg("m1", "第一版", 100.0, attachment=first))
        log.save_message(_msg("m1", "第二版", 100.0, attachment=second))    # 触发器走 UPDATE
        assert [h.message_id for h in log.search("旧方案")] == []
        assert [h.message_id for h in log.search("新方案")] == ["m1"]

        log.delete_message("m1")
        assert log.search("新方案") == []
        log.close()


# ---------------------------------------------------------------------------
# 库结构升级（老用户的 v1 库）
# ---------------------------------------------------------------------------
LEGACY_SCHEMA = """
CREATE TABLE messages(
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id       TEXT NOT NULL,
    text            TEXT NOT NULL,
    ts              REAL NOT NULL,
    outgoing        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'sent'
);
CREATE INDEX idx_messages_conv ON messages(conversation_id, ts);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, content='messages', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


def _make_v1_db(path: str) -> None:
    """照 v1 的样子造一个老库（老用户 %APPDATA% 里那个就是这个结构）。"""
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute("INSERT INTO messages(id, conversation_id, sender_id, text, ts,"
                " outgoing, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
                ("m1", CHAT, "lin", "这周要去日本和美国出差", 100.0, 0, "sent"))
    raw.execute("INSERT INTO messages(id, conversation_id, sender_id, text, ts,"
                " outgoing, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
                ("m2", CHAT, "me", "收到", 101.0, 1, "read"))
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()


def test_migrates_v1_database():
    """v1 老库打开即升级：老消息一条不丢、检索照旧、新列可用；再开一次仍然幂等。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mchat.db")
        _make_v1_db(path)

        log = ChatLog(path)
        assert log._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row["name"] for row in log._db.execute("PRAGMA table_xinfo(messages)")}
        assert {"attachment", "file_name", "search_text"} <= columns
        # v3 的三列 + 会话表的置顶列也要补上（v1 直接跳到当前版本）
        assert {"reply_to", "forwarded_from", "merged"} <= columns
        chat_columns = {row["name"] for row in log._db.execute("PRAGMA table_xinfo(chats)")}
        assert "pinned_message_id" in chat_columns
        loaded = log.load(CHAT)
        assert [m.id for m in loaded] == ["m1", "m2"]
        assert loaded[0].text == "这周要去日本和美国出差"
        assert loaded[0].attachment is None and loaded[0].kind == "text"
        assert loaded[1].status == "read"
        # 老数据在重建后的索引里照样搜得到
        assert [h.message_id for h in log.search("日本")] == ["m1"]
        assert [h.message_id for h in log.search("收到")] == ["m2"]

        # 升级完就能用新功能：附件落库 + 按文件名检索
        file_path = _write(os.path.join(tmp, "老库升级后的附件.pdf"), 512)
        log.save_message(_msg("m3", "", 102.0,
                              attachment=Attachment.from_path(file_path)))
        assert [h.message_id for h in log.search("老库升级后")] == ["m3"]
        assert log.stats()["attachments"] == 1
        log.close()

        again = ChatLog(path)                       # 第二次打开：什么都不用做
        assert again.count(CHAT) == 3
        assert again.load(CHAT)[2].attachment.name == "老库升级后的附件.pdf"
        assert [h.message_id for h in again.search("日本")] == ["m1"]
        again.close()


def test_migrates_v2_database():
    """v2 老库升到 v3：补 4 列（回复/转发/合并 + 会话置顶），老数据与检索都不受影响。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mchat.db")
        # 先按当前版本建库、写入老数据，再把 v3 的列"拆掉"，模拟 v2 的库
        log = ChatLog(path)
        log.save_message(_msg("m1", "v2 时代的一条消息", 100.0))
        log.save_message(_msg("m2", "另外一条", 102.0, outgoing=True))
        log.close()
        raw = sqlite3.connect(path)
        for column in ("reply_to", "forwarded_from", "merged"):
            raw.execute(f"ALTER TABLE messages DROP COLUMN {column}")
        raw.execute("ALTER TABLE chats DROP COLUMN pinned_message_id")
        raw.execute("PRAGMA user_version=2")
        raw.commit()
        raw.close()

        upgraded = ChatLog(path)
        assert upgraded._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row["name"] for row in upgraded._db.execute("PRAGMA table_xinfo(messages)")}
        assert {"reply_to", "forwarded_from", "merged"} <= columns
        chat_columns = {row["name"] for row in upgraded._db.execute("PRAGMA table_xinfo(chats)")}
        assert "pinned_message_id" in chat_columns
        # 老消息照旧：正文、检索、新列都是"空"的语义
        loaded = upgraded.load(CHAT)
        assert [m.id for m in loaded] == ["m1", "m2"]
        assert loaded[0].text == "v2 时代的一条消息"
        assert loaded[0].reply_to is None and loaded[0].merged_items == ()
        assert loaded[0].forwarded_from == ""
        assert [h.message_id for h in upgraded.search("v2 时代")] == ["m1"]
        # 升完就能用新能力
        upgraded.save_message(_msg("m3", "", 103.0, outgoing=True,
                                   forwarded_from="另一个会话",
                                   merged_items=(MergedItem("我", "第一条"),)))
        assert upgraded.load(CHAT)[2].merged_items[0].text == "第一条"
        upgraded.close()


# ---------------------------------------------------------------------------
# 回复 / 转发 / 合并转发 / 会话内置顶（v3）
# ---------------------------------------------------------------------------
def test_reply_round_trip():
    """回复的引用块跟着消息一起落库、读回来还认得。"""
    log = ChatLog(":memory:")
    reply = ReplyRef("m1", "王尼玛", "周五那份材料我先过一遍")
    log.save_message(_msg("m9", "好，我也看一下", 200.0, outgoing=True, reply_to=reply))
    loaded = log.load(CHAT)[0]
    assert loaded.reply_to == reply
    assert loaded.is_reply and loaded.reply_to.text == "王尼玛：周五那份材料我先过一遍"
    # 覆盖写入（同 id 再存一次）也要跟着更新，而不是留着旧引用
    log.save_message(_msg("m9", "改主意了", 201.0, outgoing=True,
                          reply_to=ReplyRef("m2", "我", "另一条")))
    again = log.load(CHAT)[0]
    assert again.reply_to.message_id == "m2" and again.text == "改主意了"
    log.close()


def test_forward_round_trip():
    """逐条转发与合并转发都要能原样落库读回（含附件名）。"""
    log = ChatLog(":memory:")
    single = _msg("f1", "转发过来的原文", 300.0, outgoing=True, forwarded_from="王尼玛")
    merged = _msg("f2", "", 301.0, outgoing=True, forwarded_from="设计组",
                  merged_items=(MergedItem("曹尼玛", "配色再淡一点", "配色预览.png", "14:05"),
                                MergedItem("我", "收到", "", "14:07")))
    log.save_messages([single, merged])
    loaded = {m.id: m for m in log.load(CHAT)}
    assert loaded["f1"].is_forwarded and loaded["f1"].forwarded_from == "王尼玛"
    assert not loaded["f1"].is_merged
    assert loaded["f2"].is_merged and loaded["f2"].merged_count == 2
    assert loaded["f2"].merged_items[0].attachment == "配色预览.png"
    assert loaded["f2"].merged_items[0].clock == "14:05"        # 每条的时间也要能往返
    assert loaded["f2"].merged_items[1].clock == "14:07"
    assert loaded["f2"].merged_line(0) == "曹尼玛：配色再淡一点"
    # 老数据（没有 clock 字段）读回来是空串，不该炸
    assert MergedItem.from_json({"author": "我", "text": "老数据"}).clock == ""
    assert loaded["f2"].merged_preview() == "曹尼玛：配色再淡一点"
    assert loaded["f2"].merged_line(1) == "我：收到"
    # 纯附件那一行摘要显示成 📎 文件名
    assert MergedItem("我", "", "评审材料-v3.pdf").preview == "📎 评审材料-v3.pdf"
    # 合并转发的会话摘要
    assert loaded["f2"].preview_text == "【合并转发】2 条消息"
    log.close()


def test_bad_json_degrades():
    """手改坏了的 JSON 不能让界面崩：降级成"没有引用 / 不是合并转发"。"""
    log = ChatLog(":memory:")
    log.save_message(_msg("b1", "坏数据", 100.0))
    log._db.execute("UPDATE messages SET reply_to = '{不是合法 JSON', merged = '[[['")
    log._db.commit()
    loaded = log.load(CHAT)[0]
    assert loaded.reply_to is None and loaded.merged_items == ()
    assert not loaded.is_reply and not loaded.is_merged
    assert reply_from_json(None) is None and reply_from_json("") is None
    assert merged_from_json("{}") == () and merged_from_json(None) == ()
    log.close()


def test_pinned_messages_persist():
    """置顶**可以多条**，每条带"仅我可见 / 双方可见"：存库读回都要保住。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mchat.db")
        log = ChatLog(path)
        for mid, ts in (("m1", 100.0), ("m2", 101.0), ("m3", 102.0)):
            log.save_message(_msg(mid, f"第 {mid} 条", ts))
        log.set_pinned_refs(CHAT, (PinRef("m2", True), PinRef("m1", False)))
        assert log.pinned_refs() == {CHAT: (PinRef("m2", True), PinRef("m1", False))}
        log.close()

        again = ChatLog(path)                        # 重开：两条都在、范围标记也在
        assert again.pinned_refs() == {CHAT: (PinRef("m2", True), PinRef("m1", False))}
        again.set_pinned_refs(CHAT, (PinRef("m2", True),))    # 只留一条
        assert again.pinned_refs() == {CHAT: (PinRef("m2", True),)}
        again.set_pinned_refs(CHAT, ())              # 全取消
        assert again.pinned_refs() == {}
        again.set_pinned_refs(CHAT, (PinRef("m1"), PinRef("m3", True)))
        assert again.clear_conversation(CHAT) == 3   # 清空会话 -> 置顶一起没
        assert again.pinned_refs() == {}
        assert again.load(CHAT) == []
        again.close()


def test_pinned_legacy_formats_read_back():
    """这一列的老存法都要能读：单个裸 id（v3 第一版）、纯 id 数组（多条那版）、坏 JSON。"""
    log = ChatLog(":memory:")
    log.save_message(_msg("m1", "老库里的置顶", 100.0))
    log.save_message(_msg("m2", "第二条", 101.0))

    def write(raw: str) -> None:
        log._db.execute(
            "INSERT INTO chats(conversation_id, pinned_message_id) VALUES (?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET pinned_message_id = excluded."
            "pinned_message_id", (CHAT, raw))
        log._db.commit()

    write("m1")                                      # 老：单个裸 id
    assert log.pinned_refs() == {CHAT: (PinRef("m1", False),)}
    write('["m2", "m1"]')                            # 老：纯 id 数组（都当"仅我可见"）
    assert log.pinned_refs() == {CHAT: (PinRef("m2", False), PinRef("m1", False))}
    write('[{"id": "m1", "both": true}]')            # 新：带可见范围
    assert log.pinned_refs() == {CHAT: (PinRef("m1", True),)}
    write("[坏掉的 JSON")
    assert log.pinned_refs() == {}                   # 坏数据降级成"没置顶"
    log.close()


# ---------------------------------------------------------------------------
# 互动与草稿
# ---------------------------------------------------------------------------
def test_reaction_toggle():
    log = _search_fixture()
    assert log.toggle_reaction("m1", "👍") == {"👍": 1}
    assert log.reactions("m1") == {"👍": 1}
    assert log.mine("m1") == {"👍"}
    assert log.toggle_reaction("m1", "❤️") == {"👍": 1, "❤️": 1}
    assert log.toggle_reaction("m1", "👍") == {"❤️": 1}      # 再点一次撤掉
    assert log.mine("m1") == {"❤️"}
    assert log.toggle_reaction("m1", "❤️") == {}             # 清空后不留下空行
    assert log.stats()["reactions"] == 0
    log.close()


def test_reactions_for_whole_conversation():
    log = _search_fixture()
    log.toggle_reaction("m1", "🎉")
    log.toggle_reaction("m4", "👍")
    log.toggle_reaction("m3", "👍")                          # 另一个会话的不该混进来
    got = log.reactions_for(CHAT)
    assert got == {"m1": {"🎉": 1}, "m4": {"👍": 1}}
    log.close()


def test_drafts():
    log = _search_fixture()
    assert log.draft(CHAT) == ""
    log.set_draft(CHAT, "还没写完的")
    assert log.draft(CHAT) == "还没写完的"
    assert log.drafts() == {CHAT: "还没写完的"}
    log.set_draft(CHAT, "改一下")
    assert log.draft(CHAT) == "改一下"
    log.set_draft(CHAT, "   ")                               # 空 = 删
    assert log.draft(CHAT) == "" and log.drafts() == {}
    log.close()


def test_delete_message_removes_reactions():
    log = _search_fixture()
    log.toggle_reaction("m1", "👍")
    assert log.delete_message("m1") is True
    assert log.delete_message("m1") is False                 # 再删没有就是 False
    assert log.reactions("m1") == {} and log.stats()["reactions"] == 0
    log.close()


def test_clear_history():
    log = _search_fixture()
    log.toggle_reaction("m1", "👍")
    log.set_draft(CHAT, "草稿")
    log.clear()
    assert log.count() == 0 and log.search("美国") == []
    stats = log.stats()
    assert stats["messages"] == 0 and stats["reactions"] == 0 and stats["drafts"] == 0
    log.close()


def test_flags_round_trip():
    log = _search_fixture()
    assert log.flags() == {}
    log.set_flag(CHAT, "pinned", True)
    log.set_flag(CHAT, "muted", True)
    log.set_flag(OTHER, "muted", True)
    assert log.flags() == {CHAT: {"pinned", "muted"}, OTHER: {"muted"}}
    log.set_flag(CHAT, "pinned", False)              # 取消置顶：upsert 改一行，不新增
    assert log.flags() == {CHAT: {"muted"}, OTHER: {"muted"}}
    try:
        log.set_flag(CHAT, "压根没有这个旗标")
    except ValueError as error:                      # 拼错旗标名要立刻炸，别静悄悄写坏库
        assert "未知旗标" in str(error)
    else:
        raise AssertionError("未知旗标名应当报错")
    log.close()


def test_clear_conversation_only_touches_that_one():
    """清空会话 = 只删这一个会话的消息/回应/草稿，并打 cleared 旗标（启动时要用）。"""
    log = _search_fixture()
    log.toggle_reaction("m1", "👍")                   # m1 属于 CHAT
    log.toggle_reaction("m3", "🎉")                   # m3 属于 OTHER
    log.set_draft(CHAT, "这个会话的草稿")
    log.set_draft(OTHER, "别的会话的草稿")
    log.set_flag(CHAT, "pinned", True)

    removed = log.clear_conversation(CHAT)
    assert removed == 3, removed                      # CHAT 下有 3 条（m1/m2/m4）
    assert log.count(CHAT) == 0 and log.search("美国") == []
    assert log.reactions("m1") == {}                  # 附带清掉回应
    assert log.draft(CHAT) == "" and log.draft(OTHER) == "别的会话的草稿"
    assert log.count(OTHER) == 1                       # 别的会话一条没少
    assert len(log.search("开会", conversation_id=OTHER)) == 1
    assert log.reactions("m3") == {"🎉": 1}           # 别的会话的回应也还在
    flags = log.flags()
    assert "cleared" in flags[CHAT]
    assert "pinned" in flags[CHAT], "只加 cleared，别把已有旗标抹掉"
    assert OTHER not in flags
    log.close()


def test_memory_log_has_no_files():
    log = ChatLog(None)
    log.save_message(_msg("m1", "内存里的", 1.0))
    assert log.load(CHAT)[0].text == "内存里的"
    assert log.stats()["bytes"] == 0                          # 没有文件占用
    log.close()


# ---------------------------------------------------------------------------
# 效率基准（"占用少、跑得快"的实测证据，不是感觉）
# ---------------------------------------------------------------------------
def test_bulk_insert_and_search_performance():
    count = 20000
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bench.db")
        log = ChatLog(path)
        messages = [
            _msg(f"b{i}", f"第{i}条测试消息：这周要去日本和美国的天气怎么样",
                 1000.0 + i * 0.001, conv=CHAT if i % 2 else OTHER)
            for i in range(count)
        ]

        started = time.perf_counter()
        log.save_messages(messages)
        insert_seconds = time.perf_counter() - started

        started = time.perf_counter()
        hits = log.search("美国", limit=50)
        search_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        recent = log.load(CHAT)
        load_ms = (time.perf_counter() - started) * 1000

        raw_mb = log.stats()["bytes"] / 1024 / 1024
        started = time.perf_counter()
        log.compact()                                          # WAL 收回主库 + 重排页
        compact_ms = (time.perf_counter() - started) * 1000
        size_mb = log.stats()["bytes"] / 1024 / 1024
        assert len(log.load(CHAT)) == DEFAULT_LIMIT            # 整理后照样能用
        log.close()

        assert insert_seconds < 2.0, f"2 万条落库用了 {insert_seconds:.2f}s"
        assert len(hits) == 50
        assert search_ms < 50, f"全文检索用了 {search_ms:.1f}ms"
        assert len(recent) == DEFAULT_LIMIT and load_ms < 50, f"取最近 200 条用了 {load_ms:.1f}ms"
        assert size_mb < 6, f"2 万条占了 {size_mb:.1f}MB（compact 前 {raw_mb:.1f}MB）"
        print(f"  [bench] {count} 条：落库 {insert_seconds * 1000:.0f}ms / "
              f"检索 {search_ms:.1f}ms / 取最近 200 条 {load_ms:.1f}ms / "
              f"compact {compact_ms:.0f}ms / 占用 {raw_mb:.1f}MB -> {size_mb:.1f}MB "
              f"（每条 {size_mb * 1024 * 1024 / count:.0f} 字节）")


# ---------------------------------------------------------------------------
# 自写 runner：不装 pytest 也能一条命令跑完
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"  ok    {name}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
