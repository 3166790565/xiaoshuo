"""上传导入：多文件与 ZIP、内容去重、后台任务进度。

epub 预留：:func:`import_one` 按扩展名分派，本轮只有 txt 分支。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import zipfile
from io import BytesIO

from .. import config, db
from . import repo
from . import settings as app_settings
from .decoder import decode_bytes
from .txt_parser import ParseOptions, clean_filename, normalize_text, parse_text

TXT_SUFFIXES = (".txt", ".text")
ZIP_SUFFIXES = (".zip",)
_WS = re.compile(r"\s+")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def content_hash(text: str) -> str:
    """按"去掉全部空白的正文"取哈希：同一本书换编码或换行风格不会重复入库。"""
    return hashlib.sha256(_WS.sub("", normalize_text(text)).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ ZIP 展开


def _zip_entry_name(info: zipfile.ZipInfo) -> str:
    """没打 UTF-8 标志位的条目名多半是 GBK，cp437 回转一次。"""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def read_zip(data: bytes) -> list[tuple[str, bytes]]:
    """只取 .txt，拦路径穿越与解压炸弹。"""
    items: list[tuple[str, bytes]] = []
    total = 0
    with zipfile.ZipFile(BytesIO(data)) as archive:
        entries = [i for i in archive.infolist() if not i.is_dir()]
        if len(entries) > config.MAX_ZIP_ENTRIES:
            raise ValueError(f"压缩包内条目过多：{len(entries)} > {config.MAX_ZIP_ENTRIES}")
        for info in entries:
            name = _zip_entry_name(info).replace("\\", "/")
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or _WINDOWS_DRIVE.match(name):
                raise ValueError(f"压缩包内存在非法路径：{name}")
            base = parts[-1]
            if not base.lower().endswith(TXT_SUFFIXES):
                continue
            if info.file_size > config.MAX_ZIP_ENTRY_BYTES:
                raise ValueError(f"{base} 解压后体积超限（{info.file_size} 字节）")
            total += info.file_size
            if total > config.MAX_ZIP_TOTAL_BYTES:
                raise ValueError("压缩包解压总体积超限")
            items.append((base, archive.read(info)))
    return items


def expand(uploads: list[tuple[str, bytes]]) -> tuple[list[tuple[str, bytes]], list[dict]]:
    """把上传列表里的 ZIP 摊平成 txt 列表；坏包只记错误，不中断整个任务。"""
    items: list[tuple[str, bytes]] = []
    errors: list[dict] = []
    for name, data in uploads:
        lower = name.lower()
        if lower.endswith(ZIP_SUFFIXES):
            try:
                inner = read_zip(data)
            except (zipfile.BadZipFile, ValueError, OSError) as exc:
                errors.append({"file": name, "status": "error", "message": f"压缩包读取失败：{exc}"})
                continue
            if not inner:
                errors.append({"file": name, "status": "error", "message": "压缩包内没有 txt"})
            items.extend(inner)
        elif lower.endswith(TXT_SUFFIXES):
            items.append((name, data))
        else:
            errors.append({"file": name, "status": "error", "message": "只支持 .txt 与 .zip"})
    return items, errors


# ------------------------------------------------------------------ 单本导入


def import_one(filename: str, data: bytes, options: ParseOptions) -> dict:
    """返回一条日志记录，状态为 ok / skip / error。"""
    display = clean_filename(filename)
    record: dict = {"file": display, "status": "error", "message": ""}

    if len(data) > config.MAX_TXT_BYTES:
        record["message"] = f"文件超过 {config.MAX_TXT_BYTES // 1024 // 1024} MB 上限"
        return record
    if not display.lower().endswith(TXT_SUFFIXES):
        record["message"] = "本轮只实现 txt 分支（epub 需要时再接 ebooklib）"
        return record

    text, encoding = decode_bytes(data)
    if not text.strip():
        record["message"] = "文件没有可用正文"
        return record

    digest = content_hash(text)
    existing = repo.get_book_by_hash(digest)
    if existing:
        record.update(
            status="skip",
            book_id=int(existing["id"]),
            title=existing["title"],
            message="内容重复，已跳过",
        )
        return record

    parsed = parse_text(text, display, options, encoding=encoding)
    if not parsed.chapters:
        record["message"] = "解析后没有章节"
        return record

    try:
        book_id = repo.insert_book(parsed, display, digest)
    except sqlite3.IntegrityError:  # 同批次里的并发重复
        record.update(status="skip", title=parsed.title, message="内容重复，已跳过")
        return record

    record.update(
        status="ok",
        book_id=book_id,
        title=parsed.title,
        author=parsed.author,
        mode=parsed.split_mode,
        mode_label=repo.SPLIT_MODE_LABELS.get(parsed.split_mode, parsed.split_mode),
        chapters=len(parsed.chapters),
        words=parsed.word_count,
        encoding=encoding,
        message="；".join(parsed.notes),
    )
    return record


# ------------------------------------------------------------------ 任务


def create_job(total: int = 0) -> str:
    job_id = uuid.uuid4().hex[:16]
    db.execute(
        "INSERT INTO import_jobs(id, status, total, done, ok, failed, log, created_at) "
        "VALUES(?, 'pending', ?, 0, 0, 0, '[]', ?)",
        (job_id, total, repo.now()),
    )
    return job_id


def get_job(job_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM import_jobs WHERE id = ?", (job_id,))
    if row is None:
        return None
    job = dict(row)
    try:
        job["log"] = json.loads(job["log"] or "[]")
    except json.JSONDecodeError:
        job["log"] = []
    return job


def recent_jobs(limit: int = 10) -> list[dict]:
    rows = db.query(
        "SELECT id, status, total, done, ok, failed, created_at FROM import_jobs "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]


def _save_job(job_id: str, status: str, total: int, log: list[dict]) -> None:
    ok = sum(1 for r in log if r["status"] == "ok")
    skipped = sum(1 for r in log if r["status"] == "skip")
    failed = sum(1 for r in log if r["status"] == "error")
    db.execute(
        "UPDATE import_jobs SET status=?, total=?, done=?, ok=?, failed=?, log=? WHERE id=?",
        (status, total, ok + skipped + failed, ok, failed, json.dumps(log, ensure_ascii=False), job_id),
    )


def run_import(
    job_id: str,
    uploads: list[tuple[str, bytes]],
    force_mode: str = "",
    target_chars: int | None = None,
) -> None:
    """后台线程入口。每本处理完就写一次进度，前端轮询即可看到逐本结果。"""
    log: list[dict] = []
    try:
        options = app_settings.load_options(force_mode=force_mode, target_chars=target_chars)
        items, log = expand(uploads)
        total = len(items) + len(log)
        _save_job(job_id, "running", total, log)

        for name, data in items:
            try:
                log.append(import_one(name, data, options))
            except Exception as exc:  # 单本失败不拖垮整批
                log.append(
                    {
                        "file": clean_filename(name),
                        "status": "error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
            _save_job(job_id, "running", total, log)

        _save_job(job_id, "done", total, log)
    except Exception as exc:
        log.append({"file": "-", "status": "error", "message": f"任务失败：{exc}"})
        _save_job(job_id, "error", len(log), log)
    finally:
        db.close_conn()  # 后台线程用完就还回去，别占着 sqlite 连接


# ------------------------------------------------------------------ 重新分章


def resplit_one(book_id: int, options: ParseOptions) -> dict:
    """用库里现存正文重跑解析器。调完规则后批量修复就靠它。"""
    book = repo.get_book(book_id)
    if book is None:
        return {"file": f"#{book_id}", "status": "error", "message": "书籍不存在"}

    record: dict = {"file": book["title"], "status": "error", "book_id": book_id}
    text = repo.chapter_source_text(book_id)
    if not text.strip():
        record["message"] = "这本书没有正文"
        return record

    parsed = parse_text(text, book["source_filename"] or book["title"], options)
    if not parsed.chapters:
        record["message"] = "重新解析后没有章节，已保留原样"
        return record

    repo.replace_chapters(book_id, parsed)
    record.update(
        status="ok",
        title=book["title"],
        mode=parsed.split_mode,
        mode_label=repo.SPLIT_MODE_LABELS.get(parsed.split_mode, parsed.split_mode),
        chapters=len(parsed.chapters),
        words=parsed.word_count,
        message="；".join(parsed.notes),
    )
    return record


def run_resplit(
    job_id: str,
    book_ids: list[int],
    force_mode: str = "",
    target_chars: int | None = None,
) -> None:
    log: list[dict] = []
    try:
        options = app_settings.load_options(force_mode=force_mode, target_chars=target_chars)
        total = len(book_ids)
        _save_job(job_id, "running", total, log)
        for book_id in book_ids:
            try:
                log.append(resplit_one(book_id, options))
            except Exception as exc:
                log.append(
                    {
                        "file": f"#{book_id}",
                        "status": "error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
            _save_job(job_id, "running", total, log)
        _save_job(job_id, "done", total, log)
    except Exception as exc:
        log.append({"file": "-", "status": "error", "message": f"任务失败：{exc}"})
        _save_job(job_id, "error", len(log), log)
    finally:
        db.close_conn()

