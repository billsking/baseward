"""Runtime source of the source-IP allowlist: values saved on the settings page win; if never saved, fall back to the environment variable.

The export channel and the MCP entrypoint each own an `IpAllowlist`. What an empty value means
must be decided at read time:

- Saved from the settings page (even an empty value) -> enforce the saved value strictly; empty
  means deny all;
- Never saved -> use the same-name environment variable (`GATEWAY_` + upper-cased key); if that
  is empty too, the export channel denies all (v1 has no TLS, so the allowlist is the only network
  control) while the MCP entrypoint stays unrestricted (preserving behavior from before this
  control existed, so upgrading does not cut off existing callers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any

from app.clock import format_shanghai, utcnow
from app.db.models import SystemSetting
from app.errors import ValidationError
from app.i18n import t, translate_label
from app.netip import (
    ALLOW_MAX_ENTRIES,
    ALLOW_VALUE_MAX_LEN,
    Network,
    describe_invalid_entries,
    format_allow_value,
    ip_in_networks,
    parse_allow_networks,
)

logger = logging.getLogger(__name__)

EXPORT_ALLOW_KEY = "mysql_export_allow_cidrs"
MCP_ALLOW_KEY = "mcp_allow_cidrs"
REFRESH_SECONDS = 5.0

SOURCE_DB = "db"
SOURCE_ENV = "env"
SOURCE_UNSET = "unset"


@dataclass(frozen=True)
class AllowlistSnapshot:
    """Result of one read; `networks` is the decision basis currently in force."""

    networks: tuple[Network, ...]
    source: str
    allow_all: bool
    updated_at: datetime | None = None

    @property
    def entries(self) -> tuple[str, ...]:
        return tuple(network.compressed for network in self.networks)

    @property
    def edit_text(self) -> str:
        """Initial value of the settings textarea: the normalized effective value, not the raw input."""
        return "\n".join(self.entries)


class IpAllowlist:
    """Source-IP allowlist for one entrypoint: TTL-cached and effective immediately after saving."""

    def __init__(
        self,
        session_factory: Any,
        setting_key: str,
        label: str,
        env_text: str = "",
        allow_when_unset: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._key = setting_key
        self._env_text = env_text
        self._allow_when_unset = allow_when_unset
        self.label = label
        self._cached: AllowlistSnapshot | None = None
        self._cached_at = 0.0

    @property
    def setting_key(self) -> str:
        return self._key

    @property
    def env_key(self) -> str:
        """Matches pydantic's GATEWAY_ prefix so a key and its env var map to each other."""
        return f"GATEWAY_{self._key.upper()}"

    async def snapshot(self) -> AllowlistSnapshot:
        cached = self._cached
        if cached is not None and monotonic() - self._cached_at < REFRESH_SECONDS:
            return cached
        try:
            row = await self._read_row()
        except Exception:  # noqa: BLE001
            logger.exception("读取 IP 白名单失败 key=%s", self._key)
            if cached is not None:
                snapshot = cached
            else:
                # When no read has ever succeeded there is no way to tell "unset" from "configured to deny"; fail closed.
                # Cache this result so the TTL also acts as a negative cache: allowlist checks run before token
                # validation, so without it every request hits the database again during a management DB outage (W3).
                snapshot = AllowlistSnapshot(
                    networks=(), source=SOURCE_UNSET, allow_all=False
                )
            self._cached = snapshot
            self._cached_at = monotonic()
            return snapshot
        fresh = self._build(row)
        self._cached = fresh
        self._cached_at = monotonic()
        return fresh

    async def is_allowed(self, ip: str) -> bool:
        current = await self.snapshot()
        if current.allow_all:
            return True
        return ip_in_networks(ip, current.networks)

    def view(self, snapshot: AllowlistSnapshot) -> dict[str, Any]:
        """One display record for the settings page: form field name plus a description of the active source."""
        if snapshot.source == SOURCE_DB:
            saved = format_shanghai(snapshot.updated_at)
            source_text = (
                t("allowlist.source_db_entries", n=len(snapshot.entries), saved=saved)
                if snapshot.entries
                else t("allowlist.source_db_empty", saved=saved)
            )
        elif snapshot.source == SOURCE_ENV:
            source_text = t(
                "allowlist.source_env_entries",
                n=len(snapshot.entries),
                env=self.env_key,
            )
        elif snapshot.allow_all:
            source_text = t("allowlist.source_allow_all", env=self.env_key)
        else:
            source_text = t("allowlist.source_deny_all", env=self.env_key)
        return {
            "label": translate_label(self.label),
            "field": self._key,
            "text": snapshot.edit_text,
            "count": len(snapshot.entries),
            "allow_all": snapshot.allow_all,
            "source_text": source_text,
        }

    def prepare(self, text: str) -> str:
        """Validate and normalize input and return a value ready to persist; raise ValidationError on invalid input without touching the database.

        The settings page submits both entrypoints at once; validate everything before persisting
        anything so a bad value cannot take effect while the other is written.
        """
        networks, invalid = parse_allow_networks(text, strict=True)
        if invalid:
            raise ValidationError(
                t("error.allowlist.invalid", entries=describe_invalid_entries(invalid))
            )
        if len(networks) > ALLOW_MAX_ENTRIES:
            raise ValidationError(
                t(
                    "error.allowlist.too_many",
                    label=translate_label(self.label),
                    limit=ALLOW_MAX_ENTRIES,
                    count=len(networks),
                )
            )
        value = format_allow_value(networks)
        if len(value) > ALLOW_VALUE_MAX_LEN:
            raise ValidationError(
                t(
                    "error.allowlist.too_long",
                    label=translate_label(self.label),
                    length=len(value),
                    limit=ALLOW_VALUE_MAX_LEN,
                )
            )
        return value

    async def save(self, value: str) -> tuple[str, str] | None:
        """Persist the value normalized by `prepare` and return (before, after); effective immediately, no restart.

        When the value equals the stored one the whole operation is a no-op returning None: this
        neither bumps updated_at (which would make the settings page's "last saved" lie) nor adds
        audit noise.
        """
        result = await save_many([(self, value)])
        if not result:
            return None
        _allowlist, before, after = result[0]
        return before, after

    async def _read_row(self) -> tuple[str, datetime] | None:
        async with self._session_factory() as session:
            row = await session.get(SystemSetting, self._key)
        if row is None:
            return None
        return row.setting_value, row.updated_at

    def _build(self, row: tuple[str, datetime] | None) -> AllowlistSnapshot:
        if row is not None:
            # Same strict rule as settings-page saves: invalid entries or host bits written straight into the
            # database must not be silently dropped or widened (W6); when undecidable, deny all and log loudly.
            networks, invalid = parse_allow_networks(row[0], strict=True)
            if invalid:
                logger.error(
                    "%s 已保存的白名单含非法条目，按全部拒绝执行 key=%s 条目=%s",
                    self.label,
                    self._key,
                    describe_invalid_entries(invalid),
                )
                return AllowlistSnapshot(
                    networks=(),
                    source=SOURCE_DB,
                    allow_all=False,
                    updated_at=row[1],
                )
            return AllowlistSnapshot(
                networks=networks, source=SOURCE_DB, allow_all=False, updated_at=row[1]
            )
        networks, invalid = parse_allow_networks(self._env_text, strict=True)
        if invalid:
            logger.warning(
                "%s 含非法条目并已忽略：%s",
                self.env_key,
                describe_invalid_entries(invalid),
            )
        if networks:
            return AllowlistSnapshot(networks=networks, source=SOURCE_ENV, allow_all=False)
        return AllowlistSnapshot(
            networks=(), source=SOURCE_UNSET, allow_all=self._allow_when_unset
        )

    def _describe_before(self, row: tuple[str, datetime] | None) -> str:
        if row is not None:
            return row[0] or "（空，全部拒绝）"
        fallback = self._env_text.strip() or ("（不限制来源）" if self._allow_when_unset else "（全部拒绝）")
        return f"（未保存过，按 {self.env_key}={fallback}）"


