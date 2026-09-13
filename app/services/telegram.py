"""TG 频道爬取：网页登录、频道管理、逐本下载入库、定期自动同步。

Telethon 是 asyncio 库，而本项目的路由与后台任务都是同步代码、跑在各自的线程里，
所以这里维护一个长驻后台事件循环线程，所有协程经 :func:`_call` 提交过去阻塞等结果。
单例 client 也活在这个循环上——"发验证码 / 输验证码"跨两个 HTTP 请求却必须共用
同一个 client（phone_code_hash 在 session 里），正好被这个设计一并解决。

会话字符串（StringSession）存 app_settings 表，进程重启、容器重建都不丢。
增量同步靠 tg_channels.last_message_id 游标，重复内容再由 content_hash 判重兜底。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from io import BytesIO

from telethon import TelegramClient
from telethon.errors import AuthKeyError, FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

from .. import config, db
from . import importer, repo
from . import settings as app_settings

# 登录中间态有效期：超时或进程重启都回到"输手机号"那一步
PENDING_TTL = 300
# 单个文件下载的硬超时，防一个大文件把任务挂死
DOWNLOAD_TIMEOUT = 600
# FloodWait 最多睡 15 分钟再试，更长的等待直接放弃该文件
FLOOD_SLEEP_CAP = 900

_loop: asyncio.AbstractEventLoop | None = None
_client: TelegramClient | None = None
_pending: dict | None = None  # {"client", "phone", "step", "expires"}
_state_lock = threading.Lock()  # 守护 _client / _pending 的替换
_sync_lock = threading.Lock()  # 全站同时只允许一个同步任务在跑
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


class TgNotLoggedIn(Exception):
    """没有可用会话（没登录过或已失效），页面应引导去登录表单。"""


# ------------------------------------------------------------------ 事件循环线程


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None and not _loop.is_closed():
        return _loop
    with _state_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=_run_loop, args=(loop,), name="tg-loop", daemon=True).start()
            _loop = loop
    return _loop


def _call(coro, timeout: float | None = None):
    """把协程提交到长驻 loop 并阻塞等结果；异常原样抛给调用线程。"""
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    return future.result(timeout)


def api_ready() -> bool:
    return bool(config.TG_API_ID and config.TG_API_HASH)


def _new_client(session: str = "") -> TelegramClient:
    if not api_ready():
        raise TgNotLoggedIn("未配置 TG_API_ID / TG_API_HASH，请先写进 .env 再重启")
    return TelegramClient(StringSession(session), config.TG_API_ID, config.TG_API_HASH)


async def _make_client(session: str = "") -> TelegramClient:
    """构造 TelegramClient 必须在有运行中 loop 的线程里做——Py3.12 起 worker 线程
    没有默认 loop，在外面直接 new 会抛 RuntimeError，所以经 _call 挪到长驻 loop 上。"""
    return _new_client(session)


async def _disconnect(client: TelegramClient) -> None:
    """Telethon 的 disconnect() 是普通方法，内部读 self.loop（= 当前线程的运行中 loop），
    在 worker 线程里直接调用会抛 "no current event loop"，所以必须包成协程在 loop 上跑。"""
    await client.disconnect()


# ------------------------------------------------------------------ 设置存取
# 这些 key 只在本模块用，不进 settings.py 的 DEFAULTS（那套只管解析设置）


def get_setting(key: str, default: str = "") -> str:
    row = db.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    if row is None or row["value"] is None:
        return default
    return str(row["value"])


def set_setting(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO app_settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def logged_in() -> bool:
    """只看库里有没有会话字符串；真伪要等连上网才知道。"""
    return bool(get_setting("tg_session"))


def phone_number() -> str:
    return get_setting("tg_phone")


def _invalidate_session() -> None:
    """会话被判失效时调用：清库里的字符串并弃掉单例 client，页面回到登录表单。"""
    global _client
    with _state_lock:
        client = _client
        _client = None
    db.execute("DELETE FROM app_settings WHERE key = 'tg_session'")
    if client is not None:
        try:
            _call(_disconnect(client), timeout=10)
        except Exception:
            pass


# ------------------------------------------------------------------ 登录状态机


def _drop_pending(force: bool = False) -> None:
    global _pending
    with _state_lock:
        pending = _pending
        if pending is None:
            return
        if not force and pending["expires"] > time.monotonic():
            return
        _pending = None
    if pending is not None:
        try:
            _call(_disconnect(pending["client"]), timeout=10)
        except Exception:
            pass


def drop_login() -> None:
    """丢弃进行中的登录流程（用户点了「换个手机号」）。"""
    _drop_pending(force=True)


def pending_step() -> str:
    """页面渲染用：'' 表示没有进行中的登录，'code' 待输验证码，'password' 待输 2FA。"""
    _drop_pending()  # 顺手清过期的
    with _state_lock:
        return _pending["step"] if _pending else ""


def login_send_code(phone: str) -> dict:
    """第一步：输手机号，发验证码。client 存进 _pending 等下一步用。"""
    global _pending
    number = phone.strip().replace(" ", "")
    if not re.fullmatch(r"\+?\d{5,15}", number):
        raise ValueError("手机号格式不对，要带国家区号，比如 +8613800138000")
    if not number.startswith("+"):
        number = "+" + number
    _drop_pending(force=True)

    client = _call(_make_client())  # 构造也要在长驻 loop 上，见 _make_client 的注释

    async def run() -> str:
        await client.connect()
        if await client.is_user_authorized():
            return "already"
        await client.send_code_request(number)
        return "sent"

    try:
        state = _call(run(), timeout=60)
    except Exception as exc:
        try:
            _call(_disconnect(client), timeout=10)
        except Exception:
            pass
        raise ValueError(f"发送验证码失败：{_friendly(exc)}") from exc

    if state == "already":  # 新建的空 session 正常不会走到这，防御一下
        _call(_disconnect(client), timeout=10)
        raise ValueError("登录状态异常，请重试一次")

    with _state_lock:
        _pending = {
            "client": client,
            "phone": number,
            "step": "code",
            "expires": time.monotonic() + PENDING_TTL,
        }
    return {"ok": True, "step": "code", "phone": number}


def login_verify(code: str) -> dict:
    """第二步：输验证码。开了两步验证会返回 step=password。"""
    with _state_lock:
        pending = _pending
    if pending is None or pending["step"] not in ("code",):
        raise ValueError("请先获取验证码")

    async def run() -> str:
        try:
            await pending["client"].sign_in(pending["phone"], code.strip())
        except SessionPasswordNeededError:
            return "password"
        return "ok"

    try:
        state = _call(run(), timeout=60)
    except Exception as exc:
        raise ValueError(f"验证码不对或已过期：{_friendly(exc)}") from exc

    if state == "password":
        with _state_lock:
            pending["step"] = "password"
            pending["expires"] = time.monotonic() + PENDING_TTL
        return {"ok": True, "step": "password"}

    _finish_login(pending)
    return {"ok": True, "step": "done"}


def login_password(password: str) -> dict:
    """第三步（可选）：输两步验证的云密码。"""
    with _state_lock:
        pending = _pending
    if pending is None or pending["step"] != "password":
        raise ValueError("不需要输云密码，或登录流程已过期")

    async def run() -> None:
        await pending["client"].sign_in(password=password)

    try:
        _call(run(), timeout=60)
    except Exception as exc:
        raise ValueError(f"云密码不对：{_friendly(exc)}") from exc

    _finish_login(pending)
    return {"ok": True, "step": "done"}


def _finish_login(pending: dict) -> None:
    """登录成功：会话字符串落库，client 转正为单例继续用。"""
    global _client, _pending
    session = pending["client"].session.save()  # StringSession.save() 是同步方法
    set_setting("tg_session", session)
    set_setting("tg_phone", pending["phone"])
    with _state_lock:
        _client = pending["client"]
        _pending = None


def logout() -> dict:
    global _client
    _drop_pending(force=True)
    with _state_lock:
        client = _client
        _client = None
    if client is not None:
        try:
            _call(client.log_out(), timeout=30)  # 让 TG 那边也作废这个会话
        except Exception:
            pass
    db.execute("DELETE FROM app_settings WHERE key = 'tg_session'")
    return {"ok": True, "step": "logged_out"}


# ------------------------------------------------------------------ client 单例


def get_client() -> TelegramClient:
    """取已授权的单例 client；没有或失效就抛 TgNotLoggedIn。"""
    global _client
    with _state_lock:
        client = _client
    if client is not None:
        return client

    session = get_setting("tg_session")
    if not session:
        raise TgNotLoggedIn("尚未登录 Telegram")

    client = _call(_make_client(session))

    async def run() -> bool:
        await client.connect()
        authorized = await client.is_user_authorized()
        if not authorized:
            await client.disconnect()
        return authorized

    try:
        state = _call(run(), timeout=60)
    except Exception as exc:
        try:
            _call(_disconnect(client), timeout=10)
        except Exception:
            pass
        # 网络抖动、超时这类临时故障不清会话，下次再试还有机会
        raise TgNotLoggedIn(f"连接 Telegram 失败：{_friendly(exc)}")
    if not state:
        _invalidate_session()  # 连上了但 TG 说未授权，这才是真失效
        raise TgNotLoggedIn("TG 会话已失效，请重新登录")

    with _state_lock:
        _client = client
    return client


# ------------------------------------------------------------------ 频道管理


def parse_username(link: str) -> str:
    """接受 t.me/xxx、https://t.me/xxx、@xxx、裸用户名；只支持公开频道。"""
    text = link.strip()
    match = re.search(r"t\.me/\+?[A-Za-z0-9_]+", text)
    if match and "/+" in match.group(0):
        raise ValueError("私有频道（+开头的邀请链接）暂不支持，请用公开频道的用户名")
    if match:
        text = match.group(0).rsplit("/", 1)[-1]
    text = text.lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", text or ""):
        raise ValueError("看不出频道用户名，试试粘贴 t.me/xxx 这样的链接")
    return text


