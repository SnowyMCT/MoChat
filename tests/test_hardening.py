"""部署加固项的回归测试（安全评估之后的改动）。

这个文件盯的是两类事：

* **客户端**：附件名是发送者给的，它绝不能把落地路径带到下载目录外面去
  （以前的写法能把它写成 ``…\\Startup\\evil.bat``，本机什么都不点就被写盘），
  超过 16MB 的附件也不该自动下载；
* **服务端**：出厂默认就要"能放出去"—— 注册要邀请码、登录有闸门、只认
  ``Authorization`` 头、正文/合并转发/附件配额都有上限、typing 有节流、
  ``--token-ttl-days 0`` 真的是永不过期、出错不回内部细节。

用真服务端跑（和 ``tests/test_server.py`` 一个路子）：``tests/server_fixture.py``。
绝大多数的闸门验完就关，只有 ``default_server`` / 那几个 function 级夹具在验闸门本身。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mchat.core.api import ApiClient, ApiError, NetworkError                          # noqa: E402
from mchat.core.models import Attachment, MergedItem                              # noqa: E402
from mchat.core.net_transport import AUTO_DOWNLOAD_MAX, NetworkTransport           # noqa: E402
from mchat.core.paths import is_inside, resolve_attachment_path, safe_local_name   # noqa: E402
from server_fixture import make_friends, running_server                            # noqa: E402

WINDOWS_STARTUP_ATTACK = (r"..\..\..\Microsoft\Windows\Start Menu\Programs\Startup"
                          r"\evil.bat")
CRLF_ATTACK = "t.txt\r\nSet-Cookie: injected=1\r\nContent-Length: 0\r\n\r\n"


# --------------------------------------------------------------------------- 工具
def register(base_url: str, username: str, password: str = "secret123",
             display: str = "", invite: str = "") -> ApiClient:
    """注册一个账号并把令牌装好，返回可直接用的客户端。"""
    api = ApiClient(base_url)
    payload = api.register(username, password, display, invite)
    api.token = str(payload["token"])
    return api


def _free_port() -> int:
    """让操作系统分一个空闲端口（测试里要手动拉起服务端时用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def raw_request(base_url: str, path: str, method: str = "GET", body: bytes | None = None,
                token: str = "", content_type: str = "application/octet-stream",
                timeout: float = 10.0) -> bytes:
    """裸 socket 发一个请求，把**原始响应**整段拿回来（要检查响应头就得看原文）。"""
    host, _, port = base_url.split("://", 1)[1].partition(":")
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
    if token:
        lines.append("Authorization: Bearer " + token)
    if body is not None:
        lines.append("Content-Type: " + content_type)
        lines.append("Content-Length: %d" % len(body))
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b""))
    chunks = []
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (ConnectionAbortedError, ConnectionResetError):
        # 服务端回完 503 就关连接，Windows 上会以 RST 结束 —— 已经收到的字节照用
        pass
    finally:
        sock.close()
    return b"".join(chunks)


def split_response(raw: bytes) -> tuple[str, bytes]:
    """``(响应头原文, 响应体)``。"""
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode("latin-1"), body


def header_names(head: str) -> list[str]:
    """响应头里的字段名（小写）。只认"行首就是字段名"的行 —— 响应拆分注进去的行会现形。"""
    text = head.replace("\r\n", "\n").replace("\r", "\n")
    names = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        name, sep, _ = line.partition(":")
        names.append(name.strip().lower() if sep else "?不是合法头部:%s" % line.strip())
    return names[1:]                     # 第一行是状态行


def post_json(base_url: str, path: str, payload: dict, token: str = "") -> tuple[int, dict]:
    """按协议 POST 一段 JSON，返回 ``(状态码, 解析后的 body)``。"""
    raw = raw_request(base_url, path, method="POST",
                      body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      token=token, content_type="application/json; charset=utf-8")
    head, body = split_response(raw)
    try:
        status = int(head.split()[1])
        data = json.loads(body.decode("utf-8"))
    except (IndexError, ValueError):
        return 0, {"raw": body.decode("utf-8", "replace")}
    return status, data


def invite_from_log(server) -> str:
    """从服务端启动日志里把自动生成的邀请码抠出来。

    这本身就是一条断言：运维**必须**能从日志里看到它 —— 否则"默认要求邀请码"
    就等于把所有新用户关在门外。
    """
    for line in server.log().splitlines():
        if "★" in line and "邀请码" in line:
            return line.split("：", 1)[1].split()[0]
    raise AssertionError("启动日志里没有邀请码那一行：\n" + server.log())


