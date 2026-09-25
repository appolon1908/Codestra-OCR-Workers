"""Document-Intelligence adapter: codestra.ocr.extract-request/v1 -> extraction-result/v1."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ..conftest import ClientFactory, assert_valid
from ..fixtures import AUTHORITY_URL, DOC_NUMBER, FakeEngine, di_body, text_page

DI = "/v1/ocr/extract"
REQUEST_SCHEMA = "document-intelligence/v1/ocr-extract-request.v1.schema.json"
RESULT_SCHEMA = "document-intelligence/v1/ocr-extraction-result.v1.schema.json"


def test_fixture_request_matches_di_schema() -> None:
    assert_valid(REQUEST_SCHEMA, di_body())


def test_di_happy_path(client: TestClient) -> None:
    body = di_body()
    res = client.post(DI, json=body, headers={"X-Request-ID": "di-req-1"})
    assert res.status_code == 200, res.text
    out = res.json()
    assert_valid(RESULT_SCHEMA, out)
    assert out["schema_ref"] == "codestra.ocr.extraction-result/v1"
    assert out["request_id"] == "di-req-1"
    assert out["document_type"] == "driver_license"
    assert out["worker"]["engine"] == "fake-0.0-test"
    sent = {img["side"]: img["sha256"] for img in body["images"]}  # type: ignore[attr-defined]
    assert out["image_digests"] == sent
    assert out["fields"]["document_number"]["value"] == DOC_NUMBER
    assert out["fields"]["document_number"]["source"] == "visual"
    assert out["fields"]["document_number"]["evidence"]["side"] == "front"
    assert out["fields"]["height"]["value"] == "1.68"
    assert out["fields"]["weight_lb"]["value"] == "140"
    assert out["qr"] == {"present": True, "source_lookup_url": AUTHORITY_URL}
    assert 0.8 < out["quality"]["overall_confidence"] <= 1
    assert out["quality"]["warnings"] == []


def test_di_missing_fields_and_warnings(client_factory: ClientFactory) -> None:
    client = client_factory(FakeEngine([text_page("nothing here")]))
    out = client.post(DI, json=di_body()).json()
    assert_valid(RESULT_SCHEMA, out)
    assert out["fields"]["full_name"] == {"value": None, "confidence": 0.0}
    assert out["quality"]["overall_confidence"] == 0.0
    assert "MISSING_FIELD:full_name" in out["quality"]["warnings"]
    assert "REVIEW_REQUIRED" in out["quality"]["warnings"]


def test_di_front_only(client: TestClient) -> None:
    body = di_body()
    body["images"] = body["images"][:1]  # type: ignore[index]
    out = client.post(DI, json=body).json()
    assert_valid(RESULT_SCHEMA, out)
    assert set(out["image_digests"]) == {"front"}


@pytest.mark.parametrize(
    ("mutate", "status", "code"),
    [
        (lambda b: b.update(schema_ref="codestra.ocr.extract-request/v2"), 422, "invalid_request"),
        (lambda b: b.update(expected_response_schema_ref="x"), 422, "invalid_request"),
        (lambda b: b["images"][0].update(sha256="f" * 64), 422, "digest_mismatch"),
        (lambda b: b["images"][0].update(media_type="image/jpeg"), 422, "invalid_image"),
        (lambda b: b.update(images=b["images"][1:]), 422, "front_image_required"),
        (lambda b: b.update(document_type="passport"), 422, "unsupported_document"),
    ],
)
def test_di_rejections(client: TestClient, mutate, status: int, code: str) -> None:  # type: ignore[no-untyped-def]
    body = di_body()
    mutate(body)
    res = client.post(DI, json=body)
    assert res.status_code == status, res.text
    assert res.json()["error"]["code"] == code


def test_di_bearer_token(client_factory: ClientFactory) -> None:
    client = client_factory(internal_token="docintel-to-ocr-worker-token")
    assert client.post(DI, json=di_body()).status_code == 401
    ok = client.post(
        DI, json=di_body(), headers={"Authorization": "Bearer docintel-to-ocr-worker-token"}
    )
    assert ok.status_code == 200
