"""Short-lived MCP credentials: exchange, authenticate, revoke, and expiry cleanup.

An exchange-kind token cannot query data; it only mints these credentials. Each credential is
bound to one asserted actor, one source IP, and the parent token's scope snapshot, and expires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.credentials.service import STATUS_ACTIVE, STATUS_EXPIRED, STATUS_REVOKED
from app.db.models import ApiToken, ShortCredential
from app.errors import NotFound, ValidationError
from app.i18n import t
from app.netip import ip_matches
from app.security.actor import Actor
from app.security.crypto import generate_short_credential, hash_token, token_prefix
from app.security.principal import Principal
from app.tokens.scope import UNRESTRICTED, TokenScope

logger = logging.getLogger(__name__)

CLEANUP_BATCH_SIZE = 1000
CREDENTIAL_MIN_LENGTH = 20
# Authentication is on the hot path; refresh the usage timestamp at most once a minute.
LAST_USED_THROTTLE_SECONDS = 60

SHORT_CREDENTIAL_EXPIRED = "SHORT_CREDENTIAL_EXPIRED"
SHORT_CREDENTIAL_REVOKED = "SHORT_CREDENTIAL_REVOKED"
SHORT_CREDENTIAL_IP_MISMATCH = "SHORT_CREDENTIAL_IP_MISMATCH"
PARENT_TOKEN_DISABLED = "PARENT_TOKEN_DISABLED"


class ShortCredentialError(Exception):
    """A known credential was rejected; the code goes to the audit while the client still gets 401."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ShortCredentialView:
    id: int
    credential_prefix: str
    parent_user_name: str
    actor_provider: str
    actor_id: str
    actor_name: str | None
    source_ip: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    status: str


def short_credential_status(row: ShortCredential, now: datetime) -> str:
    if row.revoked_at is not None:
        return STATUS_REVOKED
    if row.expires_at <= now:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


