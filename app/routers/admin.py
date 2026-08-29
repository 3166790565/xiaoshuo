"""后台：登录、上传导入、书籍管理、重新分章、章节编辑、解析设置。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import config, security
from ..paging import make_page
from ..services import importer, repo
from ..services import settings as app_settings
from ..templating import templates

router = APIRouter(prefix="/admin")
page_guard = Depends(security.require_admin_page)
api_guard = Depends(security.require_admin_api)


def _flash_redirect(path: str, message: str, ok: bool = True) -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    kind = "ok" if ok else "err"
    return RedirectResponse(
        f"{path}{separator}flash={quote(message)}&kind={kind}", status_code=303
    )


# ------------------------------------------------------------------ 登录


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = Query("/admin")):
    if security.is_admin(request):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        request, "admin/login.html", {"next": next, "error": ""}
    )


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/admin")):
    if not security.verify_password(password):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"next": next, "error": "密码不对"},
            status_code=401,
        )
    # 只允许跳回站内后台路径，挡开放重定向
    target = next if next.startswith("/admin") else "/admin"
    response = RedirectResponse(target, status_code=303)
    security.set_session_cookie(response, security.make_session_token())
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    security.clear_session_cookie(response)
    return response


# ------------------------------------------------------------------ 概览


@router.get("", response_class=HTMLResponse, dependencies=[page_guard])
def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "active": "home",
            "stats": repo.stats(),
            "mode_counts": repo.count_by_split_mode(),
            "jobs": importer.recent_jobs(8),
            "settings": app_settings.get_all(),
        },
    )


# ------------------------------------------------------------------ 上传导入


@router.get("/upload", response_class=HTMLResponse, dependencies=[page_guard])
def upload_form(request: Request, job: str = Query("")):
    return templates.TemplateResponse(
        request,
        "admin/upload.html",
        {
            "active": "upload",
            "settings": app_settings.get_all(),
            "jobs": importer.recent_jobs(8),
            "resume_job": job,
        },
    )


@router.post("/upload", dependencies=[api_guard])
async def upload(
    background: BackgroundTasks,
    files: list[UploadFile] = File(default_factory=list),
    target_chars: int = Form(0),
    force_mode: str = Form(""),
):
    """入队后台任务后立刻返回 job_id，前端轮询进度。"""
    uploads: list[tuple[str, bytes]] = []
    for item in files:
        data = await item.read()
        if data:
            uploads.append((item.filename or "未命名.txt", data))
    if not uploads:
        return JSONResponse({"ok": False, "message": "没有收到文件"}, status_code=400)

    job_id = importer.create_job(len(uploads))
    background.add_task(
        importer.run_import, job_id, uploads, force_mode, target_chars or None
    )
    return {"ok": True, "job_id": job_id, "files": len(uploads)}


@router.get("/api/jobs/{job_id}", dependencies=[api_guard])
def job_status(job_id: str):
    job = importer.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job


@router.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[page_guard])
def job_page(request: Request, job_id: str):
    job = importer.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return templates.TemplateResponse(
        request, "admin/job.html", {"active": "upload", "job": job}
    )


# ------------------------------------------------------------------ 书籍管理


@router.get("/books", response_class=HTMLResponse, dependencies=[page_guard])
def book_list(
    request: Request,
    page: int = Query(1, ge=1),
    split_mode: str = Query(""),
    q: str = Query(""),
    order: str = Query("updated"),
    flash: str = Query(""),
    kind: str = Query("ok"),
):
    keyword = q.strip()[:60]
    books, total = repo.list_books(
        page=page,
        page_size=config.ADMIN_PAGE_SIZE,
        split_mode=split_mode,
        keyword=keyword,
        order=order,
    )
    return templates.TemplateResponse(
        request,
        "admin/books.html",
        {
            "active": "books",
            "books": books,
            "page": make_page(page, config.ADMIN_PAGE_SIZE, total),
            "split_mode": split_mode,
            "q": keyword,
            "order": order,
            "flash": flash,
            "kind": kind,
            "settings": app_settings.get_all(),
        },
    )


@router.get("/books/{book_id}/edit", response_class=HTMLResponse, dependencies=[page_guard])
def book_edit_form(
    request: Request, book_id: int, flash: str = Query(""), kind: str = Query("ok")
):
    book = repo.get_book(book_id)
    if book is None:
        raise HTTPException(404, "书籍不存在")
    return templates.TemplateResponse(
        request,
        "admin/book_edit.html",
        {"active": "books", "book": book, "flash": flash, "kind": kind, "settings": app_settings.get_all()},
    )


@router.post("/books/{book_id}/edit", dependencies=[page_guard])
def book_edit(
    book_id: int,
    title: str = Form(""),
    author: str = Form(""),
    intro: str = Form(""),
    cover_url: str = Form(""),
    category: str = Form(""),
):
    if repo.get_book(book_id) is None:
        raise HTTPException(404, "书籍不存在")
    if not title.strip():
        return _flash_redirect(f"/admin/books/{book_id}/edit", "书名不能为空", ok=False)
    repo.update_book(
        book_id,
        {
            "title": title,
            "author": author or "未知",
            "intro": intro,
            "cover_url": cover_url,
            "category": category,
        },
    )
    return _flash_redirect(f"/admin/books/{book_id}/edit", "已保存")


@router.post("/books/{book_id}/delete", dependencies=[page_guard])
def book_delete(book_id: int):
    book = repo.get_book(book_id)
    if book is None:
        raise HTTPException(404, "书籍不存在")
    repo.delete_book(book_id)
    return _flash_redirect("/admin/books", f"已删除《{book['title']}》")


# ------------------------------------------------------------------ 重新分章


@router.post("/books/{book_id}/resplit", dependencies=[page_guard])
def resplit_single(
    book_id: int, force_mode: str = Form(""), target_chars: int = Form(0)
):
    if repo.get_book(book_id) is None:
        raise HTTPException(404, "书籍不存在")
    options = app_settings.load_options(force_mode, target_chars or None)
    result = importer.resplit_one(book_id, options)
    if result["status"] != "ok":
        return _flash_redirect(
            f"/admin/books/{book_id}/chapters", result.get("message", "重新分章失败"), ok=False
        )
    message = f"已重新分章：{result['mode_label']}，{result['chapters']} 章"
    if result.get("message"):
        message += f"（{result['message']}）"
    return _flash_redirect(f"/admin/books/{book_id}/chapters", message)


@router.post("/books/resplit", dependencies=[api_guard])
def resplit_batch(
    background: BackgroundTasks,
    ids: str = Form(""),
    split_mode: str = Form(""),
    q: str = Form(""),
    force_mode: str = Form(""),
    target_chars: int = Form(0),
):
    """ids 给了就按 ids，否则按当前筛选条件全量重跑。"""
    if ids.strip():
        selected = [int(part) for part in ids.replace(",", " ").split() if part.isdigit()]
    else:
        selected = repo.book_ids(split_mode=split_mode, keyword=q.strip())
    if not selected:
        return JSONResponse({"ok": False, "message": "没有匹配的书籍"}, status_code=400)

    job_id = importer.create_job(len(selected))
    background.add_task(
        importer.run_resplit, job_id, selected, force_mode, target_chars or None
    )
    return {"ok": True, "job_id": job_id, "books": len(selected)}


# ------------------------------------------------------------------ 章节


@router.get(
    "/books/{book_id}/chapters", response_class=HTMLResponse, dependencies=[page_guard]
)
def chapter_list(
    request: Request,
    book_id: int,
    page: int = Query(1, ge=1),
    flash: str = Query(""),
    kind: str = Query("ok"),
):
    book = repo.get_book(book_id)
    if book is None:
        raise HTTPException(404, "书籍不存在")
    chapters, total = repo.list_chapters(book_id, page, config.ADMIN_PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "admin/chapters.html",
        {
            "active": "books",
            "book": book,
            "chapters": chapters,
            "page": make_page(page, config.ADMIN_PAGE_SIZE, total),
            "flash": flash,
            "kind": kind,
            "settings": app_settings.get_all(),
        },
    )


@router.get(
    "/chapters/{chapter_id}/edit", response_class=HTMLResponse, dependencies=[page_guard]
)
def chapter_edit_form(
    request: Request, chapter_id: int, flash: str = Query(""), kind: str = Query("ok")
):
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "章节不存在")
    return templates.TemplateResponse(
        request,
        "admin/chapter_edit.html",
        {
            "active": "books",
            "chapter": chapter,
            "book": repo.get_book(int(chapter["book_id"])),
            "flash": flash,
            "kind": kind,
        },
    )


@router.post("/chapters/{chapter_id}/edit", dependencies=[page_guard])
def chapter_edit(chapter_id: int, title: str = Form(""), content: str = Form("")):
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(404, "章节不存在")
    if not content.strip():
        return _flash_redirect(
            f"/admin/chapters/{chapter_id}/edit", "正文不能为空", ok=False
        )
    repo.update_chapter(chapter_id, title, content)
    return _flash_redirect(f"/admin/chapters/{chapter_id}/edit", "已保存")


# ------------------------------------------------------------------ 解析设置


@router.get("/settings", response_class=HTMLResponse, dependencies=[page_guard])
def settings_form(request: Request, flash: str = Query(""), kind: str = Query("ok")):
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"active": "settings", "settings": app_settings.get_all(), "flash": flash, "kind": kind},
    )


@router.post("/settings", dependencies=[page_guard])
def settings_save(
    target_chars: int = Form(3000),
    strip_header_block: str = Form(""),
    fix_yao_variant: str = Form(""),
    add_subtitle: str = Form(""),
):
    app_settings.save(
        {
            "target_chars": max(500, min(50_000, target_chars)),
            "strip_header_block": bool(strip_header_block),
            "fix_yao_variant": bool(fix_yao_variant),
            "add_subtitle": bool(add_subtitle),
        }
    )
    return _flash_redirect("/admin/settings", "设置已保存，对之后的导入与重新分章生效")
