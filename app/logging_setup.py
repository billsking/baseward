"""Logging setup: one root handler, optional JSON records, and a request-id on every line."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.observability import RequestIdFilter

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
LOG_FORMATS = frozenset({"text", "json"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line for log shippers; keeps the human-readable text format as default."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_format: str = "text") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = (
        JsonFormatter() if log_format == "json" else logging.Formatter(LOG_FORMAT)
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)
    # mysql-mimic logs every statement it receives, including the SQL text; keep it quiet.
    logging.getLogger("mysql_mimic").setLevel(logging.WARNING)
