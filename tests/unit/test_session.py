"""Admin session and CSRF unit tests (no MySQL)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.errors import AdminUnauthorized, CSRFError, ValidationError
from app.security import admin_auth
from app.security.admin_auth import (
    COOKIE_NAME,
    AdminSession,
    authenticate_admin,
    change_password,
    clear_session,
    create_session,
    read_session,
    require_admin,
    verify_csrf,
)
from app.security.crypto import hash_password


class FakeDb:
    def __init__(self, admin=None) -> None:
        self.admin = admin
        self.commits = 0

    async def get(self, model, pk):
        if self.admin is not None and self.admin.id == pk:
            return self.admin
        return None

    async def scalar(self, stmt):
        return self.admin

    async def commit(self) -> None:
        self.commits += 1


class FakeFactory:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    def __call__(self):
        return _Ctx(self.db)


class _Ctx:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    async def __aenter__(self) -> FakeDb:
        return self.db

    async def __aexit__(self, *args) -> None:
        return None


def make_services(db: FakeDb, cookie_secure: bool = False):
    return SimpleNamespace(
        session_factory=FakeFactory(db),
        session_key=b"s" * 32,
        settings=SimpleNamespace(cookie_secure=cookie_secure, admin_session_idle_minutes=30),
    )


def make_admin(admin_id=1, username="admin", password="password123", version=0):
    return SimpleNamespace(
        id=admin_id,
        username=username,
        password_hash=hash_password(password),
        session_version=version,
    )


def request_with_cookie(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/",
            "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())],
        }
    )


def test_create_session_sets_secure_cookie_attributes():
    services = make_services(FakeDb(), cookie_secure=True)
    response = Response()
    session = create_session(response, make_admin(), services)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/admin" in cookie
    assert "Max-Age=28800" in cookie
    assert "Secure" in cookie
    assert len(session.csrf) == 64


def test_read_session_roundtrip():
    services = make_services(FakeDb())
    response = Response()
    session = create_session(response, make_admin(), services)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    loaded = read_session(request_with_cookie(token), services)
    assert loaded is not None
    assert loaded.admin_id == session.admin_id
    assert loaded.username == "admin"
    assert loaded.version == 0
    assert loaded.csrf == session.csrf


def test_read_session_missing_and_tampered():
    services = make_services(FakeDb())
    assert read_session(request_with_cookie(""), services) is None

    response = Response()
    create_session(response, make_admin(), services)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    tampered = token[:-2] + ("a" if token[-2] != "a" else "b") + token[-1]
    assert read_session(request_with_cookie(tampered), services) is None


def test_read_session_expired(monkeypatch):
    services = make_services(FakeDb())
    response = Response()
    create_session(response, make_admin(), services)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    monkeypatch.setattr(admin_auth, "SESSION_MAX_AGE", -1)
    assert read_session(request_with_cookie(token), services) is None


async def test_require_admin_ok():
    admin = make_admin()
    services = make_services(FakeDb(admin))
    response = Response()
    create_session(response, admin, services)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    session = await require_admin(request_with_cookie(token), services)
    assert session.admin_id == 1


async def test_require_admin_rejects_missing_invalid_and_stale():
    admin = make_admin()
    services = make_services(FakeDb(admin))

    with pytest.raises(AdminUnauthorized):
        await require_admin(request_with_cookie(""), services)

    response = Response()
    create_session(response, admin, services)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    admin.session_version = 1
    with pytest.raises(AdminUnauthorized):
        await require_admin(request_with_cookie(token), services)

    admin.session_version = 0
    admin.username = "renamed"
    with pytest.raises(AdminUnauthorized):
        await require_admin(request_with_cookie(token), services)


async def test_require_admin_rejects_deleted_admin():
    admin = make_admin()
    services = make_services(FakeDb(admin))
    response = Response()
    create_session(response, admin, services)
    token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    services.session_factory = FakeFactory(FakeDb(None))
    with pytest.raises(AdminUnauthorized):
        await require_admin(request_with_cookie(token), services)


def test_verify_csrf():
    session = AdminSession(
        admin_id=1, username="admin", version=0, csrf="a" * 64, last=0
    )
    verify_csrf(session, "a" * 64)
    with pytest.raises(CSRFError):
        verify_csrf(session, "b" * 64)
    with pytest.raises(CSRFError):
        verify_csrf(session, None)


def test_clear_session():
    response = Response()
    clear_session(response)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "Path=/admin" in cookie
    assert "Max-Age=0" in cookie


async def test_authenticate_admin():
    admin = make_admin(password="password123")
    services = make_services(FakeDb(admin))

    assert await authenticate_admin(services, "admin", "password123") is admin
    assert await authenticate_admin(services, "admin", "wrong") is None

    empty = make_services(FakeDb(None))
    assert await authenticate_admin(empty, "nobody", "password123") is None
    assert await authenticate_admin(empty, "", "password123") is None
    assert await authenticate_admin(empty, "admin", "") is None


async def test_change_password_increments_version():
    admin = make_admin(password="oldpassword")
    db = FakeDb(admin)
    services = make_services(db)

    await change_password(services, admin.id, "oldpassword", "newpassword")
    assert admin.session_version == 1
    assert db.commits == 1
    from app.security.crypto import verify_password

    assert verify_password("newpassword", admin.password_hash)


async def test_change_password_validation():
    admin = make_admin(password="oldpassword")
    services = make_services(FakeDb(admin))

    with pytest.raises(ValidationError):
        await change_password(services, admin.id, "oldpassword", "short")
    with pytest.raises(ValidationError):
        await change_password(services, admin.id, "wrong", "newpassword")
    with pytest.raises(ValidationError):
        await change_password(services, admin.id, "oldpassword", "oldpassword")
