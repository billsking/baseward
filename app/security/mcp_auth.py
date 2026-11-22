"""MCP Bearer-auth ASGI middleware."""

from __future__ import annotations

import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.exchange.service import ShortCredentialError
from app.i18n import t
from app.netip import client_ip_from_scope
from app.observability import new_request_id
from app.security.actor import Actor, ActorError, current_actor, parse_actor_headers
from app.security.principal import Principal, current_client_ip, current_principal

logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    """Wrap the MCP ASGI app only: admit after successful authentication and set the request context."""

    def __init__(self, app, services) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_ip = client_ip_from_scope(scope, getattr(self.services, "trusted_proxies", ()))
        request_id = new_request_id()
        if not await self.services.mcp_allowlist.is_allowed(client_ip):
            # Allowlist runs before token validation: an address outside the list cannot probe whether a credential is valid.
            logger.warning("channel=mcp 请求被 IP 白名单拒绝 ip=%s", client_ip)
            await self.services.audit.record_auth_failure(
                request_id, None, client_ip, code="IP_NOT_ALLOWED"
            )
            await self._reject(scope, receive, send, 403, "ip_not_allowed", False)
            return

        headers = Headers(scope=scope)
        authorization = headers.get("authorization") or ""

        if not authorization:
            await self.services.audit.record_auth_failure(request_id, None, client_ip)
            await self._reject(scope, receive, send, 401, "missing_token")
            return

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            await self.services.audit.record_auth_failure(request_id, None, client_ip)
            await self._reject(scope, receive, send, 401, "invalid_token")
            return

        token = token.strip()
        try:
            if token.startswith("sc_"):
                principal: Principal | None = await self.services.short_credentials.authenticate(
                    token, client_ip
                )
            else:
                principal = await self.services.tokens.authenticate(token)
        except ShortCredentialError as exc:
            logger.warning(
                "channel=mcp 短期凭证拒绝 request_id=%s code=%s", request_id, exc.code
            )
            await self.services.audit.record_auth_failure(
                request_id, token[:10], client_ip, code=exc.code
            )
            await self._reject(scope, receive, send, 401, "invalid_token")
            return
        except Exception:  # noqa: BLE001 - auth depends on the management DB; map every exception to 503
            logger.exception("MCP Token 认证查询失败 request_id=%s", request_id)
            await self._reject(scope, receive, send, 503, "auth_unavailable")
            return

        if principal is None:
            await self.services.audit.record_auth_failure(request_id, token[:10], client_ip)
            await self._reject(scope, receive, send, 401, "invalid_token")
            return

        if principal.token_kind == "exchange":
            # An exchange token only mints credentials at /api/exchange; it never reaches a tool.
            await self.services.audit.record_denied(
                request_id, "auth_failure", principal, None, None, None,
                client_ip, "EXCHANGE_ONLY", t("error.exchange.access_denied"),
            )
            await self._reject(
                scope, receive, send, 403, "exchange_only",
                message=t("error.exchange.access_denied"),
            )
            return

        actor: Actor | None = None
        if principal.bound_actor is not None:
            # The credential already carries the actor; headers are optional but must not contradict it.
            actor = principal.bound_actor
            try:
                asserted = parse_actor_headers(headers)
            except ActorError as exc:
                if exc.code != "ACTOR_REQUIRED":
                    await self._deny_actor(scope, receive, send, request_id, principal, client_ip, exc)
                    return
            else:
                if (asserted.provider, asserted.actor_id) != (actor.provider, actor.actor_id):
                    await self.services.audit.record_denied(
                        request_id, "auth_failure", principal, None, None, None,
                        client_ip, "ACTOR_MISMATCH", t("error.actor.mismatch"),
                    )
                    await self._reject(
                        scope, receive, send, 403, "actor_mismatch",
                        message=t("error.actor.mismatch"),
                    )
                    return
        elif principal.actor_policy == "require":
            try:
                actor = parse_actor_headers(headers)
            except ActorError as exc:
                await self._deny_actor(scope, receive, send, request_id, principal, client_ip, exc)
                return

        principal_token = current_principal.set(principal)
        actor_token = current_actor.set(actor)
        client_token = current_client_ip.set(client_ip)
        try:
            await self.app(scope, receive, send)
        finally:
            current_client_ip.reset(client_token)
            current_actor.reset(actor_token)
            current_principal.reset(principal_token)

    async def _deny_actor(
        self, scope, receive, send, request_id: str, principal: Principal, client_ip: str, exc: ActorError
    ) -> None:
        logger.warning("channel=mcp actor 校验拒绝 request_id=%s code=%s", request_id, exc.code)
        await self.services.audit.record_denied(
            request_id, "auth_failure", principal, None, None, None,
            client_ip, exc.code, str(exc),
        )
        await self._reject(scope, receive, send, 403, exc.code.lower(), message=str(exc))

    @staticmethod
    async def _reject(
        scope,
        receive,
        send,
        status_code: int,
        error: str,
        retry_with_credentials: bool = True,
        message: str | None = None,
    ) -> None:
        headers = {"WWW-Authenticate": "Bearer"} if retry_with_credentials else None
        response = JSONResponse(
            {
                "error": {
                    "code": error,
                    "message": message or t(f"error.auth.{error}"),
                    "request_id": new_request_id(),
                }
            },
            status_code=status_code,
            headers=headers,
        )
        await response(scope, receive, send)
