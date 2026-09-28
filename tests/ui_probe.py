"""界面测试共用的小工具（tests/ 下多个测试文件都用它，避免各写一份）。

    pump(app, ms)                    推进事件循环，让动画真正跑完
    glyph_pixels(win, widget)        在窗口截图里裁出控件区域，数深色/亮色像素
    widget_pixel(win, widget, x, y)  取控件内某点在截图里的颜色（含 DPR 处理）
    drag_split_handle(handle, dx)    在分界线上做一次真实左键拖动
    double_click_handle(handle)      在分界线上做一次真实双击
"""
from __future__ import annotations

from PyQt6.QtCore import QElapsedTimer, QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

__all__ = ["pump", "wait_for", "glyph_pixels", "widget_pixel", "drag_split_handle",
           "double_click_handle", "press_handle", "release_handle",
           "colorful_pixels"]


def pump(app, ms: int = 260) -> None:
    """推进事件循环 ``ms`` 毫秒。"""
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < ms:
        app.processEvents()


def wait_for(app, predicate, *, timeout_ms: int = 2000, step_ms: int = 25) -> bool:
    """一边跑事件循环一边等 ``predicate()`` 成立，返回是否等到了。

    动画类断言别"睡固定时长 + 卡阈值"：机器慢一拍就偶发失败（本套就这么抖过两次，
    都是动画尾巴上差最后一帧）。等条件成立再断言，时间假设就从断言里拿掉了 ——
    断言强度不变（该 > 0.99 还是 > 0.99），确定性却回来了。
    """
    waited = 0
    while waited < timeout_ms:
        if predicate():
            return True
        pump(app, step_ms)
        waited += step_ms
    return predicate()


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


def colorful_pixels(window, widget, *, spread: int = 40) -> int:
    """数控件区域里的"彩色"像素（最大通道 - 最小通道 > spread）。

    验表情是不是真的用彩色字体画出来了：真正的彩色 emoji（Segoe UI Emoji）
    有大量高饱和像素；退化成单色字形或豆腐块时，这个数会掉到接近 0。
    """
    top_left = widget.mapTo(window, QPoint(0, 0))
    pixmap = window.grab()
    ratio = pixmap.devicePixelRatio()
    rect = QRect(int(top_left.x() * ratio), int(top_left.y() * ratio),
                 max(1, int(widget.width() * ratio)),
                 max(1, int(widget.height() * ratio)))
    image = pixmap.toImage().copy(rect)

    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if max(color.red(), color.green(), color.blue()) - \
                    min(color.red(), color.green(), color.blue()) > spread:
                count += 1
    return count


def widget_pixel(window, widget, local_x: int, local_y: int):
    """取截图里控件内部某点的颜色（返回 QColor）。

    和 :func:`glyph_pixels` 一样要处理两个坑：必须裁窗口整幅（控件单独
    ``grab()`` 会得到全黑），且裁剪坐标要乘 ``devicePixelRatio()``。
    """
    point = widget.mapTo(window, QPoint(int(local_x), int(local_y)))
    pixmap = window.grab()
    ratio = pixmap.devicePixelRatio()
    image = pixmap.toImage()
    x = max(0, min(image.width() - 1, int(round(point.x() * ratio))))
    y = max(0, min(image.height() - 1, int(round(point.y() * ratio))))
    return image.pixelColor(x, y)


def _send_mouse(widget, kind, local: QPoint, window_global: QPoint, button, buttons):
    """发一个带真实全局坐标的鼠标事件（走 Qt 的事件分发链路，不是直接调方法）。"""
    event = QMouseEvent(kind, QPointF(local), QPointF(window_global), button, buttons,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def _handle_point(handle, dx: int = 0) -> tuple[QPoint, QPoint]:
    """分界线上的取点：返回 (控件内局部坐标, 加上 dx 后的全局坐标)。"""
    y = max(1, handle.height() // 2)
    local = QPoint(3, y)
    return local, handle.mapToGlobal(local) + QPoint(dx, 0)


def press_handle(handle) -> None:
    """在分界线上按下左键（不松手），用于观察"拖动中"的绘制状态。"""
    local, _ = _handle_point(handle)
    _send_mouse(handle, QEvent.Type.MouseButtonPress, local,
                handle.mapToGlobal(local),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)


def release_handle(handle) -> None:
    """松开左键（配合 :func:`press_handle`）。"""
    local, _ = _handle_point(handle)
    _send_mouse(handle, QEvent.Type.MouseButtonRelease, local,
                handle.mapToGlobal(local),
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def drag_split_handle(handle, dx: int, *, steps: int = 4) -> None:
    """模拟一次真实拖动：按下 → 分若干步移到 ``dx`` 像素处 → 松开。

    判据是全局横坐标的位移（和用户拖动鼠标一样），所以分界线被移走、
    控件的绝对位置变了也不会影响结果。
    """
    local, _ = _handle_point(handle)
    press_handle(handle)
    for step in range(1, steps + 1):
        offset = round(dx * step / steps)
        _send_mouse(handle, QEvent.Type.MouseMove, local,
                    handle.mapToGlobal(local) + QPoint(offset, 0),
                    Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _send_mouse(handle, QEvent.Type.MouseMove, local,
                handle.mapToGlobal(local) + QPoint(dx, 0),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    release_handle(handle)


def double_click_handle(handle) -> None:
    """模拟一次真实双击（Qt 的顺序是 按下 → 双击 → 松开）。"""
    local, _ = _handle_point(handle)
    global_point = handle.mapToGlobal(local)
    _send_mouse(handle, QEvent.Type.MouseButtonPress, local, global_point,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _send_mouse(handle, QEvent.Type.MouseButtonDblClick, local, global_point,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _send_mouse(handle, QEvent.Type.MouseButtonRelease, local, global_point,
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
