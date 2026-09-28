"""界面测试共用的小工具（tests/ 下多个测试文件都用它，避免各写一份）。

    pump(app, ms)              推进事件循环，让动画真正跑完
    glyph_pixels(win, widget)  在窗口截图里裁出控件区域，数深色/亮色像素
"""
from __future__ import annotations

from PyQt6.QtCore import QElapsedTimer, QPoint, QRect

__all__ = ["pump", "glyph_pixels"]


def pump(app, ms: int = 260) -> None:
    """推进事件循环 ``ms`` 毫秒。"""
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < ms:
        app.processEvents()


def glyph_pixels(window, widget) -> tuple[int, int, int, int]:
    """统计控件区域里的深色 / 亮色像素数，返回 ``(dark, light, darkest, lightest)``。

    用来验"文字颜色是否可读"这类只能靠像素说话的问题（例如暗色主题下输入框里的
    字是黑的，肉眼几乎看不见 —— 断言调色板不算数，得看真的画出来什么颜色）。

    两个坑：
    * 必须裁窗口的整幅截图，不能 `widget.grab()` —— 背景透明的控件单独抓会全黑；
    * 高分屏下 grab() 出的是设备像素（DPR 可能 1.75、2），裁剪矩形要乘 DPR，
      否则裁到的是完全不相干的位置。
    """
    top_left = widget.mapTo(window, QPoint(0, 0))
    pixmap = window.grab()
    ratio = pixmap.devicePixelRatio()
    rect = QRect(int(top_left.x() * ratio), int(top_left.y() * ratio),
                 int(widget.width() * ratio), int(widget.height() * ratio))
    image = pixmap.toImage().copy(rect)

    dark = light = 0
    darkest, lightest = 255, 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            luma = int(0.299 * color.red() + 0.587 * color.green()
                       + 0.114 * color.blue())
            darkest = min(darkest, luma)
            lightest = max(lightest, luma)
            if luma < 70:
                dark += 1
            elif luma > 185:
                light += 1
    return dark, light, darkest, lightest
