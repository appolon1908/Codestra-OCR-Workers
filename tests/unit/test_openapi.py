from __future__ import annotations

import json
from pathlib import Path

from codestra_ocr_workers.openapi_export import render

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "openapi.json"


def test_committed_openapi_matches_exactly() -> None:
    committed = SPEC_PATH.read_text(encoding="utf-8")
    assert render() == committed, (
        "openapi/openapi.json is stale; regenerate with `codestra-ocr-export-openapi`"
    )


def test_openapi_surface() -> None:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert set(spec["paths"]) == {
        "/healthz",
        "/readyz",
        "/health/ready",
        "/metrics",
        "/v1/capabilities",
        "/internal/v1/ocr/extract",
        "/v1/ocr/extract",
    }
    extract = spec["paths"]["/internal/v1/ocr/extract"]["post"]
    assert set(extract["responses"]) == {"200", "401", "413", "422", "502", "503", "504"}
    request = spec["components"]["schemas"]["ExtractRequest"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {
        "scan_id",
        "tenant_id",
        "document_type",
        "country",
        "schema_version",
        "images",
    }
    di = spec["paths"]["/v1/ocr/extract"]["post"]
    assert di["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/DIExtractRequest"
    )
    assert "HTTPValidationError" not in json.dumps(spec)