class ShortCredentialService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        audit,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._audit = audit

    async def issue(
        self,
        *,
        parent: Principal,
        actor: Actor,
        client_ip: str,
        ttl_minutes: int | None = None,
    ) -> tuple[str, ShortCredential]:
        """Mint a credential for one actor; the plaintext is returned once and never stored."""
        minutes = self._resolve_ttl(ttl_minutes)
        if not (client_ip or "").strip():
            raise ValidationError(t("error.exchange.source_ip_required"))
        now = utcnow()
        expires_at = now + timedelta(minutes=minutes)
        plaintext = generate_short_credential()
        scope_json = parent.scope.to_json() if parent.scope.restricted else None
        async with self._session_factory() as session:
            superseded = (
                await session.scalars(
                    select(ShortCredential).where(
                        ShortCredential.parent_token_id == parent.token_id,
                        ShortCredential.actor_provider == actor.provider,
                        ShortCredential.actor_id == actor.actor_id,
                        ShortCredential.revoked_at.is_(None),
                        ShortCredential.expires_at > now,
                    )
                )
            ).all()
            for old in superseded:
                old.revoked_at = now
            row = ShortCredential(
                credential_hash=hash_token(plaintext),
                credential_prefix=token_prefix(plaintext),
                parent_token_id=parent.token_id,
                actor_provider=actor.provider,
                actor_id=actor.actor_id,
                actor_name=actor.name,
                scope_json=scope_json,
                source_ip=client_ip.strip(),
                expires_at=expires_at,
                created_at=now,
                last_used_at=now,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        issuer = f"exchange:{parent.token_prefix}"
        for old in superseded:
            await self._audit.record_credential_event(
                account_name=old.credential_prefix,
                user_name=parent.user_name,
                datasource=None,
                action="exchange_supersede",
                admin_username=issuer,
                client_ip=client_ip,
            )
        await self._audit.record_credential_event(
            account_name=row.credential_prefix,
            user_name=parent.user_name,
            datasource=None,
            action="exchange_issue",
            admin_username=issuer,
            client_ip=client_ip,
        )
        return plaintext, row

    async def authenticate(self, plaintext: str, client_ip: str) -> Principal | None:
        """Resolve a short credential to a Principal carrying its bound actor.

        Unknown credentials return None; known-but-invalid ones raise ShortCredentialError so the
        audit distinguishes expiry, revocation, IP mismatch, and a disabled parent token.
        """
        if (
            not plaintext
            or not plaintext.startswith("sc_")
            or len(plaintext) < CREDENTIAL_MIN_LENGTH
        ):
            return None
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ShortCredential).where(
                    ShortCredential.credential_hash == hash_token(plaintext)
                )
            )
            if row is None:
                return None
            now = utcnow()
            if row.revoked_at is not None:
                raise ShortCredentialError(SHORT_CREDENTIAL_REVOKED)
            if row.expires_at <= now:
                raise ShortCredentialError(SHORT_CREDENTIAL_EXPIRED)
            if not ip_matches(row.source_ip, client_ip):
                raise ShortCredentialError(SHORT_CREDENTIAL_IP_MISMATCH)
            parent = await session.get(ApiToken, row.parent_token_id)
            if (
                parent is None
                or not parent.enabled
                or (parent.expires_at is not None and parent.expires_at <= now)
            ):
                raise ShortCredentialError(PARENT_TOKEN_DISABLED)
            scope = UNRESTRICTED
            if row.scope_json is not None:
                try:
                    scope = TokenScope.from_json(row.scope_json)
                except (ValueError, ValidationError) as exc:
                    logger.error("短期凭证 %s 授权范围无法解析，拒绝认证：%s", row.id, exc)
                    return None
            if (
                row.last_used_at is None
                or (now - row.last_used_at).total_seconds() >= LAST_USED_THROTTLE_SECONDS
            ):
                row.last_used_at = now
                await session.commit()
            actor = Actor(
                provider=row.actor_provider, actor_id=row.actor_id, name=row.actor_name
            )
            return Principal(
                user_name=parent.user_name,
                token_id=parent.id,
                token_prefix=parent.token_prefix,
                scope=scope,
                actor_policy="require",
                token_kind="short",
                bound_actor=actor,
                short_prefix=row.credential_prefix,
            )

    async def revoke(self, credential_id: int, admin_username: str, client_ip: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(ShortCredential, credential_id)
            if row is None:
                raise NotFound("凭证不存在")
            if row.revoked_at is None:
                row.revoked_at = utcnow()
                await session.commit()
            prefix = row.credential_prefix
            parent_token_id = row.parent_token_id
        user_name = await self._parent_user_name(parent_token_id)
        await self._audit.record_credential_event(
            account_name=prefix,
            user_name=user_name,
            datasource=None,
            action="exchange_revoke",
            admin_username=admin_username,
            client_ip=client_ip,
        )

    async def list_all(self) -> list[ShortCredentialView]:
        now = utcnow()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ShortCredential, ApiToken.user_name)
                    .join(ApiToken, ApiToken.id == ShortCredential.parent_token_id)
                    .order_by(ShortCredential.created_at.desc(), ShortCredential.id.desc())
                )
            ).all()
        return [
            ShortCredentialView(
                id=row.id,
                credential_prefix=row.credential_prefix,
                parent_user_name=user_name,
                actor_provider=row.actor_provider,
                actor_id=row.actor_id,
                actor_name=row.actor_name,
                source_ip=row.source_ip,
                created_at=row.created_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                last_used_at=row.last_used_at,
                status=short_credential_status(row, now),
            )
            for row, user_name in rows
        ]

    async def cleanup_expired(self, now_utc: datetime | None = None) -> int:
        cutoff = now_utc or utcnow()
        deleted = 0
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(ShortCredential, ApiToken.user_name)
                        .join(ApiToken, ApiToken.id == ShortCredential.parent_token_id)
                        .where(ShortCredential.expires_at < cutoff)
                        .order_by(ShortCredential.id)
                        .limit(CLEANUP_BATCH_SIZE)
                    )
                ).all()
                if not rows:
                    break
                expired = [
                    (row.id, row.credential_prefix, user_name) for row, user_name in rows
                ]
                for row, _ in rows:
                    await session.delete(row)
                await session.commit()
            deleted += len(expired)
            for _, prefix, user_name in expired:
                await self._audit.record_credential_event(
                    account_name=prefix,
                    user_name=user_name,
                    datasource=None,
                    action="exchange_expire_cleanup",
                    admin_username="system",
                    client_ip="",
                )
            if len(expired) < CLEANUP_BATCH_SIZE:
                break
        return deleted

    def _resolve_ttl(self, ttl_minutes: int | None) -> int:
        minutes = (
            self._settings.exchange_ttl_minutes if ttl_minutes is None else ttl_minutes
        )
        if not 1 <= minutes <= self._settings.exchange_max_ttl_minutes:
            raise ValidationError(
                t("error.exchange.ttl_range", max=self._settings.exchange_max_ttl_minutes)
            )
        return minutes

    async def _parent_user_name(self, token_id: int) -> str:
        async with self._session_factory() as session:
            row = await session.get(ApiToken, token_id)
        return row.user_name if row is not None else "unknown"