"""Two protocol-layer patches for mysql-mimic 3.0.5 (D29 / AC25, D-07 / AC23).

`Connection.handle_stmt_fetch` uses `async for` to pull one packet header from the generator
before checking the quota; the row fetched on the over-quota iteration is discarded while the
generator has already advanced — every full batch silently drops one row (a 1,000-row export
measured 991 rows). Dropped rows have no visible symptom in exports, so the version assertion
must fail hard.

`Connection.handle_field_list` joins multiple column definitions with `b"".join` and writes them
at once, while `MysqlStream.write` treats the whole block as **one** packet: a client parsing
packets column by column only sees the first column. Writing column by column matches real
MySQL, which sends one packet per field definition.
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

from mysql_mimic import packets, types
from mysql_mimic.connection import Connection
from mysql_mimic.schema import com_field_list_to_show_statement
from mysql_mimic.utils import aiterate, cooperative_iterate

_MYSQL_MIMIC_PINNED = "3.0.5"
_installed = _pkg_version("mysql-mimic")
if _installed != _MYSQL_MIMIC_PINNED:
    raise RuntimeError(
        f"mysql-mimic {_installed} 与协议层补丁不匹配（钉死 "
        f"{_MYSQL_MIMIC_PINNED}），请重新核对该版本的分批取行与 COM_FIELD_LIST 实现后再升级"
    )


async def handle_stmt_fetch(self: Connection, data: bytes) -> None:
    com_stmt_fetch = packets.parse_handle_stmt_fetch(data)

    stmt = self.get_stmt(com_stmt_fetch.stmt_id)
    assert stmt.cursor is not None
    # Rebuilding the wrapper on every FETCH leaves the underlying iterator where it stopped (the second batch measured LAST_ROW_SENT),
    # so reuse one iterator per cursor object; a new EXECUTE with a new cursor naturally rebuilds it.
    cached = getattr(stmt, "_fetch_iterator", None)
    if cached is None or cached[0] is not stmt.cursor:
        cached = (stmt.cursor, cooperative_iterate(stmt.cursor).__aiter__())
        stmt._fetch_iterator = cached  # type: ignore[attr-defined]

    agen = cached[1]
    count = 0
    exhausted = False

    while count < com_stmt_fetch.num_rows:
        try:
            packet = await agen.__anext__()
        except StopAsyncIteration:
            exhausted = True
            break
        await self.stream.write(packet, drain=False)
        count += 1
    await self.stream.drain()

    await self.stream.write(
        self.ok_or_eof(
            flags=(
                types.ServerStatus.SERVER_STATUS_LAST_ROW_SENT
                if exhausted
                else types.ServerStatus.SERVER_STATUS_CURSOR_EXISTS
            )
        )
    )


Connection.handle_stmt_fetch = handle_stmt_fetch  # type: ignore[method-assign]


async def handle_field_list(self: Connection, data: bytes) -> None:
    com_field_list = packets.parse_com_field_list(self.client_charset, data)
    sql = com_field_list_to_show_statement(com_field_list)
    result = await self.query(sql=sql, query_attrs={})
    async for row in aiterate(result.rows):
        await self.stream.write(
            packets.make_column_definition_41(
                server_charset=self.server_charset,
                table=com_field_list.table,
                name=row[0],
                is_com_field_list=True,
                default=row[4],
            )
        )
    await self.stream.write(self.ok_or_eof())


Connection.handle_field_list = handle_field_list  # type: ignore[method-assign]
