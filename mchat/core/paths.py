"""附件落地路径的安全规则 —— **只在这一个地方说了算**。

要解决的问题
------------
附件名是**发送者**定的，而它会参与拼本地落地路径。服务端那边虽然也洗了一遍名字，
客户端仍然必须自己再钉死一次：只"相信上游"的代码，碰上恶意服务端或老版本服务端
就是**任意文件写入** —— 实测过：对方把附件名写成
``..\\..\\..\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\evil.bat``
再发一条消息，本机什么都不点，文件就被写进了开机启动目录。

规则三条
--------
1. **只取最后一段**：``..\\..\\evil.bat``、``/etc/passwd``、``C:\\a\\b.png`` 全部只剩
   文件名本身；同时去掉控制字符（含 CR/LF/NUL）并限制长度。
2. **路径必须落在指定目录里**：拼完之后用 ``realpath`` 比一次（软链接/junction 也算），
   不满足就退回一个只用附件 id 组成的保守名字 —— 宁可名字难看，也不能写到目录外。
3. **Windows 保留名**（``CON`` ``NUL`` ``COM1`` …）前面加下划线：这些东西在 Windows 上
   指向设备，当普通文件名写会出各种怪事。

本模块零 Qt 依赖，可脱界面单测。
"""
from __future__ import annotations

import os

__all__ = [
    "MAX_NAME_CHARS",
    "safe_local_name",
    "is_inside",
    "resolve_attachment_path",
]

MAX_NAME_CHARS = 120          # 和 server/mchat_server.py 里的上限保持一致
FALLBACK_NAME = "附件"

# Windows 上这些名字是设备，不是文件（写进去会有奇怪行为，甚至不报错）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_local_name(name, fallback: str = FALLBACK_NAME) -> str:
    """把外部来的文件名收拾成"只有一个文件名"的安全形态。

    >>> safe_local_name(r"..\\..\\evil.bat")
    'evil.bat'
    >>> safe_local_name("/etc/passwd")
    'passwd'
    >>> safe_local_name("")
    '附件'
    """
    text = str(name or "")
    # 两种分隔符都换成正斜杠，然后只取最后一段
    text = text.replace("\\", "/").split("/")[-1]
    text = "".join(char for char in text
                   if char.isprintable() and char not in "\r\n\x00\x1b")
    text = text.strip()
    if len(text) > MAX_NAME_CHARS:
        # 截断时尽量保住后缀：界面和系统靠它认文件类型
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            text = stem[:MAX_NAME_CHARS - len(ext) - 1] + "." + ext
        else:
            text = text[:MAX_NAME_CHARS]
    if text in ("", ".", ".."):
        return fallback
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = "_" + text
    return text


def is_inside(folder: str, path: str) -> bool:
    """``path`` 是不是真的落在 ``folder`` 里面（相等不算，按 realpath 比）。

    两端都过 :func:`os.path.normcase`：Windows 上它折大小写（`C:\\Users\\H'S\\…`
    和 `c:\\users\\h's\\…` 是同一个地方）并把 `/` 统一成 `\\`；POSIX 上是恒等函数。
    少了这一步，"校验包含关系"会误伤正常路径（实测过：QStandardPaths 给的应用目录
    大小写和 realpath 规范化后的结果不一致，于是附件永远下不下来）。
    """
    try:
        root = os.path.normcase(os.path.realpath(os.path.abspath(folder)))
        target = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except OSError:                      # pragma: no cover - 极端路径才会走到
        return False
    if target == root:
        return False
    return target.startswith(root.rstrip(os.sep) + os.sep)


def resolve_attachment_path(folder: str, attachment_id: str, name: str) -> str:
    """算出一个附件该落到哪儿：``<folder>/<附件id>-<安全文件名>``，且保证在 folder 里。

    附件 id 前缀不只是为了好看 —— 它让"两个不同的人各发一个 报告.pdf"不会互相覆盖。

    **落地路径只能从这里出去**：路径本身没法自证清白（Windows 上
    ``<下载目录>/f_1-..\\..\\Startup\\evil.bat`` 是一条完全合法的路径，光看字符串
    分不出它是"恶意名字拼出来的"还是"本来就在 Startup 目录下"），所以必须由
    **知道目录是谁**的这一层来钉死 —— 就是这里。
    """
    root = os.path.abspath(folder)
    file_id = safe_local_name(attachment_id, fallback="file")
    leaf = f"{file_id}-{safe_local_name(name)}"
    candidate = os.path.join(root, leaf)
    if is_inside(root, candidate):
        return candidate
    # 理论上到不了这里（leaf 已经不含分隔符了），留一道兜底：只用 id 命名
    return os.path.join(root, f"{file_id}-attachment")
