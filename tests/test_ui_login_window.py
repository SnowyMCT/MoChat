"""登录 / 注册窗口的排版回归（零网络：只搭界面、量几何）。

为什么单独测这个：这一屏是"两种模式共用一扇窗"——登录 3 格、注册 6 格，
字段一多一少，布局很容易出现**间距被压扁**（注册页六格挤成 5px）或**中间空一大块**
（上下两个弹簧分走空间）这种肉眼能看出来的毛病，而功能测试（能不能登录）一条都发现不了。
所以这里盯死四件事：

1. 窗口尺寸**定死**，且两种模式完全一致（用户明确要求：这一屏不许缩放、切换不许跳）；
2. 该显示的字段才对（登录 3 格 / 注册 6 格），且都在窗口范围内（不许被裁掉）；
3. 相邻元素的间距**处处相等**（排版节奏均匀，没有被布局抽走几像素）；
4. 注册模式下"上次登录的账号"那排快捷标签要藏起来（它是登录场景的东西，还会撑版面）。

用法：``.venv/Scripts/python.exe -m pytest tests/test_ui_login_window.py -q``

**为什么文件名带 ``ui_`` 前缀（= 为什么它必须排在最后）**：这套测试会真的创建并显示控件，
而「先创建过控件」的进程会**弄脆** ``test_security_tool`` 里那几条「动画跑完没有」的断言
（它们是按固定 pump 时长 + 卡阈值写的）。这是它们自带的老毛病，不是这里引入的 ——
把现有的 ``tests/test_server.py`` 放到 ``tests/test_security_tool.py`` 前面跑，一样能把它
打成 49/53。按文件名排序 ``test_security_tool`` < ``test_server`` < ``test_ui`` <
``test_ui_login_window``，所以这套排在最后，谁也不受影响。
"""
from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import Qt                                       # noqa: E402
from PyQt6.QtWidgets import QApplication                          # noqa: E402

from mchat.core.session import DEFAULT_SERVER, Session, SessionStore   # noqa: E402
from mchat.ui.login_window import (CARD_WIDTH, MODE_LOGIN, MODE_REGISTER,  # noqa: E402
                                   LoginWindow)

# 注意：**不要**在这里设 QT_QPA_PLATFORM=offscreen。这个环境变量是进程级的，pytest 又会
# 先把所有测试模块都 import 一遍 —— 一设就把 test_ui / test_security_tool 一起拽到无头
# 平台上，而它们靠**真实字体渲染**断言（无头平台没有中文字体，全成豆腐块）。
# 跟它们一样用原生平台 + WA_DontShowOnScreen：窗口不会弹出来，量到的几何也是用户看到的。


@pytest.fixture(scope="module")
def app():
    """整个模块共用一个 QApplication（多个会崩）。"""
    return QApplication.instance() or QApplication([])


def _pump(ms: int = 60) -> None:
    """走一趟事件循环。

    **为什么必须有**：窗口现在顶上有一条自绘标题条（无边框那套），根布局因此多了一层
    （标题条 + 内容容器）。``layout().activate()`` 只把布局**算**出来，子控件的几何要过
    一趟事件循环才真排定 —— 不 pump 就量，量到的是"还没排定"的中间态（实测字段间距
    1/0/1 这种），看着像排版坏了，其实只是没排完。
    """
    application = QApplication.instance()
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        application.processEvents()
        time.sleep(0.005)


def _window(tmp_path, mode=MODE_LOGIN, *, accounts=()):
    """搭一扇窗并**真的显示**（隐藏字段的量法不同：isVisible 会全是 False）。

    用 ``WA_DontShowOnScreen``：布局照常算，屏幕上不弹窗、不抢焦点。
    """
    store = SessionStore(str(tmp_path / "account.json"))
    for index, name in enumerate(accounts):
        store.save(Session(server=DEFAULT_SERVER, token=f"tok-{index}", user_id=f"u_{index}",
                           username=name, display_name=name))
    window = LoginWindow(session_store=store)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.set_mode(mode)
    window._on_mode_changed(mode)      # set_mode 不发信号，这里显式重排
    window.show()
    window.layout().activate()
    _pump()
    return window


def _visible_fields(window):
    return [field for field in window._fields if field.isVisible()]


def test_window_size_is_locked(app, tmp_path):
    """**尺寸定死**，且两种模式一模一样（切换时不跳、也拉不动）。"""
    window = _window(tmp_path, MODE_LOGIN)
    try:
        assert window.is_fixed_size(), (window.minimumSize(), window.maximumSize())
        login_size = (window.width(), window.height())
        assert login_size[0] >= CARD_WIDTH, login_size        # 至少装得下卡片
        assert login_size[1] < 900, login_size                # 别高到放不进屏幕
        window.set_mode(MODE_REGISTER)
        window._on_mode_changed(MODE_REGISTER)
        QApplication.processEvents()
        assert (window.width(), window.height()) == login_size, "换模式不许改窗口尺寸"
    finally:
        window.close()


