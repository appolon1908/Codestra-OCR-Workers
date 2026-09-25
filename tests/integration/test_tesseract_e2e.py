"""End-to-end with the real Tesseract binary on synthetic, rendered card images.

Runs in the Docker ``test`` stage (tesseract-ocr + spa installed); skipped elsewhere.
"""

from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from codestra_ocr_workers.app import create_app
from codestra_ocr_workers.config import Settings
from codestra_ocr_workers.engines.registry import EngineRegistry
from codestra_ocr_workers.engines.tesseract import TesseractEngine
from codestra_ocr_workers.imaging import Image

from ..conftest import assert_valid
from ..fixtures import AUTHORITY_URL, CARD_SERIAL, DOC_NUMBER, DOC_NUMBER_FMT, b64, qr_image

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed"),
]

FRONT_LINES = [
    "REPUBLICA DOMINICANA",
    "LICENCIA DE CONDUCIR",
    "JUANA MARIA",
    "EJEMPLO PRUEBA",
    "Direccion",
    "CALLE FICTICIA 123",
    "SECTOR DEMO",
    "Estatura 5'06 Peso 140 Sexo F",
    "Tipo de Sangre O+",
    "Nacimiento 15/03/1990",
    "Emision 10/01/2024",
    "Vence 10/01/2099",
    f"Licencia No. {DOC_NUMBER_FMT}",
]
BACK_LINES = [
    "Categoria 02 LIVIANO",
    "Restricciones LENTES",
    "Primera Emision 05/06/2010",
    CARD_SERIAL,
]


def render(lines: list[str], *, x: int = 60, width: int = 1600, qr: str | None = None) -> Image:
    height = 120 + 70 * len(lines) + (0 if qr is None else 420)
    img = np.full((height, width, 3), 245, dtype=np.uint8)
    y = 90
    if qr is not None:
        code = cv2.cvtColor(qr_image(qr, module_px=10), cv2.COLOR_GRAY2BGR)
        h, w = code.shape[:2]
        img[30 : 30 + h, 40 : 40 + w] = code
        y = 60 + h
    for line in lines:
        cv2.putText(img, line, (x, y), cv2.FONT_HERSHEY_DUPLEX, 1.4, (20, 20, 20), 2, cv2.LINE_AA)
        y += 70
    return img


@pytest.fixture(scope="module")
def client() -> TestClient:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    engine = TesseractEngine(languages=("spa", "eng"))
    ready, detail = engine.readiness()
    if not ready:
        pytest.skip(detail)
    return TestClient(create_app(settings, EngineRegistry({"tesseract": engine}, "tesseract")))


def test_readiness_reports_real_engine(client: TestClient) -> None:
    res = client.get("/readyz")
    assert res.status_code == 200
    caps = client.get("/v1/capabilities").json()
    engine = caps["engines"][0]
    assert engine["name"] == "tesseract"
    assert engine["version"][0].isdigit()
    assert "spa" in engine["languages"]


def _images() -> list[dict[str, str]]:
    return [
        {"side": "front", "content_base64": b64(render(FRONT_LINES)), "media_type": "image/png"},
        {
            "side": "back",
            "content_base64": b64(render(BACK_LINES, qr=AUTHORITY_URL), ".jpg"),
            "media_type": "image/jpeg",
        },
    ]


def test_extracts_synthetic_licence(client: TestClient) -> None:
    body = {
        "scan_id": "scan-it-1",
        "tenant_id": "tenant-it",
        "document_type": "driver_license",
        "country": "DO",
        "schema_version": "1.0.0",
        "images": _images(),
    }
    res = client.post("/internal/v1/ocr/extract", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    result = data["result"]
    assert_valid("codestra-document-schemas/v1/do-driver-licence.schema.json", result)
    fields = result["fields"]
    assert result["engine"]["name"] == "tesseract"
    assert fields["document_number"] == DOC_NUMBER
    assert data["diagnostics"]["fields"]["document_number"]["checks"]["luhn"] is True
    assert fields["birth_date"] == "1990-03-15"
    assert fields["issue_date"] == "2024-01-10"
    assert fields["expiry_date"] == "2099-01-10"
    assert fields["first_issue_date"] == "2010-06-05"
    assert fields["sex"] == "F"
    assert fields["height"] == 1.68
    assert fields["weight_lb"] == 140
    assert fields["blood_type"] == "O+"
    assert fields["full_name"] == "JUANA MARIA EJEMPLO PRUEBA"
    assert fields["address"] == "CALLE FICTICIA 123 SECTOR DEMO"
    assert fields["card_serial"] == CARD_SERIAL
    assert fields["category"] == "02 LIVIANO"
    assert fields["restriction"] == "LENTES"
    assert result["qr_evidence"]["status"] == "decoded"
    assert result["qr_evidence"]["raw_payload"] == AUTHORITY_URL
    assert data["diagnostics"]["qr"]["allowlisted"] is True
    for name in ("document_number", "birth_date", "expiry_date"):
        evidence = result["field_evidence"][name][0]
        assert evidence["confidence"] > 0.5
        assert len(evidence["bbox"]) == 4


def test_document_intelligence_adapter_real_engine(client: TestClient) -> None:
    import base64
    import hashlib

    images = []
    for img in _images():
        raw = base64.b64decode(img["content_base64"])
        images.append({**img, "sha256": hashlib.sha256(raw).hexdigest()})
    body = {
        "schema_ref": "codestra.ocr.extract-request/v1",
        "request_id": "di-it-1",
        "tenant_id": "tenant-it",
        "document_type": "driver_license",
        "country": "DO",
        "expected_response_schema_ref": "codestra.ocr.extraction-result/v1",
        "images": images,
    }
    res = client.post("/v1/ocr/extract", json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert_valid("document-intelligence/v1/ocr-extraction-result.v1.schema.json", out)
    assert out["image_digests"] == {i["side"]: i["sha256"] for i in images}
    assert out["fields"]["document_number"]["value"] == DOC_NUMBER
    assert out["qr"]["source_lookup_url"] == AUTHORITY_URL
    assert out["worker"]["engine"].startswith("tesseract-")


def test_blank_image_is_partial_not_error(client: TestClient) -> None:
    blank = np.full((600, 960, 3), 250, dtype=np.uint8)
    body = {
        "scan_id": "s1",
        "tenant_id": "t1",
        "document_type": "driver_license",
        "country": "DO",
        "schema_version": "1.0.0",
        "images": [{"content_base64": b64(blank)}],
    }
    res = client.post("/internal/v1/ocr/extract", json=body)
    assert res.status_code == 200
    assert res.json()["status"] == "partial"


def test_no_temp_files_left(client: TestClient) -> None:
    import os
    import tempfile

    tmp = tempfile.gettempdir()
    before = set(os.listdir(tmp))
    body = {
        "scan_id": "s1",
        "tenant_id": "t1",
        "document_type": "driver_license",
        "country": "DO",
        "schema_version": "1.0.0",
        "images": [{"content_base64": b64(render(FRONT_LINES))}],
    }
    assert client.post("/internal/v1/ocr/extract", json=body).status_code == 200
    assert set(os.listdir(tmp)) - before == set()
