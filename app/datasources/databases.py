"""Single source of truth for exposed databases and per-database read/write policy.

MCP's ``databases`` field, issue-page candidates, database lists in error messages, and admin
summaries must all call this module; never assemble "exposed ∩ authorized" elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clock import utcnow
from app.db.models import (
    ApiToken,
    DataSource,
    DataSourceDatabase,
    ExportCredential,
    TokenScopeRow,
)
from app.errors import DataSourceConnectionError, ValidationError
from app.i18n import t
from app.security.crypto import decrypt_secret
from app.tokens.scope import TokenScope

DATABASE_LIMIT = 128
# TiDB and OceanBase add their own system schemas and report some names in uppercase,
# so every consumer matches case-insensitively against this lowercase set.
SYSTEM_DATABASES = frozenset(
    {
        "mysql",
        "information_schema",
        "performance_schema",
        "sys",
        "metrics_schema",
        "oceanbase",
    }
)
SYSTEM_DATABASES_SQL = ",".join(f"'{name}'" for name in sorted(SYSTEM_DATABASES))
UPSTREAM_CONNECT_TIMEOUT = 5
UPSTREAM_QUERY_TIMEOUT = 10

logger = logging.getLogger(__name__)


def exclude_system_schemas(column: str) -> str:
    """Build the system-schema exclusion predicate shared by every information_schema read."""
    return f"LOWER({column}) NOT IN ({SYSTEM_DATABASES_SQL})"


@dataclass(frozen=True)
class DatabasePolicy:
    name: str
    is_read_only: bool


async def list_exposed(
    session_factory: async_sessionmaker[AsyncSession], datasource_id: int
) -> list[str]:
    """Return the exposed database names of this datasource, sorted."""
    async with session_factory() as session:
        result = await session.execute(
            select(DataSourceDatabase.name)
            .where(DataSourceDatabase.datasource_id == datasource_id)
            .order_by(DataSourceDatabase.name.asc())
        )
        return [row[0] for row in result.all()]


async def load_policies(
    session_factory: async_sessionmaker[AsyncSession], datasource_id: int
) -> list[DatabasePolicy]:
    """Load per-database policy, merge the datasource master switch, and return the effective policy."""
    async with session_factory() as session:
        result = await session.execute(
            select(
                DataSourceDatabase.name,
                DataSourceDatabase.is_read_only,
                DataSource.is_read_only,
            )
            .join(DataSource, DataSource.id == DataSourceDatabase.datasource_id)
            .where(DataSourceDatabase.datasource_id == datasource_id)
            .order_by(DataSourceDatabase.name.asc())
        )
        return [
            DatabasePolicy(name=row[0], is_read_only=bool(row[2]) or bool(row[1]))
            for row in result.all()
        ]


async def load_stored_read_only(
    session_factory: async_sessionmaker[AsyncSession], datasource_id: int
) -> dict[str, bool]:
    """Stored per-database value (master switch not merged), so the policy page can show "read-only (master)"."""
    async with session_factory() as session:
        result = await session.execute(
            select(DataSourceDatabase.name, DataSourceDatabase.is_read_only)
            .where(DataSourceDatabase.datasource_id == datasource_id)
            .order_by(DataSourceDatabase.name.asc())
        )
        return {row[0]: bool(row[1]) for row in result.all()}


def effective_read_only(
    policies: Iterable[DatabasePolicy],
) -> Callable[[str], bool]:
    """Compile into the policy-engine callback; unregistered databases are read-only (fail-closed)."""
    lookup = {policy.name: policy.is_read_only for policy in policies}
    return lambda database: lookup.get(database, True)


async def exposed_and_policies(
    session_factory: async_sessionmaker[AsyncSession] | None, datasource
) -> tuple[frozenset[str], list[DatabasePolicy]]:
    """Exposed databases and their policies; legacy datasources without a stored list fall back to the default database."""
    if session_factory is None:
        exposed, policies = frozenset(), []
    else:
        exposed = frozenset(await list_exposed(session_factory, datasource.id))
        policies = await load_policies(session_factory, datasource.id)
    if not policies:
        default = datasource.default_database
        exposed = frozenset({default})
        policies = [DatabasePolicy(default, bool(datasource.is_read_only))]
    return exposed, policies


def available_databases(
    exposed: Sequence[str],
    policies: Sequence[DatabasePolicy],
    scope: TokenScope,
    datasource_id: int,
) -> list[DatabasePolicy]:
    """Usable databases = exposed ∩ authorized; this is the only implementation."""
    exposed_set = set(exposed)
    return [
        policy
        for policy in policies
        if policy.name in exposed_set
        and scope.allows_database(datasource_id, policy.name)
    ]


async def sync_on_save(
    session_factory: async_sessionmaker[AsyncSession],
    datasource_id: int,
    names: Iterable[str],
    default_database: str,
) -> None:
    """Add / keep / delete database rows from the submitted set; new databases are read-only."""
    target = list(dict.fromkeys(name for name in names if name))
    if default_database not in target:
        raise ValidationError("默认库必须在可访问库内")
    target_set = set(target)
    now = utcnow()
    async with session_factory() as session:
        result = await session.execute(
            select(DataSourceDatabase).where(
                DataSourceDatabase.datasource_id == datasource_id
            )
        )
        existing = {row.name: row for row in result.scalars().all()}
        for name in target_set - set(existing):
            session.add(
                DataSourceDatabase(
                    datasource_id=datasource_id,
                    name=name,
                    is_read_only=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        for name, row in existing.items():
            if name not in target_set:
                await session.delete(row)
        await session.commit()


def _scope_touches(
    scope: TokenScope, datasource_id: int, removed: set[str]
) -> bool:
    unrestricted = getattr(scope, "unrestricted_databases", None) or ()
    if any(
        ds_id == datasource_id and database in removed
        for ds_id, database in unrestricted
    ):
        return True
    if any(
        item.datasource_id == datasource_id and item.database in removed
        for item in scope.tables
    ):
        return True
    return any(
        item.datasource_id == datasource_id and item.database in removed
        for item in scope.columns
    )


async def narrowing_impact(
    session_factory: async_sessionmaker[AsyncSession],
    datasource_id: int,
    removed: Iterable[str],
) -> dict[str, int]:
    """Measure the impact of removing databases: affected tokens / active credentials / deleted policies."""
    removed_set = set(removed)
    if not removed_set:
        return {"tokens": 0, "credentials": 0, "databases": 0}

    async with session_factory() as session:
        scope_rows = (
            await session.execute(
                select(TokenScopeRow.token_id, TokenScopeRow.scope_json)
            )
        ).all()
        tokens = 0
        for _token_id, raw in scope_rows:
            try:
                scope = TokenScope.from_json(raw)
            except ValueError:
                continue
            if _scope_touches(scope, datasource_id, removed_set):
                tokens += 1

        credential_rows = (
            await session.execute(
                select(ExportCredential.id)
                .join(ApiToken, ApiToken.id == ExportCredential.token_id)
                .join(DataSource, DataSource.id == ExportCredential.datasource_id)
                .where(
                    ExportCredential.datasource_id == datasource_id,
                    ExportCredential.default_database.in_(list(removed_set)),
                    ExportCredential.revoked_at.is_(None),
                    ExportCredential.expires_at > utcnow(),
                    ApiToken.enabled == 1,
                    DataSource.enabled == 1,
                )
            )
        ).all()

    return {
        "tokens": tokens,
        "credentials": len(credential_rows),
        "databases": len(removed_set),
    }


def _errno_of(exc: BaseException) -> int | None:
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


async def list_upstream_databases(
    datasource, *, master_key: bytes | None = None
) -> list[str]:
    """Read information_schema.schemata from upstream, excluding system databases."""
    password = getattr(datasource, "password", None)
    if password is None:
        cipher = getattr(datasource, "password_cipher", None)
        if not isinstance(cipher, str) or master_key is None:
            raise DataSourceConnectionError("连接失败，请检查地址、账号与网络")
        password = decrypt_secret(cipher, master_key)

    url = URL.create(
        "mysql+aiomysql",
        username=datasource.username,
        password=password,
        host=datasource.host,
        port=int(datasource.port),
        database=datasource.default_database,
        query={"charset": "utf8mb4"},
    )
    engine = create_async_engine(
        url, connect_args={"connect_timeout": UPSTREAM_CONNECT_TIMEOUT}
    )
    try:
        async with asyncio.timeout(UPSTREAM_QUERY_TIMEOUT):
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT SCHEMA_NAME FROM information_schema.schemata")
                )
                names = [row[0] for row in result.all()]
    except Exception as exc:  # noqa: BLE001 - normalized into a business exception
        errno = _errno_of(exc)
        detail = f"（错误码 {errno}）" if errno is not None else ""
        logger.warning(
            "读取上游库清单失败 %s:%s%s", datasource.host, datasource.port, detail
        )
        raise DataSourceConnectionError(
            t(
                "error.datasource.list_failed",
                detail=t("error.datasource.error_code", errno=errno) if errno is not None else "",
            )
        ) from exc
    finally:
        await engine.dispose()
    return sorted(name for name in names if name.lower() not in SYSTEM_DATABASES)


def use_statement(database: str) -> str:
    """Build a USE statement; the only place where a database name may be concatenated into SQL."""
    if not isinstance(database, str) or not database:
        raise ValidationError("目标库名不合法")
    if len(database) > DATABASE_LIMIT or "`" in database or "\x00" in database:
        raise ValidationError("目标库名不合法")
    return f"USE `{database}`"


def read_only_summary(policies: Iterable[DatabasePolicy]) -> str:
    writable = sum(1 for policy in policies if not policy.is_read_only)
    if not writable:
        return t("datasource.summary_all_readonly")
    return t("datasource.summary_writable", n=writable)
