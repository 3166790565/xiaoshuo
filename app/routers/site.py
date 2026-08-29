"""前台页面：书架、书籍详情、阅读页、搜索。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import config
from ..paging import make_page
from ..services import booksource, repo
from ..templating import templates

router = APIRouter()


@router.get("/legado/源.json", summary="按当前配置生成的开源阅读书源")
def legado_source():
    return JSONResponse(
        booksource.build(),
        headers={"Content-Disposition": 'inline; filename="source.json"'},
    )


@router.get("/healthz", include_in_schema=False)
def healthz():
    """容器与反向代理的存活探针：库能打开就算活着，坏了让它抛 500。"""
    return JSONResponse({"ok": True, "books": repo.ping()})


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    recent, _ = repo.list_books(page=1, page_size=12, order="updated")
    longest, _ = repo.list_books(page=1, page_size=6, order="words")
    return templates.TemplateResponse(
        request,
        "index.html",
        {"recent": recent, "longest": longest, "stats": repo.stats()},
    )


@router.get("/books", response_class=HTMLResponse)
def shelf(
    request: Request,
    page: int = Query(1, ge=1),
    order: str = Query("updated"),
    q: str = Query(""),
):
    keyword = q.strip()[:60]
    books, total = repo.list_books(
        page=page, page_size=config.SHELF_PAGE_SIZE, keyword=keyword, order=order
    )
    return templates.TemplateResponse(
        request,
        "shelf.html",
        {
            "books": books,
            "page": make_page(page, config.SHELF_PAGE_SIZE, total),
            "order": order,
            "q": keyword,
        },
    )


@router.get("/book/{book_id}", response_class=HTMLResponse)
def book_detail(request: Request, book_id: int, page: int = Query(1, ge=1)):
    book = repo.get_book(book_id)
    if book is None:
        raise HTTPException(404, "这本书不存在")

    single = int(book["chapter_count"] or 0) <= 1
    if single:  # 单章书隐藏目录，直接开读
        chapters, total = [], int(book["chapter_count"] or 0)
    else:
        chapters, total = repo.list_chapters(book_id, page, config.CATALOG_PAGE_SIZE)

    return templates.TemplateResponse(
        request,
        "book.html",
        {
            "book": book,
            "chapters": chapters,
            "single": single,
            "first_idx": repo.first_chapter_idx(book_id),
            "page": make_page(page, config.CATALOG_PAGE_SIZE, total),
        },
    )


@router.get("/book/{book_id}/read", response_class=HTMLResponse)
def read_first(book_id: int):
    idx = repo.first_chapter_idx(book_id)
    if idx is None:
        raise HTTPException(404, "这本书还没有正文")
    return RedirectResponse(f"/book/{book_id}/read/{idx}", status_code=302)


@router.get("/book/{book_id}/read/{idx}", response_class=HTMLResponse)
def read(request: Request, book_id: int, idx: int):
    chapter = repo.get_chapter_by_idx(book_id, idx)
    if chapter is None:
        raise HTTPException(404, "这一章不存在")
    book = repo.get_book(book_id)
    if book is None:
        raise HTTPException(404, "这本书不存在")

    prev_row, next_row = repo.neighbors(book_id, idx)
    return templates.TemplateResponse(
        request,
        "reader.html",
        {
            "book": book,
            "chapter": chapter,
            "paragraphs": [p for p in chapter["content"].split("\n") if p],
            "prev": prev_row,
            "next": next_row,
            "single": int(book["chapter_count"] or 0) <= 1,
        },
    )


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = Query(""),
    mode: str = Query("title"),
    page: int = Query(1, ge=1),
):
    keyword = q.strip()[:60]
    mode = "full" if mode == "full" else "title"
    books: list = []
    hits: list = []
    total = 0

    if keyword:
        if mode == "full":
            hits, total = repo.search_chapters(keyword, page, config.API_PAGE_SIZE)
        else:
            books, total = repo.search_books(keyword, page, config.API_PAGE_SIZE)

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": keyword,
            "mode": mode,
            "books": books,
            "hits": hits,
            "total": total,
            "page": make_page(page, config.API_PAGE_SIZE, total),
            "fts_min": repo.FTS_MIN_CHARS,
        },
    )
