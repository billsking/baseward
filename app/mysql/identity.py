"""Export-channel identity resolution: MySQL handshake -> temporary credential -> Principal."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from mysql_mimic import context as mimic_context
from mysql_mimic.auth import IdentityProvider, NativePasswordAuthPlugin, User

from app.config import Settings
from app.credentials.service import (
    CREDENTIAL_IP_MISMATCH,
    CREDENTIAL_PASSWORD_INVALID,
    CredentialAuthError,
)
from app.errors import NotFound
from app.netip import ip_matches

logger = logging.getLogger(__name__)

# Source IP of this connection: written by ExportControl.add() before authentication.
# get_user only receives the username, not the connection, so the source has to come from here;
# the value is set before connection.start() creates the task, so handshake and command subtasks read the same value.
current_export_client_ip: ContextVar[str] = ContextVar("current_export_client_ip", default="")

# Placeholder auth_string returned on the deny path: SHA1(SHA1(password)) of all zeros cannot be forged.
# It makes the library fail as Forbidden -> 1045 instead of 3162 when get_user returns None (see D-06).
DENIED_AUTH_STRING = "0" * 40


@dataclass(frozen=True)
class ExportConnectionContext:
    """Credential context of a successful handshake, consumed when constructing the session."""

    credential: Any
    principal: Any
    datasource: Any
    client_ip: str


def current_connection_id() -> int | None:
    """get_user receives no connection, so key by the connection id the library writes at the start of each connection task."""
    return mimic_context.connection_id.get(None)


class ExportServerContext:
    """Handshake-time context keyed by connection id; cleared when the session closes.

    Concurrent connections with the same credential each keep their own entry, so they cannot
    overwrite or clear each other. The context itself is not held in a ContextVar: session
    initialization sits between the handshake and command phases, while the connection id stays
    readable for the whole connection task.
    """

    def __init__(self) -> None:
        self._pending: dict[int, ExportConnectionContext] = {}
        self._denied: set[int] = set()

    def remember(
        self, connection_id: int | None, context: ExportConnectionContext
    ) -> None:
        self._denied.discard(connection_id)
        self._pending[connection_id] = context

    def get(self, connection_id: int | None) -> ExportConnectionContext | None:
        return self._pending.get(connection_id)

    def mark_denied(self, connection_id: int | None) -> None:
        self._pending.pop(connection_id, None)
        self._denied.add(connection_id)

    def is_denied(self, connection_id: int | None) -> bool:
        return connection_id in self._denied

    def clear(self, connection_id: int | None) -> None:
        self._pending.pop(connection_id, None)
        self._denied.discard(connection_id)


def client_ip_of(connection: Any) -> str:
    try:
        peer = connection.stream.writer.get_extra_info("peername")
    except Exception:  # noqa: BLE001 - a missing source address is treated as empty
        return ""
    return str(peer[0]) if peer else ""


async def audit_handshake_failure(
    services: Any,
    context: ExportServerContext,
    connection_id: int | None,
    username: str,
    client_ip: str,
) -> None:
    """Handshake failed before authentication succeeded: a deny code already recorded by get_user is not recorded twice; everything else counts as a bad password."""
    username = username or ""
    if not username:
        return
    if context.is_denied(connection_id):
        context.clear(connection_id)
        return
    entry = context.get(connection_id)
    account_name = entry.credential.account_name if entry is not None else username
    user_name = entry.principal.user_name if entry is not None else "unknown"
    context.clear(connection_id)
    await services.audit.record_export_auth_failure(
        request_id=str(uuid4()),
        account_name=account_name,
        user_name=user_name,
        client_ip=client_ip,
        code=CREDENTIAL_PASSWORD_INVALID,
    )


class CredentialIdentityProvider(IdentityProvider):
    """Register mysql_native_password only: SimpleIdentityProvider trusts the username blindly and NoLogin skips the password."""

    def __init__(
        self, services: Any, settings: Settings, context: ExportServerContext
    ) -> None:
        self._services = services
        self._settings = settings
        self._context = context

    def get_plugins(self):
        return [NativePasswordAuthPlugin()]

    async def get_user(self, username: str) -> User | None:
        username = username or ""
        connection_id = current_connection_id()
        client_ip = current_export_client_ip.get()
        credential, deny_code = await self._services.credentials.get_for_auth(username)
        if credential is None:
            await self._record(username, "unknown", deny_code, client_ip)
            return self._deny(connection_id, username)
        if deny_code is not None:
            await self._record(
                username, await self._user_name(credential), deny_code, client_ip
            )
            return self._deny(connection_id, username)
        if not ip_matches(credential.source_ip, client_ip):
            await self._record(
                username,
                await self._user_name(credential),
                CREDENTIAL_IP_MISMATCH,
                client_ip,
            )
            return self._deny(connection_id, username)
        try:
            principal, datasource = await self._services.credentials.resolve_principal(
                credential
            )
        except CredentialAuthError as exc:
            await self._record(
                username, await self._user_name(credential), exc.code, client_ip
            )
            return self._deny(connection_id, username)

        self._context.remember(
            connection_id,
            ExportConnectionContext(
                credential=credential,
                principal=principal,
                datasource=datasource,
                client_ip=client_ip,
            ),
        )
        await self._services.credentials.touch_last_used(credential.id)
        return User(
            name=username,
            auth_string=credential.auth_string,
            auth_plugin=NativePasswordAuthPlugin.name,
        )

    async def _record(self, username: str, user_name: str, code: str, client_ip: str) -> None:
        logger.info(
            "channel=export 导出认证拒绝 account=%s ip=%s code=%s", username, client_ip, code
        )
        await self._services.audit.record_export_auth_failure(
            request_id=str(uuid4()),
            account_name=username,
            user_name=user_name,
            client_ip=client_ip,
            code=code,
        )

    def _deny(self, connection_id: int | None, username: str) -> User:
        self._context.mark_denied(connection_id)
        return User(
            name=username,
            auth_string=DENIED_AUTH_STRING,
            auth_plugin=NativePasswordAuthPlugin.name,
        )

    async def _user_name(self, credential: Any) -> str:
        try:
            token = await self._services.tokens.get(credential.token_id)
        except NotFound:
            return "unknown"
        return token.user_name