def api_error_code(exc: ApiError) -> str:
    return exc.code


def url_request(base_url: str, path: str, token: str = "") -> tuple[int, bytes]:
    """正常走 urllib 发一个请求，返回 ``(状态码, 响应体)``（4xx/5xx 也返回，不抛）。"""
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request(base_url + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


# --------------------------------------------------------------------------- 夹具
@pytest.fixture(scope="module")
def default_server():
    """**出厂默认**参数的服务端（不加任何放松开关）。"""
    with running_server(relaxed=False) as server:
        yield server


@pytest.fixture(scope="module")
def default_account(default_server):
    """出厂默认服务端上的一个已登录账号（省着用，别把每 IP 12 次的额度耗光）。"""
    code = invite_from_log(default_server)
    return register(default_server.base_url, "alice", invite=code, display="爱丽丝")


@pytest.fixture(scope="module")
def tiny_limits():
    """"闸门压到很小"的服务端：一条用例就能测穿各条上限。"""
    with running_server(extra_args=["--max-text-chars", "200", "--max-merged", "2",
                                    "--user-quota-mb", "0.01", "--typing-interval", "5"]) as server:
        yield server


@pytest.fixture(scope="module")
def tiny_account(tiny_limits):
    return register(tiny_limits.base_url, "carol", display="卡罗")


@pytest.fixture(scope="module")
def tiny_peers(tiny_account):
    """``tiny_limits`` 那台服务端上的几个对端（单聊/群聊要现成的人）。"""
    return {name: register(tiny_account.base_url, name, display=display)
            for name, display in (("dave", "戴夫"), ("erin", "艾琳"),
                                  ("frank", "弗兰克"), ("grace", "格蕾丝"))}


def two_accounts(server):
    """建一对能互相发消息的账号（攻击者 / 受害者）。"""
    attacker = register(server.base_url, "eve", display="夏娃")
    victim = register(server.base_url, "bob", display="鲍勃")
    conversation = attacker.create_direct("bob")
    make_friends(attacker, victim)                 # 门禁之后：能互发消息得先是好友
    return attacker, victim, conversation["id"]


# =========================================================================== 客户端
def test_safe_local_name_takes_only_the_last_component():
    assert safe_local_name(WINDOWS_STARTUP_ATTACK) == "evil.bat"
    assert safe_local_name("../../../../etc/passwd") == "passwd"
    assert safe_local_name("C:\\Windows\\system32\\cmd.exe") == "cmd.exe"
    assert safe_local_name("/tmp/x/y.png") == "y.png"
    assert safe_local_name(CRLF_ATTACK).startswith("t.txt")
    assert "\r" not in safe_local_name(CRLF_ATTACK)
    assert "\n" not in safe_local_name(CRLF_ATTACK)
    assert safe_local_name("") == "附件"
    assert safe_local_name("   ") == "附件"
    assert safe_local_name("..") == "附件"
    assert safe_local_name(".") == "附件"
    assert safe_local_name("\x00\x1b") == "附件"


def test_safe_local_name_keeps_normal_names_and_the_suffix():
    assert safe_local_name("季度复盘报告.pdf") == "季度复盘报告.pdf"
    assert safe_local_name("a" * 500 + ".png").endswith(".png")
    assert len(safe_local_name("a" * 500 + ".png")) <= 120
    # Windows 的保留名不是普通文件：加个下划线别让它指向设备
    assert safe_local_name("NUL") == "_NUL"
    assert safe_local_name("con.txt") == "_con.txt"


def test_attachment_path_is_pinned_inside_the_folder(tmp_path):
    root = str(tmp_path / "attachments")
    os.makedirs(root, exist_ok=True)
    for name in [WINDOWS_STARTUP_ATTACK, "../../../evil.sh", "..\\..\\evil.exe",
                 "normal.png", "", ".."]:
        path = resolve_attachment_path(root, "f_ab12", name)
        assert is_inside(root, path), "%r 逃出了下载目录：%s" % (name, path)
        assert os.path.dirname(os.path.abspath(path)) == os.path.abspath(root)
    # 附件 id 前缀还在：两个人都发"报告.pdf"时不会互相覆盖
    assert os.path.basename(resolve_attachment_path(root, "f_1", "报告.pdf")) == "f_1-报告.pdf"


def test_download_refuses_a_destination_outside_the_root(tmp_path):
    """最后一道闸：调用方交出"我想写到哪个目录"，不在里面就拒绝写。

    为什么要靠 root 而不是"看路径本身像不像恶意"：Windows 上
    ``<下载目录>/f_1-..\\..\\Startup\\evil.bat`` 是一条**完全合法**的路径
    （它的目录就是 Startup），字符串自己分不出好坏。
    """
    root = tmp_path / "downloads"
    root.mkdir()
    api = ApiClient("http://127.0.0.1:9")
    escaped = os.path.join(str(root), "f_1-" + WINDOWS_STARTUP_ATTACK)
    with pytest.raises(NetworkError) as caught:
        api.download("f_1", escaped, root=str(root))
    assert "下载目录" in caught.value.message
    assert not os.path.exists(os.path.join(str(root), "evil.bat"))


def test_cache_path_survives_a_hostile_attachment_name(tmp_path):
    transport = NetworkTransport(ApiClient("http://127.0.0.1:9"), download_dir=str(tmp_path),
                                autostart=False)
    attachment = Attachment.remote("f_ab12", WINDOWS_STARTUP_ATTACK, 12)
    path = transport.cache_path(attachment)
    assert is_inside(str(tmp_path), path)
    assert path.endswith("evil.bat")


def test_big_attachments_are_not_downloaded_automatically(tmp_path):
    """超过自动下载上限的附件保持 ``remote://``：磁盘和带宽不该由发件人说了算。"""

    class SpyApi(ApiClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.called = []

        def download(self, attachment_id, dest_path, **kwargs):
            self.called.append(attachment_id)
            return dest_path

    spy = SpyApi("http://127.0.0.1:9")
    transport = NetworkTransport(spy, download_dir=str(tmp_path / "attachments"),
                                autostart=False)
    big = Attachment.remote("f_big", "大文件.zip", AUTO_DOWNLOAD_MAX + 1)
    assert transport.localize(big) is big and big.is_remote
    assert spy.called == [], "不该自动下载超过上限的附件"
    # 上限以内：照常去下（这里让 SpyApi 假装下好了）
    small = Attachment.remote("f_small", "小文件.txt", 10)
    landed = transport.localize(small)
    assert spy.called == ["f_small"], "上限以内的附件还是要自动下"
    assert not landed.is_remote and landed.name.endswith("小文件.txt")


def test_download_never_writes_outside_the_download_dir(tmp_path):
    """端到端：攻击者用恶意附件名发一条消息，受害者客户端只该往下载目录里写。"""
    with running_server() as server:
        attacker, victim, cid = two_accounts(server)
        payload = tmp_path / "payload.bin"
        payload.write_bytes(b"@echo off\r\ncalc.exe\r\n")
        info = attacker.upload(str(payload), name=WINDOWS_STARTUP_ATTACK)
        attacker.send_message(cid, "a" * 32, "请查收", attachment_id=info["id"])

        message = victim.messages(cid)["messages"][-1]
        attachment = Attachment.remote(str(message["attachment"]["id"]),
                                       str(message["attachment"]["name"]),
                                       int(message["attachment"]["size"]))
        profile = tmp_path / "profile"
        downloads = profile / "MChat" / "attachments"
        transport = NetworkTransport(victim, download_dir=str(downloads), autostart=False)
        landing = transport.cache_path(attachment)
        victim.download(attachment.remote_id, landing, root=str(downloads))

        assert is_inside(str(downloads), landing), landing
        assert os.path.isfile(landing) and open(landing, "rb").read() == b"@echo off\r\ncalc.exe\r\n"
        # 老洞里那个"写到启动目录"的路径必须根本不存在
        escaped = profile / "MChat" / "Microsoft"
        assert not escaped.exists(), "附件名还是把文件写到下载目录外面去了：%s" % escaped
        assert not list(profile.rglob("evil.bat")) or str(landing).endswith("evil.bat")


# =========================================================================== 服务端
def test_register_requires_an_invite_by_default(default_server):
    """出厂默认：注册要邀请码（以前是"谁都能注册"）。"""
    api = ApiClient(default_server.base_url)
    with pytest.raises(ApiError) as caught:
        api.register("newbie", "secret123", "新人")
    assert api_error_code(caught.value) == "invalid_invite"
    assert caught.value.status == 403
    with pytest.raises(ApiError) as caught:
        api.register("newbie", "secret123", "新人", invite="guess")
    assert api_error_code(caught.value) == "invalid_invite"


def test_invite_code_is_printed_in_the_startup_log(default_server):
    code = invite_from_log(default_server)
    assert len(code) >= 8, "邀请码太短，容易被人猜"
    assert code in default_server.log()


def test_query_token_is_not_accepted(default_account, default_server):
    """令牌只能走 Authorization 头：以前 ?token= 也能用，URL 一进日志就撒了。"""
    quoted = urllib.parse.quote(default_account.token)
    status, _ = url_request(default_server.base_url, "/api/conversations?token=" + quoted)
    assert status == 401
    status, _ = url_request(default_server.base_url, "/api/conversations",
                            token=default_account.token)
    assert status == 200


def test_uploaded_attachment_name_is_sanitized(default_account):
    info = default_account.upload(__file__, name="../" + CRLF_ATTACK)
    name = info["name"]
    assert "/" not in name and "\\" not in name and "\r" not in name and "\n" not in name
    assert len(name) <= 120
    assert name != "." and name != ".."


def test_download_response_headers_cannot_be_injected(default_account):
    """附件名里的 CRLF 曾经能往响应头里塞 Set-Cookie / 第二个 Content-Length。

    名字里的那串字现在会被洗成普通文件名，于是它只会出现在 ``filename="…"`` 里 ——
    关键看**头部字段名**：既不该多出 set-cookie，也不该有两个 Content-Length。
    """
    info = default_account.upload(__file__, name=CRLF_ATTACK)
    raw = raw_request(default_account.base_url, "/api/files/%s" % info["id"],
                      token=default_account.token)
    head, _ = split_response(raw)
    names = header_names(head)
    assert head.startswith("HTTP/1.1 200"), head
    assert "set-cookie" not in names, names
    assert names.count("content-length") == 1, names
    assert names.count("content-disposition") == 1, names
    assert "\r" not in head.replace("\r\n", ""), head          # 头里不该有孤立的 CR
    assert "?" not in "".join(names), names                     # 每一行都得是合法头部


def test_internal_errors_do_not_leak_details(default_server):
    """500 只回一句"服务端内部错误"：异常类型/路径/SQL 都该留在服务端日志里。"""
    deep = b"[" * 20000
    raw = raw_request(default_server.base_url, "/api/register", method="POST", body=deep,
                      content_type="application/json")
    head, body = split_response(raw)
    assert head.startswith("HTTP/1.1 500"), head
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"]["code"] == "server_error"
    assert payload["error"]["message"] == "服务端内部错误"
    for leak in ("Recursion", "Traceback", "File \"", "line ", "json", "maximum"):
        assert leak not in payload["error"]["message"], payload


def test_message_length_limit(tiny_account, tiny_peers):
    """单条正文有上限（默认 20000 字符，这里压到 200）。"""
    assert tiny_peers["dave"] is not None
    cid = tiny_account.create_direct("dave")["id"]
    make_friends(tiny_account, tiny_peers["dave"])
    assert tiny_account.send_message(cid, "b" * 32, "x" * 200)["text"] == "x" * 200
    with pytest.raises(ApiError) as caught:
        tiny_account.send_message(cid, "c" * 32, "x" * 201)
    assert caught.value.code == "message_too_long"
    assert caught.value.status == 413


def test_merged_forward_limit(tiny_account, tiny_peers):
    """合并转发一条最多带几条（默认 50，这里压到 2）。"""
    assert tiny_peers["erin"] is not None
    cid = tiny_account.create_direct("erin")["id"]
    make_friends(tiny_account, tiny_peers["erin"])
    item = MergedItem("谁", "话", "", "12:00")
    sent = tiny_account.send_message(cid, "d" * 32, "两条", merged=[item, item])
    assert len(sent["merged"]) == 2
    with pytest.raises(ApiError) as caught:
        tiny_account.send_message(cid, "e" * 32, "三条", merged=[item, item, item])
    assert caught.value.code == "invalid_request"


def test_merged_items_are_normalised(tiny_account, tiny_peers):
    """合并转发只留协议约定的四个字符串字段（以前是原样 json 存下来的）。

    这里是**绕过客户端**直接按协议发的：客户端只会发那四个字段，而服务端不能指望
    "客户端守规矩" —— 别人拿 curl 就能塞任意结构、任意大小进来。
    """
    assert tiny_peers["frank"] is not None
    cid = tiny_account.create_direct("frank")["id"]
    make_friends(tiny_account, tiny_peers["frank"])
    payload = {
        "client_id": "f" * 32,
        "text": "看这个",
        "merged": [{"author": "甲", "text": "内容", "attachment": "a.png",
                    "clock": "09:30", "extra": {"硬塞进来的任意结构": ["y"] * 50},
                    "nested": {"很深": {"更深": True}}}],
    }
    status, body = post_json(tiny_account.base_url,
                             "/api/conversations/%s/messages" % cid,
                             payload, tiny_account.token)
    assert status == 200, body
    assert body["message"]["merged"] == [{"author": "甲", "text": "内容",
                                          "attachment": "a.png", "clock": "09:30"}]


def test_reply_preview_is_clipped(tiny_account, tiny_peers):
    """引用预览这类纯展示字段写不下就截断（不该为它把整条消息顶回去）。"""
    from mchat.core.models import ReplyRef
    assert tiny_peers["grace"] is not None
    cid = tiny_account.create_direct("grace")["id"]
    make_friends(tiny_account, tiny_peers["grace"])
    sent = tiny_account.send_message(cid, "g" * 32, "看这条",
                                     reply_to=ReplyRef("m_1", "某", "长" * 900))
    assert len(sent["reply_to"]["preview"]) == 300


def test_attachment_quota_is_enforced(tiny_account, tmp_path):
    """每个账号的附件总容量有上限（默认 512MB，这里压到 10KB）。"""
    blob = tmp_path / "eight.bin"
    blob.write_bytes(b"x" * 8000)
    first = tiny_account.upload(str(blob), name="a.bin")
    assert first["size"] == 8000
    with pytest.raises(ApiError) as caught:
        tiny_account.upload(str(blob), name="b.bin")
    assert caught.value.code == "quota_exceeded"
    assert caught.value.status == 413


def test_typing_is_throttled_on_the_server(tiny_limits):
    """一个人刷 typing 不能把整群人的长轮询全部吵醒；但"状态变了"必须立刻放行。"""
    api = register(tiny_limits.base_url, "heidi", display="海蒂")
    peer = register(tiny_limits.base_url, "ivan", display="伊万")
    cid = api.create_direct("ivan")["id"]
    assert peer is not None
    assert not api.typing(cid, True).get("throttled"), "第一次「正在输入」要推"
    assert api.typing(cid, True).get("throttled") is True, "同一个状态刷太快就不该再推"
    # 方向变了（我不打字了）→ 立刻放行，否则对方一直看到"正在输入…"
    assert not api.typing(cid, False).get("throttled")
    # 但**反复发同一个 false 一样要被节流**：以前 false 永远放行，循环发就是刷事件的洞
    assert api.typing(cid, False).get("throttled") is True, "重复的 typing=false 也要节流"
    assert not api.typing(cid, True).get("throttled"), "再一次翻转仍然要立刻放行"


def test_read_receipt_is_not_replayed():
    """已经"看到这儿"了再调 mark_read 不该重新推 200 条 read 事件（放大器）。"""
    with running_server() as server:
        alice = register(server.base_url, "alice", display="爱丽丝")
        bob = register(server.base_url, "bob", display="鲍勃")
        cid = alice.create_direct("bob")["id"]
        make_friends(alice, bob)
        alice.send_message(cid, "a" * 32, "第一条")
        last = alice.send_message(cid, "b" * 32, "第二条")
        first = bob.mark_read(cid, last["ts"])
        assert not first.get("unchanged"), first
        again = bob.mark_read(cid, last["ts"])
        assert again.get("unchanged") is True, again


def test_attachment_must_belong_to_the_sender():
    """只能引用**自己上传**的附件。

    以前只查"这个 id 存不存在"：猜一个别人的附件 id、再在自己会话里发一条引用它的消息，
    就能让下载接口放行（那儿的规则是"引用它的消息所在会话的成员都能下"）—— 读别人的文件。
    """
    with running_server() as server:
        victim = register(server.base_url, "victim", display="受害者")
        attacker = register(server.base_url, "eve", display="夏娃")
        payload = os.path.join(server.data_dir, "secret.bin")
        with open(payload, "wb") as handle:
            handle.write("这是别人的文件".encode("utf-8"))
        stolen = victim.upload(payload, name="secret.bin")

        cid = attacker.create_direct("victim")["id"]
        make_friends(attacker, victim)
        with pytest.raises(ApiError) as caught:
            attacker.send_message(cid, "c" * 32, "看我偷的", attachment_id=stolen["id"])
        assert caught.value.code == "not_member", caught.value
        assert caught.value.status == 403
        # 自己的附件照常能引用（别把正常路径也堵了）
        mine = attacker.upload(payload, name="mine.bin")
        sent = attacker.send_message(cid, "d" * 32, "我的文件", attachment_id=mine["id"])
        assert sent["attachment"]["id"] == mine["id"]


def test_reaction_count_is_capped():
    with running_server(extra_args=["--max-reactions", "2"]) as server:
        api = register(server.base_url, "reactor", display="反应堆")
        peer = register(server.base_url, "peer", display="对端")
        cid = api.create_direct("peer")["id"]
        make_friends(api, peer)
        message = api.send_message(cid, "e" * 32, "点我")
        assert peer is not None
        for emoji in ("👍", "🔥"):
            assert api.set_reaction(cid, message["id"], emoji, True)["ok"]
        with pytest.raises(ApiError) as caught:
            api.set_reaction(cid, message["id"], "🎉", True)
        assert caught.value.code == "invalid_request", caught.value
        # 撤掉再加不算超限
        api.set_reaction(cid, message["id"], "🔥", False)
        assert api.set_reaction(cid, message["id"], "🎉", True)["ok"]


def test_pin_count_is_capped():
    with running_server(extra_args=["--max-pins", "1"]) as server:
        api = register(server.base_url, "pinner", display="置顶人")
        peer = register(server.base_url, "peer2", display="对端二")
        cid = api.create_direct("peer2")["id"]
        make_friends(api, peer)
        first = api.send_message(cid, "f" * 32, "第一条")
        second = api.send_message(cid, "g" * 32, "第二条")
        assert peer is not None
        assert api.pin_message(cid, first["id"], True)["pinned"] is True
        with pytest.raises(ApiError) as caught:
            api.pin_message(cid, second["id"], True)
        assert caught.value.code == "invalid_request", caught.value
        # 重复置顶同一条不算超限（幂等）
        assert api.pin_message(cid, first["id"], True)["pinned"] is True
        api.pin_message(cid, first["id"], False)
        assert api.pin_message(cid, second["id"], True)["pinned"] is True




# ----------------------------------------------------- 加密存储（令牌哈希 / 磁盘加密）
def test_tokens_are_only_stored_as_hashes():
    """库里存的必须是令牌的**哈希**：拿到库文件或备份也没法直接冒充谁。"""
    with running_server() as server:
        api = register(server.base_url, "alice", display="爱丽丝")
        assert url_request(server.base_url, "/api/conversations", token=api.token)[0] == 200
        db_path = os.path.join(server.data_dir, "data", "mchat.db")
        rows = sqlite3.connect(db_path).execute("SELECT token_hash FROM tokens").fetchall()
        assert rows, "应该有一条令牌记录"
        assert all(len(str(row[0])) == 64 for row in rows), rows      # sha256 hex
        assert all(str(row[0]) != api.token for row in rows)
        for suffix in ("", "-wal"):
            path = db_path + suffix
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    assert api.token.encode("utf-8") not in handle.read(), path


def test_disk_encryption_round_trip(tmp_path):
    """给了密钥文件：**磁盘上是密文，协议上照旧是明文**（透明加密）。"""
    key_file = tmp_path / "storage.key"
    key_file.write_bytes(bytes(range(32)))
    secret = "周五下午三点在楼下等我，别告诉别人"
    with running_server(extra_args=["--storage-key-file", str(key_file)]) as server:
        alice = register(server.base_url, "alice", display="爱丽丝")
        bob = register(server.base_url, "bob", display="鲍勃")
        cid = alice.create_direct("bob")["id"]
        make_friends(alice, bob)
        alice.send_message(cid, "a" * 32, secret)

        # 协议层：对方拿到的还是原文
        assert bob.messages(cid)["messages"][-1]["text"] == secret
        # 磁盘层：正文是密文，而且库文件（含 WAL）里翻不到明文
        db_path = os.path.join(server.data_dir, "data", "mchat.db")
        stored = sqlite3.connect(db_path).execute(
            "SELECT text FROM messages ORDER BY ts DESC LIMIT 1").fetchone()[0]
        assert stored.startswith("enc1:") and secret not in stored
        for suffix in ("", "-wal"):
            path = db_path + suffix
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    assert secret.encode("utf-8") not in handle.read(), path

        # 附件：磁盘上是密文，下回来字节一模一样；文件名也不明文
        payload = tmp_path / "图纸.png"
        payload.write_bytes(b"\x89PNG-fake-bytes-for-test")
        info = alice.upload(str(payload), name="2026年体检报告.png")
        file_path = os.path.join(server.data_dir, "data", "files", info["id"])
        with open(file_path, "rb") as handle:
            assert handle.read().startswith(b"MCHSRV1"), "附件字节必须加密落盘"
        landed = tmp_path / "landed.png"
        alice.download(info["id"], str(landed))
        assert landed.read_bytes() == b"\x89PNG-fake-bytes-for-test"
        assert info["name"] == "2026年体检报告.png"
        name = sqlite3.connect(db_path).execute(
            "SELECT name FROM attachments WHERE id=?", (info["id"],)).fetchone()[0]
        assert str(name).startswith("enc1:") and "体检" not in str(name)


def test_legacy_plaintext_messages_are_encrypted_on_first_boot(tmp_path):
    """老的明文库 + 新配的密钥：第一次启动就地改写成密文，内容照旧读得出来。"""
    data_dir = str(tmp_path / "server-data")
    key_file = tmp_path / "storage.key"
    key_file.write_bytes(bytes(range(32, 64)))
    secret = "升级之前存的一句话"

    def boot(extra):
        port = _free_port()
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "mchat_server.py"),
                                 "--host", "127.0.0.1", "--port", str(port),
                                 "--data", data_dir, "--open-register",
                                 "--login-rate", "0", "--lock-after", "0", *extra],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                if ApiClient(base, timeout=2.0).health().get("ok"):
                    return proc, base
            except Exception:
                time.sleep(0.2)
        proc.kill()
        raise AssertionError("服务端没起来")

    db_path = os.path.join(data_dir, "mchat.db")
    proc, base = boot([])
    try:
        alice = register(base, "alice", display="爱丽丝")
        bob = register(base, "bob", display="鲍勃")
        cid = alice.create_direct("bob")["id"]
        make_friends(alice, bob)
        alice.send_message(cid, "a" * 32, secret)
        assert sqlite3.connect(db_path).execute(
            "SELECT text FROM messages").fetchone()[0] == secret      # 这时还是明文
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc, base = boot(["--storage-key-file", str(key_file)])
    try:
        stored = sqlite3.connect(db_path).execute("SELECT text FROM messages").fetchone()[0]
        assert str(stored).startswith("enc1:"), "旧明文应该在启动时被改写成密文"
        assert secret not in str(stored)
        alice = ApiClient(base)
        alice.token = alice.login("alice", "secret123")["token"]       # 令牌哈希化后要重登
        got = alice.messages(cid)["messages"]
        assert got[-1]["text"] == secret, "改写成密文之后照样要能读出来"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_bad_storage_key_file_refuses_to_start(tmp_path):
    """配了磁盘加密却用不了（文件不在）→ **拒绝启动**，不能"以为加密了其实没有"。"""
    with pytest.raises(RuntimeError) as caught:
        with running_server(extra_args=["--storage-key-file", str(tmp_path / "nope.key")]):
            pytest.fail("不该跑起来")
    assert "密钥文件" in str(caught.value)


