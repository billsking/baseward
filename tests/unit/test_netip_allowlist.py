"""Pure-function tests for allowlist text parsing and source-IP matching."""

import ipaddress

import pytest

from app.netip import (
    describe_invalid_entries,
    format_allow_value,
    has_host_bits,
    ip_in_networks,
    parse_allow_networks,
    split_allow_entries,
)


@pytest.mark.parametrize(
    "text",
    ["192.0.2.0/24\n192.168.1.20", "192.0.2.0/24,192.168.1.20", " 192.0.2.0/24 , \n 192.168.1.20 "],
)
def test_entries_split_by_line_and_comma(text: str) -> None:
    assert split_allow_entries(text) == ["192.0.2.0/24", "192.168.1.20"]


def test_split_drops_blank_and_keeps_order() -> None:
    assert split_allow_entries("\n::1\n\n10.0.0.1\n , \n") == ["::1", "10.0.0.1"]


def test_parse_deduplicates_and_normalizes() -> None:
    networks, invalid = parse_allow_networks("192.0.2.0/24,192.0.2.0/24,192.168.1.20")
    assert invalid == []
    assert networks == (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("192.168.1.20/32"),
    )


def test_parse_loose_mode_broadens_host_bits() -> None:
    """Env-var rule: 192.0.2.5/24 collapses to the network start."""
    networks, invalid = parse_allow_networks("192.0.2.5/24", strict=False)
    assert invalid == []
    assert networks == (ipaddress.ip_network("192.0.2.0/24"),)


def test_parse_strict_mode_reports_host_bits_as_invalid() -> None:
    """Settings-page rule: host bits must raise, never silently widen to the whole range."""
    networks, invalid = parse_allow_networks("192.0.2.5/24", strict=True)
    assert networks == ()
    assert invalid == ["192.0.2.5/24"]


@pytest.mark.parametrize("item", ["not-an-ip", "192.0.2.300", "192.0.2.0/33", "192.0.2.0/", "/24"])
def test_parse_reports_illegal_items(item: str) -> None:
    _networks, invalid = parse_allow_networks(f"192.0.2.0/24,{item}", strict=True)
    assert invalid == [item]


def test_invalid_item_is_truncated_to_column_limit() -> None:
    _networks, invalid = parse_allow_networks("x" * 200)
    assert invalid == ["x" * 64]


def test_format_value_is_comma_joined_compressed() -> None:
    networks, _ = parse_allow_networks("192.0.2.0/24\n::1/128")
    assert format_allow_value(networks) == "192.0.2.0/24,::1/128"


@pytest.mark.parametrize(
    "item,expected",
    [
        ("192.0.2.5/24", True),
        ("192.0.2.0/24", False),
        ("192.0.2.5", False),
        ("fe80::1/64", True),
        ("junk/24", False),
    ],
)
def test_has_host_bits(item: str, expected: bool) -> None:
    assert has_host_bits(item) is expected


def test_describe_invalid_entries_names_the_reason() -> None:
    text = describe_invalid_entries(["192.0.2.5/24", "junk"])
    assert "192.0.2.5/24（网段含主机位，请写起始地址）" in text
    assert "junk" in text


def test_describe_invalid_entries_collapses_long_tail() -> None:
    text = describe_invalid_entries([f"1.2.3.{n}" for n in range(9)])
    assert "等 9 项" in text
    assert text.count("1.2.3.") == 5


def test_ip_in_networks_hit_and_miss() -> None:
    networks, _ = parse_allow_networks("192.0.2.0/24,192.168.1.20")
    assert ip_in_networks("192.0.2.5", networks) is True
    assert ip_in_networks("192.168.1.20", networks) is True
    assert ip_in_networks("192.168.1.21", networks) is False
    assert ip_in_networks("198.51.100.5", networks) is False


@pytest.mark.parametrize("ip", ["not-an-ip", "", "  ", None])
def test_ip_in_networks_fail_closed_on_bad_source(ip: str | None) -> None:
    networks, _ = parse_allow_networks("192.0.2.0/24")
    assert ip_in_networks(ip or "", networks) is False


def test_empty_networks_denies_everything() -> None:
    assert ip_in_networks("192.0.2.5", ()) is False


def test_ipv4_mapped_ipv6_source_is_normalized() -> None:
    networks, _ = parse_allow_networks("192.0.2.0/24")
    assert ip_in_networks("::ffff:192.0.2.5", networks) is True


def test_ipv6_allowlist_accepts_ipv6_source() -> None:
    networks, _ = parse_allow_networks("::1/128")
    assert ip_in_networks("::1", networks) is True
    assert ip_in_networks("::2", networks) is False
