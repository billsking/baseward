"""Temporary export credentials: issue, revoke, auth lookup, and expiry cleanup."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from app.credentials.service import (
    CREDENTIAL_EXPIRED,
    CREDENTIAL_ISSUE_DENIED,
    CREDENTIAL_NOT_FOUND,
    CREDENTIAL_REVOKED,
    DATASOURCE_DISABLED,
    DATASOURCE_MISSING,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    TOKEN_DISABLED,
    CredentialAuthError,
)
from app.db.models import ExportCredential
from app.errors import NotFound, ValidationError
from app.security.crypto import native_auth_string
from app.tokens.scope import TokenScope
from tests.integration.conftest import create_datasource, extra_database

pytestmark = pytest.mark.integration


async def _principal(env):
    principal = await env.services.tokens.authenticate(env.token)
    assert principal is not None
    return principal


async def _ds_id(env, name: str | None = None) -> int:
    datasource = await env.services.datasources.get_by_name(name or env.ds_name)
    assert datasource is not None
    return datasource.id


async def _issue(env, **overrides) -> tuple[str, str]:
    principal = await _principal(env)
    payload = {
        "token_id": principal.token_id,
        "datasource_id": await _ds_id(env),
        "database": env.biz_db,
        "ttl_hours": 2,
        "admin_username": env.admin_user,
        "client_ip": "192.0.2.9",
    }
    payload.update(overrides)
    return await env.services.credentials.issue(**payload)


async def _credential_row(env, account: str):
    async with env.services.session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT account_name, auth_string, default_database, token_id, "
                    "datasource_id, issued_by, expires_at, revoked_at, last_used_at "
                    "FROM export_credential WHERE account_name = :account"
                ),
                {"account": account},
            )
        ).first()


async def _credential_by_account(env, account: str) -> ExportCredential | None:
    return await env.services.credentials.get_active(account)


async def _audit_rows(env, event_type: str):
    async with env.services.session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT tool, status, deny_reason, credential_account, user_name "
                    "FROM audit_log WHERE event_type = :event ORDER BY id"
                ),
                {"event": event_type},
            )
        ).all()


async def _expire_now(env, account: str) -> None:
    async with env.services.session_factory() as session:
        await session.execute(
            text("UPDATE export_credential SET expires_at = UTC_TIMESTAMP(3) - INTERVAL 1 SECOND "
                 "WHERE account_name = :account"),
            {"account": account},
        )
        await session.commit()


async def test_issue_returns_password_once_and_stores_only_auth_string(env, caplog):
    principal = await _principal(env)
    with caplog.at_level(logging.DEBUG):
        account, password = await _issue(env)

    assert account.startswith("exp_") and len(account) == 16
    assert len(password) == 32
    assert password not in caplog.text
    assert native_auth_string(password) not in caplog.text

    row = await _credential_row(env, account)
    assert row.auth_string == native_auth_string(password)
    assert row.auth_string != password
    assert row.default_database == env.biz_db
    assert row.issued_by == env.admin_user
    assert row.token_id == principal.token_id

    audits = await _audit_rows(env, "credential_lifecycle")
    issue_audit = [item for item in audits if item.tool == "issue"]
    assert len(issue_audit) == 1
    assert issue_audit[0].status == "success"
    assert issue_audit[0].credential_account == account
    assert issue_audit[0].deny_reason == env.admin_user
    assert password not in str(issue_audit[0])


@pytest.mark.parametrize("ttl_hours", [0, 73, -1])
async def test_issue_ttl_out_of_range(env, ttl_hours: int):
    with pytest.raises(ValidationError):
        await _issue(env, ttl_hours=ttl_hours)
    denied = [item for item in await _audit_rows(env, "credential_lifecycle") if item.status == "denied"]
    assert denied and denied[0].deny_reason == "CREDENTIAL_ISSUE_DENIED"


async def test_issue_rejects_disabled_token(env):
    principal = await _principal(env)
    ds_id = await _ds_id(env)
    await env.services.tokens.disable(principal.token_id)
    with pytest.raises(ValidationError):
        await env.services.credentials.issue(
            token_id=principal.token_id,
            datasource_id=ds_id,
            database=env.biz_db,
            ttl_hours=1,
            admin_username=env.admin_user,
            client_ip="192.0.2.9",
        )
    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0


async def test_issue_rejects_disabled_datasource(env):
    await env.services.datasources.set_enabled(await _ds_id(env), False)
    with pytest.raises(ValidationError):
        await _issue(env)
    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0


async def test_issue_rejects_datasource_outside_token_scope(env):
    other = await env.services.datasources.create(
        name="otherds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
    )
    limited_token, _ = await env.services.tokens.create(
        "limited", TokenScope(datasources=frozenset({await _ds_id(env)}))
    )
    limited = await env.services.tokens.authenticate(limited_token)
    assert limited is not None

    with pytest.raises(ValidationError) as excinfo:
        await env.services.credentials.issue(
            token_id=limited.token_id,
            datasource_id=other.id,
            database=env.biz_db,
            ttl_hours=1,
            admin_username=env.admin_user,
            client_ip="192.0.2.9",
        )
    assert "授权范围" in str(excinfo.value)
    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0

    denied = [item for item in await _audit_rows(env, "credential_lifecycle") if item.status == "denied"]
    assert denied and denied[-1].deny_reason == "CREDENTIAL_ISSUE_DENIED"


async def test_issue_binds_applicant_ip(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None
    assert record.source_ip == "192.0.2.9"


async def test_issue_overrides_bind_ip_and_normalizes(env):
    """The admin can bind a credential to another machine (the script's real egress address); the normalized value is stored."""
    account, _ = await _issue(env, source_ip="::ffff:172.16.0.9")
    record = await _credential_by_account(env, account)
    assert record is not None
    assert record.source_ip == "172.16.0.9"


async def test_issue_without_any_ip_is_refused(env):
    with pytest.raises(ValidationError, match="来源 IP"):
        await _issue(env, client_ip="")
    with pytest.raises(ValidationError, match="来源 IP"):
        await _issue(env, client_ip="192.0.2.9", source_ip="192.0.2.9/32")
    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0
    denied = [
        item
        for item in await _audit_rows(env, "credential_lifecycle")
        if item.status == "denied"
    ]
    assert denied and denied[-1].deny_reason == "CREDENTIAL_ISSUE_DENIED"


async def test_revoke_is_idempotent_and_blocks_auth(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None

    await env.services.credentials.revoke(record.id, env.admin_user, "192.0.2.9")
    await env.services.credentials.revoke(record.id, env.admin_user, "192.0.2.9")

    rec, code = await env.services.credentials.get_for_auth(account)
    assert rec is not None and code == CREDENTIAL_REVOKED
    assert await env.services.credentials.get_active(account) is None

    with pytest.raises(NotFound):
        await env.services.credentials.revoke(999999, env.admin_user, "192.0.2.9")

    revokes = [item for item in await _audit_rows(env, "credential_lifecycle") if item.tool == "revoke"]
    assert len(revokes) == 2
    assert revokes[0].credential_account == account


async def test_expired_credential_is_rejected_at_auth_time(env):
    account, _ = await _issue(env)
    await _expire_now(env, account)

    rec, code = await env.services.credentials.get_for_auth(account)
    assert rec is not None and code == CREDENTIAL_EXPIRED
    assert await env.services.credentials.get_active(account) is None


async def test_unknown_account_returns_not_found_code(env):
    rec, code = await env.services.credentials.get_for_auth("exp_doesnotexist")
    assert rec is None
    assert code == CREDENTIAL_NOT_FOUND


async def test_resolve_principal_happy_path(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None

    principal, datasource = await env.services.credentials.resolve_principal(record)
    assert principal.user_name == "tester"
    assert datasource.name == env.ds_name


async def test_resolve_principal_rejects_disabled_token(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None

    await env.services.tokens.disable(record.token_id)
    with pytest.raises(CredentialAuthError) as excinfo:
        await env.services.credentials.resolve_principal(record)
    assert excinfo.value.code == TOKEN_DISABLED


async def test_resolve_principal_rejects_disabled_datasource(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None

    await env.services.datasources.set_enabled(record.datasource_id, False)
    with pytest.raises(CredentialAuthError) as excinfo:
        await env.services.credentials.resolve_principal(record)
    assert excinfo.value.code == DATASOURCE_DISABLED


async def test_resolve_principal_missing_datasource(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None
    ghost = ExportCredential(
        account_name=record.account_name,
        auth_string=record.auth_string,
        token_id=record.token_id,
        datasource_id=999999,
        default_database=record.default_database,
        issued_by=record.issued_by,
        expires_at=record.expires_at,
        created_at=record.created_at,
    )
    with pytest.raises(CredentialAuthError) as excinfo:
        await env.services.credentials.resolve_principal(ghost)
    assert excinfo.value.code == DATASOURCE_MISSING


async def test_list_all_exposes_status_without_auth_string(env):
    account, _ = await _issue(env)
    revoked_account, _ = await _issue(env)
    expired_account, _ = await _issue(env)

    revoked_record = await _credential_by_account(env, revoked_account)
    assert revoked_record is not None
    await env.services.credentials.revoke(revoked_record.id, env.admin_user, "192.0.2.9")
    await _expire_now(env, expired_account)

    views = {item.account_name: item for item in await env.services.credentials.list_all()}
    assert views[account].status == STATUS_ACTIVE
    assert views[revoked_account].status == STATUS_REVOKED
    assert views[expired_account].status == STATUS_EXPIRED
    assert views[account].user_name == "tester"
    assert views[account].datasource_name == env.ds_name
    assert not hasattr(views[account], "auth_string")


async def test_cleanup_expired_deletes_only_expired(env):
    alive_account, _ = await _issue(env)
    expired_account, _ = await _issue(env)
    await _expire_now(env, expired_account)

    deleted = await env.services.credentials.cleanup_expired()
    assert deleted == 1

    async with env.services.session_factory() as session:
        remaining = (await session.execute(text("SELECT account_name FROM export_credential"))).scalars().all()
    assert remaining == [alive_account]

    cleanups = [item for item in await _audit_rows(env, "credential_lifecycle") if item.tool == "expire_cleanup"]
    assert len(cleanups) == 1
    assert cleanups[0].credential_account == expired_account


async def test_touch_last_used_is_best_effort(env):
    account, _ = await _issue(env)
    record = await _credential_by_account(env, account)
    assert record is not None
    assert record.last_used_at is None
    await env.services.credentials.touch_last_used(record.id)
    row = await _credential_row(env, account)
    assert row.last_used_at is not None


async def test_issue_uses_selected_database_not_datasource_default(env):
    """AC41: the server must use the target database chosen in the form, not fall back to the datasource default."""
    async with extra_database(env, "target", [(1, "13900000001")]) as target_db:
        datasource = await create_datasource(
            env, "cred-target", [env.biz_db, target_db], default_database=env.biz_db
        )
        try:
            account, _ = await _issue(
                env, datasource_id=datasource.id, database=target_db
            )
            row = await _credential_row(env, account)
        finally:
            await env.services.datasources.delete(datasource.id)

    assert row.default_database == target_db
    assert row.default_database != env.biz_db


async def test_issue_rejects_unexposed_database(env):
    async with extra_database(env, "hidden", [(1, "13900000002")]) as hidden_db:
        datasource = await create_datasource(env, "cred-hidden", [env.biz_db])
        try:
            with pytest.raises(ValidationError, match="目标库不存在或未授权"):
                await _issue(env, datasource_id=datasource.id, database=hidden_db)
        finally:
            await env.services.datasources.delete(datasource.id)

    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0


async def test_issue_rejects_database_outside_token_scope(env):
    """AC44: the database is exposed but not granted to this token; the same message as "database does not exist"."""
    async with extra_database(env, "scoped", [(1, "13900000003")]) as scoped_db:
        datasource = await create_datasource(
            env, "cred-scoped", [env.biz_db, scoped_db]
        )
        _, token_row = await env.services.tokens.create(
            "cred-scoped",
            TokenScope(
                datasources=frozenset({datasource.id}),
                unrestricted_databases=frozenset({(datasource.id, env.biz_db)}),
            ),
        )
        try:
            with pytest.raises(ValidationError, match="目标库不存在或未授权"):
                await env.services.credentials.issue(
                    token_id=token_row.id,
                    datasource_id=datasource.id,
                    database=scoped_db,
                    ttl_hours=1,
                    admin_username=env.admin_user,
                    client_ip="192.0.2.9",
                )
        finally:
            await env.services.datasources.delete(datasource.id)

    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0


async def test_issue_rejects_token_without_available_database(env):
    """E36: this token has no usable database under this datasource."""
    datasource = await create_datasource(env, "cred-empty", [env.biz_db])
    _, token_row = await env.services.tokens.create(
        "cred-empty",
        TokenScope(
            datasources=frozenset({datasource.id}),
            unrestricted_databases=frozenset(),
        ),
    )
    try:
        with pytest.raises(ValidationError, match="没有可用库"):
            await env.services.credentials.issue(
                token_id=token_row.id,
                datasource_id=datasource.id,
                database=env.biz_db,
                ttl_hours=1,
                admin_username=env.admin_user,
                client_ip="192.0.2.9",
            )
    finally:
        await env.services.datasources.delete(datasource.id)

    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
    assert count == 0


async def test_issue_auto_pins_prepared_database(env):
    """AC42: auto-issue uses this request's effective default database, not the datasource default."""
    async with extra_database(env, "auto", [(201, "13900000004")]) as view_db:
        datasource = await create_datasource(
            env, "cred-auto", [env.biz_db, view_db], default_database=env.biz_db
        )
        token, token_row = await env.services.tokens.create(
            "cred-auto",
            TokenScope(
                datasources=frozenset({datasource.id}),
                unrestricted_databases=frozenset(
                    {(datasource.id, env.biz_db), (datasource.id, view_db)}
                ),
            ),
        )
        principal = await env.services.tokens.authenticate(token)
        assert principal is not None
        try:
            issued = await env.services.credentials.issue_auto(
                token_id=token_row.id,
                datasource_id=datasource.id,
                database=view_db,
                principal=principal,
                trigger_request_id="req-ac42",
                client_ip="127.0.0.1",
            )
            row = await _credential_row(env, issued.account_name)
        finally:
            await env.services.datasources.delete(datasource.id)

    assert row.default_database == view_db
    assert row.default_database != env.biz_db


async def test_require_token_admin_issue_denied(env) -> None:
    _, token_row = await env.services.tokens.create("agent", actor_policy="require")
    with pytest.raises(ValidationError):
        await _issue(env, token_id=token_row.id)
    rows, total = await env.services.audit.query({"status": "denied"})
    assert total == 1
    assert rows[0].deny_reason == CREDENTIAL_ISSUE_DENIED
    assert rows[0].tool == "issue"
    # Personal (ignore) tokens keep issuing normally.
    account, _ = await _issue(env)
    assert account


async def test_require_token_auto_issue_denied(env) -> None:
    plaintext, token_row = await env.services.tokens.create("agent", actor_policy="require")
    principal = await env.services.tokens.authenticate(plaintext)
    assert principal is not None
    with pytest.raises(ValidationError):
        await env.services.credentials.issue_auto(
            token_id=token_row.id,
            datasource_id=await _ds_id(env),
            database=env.biz_db,
            principal=principal,
            trigger_request_id="trigger",
            client_ip="127.0.0.1",
        )
    rows, total = await env.services.audit.query({"status": "denied"})
    assert total == 1
    assert rows[0].tool == "issue_auto"
    assert rows[0].deny_reason == CREDENTIAL_ISSUE_DENIED
