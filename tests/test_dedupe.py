"""附件去重（内容寻址）的用例：同一份字节在服务端只存一份。

分两层验：

* **进程内**（直接 import ``server/mchat_server.py`` 的 App）—— 磁盘上到底有几个文件、
  blobs 登记、配额怎么算、孤儿回收什么时候才肯删字节、老数据归位。这些是"账本"，
  黑盒 HTTP 看不出来；
* **端到端**（真服务端子进程 + HTTP）—— 换个账号传同一张图，磁盘上还是那一份，
  而且两边各自下载都拿得到原字节（去重不能把"谁能下"搞坏）。

用法：``.venv/Scripts/python.exe tests/test_dedupe.py`` 或 ``pytest tests``。
"""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mchat.core.api import ApiClient                                        # noqa: E402
from server_fixture import make_friends, running_server                     # noqa: E402
import mchat_server                                                         # noqa: E402

ME = {"id": "u_me", "username": "me"}
OTHER = {"id": "u_other", "username": "other"}


def build_app(tmp_path, *, storage_key: bytes | None = None):
    """建一个跑在临时目录里的服务端应用（不监听端口，直接调方法）。"""
    argv = ["--data", str(tmp_path / "data"), "--open-register"]
    key_file = None
    if storage_key is not None:
        key_file = tmp_path / "storage.key"
        key_file.write_bytes(storage_key)
        argv += ["--storage-key-file", str(key_file)]
    cfg = mchat_server.Config(mchat_server.build_parser().parse_args(argv))
    os.makedirs(cfg.files_dir, exist_ok=True)
    db = mchat_server.Database(cfg)
    app = mchat_server.App(db, mchat_server.EventBus(db), cfg)
    return app, cfg


def files_on_disk(cfg) -> list[str]:
    return sorted(name for name in os.listdir(cfg.files_dir)
                  if not name.endswith(".part"))


def register_client(base_url: str, username: str, display: str = "") -> ApiClient:
    """注册一个账号并把令牌装好（``register`` 返回的就是令牌，不装上去后面全是 401）。"""
    api = ApiClient(base_url)
    payload = api.register(username, "secret123", display or username)
    api.token = str(payload["token"])
    return api


# --------------------------------------------------------------------- 进程内
def test_same_bytes_are_stored_once(tmp_path):
    """同样的字节传两次：两份附件记录、磁盘上只有一个文件，第二次标 deduped。"""
    app, cfg = build_app(tmp_path)
    payload = b"PNG-BYTES-" * 32

    first = app.save_upload(ME, "照片.png", payload)
    second = app.save_upload(ME, "同一张照片 (1).png", payload)
    other = app.save_upload(ME, "另一张.png", b"DIFFERENT")

    assert first["id"] != second["id"], "每次上传都要有自己的附件 id"
    assert first["deduped"] is False and second["deduped"] is True
    assert second["name"] == "同一张照片 (1).png", "名字是各次各自的，不能被去重抹掉"
    assert other["deduped"] is False
    assert len(files_on_disk(cfg)) == 2, files_on_disk(cfg)

    # 两条记录指向同一份字节，下载都拿得到原内容
    for att_id in (first["id"], second["id"]):
        _, path = app.attachment_path(ME, att_id)
        assert open(path, "rb").read() == payload


def test_quota_counts_each_content_once(tmp_path):
    """配额按**内容**算：重复上传不再扣一次，而且不会因为"服务端已有"被卡住。"""
    app, cfg = build_app(tmp_path)
    payload = b"X" * 1000
    app.cfg.user_quota_mb = 1000 / 1048576.0          # 只放得下一份 1000 字节

    app.save_upload(ME, "a.bin", payload)             # 用满
    again = app.save_upload(ME, "b.bin", payload)     # 同一份内容：不该再扣、也不该被拒
    assert again["deduped"] is True
    assert app.used_bytes(ME["id"]) == 1000

    with pytest.raises(mchat_server.ApiError) as caught:
        app.save_upload(ME, "c.bin", b"Y" * 1000)     # 真·新内容：这时才该拒
    assert caught.value.code == "quota_exceeded"