def add_channel(link: str) -> dict:
    """解析用户名、连网取频道标题与 chat_id，落库。"""
    username = parse_username(link)
    client = get_client()  # 没登录就抛 TgNotLoggedIn
    try:
        entity = _call(client.get_entity(username), timeout=60)
    except Exception as exc:
        raise ValueError(f"找不到频道 @{username}：{_friendly(exc)}") from exc

    title = (getattr(entity, "title", "") or "").strip() or f"@{username}"
    chat_id = int(getattr(entity, "id", 0) or 0)
    existing = db.query_one(
        "SELECT id FROM tg_channels WHERE username = ? OR (chat_id IS NOT NULL AND chat_id = ?)",
        (username, chat_id),
    )
    if existing is not None:
        raise ValueError("这个频道已经在列表里")

    db.execute(
        "INSERT INTO tg_channels(title, username, chat_id, created_at) VALUES(?,?,?,?)",
        (title, username, chat_id, repo.now()),
    )
    return {"ok": True, "title": title, "username": username}


def list_channels() -> list[dict]:
    rows = db.query("SELECT * FROM tg_channels ORDER BY id")
    channels = []
    for row in rows:
        channel = dict(row)
        channel["enabled"] = bool(channel["enabled"])
        channels.append(channel)
    return channels


