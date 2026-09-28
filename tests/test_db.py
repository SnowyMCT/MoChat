"""本地历史库（SQLite + FTS5）的单测，零 Qt 依赖。

用法：``python tests/test_db.py``；装了 pytest 也可以 ``pytest tests``。

覆盖：
    落库与读取 —— 幂等 upsert、时间正序、limit 取最近 N 条、按会话隔离
    全文检索   —— 中文子串、英文子串、短词（<3 字符走 LIKE）、片段、上限、无命中
    互动与草稿 —— 表情开关与计数、整会话一次取、草稿增删查
    维护操作   —— 删除消息连互动一起删、清空历史、stats
    效率基准   —— 2 万条批量落库耗时、检索耗时、文件占用（"占用少、效率高"的实测证据）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.core.db import (DEFAULT_LIMIT, SEARCH_LIMIT, TRIGRAM_MIN,  # noqa: E402
                           ChatLog)
from mchat.core.models import Message                                  # noqa: E402

CHAT = "chat-a"
OTHER = "chat-b"


def _msg(mid: str, text: str, ts: float, *, conv: str = CHAT, outgoing: bool = False,
         sender: str = "lin") -> Message:
    return Message(id=mid, conversation_id=conv, sender_id=sender, text=text, ts=ts,
                   outgoing=outgoing)


def _log(**kw) -> ChatLog:
    return ChatLog(kw.pop("path", ":memory:"))


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