def test_group_size_is_capped():
    with running_server(extra_args=["--max-group-members", "2"]) as server:
        api = register(server.base_url, "groupie", display="群主")
        for name in ("member1", "member2"):
            register(server.base_url, name, display=name)
        group = api.create_group("两个人", ["member1"])
        assert len(group["members"]) == 2, group["members"]
        with pytest.raises(ApiError) as caught:
            api.create_group("三个人", ["member1", "member2"])
        assert caught.value.code == "invalid_request", caught.value


# ----------------------------------------------------------- 需要"整台服务端"的用例
def test_login_attempts_are_rate_limited():
    with running_server(extra_args=["--login-rate", "3", "--lock-after", "0"]) as server:
        api = ApiClient(server.base_url)
        for index in range(3):
            api.register("user%d" % index, "secret123", "第%d位" % index)
        with pytest.raises(ApiError) as caught:
            api.register("user9", "secret123", "第九位")
        assert caught.value.code == "too_many_requests"
        assert caught.value.status == 429


def test_account_is_locked_after_repeated_failures():
    with running_server(extra_args=["--login-rate", "0", "--lock-after", "2",
                                    "--lock-seconds", "1"]) as server:
        api = ApiClient(server.base_url)
        api.register("locky", "secret123", "锁子")
        errors = []
        for index in range(2):
            with pytest.raises(ApiError) as caught:
                api.login("locky", "wrong-%d" % index)
            errors.append(caught.value.code)
        assert errors == ["bad_credentials", "bad_credentials"]
        with pytest.raises(ApiError) as caught:
            api.login("locky", "secret123")          # 正确的密码也不行：锁着
        assert caught.value.code == "too_many_requests"
        time.sleep(1.2)
        assert api.login("locky", "secret123")["token"]


