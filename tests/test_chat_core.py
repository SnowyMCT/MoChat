"""核心逻辑层单测（零 Qt 依赖）。

用法：``python tests/test_chat_core.py``；装了 pytest 也可以 ``pytest tests``。

覆盖三块：
    模型与时间文案   —— 预览文案、日期分组、状态文字
    会话仓库         —— 排序、未读、搜索、乱序插入、变更订阅
    传输层           —— 演示回复的排期、台词循环、已读回执、时间过滤
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.core import seed, transport                                    # noqa: E402
from mchat.core.models import (ChatKind, Conversation, Message, User,     # noqa: E402
                               format_clock, format_day_label,
                               format_list_time, same_day)
from mchat.core.store import MESSAGE, READ, ChatStore, StoreEvent         # noqa: E402

# 固定一个"现在"，让时间相关断言可重复（2026-09-27 21:30 本地时间）
NOW = time.mktime((2026, 9, 27, 21, 30, 0, 0, 0, -1))


def _noon(days: int) -> float:
    """``days`` 天前的中午 12:00。"""
    return time.mktime((2026, 9, 27 - days, 12, 0, 0, 0, 0, -1))


def _user(name: str, uid: str | None = None, **kw) -> User:
    return User(uid or name.lower(), name, **kw)


def _message(conv_id: str, text: str, ts: float, *, outgoing=False,
             sender="lin", mid="m") -> Message:
    return Message(id=mid, conversation_id=conv_id, sender_id=sender, text=text,
                   ts=ts, outgoing=outgoing)


# ---------------------------------------------------------------------------
# 模型与时间文案
# ---------------------------------------------------------------------------
def test_format_clock_and_days():
    assert format_clock(NOW) == "21:30", format_clock(NOW)
    assert format_day_label(NOW, NOW) == "今天"
    assert format_day_label(_noon(1), NOW) == "昨天"
    assert format_day_label(_noon(5), NOW) == "09月22日", format_day_label(_noon(5), NOW)
    old = time.mktime((2024, 3, 8, 9, 0, 0, 0, 0, -1))
    assert format_day_label(old, NOW) == "2024年03月08日", format_day_label(old, NOW)


def test_format_list_time():
    assert format_list_time(NOW, NOW) == "21:30"
    assert format_list_time(_noon(1), NOW) == "昨天"
    assert format_list_time(_noon(4), NOW) == "09/23", format_list_time(_noon(4), NOW)


def test_same_day():
    assert same_day(NOW, NOW)
    assert not same_day(_noon(1), NOW)


def test_conversation_preview_and_subtitle():
    lin = _user("王尼玛", "lin", online=True)
    me = User("me", "Monostar")
    conv = Conversation("c", "王尼玛", ChatKind.DIRECT, members=[me, lin])
    assert conv.preview_text() == "还没有消息"
    conv.messages.append(_message("c", "文件放共享盘了", NOW, sender="lin"))
    assert conv.preview_text() == "文件放共享盘了"
    assert conv.subtitle() == "在线"
    assert conv.subtitle(typing=True) == "正在输入…"
    conv.messages.append(_message("c", "我看看", NOW + 5, outgoing=True, sender="me"))
    assert conv.preview_text() == "我: 我看看", conv.preview_text()
    assert conv.member("lin").name == "王尼玛"
    assert conv.sender_name(conv.messages[0]) == "王尼玛"
    assert conv.sender_name(_message("c", "x", NOW, sender="ghost")) == "未知用户"


def test_group_preview_has_sender_prefix():
    me = User("me", "Monostar")
    su = _user("曹尼玛", "su")
    conv = Conversation("g", "天下尼玛是一家", ChatKind.GROUP, members=[me, su])
    conv.messages.append(_message("g", "我下午补一版交互稿", NOW, sender="su"))
    assert conv.preview_text() == "曹尼玛: 我下午补一版交互稿", conv.preview_text()
    assert conv.subtitle() == "2 位成员"
    assert conv.initial == "天"
    assert su.initial == "曹"


def test_preview_truncated():
    conv = Conversation("c", "王尼玛", ChatKind.DIRECT, members=[_user("王尼玛", "lin")])
    conv.messages.append(_message("c", "很长的消息" * 20, NOW))
    assert conv.preview_text().endswith("…")
    assert len(conv.preview_text()) <= 42


# ---------------------------------------------------------------------------
# 会话仓库
# ---------------------------------------------------------------------------
def _store() -> ChatStore:
    lin = Conversation("c-lin", "王尼玛", ChatKind.DIRECT,
                       members=[_user("王尼玛", "lin")], pinned=True)
    lin.messages.append(_message("c-lin", "昨天的文件", _noon(1)))
    group = Conversation("c-group", "天下尼玛是一家", ChatKind.GROUP,
                         members=[_user("曹尼玛", "su")])
    group.messages.append(_message("c-group", "今天开会", NOW))
    chen = Conversation("c-chen", "尼玛", ChatKind.DIRECT,
                        members=[_user("尼玛", "chen")])
    chen.messages.append(_message("c-chen", "周五评审",
                                  time.mktime((2026, 9, 26, 15, 0, 0, 0, 0, -1))))
    return ChatStore([lin, group, chen])


def test_store_ordering():
    store = _store()
    titles = [c.title for c in store.conversations()]
    assert titles[0] == "王尼玛", titles          # 置顶优先
    assert titles[1:] == ["天下尼玛是一家", "尼玛"], titles


def test_store_unread_bookkeeping():
    store = _store()
    assert store.total_unread() == 0
    store.bump_unread("c-group")
    store.bump_unread("c-group")
    assert store.unread("c-group") == 2
    assert store.total_unread() == 2
    assert store.mark_read("c-group") is True
    assert store.unread("c-group") == 0
    assert store.mark_read("c-group") is False      # 已经读过，不算变化
    store.set_unread("c-chen", 7)
    assert store.total_unread() == 7
    store.set_unread("c-chen", -5)                  # 负数被夹到 0
    assert store.unread("c-chen") == 0


def test_store_receive_only_counts_incoming():
    store = _store()
    store.receive(_message("c-lin", "收到的", NOW + 10, mid="n1"))
    assert store.unread("c-lin") == 1
    store.receive(_message("c-lin", "我发的", NOW + 20, outgoing=True, mid="n2"))
    assert store.unread("c-lin") == 1, "自己发的不该进未读"
    assert len(store.get("c-lin").messages) == 3


def test_store_append_keeps_order():
    store = _store()
    store.append(_message("c-lin", "迟到的消息", _noon(1) - 60, mid="late"))
    texts = [m.text for m in store.get("c-lin").messages]
    assert texts[0] == "迟到的消息", texts       # 时间更早 -> 插到最前
    assert texts[-1] == "昨天的文件"


def test_store_search():
    store = _store()
    assert len(store.search("")) == 3
    assert [c.id for c in store.search("评审")] == ["c-chen"], "应能按消息内容搜到"
    assert [c.id for c in store.search("开会")] == ["c-group"], "应能搜到消息内容"
    assert store.search("不存在的关键词") == []


def test_store_events():
    store = _store()
    events: list[StoreEvent] = []
    unsubscribe = store.subscribe(events.append)

    store.append(_message("c-group", "新消息", NOW + 30, mid="e1"))
    assert [e.kind for e in events] == [MESSAGE]
    assert events[0].message.text == "新消息"
    assert events[0].conversation_id == "c-group"

    store.bump_unread("c-group")        # 制造未读，否则 mark_read 不算变化
    store.mark_read("c-group")
    store.set_read("c-group", "e1")
    kinds = [e.kind for e in events]
    assert kinds[-2:] == [READ, MESSAGE], kinds
    assert events[-1].message.is_read, "已读回执要带上那条消息，界面才刷得动气泡"

    unsubscribe()
    before = len(events)
    store.append(_message("c-group", "取消订阅后", NOW + 60, mid="e2"))
    assert len(events) == before, "取消订阅后不该再收到事件"


def test_store_empty_conversation_defaults():
    store = ChatStore()
    assert store.conversations() == []
    assert store.get("nope") is None
    store.append(_message("nope", "丢弃", NOW))     # 不存在的会话：静默忽略
    assert store.conversations() == []


# ---------------------------------------------------------------------------
# 传输层（单机演示）
# ---------------------------------------------------------------------------
def _transport() -> transport.LocalTransport:
    scripts = {
        "c-a": transport.ReplyScript("bot", ("第一句", "第二句"), latency=2.0,
                                     typing_lead=0.5),
    }
    return transport.LocalTransport(scripts)


def test_transport_schedules_typing_then_reply():
    client = _transport()
    start = time.time()
    client.send(transport.Outgoing("c-a", "me", "在吗", "m-1"))
    assert client.pending() == 3, client.pending()      # typing + 回复 + 已读回执

    assert client.poll(start - 1) == [], "时间没到不该有事件"
    typing = client.poll(start + 0.55)
    assert [e.kind for e in typing] == [transport.EVENT_TYPING]
    assert typing[0].typing is True

    rest = client.poll(start + 10.0)
    kinds = [e.kind for e in rest]
    assert kinds == [transport.EVENT_READ, transport.EVENT_MESSAGE], kinds
    assert {e.text for e in rest if e.text} == {"第一句"}
    assert client.pending() == 0


def test_transport_lines_cycle_and_unknown_chat_is_ignored():
    client = _transport()
    start = time.time()
    for _ in range(2):
        client.send(transport.Outgoing("c-a", "me", "喂"))
    texts = [e.text for e in client.poll(start + 99) if e.text]
    assert texts == ["第一句", "第二句"], texts

    before = client.pending()
    client.send(transport.Outgoing("c-unknown", "me", "没人听"))
    assert client.pending() == before, "没有脚本的会话不该排期"
    client.send(transport.Outgoing("c-a", "me", "   "))
    assert client.pending() == before, "空消息不该排期"


def test_transport_is_interface_compatible():
    from mchat.core.transport import ChatTransport
    client = _transport()
    assert isinstance(client, ChatTransport)
    assert client.name == "本机演示" and client.online is True
    client.start()
    client.stop()


def test_transport_custom_clock():
    ticks = [100.0]
    client = transport.LocalTransport(
        {"c-a": transport.ReplyScript("bot", ("好",), latency=1.0, typing_lead=0.1)},
        clock=lambda: ticks[0])
    client.send(transport.Outgoing("c-a", "me", "hi"))
    assert client.poll() == []              # 假时钟还停在 100.0
    ticks[0] = 101.5
    assert len(client.poll()) == 2          # 回复 + 已读回执


# ---------------------------------------------------------------------------
# 演示数据
# ---------------------------------------------------------------------------
def test_seed_demo_shape():
    demo = seed.build_demo(now=NOW)
    store = demo.store
    convs = store.conversations()
    assert len(convs) == 5, [c.title for c in convs]
    assert convs[0].id == seed.CHAT_LIN, "置顶会话应排第一"
    assert store.total_unread() == 8, store.total_unread()
    assert store.get(seed.CHAT_BOT).is_group is False
    assert store.get(seed.CHAT_GROUP).kind is ChatKind.GROUP
    assert store.get(seed.CHAT_GROUP).subtitle() == "5 位成员"
    assert demo.default_chat_id in {c.id for c in convs}
    assert demo.me.is_me


def test_seed_messages_are_consistent():
    demo = seed.build_demo(now=NOW)
    ids = []
    for conv in demo.store.conversations():
        assert conv.messages, f"{conv.title} 没有消息"
        stamps = [m.ts for m in conv.messages]
        assert stamps == sorted(stamps), f"{conv.title} 消息时间顺序不对"
        for msg in conv.messages:
            assert msg.conversation_id == conv.id
            assert msg.sender_id in {u.id for u in conv.members}
            assert msg.time_text.count(":") == 1
            ids.append(msg.id)
    assert len(ids) == len(set(ids)), "消息 id 必须唯一"


def test_seed_timestamps_group_into_days():
    demo = seed.build_demo(now=NOW)
    group = demo.store.get(seed.CHAT_GROUP)
    labels = {format_day_label(m.ts, NOW) for m in group.messages}
    assert labels == {"今天"}, labels
    lin = demo.store.get(seed.CHAT_LIN)
    assert {format_day_label(m.ts, NOW) for m in lin.messages} == {"昨天"}


def test_seed_scripts_cover_conversations():
    demo = seed.build_demo(now=NOW)
    scripted = set(demo.scripts)
    assert scripted == {c.id for c in demo.store.conversations()}, scripted
    for conv_id, script in demo.scripts.items():
        conv = demo.store.get(conv_id)
        assert script.sender_id in {u.id for u in conv.members}, conv_id
        assert script.lines, f"{conv_id} 没有台词"


# ---------------------------------------------------------------------------
# 自写 runner：不装 pytest 也能一条命令跑完
# ---------------------------------------------------------------------------
def _conv(conv_id: str = "c1", texts=()) -> Conversation:
    conv = Conversation(id=conv_id, title="测试会话", kind=ChatKind.DIRECT,
                        members=[_user("我", "me"), _user("对方", "other")])
    conv.messages.extend(_message(conv_id, text, ts, mid=mid)
                         for text, ts, mid in texts)
    return conv


def test_hydrate_merges_sorts_and_counts_new():
    """回填：按时间排好、同 id 保留更新鲜的那份、返回值只数"新来的"。"""
    store = ChatStore([_conv("c1", [("旧的一", 100.0, "m1"), ("旧二", 200.0, "m2")])])
    added = store.hydrate("c1", [
        _message("c1", "更早的", 50.0, mid="m0"),
        _message("c1", "历史里的旧二", 150.0, mid="m2"),      # 同 id 但更旧 -> 不用
        _message("c1", "新的", 300.0, mid="m3"),
    ])
    assert added == 2, added
    assert [m.id for m in store.get("c1").messages] == ["m0", "m1", "m2", "m3"]
    assert store.get("c1").messages[1].text == "旧的一"


def test_hydrate_replaces_when_history_is_newer():
    store = ChatStore([_conv("c1", [("内存里的", 100.0, "m1")])])
    store.hydrate("c1", [_message("c1", "库里更新的", 500.0, mid="m1")])
    assert store.get("c1").messages[0].text == "库里更新的"


def test_hydrate_unknown_conversation_is_noop():
    store = ChatStore([_conv("c1")])
    assert store.hydrate("没有这个会话", [_message("x", "hi", 1.0, mid="mx")]) == 0


def test_remove_message():
    store = ChatStore([_conv("c1", [("一", 1.0, "m1"), ("二", 2.0, "m2")])])
    assert store.remove("m1") is True
    assert [m.id for m in store.get("c1").messages] == ["m2"]
    assert store.remove("m1") is False                     # 再删就没有了
    assert store.remove("压根不存在") is False


def test_set_pinned_and_muted():
    store = ChatStore([_conv("c1", [("一", 1.0, "m1")]), _conv("c2", [("二", 2.0, "m2")])])
    assert [c.id for c in store.conversations()] == ["c2", "c1"]     # 按最近消息排序
    assert store.set_pinned("c1", True) is True
    assert [c.id for c in store.conversations()] == ["c1", "c2"]     # 置顶的排前面
    assert store.set_pinned("c1", True) is False                     # 状态没变就不发事件
    assert store.set_pinned("不存在", True) is False
    assert store.set_muted("c2", True) is True
    assert store.get("c2").muted is True
    assert store.set_muted("c2", False) is True


def test_total_unread_skips_muted():
    store = ChatStore([_conv("c1", [("一", 1.0, "m1")]), _conv("c2", [("二", 2.0, "m2")])])
    store.set_unread("c1", 2)
    store.set_unread("c2", 3)
    assert store.total_unread() == 5
    store.set_muted("c2", True)
    assert store.total_unread() == 2, "免打扰的会话不该计进顶部徽标"
    assert store.unread("c2") == 3, "但它自己的未读数还在"


def test_clear_messages_only_one_conversation():
    store = ChatStore([_conv("c1", [("一", 1.0, "m1"), ("二", 2.0, "m2")]),
                       _conv("c2", [("三", 3.0, "m3")])])
    store.set_unread("c1", 4)
    assert store.clear_messages("c1") == 2
    assert store.get("c1").messages == []            # 这个会话空了
    assert store.unread("c1") == 0
    assert len(store.get("c2").messages) == 1        # 别的会话纹丝不动
    assert store.clear_messages("不存在") == 0


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
