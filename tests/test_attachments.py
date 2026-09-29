"""附件（图片 / 文件）的数据层单测，零 Qt 依赖。

用法：``python tests/test_attachments.py``；装了 pytest 也可以 ``pytest tests``。

覆盖：
    附件模型   —— 从路径造、名字/后缀/类型/大小文案、JSON 往返、坏数据降级
    消息与摘要 —— kind（text/image/file）、没有配文时的「📷 照片 / 📎 文件名」
    传输层     —— 纯附件消息也会被回复、回复可以带附件（每次一个，用完就没了）
    仓库       —— 按 id 找消息
    演示素材   —— 素材真在磁盘上、演示数据真的挂上了附件（缺素材时降级不报错）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mchat.core import assets as demo_assets                       # noqa: E402
from mchat.core import seed                                        # noqa: E402
from mchat.core.models import (Attachment, Conversation, Message,  # noqa: E402
                               human_size)
from mchat.core.store import ChatStore                             # noqa: E402
from mchat.core.transport import (EVENT_MESSAGE, LocalTransport,   # noqa: E402
                                  Outgoing, ReplyScript)


def _write(path: str, size: int = 2048) -> str:
    with open(path, "wb") as handle:
        handle.write(b"a" * size)
    return path


def _msg(mid: str, text: str, ts: float = 100.0, *, conv: str = "c1",
         attachment: Attachment | None = None, outgoing: bool = False) -> Message:
    return Message(id=mid, conversation_id=conv, sender_id="me" if outgoing else "lin",
                   text=text, ts=ts, outgoing=outgoing, attachment=attachment)


# ---------------------------------------------------------------------------
# 附件模型
# ---------------------------------------------------------------------------
def test_attachment_from_path_fills_name_and_size():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(os.path.join(tmp, "季度复盘报告.pdf"), 2048)
        attachment = Attachment.from_path(path)
        assert attachment.path == os.path.abspath(path)          # 绝对路径：换 cwd 也不怕
        assert attachment.name == "季度复盘报告.pdf"
        assert attachment.size == 2048
        assert attachment.exists is True
        assert attachment.size_text == "2 KB"                    # 2026/2 = 2048 -> 2.0 -> "2 KB"


def test_attachment_kind_suffix_and_labels():
    with tempfile.TemporaryDirectory() as tmp:
        image = Attachment.from_path(_write(os.path.join(tmp, "配色预览.PNG"), 10))
        document = Attachment.from_path(_write(os.path.join(tmp, "材料-v3.pdf"), 10))
        assert image.suffix == ".png" and image.ext == "PNG"     # 大写后缀也认
        assert image.is_image and image.kind == "image"
        assert image.label == "📷 照片"
        assert document.suffix == ".pdf" and document.ext == "PDF"
        assert not document.is_image and document.kind == "file"
        assert document.label == "📎 材料-v3.pdf"
        # 没有后缀的东西不能当成图片
        plain = Attachment.from_path(_write(os.path.join(tmp, "README"), 10))
        assert plain.is_image is False and plain.kind == "file"
        assert plain.label == "📎 README"


def test_attachment_missing_file_is_not_an_error():
    attachment = Attachment(path=r"C:\根本没有这个目录\没有这个文件.png",
                            name="没有这个文件.png", size=123)
    assert attachment.exists is False
    assert attachment.name == "没有这个文件.png"                 # 名字是存下来的，不靠路径
    assert attachment.size_text == "123 B"
    # 路径推不出名字时给个占位，别出现空名字
    assert Attachment(path="").name == "附件"


def test_human_size_units():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2 KB"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024 * 1024) == "5 MB"
    assert human_size(2 * 1024 ** 3) == "2 GB"
    assert human_size("坏数据") == ""


def test_attachment_json_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        original = Attachment.from_path(_write(os.path.join(tmp, "配色预览.png"), 4096))
        raw = original.dump()
        assert "配色预览.png" in raw                             # 中文不转义（库里也是原名）
        assert Attachment.from_json(json.loads(raw)) == original
        # 坏数据一律当"没有附件"，不要让界面炸掉
        assert Attachment.from_json(None) is None
        assert Attachment.from_json({"path": ""}) is None
        assert Attachment.from_json({"path": "/x/y.png", "size": "不小"}) \
            == Attachment(path="/x/y.png", name="y.png", size=0)


# ---------------------------------------------------------------------------
# 消息与列表摘要
# ---------------------------------------------------------------------------
def test_message_kind_and_preview():
    with tempfile.TemporaryDirectory() as tmp:
        image = Attachment.from_path(_write(os.path.join(tmp, "图.png"), 10))
        document = Attachment.from_path(_write(os.path.join(tmp, "文档.pdf"), 10))
        assert _msg("m0", "普通消息").kind == "text"
        assert _msg("m0", "普通消息").preview_text == "普通消息"
        pure_image = _msg("m1", "", attachment=image)
        assert pure_image.kind == "image" and pure_image.has_attachment
        assert pure_image.preview_text == "📷 照片"
        pure_file = _msg("m2", "  ", attachment=document)
        assert pure_file.kind == "file" and pure_file.preview_text == "📎 文档.pdf"
        captioned = _msg("m3", "看这个", attachment=image)
        assert captioned.preview_text == "看这个"                 # 有配文就用配文


def test_conversation_preview_shows_attachment_label():
    char = Conversation(id="c1", title="王尼玛")
    char.messages = [_msg("m1", "", attachment=Attachment(path="/x/图纸.png", name="图纸.png"))]
    assert char.preview_text() == "📷 照片"                       # 私聊不带前缀

    group = Conversation(id="c2", title="群")
    from mchat.core.models import ChatKind, User
    group.kind = ChatKind.GROUP
    group.members = [User("lin", "王尼玛"), User("me", "我")]
    group.messages = [_msg("m2", "", attachment=Attachment(path="/x/材料.pdf", name="材料.pdf"))]
    assert group.preview_text() == "王尼玛: 📎 材料.pdf"          # 群聊带发送者
    group.messages = [_msg("m3", "", outgoing=True,
                           attachment=Attachment(path="/x/我发的.pdf", name="我发的.pdf"))]
    assert group.preview_text() == "我: 📎 我发的.pdf"


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------
def _transport(scripts) -> tuple[LocalTransport, list[float]]:
    ticks = [time.time()]
    return LocalTransport(scripts, clock=lambda: ticks[0]), ticks


def test_transport_replies_to_attachment_only_message():
    """没有文字、只有附件也值得回一句（以前这里直接 return，附件消息会没人理）。"""
    transport, ticks = _transport({"c1": ReplyScript("lin", ("收到图了",), latency=1.0)})
    transport.send(Outgoing("c1", "me", "", "out-1",
                            attachment=Attachment(path="/x/图.png", name="图.png")))
    ticks[0] += 2.0
    messages = [event for event in transport.poll() if event.kind == EVENT_MESSAGE]
    assert len(messages) == 1 and messages[0].text == "收到图了"


def test_transport_attachment_replies_one_per_reply():
    """对方会"陆续"发文件过来：第 n 次回复带第 n 个附件，用完不再带。"""
    first = Attachment(path="/x/一.png", name="一.png")
    second = Attachment(path="/x/二.pdf", name="二.pdf")
    transport, ticks = _transport({"c1": ReplyScript("lin", ("给你",), latency=1.0,
                                                     attachments=(first, second))})
    got = []
    for _ in range(3):
        transport.send(Outgoing("c1", "me", "hi", "out"))
        ticks[0] += 2.0
        got += [event.attachment for event in transport.poll()
                if event.kind == EVENT_MESSAGE]
    assert got == [first, second, None]


def test_transport_ignores_empty_message_and_unknown_conversation():
    transport, ticks = _transport({"c1": ReplyScript("lin", ("在",), latency=1.0)})
    transport.send(Outgoing("c1", "me", "   "))                  # 没字也没附件：不回
    transport.send(Outgoing("没有这个会话", "me", "hi"))          # 没脚本：不回
    ticks[0] += 5.0
    assert transport.poll() == []
    script = ReplyScript("lin", ())
    assert script.line(0) == "" and script.attachment(0) is None   # 空脚本不炸


# ---------------------------------------------------------------------------
# 仓库 / 演示数据
# ---------------------------------------------------------------------------
def test_store_message_lookup():
    store = ChatStore([Conversation(id="c1", title="一"), Conversation(id="c2", title="二")])
    store.append(_msg("m1", "在 c1", conv="c1"))
    store.append(_msg("m2", "在 c2", conv="c2"))
    assert store.message("m2").text == "在 c2"
    assert store.message("m1").conversation_id == "c1"
    assert store.message("没有这条") is None


def test_demo_assets_exist_on_disk():
    """素材是提交在仓库里的：缺了的话程序里就看不到图片/文件消息（跑 build_demo_assets.py）。"""
    for name in (demo_assets.DEMO_PHOTO, demo_assets.DEMO_REPORT):
        path = demo_assets.demo_path(name)
        assert os.path.isabs(path), path                        # 必须绝对（冻结后同理）
        assert os.path.isfile(path), f"缺素材：{path}（跑 tools/build_demo_assets.py）"
        assert os.path.getsize(path) > 0


def test_demo_messages_carry_attachments():
    demo = seed.build_demo()
    messages = [message for conversation in demo.store.conversations()
                for message in conversation.messages if message.has_attachment]
    kinds = sorted(message.kind for message in messages)
    assert "image" in kinds and "file" in kinds, kinds
    for message in messages:
        assert os.path.isabs(message.attachment.path)           # 落库要存绝对路径
        assert message.attachment.exists, message.attachment.path
    # 自动回复里也带一张图：这样"收到别人发来的图片"在本机就能看到
    scripts = [script for script in demo.scripts.values() if script.attachments]
    assert len(scripts) == 1 and scripts[0].attachment(0).kind == "image"
    assert scripts[0].attachment(1) is None                      # 只发一次，之后不带


def test_demo_attachment_degrades_when_missing():
    """素材不在时返回 None（消息退化成纯文本），而不是抛异常。"""
    assert seed.demo_attachment("这个素材不存在.png") is None


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
