"""路径、密钥与可选开关的集中定义。

配置读取优先级：系统环境变量 > 项目根目录的 `.env` > 内置默认值。
密码与密钥两项没有默认值，缺了就每次启动随机生成并打印到日志。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"


def load_env_file(path: Path) -> list[str]:
    """把 `.env` 里的键值填进 os.environ，返回真正生效的键名。

    系统环境变量优先：已存在的键一律不覆盖。
    支持 `#` 整行注释、可选的 `export ` 前缀、成对的引号。
    不支持行尾注释——密码里带 `#` 很正常，截断了更麻烦。
    """
    if not path.is_file():
        return []
    applied: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
        applied.append(key)
    return applied


ENV_FILE = Path(os.environ.get("NOVEL_ENV_FILE") or BASE_DIR / ".env")
ENV_FILE_KEYS = load_env_file(ENV_FILE)

DATA_DIR = Path(os.environ.get("NOVEL_DATA_DIR") or BASE_DIR / "data")
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SCHEMA_PATH = APP_DIR / "schema.sql"
DB_PATH = Path(os.environ.get("NOVEL_DB_PATH") or DATA_DIR / "novel.db")

SITE_NAME = os.environ.get("SITE_NAME", "青灯书斋")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "一盏灯，一本书")
# 书源 JSON 与 API 自述地址都用它，部署后改成实际可访问地址
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# 管理员密码：没配就随机生成并打印到控制台，不留空密码后台
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
GENERATED_ADMIN_PASSWORD = ""
if not ADMIN_PASSWORD:
    GENERATED_ADMIN_PASSWORD = secrets.token_urlsafe(9)
    ADMIN_PASSWORD = GENERATED_ADMIN_PASSWORD

# 没配 SECRET_KEY 时每次启动都会换，等于重启即登出
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
GENERATED_SECRET_KEY = not SECRET_KEY
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
SESSION_COOKIE = "novel_admin"
SESSION_MAX_AGE = 7 * 24 * 3600

# /api/* 可选令牌；留空则公开（书源抓取通常需要免鉴权）
API_TOKEN = os.environ.get("API_TOKEN", "").strip()

# Telegram 爬频道用的应用凭证（在 my.telegram.org 申请）；不配则后台 TG 页面提示去配置
TG_API_ID = int(os.environ.get("TG_API_ID", "0") or "0")
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()

# 上传与解压防护上限
MAX_TXT_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000

SHELF_PAGE_SIZE = 24
CATALOG_PAGE_SIZE = 100
ADMIN_PAGE_SIZE = 30
API_PAGE_SIZE = 20


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
