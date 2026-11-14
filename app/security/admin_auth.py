"""Admin sessions, CSRF, and password changes."""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import Response

from app.clock import utcnow
from app.db.models import AdminUser
from app.errors import AdminUnauthorized, CSRFError, ValidationError
from app.security.crypto import hash_password, verify_password

logger = logging.getLogger(__name__)

COOKIE_NAME = "gw_admin_session"
SESSION_MAX_AGE = 28800
SESSION_SALT = "dbgw-admin-session"
CSRF_TOKEN_BYTES = 32


@dataclass(frozen=True)
class AdminSession:
    admin_id: int
    username: str
    version: int
    csrf: str


def _serializer(services) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(services.session_key, salt=SESSION_SALT)


def create_session(response: Response, admin: AdminUser, services) -> AdminSession:
    csrf = secrets.token_hex(CSRF_TOKEN_BYTES)
    session = AdminSession(
        admin_id=admin.id,
        username=admin.username,
        version=admin.session_version,
        csrf=csrf,
    )
    payload = {
        "uid": session.admin_id,
        "uname": session.username,
        "ver": session.version,
        "csrf": session.csrf,
    }
    token = _serializer(services).dumps(payload)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/admin",
        secure=services.settings.cookie_secure,
    )
    return session


def read_session(request: Request, services) -> AdminSession | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = _serializer(services).loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    admin_id = payload.get("uid")
    username = payload.get("uname")
    version = payload.get("ver")
    csrf = payload.get("csrf")
    if not isinstance(admin_id, int) or not isinstance(username, str):
        return None
    if not isinstance(version, int) or not isinstance(csrf, str):
        return None
    return AdminSession(admin_id=admin_id, username=username, version=version, csrf=csrf)


async def require_admin(request: Request, services) -> AdminSession:
    session = read_session(request, services)
    if session is None:
        raise AdminUnauthorized("未登录或会话已过期")
    async with services.session_factory() as db:
        admin = await db.get(AdminUser, session.admin_id)
    if admin is None or admin.username != session.username:
        raise AdminUnauthorized("会话无效")
    if admin.session_version != session.version:
        raise AdminUnauthorized("会话已失效，请重新登录")
    return session


def verify_csrf(session: AdminSession, form_token: str | None) -> None:
    if not form_token or not hmac.compare_digest(session.csrf, form_token):
        raise CSRFError("CSRF 校验失败")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/admin")


async def authenticate_admin(services, username: str, password: str) -> AdminUser | None:
    username = (username or "").strip()
    if not username or not password:
        return None
    async with services.session_factory() as db:
        admin = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if admin is None or not verify_password(password, admin.password_hash):
        return None
    return admin


async def change_password(services, admin_id: int, old_password: str, new_password: str) -> None:
    if len(new_password or "") < 8:
        raise ValidationError("新密码至少 8 字符")
    async with services.session_factory() as db:
        admin = await db.get(AdminUser, admin_id)
        if admin is None:
            raise ValidationError("管理员不存在")
        if not verify_password(old_password or "", admin.password_hash):
            raise ValidationError("旧密码不正确")
        if verify_password(new_password, admin.password_hash):
            raise ValidationError("新密码不能与旧密码相同")
        admin.password_hash = hash_password(new_password)
        admin.session_version += 1
        admin.updated_at = utcnow()
        await db.commit()
