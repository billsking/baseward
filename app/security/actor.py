"""End-user (actor) identity asserted by intermediary platforms on MCP calls.

The credential authenticates the bearer (principal); these headers carry the real
end user behind a bot/agent platform. P1 trust level is self-asserted only.
"""

from __future__ import annotations

import base64
import binascii
import re
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.datastructures import Headers

from app.i18n import t

PROVIDER_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
ACTOR_ID_MAX_LEN = 128
ACTOR_NAME_MAX_LEN = 128

TRUST_ASSERTED = "asserted"


@dataclass(frozen=True)
class Actor:
    provider: str
    actor_id: str
    name: str | None
    trust: str = TRUST_ASSERTED


current_actor: ContextVar[Actor | None] = ContextVar("current_actor", default=None)


class ActorError(Exception):
    """Actor assertion missing or malformed; the code goes to the audit and the 403 body."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.field = field

    def __str__(self) -> str:
        if self.code == "ACTOR_REQUIRED":
            return t("error.actor.required")
        return t("error.actor.invalid", field=self.field)


def parse_actor_headers(headers: Headers) -> Actor:
    provider = (headers.get("x-baseward-actor-provider") or "").strip()
    actor_id = (headers.get("x-baseward-actor-id") or "").strip()
    raw_name = (headers.get("x-baseward-actor-name") or "").strip()
    if not provider and not actor_id and not raw_name:
        raise ActorError("ACTOR_REQUIRED")
    if PROVIDER_PATTERN.fullmatch(provider) is None:
        raise ActorError("ACTOR_INVALID", "provider")
    if not 1 <= len(actor_id) <= ACTOR_ID_MAX_LEN or any(
        not 0x21 <= ord(char) <= 0x7E for char in actor_id
    ):
        raise ActorError("ACTOR_INVALID", "id")
    name: str | None = None
    if raw_name:
        try:
            padded = raw_name + "=" * (-len(raw_name) % 4)
            decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ActorError("ACTOR_INVALID", "name") from exc
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
            raise ActorError("ACTOR_INVALID", "name")
        name = decoded[:ACTOR_NAME_MAX_LEN] or None
    return Actor(provider=provider, actor_id=actor_id, name=name)