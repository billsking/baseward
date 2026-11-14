"""Audit service: intent / update / query / retention / cleanup."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import SHANGHAI, utcnow
from app.db.models import AuditLog, SystemSetting
from app.errors import AuditUnavailableError
from app.sanitize import validate_retention_days
from app.security.actor import current_actor

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 180
CLEANUP_BATCH_SIZE = 5000
ERROR_MESSAGE_LIMIT = 4000


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class AuditService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _principal_fields(principal: Any | None) -> tuple[str, int | None, str | None]:
        if principal is None:
            return "unknown", None, None
        return principal.user_name, principal.token_id, principal.token_prefix

    @staticmethod
    def _actor_fields() -> tuple[str | None, str | None, str | None, str | None]:
        actor = current_actor.get()
        if actor is None:
            return None, None, None, None
        return actor.provider, actor.actor_id, actor.name, actor.trust

    async def start(
        self,
        request_id: str,
        event_type: str,
        principal: Any | None,
        datasource: Any | None,
        tool: str,
        sql_text: str | None,
        client_ip: str,
        credential_account: str | None = None,
        database_name: str | None = None,
    ) -> None:
        """Write the pre-execution intent; callers must block execution when this fails."""
        user_name, token_id, token_prefix = self._principal_fields(principal)
        actor_provider, actor_id, actor_name, actor_trust = self._actor_fields()
        row = AuditLog(
            request_id=request_id,
            event_type=event_type,
            user_name=user_name,
            token_id=token_id,
            token_prefix=token_prefix,
            credential_account=credential_account,
            actor_provider=actor_provider,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_trust=actor_trust,
            datasource_id=getattr(datasource, "id", None),
            datasource_name=getattr(datasource, "name", None),
            database_name=database_name,
            tool=tool,
            sql_text=sql_text,
            status="pending",
            client_ip=client_ip[:64] if client_ip else None,
            created_at=utcnow(),
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - any audit write failure must block execution
            logger.exception("审计 intent 写入失败 request_id=%s", request_id)
            raise AuditUnavailableError("审计服务不可用") from exc

    async def mark_success(self, request_id: str, duration_ms: int, row_count: int) -> None:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(AuditLog)
                    .where(AuditLog.request_id == request_id)
                    .values(
                        status="success",
                        duration_ms=duration_ms,
                        row_count=row_count,
                        finished_at=utcnow(),
                    )
                )
                if result.rowcount != 1:
                    await session.rollback()
                    raise AuditUnavailableError("审计记录更新失败")
                await session.commit()
        except AuditUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("审计 success 更新失败 request_id=%s", request_id)
            raise AuditUnavailableError("审计服务不可用") from exc

    async def mark_error(
        self,
        request_id: str,
        code: str,
        message: str,
        duration_ms: int,
    ) -> None:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(AuditLog)
                    .where(
                        AuditLog.request_id == request_id,
                        AuditLog.status.in_(("pending", "success")),
                    )
                    .values(
                        status="error",
                        deny_reason=code[:512],
                        error_message=message[:ERROR_MESSAGE_LIMIT],
                        duration_ms=duration_ms,
                        finished_at=utcnow(),
                    )
                )
                if result.rowcount == 0:
                    await session.rollback()
                    raise AuditUnavailableError("审计记录更新失败")
                await session.commit()
        except AuditUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("审计 error 更新失败 request_id=%s", request_id)
            raise AuditUnavailableError("审计服务不可用") from exc

    async def mark_interrupted(
        self,
        request_id: str,
        duration_ms: int,
        row_count: int,
        reason: str,
    ) -> None:
        """Best effort: update failures are logged instead of raised into a disconnected client."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(AuditLog)
                    .where(
                        AuditLog.request_id == request_id,
                        AuditLog.status.in_(("pending", "success")),
                    )
                    .values(
                        status="interrupted",
                        deny_reason=reason[:512],
                        duration_ms=duration_ms,
                        row_count=row_count,
                        finished_at=utcnow(),
                    )
                )
                if result.rowcount == 0:
                    await session.rollback()
                    logger.error(
                        "审计 interrupted 未命中记录 request_id=%s", request_id
                    )
                    return
                await session.commit()
        except Exception:  # noqa: BLE001 - the export channel is best effort
            logger.exception("审计 interrupted 更新失败 request_id=%s", request_id)

    async def mark_denied(
        self,
        request_id: str,
        code: str,
        message: str,
        duration_ms: int,
    ) -> None:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(AuditLog)
                    .where(AuditLog.request_id == request_id, AuditLog.status == "pending")
                    .values(
                        status="denied",
                        deny_reason=code[:512],
                        error_message=message[:ERROR_MESSAGE_LIMIT],
                        duration_ms=duration_ms,
                        finished_at=utcnow(),
                    )
                )
                if result.rowcount != 1:
                    await session.rollback()
                    raise AuditUnavailableError("审计记录更新失败")
                await session.commit()
        except AuditUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("审计 denied 更新失败 request_id=%s", request_id)
            raise AuditUnavailableError("审计服务不可用") from exc

    async def mark_denied_best_effort(
        self,
        request_id: str,
        code: str,
        message: str,
        duration_ms: int,
    ) -> None:
        try:
            await self.mark_denied(request_id, code, message, duration_ms)
        except AuditUnavailableError:
            logger.error("审计 denied 更新失败（best effort）request_id=%s code=%s", request_id, code)

    async def mark_error_best_effort(
        self,
        request_id: str,
        code: str,
        message: str,
        duration_ms: int,
    ) -> None:
        try:
            await self.mark_error(request_id, code, message, duration_ms)
        except AuditUnavailableError:
            logger.error("审计 error 更新失败（best effort）request_id=%s code=%s", request_id, code)

    async def record_denied(
        self,
        request_id: str,
        event_type: str,
        principal: Any | None,
        datasource: Any | None,
        tool: str | None,
        sql_text: str | None,
        client_ip: str,
        code: str,
        message: str,
        duration_ms: int = 0,
        credential_account: str | None = None,
        database_name: str | None = None,
    ) -> None:
        """Denials are recorded as denied; on failure only log and still return the denial to the AI."""
        user_name, token_id, token_prefix = self._principal_fields(principal)
        actor_provider, actor_id, actor_name, actor_trust = self._actor_fields()
        now = utcnow()
        row = AuditLog(
            request_id=request_id,
            event_type=event_type,
            user_name=user_name,
            token_id=token_id,
            token_prefix=token_prefix,
            credential_account=credential_account,
            actor_provider=actor_provider,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_trust=actor_trust,
            datasource_id=getattr(datasource, "id", None),
            datasource_name=getattr(datasource, "name", None),
            database_name=database_name,
            tool=tool,
            sql_text=sql_text,
            status="denied",
            deny_reason=code[:512],
            error_message=message[:ERROR_MESSAGE_LIMIT] if message else None,
            client_ip=client_ip[:64] if client_ip else None,
            created_at=now,
            finished_at=now,
            duration_ms=duration_ms,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001 - the deny path must not drop the response because auditing failed
            logger.exception("审计 denied 写入失败 request_id=%s", request_id)

    async def record_auth_failure(
        self,
        request_id: str,
        token_prefix: str | None,
        client_ip: str,
        code: str = "AUTH_INVALID",
    ) -> None:
        now = utcnow()
        row = AuditLog(
            request_id=request_id,
            event_type="auth_failure",
            user_name="unknown",
            token_prefix=(token_prefix or "")[:16] or None,
            status="denied",
            deny_reason=code[:512],
            client_ip=client_ip[:64] if client_ip else None,
            created_at=now,
            finished_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("认证失败审计写入失败 request_id=%s", request_id)

    async def record_setting_change(
        self,
        setting_key: str,
        before: str,
        after: str,
        admin_username: str,
        client_ip: str,
    ) -> None:
        """Record setting changes; if the diff exceeds deny_reason's length, keep the full text in details instead of losing the audit."""
        diff = f"{before} ⇒ {after}"
        now = utcnow()
        row = AuditLog(
            request_id=str(uuid4()),
            event_type="setting_change",
            user_name=(admin_username or "unknown")[:64],
            tool=setting_key[:32],
            status="success",
            deny_reason=diff[:512],
            error_message=diff if len(diff) > 512 else None,
            client_ip=client_ip[:64] if client_ip else None,
            created_at=now,
            finished_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("设置变更审计写入失败 key=%s", setting_key)

    async def record_policy_change(
        self,
        datasource_name: str,
        before: dict[str, bool],
        after: dict[str, bool],
        admin_username: str,
        client_ip: str,
    ) -> None:
        """Merge per-database policy changes into one audit record; list real changes only, skip no-ops."""
        keys = sorted(
            key for key in set(before) | set(after) if key != "总开关"
        )
        if "总开关" in before or "总开关" in after:
            keys.insert(0, "总开关")

        def label(value: bool) -> str:
            return "只读" if value else "可写"

        parts = [
            f"{key}：{label(before.get(key, True))} ⇒ {label(after.get(key, True))}"
            for key in keys
            if before.get(key) != after.get(key)
        ]
        if not parts:
            return
        diff = "；".join(parts)
        now = utcnow()
        row = AuditLog(
            request_id=str(uuid4()),
            event_type="policy_change",
            user_name=(admin_username or "unknown")[:64],
            datasource_name=datasource_name,
            tool="datasource_policy",
            sql_text=None,
            database_name=None,
            status="success",
            deny_reason=diff[:512],
            error_message=diff if len(diff) > 512 else None,
            client_ip=client_ip[:64] if client_ip else None,
            created_at=now,
            finished_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("策略变更审计写入失败 datasource=%s", datasource_name)

    async def record_export_auth_failure(
        self,
        request_id: str,
        account_name: str | None,
        user_name: str | None,
        client_ip: str,
        code: str,
    ) -> None:
        """Export-channel authentication failure; account name and reason go to the audit only (clients always get 1045)."""
        now = utcnow()
        row = AuditLog(
            request_id=request_id,
            event_type="mysql_auth",
            user_name=user_name or "unknown",
            credential_account=account_name or None,
            status="denied",
            deny_reason=code[:512],
            client_ip=client_ip[:64] if client_ip else None,
            created_at=now,
            finished_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort, an auth failure still returns 1045
            logger.exception("导出认证失败审计写入失败 request_id=%s", request_id)

    async def record_credential_event(
        self,
        account_name: str,
        user_name: str,
        datasource: Any | None,
        action: str,
        admin_username: str,
        client_ip: str,
    ) -> None:
        """Credential lifecycle events (issue/revoke/expire cleanup); best effort."""
        actor_provider, actor_id, actor_name, actor_trust = self._actor_fields()
        now = utcnow()
        row = AuditLog(
            request_id=str(uuid4()),
            event_type="credential_lifecycle",
            user_name=user_name,
            credential_account=account_name,
            actor_provider=actor_provider,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_trust=actor_trust,
            datasource_id=getattr(datasource, "id", None),
            datasource_name=getattr(datasource, "name", None),
            tool=action,
            status="success",
            deny_reason=(admin_username or "")[:512] or None,
            client_ip=client_ip[:64] if client_ip else None,
            created_at=now,
            finished_at=now,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("凭证事件审计写入失败 account=%s action=%s", account_name, action)

    async def query(
        self,
        filters: dict[str, Any],
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[AuditLog], int]:
        conditions = []
        if filters.get("from"):
            conditions.append(AuditLog.created_at >= filters["from"])
        if filters.get("to"):
            conditions.append(AuditLog.created_at <= filters["to"])
        if filters.get("user_name"):
            conditions.append(AuditLog.user_name == filters["user_name"])
        if filters.get("datasource_name"):
            conditions.append(AuditLog.datasource_name == filters["datasource_name"])
        if filters.get("status"):
            conditions.append(AuditLog.status == filters["status"])
        if filters.get("actor"):
            pattern = _like_pattern(filters["actor"])
            conditions.append(
                or_(
                    AuditLog.actor_id.like(pattern, escape="\\"),
                    AuditLog.actor_name.like(pattern, escape="\\"),
                )
            )
        if filters.get("q"):
            pattern = _like_pattern(filters["q"])
            conditions.append(
                or_(
                    AuditLog.sql_text.like(pattern, escape="\\"),
                    AuditLog.user_name.like(pattern, escape="\\"),
                    AuditLog.datasource_name.like(pattern, escape="\\"),
                    AuditLog.credential_account.like(pattern, escape="\\"),
                    AuditLog.actor_id.like(pattern, escape="\\"),
                    AuditLog.actor_name.like(pattern, escape="\\"),
                )
            )

        page = max(1, page)
        offset = (page - 1) * page_size
        async with self._session_factory() as session:
            total = await session.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
            rows = (
                await session.scalars(
                    select(AuditLog)
                    .where(*conditions)
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .offset(offset)
                    .limit(page_size)
                )
            ).all()
        return list(rows), int(total or 0)

    async def get(self, request_id: str) -> AuditLog | None:
        async with self._session_factory() as session:
            return await session.scalar(select(AuditLog).where(AuditLog.request_id == request_id))

    async def recent(self, limit: int = 10) -> list[AuditLog]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
                )
            ).all()
        return list(rows)

    async def today_stats(self) -> tuple[int, int]:
        local_now = datetime.now(SHANGHAI)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= start_utc)
            )
            denied = await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.created_at >= start_utc, AuditLog.status == "denied")
            )
        return int(total or 0), int(denied or 0)

    async def get_retention_days(self) -> int:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(SystemSetting.setting_value).where(SystemSetting.setting_key == "audit_retention_days")
            )
        if value is None:
            return DEFAULT_RETENTION_DAYS
        try:
            return int(value)
        except ValueError:
            logger.warning("审计保留期配置非法，回退默认值：%s", value)
            return DEFAULT_RETENTION_DAYS

    async def set_retention_days(self, days: int) -> None:
        days = validate_retention_days(days)
        async with self._session_factory() as session:
            row = await session.get(SystemSetting, "audit_retention_days")
            now = utcnow()
            if row is None:
                session.add(
                    SystemSetting(
                        setting_key="audit_retention_days",
                        setting_value=str(days),
                        updated_at=now,
                    )
                )
            else:
                row.setting_value = str(days)
                row.updated_at = now
            await session.commit()

    async def cleanup_once(self, now_utc: datetime | None = None) -> int:
        days = await self.get_retention_days()
        cutoff = (now_utc or utcnow()) - timedelta(days=days)
        deleted = 0
        while True:
            async with self._session_factory() as session:
                ids = (
                    await session.scalars(
                        select(AuditLog.id)
                        .where(AuditLog.created_at < cutoff)
                        .order_by(AuditLog.id)
                        .limit(CLEANUP_BATCH_SIZE)
                    )
                ).all()
                if not ids:
                    break
                result = await session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
                await session.commit()
            deleted += result.rowcount or 0
            if len(ids) < CLEANUP_BATCH_SIZE:
                break
        return deleted
