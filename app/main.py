"""Application assembly and lifecycle."""

from __future__ import annotations

import asyncio
import gc
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.admin.rendering import BASE_DIR, render_error, render_login
from app.admin.routes import router as admin_router
from app.audit.cleanup import cleanup_loop
from app.config import Settings, load_settings
from app.db.schema import ensure_schema, seed_admin, wait_for_db
from app.errors import AdminUnauthorized, CSRFError, ConfigError
from app.exchange.routes import router as exchange_router
from app.i18n import t, translate_error
from app.i18n.middleware import LocaleMiddleware
from app.logging_setup import configure_logging
from app.mcp.server import build_mcp
from app.security.mcp_auth import BearerAuthMiddleware
from app.services import Services

logger = logging.getLogger(__name__)


class _McpPathNormalizer:
    """Redirect /mcp to /mcp/ so Mount's 307 does not bypass the auth response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    configure_logging(resolved.log_level)

    services = Services(resolved)
    mcp = build_mcp(services)
    # Must run before mounting: session_manager is created lazily and raises RuntimeError afterwards.
    mcp_app = mcp.streamable_http_app()
    export_server = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal export_server
        logger.info("管理库后端：%s", resolved.management_summary)
        # A local SQLite file has no network transient to wait out; fail fast on a bad path.
        attempts = 1 if resolved.management_backend == "sqlite" else 30
        await wait_for_db(services.engine, attempts=attempts)
        await ensure_schema(services.engine)
        async with services.session_factory() as session:
            await seed_admin(session, resolved)
        # Freeze startup-time objects to avoid gen2 full-heap scans under concurrency (hundred-millisecond pauses).
        gc.collect()
        gc.freeze()

        stop_event = asyncio.Event()
        cleanup_task = asyncio.create_task(cleanup_loop(services, stop_event))
        if resolved.mysql_export_enabled:
            # Import the protocol layer only when enabled; a disabled process carries no mysql-mimic patches.
            from app.mysql.server import build_export_server, start as start_export

            export_server = build_export_server(services)
            app.state.export_server = export_server
            # Startup failures such as a busy port propagate; no degradation.
            await start_export(export_server)
            logger.info(
                "MySQL 导出通道已监听 %s:%s",
                resolved.mysql_export_host,
                resolved.mysql_export_port,
            )
        logger.info("BaseWard 启动完成")
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            stop_event.set()
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            if export_server is not None:
                from app.mysql.server import stop as stop_export

                await stop_export(export_server)
            await services.close()
            logger.info("BaseWard 已停止")

    app = FastAPI(title="BaseWard", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(_McpPathNormalizer)
    app.add_middleware(LocaleMiddleware, mcp_language=resolved.mcp_language)
    app.state.settings = resolved
    app.state.services = services
    app.state.export_server = None

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=302)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        allow = await services.export_allowlist.snapshot()
        export_status = {
            "enabled": resolved.mysql_export_enabled,
            # Report the listening socket's actual state instead of echoing config: a mismatch means startup went wrong.
            "listening": bool(export_server is not None and export_server.sockets()),
            "active_exports": services.limiter.active if export_server is not None else 0,
            "max_concurrent": resolved.mysql_export_max_concurrent,
            # The allowlist may come from the settings page or still from env vars; to debug "cannot connect"
            # you must first know which source is active and how many entries it has.
            "allowlist_source": allow.source,
            "allowlist_entries": len(allow.entries),
        }
        try:
            async with asyncio.timeout(2):
                async with services.engine.connect() as conn:
                    await conn.exec_driver_sql("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - health check catches every exception
            logger.warning("健康检查失败：%s", exc)
            return JSONResponse(
                {"status": "unavailable", "mysql_export": export_status}, status_code=503
            )
        return JSONResponse({"status": "ok", "mysql_export": export_status})

    app.mount("/mcp", BearerAuthMiddleware(mcp_app, services))
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="admin-static",
    )
    app.include_router(admin_router)
    app.include_router(exchange_router)

    @app.exception_handler(AdminUnauthorized)
    async def admin_unauthorized(request: Request, exc: AdminUnauthorized):
        return render_login(request, str(exc), status_code=401)

    @app.exception_handler(CSRFError)
    async def csrf_failed(request: Request, exc: CSRFError):
        logger.warning("CSRF 校验失败：%s %s", request.method, request.url.path)
        return render_error(request, 400, t("error.request_rejected"), str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request: Request, exc: RequestValidationError):
        logger.info("请求参数校验失败：%s %s", request.method, request.url.path)
        if request.url.path.startswith("/admin"):
            return render_error(
                request, 400, t("error.bad_parameters"), t("error.bad_parameters_message")
            )
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": translate_error("请求参数不正确"),
                }
            },
            status_code=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/admin"):
            return render_error(
                request,
                exc.status_code,
                t("error.page_not_found")
                if exc.status_code == 404
                else t("error.request_failed"),
                t("error.page_not_found_message")
                if exc.status_code == 404
                else str(exc.detail),
            )
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        if request.url.path.startswith("/mcp"):
            return JSONResponse(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": translate_error("网关内部错误"),
                    }
                },
                status_code=500,
            )
        if request.url.path.startswith("/admin"):
            return render_error(
                request, 500, t("error.service_error"), t("error.internal_message")
            )
        return JSONResponse({"error": translate_error("网关内部错误")}, status_code=500)

    return app


try:
    app = create_app()
except ConfigError as exc:
    logging.getLogger(__name__).error("%s", exc)
    raise SystemExit(1) from exc
