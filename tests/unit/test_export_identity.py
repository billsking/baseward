"""Export handshake context: isolated per connection id; the deny path must not mix entries or crash (W8)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mysql_mimic import context as mimic_context

from app.credentials.service import CREDENTIAL_IP_MISMATCH, CREDENTIAL_NOT_FOUND
from app.mysql.identity import (
    DENIED_AUTH_STRING,
    CredentialIdentityProvider,
    ExportConnectionContext,
    ExportServerContext,
    current_export_client_ip,
)


def _entry(name: str) -> ExportConnectionContext:
    return ExportConnectionContext(
        credential=SimpleNamespace(account_name=name),
        principal=SimpleNamespace(user_name=name),
        datasource=SimpleNamespace(),
        client_ip="10.0.0.1",
    )


def test_contexts_are_isolated_per_connection():
    context = ExportServerContext()
    context.remember(1, _entry("a"))
    context.remember(2, _entry("b"))

    context.clear(1)

    assert context.get(1) is None
    assert context.get(2) is not None and context.get(2).credential.account_name == "b"


def test_denying_one_connection_keeps_the_other():
    context = ExportServerContext()
    context.remember(1, _entry("a"))

    context.mark_denied(2)
    context.remember(2, _entry("b"))

    assert context.is_denied(2) is False
    assert context.get(2) is not None
    assert context.get(1) is not None


class _Audit:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_export_auth_failure(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Tokens:
    async def get(self, token_id):
        return SimpleNamespace(user_name="owner")


class _Credentials:
    def __init__(self, credential, deny_code) -> None:
        self._credential = credential
        self._deny_code = deny_code

    async def get_for_auth(self, username: str):
        return self._credential, self._deny_code

    async def resolve_principal(self, credential):
        return SimpleNamespace(user_name="u"), SimpleNamespace()

    async def touch_last_used(self, credential_id) -> None:
        return None


@pytest.mark.asyncio
async def test_unknown_user_is_denied_without_crashing():
    """The W8 refactor once left _deny referencing an undefined variable; the deny path must still return the 1045 placeholder credential."""
    audit = _Audit()
    context = ExportServerContext()
    provider = CredentialIdentityProvider(
        SimpleNamespace(credentials=_Credentials(None, CREDENTIAL_NOT_FOUND), audit=audit),
        None,
        context,
    )
    mimic_context.connection_id.set(11)
    token = current_export_client_ip.set("10.0.0.5")
    try:
        user = await provider.get_user("ghost")
    finally:
        current_export_client_ip.reset(token)
        mimic_context.connection_id.set(None)

    assert user.name == "ghost"
    assert user.auth_string == DENIED_AUTH_STRING
    assert context.is_denied(11) is True
    assert audit.calls and audit.calls[0]["code"] == CREDENTIAL_NOT_FOUND


@pytest.mark.asyncio
async def test_ip_mismatch_is_denied_and_recorded():
    audit = _Audit()
    context = ExportServerContext()
    credential = SimpleNamespace(
        account_name="acc",
        token_id=1,
        source_ip="10.0.0.9",
        auth_string="x" * 40,
    )
    provider = CredentialIdentityProvider(
        SimpleNamespace(
            credentials=_Credentials(credential, None),
            audit=audit,
            tokens=_Tokens(),
        ),
        None,
        context,
    )
    mimic_context.connection_id.set(12)
    token = current_export_client_ip.set("10.0.0.5")
    try:
        user = await provider.get_user("acc")
    finally:
        current_export_client_ip.reset(token)
        mimic_context.connection_id.set(None)

    assert user.auth_string == DENIED_AUTH_STRING
    assert context.is_denied(12) is True
    assert context.get(12) is None
    assert audit.calls[0]["code"] == CREDENTIAL_IP_MISMATCH
