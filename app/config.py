"""Environment loading and validation."""

from __future__ import annotations

import base64
import binascii
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import (
    Field,
    ValidationError as PydanticValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from app.errors import ConfigError
from app.netip import describe_invalid_entries, parse_allow_networks

logger = logging.getLogger(__name__)

MASTER_KEY_BYTES = 32
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 3650
# A wildcard listen address cannot yield a client-reachable address, so auto-issue must degrade.
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]", "*"})

# Quick start needs no external database: an unconfigured management store is a local SQLite file.
DEFAULT_DB_PATH = "data/gateway.db"
_ASYNC_MYSQL_DRIVERS = frozenset({"aiomysql", "asyncmy"})
_ASYNC_SQLITE_DRIVERS = frozenset({"aiosqlite"})
_ASYNC_POSTGRES_DRIVERS = frozenset({"asyncpg", "psycopg"})


def _as_async_url(url: URL) -> URL:
    """Upgrade a plain SQLAlchemy URL to an async driver the async engine can load."""
    backend = url.get_backend_name()
    driver = url.get_driver_name()
    if backend in {"mysql", "mariadb"}:
        if driver not in _ASYNC_MYSQL_DRIVERS:
            url = url.set(drivername="mysql+aiomysql")
        if "charset" not in url.query:
            url = url.set(query={**dict(url.query), "charset": "utf8mb4"})
    elif backend == "sqlite" and driver not in _ASYNC_SQLITE_DRIVERS:
        url = url.set(drivername="sqlite+aiosqlite")
    elif backend == "postgresql":
        # get_driver_name() reports SQLAlchemy's sync default (psycopg) for a plain URL,
        # so only trust it when the driver was written explicitly.
        if "+" not in url.drivername or driver not in _ASYNC_POSTGRES_DRIVERS:
            url = url.set(drivername="postgresql+asyncpg")
    return url


