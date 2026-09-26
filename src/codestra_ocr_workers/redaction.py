"""Redaction helpers so OCR output never reaches logs in clear text.

Policy: log shapes (lengths, counts, field names, confidences), never values. As a
second line of defence the log filter below scrubs digit runs, URLs, e-mail addresses
and long uppercase runs (typical OCR'd names/addresses) from every log record.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_DIGITS = re.compile(r"\d[\d\s\-./]{2,}\d|\d{3,}")
_URL = re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_BASE64ISH = re.compile(r"[A-Za-z0-9+/=]{40,}")
_UPPER_RUN = re.compile(r"\b[A-ZÁÉÍÓÚÑÜ]{2,}(?:\s+[A-ZÁÉÍÓÚÑÜ]{2,}){1,}\b")


def redact(text: str) -> str:
    """Mask sensitive-looking substrings in free text."""
    text = _URL.sub("[URL]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _BASE64ISH.sub("[BLOB]", text)
    text = _DIGITS.sub("[NUM]", text)
    return _UPPER_RUN.sub("[TEXT]", text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) >= 1000:
        return "[NUM]"
    return value


class RedactingFilter(logging.Filter):
    """Scrubs the formatted message and any string ``extra`` attributes."""

    SAFE_ATTRS = frozenset(
        {
            "name",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "funcName",
            "lineno",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "process",
            "processName",
            "taskName",
            "stack_info",
            "exc_info",
            "exc_text",
            "msg",
            "args",
            "request_id",
            "duration_ms",
            "status_code",
            "outcome",
            "engine",
            "document_type",
            "country",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format strings
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Exception messages may embed OCR text; keep only the type name.
            record.exc_text = f"{type(record.exc_info[1]).__name__}: [redacted]"
            record.exc_info = None
        for key, value in list(record.__dict__.items()):
            if key not in self.SAFE_ATTRS:
                record.__dict__[key] = _redact_value(value)
        return True
