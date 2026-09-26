"""Synthetic fixtures. Every value here is invented; no real person or document."""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable

import cv2
import numpy as np

from codestra_ocr_workers.engines.base import (
    EngineInfo,
    OCREngine,
    OCRLine,
    OCRPage,
    OCRWord,
    RecognizeOptions,
)
from codestra_ocr_workers.extractors.base import luhn_valid
from codestra_ocr_workers.imaging import Image


def luhn_complete(prefix10: str) -> str:
    for d in "0123456789":
        if luhn_valid(prefix10 + d):
            return prefix10 + d
    raise AssertionError("unreachable")


# Municipality code 000 is never issued, so this number cannot belong to anyone.
DOC_NUMBER = luhn_complete("0001234567")
DOC_NUMBER_FMT = f"{DOC_NUMBER[:3]}-{DOC_NUMBER[3:10]}-{DOC_NUMBER[10]}"
BAD_DOC_NUMBER = DOC_NUMBER[:10] + str((int(DOC_NUMBER[10]) + 1) % 10)
CARD_SERIAL = "7654321"
AUTHORITY_URL = "https://licencias.intrantmoto.com/verificar?token=SYNTHETIC-TEST-TOKEN"

FRONT_TEXT = f"""REPUBLICA DOMINICANA
LICENCIA DE CONDUCIR
JUANA MARIA
EJEMPLO PRUEBA
Dirección
CALLE FICTICIA 123
SECTOR DEMO, SANTO DOMINGO
Estatura 5'06 Peso 140 Sexo F
Tipo de Sangre O+
Nacimiento 15/03/1990
Emisión 10/01/2024
Vence 10/01/2099
Licencia No. {DOC_NUMBER_FMT}"""

BACK_TEXT = f"""Categoría 02 LIVIANO
Restricciones LENTES
Primera Emisión 05/06/2010
{CARD_SERIAL}"""


def text_page(
    text: str, confidence: float = 0.92, overrides: dict[str, float] | None = None
) -> OCRPage:
    """Build an OCRPage from text lines with synthetic geometry and confidences."""
    overrides = overrides or {}
    lines = []
    for li, line in enumerate(text.splitlines()):
        words = []
        x = 10
        for token in line.split():
            conf = overrides.get(token, confidence)
            words.append(OCRWord(token, conf, (x, 10 + li * 40, 12 * len(token), 30)))
            x += 12 * len(token) + 12
        lines.append(OCRLine(tuple(words)))
    return OCRPage(tuple(lines), width=1000, height=40 * len(lines) + 20)


class FakeEngine(OCREngine):
    """Deterministic engine returning scripted pages in call order."""

    name = "fake"

    def __init__(
        self,
        pages: list[OCRPage] | None = None,
        *,
        ready: bool = True,
        delay: float = 0.0,
        gate: threading.Event | None = None,
        on_call: Callable[[RecognizeOptions], None] | None = None,
    ) -> None:
        self.pages = pages or [text_page(FRONT_TEXT), text_page(BACK_TEXT)]
        self.ready = ready
        self.delay = delay
        self.gate = gate
        self.on_call = on_call
        self.calls = 0
        self._lock = threading.Lock()

    def info(self) -> EngineInfo:
        return EngineInfo("fake", "0.0-test", ("spa", "eng"), ("word_confidence",))

    def readiness(self) -> tuple[bool, str]:
        return self.ready, "fake ready" if self.ready else "fake not ready"

    def recognize(self, image: Image, options: RecognizeOptions) -> OCRPage:
        if self.on_call:
            self.on_call(options)
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            page = self.pages[self.calls % len(self.pages)]
            self.calls += 1
        return page


def blank_card(width: int = 1000, height: int = 630, value: int = 235) -> Image:
    return np.full((height, width, 3), value, dtype=np.uint8)


def qr_image(payload: str, module_px: int = 8) -> Image:
    qr = cv2.QRCodeEncoder.create().encode(payload)
    qr = cv2.resize(qr, None, fx=module_px, fy=module_px, interpolation=cv2.INTER_NEAREST)
    return cv2.copyMakeBorder(qr, 32, 32, 32, 32, cv2.BORDER_CONSTANT, value=255)


def card_with_qr(payload: str) -> Image:
    card = blank_card()
    qr = cv2.cvtColor(qr_image(payload), cv2.COLOR_GRAY2BGR)
    h, w = qr.shape[:2]
    card[20 : 20 + h, 20 : 20 + w] = qr
    return card


def encode(img: Image, ext: str = ".png") -> bytes:
    ok, buf = cv2.imencode(ext, img)
    assert ok
    return bytes(buf.tobytes())


def b64(img: Image, ext: str = ".png") -> str:
    return base64.b64encode(encode(img, ext)).decode()


def extract_body(
    images: list[dict[str, str]] | None = None, **overrides: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "scan_id": "scan-0001",
        "tenant_id": "tenant-test",
        "document_type": "driver_license",
        "country": "DO",
        "schema_version": "1.0.0",
        "images": images
        if images is not None
        else [
            {"side": "front", "content_base64": b64(blank_card())},
            {"side": "back", "content_base64": b64(card_with_qr(AUTHORITY_URL))},
        ],
    }
    body.update(overrides)
    return body


def di_body(images: list[dict[str, str]] | None = None, **overrides: object) -> dict[str, object]:
    """Document-Intelligence ``codestra.ocr.extract-request/v1`` payload."""
    import hashlib

    if images is None:
        front, back = encode(blank_card(), ".png"), encode(card_with_qr(AUTHORITY_URL), ".png")
        images = [
            {
                "side": "front",
                "media_type": "image/png",
                "sha256": hashlib.sha256(front).hexdigest(),
                "content_base64": base64.b64encode(front).decode(),
            },
            {
                "side": "back",
                "media_type": "image/png",
                "sha256": hashlib.sha256(back).hexdigest(),
                "content_base64": base64.b64encode(back).decode(),
            },
        ]
    body: dict[str, object] = {
        "schema_ref": "codestra.ocr.extract-request/v1",
        "request_id": "di-req-1",
        "tenant_id": "tenant-test",
        "document_type": "driver_license",
        "country": "DO",
        "expected_response_schema_ref": "codestra.ocr.extraction-result/v1",
        "images": images,
    }
    body.update(overrides)
    return body
