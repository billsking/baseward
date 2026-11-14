"""Logging configuration."""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure stdout logging; fall back to INFO on an invalid level."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    logging.basicConfig(level=numeric_level, format=LOG_FORMAT, force=True)
    # mysql-mimic logs full SQL at DEBUG; export-channel SQL literals must not reach the log.
    logging.getLogger("mysql_mimic").setLevel(logging.WARNING)
