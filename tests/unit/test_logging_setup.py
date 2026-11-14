"""Logging setup: quiet mysql-mimic without following global DEBUG."""

from __future__ import annotations

import logging

import pytest

from app.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    handlers = list(root.handlers)
    root_level = root.level
    mim_level = logging.getLogger("mysql_mimic").level
    yield
    root.handlers[:] = handlers
    root.setLevel(root_level)
    logging.getLogger("mysql_mimic").setLevel(mim_level)


def test_debug_level_keeps_mysql_mimic_at_warning(caplog):
    configure_logging("DEBUG")
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("mysql_mimic").getEffectiveLevel() == logging.WARNING

    logging.getLogger("mysql_mimic").debug("Received query: SELECT secret FROM t")
    assert "Received query" not in caplog.text


def test_invalid_level_falls_back_to_info():
    configure_logging("not-a-level")
    assert logging.getLogger().getEffectiveLevel() == logging.INFO
    assert logging.getLogger("mysql_mimic").getEffectiveLevel() == logging.WARNING
