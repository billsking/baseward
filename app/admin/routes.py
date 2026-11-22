"""All admin pages and form handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from math import ceil
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from app.admin.rendering import message_context, render, render_error, render_login
from app.clock import SHANGHAI, utcnow
from app.datasources import databases as databases_module
from app.errors import (
    AdminUnauthorized,
    CSRFError,
    DataSourceConnectionError,
    NotFound,
    ValidationError,
)
from app.i18n import t, translate_error
from app.tokens.scope import UNRESTRICTED, TokenScope
from app.masking.rules import AUTO_RULES, PATTERNS, STRICTNESS, detect_auto
from app.netallow import save_many
from app.netip import client_ip_from_scope
from app.observability import new_request_id
from app.security.admin_auth import (
    AdminSession,
    authenticate_admin,
    change_password,
    clear_session,
    create_session,
    read_session,
    require_admin,
    verify_csrf,
)
from app.services import Services

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

AUDIT_PAGE_SIZE = 50
SCOPE_PAYLOAD_LIMIT = 2_000_000
SCOPE_NAME_LIMIT = 128
SCOPE_PAGE_SIZE_DEFAULT = 50
SCOPE_PAGE_SIZE_MAX = 200
SCOPE_RESOLVE_LIMIT = 2000
SUGGEST_LIMIT = 50
CREDENTIAL_PAGE_SIZE = 20
CREDENTIAL_PAGE_SIZE_MAX = 100
TTL_QUICK_OPTIONS = (1, 8, 24, 72)


def _services(request: Request) -> Services:
    return request.app.state.services


async def _current(request: Request) -> AdminSession:
    return await require_admin(request, _services(request))


async def _secured_form(request: Request, session: AdminSession):
    form = await request.form()
    verify_csrf(session, str(form.get("_csrf") or ""))
    return form


def _int_field(form, name: str, default: int = 0) -> int:
    try:
        return int(str(form.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _parse_scope(form) -> TokenScope:
    raw = str(form.get("scope_payload") or "").strip()
    if not raw:
        return UNRESTRICTED
    if len(raw) > SCOPE_PAYLOAD_LIMIT:
        raise ValidationError("授权范围过大，请减少选择")
    try:
        return TokenScope.from_json(raw)
    except ValueError as exc:
        raise ValidationError("授权范围格式不正确，请重新选择") from exc


def _scope_payload(form) -> str:
    return str(form.get("scope_payload") or "").strip()[:SCOPE_PAYLOAD_LIMIT]


def _scope_info_summary(scope: TokenScope) -> dict:
    summary = scope.summary()
    return {
        "summary": summary,
        "restricted": scope.restricted,
        "broken": 0 in scope.datasources,
    }


async def _tokens_page_context(services, **extra) -> dict:
    rows = await services.tokens.list_all()
    scopes = await services.tokens.scopes_for([row.id for row in rows])
    return {
        "rows": rows,
        "scope_info": {
            row.id: _scope_info_summary(scopes.get(row.id) or UNRESTRICTED)
            for row in rows
        },
        "datasources": await services.datasources.list_all(),
        "now": utcnow(),
        **extra,
    }


async def _metadata_denied(request: Request) -> Response | None:
    try:
        await _current(request)
    except AdminUnauthorized:
        return JSONResponse(
            {"error": {"code": "UNAUTHORIZED", "message": translate_error("未登录或会话已过期")}},
            status_code=401,
        )
    return None


def _metadata_error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": translate_error(message)}},
        status_code=status_code,
    )


def _name_param(request: Request, name: str) -> str | None:
    value = (request.query_params.get(name) or "").strip()
    if not value or len(value) > SCOPE_NAME_LIMIT:
        return None
    return value


def _suggest_term(request: Request) -> str | None:
    term = (request.query_params.get("q") or "").strip()
    if len(term) > SCOPE_NAME_LIMIT:
        return None
    return term


def _query_int(
    request: Request, name: str, default: int, minimum: int, maximum: int
) -> int | None:
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < minimum or value > maximum:
        return None
    return value


def _query_term(request: Request) -> str | None:
    raw = request.query_params.get("q")
    if raw is None:
        return ""
    term = raw.strip()
    if len(term) > SCOPE_NAME_LIMIT:
        return None
    return term


def _query_name(request: Request, name: str) -> tuple[bool, str | None]:
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return True, None
    value = _name_param(request, name)
    return value is not None, value


def _page(request: Request, name: str, context: dict, status_code: int = 200) -> Response:
    return render(request, name, context, status_code=status_code)


def _not_found(request: Request, exc: NotFound) -> Response:
    return render_error(request, 404, t("error.not_found"), str(exc))


@router.get("/login")
async def login_page(request: Request) -> Response:
    services = _services(request)
    if read_session(request, services) is not None:
        return RedirectResponse("/admin", status_code=302)
    return render_login(request)


@router.post("/login")
async def login_submit(request: Request) -> Response:
    services = _services(request)
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    client_ip = _client_ip(request)
    request_id = new_request_id()

    retry_after = services.login_guard.retry_after(username, client_ip)
    if retry_after:
        logger.warning(
            "管理员登录被锁定 username=%s ip=%s retry_after=%s",
            username,
            client_ip,
            retry_after,
        )
        await services.audit.record_admin_login(
            request_id, username, client_ip, False, "LOCKED_OUT"
        )
        return render_login(
            request,
            t("login.locked", seconds=retry_after),
            status_code=429,
        )

    try:
        admin = await authenticate_admin(services, username, password)
    except SQLAlchemyError:
        logger.exception("管理端登录查询管理库失败")
        return render_login(request, "管理库暂时不可用，请稍后重试", status_code=503)
    if admin is None:
        locked_for = services.login_guard.record_failure(username, client_ip)
        await services.audit.record_admin_login(
            request_id,
            username,
            client_ip,
            False,
            "LOCKED_OUT" if locked_for else "INVALID_CREDENTIALS",
        )
        logger.warning("管理员登录失败 username=%s ip=%s", username, client_ip)
        if locked_for:
            return render_login(
                request,
                t("login.locked", seconds=locked_for),
                status_code=429,
            )
        return render_login(request, "用户名或密码错误", status_code=401)

    services.login_guard.reset(username, client_ip)
    await services.audit.record_admin_login(request_id, admin.username, client_ip, True)
    response = RedirectResponse("/admin", status_code=302)
    create_session(response, admin, services)
    logger.info(
        "管理员登录成功 username=%s ip=%s request_id=%s",
        admin.username,
        client_ip,
        request_id,
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    session = await _current(request)
    await _secured_form(request, session)
    response = RedirectResponse("/admin/login", status_code=302)
    clear_session(response)
    return response


@router.get("")
@router.get("/")
async def dashboard(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    ds_total, ds_enabled = await services.datasources.counts()
    token_enabled = await services.tokens.enabled_count()
    today_total, today_denied = await services.audit.today_stats()
    recent = await services.audit.recent(10)
    return _page(
        request,
        "dashboard.html",
        {
            "session": session,
            "page_title": t("page.dashboard"),
            "ds_total": ds_total,
            "ds_enabled": ds_enabled,
            "token_enabled": token_enabled,
            "today_total": today_total,
            "today_denied": today_denied,
            "recent": recent,
            **message_context(request),
        },
    )


@router.get("/datasources")
async def datasource_list(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    rows = await services.datasources.list_all()
    policies = {row.id: await services.datasources.list_databases(row.id) for row in rows}
    summaries = {
        ds_id: databases_module.read_only_summary(items)
        for ds_id, items in policies.items()
    }
    return _page(
        request,
        "datasources.html",
        {
            "session": session,
            "page_title": t("page.datasources"),
            "rows": rows,
            "policies": policies,
            "db_summaries": summaries,
            "db_writable": {
                ds_id: any(not policy.is_read_only for policy in items)
                for ds_id, items in policies.items()
            },
            **message_context(request),
        },
    )


@router.get("/datasources/new")
async def datasource_new_page(request: Request) -> Response:
    session = await _current(request)
    return _page(
        request,
        "datasource_form.html",
        {
            "session": session,
            "page_title": t("page.datasource_new"),
            "mode": "new",
            "values": {"port": 3306, "databases": []},
            "message_context": message_context(request),
        },
    )


def _form_databases(form) -> list[str] | None:
    """Return None when the field was not submitted (old form or client); the service keeps the existing list."""
    if "databases" in form:
        return [str(name).strip() for name in form.getlist("databases") if str(name).strip()]
    if str(form.get("db_picker") or ""):
        return []
    return None


@router.post("/datasources/new")
async def datasource_new_submit(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    values = {
        "name": str(form.get("name") or "").strip(),
        "host": str(form.get("host") or "").strip(),
        "port": str(form.get("port") or "").strip(),
        "username": str(form.get("username") or "").strip(),
        "default_database": str(form.get("default_database") or "").strip(),
        "remark": str(form.get("remark") or "").strip(),
    }
    database_names = _form_databases(form)
    values["databases"] = database_names or []
    error = None
    try:
        await services.datasources.create(
            name=values["name"],
            host=values["host"],
            port=_int_field(form, "port"),
            username=values["username"],
            password=str(form.get("password") or ""),
            default_database=values["default_database"],
            remark=values["remark"],
            databases=database_names,
        )
    except (ValidationError, DataSourceConnectionError) as exc:
        error = str(exc)
    if error is None:
        return RedirectResponse("/admin/datasources?msg=datasource_created", status_code=302)
    return _page(
        request,
        "datasource_form.html",
        {
            "session": session,
            "page_title": t("page.datasource_new"),
            "mode": "new",
            "values": values,
            "error": error,
        },
        status_code=400,
    )


@router.post("/datasources/new/databases/refresh")
async def datasource_new_databases_refresh(request: Request) -> Response:
    try:
        session = await _current(request)
    except AdminUnauthorized:
        return _metadata_error("UNAUTHORIZED", "未登录或会话已过期", 401)
    form = await _secured_form(request, session)
    host = str(form.get("host") or "").strip()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    port = _int_field(form, "port")
    default_database = str(form.get("default_database") or "").strip()
    # The default database is picked from the list this endpoint returns, so it is still empty on the first read.
    if not host or not username or not password or not 1 <= port <= 65535:
        return _metadata_error("INVALID_ARGUMENT", "请先填写完整的连接参数（含密码）", 400)
    try:
        names = await _services(request).datasources.refresh_upstream_databases(
            host=host,
            port=port,
            username=username,
            password=password,
            default_database=default_database,
        )
    except ValidationError as exc:
        return _metadata_error("INVALID_ARGUMENT", str(exc), 400)
    except DataSourceConnectionError as exc:
        return _metadata_error("DATABASE_LIST_UNAVAILABLE", str(exc), 502)
    return JSONResponse({"databases": names})


@router.get("/datasources/{datasource_id}/edit")
async def datasource_edit_page(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    try:
        ds = await services.datasources.get(datasource_id)
        policies = await services.datasources.list_databases(datasource_id)
    except NotFound as exc:
        return _not_found(request, exc)
    values = {
        "name": ds.name,
        "host": ds.host,
        "port": ds.port,
        "username": ds.username,
        "default_database": ds.default_database,
        "remark": ds.remark or "",
        "databases": [policy.name for policy in policies],
    }
    return _page(
        request,
        "datasource_form.html",
        {
            "session": session,
            "page_title": t("page.datasource_edit_named", name=ds.name),
            "mode": "edit",
            "datasource": ds,
            "values": values,
        },
    )


@router.post("/datasources/{datasource_id}/edit")
async def datasource_edit_submit(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    values = {
        "name": str(form.get("name") or "").strip(),
        "host": str(form.get("host") or "").strip(),
        "port": str(form.get("port") or "").strip(),
        "username": str(form.get("username") or "").strip(),
        "default_database": str(form.get("default_database") or "").strip(),
        "remark": str(form.get("remark") or "").strip(),
    }
    database_names = _form_databases(form)
    try:
        current = [
            policy.name
            for policy in await services.datasources.list_databases(datasource_id)
        ]
    except NotFound as exc:
        return _not_found(request, exc)
    values["databases"] = database_names if database_names is not None else current

    removed = sorted(set(current) - set(database_names or [])) if database_names is not None else []
    if removed and str(form.get("confirm") or "") != "1":
        narrowing = await services.datasources.narrowing_impact(datasource_id, removed)
        return _page(
            request,
            "datasource_form.html",
            {
                "session": session,
                "page_title": t("page.datasource_edit"),
                "mode": "edit",
                "datasource": SimpleNamespace(id=datasource_id, name=values["name"]),
                "values": values,
                "narrowing": {**narrowing, "removed": removed},
            },
            status_code=400,
        )

    try:
        await services.datasources.update(
            datasource_id,
            name=values["name"],
            host=values["host"],
            port=_int_field(form, "port"),
            username=values["username"],
            password=str(form.get("password") or ""),
            default_database=values["default_database"],
            remark=values["remark"],
            databases=database_names,
        )
    except NotFound as exc:
        return _not_found(request, exc)
    except (ValidationError, DataSourceConnectionError) as exc:
        return _page(
            request,
            "datasource_form.html",
            {
                "session": session,
                "page_title": t("page.datasource_edit"),
                "mode": "edit",
                "datasource": SimpleNamespace(id=datasource_id, name=values["name"]),
                "values": values,
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse("/admin/datasources?msg=datasource_updated", status_code=302)


@router.post("/datasources/{datasource_id}/delete")
async def datasource_delete(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    try:
        await services.datasources.delete(datasource_id)
    except NotFound as exc:
        return _not_found(request, exc)
    return RedirectResponse("/admin/datasources?msg=datasource_deleted", status_code=302)


@router.post("/datasources/{datasource_id}/test")
async def datasource_test(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    rows = await services.datasources.list_all()
    try:
        elapsed = await services.datasources.test_saved(datasource_id)
    except NotFound as exc:
        return _not_found(request, exc)
    except (ValidationError, DataSourceConnectionError) as exc:
        return _page(
            request,
            "datasources.html",
            {
                "session": session,
                "page_title": t("page.datasources"),
                "rows": rows,
                "error": str(exc),
                **message_context(request),
            },
        )
    return RedirectResponse(f"/admin/datasources?msg=connection_ok&n={elapsed}", status_code=302)


@router.get("/datasources/{datasource_id}/databases")
async def datasource_databases(request: Request, datasource_id: int) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    try:
        policies = await _services(request).datasources.list_databases(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)
    return JSONResponse(
        {
            "databases": [
                {"name": policy.name, "read_only": policy.is_read_only}
                for policy in policies
            ]
        }
    )


@router.post("/datasources/{datasource_id}/databases/refresh")
async def datasource_databases_refresh(request: Request, datasource_id: int) -> Response:
    try:
        session = await _current(request)
    except AdminUnauthorized:
        return _metadata_error("UNAUTHORIZED", "未登录或会话已过期", 401)
    await _secured_form(request, session)
    try:
        names = await _services(request).datasources.refresh_saved_databases(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)
    except DataSourceConnectionError as exc:
        return _metadata_error("DATABASE_LIST_UNAVAILABLE", str(exc), 502)
    return JSONResponse({"databases": names})


async def _policy_rows(
    services: Services, datasource_id: int
) -> list[dict[str, object]]:
    policies = await services.datasources.list_databases(datasource_id)
    stored = await services.datasources.stored_database_read_only(datasource_id)
    return [
        {
            "name": policy.name,
            "is_read_only": policy.is_read_only,
            "stored_read_only": stored.get(policy.name, policy.is_read_only),
        }
        for policy in policies
    ]


async def _policy_context(
    services: Services, session: AdminSession, datasource
) -> dict[str, object]:
    rows = await _policy_rows(services, datasource.id)
    rules = await services.masking.list_rules(datasource.id)
    return {
        "session": session,
        "page_title": t("page.policy", name=datasource.name),
        "datasource": datasource,
        "database_rows": rows,
        "exposed_databases": {str(row["name"]) for row in rows},
        "has_preconfigured_writable": bool(datasource.is_read_only)
        and any(not bool(row["stored_read_only"]) for row in rows),
        "rules": rules,
        "auto_rules": AUTO_RULES,
        "patterns": PATTERNS,
        "strictness": STRICTNESS,
    }


@router.get("/datasources/{datasource_id}/policy")
async def policy_page(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    try:
        ds = await services.datasources.get(datasource_id)
        context = await _policy_context(services, session, ds)
    except NotFound as exc:
        return _not_found(request, exc)
    return _page(
        request,
        "policy.html",
        {**context, **message_context(request)},
    )


@router.post("/datasources/{datasource_id}/policy")
async def policy_submit(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    is_read_only = str(form.get("is_read_only") or "0") == "1"
    try:
        ds = await services.datasources.get(datasource_id)
        changed = await services.datasources.set_read_only(datasource_id, is_read_only)
    except NotFound as exc:
        return _not_found(request, exc)
    if changed:
        await services.audit.record_policy_change(
            ds.name,
            {"总开关": bool(ds.is_read_only)},
            {"总开关": is_read_only},
            session.username,
            _client_ip(request),
        )
    return RedirectResponse(
        f"/admin/datasources/{datasource_id}/policy?msg=policy_saved", status_code=302
    )


@router.post("/datasources/{datasource_id}/databases/{database}/policy")
async def database_policy_submit(
    request: Request, datasource_id: int, database: str
) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    is_read_only = str(form.get("read_only") or "1") == "1"
    try:
        ds = await services.datasources.get(datasource_id)
        rows = await _policy_rows(services, datasource_id)
    except NotFound as exc:
        return _not_found(request, exc)
    current = next((row for row in rows if row["name"] == database), None)
    if current is None:
        return _not_found(request, NotFound("库未暴露或不存在"))
    if (
        current["stored_read_only"]
        and not is_read_only
        and str(form.get("confirm") or "") != "1"
    ):
        context = await _policy_context(services, session, ds)
        return _page(
            request,
            "policy.html",
            {**context, "error": t("policy.confirm_required")},
            status_code=400,
        )
    try:
        changed = await services.datasources.set_database_read_only(
            datasource_id, database, is_read_only
        )
    except NotFound as exc:
        return _not_found(request, exc)
    if changed:
        await services.audit.record_policy_change(
            ds.name,
            {database: bool(current["stored_read_only"])},
            {database: is_read_only},
            session.username,
            _client_ip(request),
        )
    return RedirectResponse(
        f"/admin/datasources/{datasource_id}/policy?msg=policy_saved", status_code=302
    )


@router.post("/datasources/{datasource_id}/masking/new")
async def masking_add(request: Request, datasource_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    try:
        await services.datasources.get(datasource_id)
        await services.masking.add_rule(
            datasource_id,
            database=str(form.get("database") or ""),
            table=str(form.get("table") or ""),
            column=str(form.get("column") or ""),
            strategy=str(form.get("strategy") or ""),
            pattern=str(form.get("pattern") or "") or None,
        )
    except NotFound as exc:
        return _not_found(request, exc)
    except ValidationError as exc:
        ds = await services.datasources.get(datasource_id)
        context = await _policy_context(services, session, ds)
        return _page(
            request,
            "policy.html",
            {**context, "error": str(exc)},
            status_code=400,
        )
    return RedirectResponse(
        f"/admin/datasources/{datasource_id}/policy?msg=rule_added", status_code=302
    )


@router.post("/datasources/{datasource_id}/masking/{rule_id}/delete")
async def masking_delete(request: Request, datasource_id: int, rule_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    try:
        await services.masking.delete_rule(datasource_id, rule_id)
    except NotFound as exc:
        return _not_found(request, exc)
    return RedirectResponse(
        f"/admin/datasources/{datasource_id}/policy?msg=rule_deleted", status_code=302
    )


@router.get("/datasources/{datasource_id}/metadata/tables")
async def datasource_metadata_tables(request: Request, datasource_id: int) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    term = _suggest_term(request)
    if term is None:
        return _metadata_error("INVALID_ARGUMENT", "搜索关键词过长", 400)
    services = _services(request)
    try:
        datasource = await services.datasources.get(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)

    sql = (
        "SELECT table_schema, table_name FROM information_schema.tables "
        f"WHERE {databases_module.exclude_system_schemas('table_schema')}"
    )
    params: list[object] = []
    database = _name_param(request, "database")
    if database:
        sql += " AND table_schema = %s"
        params.append(database)
    if term:
        sql += " AND INSTR(LOWER(table_name), LOWER(%s)) > 0"
        params.append(term)
    sql += " ORDER BY table_schema, table_name LIMIT %s"
    params.append(SUGGEST_LIMIT + 1)
    try:
        engine = await services.pool.get(datasource)
        async with engine.connect() as conn:
            rows = (await conn.exec_driver_sql(sql, tuple(params))).fetchall()
    except Exception:  # noqa: BLE001 - metadata read failures map to a generic 502 without leaking connection details
        logger.exception("管理端联想读取表清单失败 datasource_id=%s", datasource_id)
        return _metadata_error("METADATA_UNAVAILABLE", "读取数据表失败，请检查数据源连接", 502)
    return JSONResponse(
        {
            "tables": [
                {"database": schema, "table": table}
                for schema, table in rows[:SUGGEST_LIMIT]
            ],
            "truncated": len(rows) > SUGGEST_LIMIT,
        }
    )


@router.get("/datasources/{datasource_id}/metadata/columns")
async def datasource_metadata_columns(request: Request, datasource_id: int) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    term = _suggest_term(request)
    if term is None:
        return _metadata_error("INVALID_ARGUMENT", "搜索关键词过长", 400)
    database = _name_param(request, "database")
    table = _name_param(request, "table")
    if database is None or table is None:
        return _metadata_error("INVALID_ARGUMENT", "库名或表名不正确", 400)
    services = _services(request)
    try:
        datasource = await services.datasources.get(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)

    sql = (
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s"
    )
    params: list[object] = [database, table]
    if term:
        sql += " AND INSTR(LOWER(column_name), LOWER(%s)) > 0"
        params.append(term)
    sql += " ORDER BY ordinal_position LIMIT %s"
    params.append(SUGGEST_LIMIT + 1)
    try:
        engine = await services.pool.get(datasource)
        async with engine.connect() as conn:
            rows = (await conn.exec_driver_sql(sql, tuple(params))).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception(
            "管理端联想读取表字段失败 datasource_id=%s database=%s table=%s",
            datasource_id,
            database,
            table,
        )
        return _metadata_error("METADATA_UNAVAILABLE", "读取表字段失败，请检查数据源连接", 502)
    return JSONResponse(
        {
            "columns": [
                {"column": name, "data_type": data_type}
                for name, data_type in rows[:SUGGEST_LIMIT]
            ],
            "truncated": len(rows) > SUGGEST_LIMIT,
        }
    )


@router.get("/tokens")
async def tokens_page(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    context = await _tokens_page_context(services)
    return _page(
        request,
        "tokens.html",
        {
            "session": session,
            "page_title": "Token",
            "scope_json": "",
            **context,
            **message_context(request),
        },
    )


@router.post("/tokens/new")
async def token_create(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    user_name = str(form.get("user_name") or "")
    actor_policy = str(form.get("actor_policy") or "ignore")
    token_kind = str(form.get("token_kind") or "access")
    raw_expires_at = str(form.get("expires_at") or "").strip()
    raw_scope = _scope_payload(form)
    error = None
    plaintext = None
    try:
        expires_at = _parse_local_datetime(raw_expires_at)
        if raw_expires_at and expires_at is None:
            raise ValidationError(t("error.token.expiry_invalid"))
        plaintext, _ = await services.tokens.create(
            user_name, _parse_scope(form), actor_policy, token_kind, expires_at
        )
    except ValidationError as exc:
        error = str(exc)
    context = await _tokens_page_context(services)
    return _page(
        request,
        "tokens.html",
        {
            "session": session,
            "page_title": "Token",
            "new_token": plaintext,
            "values": {
                "user_name": user_name,
                "actor_policy": actor_policy,
                "token_kind": token_kind,
                "expires_at": raw_expires_at,
            },
            "scope_json": raw_scope,
            "error": error,
            **context,
        },
    )


@router.get("/tokens/scope/databases")
async def token_scope_databases(request: Request) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    services = _services(request)
    datasource_id = _int_field(request.query_params, "datasource_id")
    if datasource_id <= 0:
        return _metadata_error("INVALID_ARGUMENT", "数据源参数不正确", 400)
    try:
        await services.datasources.get(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)
    token_id = _int_field(request.query_params, "token_id")
    scope = UNRESTRICTED
    if token_id > 0:
        scope = await services.tokens.scope_for(token_id)
    policies = await services.datasources.list_databases(datasource_id)
    return JSONResponse(
        {
            "databases": [
                {
                    "name": policy.name,
                    "state": scope.database_state(datasource_id, policy.name),
                }
                for policy in policies
            ]
        }
    )


@router.get("/tokens/scope/tables")
async def token_scope_tables(request: Request) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    services = _services(request)
    datasource_id = _int_field(request.query_params, "datasource_id")
    if datasource_id <= 0:
        return _metadata_error("INVALID_ARGUMENT", "数据源参数不正确", 400)
    database_ok, database = _query_name(request, "database")
    term = _query_term(request)
    offset = _query_int(request, "offset", 0, 0, 10_000_000)
    limit = _query_int(
        request, "limit", SCOPE_PAGE_SIZE_DEFAULT, 1, SCOPE_PAGE_SIZE_MAX
    )
    match = (request.query_params.get("match") or "table").strip().lower()
    if not database_ok or term is None or offset is None or limit is None:
        return _metadata_error("INVALID_ARGUMENT", "搜索或分页参数不正确", 400)
    if match not in ("table", "column"):
        return _metadata_error("INVALID_ARGUMENT", "match 必须是 table 或 column", 400)
    if match == "column" and not term:
        match = "table"
    try:
        datasource = await services.datasources.get(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)

    where = [databases_module.exclude_system_schemas("t.table_schema")]
    params: list[object] = []
    if database:
        where.append("t.table_schema = %s")
        params.append(database)
    if term:
        if match == "column":
            where.append(
                "(INSTR(LOWER(t.table_name), LOWER(%s)) > 0 "
                "OR INSTR(LOWER(COALESCE(t.table_comment, '')), LOWER(%s)) > 0 "
                "OR EXISTS ("
                "SELECT 1 FROM information_schema.columns c "
                "WHERE c.table_schema = t.table_schema "
                "AND c.table_name = t.table_name "
                "AND (INSTR(LOWER(c.column_name), LOWER(%s)) > 0 "
                "OR INSTR(LOWER(COALESCE(c.column_comment, '')), LOWER(%s)) > 0)))"
            )
            params.extend([term, term, term, term])
        else:
            where.append(
                "(INSTR(LOWER(t.table_name), LOWER(%s)) > 0 "
                "OR INSTR(LOWER(COALESCE(t.table_comment, '')), LOWER(%s)) > 0)"
            )
            params.extend([term, term])
    where_sql = " AND ".join(where)

    try:
        engine = await services.pool.get(datasource)
        async with engine.connect() as conn:
            total = int(
                (
                    await conn.exec_driver_sql(
                        f"SELECT COUNT(*) FROM information_schema.tables t WHERE {where_sql}",
                        tuple(params),
                    )
                ).scalar_one()
            )
            rows = (
                await conn.exec_driver_sql(
                    "SELECT t.table_schema, t.table_name, "
                    "COALESCE(t.table_comment, '') "
                    f"FROM information_schema.tables t WHERE {where_sql} "
                    "ORDER BY t.table_schema, t.table_name LIMIT %s OFFSET %s",
                    tuple([*params, limit, offset]),
                )
            ).fetchall()
            database_rows = (
                await conn.exec_driver_sql(
                    "SELECT t.table_schema, COUNT(*) "
                    f"FROM information_schema.tables t WHERE {where_sql} "
                    "GROUP BY t.table_schema ORDER BY t.table_schema",
                    tuple(params),
                )
            ).fetchall()
            matched: dict[tuple[str, str], list[dict]] = {}
            if match == "column" and rows:
                pairs = [(str(schema), str(table)) for schema, table, _ in rows]
                placeholders = ",".join(["(%s,%s)"] * len(pairs))
                column_params: list[object] = [
                    value for pair in pairs for value in pair
                ]
                column_params.extend([term, term])
                column_rows = (
                    await conn.exec_driver_sql(
                        "SELECT table_schema, table_name, column_name, data_type "
                        "FROM information_schema.columns "
                        f"WHERE (table_schema, table_name) IN ({placeholders}) "
                        "AND (INSTR(LOWER(column_name), LOWER(%s)) > 0 "
                        "OR INSTR(LOWER(COALESCE(column_comment, '')), LOWER(%s)) > 0) "
                        "ORDER BY table_schema, table_name, ordinal_position",
                        tuple(column_params),
                    )
                ).fetchall()
                for schema, table, column, data_type in column_rows:
                    bucket = matched.setdefault((str(schema), str(table)), [])
                    if len(bucket) < 5:
                        bucket.append(
                            {"column": column, "data_type": data_type}
                        )
    except Exception:  # noqa: BLE001 - metadata read failures map to a generic 502 without leaking connection details
        logger.exception("管理端读取数据源表清单失败 datasource_id=%s", datasource_id)
        return _metadata_error("METADATA_UNAVAILABLE", "读取数据表失败，请检查数据源连接", 502)

    has_more = offset + len(rows) < total
    return JSONResponse(
        {
            "datasource_id": datasource.id,
            "default_database": datasource.default_database,
            "tables": [
                {
                    "database": schema,
                    "table": table,
                    "comment": comment,
                    **(
                        {"matched_columns": matched.get((str(schema), str(table)), [])}
                        if match == "column"
                        else {}
                    ),
                }
                for schema, table, comment in rows
            ],
            "databases": [
                {"database": schema, "total": int(count)}
                for schema, count in database_rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "truncated": has_more,
        }
    )


@router.get("/tokens/scope/columns")
async def token_scope_columns(request: Request) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    services = _services(request)
    datasource_id = _int_field(request.query_params, "datasource_id")
    database = _name_param(request, "database")
    table = _name_param(request, "table")
    term = _query_term(request)
    offset = _query_int(request, "offset", 0, 0, 10_000_000)
    limit = _query_int(
        request, "limit", SCOPE_PAGE_SIZE_MAX, 1, SCOPE_PAGE_SIZE_MAX
    )
    if (
        datasource_id <= 0
        or database is None
        or table is None
        or term is None
        or offset is None
        or limit is None
    ):
        return _metadata_error("INVALID_ARGUMENT", "数据源、库名、表名或分页参数不正确", 400)
    try:
        datasource = await services.datasources.get(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)

    where = "table_schema = %s AND table_name = %s"
    params: list[object] = [database, table]
    if term:
        where += (
            " AND (INSTR(LOWER(column_name), LOWER(%s)) > 0 "
            "OR INSTR(LOWER(COALESCE(column_comment, '')), LOWER(%s)) > 0)"
        )
        params.extend([term, term])
    try:
        engine = await services.pool.get(datasource)
        async with engine.connect() as conn:
            total = int(
                (
                    await conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        f"WHERE {where}",
                        tuple(params),
                    )
                ).scalar_one()
            )
            rows = (
                await conn.exec_driver_sql(
                    "SELECT column_name, data_type, COALESCE(column_comment, '') "
                    "FROM information_schema.columns "
                    f"WHERE {where} ORDER BY ordinal_position LIMIT %s OFFSET %s",
                    tuple([*params, limit, offset]),
                )
            ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception(
            "管理端读取表字段失败 datasource_id=%s database=%s table=%s",
            datasource_id,
            database,
            table,
        )
        return _metadata_error("METADATA_UNAVAILABLE", "读取表字段失败，请检查数据源连接", 502)

    has_more = offset + len(rows) < total
    columns = []
    for name, data_type, comment in rows:
        rule = detect_auto(str(name))
        columns.append(
            {
                "column": name,
                "data_type": data_type,
                "comment": comment,
                "sensitive": rule is not None,
                "sensitivity": (rule.pattern or rule.strategy) if rule else None,
            }
        )
    return JSONResponse(
        {
            "datasource_id": datasource.id,
            "database": database,
            "table": table,
            "columns": columns,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "truncated": has_more,
        }
    )


@router.get("/tokens/scope/presets")
async def token_scope_presets(request: Request) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    services = _services(request)
    rows = await services.tokens.list_all()
    scopes = await services.tokens.scopes_for([row.id for row in rows])
    return JSONResponse(
        {
            "tokens": [
                {
                    "id": row.id,
                    "user_name": row.user_name,
                    "enabled": bool(row.enabled),
                    "summary": _scope_info_summary(
                        scopes.get(row.id) or UNRESTRICTED
                    )["summary"],
                }
                for row in rows
            ]
        }
    )


@router.get("/tokens/{token_id}/scope")
async def token_scope_get(request: Request, token_id: int) -> Response:
    denied = await _metadata_denied(request)
    if denied is not None:
        return denied
    services = _services(request)
    try:
        token = await services.tokens.get(token_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "Token 不存在", 404)
    scope = await services.tokens.scope_for(token_id)
    return JSONResponse(
        {
            "token_id": token.id,
            "user_name": token.user_name,
            "enabled": bool(token.enabled),
            "scope": scope.to_dict(),
        }
    )


@router.post("/tokens/scope/resolve")
async def token_scope_resolve(request: Request) -> Response:
    try:
        session = await _current(request)
    except AdminUnauthorized:
        return JSONResponse(
            {"error": {"code": "UNAUTHORIZED", "message": translate_error("未登录或会话已过期")}},
            status_code=401,
        )
    try:
        verify_csrf(session, request.headers.get("X-CSRF-Token"))
    except CSRFError:
        return _metadata_error("CSRF_FAILED", "CSRF 校验失败", 400)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return _metadata_error("INVALID_ARGUMENT", "请求体不是合法 JSON", 400)
    if not isinstance(body, dict):
        return _metadata_error("INVALID_ARGUMENT", "请求体格式不正确", 400)
    datasource_id = body.get("datasource_id")
    items = body.get("items")
    if (
        isinstance(datasource_id, bool)
        or not isinstance(datasource_id, int)
        or datasource_id <= 0
    ):
        return _metadata_error("INVALID_ARGUMENT", "数据源参数不正确", 400)
    if not isinstance(items, list) or len(items) > SCOPE_RESOLVE_LIMIT:
        return _metadata_error("INVALID_ARGUMENT", "导入条目过多或格式不正确", 400)
    parsed: list[tuple[str, str, str | None]] = []
    for item in items:
        if not isinstance(item, list) or len(item) not in (2, 3):
            return _metadata_error("INVALID_ARGUMENT", "导入条目格式不正确", 400)
        parts = [str(part).strip() for part in item]
        if any(not part or len(part) > SCOPE_NAME_LIMIT for part in parts):
            return _metadata_error("INVALID_ARGUMENT", "导入条目名称不正确", 400)
        parsed.append((parts[0], parts[1], parts[2] if len(parts) == 3 else None))
    services = _services(request)
    try:
        datasource = await services.datasources.get(datasource_id)
    except NotFound:
        return _metadata_error("NOT_FOUND", "数据源不存在", 404)

    table_keys = sorted({(db, table) for db, table, _ in parsed})
    column_keys = sorted({item for item in parsed if item[2] is not None})
    tables_found: set[tuple[str, str]] = set()
    columns_found: set[tuple[str, str, str]] = set()
    try:
        engine = await services.pool.get(datasource)
        async with engine.connect() as conn:
            if table_keys:
                placeholders = ",".join(["(%s,%s)"] * len(table_keys))
                params = [value for pair in table_keys for value in pair]
                rows = (
                    await conn.exec_driver_sql(
                        "SELECT table_schema, table_name "
                        "FROM information_schema.tables "
                        f"WHERE {databases_module.exclude_system_schemas('table_schema')} "
                        f"AND (table_schema, table_name) IN ({placeholders})",
                        tuple(params),
                    )
                ).fetchall()
                tables_found = {(str(schema), str(table)) for schema, table in rows}
            if column_keys:
                placeholders = ",".join(["(%s,%s,%s)"] * len(column_keys))
                params = [value for triple in column_keys for value in triple]
                rows = (
                    await conn.exec_driver_sql(
                        "SELECT table_schema, table_name, column_name "
                        "FROM information_schema.columns "
                        f"WHERE (table_schema, table_name, column_name) IN ({placeholders})",
                        tuple(params),
                    )
                ).fetchall()
                columns_found = {
                    (str(schema), str(table), str(column))
                    for schema, table, column in rows
                }
    except Exception:  # noqa: BLE001
        logger.exception("管理端校验授权导入条目失败 datasource_id=%s", datasource_id)
        return _metadata_error("METADATA_UNAVAILABLE", "校验导入条目失败，请检查数据源连接", 502)

    matched: list[list[str]] = []
    missing: list[list[str]] = []
    for db, table, column in parsed:
        if column is None:
            ok = (db, table) in tables_found
        else:
            ok = (db, table, column) in columns_found
        target = matched if ok else missing
        target.append([db, table] if column is None else [db, table, column])
    return JSONResponse({"matched": matched, "missing": missing})


@router.get("/tokens/{token_id}/edit")
async def token_edit_page(request: Request, token_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    try:
        token = await services.tokens.get(token_id)
    except NotFound as exc:
        return _not_found(request, exc)
    scope = await services.tokens.scope_for(token_id)
    return _page(
        request,
        "token_edit.html",
        {
            "session": session,
            "page_title": t("page.token_edit"),
            "token": token,
            "scope_json": scope.to_json(),
            "datasources": await services.datasources.list_all(),
            **message_context(request),
        },
    )


@router.post("/tokens/{token_id}/scope")
async def token_scope_update(request: Request, token_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    raw_scope = _scope_payload(form)
    try:
        await services.tokens.update_scope(token_id, _parse_scope(form))
    except NotFound as exc:
        return _not_found(request, exc)
    except ValidationError as exc:
        try:
            token = await services.tokens.get(token_id)
        except NotFound as not_found:
            return _not_found(request, not_found)
        return _page(
            request,
            "token_edit.html",
            {
                "session": session,
                "page_title": t("page.token_edit"),
                "token": token,
                "scope_json": raw_scope,
                "datasources": await services.datasources.list_all(),
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse("/admin/tokens?msg=token_scope_updated", status_code=302)


@router.post("/tokens/{token_id}/policy")
async def token_policy_update(request: Request, token_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    actor_policy = str(form.get("actor_policy") or "")
    try:
        token = await services.tokens.get(token_id)
    except NotFound as exc:
        return _not_found(request, exc)
    try:
        await services.tokens.update_actor_policy(token_id, actor_policy)
    except ValidationError as exc:
        scope = await services.tokens.scope_for(token_id)
        return _page(
            request,
            "token_edit.html",
            {
                "session": session,
                "page_title": t("page.token_edit"),
                "token": token,
                "scope_json": scope.to_json(),
                "datasources": await services.datasources.list_all(),
                "error": str(exc),
            },
            status_code=400,
        )
    if token.actor_policy != actor_policy:
        await services.audit.record_setting_change(
            "token_actor_policy",
            f"{token.id} {token.user_name}: {token.actor_policy}",
            f"{token.id} {token.user_name}: {actor_policy}",
            session.username,
            _client_ip(request),
        )
    return RedirectResponse("/admin/tokens?msg=token_policy_updated", status_code=302)


@router.post("/tokens/{token_id}/disable")
async def token_disable(request: Request, token_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    try:
        await services.tokens.disable(token_id)
    except NotFound as exc:
        return _not_found(request, exc)
    return RedirectResponse("/admin/tokens?msg=token_disabled", status_code=302)


@router.post("/tokens/{token_id}/delete")
async def token_delete(request: Request, token_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    try:
        await services.tokens.delete(token_id)
    except NotFound as exc:
        return _not_found(request, exc)
    return RedirectResponse("/admin/tokens?msg=token_deleted", status_code=302)


# ---------- Temporary export credentials (v1.4) ----------


def _client_ip(request: Request) -> str:
    """Admin-side source IP: same rule as MCP, X-Forwarded-For is trusted only from a trusted proxy."""
    services = _services(request)
    return client_ip_from_scope(
        request.scope, getattr(services, "trusted_proxies", ())
    )


async def _credential_form_context(
    services,
    selected_token_id: int = 0,
    selected_datasource_id: int = 0,
    client_ip: str = "",
) -> dict:
    """Issue-form context: datasource and target database are filtered by the selected token's scope."""
    datasources = [item for item in await services.datasources.list_all() if item.enabled]
    now = utcnow()
    tokens = [
        item
        for item in await services.tokens.list_all()
        if item.enabled and (item.expires_at is None or item.expires_at > now)
    ]
    scopes = await services.tokens.scopes_for([item.id for item in tokens])
    token = next(
        (row for row in tokens if row.id == selected_token_id),
        tokens[0] if tokens else None,
    )
    scope = (scopes.get(token.id) if token else None) or UNRESTRICTED
    visible = [ds for ds in datasources if scope.allows_datasource(ds.id)]
    selected = next(
        (ds for ds in visible if ds.id == selected_datasource_id),
        visible[0] if visible else None,
    )
    database_options = []
    selected_database = ""
    if selected is not None:
        exposed, policies = await databases_module.exposed_and_policies(
            services.session_factory, selected
        )
        database_options = databases_module.available_databases(
            sorted(exposed), policies, scope, selected.id
        )
        names = [item.name for item in database_options]
        selected_database = (
            selected.default_database
            if selected.default_database in names
            else (names[0] if names else "")
        )
    ttl_max = services.settings.mysql_export_max_ttl_hours
    return {
        "tokens": tokens,
        "datasources": visible,
        "database_options": database_options,
        "ttl_options": [hour for hour in TTL_QUICK_OPTIONS if hour <= ttl_max],
        "ttl_default": min(8, ttl_max),
        "ttl_max": ttl_max,
        "selected_token_id": token.id if token else 0,
        "selected_datasource_id": selected.id if selected else 0,
        "selected_database": selected_database,
        "client_ip": client_ip,
    }


