"""Admission control for the export channel: IP allowlist, active-export counting, and rejecting client KILL."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from mysql_mimic.constants import KillKind
from mysql_mimic.control import LocalControl
from mysql_mimic.errors import ErrorCode, MysqlError

from app.mysql.identity import client_ip_of, current_export_client_ip

logger = logging.getLogger(__name__)


class ExportLimiter:
    """Active export query counter; reaching the cap rejects immediately without queueing (E15/D16)."""

    def __init__(self, max_concurrent: int) -> None:
        self._max = int(max_concurrent)
        self._active = 0

    def try_acquire(self) -> bool:
        if self._active >= self._max:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1

    @property
    def active(self) -> int:
        return self._active


class ExportControl(LocalControl):
    def __init__(self, services: Any, allowlist: Any) -> None:
        super().__init__()
        self._services = services
        self._allowlist = allowlist

    async def add(self, connection: Any) -> int:
        """Reject by allowlist before authentication; fail-closed when peername is unavailable."""
        peer = connection.stream.writer.get_extra_info("peername")
        ip = peer[0] if peer else None
        if not ip or not await self._allowlist.is_allowed(str(ip)):
            logger.warning("channel=export 导出连接被 IP 白名单拒绝 ip=%s", ip)
            await self._services.audit.record_export_auth_failure(
                request_id=str(uuid4()),
                account_name="",
                user_name="unknown",
                client_ip=str(ip or ""),
                code="IP_NOT_ALLOWED",
            )
            raise PermissionError("export channel denied")
        # get_user receives no connection, so the source IP is fixed here and handed to the credential's source binding check.
        current_export_client_ip.set(str(ip))
        return await super().add(connection)

    async def remove(self, connection_id: int) -> None:
        connection = self._connections.get(connection_id)
        session = getattr(connection, "session", None)
        limiter = getattr(self._services, "limiter", None)
        if (
            limiter is not None
            and session is not None
            and getattr(session, "holding_quota", False)
        ):
            limiter.release()
            session.holding_quota = False
        await super().remove(connection_id)

    async def kill(
        self, connection_id: int, kind: KillKind = KillKind.CONNECTION
    ) -> None:
        """Always reject: LocalControl kills connections by id unconditionally, which would let export accounts kill each other."""
        connection = self._connections.get(connection_id)
        session = getattr(connection, "session", None) if connection else None
        await self._services.audit.record_denied(
            request_id=str(uuid4()),
            event_type="mysql_export",
            principal=getattr(session, "principal", None),
            datasource=getattr(session, "datasource", None),
            tool="kill",
            sql_text=None,
            client_ip=client_ip_of(connection) if connection else "",
            code="KILL_DENIED",
            message="KILL is not allowed on the export channel",
            credential_account=getattr(session, "credential_account", None),
        )
        raise MysqlError(
            "KILL is not allowed on the export channel", ErrorCode.NOT_SUPPORTED_YET
        )
