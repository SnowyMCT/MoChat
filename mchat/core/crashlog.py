"""崩溃留痕：把"窗口突然消失"这类崩溃的现场写进文件。

为什么需要它
------------
这个程序偶发"什么都没提示、窗口直接没了"的崩溃（Qt 退出期/绘制期的段错误，
在真机探针里表现为退出码 127/139，连 Python 栈都没有）。没有现场就只能猜。
装上这个之后：

* **致命信号**（段错误、非法指令…）由 :mod:`faulthandler` 把当时所有线程的
  Python 栈写进日志 —— 段错误本身没有 Python 异常，这是唯一能拿到线索的办法；
* **未捕获的 Python 异常**（比如 Qt 槽里抛出来的）由 :func:`sys.excepthook` 记一份
  traceback，同时仍照常打到终端，不改变原有行为。

零 Qt 依赖，放在 core 里，测试可以直接调用。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import traceback

__all__ = ["install_crash_log", "default_crash_log_path", "CRASH_LOG_NAME"]

CRASH_LOG_NAME = "crash.log"
_HANDLES: list = []          # 别让文件对象被 GC：faulthandler 只留了 fd


def default_crash_log_path(data_dir: str) -> str:
    """日志跟数据库放一块（``%APPDATA%\\MChat\\MChat\\crash.log``）。"""
    return os.path.join(data_dir, CRASH_LOG_NAME)


def install_crash_log(path: str) -> str:
    """开启崩溃留痕，返回日志路径；多次调用是安全的（后一次接着写同一个文件）。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    handle = open(path, "a", buffering=1, encoding="utf-8")
    _HANDLES.append(handle)
    faulthandler.enable(file=handle, all_threads=True)

    def _hook(exc_type, exc_value, exc_tb) -> None:
        handle.write("\n=== 未捕获异常 ===\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=handle)
        handle.flush()
        sys.__excepthook__(exc_type, exc_value, exc_tb)      # 终端照常打印

    sys.excepthook = _hook
    return path
