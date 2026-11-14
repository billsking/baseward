"""Service container: management database, pools, and business services."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.config import Settings
from app.credentials.service import CredentialService
from app.datasources.export_pool import ExportPool
from app.datasources.pool import DataSourcePool
from app.datasources.service import DataSourceService
from app.db.schema import create_management_engine, create_session_factory
from app.exchange.service import ShortCredentialService
from app.masking.service import MaskingService
from app.netallow import EXPORT_ALLOW_KEY, MCP_ALLOW_KEY, IpAllowlist
from app.netip import parse_allow_networks
from app.security.crypto import derive_key
from app.tokens.service import TokenService


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_management_engine(settings)
        self.session_factory: async_sessionmaker[AsyncSession] = create_session_factory(self.engine)
        self.pool = DataSourcePool(settings)
        self.export_pool = ExportPool(settings)
        self.audit = AuditService(self.session_factory)
        self.datasources = DataSourceService(
            self.session_factory, settings, self.pool, self.export_pool
        )
        self.tokens = TokenService(self.session_factory)
        self.masking = MaskingService(self.session_factory)
        self.credentials = CredentialService(
            self.session_factory, settings, self.tokens, self.datasources, self.audit
        )
        self.short_credentials = ShortCredentialService(
            self.session_factory, settings, self.audit
        )
        self.export_allowlist = IpAllowlist(
            self.session_factory,
            EXPORT_ALLOW_KEY,
            "MySQL 导出通道",
            settings.mysql_export_allow_cidrs,
        )
        # Never restrict sources when the MCP allowlist was never configured; otherwise upgrading locks out every existing caller.
        self.mcp_allowlist = IpAllowlist(
            self.session_factory,
            MCP_ALLOW_KEY,
            "MCP 入口",
            settings.mcp_allow_cidrs,
            allow_when_unset=True,
        )
        self.mask_key = derive_key(settings.master_key_bytes, b"dbgw-mask")
        self.session_key = derive_key(settings.master_key_bytes, b"dbgw-session")
        self.trusted_proxies = parse_allow_networks(settings.trusted_proxies, strict=True)[0]
        self._export_context: Any = None
        self._export_limiter: Any = None

    # ---------- Export channel (app.mysql.* is imported only when enabled) ----------

    @property
    def export_context(self) -> Any:
        if self._export_context is None:
            from app.mysql.identity import ExportServerContext

            self._export_context = ExportServerContext()
        return self._export_context

    @property
    def limiter(self) -> Any:
        if self._export_limiter is None:
            from app.mysql.control import ExportLimiter

            self._export_limiter = ExportLimiter(
                self.settings.mysql_export_max_concurrent
            )
        return self._export_limiter

    def make_export_session(self) -> Any:
        """Export session factory: handshake context and quota are owned by the Services singleton."""
        from app.mysql.session import ExportSession

        return ExportSession(self, self.settings, self.export_context, self.limiter)

    async def close(self) -> None:
        await self.pool.close_all()
        await self.export_pool.close_all()
        await self.engine.dispose()
