"""Listener for the MySQL-protocol export channel.

mysql-mimic 3.0.5's `MysqlServer._client_connected_cb` only writes an error packet and
`return`s when `control.add()` raises (including an IP-allowlist rejection): it neither closes
the writer nor registers the connection, so asyncio's active-connection count never drops,
rejected connections keep the socket open, and `wait_closed()` hangs forever when the listener
shuts down. `ExportMysqlServer` closes the writer after the callback returns.
"""

from __future__ import annotations

from typing import Any

from mysql_mimic import MysqlServer

from app.mysql import connection as _connection_patches  # noqa: F401 - importing applies the protocol patches (D29 / D-07)
from app.mysql.control import ExportControl
from app.mysql.identity import CredentialIdentityProvider


class ExportMysqlServer(MysqlServer):
    async def _client_connected_cb(self, reader: Any, writer: Any) -> None:
        try:
            await super()._client_connected_cb(reader, writer)
        finally:
            if not writer.is_closing():
                writer.close()


def build_export_server(services: Any) -> MysqlServer:
    settings = services.settings
    return ExportMysqlServer(
        session_factory=services.make_export_session,
        control=ExportControl(services, services.export_allowlist),
        identity_provider=CredentialIdentityProvider(
            services, settings, services.export_context
        ),
        host=settings.mysql_export_host,
        port=settings.mysql_export_port,
    )


async def start(server: MysqlServer) -> None:
    await server.start_server()


async def stop(server: MysqlServer) -> None:
    server.close()
    await server.wait_closed()
