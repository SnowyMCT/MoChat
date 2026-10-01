"""好友系统的端到端回归：门禁 / 申请 / 同意 / 删除。

和 ``tests/test_server.py`` 一个路子：起一个**真服务端**（``tests/server_fixture.py``），
用真客户端把整条产品流程跑一遍。这里盯的是"好友"这套新语义：

    门禁 —— 还不是好友时在单聊里发消息**不落库**，变成对方的一条好友申请；
            对方只收到 friend_request 事件，收不到 message 事件。
    列表 —— 申请方在 ``outgoing``、被申请方在 ``incoming`` 里看到，附言原样带上。
    同意 —— accept 之后：申请附言成为这条单聊的**第一条消息**（发送者是申请人），
            两人互为好友，之后互发消息照常送达。
    删除 —— remove 之后好友列表清空、再发消息又变成好友申请，但**历史消息仍在**。

好友申请空附言时不落消息（做前置用 ``server_fixture.make_friends``）；非空附言
（包括门禁把消息正文当留言）才会在同意时落成一条消息 —— 这一点在下面专门断言。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest                                                            # noqa: E402
from mchat.core.api import ApiClient                                     # noqa: E402
from server_fixture import make_friends, running_server                  # noqa: E402

PW = "secret123"


@pytest.fixture(scope="module")
def server():
    with running_server() as process:
        yield process


def register(base_url, username, display=""):
    api = ApiClient(base_url)
    payload = api.register(username, PW, display or username)
    api.token = str(payload.get("token") or "")
    return api


def prime(api) -> int:
    """把事件游标落到"现在"（真客户端连上就先这么同步一次）。"""
    return int(api.sync(0, timeout=1).get("cursor") or 0)


def drain(api, cursor, kind, timeout=10.0):
    """长轮询直到收到 ``kind`` 的事件或超时，返回 ``(全部事件, 新游标)``。"""
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = api.sync(cursor, timeout=2)
        cursor = int(data.get("cursor") or cursor)
        events.extend(data.get("events") or [])
        if any(event.get("kind") == kind for event in events):
            break
    return events, cursor


# --------------------------------------------------------------------------- 门禁
def test_message_to_a_non_friend_is_turned_into_a_request(server):
    """非好友的单聊消息：不落库、变申请，对方收不到 message 事件。"""
    alice = register(server.base_url, "nf_alice", "爱丽丝")
    bob = register(server.base_url, "nf_bob", "鲍勃")
    cid = alice.create_direct("nf_bob")["id"]
    cursor = prime(bob)

    result = alice.send_message(cid, "a" * 32, "我们先加个好友吧")

    assert result.intercepted is True, "非好友的单聊消息必须被门禁拦下"
    assert result.friend_request is not None
    assert not result, "被拦下的消息服务端没落库，返回里不该有 message"
    request = result.friend_request
    assert request["user"]["username"] == "nf_bob", "friend_request 里的 user 是对方"
    assert request["note"] == "我们先加个好友吧", request

    # 两边都翻不到这条消息（门禁根本没写库）
    assert alice.messages(cid)["messages"] == []
    assert bob.messages(cid)["messages"] == []

    # bob 只收到 friend_request 事件，没有 message 事件
    events, _ = drain(bob, cursor, kind="friend_request")
    kinds = [event.get("kind") for event in events]
    assert "friend_request" in kinds, kinds
    assert "message" not in kinds, kinds


def test_both_sides_see_the_request_with_the_note(server):
    """申请方看 outgoing、被申请方看 incoming，附言原样带上。"""
    alice = register(server.base_url, "list_alice", "爱丽丝")
    bob = register(server.base_url, "list_bob", "鲍勃")
    cid = alice.create_direct("list_bob")["id"]
    alice.send_message(cid, "b" * 32, "你好，我是爱丽丝")

    incoming = bob.friends()
    assert incoming["friends"] == []
    assert [item["user"]["username"] for item in incoming["incoming"]] == ["list_alice"]
    assert incoming["incoming"][0]["note"] == "你好，我是爱丽丝", incoming["incoming"]

    outgoing = alice.friends()
    assert outgoing["friends"] == []
    assert [item["user"]["username"] for item in outgoing["outgoing"]] == ["list_bob"]
    assert outgoing["outgoing"][0]["note"] == "你好，我是爱丽丝", outgoing["outgoing"]


# --------------------------------------------------------------------------- 同意
def test_accept_materialises_the_note_as_the_first_message(server):
    """同意申请：附言成为这条单聊的第一条消息，发送者是申请人。"""
    alice = register(server.base_url, "acc_alice", "爱丽丝")
    bob = register(server.base_url, "acc_bob", "鲍勃")
    cid = alice.create_direct("acc_bob")["id"]
    alice.send_message(cid, "c" * 32, "一起打游戏吗")

    accepted = bob.friend_accept("acc_alice")
    assert accepted["status"] == "accepted", accepted
    assert accepted["conversation"]["id"] == cid, "复用门禁那次已经建好的会话"

    messages = bob.messages(cid)["messages"]
    assert [message["text"] for message in messages] == ["一起打游戏吗"], messages
    assert messages[0]["sender_name"] == "爱丽丝"
    assert messages[0]["sender_id"] == alice.me()["user"]["id"], "留言是申请人说的"
    assert messages[0]["sender_id"] != bob.me()["user"]["id"]

    listing = bob.friends()
    assert [item["user"]["username"] for item in listing["friends"]] == ["acc_alice"]
    assert listing["incoming"] == [], "同意之后申请就不该继续挂着"


def test_friends_can_chat_normally(server):
    """加完好友：消息正常双向送达，不再被门禁拦。"""
    alice = register(server.base_url, "fc_alice", "爱丽丝")
    bob = register(server.base_url, "fc_bob", "鲍勃")
    make_friends(alice, bob)

    listing = alice.friends()
    assert [item["user"]["username"] for item in listing["friends"]] == ["fc_bob"]
    assert listing["incoming"] == [] and listing["outgoing"] == []

    cid = alice.create_direct("fc_bob")["id"]
    cursor = prime(bob)
    sent = alice.send_message(cid, "d" * 32, "现在能发了")
    assert sent.friend_request is None, "好友之间不该再被拦"
    assert sent["id"] == "d" * 32 and sent["text"] == "现在能发了"

    events, _ = drain(bob, cursor, kind="message")
    assert "现在能发了" in [event["message"]["text"] for event in events
                            if event.get("kind") == "message"], events

    alice_cursor = prime(alice)
    bob.send_message(cid, "e" * 32, "收到了")
    events, _ = drain(alice, alice_cursor, kind="message")
    assert "收到了" in [event["message"]["text"] for event in events
                        if event.get("kind") == "message"], events


# --------------------------------------------------------------------------- 删除
def test_remove_blocks_again_but_keeps_history(server):
    """删好友：列表清空、再发消息又变申请，但历史消息一条不少。"""
    alice = register(server.base_url, "rm_alice", "爱丽丝")
    bob = register(server.base_url, "rm_bob", "鲍勃")
    make_friends(alice, bob)
    cid = alice.create_direct("rm_bob")["id"]
    alice.send_message(cid, "f" * 32, "好友时期的一条消息")

    bob.friend_remove("rm_alice")
    assert bob.friends()["friends"] == []
    assert alice.friends()["friends"] == [], "删好友是双向的（两边都不再是好友）"

    cursor = prime(bob)
    result = alice.send_message(cid, "g" * 32, "删了还能发吗")
    assert result.intercepted is True, "不再是好友，消息又该被拦成申请"
    assert result.friend_request["note"] == "删了还能发吗"

    texts = [message["text"] for message in alice.messages(cid)["messages"]]
    assert "好友时期的一条消息" in texts, "历史消息不能因为删好友就没了"
    assert "删了还能发吗" not in texts, "被拦下的消息不落库"

    assert [item["user"]["username"] for item in bob.friends()["incoming"]] == ["rm_alice"]
    events, _ = drain(bob, cursor, kind="friend_request")
    assert "message" not in [event.get("kind") for event in events], events
