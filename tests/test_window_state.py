"""窗口位置/大小记忆 + 托盘常驻与未读提醒（无头、不弹窗）。

覆盖：
* 关窗存几何、下次开在原处；
* 「不在任何屏幕上」时丢弃那份几何、退回默认尺寸（拔外接屏之后的经典事故）；
* 托盘回调：点图标叫回窗口、右键/中键不叫、回调抛异常不许冒出来；
* 托盘菜单两项（显示主窗口 / 退出 MChat）—— 有托盘的环境才断言菜单内容；
* 未读数写进托盘提示（0 时退回纯 "MChat"）、怪值不崩；
* 收到消息时"未读同步 + 任务栏闪一下"两条都被触发；已读之后未读跟着掉。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest                                                       # noqa: E402
from PyQt6.QtCore import QSettings, Qt                               # noqa: E402
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon            # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed                                                         # noqa: E402
from mchat.core.db import ChatLog                                   # noqa: E402
from mchat.core.models import Message                               # noqa: E402
from mchat.core.transport import LocalTransport                     # noqa: E402
from mchat.ui.main_window import MainWindow                         # noqa: E402
from mchat.ui.notify import Notifier                                # noqa: E402

NAMESPACE = ("MChatWindowStateTest", "MChatWindowStateTest")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(application, ms=140) -> None:
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def settings():
    value = QSettings(*NAMESPACE)
    value.clear()
    yield value
    value.clear()
    value.sync()


def make_window(settings, *, show=True) -> MainWindow:
    demo = seed.build_demo()
    transport = LocalTransport(demo.scripts, clock=time.time)
    win = MainWindow(demo=demo, transport=transport, settings=settings,
                     log=ChatLog(":memory:"), confirm=lambda **kw: True,
                     filedialog=lambda kind, parent=None: "")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    if show:
        win.show()
        QApplication.setActiveWindow(win)
    return win


# --------------------------------------------------------------- 窗口几何
def test_geometry_survives_a_restart(app, settings):
    first = make_window(settings)
    pump(app, 120)
    first.resize(1020, 660)
    first.move(80, 60)
    pump(app, 80)
    first._save_window_state()
    first.close()
    first.deleteLater()
    pump(app, 60)

    second = make_window(settings)
    pump(app, 120)
    try:
        assert (second.width(), second.height()) == (1020, 660), "尺寸没记住"
        assert (second.x(), second.y()) == (80, 60), "位置没记住"
    finally:
        second.close()
        second.deleteLater()
        pump(app, 40)


def test_default_size_when_nothing_saved(app, settings):
    win = make_window(settings)
    pump(app, 60)
    try:
        assert win.size() == win._preferred_size(), "第一次打开该用默认尺寸"
    finally:
        win.close()
        win.deleteLater()
        pump(app, 40)


def test_geometry_off_screen_falls_back_to_default(app, settings):
    """那份几何是上次写的，中间可能拔了外接屏 —— 不能在屏幕外凭空开一个窗口。"""
    first = make_window(settings)
    pump(app, 80)
    first._save_window_state()
    first.close()
    first.deleteLater()
    pump(app, 40)

    second = make_window(settings, show=False)
    second._geometry_on_screen = lambda: False           # 模拟"框不在任何屏幕上"
    try:
        assert second._restore_window_state() is False, "屏幕外应当拒绝恢复"
        assert second.size() == second._preferred_size(), "拒绝之后要退回默认尺寸"
    finally:
        second.close()
        second.deleteLater()
        pump(app, 40)


def test_quit_from_tray_closes_the_window(app, settings):
    """托盘「退出」要走正常关窗（收尾 + 通知启动器），不是 app.quit()。"""
    win = make_window(settings)
    pump(app, 80)
    closed: list[int] = []
    win.closed.connect(lambda: closed.append(1))
    win.quit_from_tray()
    pump(app, 80)
    assert closed, "托盘退出要让启动器收到 closed，否则进程不退"


def test_show_from_tray_brings_the_window_back(app, settings):
    win = make_window(settings)
    pump(app, 80)
    try:
        win.hide()
        pump(app, 60)
        assert not win.isVisible()
        win.show_from_tray()
        pump(app, 60)
        assert win.isVisible()
    finally:
        win.close()
        win.deleteLater()
        pump(app, 40)


# ------------------------------------------------------------------- 托盘
def test_tray_callbacks_and_no_crash_on_click(app):
    notifier = Notifier()
    try:
        shown: list[int] = []
        quits: list[int] = []
        notifier.set_actions(lambda: shown.append(1), lambda: quits.append(1))
        notifier._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        notifier._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        assert len(shown) == 2, "左键单击/双击都该把窗口叫回来"
        notifier._on_activated(QSystemTrayIcon.ActivationReason.Context)
        assert len(shown) == 2, "右键（弹菜单）不该顺手把窗口叫回来"
        assert not quits, "点图标不是退出"
        notifier.set_actions(lambda: 1 / 0, None)        # 回调自己炸了也不许冒出来
        notifier._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    finally:
        notifier.stop()


def test_tray_menu_offers_show_and_quit(app):
    notifier = Notifier()
    try:
        notifier.set_actions(lambda: None, lambda: None)
        if notifier._tray is None:                       # 没托盘的环境：什么都不该建
            assert notifier._menu is None
            return
        labels = [action.text() for action in notifier._menu.actions()]
        assert labels == ["显示主窗口", "退出 MChat"], labels
    finally:
        notifier.stop()


def test_unread_written_to_tray_tooltip(app):
    notifier = Notifier()
    try:
        notifier.set_unread(3)
        assert notifier.unread() == 3
        if notifier._tray is not None and notifier.available():
            assert "3 条未读" in notifier._tray.toolTip()
        notifier.set_unread(0)
        assert notifier.unread() == 0
        if notifier._tray is not None and notifier.available():
            assert notifier._tray.toolTip() == "MChat", "没有未读就别挂着一串数字"
        notifier.set_unread("乱七八糟")                   # 脏值不许崩
        assert notifier.unread() == 0
    finally:
        notifier.stop()


# ------------------------------------------------- 收消息 → 未读 + 任务栏
def test_incoming_message_syncs_unread_and_flashes(app, settings):
    win = make_window(settings)
    pump(app, 120)
    calls: list[str] = []
    win._sync_tray_unread = lambda: calls.append("unread")
    win._flash_taskbar = lambda: calls.append("flash")
    message = Message(id="m_test_incoming", text="在吗", conversation_id=seed.CHAT_LIN,
                      sender_id="u_bob", ts=time.time(), outgoing=False)
    win._notify_incoming(message)
    assert calls == ["unread", "flash"], calls
    win.close()
    win.deleteLater()
    pump(app, 40)


def test_tray_unread_follows_store_total(app, settings):
    win = make_window(settings)
    pump(app, 120)
    win._sync_tray_unread()
    assert win._notifier.unread() == win._store.total_unread()
    win.close()
    win.deleteLater()
    pump(app, 40)
