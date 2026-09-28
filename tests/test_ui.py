"""聊天界面端到端测试（不弹窗、不抢焦点，走真实字体渲染）。

用法：``python tests/test_ui.py``
artifacts/ 下会留五张截图供人工复核：亮/暗主题的聊天窗口、表情面板（展开态 + 搜索态）、
分界线高亮态，另有工具箱窗口一张。

覆盖：侧栏会话列表与未读徽标、搜索过滤、切换会话、发消息与自动回复、
'"正在输入…"、已读回执、未读规则、工具箱按钮与预填、主题切换、空消息拦截、
分界线拖动改变会话列表宽度（含夹取/双击还原/持久化/竖线渲染）、
表情面板（分类/搜索/插入/最近使用/持久化/彩色渲染）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QEvent, QPoint, QSettings, Qt                  # noqa: E402
from PyQt6.QtGui import (QColor, QContextMenuEvent, QPalette,       # noqa: E402
                         QTextCursor)
from PyQt6.QtTest import QTest                                          # noqa: E402
from PyQt6.QtWidgets import QApplication                                # noqa: E402

from mchat.core import seed                                             # noqa: E402
from mchat.core.db import ChatLog                                       # noqa: E402
from mchat.core.models import Message                                   # noqa: E402
from mchat.core.transport import LocalTransport                         # noqa: E402
from mchat.ui import emoji, flags as flags_data                         # noqa: E402
from mchat.ui.main_window import MainWindow                             # noqa: E402
from mchat.ui.sidebar import SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH       # noqa: E402
from mchat.ui.theme import THEME_DARK, THEME_LIGHT                      # noqa: E402
from ui_probe import (double_click_handle, drag_split_handle,           # noqa: E402
                      colorful_pixels, glyph_pixels, press_handle, pump,
                      release_handle, wait_for, widget_pixel)

ARTIFACTS = os.path.join(ROOT, "artifacts")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # 假时钟：让"对方回复/已读回执"的排期完全可控，测试不用真的等 2 秒
    ticks = [time.time()]
    demo = seed.build_demo()
    transport = LocalTransport(demo.scripts, clock=lambda: ticks[0])
    # 独立设置命名空间：既不污染用户真实的主题/宽度偏好，也不被它影响
    settings = QSettings("MChatTest", "MChatTest")
    settings.clear()
    # 本地历史库也注入：内存库，绝不碰用户 %APPDATA% 里的真实数据；确认弹窗走固定答案
    log = ChatLog(":memory:")
    answers = {"confirm": True}
    win = MainWindow(demo=demo, transport=transport, settings=settings, log=log,
                     confirm=lambda **kw: answers["confirm"])
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
    check("侧栏默认宽度", win.sidebar.width() == 296, str(win.sidebar.width()))
    check("分界线紧贴侧栏右边", win.split_handle.x() == win.sidebar.width(),
          f"{win.split_handle.x()} vs {win.sidebar.width()}")
    check("列出全部 5 个会话", len(win.sidebar._items) == 5,
          str(list(win.sidebar._items)))

    # --- 分界线：拖动改变会话列表宽度 -------------------------------------
    handle = win.split_handle
    check("分界线是左右拖动光标",
          handle.cursor().shape() == Qt.CursorShape.SplitHCursor)
    check("分界线默认竖线色取自边框令牌",
          widget_pixel(win, handle, 3, handle.height() // 2).name().lower()
          == QColor(win.current_palette().border).name().lower())

    chat_before = win.chat_view.width()
    drag_split_handle(handle, 90)
    pump(app, 320)
    check("向右拖动加宽会话列表", win.sidebar.width() == 386,
          str(win.sidebar.width()))
    check("宽度同步给分界线", handle.sidebar_width() == 386, str(handle.sidebar_width()))
    check("聊天区等量变窄", win.chat_view.width() == chat_before - 90,
          f"{chat_before} -> {win.chat_view.width()}")
    check("分界线跟着挪位", handle.x() == win.sidebar.width(),
          f"{handle.x()} vs {win.sidebar.width()}")
    check("会话条目跟着变宽（摘要不再更早被截断）",
          win.sidebar._items[seed.CHAT_GROUP].width() > 260,
          str(win.sidebar._items[seed.CHAT_GROUP].width()))
    check("松开后不再是拖动状态", handle.is_dragging() is False)

    drag_split_handle(handle, -200)
    pump(app, 320)
    check("向左拖动收窄会话列表（并夹在最小宽度）",
          win.sidebar.width() == SIDEBAR_MIN_WIDTH, str(win.sidebar.width()))
    check("收窄夹在最小宽度上", win.sidebar_width() == SIDEBAR_MIN_WIDTH,
          str(win.sidebar_width()))
    check("聊天区回到宽的一侧", win.chat_view.width() == chat_before + 296
          - SIDEBAR_MIN_WIDTH, str(win.chat_view.width()))

    drag_split_handle(handle, 900)
    pump(app, 320)
    check("拖到超出上限被夹住", win.sidebar_width() == SIDEBAR_MAX_WIDTH,
          str(win.sidebar_width()))

    check("宽度已写进设置", settings.value("sidebar_width", -1) == SIDEBAR_MAX_WIDTH,
          str(settings.value("sidebar_width", -1)))
    check("新窗口沿用上次宽度（重启记忆）",
          _restarted_width(settings, app) == SIDEBAR_MAX_WIDTH)

    double_click_handle(handle)
    pump(app, 320)
    check("双击分界线还原默认宽度", win.sidebar.width() == 296,
          str(win.sidebar.width()))

    check("鼠标悬停时竖线变主题色（会真的重绘）",
          _hover_line_color(win, app) ==
          QColor(win.current_palette().accent).name().lower())
    check("移出后竖线恢复边框色",
          widget_pixel(win, handle, 3, handle.height() // 2).name().lower()
          == QColor(win.current_palette().border).name().lower())
    check("拖动中的竖线加粗且底色变浅（拖完自动恢复）",
          _drag_visual(win) is True)

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
    # 打字指示器：文字省掉省略号，改由三个跳动点承担（Telegram 那种）
    check("头部显示正在输入", win.chat_view.status_label.text() == "正在输入",
          win.chat_view.status_label.text())
    check("三个跳动的小点在跑",
          win.chat_view.typing_dots.isVisible() and win.chat_view.typing_dots.is_running(),
          f"可见={win.chat_view.typing_dots.isVisible()} "
          f"运行={win.chat_view.typing_dots.is_running()}")
    first_offsets = win.chat_view.typing_dots.dot_offsets()
    win.chat_view.typing_dots.advance(90)                 # 推进相位，点会动
    check("点会真的动起来（相位不同 -> 偏移不同）",
          win.chat_view.typing_dots.dot_offsets() != first_offsets
          and len(set(round(v, 3) for v in win.chat_view.typing_dots.dot_offsets())) > 1,
          str([round(v, 2) for v in win.chat_view.typing_dots.dot_offsets()]))
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
    check("回复到了，跳动点也停了",
          not win.chat_view.typing_dots.isVisible()
          and not win.chat_view.typing_dots.is_running())
    settled = wait_for(app, lambda: all(
        bubble.graphicsEffect() is None and not bubble.is_animating()
        for bubble in win.chat_view.message_list.bubbles().values()))
    check("入场动画结束后不留透明效果（稳态零开销）", settled,
          str([b.id for b in win.chat_view.message_list.bubbles().values()
               if b.is_animating()]))
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

    # --- 表情面板 ---------------------------------------------------------
    picker = win.chat_view.emoji_picker
    check("表情面板默认收起",
          not win.chat_view.emoji_panel_open() and not picker.isVisible())
    check("收起时高度为 0", picker.height() == 0, str(picker.height()))
    check("分类图标条齐全（最近 + 7 类文字 + 旗帜）",
          picker.categories() == ["recent", "faces", "animals", "food",
                                  "activity", "travel", "objects", "symbols",
                                  "flags"],
          str(picker.categories()))
    check("面板里表情够多（Telegram 那种量级）", emoji.total_count() >= 400,
          str(emoji.total_count()))

    message_height = win.chat_view.message_list.height()
    win.chat_view.composer.smile_btn.click()          # 真实点击表情按钮
    pump(app, 460)
    check("点表情按钮展开面板",
          win.chat_view.emoji_panel_open() and picker.isVisible())
    check("展开后占满面板高度", picker.height() == picker.PANEL_HEIGHT,
          str(picker.height()))
    check("展开是把消息区顶上去（不是浮层遮挡）",
          win.chat_view.message_list.height() < message_height,
          f"{message_height} -> {win.chat_view.message_list.height()}")
    check("默认停在「最近」，没用过所以是空的",
          picker.visible_chars() == [] and "还没有用过" in picker.hint_text(),
          picker.hint_text())

    picker.select_category("faces")
    pump(app, 260)
    check("切到「表情」分类列出整类",
          len(picker.visible_chars()) == emoji.item_count("faces"),
          f"{len(picker.visible_chars())} vs {emoji.item_count('faces')}")
    filled, detail = _grid_fills_width(picker)
    check("格子按面板宽度排满（不是固定 9 列）", filled, detail)
    columns_wide = _row_columns(picker)

    win.set_sidebar_width(440, persist=False)      # 面板变窄 -> 每行格子数应该变少
    pump(app, 320)
    columns_narrow = _row_columns(picker)
    check("面板变窄后自动重排（列数变少）",
          columns_narrow < columns_wide, f"{columns_wide} -> {columns_narrow}")
    check("重排后仍是排满的", _grid_fills_width(picker)[0], "")
    win.reset_sidebar_width()
    pump(app, 320)
    check("恢复宽度后列数也回来",
          _row_columns(picker) == columns_wide,
          f"{_row_columns(picker)} vs {columns_wide}")

    picker.set_search_text("猫")
    pump(app, 260)
    check("中文关键词能搜",
          "🐱" in picker.visible_chars() and "🐼" in picker.visible_chars(),
          "".join(picker.visible_chars()[:10]))
    picker.set_search_text("rocket")
    pump(app, 260)
    check("英文关键词也能搜", picker.visible_chars() == ["🚀"],
          "".join(picker.visible_chars()))
    picker.set_search_text("这个词肯定搜不到")
    pump(app, 260)
    check("搜不到时给提示", "没有匹配" in picker.hint_text(), picker.hint_text())
    picker.set_search_text("")
    pump(app, 260)
    check("清空搜索回到原分类",
          len(picker.visible_chars()) == emoji.item_count("faces"),
          str(len(picker.visible_chars())))

    # 点格子插入：走真实 clicked 信号，且不该把焦点从输入框抢走
    win.chat_view.composer.edit.setPlainText("早上好")
    cursor = win.chat_view.composer.edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)     # 光标放句尾，验"插到光标处"
    win.chat_view.composer.edit.setTextCursor(cursor)
    win.chat_view.composer.edit.setFocus(Qt.FocusReason.OtherFocusReason)
    pump(app, 200)
    cell = picker.cell_at("😀")
    cell.click()
    pump(app, 240)
    check("点表情插到光标处", win.chat_view.input_text() == "早上好😀",
          win.chat_view.input_text())
    check("插入后焦点仍在输入框（格子不吃焦点）",
          win.focusWidget() is win.chat_view.composer.edit,
          str(win.focusWidget()))
    check("用过的表情进了「最近」", picker.recents()[:1] == ["😀"],
          str(picker.recents()))
    check("表情格子真的是彩色渲染（不是豆腐块/单色）",
          colorful_pixels(win, cell) > 50, str(colorful_pixels(win, cell)))

    picker.select_category("recent")
    pump(app, 240)
    check("「最近」里能看到刚用过的表情", picker.visible_chars()[:1] == ["😀"],
          "".join(picker.visible_chars()[:5]))
    picker.choose("🎉")
    pump(app, 200)
    check("最近使用按新→旧排序", picker.recents()[:2] == ["🎉", "😀"],
          str(picker.recents()[:3]))
    check("最近使用已写进设置", "😀" in str(settings.value("emoji_recent", "")),
          str(settings.value("emoji_recent", "")))
    check("重启后仍记得用过的表情",
          "🎉" in _restarted_emoji_recents(settings, app),
          str(_restarted_emoji_recents(settings, app)))

    win.chat_view.composer.smile_btn.click()          # 再点一次 = 收起
    pump(app, 420)
    check("再点表情按钮收起面板",
          not win.chat_view.emoji_panel_open() and picker.height() == 0,
          f"open={win.chat_view.emoji_panel_open()} h={picker.height()}")
    check("收起后消息区高度复原",
          win.chat_view.message_list.height() == message_height,
          f"{win.chat_view.message_list.height()} vs {message_height}")

    check("Ctrl+E 打开、再按一次收起",
          win.chat_view.toggle_emoji_panel() is True
          and win.chat_view.emoji_panel_open()
          and win.chat_view.toggle_emoji_panel() is False
          and not win.chat_view.emoji_panel_open())
    check("Esc 收起面板（按钮状态跟着回弹）",
          _escape_closes_panel(win, app))
    win.chat_view.composer.edit.setPlainText("")

    # --- 旗帜（本地精灵图：Windows 字体画不了国旗）-------------------------
    check("旗帜素材就在仓库里（不联网）", flags_data.sprite_exists(),
          flags_data.sprite_path())
    check("旗帜清单：259 国旗 + 10 特殊旗",
          len(flags_data.FLAGS) == 269, str(len(flags_data.FLAGS)))
    win.chat_view.set_emoji_panel_open(True)
    picker.select_category("flags")
    pump(app, 360)
    check("旗帜分类整类列出",
          len(picker.visible_chars()) == len(flags_data.FLAGS),
          f"{len(picker.visible_chars())} vs {len(flags_data.FLAGS)}")
    cn_cell = picker.cell_at("🇨🇳")
    check("旗帜格子画的是图片不是文字",
          cn_cell is not None and cn_cell.text() == ""
          and not cn_cell.icon().isNull())
    check("旗帜格子真的是彩色的（从精灵图切出来了）",
          colorful_pixels(win, cn_cell) > 50, str(colorful_pixels(win, cn_cell)))
    jp_cell = picker.cell_at("🇯🇵")
    check("每面旗切的是自己那一格（中日颜色不同）",
          cn_cell.icon().pixmap(24, 24).toImage() !=
          jp_cell.icon().pixmap(24, 24).toImage())

    picker.set_search_text("中国")
    pump(app, 260)
    check("搜中文国名能出旗",
          {"🇨🇳", "🇭🇰", "🇲🇴"} <= set(picker.visible_chars()),
          "".join(picker.visible_chars()[:6]))
    picker.set_search_text("japan")
    pump(app, 260)
    check("搜英文国名能出旗", picker.visible_chars() == ["🇯🇵"],
          "".join(picker.visible_chars()))
    picker.set_search_text("cote")
    pump(app, 260)
    check("重音名也搜得到（cote -> côte d'ivoire）",
          picker.visible_chars() == ["🇨🇮"], "".join(picker.visible_chars()))
    picker.set_search_text("")
    pump(app, 260)

    win.chat_view.composer.edit.setPlainText("我们在")
    cursor = win.chat_view.composer.edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    win.chat_view.composer.edit.setTextCursor(cursor)
    picker.cell_at("🇨🇳").click()
    pump(app, 240)
    check("点旗子插进输入框", win.chat_view.input_text() == "我们在🇨🇳",
          win.chat_view.input_text())
    check("旗子也进「最近」", picker.recents()[:1] == ["🇨🇳"],
          str(picker.recents()))
    check("输入框里旗帜是图片（文档里有 image 片段）",
          _edit_image_count(win.chat_view.composer.edit) == 1,
          str(_edit_image_count(win.chat_view.composer.edit)))
    check("取文本时又还原成字符（不会把图片符号发出去）",
          win.chat_view.input_text() == "我们在🇨🇳", win.chat_view.input_text())

    # 发出去：仓库里是原字符，气泡/摘要里是图片
    win.chat_view.composer.submit()
    pump(app, 420)
    conversation_id = win.current_conversation_id()
    sent = win.store.get(conversation_id).last_message
    check("仓库里存的是原字符（core/传输层不知道图片的存在）",
          sent.text == "我们在🇨🇳", sent.text)
    bubble = win.chat_view.message_list._bubbles[sent.id]
    check("气泡里旗帜换成了 <img> 富文本",
          bubble.text_label.textFormat() == Qt.TextFormat.RichText
          and flags_data.png_url("🇨🇳") in bubble.text_label.text(),
          bubble.text_label.text()[:90])
    check("气泡里的旗帜真的画出来了（彩色像素）",
          colorful_pixels(win, bubble.text_label) > 30,
          str(colorful_pixels(win, bubble.text_label)))
    check("气泡取文本仍是原字符", bubble.text() == "我们在🇨🇳", bubble.text())
    check("侧栏摘要里旗帜也是图片",
          win.sidebar._items[conversation_id].preview_label.textFormat()
          == Qt.TextFormat.RichText,
          win.sidebar._items[conversation_id].preview_label.text()[:60])

    # 不含旗帜的消息仍走纯文本：消息里的 HTML 不该被执行
    win.chat_view.composer.edit.setPlainText("<b>加粗</b>")
    win.chat_view.composer.submit()
    pump(app, 320)
    plain = win.store.get(conversation_id).last_message
    plain_bubble = win.chat_view.message_list._bubbles[plain.id]
    check("不含旗帜的消息仍是纯文本格式",
          plain_bubble.text_label.textFormat() == Qt.TextFormat.PlainText)
    check("消息里的 <b> 原样显示，不被当标签吃掉",
          plain_bubble.text_label.text() == "<b>加粗</b>",
          plain_bubble.text_label.text())

    # 旗帜 + 标签混排：标签转义成文字，只有旗帜变图
    win.chat_view.composer.edit.set_message_text("")
    win.chat_view.composer.edit.insert_text("🇨🇳<i>x</i>")
    win.chat_view.composer.submit()
    pump(app, 320)
    mixed = win.store.get(conversation_id).last_message
    mixed_bubble = win.chat_view.message_list._bubbles[mixed.id]
    check("旗帜 + 尖括号：存的仍是原文", mixed.text == "🇨🇳<i>x</i>", mixed.text)
    check("旗帜 + 尖括号：标签被转义，只插了图片",
          "&lt;i&gt;" in mixed_bubble.text_label.text()
          and "<i>" not in mixed_bubble.text_label.text(),
          mixed_bubble.text_label.text()[:90])

    win.chat_view.composer.edit.setPlainText("")
    win.chat_view.hide_emoji_panel()
    pump(app, 320)

    # --- 本地历史（SQLite 写穿 + 重启回填）----------------------------------
    conversation_id = win.current_conversation_id()
    check("演示数据也进了库（库是完整日志）", win.log.count() >= 20,
          str(win.log.count()))
    check("发出去的消息立刻写穿到库",
          any(m.text == "我们在🇨🇳" for m in win.log.load(conversation_id)))
    stats = win.log_stats()
    check("统计信息可用（条数/占用）",
          stats["messages"] >= 20 and stats["conversations"] >= 1 and stats["bytes"] >= 0,
          str(stats))
    check("内存库这次没有可回填的历史", win.hydrated_count() == 0,
          str(win.hydrated_count()))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "mchat.db")
        first = _fresh_window(settings, app, ChatLog(db_path))
        first_id = first.current_conversation_id()
        first.send_message("重启前发的消息")
        pump(app, 220)
        saved = first.log.count(first_id)
        check("关窗前消息已落盘", saved >= 1, str(saved))
        _close_window(first, app)

        second = _fresh_window(settings, app, ChatLog(db_path))
        restored = second.store.get(first_id)
        check("重启后历史被回填进内存",
              any(m.text == "重启前发的消息" for m in restored.messages),
              str([m.text for m in restored.messages][-3:]))
        check("回填只算「库里多出来的」那些", second.hydrated_count() >= 1,
              str(second.hydrated_count()))
        check("重启后库里的条数不变（幂等 upsert）",
              second.log.count(first_id) == saved,
              f"{second.log.count(first_id)} vs {saved}")

        # 会话旗标也要跨重启：置顶/免打扰照旧，清空过的会话**不能**让演示数据复活。
        # 在仍开着的 second 上改（它持有这个文件库），关掉后再开第三个窗口验证。
        check("重启前先置顶 + 免打扰 + 清掉一个会话",
              second.toggle_pin(seed.CHAT_CHEN) is True
              and second.toggle_mute(seed.CHAT_DESIGN) is True
              and second.clear_conversation_history(seed.CHAT_BOT) is True)
        pump(app, 300)
        _close_window(second, app)

        third = _fresh_window(settings, app, ChatLog(db_path))
        check("重启后置顶/免打扰都还在",
              third.store.get(seed.CHAT_CHEN).pinned is True
              and third.store.get(seed.CHAT_DESIGN).muted is True)
        check("重启后列表里也画着 📌 / 🔕",
              "📌" in third.sidebar._items[seed.CHAT_CHEN].mark_label.text()
              and "🔕" in third.sidebar._items[seed.CHAT_DESIGN].mark_label.text())
        check("重启后清空过的会话仍然是空的（演示数据没复活）",
              third.store.get(seed.CHAT_BOT).messages == []
              and third.log.count(seed.CHAT_BOT) == 0,
              f"{len(third.store.get(seed.CHAT_BOT).messages)} 条")
        check("没清过的会话照常有演示消息",
              len(third.store.get(seed.CHAT_GROUP).messages) >= 1)
        _close_window(third, app)

    # --- 草稿 -------------------------------------------------------------
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 220)
    win.chat_view.composer.edit.insert_text("这段话还没写完")
    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 260)
    check("切走后输入框清空（草稿没跟过来）", win.chat_view.input_text() == "",
          win.chat_view.input_text())
    check("草稿写进了库", win.drafts().get(seed.CHAT_LIN) == "这段话还没写完",
          str(win.drafts()))
    check("会话列表把草稿标出来了",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text().startswith("草稿："),
          win.sidebar._items[seed.CHAT_LIN].preview_label.text())
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 260)
    check("切回来草稿还在", win.chat_view.input_text() == "这段话还没写完",
          win.chat_view.input_text())
    win.chat_view.composer.submit()
    pump(app, 260)
    check("发出后草稿作废",
          win.drafts().get(seed.CHAT_LIN, "") == ""
          and not win.sidebar._items[seed.CHAT_LIN].preview_label.text().startswith("草稿："),
          str(win.drafts()))

    # --- 搜索：从哪个会话进去就搜哪个会话（FTS5）---------------------------
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 240)
    dialog = win.open_search()
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pump(app, 240)
    check("搜索窗口打开且复用同一实例", dialog.isVisible() and win.open_search() is dialog)
    check("默认只搜进来时那个会话",
          dialog.scope_mode() == "current" and dialog.scope_id() == seed.CHAT_LIN,
          f"{dialog.scope_mode()} / {dialog.scope_id()}")
    check("窗口标题/输入框都标出是哪个会话",
          seed.LIN.name in dialog.windowTitle() and seed.LIN.name in dialog.scope_title(),
          f"{dialog.windowTitle()} / {dialog.scope_title()}")
    dialog.set_query("评审")                       # 演示数据里两个会话都有"评审"
    pump(app, 60)
    check("输入后先防抖（不立刻查库）", dialog.row_count() == 0, str(dialog.row_count()))
    pump(app, 360)
    check("防抖结束才出结果", dialog.row_count() >= 1, str(dialog.row_count()))
    hits = dialog.results()
    check("结果带着会话与消息 id", hits and all(len(pair) == 2 for pair in hits), str(hits[:2]))
    check("❰范围❱只出当前会话的结果",
          {cid for cid, _mid in hits} == {seed.CHAT_LIN}, str({cid for cid, _mid in hits}))
    check("提示里点明搜的是哪个会话",
          f"在「{seed.LIN.name}」" in dialog.hint.text(), dialog.hint.text())
    target_conversation, target_message = hits[0]
    check("点第一条能跳过去", dialog.pick_first() is True)
    pump(app, 320)
    check("跳转切到那条会话", win.current_conversation_id() == target_conversation,
          f"{win.current_conversation_id()} vs {target_conversation}")
    jumped = win.chat_view.message_list.bubbles().get(target_message)
    check("跳到的气泡被高亮（flash）", jumped is not None and jumped.is_flashing())

    # 切到"全部会话"：才允许跨会话
    dialog.scope_control.set_index(1)
    pump(app, 340)
    check("切到全部会话后模式记录为 all",
          dialog.scope_mode() == "all" and dialog.scope_id() == "",
          f"{dialog.scope_mode()} / {dialog.scope_id()}")
    all_hits = dialog.results()
    check("全部会话时能跨会话出结果",
          len({cid for cid, _mid in all_hits}) >= 2, str({cid for cid, _mid in all_hits}))
    other_hit = next((hit for hit in all_hits if hit[0] != seed.CHAT_LIN), None)
    check("另有一条在别的会话里", other_hit is not None, str(all_hits))
    if other_hit is not None:
        check("跨会话跳转也能定位", win.jump_to_message(*other_hit) is True
              and win.current_conversation_id() == other_hit[0]
              and win.chat_view.message_list.bubbles()[other_hit[1]].is_flashing())

    # 切回"仅当前会话"，并且范围要跟着当前会话走
    dialog.scope_control.set_index(0)
    pump(app, 340)
    check("切回仅当前会话：又只剩一个会话",
          {cid for cid, _mid in dialog.results()} == {win.current_conversation_id()},
          str(dialog.results()))
    win.select_conversation(seed.CHAT_CHEN)
    pump(app, 340)
    check("换会话时范围跟着走（不用重新开搜索）",
          dialog.scope_id() == seed.CHAT_CHEN and dialog.scope_mode() == "current",
          f"{dialog.scope_id()} / {dialog.scope_mode()}")
    check("结果也按新会话重算",
          {cid for cid, _mid in dialog.results()} == {seed.CHAT_CHEN},
          str(dialog.results()))
    dialog.set_query("这个词肯定搜不到")
    pump(app, 360)
    check("搜不到时提示可以切全部会话",
          dialog.row_count() == 0 and "没有包含" in dialog.hint.text()
          and "全部会话" in dialog.hint.text(), dialog.hint.text())
    dialog.set_query("")
    pump(app, 300)
    check("清空关键词回到引导态（且提示当前会话）",
          dialog.row_count() == 0 and "输入关键词" in dialog.hint.text()
          and seed.CHEN.name in dialog.hint.text(), dialog.hint.text())
    dialog.close()
    pump(app, 220)

    # 两个入口：聊天头部的放大镜 / 菜单里的"在所有会话中搜索"
    win.chat_view.search_btn.click()
    pump(app, 320)
    check("聊天头部放大镜开搜索，同样收窄到当前会话",
          dialog.isVisible() and dialog.scope_id() == seed.CHAT_CHEN
          and dialog.scope_mode() == "current",
          f"{dialog.scope_id()} / {dialog.scope_mode()}")
    win.open_search_all()
    pump(app, 300)
    check("「在所有会话中搜索」直接用全部会话范围",
          dialog.scope_mode() == "all" and dialog.scope_id() == "")
    dialog.set_query("评审")
    pump(app, 360)
    check("该入口下能跨会话检索",
          len({cid for cid, _mid in dialog.results()}) >= 2, str(dialog.results()))
    dialog.set_query("")
    dialog.close()
    pump(app, 220)

    # --- 表情回应 ---------------------------------------------------------
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 220)
    reacted = win.store.get(seed.CHAT_LIN).last_message.id
    check("先没有回应", win.reactions(reacted) == {})
    win.toggle_reaction(reacted, "👍")
    pump(app, 200)
    bubble = win.chat_view.message_list.bubbles()[reacted]
    check("回应挂到了气泡上", bubble.chip("👍") is not None and bubble.has_reactions())
    check("自己的回应有高亮标记",
          bubble.chip("👍").property("mine") == "true",
          str(bubble.chip("👍").property("mine")))
    check("回应落库", win.reactions(reacted) == {"👍": 1}, str(win.reactions(reacted)))
    win.toggle_reaction(reacted, "👍")
    pump(app, 200)
    check("再点一次撤掉（库和界面都干净）",
          win.reactions(reacted) == {} and not bubble.has_reactions())

    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 220)
    win.select_conversation(seed.CHAT_LIN)
    win.toggle_reaction(reacted, "❤️")
    pump(app, 200)
    win.select_conversation(seed.CHAT_GROUP)
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 260)
    check("切回来回应还在（从库里读回）",
          win.chat_view.message_list.bubbles()[reacted].chip("❤️") is not None)

    # 回应模式：从面板选的表情应该变成回应，而不是插进输入框
    before_input = win.chat_view.input_text()
    win.chat_view.begin_reaction(reacted)
    pump(app, 300)
    check("进入回应模式（面板开着、目标记下了）",
          win.chat_view.reaction_mode_target() == reacted
          and win.chat_view.emoji_panel_open())
    win.chat_view.emoji_picker.select_category("faces")
    pump(app, 240)
    win.chat_view.emoji_picker.cell_at("😀").click()
    pump(app, 240)
    check("选的表情加到了那条消息上",
          win.reactions(reacted).get("😀") == 1, str(win.reactions(reacted)))
    check("回应模式下不动输入框", win.chat_view.input_text() == before_input,
          win.chat_view.input_text())
    win.chat_view.hide_emoji_panel()
    pump(app, 260)
    check("关掉面板就退出回应模式",
          win.chat_view.reaction_mode_target() is None)

    # --- 删除消息 / 清空历史（都要二次确认）--------------------------------
    doomed = win.store.get(seed.CHAT_LIN).last_message
    win.store.append(Message(id="out-doomed", conversation_id=seed.CHAT_LIN,
                             sender_id="me", text="这条待会儿删掉", ts=time.time(),
                             outgoing=True))
    pump(app, 240)
    answers["confirm"] = False
    check("确认弹窗点了取消：什么都不动",
          win.delete_message("out-doomed") is False
          and win.chat_view.message_list.has_message("out-doomed")
          and win.log.count(seed.CHAT_LIN) >= 1)
    answers["confirm"] = True
    check("确认后删掉（气泡 + 库 + 内存三处）",
          win.delete_message("out-doomed") is True
          and not win.chat_view.message_list.has_message("out-doomed")
          and not any(m.id == "out-doomed" for m in win.store.get(seed.CHAT_LIN).messages)
          and all(m.id != "out-doomed" for m in win.log.load(seed.CHAT_LIN)))
    check("删完提示", "删除" in win.status_text(), win.status_text())

    # --- 侧栏右键菜单：置顶 / 免打扰 / 标记未读 / 清空这个会话 --------------
    def conversation_menu(conversation_id: str):
        """拿到右键菜单对象（exec() 会阻塞，测试直接点名动作再 trigger()）。"""
        menu = win._build_conversation_menu(conversation_id)
        return menu, {action.text(): action for action in menu.actions()}

    menu, actions = conversation_menu(seed.CHAT_CHEN)
    check("右键菜单有四个动作（外加一条灰掉的会话名）",
          {"置顶会话", "免打扰", "标记为未读", "⚠ 清空这个会话的历史…"} <= set(actions),
          " / ".join(actions))
    check("菜单顶部标出是对哪个会话操作",
          any(action.text() == seed.CHEN.name and not action.isEnabled()
              for action in menu.actions()))

    # 置顶：状态 + 落库 + 列表顺序（置顶的排最前）
    check("点「置顶会话」能把会话置顶",
          actions["置顶会话"].trigger() is None and win.store.get(seed.CHAT_CHEN).pinned)
    check("置顶状态落库了", "pinned" in win.log.flags().get(seed.CHAT_CHEN, set()))
    pump(app, 240)
    # 演示数据里王尼玛本来就是置顶的，所以"排最前"要按置顶组来断言
    check("置顶后排进最前面的置顶组",
          set(list(win.sidebar._items)[:2]) == {seed.CHAT_LIN, seed.CHAT_CHEN},
          str(list(win.sidebar._items)))
    check("列表里显示了置顶标记",
          "📌" in win.sidebar._items[seed.CHAT_CHEN].mark_label.text())
    menu, actions = conversation_menu(seed.CHAT_CHEN)
    check("再点一次是「取消置顶」", "取消置顶" in actions)
    actions["取消置顶"].trigger()
    check("取消置顶后状态与顺序都回去",
          not win.store.get(seed.CHAT_CHEN).pinned
          and "pinned" not in win.log.flags().get(seed.CHAT_CHEN, set()))

    # 免打扰：徽标变灰 + 不计进顶部总数 + 不再弹跳
    win.store.set_unread(seed.CHAT_CHEN, 3)
    pump(app, 240)
    total_before = win.store.total_unread()
    check("标记未读后总数里算它", total_before >= 3)
    menu, actions = conversation_menu(seed.CHAT_CHEN)
    actions["免打扰"].trigger()
    pump(app, 240)
    item = win.sidebar._items[seed.CHAT_CHEN]
    check("免打扰落库 + 行上显示 🔕",
          win.store.get(seed.CHAT_CHEN).muted
          and "muted" in win.log.flags().get(seed.CHAT_CHEN, set())
          and "🔕" in item.mark_label.text())
    check("免打扰的徽标走灰色样式", item.badge.property("muted") == "true")
    check("免打扰的未读不进顶部总数",
          win.store.total_unread() == total_before - 3, str(win.store.total_unread()))
    win.store.set_unread(seed.CHAT_CHEN, 5)             # 又来了两条
    pump(app, 240)
    check("免打扰的会话不再弹跳（别抢注意力）",
          not item.badge.is_popping() and abs(item.badge.scale - 1.0) < 1e-6)
    menu, actions = conversation_menu(seed.CHAT_CHEN)
    actions["取消免打扰"].trigger()
    pump(app, 240)
    check("取消免打扰后恢复计数",
          not win.store.get(seed.CHAT_CHEN).muted
          and win.store.unread(seed.CHAT_CHEN) == 5)

    # 标记为未读：徽标立刻出来，再点开就消
    win.store.mark_read(seed.CHAT_CHEN)
    pump(app, 200)
    menu, actions = conversation_menu(seed.CHAT_CHEN)
    check("已读状态下「标记为未读」可点", actions["标记为未读"].isEnabled())
    actions["标记为未读"].trigger()
    pump(app, 240)
    check("标记后徽标出现", win.store.unread(seed.CHAT_CHEN) == 1)
    menu, actions = conversation_menu(seed.CHAT_CHEN)
    check("已有未读时「标记为未读」置灰", not actions["标记为未读"].isEnabled())
    win.select_conversation(seed.CHAT_CHEN)
    pump(app, 240)
    check("点开这个会话就消掉未读", win.store.unread(seed.CHAT_CHEN) == 0)

    # 右键事件链路：那一行真的会把"哪个会话"抛给主窗口。
    # 注意先把主窗口的处理器摘掉 —— 它会 menu.exec() 真弹菜单，在测试里会一直等下去。
    win.sidebar.conversationMenuRequested.disconnect(win._show_conversation_menu)
    right_clicked = []
    win.sidebar.conversationMenuRequested.connect(
        lambda cid, pos: right_clicked.append(cid))
    item = win.sidebar._items[seed.CHAT_CHEN]
    QApplication.sendEvent(item, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(300, 300)))
    check("右键会话那行会请求弹菜单，并带上会话 id",
          right_clicked == [seed.CHAT_CHEN], str(right_clicked))
    win.sidebar.conversationMenuRequested.disconnect()
    win.sidebar.conversationMenuRequested.connect(win._show_conversation_menu)

    # 旧的两个入口要真的没了（标为已读已删、清空历史挪进会话右键菜单）
    more_labels = [action.text() for action in win._build_menu().actions()]
    check("主菜单里没有「标记当前会话为已读」了",
          not any("标记当前会话为已读" in text for text in more_labels),
          " / ".join(more_labels))
    check("主菜单里没有全局「清空本地历史」了（已经按会话挪走）",
          not any("清空本地历史" in text for text in more_labels))
    check("主菜单里还留着本地历史统计与动效开关",
          any(text.startswith("本地历史：") for text in more_labels)
          and "界面动效" in more_labels)

    # 清空这个会话的历史：只动它一个 + 二次确认
    other_before = win.log.count(seed.CHAT_GROUP)
    lin_before = win.log.count(seed.CHAT_LIN)
    answers["confirm"] = False
    check("清空历史的取消分支：数据一条不少",
          win.clear_conversation_history(seed.CHAT_LIN) is False
          and win.log.count(seed.CHAT_LIN) == lin_before)
    answers["confirm"] = True
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 240)
    check("清空「王尼玛」这个会话",
          win.clear_conversation_history(seed.CHAT_LIN) is True
          and win.store.get(seed.CHAT_LIN).messages == []           # 内存里空了
          and win.log.count(seed.CHAT_LIN) == 0)                    # 库里也空了
    pump(app, 300)
    check("其它会话一条都没少",
          win.log.count(seed.CHAT_GROUP) == other_before
          and len(win.store.get(seed.CHAT_GROUP).messages) >= 1,
          f"{win.log.count(seed.CHAT_GROUP)} / {other_before}")
    check("清空后界面上一眼看去是空的（气泡没了）",
          win.chat_view.sent_bubbles() == 0, str(win.chat_view.sent_bubbles()))
    check("记下了 cleared 旗标（下次启动别让演示数据复活）",
          "cleared" in win.log.flags().get(seed.CHAT_LIN, set()))
    check("清空提示带会话名", seed.LIN.name in win.status_text(), win.status_text())
    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 240)

    # --- 工具箱按钮 -------------------------------------------------------
    # --- 动效：入场 / 打字点 / 主题交叉淡入 / 徽标回弹 / 结果错峰 -----------
    from mchat.ui import anim as anim_module

    check("动效默认开启", anim_module.is_enabled())
    before_bubbles = win.chat_view.sent_bubbles()
    win.chat_view.composer.edit.insert_text("看这条消息怎么冒出来")
    win.chat_view.composer.submit()
    fresh = win.store.get(win.current_conversation_id()).last_message
    fresh_bubble = win.chat_view.message_list.bubbles()[fresh.id]
    # 入场动效 = 只淡入 + 滚动滑入：几何一上来就是终态，全程不动
    check("新消息入场是淡入（透明度效果已挂上）", fresh_bubble.is_animating())
    check("气泡一上来就占最终高度（不再从 0 长出来）",
          fresh_bubble.maximumHeight() == anim_module.UNLIMITED
          and fresh_bubble.height() > 0,
          f"上限={fresh_bubble.maximumHeight()} 高={fresh_bubble.height()}")
    geometry = []
    for _ in range(10):                       # 166ms 内采样：高度/位置必须稳
        pump(app, 16)
        geometry.append(_bubble_geometry(win, fresh.id))
    # 抖动回归：旧实现动高度，气泡在行里被反复居中，y 会 628↔619 来回跳；
    # 现在是"高度恒定 + y 单调"，所以按单调性断言（缓动前段步子大是正常的）
    check("入场期间几何不抖（高度恒定、y 不反跳）",
          len({h for h, _ in geometry}) == 1
          and all(geometry[i + 1][1] <= geometry[i][1]
                  for i in range(len(geometry) - 1)),
          f"高度={[h for h, _ in geometry]} y={[y for _, y in geometry]}")
    check("淡入结束：透明效果已摘掉",
          wait_for(app, lambda: not fresh_bubble.is_animating()
                   and fresh_bubble.graphicsEffect() is None),
          f"效果={fresh_bubble.graphicsEffect()}")
    check("气泡数对上了", win.chat_view.sent_bubbles() == before_bubbles + 1,
          f"{win.chat_view.sent_bubbles()} vs {before_bubbles + 1}")

    other_theme = THEME_DARK if win.current_palette().key == "light" else THEME_LIGHT
    win.set_theme(other_theme)
    pump(app, 60)
    overlay = win.theme_overlay()
    check("切主题时盖了一张旧快照在淡出",
          overlay is not None and overlay.is_fading(),
          f"叠层={overlay}")
    check("淡完就把快照收拾干净（不留顶层窗口）",
          wait_for(app, lambda: win.theme_overlay() is None))
    check("主题确实换了", win.current_palette().key == (
        "dark" if other_theme == THEME_DARK else "light"), win.current_palette().key)

    anim_dialog = win.open_search()
    anim_dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    anim_dialog.set_query("评审")
    anim_dialog.run_search()
    pump(app, 60)
    check("结果行是错峰淡入（这一刻有的还在淡）",
          not anim_dialog._rows
          or any(row.graphicsEffect() is not None for row in anim_dialog._rows),
          str([row.graphicsEffect() is not None for row in anim_dialog._rows]))
    check("淡完摘掉效果（稳态零开销）",
          wait_for(app, lambda: all(row.graphicsEffect() is None
                                    for row in anim_dialog._rows)))
    anim_dialog.close()
    pump(app, 200)

    badge_item = win.sidebar._items[seed.CHAT_CHEN]
    badge_item.refresh(win.store.get(seed.CHAT_CHEN), 1)
    pump(app, 80)
    badge_item.refresh(win.store.get(seed.CHAT_CHEN), 3)      # 变多了 -> 该弹一下
    check("未读变多时徽标回弹（scale 离开 1.0）",
          badge_item.badge.is_popping() and badge_item.badge.scale < 1.0,
          f"弹={badge_item.badge.is_popping()} scale={badge_item.badge.scale}")
    check("弹完停在 1.0",
          wait_for(app, lambda: not badge_item.badge.is_popping()
                   and abs(badge_item.badge.scale - 1.0) < 1e-6),
          str(badge_item.badge.scale))
    badge_item.refresh(win.store.get(seed.CHAT_CHEN), 0)
    check("未读清零后徽标收起", not badge_item.badge.isVisible())

    # 总开关：关掉动效时所有过渡都直接落终态（界面语义不变）
    anim_module.set_enabled(False)
    try:
        win.chat_view.composer.edit.insert_text("关掉动效后的消息")
        win.chat_view.composer.submit()
        plain = win.chat_view.message_list.bubbles()[
            win.store.get(win.current_conversation_id()).last_message.id]
        check("关掉动效：新气泡直接是终态（没效果、高度不受限）",
              not plain.is_animating() and plain.graphicsEffect() is None)
        check("关掉动效：切主题也不铺快照", anim_module.cross_fade(win) is None)
        pump(app, 200)
    finally:
        anim_module.set_enabled(True)
    check("动效开关恢复了", anim_module.is_enabled())

    win.set_animations(False)
    check("菜单里的动效开关能关掉，而且记住了选择",
          not win.animations_enabled()
          and str(win._settings.value("animations")) == "false")
    win.set_animations(True)
    check("再打开也正常", win.animations_enabled()
          and str(win._settings.value("animations")) == "true")

    # --- 动效开启时"最新消息必须看得见"（三个回归，都是用户报的）-------------
    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 320)
    area = win.chat_view.message_list
    bar = area.verticalScrollBar()

    def bubble_fully_visible(message_id: str) -> bool:
        """气泡整体是否落在消息区视口里。"""
        height, y = _bubble_geometry(win, message_id)
        return 0 <= y and y + height <= area.viewport().height() + 2

    guard = 0
    while bar.maximum() < 100 and guard < 12:      # 先把列表堆到真的能滚
        win.chat_view.composer.edit.insert_text(f"堆一点消息 {guard}")
        win.chat_view.composer.submit()
        pump(app, 330)
        guard += 1
    check("消息区已经能滚（否则这条回归没意义）", bar.maximum() >= 100,
          f"max={bar.maximum()}，堆了 {guard} 条")

    win.chat_view.composer.edit.insert_text("最后这条必须看得见")
    win.chat_view.composer.submit()
    last_id = win.store.get(seed.CHAT_GROUP).last_message.id
    # 回归 1：入场动画期间高度为 0，只在结束时滚一次会停在半路（实测差一条气泡的高度）
    slide_samples = []
    for _ in range(8):
        pump(app, 16)
        slide_samples.append(_bubble_geometry(win, last_id))
    check("有滚动余量时是「滑进来」：高度稳、y 单调上移不反跳",
          len({h for h, _ in slide_samples}) == 1
          and all(slide_samples[i + 1][1] <= slide_samples[i][1]
                  for i in range(len(slide_samples) - 1)),
          f"高度={[h for h, _ in slide_samples]} y={[y for _, y in slide_samples]}")
    check("开动效时新消息入场后仍贴底且完整可见",
          wait_for(app, lambda: area.is_at_bottom() and bubble_fully_visible(last_id)),
          f"滚动 {bar.value()}/{bar.maximum()}，可见={bubble_fully_visible(last_id)}")

    # 回归 2：展开表情面板会把消息区顶上去，原来贴底的位置就错位了
    win.chat_view.set_emoji_panel_open(True)
    check("开表情面板后最新消息仍完整可见",
          wait_for(app, lambda: bubble_fully_visible(last_id) and area.is_at_bottom()),
          f"滚动 {bar.value()}/{bar.maximum()}，面板={win.chat_view.emoji_panel_open()}")

    # 回归 3：点消息区空白处应当收起面板（不吞掉点击、点面板内部不收）
    QTest.mouseClick(area.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(40, 40))
    pump(app, 320)
    check("点消息区空白处收起面板",
          not win.chat_view.emoji_panel_open()
          and not win.chat_view.composer.smile_btn.isChecked())
    check("收起面板后最新消息也还看得见",
          wait_for(app, lambda: bubble_fully_visible(last_id) and area.is_at_bottom()))
    win.chat_view.set_emoji_panel_open(True)
    pump(app, 320)
    QTest.mouseClick(win.chat_view.emoji_picker, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(200, 40))
    pump(app, 300)
    check("点面板内部不会误收面板", win.chat_view.emoji_panel_open())
    win.chat_view.hide_emoji_panel()
    pump(app, 320)

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
    # 塞一条带旗帜的消息：截图里要能看到"气泡里的旗帜是图片"（回归：曾经显示成 CN 字母）
    win.store.receive(Message(
        id="in-flag-shot", conversation_id=seed.CHAT_GROUP, sender_id="su",
        text="这周要去 🇯🇵 和 🇺🇸，顺便把 🏴‍☠️ 旗带上，别忘 🏳️‍🌈",
        ts=time.time(), outgoing=False))
    pump(app, 300)
    shoot("light_group")
    shot_bubble = win.chat_view.message_list._bubbles.get("in-flag-shot")
    check("截图里那条消息的气泡是富文本（旗帜变图了）",
          shot_bubble is not None
          and shot_bubble.text_label.textFormat() == Qt.TextFormat.RichText
          and shot_bubble.text_label.text().count("<img") == 4,
          shot_bubble.text_label.text()[:120] if shot_bubble else "没找到气泡")
    # 表情面板：亮色下展开「表情」分类，人工复核格子间距/彩色字体/分类条
    win.chat_view.set_emoji_panel_open(True)
    win.chat_view.emoji_picker.select_category("faces")
    # 输入框里也放一面旗：截图里要能看到"输入框里也是图片"
    win.chat_view.composer.edit.set_message_text("")      # 先清掉上一段流程留下的草稿
    win.chat_view.composer.edit.insert_text("要去 🇯🇵 吗")
    pump(app, 420)
    shoot("light_emoji")
    check("截图里输入框的旗帜也是图片（文档里有图片对象）",
          _edit_image_count(win.chat_view.composer.edit) == 1
          and win.chat_view.input_text() == "要去 🇯🇵 吗",
          win.chat_view.input_text())
    win.chat_view.composer.edit.set_message_text("")
    win.chat_view.hide_emoji_panel()
    pump(app, 320)

    win.set_theme(THEME_DARK)
    pump(app, 400)
    check("主题切到暗色", win.current_palette().key == "dark", win.current_palette().key)
    check("工具箱窗口跟着换主题", "#17212B" in tool.styleSheet() or
          "#1E2A38" in tool.styleSheet())
    win.select_conversation(seed.CHAT_BOT)
    # 给机器人那条挂个回应：截图里要能看见气泡下方的胶囊
    win.toggle_reaction(win.store.get(seed.CHAT_BOT).last_message.id, "👍")
    pump(app, 320)
    shoot("dark_bot")

    # 搜索窗口：截图（暗色）留给人工复核范围控件/结果行/片段
    search = win.open_search()                    # 默认就是"仅当前会话"
    search.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    search.set_query("聊天")
    pump(app, 420)
    check("搜索窗口按当前会话出结果",
          search.scope_id() == seed.CHAT_BOT and search.row_count() >= 1,
          f"{search.scope_id()} / {search.row_count()}")
    path = os.path.join(ARTIFACTS, "chat_search_dark.png")
    search.grab().save(path)
    shots.append(path)
    search.close()
    pump(app, 200)

    # 暗色下再留一张搜索结果态（顺带复核暗色里的格子底色与提示文案）
    win.chat_view.set_emoji_panel_open(True)
    win.chat_view.emoji_picker.set_search_text("爱")
    pump(app, 420)
    shoot("dark_emoji_search")
    win.chat_view.emoji_picker.set_search_text("")
    # 旗帜分类也留一张：人工复核"每个格子是不是切对了旗子"
    win.chat_view.emoji_picker.select_category("flags")
    pump(app, 420)
    shoot("dark_emoji_flags")
    win.chat_view.hide_emoji_panel()
    pump(app, 320)

    # 分界线：拉宽 + 悬停态留一张图，人工复核竖线高亮与侧栏宽度
    win.set_sidebar_width(392, persist=False)
    QApplication.sendEvent(win.split_handle, QEvent(QEvent.Type.Enter))
    pump(app, 300)
    shoot("dark_sidebar_wide_hover")
    QApplication.sendEvent(win.split_handle, QEvent(QEvent.Type.Leave))
    win.reset_sidebar_width()

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


def _hover_line_color(win, app) -> str:
    """把鼠标"移进"分界线，返回那一刻竖线的实际像素色（随后移出）。

    验的是"真的画出来了"而不是状态标志位：只看 ``is_hovered()``
    的话，就算 paintEvent 根本没接上颜色也是绿的。
    """
    handle = win.split_handle
    QApplication.sendEvent(handle, QEvent(QEvent.Type.Enter))
    pump(app, 180)
    color = widget_pixel(win, handle, 3, handle.height() // 2).name().lower()
    QApplication.sendEvent(handle, QEvent(QEvent.Type.Leave))
    pump(app, 180)
    return color


def _drag_visual(win) -> bool:
    """按住不放：竖线变主题色、两侧铺浅色底；松手后应当恢复默认绘制。"""
    handle = win.split_handle
    palette = win.current_palette()
    mid_y = handle.height() // 2
    press_handle(handle)
    dragging = handle.is_dragging()
    line = widget_pixel(win, handle, 3, mid_y).name().lower()
    gutter = widget_pixel(win, handle, 0, mid_y).name().lower()
    release_handle(handle)
    return (dragging
            and line == QColor(palette.accent).name().lower()
            and gutter == QColor(palette.accent_soft).name().lower())


def _restarted_window(settings, app):
    """另开一个窗口（等价于重启程序）。调用方读完数据记得用 `_close_window` 收尾。"""
    demo = seed.build_demo()
    other = MainWindow(demo=demo, transport=LocalTransport(demo.scripts),
                       settings=settings, log=ChatLog(":memory:"),
                       confirm=lambda **kw: True)
    other.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    other.show()
    pump(app, 300)
    return other


def _close_window(win, app) -> None:
    """收尾必须关窗：留着可见窗口会让脚本退出时挂住。"""
    win.close()
    win.deleteLater()
    app.processEvents()


def _bubble_geometry(win, message_id: str) -> tuple[int, int]:
    """气泡的 ``(高, 在消息区视口里的 y)`` —— 断言"入场时不抖"用。

    注意要用 ``mapTo`` 换算到视口坐标系：``bubble.y()`` 是它在自己那一行里的相对
    坐标（恒为 0），拿它做抖动断言等于什么都没断言。
    """
    area = win.chat_view.message_list
    bubble = area.bubbles().get(message_id)
    if bubble is None:
        return 0, 0
    return bubble.height(), bubble.mapTo(area.viewport(), QPoint(0, 0)).y()


def _fresh_window(settings, app, log):
    """另开一个窗口，用指定的本地库（测"重启"时两个窗口共用同一个文件库）。"""
    demo = seed.build_demo()
    win = MainWindow(demo=demo, transport=LocalTransport(demo.scripts),
                     settings=settings, log=log, confirm=lambda **kw: True)
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    pump(app, 320)
    return win


def _restarted_width(settings, app) -> int:
    """重启后恢复出来的会话列表宽度。"""
    other = _restarted_window(settings, app)
    width = other.sidebar.width()
    _close_window(other, app)
    return width


def _restarted_emoji_recents(settings, app) -> list[str]:
    """重启后恢复出来的"最近使用"表情。"""
    other = _restarted_window(settings, app)
    recents = other.chat_view.emoji_recents()
    _close_window(other, app)
    return recents


def _edit_image_count(editor) -> int:
    """输入框文档里有几个图片（验旗帜真的以图片形式嵌进去了）。

    判据用 ``U+FFFC``（Qt 表示内嵌对象的方式），**不能只看 isImageFormat()**：
    图片后面插入的文字会继承图片格式，把自己也报成"图片"。
    """
    count = 0
    block = editor.document().begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid() and fragment.text() == "\ufffc":
                count += 1
            iterator += 1
        block = block.next()
    return count


def _row_columns(picker) -> int:
    """网格第一行实际摆了几个格子（= 自动算出来的列数）。"""
    if not picker._cells:
        return 0
    top = picker._cells[0].y()
    return len([btn for btn in picker._cells if btn.y() == top])


def _grid_fills_width(picker) -> tuple[bool, str]:
    """格子有没有把面板宽度排满：不溢出，且右边剩下的空白小于一格（除非已经到列数上限）。"""
    if not picker._cells:
        return False, "网格里没有格子"
    top = picker._cells[0].y()
    row = [btn for btn in picker._cells if btn.y() == top]
    used = row[-1].x() + row[-1].width()
    step = picker.CELL + 2
    usable = picker.width() - 24                      # 面板左右各 12px 内边距
    return (used <= usable
            and (len(row) >= picker.MAX_COLUMNS or used + step > usable),
            f"每行 {len(row)} 个，用了 {used}px / 可用 {usable}px")


def _escape_closes_panel(win, app) -> bool:
    """Esc 的完整行为：面板开着就收面板，按钮状态跟着回弹。

    这里直接调主窗口接的那个槽，而不是合成按键事件 —— Qt 的 QShortcut 只吃
    来自窗口系统的按键，``sendEvent`` 出来的 QKeyEvent 触发不了它，
    合成出来的"绿灯"没有意义。
    """
    win.chat_view.set_emoji_panel_open(True)
    pump(app, 320)
    opened = win.chat_view.emoji_panel_open()
    win._on_escape()
    pump(app, 420)
    return (opened
            and not win.chat_view.emoji_panel_open()
            and not win.chat_view.composer.smile_btn.isChecked())


def test_ui_suite() -> None:
    """让 ``pytest tests`` 也能跑这套脚本式用例（本地直接运行 main() 同样可行）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
