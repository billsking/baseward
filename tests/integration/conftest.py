"""Integration fixtures: temporary management database + temporary business databases + the full app.

Requires .env.test in the project root (see .env.test.example). The test account must have
CREATE DATABASE / DROP DATABASE; every temporary database starts with TEST_DB_PREFIX and is
dropped when the test ends.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.main import create_app
from app.netallow import EXPORT_ALLOW_KEY, IpAllowlist
from app.tokens.scope import TokenScope

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env.test"
BASE_URL = "http://test"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")

USERS_DDL = """
CREATE TABLE users (
  id INT NOT NULL AUTO_INCREMENT,
  phone VARCHAR(32) NULL COMMENT '手机号',
  mobile VARCHAR(32) NULL,
  email VARCHAR(128) NULL,
  password VARCHAR(128) NULL,
  real_name VARCHAR(64) NULL,
  address VARCHAR(255) NULL,
  note VARCHAR(255) NULL,
  amount DECIMAL(10,2) NULL,
  created_at DATETIME(3) NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户信息'
"""

ORDERS_DDL = """
CREATE TABLE orders (
  id INT NOT NULL AUTO_INCREMENT,
  user_id INT NOT NULL,
  order_no VARCHAR(32) NOT NULL COMMENT '订单号',
  amount DECIMAL(10,2) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单记录'
"""

USERS = [
    (
        1,
        "13800138000",
        "13900139000",
        "alice@example.com",
        "secret123",
        "张三",
        "北京市朝阳区建国路1号",
        "正常客户",
        "12.34",
    ),
    (2, None, "13700137000", "b@example.com", "p2", "李四", None, "无手机号", "99.90"),
    (3, "13600136000", None, None, "p3", "王五", "上海市浦东新区", None, "0.00"),
]

ORDERS = [
    (1, 1, "SO-0001", "10.00"),
    (2, 1, "SO-0002", "20.00"),
    (3, 2, "SO-0003", "30.00"),
]


@dataclass
class GatewayEnv:
    app: Any
    settings: Settings
    client: httpx.AsyncClient
    token: str
    ds_name: str
    biz_db: str
    mgmt_db: str
    admin_user: str
    admin_password: str
    db_host: str
    db_port: int
    db_user: str
    db_password: str

    @property
    def services(self):
        return self.app.state.services

    def mcp_http_client_factory(self):
        transport = httpx.ASGITransport(app=self.app)

        def factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=transport,
                base_url=BASE_URL,
                headers=headers,
                timeout=timeout,
                auth=auth,
            )

        return factory


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@pytest.fixture(scope="session")
def test_config() -> dict[str, str]:
    if not ENV_FILE.exists():
        pytest.skip("缺少 .env.test（参考 .env.test.example），跳过集成测试")
    values = _parse_env_file(ENV_FILE)
    required = ("TEST_DB_HOST", "TEST_DB_USER", "TEST_DB_PASSWORD")
    missing = [key for key in required if not values.get(key)]
    if missing:
        pytest.fail(f".env.test 缺少必填项：{', '.join(missing)}")
    config = {
        "host": values["TEST_DB_HOST"],
        "port": int(values.get("TEST_DB_PORT", "3306")),
        "user": values["TEST_DB_USER"],
        "password": values["TEST_DB_PASSWORD"],
        "prefix": values.get("TEST_DB_PREFIX", "dbgw_test"),
        "admin_user": values.get("TEST_ADMIN_USER", "admin"),
        "admin_password": values.get("TEST_ADMIN_PASSWORD", "admin-test-123"),
    }
    if not IDENTIFIER_RE.fullmatch(config["prefix"]):
        pytest.fail("TEST_DB_PREFIX 只能包含字母、数字与下划线")
    if len(config["admin_password"]) < 8:
        pytest.fail("TEST_ADMIN_PASSWORD 至少 8 字符")
    return config


def _url(config: dict[str, Any], database: str | None = None) -> URL:
    return URL.create(
        "mysql+aiomysql",
        username=config["user"],
        password=config["password"],
        host=config["host"],
        port=config["port"],
        database=database,
        query={"charset": "utf8mb4"},
    )


async def _create_database(engine, name: str) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )


async def _seed_business_database(engine, biz_db: str) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(USERS_DDL)
        await conn.exec_driver_sql(ORDERS_DDL)
        await conn.exec_driver_sql(
            "INSERT INTO users (id, phone, mobile, email, password, real_name, address, note, amount, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(3))",
            USERS,
        )
        await conn.exec_driver_sql(
            "INSERT INTO orders (id, user_id, order_no, amount) VALUES (%s, %s, %s, %s)",
            ORDERS,
        )


async def _drop_databases(engine, names: list[str]) -> None:
    for name in names:
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        except Exception:  # noqa: BLE001 - log cleanup failures without masking the test result
            logger.exception("清理临时数据库失败：%s", name)


@pytest.fixture
async def env(test_config) -> GatewayEnv:
    config = test_config
    suffix = secrets.token_hex(4)
    prefix = config["prefix"][:36]
    mgmt_db = f"{prefix}_{suffix}"
    biz_db = f"{prefix}_biz_{suffix}"

    admin_engine = create_async_engine(
        _url(config), pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )
    created: list[str] = []
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await _create_database(admin_engine, mgmt_db)
        created.append(mgmt_db)
        await _create_database(admin_engine, biz_db)
        created.append(biz_db)

        biz_engine = create_async_engine(
            _url(config, biz_db), connect_args={"connect_timeout": 5}
        )
        try:
            await _seed_business_database(biz_engine, biz_db)
        finally:
            await biz_engine.dispose()

        settings = Settings(
            _env_file=None,
            master_key=base64.b64encode(b"t" * 32).decode("ascii"),
            admin_user=config["admin_user"],
            admin_password=config["admin_password"],
            db_host=config["host"],
            db_port=config["port"],
            db_user=config["user"],
            db_password=config["password"],
            db_name=mgmt_db,
            log_level="WARNING",
        )
        app = create_app(settings)
        # MySQL over a remote/WSL host makes schema replay slow enough to exceed the 5s default.
        async with LifespanManager(app, startup_timeout=30, shutdown_timeout=30):
            services = app.state.services
            await services.datasources.create(
                name="testds",
                host=config["host"],
                port=config["port"],
                username=config["user"],
                password=config["password"],
                default_database=biz_db,
                remark="集成测试数据源",
            )
            token, _ = await services.tokens.create("tester")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL, follow_redirects=False
            ) as client:
                yield GatewayEnv(
                    app=app,
                    settings=settings,
                    client=client,
                    token=token,
                    ds_name="testds",
                    biz_db=biz_db,
                    mgmt_db=mgmt_db,
                    admin_user=config["admin_user"],
                    admin_password=config["admin_password"],
                    db_host=config["host"],
                    db_port=config["port"],
                    db_user=config["user"],
                    db_password=config["password"],
                )
    finally:
        await _drop_databases(admin_engine, list(reversed(created)))
        await admin_engine.dispose()


def _engine_for(env: GatewayEnv, database: str | None = None):
    return create_async_engine(
        _url(
            {
                "user": env.db_user,
                "password": env.db_password,
                "host": env.db_host,
                "port": env.db_port,
            },
            database,
        ),
        connect_args={"connect_timeout": 5},
    )


def business_engine(env: GatewayEnv):
    return _engine_for(env, env.biz_db)


@asynccontextmanager
async def extra_database(
    env: GatewayEnv,
    suffix: str,
    rows: list[tuple[int, str | None]],
    extra_sql: str | None = None,
):
    """Create a temporary database with the same schema as biz and insert users rows; drop it on exit.

    rows provide only (id, phone) and leave other columns empty; extra_sql creates schema differences between databases.
    """
    name = f"{env.biz_db}_{suffix}"
    admin = _engine_for(env)
    try:
        async with admin.begin() as conn:
            await conn.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        engine = _engine_for(env, name)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(USERS_DDL)
                if extra_sql:
                    await conn.exec_driver_sql(extra_sql)
                for row in rows:
                    await conn.exec_driver_sql(
                        "INSERT INTO users (id, phone, mobile, email, password) "
                        "VALUES (%s, %s, NULL, NULL, NULL)",
                        row,
                    )
        finally:
            await engine.dispose()
        yield name
    finally:
        try:
            async with admin.begin() as conn:
                await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        finally:
            await admin.dispose()


async def create_datasource(
    env: GatewayEnv,
    name: str,
    databases: list[str],
    default_database: str | None = None,
):
    return await env.services.datasources.create(
        name=name,
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=default_database or env.biz_db,
        databases=databases,
        remark="多库导出测试",
    )


@asynccontextmanager
async def mcp_session(env: GatewayEnv, token: str | None = None, extra_headers: dict | None = None):
    headers = {"Authorization": f"Bearer {token or env.token}"}
    if extra_headers:
        headers.update(extra_headers)
    async with streamablehttp_client(
        f"{BASE_URL}/mcp",
        headers=headers,
        httpx_client_factory=env.mcp_http_client_factory(),
        timeout=30,
        sse_read_timeout=30,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def tool_payload(result) -> dict:
    text_content = None
    for item in result.content:
        if getattr(item, "type", None) == "text":
            text_content = item.text
            break
    assert text_content is not None, f"工具未返回文本内容：{result}"
    text = text_content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # FastMCP wraps tool exceptions as "Error executing tool <name>: {json}".
        start = text.find("{")
        end = text.rfind("}")
        assert start != -1 and end > start, f"工具错误不是 JSON 载荷：{text_content}"
        return json.loads(text[start : end + 1])


async def call_tool(session: ClientSession, name: str, arguments: dict) -> tuple[bool, dict]:
    result = await session.call_tool(name, arguments)
    return bool(getattr(result, "isError", False)), tool_payload(result)


async def admin_login(env: GatewayEnv) -> str:
    response = await env.client.post(
        "/admin/login",
        data={"username": env.admin_user, "password": env.admin_password},
    )
    assert response.status_code == 302, response.text
    dashboard = await env.client.get("/admin")
    assert dashboard.status_code == 200
    match = re.search(r'name="_csrf" value="([^"]+)"', dashboard.text)
    assert match is not None, "仪表盘缺少 CSRF Token"
    return match.group(1)


def csrf_from(html: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match is not None, "页面缺少 CSRF Token"
    return match.group(1)


async def management_scalar(env: GatewayEnv, sql: str, params: dict | None = None):
    async with env.services.session_factory() as session:
        result = await session.execute(text(sql), params or {})
        return result.scalar()


# --- Export channel (v1.4) test helpers -----------------------------------------


def export_settings(env: GatewayEnv, **overrides) -> Settings:
    data = env.settings.model_dump()
    data.update({"mysql_export_enabled": True, "mysql_export_allow_cidrs": "127.0.0.1/32"})
    data.update(overrides)
    return Settings(_env_file=None, **data)


def export_allowlist(env: GatewayEnv, settings: Settings) -> IpAllowlist:
    """Build an allowlist checker from the given settings env values (does not share the Services cache)."""
    return IpAllowlist(
        env.services.session_factory,
        EXPORT_ALLOW_KEY,
        "MySQL 导出通道",
        settings.mysql_export_allow_cidrs,
    )


async def issue_credential(
    env: GatewayEnv,
    ttl_hours: int = 1,
    *,
    datasource_id: int | None = None,
    token_id: int | None = None,
    database: str | None = None,
) -> tuple[str, str]:
    if datasource_id is None:
        datasource = await env.services.datasources.get_by_name(env.ds_name)
        assert datasource is not None
    else:
        datasource = await env.services.datasources.get(datasource_id)
    if token_id is None:
        token_id = (await env.services.tokens.list_all())[0].id
    return await env.services.credentials.issue(
        token_id=token_id,
        datasource_id=datasource.id,
        database=database or datasource.default_database,
        ttl_hours=ttl_hours,
        admin_username="admin",
        client_ip="127.0.0.1",
    )


@dataclass
class MultiDbExport:
    datasource: Any
    view_db: str
    denied_db: str
    account: str
    password: str


@asynccontextmanager
async def multi_db_export(
    env: GatewayEnv,
    *,
    view_rows: list[tuple[int, str | None]],
    denied_rows: list[tuple[int, str | None]],
    view_extra_sql: str | None = None,
):
    """Export-channel three-database fixture: biz (default), view (usable by the token), denied (exposed but unauthorized)."""
    async with extra_database(env, "view", view_rows, view_extra_sql) as view_db:
        async with extra_database(env, "denied", denied_rows) as denied_db:
            datasource = await create_datasource(
                env, "export-multi", [env.biz_db, view_db, denied_db]
            )
            try:
                scope = TokenScope(
                    datasources=frozenset({datasource.id}),
                    unrestricted_databases=frozenset(
                        {(datasource.id, env.biz_db), (datasource.id, view_db)}
                    ),
                )
                _, token_row = await env.services.tokens.create("export-multi", scope)
                account, password = await issue_credential(
                    env, datasource_id=datasource.id, token_id=token_row.id
                )
                yield MultiDbExport(datasource, view_db, denied_db, account, password)
            finally:
                await env.services.datasources.delete(datasource.id)


@asynccontextmanager
async def export_server(env: GatewayEnv, settings: Settings | None = None):
    """Start only the export-channel mimic server (without assembling the whole app), yielding (port, limiter).

    `app.mysql.*` is imported lazily right here: a process with `enabled=false` should not contain these modules.
    """
    from app.mysql.control import ExportControl, ExportLimiter
    from app.mysql.identity import CredentialIdentityProvider, ExportServerContext
    from app.mysql.server import ExportMysqlServer
    from app.mysql.session import ExportSession

    settings = settings or export_settings(env)
    context = ExportServerContext()
    limiter = ExportLimiter(settings.mysql_export_max_concurrent)
    server = ExportMysqlServer(
        session_factory=lambda: ExportSession(env.services, settings, context, limiter),
        control=ExportControl(env.services, export_allowlist(env, settings)),
        identity_provider=CredentialIdentityProvider(env.services, settings, context),
        host="127.0.0.1",
        port=0,
    )
    await server.start_server()
    port = server.sockets()[0].getsockname()[1]
    try:
        yield port, limiter
    finally:
        server.close()
        await server.wait_closed()


def connect_export(port: int, account: str, password: str, cursorclass=None):
    import pymysql

    kwargs = {}
    if cursorclass is not None:
        kwargs["cursorclass"] = cursorclass
    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user=account,
        password=password,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,
        **kwargs,
    )


async def wait_deny_reason(
    env: GatewayEnv,
    code: str,
    event_type: str = "mysql_auth",
    timeout: float = 5.0,
    database_name: str | None = None,
) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    sql = (
        "SELECT COUNT(*) FROM audit_log "
        "WHERE event_type=:event_type AND status='denied' AND deny_reason=:code"
    )
    params = {"event_type": event_type, "code": code}
    if database_name is not None:
        sql += " AND database_name=:database_name"
        params["database_name"] = database_name
    while True:
        count = await management_scalar(env, sql, params)
        if count:
            return
        if loop.time() >= deadline:
            raise AssertionError(f"未等到审计 deny_reason={code} database={database_name}")
        await asyncio.sleep(0.05)


async def wait_success_database(
    env: GatewayEnv,
    database: str,
    event_type: str = "mysql_export",
    timeout: float = 5.0,
) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    sql = (
        "SELECT COUNT(*) FROM audit_log "
        "WHERE event_type=:event_type AND status='success' AND database_name=:database"
    )
    params = {"event_type": event_type, "database": database}
    while True:
        count = await management_scalar(env, sql, params)
        if count:
            return
        if loop.time() >= deadline:
            raise AssertionError(f"未等到审计 database_name={database}")
        await asyncio.sleep(0.05)
