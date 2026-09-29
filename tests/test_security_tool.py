"""安全工具箱的界面测试（原 test_ui.py 迁移过来，断言原样保留）。

工具箱现在只是聊天软件里的一个按钮打开的窗口，所以这里既测"嵌入用的面板"
（SecurityToolWidget），也测"窗口包装"（SecurityToolDialog）。

用法：``python tests/test_security_tool.py``；不弹窗、不抢焦点，但走真实字体渲染。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import base64 as _b64                                                  # noqa: E402
import hashlib as _hl                                                  # noqa: E402

from Crypto.Cipher import AES as _AES                                  # noqa: E402
from Crypto.Util.Padding import pad as _pad                            # noqa: E402
from PyQt6.QtCore import QPoint, Qt                                       # noqa: E402
from PyQt6.QtGui import QPalette                                          # noqa: E402
from PyQt6.QtTest import QTest                                            # noqa: E402
from PyQt6.QtWidgets import QApplication, QToolButton                     # noqa: E402

from mchat.core import crypto                                             # noqa: E402
from mchat.ui.security_tool import (ALGORITHM_AES, ALGORITHM_BASE64,       # noqa: E402
                                    SecurityToolDialog,
                                    SecurityToolWidget)
from mchat.ui.theme import DARK, LIGHT                                    # noqa: E402
from ui_probe import glyph_pixels, pump, wait_for                           # noqa: E402

ARTIFACTS = os.path.join(ROOT, "artifacts")



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

def main() -> int:
    app = QApplication.instance() or QApplication([])
    clipboard = FakeClipboard()
    dialog = SecurityToolDialog(clipboard=clipboard)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.apply_palette(DARK)
    dialog.show()
    QApplication.setActiveWindow(dialog)   # 真实焦点事件可用（窗口不显示在屏幕上）
    pump(app, 400)
    win = dialog.tool                      # 断言直接打在面板上

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # --- 窗口包装 ---------------------------------------------------------
    check("对话框标题正确", dialog.windowTitle() == "安全工具箱 · MChat", dialog.windowTitle())
    check("对话框尺寸贴合面板",
          dialog.size().width() == win.width() and dialog.size().height() == win.height(),
          f"{dialog.size()} vs {win.size()}")
    check("对话框属性转发可用", dialog.output_text() == "" and dialog.tool is win)
    check("工具箱是独立顶层窗口（非模态）", dialog.isModal() is False)

    # --- 初始状态 ---------------------------------------------------------
    check("默认算法为 Base64", win.algorithm == ALGORITHM_BASE64, win.algorithm)
    check("默认模式为编码", win.mode == "编码", win.mode)
    check("密钥区默认收起", win.key_section.maximumHeight() == 0,
          str(win.key_section.maximumHeight()))
    check("初始输出框标签落在框内",
          wait_for(app, lambda: win.output_field.get_label_progress() < 0.05),
          str(win.output_field.get_label_progress()))

    # --- Base64 编码 ------------------------------------------------------
    sample = "你好 MChat 🎉"
    win.set_input(sample)
    pump(app)
    check("有内容后标签浮起",
          wait_for(app, lambda: win.input_field.get_label_progress() > 0.99),
          str(win.input_field.get_label_progress()))
    check("字符计数已更新", win.counter_label.text() == f"{len(sample)} 字符",
          win.counter_label.text())
    win.execute()
    pump(app)
    expected = crypto.encode_base64(sample)
    check("Base64 编码结果正确", win.output_text() == expected, win.output_text())
    check("编码成功提示", "编码完成" in win.status_text(), win.status_text())

    win.copy_output()
    pump(app, 120)
    check("复制到剪贴板（写的是注入的那个剪贴板）",
          clipboard.text() == expected, repr(clipboard.text()))

    # --- Base64 解码（切换模式保留输入） ----------------------------------
    win.select_mode(1)
    pump(app, 200)
    check("切换模式后输入框内容原样保留",
          win.input_field.text() == sample, f"'{win.input_field.text()}'")
    check("切换模式后字符计数也跟着留着",
          win.counter_label.text() == f"{len(sample)} 字符", win.counter_label.text())
    check("切换模式后上一轮结果被收掉（标签落回提示）",
          win.output_text() == "" and win.output_field.label_text() == "结果会显示在这里",
          f"'{win.output_text()}' / {win.output_field.label_text()}")
    check("切换模式后按钮文案联动", win.action_btn.text() == "执 行 解 码",
          win.action_btn.text())
    win.set_input("这不是 Base64!!!")
    win.execute()
    pump(app)
    check("非法密文被拦截", "Base64" in win.status_text(), win.status_text())
    win.set_input(expected)
    win.execute()
    pump(app)
    check("Base64 解码结果正确", win.output_text() == sample, win.output_text())

    # --- 切到 AES ---------------------------------------------------------
    win.select_algorithm(1)
    pump(app, 320)
    check("AES 模式选项联动", win.mode_seg.options() == ["加密", "解密"],
          str(win.mode_seg.options()))
    # 回归：set_options 曾因 deleteLater 延迟回收，导致新旧按钮文字叠画在一起
    live = [b for b in win.mode_seg.findChildren(QToolButton)
            if b.parent() is win.mode_seg and b.isVisible()]
    check("分段控件切换后无残留按钮", len(live) == 2, f"{len(live)} 个")
    check("AES 模式密钥区展开", win.key_section.maximumHeight() > 0,
          str(win.key_section.maximumHeight()))
    check("AES 模式按钮文案", win.action_btn.text() == "执 行 加 密", win.action_btn.text())
    pump(app, 200)
    # 需求回归：两种方式（Base64 / AES-256）互相切换时，输入框里的字符保留不变。
    # 切算法会把模式标签整套换掉（编码/解码 -> 加密/解密），分段控件重新赋值会再发一次
    # valueChanged，以前就是这一下把输入框清空的。
    check("Base64 → AES-256：输入框里的字一个没少",
          win.input_field.text() == expected, f"'{win.input_field.text()}'")
    check("切过去后按键计数也还在（不是只剩个空框）",
          win.counter_label.text() == f"{len(expected)} 字符", win.counter_label.text())

    win.set_input("绝密消息", "口令123")
    win.execute()
    pump(app)
    cipher = win.output_text()
    check("AES 加密结果可自解", crypto.aes_decrypt(cipher, "口令123") == "绝密消息")
    check("加密结果为随机 IV 格式", crypto.detect_format(cipher).name == "MChat v1 格式")
    check("加密提示含随机 IV", "随机 IV" in win.status_text(), win.status_text())

    win.select_algorithm(0)                    # 切回 Base64
    pump(app, 320)
    check("AES-256 → Base64：输入框里的字依然原样保留",
          win.input_field.text() == "绝密消息", f"'{win.input_field.text()}'")
    # 收起是动画：断言"收干净了"必须等落定，不能拿固定 pump 后的 maximumHeight() 卡 0
    # （机器忙的时候折叠还没跑完，这一条会偶发翻车）
    check("切回 Base64 后密钥区收起",
          wait_for(app, lambda: win.key_section.maximumHeight() == 0),
          str(win.key_section.maximumHeight()))
    check("切回 Base64 后选项换回编码/解码",
          win.mode_seg.options() == ["编码", "解码"], str(win.mode_seg.options()))
    check("切方式只收结果，不动输入（输出框清空 + 标签落回提示）",
          win.output_text() == "" and win.output_field.label_text() == "结果会显示在这里",
          f"'{win.output_text()}' / {win.output_field.label_text()}")
    win.select_algorithm(1)                    # 后面的用例接着按 AES 走
    win.set_input("绝密消息", "口令123")
    win.execute()
    pump(app)
    cipher = win.output_text()

    # --- AES 解密 + 错误密钥 ---------------------------------------------
    win.select_mode(1)
    pump(app)
    win.set_input(cipher, "错误口令")
    win.execute()
    pump(app)
    check("错误密钥给出提示", "解密失败" in win.status_text(), win.status_text())
    win.set_input(cipher, "口令123")
    win.execute()
    pump(app)
    check("AES 解密结果正确", win.output_text() == "绝密消息", win.output_text())
    check("解密提示标明格式", "MChat v1" in win.status_text(), win.status_text())

    # --- Test16 旧格式互通 ------------------------------------------------
    key = _hl.sha256("口令123".encode()).digest()
    legacy = _b64.b64encode(
        _AES.new(key, _AES.MODE_CBC, b"1234567890123456").encrypt(
            _pad("老格式密文".encode(), 16))
    ).decode()
    win.set_input(legacy, "口令123")
    win.execute()
    pump(app)
    check("兼容解密 Test16 旧格式", win.output_text() == "老格式密文", win.output_text())
    check("旧格式提示正确", "Test16" in win.status_text(), win.status_text())

    # --- 空输入拦截 -------------------------------------------------------
    win.set_input("")
    win.execute()
    pump(app)
    check("空输入被拦截", "不能为空" in win.status_text(), win.status_text())

    # --- 清空 -------------------------------------------------------------
    win.clear_all()
    pump(app, 200)
    check("清空按钮生效", win.input_field.text() == "" and win.output_text() == "")
    check("清空后输出标签落回框内",
          wait_for(app, lambda: win.output_field.get_label_progress() < 0.05),
          str(win.output_field.get_label_progress()))
    # 回归：只读框的标签底色要和输入区一致，否则会露出一条色带
    check("只读框标签底色与输入区一致",
          win.output_field._label_bg == DARK.card_alt, str(win.output_field._label_bg))

    # --- 标签浮起 / 点空白处收回 -------------------------------------------
    win.input_field.focus_field()
    pump(app, 300)
    check("聚焦时标签浮起",
          wait_for(app, lambda: win.input_field.get_label_progress() > 0.99),
          str(win.input_field.get_label_progress()))
    check("窗口留白处不属于任何子控件", win.childAt(QPoint(12, 12)) is None)
    QTest.mouseClick(win, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(12, 12))
    pump(app, 340)
    check("点空白处输入框失焦", not win.input_field._focused,
          str(win.input_field._focused))
    # 阈值给到 0.05 而不是 0.01：170ms 的动画尾巴上偶尔差最后一帧（实测抖动过一次）
    check("失焦且为空时标签收回",
          wait_for(app, lambda: win.input_field.get_label_progress() < 0.05),
          str(win.input_field.get_label_progress()))
    # 回归：点输入框本身照常聚焦，不能被"点空白失焦"吃掉。
    # 这里不用 QTest.mouseClick 投合成点击：在 WA_DontShowOnScreen 的窗口上它不走
    # Qt 的命中测试/点击获焦链路，所以改用两条确定性的等价断言。
    inner = win.input_field._field
    field_pt = win.input_field.mapTo(win, QPoint(30, 30))
    hit = win.childAt(field_pt)
    check("该坐标命中输入控件", hit in (inner, inner.viewport()),
          str(type(hit).__name__))
    inner.setFocus(Qt.FocusReason.MouseFocusReason)
    pump(app, 300)
    check("输入框可重新聚焦", win.input_field._focused, str(win.input_field._focused))
    check("重新聚焦后标签浮起",
          wait_for(app, lambda: win.input_field.get_label_progress() > 0.99),
          str(win.input_field.get_label_progress()))

    # --- 快捷键：Ctrl+Enter 执行 ------------------------------------------
    win.select_algorithm(0)
    win.select_mode(0)
    pump(app, 200)
    win.set_input("快捷键测试")
    QTest.keyClick(win, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    pump(app, 200)
    check("Ctrl+Enter 触发执行", win.output_text() == crypto.encode_base64("快捷键测试"),
          win.output_text())

    # --- 主题由外部驱动 ---------------------------------------------------
    dialog.apply_palette(DARK)
    pump(app, 200)
    check("面板样式表跟着主题变", "#17212B" in win.styleSheet() or DARK.surface in win.styleSheet(),
          win.styleSheet()[:60])
    # 回归：输入框的字色不能退回系统默认（暗色下就是黑字，落在深色底上看不见）
    check("暗色下工具箱输入框字色为浅色",
          win.input_field._field.palette().color(QPalette.ColorRole.Text).name().lower()
          == DARK.text.lower(),
          win.input_field._field.palette().color(QPalette.ColorRole.Text).name())
    win.set_input("暗色下的待加密文本", "口令")
    win.select_algorithm(1)
    pump(app, 300)
    _, light_px, darkest, lightest = glyph_pixels(dialog, win.input_field)
    # 判断依据用"最亮/最暗"而不是亮像素计数：中文字形笔画细，抗锯齿后大量像素落在
    # 中间调，计数阈值不稳；而"最亮 ≥ 浅色字亮度、最暗不低于底色"是稳的。
    check("暗色下工具箱输入框画出来是浅色字",
          lightest >= 150 and darkest >= 30,
          f"亮={light_px} 最亮={lightest} 最暗={darkest}")
    win.clear_all()
    win.select_algorithm(0)
    pump(app, 300)

    # --- 截图存档 ---------------------------------------------------------
    os.makedirs(ARTIFACTS, exist_ok=True)
    win.select_algorithm(1)
    win.select_mode(0)
    pump(app, 360)
    win.set_input("今天下午三点，老地方见 —— 别忘了带上那份文件。", "我的口令")
    win.execute()
    pump(app, 320)
    check("截图态加密成功", "加密完成" in win.status_text(), win.status_text())
    for tag in ("dark", "light"):
        dialog.apply_palette(DARK if tag == "dark" else LIGHT)
        pump(app, 320)
        path = os.path.join(ARTIFACTS, f"tool_{tag}.png")
        dialog.grab().save(path)
        print(f"  shot  {path}")

    # --- 报告 -------------------------------------------------------------
    # 收尾务必关窗：留着可见窗口的话，脚本退出时 Qt 可能挂住不返回
    #（曾有一个探针脚本因此变成僵尸进程让 ps 一直看得到）。
    dialog.close()
    dialog.deleteLater()
    app.processEvents()

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}" +
              (f"  -> {detail}" if detail and not ok else ""))
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


def test_security_tool_suite() -> None:
    """让 ``pytest tests`` 也能跑这套脚本式用例（本地直接运行 main() 同样可行）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
