"""Temporary export credentials: issue, revoke, auth lookup, and expiry cleanup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.db.models import ApiToken, DataSource, ExportCredential
from app.errors import GatewayError, NotFound, ValidationError
from app.i18n import t
from app.netip import IP_MAX_LEN, normalize_ip
from app.security.crypto import (
    generate_export_account,
    generate_export_password,
    native_auth_string,
)
from app.security.principal import Principal
from app.tokens.service import TokenService
from app.datasources import databases
from app.datasources.service import DataSourceService

logger = logging.getLogger(__name__)

ACCOUNT_RETRY_LIMIT = 5
CLEANUP_BATCH_SIZE = 1000

CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
CREDENTIAL_PASSWORD_INVALID = "CREDENTIAL_PASSWORD_INVALID"
TOKEN_DISABLED = "TOKEN_DISABLED"
DATASOURCE_MISSING = "DATASOURCE_MISSING"
DATASOURCE_DISABLED = "DATASOURCE_DISABLED"
IP_NOT_ALLOWED = "IP_NOT_ALLOWED"
CREDENTIAL_IP_MISMATCH = "CREDENTIAL_IP_MISMATCH"
CREDENTIAL_ISSUE_DENIED = "CREDENTIAL_ISSUE_DENIED"

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"

# Auto-issued credentials are identified by mcp:<token_prefix>; token_prefix always starts with gw_,
# so the gw_ prefix rules out collisions such as an admin username starting with "mcp:".
AUTO_ISSUER_PREFIX = "mcp:"
AUTO_ISSUER_LIKE = "mcp:gw\\_%"


class CredentialAuthError(GatewayError):
    """Rejected during authentication; the deny code goes to the audit only and clients always get 1045."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExportCredentialView:
    id: int
    account_name: str
    user_name: str
    datasource_name: str
    default_database: str
    issued_by: str
    source_ip: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    status: str


@dataclass(frozen=True)
class AutoIssuedCredential:
    """Auto-issue result for MCP handoff; the plaintext exists only in the return value."""

    account_name: str
    password: str
    expires_at: datetime
    datasource_id: int
    source_ip: str


def resolve_source_ip(client_ip: str, source_ip: str | None = None) -> str:
    """Resolve the source IP the credential is bound to.

    source_ip can only be set from the admin UI (credentials are often issued for scripts on
    another machine); it defaults to the requesting IP. Missing or invalid input refuses to issue:
    an unbound credential delegates revocation granularity to the TTL.
    """
    candidate = (source_ip or "").strip() or (client_ip or "").strip()
    if not candidate:
        raise ValidationError("无法确定申请来源 IP，拒绝签发未绑定来源的凭证")
    normalized = normalize_ip(candidate)
    if not normalized:
        raise ValidationError(
            t("error.credential.source_ip_invalid", value=candidate[:IP_MAX_LEN])
        )
    return normalized


def credential_status(row: ExportCredential, now: datetime) -> str:
    if row.revoked_at is not None:
        return STATUS_REVOKED
    if row.expires_at <= now:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


