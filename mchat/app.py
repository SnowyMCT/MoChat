"""启动入口。

    python main.py

主窗口与 :func:`main` 实际住在 :mod:`mchat.ui.main_window`，这里只是转出去，
让 ``from mchat.app import main`` 这种写法继续有效。直接运行本文件也能起来
（下面有 sys.path 兜底）。
"""
from __future__ import annotations

import os
import sys

try:                                   # 作为包的一部分被导入
    from .ui.main_window import SETTINGS_APP, SETTINGS_ORG, MainWindow, main
except ImportError:                    # 有人直接 python mchat/app.py
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from mchat.ui.main_window import (SETTINGS_APP, SETTINGS_ORG, MainWindow,
                                      main)

__all__ = ["MainWindow", "main", "SETTINGS_ORG", "SETTINGS_APP"]


if __name__ == "__main__":
    raise SystemExit(main())
