"""Admin temporary export credentials: auth, CSRF, one-time plaintext, scope filtering, and revocation."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.credentials.service import STATUS_REVOKED
from app.tokens.scope import TokenScope
from tests.integration.conftest import (
    admin_login,
    create_datasource,
    extra_database,
)

pytestmark = pytest.mark.integration

PASSWORD_RE = re.compile(r'id="credential-password">([^<]+)</code>')


async def _ids(env) -> tuple[int, int]:
    datasource = await env.services.datasources.get_by_name(env.ds_name)
    assert datasource is not None
    token = (await env.services.tokens.list_all())[0]
    return token.id, datasource.id


async def _credential_count(env) -> int:
    async with env.services.session_factory() as session:
        return int(
            await session.scalar(text("SELECT COUNT(*) FROM export_credential"))
        )


async def test_credentials_require_admin(env):
    page = await env.client.get("/admin/credentials")
    assert page.status_code == 401
    form = await env.client.get("/admin/credentials/new")
    assert form.status_code == 401


async def test_issue_requires_csrf(env):
    await admin_login(env)
    token_id, datasource_id = await _ids(env)
    before = await _credential_count(env)
    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "token_id": token_id,
            "datasource_id": datasource_id,
            "default_database": env.biz_db,
            "ttl_hours": 1,
        },
    )
    assert response.status_code == 400
    assert await _credential_count(env) == before


async def test_issue_shows_plaintext_once_and_list_hides_it(env):
    csrf = await admin_login(env)
    token_id, datasource_id = await _ids(env)

    form = await env.client.get("/admin/credentials/new")
    assert form.status_code == 200
    assert "导出通道未启用" in form.text
    assert "签发" in form.text

    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "_csrf": csrf,
            "token_id": token_id,
            "datasource_id": datasource_id,
            "default_database": env.biz_db,
            "ttl_hours": 1,
        },
    )
    assert response.status_code == 200
    assert "凭证已签发" in response.text
    assert "密码仅本次展示" in response.text
    match = PASSWORD_RE.search(response.text)
    assert match is not None, "结果页缺少一次性明文密码"
    password = match.group(1)
    assert len(password) == 32
    assert "mysql -h" in response.text
    assert "pandas" in response.text
    assert "SQLAlchemy" in response.text
    assert "jdbc:mysql://" in response.text

    rows = await env.services.credentials.list_all()
    account = rows[0].account_name
    listing = await env.client.get("/admin/credentials")
    assert listing.status_code == 200
    assert account in listing.text
    assert password not in listing.text
    assert "credential-password" not in listing.text
    assert "明文" not in listing.text

    async with env.services.session_factory() as session:
        audit_rows = (
            await session.execute(
                text(
                    "SELECT sql_text, error_message FROM audit_log "
                    "WHERE credential_account = :account"
                ),
                {"account": account},
            )
        ).all()
    for sql_text, error_message in audit_rows:
        assert password not in (sql_text or "")
        assert password not in (error_message or "")


async def test_datasource_options_follow_token_scope(env):
    datasource = await env.services.datasources.get_by_name(env.ds_name)
    assert datasource is not None
    await env.services.datasources.create(
        name="testds-bak",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
        remark="集成测试数据源备份",
    )
    unrestricted_row = (await env.services.tokens.list_all())[0]
    scoped, scoped_row = await env.services.tokens.create(
        "scoped-user",
        TokenScope(datasources=frozenset({datasource.id})),
    )
    assert scoped
    _, disabled_row = await env.services.tokens.create("disabled-user")
    await env.services.tokens.disable(disabled_row.id)

    await admin_login(env)
    scoped_page = await env.client.get(
        f"/admin/credentials/new?token_id={scoped_row.id}"
    )
    assert scoped_page.status_code == 200
    assert f'<option value="{datasource.id}"' in scoped_page.text
    assert "testds-bak" not in scoped_page.text
    assert f'<option value="{env.biz_db}"' in scoped_page.text
    assert "disabled-user" not in scoped_page.text

    full_page = await env.client.get(
        f"/admin/credentials/new?token_id={unrestricted_row.id}"
    )
    assert full_page.status_code == 200
    assert "testds-bak" in full_page.text

    missing = await env.client.get("/admin/credentials/new?datasource_id=999999")
    assert missing.status_code == 404


async def test_issue_uses_selected_target_database(env):
    """AC41: both the result-page connection string and the stored row use the target database chosen in the form."""
    async with extra_database(env, "target", [(1, "13900000011")]) as target_db:
        datasource = await create_datasource(
            env, "admin-target", [env.biz_db, target_db], default_database=env.biz_db
        )
        try:
            csrf = await admin_login(env)
            token_id = (await env.services.tokens.list_all())[0].id
            form = await env.client.get(
                f"/admin/credentials/new?token_id={token_id}"
                f"&datasource_id={datasource.id}"
            )
            assert form.status_code == 200
            assert f'<option value="{target_db}"' in form.text

            response = await env.client.post(
                "/admin/credentials/new",
                data={
                    "_csrf": csrf,
                    "token_id": token_id,
                    "datasource_id": datasource.id,
                    "default_database": target_db,
                    "ttl_hours": 1,
                },
            )
            assert response.status_code == 200
            assert f"本凭证的默认落点已固定为库 {target_db}" in response.text
            assert f"/{target_db}" in response.text
            assert "mysql -h" in response.text
            assert f" {target_db}" in response.text

            row = next(iter(await env.services.credentials.list_all()))
            assert row.default_database == target_db
            assert row.default_database != datasource.default_database
        finally:
            await env.services.datasources.delete(datasource.id)


async def test_issue_rejects_unauthorized_target_database(env):
    """AC44: exposed but not granted to the token -> 400 and nothing persisted."""
    async with extra_database(env, "denied", [(1, "13900000012")]) as denied_db:
        datasource = await create_datasource(
            env, "admin-denied", [env.biz_db, denied_db]
        )
        _, token_row = await env.services.tokens.create(
            "admin-denied",
            TokenScope(
                datasources=frozenset({datasource.id}),
                unrestricted_databases=frozenset({(datasource.id, env.biz_db)}),
            ),
        )
        try:
            csrf = await admin_login(env)
            before = await _credential_count(env)
            response = await env.client.post(
                "/admin/credentials/new",
                data={
                    "_csrf": csrf,
                    "token_id": token_row.id,
                    "datasource_id": datasource.id,
                    "default_database": denied_db,
                    "ttl_hours": 1,
                },
            )
            assert response.status_code == 400
            assert "目标库不存在或未授权" in response.text
            assert await _credential_count(env) == before
        finally:
            await env.services.datasources.delete(datasource.id)


async def test_issue_without_available_database_blocks_form_and_post(env):
    """E36: with no usable database the dropdown is empty and submission is disabled; the server rejects too."""
    datasource = await create_datasource(env, "admin-empty", [env.biz_db])
    _, token_row = await env.services.tokens.create(
        "admin-empty",
        TokenScope(
            datasources=frozenset({datasource.id}),
            unrestricted_databases=frozenset(),
        ),
    )
    try:
        csrf = await admin_login(env)
        form = await env.client.get(
            f"/admin/credentials/new?token_id={token_row.id}"
            f"&datasource_id={datasource.id}"
        )
        assert form.status_code == 200
        assert "没有可用库" in form.text
        assert "disabled" in form.text

        before = await _credential_count(env)
        response = await env.client.post(
            "/admin/credentials/new",
            data={
                "_csrf": csrf,
                "token_id": token_row.id,
                "datasource_id": datasource.id,
                "default_database": env.biz_db,
                "ttl_hours": 1,
            },
        )
        assert response.status_code == 400
        assert "没有可用库" in response.text
        assert await _credential_count(env) == before
    finally:
        await env.services.datasources.delete(datasource.id)


@pytest.mark.parametrize("ttl", [0, -1, 73, 1000])
async def test_issue_ttl_out_of_range_rejected(env, ttl):
    csrf = await admin_login(env)
    token_id, datasource_id = await _ids(env)
    before = await _credential_count(env)
    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "_csrf": csrf,
            "token_id": token_id,
            "datasource_id": datasource_id,
            "default_database": env.biz_db,
            "ttl_hours": ttl,
        },
    )
    assert response.status_code == 400
    assert "有效期" in response.text
    assert await _credential_count(env) == before


async def test_issue_datasource_outside_scope_rejected(env):
    allowed_ds = await env.services.datasources.get_by_name(env.ds_name)
    assert allowed_ds is not None
    outside = await env.services.datasources.create(
        name="testds-outside",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
        remark="范围外数据源",
    )
    _, token_row = await env.services.tokens.create(
        "scoped-issue",
        TokenScope(datasources=frozenset({allowed_ds.id})),
    )

    csrf = await admin_login(env)
    before = await _credential_count(env)
    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "_csrf": csrf,
            "token_id": token_row.id,
            "datasource_id": outside.id,
            "default_database": env.biz_db,
            "ttl_hours": 1,
        },
    )
    assert response.status_code == 400
    assert "不在该 Token 授权范围内" in response.text
    assert await _credential_count(env) == before

    async with env.services.session_factory() as session:
        denied = (
            await session.execute(
                text(
                    "SELECT deny_reason FROM audit_log "
                    "WHERE event_type='credential_lifecycle' AND status='denied' "
                    "ORDER BY id DESC LIMIT 1"
                )
            )
        ).scalar()
    assert denied == "CREDENTIAL_ISSUE_DENIED"


async def test_issue_binds_form_source_ip(env):
    """The admin specifies the actual consumer IP; the stored value and the result page both show it normalized."""
    csrf = await admin_login(env)
    token_id, datasource_id = await _ids(env)
    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "_csrf": csrf,
            "token_id": token_id,
            "datasource_id": datasource_id,
            "default_database": env.biz_db,
            "ttl_hours": 1,
            "source_ip": "::ffff:172.16.0.9",
        },
    )
    assert response.status_code == 200
    assert "172.16.0.9" in response.text
    row = next(iter(await env.services.credentials.list_all()))
    assert row.source_ip == "172.16.0.9"
    listing = await env.client.get("/admin/credentials")
    assert "172.16.0.9" in listing.text


async def test_issue_defaults_to_request_ip(env):
    """An empty form field binds the requester IP (127.0.0.1 from the in-process ASGI view)."""
    csrf = await admin_login(env)
    token_id, datasource_id = await _ids(env)
    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "_csrf": csrf,
            "token_id": token_id,
            "datasource_id": datasource_id,
            "default_database": env.biz_db,
            "ttl_hours": 1,
        },
    )
    assert response.status_code == 200
    row = next(iter(await env.services.credentials.list_all()))
    assert row.source_ip == "127.0.0.1"


@pytest.mark.parametrize("source_ip", ["192.0.2.9/32", "not-an-ip"])
async def test_issue_invalid_source_ip_rejected(env, source_ip):
    csrf = await admin_login(env)
    token_id, datasource_id = await _ids(env)
    before = await _credential_count(env)
    response = await env.client.post(
        "/admin/credentials/new",
        data={
            "_csrf": csrf,
            "token_id": token_id,
            "datasource_id": datasource_id,
            "default_database": env.biz_db,
            "ttl_hours": 1,
            "source_ip": source_ip,
        },
    )
    assert response.status_code == 400
    assert "来源 IP 不合法" in response.text
    assert await _credential_count(env) == before


async def test_revoke_updates_status(env):
    csrf = await admin_login(env)
    token_id, datasource_id = await _ids(env)
    account, _ = await env.services.credentials.issue(
        token_id=token_id,
        datasource_id=datasource_id,
        database=env.biz_db,
        ttl_hours=1,
        admin_username=env.admin_user,
        client_ip="127.0.0.1",
    )
    view = next(
        item for item in await env.services.credentials.list_all() if item.account_name == account
    )
    assert view.status != STATUS_REVOKED

    revoked = await env.client.post(
        f"/admin/credentials/{view.id}/revoke", data={"_csrf": csrf}
    )
    assert revoked.status_code == 302
    assert revoked.headers["location"] == "/admin/credentials?msg=credential_revoked"

    listing = await env.client.get(revoked.headers["location"])
    assert listing.status_code == 200
    assert "凭证已撤销" in listing.text
    assert "已撤销" in listing.text
    refreshed = next(
        item for item in await env.services.credentials.list_all() if item.id == view.id
    )
    assert refreshed.status == STATUS_REVOKED

    again = await env.client.post(
        f"/admin/credentials/{view.id}/revoke", data={"_csrf": csrf}
    )
    assert again.status_code == 302
