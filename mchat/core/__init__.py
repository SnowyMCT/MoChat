"""核心逻辑层：不依赖 Qt，可离线单测。

    crypto.py         Base64 / AES-256-CBC 算法（原 mchat.crypto）
    models.py         领域模型：User / Message / Conversation / Attachment + 时间文案
    store.py          会话与消息仓库：查询、未读、搜索、变更订阅
    db.py             本地历史库（SQLite + FTS5）：消息、草稿、会话旗标
    transport.py      传输层接口（界面只认 start / send / poll 三个动作）
    net_transport.py  真联网的实现（HTTP 长轮询 + 掉线重发 + 附件上传下载）
    api.py            服务端 HTTP 客户端 + 线上 JSON → 模型的转换
    session.py        账号登录态的本地缓存（"登一次就够了"）
"""
from __future__ import annotations

__all__ = ["crypto", "models", "store", "db", "transport", "net_transport",
           "api", "session"]
