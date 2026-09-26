"""Dominican Republic driver licence extractor (INTRANT format).

Conceptually ported from the FACE-ID ID-scan parser (label-anchored regexes over
Tesseract text, 11-digit number, 7-digit reverse serial, QR on the reverse) and extended
with per-field confidence/evidence, OCR digit repair, Luhn validation, ISO dates and
side fallbacks. No FACE-ID code is imported at runtime.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ..qr import QRResult
from .base import (
    Evidence,
    ExtractionOutput,
    ExtractionWarning,
    Extractor,
    FieldResult,
    FieldSpec,
    PageInput,
    Sensitivity,
    Severity,
    Side,
    TextIndex,
    WarningCode,
    fix_digits,
    luhn_valid,
)

F, B = Side.FRONT, Side.BACK
P, R = Sensitivity.PERSONAL, Sensitivity.RESTRICTED

SCHEMA_ID = "https://schemas.codestra.dev/document-intelligence/v1/do-driver-licence.schema.json"

FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("full_name", "string", True, P, F, "Holder full name as printed"),
    FieldSpec(
        "document_number",
        "string",
        True,
        R,
        F,
        "11-digit licence/cédula number; transient, never persist or log",
    ),
    FieldSpec("address", "string", False, P, F, "Residential address as printed"),
    FieldSpec("height", "number", False, P, F, "Height normalised to metres", unit="m"),
    FieldSpec("weight_lb", "number", False, P, F, "Weight normalised to pounds", unit="lb"),
    FieldSpec("sex", "enum", False, P, F, "M or F"),
    FieldSpec("blood_type", "enum", False, P, F, "ABO/Rh blood type, e.g. O+"),
    FieldSpec("birth_date", "date", True, P, F, "ISO-8601 birth date"),
    FieldSpec("issue_date", "date", False, P, F, "ISO-8601 issue date of this card"),
    FieldSpec("expiry_date", "date", True, P, F, "ISO-8601 expiry date"),
    FieldSpec("category", "string", False, P, B, "Category code and description (ASCII upper)"),
    FieldSpec("restriction", "string", False, P, B, "Driving restrictions as printed"),
    FieldSpec("first_issue_date", "date", False, P, B, "ISO-8601 first issue date"),
    FieldSpec("card_serial", "string", False, P, B, "Card serial/control number"),
)
REQUIRED = tuple(spec.name for spec in FIELDS if spec.required)

# Punctuation/whitespace (incl. one line break) allowed between a label and its value.
GAP = r"[\s:.\-–|]{0,12}"
DATE = r"([0-9OoIlSB]{1,2})\s?[/\-.]\s?([0-9OoIlSB]{1,2})\s?[/\-.]\s?([0-9OoIlSB]{4})"
_I = re.IGNORECASE

LABELS = {
    "birth_date": r"(?:Fecha\s+de\s+)?Nac(?:imiento|\.)?|F\.?\s*Nac\w*",
    "issue_date": r"(?:Fecha\s+de\s+)?(?:Emisi[oó0]n|Expedici[oó0]n)",
    "expiry_date": r"(?:Fecha\s+de\s+)?(?:Vence|Venc\w*|Expira\w*|V[aá]lida\s+hasta)",
    "first_issue_date": r"Primera\s+(?:Emisi[oó0]n|Expedici[oó0]n)",
    "height": r"Estatura|Altura",
    "weight_lb": r"Peso",
    "sex": r"Sexo",
    "blood_type": r"(?:Tipo\s+de\s+)?Sangre|T\.?\s*Sangre|Grupo\s+Sangu\w*",
    "document_number": r"Licencia\s*(?:No\.?|N[uú]m\.?|#)|C[eé]dula(?:\s*No\.?)?|No\.\s*Lic\w*",
    "card_serial": r"(?:No\.?\s*(?:de\s+)?)?Seri(?:e|al)|Control",
    "category": r"Categor[ií1]a",
    "restriction": r"Restricci[oó0]n(?:es)?",
    "address": r"Direcci[oó0]n|Domicilio",
    "name": r"Nombres?|Apellidos?",
}
ANY_LABEL = re.compile(
    r"\b(?:" + "|".join(v for k, v in LABELS.items() if k != "name") + r")\b", _I
)
STOP_LINES = (
    "REPUBLICA",
    "REPÚBLICA",
    "DOMINICANA",
    "LICENCIA",
    "CONDUCIR",
    "INTRANT",
    "INSTITUTO",
    "TRANSITO",
    "TRÁNSITO",
    "TRANSPORTE",
    "TERRESTRE",
    "DRIVER",
    "LICENSE",
    "MINISTERIO",
)


@dataclass
class Found:
    value: str | float
    start: int
    end: int
    method: str
    label: str | None = None
    unit: str | None = None
    factor: float = 1.0
    checks: dict[str, bool] = field(default_factory=dict)


Finder = Callable[[TextIndex, Side], "Found | None"]


# --- value parsers ---------------------------------------------------------------------


def parse_date(d: str, m: str, y: str) -> date | None:
    try:
        day, month, year = int(fix_digits(d)), int(fix_digits(m)), int(fix_digits(y))
        if not 1900 <= year <= 2100:
            return None
        return date(year, month, day)
    except ValueError:
        return None


def _label_value(
    idx: TextIndex, label_key: str, value_re: str, *, exclude_prefix: str | None = None
) -> re.Match[str] | None:
    pattern = re.compile(r"\b(?P<label>" + LABELS[label_key] + r")" + GAP + value_re, _I)
    for match in idx.finditer(pattern):
        if exclude_prefix:
            before = idx.text[max(0, match.start() - 14) : match.start()]
            if re.search(exclude_prefix, before, _I):
                continue
        return match
    return None


def _date_finder(key: str, exclude_prefix: str | None = None) -> Finder:
    def find(idx: TextIndex, _side: Side) -> Found | None:
        m = _label_value(idx, key, DATE, exclude_prefix=exclude_prefix)
        if m is None:
            return None
        parsed = parse_date(m.group(2), m.group(3), m.group(4))
        if parsed is None:
            return None
        return Found(
            parsed.isoformat(), m.start(2), m.end(4), "label", key, checks={"valid_date": True}
        )

    return find


def _find_height(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(idx, "height", r"(\d)\s?['’´`\"\-]\s?(\d{1,2})(?!\d)")
    if m:
        feet, inches = int(m.group(2)), int(m.group(3))
        if 3 <= feet <= 7 and inches < 12:
            metres = round(feet * 0.3048 + inches * 0.0254, 2)
            return Found(
                metres,
                m.start(2),
                m.end(3),
                "label",
                "height",
                checks={"converted_from_ft_in": True},
            )
    m = _label_value(idx, "height", r"([12])[.,](\d{2})\s?m?\b")
    if m:
        metres = float(f"{m.group(2)}.{m.group(3)}")
        if 0.5 <= metres <= 2.6:
            return Found(metres, m.start(2), m.end(3), "label", "height")
    return None


def _find_weight(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(idx, "weight_lb", r"([0-9OIlSB]{2,3})(?!\d)\s?(lbs?|kg)?")
    if not m:
        return None
    digits = fix_digits(m.group(2))
    if not digits.isdigit():
        return None
    if (m.group(3) or "").lower() == "kg":
        pounds = round(int(digits) * 2.20462, 1)
        checks = {"converted_from_kg": True}
    else:
        pounds, checks = float(int(digits)), {}
    if not 40 <= pounds <= 700:
        return None
    return Found(pounds, m.start(2), m.end(2), "label", "weight_lb", checks=checks)


def _find_sex(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(idx, "sex", r"([MF])(?![A-Za-z])")
    if not m:
        return None
    return Found(m.group(2).upper(), m.start(2), m.end(2), "label", "sex")


def _find_blood(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(idx, "blood_type", r"(AB|A|B|O|0)\s?(\+|-|–|POS\w*|NEG\w*)")
    if not m:
        return None
    group = "O" if m.group(2) == "0" else m.group(2).upper()
    rh = "+" if m.group(3).upper().startswith(("+", "POS")) else "-"
    return Found(f"{group}{rh}", m.start(2), m.end(3), "label", "blood_type")


_DOCNUM_LOOSE = r"([0-9OIlSB]{3})[\s\-]?([0-9OIlSB]{7})[\s\-]?([0-9OIlSB])(?![0-9A-Za-z])"
_DOCNUM_STRICT = re.compile(r"(?<![\w/.\-])(\d{3})[\s\-]?(\d{7})[\s\-]?(\d)(?![\w/])")


def _find_document_number(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(idx, "document_number", _DOCNUM_LOOSE)
    if m:
        digits = fix_digits(m.group(2) + m.group(3) + m.group(4))
        if digits.isdigit():
            ok = luhn_valid(digits)
            return Found(
                digits,
                m.start(2),
                m.end(4),
                "label",
                "document_number",
                factor=1.0 if ok else 0.6,
                checks={"luhn": ok, "length_11": True},
            )
    candidates: list[Found] = []
    for m in idx.finditer(_DOCNUM_STRICT):
        digits = m.group(1) + m.group(2) + m.group(3)
        ok = luhn_valid(digits)
        candidates.append(
            Found(
                digits,
                m.start(1),
                m.end(3),
                "pattern",
                None,
                factor=0.9 if ok else 0.55,
                checks={"luhn": ok, "length_11": True},
            )
        )
    if not candidates:
        return None
    # Prefer the first Luhn-valid candidate, else the first candidate.
    return next((c for c in candidates if c.checks["luhn"]), candidates[0])


def _find_card_serial(idx: TextIndex, side: Side) -> Found | None:
    m = _label_value(idx, "card_serial", r"([0-9OIlSB]{6,10})(?![0-9])")
    if m:
        digits = fix_digits(m.group(2))
        if digits.isdigit():
            return Found(digits, m.start(2), m.end(2), "label", "card_serial")
    if side != Side.BACK:
        return None
    # FACE-ID heuristic: the last standalone 7-digit number on the reverse.
    last: Found | None = None
    for m in idx.finditer(re.compile(r"(?<![\w/.\-])(\d{7})(?![\w/.\-])")):
        last = Found(m.group(1), m.start(1), m.end(1), "pattern", None, factor=0.8)
    return last


def ascii_upper(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).upper()


def _find_category(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(
        idx,
        "category",
        r"([0-9OIl]{1,3})(?:[ \t]*[-–:]?[ \t]*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ \t]{2,40}))?",
    )
    if not m:
        return None
    code = fix_digits(m.group(2))
    if not code.isdigit():
        return None
    desc = re.sub(r"\s+", " ", (m.group(3) or "")).strip()
    desc = ANY_LABEL.split(desc)[0].strip() if desc else ""
    desc = re.sub(r"[^A-Z0-9 ./-]", "", ascii_upper(desc))
    value = f"{int(code):02d} {desc}".strip()[:20].rstrip()
    end = m.end(3) if desc else m.start(2) + len(m.group(2))
    return Found(value, m.start(2), end, "label", "category")


def _find_restrictions(idx: TextIndex, _side: Side) -> Found | None:
    m = _label_value(idx, "restriction", r"([A-ZÁÉÍÓÚÑ0-9][A-ZÁÉÍÓÚÑ0-9 ,/\-]{1,60})")
    if not m:
        return None
    raw = m.group(2)
    cut = ANY_LABEL.search(raw)
    if cut:
        raw = raw[: cut.start()]
    value = re.sub(r"\s+", " ", raw).strip(" ,/-").upper()
    if len(value) < 2:
        return None
    return Found(value, m.start(2), m.start(2) + len(raw.rstrip()), "label", "restriction")


def _find_address(idx: TextIndex, _side: Side) -> Found | None:
    label = re.compile(r"\b(?:" + LABELS["address"] + r")\b[\s:.\-]*", _I)
    lm = label.search(idx.text)
    if lm is None:
        return None
    pieces: list[tuple[int, int]] = []
    for start, end, _text in idx.lines():
        if end <= lm.start():
            continue
        s = max(start, lm.end())
        segment = idx.text[s:end]
        if not segment.strip():
            continue
        if pieces and (ANY_LABEL.search(segment) or re.search(DATE, segment)):
            break
        cut = ANY_LABEL.search(segment)
        if cut:
            segment = segment[: cut.start()]
            if segment.strip():
                pieces.append((s, s + len(segment.rstrip())))
            break
        pieces.append((s, end))
        if len(pieces) == 3:
            break
    pieces = [(a, b) for a, b in pieces if idx.text[a:b].strip()]
    if not pieces:
        return None
    value = " ".join(idx.text[a:b].strip() for a, b in pieces)
    value = re.sub(r"\s+", " ", value).strip(" ,.")[:200]
    if len(value) < 4:
        return None
    return Found(value, pieces[0][0], pieces[-1][1], "label", "address")


_NAME_TOKEN = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ'\-]+$")


def _name_line(text: str) -> bool:
    words = text.split()
    if not 1 <= len(words) <= 5:
        return False
    if any(stop in text.upper() for stop in STOP_LINES) or ANY_LABEL.search(text):
        return False
    return all(_NAME_TOKEN.match(w) for w in words)


def _find_full_name(idx: TextIndex, side: Side) -> Found | None:
    labelled = re.compile(r"\b(?:" + LABELS["name"] + r")\b[\s:.\-]*([^\n]+)", _I)
    parts = []
    for m in idx.finditer(labelled):
        text = m.group(1).strip()
        if _name_line(text):
            parts.append((m.start(1), m.start(1) + len(text), text))
    if parts:
        value = " ".join(p[2] for p in parts[:2]).upper()
        return Found(value, parts[0][0], parts[min(1, len(parts) - 1)][1], "label", "name")
    if side == Side.BACK:
        return None
    # Layout heuristic (from FACE-ID): the name is the first strong uppercase line(s)
    # between the card title and the address label.
    address = re.search(r"\b(?:" + LABELS["address"] + r")\b", idx.text, _I)
    limit = address.start() if address else len(idx.text)
    lines = [(s, e, t.strip()) for s, e, t in idx.lines() if e <= limit and t.strip()]
    candidates = [(s, e, t) for s, e, t in lines if _name_line(t)]
    for i, (s, e, text) in enumerate(candidates):
        if len(text.split()) < 2 and not (i + 1 < len(candidates)):
            continue
        if i + 1 < len(candidates):
            _ns, ne, nxt = candidates[i + 1]
            combo = f"{text} {nxt}"
            if 3 <= len(combo.split()) <= 6:
                return Found(combo.upper(), s, ne, "layout", None, factor=0.85)
        if len(text.split()) >= 2:
            return Found(text.upper(), s, e, "layout", None, factor=0.8)
    return None


FINDERS: dict[str, Finder] = {
    "full_name": _find_full_name,
    "document_number": _find_document_number,
    "address": _find_address,
    "height": _find_height,
    "weight_lb": _find_weight,
    "sex": _find_sex,
    "blood_type": _find_blood,
    "birth_date": _date_finder("birth_date"),
    "issue_date": _date_finder("issue_date", exclude_prefix=r"Primera\s*$"),
    "expiry_date": _date_finder("expiry_date"),
    "category": _find_category,
    "restriction": _find_restrictions,
    "first_issue_date": _date_finder("first_issue_date"),
    "card_serial": _find_card_serial,
}

_UNLABELLED_DATE_FIELDS = ("birth_date", "issue_date", "expiry_date")


def _unlabelled_dates(idx: TextIndex) -> dict[str, Found]:
    """Fallback when labels are unreadable: earliest=birth, latest=expiry, middle=issue."""
    found: list[tuple[date, int, int]] = []
    for m in idx.finditer(re.compile(DATE)):
        parsed = parse_date(m.group(1), m.group(2), m.group(3))
        if parsed:
            found.append((parsed, m.start(), m.end()))
    if len(found) < 2:
        return {}
    found.sort()
    picks = {"birth_date": found[0], "expiry_date": found[-1]}
    if len(found) >= 3:
        picks["issue_date"] = found[-2]
    return {
        k: Found(
            v[0].isoformat(), v[1], v[2], "pattern", None, factor=0.6, checks={"valid_date": True}
        )
        for k, v in picks.items()
    }


class DominicanDriverLicenseExtractor(Extractor):
    document_type = "driver_license"  # Codestra-Document-Schemas >= 093f9bc spelling
    document_type_aliases = ("driver_licence",)
    country = "DO"
    schema_id = SCHEMA_ID
    schema_versions = ("1.0.0",)
    fields = FIELDS
    expected_sides = (Side.FRONT, Side.BACK)

    def extract(
        self, pages: list[PageInput], qr: QRResult | None, low_confidence_threshold: float
    ) -> ExtractionOutput:
        indexes = [(p, TextIndex(p.page)) for p in pages]
        results: dict[str, FieldResult] = {}
        for spec in FIELDS:
            results[spec.name] = self._extract_field(spec, indexes)
        self._fill_unlabelled_dates(results, indexes)
        warnings = self._warnings(results, pages, qr, low_confidence_threshold)
        return ExtractionOutput(fields=results, warnings=warnings)

    @staticmethod
    def _ordered(
        spec: FieldSpec, indexes: list[tuple[PageInput, TextIndex]]
    ) -> list[tuple[PageInput, TextIndex, float]]:
        preferred = [
            (p, i, 1.0) for p, i in indexes if p.side in (spec.expected_side, Side.UNKNOWN)
        ]
        others = [
            (p, i, 0.9) for p, i in indexes if p.side not in (spec.expected_side, Side.UNKNOWN)
        ]
        return preferred + others

    def _extract_field(
        self, spec: FieldSpec, indexes: list[tuple[PageInput, TextIndex]]
    ) -> FieldResult:
        finder = FINDERS[spec.name]
        for page, idx, side_factor in self._ordered(spec, indexes):
            found = finder(idx, page.side)
            if found is not None:
                return _to_result(spec, found, page, idx, side_factor)
        return FieldResult(spec=spec)

    def _fill_unlabelled_dates(
        self, results: dict[str, FieldResult], indexes: list[tuple[PageInput, TextIndex]]
    ) -> None:
        if all(results[name].present for name in _UNLABELLED_DATE_FIELDS):
            return
        for page, idx in indexes:
            if page.side == Side.BACK:
                continue
            fallback = _unlabelled_dates(idx)
            taken = {results[n].value for n in results if results[n].spec.kind == "date"}
            for name, found in fallback.items():
                if not results[name].present and found.value not in taken:
                    spec = results[name].spec
                    results[name] = _to_result(spec, found, page, idx, 1.0)
            return

    @staticmethod
    def _warnings(
        results: dict[str, FieldResult],
        pages: list[PageInput],
        qr: QRResult | None,
        threshold: float,
    ) -> list[ExtractionWarning]:
        out: list[ExtractionWarning] = []
        sides = {p.side for p in pages}
        if Side.UNKNOWN not in sides:
            for side in (Side.FRONT, Side.BACK):
                if side not in sides:
                    out.append(
                        ExtractionWarning(
                            WarningCode.SIDE_MISSING,
                            Severity.WARNING,
                            f"no {side.value} image supplied",
                        )
                    )
        for p in pages:
            if p.page.mean_confidence < threshold:
                out.append(
                    ExtractionWarning(
                        WarningCode.LOW_PAGE_CONFIDENCE,
                        Severity.WARNING,
                        f"page {p.index} mean OCR confidence below threshold",
                    )
                )
        for name, res in results.items():
            if not res.present:
                code = (
                    WarningCode.REQUIRED_FIELD_MISSING
                    if res.spec.required
                    else WarningCode.FIELD_MISSING
                )
                sev = Severity.ERROR if res.spec.required else Severity.INFO
                out.append(ExtractionWarning(code, sev, "field not found", field=name))
                continue
            if res.confidence is not None and res.confidence < threshold:
                out.append(
                    ExtractionWarning(
                        WarningCode.LOW_CONFIDENCE,
                        Severity.WARNING,
                        "confidence below threshold",
                        field=name,
                    )
                )
            if res.checks.get("luhn") is False:
                out.append(
                    ExtractionWarning(
                        WarningCode.CHECKSUM_FAILED,
                        Severity.WARNING,
                        "check digit does not validate",
                        field=name,
                    )
                )
        out.extend(_date_consistency(results))
        if qr is not None:
            out.extend(_qr_warnings(qr, Side.BACK in sides))
        return out


def _to_result(
    spec: FieldSpec, found: Found, page: PageInput, idx: TextIndex, side_factor: float
) -> FieldResult:
    span = idx.span(found.start, found.end)
    ocr_conf = span.confidence
    confidence = (
        None
        if ocr_conf is None
        else round(max(0.0, min(1.0, ocr_conf * found.factor * side_factor)), 4)
    )
    return FieldResult(
        spec=spec,
        value=found.value,
        confidence=confidence,
        unit=found.unit or spec.unit,
        evidence=Evidence(
            page_index=page.index,
            side=page.side,
            method=found.method,
            label=found.label,
            bbox=span.bbox,
            ocr_confidence=None if ocr_conf is None else round(ocr_conf, 4),
        ),
        checks=dict(found.checks),
    )


def _as_date(res: FieldResult) -> date | None:
    return date.fromisoformat(res.value) if isinstance(res.value, str) else None


def _date_consistency(results: dict[str, FieldResult]) -> list[ExtractionWarning]:
    out: list[ExtractionWarning] = []
    birth = _as_date(results["birth_date"])
    issue = _as_date(results["issue_date"])
    expiry = _as_date(results["expiry_date"])
    first = _as_date(results["first_issue_date"])
    pairs = [
        ("birth_date", birth, "issue_date", issue),
        ("issue_date", issue, "expiry_date", expiry),
        ("birth_date", birth, "expiry_date", expiry),
        ("first_issue_date", first, "expiry_date", expiry),
        ("birth_date", birth, "first_issue_date", first),
    ]
    for a_name, a, b_name, b in pairs:
        if a and b and a >= b:
            out.append(
                ExtractionWarning(
                    WarningCode.DATE_INCONSISTENT,
                    Severity.WARNING,
                    f"{a_name} is not before {b_name}",
                    field=b_name,
                )
            )
    if first and issue and first > issue:
        out.append(
            ExtractionWarning(
                WarningCode.DATE_INCONSISTENT,
                Severity.WARNING,
                "first_issue_date is after issue_date",
                field="first_issue_date",
            )
        )
    if expiry and expiry < date.today():
        out.append(
            ExtractionWarning(
                WarningCode.DOCUMENT_EXPIRED,
                Severity.INFO,
                "printed expiry date is in the past",
                field="expiry_date",
            )
        )
    return out


def _qr_warnings(qr: QRResult, back_supplied: bool) -> list[ExtractionWarning]:
    if not qr.detected:
        if back_supplied:
            return [
                ExtractionWarning(
                    WarningCode.QR_NOT_FOUND,
                    Severity.INFO,
                    "no QR code detected on supplied images",
                )
            ]
        return []
    if not qr.decoded:
        return [
            ExtractionWarning(
                WarningCode.QR_NOT_DECODED,
                Severity.WARNING,
                "QR code detected but could not be decoded",
            )
        ]
    if qr.payload_kind != "url":
        return [ExtractionWarning(WarningCode.QR_NOT_URL, Severity.INFO, "QR payload is not a URL")]
    if not qr.allowlisted:
        return [
            ExtractionWarning(
                WarningCode.QR_HOST_NOT_ALLOWLISTED,
                Severity.WARNING,
                "QR URL host is not an allowlisted issuing authority",
            )
        ]
    return []
