"""测试用的**真服务端**：把 ``server/mchat_server.py`` 当子进程跑起来。

为什么测试要起真的服务端，而不是写一个假的 HTTP 层
--------------------------------------------------
"客户端能不能连上服务端、消息能不能真的送达、附件能不能真的传过去"这件事，
只有让**两边真跑一遍**才算验过。假 HTTP 层只能证明"我调用了我以为的那个方法"，
协议字段写错了、长轮询认不出来的那种问题它一个都抓不到。

用法（每个测试文件都能自己跑）：

    from server_fixture import running_server

    with running_server() as server:          # 随机端口 + 临时数据目录
        api = ApiClient(server.base_url)
        api.register("alice", "secret123", "爱丽丝")
        ...

收尾一定会杀掉子进程并删掉临时目录（``with`` 退出时），不会在机器上留垃圾。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_SCRIPT = os.path.join(ROOT, "server", "mchat_server.py")
START_TIMEOUT = 30.0            # 等它把端口监听起来（第一次跑要编译 pyc，给宽一点）


def free_port() -> int:
    """让操作系统分一个空闲端口（避免和机器上正在跑的服务端撞车）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerProcess:
    """一个跑起来的服务端。``base_url`` 就是客户端要填的那个地址。"""

    def __init__(self, process: subprocess.Popen, base_url: str, data_dir: str,
                 log_path: str):
        self.process = process
        self.base_url = base_url
        self.data_dir = data_dir
        self.log_path = log_path

    def alive(self) -> bool:
        return self.process.poll() is None

    def log(self, limit: int = 4000) -> str:
        """服务端的输出（起不来的时候看一眼就知道为什么）。"""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()[-limit:]
        except OSError:
            return ""

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            # Windows 上一定要**连整棵树**一起收：venv 的 Scripts\python.exe 只是个壳，
            # 真正跑起来的服务端是它 fork 出来的子进程，只 terminate() 壳子会把服务端
            # 留成孤儿 —— 任务管理器里每个漏下的测试服务端正好是**两个** python，
            # 攒几次就成了"关了软件还一堆 python 在跑"。
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                           capture_output=True)
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=8)


def _health_ok(base_url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/api/health", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


@contextlib.contextmanager
def running_server(*, extra_args=(), keep: bool = False, relaxed: bool = True):
    """起一个服务端，退出时保证收干净。``extra_args`` 可以加 ``--no-register`` 之类。

    默认（``relaxed=True``）刻意把闸门放开：那些闸门（邀请码 / 登录限流 / 账号锁定）
    本身要单独测，而绝大多数用例只想安安静静地注册几个账号 —— 全部流量都来自
    127.0.0.1，开着限流会让它们互相干扰（第 13 次注册就开始 429）。
    ``relaxed=False`` 起的是**出厂默认**那个服务端（注册要邀请码、登录有限流），
    加固项由 ``tests/test_hardening.py`` 用它来验。
    """
    data_dir = tempfile.mkdtemp(prefix="mchat-test-")
    log_path = os.path.join(data_dir, "server.log")
    port = free_port()
    python, env = _server_launch()
    command = [python, SERVER_SCRIPT, "--host", "127.0.0.1", "--port", str(port),
               "--data", os.path.join(data_dir, "data")]
    if relaxed:
        command += ["--open-register", "--login-rate", "0", "--lock-after", "0"]
    command += list(extra_args)
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                               cwd=ROOT, env=env)
    server = ServerProcess(process, f"http://127.0.0.1:{port}", data_dir, log_path)
    try:
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if not server.alive():
                raise RuntimeError("服务端起来就退出了：\n" + server.log())
            if _health_ok(server.base_url):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("等不到服务端的 /api/health：\n" + server.log())
        yield server
    finally:
        server.stop()
        log.close()
        if not keep:
            shutil.rmtree(data_dir, ignore_errors=True)


def _server_launch() -> tuple[str, dict | None]:
    """跑服务端用哪个解释器、配什么环境变量。

    为什么不用现成的 ``sys.executable``：venv 里的 ``Scripts\\python.exe`` 在 Windows 上
    是个**重定向壳**，它会把真正的解释器再拉成一个子进程 —— 于是每个测试服务端在任务
    管理器里占**两条** python（父壳 + 子解释器）。壳被 kill 掉、子进程留下就是孤儿，
    攒几次就变成"跑过测试之后一堆 python 在跑"。直接用基础解释器跑就只剩一条，
    ``terminate()``/``taskkill`` 也不会再漏。

    代价是要自己把 venv 的 site-packages 带上（服务端的可选磁盘加密要 pycryptodome）：
    走 ``PYTHONPATH`` 显式传过去，效果和 venv 里跑一样。不在 venv 里跑就直接用
    ``sys.executable``、环境原样继承。
    """
    base = getattr(sys, "_base_executable", "") or ""
    if not base or not os.path.isfile(base):
        return sys.executable, None
    if os.path.normcase(base) == os.path.normcase(sys.executable):
        return sys.executable, None
    env = dict(os.environ)
    extra = [path for path in sys.path
             if path and os.path.isdir(path)
             and os.path.normcase(os.path.abspath(path)).startswith(
                 os.path.normcase(os.path.abspath(sys.prefix)) + os.sep)]
    if extra:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(extra + ([existing] if existing else []))
    return base, env


def make_friends(client_a, client_b, *, note: str = ""):
    """走**真实产品流程**把两个账号变成好友：A 申请、B 同意。

    做别的前置时（只想让两人能互发消息）``note`` 留空 —— 服务端会把非空附言
    落成会话的**第一条消息**，白白多出一条会搅乱后面的断言。要测附言本身
    就自己传 ``note``，并断言那条消息。

    返回 ``(a 的用户名, b 的用户名)``，方便调用方继续拿用户名建会话。
    """
    a_name = str(client_a.me()["user"]["username"])
    b_name = str(client_b.me()["user"]["username"])
    result = client_a.friend_request(b_name, note)
    if result.get("status") != "accepted":      # 对方先前也伸过手，就直接成了
        client_b.friend_accept(a_name)
    return a_name, b_name
