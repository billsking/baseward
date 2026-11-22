"""Admin sign-in hardening: brute-force guard, login audit, and security headers."""

from __future__ import annotations

import base64
import re
import time

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.config import Settings
from app.db.models import AdminUser, AuditLog
from app.main import create_app
from app.security.admin_auth import COOKIE_NAME, AdminSession, serialize_session
from app.security.login_guard import LoginGuard

MASTER_KEY = base64.b64encode(b"k" * 32).decode("ascii")
NONCE_PATTERN = re.compile(r"'nonce-([^']+)'")


def base_settings(tmp_path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "master_key": MASTER_KEY,
        "admin_user": "admin",
        "admin_password": "admin-test-123",
        "db_path": str(tmp_path / "gateway.db"),
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


# ---------- LoginGuard ----------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_guard_locks_after_threshold_and_unlocks_over_time() -> None:
    clock = FakeClock()
    guard = LoginGuard(3, 300, 60, clock=clock)
    assert guard.record_failure("admin", "10.0.0.1") == 0
    assert guard.record_failure("admin", "10.0.0.1") == 0
    assert guard.record_failure("admin", "10.0.0.1") == 60
    assert guard.retry_after("admin", "10.0.0.1") == 60

    clock.now += 59
    assert guard.retry_after("admin", "10.0.0.1") == 1
    clock.now += 2
    assert guard.retry_after("admin", "10.0.0.1") == 0


def test_guard_reset_clears_failures_and_lock() -> None:
    guard = LoginGuard(1, 300, 60)
    assert guard.record_failure("admin", "10.0.0.1") == 60
    guard.reset("admin", "10.0.0.1")
    assert guard.retry_after("admin", "10.0.0.1") == 0
    # A fresh failure starts a new count instead of relocking immediately.
    assert guard.record_failure("admin", "10.0.0.1") == 60


def test_guard_isolates_username_and_ip() -> None:
    clock = FakeClock()
    guard = LoginGuard(2, 300, 60, clock=clock)
    guard.record_failure("admin", "10.0.0.1")
    assert guard.record_failure("admin", "10.0.0.2") == 0
    assert guard.retry_after("admin", "10.0.0.2") == 0
    assert guard.record_failure("other", "10.0.0.1") == 0
    assert guard.retry_after("other", "10.0.0.1") == 0
    assert guard.record_failure("admin", "10.0.0.1") == 60


def test_guard_window_expiry_restarts_count() -> None:
    clock = FakeClock()
    guard = LoginGuard(2, 300, 60, clock=clock)
    guard.record_failure("admin", "10.0.0.1")
    clock.now += 301
    assert guard.record_failure("admin", "10.0.0.1") == 0
    assert guard.retry_after("admin", "10.0.0.1") == 0


def test_guard_folds_ipv4_mapped_addresses() -> None:
    clock = FakeClock()
    guard = LoginGuard(2, 300, 60, clock=clock)
    guard.record_failure("admin", "::ffff:10.0.0.1")
    assert guard.record_failure("admin", "10.0.0.1") == 60


def test_admin_login_settings_bounds() -> None:
    with pytest.raises(PydanticValidationError):
        Settings(
            _env_file=None,
            master_key=MASTER_KEY,
            admin_login_max_attempts=0,
        )


# ---------- Sign-in flow, audit, and headers ----------


@pytest.fixture
def clean_db_env(monkeypatch) -> None:
    for key in (
        "GATEWAY_DB_URL",
        "GATEWAY_DB_HOST",
        "GATEWAY_DB_USER",
        "GATEWAY_DB_PASSWORD",
        "GATEWAY_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(clean_db_env, tmp_path) -> Settings:
    return base_settings(
        tmp_path,
        admin_login_max_attempts=2,
        admin_login_window_seconds=300,
        admin_login_lock_seconds=60,
    )


async def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    )


async def test_failed_logins_lock_and_are_audited(settings) -> None:
    app = create_app(settings)
    async with LifespanManager(app):
        async with await _client(app) as client:
            first = await client.post(
                "/admin/login", data={"username": "admin", "password": "wrong"}
            )
            assert first.status_code == 401
            assert "用户名或密码错误" in first.text

            second = await client.post(
                "/admin/login", data={"username": "admin", "password": "wrong"}
            )
            assert second.status_code == 429
            assert "秒后重试" in second.text

            # A correct password is still refused while the pair is locked.
            locked = await client.post(
                "/admin/login",
                data={"username": "admin", "password": "admin-test-123"},
            )
            assert locked.status_code == 429

    async with app.state.services.session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == "admin_login")
                .order_by(AuditLog.id)
            )
        ).all()
    assert len(rows) == 3
    assert {row.status for row in rows} == {"denied"}
    assert all(row.request_id for row in rows)
    assert all(row.client_ip for row in rows)
    assert [row.deny_reason for row in rows] == [
        "INVALID_CREDENTIALS",
        "LOCKED_OUT",
        "LOCKED_OUT",
    ]


