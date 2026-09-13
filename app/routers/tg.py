"""后台的 TG 频道页：账号登录、频道管理、自动同步设置、手动触发同步。

页面与表单提交沿用 admin.py 的 page_guard + _flash_redirect 风格；
登录与触发同步是前端 fetch 调的，走 api_guard，路径含 /api/ 让错误也回 JSON。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import security
from ..services import importer, telegram
from ..templating import templates
from .admin import _flash_redirect

router = APIRouter(prefix="/admin/telegram")
page_guard = Depends(security.require_admin_page)
api_guard = Depends(security.require_admin_api)

PAGE = "/admin/telegram"


def _page_context() -> dict:
    api_ready = telegram.api_ready()
    return {
        "active": "telegram",
        "api_ready": api_ready,
        "logged_in": api_ready and telegram.logged_in(),
        "phone": telegram.phone_number(),
        "pending_step": telegram.pending_step() if api_ready else "",
        "channels": telegram.list_channels(),
        "sync_interval": telegram.get_setting("tg_sync_interval", "0"),
        "sync_busy": telegram.sync_busy(),
        "jobs": importer.recent_jobs(8),
    }


@router.get("", response_class=HTMLResponse, dependencies=[page_guard])
def telegram_page(request: Request, restart: str = Query("")):
    if restart:
        telegram.drop_login()  # 「换个手机号」：丢弃进行中的登录流程
    return templates.TemplateResponse(request, "admin/telegram.html", _page_context())


# ------------------------------------------------------------------ 登录（fetch JSON）


@router.post("/api/login/send", dependencies=[api_guard])
def login_send(phone: str = Form("")):
    try:
        return telegram.login_send_code(phone)
    except (ValueError, telegram.TgNotLoggedIn) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.post("/api/login/verify", dependencies=[api_guard])
def login_verify(code: str = Form("")):
    try:
        return telegram.login_verify(code)
    except (ValueError, telegram.TgNotLoggedIn) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.post("/api/login/password", dependencies=[api_guard])
def login_password(password: str = Form("")):
    try:
        return telegram.login_password(password)
    except (ValueError, telegram.TgNotLoggedIn) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@router.post("/api/logout", dependencies=[api_guard])
def logout():
    return telegram.logout()


# ------------------------------------------------------------------ 频道管理（表单提交）


@router.post("/channels", dependencies=[page_guard])
def channel_add(link: str = Form("")):
    try:
        result = telegram.add_channel(link)
    except (ValueError, telegram.TgNotLoggedIn) as exc:
        return _flash_redirect(PAGE, str(exc), ok=False)
    return _flash_redirect(PAGE, f"已添加频道：{result['title']}")


@router.post("/channels/{channel_id}/delete", dependencies=[page_guard])
def channel_delete(channel_id: int):
    if telegram.get_channel(channel_id) is None:
        return _flash_redirect(PAGE, "频道不存在", ok=False)
    telegram.remove_channel(channel_id)
    return _flash_redirect(PAGE, "已移除频道")


@router.post("/channels/{channel_id}/toggle", dependencies=[page_guard])
def channel_toggle(channel_id: int):
    if telegram.get_channel(channel_id) is None:
        return _flash_redirect(PAGE, "频道不存在", ok=False)
    telegram.toggle_channel(channel_id)
    return _flash_redirect(PAGE, "已切换频道启用状态")


# ------------------------------------------------------------------ 自动同步设置


@router.post("/settings", dependencies=[page_guard])
def settings_save(sync_interval: int = Form(0)):
    interval = max(0, min(1440, sync_interval))
    telegram.set_setting("tg_sync_interval", str(interval))
    message = f"自动同步间隔已存为 {interval} 分钟" if interval else "自动同步已关闭"
    return _flash_redirect(PAGE, message)


# ------------------------------------------------------------------ 触发同步（fetch JSON）


@router.post("/api/sync", dependencies=[api_guard])
def sync(
    background: BackgroundTasks,
    channel_id: str = Form(""),
    force_mode: str = Form(""),
    target_chars: int = Form(0),
):
    """channel_id 为数字就同步单个频道，为 all 就把启用的频道串行跑一遍。"""
    if telegram.sync_busy():
        return JSONResponse({"ok": False, "message": "已有同步任务在进行，请稍后再试"}, status_code=409)

    if channel_id == "all":
        channels = telegram.enabled_channels()
        if not channels:
            return JSONResponse({"ok": False, "message": "没有启用的频道"}, status_code=400)
        entries = [(channel["id"], importer.create_job(0)) for channel in channels]
        background.add_task(telegram.sync_channels, entries, force_mode, target_chars or None)
        return {
            "ok": True,
            "job_id": entries[0][1],
            "job_ids": [job_id for _, job_id in entries],
            "count": len(entries),
        }

    if not channel_id.isdigit():
        return JSONResponse({"ok": False, "message": "参数不对"}, status_code=400)
    if telegram.get_channel(int(channel_id)) is None:
        return JSONResponse({"ok": False, "message": "频道不存在"}, status_code=404)

    job_id = importer.create_job(0)
    background.add_task(
        telegram.sync_channel, int(channel_id), job_id, force_mode, target_chars or None
    )
    return {"ok": True, "job_id": job_id, "job_ids": [job_id], "count": 1}
