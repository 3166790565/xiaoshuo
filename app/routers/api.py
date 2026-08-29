"""阅读软件（开源阅读 / Legado）用的只读 JSON 接口。

统一返回 ``{"isSuccess": true, "data": ...}``；失败返回
``{"isSuccess": false, "errorMsg": "..."}``。

单章书走同一套路径：目录返回单元素数组，Legado 能正常处理。

鉴权：``API_TOKEN`` 未配置时公开（书源抓取通常需要免鉴权），
配置后要求 ``?token=`` 或 ``Authorization`` 头。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from .. import config
from ..security import check_api_token
from ..services import repo

router = APIRouter(prefix="/api", tags=["书源接口"], dependencies=[Depends(check_api_token)])


def _url(path: str) -> str:
    base = f"{config.SITE_BASE_URL}{path}"
    if not config.API_TOKEN:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={config.API_TOKEN}"


def ok(data: object) -> dict:
    return {"isSuccess": True, "data": data}


def fail(message: str, status_code: int = 404) -> JSONResponse:
    return JSONResponse({"isSuccess": False, "errorMsg": message}, status_code=status_code)


def book_brief(row: sqlite3.Row) -> dict:
    book_id = int(row["id"])
    return {
        "id": book_id,
        "name": row["title"],
        "author": row["author"],
        "intro": row["intro"],
        "coverUrl": row["cover_url"],
        "kind": row["category"],
        "wordCount": int(row["word_count"] or 0),
        "chapterCount": int(row["chapter_count"] or 0),
        "updateTime": row["updated_at"],
        "bookUrl": _url(f"/api/book/{book_id}"),
        "tocUrl": _url(f"/api/book/{book_id}/catalog"),
    }


@router.get("/search", summary="书名与作者模糊匹配")
def api_search(key: str = Query("", max_length=60), page: int = Query(1, ge=1)):
    rows, total = repo.list_books(
        page=page, page_size=config.API_PAGE_SIZE, keyword=key.strip(), order="updated"
    )
    return ok({"total": total, "page": page, "list": [book_brief(row) for row in rows]})


@router.get("/book/{book_id}", summary="书籍详情，含最新章节")
def api_book(book_id: int):
    row = repo.get_book(book_id)
    if row is None:
        return fail("书籍不存在")
    latest = repo.last_chapter(book_id)
    data = book_brief(row)
    data["latestChapterTitle"] = latest["title"] if latest else ""
    return ok(data)


@router.get("/book/{book_id}/catalog", summary="目录")
def api_catalog(book_id: int, page: int = Query(1, ge=1)):
    if repo.get_book(book_id) is None:
        return fail("书籍不存在")
    rows, total = repo.list_chapters(book_id, page, config.CATALOG_PAGE_SIZE)
    pages = max(1, -(-total // config.CATALOG_PAGE_SIZE))
    next_url = (
        _url(f"/api/book/{book_id}/catalog?page={page + 1}") if page < pages else ""
    )
    return ok(
        {
            "total": total,
            "page": min(page, pages),
            "pages": pages,
            "nextUrl": next_url,
            "list": [
                {
                    "id": int(row["id"]),
                    "idx": int(row["idx"]),
                    "title": row["title"],
                    "url": _url(f"/api/chapter/{int(row['id'])}"),
                }
                for row in rows
            ],
        }
    )


@router.get("/chapter/{chapter_id}", summary="章节正文，段落以 \\n 分隔")
def api_chapter(chapter_id: int):
    row = repo.get_chapter(chapter_id)
    if row is None:
        return fail("章节不存在")
    return ok(
        {
            "id": int(row["id"]),
            "bookId": int(row["book_id"]),
            "idx": int(row["idx"]),
            "title": row["title"],
            "wordCount": int(row["word_count"] or 0),
            "content": row["content"],
        }
    )
