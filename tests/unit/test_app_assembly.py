"""Application assembly smoke test (no database connection)."""

import base64

import httpx

from app.config import Settings
from app.main import create_app


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        master_key=base64.b64encode(b"k" * 32).decode("ascii"),
        db_host="127.0.0.1",
        db_user="test",
        db_password="test",
    )


def test_app_assembles_routes_and_mcp_mount() -> None:
    app = create_app(make_settings())
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/healthz" in paths
    assert "/mcp" in paths
    assert "/" in paths
    # FastAPI 0.159 stores include_router as _IncludedRouter; admin routes are verified through a request smoke test.
    assert any(type(route).__name__ == "_IncludedRouter" for route in app.routes)


async def test_unauthenticated_admin_returns_401_login_page() -> None:
    app = create_app(make_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/datasources")
    assert response.status_code == 401
    assert "请使用管理员账号登录" in response.text

    # Static assets skip authentication so the login page can load styles.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        static = await client.get("/admin/static/app.css")
    assert static.status_code == 200
    assert "--primary" in static.text
    # The component's display rule overrides the UA [hidden]; without the fallback, hidden stops working.
    assert "[hidden] { display: none !important; }" in static.text

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.get("/admin/login")
    assert login.status_code == 200
    assert 'action="/admin/login"' in login.text
