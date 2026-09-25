from __future__ import annotations

import pytest

from codestra_ocr_workers.config import Settings
from codestra_ocr_workers.engines.base import RecognizeOptions
from codestra_ocr_workers.engines.registry import EngineRegistry
from codestra_ocr_workers.engines.tesseract import TesseractEngine, parse_tsv
from codestra_ocr_workers.errors import EngineUnavailableError

from ..fixtures import blank_card

HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def _row(level: int, line: int, word: int, left: int, conf: float, text: str) -> str:
    return f"{level}\t1\t1\t1\t{line}\t{word}\t{left}\t{line * 40}\t50\t30\t{conf}\t{text}"


def test_parse_tsv_groups_lines_and_scales_confidence() -> None:
    tsv = "\n".join(
        [
            HEADER,
            "1\t1\t0\t0\t0\t0\t0\t0\t1000\t600\t-1\t",
            _row(5, 1, 2, 80, 90, "MARIA"),
            _row(5, 1, 1, 10, 80, "JUANA"),
            _row(5, 1, 3, 150, -1, ""),
            _row(5, 2, 1, 10, 95.5, "Sexo"),
            _row(5, 2, 2, 80, 70, "F"),
            _row(5, 2, 3, 99, 50, "   "),
        ]
    )
    page = parse_tsv(tsv, width=1000, height=600)
    assert [line.text for line in page.lines] == ["JUANA MARIA", "Sexo F"]
    assert page.lines[0].words[0].confidence == pytest.approx(0.8)
    assert page.lines[1].words[0].bbox == (10, 80, 50, 30)
    assert page.word_count == 4
    assert page.mean_confidence == pytest.approx((0.8 + 0.9 + 0.955 + 0.7) / 4)


def test_parse_tsv_tolerates_garbage() -> None:
    page = parse_tsv(HEADER + "\n5\tx\ty\n\n", width=10, height=10)
    assert page.lines == ()


def test_missing_binary_is_unavailable() -> None:
    engine = TesseractEngine(cmd="definitely-not-tesseract-xyz")
    ready, detail = engine.readiness()
    assert not ready and "not found" in detail
    assert engine.info().version == "unavailable"
    with pytest.raises(EngineUnavailableError):
        engine.recognize(blank_card(), RecognizeOptions(("spa",), 1.0))


def test_registry_from_settings() -> None:
    settings = Settings(_env_file=None, tesseract_cmd="nope-xyz")  # type: ignore[call-arg]
    registry = EngineRegistry.from_settings(settings)
    assert registry.names() == ["tesseract"]
    assert registry.get(None).name == "tesseract"
    with pytest.raises(EngineUnavailableError):
        registry.get("paddleocr")


def test_registry_rejects_unknown_engine_and_bad_default() -> None:
    with pytest.raises(EngineUnavailableError):
        EngineRegistry.from_settings(Settings(_env_file=None, engines=["does-not-exist"]))  # type: ignore[call-arg]
    with pytest.raises(EngineUnavailableError):
        EngineRegistry({}, "tesseract")


def test_settings_accept_csv_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_WORKER_TESSERACT_LANGUAGES", "spa, eng")
    monkeypatch.setenv("OCR_WORKER_QR_ALLOWED_HOSTS", "A.example.,b.example")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.tesseract_languages == ["spa", "eng"]
    assert settings.qr_allowed_hosts == ["a.example", "b.example"]
