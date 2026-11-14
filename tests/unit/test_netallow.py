"""`IpAllowlist` unit tests: source precedence, effect-on-save, caching, and outage behavior.

Use a fake session_factory in place of the management DB to verify decision logic only, without MySQL.
"""

import pytest
from datetime import datetime
from time import monotonic

from app.db.models import SystemSetting
from app.errors import ValidationError
from app.netallow import (
    EXPORT_ALLOW_KEY,
    MCP_ALLOW_KEY,
    REFRESH_SECONDS,
    SOURCE_DB,
    SOURCE_ENV,
    SOURCE_UNSET,
    IpAllowlist,
)

EXPORT_ENV = "192.0.2.0/24,192.168.1.20"
_NOW = datetime(2026, 11, 5, 1, 2, 3)


class FakeSession:
    def __init__(self, store: dict[str, SystemSetting], fail: bool) -> None:
        self._store = store
        self._fail = fail

    async def __aenter__(self) -> "FakeSession":
        if self._fail:
            raise RuntimeError("管理库不可用")
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model, key: str):
        return self._store.get(key)

    def add(self, obj: SystemSetting) -> None:
        self._store[obj.setting_key] = obj

    async def commit(self) -> None:
        return None


class FakeSessionFactory:
    def __init__(self) -> None:
        self.store: dict[str, SystemSetting] = {}
        self.fail = False

    def __call__(self) -> FakeSession:
        return FakeSession(self.store, self.fail)

    def seed(self, key: str, value: str) -> None:
        self.store[key] = SystemSetting(
            setting_key=key, setting_value=value, updated_at=_NOW
        )


def export_list(factory: FakeSessionFactory, env_text: str = EXPORT_ENV) -> IpAllowlist:
    return IpAllowlist(factory, EXPORT_ALLOW_KEY, "MySQL 导出通道", env_text)


def mcp_list(factory: FakeSessionFactory, env_text: str = "") -> IpAllowlist:
    return IpAllowlist(
        factory, MCP_ALLOW_KEY, "MCP 入口", env_text, allow_when_unset=True
    )


async def test_env_value_is_used_until_the_page_saves() -> None:
    allow = export_list(FakeSessionFactory())
    snapshot = await allow.snapshot()
    assert snapshot.source == SOURCE_ENV
    assert snapshot.entries == ("192.0.2.0/24", "192.168.1.20/32")
    assert await allow.is_allowed("192.0.2.5") is True
    assert await allow.is_allowed("10.20.0.1") is False


async def test_export_channel_with_no_source_denies_everything() -> None:
    """With no entries in either place, the export channel denies all (v1 has no TLS; the allowlist is the only network control)."""
    allow = export_list(FakeSessionFactory(), env_text="")
    snapshot = await allow.snapshot()
    assert snapshot.source == SOURCE_UNSET
    assert snapshot.allow_all is False
    assert await allow.is_allowed("192.0.2.5") is False


async def test_mcp_channel_unconfigured_is_unrestricted() -> None:
    allow = mcp_list(FakeSessionFactory())
    snapshot = await allow.snapshot()
    assert snapshot.source == SOURCE_UNSET
    assert snapshot.allow_all is True
    assert await allow.is_allowed("203.0.113.9") is True


async def test_saving_an_empty_mcp_list_locks_the_channel() -> None:
    """Unrestricted belongs only to "never saved"; a saved empty value means deny all."""
    factory = FakeSessionFactory()
    allow = mcp_list(factory)
    assert await allow.is_allowed("203.0.113.9") is True
    await allow.save(allow.prepare(""))
    assert await allow.is_allowed("203.0.113.9") is False
    assert (await allow.snapshot()).source == SOURCE_DB


async def test_save_pins_value_and_takes_over_from_env() -> None:
    factory = FakeSessionFactory()
    allow = export_list(factory)
    before, after = await allow.save(allow.prepare("172.16.0.0/16"))
    assert after == "172.16.0.0/16"
    assert "未保存过" in before
    snapshot = await allow.snapshot()
    assert snapshot.source == SOURCE_DB
    assert snapshot.entries == ("172.16.0.0/16",)
    assert await allow.is_allowed("172.16.9.9") is True
    assert await allow.is_allowed("192.0.2.5") is False


async def test_save_takes_effect_without_waiting_for_ttl() -> None:
    factory = FakeSessionFactory()
    allow = export_list(factory)
    await allow.snapshot()  # Prime the cache
    await allow.save(allow.prepare("192.0.2.7"))
    assert await allow.is_allowed("192.0.2.7") is True