async def test_successful_login_is_audited_and_clears_failures(settings) -> None:
    app = create_app(settings)
    async with LifespanManager(app):
        async with await _client(app) as client:
            failure = await client.post(
                "/admin/login", data={"username": "admin", "password": "wrong"}
            )
            assert failure.status_code == 401
            success = await client.post(
                "/admin/login",
                data={"username": "admin", "password": "admin-test-123"},
            )
            assert success.status_code == 302
            assert success.headers["location"] == "/admin"

    # The failed attempt was cleared, so a new failure does not immediately trigger the lock.
    async with app.state.services.session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.event_type == "admin_login")
                .order_by(AuditLog.id)
            )
        ).all()
    assert [row.status for row in rows] == ["denied", "success"]
    assert rows[1].user_name == "admin"


async def test_security_headers_and_csp_nonce(settings) -> None:
    app = create_app(settings)
    async with LifespanManager(app):
        async with await _client(app) as client:
            response = await client.get("/admin/login")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in response.headers

    csp = response.headers["content-security-policy"]
    nonce = NONCE_PATTERN.search(csp).group(1)
    assert "'unsafe-inline'" not in csp
    assert f'nonce="{nonce}"' in response.text


async def test_hsts_only_when_cookie_secure(clean_db_env, tmp_path) -> None:
    app = create_app(base_settings(tmp_path, cookie_secure=True))
    async with LifespanManager(app):
        async with await _client(app) as client:
            response = await client.get("/admin/login")
    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


async def test_admin_pages_are_not_cacheable(settings) -> None:
    app = create_app(settings)
    async with LifespanManager(app):
        async with await _client(app) as client:
            page = await client.get("/admin/login")
            asset = await client.get("/admin/static/app.css")
            health = await client.get("/healthz")

    assert page.headers["cache-control"] == "no-store"
    assert page.headers["pragma"] == "no-cache"
    assert page.headers["cross-origin-opener-policy"] == "same-origin"
    assert page.headers["permissions-policy"] == (
        "geolocation=(), microphone=(), camera=()"
    )
    assert "no-store" not in asset.headers.get("cache-control", "")
    assert "no-store" not in health.headers.get("cache-control", "")


async def _admin_cookie(app, idle_seconds: int) -> str:
    """Build a signed cookie whose last activity is `idle_seconds` ago."""
    async with app.state.services.session_factory() as session:
        admin = await session.scalar(select(AdminUser))
    payload = AdminSession(
        admin_id=admin.id,
        username=admin.username,
        version=admin.session_version,
        csrf="c" * 64,
        last=int(time.time()) - idle_seconds,
    )
    return serialize_session(payload, app.state.services)


async def test_idle_session_expires_and_cookie_is_cleared(clean_db_env, tmp_path) -> None:
    app = create_app(base_settings(tmp_path, admin_session_idle_minutes=1))
    async with LifespanManager(app):
        async with await _client(app) as client:
            token = await _admin_cookie(app, idle_seconds=120)
            response = await client.get(
                "/admin", headers={"cookie": f"{COOKIE_NAME}={token}"}
            )

    assert response.status_code == 401
    deleted = response.headers["set-cookie"]
    assert deleted.startswith(f"{COOKIE_NAME}=")
    assert "Max-Age=0" in deleted


async def test_active_session_slides_the_idle_timer(clean_db_env, tmp_path) -> None:
    from app.security.admin_auth import read_session_token

    app = create_app(base_settings(tmp_path))
    async with LifespanManager(app):
        async with await _client(app) as client:
            token = await _admin_cookie(app, idle_seconds=120)
            response = await client.get(
                "/admin", headers={"cookie": f"{COOKIE_NAME}={token}"}
            )

    assert response.status_code == 200
    refreshed_header = response.headers["set-cookie"]
    refreshed_token = refreshed_header.split(";", 1)[0].split("=", 1)[1]
    refreshed = read_session_token(refreshed_token, app.state.services)
    assert refreshed is not None
    assert refreshed.last > int(time.time()) - 120
