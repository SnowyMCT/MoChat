"""聊天界面端到端测试（不弹窗、不抢焦点，走真实字体渲染）。

用法：``python tests/test_ui.py``
artifacts/ 下会留一批截图供人工复核：亮/暗主题的聊天窗口、亮/暗的附件（图片消息 +
文件卡片）、亮/暗的设置区域、表情面板（展开态 + 搜索态 + 旗帜）、分界线高亮态，
另有工具箱与搜索窗口。

覆盖：侧栏会话列表与未读徽标、搜索过滤、切换会话、发消息与自动回复、
"正在输入…"、已读回执、未读规则、工具箱按钮与预填、主题切换、空消息拦截、
分界线拖动改变会话列表宽度（含夹取/双击还原/持久化/竖线渲染）、
表情面板（分类/搜索/插入/最近使用/持久化/彩色渲染）、
图片与文件附件（演示数据渲染、选文件→待发送条→发送、纯附件消息、切会话不串附件、
✕ 撤回、剪贴板粘贴图片落地、打开文件/文件夹、文件丢失后的降级、按附件名检索、换主题重画图标）、
设置区域（入场/错峰/hover 渐变动画、动效开关与"关掉后直接落终态"、文字大小全局缩放与
悬浮标签跟随、预览字号过渡、数据卡片、三项设置重启后仍在）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import (QEvent, QMimeData, QPoint, QRect, QSettings,   # noqa: E402
                          Qt)
from PyQt6.QtGui import (QColor, QContextMenuEvent, QFontMetrics,       # noqa: E402
                         QImage, QPalette, QPixmap, QShortcut, QTextCursor)
from PyQt6.QtTest import QTest                                          # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QLabel                           # noqa: E402

from mchat.core import assets as core_assets                            # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed                                                             # noqa: E402
from mchat.core.db import ChatLog                                       # noqa: E402
from mchat.core.models import Attachment, MergedItem, Message, PinRef       # noqa: E402
from mchat.core.transport import LocalTransport                         # noqa: E402
from mchat.ui import emoji, flags as flags_data                         # noqa: E402
from mchat.ui import main_window as main_window_module                  # noqa: E402
from mchat.ui import theme as theme_module                              # noqa: E402
from mchat.ui.icons import make_icon                                    # noqa: E402
from mchat.ui.forward_dialog import FORWARD_HEIGHT, FORWARD_WIDTH      # noqa: E402
from mchat.ui.main_window import MainWindow                             # noqa: E402
from mchat.ui.pin_dialog import PinDialog                                # noqa: E402
from mchat.ui.sidebar import SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH       # noqa: E402
from mchat.ui.widgets import CHROME_H, ToggleSwitch                    # noqa: E402
from mchat.ui.theme import (THEME_DARK, THEME_LIGHT,                    # noqa: E402
                            THEME_OPTIONS)
from ui_probe import (double_click_handle, drag_split_handle,           # noqa: E402
                      colorful_pixels, glyph_pixels, press_handle, pump,
                      release_handle, sweep_select, wait_for, widget_pixel)
from mchat.ui.chat_view import SWEEP_SLOP                                # noqa: E402

ARTIFACTS = os.path.join(ROOT, "artifacts")


def visible_bubbles(listw) -> list[int]:
    """当前视口里"完整看得见"的气泡下标（划选只划得到看得见的那些）。

    消息一多，列表就停在底部：前面几条被滚到视口外面去了（``mapTo`` 出来是负的 y），
    拿它们当划选的两端点会划到一个空矩形上 —— 测试里必须先挑看得见的那几条。
    """
    viewport = listw.viewport()
    usable = viewport.rect().adjusted(0, 2, 0, -2)
    found = []
    for index, bubble in enumerate(listw.bubbles().values()):
        rect = QRect(bubble.mapTo(viewport, QPoint(0, 0)), bubble.size())
        if usable.contains(rect):
            found.append(index)
    return found


def sweep_span(listw, first_index: int, last_index: int) -> tuple[QPoint, QPoint]:
    """取"从第 first 条气泡划到最后一条"两个点（都用视口坐标系）。

    起点取气泡内的空白处（靠左 6px，别落在文字上 —— 落在文字上就会开始选文字）；
    终点取最后那条气泡的底部再往里收 3px，这样"刚好划到它"而不是"划过它"。
    两个点都夹在视口里：划出消息区的部分不算数（没有自动滚动）。
    """
    viewport = listw.viewport()
    ids = list(listw.bubbles().keys())
    first = listw.bubbles()[ids[first_index]]
    last = listw.bubbles()[ids[last_index]]
    start = first.mapTo(viewport, QPoint(6, first.height() // 2))
    bottom = last.mapTo(viewport, QPoint(0, 0)).y() + last.height() - 3
    end = QPoint(viewport.width() - 3, max(4, min(bottom, viewport.height() - 4)))
    return start, end


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
    answers = {"confirm": True, "pick": "", "kinds": []}

    def fake_pick(kind: str, parent=None) -> str:
        """替掉 QFileDialog：模态对话框在无头测试里会一直等下去。"""
        answers["kinds"].append(kind)
        return answers["pick"]

    clipboard = FakeClipboard()          # 复制只写它，别动用户真剪贴板
    win = MainWindow(demo=demo, transport=transport, settings=settings, log=log,
                     clipboard=clipboard,
                     confirm=lambda **kw: answers["confirm"], filedialog=fake_pick)
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
    # 别拿"睡了 460ms 就该展开完"当判据：机器忙的时候动画尾巴会拖过这个时长，
    # 实测偶发读到中间高度（118）。改成等它落定，断言强度不变。
    check("展开后占满面板高度",
          wait_for(app, lambda: picker.height() == picker.PANEL_HEIGHT),
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
    # 分界线宽度是动画，格子重排又是布局事件驱动的：别用"睡 320ms 后取一次列数"当判据
    # （机器忙的时候会读到上一轮的列数，实测偶发 19 vs 23），等它落定再断言。
    check("面板变窄后自动重排（列数变少）",
          wait_for(app, lambda: _row_columns(picker) < columns_wide),
          f"{columns_wide} -> {_row_columns(picker)}")
    check("重排后仍是排满的", _grid_fills_width(picker)[0], "")
    win.reset_sidebar_width()
    pump(app, 320)
    check("恢复宽度后列数也回来",
          wait_for(app, lambda: _row_columns(picker) == columns_wide),
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
          not win.chat_view.emoji_panel_open()
          and wait_for(app, lambda: picker.height() == 0),
          f"open={win.chat_view.emoji_panel_open()} h={picker.height()}")
    check("收起后消息区高度复原",
          wait_for(app, lambda: win.chat_view.message_list.height() == message_height),
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

    # 别人发的也能删：这是**本地删除**，不是撤回（对方设备上仍然看得到）
    received = next(m for m in win.store.get(seed.CHAT_LIN).messages if not m.outgoing)
    answers["confirm"] = True
    check("收到的消息也能删（本地删除，不是撤回）",
          win.delete_message(received.id) is True
          and not win.chat_view.message_list.has_message(received.id)
          and win.store.message(received.id) is None
          and all(m.id != received.id for m in win.log.load(seed.CHAT_LIN)))

    # --- 图片 / 文件附件 ---------------------------------------------------
    # 演示数据里就有两条附件消息：王尼玛那条带一个 PDF、尼玛一家那条带一张图
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 260)
    seeded_file = next((m for m in win.store.get(seed.CHAT_LIN).messages
                        if m.has_attachment), None)
    check("演示数据里带了一条文件消息（PDF）",
          seeded_file is not None and seeded_file.kind == "file"
          and seeded_file.attachment.name == core_assets.DEMO_REPORT,
          str(seeded_file.attachment if seeded_file else None))
    seeded_bubble = win.chat_view.message_list.bubbles().get(
        seeded_file.id) if seeded_file else None
    check("收到的文件在气泡里画成文件卡片（不是图片）",
          seeded_bubble is not None and seeded_bubble.file_card is not None
          and seeded_bubble.image_view is None)
    check("卡片上有完整文件名与「类型 · 大小」",
          seeded_bubble is not None
          and seeded_bubble.file_card.name_text() == core_assets.DEMO_REPORT
          and "PDF 文件" in seeded_bubble.file_card.meta_text()
          and "B" in seeded_bubble.file_card.meta_text(),
          seeded_bubble.file_card.meta_text() if seeded_bubble else "")
    check("卡片 tooltip 给出完整路径",
          seeded_bubble is not None
          and seeded_file.attachment.path in seeded_bubble.file_card.toolTip())

    win.select_conversation(seed.CHAT_DESIGN)
    pump(app, 300)
    seeded_photo = next((m for m in win.store.get(seed.CHAT_DESIGN).messages
                         if m.has_attachment), None)
    photo_bubble = win.chat_view.message_list.bubbles().get(
        seeded_photo.id) if seeded_photo else None
    check("演示数据里的图片消息画成缩略图（不是文件卡片）",
          photo_bubble is not None and photo_bubble.image_view is not None
          and photo_bubble.file_card is None and photo_bubble.image_view.has_image())
    original = QPixmap(core_assets.demo_path(core_assets.DEMO_PHOTO)).size()
    drawn = photo_bubble.image_view.preview_size() if photo_bubble else None
    check("缩略图等比缩放、不超上限",
          drawn is not None and 0 < drawn.width() <= 300 and 0 < drawn.height() <= 300
          and abs(drawn.width() / max(1, drawn.height())
                  - original.width() / max(1, original.height())) < 0.05,
          f"{drawn.width()}x{drawn.height()} vs 原图 {original.width()}x{original.height()}"
          if drawn else "没有缩略图")
    check("图片气泡里也能带配文",
          photo_bubble is not None and photo_bubble.text() != ""
          and "主视觉" in photo_bubble.text(),
          photo_bubble.text() if photo_bubble else "")

    # 选文件：对话框由 fake_pick 顶替（模态窗口在无头测试里会卡住）
    temp_dir = tempfile.mkdtemp(prefix="mchat-attach-")
    picked_image = os.path.join(temp_dir, "临时截图.png")
    shutil.copyfile(core_assets.demo_path(core_assets.DEMO_PHOTO), picked_image)
    tray = win.chat_view.attachment_tray()

    answers["pick"] = ""
    check("取消选择：什么都不挂",
          win.attach_file("image") is None and not win.chat_view.has_pending_attachment()
          and not tray.isVisible())
    answers["pick"] = os.path.join(temp_dir, "根本没有这个文件.png")
    check("选到不存在的路径：当场挡下并提示",
          win.attach_file("image") is None and "找不到" in win.status_text(),
          win.status_text())

    answers["pick"] = picked_image
    win.select_conversation(seed.CHAT_LIN)          # 发到王尼玛那个会话里
    pump(app, 260)
    win.chat_view.attachRequested.emit("image")          # 等价于点回形针菜单里的「图片…」
    pump(app, 220)
    check("选完图片：待发送条出现，名字/类型/大小都在",
          win.chat_view.has_pending_attachment() and tray.isVisible()
          and "临时截图.png" in tray.name_label.text()
          and "PNG 图片" in tray.meta_label.text() and "KB" in tray.meta_label.text(),
          f"{tray.name_label.text()} / {tray.meta_label.text()}")
    check("待发送条里有缩略图（不是空框）", not tray.thumb.pixmap().isNull())
    check("附件请求按「图片」类型发给选择器", answers["kinds"][-1] == "image",
          str(answers["kinds"]))

    # 附件和草稿一样是"这个会话里正在组织的一条消息"：换个会话必须是干净的
    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 240)
    check("切到别的会话：待发送的附件不会跟过来（免得发错人）",
          not win.chat_view.has_pending_attachment() and not tray.isVisible())
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 240)
    check("切回来附件还在",
          win.chat_view.pending_attachment() == Attachment.from_path(picked_image),
          str(win.chat_view.pending_attachment()))

    win.chat_view.composer.edit.insert_text("这是新的主视觉")
    win.chat_view.composer.submit()
    pump(app, 320)
    sent_image = win.store.get(seed.CHAT_LIN).last_message
    check("带配文的图片发出去了",
          sent_image.kind == "image" and sent_image.outgoing
          and sent_image.text == "这是新的主视觉"
          and sent_image.attachment.name == "临时截图.png",
          f"{sent_image.kind} / {sent_image.text}")
    sent_bubble = win.chat_view.message_list.bubbles()[sent_image.id]
    check("自己的图片气泡里有缩略图 + 配文",
          sent_bubble.image_view is not None and sent_bubble.image_view.has_image()
          and sent_bubble.text() == "这是新的主视觉")
    check("发完待发送条收起来了",
          not win.chat_view.has_pending_attachment() and not tray.isVisible())
    check("附件随消息一起落库",
          [m for m in win.log.load(seed.CHAT_LIN) if m.id == sent_image.id][0]
          .attachment.name == "临时截图.png")
    check("会话摘要显示配文",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text().startswith("我: 这是新的主视觉"),
          win.sidebar._items[seed.CHAT_LIN].preview_label.text())

    # 纯附件（不写配文）
    picked_pdf = os.path.join(temp_dir, "季度复盘报告.pdf")
    with open(picked_pdf, "wb") as handle:
        handle.write(b"p" * 4096)
    answers["pick"] = picked_pdf
    win.attach_file("file")
    win.chat_view.composer.submit()                       # 空文字也要能发出去
    pump(app, 320)
    sent_file = win.store.get(seed.CHAT_LIN).last_message
    check("没有配文、只有附件也能发",
          sent_file.kind == "file" and sent_file.text == ""
          and sent_file.attachment.name == "季度复盘报告.pdf")
    check("纯附件消息的摘要写成「我: 📎 文件名」",
          win.sidebar._items[seed.CHAT_LIN].preview_label.text()
          .startswith("我: 📎 季度复盘报告.pdf"),
          win.sidebar._items[seed.CHAT_LIN].preview_label.text())
    file_bubble = win.chat_view.message_list.bubbles()[sent_file.id]
    check("发出的文件也是卡片，大小按磁盘算",
          file_bubble.file_card is not None and "4 KB" in file_bubble.file_card.meta_text(),
          file_bubble.file_card.meta_text() if file_bubble.file_card else "")
    hits = win.log.search("季度复盘报告")
    check("附件文件名也进了检索（搜文件名能找到这条消息）",
          [h.message_id for h in hits] == [sent_file.id], str([h.message_id for h in hits]))

    # ✕ 撤回待发送的附件
    answers["pick"] = picked_image
    win.attach_file("image")
    tray.remove_btn.click()
    pump(app, 220)
    check("点 ✕ 撤掉待发送的附件",
          not win.chat_view.has_pending_attachment() and not tray.isVisible())
    win.chat_view.composer.submit()
    pump(app, 220)
    check("撤掉附件后又什么都没有：空消息照样被拦",
          "空消息" in win.status_text(), win.status_text())

    # 剪贴板里的图片（截图工具复制出来的那种）
    mime = QMimeData()
    mime.setImageData(QImage(core_assets.demo_path(core_assets.DEMO_PHOTO)))
    win.chat_view.composer.edit.insertFromMimeData(mime)   # 走真实粘贴那条路
    pump(app, 260)
    pasted = win.chat_view.pending_attachment()
    check("粘贴图片 -> 变成待发送附件（不是塞进输入框里）",
          pasted is not None and pasted.kind == "image"
          and "粘贴的图片" in pasted.name, str(pasted))
    check("粘贴的图片先落地成 PNG 文件",
          pasted is not None and pasted.path.endswith(".png") and os.path.isfile(pasted.path),
          pasted.path if pasted else "")
    if pasted is not None:
        os.remove(pasted.path)                 # 别在系统临时目录里留垃圾
    win.chat_view.clear_attachment()

    # 打开附件 / 打开所在文件夹（把"用系统默认程序打开"这一步换成记录器，
    # 否则测试会真的弹出资源管理器和看图器）
    opened: list[str] = []

    class _FakeDesktopServices:
        @staticmethod
        def openUrl(url) -> None:              # noqa: N802
            opened.append(url.toLocalFile())

    real_desktop = main_window_module.QDesktopServices
    main_window_module.QDesktopServices = _FakeDesktopServices

    def same_path(left: str, right: str) -> bool:
        """比路径要归一化：QUrl.toLocalFile() 给的是正斜杠（``C:/...``）。"""
        return (os.path.normcase(os.path.normpath(left))
                == os.path.normcase(os.path.normpath(right)))

    try:
        check("打开附件：交给系统打开那个文件",
              win.attachment_action(sent_file.id, "open") is True
              and opened and same_path(opened[-1], sent_file.attachment.path), str(opened))
        check("打开所在文件夹：打开的是它所在的目录",
              win.attachment_action(sent_file.id, "folder") is True
              and opened and same_path(opened[-1], os.path.dirname(picked_pdf)), str(opened))
    finally:
        main_window_module.QDesktopServices = real_desktop

    # 文件被删掉以后（丢了、移走了；连目录都不在了）
    gone_path = os.path.join(temp_dir, "已经不存在的目录", "已经被删掉的图.png")
    gone = Attachment(path=gone_path, name="已经被删掉的图.png", size=10)
    win.store.append(Message(id="out-gone", conversation_id=seed.CHAT_LIN,
                             sender_id="me", text="", ts=time.time(),
                             outgoing=True, attachment=gone))
    pump(app, 260)
    gone_bubble = win.chat_view.message_list.bubbles()["out-gone"]
    check("图片不在本地了：气泡降级成一行提示（不空白、不崩）",
          gone_bubble.image_view is not None
          and gone_bubble.image_view.has_image() is False
          and "不在本地" in gone_bubble.image_view.text(),
          gone_bubble.image_view.text() if gone_bubble.image_view else "")
    check("打开丢了的附件：给提示而不是静默失败",
          win.attachment_action("out-gone", "open") is False
          and "不在这个位置" in win.status_text(), win.status_text())
    check("打开丢了的文件夹：也给提示",
          win.attachment_action("out-gone", "folder") is False
          and "不见了" in win.status_text(), win.status_text())
    check("对着没有附件的消息点打开：安静地返回 False",
          win.attachment_action(win.store.get(seed.CHAT_LIN).messages[0].id, "open") is False)

    # 气泡右键菜单 + 附件按钮的菜单
    bubble_menu = win.chat_view._build_bubble_menu(sent_file.id)
    labels = [action.text() for action in bubble_menu.actions()]
    check("附件气泡的右键菜单里有「打开…」和「打开所在文件夹」",
          any(text.startswith("打开「") for text in labels) and "打开所在文件夹" in labels,
          " / ".join(labels))
    attach_menu = win.chat_view.composer.build_attach_menu()
    check("回形针按钮的菜单是「图片…」「文件…」两项",
          [action.text() for action in attach_menu.actions()] == ["🖼　图片…", "📎　文件…"],
          " / ".join(action.text() for action in attach_menu.actions()))

    # 换主题：文件卡片上的图标要跟着重画（图标是画出来的，不跟主题就露馅）
    icon_before = file_bubble.file_card.icon_label.pixmap().toImage()
    win.set_theme(THEME_DARK)
    pump(app, 460)
    icon_after = file_bubble.file_card.icon_label.pixmap().toImage()
    check("换主题后文件卡片的图标跟着重画", icon_before != icon_after)
    win.set_theme(THEME_LIGHT)
    pump(app, 460)
    shutil.rmtree(temp_dir, ignore_errors=True)

    # --- 设置区域（主题 / 文字大小 / 动效 / 数据）---------------------------
    check("侧栏有设置入口（齿轮按钮）",
          win.sidebar.settings_btn.toolTip().startswith("设置"), win.sidebar.settings_btn.toolTip())
    check("设置还有快捷键（Ctrl+,）—— 原先菜单里的那条随三点图标一起删了",
          "Ctrl+," in {s.key().toString() for s in win.findChildren(QShortcut)})

    os.makedirs(ARTIFACTS, exist_ok=True)
    dialog = win.open_settings()
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    check("设置窗口打开且复用同一实例", dialog.isVisible() and win.open_settings() is dialog)
    check("是独立窗口（不挡聊天、非模态）",
          dialog.parent() is win and not dialog.isModal())
    # 断言"正在入场"不能靠"睡一小会儿再看"：机器忙的时候 pump 会超时，动画可能已经跑完
    # （实测偶发翻车一次）。show() 里是同步启动动画的，所以直接就查状态。
    check("打开时整窗在动（淡入 + 从下往上滑）", dialog.is_entering())
    check("六张卡片（外观 / 动效 / 数据 / 关于 / 消息通知 / 窗口行为）",
          dialog.card_count() == 6, str(dialog.card_count()))
    check("入场结束后不留透明效果（稳态零开销）",
          wait_for(app, lambda: not dialog.is_entering()
                   and dialog.graphicsEffect() is None
                   and all(card.graphicsEffect() is None for card in dialog.cards)))
    path = os.path.join(ARTIFACTS, "settings_light.png")
    dialog.grab().save(path)
    print(f"  shot  {path}")

    # 卡片 hover：底色/描边渐变过渡（自绘插值，不是 QSS 硬切）
    look = dialog.cards[0]
    check("卡片默认底色 = 卡片色（未悬停）",
          look.hover_progress() == 0.0
          and look.background_color() == QColor(win.current_palette().card),
          look.background_color().name())
    QApplication.sendEvent(look, QEvent(QEvent.Type.Enter))
    check("鼠标进入：卡片开始渐变过渡", look.is_animating())
    check("过渡走到底，底色变成悬停色",
          wait_for(app, lambda: look.hover_progress() > 0.99)
          and look.background_color() == QColor(win.current_palette().track),
          look.background_color().name())
    QApplication.sendEvent(look, QEvent(QEvent.Type.Leave))
    check("移出后渐变回原色", wait_for(app, lambda: look.hover_progress() < 0.01))

    # 动效开关：旋钮滑动 + 轨道颜色渐变
    switch = dialog.motion_switch
    check("开关初始是开的", switch.is_checked() and switch.progress() == 1.0)
    track_on = switch.track_color()
    QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
    pump(app, 60)
    check("点一下开关：状态翻转，全局动效跟着关掉",
          not switch.is_checked() and not win.animations_enabled())
    check("关掉的瞬间直接落终态（不留动画尾巴）", switch.progress() == 0.0,
          str(switch.progress()))
    check("轨道颜色跟着变（开/关不是同一个色）", switch.track_color() != track_on,
          f"{track_on.name()} -> {switch.track_color().name()}")
    check("选择被记住（写进设置）",
          str(settings.value("animations", "")).lower() == "false",
          str(settings.value("animations", "")))
    check("关掉后其它动效直接落终态（界面语义不变）", _bubble_lands_instantly(win, app))
    switch.toggle()
    pump(app, 80)
    check("再点回来：动效恢复", win.animations_enabled() and switch.is_checked())
    # 动效开着时，旋钮的滑动是**真的在过渡**（直接改状态，绕开全局同步那条路）
    switch.set_checked(False, animate=True)
    check("动效开着：旋钮开始滑动", switch.is_animating())
    pump(app, 70)
    mid = switch.progress()
    check("滑动过程是插值的（不是一步到位）", 0.0 < mid < 1.0, f"{mid:.2f}")
    check("滑到底", wait_for(app, lambda: switch.progress() < 0.01))
    switch.set_checked(True, animate=False)          # 复位成开，后面还要用

    # 文字大小：全局缩放（样式表 + 写死在代码里的悬浮标签）
    sample_bubble = next(iter(win.chat_view.message_list.bubbles().values()))
    text_height_before = sample_bubble.text_label.fontMetrics().height()
    label_pt_before = win.sidebar.search_field._label.font().pointSizeF()
    target_scale = theme_module.FONT_SCALES[-2][1]          # 「大」
    dialog.font_control.set_index(theme_module.FONT_SCALE_LABELS.index("大"))
    # 同样不靠"睡一会儿"：set_index 会同步发出信号，动画这时就已经起来了
    check("改字号时预览文字在动画过渡（所见即所得）", dialog.is_preview_animating())
    check("字号不是一步到位（此刻还在起点附近）",
          dialog.preview_font_px() < 14.0 * target_scale - 0.01,
          str(dialog.preview_font_px()))
    pump(app, 300)
    check("选「大」：系数写进设置",
          abs(float(settings.value("text_scale", 0)) - target_scale) < 1e-6,
          str(settings.value("text_scale", 0)))
    check("分段控件停在「大」这一档", dialog.font_scale_label() == "大",
          dialog.font_scale_label())
    check("主窗口样式表里所有字号都按系数缩放了",
          "font-size: 15.39px" in win.styleSheet()          # 气泡正文 13.5 × 1.14
          and "font-size: 13.5px" not in win.styleSheet(),
          "15.39px 出现" if "font-size: 15.39px" in win.styleSheet() else "没找到缩放后的字号")
    check("气泡里的字真的变大了（按字体度量）",
          sample_bubble.text_label.fontMetrics().height() > text_height_before,
          f"{text_height_before} -> {sample_bubble.text_label.fontMetrics().height()}")
    check("悬浮标签的磅值也跟着放大（这份字号写死在代码里）",
          win.sidebar.search_field._label.font().pointSizeF() > label_pt_before + 0.5,
          f"{label_pt_before} -> {win.sidebar.search_field._label.font().pointSizeF()}")
    check("设置窗口自己也拿到了新样式表（对话框各有一套 QSS）",
          "font-size: 15.39px" in dialog.styleSheet())
    check("预览落在目标字号上",
          abs(dialog.preview_font_px() - 14.0 * target_scale) < 0.2
          and abs(dialog.preview.font().pixelSize() - round(14.0 * target_scale)) <= 1,
          f"{dialog.preview_font_px():.2f} / {dialog.preview.font().pixelSize()}")
    check("预览框高度够放新字号（回归：曾经被上下裁掉）",
          dialog.preview.height() >= QFontMetrics(dialog.preview.font()).height() + 20,
          f"{dialog.preview.height()} vs {QFontMetrics(dialog.preview.font()).height()}")

    # 主题：设置里切、侧栏跟着走、窗口自己换皮
    dialog.theme_control.set_index(THEME_OPTIONS.index(THEME_DARK))
    pump(app, 480)
    check("在设置里切主题：主窗口跟着变", win.current_palette().key == "dark")
    check("侧栏的分段控件同步到暗色", win.sidebar.theme_seg.value() == THEME_DARK)
    check("当前值文字标出档位", dialog.theme_label.text() == THEME_DARK,
          dialog.theme_label.text())
    check("设置窗口自己也换了皮（对话框不继承主窗口的样式表）",
          "#17212B" in dialog.styleSheet() or "#1E2A38" in dialog.styleSheet())
    # 回归：暗色下"关"态的旋钮曾经和轨道几乎同色（截图里糊成一块实心胶囊）
    switch.set_checked(False, animate=False)
    check("暗色下关态旋钮与轨道拉开对比（旋钮是白的）",
          dialog.motion_switch.knob_color().lightness()
          > dialog.motion_switch.track_color().lightness() + 60,
          f"旋钮 {dialog.motion_switch.knob_color().name()} "
          f"vs 轨道 {dialog.motion_switch.track_color().name()}")
    switch.set_checked(True, animate=False)
    dialog.motion_switch.apply_palette(win.current_palette())
    path = os.path.join(ARTIFACTS, "settings_dark.png")
    dialog.grab().save(path)
    print(f"  shot  {path}")

    # 别处改设置，设置窗口要同步（菜单里的动效开关）
    win.set_animations(False)
    pump(app, 120)
    check("菜单里关动效：设置窗口的开关同步关上",
          not dialog.motion_switch.is_checked())
    win.set_animations(True)
    pump(app, 140)
    check("再打开也同步回来", dialog.motion_switch.is_checked())

    # 数据卡片：统计与库路径
    check("数据卡片显示统计与库文件",
          "消息" in dialog.stats_label.text() and "附件" in dialog.stats_label.text()
          and win.log.path in dialog.data_path_label.text(),
          dialog.data_path_label.text())
    check("数据卡片有「打开数据目录」按钮", dialog.open_dir_btn.text() == "打开数据目录")

    # 重启：三项设置都要记得住
    state = _restarted_settings(settings, app)
    check("重启后字号恢复（样式表也是缩放过的）",
          abs(state["scale"] - target_scale) < 1e-6 and state["qss_scaled"],
          f"{state['scale']} / qss={state['qss_scaled']}")
    check("重启后主题恢复", state["theme"] == THEME_DARK, state["theme"])
    check("重启后动效开关恢复", state["animations"] is True)

    # 收尾：把界面恢复成"标准 / 亮色 / 动效开"，后面的用例都依赖这个稳态
    dialog.font_control.set_index(theme_module.FONT_SCALE_LABELS.index("标准"))
    pump(app, 280)
    win.set_theme(THEME_LIGHT)
    pump(app, 480)
    check("设置里能回到标准字号", abs(win.text_scale() - 1.0) < 1e-6, str(win.text_scale()))
    dialog.close_animated()
    check("关闭是淡出后收起", wait_for(app, lambda: not dialog.isVisible()))

    # --- 气泡右键菜单：回复 / 复制 / 置顶 / 转发 / 选择 / 删除 --------------
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 260)
    lin = win.store.get(seed.CHAT_LIN)
    incoming = next(m for m in lin.messages if not m.outgoing)
    mine = next(m for m in lin.messages if m.outgoing)
    menu = win.chat_view._build_bubble_menu(incoming.id)
    labels = [action.text() for action in menu.actions()]
    check("气泡菜单里有回复/复制/置顶/转发/选择",
          {"回复", "复制", "置顶", "转发…", "选择"} <= set(labels),
          " / ".join(labels))
    # 删除是**本地删除，不是撤回**：谁发的都能删（对方设备上仍然看得到）
    check("别人的消息也能删（本地删除，不是撤回）",
          any(text.startswith("⚠ 删除这条消息") for text in labels), " / ".join(labels))
    remove_action = next(a for a in menu.actions()
                         if a.text().startswith("⚠ 删除这条消息"))
    check("删除项上写清了语义（tooltip 提到不是撤回）",
          "不是撤回" in remove_action.toolTip(), remove_action.toolTip())
    own_labels = [a.text() for a in win.chat_view._build_bubble_menu(mine.id).actions()]
    check("自己的消息同样有「删除这条消息」",
          any(text.startswith("⚠ 删除这条消息") for text in own_labels),
          " / ".join(own_labels))

    # ---- 复制 ----
    copied = win.copy_message(incoming.id)
    pump(app, 160)
    check("复制把正文放进剪贴板",
          copied == incoming.text and clipboard.text() == incoming.text,
          clipboard.text())
    check("复制后有提示", "已复制" in win.status_text(), win.status_text())

    # ---- 回复 ----
    ref = win.start_reply(incoming.id)
    pump(app, 160)
    bar = win.chat_view.composer.reply_bar
    check("回复条挂上了并显示作者", bar.isVisible()
          and bar.title_label.text() == f"回复 {seed.LIN.name}",
          bar.title_label.text())
    check("回复条显示摘要", bar.text_label.text() == ref.preview, bar.text_label.text())
    check("切换会话不会把待回复带走",
          win.select_conversation(seed.CHAT_GROUP) and win.chat_view.reply_target() is None
          and win.select_conversation(seed.CHAT_LIN) and win.chat_view.reply_target() == ref)

    win.chat_view.set_input_text("我也看过了，没问题")
    win.send_message("我也看过了，没问题")
    pump(app, 300)
    reply_message = win.store.get(seed.CHAT_LIN).last_message
    reply_bubble = win.chat_view.message_list.bubbles().get(reply_message.id)
    check("发出的消息带着引用块（作者 + 摘要）",
          reply_bubble.quote is not None and reply_bubble.quote_text() == ref.text,
          reply_bubble.quote_text() if reply_bubble.quote else "没有引用块")
    check("发完回复条收起来（引用已经进消息了）",
          win.chat_view.reply_target() is None and not bar.isVisible())
    check("引用块跟着消息落库",
          win.log.load(seed.CHAT_LIN)[-1].reply_to == ref,
          str(win.log.load(seed.CHAT_LIN)[-1].reply_to))

    check("点引用块跳到被引用的那条",
          win.jump_to_quoted(ref.message_id)
          and win.chat_view.message_list.bubbles()[ref.message_id].is_flashing())

    # ---- 置顶（点置顶弹小窗问"要不要也给对方置顶"；可以多条）----
    pin_asks: list[tuple[str, bool]] = []

    def fake_ask_pin(parent, palette, *, peer_label="", for_both=False):
        """假的置顶小窗：记下被问的内容，按预先设定的答案返回（模态窗在无头下会挂住）。"""
        pin_asks.append((peer_label, for_both))
        if answers.get("pin_cancel"):
            return None
        return bool(answers.get("pin_both", False))

    win.ask_pin = fake_ask_pin
    answers["pin_both"] = False
    answers["pin_cancel"] = False
    check("置顶一条消息", win.toggle_pin_message(incoming.id) is True)
    check("点置顶弹了小窗，选项写的是对方的名字",
          pin_asks == [(f"也为 {seed.LIN.name} 置顶", False)], str(pin_asks))
    pump(app, 200)
    banner = win.chat_view.pin_banner
    check("置顶横幅出现并写着作者与摘要",
          banner.isVisible() and banner.text().startswith(f"{seed.LIN.name}："),
          banner.text())
    check("只有一条时不显示序号与翻页",
          banner.total() == 1 and banner.counter_text() == ""
          and not banner.next_btn.isVisible() and not banner.prev_btn.isVisible())
    check("置顶落到本地历史（带可见范围）",
          win.log.pinned_refs().get(seed.CHAT_LIN) == (PinRef(incoming.id, False),),
          str(win.log.pinned_refs()))
    labels_now = [a.text() for a in win.chat_view._build_bubble_menu(incoming.id).actions()]
    check("已置顶后菜单变成「取消置顶（仅我可见）」",
          any(text.startswith("取消置顶（仅我可见）") for text in labels_now),
          " / ".join(labels_now))

    # 菜单里只有**一个**「置顶」：可见范围靠小窗里的勾选项，不是两个带括号的菜单项
    raw_labels = [a.text() for a in win.chat_view._build_bubble_menu(mine.id).actions()]
    check("没置顶时菜单里就是一个「置顶」",
          "置顶" in raw_labels
          and not any("（" in text and text.startswith("置顶") for text in raw_labels),
          " / ".join(raw_labels))
    check("横幅上标着「仅我可见」", banner.scope_text() == "仅我可见", banner.scope_text())

    # 再置顶第二条 —— 这次在小窗里勾上"也为对方置顶"
    answers["pin_both"] = True
    check("置顶第二条，并在小窗里勾了「也为对方置顶」",
          win.toggle_pin_message(mine.id) is True and len(pin_asks) == 2
          and pin_asks[1][0] == f"也为 {seed.LIN.name} 置顶",
          str(pin_asks))
    answers["pin_both"] = False

    # 小窗里点取消 -> 什么都不改
    third_pin = next(m.id for m in win.store.get(seed.CHAT_LIN).messages
                     if m.id not in (incoming.id, mine.id))
    answers["pin_cancel"] = True
    check("小窗点取消 -> 不置顶、不落库",
          win.toggle_pin_message(third_pin) is False and len(pin_asks) == 3
          and win.store.get(seed.CHAT_LIN).pins
          == win.log.pinned_refs().get(seed.CHAT_LIN),
          str(win.store.get(seed.CHAT_LIN).pins))
    answers["pin_cancel"] = False
    pump(app, 200)
    check("横幅显示「1/2」并出现翻页按钮",
          banner.total() == 2 and banner.counter_text() == "1/2"
          and banner.next_btn.isVisible() and banner.prev_btn.isVisible(),
          banner.counter_text())
    check("横幅先显示最近置顶的那条", banner.message_id() == mine.id)
    check("两条都落库了（顺序：新的在前；范围标记也存住）",
          win.log.pinned_refs().get(seed.CHAT_LIN)
          == (PinRef(mine.id, True), PinRef(incoming.id, False)),
          str(win.log.pinned_refs()))
    check("横幅跟着显示「双方可见」", banner.scope_text() == "双方可见", banner.scope_text())
    both_labels = [a.text() for a in win.chat_view._build_bubble_menu(mine.id).actions()]
    check("已置顶的那条显示「取消置顶（双方可见）」",
          any(text.startswith("取消置顶（双方可见）") for text in both_labels),
          " / ".join(both_labels))
    check("另一条显示「取消置顶（仅我可见）」",
          any(text.startswith("取消置顶（仅我可见）")
              for text in [a.text() for a in
                           win.chat_view._build_bubble_menu(incoming.id).actions()]))

    banner.cycleRequested.emit(1)                   # 等价于点 ›
    pump(app, 160)
    check("点 › 翻到第二条", banner.message_id() == incoming.id and banner.counter_text() == "2/2",
          f"{banner.message_id()} {banner.counter_text()}")
    banner.cycleRequested.emit(1)                   # 到头绕回第一条
    check("翻到底是绕回去（不会卡住）", banner.message_id() == mine.id and banner.counter_text() == "1/2")
    banner.cycleRequested.emit(-1)
    check("‹ 往回翻", banner.message_id() == incoming.id)

    win.chat_view._on_pin_tapped()                  # 等价于点那一下横幅
    check("点横幅跳到当前显示的那条（这里是被翻到的那条）",
          win.chat_view.message_list.bubbles()[incoming.id].is_flashing())
    check("切换会话时横幅跟着换（别的会话没置顶就不显示）",
          win.select_conversation(seed.CHAT_GROUP) and not banner.isVisible()
          and win.select_conversation(seed.CHAT_LIN) and banner.isVisible())

    # ✕ 只取消当前这条，另一条留着
    shown = banner.message_id()
    check("✕ 只取消当前显示的那条",
          win.unpin_current() is True and banner.total() == 1
          and banner.message_id() != shown
          and win.store.get(seed.CHAT_LIN).pins
          == win.log.pinned_refs().get(seed.CHAT_LIN),
          f"取消前显示={shown} / 现在={banner.message_id()} / "
          f"store={win.store.get(seed.CHAT_LIN).pins} / log={win.log.pinned_refs()}")
    check("取消最后一条后横幅收起", win.unpin_current() is True and not banner.isVisible()
          and win.log.pinned_refs() == {})

    # 小窗自己：默认不勾、文案带对方名字、勾了才返回 True、取消返回 None
    dialog = PinDialog(win, peer_label="也为 王尼玛 置顶")
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.apply_palette(win.current_palette())
    dialog.show()
    pump(app, 160)
    check("小窗默认不勾（= 不打扰对方）",
          not dialog.for_both() and dialog.option_text() == "也为 王尼玛 置顶",
          f"{dialog.for_both()} / {dialog.option_text()}")
    check("小窗说清楚不勾会怎样（只有你这边置顶）",
          "自己" in dialog.scope_text() and not dialog.for_both(),
          dialog.scope_text())
    dialog.checkbox.set_checked(True)          # 点开关（用户路径：值变 + 发 toggled）
    dialog.checkbox.toggled.emit(True)
    pump(app, 120)
    check("勾上之后说法也跟着变（双方都置顶）",
          "双方" in dialog.scope_text() and dialog.for_both(),
          dialog.scope_text())
    dialog.ok_btn.click()
    check("点「置顶」= 接受，且带回勾选状态",
          dialog.result() == QDialog.DialogCode.Accepted and dialog.for_both() is True)
    dialog.close()
    cancel = PinDialog(win, peer_label="也为 王尼玛 置顶")
    cancel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    cancel.show()
    pump(app, 120)
    cancel.cancel_btn.click()
    check("点「取消」= 拒绝（调用方据此什么都不做）",
          cancel.result() == QDialog.DialogCode.Rejected)
    cancel.close()

    # ---- 开关：切换要走动画（还带旋钮"被拖着"的拉长），别"啪"一下落定 ----
    check("小窗套了整套主题样式（不是 Qt 默认灰按钮）",
          win.current_palette().accent.lower() in dialog.styleSheet().lower()
          and "QPushButton#primary" in dialog.styleSheet(),
          dialog.styleSheet()[:60])
    probe_switch = ToggleSwitch(False, win)
    probe_switch.apply_palette(win.current_palette())
    check("关态轨道和窗口底色分得开（不然就剩一个浮着的白圆）",
          abs(probe_switch.track_color().lightness()
              - QColor(win.current_palette().surface).lightness()) > 10,
          f"轨道={probe_switch.track_color().name()} "
          f"底色={win.current_palette().surface}")
    check("没在动的时候 phase/拉长都是 0",
          probe_switch.phase() == 0.0 and probe_switch.knob_stretch() == 0.0)
    # 用户点一下，然后**一边跑一边采样**：别假设"睡 110ms 就一定落在动画半路"——
    # 机器忙的时候定时器稀疏，一次 tick 就能从 0 跳到 1（这条断言自己就翻过车）。
    probe_switch.toggle()
    samples: list[tuple[float, float]] = []
    for _ in range(45):
        pump(app, 8)
        if probe_switch.is_animating():
            samples.append((probe_switch.progress(), probe_switch.knob_stretch()))
    check("切换过程中确实在动（采样到中间进度）",
          any(0.0 < value < 1.0 for value, _ in samples),
          f"采到 {len(samples)} 帧，前几帧={samples[:3]}")
    check("滑动途中旋钮会拉长（iOS 那种手感）",
          max((stretch for _, stretch in samples), default=0.0) > 0.5,
          f"最大拉长={max((s for _, s in samples), default=0.0):.2f}px")
    check("落定：动画结束、旋钮恢复正圆、进度到 1",
          not probe_switch.is_animating() and probe_switch.progress() == 1.0
          and probe_switch.knob_stretch() == 0.0)

    # ---- 选择模式 + 批量转发（逐条 / 合并）+ 批量删除 ----
    win.chat_view.begin_selection(incoming.id)
    pump(app, 260)
    check("进入选择模式：批量条出现、这一条已被勾上",
          win.chat_view.selection_mode() and win.chat_view.select_bar.isVisible()
          and win.chat_view.selected_ids() == [incoming.id])
    check("批量条显示已选条数", win.chat_view.select_bar.count_text() == "已选 1 条",
          win.chat_view.select_bar.count_text())
    check("选择模式里点气泡就勾选",
          win.chat_view.message_list.toggle_selection(mine.id)
          and set(win.chat_view.selected_ids()) == {incoming.id, mine.id},
          str(win.chat_view.selected_ids()))
    check("选择模式下不再弹气泡菜单",
          win.chat_view._build_bubble_menu(incoming.id) is None)
    bubble = win.chat_view.message_list.bubbles()[incoming.id]
    check("气泡上的圆勾跟着勾选状态", bubble.check.is_checked() and bubble.is_selected())
    check("勾选状态体现到气泡属性上（描边靠它）",
          bubble.property("selected") == "true")
    # 像素级回归：圆勾是自绘的，调色板没传进去就"状态对但什么都没画"（第一版就是这样）
    accent = QColor(win.current_palette().accent)
    drawn = sum(1 for x in range(bubble.check.width()) for y in range(bubble.check.height())
                if widget_pixel(bubble.check, bubble.check, x, y) == accent)
    check("圆勾真的画出来了（有主题色像素）", drawn > 8, f"{drawn} 个主题色像素")
    unselected = win.chat_view.message_list.bubbles()[
        win.store.get(seed.CHAT_LIN).messages[-1].id]
    check("没勾的那些画的是空心圈（不该有实心主题色）",
          sum(1 for x in range(unselected.check.width())
              for y in range(unselected.check.height())
              if widget_pixel(unselected.check, unselected.check, x, y) == accent) < 8)
    check("收到的消息：圆勾在气泡外侧（左边）", bubble.check.x() < bubble.x(),
          f"勾 {bubble.check.x()} vs 气泡 {bubble.x()}")
    outgoing_bubble = win.chat_view.message_list.bubbles()[mine.id]
    check("发出的消息：圆勾在外侧（右边）",
          outgoing_bubble.check.x() > outgoing_bubble.x(),
          f"勾 {outgoing_bubble.check.x()} vs 气泡 {outgoing_bubble.x()}")
    # ---- 点圆勾 & 拖动划选（Telegram 那种"按住左键在聊天页面划过去"）----------
    # 1) 点圆勾本身要能勾上。勾就挂在那一行里、画在气泡外面，以前它挂的是
    #    ``WA_TransparentForMouseEvents``（想当然以为"点在勾上等于点在气泡上"），
    #    而气泡的接收层只盖气泡自己 —— 点上去谁也收不到，点了没反应（用户报的就是这个）。
    listw = win.chat_view.message_list
    viewport = listw.viewport()
    listw.scroll_to_bottom()
    pump(app, 260)
    order = list(listw.bubbles().keys())
    visible = visible_bubbles(listw)          # 划选只能划到看得见的那几条
    check("消息区里至少 4 条气泡完整可见（不然下面的划选用例没意义）",
          len(visible) >= 4, f"可见 {len(visible)} 条 / 共 {len(order)} 条")
    first_id = order[visible[0]]
    first_bubble = listw.bubbles()[first_id]
    win.chat_view.end_selection()
    pump(app, 240)
    win.chat_view.begin_selection()
    pump(app, 260)
    listw.select_message(first_id, False)     # 先清空，专测"点勾"这一下
    pump(app, 160)
    hit = viewport.childAt(first_bubble.check.mapTo(viewport,
                                                    first_bubble.check.rect().center()))
    check("圆勾是真实的命中目标（以前这里落到行上，等于点了个寂寞）",
          hit is first_bubble.check, type(hit).__name__ if hit is not None else "None")
    QTest.mouseClick(first_bubble.check, Qt.MouseButton.LeftButton)
    pump(app, 180)
    check("点圆勾能把它选进来（用户报的那个 bug）",
          listw.selected_ids() == [first_id] and first_bubble.check.is_checked(),
          str(listw.selected_ids()))
    check("批量条跟着数到 1 条", win.chat_view.select_bar.count_text() == "已选 1 条",
          win.chat_view.select_bar.count_text())
    QTest.mouseClick(first_bubble.check, Qt.MouseButton.LeftButton)
    pump(app, 180)
    check("再点一下取消",
          listw.selected_ids() == [] and not first_bubble.check.is_checked(),
          str(listw.selected_ids()))

    # 2) 划选：在多选模式里按住左键划过去 = 一路选上（模式已经开着，划的途中布局不会跳）
    start, end = sweep_span(listw, visible[0], visible[2])
    sweep_select(listw, start, end)
    pump(app, 240)
    swept = [order[index] for index in visible[:3]]
    check("划选：划过的三条正好都选上（松手落在第三条上也没把它翻掉）",
          listw.selected_ids() == swept, str(listw.selected_ids()))
    check("批量条跟着显示 3 条", win.chat_view.select_bar.count_text() == "已选 3 条",
          win.chat_view.select_bar.count_text())
    path = os.path.join(ARTIFACTS, "chat_selection_sweep.png")
    win.grab().save(path)                     # 截图里核对：划过的那三条都实心勾上了
    print(f"  shot  {path}")
    check("划完手就松开了（扫选状态复位）", listw.is_sweeping() is False)
    sweep_select(listw, start, end)           # 起点那条已经选中 -> 反着刷
    pump(app, 240)
    check("从已选中的那条划起：把划过的这几条取消",
          listw.selected_count() == 0, str(listw.selected_ids()))
    check("一条不剩就自动退出多选模式（不留一个空的批量条）",
          not listw.selection_mode())

    # 3) 没进多选时划一下 = 直接进多选，而且按下的那条一定算划中
    #    （进多选这一下会顶布局：批量条出现、气泡右移、消息区矮一截 —— 锚点不能因此丢）
    start, end = sweep_span(listw, visible[0], visible[2])
    sweep_select(listw, start, end)
    pump(app, 280)
    check("没进多选时划一下就直接进多选模式",
          listw.selection_mode() and win.chat_view.select_bar.isVisible())
    check("按下的那条算划中（布局被顶了一下也不能丢）",
          swept[0] in listw.selected_ids(), str(listw.selected_ids()))
    check("划到的那几条也都在", set(swept) <= set(listw.selected_ids()),
          str(listw.selected_ids()))
    check("划不到的那条（再下面那条）没被选上",
          order[visible[3]] not in listw.selected_ids(), str(listw.selected_ids()))
    check("批量条上的数字 = 实际选中数",
          win.chat_view.select_bar.count_text() == f"已选 {listw.selected_count()} 条",
          win.chat_view.select_bar.count_text())

    # 4) 点一下 / 只挪几个像素 / Alt + 拖：都不算划选
    win.chat_view.end_selection()
    pump(app, 240)
    spot, _ = sweep_span(listw, visible[0], visible[0])
    sweep_select(listw, spot, spot, steps=0)
    pump(app, 200)
    check("原地按一下不进多选模式（还是普通点击）", not listw.selection_mode())
    sweep_select(listw, spot, spot + QPoint(0, SWEEP_SLOP - 3), steps=1)
    pump(app, 200)
    check("只挪 3px（没到划选阈值）也不算", not listw.selection_mode())
    sweep_select(listw, spot, QPoint(viewport.width() - 3, viewport.height() - 3),
                 modifiers=Qt.KeyboardModifier.AltModifier)
    pump(app, 200)
    check("Alt + 拖不划选（留着老规矩：用鼠标选中文字）", not listw.selection_mode())

    # 5) 从消息文字上起划：也要能划选，而且顺手被选中的文字得收干净
    text_point = first_bubble.text_label.mapTo(
        viewport, QPoint(4, first_bubble.text_label.height() // 2))
    sweep_select(listw, text_point, QPoint(viewport.width() - 3, viewport.height() - 3))
    pump(app, 280)
    check("从消息文字上起划也能划选",
          listw.selection_mode() and swept[0] in listw.selected_ids(),
          str(listw.selected_ids()))
    check("划选不是选文字：顺手选中的文字被收干净了",
          not first_bubble.text_label.hasSelectedText())

    # 还原成"选了两条"：后面的转发 / 删除用例都按这个状态来
    win.chat_view.end_selection()
    pump(app, 240)
    win.chat_view.begin_selection(incoming.id)
    listw.toggle_selection(mine.id)
    pump(app, 240)
    check("选择状态还原（incoming + mine 两条）",
          set(win.chat_view.selected_ids()) == {incoming.id, mine.id},
          str(win.chat_view.selected_ids()))

    # 转发窗口：默认逐条，多条时能切合并
    dialog = win.open_forward(win.chat_view.selected_ids())
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pump(app, 200)
    check("转发窗口列出所有会话", dialog.row_count() == 5
          and seed.LIN.name in dialog.row_titles(), str(dialog.row_titles()))
    check("转发窗口显示要转几条", dialog.subtitle_label.text() == "2 条消息",
          dialog.subtitle_label.text())
    check("默认方式是逐条转发", dialog.mode() == "逐条转发" and not dialog.is_merge())
    dialog.scope_control.set_index(1)
    pump(app, 120)
    check("可以切成合并转发", dialog.is_merge() and dialog.mode() == "合并转发")
    check("转发窗口复用一个实例", win.open_forward([incoming.id]) is dialog)
    check("只有一条消息时不显示方式选择（一条没有可合并的）",
          dialog.scope_control.isVisible() is False)
    win.open_forward(win.chat_view.selected_ids())   # 重新挂上刚选的两条

    # 转发窗口是"点一行就发出去"的小窗口：拉不动，会话行也不许探出窗口
    check("转发窗口固定尺寸（最小 == 最大，拉不动）",
          dialog.is_fixed_size() and dialog.minimumSize() == dialog.maximumSize(),
          f"{dialog.minimumSize().width()} / {dialog.maximumSize().width()}")
    check("转发窗口用了\"不能拉伸\"的对话框边框（鼠标不会出现双向箭头）",
          bool(dialog.windowFlags() & Qt.WindowType.MSWindowsFixedSizeDialogHint))
    dialog.resize(760, 340)                      # 模拟用户拖边框
    pump(app, 120)
    check("拖了也拉不动：尺寸还是 420x520（外加顶上自绘标题条那一条）",
          (dialog.width(), dialog.height()) == (FORWARD_WIDTH, FORWARD_HEIGHT + CHROME_H),
          f"{dialog.width()}x{dialog.height()}")
    check("所有会话行都在窗口里（右边/下边没有探出去的）",
          dialog.overflowing_rows() == [], str(dialog.overflowing_rows()))
    # 超长摘要：QPushButton 的文字不换行也不省略，会把行按钮撑得比视口还宽，
    # 而横向滚动条是关掉的 —— 探出去的部分直接看不见，所以要自己截断。
    targets = list(win._forward_targets())
    long_subtitle = "一条特别长的会话摘要" * 10
    targets[-1] = (targets[-1][0], long_subtitle)
    dialog.set_targets(targets)
    pump(app, 150)
    tail = dialog.row_labels()[-1].split("\n")[1]
    check("超长摘要被截成省略号（不把行按钮撑宽）",
          tail.endswith("…") and len(tail) < len(long_subtitle), tail)
    check("截断之后所有行仍然在窗口里",
          dialog.overflowing_rows() == [], str(dialog.overflowing_rows()))
    check("row_titles() 拿到的还是原始标题（截断只影响显示）",
          dialog.row_titles()[-1] == targets[-1][0].title, str(dialog.row_titles()[-1]))
    path = os.path.join(ARTIFACTS, "chat_light_forward.png")
    dialog.grab().save(path)                     # 截图里核对左右边距对称、长的截成省略号
    print(f"  shot  {path}")
    win.open_forward(win.chat_view.selected_ids())   # 恢复成"要转发这两条"

    # 逐条转发
    target = seed.CHAT_GROUP
    before_target = len(win.store.get(target).messages)
    # 假时钟对齐"真实此刻"：转发消息用真实时间戳，回执/回复按假时钟排期，
    # 不对齐的话回复的时间戳可能落在转发消息**之前**，顺序就乱了。
    ticks[0] = time.time()
    created = win.forward_messages(target, merge=False)
    pump(app, 320)
    check("逐条转发：目标会话多了两条、都带「转发自」",
          len(created) == 2 and len(win.store.get(target).messages) == before_target + 2
          and all(m.forwarded_from == seed.LIN.name and m.outgoing for m in created))
    # 转发也要"像手打发送一样"走传输层：单勾 -> 双勾，并且对面会回一句
    check("转发的时候也交给传输层排期（对面会收到）", transport.pending() >= 1,
          str(transport.pending()))
    check("转发完自动跳到目标会话、退出选择模式",
          win.current_conversation_id() == target and not win.chat_view.selection_mode())
    forwarded_bubble = win.chat_view.message_list.bubbles().get(created[0].id)
    check("转发消息画出了「转发自」小字",
          forwarded_bubble is not None
          and forwarded_bubble.findChild(QLabel, "forwardCaption") is not None)
    check("转发消息落库", win.log.count(target) == len(win.store.get(target).messages))

    target_before_reply = len(win.store.get(target).messages)
    ticks[0] += 0.5                      # 越过 typing_lead：对面开始"正在输入"
    win.tick()
    pump(app, 200)
    check("转发后对面就开始输入", win.chat_view.status_label.text() == "正在输入",
          win.chat_view.status_label.text())
    ticks[0] += 2.5                      # 越过 latency + 已读回执
    win.tick()
    pump(app, 320)
    group_now = win.store.get(target).messages
    check("转发后对面马上回了一句（每条转发算一次发送）",
          len(group_now) == target_before_reply + len(created)
          and not group_now[-1].outgoing and group_now[-1].text.strip() != "",
          f"{len(group_now)} vs {target_before_reply + len(created)}")
    check("转发出去的消息都变成已读（气泡从单勾变双勾）",
          all(m.is_read for m in created),
          str([(m.id, m.status) for m in created]))

    # 合并转发
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 200)
    win.chat_view.begin_selection()
    for message in win.store.get(seed.CHAT_LIN).messages[:3]:
        win.chat_view.message_list.toggle_selection(message.id)
    check("勾上三条准备合并转发", win.chat_view.selected_count() == 3)
    merged_dialog = win.open_forward(win.chat_view.selected_ids())
    merged_dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    merged_dialog.scope_control.set_index(1)
    pump(app, 120)
    before_target = len(win.store.get(target).messages)
    merged_created = win.forward_messages(target, merge=True)
    pump(app, 400)
    check("合并转发只生成一条消息",
          len(merged_created) == 1 and len(win.store.get(target).messages) == before_target + 1,
          f"{len(merged_created)} / {len(win.store.get(target).messages)}")
    merged_message = merged_created[0]
    check("合并转发里有三条、来源正确",
          merged_message.is_merged and merged_message.merged_count == 3
          and merged_message.forwarded_from == seed.LIN.name)
    card = win.chat_view.message_list.bubbles()[merged_message.id].merged_card
    # 卡片是 QQ 那种样子：标题（谁的聊天记录）+ 一行摘要 + "查看 N 条转发消息"
    check("气泡里是「X 的聊天记录」卡片",
          card is not None and card.title_label.text() == f"{seed.LIN.name} 的聊天记录",
          card.title_label.text() if card else "没有卡片")
    first_author = merged_message.merged_items[0].author
    check("卡片上只有一行摘要（作者：内容），不把全部内容塞进气泡",
          card.preview_text() == merged_message.merged_preview()
          and chr(10) not in card.preview_text()
          and card.preview_text().startswith(f"{first_author}："),
          card.preview_text())
    check("卡片底部写着「查看 N 条转发消息」",
          card.count_text() == "查看 3 条转发消息 ›", card.count_text())
    check("卡片里每条都记了原消息的时间（查看窗口要显示）",
          all(entry.clock for entry in merged_message.merged_items),
          str([entry.clock for entry in merged_message.merged_items]))
    stored = {m.id: m for m in win.log.load(target)}     # 别用 [-1]：对面可能已经回了一句
    check("合并转发落库可还原",
          stored[merged_message.id].merged_items == merged_message.merged_items)

    # 点开 = 独立的"聊天记录"窗口，按聊天气泡逐条列
    card.opened.emit()                      # 等价于点那张卡片
    pump(app, 260)
    viewer = win.merged_viewer()
    viewer.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pump(app, 200)
    check("点卡片打开「聊天记录」窗口", viewer is not None and viewer.isVisible())
    check("窗口标题写明来源与条数",
          viewer.title_label.text() == f"{seed.LIN.name} 的聊天记录"
          and viewer.subtitle_label.text() == "共 3 条消息",
          f"{viewer.title_label.text()} / {viewer.subtitle_label.text()}")
    check("窗口里逐条列出三条（头像 + 昵称 + 内容 + 时间）",
          viewer.row_count() == 3
          and [row[0] for row in viewer.row_texts()] == [i.author for i in
                                                         merged_message.merged_items]
          and all(row[1] for row in viewer.row_texts()),
          str(viewer.row_texts()))
    check("每条内容就是原消息正文（能选中复制）",
          viewer.body_texts() == [i.text for i in merged_message.merged_items],
          str(viewer.body_texts()))
    path = os.path.join(ARTIFACTS, "chat_light_merged_viewer.png")
    viewer.grab().save(path)
    print(f"  shot  {path}")
    check("再点一次还是同一个窗口（复用，不叠窗）",
          win.open_merged_view(merged_message.id) is viewer)
    check("（逐条转发这种非合并消息）不会开查看窗口",
          win.open_merged_view(created[0].id) is None)
    check("没有正文的合并转发消息，复制会明确提示（不静默失败）",
          win.copy_message(merged_message.id) == ""
          and "没有可复制的文字" in win.status_text(), win.status_text())
    path = os.path.join(ARTIFACTS, "chat_light_merged.png")
    pump(app, 320)
    win.grab().save(path)
    print(f"  shot  {path}")

    # 超过 3 条：默认露 3 条 + "还有 N 条"
    win.select_conversation(seed.CHAT_LIN)
    pump(app, 220)
    win.chat_view.begin_selection()
    for message in win.store.get(seed.CHAT_LIN).messages[:4]:
        win.chat_view.message_list.toggle_selection(message.id)
    check("勾上四条准备合并转发", win.chat_view.selected_count() == 4)
    win.open_forward(win.chat_view.selected_ids())
    four = win.forward_messages(target, merge=True)
    pump(app, 360)
    card4 = win.chat_view.message_list.bubbles()[four[0].id].merged_card
    check("四条时卡片写着「查看 4 条转发消息」",
          card4.count_text() == "查看 4 条转发消息 ›", card4.count_text())
    win.open_merged_view(four[0].id)
    pump(app, 240)
    check("查看窗口里也是四条（且带附件的那条有 📎 行）",
          viewer.row_count() == 4
          and viewer.subtitle_label.text() == "共 4 条消息",
          f"{viewer.row_count()} / {viewer.subtitle_label.text()}")

    # 批量删除：取消分支 / 谁发的都删（本地删除）
    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 200)
    group_messages = win.store.get(seed.CHAT_GROUP).messages
    group_count = len(group_messages)            # 注意：列表是"活的"，删除会就地改它
    my_ids = [m.id for m in list(group_messages) if m.outgoing][:2]
    other_id = next(m.id for m in group_messages if not m.outgoing)
    answers["confirm"] = False
    check("批量删除的取消分支：一条不少",
          win.delete_messages(my_ids) == 0
          and len(win.store.get(seed.CHAT_GROUP).messages) == group_count)
    answers["confirm"] = True
    win.chat_view.begin_selection()
    for message_id in my_ids + [other_id]:
        win.chat_view.message_list.toggle_selection(message_id)
    check("选了两条自己的 + 一条别人的", win.chat_view.selected_count() == 3)
    removed = win.delete_messages(win.chat_view.selected_ids())
    pump(app, 260)
    check("批量删除把选中的三条都删掉（别人发的那条也删）",
          removed == 3 and len(win.store.get(seed.CHAT_GROUP).messages) == group_count - 3,
          f"删了 {removed} 条，剩 {len(win.store.get(seed.CHAT_GROUP).messages)}（原 {group_count}）")
    check("别人那条只是「我这边看不到」：库里也没有了",
          win.store.message(other_id) is None
          and not win.chat_view.message_list.has_message(other_id))
    check("删掉的气泡都不在界面上了",
          all(not win.chat_view.message_list.has_message(mid) for mid in my_ids))
    check("本地历史里也删干净了（不是只从界面拿掉）",
          win.log.load(seed.CHAT_GROUP)[-1].id not in my_ids + [other_id])
    check("批量删除后退出选择模式", not win.chat_view.selection_mode())
    check("批量删除提示写明了条数与本机语义",
          "已删除 3 条消息（仅本地）" in win.status_text(), win.status_text())

    # 截图：选择模式 + 合并转发卡片
    win.select_conversation(seed.CHAT_GROUP)
    pump(app, 260)
    dialog.close()
    win.chat_view.begin_selection()
    for message_id in [m.id for m in win.store.get(seed.CHAT_GROUP).messages][:3]:
        win.chat_view.message_list.toggle_selection(message_id)
    path = os.path.join(ARTIFACTS, "chat_light_selection.png")
    pump(app, 320)
    win.grab().save(path)
    print(f"  shot  {path}")
    win.chat_view.end_selection()
    pump(app, 240)

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

    # 旧的两个入口要真的没了（标为已读已删、清空历史挪进会话右键菜单），
    # 三点图标（"更多"菜单）也整个删掉了：图标 / 两个按钮 / 信号 / 菜单构造都没了。
    # 原来菜单里的能力必须都还有别的入口 —— 这里逐项点名，免得以后"顺手"又删掉一处。
    check("侧栏头部不再有三点按钮", not hasattr(win.sidebar, "more_btn"))
    check("聊天头部不再有三点按钮", not hasattr(win.chat_view, "more_btn"))
    check("三点菜单的信号与构造都不在了",
          not hasattr(win.sidebar, "menuRequested")
          and not hasattr(win.chat_view, "moreRequested")
          and not hasattr(win, "_build_menu")
          and not hasattr(win, "_show_menu"))
    check("「more」图标也从图标表里删了（画不出东西）",
          make_icon("more", "#000000", 20).isNull())
    check("原来菜单里的能力都还有按钮入口",
          win.sidebar.tool_btn.isVisible()                  # 安全工具箱
          and win.chat_view.composer.smile_btn.isVisible()  # 表情面板
          and win.sidebar.settings_btn.isVisible()          # 设置（统计/打开目录/动效都在里面）
          and win.chat_view.search_btn.isVisible())         # 搜索当前会话
    shortcut_keys = {s.key().toString() for s in win.findChildren(QShortcut)}
    check("原菜单里的快捷键一个没少",
          {"Ctrl+T", "Ctrl+E", "Ctrl+,", "Ctrl+Shift+F"} <= shortcut_keys,
          str(sorted(shortcut_keys)))
    check("跨会话搜索还有别的路（搜索窗口里自带「全部会话」档）",
          win.open_search_all().scope_mode() == "all")
    check("本地历史统计与动效开关搬进了设置区域（菜单删了也不丢）",
          "消息" in win.open_settings().stats_label.text()
          and win.settings_dialog().motion_switch.is_checked())

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

    # "把输入框文字送进工具箱"原来挂在"更多"菜单里；菜单删掉后并进了工具箱按钮本身：
    # 打开时如果工具箱是空的、而聊天输入框有字，就自动带过去（带过去时给个提示）。
    tool.set_input("")                              # 先清空，模拟"干净的工具箱"
    win.chat_view.composer.edit.setPlainText("这段话要加密发出")
    win.open_tool()
    pump(app, 300)
    check("打开工具箱会自动带上输入框里的文字",
          tool.input_field.text() == "这段话要加密发出", tool.input_field.text())
    check("带过去时有提示", "带进工具箱" in win.status_text(), win.status_text())

    tool.set_input("用户自己敲的")
    win.chat_view.composer.edit.setPlainText("输入框里的新内容")
    win.open_tool()
    pump(app, 200)
    check("工具箱里已经有内容时不覆盖（免得顶掉用户敲的东西）",
          tool.input_field.text() == "用户自己敲的", tool.input_field.text())
    win.open_tool(prefill="显式指定")
    check("显式传 prefill 仍然强制送入（老 API 的语义保留）",
          tool.input_field.text() == "显式指定", tool.input_field.text())
    win.chat_view.composer.edit.setPlainText("")

    # --- 输入字色可读性（回归：暗色主题下输入框曾是黑字，肉眼看不见）------
    win.select_conversation(seed.CHAT_LIN)
    composer = win.chat_view.composer.edit
    composer.setPlainText("输入字色可读性")
    pump(app, 250)

    win.set_theme(THEME_LIGHT)
    # 切主题会盖一层"旧外观快照"做交叉淡入。抓像素前必须等它收干净：淡出到一半时
    # 快照是半透明的，会把深浅两头都拉平 —— 实测偶发"深=0 亮=0"（不是字色的问题）。
    pump(app, 200)
    check("切换主题的交叉淡入已收干净（快照不挡像素断言）",
          wait_for(app, lambda: win.theme_overlay() is None))
    dark_px, light_px, darkest, _ = glyph_pixels(win, composer)
    check("亮色：输入框里是深色字", dark_px > 100, f"深={dark_px} 亮={light_px}")
    check("亮色：输入框调色板跟随主题",
          composer.palette().color(QPalette.ColorRole.Text).name().lower()
          == win.current_palette().text.lower(),
          composer.palette().color(QPalette.ColorRole.Text).name())

    win.set_theme(THEME_DARK)
    pump(app, 200)
    check("暗色下同样等淡入收干净", wait_for(app, lambda: win.theme_overlay() is None))
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
        # 截图也等交叉淡入收干净：不然可能把"半透明的旧外观"拍进去（artifact 就废了）
        wait_for(app, lambda: win.theme_overlay() is None)
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

    # 置顶横幅是**另一处** QTextDocument 渲染的文本：国旗同样要换成图
    # （回归：气泡里是图、置顶栏里却一直是 "JP/US" 字母 —— 就是漏了这一处）
    check("置顶一条带国旗的消息", win.toggle_pin_message("in-flag-shot", for_both=False) is True)
    pump(app, 240)
    flag_banner = win.chat_view.pin_banner
    check("置顶横幅里的国旗也是图片（不是 CN 字母）",
          win.chat_view.pinned_message_id() == "in-flag-shot"
          and flag_banner.text_label.textFormat() == Qt.TextFormat.RichText
          and flag_banner.text_label.text().count("<img") == 4,
          flag_banner.text_label.text()[:140])
    shoot("light_pin_flag")
    # 光看标签文本里写着 <img> 还不够：素材没画出来时文本照样是 <img>。
    # 数一次横幅区域里的彩色像素 —— 国旗是彩色图，退化成 "JP" 字母时这里会掉到 0 附近。
    banner_colors = colorful_pixels(win, flag_banner.text_label)
    check("置顶横幅里真的画出了彩色国旗（不只是标签里写着 <img>）",
          banner_colors > 300, str(banner_colors))

    # 同一类漏网之鱼：回复条 / 引用块 / 合并转发 / 搜索结果都是 QTextDocument 渲染的
    # 消息文本 —— 国旗都得是图片（此前只有气泡和侧栏做了转换）
    flag_ref = win.start_reply("in-flag-shot")
    pump(app, 200)
    reply_bar = win.chat_view.composer.reply_bar
    check("回复条里的国旗也是图片",
          flag_ref is not None and reply_bar.isVisible()
          and reply_bar.text_label.textFormat() == Qt.TextFormat.RichText
          and reply_bar.text_label.text().count("<img") == 4
          and reply_bar.text() == flag_ref.preview,
          reply_bar.text_label.text()[:140])
    win.chat_view.set_input_text("收到，那就带上 🇯🇵 那面")
    win.send_message("收到，那就带上 🇯🇵 那面")
    pump(app, 320)
    sent_flag = next((m for m in win.store.get(seed.CHAT_GROUP).messages
                      if m.text.startswith("收到，那就带上")), None)
    sent_bubble = (win.chat_view.message_list.bubbles().get(sent_flag.id)
                   if sent_flag is not None else None)
    check("气泡里引用块的国旗也是图片（原文仍按纯文本返回）",
          sent_bubble is not None and sent_bubble.quote is not None
          and sent_bubble.quote.text_label.textFormat() == Qt.TextFormat.RichText
          and sent_bubble.quote.text_label.text().count("<img") == 4
          and sent_bubble.quote_text() == f"{flag_ref.author}：{flag_ref.preview}",
          sent_bubble.quote_text() if sent_bubble else "没有引用块")

    # 合并转发：卡片那一行摘要 + "查看聊天记录"窗口里逐条正文，国旗同样要换图
    merged_flag = Message(
        id="in-merged-flag", conversation_id=seed.CHAT_GROUP, sender_id="su", text="",
        ts=time.time() + 6, outgoing=False,
        merged_items=(MergedItem("尼玛", "看这面 🇨🇳 和这面 🇯🇵", "", "09:00"),
                      MergedItem("我", "收到 🏴‍☠️", "", "09:01")))
    win.store.receive(merged_flag)
    pump(app, 320)
    merged_card = win.chat_view.message_list.bubbles()["in-merged-flag"].merged_card
    check("合并转发卡片摘要里的国旗是图片（preview_text 仍是原文）",
          merged_card is not None
          and merged_card.preview_label.textFormat() == Qt.TextFormat.RichText
          and merged_card.preview_label.text().count("<img") == 2
          and merged_card.preview_text() == merged_flag.merged_preview(),
          merged_card.preview_label.text()[:140] if merged_card else "没有卡片")
    flag_viewer = win.open_merged_view("in-merged-flag")
    flag_viewer.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pump(app, 260)
    check("「聊天记录」窗口里每条正文的国旗也是图片",
          [row.body_label.textFormat() for row in flag_viewer._rows]
          == [Qt.TextFormat.RichText, Qt.TextFormat.RichText]
          and [row.body_label.text().count("<img") for row in flag_viewer._rows] == [2, 1]
          and flag_viewer.body_texts() == [i.text for i in merged_flag.merged_items],
          str(flag_viewer.body_texts()))

    # 搜索结果：命中片段那一行同样是 QTextDocument 渲染的文本
    win.store.receive(Message(id="in-search-flag", conversation_id=seed.CHAT_GROUP,
                              sender_id="su", text="国旗测试 🇨🇳 关键词语",
                              ts=time.time() + 8, outgoing=False))
    pump(app, 300)
    flag_search = win.open_search()
    flag_search.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    flag_search.set_query("关键词语")
    hit_count = flag_search.run_search()
    hit_row = flag_search._rows[0] if flag_search._rows else None
    check("搜索结果里的国旗也是图片",
          hit_count >= 1 and hit_row is not None
          and hit_row.snippet_label.textFormat() == Qt.TextFormat.RichText
          and hit_row.snippet_label.text().count("<img") == 1,
          hit_row.snippet_label.text()[:140] if hit_row else "没有结果")
    flag_search.close()
    # 一张图把"回复条 + 置顶横幅 + 合并转发卡片"都框进去，人工复核旗帜真的是彩图
    shoot("light_flag_everywhere")

    check("取消置顶后横幅收起来",
          win.unpin_current("in-flag-shot") is True
          and win.chat_view.pinned_message_id() == "" and not flag_banner.isVisible())

    # 附件：图片消息（演示数据里那条）+ 一条文件消息，人工复核缩略图比例、卡片排版
    win.select_conversation(seed.CHAT_DESIGN)
    report = seed.demo_attachment(core_assets.DEMO_REPORT)
    if report is not None:
        win.store.receive(Message(
            id="in-file-shot", conversation_id=seed.CHAT_DESIGN, sender_id="su",
            text="顺便把评审材料发你，v3 那版。", ts=time.time() + 2,
            outgoing=False, attachment=report))
    pump(app, 380)
    # 滚到图片那条，截图里要看到完整的缩略图（不然只露出半张）
    photo_bubble = next((win.chat_view.message_list.bubbles()[m.id]
                         for m in win.store.get(seed.CHAT_DESIGN).messages
                         if m.has_attachment and m.id in win.chat_view.message_list.bubbles()),
                        None)
    if photo_bubble is not None:
        win.chat_view.message_list.ensureWidgetVisible(photo_bubble, 0, 30)
    pump(app, 320)
    shoot("light_attach")
    check("截图里附件消息真的画出来了（缩略图不是空图）",
          photo_bubble is not None and photo_bubble.image_view is not None
          and not photo_bubble.image_view.pixmap().isNull(),
          "没有附件气泡")
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
    # 转发窗口也是独立顶层窗口：样式表得自己重建才会跟着换主题（顺带留一张暗色截图）
    bot_messages = win.store.get(seed.CHAT_BOT).messages
    forward = win.open_forward([m.id for m in bot_messages[-2:]])
    forward.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pump(app, 260)
    check("转发窗口也跟着换主题", "#17212B" in forward.styleSheet() or
          "#1E2A38" in forward.styleSheet())
    check("暗色下会话行依旧都在窗口里", forward.overflowing_rows() == [],
          str(forward.overflowing_rows()))
    path = os.path.join(ARTIFACTS, "chat_dark_forward.png")
    forward.grab().save(path)
    shots.append(path)
    forward.close()
    pump(app, 160)
    win.select_conversation(seed.CHAT_BOT)
    # 给机器人那条挂个回应：截图里要能看见气泡下方的胶囊
    win.toggle_reaction(win.store.get(seed.CHAT_BOT).last_message.id, "👍")
    pump(app, 320)
    shoot("dark_bot")

    # 暗色下的附件：文件卡片的半透明底、图片边框在深色里什么样，得看一眼
    win.select_conversation(seed.CHAT_DESIGN)
    pump(app, 340)
    shoot("dark_attach")
    win.select_conversation(seed.CHAT_BOT)      # 下面这条搜索用例假定当前是机器人会话
    pump(app, 240)

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


def _restarted_settings(settings, app) -> dict:
    """重启一次：三项设置（字号 / 主题 / 动效）有没有记住。

    字号还要看**样式表里是不是真的缩放过** —— 只把数字存下来、却没重建 QSS 的话
    用户重启后看到的还是原来大小。
    """
    other = _restarted_window(settings, app)
    state = {
        "scale": other.text_scale(),
        "theme": other.theme_name(),
        "animations": other.animations_enabled(),
        "qss_scaled": "font-size: 15.39px" in other.styleSheet(),
    }
    _close_window(other, app)
    return state


def _bubble_lands_instantly(win, app) -> bool:
    """关掉动效时：新气泡不该留透明效果（直接落终态，界面语义不变）。"""
    win.chat_view.composer.edit.insert_text("关掉动效之后发的消息")
    win.chat_view.composer.submit()
    pump(app, 140)
    message = win.store.get(win.current_conversation_id()).last_message
    bubble = win.chat_view.message_list.bubbles().get(message.id)
    return bubble is not None and bubble.graphicsEffect() is None


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



class FakeClipboard:
    """假剪贴板：只记下被写进来的东西，**不碰系统剪贴板**。

    产品代码里"复制"是写系统剪贴板的；测试如果直接调用它，每跑一次就会把用户
    剪贴板里的东西顶掉（这事儿真发生过）。所以测试统一换成这个假实现。
    """

    def __init__(self) -> None:
        self.text_value = ""

    def setText(self, text) -> None:                     # noqa: N802
        self.text_value = str(text)

    def text(self) -> str:
        return self.text_value

def test_ui_suite() -> None:
    """让 ``pytest tests`` 也能跑这套脚本式用例（本地直接运行 main() 同样可行）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
