from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ..conftest import ClientFactory, assert_valid
from ..fixtures import (
    AUTHORITY_URL,
    DOC_NUMBER,
    FRONT_TEXT,
    FakeEngine,
    b64,
    blank_card,
    card_with_qr,
    extract_body,
    text_page,
)

EXTRACT = "/internal/v1/ocr/extract"
LICENCE_SCHEMA = "codestra-document-schemas/v1/do-driver-licence.schema.json"
ERROR_SCHEMA = "codestra-document-schemas/v1/error.schema.json"


def test_healthz(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/readyz", "/health/ready"])
def test_readiness(client_factory: ClientFactory, path: str) -> None:
    ok = client_factory().get(path)
    assert ok.status_code == 200
    assert ok.json() == {
        "status": "ready",
        "checks": {"engine:fake": {"ok": True, "detail": "fake ready"}},
    }
    bad = client_factory(FakeEngine(ready=False)).get(path)
    assert bad.status_code == 503
    assert bad.json()["status"] == "not_ready"


def test_capabilities(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["engines"][0] == {
        "name": "fake",
        "version": "0.0-test",
        "languages": ["spa", "eng"],
        "capabilities": ["word_confidence"],
        "ready": True,
        "default": True,
    }
    doc = body["documents"][0]
    assert (doc["country"], doc["document_type"]) == ("DO", "driver_license")
    assert doc["document_type_aliases"] == ["driver_licence"]
    assert doc["schema_versions"] == ["1.0.0"]
    assert doc["schema_id"].endswith("/v1/do-driver-licence.schema.json")
    fields = {f["name"]: f for f in doc["fields"]}
    assert fields["document_number"]["sensitivity"] == "restricted"
    assert fields["document_number"]["required"] is True
    assert fields["height"]["unit"] == "m"
    assert {c["endpoint"] for c in body["contracts"]} == {
        "/internal/v1/ocr/extract",
        "/v1/ocr/extract",
    }
    assert body["qr"]["fetches_urls"] is False
    assert body["limits"]["max_images"] == 2


def test_extract_happy_path(client: TestClient) -> None:
    res = client.post(EXTRACT, json=extract_body(request_id="req-123"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert res.headers["x-request-id"] == "req-123"
    assert body["request_id"] == "req-123"
    assert body["status"] == "succeeded"
    assert body["review_required"] is False
    assert body["retention"] == "transient"
    result = body["result"]
    assert_valid(LICENCE_SCHEMA, result)
    assert result["scan_id"] == "scan-0001"
    assert result["tenant_id"] == "tenant-test"
    assert result["document_type"] == "driver_license"
    assert result["engine"] == {"name": "fake", "version": "0.0-test"}
    assert result["fields"]["document_number"] == DOC_NUMBER
    assert result["fields"]["height"] == 1.68
    assert result["fields"]["weight_lb"] == 140.0
    assert result["fields"]["restriction"] == "LENTES"
    ev = result["field_evidence"]["document_number"][0]
    assert ev["source"] == "ocr"
    assert ev["page"] == 1
    assert len(ev["bbox"]) == 4
    assert ev["bbox"][0] <= ev["bbox"][2]
    assert ev["bbox"][1] <= ev["bbox"][3]
    assert result["field_evidence"]["card_serial"][0]["page"] == 2
    assert result["qr_evidence"]["status"] == "decoded"
    assert result["qr_evidence"]["raw_payload"] == AUTHORITY_URL
    assert result["qr_evidence"]["payload_digest"] == (
        "sha256:" + hashlib.sha256(AUTHORITY_URL.encode()).hexdigest()
    )
    assert [d["role"] for d in result["content_digests"]] == ["front", "back"]
    assert result["warnings"] == []
    diag = body["diagnostics"]
    assert diag["qr"]["allowlisted"] is True
    assert diag["qr"]["fetched"] is False
    assert diag["engine"]["languages"] == ["spa", "eng"]
    assert diag["fields"]["document_number"]["sensitivity"] == "restricted"
    assert diag["fields"]["document_number"]["checks"]["luhn"] is True
    assert [p["side"] for p in diag["pages"]] == ["front", "back"]
    assert diag["pages"][0]["preprocessing"][0] == "variant:clahe"
    assert set(diag["timings"]) == {"decode_ms", "ocr_ms", "qr_ms", "extract_ms", "total_ms"}
    # No raw OCR text anywhere in the response.
    assert "REPUBLICA" not in res.text


def test_content_digests_match_input_bytes(client: TestClient) -> None:
    body = extract_body()
    res = client.post(EXTRACT, json=body).json()
    expected = [
        "sha256:" + hashlib.sha256(base64.b64decode(img["content_base64"])).hexdigest()
        for img in body["images"]  # type: ignore[attr-defined]
    ]
    assert [d["digest"] for d in res["result"]["content_digests"]] == expected


def test_sha256_mismatch_rejected(client: TestClient) -> None:
    body = extract_body(images=[{"content_base64": b64(blank_card()), "sha256": "0" * 64}])
    res = client.post(EXTRACT, json=body)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "digest_mismatch"


def test_british_spelling_alias_is_normalised(client: TestClient) -> None:
    res = client.post(EXTRACT, json=extract_body(document_type="driver_licence"))
    assert res.status_code == 200
    assert res.json()["result"]["document_type"] == "driver_license"


def test_non_allowlisted_qr_hides_payload(client: TestClient) -> None:
    images = [
        {"side": "front", "content_base64": b64(blank_card())},
        {"side": "back", "content_base64": b64(card_with_qr("https://evil.example/x"))},
    ]
    res = client.post(EXTRACT, json=extract_body(images=images)).json()
    assert_valid(LICENCE_SCHEMA, res["result"])
    assert "raw_payload" not in res["result"]["qr_evidence"]
    assert {"code": "QR_MISMATCH", "severity": "warning"} in res["result"]["warnings"]
    assert res["diagnostics"]["qr"]["authority_host"] == "evil.example"
    assert res["review_required"] is True


def test_unknown_sides_assigned_front_then_back(client: TestClient) -> None:
    body = extract_body(
        images=[
            {"content_base64": b64(blank_card())},
            {"content_base64": b64(card_with_qr(AUTHORITY_URL))},
        ]
    )
    res = client.post(EXTRACT, json=body).json()
    assert [p["side"] for p in res["diagnostics"]["pages"]] == ["front", "back"]
    assert res["result"]["qr_evidence"]["status"] == "decoded"


def test_qr_disabled(client: TestClient) -> None:
    res = client.post(EXTRACT, json=extract_body(options={"decode_qr": False})).json()
    assert res["result"]["qr_evidence"] == {"status": "not_detected"}
    assert res["diagnostics"]["qr"]["scanned"] is False
    assert_valid(LICENCE_SCHEMA, res["result"])


def test_partial_when_required_missing(client_factory: ClientFactory) -> None:
    engine = FakeEngine([text_page("nothing useful here")])
    res = client_factory(engine).post(EXTRACT, json=extract_body()).json()
    assert res["status"] == "partial"
    assert res["review_required"] is True
    result = res["result"]
    assert_valid(LICENCE_SCHEMA, result)
    assert set(result["fields"].values()) == {None}
    assert result["field_evidence"] == {}
    codes = {(w["code"], w.get("field")) for w in result["warnings"]}
    assert ("MISSING_FIELD", "full_name") in codes
    assert ("REVIEW_REQUIRED", None) in codes
    assert res["diagnostics"]["fields"]["full_name"]["status"] == "missing"


def test_low_confidence_triggers_otsu_retry(client_factory: ClientFactory) -> None:
    engine = FakeEngine(
        [text_page(FRONT_TEXT, confidence=0.3), text_page(FRONT_TEXT, confidence=0.8)]
    )
    res = client_factory(engine).post(
        EXTRACT, json=extract_body(images=[{"content_base64": b64(blank_card())}])
    )
    assert engine.calls == 2
    page = res.json()["diagnostics"]["pages"][0]
    assert page["attempts"] == 2
    assert page["preprocessing"][0] == "variant:otsu"


@pytest.mark.parametrize(
    ("override", "status", "code", "contract_code"),
    [
        ({"country": "US"}, 422, "unsupported_document", "UNSUPPORTED_DOCUMENT"),
        ({"document_type": "passport"}, 422, "unsupported_document", "UNSUPPORTED_DOCUMENT"),
        ({"schema_version": "9.9.9"}, 422, "unsupported_schema_version", "UNSUPPORTED_DOCUMENT"),
        ({"options": {"engine": "paddleocr"}}, 503, "engine_unavailable", "EXTRACTION_FAILED"),
        ({"country": "do"}, 422, "invalid_request", "INVALID_INPUT"),
        ({"unexpected": 1}, 422, "invalid_request", "INVALID_INPUT"),
        ({"images": []}, 422, "invalid_request", "INVALID_INPUT"),
        ({"scan_id": "bad id"}, 422, "invalid_request", "INVALID_INPUT"),
    ],
)
def test_request_rejections(
    client: TestClient, override: dict[str, object], status: int, code: str, contract_code: str
) -> None:
    res = client.post(EXTRACT, json=extract_body(**override))
    assert res.status_code == status, res.text
    err = res.json()["error"]
    assert err["code"] == code
    assert err["contract_code"] == contract_code
    # The error block carries everything needed for an error.schema.json message.
    assert_valid(
        ERROR_SCHEMA,
        {
            "scan_id": "scan-0001",
            "tenant_id": "tenant-test",
            "schema_version": "1.0.0",
            "code": err["contract_code"],
            "stage": err["stage"],
            "retryable": err["retryable"],
            "occurred_at": "2026-09-25T00:00:00Z",
        },
    )


def test_validation_error_does_not_echo_input(client: TestClient) -> None:
    body = extract_body(images=[{"side": "sideways", "content_base64": "SECRETSECRET" * 3}])
    res = client.post(EXTRACT, json=body)
    assert res.status_code == 422
    assert "SECRETSECRET" not in res.text


def test_too_many_images(client: TestClient) -> None:
    img = {"content_base64": b64(blank_card())}
    res = client.post(EXTRACT, json=extract_body(images=[img, img, img]))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "too_many_images"


def test_invalid_image(client: TestClient) -> None:
    img = {"content_base64": base64.b64encode(b"not an image at all").decode()}
    res = client.post(EXTRACT, json=extract_body(images=[img]))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_image"


def test_image_byte_limit(client_factory: ClientFactory) -> None:
    client = client_factory(max_image_bytes=2048)
    res = client.post(EXTRACT, json=extract_body())
    assert res.status_code == 413
    err = res.json()["error"]
    assert err["code"] == "image_too_large"
    assert err["contract_code"] == "INVALID_INPUT"
    assert err["stage"] == "ingest"
    assert err["retryable"] is False


def test_request_body_limit(client_factory: ClientFactory) -> None:
    client = client_factory(max_request_bytes=4096)
    res = client.post(EXTRACT, json=extract_body())
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "payload_too_large"
    # Chunked upload without Content-Length is also bounded.
    payload = json.dumps(extract_body()).encode()

    def chunks():  # type: ignore[no-untyped-def]
        for i in range(0, len(payload), 1024):
            yield payload[i : i + 1024]

    res = client.post(EXTRACT, content=chunks(), headers={"content-type": "application/json"})
    assert res.status_code == 413


def test_internal_token(client_factory: ClientFactory) -> None:
    client = client_factory(internal_token="s3cret-token")
    assert client.post(EXTRACT, json=extract_body()).status_code == 401
    assert client.get("/v1/capabilities").status_code == 401
    assert client.get("/v1/capabilities", headers={"X-Internal-Token": "wrong"}).status_code == 401
    ok = client.post(EXTRACT, json=extract_body(), headers={"X-Internal-Token": "s3cret-token"})
    assert ok.status_code == 200
    bearer = client.post(
        EXTRACT, json=extract_body(), headers={"Authorization": "Bearer s3cret-token"}
    )
    assert bearer.status_code == 200
    wrong = client.post(EXTRACT, json=extract_body(), headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    # Probes and metrics stay open for the orchestrator.
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_concurrency_limit_returns_busy(client_factory: ClientFactory) -> None:
    gate = threading.Event()
    entered = threading.Event()
    engine = FakeEngine(gate=gate, on_call=lambda _o: entered.set())
    client = client_factory(engine, max_concurrency=1, queue_timeout_seconds=0.05)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(client.post, EXTRACT, json=extract_body())
        assert entered.wait(5)
        busy = client.post(EXTRACT, json=extract_body())
        gate.set()
        assert first.result().status_code == 200
    assert busy.status_code == 503
    err = busy.json()["error"]
    assert err["code"] == "worker_busy"
    assert err["contract_code"] == "RATE_LIMITED"
    assert err["retryable"] is True
    assert busy.headers["retry-after"] == "1"
    metrics = client.get("/metrics").text
    assert 'ocr_worker_rejections_total{reason="concurrency"} 1.0' in metrics


def test_deadline_exceeded(client_factory: ClientFactory) -> None:
    seen: list[float] = []
    engine = FakeEngine(delay=0.8, on_call=lambda o: seen.append(o.timeout_seconds))
    client = client_factory(engine, request_timeout_seconds=0.5)
    started = time.monotonic()
    res = client.post(EXTRACT, json=extract_body())
    assert res.status_code == 504
    assert res.json()["error"]["code"] == "deadline_exceeded"
    assert res.json()["error"]["contract_code"] == "TIMEOUT"
    assert time.monotonic() - started < 3
    assert seen
    assert all(0 < t <= 0.5 for t in seen)


def test_metrics_exposed_without_sensitive_labels(client: TestClient) -> None:
    client.post(EXTRACT, json=extract_body())
    text = client.get("/metrics").text
    assert (
        'ocr_worker_extract_requests_total{country="DO",document_type="driver_license",'
        'outcome="succeeded"} 1.0'
    ) in text
    assert (
        'ocr_worker_field_status_total{document_type="driver_license",'
        'field="document_number",status="extracted"} 1.0'
    ) in text
    assert 'ocr_worker_qr_total{result="allowlisted"} 1.0' in text
    assert "ocr_worker_engine_duration_seconds_bucket" in text
    assert DOC_NUMBER not in text


def test_logs_never_contain_document_values(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    from codestra_ocr_workers.redaction import RedactingFilter

    caplog.handler.addFilter(RedactingFilter())
    with caplog.at_level(logging.DEBUG):
        client.post(EXTRACT, json=extract_body())
    text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "extraction completed" in text
    assert DOC_NUMBER not in text
    assert "JUANA" not in text
    assert "intrantmoto" not in text


def test_unexpected_errors_are_contained(client_factory: ClientFactory) -> None:
    def boom(_options: object) -> None:
        raise RuntimeError(f"leak {DOC_NUMBER}")

    client = client_factory(FakeEngine(on_call=boom), raise_server_exceptions=False)
    res = client.post(EXTRACT, json=extract_body())
    assert res.status_code == 500
    assert res.json()["error"]["contract_code"] == "INTERNAL_ERROR"
    assert DOC_NUMBER not in res.text


def test_request_id_header_validation(client: TestClient) -> None:
    res = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert res.headers["x-request-id"] != "bad id with spaces"
    res = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert res.headers["x-request-id"] == "abc-123"


def test_docs_disabled(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