def test_each_mode_shows_the_right_fields(app, tmp_path):
    window = _window(tmp_path, MODE_LOGIN)
    try:
        assert _visible_fields(window) == [window.server_field, window.username_field,
                                           window.password_field], "登录模式只该有三格"
        window.set_mode(MODE_REGISTER)
        window._on_mode_changed(MODE_REGISTER)
        QApplication.processEvents()
        assert _visible_fields(window) == list(window._fields), "注册模式六格都要出来"
    finally:
        window.close()


def test_password_fields_are_adjacent(app, tmp_path):
    """密码和确认密码必须挨着（昵称夹在中间是纯添乱）。"""
    window = _window(tmp_path, MODE_REGISTER)
    try:
        order = list(window._fields)
        assert order.index(window.confirm_field) == order.index(window.password_field) + 1
        assert order.index(window.display_field) == order.index(window.username_field) + 1
    finally:
        window.close()


def test_visible_widgets_stay_inside_the_window(app, tmp_path):
    """谁都不许被裁到窗口外面（字段、按钮、状态行都算）。"""
    for mode in (MODE_LOGIN, MODE_REGISTER):
        window = _window(tmp_path, mode)
        try:
            widgets = _visible_fields(window) + [window.submit_btn, window.test_btn,
                                                 window.status_label]
            for widget in widgets:
                top = widget.mapTo(window, widget.rect().topLeft()).y()
                assert top >= 0, f"{mode}：{widget} 被顶到窗口外面了"
                assert top + widget.height() <= window.height(), \
                    f"{mode}：{widget} 底部越界（{top + widget.height()} > {window.height()}）"
        finally:
            window.close()


def test_vertical_rhythm_is_even(app, tmp_path):
    """相邻元素的间距**处处相等**：这是"排版乱了"最直接的机器判据。"""
    for mode in (MODE_LOGIN, MODE_REGISTER):
        window = _window(tmp_path, mode)
        try:
            widgets = _visible_fields(window) + [window.remember_switch, window.submit_btn,
                                                 window.status_label, window.test_btn]
            tops = [widget.mapTo(window, widget.rect().topLeft()).y() for widget in widgets]
            bottoms = [top + widget.height() for top, widget in zip(tops, widgets)]
            gaps = [tops[index + 1] - bottoms[index] for index in range(len(tops) - 1)]
            assert gaps, "至少要有两个元素才能量间距"
            assert max(gaps) - min(gaps) <= 1, f"{mode} 间距不匀：{gaps}"
        finally:
            window.close()


def _focused_field(window):
    """现在焦点落在哪一格上。

    焦点其实在 ``FloatingLabelInput`` **内部**那个输入框上（``focus_field()`` 只把光标
    交给里面），所以从 ``focusWidget()`` 顺着父链往上找到所属的格子 —— 这样测试只依赖
    公开 API，不碰私有属性。
    """
    widget = window.focusWidget()
    while widget is not None:
        if widget in window._fields:
            return widget
        widget = widget.parentWidget()
    return None


def test_enter_moves_to_the_next_visible_field(app, tmp_path):
    """回车往下走：登录模式跳过藏起来的昵称/确认密码，注册模式一格一格往下。"""
    window = _window(tmp_path, MODE_LOGIN)
    try:
        window.username_field.setText("alice")                  # 用户名填好，免得先卡在它上面
        window.username_field.submitted.emit()                  # 模拟在用户名里按回车
        assert _focused_field(window) is window.password_field, "登录模式：用户名回车该进密码框"
        window.password_field.submitted.emit()                  # 最后一格 → 落到提交
        # 密码空着，提交会先在本地拦下（说明这一路真的走到了 submit，而不是卡在某格）
        assert "密码" in window.status_text(), window.status_text()
    finally:
        window.close()
    window = _window(tmp_path, MODE_REGISTER)
    try:
        window.username_field.submitted.emit()
        assert _focused_field(window) is window.display_field, "注册模式：用户名回车该进昵称"
        window.display_field.submitted.emit()
        assert _focused_field(window) is window.password_field, "注册模式：昵称回车该进密码"
        window.password_field.submitted.emit()
        assert _focused_field(window) is window.confirm_field, "注册模式：密码回车该进确认密码"
    finally:
        window.close()


def test_recent_account_chips_only_in_login_mode(app, tmp_path):
    """快捷标签：登录模式最多两个、注册模式整排藏起来。"""
    window = _window(tmp_path, MODE_LOGIN, accounts=("alice", "bob", "carol"))
    try:
        chips = [window.recent_row.itemAt(index).widget()
                 for index in range(window.recent_row.count())]
        assert len(chips) <= 2, f"标签最多两个，现在是 {len(chips)} 个"
        assert chips and all(chip.isVisible() for chip in chips)
        assert all(chip.maximumWidth() <= 200 for chip in chips), "标签不许撑破卡片"
        window.set_mode(MODE_REGISTER)
        window._on_mode_changed(MODE_REGISTER)
        QApplication.processEvents()
        assert not window.recent_box.isVisible(), "注册模式不该显示'上次登录的账号'"
    finally:
        window.close()
