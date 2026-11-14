"""Datasource service and connection pools."""

from __future__ import annotations

import pytest

from app.errors import DataSourceConnectionError, NotFound, ValidationError
from app.datasources import databases as databases_module
from app.security.crypto import decrypt_secret

pytestmark = pytest.mark.integration


async def _get_ds(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    assert ds is not None
    return ds


async def test_list_counts_and_lookup(env):
    rows = await env.services.datasources.list_all()
    assert [row.name for row in rows] == [env.ds_name]
    assert await env.services.datasources.counts() == (1, 1)
    assert (await _get_ds(env)).default_database == env.biz_db


async def test_create_duplicate_and_connection_failure(env):
    with pytest.raises(ValidationError):
        await env.services.datasources.create(
            name=env.ds_name,
            host=env.db_host,
            port=env.db_port,
            username=env.db_user,
            password=env.db_password,
            default_database=env.biz_db,
        )

    with pytest.raises(DataSourceConnectionError):
        await env.services.datasources.create(
            name="bad-port",
            host="127.0.0.1",
            port=1,
            username="nobody",
            password="nobody",
            default_database="nobody",
        )
    assert await env.services.datasources.get_by_name("bad-port") is None


async def test_update_keeps_password_and_invalidates_pool(env):
    ds = await _get_ds(env)
    old_cipher = ds.password_cipher
    old_engine = await env.services.pool.get(ds)
    old_export_pool = await env.services.export_pool.get(ds)
    assert await env.services.pool.get(ds) is old_engine
    assert await env.services.export_pool.get(ds) is old_export_pool

    updated = await env.services.datasources.update(
        ds.id,
        name=ds.name,
        host=ds.host,
        port=ds.port,
        username=ds.username,
        password="",
        default_database=ds.default_database,
        remark="改了备注",
    )
    assert updated.password_cipher == old_cipher
    assert updated.remark == "改了备注"
    assert decrypt_secret(updated.password_cipher, env.settings.master_key_bytes) == env.db_password

    new_engine = await env.services.pool.get(updated)
    assert new_engine is not old_engine
    new_export_pool = await env.services.export_pool.get(updated)
    assert new_export_pool is not old_export_pool
    assert old_export_pool.closed is True


async def test_test_saved_connection(env):
    ds = await _get_ds(env)
    elapsed = await env.services.datasources.test_saved(ds.id)
    assert elapsed >= 0


async def test_read_only_and_enabled_toggle(env):
    ds = await _get_ds(env)
    await env.services.datasources.set_read_only(ds.id, False)
    assert (await env.services.datasources.get(ds.id)).is_read_only == 0
    await env.services.datasources.set_read_only(ds.id, True)
    assert (await env.services.datasources.get(ds.id)).is_read_only == 1

    await env.services.datasources.set_enabled(ds.id, False)
    assert (await env.services.datasources.get(ds.id)).enabled == 0
    await env.services.datasources.set_enabled(ds.id, True)
    assert (await env.services.datasources.get(ds.id)).enabled == 1


async def test_delete_cascades_masking_rules(env):
    ds = await _get_ds(env)
    await env.services.masking.add_rule(
        ds.id, env.biz_db, "users", "phone", "mask", "phone"
    )
    assert len(await env.services.masking.list_rules(ds.id)) == 1

    token_row = (await env.services.tokens.list_all())[0]
    account, _ = await env.services.credentials.issue(
        token_id=token_row.id,
        datasource_id=ds.id,
        database=ds.default_database,
        ttl_hours=1,
        admin_username="admin",
        client_ip="127.0.0.1",
    )
    assert await env.services.credentials.get_active(account) is not None
    old_export_pool = await env.services.export_pool.get(ds)

    await env.services.datasources.delete(ds.id)
    with pytest.raises(NotFound):
        await env.services.datasources.get(ds.id)
    assert await env.services.masking.list_rules(ds.id) == []
    assert await env.services.datasources.get_by_name(env.ds_name) is None
    assert await env.services.credentials.list_all() == []
    assert old_export_pool.closed is True


async def test_database_list_saved_with_default_read_only(env):
    ds = await env.services.datasources.create(
        name="multi-ds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
        databases=[env.biz_db, "report"],
    )
    assert await databases_module.list_exposed(
        env.services.session_factory, ds.id
    ) == sorted([env.biz_db, "report"])
    policies = await env.services.datasources.list_databases(ds.id)
    assert {item.name: item.is_read_only for item in policies} == {
        env.biz_db: True,
        "report": True,
    }


async def test_removed_database_is_deleted_and_readded_read_only(env):
    ds = await env.services.datasources.create(
        name="multi-ds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
        databases=[env.biz_db, "report"],
    )
    await env.services.datasources.update(
        ds.id,
        name="multi-ds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password="",
        default_database=env.biz_db,
        databases=[env.biz_db],
    )
    assert await databases_module.list_exposed(
        env.services.session_factory, ds.id
    ) == [env.biz_db]

    await env.services.datasources.update(
        ds.id,
        name="multi-ds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password="",
        default_database=env.biz_db,
        databases=[env.biz_db, "report"],
    )
    policies = await env.services.datasources.list_databases(ds.id)
    assert {item.name: item.is_read_only for item in policies} == {
        env.biz_db: True,
        "report": True,
    }


async def test_delete_cascades_datasource_database(env):
    ds = await env.services.datasources.create(
        name="multi-ds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
        databases=[env.biz_db, "report"],
    )
    assert len(await databases_module.list_exposed(env.services.session_factory, ds.id)) == 2
    await env.services.datasources.delete(ds.id)
    assert await databases_module.list_exposed(env.services.session_factory, ds.id) == []