def enabled_channels() -> list[dict]:
    return [channel for channel in list_channels() if channel["enabled"]]


def get_channel(channel_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM tg_channels WHERE id = ?", (channel_id,))
    return dict(row) if row is not None else None


def remove_channel(channel_id: int) -> None:
    db.execute("DELETE FROM tg_channels WHERE id = ?", (channel_id,))


def toggle_channel(channel_id: int) -> None:
    db.execute(
        "UPDATE tg_channels SET enabled = 1 - enabled WHERE id = ?", (channel_id,)
    )


# ------------------------------------------------------------------ 同步


def _bump_cursor(channel_id: int, message_id: int) -> None:
    """游标只前进不后退，同步中断也能从断点续跑。"""
    db.execute(
        "UPDATE tg_channels SET last_message_id = MAX(last_message_id, ?) WHERE id = ?",
        (message_id, channel_id),
    )


async def _resolve_entity(client: TelegramClient, channel: dict):
    """优先按 chat_id 取实体（防频道改名），重登后实体缓存丢了就退回用户名。"""
    try:
        return await client.get_entity(channel["chat_id"] or channel["username"])
    except ValueError:
        if channel["chat_id"] and channel["username"]:
            return await client.get_entity(channel["username"])
        raise


def sync_channel(
    channel_id: int,
    job_id: str,
    force_mode: str = "",
    target_chars: int | None = None,
) -> None:
    """后台线程入口：逐个下载频道里的 txt 并入库，每本写一次进度。

    手动同步跑在 BackgroundTasks 线程，自动同步跑在调度器线程，都从这里进。
    """
    log: list[dict] = []
    acquired = _sync_lock.acquire(blocking=False)
    try:
        if not acquired:
            log.append({"file": "-", "status": "error", "message": "已有同步任务在进行，请稍后再试"})
            importer._save_job(job_id, "error", len(log), log)
            return

        channel = get_channel(channel_id)
        if channel is None:
            log.append({"file": "-", "status": "error", "message": "频道不存在"})
            importer._save_job(job_id, "error", len(log), log)
            return

        try:
            client = get_client()
        except TgNotLoggedIn as exc:
            log.append({"file": "-", "status": "error", "message": str(exc)})
            importer._save_job(job_id, "error", len(log), log)
            return

        options = app_settings.load_options(force_mode=force_mode, target_chars=target_chars)

        try:
            entity = _call(_resolve_entity(client, channel), timeout=60)
        except AuthKeyError:
            _invalidate_session()
            log.append({"file": "-", "status": "error", "message": "TG 会话已失效，请重新登录"})
            importer._save_job(job_id, "error", len(log), log)
            return
        except Exception as exc:
            log.append(
                {"file": "-", "status": "error", "message": f"频道取不到：{_friendly(exc)}"}
            )
            importer._save_job(job_id, "error", len(log), log)
            return

        # 元数据遍历：只翻页不下载，数出 txt 个数当 total，让进度条有准确分母。
        async def collect() -> list:
            items = []
            async for message in client.iter_messages(
                entity, min_id=channel["last_message_id"] or 0
            ):
                name = message.file.name if message.file else None
                if not name or not name.lower().endswith(importer.TXT_SUFFIXES):
                    continue  # 只收 txt，图片/视频/epub 等直接忽略
                items.append(message)
            items.sort(key=lambda m: m.id)
            return items

        try:
            messages = _call(collect())
        except AuthKeyError:
            _invalidate_session()
            log.append({"file": "-", "status": "error", "message": "TG 会话已失效，请重新登录"})
            importer._save_job(job_id, "error", len(log), log)
            return
        except Exception as exc:
            log.append(
                {"file": "-", "status": "error", "message": f"翻频道消息失败：{_friendly(exc)}"}
            )
            importer._save_job(job_id, "error", len(log), log)
            return

        # 超过大小上限的不浪费流量下载，直接记错误
        oversize: list = []
        download: list = []
        for message in messages:
            if (message.file.size or 0) > config.MAX_TXT_BYTES:
                oversize.append(message)
            else:
                download.append(message)
        total = len(download) + len(oversize)
        log.extend(
            {
                "file": message.file.name,
                "status": "error",
                "message": f"文件超过 {config.MAX_TXT_BYTES // 1024 // 1024} MB 上限，未下载",
            }
            for message in oversize
        )
        if log:
            importer._save_job(job_id, "running", total, log)
        for message in oversize:  # 超限的不会下载，游标直接推进，别每次同步都重新列出来
            _bump_cursor(channel_id, message.id)

        for message in download:
            record = _sync_one(client, message, options)
            log.append(record)
            # 游标只在成功或判重跳过时推进：下载失败的留给下次同步重试
            if record["status"] in ("ok", "skip"):
                _bump_cursor(channel_id, message.id)
            importer._save_job(job_id, "running", total, log)  # 每本写一次，前台逐本可见
            if record.get("auth_failed"):
                log.append(
                    {"file": "-", "status": "error", "message": "TG 会话已失效，剩余文件留待重新登录后再同步"}
                )
                break

        db.execute("UPDATE tg_channels SET last_sync_at = ? WHERE id = ?", (repo.now(), channel_id))
        importer._save_job(job_id, "done", total, log)
    finally:
        if acquired:
            _sync_lock.release()
        db.close_conn()  # 后台线程用完就还回去，别占着 sqlite 连接


def _sync_one(client: TelegramClient, message, options) -> dict:
    """下载单条消息的 txt 附件并走完整导入管线，返回一条日志记录。"""
    name = message.file.name or f"tg_{message.id}.txt"

    async def run() -> bytes:
        buffer = BytesIO()
        result = await client.download_media(message, file=buffer)
        if result is None:
            raise RuntimeError("文件已不在频道里（可能被撤回或删除）")
        return buffer.getvalue()

    for attempt in range(3):  # FloodWait 睡一会儿重试
        try:
            data = _call(run(), timeout=DOWNLOAD_TIMEOUT)
            return importer.import_one(name, data, options)
        except AuthKeyError:
            _invalidate_session()
            return {
                "file": name,
                "status": "error",
                "auth_failed": True,
                "message": "TG 会话已失效，请重新登录",
            }
        except FloodWaitError as exc:
            if attempt == 2:
                return {
                    "file": name,
                    "status": "error",
                    "message": f"触发限流，需等 {exc.seconds} 秒，已跳过",
                }
            time.sleep(min(exc.seconds, FLOOD_SLEEP_CAP) + 1)
        except Exception as exc:
            return {"file": name, "status": "error", "message": f"下载失败：{_friendly(exc)}"}

    return {"file": name, "status": "error", "message": "下载失败"}


def sync_busy() -> bool:
    """是否有同步任务正在跑（含即将排队的窗口期）。"""
    return _sync_lock.locked()


def sync_channels(
    entries: list[tuple[int, str]],
    force_mode: str = "",
    target_chars: int | None = None,
) -> None:
    """「全部同步」入口：每频道一个 job，串行跑完。"""
    try:
        for channel_id, job_id in entries:
            sync_channel(channel_id, job_id, force_mode, target_chars)
    finally:
        db.close_conn()


# ------------------------------------------------------------------ 定期自动同步


def start_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="tg-sync-scheduler", daemon=True
    )
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _scheduler_stop.set()


