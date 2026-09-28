"""MChat 启动入口。

    python main.py                # 直接运行
    uv run main.py                # 用 pyproject.toml 里的依赖运行
"""
from mchat.app import main

if __name__ == "__main__":
    raise SystemExit(main())
