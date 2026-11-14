"""Config validation for the export channel and MCP entrypoint (E14; former AC22 per spec)."""

import base64

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import ConfigError

VALID_KEY = base64.b64encode(b"k" * 32).decode("ascii")


def make_settings(**overrides) -> Settings:
    data = {
        "master_key": VALID_KEY,
        "db_host": "127.0.0.1",
        "db_user": "gateway",
        "db_password": "secret",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)


def test_export_disabled_by_default() -> None:
    settings = make_settings()
    assert settings.mysql_export_enabled is False
    assert settings.mysql_export_allow_cidrs == ""
    assert settings.mcp_allow_cidrs == ""


@pytest.mark.parametrize("cidrs", ["", "   ", " , ,"])
def test_enabled_with_blank_env_allowlist_only_warns(
    cidrs: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Allowlists can now be saved from the settings page, so an empty env var no longer blocks startup; an empty list denies at connection time."""
    with caplog.at_level("WARNING", logger="app.config"):
        settings = make_settings(mysql_export_enabled=True, mysql_export_allow_cidrs=cidrs)
    assert settings.mysql_export_enabled is True
    assert any("GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS" in record.message for record in caplog.records)


def test_blank_env_allowlist_is_quiet_while_export_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When export is disabled the allowlist should not be demanded (the MCP auto-issue warning is separate and already covered)."""
    with caplog.at_level("WARNING", logger="app.config"):
        make_settings(mysql_export_allow_cidrs="")
    assert not any(
        "GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS" in record.message for record in caplog.records
    )


def test_illegal_cidr_names_the_item() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(mysql_export_enabled=True, mysql_export_allow_cidrs="192.0.2.0/24,192.0.2.300")
    assert "192.0.2.300" in str(excinfo.value)
    assert "GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS" in str(excinfo.value)


def test_illegal_mcp_cidr_rejects_startup() -> None:
    """Invalid env entries must fail at startup: silently dropping them at runtime would quietly widen access."""
    with pytest.raises(ConfigError) as excinfo:
        make_settings(mcp_allow_cidrs="192.0.2.0/24,not-an-ip")
    assert "GATEWAY_MCP_ALLOW_CIDRS" in str(excinfo.value)


def test_host_bits_env_entry_rejects_startup() -> None:
    """An allowlist with host bits silently widens exposure, so env vars are rejected at startup just like settings-page saves."""
    with pytest.raises(ConfigError, match="网段含主机位"):
        make_settings(
            mysql_export_enabled=True, mysql_export_allow_cidrs="192.0.2.5/24"
        )


def test_export_port_must_differ_from_http_port() -> None:
    with pytest.raises(ConfigError):
        make_settings(
            mysql_export_enabled=True,
            mysql_export_allow_cidrs="192.0.2.0/24",
            port=3307,
            mysql_export_port=3307,
        )


def test_same_port_is_tolerated_while_export_disabled() -> None:
    settings = make_settings(port=3307, mysql_export_port=3307)
    assert settings.mysql_export_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mysql_export_max_concurrent", 0),
        ("mysql_export_max_concurrent", 65),
        ("mysql_export_max_rows", 0),
        ("mysql_export_max_rows", 100_000_001),
        ("mysql_export_max_seconds", 0),
        ("mysql_export_max_seconds", 86_401),
        ("mysql_export_max_ttl_hours", 0),
        ("mysql_export_max_ttl_hours", 721),
        ("mysql_export_fetch_batch", 99),
        ("mysql_export_fetch_batch", 1_000_001),
        ("mysql_export_port", 0),
        ("mysql_export_port", 65_536),
    ],
)
def test_export_numeric_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


# ---- v1.5: MCP large-result handoff config ----


def test_handoff_defaults() -> None:
    settings = make_settings()
    assert settings.mcp_result_max_rows == 2000
    assert settings.mcp_result_max_bytes == 2_000_000
    assert settings.mcp_export_auto_issue is True
    assert settings.mcp_export_ttl_seconds == 1800
    assert settings.mcp_export_max_active == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mcp_result_max_rows", 0),
        ("mcp_result_max_rows", 10_000_000),
        ("mcp_result_max_bytes", 0),
        ("mcp_result_max_bytes", 1_000_000_000),
        ("mcp_export_max_active", 1),
        ("mcp_export_max_active", 64),
        ("mcp_export_ttl_seconds", 60),
        ("mcp_export_ttl_seconds", 86400),
    ],
)
def test_handoff_bounds_accept_edge_values(field: str, value: int) -> None:
    assert getattr(make_settings(**{field: value}), field) == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mcp_result_max_rows", -1),
        ("mcp_result_max_rows", 10_000_001),
        ("mcp_result_max_bytes", -1),
        ("mcp_result_max_bytes", 1_000_000_001),
        ("mcp_export_max_active", 0),
        ("mcp_export_max_active", 65),
        ("mcp_export_ttl_seconds", 59),
        ("mcp_export_ttl_seconds", 86_401),
        ("mysql_export_advertise_port", -1),
        ("mysql_export_advertise_port", 65_536),
    ],
)
def test_handoff_bounds_reject_out_of_range(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_auto_ttl_cannot_exceed_manual_ttl_when_export_enabled() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            mysql_export_enabled=True,
            mysql_export_allow_cidrs="192.0.2.0/24",
            mysql_export_max_ttl_hours=1,
            mcp_export_ttl_seconds=7200,
        )
    assert "GATEWAY_MCP_EXPORT_TTL_SECONDS" in str(excinfo.value)


def test_auto_ttl_check_is_skipped_while_export_disabled() -> None:
    settings = make_settings(mysql_export_max_ttl_hours=1, mcp_export_ttl_seconds=7200)
    assert settings.mcp_export_ttl_seconds == 7200


def test_auto_issue_without_export_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="app.config"):
        settings = make_settings()
    assert settings.mysql_export_enabled is False
    assert any("GATEWAY_MYSQL_EXPORT_ENABLED" in record.message for record in caplog.records)


def test_advertise_host_prefers_explicit_value() -> None:
    settings = make_settings(
        mysql_export_host="192.0.2.10", mysql_export_advertise_host="gw.internal"
    )
    assert settings.export_advertise_host == "gw.internal"


def test_advertise_host_falls_back_to_specific_listen_address() -> None:
    settings = make_settings(mysql_export_host="192.0.2.10", mysql_export_advertise_host="  ")
    assert settings.export_advertise_host == "192.0.2.10"


@pytest.mark.parametrize("listen_host", ["0.0.0.0", "::", "", "*"])
def test_advertise_host_is_empty_for_wildcard_listen(listen_host: str) -> None:
    settings = make_settings(mysql_export_host=listen_host)
    assert settings.export_advertise_host == ""


def test_advertise_port_falls_back_to_listen_port() -> None:
    assert make_settings(mysql_export_port=3308).export_advertise_port == 3308
    assert (
        make_settings(mysql_export_port=3308, mysql_export_advertise_port=13308)
        .export_advertise_port
        == 13308
    )
