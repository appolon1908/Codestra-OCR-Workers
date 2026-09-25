"""Structured JSON logging with mandatory redaction."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from .redaction import RedactingFilter

_EXTRA_KEYS = (
    "request_id",
    "duration_ms",
    "status_code",
    "outcome",
    "engine",
    "document_type",
    "country",
    "path",
    "method",
    "fields_extracted",
    "warnings",
    "images",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRA_KEYS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_text:
            payload["error"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # Access logs duplicate our own request log line.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
