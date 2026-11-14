"""Source-IP normalization and binding checks.

At issue time we record the requester IP (on HTTP only when the TCP peer is inside a trusted
proxy do we trust X-Forwarded-For; on MySQL we use the peername), and at use time we compare
against the TCP peername; `::ffff:10.0.0.1` and `10.0.0.1` must be normalized before comparing,
otherwise dual-stack listening rejects every legitimate connection.
"""

from __future__ import annotations

import ipaddress

from starlette.datastructures import Headers

from app.i18n import t

IP_MAX_LEN = 64
ALLOW_MAX_ENTRIES = 50
ALLOW_VALUE_MAX_LEN = 1024
ALLOW_ERROR_ITEMS = 5

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def normalize_ip(raw: str | None) -> str:
    """Normalize an IP literal; fold IPv4-mapped IPv6 into IPv4; return an empty string for empty or invalid input."""
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return ""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.compressed[:IP_MAX_LEN]


def ip_matches(source_ip: str | None, peer_ip: str) -> bool:
    """Whether this connection's source matches the source IP the credential is bound to.

    An empty binding means unbound (legacy credentials from before the migration); an invalid
    binding or a missing connection source is treated as a deny, matching the fail-closed policy
    of the export allowlist.
    """
    if source_ip is None or not source_ip.strip():
        return True
    bound = normalize_ip(source_ip)
    peer = normalize_ip(peer_ip)
    return bool(bound) and bool(peer) and bound == peer


def client_ip_from_scope(scope, trusted_proxies: tuple[Network, ...] = ()) -> str:
    """Client IP from the ASGI scope, truncated to 64 characters.

    Trust `X-Forwarded-For` only when the TCP peer is inside `trusted_proxies`: the header is
    forgeable, so a directly exposed gateway uses the peer address. Walk the trusted chain right
    to left and take the first untrusted hop (the rightmost is what the nearest proxy actually
    saw); fall back to the leftmost value when the whole chain is trusted.
    """
    client = scope.get("client")
    peer = str(client[0])[:IP_MAX_LEN] if client else ""
    if not peer or not ip_in_networks(peer, trusted_proxies):
        return peer
    forwarded = Headers(scope=scope).get("x-forwarded-for")
    if not forwarded:
        return peer
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not ip_in_networks(hop, trusted_proxies):
            return hop[:IP_MAX_LEN]
    return (hops[0] if hops else peer)[:IP_MAX_LEN]


def split_allow_entries(text: str | None) -> list[str]:
    """Split allowlist text on newlines and commas, dropping blank entries and preserving input order."""
    items: list[str] = []
    for chunk in (text or "").replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            items.append(item)
    return items


def _literal_is_v4_mapped(item: str) -> bool:
    try:
        address = ipaddress.ip_address(item.split("/", 1)[0].strip())
    except ValueError:
        return False
    return getattr(address, "ipv4_mapped", None) is not None


def _normalize_network(item: str, network: Network) -> Network | None:
    """Fold an IPv4-mapped IPv6 network into IPv4; prefixes narrower than /96 cannot be mapped and are rejected as invalid.

    For prefixes narrower than /96, `ip_network` normalizes network_address into a plain IPv6
    address and `ipv4_mapped` disappears, so the check must fall back to the original literal;
    otherwise such entries can be saved yet never match an IPv4 source (N12).
    """
    if isinstance(network, ipaddress.IPv6Network):
        mapped = network.network_address.ipv4_mapped
        if mapped is not None:
            return ipaddress.IPv4Network((mapped, network.prefixlen - 96))
        if _literal_is_v4_mapped(item):
            return None
    return network


def parse_allow_networks(text: str | None, strict: bool = False) -> tuple[tuple[Network, ...], list[str]]:
    """Parse allowlist text into (deduplicated networks, invalid entries).

    `strict=True` rejects host bits (e.g. `192.0.2.5/24`): such input usually means a mask was
    added to a single host by mistake, and allowing the whole range would silently widen
    exposure. Settings-page saves must use strict mode.
    """
    unique: dict[Network, None] = {}
    invalid: list[str] = []
    for item in split_allow_entries(text):
        try:
            network = _normalize_network(item, ipaddress.ip_network(item, strict=strict))
        except ValueError:
            network = None
        if network is None:
            invalid.append(item[:IP_MAX_LEN])
            continue
        unique[network] = None
    return tuple(unique), invalid


def format_allow_value(networks: tuple[Network, ...] | list[Network]) -> str:
    """Normalize into a comma-separated storage value; entries are deduplicated by network."""
    return ",".join(network.compressed for network in networks)


def has_host_bits(item: str) -> bool:
    """Whether an entry is a network with host bits (e.g. `192.0.2.5/24`); unparsable entries return False."""
    value = (item or "").strip()
    if "/" not in value:
        return False
    try:
        network = ipaddress.ip_network(value, strict=False)
        host = ipaddress.ip_address(value.split("/")[0])
    except ValueError:
        return False
    return network.network_address != host


def describe_invalid_entries(invalid: list[str], limit: int = ALLOW_ERROR_ITEMS) -> str:
    """Describe each invalid entry; host-bit problems are called out separately, otherwise users only see "invalid"."""
    named = [
        t("error.allowlist.host_bits", item=item) if has_host_bits(item) else item
        for item in invalid[:limit]
    ]
    if len(invalid) > limit:
        named.append(t("error.allowlist.more_items", n=len(invalid)))
    return t("common.list_separator").join(named)


def ip_in_networks(ip: str, networks: tuple[Network, ...]) -> bool:
    """Whether the source IP falls in any network; a missing or invalid IP is a deny (fail-closed)."""
    if not networks:
        return False
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    # Membership raises TypeError across versions on older Pythons, so filter by version first.
    return any(
        address in network
        for network in networks
        if network.version == address.version
    )
