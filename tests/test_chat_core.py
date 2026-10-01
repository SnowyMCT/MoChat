"""核心逻辑层单测（零 Qt 依赖）。

用法：``python tests/test_chat_core.py``；装了 pytest 也可以 ``pytest tests``。

覆盖三块：
    模型与时间文案   —— 预览文案、日期分组、状态文字
    会话仓库         —— 排序、未读、搜索、乱序插入、变更订阅
    传输层           —— 演示回复的排期、台词循环、已读回执、时间过滤
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))         # tests/ 下有 seed.py
import seed                                                            # noqa: E402
from mchat.core import transport                                       # noqa: E402
from mchat.core.crashlog import (CRASH_LOG_NAME,                          # noqa: E402
                                 default_crash_log_path, install_crash_log)                                    # noqa: E402
from mchat.core.models import (ChatKind, Conversation, Message, PinRef,   # noqa: E402
                               User, ellipsize, strip_partial_flag,
                               format_clock, format_day_label,
                               format_list_time, same_day)
from mchat.core.store import (CONFIRM_PREVIEW, MESSAGE, PIN, READ,        # noqa: E402
                              ChatStore, StoreEvent, confirmation_preview)

# 固定一个"现在"，让时间相关断言可重复（2026-09-27 21:30 本地时间）
NOW = time.mktime((2026, 9, 27, 21, 30, 0, 0, 0, -1))


def _noon(days: int) -> float:
    """``days`` 天前的中午 12:00。"""
    return time.mktime((2026, 9, 27 - days, 12, 0, 0, 0, 0, -1))


def _user(name: str, uid: str | None = None, **kw) -> User:
    return User(uid or name.lower(), name, **kw)


_seed_ids = itertools.count(1)


def _message(conv_id: str, text: str, ts: float, *, outgoing=False,
             sender="lin", mid: str | None = None) -> Message:
    """不传 ``mid`` 时自动生成唯一 id。

    以前默认写死成 ``"m"``，于是夹具里三个会话的消息 id 全是 ``"m"`` ——
    "按 id 批量删除"这类测试会被跨会话的同 id 带偏（删一条等于删三个会话的同 id 那条）。
    """
    return Message(id=mid or f"seed-{next(_seed_ids)}", conversation_id=conv_id,
                   sender_id=sender, text=text, ts=ts, outgoing=outgoing)


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


def test_truncation_never_splits_a_flag():
    """截断只能切在整面旗之外 —— 落单的半面旗会被字体画成一个字母方块（像乱码）。

    覆盖三处"取前几十个字"的地方：会话摘要、回复引用快照，以及它们共用的
    :func:`ellipsize` / :func:`strip_partial_flag`。
    """
    flag = "🇯🇵"

    def lone_halves(text: str) -> int:
        """数文里有几个"半面旗"（成对的 regional indicator 不算）。"""
        found = 0
        index = 0
        while index < len(text):
            if 0x1F1E6 <= ord(text[index]) <= 0x1F1FF:
                paired = (index + 1 < len(text)
                          and 0x1F1E6 <= ord(text[index + 1]) <= 0x1F1FF)
                found += 0 if paired else 1
                index += 2 if paired else 1
            else:
                index += 1
        return found

    assert strip_partial_flag("abc" + flag) == "abc" + flag        # 整面旗留着
    assert strip_partial_flag("abc" + flag[0]) == "abc"            # 半面旗去掉
    assert strip_partial_flag("abc\u200d") == "abc"                # 尾巴上的零宽连接符

    cut = "x" * 40 + flag[0]
    assert ellipsize(cut + "尾巴", 42) == "x" * 40 + "…"           # 正好切在半面旗上
    assert ellipsize("x" * 40 + flag + "尾巴", 42) == "x" * 40 + "…"   # 整面旗也放不下

    # 会话摘要（42 字）与回复引用（42 字）都不能切出半面旗
    text = "x" * 40 + flag + "尾巴"
    conv = Conversation("c", "王尼玛", ChatKind.DIRECT, members=[_user("王尼玛", "lin")])
    conv.messages.append(_message("c", text, NOW))
    assert conv.preview_text() == "x" * 40 + "…", repr(conv.preview_text())
    assert lone_halves(store_ref := _store().reply_ref(_message("c-lin", text, NOW)).preview) == 0
    assert store_ref == "x" * 40 + "…", repr(store_ref)


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


def test_set_user_online_flips_frozen_members():
    """好友上线/下线：``User`` 是 frozen dataclass，改状态只能 replace 出新的。

    这条是 bug 回归：原来 ``_apply_presence`` 写的是 ``member.online = ...``，
    在 frozen dataclass 上抛 ``FrozenInstanceError``，而它跑在 ``tick()`` 里 ——
    异常一冒整条事件轮询就断，表现就是"对方上线了，我这边还写着离线"。
    """
    lin = _user("王尼玛", "lin", online=False)
    direct = Conversation("c-lin", "王尼玛", ChatKind.DIRECT, members=[lin])
    group = Conversation("c-group", "天下尼玛是一家", ChatKind.GROUP,
                         members=[_user("曹尼玛", "su", online=True), lin])
    store = ChatStore([direct, group])
    assert direct.subtitle() == "离线"

    # 一个成员在多处出现（单聊 + 群），一次都要改到
    assert store.set_user_online("lin", True) == ["c-lin", "c-group"]
    assert store.get("c-lin").member("lin").online is True
    assert store.get("c-group").member("lin").online is True
    assert store.get("c-lin").subtitle() == "在线"          # 聊天头部第二行跟着变
    assert store.get("c-group").subtitle() == "2 位成员"     # 群聊不显示某人在线

    # 幂等：状态没变就不算"变了"（界面不会白刷一遍）
    assert store.set_user_online("lin", True) == []
    # 离线也能翻回去；不认识的人 / 不认识的会话都不炸
    assert store.set_user_online("lin", False) == ["c-lin", "c-group"]
    assert store.get("c-lin").member("lin").online is False
    assert store.set_user_online("ghost", True) == []
    assert store.set_member_online("不存在的会话", "lin", True) is False
    assert store.set_member_online("c-lin", "ghost", True) is False


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


# ---------------------------------------------------------------------------
# 回复 / 置顶 / 转发（逐条 + 合并）/ 批量删除
# ---------------------------------------------------------------------------
def test_reply_ref_snapshot():
    """引用快照：作者 + 一行摘要，太长截断；自己发的一律显示成"我"。"""
    store = _store()
    incoming = store.get("c-lin").messages[0]
    ref = store.reply_ref(incoming)
    assert ref.message_id == incoming.id
    assert ref.author == "王尼玛" and ref.preview == "昨天的文件"
    assert ref.text == "王尼玛：昨天的文件"
    mine = store.reply_ref(_message("c-lin", "我发的一条", NOW, outgoing=True))
    assert mine.author == "我"
    long_ref = store.reply_ref(_message("c-lin", "很长" * 40, NOW))
    assert len(long_ref.preview) == 42 and long_ref.preview.endswith("…")


def test_peer_name_for_pin_dialog():
    """置顶小窗要写"也为 XX 置顶"：单聊给对方名字，群聊给空串（那边改用"所有群成员"）。"""
    store = _store()
    assert store.get("c-lin").peer_name == "王尼玛"
    assert store.get("c-chen").peer_name.strip() != ""
    # 群聊里"对方"不唯一：peer_name 只是第一个不是你的人，
    # 小窗上的文案由主窗口按会话类型决定（群聊写"所有群成员"）
    assert store.get("c-group").kind is ChatKind.GROUP
    assert store.get("c-group").peer_name != ""


def test_pin_multiple_messages():
    """多条置顶 + 每条都能选"仅我可见 / 双方可见"（Telegram 那两个选项）。"""
    store = _store()
    events: list[str] = []
    store.subscribe(lambda event: events.append(event.kind))
    first = store.get("c-lin").messages[0]
    second = _message("c-lin", "第二条", NOW + 10, mid="m2")
    third = _message("c-lin", "第三条", NOW + 20, mid="m3")
    store.append(second)
    store.append(third)
    assert store.pin_message(first.id) is True                     # 默认：仅我可见
    assert [pin.message_id for pin in store.get("c-lin").pins] == [first.id]
    assert store.pin_ref("c-lin", first.id).for_both is False
    assert store.pin_message(second.id, for_both=True) is True     # 这条对方也置顶
    assert [(pin.message_id, pin.for_both) for pin in store.get("c-lin").pins] == [
        (second.id, True), (first.id, False)]
    assert store.pin_ref("c-lin", second.id).label == "双方可见"
    assert store.pin_ref("c-lin", first.id).label == "仅我可见"
    assert store.pin_message(third.id) is True
    assert [pin.message_id for pin in store.get("c-lin").pins] == [third.id, second.id, first.id]
    # 同一可见范围再点一次 = 取消这一条，别的留着
    assert store.pin_message(second.id, for_both=True) is True
    assert [pin.message_id for pin in store.get("c-lin").pins] == [third.id, first.id]
    assert store.is_pinned("c-lin", second.id) is False
    # 换了可见范围 = 就地改这一条（不新增、也不取消）
    assert store.pin_message(first.id, for_both=True) is True
    assert store.pin_ref("c-lin", first.id).for_both is True
    assert [pin.message_id for pin in store.get("c-lin").pins] == [first.id, third.id]
    # pinned_entries 给出 (消息, PinRef)，顺序跟着置顶顺序
    entries = store.pinned_entries("c-lin")
    assert [(m.id, pin.for_both) for m, pin in entries] == [(first.id, True), (third.id, False)]
    assert store.unpin_message(first.id) is True                   # ✕ 取消某一条
    assert [pin.message_id for pin in store.get("c-lin").pins] == [third.id]
    assert store.unpin_message(first.id) is False                  # 已经没置顶了
    assert store.pin_message("不存在的消息", for_both=True) is False
    assert PIN in events


def test_forward_one_by_one():
    """逐条转发：一条对一条、带上来源、正文与附件跟着走、时间戳递增。"""
    store = _store()
    store.append(_message("c-lin", "第二条", NOW + 1, mid="m2"))
    source = [message.id for message in store.get("c-lin").messages]
    assert len(source) == 2
    before = len(store.get("c-group").messages)
    created = store.forward(source, "c-group", now=5000.0)
    assert len(created) == len(source) == len(store.get("c-group").messages) - before
    assert all(message.outgoing and message.conversation_id == "c-group"
               for message in created)
    assert [m.forwarded_from for m in created] == ["王尼玛"] * len(source)
    assert [m.text for m in created] == [m.text for m in store.get("c-lin").messages]
    assert created[0].ts < created[1].ts
    assert created[0].is_forwarded and not created[0].is_merged


def test_forward_merged():
    """合并转发：只生成一条，内容是"N 条消息"，附件名也带上。"""
    store = _store()
    store.append(_message("c-lin", "", NOW + 5, mid="m2"))       # 纯附件式空消息
    source = [message.id for message in store.get("c-lin").messages]
    assert len(source) == 2
    created = store.forward(source, "c-group", merge=True, now=6000.0)
    assert len(created) == 1
    merged = created[0]
    assert merged.is_merged and merged.merged_count == 2
    assert merged.forwarded_from == "王尼玛"
    assert merged.text == "" and merged.merged_items[0].author == "王尼玛"
    assert merged.merged_line(0) == "王尼玛：昨天的文件"
    assert merged.preview_text == "【合并转发】2 条消息"
    # 目标会话只多了一条
    assert len([m for m in store.get("c-group").messages if m.is_merged]) == 1


def test_forward_edge_cases():
    """转发的边界：空列表 / 不存在的目标 / 不存在的消息都不该出事。"""
    store = _store()
    assert store.forward([], "c-group") == []
    assert store.forward(["没有这条"], "c-group") == []
    assert store.forward([store.get("c-lin").messages[0].id], "没有这个会话") == []
    assert len(store.get("c-group").messages) == 1


def test_delete_messages_deletes_anyone():
    """本地删除（不是撤回）：**别人发的也能删**，删的是本机这份记录。"""
    store = _store()
    store.append(_message("c-lin", "我的一条", NOW + 1, outgoing=True, mid="m2"))
    store.append(_message("c-lin", "我的另一条", NOW + 2, outgoing=True, mid="m3"))
    other = store.get("c-lin").messages[0]                   # 别人发的（种子那条）
    mine = [m for m in store.get("c-lin").messages if m.outgoing]
    store.pin_message(other.id)                              # 置顶一条"别人的"消息
    removed = store.delete_messages([m.id for m in mine] + [other.id])
    assert removed == 3                                      # 三条都删掉了，不分谁发的
    assert store.get("c-lin").messages == []
    assert store.message(other.id) is None
    assert store.get("c-lin").pins == ()                      # 置顶跟着清掉
    assert store.delete_messages(["没有这条"]) == 0


def test_delete_messages_only_mine_switch():
    """``only_mine=True`` 留着"只清自己那几条"的场景（默认是都能删）。"""
    store = _store()
    store.append(_message("c-lin", "我的一条", NOW + 1, outgoing=True, mid="m2"))
    other = store.get("c-lin").messages[0]
    mine = [m for m in store.get("c-lin").messages if m.outgoing]
    assert store.delete_messages([m.id for m in mine] + [other.id], only_mine=True) == 1
    assert store.get("c-lin").messages == [other]            # 别人的那条留着
    assert store.message(other.id) is not None


def test_confirmation_preview():
    """确认弹窗里的摘要：一行一条、超长截断、空消息有兜底文案。"""
    store = _store()
    lines = confirmation_preview([_message("c-lin", "很短", NOW),
                                  _message("c-lin", "长" * 60, NOW)]).split("\n")
    assert lines[0] == "· 很短"
    assert len(lines[1]) == 2 + CONFIRM_PREVIEW + 1 and lines[1].endswith("…")
    assert confirmation_preview([_message("c-lin", "", NOW)]) == "· （空消息）"


def test_forward_payload_reaches_transport():
    """合并转发自己没有 text，但**发出去时必须有载荷**。

    传输层是拿 text 判"空消息"的：空消息既不触发对方"正在输入…"，也不给已读回执 ——
    转发过去的东西因此会永远停在单勾、对面像没收到。这条测试把载荷与排期钉住。
    """
    store = _store()
    source_message = store.get("c-lin").messages[0]
    store.append(_message("c-lin", "", NOW + 5, mid="m2"))
    merged = store.forward([m.id for m in store.get("c-lin").messages], "c-group",
                           merge=True, now=6000.0)[0]
    assert merged.text == ""                                  # 正文是空的……
    assert merged.send_text == "【合并转发】2 条消息"           # ……载荷不能是空的
    assert merged.merged_items[0].clock == format_clock(source_message.ts)   # 时间也带上了
    # 普通消息原样；纯附件消息仍然返回空串（那种情况由 attachment 证明它不是空消息）
    assert _message("c-lin", "普通消息", NOW).send_text == "普通消息"
    assert _message("c-lin", "", NOW).send_text == ""
    # 拿这句载荷喂传输层：typing + 回复 + 已读，三个事件都得排上
    pipe = transport.LocalTransport({"c-group": transport.ReplyScript("su", ("收到",))},
                                    clock=lambda: 0.0)
    pipe.send(transport.Outgoing("c-group", "me", merged.send_text, merged.id))
    assert pipe.pending() == 3


def test_crash_log_keeps_evidence():
    """崩溃要留现场：致命信号写栈、未捕获异常写 traceback（都是"窗口突然消失"时唯一的线索）。"""
    # 文件名带上进程号：%TEMP% 里那份固定名字的日志是**故意留着不删**的
    # （faulthandler 抓着句柄），同一个进程里第二次跑到这里删除就会 PermissionError
    # —— 换唯一名字，测试之间不再互相踩。
    path = os.path.join(tempfile.gettempdir(), f"mchat-crash-test-{os.getpid()}.log")
    old_hook = sys.excepthook
    try:
        assert install_crash_log(path) == path
        assert os.path.isfile(path)                      # 文件建起来了（faulthandler 已装）
        assert default_crash_log_path(r"C:\data").endswith(CRASH_LOG_NAME)
        try:
            raise RuntimeError("模拟一次崩溃")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())              # 模拟"没人接住的异常"
        text = open(path, encoding="utf-8").read()
        assert "未捕获异常" in text and "RuntimeError" in text
        assert "模拟一次崩溃" in text
    finally:
        sys.excepthook = old_hook                        # 别把全局钩子留在测试进程里
        # 日志文件不删：faulthandler 还拿着它的句柄（Windows 上删不掉，也不该删 ——
        # 它正是"崩溃现场"，下次崩溃会接着往同一个文件写）。


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
