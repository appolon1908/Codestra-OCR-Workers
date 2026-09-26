from __future__ import annotations

import pytest

from codestra_ocr_workers.extractors.base import PageInput, Side, WarningCode, luhn_valid
from codestra_ocr_workers.extractors.do_driver_license import (
    FIELDS,
    DominicanDriverLicenseExtractor,
    parse_date,
)
from codestra_ocr_workers.qr import QRResult, classify

from ..fixtures import (
    AUTHORITY_URL,
    BACK_TEXT,
    BAD_DOC_NUMBER,
    CARD_SERIAL,
    DOC_NUMBER,
    DOC_NUMBER_FMT,
    FRONT_TEXT,
    text_page,
)

EXTRACTOR = DominicanDriverLicenseExtractor()


def run(
    front: str | None = FRONT_TEXT,
    back: str | None = BACK_TEXT,
    qr: QRResult | None = None,
    threshold: float = 0.6,
    **page_kw: object,
):
    pages = []
    if front is not None:
        pages.append(PageInput(0, Side.FRONT, text_page(front, **page_kw)))  # type: ignore[arg-type]
    if back is not None:
        pages.append(PageInput(len(pages), Side.BACK, text_page(back, **page_kw)))  # type: ignore[arg-type]
    return EXTRACTOR.extract(pages, qr, threshold)


def values(out) -> dict[str, object]:
    return {k: v.value for k, v in out.fields.items()}


def codes(out) -> set[tuple[str, str | None]]:
    return {(w.code.value, w.field) for w in out.warnings}


def test_fixture_number_is_luhn_valid_and_synthetic() -> None:
    assert luhn_valid(DOC_NUMBER)
    assert not luhn_valid(BAD_DOC_NUMBER)
    assert DOC_NUMBER.startswith("000")


def test_full_extraction_all_fields() -> None:
    out = run()
    assert values(out) == {
        "full_name": "JUANA MARIA EJEMPLO PRUEBA",
        "document_number": DOC_NUMBER,
        "address": "CALLE FICTICIA 123 SECTOR DEMO, SANTO DOMINGO",
        "height": 1.68,
        "weight_lb": 140.0,
        "sex": "F",
        "blood_type": "O+",
        "birth_date": "1990-03-15",
        "issue_date": "2024-01-10",
        "expiry_date": "2099-01-10",
        "category": "02 LIVIANO",
        "restriction": "LENTES",
        "first_issue_date": "2010-06-05",
        "card_serial": CARD_SERIAL,
    }
    assert out.warnings == []
    assert set(out.fields) == {f.name for f in FIELDS}


def test_confidence_and_evidence() -> None:
    out = run()
    num = out.fields["document_number"]
    assert num.confidence == pytest.approx(0.92)
    assert num.checks == {"luhn": True, "length_11": True}
    assert num.evidence is not None
    assert num.evidence.side is Side.FRONT
    assert num.evidence.method == "label"
    assert num.evidence.bbox is not None
    name = out.fields["full_name"]
    assert name.evidence is not None and name.evidence.method == "layout"
    assert name.confidence is not None and name.confidence < 0.92  # heuristic penalty
    assert out.fields["weight_lb"].unit == "lb"
    assert out.fields["height"].unit == "m"
    assert out.fields["height"].checks == {"converted_from_ft_in": True}
    serial = out.fields["card_serial"]
    assert serial.evidence is not None and serial.evidence.side is Side.BACK


def test_low_confidence_word_produces_warning() -> None:
    out = run(overrides={DOC_NUMBER_FMT: 0.3})
    assert out.fields["document_number"].value == DOC_NUMBER
    assert out.fields["document_number"].confidence == pytest.approx(0.3)
    assert ("low_confidence", "document_number") in codes(out)


def test_low_page_confidence() -> None:
    out = run(confidence=0.4)
    assert "low_page_confidence" in {w.code.value for w in out.warnings}


