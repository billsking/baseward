"""POST /api/exchange: trade an exchange-kind token for a short-lived actor-bound credential."""

from __future__ import annotations

import logging
from datetime import timezone
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.errors import ValidationError
from app.i18n import t
from app.netip import client_ip_from_scope
from app.security.actor import ActorError, current_actor, parse_actor_headers
from app.security.principal import current_client_ip, current_principal

logger = logging.getLogger(__name__)

router = APIRouter()


class ExchangeRequest(BaseModel):
    ttl_minutes: int | None = Field(default=None, ge=1)


def _reject(status_code: int, error: str, message: str | None = None) -> JSONResponse:
    body: dict[str, str] = {"error": error}
    if message:
        body["message"] = message
    return JSONResponse(body, status_code=status_code)


@router.post("/api/exchange")
async def exchange(request: Request, payload: ExchangeRequest | None = None) -> JSONResponse:
    services = request.app.state.services
    client_ip = client_ip_from_scope(request.scope, services.trusted_proxies)
    request_id = str(uuid4())

    if not await services.mcp_allowlist.is_allowed(client_ip):
        logger.warning("channel=exchange 请求被 IP 白名单拒绝 ip=%s", client_ip)
        await services.audit.record_auth_failure(
            request_id, None, client_ip, code="IP_NOT_ALLOWED"
        )
        return _reject(403, "ip_not_allowed")

    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if not authorization or scheme.lower() != "bearer" or not token:
        await services.audit.record_auth_failure(request_id, None, client_ip)
        return _reject(401, "missing_token" if not authorization else "invalid_token")

    try:
        principal = await services.tokens.authenticate(token)
    except Exception:  # noqa: BLE001 - auth depends on the management DB
        logger.exception("Exchange Token 认证查询失败 request_id=%s", request_id)
        return _reject(503, "auth_unavailable")
    if principal is None:
        await services.audit.record_auth_failure(request_id, token[:10], client_ip)
        return _reject(401, "invalid_token")

    if principal.token_kind != "exchange":
        await services.audit.record_denied(
            request_id, "exchange", principal, None, "exchange", None, client_ip,
            "EXCHANGE_TOKEN_REQUIRED", t("error.exchange.token_required"),
        )
        return _reject(403, "exchange_token_required")

    try:
        actor = parse_actor_headers(request.headers)
    except ActorError as exc:
        logger.warning("channel=exchange actor 校验拒绝 request_id=%s code=%s", request_id, exc.code)
        await services.audit.record_denied(
            request_id, "exchange", principal, None, "exchange", None, client_ip,
            exc.code, str(exc),
        )
        return _reject(403, exc.code.lower())

    principal_token = current_principal.set(principal)
    actor_token = current_actor.set(actor)
    client_token = current_client_ip.set(client_ip)
    try:
        plaintext, row = await services.short_credentials.issue(
            parent=principal,
            actor=actor,
            client_ip=client_ip,
            ttl_minutes=payload.ttl_minutes if payload else None,
        )
    except ValidationError as exc:
        await services.audit.record_denied(
            request_id, "exchange", principal, None, "exchange", None, client_ip,
            "EXCHANGE_INVALID", str(exc),
        )
        return _reject(400, "invalid_request", str(exc))
    finally:
        current_client_ip.reset(client_token)
        current_actor.reset(actor_token)
        current_principal.reset(principal_token)

    expires_at = row.expires_at.replace(tzinfo=timezone.utc)
    return JSONResponse(
        {
            "credential": plaintext,
            "expires_at": expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "ttl_seconds": int((row.expires_at - row.created_at).total_seconds()),
            "actor": {"provider": actor.provider, "id": actor.actor_id, "name": actor.name},
        }
    )