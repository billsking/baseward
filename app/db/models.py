"""Management ORM models, kept in sync with migrations/001_init.sql."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# SQLite only auto-increments INTEGER PRIMARY KEY, and BIGINT is not treated as one.
AutoId = BigInteger().with_variant(Integer, "sqlite")


class AdminUser(Base):
    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DataSource(Base):
    __tablename__ = "datasource"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=3306)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    default_database: Mapped[str] = mapped_column(String(128), nullable=False)
    is_read_only: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DataSourceDatabase(Base):
    __tablename__ = "datasource_database"
    __table_args__ = (
        UniqueConstraint("datasource_id", "name", name="uq_datasource_database"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_read_only: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MaskingRule(Base):
    __tablename__ = "masking_rule"
    __table_args__ = (
        UniqueConstraint(
            "datasource_id", "database_name", "table_name", "column_name",
            name="uq_masking_rule",
        ),
        Index("idx_masking_ds", "datasource_id"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False
    )
    database_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy: Mapped[str] = mapped_column(String(16), nullable=False)
    mask_pattern: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ApiToken(Base):
    __tablename__ = "api_token"
    __table_args__ = (Index("idx_token_user", "user_name", "enabled"),)

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    user_name: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="ignore")
    token_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="access")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TokenScopeRow(Base):
    __tablename__ = "token_scope"

    token_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_token.id", ondelete="CASCADE"), primary_key=True
    )
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_created", "created_at", "id"),
        Index("idx_audit_user", "user_name", "created_at"),
        Index("idx_audit_ds", "datasource_name", "created_at"),
        Index("idx_audit_status", "status", "created_at"),
        Index("idx_audit_actor", "actor_provider", "actor_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_name: Mapped[str] = mapped_column(String(64), nullable=False)
    token_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credential_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_trust: Mapped[str | None] = mapped_column(String(16), nullable=True)
    datasource_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    datasource_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    deny_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_setting"

    setting_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Keys such as the source-IP allowlist must hold a whole CIDR list; a missing row means "never saved",
    # while an existing but empty row means "configured to deny all". The two differ, so a seed row cannot represent both.
    setting_value: Mapped[str] = mapped_column(String(1024), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExportCredential(Base):
    __tablename__ = "export_credential"
    __table_args__ = (
        Index("idx_export_token", "token_id"),
        Index("idx_export_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    account_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    auth_string: Mapped[str] = mapped_column(String(40), nullable=False)
    token_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_token.id", ondelete="CASCADE"), nullable=False
    )
    datasource_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False
    )
    default_database: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(64), nullable=False)
    # Bound source IP; NULL is a legacy credential from before the migration and is not source-checked.
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ShortCredential(Base):
    """Short-lived MCP credential exchanged from an exchange-kind token and bound to one actor."""

    __tablename__ = "short_credential"
    __table_args__ = (
        Index("idx_short_parent", "parent_token_id"),
        Index("idx_short_expires", "expires_at"),
        Index("idx_short_actor", "actor_provider", "actor_id"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    credential_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    credential_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_token_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_token.id", ondelete="CASCADE"), nullable=False
    )
    actor_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Snapshot of the parent token's scope at issue time; NULL means unrestricted, like token_scope.
    scope_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

