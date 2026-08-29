"""让 pytest 能直接 import app 包，并把测试用的数据库指到临时目录。

这些环境变量必须在 app.config 被导入之前设好，所以放在 conftest 模块顶层。
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="novel-test-")

# 指向一个不存在的路径，免得开发机上真实的 .env 影响测试
os.environ.setdefault("NOVEL_ENV_FILE", os.path.join(_TMP, "absent.env"))
os.environ.setdefault("NOVEL_DATA_DIR", _TMP)
os.environ.setdefault("NOVEL_DB_PATH", os.path.join(_TMP, "novel.db"))
os.environ.setdefault("ADMIN_PASSWORD", "test-password-123")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SITE_BASE_URL", "http://testserver")
os.environ.setdefault("API_TOKEN", "")