async def save_many(
    entries: list[tuple[IpAllowlist, str]],
) -> list[tuple[IpAllowlist, str, str]]:
    """Persist the allowlists of several entrypoints in one transaction, returning the ones that really changed as (allowlist, before, after).

    Values must already be normalized by `prepare()`. Both entrypoints commit together or not at
    all: a partial failure must not leave one active and the other stale (W5).
    """
    pending: list[tuple[IpAllowlist, str, str]] = []
    unchanged: list[tuple[IpAllowlist, tuple[str, datetime]]] = []
    for allowlist, value in entries:
        stored = await allowlist._read_row()
        if stored is not None and stored[0] == value:
            unchanged.append((allowlist, stored))
            continue
        pending.append((allowlist, value, allowlist._describe_before(stored)))

    for allowlist, stored in unchanged:
        allowlist._cached = allowlist._build(stored)
        allowlist._cached_at = monotonic()
    if not pending:
        return []

    now = utcnow()
    factory = pending[0][0]._session_factory
    async with factory() as session:
        for allowlist, value, _before in pending:
            row = await session.get(SystemSetting, allowlist.setting_key)
            if row is None:
                session.add(
                    SystemSetting(
                        setting_key=allowlist.setting_key,
                        setting_value=value,
                        updated_at=now,
                    )
                )
            else:
                row.setting_value = value
                row.updated_at = now
        await session.commit()

    changed: list[tuple[IpAllowlist, str, str]] = []
    for allowlist, value, before in pending:
        allowlist._cached = allowlist._build((value, now))
        allowlist._cached_at = monotonic()
        logger.info(
            "%s IP 白名单已更新 key=%s 变更前=%s 变更后=%s",
            allowlist.label,
            allowlist.setting_key,
            before,
            value or "（空，全部拒绝）",
        )
        changed.append((allowlist, before, value))
    return changed
