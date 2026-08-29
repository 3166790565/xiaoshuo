"""FastAPI 装配：数据库初始化、静态与模板挂载、错误页。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db
from .routers import admin, api, site
from .security import AdminRedirect, redirect_to_login
from .templating import templates

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init_db()
    if config.ENV_FILE_KEYS:
        log.info("已从 %s 读取：%s", config.ENV_FILE, "、".join(config.ENV_FILE_KEYS))
    if config.GENERATED_ADMIN_PASSWORD:
        log.warning(
            "没有配置 ADMIN_PASSWORD，本次启动的随机管理员密码：%s"
            "（写进 %s 可长期固定）",
            config.GENERATED_ADMIN_PASSWORD,
            config.ENV_FILE,
        )
    if config.GENERATED_SECRET_KEY:
        log.warning("没有配置 SECRET_KEY，本次启动随机生成，重启后已登录的会话会失效")
    if not config.API_TOKEN:
        log.info("API_TOKEN 未设置，/api/* 为公开只读接口（书源抓取需要如此）")
    yield
    db.close_conn()


app = FastAPI(
    title=config.SITE_NAME,
    description="小说站：前台阅读 + 后台 txt 导入 + 书源 JSON 接口",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
app.include_router(site.router)
app.include_router(admin.router)
app.include_router(api.router)


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/") or "/api/" in request.url.path:
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.exception_handler(AdminRedirect)
async def on_admin_redirect(_: Request, exc: AdminRedirect):
    return redirect_to_login(exc.next_url)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail or "出错了"
    if _wants_json(request):
        return JSONResponse(
            {"isSuccess": False, "errorMsg": detail}, status_code=exc.status_code
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status": exc.status_code, "detail": detail},
        status_code=exc.status_code,
    )
