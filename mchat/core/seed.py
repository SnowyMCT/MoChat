"""单机演示数据：示例会话、消息，以及"对方"的自动回复台词。

界面启动时若没有真实传输层（还没有联网版），就用这里的例子把界面填满，
保证打开就能看到一个像模像样的聊天窗口。所有时间戳都相对"现在"生成，
所以日期分隔条永远显示"今天 / 昨天"，不会随日期跑偏。

演示数据里还带两条**附件消息**（一张图片 + 一个 PDF），素材是
``tools/build_demo_assets.py`` 生成、提交在仓库里的：这样打开程序就能看到
"图片消息 / 文件消息"长什么样，也能看到"对方发来的图片"（LIN 的自动回复带一张图）。
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass

from . import assets as demo_assets
from .models import Attachment, ChatKind, Conversation, ME_ID, Message, User
from .store import ChatStore
from .transport import ReplyScript

__all__ = ["Demo", "build_demo", "ME", "CHAT_LIN", "CHAT_GROUP", "CHAT_DESIGN",
           "CHAT_CHEN", "CHAT_BOT"]

# --------------------------------------------------------------------------- 参与者
# 注意：User 的 id（lin / chen / su / zhou）是历史命名的稳定标识，与显示名无关；
# 显示名（王尼玛 / 尼玛 / 曹尼玛 / 王尼美）随时可改，改这些字符串不会被 id 影响。
ME = User(ME_ID, "Monostar", online=True)
LIN = User("lin", "王尼玛", online=True)
CHEN = User("chen", "尼玛")
SU = User("su", "曹尼玛", online=True)
ZHOU = User("zhou", "王尼美")
BOT = User("bot", "MChat 助手", is_bot=True)

# --------------------------------------------------------------------------- 会话 id
CHAT_LIN = "c-lin"
CHAT_GROUP = "c-group"
CHAT_DESIGN = "c-design"
CHAT_CHEN = "c-chen"
CHAT_BOT = "c-bot"


@dataclass(frozen=True)
class Demo:
    """一次演示装配所需的全部东西。"""

    store: ChatStore
    scripts: dict[str, ReplyScript]
    me: User
    default_chat_id: str


def _builder():
    """消息工厂：自动编号，省得每行手写 id。"""
    counter = itertools.count(1)

    def make(conversation_id: str, sender: User, text: str, ts: float,
             *, status: str = "sent",
             attachment: Attachment | None = None) -> Message:
        return Message(
            id=f"m{next(counter)}",
            conversation_id=conversation_id,
            sender_id=sender.id,
            text=text,
            ts=ts,
            outgoing=sender.is_me,
            status=status,
            attachment=attachment,
        )

    return make


def demo_attachment(name: str) -> Attachment | None:
    """演示素材里的一个附件；素材不在（没跑生成脚本）就返回 None，不报错。"""
    if not demo_assets.demo_exists(name):
        return None
    return Attachment.from_path(demo_assets.demo_path(name))


def _day_anchor(now: float, days_back: int) -> float:
    """``days_back`` 天前那一天的本地 0 点。"""
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight - days_back * 86400


def build_demo(now: float | None = None) -> Demo:
    """生成示例仓库与自动回复脚本。``now`` 可注入，便于测试固定时间。"""
    now = time.time() if now is None else now

    def at(days_back: int, hour: int, minute: int) -> float:
        """``days_back`` 天前的 ``hour:minute``（对话时间统一从这里生成）。"""
        return _day_anchor(now, days_back) + hour * 3600 + minute * 60

    today = lambda h, m: at(0, h, m)          # noqa: E731
    yesterday = lambda h, m: at(1, h, m)      # noqa: E731
    days_ago = lambda d, h, m: at(d, h, m)    # noqa: E731
    make = _builder()
    # 演示附件（素材缺失会是 None，消息就退化成普通文本）
    photo = demo_attachment(demo_assets.DEMO_PHOTO)
    report = demo_attachment(demo_assets.DEMO_REPORT)

    lin = Conversation(id=CHAT_LIN, title=LIN.name, kind=ChatKind.DIRECT,
                       members=[ME, LIN], pinned=True)
    lin.messages = [
        make(CHAT_LIN, LIN, "周五那份评审材料我先过一遍，看有没有漏的。", yesterday(17, 42)),
        make(CHAT_LIN, ME, "好，我这边把流程图补上再发你。", yesterday(17, 45), status="read"),
        make(CHAT_LIN, LIN, "文件我放共享盘了，命名是 review-v3-final。", yesterday(17, 58),
             attachment=report),
        make(CHAT_LIN, LIN, "你有空看下最后一节，那部分我拿不准。", yesterday(18, 2)),
    ]

    group = Conversation(id=CHAT_GROUP, title="天下尼玛是一家", kind=ChatKind.GROUP,
                         members=[ME, LIN, CHEN, SU, ZHOU])
    group.messages = [
        make(CHAT_GROUP, CHEN, "早上好，昨天的方案我整理成表格了。", today(9, 14)),
        make(CHAT_GROUP, SU, "收到，我下午补一版交互稿。", today(9, 16)),
        make(CHAT_GROUP, ZHOU, "本周的验收节点还是周四吗？", today(9, 31)),
        make(CHAT_GROUP, LIN, "@王尼美 是的，周四下午两点，会议室改成 3 号。", today(9, 32)),
        make(CHAT_GROUP, ME, "我这边把接口文档同步完就发群里。", today(9, 35), status="read"),
    ]

    design = Conversation(id=CHAT_DESIGN, title="尼玛一家", kind=ChatKind.GROUP,
                          members=[ME, SU, LIN])
    design.messages = [
        make(CHAT_DESIGN, SU, "先看这张：暮色那版的主视觉，我按你上次说的把对比降了一点。",
             days_ago(1, 16, 18), attachment=photo),
        make(CHAT_DESIGN, SU, "新的配色方案放在 Figma 里了，大家看看。", days_ago(1, 16, 20)),
        make(CHAT_DESIGN, ME, "深色那套不错，层次比上一版清楚。", days_ago(1, 16, 26), status="read"),
        make(CHAT_DESIGN, SU, "嗯，我再把按钮的圆角统一一下。", days_ago(1, 16, 31)),
    ]

    chen = Conversation(id=CHAT_CHEN, title=CHEN.name, kind=ChatKind.DIRECT,
                        members=[ME, CHEN])
    chen.messages = [
        make(CHAT_CHEN, ME, "合同附件记得带原件。", days_ago(2, 15, 10), status="read"),
        make(CHAT_CHEN, CHEN, "知道，我放包里了。", days_ago(2, 15, 12)),
        make(CHAT_CHEN, CHEN, "周五的评审改到下午三点了。", today(8, 40)),
        make(CHAT_CHEN, ME, "收到，我调整一下日程。", today(8, 41), status="read"),
    ]

    bot = Conversation(id=CHAT_BOT, title=BOT.name, kind=ChatKind.DIRECT,
                       members=[ME, BOT])
    bot.messages = [
        make(CHAT_BOT, BOT, "欢迎使用 MChat 👋", today(8, 2)),
        make(CHAT_BOT, BOT, "左边是会话列表，右边是聊天窗口。Enter 发送，Shift+Enter 换行。", today(8, 3)),
        make(CHAT_BOT, BOT, "要编解码或加解密文字，点左上角「工具箱」按钮（Ctrl+T）——"
                            "那套安全文本转换器被收进来当小工具了。", today(8, 4)),
        make(CHAT_BOT, BOT, "想改成真联网的聊天也可以：传输层接口已经留好，"
                            "换掉 mchat/core/transport.py 里的实现就行。", today(8, 5)),
    ]

    store = ChatStore([lin, group, design, chen, bot])
    # 演示用的未读数是"开局状态"，接入真传输层后由新消息自然累积
    store.set_unread(CHAT_LIN, 2)
    store.set_unread(CHAT_GROUP, 5)
    store.set_unread(CHAT_DESIGN, 1)

    scripts = {
        CHAT_LIN: ReplyScript(LIN.id, (
            "截图我发你了，对着看。",
            "好，我看完回你 👍",
            "嗯，这份我来跟。",
            "行，那就这么定了。",
            "刚想起来，还有个细节要补。",
        ), latency=1.6,
            # 第一次回复带一张图：本机就能演示"收到别人发来的图片消息"
            attachments=tuple(a for a in (photo,) if a is not None)),
        CHAT_GROUP: ReplyScript(ZHOU.id, (
            "同意。",
            "我这边没问题。",
            "等我五分钟，先开个会。",
            "这个得跟供应商确认一下再回你。",
        ), latency=2.0),
        CHAT_DESIGN: ReplyScript(SU.id, (
            "我把图层重新整理了一版。",
            "间距统一成 8 的倍数了。",
            "这个组件的深浅色对比我再调调。",
        ), latency=1.8),
        CHAT_CHEN: ReplyScript(CHEN.id, (
            "好，那就这样。",
            "我已经更新到日程里了。",
            "有变动我随时同步给你。",
            "👍",
        ), latency=1.4),
        CHAT_BOT: ReplyScript(BOT.id, (
            "工具箱里的 AES 用随机 IV，同一条明文每次密文都不同。",
            "需要我帮你把某段文字加密后再发出来吗？",
            "小提示：工具箱里 Ctrl+Enter 直接执行转换。",
            "这条会话的回复都是本机脚本生成的，没有联网。",
        ), latency=1.2),
    }
    return Demo(store=store, scripts=scripts, me=ME, default_chat_id=CHAT_GROUP)
