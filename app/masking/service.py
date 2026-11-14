"""Persistence CRUD for manual masking rules."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.db.models import DataSource, DataSourceDatabase, MaskingRule
from app.errors import NotFound, ValidationError
from app.i18n import t, translate_label
from app.masking.rules import ManualRule, validate_rule

FIELD_LIMIT = 128


def _to_rule(row: MaskingRule) -> ManualRule:
    return ManualRule(
        id=row.id,
        datasource_id=row.datasource_id,
        database=row.database_name,
        table=row.table_name,
        column=row.column_name,
        strategy=row.strategy,
        pattern=row.mask_pattern,
    )


def _validate_text(label: str, value: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > FIELD_LIMIT:
        raise ValidationError(
            t("error.field.required_spaced", field=translate_label(label), limit=FIELD_LIMIT)
        )
    return text


class MaskingService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_rules(self, datasource_id: int) -> list[ManualRule]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(MaskingRule)
                    .where(MaskingRule.datasource_id == datasource_id)
                    .order_by(MaskingRule.database_name, MaskingRule.table_name, MaskingRule.column_name)
                )
            ).all()
        return [_to_rule(row) for row in rows]

    async def add_rule(
        self,
        datasource_id: int,
        database: str,
        table: str,
        column: str,
        strategy: str,
        pattern: str | None,
    ) -> ManualRule:
        database = _validate_text("数据库名", database)
        table = _validate_text("表名", table)
        column = _validate_text("列名", column)
        rule = validate_rule(strategy, pattern or None)

        async with self._session_factory() as session:
            datasource = await session.get(DataSource, datasource_id)
            if datasource is None:
                raise NotFound("数据源不存在")
            exposed = set(
                (
                    await session.scalars(
                        select(DataSourceDatabase.name).where(
                            DataSourceDatabase.datasource_id == datasource_id
                        )
                    )
                ).all()
            )
            if not exposed:
                # Legacy datasources without a database list fall back to the default database, same as list_databases.
                exposed = {datasource.default_database}
            if database not in exposed:
                raise ValidationError(
                    t("error.masking.database_outside", database=database)
                )
            duplicate = await session.scalar(
                select(MaskingRule.id).where(
                    MaskingRule.datasource_id == datasource_id,
                    MaskingRule.database_name == database,
                    MaskingRule.table_name == table,
                    MaskingRule.column_name == column,
                )
            )
            if duplicate is not None:
                raise ValidationError("该列已存在脱敏规则")
            now = utcnow()
            row = MaskingRule(
                datasource_id=datasource_id,
                database_name=database,
                table_name=table,
                column_name=column,
                strategy=rule.strategy,
                mask_pattern=rule.pattern,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValidationError("该列已存在脱敏规则") from exc
            await session.refresh(row)
            return _to_rule(row)

    async def delete_rule(self, datasource_id: int, rule_id: int) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(MaskingRule).where(
                    MaskingRule.id == rule_id,
                    MaskingRule.datasource_id == datasource_id,
                )
            )
            if result.rowcount == 0:
                await session.rollback()
                raise NotFound("脱敏规则不存在")
            await session.commit()
