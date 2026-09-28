"""MChat —— 一个 Telegram 风格的聊天软件（当前为单机演示），内置安全文本工具箱。

历史：最初由三份原型合并成一个编码/加密工具（Test15 Base64、Test16 AES-256、
Test17 动效输入框）。后来整体长成了聊天软件，那套转换器收进「工具箱」按钮里
当小功能，界面动效组件继续复用。

包结构
------
    mchat.core   纯逻辑层（零 Qt 依赖）：crypto / models / store / transport / seed
    mchat.ui     界面层：theme / icons / avatar / widgets / sidebar / chat_view
                 / security_tool / main_window
    mchat.app    启动入口（转出 main_window.main）

联网说明：现在跑的是单机演示，传输层是 ``core.transport.LocalTransport``；
要接真网络，实现同一个接口（start/send/poll）替换进去即可，界面层不用改。
"""

__version__ = "2.0.0"
__all__ = ["core", "ui", "app"]
