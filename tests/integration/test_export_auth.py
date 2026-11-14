"""Export-channel authentication: all seven deny codes return 1045, audits distinguish them, and allowlist/KILL rejections.

Start a mysql-mimic server directly (without the full app assembly) with the real Services, the real
CredentialIdentityProvider / ExportControl, and a probe session that records the bad-password audit
after the handshake (same logic as ExportSession.init, see D-06).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pymysql
import pytest
from mysql_mimic import Session
from sqlalchemy import text

from app.config import Settings
from app.mysql.control import ExportControl
from app.mysql.identity import (
    CredentialIdentityProvider,
    ExportServerContext,
    audit_handshake_failure,
    client_ip_of,
)
from app.mysql.server import ExportMysqlServer
from tests.integration.conftest import export_allowlist, management_scalar

pytestmark = pytest.mark.integration

ALLOW_LOCAL = "127.0.0.1/32"


class _ProbeSession(Session):
    """Verify authentication and admission only; queries go through the built-in local middleware."""

    def __init__(self, services, context: ExportServerContext) -> None:
        super().__init__()
        self.services = services
        self.context = context
        self.client_ip = ""
        self.connection_id: int | None = None

    async def init(self, connection) -> None:
        await super().init(connection)
        self.client_ip = client_ip_of(connection)
        self.connection_id = getattr(connection, "connection_id", None)
        if self.username is None:
            attempted = self.variables.get("external_user") or ""
            await audit_handshake_failure(
                self.services,
                self.context,
                self.connection_id,
                str(attempted),
                self.client_ip,
            )


def _export_settings(env, **overrides) -> Settings:
    data = env.settings.model_dump()
    data.update(
        {
            "mysql_export_enabled": True,
            "mysql_export_allow_cidrs": ALLOW_LOCAL,
        }
    )
    data.update(overrides)
    return Settings(_env_file=None, **data)


@asynccontextmanager
async def _running_server(env, settings: Settings):
    context = ExportServerContext()
    sessions: list[_ProbeSession] = []

    def _factory() -> _ProbeSession:
        session = _ProbeSession(env.services, context)
        sessions.append(session)
        return session

    server = ExportMysqlServer(
        session_factory=_factory,
        control=ExportControl(env.services, export_allowlist(env, settings)),
        identity_provider=CredentialIdentityProvider(env.services, settings, context),
        host="127.0.0.1",
        port=0,
    )
    await server.start_server()
    port = server.sockets()[0].getsockname()[1]
    try:
        yield port, context, sessions
    finally:
        server.close()
        await server.wait_closed()


def _connect(port: int, account: str, password: str):
    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user=account,
        password=password,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        autocommit=True,
    )


def _ping(connection) -> tuple:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()


async def _ds(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    assert ds is not None
    return ds


async def _issue(env, source_ip: str | None = None) -> tuple[str, str]:
    token_row = (await env.services.tokens.list_all())[0]
    datasource = await _ds(env)
    return await env.services.credentials.issue(
        token_id=token_row.id,
        datasource_id=datasource.id,
        database=datasource.default_database,
        ttl_hours=1,
        admin_username="admin",
        client_ip="127.0.0.1",
        source_ip=source_ip,
    )


async def _set_bound_ip(env, account: str, source_ip: str | None) -> None:
    """Rewrite the binding directly to simulate a legacy credential from before the migration (source_ip NULL)."""
    async with env.services.session_factory() as session:
        await session.execute(
            text("UPDATE export_credential SET source_ip = :source_ip "
                 "WHERE account_name = :account"),
            {"source_ip": source_ip, "account": account},
        )
        await session.commit()


async def _wait_deny_reason(
    env, code: str, event_type: str = "mysql_auth", timeout: float = 5.0
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    sql = (
        "SELECT COUNT(*) FROM audit_log "
        "WHERE event_type=:event_type AND status='denied' AND deny_reason=:code"
    )
    while True:
        count = await management_scalar(
            env, sql, {"event_type": event_type, "code": code}
        )
        if count:
            return
        if loop.time() >= deadline:
            raise AssertionError(f"未等到审计 deny_reason={code}")
        await asyncio.sleep(0.05)


async def _expect_denied(env, account: str, password: str, code: str) -> None:
    settings = _export_settings(env)
    async with _running_server(env, settings) as (port, _, _):
        with pytest.raises(pymysql.err.OperationalError) as excinfo:
            await asyncio.to_thread(_connect, port, account, password)
    assert excinfo.value.args[0] == 1045, excinfo.value
    await _wait_deny_reason(env, code)


async def test_correct_password_authenticates(env):
    account, password = await _issue(env)
    async with _running_server(env, _export_settings(env)) as (port, context, sessions):
        connection = await asyncio.to_thread(_connect, port, account, password)
        try:
            assert await asyncio.to_thread(_ping, connection) == (1,)
            assert sessions and sessions[0].connection_id is not None
            assert context.get(sessions[0].connection_id) is not None
        finally:
            await asyncio.to_thread(connection.close)


async def test_unknown_account_is_1045(env):
    await _expect_denied(env, "exp_not_exists", "whatever", "CREDENTIAL_NOT_FOUND")


async def test_credential_from_other_ip_is_1045(env):
    """Binding IP differs from the connection source: the handshake is rejected, still with a uniform 1045."""
    account, password = await _issue(env, source_ip="10.99.99.99")
    await _expect_denied(env, account, password, "CREDENTIAL_IP_MISMATCH")


async def test_legacy_unbound_credential_still_connects(env):
    """Legacy credentials (source_ip NULL) skip the source check and remain usable until expiry."""
    account, password = await _issue(env)
    await _set_bound_ip(env, account, None)
    async with _running_server(env, _export_settings(env)) as (port, _, _):
        connection = await asyncio.to_thread(_connect, port, account, password)
        try:
            assert await asyncio.to_thread(_ping, connection) == (1,)
        finally:
            await asyncio.to_thread(connection.close)


async def test_expired_credential_is_1045(env):
    account, password = await _issue(env)
    async with env.services.session_factory() as session:
        await session.execute(
            text(
                "UPDATE export_credential SET expires_at = '2000-01-01 00:00:00' "
                "WHERE account_name = :account"
            ),
            {"account": account},
        )
        await session.commit()
    await _expect_denied(env, account, password, "CREDENTIAL_EXPIRED")


async def test_revoked_credential_is_1045(env):
    account, password = await _issue(env)
    credential = (await env.services.credentials.list_all())[0]
    await env.services.credentials.revoke(credential.id, "admin", "127.0.0.1")
    await _expect_denied(env, account, password, "CREDENTIAL_REVOKED")


async def test_wrong_password_is_1045_and_audited(env):
    account, _ = await _issue(env)
    await _expect_denied(env, account, "wrong-password", "CREDENTIAL_PASSWORD_INVALID")


async def test_disabled_token_is_1045(env):
    account, password = await _issue(env)
    token_row = (await env.services.tokens.list_all())[0]
    await env.services.tokens.disable(token_row.id)
    await _expect_denied(env, account, password, "TOKEN_DISABLED")


async def test_disabled_datasource_is_1045(env):
    account, password = await _issue(env)
    datasource = await _ds(env)
    await env.services.datasources.set_enabled(datasource.id, False)
    await _expect_denied(env, account, password, "DATASOURCE_DISABLED")


async def test_missing_datasource_is_1045(env):
    account, password = await _issue(env)
    async with env.services.session_factory() as session:
        await session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        await session.execute(
            text(
                "UPDATE export_credential SET datasource_id = 999999 "
                "WHERE account_name = :account"
            ),
            {"account": account},
        )
        await session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        await session.commit()
    await _expect_denied(env, account, password, "DATASOURCE_MISSING")


async def test_ip_outside_allowlist_is_closed_before_auth(env):
    account, password = await _issue(env)
    settings = _export_settings(env, mysql_export_allow_cidrs="10.0.0.0/8")
    async with _running_server(env, settings) as (port, _, _):
        with pytest.raises(pymysql.err.OperationalError):
            await asyncio.to_thread(_connect, port, account, password)
    await _wait_deny_reason(env, "IP_NOT_ALLOWED")


async def test_client_kill_is_rejected(env):
    account, password = await _issue(env)
    async with _running_server(env, _export_settings(env)) as (port, _, _):
        connection = await asyncio.to_thread(_connect, port, account, password)
        try:
            # 1235 ER_NOT_SUPPORTED_YET: pymysql maps it to NotSupportedError
            with pytest.raises(pymysql.err.NotSupportedError) as excinfo:
                await asyncio.to_thread(_execute, connection, "KILL CONNECTION 1")
            assert excinfo.value.args[0] == 1235
        finally:
            await asyncio.to_thread(connection.close)
    await _wait_deny_reason(env, "KILL_DENIED", event_type="mysql_export")


def _execute(connection, sql: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql)