def _scheduler_loop() -> None:
    """每分钟醒一次看是否到了自动同步时间；任何异常都不能让线程死掉。"""
    while not _scheduler_stop.wait(60):
        try:
            _auto_tick()
        except Exception:
            pass
        finally:
            db.close_conn()  # 长驻线程别一直占着连接


def _auto_tick() -> None:
    try:
        interval = int(get_setting("tg_sync_interval") or 0)
    except ValueError:
        interval = 0
    if interval <= 0:
        return
    last = get_setting("tg_last_auto_run")
    now = time.time()
    if last:
        try:
            if now - float(last) < interval * 60:
                return
        except ValueError:
            pass
    if not logged_in():
        return
    channels = enabled_channels()
    if not channels:
        return
    set_setting("tg_last_auto_run", str(int(now)))  # 先占坑，防下一轮重叠触发
    for channel in channels:
        job_id = importer.create_job(0)
        sync_channel(channel["id"], job_id)


# ------------------------------------------------------------------ 退出清理


def shutdown() -> None:
    """main.py lifespan 退出时调：停调度器、断开 client、停 loop 线程。"""
    global _loop, _client
    stop_scheduler()
    _drop_pending(force=True)
    with _state_lock:
        client = _client
        _client = None
    if client is not None:
        try:
            _call(_disconnect(client), timeout=10)
        except Exception:
            pass
    loop = _loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
    _loop = None


# ------------------------------------------------------------------ 杂项


def _friendly(exc: Exception) -> str:
    """把 Telethon 的异常翻成能给管理员看的中文。"""
    name = type(exc).__name__
    if name in ("PhoneNumberInvalidError",):
        return "手机号格式不对"
    if name in ("PhoneCodeInvalidError", "PhoneCodeExpiredError"):
        return "验证码不对或已过期"
    if name in ("PasswordHashInvalidError",):
        return "云密码不对"
    if name in ("UsernameNotOccupiedError", "UsernameInvalidError"):
        return "用户名不存在或不可用"
    if name in ("FloodWaitError",):
        return f"触发限流，需等 {getattr(exc, 'seconds', '?')} 秒"
    if name in ("TimeoutError",):
        return "连接 Telegram 超时"
    message = str(exc).strip()
    return message or name