def test_each_uploader_owns_their_own_record(tmp_path):
    """去重的是字节，不是"附件记录"：属主/名字还是各自的（IDOR 那道校验靠它）。"""
    app, cfg = build_app(tmp_path)
    payload = b"SAME"

    mine = app.save_upload(ME, "我的.png", payload)
    theirs = app.save_upload(OTHER, "他的.png", payload)

    owners = {row["id"]: row["owner_id"] for row in
              app.db.query("SELECT id, owner_id FROM attachments")}
    assert owners[mine["id"]] == ME["id"]
    assert owners[theirs["id"]] == OTHER["id"]
    # 只有引用它的那条消息所在会话的成员能下：别人的 id 拿不到
    with pytest.raises(mchat_server.ApiError) as caught:
        app.attachment_path(OTHER, mine["id"])
    assert caught.value.code == "not_member"
    assert len(files_on_disk(cfg)) == 1


def test_orphan_sweep_keeps_bytes_that_someone_else_still_uses(tmp_path):
    """回收孤儿附件时：这份内容只要还有别的附件引用，磁盘字节就不许删。"""
    app, cfg = build_app(tmp_path)
    payload = b"SHARED-IMAGE"
    orphan = app.save_upload(ME, "发到一半失败了.png", payload)
    used = app.save_upload(OTHER, "我还在用的.png", payload)
    # used 被一条消息引用（orphan 没有）
    stamp = mchat_server.now_ts()
    app.db.execute(
        "INSERT INTO messages(id, conversation_id, sender_id, text, ts, attachment_id, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("m_1", "c_1", OTHER["id"], "看图", stamp, used["id"], stamp))

    # 把这些附件的创建时间改到很久以前，好让回收器认为它们过期
    old = stamp - 30 * 86400
    app.db.execute("UPDATE attachments SET created_at=?", (old,))

    assert app.cleanup_orphan_attachments(days=7) == 1          # 只有孤儿那条被清
    assert len(files_on_disk(cfg)) == 1, "别人还在用的字节被误删了"
    _, path = app.attachment_path(OTHER, used["id"])
    assert open(path, "rb").read() == payload
    assert app.db.query_one("SELECT 1 FROM attachments WHERE id=?", (orphan["id"],)) is None


def test_bytes_are_dropped_once_nobody_references_them(tmp_path):
    """最后一个引用也没了 → blobs 行和磁盘字节一起删（不能让空间只涨不跌）。"""
    app, cfg = build_app(tmp_path)
    att = app.save_upload(ME, "没人要了.png", b"GONE")
    app.db.execute("UPDATE attachments SET created_at=?", (mchat_server.now_ts() - 30 * 86400,))

    assert app.cleanup_orphan_attachments(days=7) == 1
    assert files_on_disk(cfg) == []
    assert app.db.query_one("SELECT 1 FROM blobs") is None


def test_orphan_blobs_without_attachment_are_swept(tmp_path):
    """上传到一半就崩了（字节写了、附件记录没落）→ 孤儿字节也该被收掉。"""
    app, cfg = build_app(tmp_path)
    leaf = mchat_server.BLOB_PREFIX + mchat_server.content_hash(b"HALF")
    with open(os.path.join(cfg.files_dir, leaf), "wb") as handle:
        handle.write(b"HALF")
    app.db.execute("INSERT INTO blobs(hash,size,path,created_at) VALUES(?,?,?,?)",
                   (mchat_server.content_hash(b"HALF"), 4, leaf, mchat_server.now_ts()))

    assert app.cleanup_orphan_blobs() == 1
    assert files_on_disk(cfg) == []


def test_quota_is_checked_before_new_bytes_are_written(tmp_path):
    """配额不够时**不许**先落盘（否则每次失败都白留一份字节）。"""
    app, cfg = build_app(tmp_path)
    app.cfg.user_quota_mb = 1 / 1048576.0            # 1 字节
    with pytest.raises(mchat_server.ApiError):
        app.save_upload(ME, "太大.png", b"TOO-BIG")
    assert files_on_disk(cfg) == []


# --------------------------------------------------------------- 老数据归位
def test_legacy_attachments_are_indexed_and_deduped(tmp_path):
    """老布局（文件叫附件 id、没有内容哈希）+ 加密落盘：启动时归位、重复的收回。"""
    app, cfg = build_app(tmp_path, storage_key=bytes(range(32)))
    payload = b"LEGACY-BYTES" * 8
    # 两份内容相同的老数据：一份明文、一份密文（正是"升级到加密那天的库"的样子）
    with open(os.path.join(cfg.files_dir, "f_old1"), "wb") as handle:
        handle.write(payload)
    with open(os.path.join(cfg.files_dir, "f_old2"), "wb") as handle:
        handle.write(app.storage.encrypt_bytes(payload))
    for index in (1, 2):
        app.db.execute(
            "INSERT INTO attachments(id, owner_id, name, size, created_at) VALUES(?,?,?,?,?)",
            ("f_old%d" % index, ME["id"], app._seal("旧图%d.png" % index),
             len(payload), float(index)))

    assert app.migrate_attachment_blobs() == (2, 1)          # 归位 2 条、回收 1 份
    assert files_on_disk(cfg) == ["f_old1"], "重复的那份该被回收，保留原来的名字不动"
    hashes = {row["id"]: row["content_hash"] for row in
              app.db.query("SELECT id, content_hash FROM attachments")}
    assert hashes["f_old1"] == hashes["f_old2"] == mchat_server.content_hash(payload)
    for att_id in ("f_old1", "f_old2"):                      # 两条都能下载（都指向同一份）
        _, path = app.attachment_path(ME, att_id)
        assert app.storage.decrypt_bytes(open(path, "rb").read()) == payload

    assert app.migrate_attachment_blobs() == (0, 0), "归位必须幂等"

    # 归位之后：再传同样内容的人也走去重（老图和新图统一了）
    fresh = app.save_upload(ME, "今天又发一遍.png", payload)
    assert fresh["deduped"] is True
    assert len(files_on_disk(cfg)) == 1


def test_missing_bytes_do_not_get_a_hash(tmp_path):
    """字节本来就丢了的老记录：留着空哈希（下载时报"已丢失"），不许瞎指一个文件。"""
    app, cfg = build_app(tmp_path)
    app.db.execute(
        "INSERT INTO attachments(id, owner_id, name, size, created_at) VALUES(?,?,?,?,?)",
        ("f_gone", ME["id"], "早就没了.png", 10, 1.0))

    assert app.migrate_attachment_blobs() == (0, 0)
    row = app.db.query_one("SELECT content_hash FROM attachments WHERE id=?", ("f_gone",))
    assert not row["content_hash"]
    with pytest.raises(mchat_server.ApiError) as caught:
        app.attachment_path(ME, "f_gone")
    assert caught.value.code == "not_found"


# ------------------------------------------------------------------- 端到端
def test_end_to_end_two_accounts_share_one_copy():
    """真服务端：两个账号传同一张图 → files/ 只有一个文件，两边都能下回原字节。"""
    with running_server() as live:
        alice = register_client(live.base_url, "dedupe_alice", "爱丽丝")
        bob = register_client(live.base_url, "dedupe_bob", "鲍勃")
        cid = alice.create_direct("dedupe_bob")["id"]
        make_friends(alice, bob)

        payload = bytes(range(256)) * 4 + b"same-picture"
        folder = tempfile.mkdtemp()
        try:
            image = os.path.join(folder, "风景.png")
            with open(image, "wb") as handle:
                handle.write(payload)
            first = alice.upload(image, name="风景.png")
            second = alice.upload(image, name="风景（再发一次）.png")
            third = bob.upload(image, name="我这边也有一张.png")

            assert first["deduped"] is False
            assert second["deduped"] is True and third["deduped"] is True
            assert len({first["id"], second["id"], third["id"]}) == 3

            files = os.path.join(live.data_dir, "data", "files")
            stored = [name for name in os.listdir(files) if not name.endswith(".part")]
            assert len(stored) == 1, "同一份内容在服务端只该占一个文件：%s" % stored
            assert stored[0].startswith(mchat_server.BLOB_PREFIX)

            # 谁发的那条消息引用自己的附件 id（不能引用别人的）
            alice.send_message(cid, "a" * 32, "第一张", attachment_id=first["id"])
            bob.send_message(cid, "b" * 32, "我这张一样的", attachment_id=third["id"])
            try:
                alice.send_message(cid, "c" * 32, "偷用别人的 id", attachment_id=third["id"])
            except Exception as error:                      # noqa: BLE001
                assert "不能引用" in str(error), str(error)
            else:
                pytest.fail("引用别人的附件 id 竟然成功了")

            # 双方都能把对方那条消息里的附件下回来，字节一模一样
            for client in (alice, bob):
                for item in client.messages(cid, limit=20)["messages"]:
                    if item.get("attachment"):
                        dest = os.path.join(folder, "dl_" + item["attachment"]["id"])
                        client.download(item["attachment"]["id"], dest)
                        assert open(dest, "rb").read() == payload
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def test_end_to_end_legacy_layout_is_migrated_on_boot(tmp_path):
    """老库（文件叫附件 id、没有内容哈希）上线新版：启动时归位，随后去重生效。

    这里必须**用同一个数据目录起两次**（``running_server`` 每次都是新临时目录），
    所以自己拉进程。
    """
    data_dir = str(tmp_path / "legacy-data")
    key = tmp_path / "storage.key"
    key.write_bytes(bytes(range(32)))

    def boot():
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "mchat_server.py"),
             "--host", "127.0.0.1", "--port", str(port), "--data", data_dir,
             "--open-register", "--login-rate", "0", "--lock-after", "0",
             "--storage-key-file", str(key)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        base = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                if ApiClient(base, timeout=2.0).health().get("ok"):
                    return process, base
            except Exception:                     # noqa: BLE001
                time.sleep(0.2)
        process.kill()
        raise AssertionError("服务端没起来")

    payload = b"OLD-LAYOUT-IMAGE" * 16
    folder = tmp_path / "uploads"
    folder.mkdir()
    image = str(folder / "老图.png")
    with open(image, "wb") as handle:
        handle.write(payload)

    process, base = boot()
    try:
        client = register_client(base, "legacy_alice", "爱丽丝")
        info = client.upload(image, name="老图.png")
        old_id = info["id"]
        assert info["deduped"] is False
    finally:
        process.terminate()
        process.wait(timeout=10)

    # 把磁盘/库改回"老版本留下的样子"：字节文件叫附件 id、没有去重登记
    files_dir = os.path.join(data_dir, "files")
    stored = [name for name in os.listdir(files_dir) if not name.endswith(".part")]
    assert len(stored) == 1
    os.replace(os.path.join(files_dir, stored[0]), os.path.join(files_dir, old_id))
    with sqlite3.connect(os.path.join(data_dir, "mchat.db")) as conn:
        conn.execute("UPDATE attachments SET content_hash=NULL")
        conn.execute("DELETE FROM blobs")

    process, base = boot()
    try:
        client = ApiClient(base)
        client.token = str(client.login("legacy_alice", "secret123")["token"])

        # 启动时归位：登记 blobs，文件**不改名**（还是那个附件 id）
        with sqlite3.connect(os.path.join(data_dir, "mchat.db")) as conn:
            rows = list(conn.execute("SELECT id, content_hash FROM attachments"))
            blobs = list(conn.execute("SELECT hash, path FROM blobs"))
        assert rows and all(row[1] for row in rows), "老附件应该被算出内容哈希"
        assert len(blobs) == 1 and blobs[0][1] == old_id, blobs
        assert os.path.isfile(os.path.join(files_dir, old_id))

        # 老附件照样下得回来
        dest = str(folder / "down.png")
        client.download(old_id, dest)
        assert open(dest, "rb").read() == payload

        # 归位之后，"再传一遍同样的内容"走去重
        again = client.upload(image, name="再传一遍.png")
        assert again["deduped"] is True
        assert sorted(name for name in os.listdir(files_dir)
                      if not name.endswith(".part")) == [old_id]
    finally:
        process.terminate()
        process.wait(timeout=10)


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
