"""Manual masking rule persistence."""

from __future__ import annotations

import pytest

from app.errors import NotFound, ValidationError

pytestmark = pytest.mark.integration


async def _ds(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    assert ds is not None
    return ds


async def test_add_list_delete(env):
    ds = await _ds(env)
    rule = await env.services.masking.add_rule(
        ds.id, env.biz_db, "users", "phone", "mask", "phone"
    )
    assert rule.id is not None
    assert rule.strategy == "mask"
    assert rule.pattern == "phone"

    rules = await env.services.masking.list_rules(ds.id)
    assert [(r.database, r.table, r.column) for r in rules] == [
        (env.biz_db, "users", "phone")
    ]

    await env.services.masking.delete_rule(ds.id, rule.id)
    assert await env.services.masking.list_rules(ds.id) == []
    with pytest.raises(NotFound):
        await env.services.masking.delete_rule(ds.id, rule.id)


async def test_duplicate_and_invalid_rules(env):
    ds = await _ds(env)
    await env.services.masking.add_rule(ds.id, env.biz_db, "users", "email", "hash", None)
    with pytest.raises(ValidationError):
        await env.services.masking.add_rule(
            ds.id, env.biz_db, "users", "email", "null", None
        )
    with pytest.raises(ValidationError):
        await env.services.masking.add_rule(
            ds.id, env.biz_db, "users", "phone", "unknown", None
        )
    with pytest.raises(ValidationError):
        await env.services.masking.add_rule(
            ds.id, env.biz_db, "users", "phone", "mask", "unknown"
        )
    with pytest.raises(ValidationError):
        await env.services.masking.add_rule(
            ds.id, env.biz_db, "users", "phone", "hash", "phone"
        )
    with pytest.raises(ValidationError):
        await env.services.masking.add_rule(
            ds.id, "", "users", "phone", "off", None
        )
    with pytest.raises(ValidationError):
        await env.services.masking.add_rule(
            ds.id, env.biz_db, "x" * 129, "phone", "off", None
        )


async def test_add_rule_rejects_unexposed_database(env):
    ds = await _ds(env)
    with pytest.raises(ValidationError, match="不在可访问库内"):
        await env.services.masking.add_rule(
            ds.id, f"{env.biz_db}_nope", "users", "phone", "mask", "phone"
        )
