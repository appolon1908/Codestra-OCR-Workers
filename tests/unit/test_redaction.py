from __future__ import annotations

import json
import logging

from codestra_ocr_workers.logging_setup import JsonFormatter
from codestra_ocr_workers.redaction import RedactingFilter, redact

from ..fixtures import DOC_NUMBER, DOC_NUMBER_FMT


def test_redact_numbers_urls_names() -> None:
    text = (
        f"doc {DOC_NUMBER} fmt {DOC_NUMBER_FMT} born 15/03/1990 "
        "name JUANA EJEMPLO at https://licencias.intrantmoto.com/x?id=9 mail a@b.co"
    )
    out = redact(text)
    for secret in (DOC_NUMBER, DOC_NUMBER_FMT, "1990", "JUANA", "EJEMPLO", "intrantmoto", "a@b.co"):
        assert secret not in out
    assert "[NUM]" in out and "[URL]" in out and "[TEXT]" in out and "[EMAIL]" in out


def _format(record: logging.LogRecord) -> dict[str, object]:
    RedactingFilter().filter(record)
    return json.loads(JsonFormatter().format(record))  # type: ignore[no-any-return]


def test_filter_scrubs_args_extras_and_exceptions() -> None:
    try:
        raise ValueError(f"ocr text {DOC_NUMBER}")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "value %s", (DOC_NUMBER,), exc_info)
    record.request_id = "req-1"
    record.ocr_text = f"JUANA EJEMPLO {DOC_NUMBER}"
    record.duration_ms = 1234.5
    payload = _format(record)
    blob = json.dumps(payload) + str(record.__dict__)
    assert DOC_NUMBER not in blob
    assert "JUANA" not in blob
    assert payload["request_id"] == "req-1"
    assert payload["duration_ms"] == 1234.5
    assert payload["error"] == "ValueError: [redacted]"
