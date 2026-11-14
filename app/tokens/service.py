"""User token lifecycle management."""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.db.models import ApiToken, DataSource, TokenScopeRow
from app.errors import NotFound, ValidationError
from app.i18n import t
from app.security.crypto import generate_token, hash_token, token_prefix
from app.security.principal import Principal
from app.tokens.scope import BROKEN_SCOPE, UNRESTRICTED, TokenScope

logger = logging.getLogger(__name__)

USERNAME_LIMIT = 64
TOKEN_MIN_LENGTH = 20
ACTOR_POLICIES = frozenset({"ignore", "require"})
TOKEN_KINDS = frozenset({"access", "exchange"})


class TokenService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_all(self) -> list[ApiToken]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ApiToken).order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
                )
            ).all()
        return list(rows)

    async def enabled_count(self) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(ApiToken).where(ApiToken.enabled == 1)
            )
        return int(count or 0)

    async def get(self, token_id: int) -> ApiToken:
        async with self._session_factory() as session:
            row = await session.get(ApiToken, token_id)
        if row is None:
            raise NotFound("Token 不存在")
        return row

    async def create(
        self,
        user_name: str,
        scope: TokenScope | None = None,
        actor_policy: str = "ignore",
        token_kind: str = "access",
    ) -> tuple[str, ApiToken]:
        user_name = (user_name or "").strip()
        if not 1 <= len(user_name) <= USERNAME_LIMIT:
            raise ValidationError(
                t("error.token.username_required", limit=USERNAME_LIMIT)
            )
        if token_kind not in TOKEN_KINDS:
            raise ValidationError(t("error.token.kind_invalid"))
        if token_kind == "exchange":
            # An exchange token only mints actor-bound credentials, so it always requires an actor.
            actor_policy = "require"
        if actor_policy not in ACTOR_POLICIES:
            raise ValidationError(t("error.token.actor_policy_invalid"))
        resolved = scope or UNRESTRICTED
        async with self._session_factory() as session:
            await self._require_datasources(session, resolved)
            existing = await session.scalar(
                select(ApiToken.id).where(ApiToken.user_name == user_name, ApiToken.enabled == 1)
            )
            if existing is not None:
                raise ValidationError("该用户已有启用 Token，请先停用")
            plaintext = generate_token()
            row = ApiToken(
                user_name=user_name,
                token_hash=hash_token(plaintext),
                token_prefix=token_prefix(plaintext),
                actor_policy=actor_policy,
                token_kind=token_kind,
                enabled=1,
                created_at=utcnow(),
            )
            session.add(row)
            try:
                await session.flush()
                if resolved.restricted:
                    now = utcnow()
                    session.add(
                        TokenScopeRow(
                            token_id=row.id,
                            scope_json=resolved.to_json(),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValidationError("Token 创建冲突，请重试") from exc
            await session.refresh(row)
        return plaintext, row

    async def authenticate(self, plaintext: str) -> Principal | None:
        if not plaintext or not plaintext.startswith("gw_") or len(plaintext) < TOKEN_MIN_LENGTH:
            return None
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ApiToken).where(
                    ApiToken.token_hash == hash_token(plaintext),
                    ApiToken.enabled == 1,
                )
            )
            if row is None:
                return None
            raw_scope = await session.scalar(
                select(TokenScopeRow.scope_json).where(TokenScopeRow.token_id == row.id)
            )
        scope = UNRESTRICTED
        if raw_scope is not None:
            try:
                scope = TokenScope.from_json(raw_scope)
            except (ValueError, ValidationError) as exc:
                logger.error("Token %s 授权范围无法解析，拒绝认证：%s", row.id, exc)
                return None
        return Principal(
            user_name=row.user_name,
            token_id=row.id,
            token_prefix=row.token_prefix,
            scope=scope,
            actor_policy=row.actor_policy,
            token_kind=row.token_kind,
        )

    async def update_actor_policy(self, token_id: int, actor_policy: str) -> None:
        if actor_policy not in ACTOR_POLICIES:
            raise ValidationError(t("error.token.actor_policy_invalid"))
        async with self._session_factory() as session:
            row = await session.get(ApiToken, token_id)
            if row is None:
                raise NotFound("Token 不存在")
            if row.token_kind == "exchange" and actor_policy != "require":
                raise ValidationError(t("error.token.exchange_policy_locked"))
            row.actor_policy = actor_policy
            await session.commit()

    async def update_scope(self, token_id: int, scope: TokenScope) -> None:
        async with self._session_factory() as session:
            row = await session.get(ApiToken, token_id)
            if row is None:
                raise NotFound("Token 不存在")
            await self._require_datasources(session, scope)
            existing = await session.get(TokenScopeRow, token_id)
            now = utcnow()
            if scope.restricted:
                if existing is None:
                    session.add(
                        TokenScopeRow(
                            token_id=token_id,
                            scope_json=scope.to_json(),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    existing.scope_json = scope.to_json()
                    existing.updated_at = now
            elif existing is not None:
                await session.delete(existing)
            await session.commit()

    async def scope_for(self, token_id: int) -> TokenScope:
        async with self._session_factory() as session:
            raw = await session.scalar(
                select(TokenScopeRow.scope_json).where(TokenScopeRow.token_id == token_id)
            )
        if raw is None:
            return UNRESTRICTED
        try:
            return TokenScope.from_json(raw)
        except (ValueError, ValidationError) as exc:
            logger.error("Token %s 授权范围无法解析：%s", token_id, exc)
            return BROKEN_SCOPE

    async def scopes_for(self, token_ids: Sequence[int]) -> dict[int, TokenScope]:
        if not token_ids:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(TokenScopeRow.token_id, TokenScopeRow.scope_json).where(
                        TokenScopeRow.token_id.in_(token_ids)
                    )
                )
            ).all()
        scopes = {token_id: UNRESTRICTED for token_id in token_ids}
        for token_id, raw in rows:
            try:
                scopes[token_id] = TokenScope.from_json(raw)
            except (ValueError, ValidationError) as exc:
                logger.error("Token %s 授权范围无法解析：%s", token_id, exc)
                scopes[token_id] = BROKEN_SCOPE
        return scopes

    @staticmethod
    async def _require_datasources(session: AsyncSession, scope: TokenScope) -> None:
        if not scope.restricted:
            return
        wanted = scope.effective_datasource_ids
        known = set(
            (await session.scalars(select(DataSource.id).where(DataSource.id.in_(wanted)))).all()
        )
        missing = sorted(wanted - known)
        if missing:
            raise ValidationError(
                t(
                    "error.token.missing_datasources",
                    ids=t("common.list_separator").join(str(item) for item in missing),
                )
            )

    async def disable(self, token_id: int) -> None:
        async with self._session_factory() as session:
            row = await session.get(ApiToken, token_id)
            if row is None:
                raise NotFound("Token 不存在")
            if row.enabled:
                row.enabled = 0
                row.disabled_at = utcnow()
                await session.commit()

    async def delete(self, token_id: int) -> None:
        async with self._session_factory() as session:
            row = await session.get(ApiToken, token_id)
            if row is None:
                raise NotFound("Token 不存在")
            await session.delete(row)
            await session.commit()
