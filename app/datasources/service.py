"""Business rules for datasource configuration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.datasources import databases as databases_module
from app.datasources.export_pool import ExportPool
from app.datasources.pool import DataSourcePool, test_connection
from app.db.models import DataSource, DataSourceDatabase
from app.errors import NotFound, ValidationError
from app.i18n import t, translate_label
from app.security.crypto import decrypt_secret, encrypt_secret

NAME_LIMIT = 64
HOST_LIMIT = 255
USERNAME_LIMIT = 128
DATABASE_LIMIT = 128
REMARK_LIMIT = 255


def _text(label: str, value: str, limit: int) -> str:
    text = (value or "").strip()
    if not text or len(text) > limit:
        raise ValidationError(
            t("error.field.required", field=translate_label(label), limit=limit)
        )
    return text


def _validate_port(port: int) -> int:
    if not 1 <= int(port) <= 65535:
        raise ValidationError("端口必须在 1-65535 之间")
    return int(port)


def _normalize_databases(
    values: Iterable[str], default_database: str
) -> list[str]:
    """Validate and normalize the exposed database list: non-empty, each ≤128 chars, no duplicates, includes the default database."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = (raw or "").strip()
        if not name:
            continue
        if len(name) > DATABASE_LIMIT:
            raise ValidationError(
                t(
                    "error.datasource.database_name_too_long",
                    name=name[:32],
                    limit=DATABASE_LIMIT,
                )
            )
        if name in seen:
            raise ValidationError(
                t("error.datasource.duplicate_database", name=name)
            )
        seen.add(name)
        names.append(name)
    if not names:
        raise ValidationError("至少选择一个可访问库")
    if default_database not in seen:
        raise ValidationError("默认库必须在可访问库内")
    return names


class DataSourceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        pool: DataSourcePool,
        export_pool: ExportPool,
    ) -> None:
        self._session_factory = session_factory
        self._master_key = settings.master_key_bytes
        self._pool = pool
        self._export_pool = export_pool

    async def list_all(self) -> list[DataSource]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(select(DataSource).order_by(DataSource.name.asc()))
            ).all()
        return list(rows)

    async def get(self, datasource_id: int) -> DataSource:
        async with self._session_factory() as session:
            row = await session.get(DataSource, datasource_id)
        if row is None:
            raise NotFound("数据源不存在")
        return row

    async def get_by_name(self, name: str) -> DataSource | None:
        async with self._session_factory() as session:
            return await session.scalar(select(DataSource).where(DataSource.name == name))

    async def counts(self) -> tuple[int, int]:
        async with self._session_factory() as session:
            total = await session.scalar(select(func.count()).select_from(DataSource))
            enabled = await session.scalar(
                select(func.count()).select_from(DataSource).where(DataSource.enabled == 1)
            )
        return int(total or 0), int(enabled or 0)

    async def create(
        self,
        name: str,
        host: str,
        port: int,
        username: str,
        password: str,
        default_database: str,
        remark: str | None = None,
        databases: Sequence[str] | None = None,
    ) -> DataSource:
        name = _text("名称", name, NAME_LIMIT)
        host = _text("主机", host, HOST_LIMIT)
        port = _validate_port(port)
        username = _text("账号", username, USERNAME_LIMIT)
        default_database = _text("默认库", default_database, DATABASE_LIMIT)
        database_names = (
            _normalize_databases(databases, default_database)
            if databases is not None
            else None
        )
        password = password or ""
        if not password:
            raise ValidationError("密码不能为空")
        remark = (remark or "").strip()[:REMARK_LIMIT] or None

        if await self.get_by_name(name) is not None:
            raise ValidationError("数据源名称已存在")
        await test_connection(host, port, username, password, default_database)

        now = utcnow()
        row = DataSource(
            name=name,
            host=host,
            port=port,
            username=username,
            password_cipher=encrypt_secret(password, self._master_key),
            default_database=default_database,
            is_read_only=1,
            enabled=1,
            remark=remark,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValidationError("数据源名称已存在") from exc
            await session.refresh(row)

        if database_names is not None:
            await databases_module.sync_on_save(
                self._session_factory, row.id, database_names, default_database
            )
        return row

    async def update(
        self,
        datasource_id: int,
        name: str,
        host: str,
        port: int,
        username: str,
        password: str,
        default_database: str,
        remark: str | None = None,
        databases: Sequence[str] | None = None,
    ) -> DataSource:
        name = _text("名称", name, NAME_LIMIT)
        host = _text("主机", host, HOST_LIMIT)
        port = _validate_port(port)
        username = _text("账号", username, USERNAME_LIMIT)
        default_database = _text("默认库", default_database, DATABASE_LIMIT)
        if databases is not None:
            database_names = _normalize_databases(databases, default_database)
        else:
            database_names = None
            existing = await databases_module.list_exposed(
                self._session_factory, datasource_id
            )
            if existing and default_database not in existing:
                raise ValidationError("默认库必须在可访问库内")
        remark = (remark or "").strip()[:REMARK_LIMIT] or None

        async with self._session_factory() as session:
            row = await session.get(DataSource, datasource_id)
            if row is None:
                raise NotFound("数据源不存在")
            duplicate = await session.scalar(
                select(DataSource.id).where(DataSource.name == name, DataSource.id != datasource_id)
            )
            if duplicate is not None:
                raise ValidationError("数据源名称已存在")

            new_password = password or decrypt_secret(row.password_cipher, self._master_key)
            await test_connection(host, port, username, new_password, default_database)

            row.name = name
            row.host = host
            row.port = port
            row.username = username
            row.default_database = default_database
            row.remark = remark
            if password:
                row.password_cipher = encrypt_secret(password, self._master_key)
            row.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValidationError("数据源名称已存在") from exc
            await session.refresh(row)

        if database_names is not None:
            await databases_module.sync_on_save(
                self._session_factory, datasource_id, database_names, default_database
            )
        await self._pool.invalidate(datasource_id)
        await self._export_pool.invalidate(datasource_id)
        return row

    async def delete(self, datasource_id: int) -> None:
        async with self._session_factory() as session:
            row = await session.get(DataSource, datasource_id)
            if row is None:
                raise NotFound("数据源不存在")
            await session.delete(row)
            await session.commit()
        await self._pool.invalidate(datasource_id)
        await self._export_pool.invalidate(datasource_id)

    async def set_read_only(self, datasource_id: int, is_read_only: bool) -> bool:
        """Master forced-read-only switch; returns whether it actually changed."""
        async with self._session_factory() as session:
            row = await session.get(DataSource, datasource_id)
            if row is None:
                raise NotFound("数据源不存在")
            value = 1 if is_read_only else 0
            if row.is_read_only == value:
                return False
            row.is_read_only = value
            row.updated_at = utcnow()
            await session.commit()
            return True

    async def set_database_read_only(
        self, datasource_id: int, database: str, is_read_only: bool
    ) -> bool:
        """Per-database policy; no connection test, no pool invalidation (evaluated at execution). Returns whether it changed."""
        name = (database or "").strip()
        value = 1 if is_read_only else 0
        async with self._session_factory() as session:
            row = await session.get(DataSource, datasource_id)
            if row is None:
                raise NotFound("数据源不存在")
            target = await session.scalar(
                select(DataSourceDatabase).where(
                    DataSourceDatabase.datasource_id == datasource_id,
                    DataSourceDatabase.name == name,
                )
            )
            if target is None:
                has_rows = await session.scalar(
                    select(DataSourceDatabase.id)
                    .where(DataSourceDatabase.datasource_id == datasource_id)
                    .limit(1)
                )
                if has_rows is not None or name != row.default_database:
                    raise NotFound("库未暴露或不存在")
                # AC46 compatibility: datasources without a stored database list fall back to the default database + master switch;
                # rows are persisted only on the first explicit per-database configuration.
                if value == row.is_read_only:
                    return False
                now = utcnow()
                session.add(
                    DataSourceDatabase(
                        datasource_id=datasource_id,
                        name=name,
                        is_read_only=value,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.commit()
                return True
            if target.is_read_only == value:
                return False
            target.is_read_only = value
            target.updated_at = utcnow()
            await session.commit()
            return True

    async def stored_database_read_only(
        self, datasource_id: int
    ) -> dict[str, bool]:
        """Stored per-database value (master switch not merged); the policy page uses it to render "read-only (master)"."""
        return await databases_module.load_stored_read_only(
            self._session_factory, datasource_id
        )

    async def set_enabled(self, datasource_id: int, enabled: bool) -> None:
        async with self._session_factory() as session:
            row = await session.get(DataSource, datasource_id)
            if row is None:
                raise NotFound("数据源不存在")
            row.enabled = 1 if enabled else 0
            row.updated_at = utcnow()
            await session.commit()

    async def test_saved(self, datasource_id: int) -> int:
        row = await self.get(datasource_id)
        password = decrypt_secret(row.password_cipher, self._master_key)
        return await test_connection(
            row.host,
            row.port,
            row.username,
            password,
            row.default_database,
        )

    async def list_databases(
        self, datasource_id: int
    ) -> list[databases_module.DatabasePolicy]:
        """Exposed databases plus each database's effective policy (master switch merged)."""
        row = await self.get(datasource_id)
        policies = await databases_module.load_policies(
            self._session_factory, datasource_id
        )
        if policies:
            return policies
        # AC46 compatibility: datasources without an explicit database list fall back to the default database, matching v1.7.
        return [
            databases_module.DatabasePolicy(
                row.default_database, bool(row.is_read_only)
            )
        ]

    async def narrowing_impact(
        self, datasource_id: int, removed: Iterable[str]
    ) -> dict[str, int]:
        return await databases_module.narrowing_impact(
            self._session_factory, datasource_id, removed
        )

    async def refresh_upstream_databases(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        default_database: str,
    ) -> list[str]:
        """Read the upstream database list with unsaved form connection parameters; read-only, nothing persisted."""
        target = SimpleNamespace(
            host=host,
            port=port,
            username=username,
            password=password,
            default_database=default_database,
        )
        return await databases_module.list_upstream_databases(target)

    async def refresh_saved_databases(self, datasource_id: int) -> list[str]:
        row = await self.get(datasource_id)
        return await databases_module.list_upstream_databases(
            row, master_key=self._master_key
        )
