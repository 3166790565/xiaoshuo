"""书籍与章节的数据访问。"""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import datetime

from .. import db
from .txt_parser import ParsedBook

BOOK_COLUMNS = (
    "id, title, author, intro, cover_url, category, word_count, "
    "chapter_count, split_mode, source_filename, created_at, updated_at"
)
SPLIT_MODES = ("marked", "single", "auto")
SPLIT_MODE_LABELS = {"marked": "标记分章", "single": "整本单章", "auto": "按字数分节"}

_WS = re.compile(r"\s+")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_intro(parsed: ParsedBook, limit: int = 140) -> str:
    if not parsed.chapters:
        return ""
    text = _WS.sub("", parsed.chapters[0].content)
    return text[:limit] + ("…" if len(text) > limit else "")


# ------------------------------------------------------------------ 写入


def _insert_chapters(conn: sqlite3.Connection, book_id: int, parsed: ParsedBook) -> None:
    conn.executemany(
        "INSERT INTO chapters(book_id, idx, title, content, word_count) VALUES(?,?,?,?,?)",
        [(book_id, c.idx, c.title, c.content, c.word_count) for c in parsed.chapters],
    )


def insert_book(parsed: ParsedBook, source_filename: str, content_hash: str) -> int:
    ts = now()
    with db.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO books(title, author, intro, cover_url, category, word_count,
                                 chapter_count, split_mode, source_filename, content_hash,
                                 created_at, updated_at)
               VALUES(?,?,?,'','',?,?,?,?,?,?,?)""",
            (parsed.title, parsed.author, make_intro(parsed), parsed.word_count,
             len(parsed.chapters), parsed.split_mode, source_filename, content_hash, ts, ts),
        )
        book_id = int(cursor.lastrowid or 0)
        _insert_chapters(conn, book_id, parsed)
    return book_id


def replace_chapters(book_id: int, parsed: ParsedBook) -> None:
    """重新分章：换掉章节，书名作者等人工改过的字段保持不动。"""
    with db.transaction() as conn:
        conn.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
        _insert_chapters(conn, book_id, parsed)
        conn.execute(
            "UPDATE books SET word_count=?, chapter_count=?, split_mode=?, updated_at=? "
            "WHERE id=?",
            (parsed.word_count, len(parsed.chapters), parsed.split_mode, now(), book_id),
        )


EDITABLE_FIELDS = ("title", "author", "intro", "cover_url", "category")


def update_book(book_id: int, fields: dict[str, str]) -> None:
    changes = {k: (v or "").strip() for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not changes:
        return
    assignments = ", ".join(f"{key} = ?" for key in changes)
    db.execute(
        f"UPDATE books SET {assignments}, updated_at = ? WHERE id = ?",
        tuple(changes.values()) + (now(), book_id),
    )


def update_chapter(chapter_id: int, title: str, content: str) -> None:
    """正文按行传入，统一成"段落之间一个 \\n"的存储格式。"""
    paragraphs = [line.strip() for line in content.replace("\r\n", "\n").split("\n")]
    body = "\n".join(p for p in paragraphs if p)
    from .txt_parser import count_words

    db.execute(
        "UPDATE chapters SET title = ?, content = ?, word_count = ? WHERE id = ?",
        (title.strip() or "无题", body, count_words(body), chapter_id),
    )
    row = db.query_one("SELECT book_id FROM chapters WHERE id = ?", (chapter_id,))
    if row:
        recount_book(int(row["book_id"]))


def recount_book(book_id: int) -> None:
    db.execute(
        """UPDATE books SET
              word_count = COALESCE((SELECT SUM(word_count) FROM chapters WHERE book_id = ?), 0),
              chapter_count = (SELECT COUNT(*) FROM chapters WHERE book_id = ?),
              updated_at = ?
           WHERE id = ?""",
        (book_id, book_id, now(), book_id),
    )


def delete_book(book_id: int) -> None:
    # chapters 由外键级联删除，FTS 索引由 trigger 同步
    db.execute("DELETE FROM books WHERE id = ?", (book_id,))


# ------------------------------------------------------------------ 查询


def ping() -> int:
    """健康检查用：确认库能打开、schema 在，返回书籍数。

    只查 books 的行数，不碰 chapters 与 FTS 表，几十 G 的库也是毫秒级。
    """
    return int(db.scalar("SELECT COUNT(*) FROM books"))


_ORDERS = {
    "updated": "updated_at DESC, id DESC",
    "created": "created_at DESC, id DESC",
    "title": "title ASC, id ASC",
    "words": "word_count DESC, id DESC",
}


def list_books(
    page: int = 1,
    page_size: int = 24,
    split_mode: str = "",
    keyword: str = "",
    order: str = "updated",
) -> tuple[list[sqlite3.Row], int]:
    conditions: list[str] = []
    params: list[object] = []
    if split_mode in SPLIT_MODES:
        conditions.append("split_mode = ?")
        params.append(split_mode)
    if keyword:
        conditions.append("(title LIKE ? OR author LIKE ?)")
        params += [f"%{keyword}%"] * 2
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    total = int(db.scalar(f"SELECT COUNT(*) FROM books {where}", tuple(params)))
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)  # 越界页码收回最后一页，不返回空列表
    order_sql = _ORDERS.get(order, _ORDERS["updated"])
    rows = db.query(
        f"SELECT {BOOK_COLUMNS} FROM books {where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        tuple(params) + (page_size, (page - 1) * page_size),
    )
    return rows, total


def get_book(book_id: int) -> sqlite3.Row | None:
    return db.query_one(f"SELECT {BOOK_COLUMNS} FROM books WHERE id = ?", (book_id,))


def get_book_by_hash(content_hash: str) -> sqlite3.Row | None:
    return db.query_one("SELECT id, title FROM books WHERE content_hash = ?", (content_hash,))


def stats() -> dict[str, int]:
    return {
        "books": int(db.scalar("SELECT COUNT(*) FROM books")),
        "chapters": int(db.scalar("SELECT COUNT(*) FROM chapters")),
        "words": int(db.scalar("SELECT COALESCE(SUM(word_count),0) FROM books")),
    }


def count_by_split_mode() -> dict[str, int]:
    counts = {mode: 0 for mode in SPLIT_MODES}
    for row in db.query("SELECT split_mode, COUNT(*) AS n FROM books GROUP BY split_mode"):
        counts[row["split_mode"] or "unknown"] = int(row["n"])
    return counts


def book_ids(split_mode: str = "", keyword: str = "") -> list[int]:
    """批量重新分章要用的 id 清单，条件与后台列表保持一致。"""
    conditions: list[str] = []
    params: list[object] = []
    if split_mode in SPLIT_MODES:
        conditions.append("split_mode = ?")
        params.append(split_mode)
    if keyword:
        conditions.append("(title LIKE ? OR author LIKE ?)")
        params += [f"%{keyword}%"] * 2
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return [int(row["id"]) for row in db.query(f"SELECT id FROM books {where} ORDER BY id", tuple(params))]


def list_chapters(
    book_id: int, page: int = 1, page_size: int = 0
) -> tuple[list[sqlite3.Row], int]:
    total = int(db.scalar("SELECT COUNT(*) FROM chapters WHERE book_id = ?", (book_id,)))
    sql = "SELECT id, idx, title, word_count FROM chapters WHERE book_id = ? ORDER BY idx"
    if page_size > 0:
        pages = max(1, -(-total // page_size))
        page = min(max(1, page), pages)
        rows = db.query(sql + " LIMIT ? OFFSET ?", (book_id, page_size, (page - 1) * page_size))
    else:
        rows = db.query(sql, (book_id,))
    return rows, total


def get_chapter(chapter_id: int) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT id, book_id, idx, title, content, word_count FROM chapters WHERE id = ?",
        (chapter_id,),
    )


def get_chapter_by_idx(book_id: int, idx: int) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT id, book_id, idx, title, content, word_count "
        "FROM chapters WHERE book_id = ? AND idx = ?",
        (book_id, idx),
    )


def first_chapter_idx(book_id: int) -> int | None:
    value = db.scalar("SELECT MIN(idx) FROM chapters WHERE book_id = ?", (book_id,), None)
    return int(value) if value is not None else None


def last_chapter(book_id: int) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT id, idx, title FROM chapters WHERE book_id = ? ORDER BY idx DESC LIMIT 1",
        (book_id,),
    )


def neighbors(book_id: int, idx: int) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    prev_row = db.query_one(
        "SELECT idx, title FROM chapters WHERE book_id = ? AND idx < ? ORDER BY idx DESC LIMIT 1",
        (book_id, idx),
    )
    next_row = db.query_one(
        "SELECT idx, title FROM chapters WHERE book_id = ? AND idx > ? ORDER BY idx ASC LIMIT 1",
        (book_id, idx),
    )
    return prev_row, next_row


def chapter_source_text(book_id: int) -> str:
    """把库里的正文拼回"可再次解析"的文本，供重新分章使用。

    段落之间留空行，避免二次解析时把段落错误地当作硬换行合并。
    marked 书才把章名写回去，auto/single 的章名是生成的，写回去会污染标记识别。
    """
    book = db.query_one("SELECT split_mode FROM books WHERE id = ?", (book_id,))
    keep_titles = bool(book) and book["split_mode"] == "marked"
    parts: list[str] = []
    for row in db.query(
        "SELECT title, content FROM chapters WHERE book_id = ? ORDER BY idx", (book_id,)
    ):
        if keep_titles:
            parts.append(row["title"])
        parts.append(row["content"].replace("\n", "\n\n"))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ 搜索

_HL_OPEN = "\x02"
_HL_CLOSE = "\x03"
FTS_MIN_CHARS = 3  # trigram 分词器要求匹配串至少 3 字


def _fts_match(keyword: str) -> str:
    """整串当短语匹配，内部双引号需转义，避免用户输入变成 FTS 语法。"""
    return '"' + keyword.replace('"', '""') + '"'


def highlight(fragment: str) -> str:
    """先转义再把哨兵换成 <mark>，模板里可以放心 |safe。"""
    return (
        html.escape(fragment)
        .replace(_HL_OPEN, "<mark>")
        .replace(_HL_CLOSE, "</mark>")
    )


def search_books(
    keyword: str, page: int = 1, page_size: int = 20
) -> tuple[list[sqlite3.Row], int]:
    return list_books(page=page, page_size=page_size, keyword=keyword.strip(), order="updated")


def _fallback_fragment(content: str, keyword: str, width: int = 80) -> str:
    pos = content.find(keyword)
    if pos < 0:
        return content[:width]
    start = max(0, pos - width // 2)
    end = min(len(content), pos + len(keyword) + width // 2)
    piece = content[start:end].replace("\n", " ")
    piece = piece.replace(keyword, f"{_HL_OPEN}{keyword}{_HL_CLOSE}")
    return ("…" if start else "") + piece + ("…" if end < len(content) else "")


def search_chapters(
    keyword: str, page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    keyword = keyword.strip()
    if not keyword:
        return [], 0
    offset = max(0, (page - 1) * page_size)

    if len(keyword) >= FTS_MIN_CHARS:
        match = _fts_match(keyword)
        total = int(
            db.scalar("SELECT COUNT(*) FROM chapters_fts WHERE chapters_fts MATCH ?", (match,))
        )
        rows = db.query(
            f"""SELECT c.id AS chapter_id, c.book_id, c.idx, c.title AS chapter_title,
                       b.title AS book_title, b.author, b.split_mode,
                       snippet(chapters_fts, 1, '{_HL_OPEN}', '{_HL_CLOSE}', '…', 16) AS frag
                  FROM chapters_fts
                  JOIN chapters c ON c.id = chapters_fts.rowid
                  JOIN books b ON b.id = c.book_id
                 WHERE chapters_fts MATCH ?
                 ORDER BY rank
                 LIMIT ? OFFSET ?""",
            (match, page_size, offset),
        )
    else:
        like = f"%{keyword}%"
        total = int(
            db.scalar("SELECT COUNT(*) FROM chapters WHERE content LIKE ?", (like,))
        )
        rows = db.query(
            """SELECT c.id AS chapter_id, c.book_id, c.idx, c.title AS chapter_title,
                      c.content AS frag, b.title AS book_title, b.author, b.split_mode
                 FROM chapters c JOIN books b ON b.id = c.book_id
                WHERE c.content LIKE ?
                ORDER BY c.book_id, c.idx
                LIMIT ? OFFSET ?""",
            (like, page_size, offset),
        )

    results: list[dict] = []
    for row in rows:
        item = dict(row)
        fragment = item["frag"] or ""
        if len(keyword) < FTS_MIN_CHARS:
            fragment = _fallback_fragment(fragment, keyword)
        item["frag"] = highlight(fragment)
        results.append(item)
    return results, total