def _export_endpoint(settings) -> tuple[str, int]:
    host = settings.mysql_export_host
    if host in {"0.0.0.0", "::", ""}:
        host = t("credential.gateway_placeholder")
    return host, settings.mysql_export_port


@router.get("/credentials")
async def credentials_page(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    page = max(1, _int_field(request.query_params, "page", 1))
    page_size = _int_field(request.query_params, "page_size", CREDENTIAL_PAGE_SIZE)
    page_size = min(max(page_size, 1), CREDENTIAL_PAGE_SIZE_MAX)
    rows = await services.credentials.list_all()
    pages = max(1, ceil(len(rows) / page_size))
    page = min(page, pages)
    start = (page - 1) * page_size
    return _page(
        request,
        "credentials.html",
        {
            "session": session,
            "page_title": t("page.credentials"),
            "rows": rows[start : start + page_size],
            "total": len(rows),
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "prev_url": f"/admin/credentials?{urlencode({'page': page - 1, 'page_size': page_size})}",
            "next_url": f"/admin/credentials?{urlencode({'page': page + 1, 'page_size': page_size})}",
            "export_enabled": services.settings.mysql_export_enabled,
            **message_context(request),
        },
    )


@router.get("/credentials/new")
async def credential_new_page(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    token_id = _int_field(request.query_params, "token_id")
    datasource_id = _int_field(request.query_params, "datasource_id")
    if datasource_id:
        try:
            await services.datasources.get(datasource_id)
        except NotFound as exc:
            return _not_found(request, exc)
    context = await _credential_form_context(
        services, token_id, datasource_id, client_ip=_client_ip(request)
    )
    return _page(
        request,
        "credentials_new.html",
        {
            "session": session,
            "page_title": t("page.credential_new"),
            "export_enabled": services.settings.mysql_export_enabled,
            **context,
            **message_context(request),
        },
    )


@router.post("/credentials/new")
async def credential_issue(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    token_id = _int_field(form, "token_id")
    datasource_id = _int_field(form, "datasource_id")
    database_input = str(form.get("default_database") or "").strip()
    ttl_hours = _int_field(form, "ttl_hours")
    source_input = str(form.get("source_ip") or "").strip()
    client_ip = _client_ip(request)
    error = None
    account = password = None
    if error is None:
        try:
            account, password = await services.credentials.issue(
                token_id=token_id,
                datasource_id=datasource_id,
                database=database_input,
                ttl_hours=ttl_hours,
                admin_username=session.username,
                client_ip=client_ip,
                source_ip=source_input or None,
            )
        except (ValidationError, NotFound) as exc:
            error = str(exc)
    if error is not None:
        context = await _credential_form_context(services, token_id, datasource_id, client_ip)
        return _page(
            request,
            "credentials_new.html",
            {
                "session": session,
                "page_title": t("page.credential_new"),
                "export_enabled": services.settings.mysql_export_enabled,
                "error": error,
                "values": {
                    "token_id": token_id,
                    "datasource_id": datasource_id,
                    "default_database": database_input,
                    "ttl_hours": ttl_hours,
                    "source_ip": source_input,
                },
                **context,
            },
            status_code=400,
        )

    datasource = await services.datasources.get(datasource_id)
    credential = await services.credentials.get_active(account)
    target_database = (
        credential.default_database if credential is not None else database_input
    )
    host, port = _export_endpoint(services.settings)
    url = f"mysql+pymysql://{account}:{password}@{host}:{port}/{target_database}"
    return _page(
        request,
        "credentials_issued.html",
        {
            "session": session,
            "page_title": t("page.credential_issued"),
            "account": account,
            "password": password,
            "datasource_name": datasource.name,
            "source_ip": credential.source_ip if credential else "",
            "database": target_database,
            "database_notice": t("credential.database_notice", db=target_database),
            "expires_at": utcnow() + timedelta(hours=ttl_hours),
            "mysql_snippet": (
                f"mysql -h {host} -P {port} -u {account} "
                f"-p'{password}' {target_database}"
            ),
            "pandas_snippet": (
                'import pandas as pd\n'
                f'pd.read_sql("SELECT * FROM users LIMIT 100", "{url}")'
            ),
            "sqlalchemy_snippet": (
                "from sqlalchemy import create_engine\n"
                f'engine = create_engine("{url}")'
            ),
            "jdbc_snippet": (
                f"jdbc:mysql://{host}:{port}/{target_database}"
                f"?user={account}&password={password}"
            ),
        },
    )


@router.post("/credentials/{credential_id}/revoke")
async def credential_revoke(request: Request, credential_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    try:
        await services.credentials.revoke(
            credential_id, session.username, _client_ip(request)
        )
    except NotFound as exc:
        return _not_found(request, exc)
    return RedirectResponse("/admin/credentials?msg=credential_revoked", status_code=302)


@router.get("/short-credentials")
async def short_credentials_page(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    page = max(1, _int_field(request.query_params, "page", 1))
    page_size = _int_field(request.query_params, "page_size", CREDENTIAL_PAGE_SIZE)
    page_size = min(max(page_size, 1), CREDENTIAL_PAGE_SIZE_MAX)
    rows = await services.short_credentials.list_all()
    pages = max(1, ceil(len(rows) / page_size))
    page = min(page, pages)
    start = (page - 1) * page_size
    return _page(
        request,
        "short_credentials.html",
        {
            "session": session,
            "page_title": t("page.short_credentials"),
            "rows": rows[start : start + page_size],
            "total": len(rows),
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "prev_url": f"/admin/short-credentials?{urlencode({'page': page - 1, 'page_size': page_size})}",
            "next_url": f"/admin/short-credentials?{urlencode({'page': page + 1, 'page_size': page_size})}",
            **message_context(request),
        },
    )


@router.post("/short-credentials/{credential_id}/revoke")
async def short_credential_revoke(request: Request, credential_id: int) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    try:
        await services.short_credentials.revoke(
            credential_id, session.username, _client_ip(request)
        )
    except NotFound as exc:
        return _not_found(request, exc)
    return RedirectResponse(
        "/admin/short-credentials?msg=short_credential_revoked", status_code=302
    )


def _parse_local_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = datetime.strptime(value, fmt).replace(tzinfo=SHANGHAI)
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _audit_filters(request: Request) -> tuple[dict, dict]:
    params = request.query_params
    raw = {
        "from": params.get("from", ""),
        "to": params.get("to", ""),
        "user_name": params.get("user_name", ""),
        "datasource_name": params.get("datasource_name", ""),
        "status": params.get("status", ""),
        "actor": params.get("actor", ""),
        "q": params.get("q", ""),
    }
    filters = {
        "from": _parse_local_datetime(raw["from"]),
        "to": _parse_local_datetime(raw["to"]),
        "user_name": raw["user_name"].strip() or None,
        "datasource_name": raw["datasource_name"].strip() or None,
        "status": raw["status"].strip() or None,
        "actor": raw["actor"].strip() or None,
        "q": raw["q"].strip() or None,
    }
    return filters, raw


def _audit_page_url(raw: dict, page: int) -> str:
    query = {key: value for key, value in raw.items() if value}
    if page > 1:
        query["page"] = page
    return "/admin/audit" + (f"?{urlencode(query)}" if query else "")


@router.get("/audit")
async def audit_list(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    filters, raw = _audit_filters(request)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    rows, total = await services.audit.query(filters, page=page, page_size=AUDIT_PAGE_SIZE)
    pages = max(1, ceil(total / AUDIT_PAGE_SIZE))
    return _page(
        request,
        "audit.html",
        {
            "session": session,
            "page_title": t("page.audit"),
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "raw": raw,
            "prev_url": _audit_page_url(raw, page - 1) if page > 1 else None,
            "next_url": _audit_page_url(raw, page + 1) if page < pages else None,
        },
    )


@router.get("/audit/{request_id}")
async def audit_detail(request: Request, request_id: str) -> Response:
    services = _services(request)
    session = await _current(request)
    row = await services.audit.get(request_id)
    if row is None:
        return render_error(request, 404, t("error.not_found"), "审计记录不存在")
    return _page(
        request,
        "audit_detail.html",
        {"session": session, "page_title": t("page.audit_detail"), "row": row},
    )


async def _settings_context(services) -> dict:
    """Settings page data: audit retention plus the effective source-IP allowlist of both entrypoints."""
    allowlists = []
    for allowlist in (services.export_allowlist, services.mcp_allowlist):
        allowlists.append(allowlist.view(await allowlist.snapshot()))
    return {
        "retention_days": await services.audit.get_retention_days(),
        "allowlists": allowlists,
        "export_enabled": services.settings.mysql_export_enabled,
    }


async def _settings_page(
    request: Request, services, session, error: str | None = None, status_code: int = 200
) -> Response:
    context = {
        "session": session,
        "page_title": t("page.settings"),
        **(await _settings_context(services)),
    }
    if error is not None:
        context["error"] = error
    else:
        context.update(message_context(request))
    return _page(request, "settings.html", context, status_code=status_code)


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    return await _settings_page(request, services, session)


@router.post("/settings/password")
async def settings_password(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    old_password = str(form.get("old_password") or "")
    new_password = str(form.get("new_password") or "")
    confirm = str(form.get("confirm_password") or "")
    if new_password != confirm:
        return await _settings_page(
            request, services, session, "两次输入的新密码不一致", 400
        )
    try:
        await change_password(services, session.admin_id, old_password, new_password)
    except ValidationError as exc:
        return await _settings_page(request, services, session, str(exc), 400)
    response = RedirectResponse("/admin/login?msg=password_changed", status_code=302)
    clear_session(response)
    return response


@router.post("/settings/retention")
async def settings_retention(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    days = _int_field(form, "retention_days")
    try:
        await services.audit.set_retention_days(days)
    except ValidationError as exc:
        return await _settings_page(request, services, session, str(exc), 400)
    return RedirectResponse("/admin/settings?msg=retention_saved", status_code=302)


@router.post("/settings/allowlist")
async def settings_allowlist(request: Request) -> Response:
    """Source-IP allowlists: validate both entrypoints first, then persist them in a single transaction."""
    services = _services(request)
    session = await _current(request)
    form = await _secured_form(request, session)
    targets = (services.export_allowlist, services.mcp_allowlist)
    missing = [allowlist.label for allowlist in targets if allowlist.setting_key not in form]
    if missing:
        # A missing field must not be treated as empty: that would clear an entrypoint that was never submitted (W7).
        return await _settings_page(
            request,
            services,
            session,
            t("settings.allowlist_missing", fields=t("common.list_separator").join(missing)),
            400,
        )
    try:
        prepared = [
            (allowlist, allowlist.prepare(str(form.get(allowlist.setting_key) or "")))
            for allowlist in targets
        ]
    except ValidationError as exc:
        return await _settings_page(request, services, session, str(exc), 400)

    changed = await save_many(prepared)
    client_ip = _client_ip(request)
    for allowlist, before, after in changed:
        await services.audit.record_setting_change(
            allowlist.setting_key, before, after, session.username, client_ip
        )
    logger.info("管理员 %s 保存来源 IP 白名单，实际变更 %s 个入口", session.username, len(changed))
    return RedirectResponse("/admin/settings?msg=allowlist_saved", status_code=302)


@router.post("/settings/cleanup")
async def settings_cleanup(request: Request) -> Response:
    services = _services(request)
    session = await _current(request)
    await _secured_form(request, session)
    deleted = await services.audit.cleanup_once()
    logger.info("管理员 %s 手动清理过期审计 %s 条", session.username, deleted)
    return RedirectResponse(f"/admin/settings?msg=cleanup_done&n={deleted}", status_code=302)