def test_checksum_failure_lowers_confidence() -> None:
    out = run(front=FRONT_TEXT.replace(DOC_NUMBER_FMT, BAD_DOC_NUMBER))
    field = out.fields["document_number"]
    assert field.value == BAD_DOC_NUMBER
    assert field.checks["luhn"] is False
    assert field.confidence is not None and field.confidence < 0.6
    assert ("checksum_failed", "document_number") in codes(out)


def test_ocr_digit_confusions_repaired() -> None:
    noisy = FRONT_TEXT.replace("15/03/1990", "l5/O3/199O").replace(
        DOC_NUMBER_FMT, DOC_NUMBER_FMT.replace("0", "O", 2)
    )
    out = run(front=noisy)
    assert out.fields["birth_date"].value == "1990-03-15"
    assert out.fields["document_number"].value == DOC_NUMBER


def test_unlabelled_number_uses_pattern_and_prefers_luhn() -> None:
    front = FRONT_TEXT.replace(f"Licencia No. {DOC_NUMBER_FMT}", f"{BAD_DOC_NUMBER}\n{DOC_NUMBER}")
    out = run(front=front)
    field = out.fields["document_number"]
    assert field.value == DOC_NUMBER
    assert field.evidence is not None and field.evidence.method == "pattern"


def test_missing_back_side() -> None:
    out = run(back=None)
    got = codes(out)
    assert ("side_missing", None) in got
    for name in ("category", "restriction", "first_issue_date", "card_serial"):
        assert out.fields[name].value is None
        assert ("field_missing", name) in got
    assert all(w.code != WarningCode.REQUIRED_FIELD_MISSING for w in out.warnings)


def test_required_missing_is_error() -> None:
    front = FRONT_TEXT.replace(f"Licencia No. {DOC_NUMBER_FMT}", "")
    out = run(front=front)
    warning = next(w for w in out.warnings if w.field == "document_number")
    assert warning.code is WarningCode.REQUIRED_FIELD_MISSING
    assert warning.severity.value == "error"


def test_issue_date_not_confused_with_first_issue_on_same_page() -> None:
    combined = FRONT_TEXT.replace("Emisión 10/01/2024\n", "") + "\n" + BACK_TEXT
    combined = combined.replace("Vence", "Emisión 10/01/2024\nVence")
    out = run(front=combined, back=None)
    assert out.fields["issue_date"].value == "2024-01-10"
    assert out.fields["first_issue_date"].value == "2010-06-05"


def test_unlabelled_dates_fallback() -> None:
    front = FRONT_TEXT.replace("Nacimiento ", "").replace("Emisión ", "").replace("Vence ", "")
    out = run(front=front)
    assert out.fields["birth_date"].value == "1990-03-15"
    assert out.fields["issue_date"].value == "2024-01-10"
    assert out.fields["expiry_date"].value == "2099-01-10"
    ev = out.fields["birth_date"].evidence
    assert ev is not None and ev.method == "pattern"
    assert ("low_confidence", "birth_date") in codes(out)


def test_date_inconsistency_and_expired() -> None:
    front = FRONT_TEXT.replace("Vence 10/01/2099", "Vence 10/01/2020")
    got = codes(run(front=front))
    assert ("date_inconsistent", "expiry_date") in got
    assert ("document_expired", "expiry_date") in got


