"""Management store connection, schema creation, and bootstrap admin."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import DDL, event, func, inspect, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn, CreateIndex

from app.audit.service import DEFAULT_RETENTION_DAYS
from app.clock import utcnow
from app.config import Settings
from app.db.models import AdminUser, Base, SystemSetting
from app.errors import ConfigError, DatabaseUnavailable
from app.security.crypto import hash_password

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
IGNORED_SCHEMA_ERRNOS = (1050, 1060, 1061)  # table / column / index already exists
SQLITE_BUSY_TIMEOUT_MS = 30_000


def create_management_engine(settings: Settings) -> AsyncEngine:
    url = settings.management_url
    if url.get_backend_name() != "sqlite":
        return create_async_engine(
            url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    database = url.database
    if database and database != ":memory:":
        # Quick start writes next to the process by default, so the parent directory must exist.
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "connect_args": {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
    }
    if not database or database == ":memory:":
        # A memory database only survives while its single connection does.
        options["poolclass"] = StaticPool
    engine = create_async_engine(url, **options)
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    logger.info("管理库使用 SQLite：%s", database or ":memory:")
    return engine


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Per-connection SQLite settings: WAL for concurrent readers, FK cascades for service deletes."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


async def wait_for_db(engine: AsyncEngine, attempts: int = 30, interval: float = 2.0) -> None:
    """Wait for the management database; raise DatabaseUnavailable after all attempts fail."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 - must catch every driver-level connection error
            last_error = exc
            logger.warning("等待管理库连接失败（%s/%s）：%s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(interval)
    raise DatabaseUnavailable(f"管理库连接失败：{last_error}") from last_error


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create or upgrade the management schema for the configured backend."""
    if engine.dialect.name == "mysql":
        # Shipped deployments keep the append-only SQL migrations as their upgrade path.
        await _apply_migrations(engine)
    else:
        await _create_schema_from_models(engine)


async def _apply_migrations(engine: AsyncEngine) -> None:
    """Run idempotent migrations in filename order; silently skip existing tables and columns."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise ConfigError(f"建表脚本不存在：{MIGRATIONS_DIR}")
    executed = 0
    async with engine.begin() as conn:
        for path in files:
            sql_text = path.read_text(encoding="utf-8")
            statements = [part.strip() for part in sql_text.split(";") if part.strip()]
            for statement in statements:
                try:
                    await conn.exec_driver_sql(statement)
                except DBAPIError as exc:
                    errno = _mysql_errno(exc)
                    if errno in IGNORED_SCHEMA_ERRNOS:
                        logger.info("迁移 %s 跳过已存在对象（errno %s）", path.name, errno)
                        continue
                    raise
                executed += 1
    logger.info("管理库表结构检查完成，共执行 %s 条 DDL/DML", executed)


def _mysql_errno(exc: DBAPIError) -> int | None:
    args = getattr(exc.orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


async def _create_schema_from_models(engine: AsyncEngine) -> None:
    """Non-MySQL backends create the final schema from the ORM models instead of MySQL DDL.

    Existing installs only need additive upgrades, so missing columns are added in place; the
    metadata is the single source of truth and keeps SQLite files working across versions.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
        added_indexes = await conn.run_sync(_add_missing_indexes)
        seeded = await _seed_retention_default(conn)
    logger.info(
        "管理库表结构检查完成（%s）：缺失列 %s，缺失索引 %s，保留期默认值%s",
        engine.dialect.name,
        ", ".join(added) or "无",
        ", ".join(added_indexes) or "无",
        "已写入" if seeded else "已存在",
    )


def _add_missing_columns(conn: Connection) -> list[str]:
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    preparer = conn.dialect.identifier_preparer
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            definition = CreateColumn(column).compile(dialect=conn.dialect)
            conn.execute(
                DDL(
                    f"ALTER TABLE {preparer.quote(table.name)} ADD COLUMN {definition}"
                )
            )
            added.append(f"{table.name}.{column.name}")
    return added


def _add_missing_indexes(conn: Connection) -> list[str]:
    """create_all only creates missing tables; indexes added to existing tables are synced here."""
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            conn.execute(CreateIndex(index))
            added.append(f"{table.name}.{index.name}")
    return added


async def _seed_retention_default(conn: AsyncConnection) -> bool:
    existing = await conn.scalar(
        select(SystemSetting.setting_key).where(
            SystemSetting.setting_key == "audit_retention_days"
        )
    )
    if existing is not None:
        return False
    await conn.execute(
        insert(SystemSetting).values(
            setting_key="audit_retention_days",
            setting_value=str(DEFAULT_RETENTION_DAYS),
            updated_at=utcnow(),
        )
    )
    return True


async def seed_admin(session: AsyncSession, settings: Settings) -> None:
    """Create the admin on first start; ignore env vars when an admin already exists."""
    existing = await session.scalar(select(func.count()).select_from(AdminUser))
    if existing:
        return
    if not settings.admin_user or not settings.admin_password:
        raise ConfigError(
            "管理库中尚无管理员，请设置 GATEWAY_ADMIN_USER 与 GATEWAY_ADMIN_PASSWORD 后重启"
        )
    username = settings.admin_user.strip()
    password = settings.admin_password
    if not 1 <= len(username) <= 64:
        raise ConfigError("GATEWAY_ADMIN_USER 长度必须为 1-64 字符")
    if len(password) < 8:
        raise ConfigError("GATEWAY_ADMIN_PASSWORD 至少 8 字符")
    now = utcnow()
    session.add(
        AdminUser(
            username=username,
            password_hash=hash_password(password),
            session_version=0,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    logger.info("已创建初始管理员：%s", username)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
