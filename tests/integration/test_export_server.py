"""AC14/AC22: export listener assembly, /healthz state, and port/pool release after shutdown."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app
from tests.integration.conftest import (
    connect_export,
    export_settings,
    issue_credential,
)

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _select_one(port: int, account: str, password: str) -> int:
    connection = connect_export(port, account, password)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            return int(row[0])
    finally:
        connection.close()


async def _healthz(app) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    return response.json()["mysql_export"]


async def test_export_disabled_has_no_listener(env):
    port = _free_port()
    settings = export_settings(env, mysql_export_enabled=False, mysql_export_port=port)
    app = create_app(settings)
    async with LifespanManager(app):
        status = await _healthz(app)
        assert status == {
            "enabled": False,
            "listening": False,
            "active_exports": 0,
            "max_concurrent": settings.mysql_export_max_concurrent,
            "allowlist_source": "env",
            "allowlist_entries": 1,
        }
        assert app.state.export_server is None
        assert not _listening(port)


def test_disabled_does_not_import_protocol_modules():
    """A disabled process must not contain app.mysql.* — patches apply only through the lazy import when enabled."""
    master_key = base64.b64encode(b"t" * 32).decode("ascii")
    code = "\n".join(
        [
            "import sys",
            "from app.config import Settings",
            "from app.main import create_app",
            "settings = Settings(",
            "    _env_file=None,",
            f"    master_key={master_key!r},",
            "    db_host='127.0.0.1',",
            "    db_user='user',",
            "    db_password='pass',",
            ")",
            "create_app(settings)",
            "print(sorted(name for name in sys.modules if name.startswith('app.mysql')))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GATEWAY_MYSQL_EXPORT_ENABLED": "false"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


async def test_export_enabled_listens_and_releases(env):
    port = _free_port()
    settings = export_settings(
        env, mysql_export_host="127.0.0.1", mysql_export_port=port
    )
    account, password = await issue_credential(env)
    app = create_app(settings)
    async with LifespanManager(app):
        status = await _healthz(app)
        assert status["enabled"] is True
        assert status["listening"] is True
        assert status["active_exports"] == 0
        assert status["max_concurrent"] == settings.mysql_export_max_concurrent
        assert await asyncio.to_thread(_select_one, port, account, password) == 1
        assert app.state.export_server.sockets()

    assert app.state.export_server.sockets() == ()
    # The export pool closes with Services.close(): no upstream connections remain.
    assert not app.state.services.export_pool._pools
    assert not _listening(port)
