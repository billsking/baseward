"""The business pool must open every session with autocommit off: engine.begin() is the only rollback path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.datasources import pool as pool_module


@pytest.fixture
def captured_engine(monkeypatch):
    captured: dict = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(pool_module, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(pool_module, "decrypt_secret", lambda cipher, key: "pw")
    return captured


def _datasource() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        host="db.example.com",
        port=3306,
        username="reader",
        password_cipher="cipher",
        default_database="demo",
    )


async def test_pool_disables_autocommit_at_connect(captured_engine):
    pool = pool_module.DataSourcePool(
        Settings(_env_file=None, db_host="127.0.0.1", db_user="u", db_password="p")
    )
    await pool.get(_datasource())
    connect_args = captured_engine["kwargs"]["connect_args"]
    assert connect_args["init_command"] == "SET autocommit=0"
    assert connect_args["connect_timeout"] == pool_module.CONNECT_TIMEOUT_SECONDS