async def test_direct_db_change_is_picked_up_after_ttl() -> None:
    """When the database is changed outside the settings page (rollback, ticket correction), convergence happens within TTL seconds."""
    factory = FakeSessionFactory()
    allow = export_list(factory)
    await allow.save(allow.prepare("192.0.2.7"))
    factory.seed(EXPORT_ALLOW_KEY, "198.51.100.0/24")
    assert await allow.is_allowed("192.0.2.7") is True
    allow._cached_at = monotonic() - REFRESH_SECONDS - 1
    assert await allow.is_allowed("198.51.100.4") is True
    assert await allow.is_allowed("192.0.2.7") is False


async def test_db_failure_keeps_last_known_value() -> None:
    factory = FakeSessionFactory()
    allow = export_list(factory)
    await allow.save(allow.prepare("192.0.2.7"))
    factory.fail = True
    allow._cached_at = monotonic() - REFRESH_SECONDS - 1
    assert await allow.is_allowed("192.0.2.7") is True


async def test_db_failure_without_any_cache_fails_closed() -> None:
    """Never having read successfully must not be taken as "unrestricted"; the MCP entrypoint denies too."""
    factory = FakeSessionFactory()
    factory.fail = True
    allow = mcp_list(factory)
    assert await allow.is_allowed("203.0.113.9") is False
    assert (await allow.snapshot()).allow_all is False


@pytest.mark.parametrize("text", ["192.0.2.300", "not-an-ip", "192.0.2.0/33"])
def test_prepare_rejects_illegal_entries(text: str) -> None:
    allow = export_list(FakeSessionFactory())
    with pytest.raises(ValidationError) as excinfo:
        allow.prepare(text)
    assert "非法" in str(excinfo.value)


def test_prepare_rejects_host_bits_entry() -> None:
    """Host bits would be allowed as a whole range, so they must be rejected before saving."""
    allow = export_list(FakeSessionFactory())
    with pytest.raises(ValidationError) as excinfo:
        allow.prepare("192.0.2.5/24")
    assert "含主机位" in str(excinfo.value)


def test_prepare_caps_entry_count() -> None:
    allow = export_list(FakeSessionFactory())
    text = "\n".join(f"10.{i // 256}.{i % 256}.0/24" for i in range(51))
    with pytest.raises(ValidationError) as excinfo:
        allow.prepare(text)
    assert "最多 50 条" in str(excinfo.value)


def test_prepare_caps_value_length() -> None:
    """A valid entry count can still exceed the column width after normalization; reject that too."""
    allow = export_list(FakeSessionFactory())
    text = ",".join(f"ffff:ffff:ffff:ffff:ffff:ffff:ffff:{i:04x}" for i in range(1, 51))
    with pytest.raises(ValidationError) as excinfo:
        allow.prepare(text)
    assert "超出 1024 上限" in str(excinfo.value)


async def test_save_is_noop_when_value_unchanged() -> None:
    """Saving the same value again neither bumps updated_at nor records a change; empty values must still compare against the raw stored value."""
    factory = FakeSessionFactory()
    allow = export_list(factory)
    await allow.save(allow.prepare("192.0.2.0/24"))
    saved_at = factory.store[EXPORT_ALLOW_KEY].updated_at
    assert await allow.save("192.0.2.0/24") is None
    assert factory.store[EXPORT_ALLOW_KEY].updated_at == saved_at

    mcp = mcp_list(factory)
    await mcp.save(mcp.prepare(""))
    assert await mcp.save("") is None


async def test_view_reports_the_live_source() -> None:
    factory = FakeSessionFactory()
    allow = export_list(factory)
    view = allow.view(await allow.snapshot())
    assert view["field"] == EXPORT_ALLOW_KEY
    assert "GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS" in view["source_text"]
    assert view["allow_all"] is False

    unrestricted = mcp_list(FakeSessionFactory())
    mcp_view = unrestricted.view(await unrestricted.snapshot())
    assert "不限制来源" in mcp_view["source_text"]
    assert mcp_view["text"] == ""

    await unrestricted.save(unrestricted.prepare("203.0.113.0/24"))
    saved_view = unrestricted.view(await unrestricted.snapshot())
    assert saved_view["count"] == 1
    assert "本页已保存" in saved_view["source_text"]

    await unrestricted.save(unrestricted.prepare(""))
    empty_view = unrestricted.view(await unrestricted.snapshot())
    assert "拒绝所有连接" in empty_view["source_text"]


async def test_env_key_matches_the_settings_prefix() -> None:
    assert export_list(FakeSessionFactory()).env_key == "GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS"
    assert mcp_list(FakeSessionFactory()).env_key == "GATEWAY_MCP_ALLOW_CIDRS"


async def test_illegal_env_entries_are_logged_and_dropped() -> None:
    factory = FakeSessionFactory()
    allow = export_list(factory, env_text="192.0.2.0/24,192.0.2.300")
    snapshot = await allow.snapshot()
    assert snapshot.entries == ("192.0.2.0/24",)