class Settings(BaseSettings):
    """Gateway settings, all sourced from GATEWAY_ environment variables or .env."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    master_key: str
    admin_user: str | None = None
    admin_password: str | None = None

    # Management store. Explicit URL wins; otherwise the GATEWAY_DB_* quartet selects MySQL;
    # when neither is configured the gateway uses the SQLite file at db_path.
    db_url: str | None = None
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_password: str | None = None
    db_name: str = "db_gateway"
    db_path: str | None = None

    cookie_secure: bool = False
    # Admin sign-in brute-force guard: failures per username+client-IP inside the window
    # lock that pair for lock_seconds. State is process-local.
    admin_login_max_attempts: int = Field(default=5, ge=1, le=100)
    admin_login_window_seconds: int = Field(default=300, ge=10, le=86400)
    admin_login_lock_seconds: int = Field(default=900, ge=10, le=86400)
    # Admin session cookie slides while used; this caps how long an abandoned session survives.
    admin_session_idle_minutes: int = Field(default=30, ge=1, le=1440)
    log_level: str = "INFO"
    # text keeps the human-readable format; json emits one object per line for log shippers.
    log_format: str = "text"
    # Prometheus /metrics stays off by default so upgrading never exposes a new endpoint.
    metrics_enabled: bool = False
    audit_retention_days: int = Field(default=180, ge=RETENTION_MIN_DAYS, le=RETENTION_MAX_DAYS)
    # Refuse to start when the management store (and therefore the fail-closed audit log) is SQLite.
    audit_require_server_backend: bool = False
    host: str = "0.0.0.0"
    port: int = 8080

    # Business datasource pool: every datasource owns one engine with this sizing.
    datasource_pool_size: int = Field(default=5, ge=1, le=200)
    datasource_pool_max_overflow: int = Field(default=5, ge=0, le=200)
    # 0 disables pool recycling.
    datasource_pool_recycle_seconds: int = Field(default=3600, ge=0, le=86400)

    mysql_export_enabled: bool = False
    mysql_export_host: str = "0.0.0.0"
    mysql_export_port: int = Field(default=3307, ge=1, le=65535)
    mysql_export_allow_cidrs: str = ""
    mcp_allow_cidrs: str = ""
    # Trust X-Forwarded-For only when the TCP peer falls inside these proxy networks; empty = always use the peer address.
    trusted_proxies: str = ""
    mysql_export_max_concurrent: int = Field(default=2, ge=1, le=64)
    mysql_export_max_rows: int = Field(default=1_000_000, ge=1, le=100_000_000)
    mysql_export_max_seconds: int = Field(default=600, ge=1, le=86400)
    mysql_export_max_ttl_hours: int = Field(default=72, ge=1, le=720)
    mysql_export_fetch_batch: int = Field(default=10_000, ge=100, le=1_000_000)
    mysql_export_advertise_host: str = ""
    mysql_export_advertise_port: int = Field(default=0, ge=0, le=65535)

    mcp_language: str = "zh"
    mcp_result_max_rows: int = Field(default=2000, ge=0, le=10_000_000)
    mcp_result_max_bytes: int = Field(default=2_000_000, ge=0, le=1_000_000_000)
    # Wall-clock budget for one MCP SQL statement; 0 disables the client-side timeout.
    mcp_query_timeout_seconds: float = Field(default=30.0, ge=0, le=3600)
    # UPDATE/DELETE affecting more rows than this are rolled back; 0 disables the cap.
    mcp_write_max_affected_rows: int = Field(default=1000, ge=0, le=10_000_000)
    # Admission control for MCP tool calls: in-flight calls across the gateway and per token,
    # plus a per-token request budget per fixed 60s window. Rejections never queue.
    mcp_max_concurrent: int = Field(default=32, ge=1, le=1000)
    mcp_max_concurrent_per_token: int = Field(default=4, ge=0, le=1000)
    mcp_rate_limit_per_minute: int = Field(default=120, ge=0, le=100_000)
    mcp_export_auto_issue: bool = True
    mcp_export_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    mcp_export_max_active: int = Field(default=3, ge=1, le=64)

    exchange_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    exchange_max_ttl_minutes: int = Field(default=60, ge=1, le=1440)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, value: str) -> str:
        _networks, invalid = parse_allow_networks(value, strict=True)
        if invalid:
            raise ValueError(
                "GATEWAY_TRUSTED_PROXIES 含非法 IP 或 CIDR："
                f"{describe_invalid_entries(invalid)}"
            )
        return value

    @field_validator("mcp_language")
    @classmethod
    def _validate_mcp_language(cls, value: str) -> str:
        from app.i18n import normalize_locale

        normalized = normalize_locale(value)
        if normalized is None:
            raise ValueError("GATEWAY_MCP_LANGUAGE must be zh or en")
        return normalized

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"text", "json"}:
            raise ValueError("GATEWAY_LOG_FORMAT must be text or json")
        return normalized

    @field_validator("master_key")
    @classmethod
    def _validate_master_key(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("GATEWAY_MASTER_KEY 必须是 base64 编码的 32 字节密钥") from exc
        if len(raw) != MASTER_KEY_BYTES:
            raise ValueError("GATEWAY_MASTER_KEY 解码后必须为 32 字节")
        return value

    @field_validator("db_url", "db_host", "db_user", "db_password", "db_path", mode="before")
    @classmethod
    def _blank_db_setting_is_unset(cls, value: object) -> object:
        # compose and .env templates pass empty strings; treat them as "not configured" so the
        # SQLite default still applies.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_management_store(self) -> "Settings":
        if self.db_url:
            try:
                url = _as_async_url(make_url(self.db_url))
            except Exception as exc:  # noqa: BLE001 - any parse failure is a configuration error
                raise ConfigError(f"GATEWAY_DB_URL 不是合法的数据库 URL：{self.db_url}") from exc
            if url.get_backend_name() == "sqlite" and not url.database:
                raise ConfigError("GATEWAY_DB_URL 的 SQLite URL 缺少数据库文件路径")
            if self.db_host:
                logger.warning("已配置 GATEWAY_DB_URL，GATEWAY_DB_HOST 等连接项将被忽略")
            return self._check_audit_backend()
        if self.db_host:
            missing = [
                name
                for name, value in (
                    ("GATEWAY_DB_USER", self.db_user),
                    ("GATEWAY_DB_PASSWORD", self.db_password),
                )
                if not value
            ]
            if missing:
                raise ConfigError(
                    f"已配置 GATEWAY_DB_HOST 但缺少 {', '.join(missing)}；"
                    "如需使用默认 SQLite 管理库，请清空 GATEWAY_DB_HOST"
                )
        return self._check_audit_backend()

    def _check_audit_backend(self) -> "Settings":
        if self.audit_require_server_backend and self.management_backend == "sqlite":
            raise ConfigError(
                "GATEWAY_AUDIT_REQUIRE_SERVER_BACKEND=true 时管理库不能使用 SQLite；"
                "请配置 GATEWAY_DB_URL 或 GATEWAY_DB_HOST 指向 MySQL/PostgreSQL"
            )
        return self

    @model_validator(mode="after")
    def _validate_export(self) -> "Settings":
        networks, invalid = parse_allow_networks(self.mysql_export_allow_cidrs, strict=True)
        if invalid:
            raise ConfigError(
                "GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS 含非法 IP 或 CIDR："
                f"{describe_invalid_entries(invalid)}"
            )
        _mcp_networks, mcp_invalid = parse_allow_networks(self.mcp_allow_cidrs, strict=True)
        if mcp_invalid:
            raise ConfigError(
                f"GATEWAY_MCP_ALLOW_CIDRS 含非法 IP 或 CIDR：{describe_invalid_entries(mcp_invalid)}"
            )
        if not self.mysql_export_enabled:
            return self
        if self.mysql_export_port == self.port:
            raise ConfigError(
                "GATEWAY_MYSQL_EXPORT_PORT 不能与 GATEWAY_PORT 相同；HTTP 与 MySQL 不得共用端口"
            )
        if not networks:
            # Allowlists can now be saved from the admin settings page, so an empty env var no longer means "unset";
            # enforcement moved to connection time: no entries from either source means deny all.
            logger.warning(
                "已开启 MySQL 导出通道但 GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS 为空；"
                "管理端设置页也未保存过白名单时，所有导出连接都会被拒绝"
            )
        return self

    @model_validator(mode="after")
    def _validate_mcp_handoff(self) -> "Settings":
        if self.mysql_export_enabled and self.mcp_export_ttl_seconds > (
            self.mysql_export_max_ttl_hours * 3600
        ):
            raise ConfigError(
                "GATEWAY_MCP_EXPORT_TTL_SECONDS 不能超过 "
                "GATEWAY_MYSQL_EXPORT_MAX_TTL_HOURS * 3600；自动签发的凭证不得比手工签发的活得更久"
            )
        if self.mcp_export_auto_issue and not self.mysql_export_enabled:
            logger.warning(
                "已开启 MCP 自动签发导出凭证但导出通道未启用（GATEWAY_MYSQL_EXPORT_ENABLED=false）；"
                "结果超限时只会截断并提示，不会签发凭证"
            )
        return self

    @model_validator(mode="after")
    def _validate_exchange_ttl(self) -> "Settings":
        if self.exchange_ttl_minutes > self.exchange_max_ttl_minutes:
            raise ConfigError(
                "GATEWAY_EXCHANGE_TTL_MINUTES 不能超过 GATEWAY_EXCHANGE_MAX_TTL_MINUTES；"
                "默认有效期不得比允许的上限更久"
            )
        return self

    @property
    def export_advertise_host(self) -> str:
        """Client-reachable export address; empty means unknown, so auto-issue degrades to truncation only."""
        configured = self.mysql_export_advertise_host.strip()
        if configured:
            return configured
        if self.mysql_export_host.strip() not in WILDCARD_HOSTS:
            return self.mysql_export_host.strip()
        return ""

    @property
    def export_advertise_port(self) -> int:
        return self.mysql_export_advertise_port or self.mysql_export_port

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_key, validate=True)

    @property
    def management_url(self) -> URL:
        """Management store URL: explicit GATEWAY_DB_URL, else MySQL, else the SQLite file."""
        if self.db_url:
            return _as_async_url(make_url(self.db_url))
        if self.db_host:
            # The password goes through URL.create to avoid string concatenation.
            return URL.create(
                "mysql+aiomysql",
                username=self.db_user,
                password=self.db_password,
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
                query={"charset": "utf8mb4"},
            )
        path = Path(self.db_path or DEFAULT_DB_PATH).expanduser().resolve()
        return URL.create("sqlite+aiosqlite", database=path.as_posix())

    @property
    def management_backend(self) -> str:
        return self.management_url.get_backend_name()

    @property
    def management_summary(self) -> str:
        """Loggable backend description; never includes credentials."""
        url = self.management_url
        if url.get_backend_name() == "sqlite":
            return f"sqlite {url.database}"
        return f"{url.get_backend_name()} {url.host}:{url.port}/{url.database}"


def _format_validation_error(exc: PydanticValidationError) -> str:
    details: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "配置"
        message = error.get("msg", "非法值")
        details.append(f"{location}: {message}")
    joined = "；".join(details)
    return (
        f"配置错误：{joined}。请检查环境变量或 .env（参考 .env.example）；"
        "主密钥可用 python scripts/gen_master_key.py 生成。"
    )


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Load settings, raising a readable ConfigError on failure."""
    try:
        return Settings()
    except PydanticValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
