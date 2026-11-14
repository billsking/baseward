"""Version assertion: upgrading mysql-mimic must fail hard rather than fall back to the row-dropping original (D29/AC25)."""

from __future__ import annotations

import importlib
import importlib.metadata

import pytest


def test_version_mismatch_fails_at_import(monkeypatch):
    module = importlib.import_module("app.mysql.connection")
    real_version = importlib.metadata.version

    def fake_version(name: str) -> str:
        if name == "mysql-mimic":
            return "3.0.4"
        return real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    try:
        with pytest.raises(RuntimeError, match="3.0.4"):
            importlib.reload(module)
    finally:
        monkeypatch.undo()
        importlib.reload(module)


def test_patches_are_installed_on_connection():
    from mysql_mimic.connection import Connection

    module = importlib.import_module("app.mysql.connection")
    assert Connection.handle_stmt_fetch is module.handle_stmt_fetch
    assert Connection.handle_field_list is module.handle_field_list
