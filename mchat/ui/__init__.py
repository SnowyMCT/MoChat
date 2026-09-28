"""界面层：只负责"怎么画、怎么点"，业务规则都在 mchat.core 里。

    theme.py          颜色令牌（亮/暗）+ 全局 QSS
    icons.py          QPainter 画的矢量图标（不依赖图片资源）
    avatar.py         圆形头像（渐变底 + 首字 + 在线点）
    widgets.py        通用动效组件：悬浮标签输入框 / 分段控件 / 提示气泡 / 折叠区
    sidebar.py        左侧会话列表（搜索 + 未读徽标 + 工具箱入口）
    chat_view.py      右侧消息区（气泡、日期分隔条、输入栏）
    security_tool.py  安全工具箱：Base64 / AES 转换器（原 MChat 主界面）
    main_window.py    主窗口：把上面几块拼起来 + 主题 + 事件轮询
"""
from __future__ import annotations

__all__ = ["theme", "icons", "avatar", "widgets", "sidebar", "chat_view",
           "security_tool", "main_window"]
