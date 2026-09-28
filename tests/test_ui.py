"""聊天界面端到端测试（不弹窗、不抢焦点，走真实字体渲染）。

用法：``python tests/test_ui.py``
artifacts/ 下会留四张截图供人工复核：亮/暗主题的聊天窗口 + 工具箱窗口。

覆盖：侧栏会话列表与未读徽标、搜索过滤、切换会话、发消息与自动回复、
"正在输入…"、已读回执、未读规则、工具箱按钮与预填、主题切换、空消息拦截。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import Qt                                             # noqa: E402
from PyQt6.QtGui import QPalette                                        # noqa: E402
from PyQt6.QtWidgets import QApplication                                # noqa: E402

from mchat.core import seed                                             # noqa: E402
from mchat.core.models import Message                                   # noqa: E402
from mchat.core.transport import LocalTransport                         # noqa: E402
from mchat.ui.main_window import MainWindow                             # noqa: E402
from mchat.ui.theme import THEME_DARK, THEME_LIGHT                      # noqa: E402
from ui_probe import glyph_pixels, pump                                 # noqa: E402

ARTIFACTS = os.path.join(ROOT, "artifacts")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # 假时钟：让"对方回复/已读回执"的排期完全可控，测试不用真的等 2 秒
    ticks = [time.time()]
    demo = seed.build_demo()
    transport = LocalTransport(demo.scripts, clock=lambda: ticks[0])
    win = MainWindow(demo=demo, transport=transport)
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    QApplication.setActiveWindow(win)
    pump(app, 500)

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # --- 骨架 -------------------------------------------------------------
    check("窗口标题为 MChat", win.windowTitle() == "MChat", win.windowTitle())
    check("窗口可缩放（有最小尺寸、无固定尺寸）",
          win.minimumSize().width() >= 900 and
          win.maximumSize().width() > win.minimumSize().width(),
          f"{win.minimumSize()} / {win.maximumSize()}")
    check("侧栏宽度固定", win.sidebar.width() == 296, str(win.sidebar.width()))
    check("列出全部 5 个会话", len(win.sidebar._items) == 5,
          str(list(win.sidebar._items)))

    # --- 默认会话 ---------------------------------------------------------
    check("默认打开天下尼玛是一家", win.current_conversation_id() == seed.CHAT_GROUP,
          win.current_conversation_id())
    check("会话标题显示在头部", win.chat_view.name_label.text() == "天下尼玛是一家",
          win.chat_view.name_label.text())
    check("群聊头部显示成员数", win.chat_view.status_label.text() == "5 位成员",
          win.chat_view.status_label.text())
    check("消息气泡数量与数据一致",
          win.chat_view.sent_bubbles() == 5, str(win.chat_view.sent_bubbles()))
    check("日期分隔条为今天", win.chat_view.message_list.date_chips() == ["今天"],
          str(win.chat_view.message_list.date_chips()))
    check("当前会话被标为已读", win.store.unread(seed.CHAT_GROUP) == 0)
    check("总未读=其余三个会话之和", win.sidebar.total_badge.text() == "3",
          win.sidebar.total_badge.text())
    check("群聊里显示发送者名字",
          any(b.text() == "早上好，昨天的方案我整理成表格了。" for b in
              win.chat_view.message_list._bubbles.values()))

    # --- 切换会话 ---------------------------------------------------------
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 300)
    check("切换后头部跟着换", win.chat_view.name_label.text() == "王尼玛",
          win.chat_view.name_label.text())
    check("单聊头部显示在线状态", win.chat_view.status_label.text() == "在线",
          win.chat_view.status_label.text())
    check("切换后气泡重建", win.chat_view.sent_bubbles() == 4,
          str(win.chat_view.sent_bubbles()))
    check("昨天的时间线只有一条日期分隔", win.chat_view.message_list.date_chips() == ["昨天"],
          str(win.chat_view.message_list.date_chips()))
    check("切换会话清掉该会话未读", win.store.unread(seed.CHAT_LIN) == 0)
    check("总未读随之减少", win.sidebar.total_badge.text() == "1",
          win.sidebar.total_badge.text())

    # --- 搜索 -------------------------------------------------------------
    win.sidebar.search_field.setText("天下")
    pump(app, 260)
    check("搜索按会话名过滤", len(win.sidebar._items) == 1, str(list(win.sidebar._items)))
    win.sidebar.search_field.setText("评审")
    pump(app, 260)
    check("搜索能命中消息内容",
          list(win.sidebar._items) == [seed.CHAT_LIN, seed.CHAT_CHEN],
          str(list(win.sidebar._items)))
    win.sidebar.search_field.setText("不存在的词")
    pump(app, 260)
    check("无结果时列表为空", win.sidebar._items == {})
    win.sidebar.search_field.setText("")
    pump(app, 260)
    check("清空搜索后列表恢复", len(win.sidebar._items) == 5,
          str(len(win.sidebar._items)))

    # --- 发送消息 ---------------------------------------------------------
    # 把假时钟对齐到"真实此刻"再发：这样排期出来的回复时间戳一定晚于这条消息，
    # 消息顺序与真实运行一致（否则假时钟停得比真实时间早，回复会被按时间插到前面）。
    ticks[0] = time.time()
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 200)
    before = win.chat_view.sent_bubbles()
    win.chat_view.composer.edit.setPlainText("周五三点我有空")
    win.chat_view.composer.submit()
    pump(app, 200)
    check("发出后新增一个气泡", win.chat_view.sent_bubbles() == before + 1,
          f"{before} -> {win.chat_view.sent_bubbles()}")
    check("发送后输入框清空", win.chat_view.input_text() == "", win.chat_view.input_text())
    check("发出的是本机消息（立即入库）",
          win.store.get(seed.CHAT_LIN).last_message.text == "周五三点我有空")
    check("发消息不增加未读", win.store.unread(seed.CHAT_LIN) == 0)
    check("传输层已排期回复", transport.pending() >= 1, str(transport.pending()))

    # --- 正在输入 + 自动回复 + 已读回执 -----------------------------------
    ticks[0] += 0.5                      # 越过 typing_lead
    moved = win.tick()
    pump(app, 200)
    check("轮询到 1 个事件", moved == 1, str(moved))
    check("头部显示正在输入", win.chat_view.status_label.text() == "正在输入…",
          win.chat_view.status_label.text())
    check("侧栏预览也显示正在输入",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text() == "正在输入…",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text())

    ticks[0] += 2.5                      # 越过 latency + 已读回执
    win.tick()
    pump(app, 300)
    check("自动回复进了消息区", win.chat_view.sent_bubbles() == before + 2,
          str(win.chat_view.sent_bubbles()))
    check("回复后头部恢复正常状态", win.chat_view.status_label.text() == "在线",
          win.chat_view.status_label.text())
    check("侧栏预览更新为对方回复",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text() != "正在输入…",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text())
    check("自己发的消息变成已读", win.store.get(seed.CHAT_LIN).messages[-2].is_read)
    check("当前会话收到回复不留未读", win.store.unread(seed.CHAT_LIN) == 0)
    check("传输层已清空", transport.pending() == 0, str(transport.pending()))

    # --- 后台会话收到消息要累计未读 ---------------------------------------
    other = Message(id="in-test01", conversation_id=seed.CHAT_DESIGN, sender_id="su",
                    text="背景色再淡一点", ts=time.time(), outgoing=False)
    win.store.receive(other)
    pump(app, 200)
    check("非当前会话进未读", win.store.unread(seed.CHAT_DESIGN) == 2,
          str(win.store.unread(seed.CHAT_DESIGN)))
    check("未读徽标跟着变", win.sidebar._items[seed.CHAT_DESIGN].badge.text() == "2",
          win.sidebar._items[seed.CHAT_DESIGN].badge.text())
    check("别人的会话消息不进当前消息区",
          win.chat_view.message_list.has_message("in-test01") is False)
    current = Message(id="in-test02", conversation_id=seed.CHAT_LIN, sender_id="lin",
                      text="那我先按这版走", ts=time.time() + 1, outgoing=False)
    win.store.receive(current)
    pump(app, 200)
    check("当前会话的消息直接上屏",
          win.chat_view.message_list.has_message("in-test02") is True)
    check("当前会话不留未读", win.store.unread(seed.CHAT_LIN) == 0)

    # --- 空消息拦截 -------------------------------------------------------
    count_before = len(win.store.get(seed.CHAT_LIN).messages)
    win.chat_view.composer.edit.setPlainText("   ")
    win.chat_view.composer.submit()
    pump(app, 200)
    check("空消息被拦截", "空消息" in win.status_text(), win.status_text())
    check("空消息不产生气泡",
          len(win.store.get(seed.CHAT_LIN).messages) == count_before)

    # --- 工具箱按钮 -------------------------------------------------------
    tool = win.open_tool()
    tool.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pump(app, 400)
    check("工具箱已打开", tool.isVisible())
    check("工具箱是独立窗口", tool.parent() is win)
    check("重复点击复用同一实例", win.open_tool() is tool)
    check("工具箱跟随当前主题",
          tool.tool.output_field.get_label_progress() == 0.0)
    check("工具箱可用：Base64 编码",
          _tool_roundtrip(tool), tool.status_text())

    win.chat_view.composer.edit.setPlainText("这段话要加密发出")
    win._send_input_to_tool()
    pump(app, 300)
    check("可把输入框文字送进工具箱",
          tool.input_field.text() == "这段话要加密发出", tool.input_field.text())
    check("送进工具箱后有提示", "送进工具箱" in win.status_text(), win.status_text())

    # --- 输入字色可读性（回归：暗色主题下输入框曾是黑字，肉眼看不见）------
    win.select_conversation(seed.CHAT_LIN)
    composer = win.chat_view.composer.edit
    composer.setPlainText("输入字色可读性")
    pump(app, 250)

    win.set_theme(THEME_LIGHT)
    pump(app, 350)
    dark_px, light_px, darkest, _ = glyph_pixels(win, composer)
    check("亮色：输入框里是深色字", dark_px > 100, f"深={dark_px} 亮={light_px}")
    check("亮色：输入框调色板跟随主题",
          composer.palette().color(QPalette.ColorRole.Text).name().lower()
          == win.current_palette().text.lower(),
          composer.palette().color(QPalette.ColorRole.Text).name())

    win.set_theme(THEME_DARK)
    pump(app, 350)
    dark_px, light_px, darkest, lightest = glyph_pixels(win, composer)
    check("暗色：输入框里是浅色字", light_px > 100,
          f"深={dark_px} 亮={light_px} 最亮={lightest}")
    check("暗色：输入框没有黑字（最暗不低于底色）", darkest >= 30,
          f"最暗={darkest}")
    check("暗色：输入框调色板跟随主题",
          composer.palette().color(QPalette.ColorRole.Text).name().lower()
          == win.current_palette().text.lower(),
          composer.palette().color(QPalette.ColorRole.Text).name())
    composer.setPlainText("")          # 清空 -> 只剩占位符，一并验一下
    win.setFocus(Qt.FocusReason.OtherFocusReason)   # 焦点让出去：输入框聚焦时 Qt 不画占位符
    pump(app, 300)
    _, _, darkest, lightest = glyph_pixels(win, composer)
    check("暗色：占位符可读（也不是黑字）", lightest >= 120 and darkest >= 30,
          f"最亮={lightest} 最暗={darkest}")
    check("暗色：占位符色取自 muted 令牌",
          composer.palette().color(QPalette.ColorRole.PlaceholderText).name().lower()
          == win.current_palette().muted.lower(),
          composer.palette().color(QPalette.ColorRole.PlaceholderText).name())

    # --- 主题 + 截图 ------------------------------------------------------
    os.makedirs(ARTIFACTS, exist_ok=True)
    shots = []

    def shoot(tag: str) -> str:
        pump(app, 320)
        path = os.path.join(ARTIFACTS, f"chat_{tag}.png")
        win.grab().save(path)
        shots.append(path)
        return path

    win.select_conversation(seed.CHAT_GROUP)
    win.set_theme(THEME_LIGHT)
    pump(app, 400)
    check("主题切到亮色", win.current_palette().key == "light", win.current_palette().key)
    check("亮色下样式表已重建", "#3390EC" in win.styleSheet())
    shoot("light_group")

    win.set_theme(THEME_DARK)
    pump(app, 400)
    check("主题切到暗色", win.current_palette().key == "dark", win.current_palette().key)
    check("工具箱窗口跟着换主题", "#17212B" in tool.styleSheet() or
          "#1E2A38" in tool.styleSheet())
    win.select_conversation(seed.CHAT_BOT)
    pump(app, 320)
    shoot("dark_bot")

    tool.apply_palette(win.current_palette())
    pump(app, 320)
    path = os.path.join(ARTIFACTS, "chat_tool_dark.png")
    tool.grab().save(path)
    print(f"  shot  {path}")

    # --- 报告 -------------------------------------------------------------
    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}" +
              (f"  -> {detail}" if detail and not ok else ""))
    for path in shots:
        print(f"  shot  {path}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    win.close()          # 收尾关窗：留着可见窗口的话，脚本退出时 Qt 可能挂住不返回
    tool.close()
    return 1 if failed else 0


def _tool_roundtrip(tool) -> bool:
    """在工具箱里真跑一次 Base64 编码 -> 解码。"""
    from mchat.core import crypto
    tool.select_algorithm(0)
    tool.select_mode(0)
    tool.set_input("界面联调")
    tool.execute()
    encoded = tool.output_text()
    tool.select_mode(1)
    tool.set_input(encoded)
    tool.execute()
    return tool.output_text() == "界面联调" and encoded == crypto.encode_base64("界面联调")


def test_ui_suite() -> None:
    """让 ``pytest tests`` 也能跑这套脚本式用例（本地直接运行 main() 同样可行）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
