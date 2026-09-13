"""把本地目录里的 txt 批量灌进库，走的是和后台上传完全一样的格式化流程。

和 `/admin/upload` 的唯一区别是"文件从哪来"：上传是内存里的字节流，
这里逐个文件读盘，所以 8 万个文件也不会把内存撑爆。链路一步没少——
解码 → 内容哈希去重 → `txt_parser.parse_text` 分章 → `repo.insert_book`，
`split_mode` 三态判定、广告行清理、硬换行合并、目标字数都用后台设置里的值。

    python scripts/bulk_import.py "E:/PythonProject/小说爬/小说_90000-"
    python scripts/bulk_import.py <目录> --workers 8 --limit 200
    python scripts/bulk_import.py <目录> --dry-run          # 只解析不写库
    python scripts/bulk_import.py <目录> --http http://127.0.0.1:8000   # 走站点 HTTP 上传接口

内置 `--http` 时不再走进程池，而是把文件逐个 POST 给站点的上传接口
（`/admin/upload`，复用同一条导入链路），适合脚本跑在另一台机器/容器里、
只想把文件推给已运行的站点。示例：需要管理员密码（会话 cookie 保存在
`<数据目录>/uploader_cookies.txt`，过期会自动重新登录，重跑免重新输入）：

    python scripts/bulk_import.py <目录> --http http://127.0.0.1:8000 --admin-password 你的密码

中断了直接重跑同一条命令：处理过的文件都记在 jsonl 日志里，重跑会跳过。
Git Bash 里建议先 `export PYTHONIOENCODING=utf-8`，脚本也会自己纠一次 stdout。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 允许 python scripts/bulk_import.py 直接跑
    sys.path.insert(0, str(ROOT))

TXT_SUFFIXES = (".txt", ".text")


# --------------------------------------------------------------------- 子进程

# 解析是纯 CPU 活且不碰数据库（txt_parser 不 import db），丢给进程池最划算。
# 每个 worker 起来时把解析选项和已入库的哈希集合塞进这里，避免逐个文件重复传。
_WORKER: dict = {}


def _worker_init(options, known_hashes: frozenset[str]) -> None:
    _WORKER["options"] = options
    _WORKER["known"] = known_hashes


def _parse_file(path_str: str) -> dict:
    """读盘 → 解码 → 算哈希 → 分章。返回一条待落库的记录，绝不抛异常。"""
    from app import config
    from app.services import importer
    from app.services.decoder import decode_bytes
    from app.services.txt_parser import clean_filename, parse_text

    display = clean_filename(os.path.basename(path_str))
    rec: dict = {"path": path_str, "file": display, "status": "error", "message": ""}
    try:
        data = Path(path_str).read_bytes()
        if len(data) > config.MAX_TXT_BYTES:
            rec["message"] = f"文件超过 {config.MAX_TXT_BYTES // 1024 // 1024} MB 上限"
            return rec

        text, encoding = decode_bytes(data)
        if not text.strip():
            rec["message"] = "文件没有可用正文"
            return rec

        digest = importer.content_hash(text)
        rec["hash"] = digest
        if digest in _WORKER["known"]:  # 已在库里，省掉一次分章
            rec.update(status="skip", message="内容重复，已跳过")
            return rec

        parsed = parse_text(text, display, _WORKER["options"], encoding=encoding)
        if not parsed.chapters:
            rec["message"] = "解析后没有章节"
            return rec

        rec.update(status="parsed", parsed=parsed, encoding=encoding)
    except Exception as exc:  # 单本坏掉不能拖垮整批
        rec.update(status="error", message=f"{type(exc).__name__}: {exc}")
    return rec


# --------------------------------------------------------------------- 主进程


def _store(rec: dict, seen: set[str], dry_run: bool) -> dict:
    """落库。写入只在主进程做，sqlite 连接不跨进程。"""
    from app.services import repo

    parsed = rec.pop("parsed")
    digest = rec["hash"]
    rec.update(
        title=parsed.title,
        author=parsed.author,
        mode=parsed.split_mode,
        chapters=len(parsed.chapters),
        words=parsed.word_count,
    )
    if digest in seen:  # 同一批里出现的重复内容
        rec.update(status="skip", message="内容重复，已跳过")
        return rec
    if dry_run:
        seen.add(digest)
        rec.update(status="ok", message="dry-run 未写库")
        return rec
    try:
        # source_filename 传解码后的文件名，和后台上传保持一致
        rec["book_id"] = repo.insert_book(parsed, rec["file"], digest)
    except sqlite3.IntegrityError:
        rec.update(status="skip", message="内容重复，已跳过")
        return rec
    seen.add(digest)
    rec.update(status="ok", message="；".join(parsed.notes))
    return rec


def _iter_txt(root: Path, recursive: bool) -> list[str]:
    """列文件。8 万个条目用 scandir 走一遍就够，不用 glob。"""
    found: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        stack.append(Path(entry.path))
                elif entry.name.lower().endswith(TXT_SUFFIXES):
                    found.append(entry.path)
    found.sort()
    return found


# --------------------------------------------------------------------- HTTP 模式

def _http_import(
    base: str,
    path_str: str,
    session: requests.Session,
    force_mode: str,
    target_chars: int | None,
) -> dict:
    """把单个文件 POST 到 /admin/upload，轮询到任务完成，返回一条与离线模式同构的记录。"""
    file = Path(path_str)
    rec: dict = {"path": path_str, "file": clean_filename(file.name), "status": "error", "message": ""}
    try:
        with file.open("rb") as handle:
            files = {"files": (file.name, handle, "text/plain")}
            data = {"force_mode": force_mode}
            if target_chars:
                data["target_chars"] = str(target_chars)
            resp = session.post(f"{base}/admin/upload", data=data, files=files, timeout=600)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            rec["message"] = payload.get("message", "上传失败")
            return rec

        job_id = payload["job_id"]
        for _ in range(300):  # 任务完成超时约 5 分钟
            time.sleep(1)
            job = session.get(f"{base}/admin/api/jobs/{job_id}", timeout=30).json()
            if job.get("status") in ("done", "error"):
                break
        else:
            rec["message"] = "任务超时"
            return rec
        log = job.get("log", [])
        result = next((r for r in log if r.get("status") != "error"), log[0]) if log else {}
        rec.update(
            status=result.get("status", "error"),
            title=result.get("title", ""),
            author=result.get("author", ""),
            mode=result.get("mode", ""),
            chapters=result.get("chapters", 0),
            words=result.get("words", 0),
            message=result.get("message", job.get("errorMsg", "") or ""),
        )
        rec.pop("file", None)  # job 日志里已带 file/title，主循环不重复
        return rec
    except (requests.RequestException, ValueError, KeyError) as exc:
        rec["message"] = f"{type(exc).__name__}: {exc}"
        return rec


def _load_done(log_path: Path, retry_errors: bool) -> set[str]:
    """从 jsonl 日志里恢复"已处理过的文件"，实现断点续跑。"""
    done: set[str] = set()
    if not log_path.is_file():
        return done
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if retry_errors and rec.get("status") == "error":
                continue
            if rec.get("path"):
                done.add(rec["path"])
    return done


def _fmt_dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _db_size() -> str:
    from app import config

    total = 0
    for suffix in ("", "-wal"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.is_file():
            total += path.stat().st_size
    return f"{total / 1024 / 1024 / 1024:.2f}GB"


def _tune_conn() -> None:
    """只对本次导入的连接调参：大 page cache 让 FTS5 trigram 索引写得快些。"""
    from app import db

    conn = db.get_conn()
    conn.execute("PRAGMA cache_size=-262144")  # 256MB
    conn.execute("PRAGMA wal_autocheckpoint=4000")


def _default_log() -> Path:
    """续跑日志跟着 NOVEL_DATA_DIR 走——容器里数据目录是挂载进来的 /data，不是 ROOT/data。"""
    from app import config

    return config.DATA_DIR / "bulk_import.jsonl"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量导入目录下的 txt")
    parser.add_argument("directory", type=Path, help="txt 所在目录")
    parser.add_argument("-r", "--recursive", action="store_true", help="连子目录一起扫")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几个文件（先跑小样用）")
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) - 1),
        help="解析进程数，0 表示不开进程池（调试用）",
    )
    parser.add_argument("--target-chars", type=int, default=None, help="覆盖后台设置的分节目标字数")
    parser.add_argument(
        "--force-mode", choices=("", "single", "auto"), default="", help="强制分章模式"
    )
    parser.add_argument("--dry-run", action="store_true", help="只解析不写库")
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="jsonl 日志，同时是续跑依据（默认数据目录下的 bulk_import.jsonl）",
    )
    parser.add_argument("--no-resume", action="store_true", help="忽略日志，全部重新处理")
    parser.add_argument("--retry-errors", action="store_true", help="续跑时重试上次失败的文件")
    parser.add_argument("--progress-every", type=int, default=100, help="每处理多少个打一行进度")
    parser.add_argument("--http", type=str, default="", help="HTTP 模式：站点地址，如 http://127.0.0.1:8000")
    parser.add_argument("--admin-password", type=str, default="", help="HTTP 模式：管理员密码（首次或会话过期时用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台遇到中文会炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parse_args(argv)
    if args.log is None:
        args.log = _default_log()
        if args.dry_run:
            # dry-run 没真写库，日志不能污染正式续跑记录（显式给了 --log 就听用户的）
            args.log = args.log.with_name("bulk_import.dryrun.jsonl")

    if not args.directory.is_dir():
        print(f"目录不存在：{args.directory}")
        return 2

    from app import config
    from app.services import settings as app_settings
    from app.services.txt_parser import clean_filename

    if args.http:
        import requests  # 只在 HTTP 模式下才需要

        return _run_http(args, config, app_settings, clean_filename)

    from app import db

    config.ensure_dirs()
    db.init_db()
    _tune_conn()
    options = app_settings.load_options(
        force_mode=args.force_mode, target_chars=args.target_chars
    )

    print(f"数据库    {config.DB_PATH}（当前 {_db_size()}）")
    print(
        f"解析选项  目标 {options.target_chars} 字 / 模式 "
        f"{options.force_mode or '自动'} / 小标题 {'开' if options.add_subtitle else '关'} / "
        f"去广告 {'开' if options.strip_ads else '关'}"
    )

    paths = _iter_txt(args.directory, args.recursive)
    print(f"扫描到    {len(paths)} 个 txt")
    done_paths = set() if args.no_resume else _load_done(args.log, args.retry_errors)
    if done_paths:
        paths = [p for p in paths if p not in done_paths]
        print(f"续跑      跳过日志里已处理的 {len(done_paths)} 个，剩 {len(paths)} 个")
    if args.limit > 0:
        paths = paths[: args.limit]
        print(f"限量      本轮只处理 {len(paths)} 个")
    if not paths:
        print("没有需要处理的文件。")
        return 0

    known = frozenset(
        row["content_hash"]
        for row in db.query("SELECT content_hash FROM books WHERE content_hash IS NOT NULL")
    )
    print(f"已入库    {len(known)} 本，内容重复的会跳过")
    print(f"日志      {args.log}")
    if args.dry_run:
        print("dry-run   只解析，不写库")
    print("-" * 72, flush=True)

    args.log.parent.mkdir(parents=True, exist_ok=True)
    seen = set(known)
    stats = {"ok": 0, "skip": 0, "error": 0}
    total = len(paths)
    start = time.monotonic()
    counter = 0
    interrupted = False

    with args.log.open("a", encoding="utf-8", buffering=1) as log_file:

        def handle(rec: dict) -> None:
            """统计 + 落库 + 写日志，主进程串行执行。"""
            nonlocal counter
            if rec.get("status") == "parsed":
                rec = _store(rec, seen, args.dry_run)
            counter += 1
            stats[rec["status"]] = stats.get(rec["status"], 0) + 1
            if rec["status"] == "error" and stats["error"] <= 20:
                print(f"  ! {rec['file']}：{rec['message']}")
            log_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if counter % args.progress_every == 0 or counter == total:
                elapsed = time.monotonic() - start
                rate = counter / elapsed if elapsed else 0
                eta = (total - counter) / rate if rate else 0
                print(
                    f"[{counter:>6}/{total}] 入库 {stats['ok']} 跳过 {stats['skip']} "
                    f"失败 {stats['error']} | {rate:.1f} 本/秒 | 已用 {_fmt_dur(elapsed)} "
                    f"| 剩余 {_fmt_dur(eta)} | 库 {_db_size()}",
                    flush=True,
                )

        try:
            if args.workers <= 0:  # 单进程：调试时看堆栈方便
                _worker_init(options, known)
                for path_str in paths:
                    handle(_parse_file(path_str))
            else:
                # 只保持有限个任务在飞，否则 8 万份解析结果会一起堆在内存里
                inflight = max(args.workers * 3, 8)
                pending: set = set()
                queue = iter(paths)
                with ProcessPoolExecutor(
                    max_workers=args.workers,
                    initializer=_worker_init,
                    initargs=(options, known),
                ) as pool:
                    try:
                        while True:
                            while len(pending) < inflight:
                                nxt = next(queue, None)
                                if nxt is None:
                                    break
                                pending.add(pool.submit(_parse_file, nxt))
                            if not pending:
                                break
                            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                            for future in finished:
                                handle(future.result())
                    except KeyboardInterrupt:
                        for future in pending:
                            future.cancel()
                        raise
        except KeyboardInterrupt:
            interrupted = True
            print("\n收到 Ctrl+C，已处理的都写进日志了。")

    elapsed = time.monotonic() - start
    print("-" * 72)
    print(
        f"{'中断' if interrupted else '完成'}：处理 {counter}/{total}，"
        f"入库 {stats['ok']}，跳过 {stats['skip']}，失败 {stats['error']}，"
        f"耗时 {_fmt_dur(elapsed)}"
    )
    print(f"库现在 {_db_size()}，共 {db.scalar('SELECT COUNT(*) FROM books')} 本 / "
          f"{db.scalar('SELECT COUNT(*) FROM chapters')} 章")
    if interrupted or counter < total:
        print("重跑同一条命令即可继续（已处理的会按日志跳过）。")
    db.close_conn()
    return 130 if interrupted else 0


def _run_http(args: argparse.Namespace, config, app_settings, clean_filename) -> int:
    """HTTP 模式主流程：逐个文件 POST 给站点导入接口，不占本地进程池与 sqlite。"""
    import requests

    base = args.http.rstrip("/")
    params = {"force_mode": args.force_mode}
    if args.target_chars:
        params["target_chars"] = args.target_chars

    # 会话 cookie 与续跑日志都放数据目录，与容器挂载卷行为一致
    cookies_path = config.DATA_DIR / "uploader_cookies.json"
    try:
        session = requests.Session()
        if cookies_path.is_file():
            session.cookies.update(json.loads(cookies_path.read_text(encoding="utf-8")))
        probe = session.get(f"{base}/admin/upload", timeout=30, allow_redirects=False)
        if probe.status_code == 303 or (probe.status_code >= 400 and not session.cookies):
            if not args.admin_password:
                print("会话已失效，请提供 --admin-password 重新登录。")
                return 2
            resp = session.post(
                f"{base}/admin/login",
                data={"password": args.admin_password, "next": "/admin"},
                timeout=30,
                allow_redirects=False,
            )
            if resp.status_code not in (302, 303) or not session.cookies:
                print("登录失败：请检查管理员密码或站点地址。")
                return 2
            cookies_path.parent.mkdir(parents=True, exist_ok=True)
            cookies_path.write_text(json.dumps(dict(session.cookies)), encoding="utf-8")

        paths = _iter_txt(args.directory, args.recursive)
        print(f"HTTP 模式：{args.http}，扫描到 {len(paths)} 个 txt")

        done_paths = set() if args.no_resume else _load_done(args.log, args.retry_errors)
        if done_paths:
            paths = [p for p in paths if p not in done_paths]
            print(f"续跑      跳过日志里已处理的 {len(done_paths)} 个，剩 {len(paths)} 个")
        if args.limit > 0:
            paths = paths[: args.limit]
            print(f"限量      本轮只处理 {len(paths)} 个")
        if not paths:
            print("没有需要处理的文件。")
            return 0

        args.log.parent.mkdir(parents=True, exist_ok=True)
        stats = {"ok": 0, "skip": 0, "error": 0}
        total = len(paths)
        start = time.monotonic()
        counter = 0
        interrupted = False

        def handle(rec: dict) -> None:
            nonlocal counter
            counter += 1
            status = rec.get("status", "error")
            stats[status] = stats.get(status, 0) + 1
            if status == "error" and stats["error"] <= 20:
                print(f"  ! {rec['file']}：{rec['message']}")
            log_line = json.dumps(rec, ensure_ascii=False)
            with args.log.open("a", encoding="utf-8", buffering=1) as log_file:
                log_file.write(log_line + "\n")
            if counter % args.progress_every == 0 or counter == total:
                elapsed = time.monotonic() - start
                rate = counter / elapsed if elapsed else 0
                eta = (total - counter) / rate if rate else 0
                print(
                    f"[{counter:>6}/{total}] 入库 {stats['ok']} 跳过 {stats['skip']} "
                    f"失败 {stats['error']} | {rate:.1f} 本/秒 | 已用 {_fmt_dur(elapsed)} "
                    f"| 剩余 {_fmt_dur(eta)}",
                    flush=True,
                )

        try:
            for path_str in paths:
                rec = _http_import(base, path_str, session, params, None)
                if rec.get("status") == "error" and rec["message"].startswith("需要管理员登录"):
                    # 会话中途失效，重新登录后重试一次
                    resp = session.post(
                        f"{base}/admin/login",
                        data={"password": args.admin_password, "next": "/admin"},
                        timeout=30, allow_redirects=False,
                    )
                    if session.cookies:
                        cookies_path.parent.mkdir(parents=True, exist_ok=True)
                        cookies_path.write_text(json.dumps(dict(session.cookies)), encoding="utf-8")
                        rec = _http_import(base, path_str, session, params, None)
                handle(rec)
        except KeyboardInterrupt:
            interrupted = True
            print("\n收到 Ctrl+C，已处理的都写进日志了。")

        elapsed = time.monotonic() - start
        print("-" * 72)
        print(
            f"{'中断' if interrupted else '完成'}：处理 {counter}/{total}，"
            f"入库 {stats['ok']}，跳过 {stats['skip']}，失败 {stats['error']}，"
            f"耗时 {_fmt_dur(elapsed)}"
        )
        print(f"日志 {args.log}（重跑同一条命令即可继续）")
        return 130 if interrupted else 0
    except requests.RequestException as exc:
        print(f"与站点通信失败：{exc}")
        return 1


if __name__ == "__main__":  # Windows 用 spawn 起子进程，这个 guard 必须有
    raise SystemExit(main())