class CredentialService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        tokens: TokenService,
        datasources: DataSourceService,
        audit: Any,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._tokens = tokens
        self._datasources = datasources
        self._audit = audit

    async def issue(
        self,
        *,
        token_id: int,
        datasource_id: int,
        database: str,
        ttl_hours: int,
        admin_username: str,
        client_ip: str,
        source_ip: str | None = None,
    ) -> tuple[str, str]:
        """Issue a credential and return (account name, plaintext password); the plaintext appears once.

        database is the caller-selected target database and is fixed at issue time; source_ip lets
        an admin pin the actual consumer IP (credentials are often delivered to scripts elsewhere)
        and defaults to the requesting IP when empty.
        """
        datasource: DataSource | None = None
        try:
            if not 1 <= int(ttl_hours) <= self._settings.mysql_export_max_ttl_hours:
                raise ValidationError(
                    t(
                        "error.credential.ttl_range",
                        max=self._settings.mysql_export_max_ttl_hours,
                    )
                )
            token = await self._token_for_issue(token_id)
            datasource = await self._datasource_for_issue(datasource_id)
            scope = await self._tokens.scope_for(token.id)
            if not scope.allows_datasource(datasource.id):
                raise ValidationError("数据源不在该 Token 授权范围内")
            await self._validate_database(datasource, scope, database)
            bound_ip = resolve_source_ip(client_ip, source_ip)
        except ValidationError as exc:
            await self._record_issue_denied(str(exc), datasource, client_ip)
            raise

        expires_at = utcnow() + timedelta(hours=int(ttl_hours))
        account_name, password = await self._create_credential(
            token=token,
            datasource=datasource,
            database=database,
            issued_by=admin_username,
            expires_at=expires_at,
            source_ip=bound_ip,
        )
        await self._audit.record_credential_event(
            account_name=account_name,
            user_name=token.user_name,
            datasource=datasource,
            action="issue",
            admin_username=admin_username,
            client_ip=client_ip,
        )
        return account_name, password

    async def issue_auto(
        self,
        *,
        token_id: int,
        datasource_id: int,
        database: str,
        principal: Principal,
        trigger_request_id: str,
        client_ip: str,
        reason: str | None = None,
    ) -> AutoIssuedCredential:
        """Issue for MCP handoff or the explicit tool; the plaintext appears once and failures raise ValidationError only.

        database is the effective default database of this request, so re-running the same SQL
        lands in the same database. A non-empty reason means the Agent called
        issue_export_credential explicitly; the audit separates issue_explicit from the
        truncation fallback issue_auto.
        """
        action = "issue_explicit" if reason else "issue_auto"
        datasource: DataSource | None = None
        try:
            token = await self._token_for_issue(token_id)
            datasource = await self._datasource_for_issue(datasource_id)
            scope = await self._tokens.scope_for(token.id)
            if not scope.allows_datasource(datasource.id):
                raise ValidationError("数据源不在该 Token 授权范围内")
            await self._validate_database(datasource, scope, database)
            bound_ip = resolve_source_ip(client_ip)
        except ValidationError as exc:
            await self._record_issue_denied(
                str(exc), datasource, client_ip, tool=action
            )
            raise

        issued_by = f"{AUTO_ISSUER_PREFIX}{principal.token_prefix}"
        await self._supersede_auto_credentials(
            token.id, datasource.id, trigger_request_id, client_ip
        )
        expires_at = utcnow() + timedelta(
            seconds=self._settings.mcp_export_ttl_seconds
        )
        account_name, password = await self._create_credential(
            token=token,
            datasource=datasource,
            database=database,
            issued_by=issued_by,
            expires_at=expires_at,
            source_ip=bound_ip,
        )
        await self._audit.record_credential_event(
            account_name=account_name,
            user_name=token.user_name,
            datasource=datasource,
            action=action,
            # audit_log.request_id is unique, so it cannot be shared with the triggering MCP request; record the link here instead.
            admin_username=f"{issued_by} req={trigger_request_id}"
            + (f" reason={reason}" if reason else ""),
            client_ip=client_ip,
        )
        return AutoIssuedCredential(
            account_name=account_name,
            password=password,
            expires_at=expires_at,
            datasource_id=datasource.id,
            source_ip=bound_ip,
        )

    async def revoke(self, credential_id: int, admin_username: str, client_ip: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(ExportCredential, credential_id)
            if row is None:
                raise NotFound("凭证不存在")
            if row.revoked_at is None:
                row.revoked_at = utcnow()
                await session.commit()
            account_name = row.account_name
            token_id = row.token_id
            datasource_id = row.datasource_id
        user_name = await self._token_user_name(token_id)
        datasource = await self._datasource_or_none(datasource_id)
        await self._audit.record_credential_event(
            account_name=account_name,
            user_name=user_name,
            datasource=datasource,
            action="revoke",
            admin_username=admin_username,
            client_ip=client_ip,
        )

    async def get_active(self, account_name: str) -> ExportCredential | None:
        row = await self._get_by_account(account_name)
        if row is None or row.revoked_at is not None or row.expires_at <= utcnow():
            return None
        return row

    async def get_for_auth(self, account_name: str) -> tuple[ExportCredential | None, str | None]:
        row = await self._get_by_account(account_name)
        if row is None:
            return None, CREDENTIAL_NOT_FOUND
        if row.revoked_at is not None:
            return row, CREDENTIAL_REVOKED
        if row.expires_at <= utcnow():
            return row, CREDENTIAL_EXPIRED
        return row, None

    async def resolve_principal(
        self, credential: ExportCredential
    ) -> tuple[Principal, DataSource]:
        """Credential -> (Principal, datasource); raises CredentialAuthError when the token or datasource is unavailable."""
        token = await self._token_or_none(credential.token_id)
        if (
            token is None
            or not token.enabled
            or (token.expires_at is not None and token.expires_at <= utcnow())
        ):
            raise CredentialAuthError(TOKEN_DISABLED)
        datasource = await self.datasource_for(credential)
        scope = await self._tokens.scope_for(token.id)
        return Principal(
            user_name=token.user_name,
            token_id=token.id,
            token_prefix=token.token_prefix,
            scope=scope,
        ), datasource

    async def datasource_for(self, credential: ExportCredential) -> DataSource:
        datasource = await self._datasource_or_none(credential.datasource_id)
        if datasource is None:
            raise CredentialAuthError(DATASOURCE_MISSING)
        if not datasource.enabled:
            raise CredentialAuthError(DATASOURCE_DISABLED)
        return datasource

    async def touch_last_used(self, credential_id: int) -> None:
        try:
            async with self._session_factory() as session:
                row = await session.get(ExportCredential, credential_id)
                if row is not None:
                    row.last_used_at = utcnow()
                    await session.commit()
        except Exception:  # noqa: BLE001 - the usage timestamp does not affect authentication
            logger.warning("更新凭证最后使用时间失败 credential_id=%s", credential_id)

    async def list_all(self) -> list[ExportCredentialView]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ExportCredential, ApiToken.user_name, DataSource.name)
                    .join(ApiToken, ApiToken.id == ExportCredential.token_id)
                    .join(DataSource, DataSource.id == ExportCredential.datasource_id)
                    .order_by(ExportCredential.created_at.desc(), ExportCredential.id.desc())
                )
            ).all()
        now = utcnow()
        return [
            ExportCredentialView(
                id=row.id,
                account_name=row.account_name,
                user_name=user_name,
                datasource_name=datasource_name,
                default_database=row.default_database,
                issued_by=row.issued_by,
                source_ip=row.source_ip,
                created_at=row.created_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                last_used_at=row.last_used_at,
                status=credential_status(row, now),
            )
            for row, user_name, datasource_name in rows
        ]

    async def cleanup_expired(self, now_utc: datetime | None = None) -> int:
        cutoff = now_utc or utcnow()
        deleted = 0
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(ExportCredential, ApiToken.user_name, DataSource.name)
                        .join(ApiToken, ApiToken.id == ExportCredential.token_id)
                        .join(DataSource, DataSource.id == ExportCredential.datasource_id)
                        .where(ExportCredential.expires_at < cutoff)
                        .order_by(ExportCredential.id)
                        .limit(CLEANUP_BATCH_SIZE)
                    )
                ).all()
                if not rows:
                    break
                ids = [row.id for row, _, _ in rows]
                credentials = [(row.id, row.account_name, row.datasource_id, user_name, ds_name)
                               for row, user_name, ds_name in rows]
                for row, _, _ in rows:
                    await session.delete(row)
                await session.commit()
            deleted += len(ids)
            for _, account_name, datasource_id, user_name, ds_name in credentials:
                await self._audit.record_credential_event(
                    account_name=account_name,
                    user_name=user_name,
                    datasource=_DatasourceRef(datasource_id, ds_name),
                    action="expire_cleanup",
                    admin_username="system",
                    client_ip="",
                )
            if len(ids) < CLEANUP_BATCH_SIZE:
                break
        return deleted

    # --- Internal helpers -------------------------------------------------

    async def _create_credential(
        self,
        *,
        token: ApiToken,
        datasource: DataSource,
        database: str,
        issued_by: str,
        expires_at: datetime,
        source_ip: str,
    ) -> tuple[str, str]:
        """Generate account name and password, persist, and return (account name, plaintext password)."""
        for _ in range(ACCOUNT_RETRY_LIMIT):
            account_name = generate_export_account()
            password = generate_export_password()
            row = ExportCredential(
                account_name=account_name,
                auth_string=native_auth_string(password),
                token_id=token.id,
                datasource_id=datasource.id,
                default_database=database,
                issued_by=issued_by,
                source_ip=source_ip,
                expires_at=expires_at,
                created_at=utcnow(),
            )
            async with self._session_factory() as session:
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
            return account_name, password
        raise ValidationError("凭证账号生成冲突，请重试")

    async def _supersede_auto_credentials(
        self,
        token_id: int,
        datasource_id: int,
        trigger_request_id: str,
        client_ip: str,
    ) -> None:
        """Revoke the oldest auto credentials when the active cap is hit; manual credentials are never touched."""
        now = utcnow()
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ExportCredential)
                        .where(
                            ExportCredential.token_id == token_id,
                            ExportCredential.datasource_id == datasource_id,
                            ExportCredential.revoked_at.is_(None),
                            ExportCredential.expires_at > now,
                            ExportCredential.issued_by.like(
                                AUTO_ISSUER_LIKE, escape="\\"
                            ),
                        )
                        .order_by(ExportCredential.created_at, ExportCredential.id)
                    )
                )
                .scalars()
                .all()
            )
            excess = len(rows) - self._settings.mcp_export_max_active + 1
            if excess <= 0:
                return
            superseded = rows[:excess]
            for row in superseded:
                row.revoked_at = now
            await session.commit()
            account_names = [row.account_name for row in superseded]
        user_name = await self._token_user_name(token_id)
        datasource = await self._datasource_or_none(datasource_id)
        for account_name in account_names:
            await self._audit.record_credential_event(
                account_name=account_name,
                user_name=user_name,
                datasource=datasource,
                action="revoke",
                admin_username=f"auto_superseded req={trigger_request_id}",
                client_ip=client_ip,
            )

    async def _record_issue_denied(
        self,
        message: str,
        datasource: DataSource | None,
        client_ip: str,
        tool: str = "issue",
    ) -> None:
        await self._audit.record_denied(
            request_id=str(uuid4()),
            event_type="credential_lifecycle",
            principal=None,
            datasource=datasource,
            tool=tool,
            sql_text=None,
            client_ip=client_ip,
            code=CREDENTIAL_ISSUE_DENIED,
            message=message,
        )

    async def _validate_database(
        self, datasource: DataSource, scope, database: str
    ) -> None:
        """The target database must be both exposed and usable by this token.

        Both failure modes share one message: issue failures are admin-side 400s, and telling them
        apart would reveal whether the database exists.
        """
        databases.use_statement(database)
        exposed, policies = await databases.exposed_and_policies(
            self._session_factory, datasource
        )
        available = databases.available_databases(
            sorted(exposed), policies, scope, datasource.id
        )
        if not available:
            raise ValidationError("该 Token 在此数据源下没有可用库，请先调整授权")
        if database not in exposed or database not in {item.name for item in available}:
            raise ValidationError("目标库不存在或未授权")

    async def _token_for_issue(self, token_id: int) -> ApiToken:
        token = await self._token_or_none(token_id)
        if (
            token is None
            or not token.enabled
            or (token.expires_at is not None and token.expires_at <= utcnow())
        ):
            raise ValidationError("Token 不存在或已停用")
        if token.actor_policy == "require":
            raise ValidationError(t("error.credential.agent_token_no_export"))
        return token

    async def _datasource_for_issue(self, datasource_id: int) -> DataSource:
        datasource = await self._datasource_or_none(datasource_id)
        if datasource is None or not datasource.enabled:
            raise ValidationError("数据源不存在或已停用")
        return datasource

    async def _get_by_account(self, account_name: str) -> ExportCredential | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ExportCredential).where(ExportCredential.account_name == account_name)
            )

    async def _token_or_none(self, token_id: int) -> ApiToken | None:
        async with self._session_factory() as session:
            return await session.get(ApiToken, token_id)

    async def _token_user_name(self, token_id: int) -> str:
        token = await self._token_or_none(token_id)
        return token.user_name if token is not None else "unknown"

    async def _datasource_or_none(self, datasource_id: int) -> DataSource | None:
        async with self._session_factory() as session:
            return await session.get(DataSource, datasource_id)


@dataclass(frozen=True)
class _DatasourceRef:
    id: int
    name: str
