"""管理员口令校验与签名会话 cookie。"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import config

_signer = TimestampSigner(config.SECRET_KEY, salt="admin-session")

_PBKDF2_ROUNDS = 200_000
_PBKDF2_SALT = b"novel-admin-salt"


def _hash(password: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _PBKDF2_SALT, _PBKDF2_ROUNDS
    )


_EXPECTED = _hash(config.ADMIN_PASSWORD)


def verify_password(password: str) -> bool:
    """常量时间比较，避免按字符试探。"""
    return hmac.compare_digest(_hash(password), _EXPECTED)


def make_session_token() -> str:
    return _signer.sign(b"admin").decode("ascii")


def token_is_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        _signer.unsign(token, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return True


def is_admin(request: Request) -> bool:
    return token_is_valid(request.cookies.get(config.SESSION_COOKIE))


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/")


class AdminRedirect(Exception):
    """未登录访问后台页面时抛出，由异常处理器转成 302。"""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def require_admin_page(request: Request) -> None:
    """页面守卫：未登录跳登录页并带回跳地址。"""
    if not is_admin(request):
        raise AdminRedirect(request.url.path)


def require_admin_api(request: Request) -> None:
    """接口守卫：未登录直接 401，不做跳转。"""
    if not is_admin(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要管理员登录")


def redirect_to_login(next_url: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/admin/login?next={quote(next_url, safe='')}", status_code=302)


def check_api_token(request: Request) -> None:
    """API_TOKEN 未配置时公开；配置后接受 ?token= 或 Authorization 头。"""
    if not config.API_TOKEN:
        return
    supplied = request.query_params.get("token") or ""
    if not supplied:
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
    if not hmac.compare_digest(supplied, config.API_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效的 API token")
