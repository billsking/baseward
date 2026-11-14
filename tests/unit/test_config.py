"""Settings validation unit tests."""

import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import ConfigError

VALID_KEY = base64.b64encode(b"k" * 32).decode("ascii")


def make_settings(**overrides) -> Settings:
    data = {
        "master_key": VALID_KEY,
        "db_host": "127.0.0.1",
        "db_user": "gateway",
        "db_password": "secret",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)


def test_valid_settings() -> None:
    settings = make_settings()
    assert settings.db_port == 3306
    assert settings.db_name == "db_gateway"
    assert settings.audit_retention_days == 180
    assert settings.master_key_bytes == b"k" * 32


def test_db_url_is_built_without_string_concat() -> None:
    settings = make_settings(db_password="p@ss:word")
    url = settings.management_url
    assert url.drivername == "mysql+aiomysql"
    assert url.password == "p@ss:word"
    assert url.query.get("charset") == "utf8mb4"


def test_missing_db_configuration_falls_back_to_sqlite(monkeypatch, tmp_path) -> None:
    for key in (
        "GATEWAY_DB_URL",
        "GATEWAY_DB_HOST",
        "GATEWAY_DB_USER",
        "GATEWAY_DB_PASSWORD",
        "GATEWAY_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None, master_key=VALID_KEY, db_path=str(tmp_path / "gw.db"))
    url = settings.management_url
    assert url.drivername == "sqlite+aiosqlite"
    assert Path(url.database) == (tmp_path / "gw.db").resolve()
    assert settings.management_backend == "sqlite"


def test_db_url_upgrades_plain_schemes_to_async_drivers() -> None:
    settings = make_settings(db_url="mysql://gateway:secret@127.0.0.1:3306/gw")
    url = settings.management_url
    assert url.drivername == "mysql+aiomysql"
    assert url.query.get("charset") == "utf8mb4"

    sqlite = Settings(_env_file=None, master_key=VALID_KEY, db_url="sqlite:///tmp/gw.db")
    assert sqlite.management_url.drivername == "sqlite+aiosqlite"

    postgres = Settings(
        _env_file=None, master_key=VALID_KEY, db_url="postgresql://gateway:secret@127.0.0.1:5432/gw"
    )
    assert postgres.management_url.drivername == "postgresql+asyncpg"

    explicit = Settings(
        _env_file=None,
        master_key=VALID_KEY,
        db_url="postgresql+psycopg://gateway:secret@127.0.0.1:5432/gw",
    )
    assert explicit.management_url.drivername == "postgresql+psycopg"


def test_partial_db_host_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_DB_USER", raising=False)
    monkeypatch.delenv("GATEWAY_DB_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        Settings(_env_file=None, master_key=VALID_KEY, db_host="127.0.0.1")


def test_missing_master_key_rejected(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_host="127.0.0.1", db_user="u", db_password="p")


def test_master_key_wrong_length_rejected() -> None:
    short_key = base64.b64encode(b"k" * 16).decode("ascii")
    with pytest.raises(ValidationError):
        make_settings(master_key=short_key)


def test_master_key_not_base64_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(master_key="not-base64!!")


@pytest.mark.parametrize("days", [0, 3651, -1])
def test_retention_out_of_range_rejected(days: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(audit_retention_days=days)
