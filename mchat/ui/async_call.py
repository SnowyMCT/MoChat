"""把"会阻塞的活"扔到后台线程，结果回到界面线程 —— 登录 / 建会话这类网络调用都用它。

为什么需要它
------------
登录、搜索用户、拉会话历史这些都要走网络，而网络**最短也是几十毫秒、最长十几秒**。
直接在界面线程里调 ``urllib`` 的后果是窗口"卡住不动"，用户会以为程序死了。
所以所有网络调用都走这里：后台线程干活，干完把结果交回主线程。

为什么不用 ``QTimer.singleShot(0, ...)`` 回主线程
------------------------------------------------
1. 在**没有事件循环**的线程里创建的定时器永远不会触发（后台线程就没有事件循环）；
2. PyQt6 里 ``QTimer.singleShot(0, 上下文对象, lambda)`` 这个三参数重载会**崩进程**
   （本项目在别处已经踩过，见 pyqt6 那条笔记）。

所以用最朴素也最可靠的办法：**信号**。Qt 的自动连接（AutoConnection）会在
"发射方线程 ≠ 接收方线程"时自动排队投递到接收方所在线程 —— 只要接收方（槽函数所属的
QObject，例如窗口自己）活在主线程，槽就一定在主线程里执行。这就是下面那条铁律的来历：

    **信号只能连接"活在主线程的 QObject 的绑定方法"，不要连裸函数/lambda，
      否则它会变成直接调用，等于把界面操作搬到了后台线程里。**

本模块零业务逻辑，只有这一个包装。
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, pyqtSignal

__all__ = ["AsyncCall"]


class AsyncCall(QObject):
    """一次后台调用：``run(fn)`` 起线程，成功发 ``finished``，失败发 ``failed``。"""

    finished = pyqtSignal(object)
    failed = pyqtSignal(object)          # 异常对象原样送出来（调用方自己认类型）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def run(self, work) -> bool:
        """开始干活；上一个还没干完就返回 False（按钮这时该是灰的）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self._body, args=(work,),
                                            name="mchat-async", daemon=True)
            self._thread.start()
            return True

    def _body(self, work) -> None:
        try:
            result = work()
        except BaseException as error:          # noqa: BLE001 —— 要原样交给界面判断
            self.failed.emit(error)
        else:
            self.finished.emit(result)

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())