def test_token_ttl_zero_means_never_expires():
    with running_server(extra_args=["--token-ttl-days", "0"]) as server:
        api = register(server.base_url, "forever", display="永久")
        assert api.me()["user"]["username"] == "forever"
        status, _ = url_request(server.base_url, "/api/conversations", token=api.token)
        assert status == 200


def test_connection_cap_answers_503():
    """慢连接占满连接数时，新连接立刻拿到 503 而不是排队等（线程不会被拖光）。"""
    with running_server(extra_args=["--max-connections", "1"]) as server:
        host, _, port = server.base_url.split("://", 1)[1].partition(":")
        idle = socket.create_connection((host, int(port)), timeout=10)
        try:
            idle.sendall(b"GET /api/health HTTP/1.1\r\nHost: x\r\n")   # 半截请求，占着坑
            time.sleep(0.4)
            raw = raw_request(server.base_url, "/api/health")
            head, body = split_response(raw)
            assert head.startswith("HTTP/1.1 503"), head
            assert json.loads(body.decode("utf-8"))["error"]["code"] == "too_many_requests"
        finally:
            idle.close()
        time.sleep(0.4)
        assert url_request(server.base_url, "/api/health")[0] == 200      # 坑放出来了


def test_orphan_attachment_cleanup_removes_unreferenced_files():
    """上传了却没有任何消息引用的附件会被回收（磁盘不能只涨不跌）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mchat_server", os.path.join(ROOT, "server", "mchat_server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    parser = module.build_parser()
    args = parser.parse_args(["--data", os.path.join(ROOT, "server", "data")])
    cfg = module.Config(args)
    cfg.orphan_days = 7.0
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        cfg.data_dir = folder
        cfg.db_path = os.path.join(folder, "mchat.db")
        cfg.files_dir = os.path.join(folder, "files")
        os.makedirs(cfg.files_dir, exist_ok=True)
        db = module.Database(cfg)
        app = module.App(db, module.EventBus(db), cfg)
        me = db.query_one("SELECT * FROM users LIMIT 1")
        assert me is None                                   # 空库：下面自己造
        now = module.now_ts()
        with db.write() as conn:
            conn.execute("INSERT INTO users(id, username, display_name, pw_hash, salt,"
                         " created_at, last_seen) VALUES('u_x','x','X',x'00',x'00',?,?)",
                         (now, now))
            conn.execute("INSERT INTO attachments(id, owner_id, name, size, created_at)"
                         " VALUES('f_old','u_x','old.bin',4,?)", (now - 30 * 86400,))
            conn.execute("INSERT INTO attachments(id, owner_id, name, size, created_at)"
                         " VALUES('f_new','u_x','new.bin',4,?)", (now,))
        for ident in ("f_old", "f_new"):
            with open(os.path.join(cfg.files_dir, ident), "wb") as handle:
                handle.write(b"1234")
        assert app.cleanup_orphan_attachments(7.0) == 1
        assert not os.path.exists(os.path.join(cfg.files_dir, "f_old"))
        assert os.path.exists(os.path.join(cfg.files_dir, "f_new"))
        db.close_all()
