"""核心逻辑层：不依赖 Qt，可离线单测。

    crypto.py      Base64 / AES-256-CBC 算法（原 mchat.crypto）
    models.py      领域模型：User / Message / Conversation + 时间文案
    store.py       会话与消息仓库：查询、未读、搜索、变更订阅
    transport.py   传输层接口 + 单机演示实现（未来接网络只替换这一层）
    seed.py        演示用示例会话与自动回复台词
"""
from __future__ import annotations

__all__ = ["crypto", "models", "store", "transport", "seed"]
