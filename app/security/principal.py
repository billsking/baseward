"""Caller and client IP in the request context."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from app.security.actor import Actor
from app.tokens.scope import UNRESTRICTED, TokenScope


@dataclass(frozen=True)
class Principal:
    user_name: str
    token_id: int
    token_prefix: str
    scope: TokenScope = UNRESTRICTED
    actor_policy: str = "ignore"
    token_kind: str = "access"
    # Set only for short-lived credentials: the actor is bound at exchange time, not per request.
    bound_actor: Actor | None = None
    short_prefix: str | None = None


current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)
current_client_ip: ContextVar[str] = ContextVar("current_client_ip", default="")


def require_principal() -> Principal:
    principal = current_principal.get()
    if principal is None:
        raise RuntimeError("当前请求缺少已认证调用者")
    return principal
