"""开发用启动脚本：python run.py

先 import app.config，它会把 .env 灌进 os.environ，
这样下面读 HOST / PORT / RELOAD 才能拿到 .env 里的值。
"""

from __future__ import annotations

import os

import uvicorn

from app import config  # noqa: F401  仅为触发 .env 加载

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