def test_labelled_name_and_alternative_formats() -> None:
    front = """LICENCIA DE CONDUCIR
Nombres: PEDRO FICTICIO
Apellidos: DEMO EJEMPLO
Domicilio: AVENIDA INVENTADA 45
Altura 1.72 m Peso 80 kg Sexo M
Sangre AB NEG
Fecha de Nacimiento 01-02-1985
Expedición 03.04.2023
Vencimiento 03/04/2099
Cédula 000 1234567 """ + DOC_NUMBER[-1]
    out = run(front=front)
    got = values(out)
    assert got["full_name"] == "PEDRO FICTICIO DEMO EJEMPLO"
    assert got["address"] == "AVENIDA INVENTADA 45"
    assert got["height"] == 1.72
    assert got["weight_lb"] == 176.4
    assert out.fields["weight_lb"].checks == {"converted_from_kg": True}
    assert got["sex"] == "M"
    assert got["blood_type"] == "AB-"
    assert got["birth_date"] == "1985-02-01"
    assert got["issue_date"] == "2023-04-03"
    assert got["expiry_date"] == "2099-04-03"
    assert got["document_number"] == DOC_NUMBER


def test_label_value_on_next_line() -> None:
    front = FRONT_TEXT.replace("Nacimiento 15/03/1990", "Nacimiento\n15/03/1990")
    assert run(front=front).fields["birth_date"].value == "1990-03-15"


def test_restrictions_stop_at_next_label() -> None:
    back = "Categoría 03 PESADO Restricciones NINGUNA Primera Emisión 05/06/2010"
    got = values(run(back=back))
    assert got["category"] == "03 PESADO"
    assert got["restriction"] == "NINGUNA"
    assert got["first_issue_date"] == "2010-06-05"


def test_unknown_side_pages_are_searched() -> None:
    pages = [PageInput(0, Side.UNKNOWN, text_page(FRONT_TEXT + "\n" + BACK_TEXT))]
    out = EXTRACTOR.extract(pages, None, 0.6)
    assert out.fields["document_number"].value == DOC_NUMBER
    assert out.fields["category"].value == "02 LIVIANO"
    assert not any(w.code is WarningCode.SIDE_MISSING for w in out.warnings)


def test_fields_found_on_unexpected_side_are_discounted() -> None:
    out = run(front=BACK_TEXT, back=FRONT_TEXT)
    field = out.fields["document_number"]
    assert field.value == DOC_NUMBER
    assert field.confidence == pytest.approx(0.92 * 0.9)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (AUTHORITY_URL, None),
        ("http://licencias.intrantmoto.com/x", "qr_host_not_allowlisted"),
        ("https://evil.example/x", "qr_host_not_allowlisted"),
        ("plain text", "qr_not_url"),
        (None, "qr_not_found"),
    ],
)
def test_qr_warnings(payload: str | None, code: str | None) -> None:
    qr = classify(
        payload, detected=payload is not None, allowed_hosts=["licencias.intrantmoto.com"]
    )
    got = {w.code.value for w in run(qr=qr).warnings}
    if code is None:
        assert not {c for c in got if c.startswith("qr_")}
    else:
        assert code in got


def test_qr_detected_not_decoded() -> None:
    qr = classify(None, detected=True, allowed_hosts=[])
    assert "qr_not_decoded" in {w.code.value for w in run(qr=qr).warnings}


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (("31", "12", "1999"), "1999-12-31"),
        (("31", "02", "1999"), None),
        (("01", "01", "1800"), None),
        (("O1", "l2", "2OOO"), "2000-12-01"),
    ],
)
def test_parse_date(parts: tuple[str, str, str], expected: str | None) -> None:
    got = parse_date(*parts)
    assert (got.isoformat() if got else None) == expected


def test_category_is_ascii_folded_to_contract_pattern() -> None:
    back = "Categoría 04 VEHÍCULOS PESADOS ESPECIALES"
    value = run(back=back).fields["category"].value
    assert value == "04 VEHICULOS PESADOS"
    assert isinstance(value, str) and len(value) <= 20


def test_document_type_aliases() -> None:
    from codestra_ocr_workers.errors import UnsupportedDocumentError
    from codestra_ocr_workers.extractors.registry import get_extractor

    assert get_extractor("DO", "driver_license", "1.0.0") is get_extractor(
        "do", "driver_licence", None
    )
    with pytest.raises(UnsupportedDocumentError):
        get_extractor("DO", "driver_license", "2.0.0")
