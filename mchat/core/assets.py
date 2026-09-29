"""演示附件的素材路径（**零 Qt 依赖**，可脱离界面单测）。

程序里唯二"自带"的图片/文件素材，都是 ``tools/build_demo_assets.py`` 生成的：

    mchat/assets/demo/配色预览.png     演示图片消息（气泡里的缩略图）
    mchat/assets/demo/评审材料-v3.pdf  演示文件消息（气泡里的文件卡片）

为什么要有它们：打开程序就能看到"图片消息 / 文件消息"长什么样，不用先自己找张图；
测试和截图复核也拿它们当真实附件用（不走网络、不写临时文件）。

路径规则和 ``mchat/ui/flags.py`` 里的 ``_assets_dir()`` 一致（那边由生成脚本产出、
不便共享代码，这里给附件素材单写一份）：**打包后先看 PyInstaller 的解包目录**，
源码运行时才是仓库里的 ``mchat/assets/``；两条分支都必须是绝对路径。
"""
from __future__ import annotations

import os
import sys

__all__ = ["assets_dir", "demo_dir", "demo_path", "demo_exists",
           "DEMO_DIRNAME", "DEMO_PHOTO", "DEMO_REPORT"]

DEMO_DIRNAME = "demo"
DEMO_PHOTO = "配色预览.png"           # 演示图片
DEMO_REPORT = "评审材料-v3.pdf"       # 演示文件（文件名带中文，顺便覆盖非 ASCII 路径）


def assets_dir() -> str:
    """素材根目录（绝对路径）：冻结后是解包目录，源码运行是 mchat/assets/。"""
    frozen_root = getattr(sys, "_MEIPASS", "")
    bundled = os.path.join(frozen_root, "mchat", "assets") if frozen_root else ""
    if bundled and os.path.isdir(bundled):
        return bundled
    # 兜底分支一定要拼成绝对路径：相对路径在冻结环境里会被解析到别的地方去
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets")


def demo_dir() -> str:
    return os.path.join(assets_dir(), DEMO_DIRNAME)


def demo_path(name: str) -> str:
    """演示素材的绝对路径（素材缺失时调用方自己判断，这里不抛）。"""
    return os.path.join(demo_dir(), name)


def demo_exists(name: str) -> bool:
    return os.path.isfile(demo_path(name))
