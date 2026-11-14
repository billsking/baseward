"""Source-IP normalization, binding match, and issue-side resolution (E-IP / legacy unbound + fail-closed)."""

from __future__ import annotations

import pytest

from app.credentials.service import resolve_source_ip
from app.errors import ValidationError
from app.netip import (
    client_ip_from_scope,
    ip_in_networks,
    ip_matches,
    normalize_ip,
    parse_allow_networks,
)


def test_normalize_ip_keeps_plain_v4_and_v6():
    assert normalize_ip("10.0.0.5") == "10.0.0.5"
    assert normalize_ip("fe80::1") == "fe80::1"


def test_normalize_ip_folds_v4_mapped_v6():
    assert normalize_ip("::ffff:10.0.0.5") == "10.0.0.5"


def test_normalize_ip_trims_and_rejects_garbage():
    assert normalize_ip("  10.0.0.5 ") == "10.0.0.5"
    assert normalize_ip("") == ""
    assert normalize_ip(None) == ""
    assert normalize_ip("10.0.0.5/32") == ""
    assert normalize_ip("not-an-ip") == ""


def test_ip_matches_treats_blank_binding_as_unbound():
    """Legacy credentials from before the migration have source_ip NULL/empty and skip the source check."""
    assert ip_matches(None, "10.0.0.5") is True
    assert ip_matches("", "10.0.0.5") is True
    assert ip_matches("   ", "10.0.0.5") is True


def test_ip_matches_accepts_equal_source_after_normalize():
    assert ip_matches("10.0.0.5", "10.0.0.5") is True
    assert ip_matches("::ffff:10.0.0.5", "10.0.0.5") is True
    assert ip_matches("10.0.0.5", "::ffff:10.0.0.5") is True


def test_ip_matches_rejects_different_source():
    assert ip_matches("10.0.0.5", "10.0.0.6") is False


@pytest.mark.parametrize(
    "binding,peer",
    [
        ("not-an-ip", "10.0.0.5"),
        ("10.0.0.5", ""),
        ("10.0.0.5", "unknown"),
    ],
)
def test_ip_matches_fails_closed_on_unusable_evidence(binding, peer):
    """An invalid binding or a missing connection source is always a deny, same rule as the export allowlist."""
    assert ip_matches(binding, peer) is False


def test_client_ip_from_scope_ignores_forwarded_for_from_untrusted_peer():
    """X-Forwarded-For is forgeable: when the TCP peer is not a trusted proxy, always use the peer."""
    scope = {
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("198.51.100.9", 123),
    }
    proxies = parse_allow_networks("127.0.0.1/32")[0]
    assert client_ip_from_scope(scope) == "198.51.100.9"
    assert client_ip_from_scope(scope, proxies) == "198.51.100.9"


def test_client_ip_from_scope_takes_nearest_untrusted_hop():
    """Walk the trusted chain right to left to the first untrusted hop: entries further left are client-forgeable."""
    proxies = parse_allow_networks("127.0.0.1/32")[0]
    scope = {
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("127.0.0.1", 123),
    }
    assert client_ip_from_scope(scope, proxies) == "10.0.0.1"


def test_client_ip_from_scope_falls_back_to_leftmost_when_chain_trusted():
    proxies = parse_allow_networks("127.0.0.1/32,10.0.0.1/32")[0]
    scope = {
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("127.0.0.1", 123),
    }
    assert client_ip_from_scope(scope, proxies) == "203.0.113.7"


def test_client_ip_from_scope_falls_back_to_peer():
    assert client_ip_from_scope({"headers": [], "client": ("10.0.0.5", 456)}) == "10.0.0.5"
    assert client_ip_from_scope({"headers": [], "client": None}) == ""


def test_client_ip_from_scope_caps_length():
    proxies = parse_allow_networks("127.0.0.1/32")[0]
    scope = {
        "headers": [(b"x-forwarded-for", b"9" * 100)],
        "client": ("127.0.0.1", 123),
    }
    assert client_ip_from_scope(scope, proxies) == "9" * 64


def test_resolve_source_ip_defaults_to_applicant():
    assert resolve_source_ip("10.0.0.5") == "10.0.0.5"
    assert resolve_source_ip("10.0.0.5", None) == "10.0.0.5"


def test_resolve_source_ip_honours_admin_override():
    """An admin may issue a credential for a script on another machine."""
    assert resolve_source_ip("10.0.0.5", " 172.16.0.9 ") == "172.16.0.9"
    assert resolve_source_ip("10.0.0.5", "::ffff:172.16.0.9") == "172.16.0.9"


def test_resolve_source_ip_without_any_ip_is_refused():
    with pytest.raises(ValidationError, match="无法确定申请来源 IP"):
        resolve_source_ip("")
    with pytest.raises(ValidationError, match="无法确定申请来源 IP"):
        resolve_source_ip("", "   ")


def test_resolve_source_ip_rejects_invalid_override():
    with pytest.raises(ValidationError, match="来源 IP 不合法"):
        resolve_source_ip("10.0.0.5", "10.0.0.5/32")


def test_v4_mapped_network_folds_and_loses_its_prefix():
    """N12: `::ffff:10.0.0.0/120` must equal `10.0.0.0/24`, otherwise it never matches IPv4."""
    networks, invalid = parse_allow_networks("::ffff:10.0.0.0/120")
    assert invalid == []
    assert [network.compressed for network in networks] == ["10.0.0.0/24"]
    assert ip_in_networks("10.0.0.7", networks) is True


def test_v4_mapped_network_with_wide_prefix_is_invalid():
    networks, invalid = parse_allow_networks("::ffff:10.0.0.0/95")
    assert networks == ()
    assert invalid == ["::ffff:10.0.0.0/95"]


def test_v4_mapped_network_dedupes_with_plain_v4():
    networks, invalid = parse_allow_networks("10.0.0.0/24, ::ffff:10.0.0.0/120")
    assert invalid == []
    assert len(networks) == 1


def test_ip_in_networks_skips_other_version():
    """N13: a mixed v4/v6 list must never raise TypeError on either version and must not match across versions."""
    networks, invalid = parse_allow_networks("10.0.0.0/24, 2001:db8::/64")
    assert invalid == []
    assert ip_in_networks("10.0.0.7", networks) is True
    assert ip_in_networks("2001:db8::1", networks) is True
    assert ip_in_networks("10.1.0.7", networks) is False
    assert ip_in_networks("2001:db9::1", networks) is False
    assert ip_in_networks("::ffff:10.0.0.7", networks) is True
    assert ip_in_networks("", networks) is False

