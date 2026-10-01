#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MChat 服务端端到端自检（纯标准库）。

它会：
1. 在 127.0.0.1 上挑一个空闲端口，把 server/mchat_server.py 作为**真实子进程**拉起来
   （数据目录是临时目录，跑完删掉，绝不碰仓库里的 server/data/）；
2. 用 urllib.request 按 PROTOCOL.md 走完整流程：健康检查 → 注册/登录 → 找人 → 建会话
   → 发消息 → 长轮询收事件 → 幂等 → 未读/已读 → typing → 群 → 附件 → 分页 → 退出/过期
   → 好友（门禁 / 申请 / 同意 / 删除 / 存量迁移）；
3. 每条断言打印一行 `  ok    <名字>` / `  FAIL  <名字>: <原因>`，最后打印 `N/M 通过`，
   全绿退出码 0，有任何失败退出码 1。

用法：
    python3 selftest.py                 # 用默认解释器
    .venv/Scripts/python.exe server/selftest.py   # 开发时
"""

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "mchat_server.py")

PASSED = 0
FAILED = 0
S = {}
PROCS = []
TMPDIRS = []


# ---------------------------------------------------------------------------
# 断言 / 输出
# ---------------------------------------------------------------------------
def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  ok    " + name, flush=True)
    else:
        FAILED += 1
        print("  FAIL  " + name + ": " + (detail or "断言不成立"), flush=True)
    return bool(cond)


def eq(name, got, want):
    return check(name, got == want, "期望 %r，实际 %r" % (want, got))


def section(title):
    print("\n== " + title + " ==", flush=True)


def run_section(title, fn):
    section(title)
    try:
        fn()
    except Exception as exc:  # 一段里出意外也别让整轮挂掉
        check(title + "（该段未预期异常）", False, "%s: %s" % (type(exc).__name__, exc))


def err_code(data):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("code")
    return None


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def api(method, path, token=None, body=None, raw=None, base=None, timeout=40, headers=None):
    base = base or S["base"]
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = "Bearer " + token
    data = None
    if raw is not None:
        data = raw
        hdrs.setdefault("Content-Type", "application/octet-stream")
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            return resp.status, resp.read(), dict(resp.headers)
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), dict(exc.headers)
        finally:
            exc.close()


def japi(method, path, **kw):
    status, blob, headers = api(method, path, **kw)
    data = None
    if blob:
        try:
            data = json.loads(blob.decode("utf-8"))
        except ValueError:
            data = None
    return status, data, blob


class Poll(object):
    """在后台线程里挂一个 /api/sync 长轮询。"""

    def __init__(self, token, cursor, timeout):
        self.token = token
        self.cursor = cursor
        self.timeout = timeout
        self.status = None
        self.data = None
        self.error = None
        self.t0 = None
        self.elapsed = None
        self.base = S["base"]
        self._th = threading.Thread(target=self._run)
        self._th.daemon = True

    def _run(self):
        self.t0 = time.time()
        try:
            self.status, self.data, _ = japi(
                "GET", "/api/sync?cursor=%s&timeout=%s" % (self.cursor, self.timeout),
                token=self.token, base=self.base, timeout=self.timeout + 25,
            )
        except Exception as exc:
            self.error = exc
        self.elapsed = time.time() - self.t0

    def start(self):
        self._th.start()
        return self

    def wait(self, timeout=None):
        self._th.join(timeout if timeout is not None else (self.timeout + 20))
        if self._th.is_alive():
            raise RuntimeError("长轮询线程没在 %s 秒内返回" % timeout)
        if self.error:
            raise self.error
        return self.data or {}

    def events(self):
        data = self.wait()
        return [e for e in (data.get("events") or []) if isinstance(e, dict)]


def find_event(events, kind, **match):
    for ev in events:
        if ev.get("kind") != kind:
            continue
        if all(ev.get(k) == v for k, v in match.items()):
            return ev
    return None


def cursor_of(token, timeout=1):
    """空跑一次 sync 取一个真实游标（cursor=0 语义 = 从此刻起，不会丢事件）。"""
    status, data, _ = japi("GET", "/api/sync?cursor=0&timeout=%s" % timeout, token=token, timeout=timeout + 25)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError("取游标失败：HTTP %s %s" % (status, data))
    return data["cursor"]


# ---------------------------------------------------------------------------
# 起服务
# ---------------------------------------------------------------------------
def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def start_server(extra_args=None, data_dir=None, label="server", relaxed=True):
    """起一个服务端子进程。

    ``relaxed=True``（默认）会加上 ``--open-register --login-rate 0 --lock-after 0``：
    自检里的多数段落只想安安静静地注册一堆账号（全部请求都来自 127.0.0.1，
    开着限流会互相干扰）。要验"出厂默认"就用 ``relaxed=False``。
    ``extra_args`` 加在最后 —— 同名的参数以它为准（``--login-rate`` 这种标量取最后一个）。
    """
    if data_dir is None:
        data_dir = tempfile.mkdtemp(prefix="mchat-selftest-")
        TMPDIRS.append(data_dir)
    port = free_port()
    log_path = os.path.join(data_dir, label + ".log")
    log = open(log_path, "wb")
    cmd = [sys.executable, SERVER_PY, "--host", "127.0.0.1", "--port", str(port), "--data", data_dir]
    if relaxed:
        cmd.extend(["--open-register", "--login-rate", "0", "--lock-after", "0"])
    cmd.extend(extra_args or [])
    creationflags = 0
    if os.name == "nt":
        # 自己的进程组，方便后面用 CTRL_BREAK 测“优雅退出”
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=creationflags)
    PROCS.append(proc)
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            log.flush()
            with open(log_path, "rb") as fh:
                out = fh.read().decode("utf-8", "replace")
            raise RuntimeError("服务端启动即退出（exit=%s）：\n%s" % (proc.returncode, out))
        try:
            status, data, _ = japi("GET", "/api/health", base=base, timeout=3)
            if status == 200 and data and data.get("ok"):
                return {"proc": proc, "base": base, "data_dir": data_dir, "log": log_path}
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("服务端 25 秒内没起来，日志：" + log_path)


# ---------------------------------------------------------------------------
# 各段自检
# ---------------------------------------------------------------------------
def sec_health():
    status, data, _ = japi("GET", "/api/health")
    eq("GET /api/health 返回 200", status, 200)
    check("health.ok == true", isinstance(data, dict) and data.get("ok") is True, repr(data))
    eq("health.name", (data or {}).get("name"), "mchat-server")
    check("health.version 是字符串", isinstance((data or {}).get("version"), str), repr(data))
    check("health.time 是数字", isinstance((data or {}).get("time"), (int, float)), repr(data))
    eq("health.users 初始为 0", (data or {}).get("users"), 0)
    eq("health.messages 初始为 0", (data or {}).get("messages"), 0)


def sec_register():
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "alice", "password": "secret123", "display_name": "爱丽丝"})
    eq("注册 alice → 200", status, 200)
    check("注册返回 token", isinstance((data or {}).get("token"), str) and len(data["token"]) > 20, repr(data))
    check("注册返回 user.id（u_ 前缀）", str((data or {}).get("user", {}).get("id", "")).startswith("u_"), repr(data))
    eq("注册返回 user.username", (data or {}).get("user", {}).get("username"), "alice")
    eq("注册返回 user.display_name", (data or {}).get("user", {}).get("display_name"), "爱丽丝")
    check("注册返回 server_time", isinstance((data or {}).get("server_time"), (int, float)), repr(data))
    S["token_a"] = (data or {}).get("token")
    S["user_a"] = (data or {}).get("user") or {}

    status, data, _ = japi("POST", "/api/register",
                           body={"username": "bob", "password": "secret123", "display_name": "鲍勃"})
    eq("注册 bob → 200", status, 200)
    S["token_b"] = (data or {}).get("token")
    S["user_b"] = (data or {}).get("user") or {}

    status, data, _ = japi("POST", "/api/register",
                           body={"username": "carol", "password": "secret123", "display_name": "卡罗尔"})
    eq("注册 carol → 200", status, 200)
    S["token_c"] = (data or {}).get("token")

    status, data, _ = japi("POST", "/api/register",
                           body={"username": "dave", "password": "secret123", "display_name": ""})
    eq("注册 dave（display_name 为空）→ 200", status, 200)
    eq("display_name 为空时回落到 username", (data or {}).get("user", {}).get("display_name"), "dave")
    S["token_d"] = (data or {}).get("token")

    # 负例
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "alice", "password": "secret123", "display_name": "假爱丽丝"})
    eq("重复用户名 → 409", status, 409)
    eq("重复用户名 → username_taken", err_code(data), "username_taken")

    status, data, _ = japi("POST", "/api/register", body={"username": "x", "password": "secret123"})
    eq("用户名太短 → 400", status, 400)
    eq("用户名太短 → invalid_username", err_code(data), "invalid_username")

    status, data, _ = japi("POST", "/api/register", body={"username": "bad name!", "password": "secret123"})
    eq("用户名非法字符 → invalid_username", err_code(data), "invalid_username")

    status, data, _ = japi("POST", "/api/register", body={"username": "weakling", "password": "123"})
    eq("密码太短 → 400", status, 400)
    eq("密码太短 → weak_password", err_code(data), "weak_password")

    status, data, _ = japi("POST", "/api/register", body={"username": "nopass"})
    eq("缺字段 → 400 invalid_request", err_code(data), "invalid_request")


def sec_login():
    status, data, _ = japi("POST", "/api/login", body={"username": "alice", "password": "secret123"})
    eq("alice 正确密码登录 → 200", status, 200)
    check("登录返回 token", isinstance((data or {}).get("token"), str), repr(data))
    eq("登录返回同一个 user.id", (data or {}).get("user", {}).get("id"), S["user_a"].get("id"))
    S["token_a2"] = (data or {}).get("token")

    status, data, _ = japi("POST", "/api/login", body={"username": "alice", "password": "wrongpass"})
    eq("alice 错误密码 → 401", status, 401)
    eq("alice 错误密码 → bad_credentials", err_code(data), "bad_credentials")

    status, data, _ = japi("POST", "/api/login", body={"username": "nobody-here", "password": "whatever"})
    eq("不存在的用户登录 → bad_credentials", err_code(data), "bad_credentials")

    # /api/me
    status, data, _ = japi("GET", "/api/me", token="totally-bogus-token")
    eq("/api/me 假 token → 401", status, 401)
    eq("/api/me 假 token → unauthorized", err_code(data), "unauthorized")

    status, data, _ = japi("GET", "/api/me")
    eq("/api/me 不带 token → 401", status, 401)

    status, data, _ = japi("GET", "/api/me", token=S["token_a2"])
    eq("/api/me 正确 token → 200", status, 200)
    eq("/api/me 返回自己", (data or {}).get("user", {}).get("username"), "alice")
    check("/api/me 带 server_time", isinstance((data or {}).get("server_time"), (int, float)), repr(data))

    # 会话类接口同样要鉴权
    status, data, _ = japi("GET", "/api/conversations")
    eq("/api/conversations 无 token → 401", status, 401)
    status, data, _ = japi("GET", "/api/sync?cursor=0&timeout=0", token="bogus")
    eq("/api/sync 假 token → 401", status, 401)


def sec_users():
    status, data, _ = japi("GET", "/api/users?query=bob", token=S["token_a"])
    eq("GET /api/users?query=bob → 200", status, 200)
    users = (data or {}).get("users") or []
    check("alice 搜 bob 搜到了", any(u.get("username") == "bob" for u in users), repr(users))
    check("搜索结果不含自己", all(u.get("id") != S["user_a"].get("id") for u in users), repr(users))
    bob = [u for u in users if u.get("username") == "bob"]
    if bob:
        check("User 对象字段齐全（id/username/display_name/online）",
              set(["id", "username", "display_name", "online"]).issubset(bob[0].keys()), repr(bob[0]))
        eq("搜索到的 bob 显示名", bob[0].get("display_name"), "鲍勃")

    status, data, _ = japi("GET", "/api/users?query=" + urllib.parse.quote("鲍勃"), token=S["token_a"])
    check("按 display_name 子串也能搜到", any(u.get("username") == "bob" for u in ((data or {}).get("users") or [])),
          repr(data))

    status, data, _ = japi("GET", "/api/users?query=zzzz-nobody", token=S["token_a"])
    eq("搜不到就是空数组", (data or {}).get("users"), [])

    status, data, _ = japi("GET", "/api/users", token=S["token_a"])
    eq("不带 query → 200", status, 200)
    check("不带 query 返回若干用户", len((data or {}).get("users") or []) >= 3, repr(data))


def sec_conversations():
    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_a"], body={"username": "bob"})
    eq("建单聊 → 200", status, 200)
    conv = (data or {}).get("conversation") or {}
    cid = conv.get("id")
    check("单聊 id 以 c_ 开头", str(cid).startswith("c_"), repr(conv))
    eq("单聊 kind == direct", conv.get("kind"), "direct")
    eq("单聊 title == 对方显示名", conv.get("title"), "鲍勃")
    eq("单聊成员 2 人", len(conv.get("members") or []), 2)
    eq("新会话 last_message 为 null", conv.get("last_message"), None)
    eq("新会话 unread == 0", conv.get("unread"), 0)
    S["cid"] = cid

    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_a"], body={"username": "bob"})
    eq("再建一次单聊仍然是 200", status, 200)
    eq("建单聊幂等（同一个 id）", (data or {}).get("conversation", {}).get("id"), cid)

    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_a"], body={"username": "ghost"})
    eq("和不存在的用户建单聊 → 404", status, 404)
    eq("和不存在的用户建单聊 → not_found", err_code(data), "not_found")

    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_a"], body={"username": "alice"})
    eq("和自己建单聊 → 400 invalid_request", err_code(data), "invalid_request")

    status, data, _ = japi("GET", "/api/conversations", token=S["token_b"])
    convs = (data or {}).get("conversations") or []
    check("bob 的会话列表里有这个单聊", any(c.get("id") == cid for c in convs), repr(convs))
    mine = [c for c in convs if c.get("id") == cid]
    if mine:
        eq("bob 看到的单聊 title 是对方（爱丽丝）", mine[0].get("title"), "爱丽丝")

    status, data, _ = japi("GET", "/api/conversations", token="bogus")
    eq("/api/conversations 假 token → 401", status, 401)

    # 好友门禁：新规则下"还不是好友的单聊"发不出消息（会变成好友申请）。
    # 所以先让 alice 申请、bob 同意，后面的直聊段落（06-12）才走得通。
    # 申请**故意不带留言**：留言会变成会话里的第一条消息，而分页那一段断言的是
    # 这个会话里精确的消息集合，不能凭空多一条。
    status, data, _ = japi("POST", "/api/friends/request", token=S["token_a"], body={"username": "bob"})
    eq("alice 申请加 bob 为好友 → pending", (data or {}).get("status"), "pending")
    status, data, _ = japi("POST", "/api/friends/accept", token=S["token_b"], body={"username": "alice"})
    eq("bob 同意 alice → accepted", (data or {}).get("status"), "accepted")
    eq("同意时复用前面建好的单聊", (data or {}).get("conversation", {}).get("id"), cid)
    eq("没带留言就不会凭空多出一条消息", (data or {}).get("conversation", {}).get("last_message"), None)


def sec_longpoll():
    # 长轮询要“被唤醒”，先把双方游标推到当前位置（cursor=0 语义 = 从此刻起）
    S["cur_b"] = cursor_of(S["token_b"])
    S["cur_a"] = cursor_of(S["token_a"])

    client_id = "a" * 32
    S["m1"] = client_id
    sent = {}

    def sender():
        time.sleep(0.8)  # 让主线程先把长轮询挂上去
        sent["status"], sent["data"], _ = japi(
            "POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
            body={"client_id": client_id, "text": "在吗", "attachment_id": None,
                  "reply_to": None, "forwarded_from": "", "merged": []},
        )

    th = threading.Thread(target=sender)
    th.daemon = True
    th.start()
    t0 = time.time()
    status, data, _ = japi("GET", "/api/sync?cursor=%s&timeout=15" % S["cur_b"], token=S["token_b"])
    elapsed = time.time() - t0
    th.join(30)

    eq("发送方（alice）发消息 → 200", sent.get("status"), 200)
    msg = (sent.get("data") or {}).get("message") or {}
    eq("服务端原样保存客户端 id", msg.get("id"), client_id)
    check("服务端补了 ts", isinstance(msg.get("ts"), (int, float)), repr(msg))
    eq("服务端补了 sender_name", msg.get("sender_name"), "爱丽丝")
    eq("服务端补了 conversation_id", msg.get("conversation_id"), S["cid"])
    eq("新消息 status == sent", msg.get("status"), "sent")

    eq("bob 的长轮询 → 200", status, 200)
    events = (data or {}).get("events") or []
    got = find_event(events, "message", conversation_id=S["cid"])
    check("bob 通过长轮询收到 message 事件（线程里发、主线程收）", got is not None, repr(events))
    if got:
        eq("事件里的消息 id 对得上", got.get("message", {}).get("id"), client_id)
        eq("事件里的消息正文", got.get("message", {}).get("text"), "在吗")
    check("长轮询是被唤醒的（%.2fs < 15s 超时）" % elapsed, elapsed < 10,
          "等了 %.2fs，像是等到超时才返回" % elapsed)
    S["cur_b"] = (data or {}).get("cursor", S["cur_b"])

    # 发送者自己（其它设备）也必须收到
    status, data, _ = japi("GET", "/api/sync?cursor=%s&timeout=8" % S["cur_a"], token=S["token_a"])
    evs = (data or {}).get("events") or []
    check("发送者自己的其它设备也会收到 message 事件",
          find_event(evs, "message", conversation_id=S["cid"]) is not None, repr(evs))
    S["cur_a"] = (data or {}).get("cursor", S["cur_a"])


def sec_idempotent():
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
                           body={"client_id": S["m1"], "text": "在吗"})
    eq("同一 client_id 重发 → 200", status, 200)
    eq("重发返回同一条消息", (data or {}).get("message", {}).get("id"), S["m1"])

    status, data, _ = japi("GET", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"])
    msgs = (data or {}).get("messages") or []
    eq("/api/messages 里这条 id 只出现一次", len([m for m in msgs if m.get("id") == S["m1"]]), 1)
    eq("/api/messages 总条数还是 1", len(msgs), 1)

    # 直接开一下服务端的 SQLite 文件，确认真库里只有一行（WAL 模式下别的进程只读没问题）
    db_path = os.path.join(S["data_dir"], "mchat.db")
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            count = conn.execute("SELECT COUNT(*) FROM messages WHERE id=?", (S["m1"],)).fetchone()[0]
        finally:
            conn.close()
        eq("数据库里同一条 client_id 只有一行", count, 1)
    except Exception as exc:
        check("能直接读 SQLite 校验唯一性", False, str(exc))

    # 别人占用同一个 id → duplicate_id
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_b"],
                           body={"client_id": S["m1"], "text": "抢 id"})
    eq("别人占用同一 id → 409", status, 409)
    eq("别人占用同一 id → duplicate_id", err_code(data), "duplicate_id")

    # 空消息的负例
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
                           body={"client_id": "b" * 32, "text": ""})
    eq("空正文且没附件 → 400 invalid_request", err_code(data), "invalid_request")
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
                           body={"text": "没有 client_id"})
    eq("缺 client_id → 400 invalid_request", err_code(data), "invalid_request")


def sec_unread_read():
    poll = Poll(S["token_a"], S["cur_a"], 15).start()
    time.sleep(0.5)

    status, data, _ = japi("GET", "/api/conversations", token=S["token_b"])
    convs = [c for c in ((data or {}).get("conversations") or []) if c.get("id") == S["cid"]]
    check("bob 的会话列表里找得到这个单聊", bool(convs), repr(data))
    eq("bob 未读 == 1", convs[0].get("unread") if convs else None, 1)
    if convs:
        check("bob 看到的 last_message 就是那条", (convs[0].get("last_message") or {}).get("id") == S["m1"], repr(convs[0]))
        check("last_ts 已更新", float(convs[0].get("last_ts") or 0) > 0, repr(convs[0]))

    status, data, _ = japi("GET", "/api/conversations", token=S["token_a"])
    mine = [c for c in ((data or {}).get("conversations") or []) if c.get("id") == S["cid"]]
    eq("自己发的不算自己的未读", mine[0].get("unread") if mine else None, 0)

    # 用消息列表里的 ts 作 up_to_ts
    status, data, _ = japi("GET", "/api/conversations/%s/messages" % S["cid"], token=S["token_b"])
    msgs = (data or {}).get("messages") or []
    up_to_ts = msgs[-1]["ts"] if msgs else 0

    status, data, _ = japi("POST", "/api/conversations/%s/read" % S["cid"], token=S["token_b"],
                           body={"up_to_ts": up_to_ts})
    eq("bob 标记已读 → 200", status, 200)
    eq("标记已读返回 ok", (data or {}).get("ok"), True)

    status, data, _ = japi("GET", "/api/conversations", token=S["token_b"])
    convs = [c for c in ((data or {}).get("conversations") or []) if c.get("id") == S["cid"]]
    eq("bob 已读后 unread == 0", convs[0].get("unread") if convs else None, 0)

    events = poll.events()
    got = find_event(events, "read", conversation_id=S["cid"])
    check("alice 收到 read 事件", got is not None, repr(events))
    if got:
        eq("read 事件的 reader_id 是 bob", got.get("reader_id"), S["user_b"].get("id"))
        eq("read 事件的 message_id", got.get("message_id"), S["m1"])
    S["cur_a"] = poll.wait().get("cursor", S["cur_a"])

    status, data, _ = japi("GET", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"])
    msgs = (data or {}).get("messages") or []
    eq("被读之后发送方看到 status == read", msgs[0].get("status") if msgs else None, "read")

    status, data, _ = japi("POST", "/api/conversations/%s/read" % S["cid"], token=S["token_b"], body={})
    eq("read 缺 up_to_ts → 400 invalid_request", err_code(data), "invalid_request")


def sec_typing():
    poll = Poll(S["token_a"], S["cur_a"], 15).start()
    time.sleep(0.5)
    status, data, _ = japi("POST", "/api/conversations/%s/typing" % S["cid"], token=S["token_b"],
                           body={"typing": True})
    eq("bob 发 typing → 200", status, 200)
    events = poll.events()
    got = find_event(events, "typing", conversation_id=S["cid"], user_id=S["user_b"].get("id"))
    check("alice 收到 typing 事件", got is not None, repr(events))
    if got:
        eq("typing 事件带 user_name", got.get("user_name"), "鲍勃")
        eq("typing == true", got.get("typing"), True)
    S["cur_a"] = poll.wait().get("cursor", S["cur_a"])

    # 自己给自己发 typing 不应该收到（收件人是其它成员）
    status, data, _ = japi("GET", "/api/sync?cursor=%s&timeout=1" % S["cur_b"], token=S["token_b"])
    evs = (data or {}).get("events") or []
    check("typing 不会回推给发送者自己", find_event(evs, "typing") is None, repr(evs))
    S["cur_b"] = (data or {}).get("cursor", S["cur_b"])


def sec_group():
    status, data, _ = japi("POST", "/api/conversations/group", token=S["token_a"],
                           body={"title": "评审组", "usernames": ["bob", "carol"]})
    eq("建群 → 200", status, 200)
    group = (data or {}).get("conversation") or {}
    gid = group.get("id")
    eq("群 kind == group", group.get("kind"), "group")
    eq("群 title", group.get("title"), "评审组")
    eq("群成员 3 人", len(group.get("members") or []), 3)
    check("群里包含建群者自己", any(m.get("id") == S["user_a"].get("id") for m in group.get("members") or []), repr(group))
    S["gid"] = gid

    status, data, _ = japi("GET", "/api/conversations", token=S["token_c"])
    check("被拉进来的 carol 会话列表里有这个群",
          any(c.get("id") == gid for c in ((data or {}).get("conversations") or [])), repr(data))

    status, data, _ = japi("POST", "/api/conversations/group", token=S["token_a"],
                           body={"title": "空的", "usernames": ["ghost"]})
    eq("拉不存在的人建群 → 404 not_found", err_code(data), "not_found")
    status, data, _ = japi("POST", "/api/conversations/group", token=S["token_a"],
                           body={"title": "就我一个", "usernames": []})
    eq("群里一个人都没有 → 400 invalid_request", err_code(data), "invalid_request")

    group_id = "c" * 32
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % gid, token=S["token_a"],
                           body={"client_id": group_id, "text": "周会改到三点"})
    eq("群里发消息 → 200", status, 200)
    eq("群消息 conversation_id", (data or {}).get("message", {}).get("conversation_id"), gid)

    status, data, _ = japi("GET", "/api/conversations/%s/messages" % gid, token=S["token_b"])
    eq("bob（成员）拉群历史 → 200", status, 200)
    eq("群历史里有那条消息", [m.get("id") for m in ((data or {}).get("messages") or [])], [group_id])

    # 非成员
    status, data, _ = japi("GET", "/api/conversations/%s/messages" % gid, token=S["token_d"])
    eq("非成员拉群历史 → 403", status, 403)
    eq("非成员拉群历史 → not_member", err_code(data), "not_member")

    status, data, _ = japi("POST", "/api/conversations/%s/messages" % gid, token=S["token_d"],
                           body={"client_id": "d" * 32, "text": "我能插一句吗"})
    eq("非成员往群里发 → 403 not_member", err_code(data), "not_member")

    status, data, _ = japi("POST", "/api/conversations/%s/typing" % gid, token=S["token_d"], body={"typing": True})
    eq("非成员发 typing → 403 not_member", err_code(data), "not_member")

    status, data, _ = japi("GET", "/api/conversations/c_nope/messages", token=S["token_a"])
    eq("不存在的会话 → 404 not_found", err_code(data), "not_found")

    status, data, _ = japi("GET", "/api/users?query=carol", token=S["token_a"])
    check("（群相关）carol 能被搜到", any(u.get("username") == "carol" for u in ((data or {}).get("users") or [])),
          repr(data))


def sec_attachment():
    blob = b"\x89PNG\r\n\x1a\n" + bytes(range(56))  # 64 字节假 png
    name = "配色预览.png"
    status, data, body = japi("POST", "/api/upload?name=" + urllib.parse.quote(name),
                              token=S["token_a"], raw=blob)
    eq("上传附件 → 200", status, 200)
    att = (data or {}).get("attachment") or {}
    fid = att.get("id")
    check("附件 id 以 f_ 开头", str(fid).startswith("f_"), repr(att))
    eq("附件名字对得上（中文不丢）", att.get("name"), name)
    eq("附件大小对得上", att.get("size"), len(blob))
    S["fid"] = fid

    att_msg_id = "e" * 32
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
                           body={"client_id": att_msg_id, "text": "", "attachment_id": fid})
    eq("带附件发消息 → 200", status, 200)
    m = (data or {}).get("message") or {}
    eq("消息里带 attachment.id", (m.get("attachment") or {}).get("id"), fid)
    eq("消息里带 attachment.size", (m.get("attachment") or {}).get("size"), len(blob))
    eq("消息里带 attachment.name", (m.get("attachment") or {}).get("name"), name)

    status, blob2, headers = api("GET", "/api/files/%s" % fid, token=S["token_b"])
    eq("会话成员（bob）下载附件 → 200", status, 200)
    eq("下载回来的字节完全一样", blob2, blob)
    check("Content-Type 按后缀猜成 image/png", headers.get("Content-Type") == "image/png", headers.get("Content-Type"))
    disp = headers.get("Content-Disposition") or ""
    check("Content-Disposition 带 UTF-8 文件名", "filename*=UTF-8''" in disp, disp)
    check("Content-Disposition 里的文件名是 URL 编码的中文",
          urllib.parse.quote(name) in disp, disp)

    # ?token= 这条"浏览器直链"后门**已经封掉**了：URL 会进反代日志/浏览器历史，
    # 长效令牌不该出现在里面。现在只认 Authorization 头（客户端也没人会带 token 拼 URL）。
    status, blob3, _ = api("GET", "/api/files/%s?token=%s" % (fid, urllib.parse.quote(S["token_b"])))
    eq("?token= 走 URL → 401（后门已封）", status, 401)
    check("?token= 不再返回文件字节", blob3 != blob, blob3[:120])

    status, blob4, _ = api("GET", "/api/files/%s" % fid, token=S["token_a"])
    eq("上传者本人能下载 → 200", status, 200)

    status, data, _ = japi("GET", "/api/files/%s" % fid, token=S["token_c"])
    eq("不相关的 carol 下载 → 403", status, 403)
    eq("不相关的 carol 下载 → not_member", err_code(data), "not_member")

    status, data, _ = japi("GET", "/api/files/%s" % fid, token=S["token_d"])
    eq("不相关的 dave 下载 → 403 not_member", err_code(data), "not_member")

    status, data, _ = japi("GET", "/api/files/%s" % fid)
    eq("不带 token 下载 → 401 unauthorized", err_code(data), "unauthorized")

    status, data, _ = japi("GET", "/api/files/f_nope", token=S["token_a"])
    eq("不存在的附件 → 404 not_found", err_code(data), "not_found")

    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
                           body={"client_id": "f" * 32, "text": "x", "attachment_id": "f_nope"})
    eq("引用不存在的附件 → 404 not_found", err_code(data), "not_found")

    # 被别的会话引用后，那个会话的成员也能下载
    ref_id = "9" * 32
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["gid"], token=S["token_a"],
                           body={"client_id": ref_id, "text": "群里也发一份", "attachment_id": fid})
    eq("同一个附件在群里再引用一次 → 200", status, 200)
    status, blob5, _ = api("GET", "/api/files/%s" % fid, token=S["token_c"])
    eq("群成员 carol 现在能下载它了 → 200", status, 200)
    eq("群成员下载的字节一样", blob5, blob)


def sec_paging():
    posted = []
    for i in range(5):
        cid_hex = ("%02d" % (10 + i)) * 16  # 32 位 hex，互不相同
        status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"],
                               body={"client_id": cid_hex, "text": "第 %d 条" % (i + 1)})
        posted.append(cid_hex)
        eq("翻页用第 %d 条消息 → 200" % (i + 1), status, 200)

    # 此刻直聊里共有：m1、附件消息、F1..F5 —— 倒序 F5,F4,F3,F2,F1,附件,m1
    status, data, _ = japi("GET", "/api/conversations/%s/messages?limit=3" % S["cid"], token=S["token_a"])
    eq("?limit=3 → 200", status, 200)
    page1 = (data or {}).get("messages") or []
    eq("第一页 3 条", len(page1), 3)
    eq("第一页是新的 3 条，且按 ts 升序", [m.get("id") for m in page1], posted[2:5])
    eq("第一页 has_more == true", (data or {}).get("has_more"), True)
    ts1 = [m.get("ts") for m in page1]
    check("同一页内 ts 单调不减", ts1 == sorted(ts1), repr(ts1))

    before = page1[0].get("ts")
    status, data, _ = japi("GET", "/api/conversations/%s/messages?limit=3&before=%s" % (S["cid"], before),
                           token=S["token_a"])
    page2 = (data or {}).get("messages") or []
    eq("第二页 3 条", len(page2), 3)
    eq("第二页是再往前的 3 条（附件消息 + 最早两条）",
       [m.get("id") for m in page2], ["e" * 32, posted[0], posted[1]])
    eq("第二页 has_more == true", (data or {}).get("has_more"), True)

    before2 = page2[0].get("ts")
    status, data, _ = japi("GET", "/api/conversations/%s/messages?limit=3&before=%s" % (S["cid"], before2),
                           token=S["token_a"])
    page3 = (data or {}).get("messages") or []
    eq("第三页只剩最早那条", [m.get("id") for m in page3], [S["m1"]])
    eq("第三页 has_more == false", (data or {}).get("has_more"), False)
    check("第二页的 ts 全都小于第一页",
          all(m.get("ts") < before for m in page2), repr([m.get("ts") for m in page2]))
    check("分页不重不漏（三页 id 无重复）",
          len(set([m.get("id") for m in page1 + page2 + page3])) == 7, "总共 %d 条" % len(set([m.get("id") for m in page1 + page2 + page3])))

    status, data, _ = japi("GET", "/api/conversations/%s/messages?limit=1000" % S["cid"], token=S["token_a"])
    eq("limit 超上限也不报错（夹到 200）", status, 200)
    status, data, _ = japi("GET", "/api/conversations/%s/messages?before=abc" % S["cid"], token=S["token_a"])
    eq("before 不是数字 → 400 invalid_request", err_code(data), "invalid_request")
    status, data, _ = japi("GET", "/api/conversations/%s/messages?limit=abc" % S["cid"], token=S["token_a"])
    eq("limit 不是数字 → 400 invalid_request", err_code(data), "invalid_request")


def sec_logout_and_expiry():
    status, data, _ = japi("POST", "/api/logout", token=S["token_a2"])
    eq("退出登录 → 200", status, 200)
    eq("退出登录返回 ok", (data or {}).get("ok"), True)

    status, data, _ = japi("GET", "/api/me", token=S["token_a2"])
    eq("退出后 /api/me → 401", status, 401)
    eq("退出后 /api/me → unauthorized", err_code(data), "unauthorized")

    status, data, _ = japi("GET", "/api/conversations", token=S["token_a2"])
    eq("退出后 /api/conversations → 401", status, 401)

    # 另起一个 TTL 极小（但为正）的实例，验证"token 会过期"。
    # 注意：0 现在表示**永不过期**（见 --help），所以这里不能再用 0 来测过期 ——
    # 用 0.000005 天（约 0.43 秒）再等一会儿，效果一样。
    other = start_server(extra_args=["--token-ttl-days", "0.000005"], label="expiry")
    base = other["base"]
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "expired", "password": "secret123"}, base=base)
    eq("过期实例上注册 → 200", status, 200)
    tok = (data or {}).get("token")
    status, _, _ = japi("GET", "/api/me", token=tok, base=base)
    eq("token 还没到期时 /api/me → 200", status, 200)
    time.sleep(0.8)
    status, data, _ = japi("GET", "/api/me", token=tok, base=base)
    eq("token 过期后 /api/me → 401", status, 401)
    eq("token 过期 → unauthorized", err_code(data), "unauthorized")
    other["proc"].terminate()
    try:
        other["proc"].wait(timeout=10)
    except Exception:
        other["proc"].kill()


def sec_friends():
    """好友系统（一）：门禁 → 申请 → 同意（留言落成第一条消息）→ 删好友 → 再发又变申请。

    整段都复用前面已经注册好的账号 —— 不在主实例上再注册新人（第 17 段要核对
    health.users == 4）。
    """
    # alice 和 carol 之间先有一个单聊，但两个人还不是好友
    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_a"],
                           body={"username": "carol"})
    eq("和 carol 建单聊 → 200", status, 200)
    fcid = (data or {}).get("conversation", {}).get("id")
    check("好友用例的单聊建好了", bool(fcid), repr(data))

    cur_c = cursor_of(S["token_c"])          # carol 从"此刻"起收事件，别混进上面建会话那条
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % fcid, token=S["token_a"],
                           body={"client_id": "b1" * 16, "text": "你好，我是爱丽丝"})
    eq("非好友直聊发消息 → 200（不是报错）", status, 200)
    eq("门禁把 message 换成了 null", (data or {}).get("message"), None)
    fr = (data or {}).get("friend_request") or {}
    eq("friend_request.user 是对方 carol", (fr.get("user") or {}).get("username"), "carol")
    eq("friend_request.note 就是这条消息的正文", fr.get("note"), "你好，我是爱丽丝")
    check("friend_request.ts 是数字", isinstance(fr.get("ts"), (int, float)), repr(fr))

    status, data, _ = japi("GET", "/api/conversations/%s/messages" % fcid, token=S["token_c"])
    eq("被拦下的消息没有写进 messages", (data or {}).get("messages"), [])

    status, data, _ = japi("GET", "/api/friends", token=S["token_c"])
    eq("GET /api/friends → 200", status, 200)
    incoming = (data or {}).get("incoming") or []
    eq("carol 收到 1 条好友申请", len(incoming), 1)
    eq("申请人是 alice", (incoming[0].get("user") or {}).get("username") if incoming else None, "alice")
    eq("申请带着那条消息当留言", incoming[0].get("note") if incoming else None, "你好，我是爱丽丝")
    eq("还没同意 → friends 为空", (data or {}).get("friends"), [])

    status, data, _ = japi("GET", "/api/friends", token=S["token_a"])
    outgoing = (data or {}).get("outgoing") or []
    check("alice 的 outgoing 里是 carol",
          any((o.get("user") or {}).get("username") == "carol" for o in outgoing), repr(outgoing))

    events = Poll(S["token_c"], cur_c, 10).start().events()
    got = find_event(events, "friend_request")
    check("carol 长轮询收到的是 friend_request 事件", got is not None, repr(events))
    if got:
        eq("friend_request 事件带申请人", (got.get("user") or {}).get("username"), "alice")
        eq("friend_request 事件带留言", got.get("note"), "你好，我是爱丽丝")
    check("carol 没有收到 message 事件", find_event(events, "message") is None, repr(events))

    # 同意：留言落成会话里的第一条消息
    cur_a = cursor_of(S["token_a"])
    status, data, _ = japi("POST", "/api/friends/accept", token=S["token_c"], body={"username": "alice"})
    eq("carol 同意 alice 的申请 → 200", status, 200)
    eq("accept 返回 status=accepted", (data or {}).get("status"), "accepted")
    eq("accept 返回的 friend.user 是申请人",
       ((data or {}).get("friend") or {}).get("user", {}).get("username"), "alice")
    check("accept 返回的 since 是数字",
          isinstance(((data or {}).get("friend") or {}).get("since"), (int, float)), repr(data))
    eq("同意时复用已有的单聊（不另开一个）", ((data or {}).get("conversation") or {}).get("id"), fcid)

    status, data, _ = japi("GET", "/api/conversations/%s/messages" % fcid, token=S["token_c"])
    msgs = (data or {}).get("messages") or []
    eq("同意后会话里正好 1 条消息（就是那条留言）", len(msgs), 1)
    eq("留言变成了第一条消息", msgs[0].get("text") if msgs else None, "你好，我是爱丽丝")
    eq("这条消息是申请人发出的", msgs[0].get("sender_id") if msgs else None, S["user_a"].get("id"))

    status, data, _ = japi("GET", "/api/friends", token=S["token_a"])
    check("alice 的好友里有 carol",
          any((f.get("user") or {}).get("username") == "carol"
              for f in ((data or {}).get("friends") or [])), repr(data))
    eq("alice 的 outgoing 清空了", (data or {}).get("outgoing"), [])

    events = Poll(S["token_a"], cur_a, 10).start().events()
    got = find_event(events, "friend_accept")
    check("alice（申请人）收到 friend_accept 事件", got is not None, repr(events))
    if got:
        eq("friend_accept.user 是同意的 carol", (got.get("user") or {}).get("username"), "carol")

    # 是好友了 → 门禁放行
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % fcid, token=S["token_a"],
                           body={"client_id": "b2" * 16, "text": "现在能聊了"})
    eq("是好友之后发消息 → 200", status, 200)
    eq("好友单聊返回正常的 message", ((data or {}).get("message") or {}).get("id"), "b2" * 16)
    check("好友单聊的响应里没有 friend_request", (data or {}).get("friend_request") is None, repr(data))

    status, data, _ = japi("POST", "/api/friends/request", token=S["token_a"], body={"username": "carol"})
    eq("已经是好友再申请 → already_friends", (data or {}).get("status"), "already_friends")

    # 删好友：历史留着，再发消息又变成申请
    status, data, _ = japi("POST", "/api/friends/remove", token=S["token_a"], body={"username": "carol"})
    eq("删除好友 → 200 ok", (data or {}).get("ok"), True)
    status, data, _ = japi("GET", "/api/friends", token=S["token_a"])
    eq("删除后好友列表里没有 carol",
       [f for f in ((data or {}).get("friends") or [])
        if (f.get("user") or {}).get("username") == "carol"], [])
    status, data, _ = japi("POST", "/api/friends/remove", token=S["token_a"], body={"username": "carol"})
    eq("重复删除也是 200（幂等）", (data or {}).get("ok"), True)

    status, data, _ = japi("GET", "/api/conversations/%s/messages" % fcid, token=S["token_c"])
    eq("删好友不动历史消息", len((data or {}).get("messages") or []), 2)

    status, data, _ = japi("POST", "/api/conversations/%s/messages" % fcid, token=S["token_a"],
                           body={"client_id": "b3" * 16, "text": "删了好友还能发吗"})
    eq("删好友后再发 → 又变回申请（message 为 null）", (data or {}).get("message"), None)
    eq("新的 friend_request.note 就是新消息正文",
       ((data or {}).get("friend_request") or {}).get("note"), "删了好友还能发吗")
    status, data, _ = japi("GET", "/api/friends", token=S["token_c"])
    eq("carol 又收到一条 incoming", len((data or {}).get("incoming") or []), 1)

    # 收尾：carol 拒绝掉（这个会话有历史，必须留着）
    status, data, _ = japi("POST", "/api/friends/reject", token=S["token_c"], body={"username": "alice"})
    eq("carol 拒绝 alice 的申请 → ok", (data or {}).get("ok"), True)
    status, data, _ = japi("GET", "/api/conversations/%s/messages" % fcid, token=S["token_c"])
    eq("有历史的会话不会被拒绝顺手删掉", len((data or {}).get("messages") or []), 2)


def sec_friends_edges():
    """好友系统（二）：拒绝 / 空会话清理 / 两个人都伸手 / 边界与鉴权。"""
    # --- 拒绝：把"一条消息都没有"的单聊一起收掉，免得申请人留个点进去发不出话的空壳 ---
    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_d"],
                           body={"username": "carol"})
    dcid = (data or {}).get("conversation", {}).get("id")
    check("dave↔carol 的单聊建好了", bool(dcid), repr(data))
    cur_d = cursor_of(S["token_d"])
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % dcid, token=S["token_d"],
                           body={"client_id": "b4" * 16, "text": "交个朋友"})
    eq("dave 发消息同样被门禁拦成申请", (data or {}).get("message"), None)

    status, data, _ = japi("POST", "/api/friends/reject", token=S["token_c"], body={"username": "dave"})
    eq("carol 拒绝 dave → 200 ok", (data or {}).get("ok"), True)
    status, data, _ = japi("GET", "/api/friends", token=S["token_c"])
    eq("拒绝后 carol 的 incoming 空", (data or {}).get("incoming"), [])
    status, data, _ = japi("GET", "/api/friends", token=S["token_d"])
    eq("拒绝后 dave 的 outgoing 空", (data or {}).get("outgoing"), [])
    status, data, _ = japi("GET", "/api/conversations/%s/messages" % dcid, token=S["token_d"])
    eq("被拒后那个空单聊被一起删掉了", err_code(data), "not_found")
    events = Poll(S["token_d"], cur_d, 10).start().events()
    check("dave 收到 friend_reject 事件", find_event(events, "friend_reject") is not None, repr(events))

    # --- 两个人都伸了手 → /api/friends/request 直接 accepted ---
    status, data, _ = japi("POST", "/api/conversations/direct", token=S["token_b"],
                           body={"username": "dave"})
    bd = (data or {}).get("conversation", {}).get("id")
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % bd, token=S["token_b"],
                           body={"client_id": "b5" * 16, "text": "鲍勃举手"})
    eq("bob 的门禁申请带着留言", ((data or {}).get("friend_request") or {}).get("note"), "鲍勃举手")
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % bd, token=S["token_d"],
                           body={"client_id": "b6" * 16, "text": "戴夫举手"})
    eq("dave 的门禁申请带着留言", ((data or {}).get("friend_request") or {}).get("note"), "戴夫举手")
    status, data, _ = japi("POST", "/api/friends/request", token=S["token_b"], body={"username": "dave"})
    eq("对方已经申请过我 → 直接 accepted", (data or {}).get("status"), "accepted")
    eq("auto-accept 返回的好友是 dave",
       ((data or {}).get("friend") or {}).get("user", {}).get("username"), "dave")
    eq("auto-accept 复用同一个单聊", ((data or {}).get("conversation") or {}).get("id"), bd)
    status, data, _ = japi("GET", "/api/conversations/%s/messages" % bd, token=S["token_b"])
    eq("对方申请里的留言落成了消息",
       [m.get("text") for m in ((data or {}).get("messages") or [])], ["戴夫举手"])

    # --- 边界 ---
    status, data, _ = japi("POST", "/api/friends/accept", token=S["token_c"], body={"username": "dave"})
    eq("没有申请还同意 → 404 not_found", err_code(data), "not_found")
    status, data, _ = japi("POST", "/api/friends/reject", token=S["token_c"], body={"username": "dave"})
    eq("没有申请还拒绝 → 404 not_found", err_code(data), "not_found")
    status, data, _ = japi("POST", "/api/friends/request", token=S["token_a"], body={"username": "ghost"})
    eq("申请加不存在的人 → 404 not_found", err_code(data), "not_found")
    status, data, _ = japi("POST", "/api/friends/request", token=S["token_a"], body={"username": "alice"})
    eq("加自己为好友 → 400 invalid_request", err_code(data), "invalid_request")
    status, data, _ = japi("POST", "/api/friends/remove", token=S["token_c"], body={"username": "dave"})
    eq("删一个本来就不是好友的人 → 200（幂等）", (data or {}).get("ok"), True)

    # --- 鉴权（和现有一致：没带合法 token 一律 401 unauthorized） ---
    status, data, _ = japi("GET", "/api/friends")
    eq("GET /api/friends 不带 token → 401", status, 401)
    eq("…错误码是 unauthorized", err_code(data), "unauthorized")
    status, data, _ = japi("POST", "/api/friends/request", body={"username": "bob"})
    eq("POST /api/friends/request 不带 token → 401", status, 401)
    status, data, _ = japi("POST", "/api/friends/accept", token="bogus", body={"username": "bob"})
    eq("POST /api/friends/accept 假 token → 401", status, 401)
    status, data, _ = japi("POST", "/api/friends/remove", token="bogus", body={"username": "bob"})
    eq("POST /api/friends/remove 假 token → 401", status, 401)


def sec_friends_migration():
    """存量迁移：老库里已经存在的单聊，升级后要自动变成好友（否则他们发不出消息）。

    做法：先用一个正常实例建"两个账号 + 一个单聊"（这一步不会有 friends 行），
    再把库退回到"还没有好友功能"的样子 —— 删掉迁移标记位（老库根本没有这个键）
    和所有 friends 行 —— 最后再起一次服务端，看它有没有把这对单聊追认成好友。
    """
    data_dir = tempfile.mkdtemp(prefix="mchat-friends-migrate-")
    TMPDIRS.append(data_dir)
    first = start_server(data_dir=data_dir, label="legacy")
    base = first["base"]
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "old_a", "password": "secret123"}, base=base)
    token_a = (data or {}).get("token")
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "old_b", "password": "secret123"}, base=base)
    eq("存量用例的两个账号建好了", status, 200)
    status, data, _ = japi("POST", "/api/conversations/direct", token=token_a,
                           body={"username": "old_b"}, base=base)
    conv_id = (data or {}).get("conversation", {}).get("id")
    status, data, _ = japi("GET", "/api/friends", token=token_a, base=base)
    eq("只建了单聊、没同意过 → 还不是好友", (data or {}).get("friends"), [])
    _stop(first)

    # 把库退回"老库"：没有迁移标记位、没有任何 friends 行
    conn = sqlite3.connect(os.path.join(data_dir, "mchat.db"), timeout=30)
    try:
        conn.execute("DELETE FROM friends")
        conn.execute("DELETE FROM meta WHERE k='friends_migrated'")
        conn.commit()
    finally:
        conn.close()

    second = start_server(data_dir=data_dir, label="legacy2")
    base = second["base"]
    status, data, _ = japi("GET", "/api/friends", token=token_a, base=base)
    eq("重启后存量单聊自动成为好友",
       [f.get("user", {}).get("username") for f in ((data or {}).get("friends") or [])], ["old_b"])
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % conv_id, token=token_a,
                           body={"client_id": "c9" * 16, "text": "老会话照样能聊"}, base=base)
    eq("迁移之后老单聊直接就能发消息",
       ((data or {}).get("message") or {}).get("text"), "老会话照样能聊")
    _stop(second)


def _stop(info):
    """停掉一个自检起的服务端实例。"""
    proc = info["proc"]
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def sec_misc():
    # --no-register 的实例
    other = start_server(extra_args=["--no-register"], label="noreg")
    base = other["base"]
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "newbie", "password": "secret123"}, base=base)
    eq("--no-register 时注册 → 403", status, 403)
    eq("--no-register 时注册 → registration_disabled", err_code(data), "registration_disabled")
    other["proc"].terminate()
    try:
        other["proc"].wait(timeout=10)
    except Exception:
        other["proc"].kill()

    # 邀请码
    other = start_server(extra_args=["--invite-code", "letmein"], label="invite")
    base = other["base"]
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "newbie", "password": "secret123"}, base=base)
    eq("邀请码缺失 → 403 invalid_invite", err_code(data), "invalid_invite")
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "newbie", "password": "secret123", "invite": "letmein"}, base=base)
    eq("带对邀请码 → 200", status, 200)
    other["proc"].terminate()
    try:
        other["proc"].wait(timeout=10)
    except Exception:
        other["proc"].kill()

    # sync 参数校验
    status, data, _ = japi("GET", "/api/sync?cursor=abc", token=S["token_a"])
    eq("cursor 不是数字 → 400 invalid_request", err_code(data), "invalid_request")
    status, data, _ = japi("GET", "/api/sync?cursor=999999&timeout=1", token=S["token_a"])
    eq("cursor 超前（服务端重启过）也能拿到 resync 而不是报错", status, 200)
    evs = (data or {}).get("events") or []
    check("cursor 超前时回 resync 事件",
          any(e.get("kind") == "resync" for e in evs) or evs == [], repr(evs))

    # 未知接口
    status, data, _ = japi("GET", "/api/nope", token=S["token_a"])
    eq("未知接口 → 404 not_found", err_code(data), "not_found")

    # 413 too_large：上传超过 --max-upload-mb
    other = start_server(extra_args=["--max-upload-mb", "0.01"], label="small")
    base = other["base"]
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "uploader", "password": "secret123"}, base=base)
    tok = (data or {}).get("token")
    status, data, _ = japi("POST", "/api/upload?name=a.bin", token=tok, raw=b"x" * 20000, base=base)
    eq("超过 --max-upload-mb 的上传 → 413", status, 413)
    eq("超过 --max-upload-mb 的上传 → too_large", err_code(data), "too_large")
    status, data, _ = japi("POST", "/api/upload?name=a.bin", token=tok, raw=b"y" * 1000, base=base)
    eq("限额以内的上传 → 200", status, 200)
    eq("限额以内上传成功", (data or {}).get("attachment", {}).get("size"), 1000)
    other["proc"].terminate()
    try:
        other["proc"].wait(timeout=10)
    except Exception:
        other["proc"].kill()

    status, data, _ = japi("POST", "/api/conversations/%s/messages" % S["cid"], token=S["token_a"], raw=b"{bad json")
    eq("坏 JSON → 400 invalid_request", err_code(data), "invalid_request")

    # 健康检查还是活的，且计数对得上
    status, data, _ = japi("GET", "/api/health")
    check("跑完全程后 health 仍然 ok", (data or {}).get("ok") is True, repr(data))
    eq("health.users == 4", (data or {}).get("users"), 4)
    check("health.messages >= 7", (data or {}).get("messages") >= 7, repr(data))


def sec_hardening():
    """部署加固项：默认要邀请码、token 只认头、名字清洗、限流/锁定、长度与配额。

    这一段特意用**不带任何放松开关**的服务端（也就是别人把 server/ 拷过去直接
    ``python3 mchat_server.py`` 的那个状态），验的就是"出厂的默认状态能不能放出去"。
    """
    default = start_server(label="default", relaxed=False)
    base = default["base"]

    # (1) 默认要求邀请码，而且这个码必须能在启动日志里看见
    status, data, _ = japi("POST", "/api/register",
                           body={"username": "newbie", "password": "secret123"}, base=base)
    eq("默认参数下注册（无邀请码）→ 403", status, 403)
    eq("默认参数下注册 → invalid_invite", err_code(data), "invalid_invite")
    time.sleep(0.3)
    try:
        with open(default["log"], "rb") as fh:
            log_text = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        log_text = ""
        check("能读到默认服务端的日志", False, str(exc))
    invite = ""
    for line in log_text.splitlines():
        if "★" in line and "邀请码" in line:
            invite = line.split("：", 1)[1].split()[0]
    check("启动日志里能看到自动生成的邀请码", len(invite) >= 8, log_text[-600:])

    status, data, _ = japi("POST", "/api/register",
                           body={"username": "newbie", "password": "secret123",
                                 "invite": invite}, base=base)
    eq("带上日志里的邀请码注册 → 200", status, 200)
    token = (data or {}).get("token", "")
    check("拿到了令牌", bool(token))

    # (2) 令牌只走 Authorization 头：URL 里带 token 不再受理
    status, _, _ = japi("GET", "/api/conversations?token=" + urllib.parse.quote(token or ""),
                       base=base)
    eq("?token= 走 URL → 401", status, 401)
    status, _, _ = japi("GET", "/api/conversations", token=token, base=base)
    eq("Authorization 头 → 200", status, 200)

    # (3) 附件名清洗：路径成分 + CRLF（响应拆分）都得被洗掉
    dirty = "../..\\evil.bat\r\nSet-Cookie: injected=1\r\n"
    status, data, _ = japi("POST", "/api/upload?name=" + urllib.parse.quote(dirty),
                           raw=b"hello", token=token, base=base)
    info = ((data or {}).get("attachment") or {})
    got_name = str(info.get("name") or "")
    check("上传后存下的名字只剩文件名", got_name.startswith("evil.bat"), repr(got_name))
    check("名字里没有路径分隔符", "/" not in got_name and "\\" not in got_name, repr(got_name))
    check("名字里没有 CR/LF", "\r" not in got_name and "\n" not in got_name, repr(got_name))
    status, _, headers = api("GET", "/api/files/" + str(info.get("id")), token=token, base=base)
    keys = {k.lower() for k in (headers or {})}
    eq("下载附件 → 200", status, 200)
    check("响应头里没有被注入的字段", "set-cookie" not in keys and "injected" not in keys,
          repr(sorted(keys)))

    # (4) 每 IP 的登录/注册频率有闸门
    rate = start_server(extra_args=["--login-rate", "3", "--lock-after", "0"], label="rate")
    codes = []
    for index in range(4):
        status, _, _ = japi("POST", "/api/register",
                           body={"username": "rate%d" % index, "password": "secret123"},
                           base=rate["base"])
        codes.append(status)
    eq("前 3 次注册放行", codes[:3], [200, 200, 200])
    eq("第 4 次注册 → 429", codes[3], 429)

    # (5) 连续失败会锁账号，锁到期又能进
    lock = start_server(extra_args=["--login-rate", "0", "--lock-after", "2",
                                    "--lock-seconds", "1"], label="lock")
    status, _, _ = japi("POST", "/api/register",
                       body={"username": "locky", "password": "secret123"},
                       base=lock["base"])
    eq("锁定用例的账号建好了", status, 200)
    for index in range(2):
        status, data, _ = japi("POST", "/api/login",
                               body={"username": "locky", "password": "wrong-%d" % index},
                               base=lock["base"])
        eq("错密码第 %d 次 → 401" % (index + 1), status, 401)
        eq("错密码错误码", err_code(data), "bad_credentials")
    status, data, _ = japi("POST", "/api/login",
                           body={"username": "locky", "password": "secret123"},
                           base=lock["base"])
    eq("锁定期内连正确密码也进不去 → 429", status, 429)
    eq("锁定错误码", err_code(data), "too_many_requests")
    time.sleep(1.2)
    status, data, _ = japi("POST", "/api/login",
                           body={"username": "locky", "password": "secret123"},
                           base=lock["base"])
    eq("锁过期后正常登录 → 200", status, 200)

    # (6) 单条正文 / 合并转发有上限；typing 有节流
    limits = start_server(extra_args=["--max-text-chars", "50", "--max-merged", "1",
                                      "--typing-interval", "30"], label="limits")
    status, data, _ = japi("POST", "/api/register",
                          body={"username": "limits", "password": "secret123"},
                          base=limits["base"])
    ltoken = (data or {}).get("token", "")
    status, data, _ = japi("POST", "/api/register",
                          body={"username": "limits2", "password": "secret123"},
                          base=limits["base"])
    status, data, _ = japi("POST", "/api/conversations/direct", token=ltoken,
                          body={"username": "limits2"}, base=limits["base"])
    cid = ((data or {}).get("conversation") or {}).get("id", "")
    check("限额用例的会话建好了", bool(cid), repr(data))

    status, _, _ = japi("POST", "/api/conversations/%s/messages" % cid, token=ltoken,
                       body={"client_id": "h" * 32, "text": "x" * 50}, base=limits["base"])
    eq("50 字符正文 → 200", status, 200)
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % cid, token=ltoken,
                          body={"client_id": "i" * 32, "text": "x" * 51}, base=limits["base"])
    eq("51 字符正文 → 413", status, 413)
    eq("超长正文错误码", err_code(data), "message_too_long")

    item = {"author": "谁", "text": "话", "attachment": "", "clock": "12:00"}
    status, _, _ = japi("POST", "/api/conversations/%s/messages" % cid, token=ltoken,
                       body={"client_id": "j" * 32, "text": "合并", "merged": [item]},
                       base=limits["base"])
    eq("合并 1 条 → 200", status, 200)
    status, data, _ = japi("POST", "/api/conversations/%s/messages" % cid, token=ltoken,
                          body={"client_id": "k" * 32, "text": "合并", "merged": [item, item]},
                          base=limits["base"])
    eq("合并 2 条（上限 1）→ 400", status, 400)

    status, data, _ = japi("POST", "/api/conversations/%s/typing" % cid, token=ltoken,
                           body={"typing": True}, base=limits["base"])
    check("第一次 typing 正常推", status == 200 and not (data or {}).get("throttled"), data)
    status, data, _ = japi("POST", "/api/conversations/%s/typing" % cid, token=ltoken,
                           body={"typing": True}, base=limits["base"])
    check("紧接着再刷 typing 被节流", (data or {}).get("throttled") is True, data)
    status, data, _ = japi("POST", "/api/conversations/%s/typing" % cid, token=ltoken,
                           body={"typing": False}, base=limits["base"])
    check("typing=false 永远放行（对方要能收起\"正在输入…\"）",
          not (data or {}).get("throttled"), data)

    # (7) --token-ttl-days 0 = 永不过期（以前设 0 会让所有人立刻掉线）
    forever = start_server(extra_args=["--token-ttl-days", "0"], label="ttl0")
    status, data, _ = japi("POST", "/api/register",
                          body={"username": "forever", "password": "secret123"},
                          base=forever["base"])
    ftoken = (data or {}).get("token", "")
    status, _, _ = japi("GET", "/api/me", token=ftoken, base=forever["base"])
    eq("--token-ttl-days 0 时令牌立刻可用 → 200", status, 200)


def sec_shutdown():
    proc = S["proc"]
    eq("服务端进程还在跑", proc.poll(), None)
    if os.name == "nt":
        # Windows 上 terminate() 是硬杀，用 CTRL_BREAK 才能真正走到信号处理器
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()
    try:
        code = proc.wait(timeout=15)
        check("收到停止信号后 15 秒内退出", True)
    except Exception as exc:
        check("收到停止信号后 15 秒内退出", False, "超时：%s" % exc)
        proc.kill()
        return
    try:
        with open(S["log"], "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        check("能读到服务端日志", False, str(exc))
        return
    check("收到信号时打印了那一行日志", "正在优雅关闭" in text, text[-400:])
    check("退出时提交事务并打印收尾日志", "已退出，数据已落盘" in text, text[-400:])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    if not os.path.isfile(SERVER_PY):
        print("找不到 " + SERVER_PY)
        return 1
    print("MChat 服务端端到端自检")
    print("  解释器：%s" % sys.executable)
    print("  服务端：%s" % SERVER_PY)

    try:
        info = start_server()
    except Exception as exc:
        print("  FAIL  启动服务端: %s" % exc)
        print("\n0/1 通过")
        return 1
    S["base"] = info["base"]
    S["data_dir"] = info["data_dir"]
    S["log"] = info["log"]
    S["proc"] = info["proc"]
    print("  已启动：%s（数据目录 %s）" % (info["base"], info["data_dir"]))

    run_section("01 健康检查", sec_health)
    run_section("02 注册（含 3 条负例）", sec_register)
    run_section("03 登录 / 退出 / 鉴权", sec_login)
    run_section("04 找人（/api/users）", sec_users)
    run_section("05 建会话（单聊幂等）", sec_conversations)
    run_section("06 长轮询收到 message 事件", sec_longpoll)
    run_section("07 消息幂等（client_id）", sec_idempotent)
    run_section("08 未读 / 已读 + read 事件", sec_unread_read)
    run_section("09 typing 事件", sec_typing)
    run_section("10 群聊 + 非成员 403", sec_group)
    run_section("11 附件上传 / 下载 / 越权", sec_attachment)
    run_section("12 历史分页（before / has_more）", sec_paging)
    run_section("13 退出登录与 token 过期", sec_logout_and_expiry)
    run_section("14 好友：门禁 / 申请 / 同意 / 删除", sec_friends)
    run_section("15 好友：拒绝 / 互相申请 / 边界 / 鉴权", sec_friends_edges)
    run_section("16 好友：存量单聊自动成为好友（迁移）", sec_friends_migration)
    run_section("17 其它（开关 / 参数校验）", sec_misc)
    run_section("18 部署加固（邀请码 / 限流 / 名字清洗 / 上限）", sec_hardening)
    run_section("19 优雅停止", sec_shutdown)

    total = PASSED + FAILED
    print("\n%d/%d 通过" % (PASSED, total))
    if FAILED:
        print("（失败 %d 条）" % FAILED)
        print("服务端日志：%s" % info["log"])
    return 0 if FAILED == 0 else 1


def cleanup():
    for proc in PROCS:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if os.environ.get("MCHAT_SELFTEST_KEEP") != "1":
        for path in TMPDIRS:
            shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        cleanup()
    sys.exit(rc)
