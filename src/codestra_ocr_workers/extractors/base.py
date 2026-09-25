"""Engine-neutral extraction primitives.

Extractors receive :class:`OCRPage` objects (from any engine) and return typed field
results with confidence and positional evidence. Evidence never contains OCR text — only
page/side, the label that anchored the match, a bounding box and word confidence.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from ..engines.base import BBox, OCRPage, OCRWord
from ..qr import QRResult


class Side(StrEnum):
    FRONT = "front"
    BACK = "back"
    UNKNOWN = "unknown"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    RESTRICTED = "restricted"  # national identifiers: transient only, never log/persist


class WarningCode(StrEnum):
    REQUIRED_FIELD_MISSING = "required_field_missing"
    FIELD_MISSING = "field_missing"
    LOW_CONFIDENCE = "low_confidence"
    CHECKSUM_FAILED = "checksum_failed"
    DATE_INCONSISTENT = "date_inconsistent"
    DOCUMENT_EXPIRED = "document_expired"
    SIDE_MISSING = "side_missing"
    LOW_PAGE_CONFIDENCE = "low_page_confidence"
    QR_NOT_FOUND = "qr_not_found"
    QR_NOT_DECODED = "qr_not_decoded"
    QR_NOT_URL = "qr_not_url"
    QR_HOST_NOT_ALLOWLISTED = "qr_host_not_allowlisted"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ExtractionWarning:
    code: WarningCode
    severity: Severity
    message: str
    field: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str  # string | number | date | enum
    required: bool
    sensitivity: Sensitivity
    expected_side: Side
    description: str
    unit: str | None = None


@dataclass(frozen=True)
class Evidence:
    page_index: int
    side: Side
    method: str  # label | pattern | layout | qr
    label: str | None = None
    bbox: BBox | None = None
    ocr_confidence: float | None = None


@dataclass
class FieldResult:
    spec: FieldSpec
    value: str | float | None = None
    confidence: float | None = None
    unit: str | None = None
    evidence: Evidence | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class PageInput:
    index: int
    side: Side
    page: OCRPage


@dataclass
class ExtractionOutput:
    fields: dict[str, FieldResult]
    warnings: list[ExtractionWarning]


class Extractor(abc.ABC):
    document_type: str
    document_type_aliases: tuple[str, ...] = ()
    country: str
    schema_id: str
    schema_versions: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    expected_sides: tuple[Side, ...]

    @abc.abstractmethod
    def extract(
        self, pages: list[PageInput], qr: QRResult | None, low_confidence_threshold: float
    ) -> ExtractionOutput:
        """Parse OCR pages into schema fields."""


# --- text index ------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    words: tuple[OCRWord, ...]

    @property
    def confidence(self) -> float | None:
        if not self.words:
            return None
        return sum(w.confidence for w in self.words) / len(self.words)

    @property
    def bbox(self) -> BBox | None:
        if not self.words:
            return None
        x0 = min(w.bbox[0] for w in self.words)
        y0 = min(w.bbox[1] for w in self.words)
        x1 = max(w.bbox[0] + w.bbox[2] for w in self.words)
        y1 = max(w.bbox[1] + w.bbox[3] for w in self.words)
        return (x0, y0, x1 - x0, y1 - y0)


class TextIndex:
    """Page text with a character -> word map so regex matches can cite OCR evidence."""

    def __init__(self, page: OCRPage) -> None:
        parts: list[str] = []
        owners: list[OCRWord | None] = []
        for li, line in enumerate(page.lines):
            if li:
                parts.append("\n")
                owners.append(None)
            for wi, word in enumerate(line.words):
                if wi:
                    parts.append(" ")
                    owners.append(None)
                parts.append(word.text)
                owners.extend([word] * len(word.text))
        self.text = "".join(parts)
        self._owners = owners

    def span(self, start: int, end: int) -> Span:
        seen: list[OCRWord] = []
        for owner in self._owners[start:end]:
            if owner is not None and (not seen or seen[-1] is not owner):
                seen.append(owner)
        return Span(start=start, end=end, words=tuple(seen))

    def finditer(self, pattern: re.Pattern[str]) -> Iterator[re.Match[str]]:
        return pattern.finditer(self.text)

    def lines(self) -> Iterator[tuple[int, int, str]]:
        """Yield ``(start, end, text)`` for every line."""
        pos = 0
        for line in self.text.split("\n"):
            yield pos, pos + len(line), line
            pos += len(line) + 1


# --- OCR normalisation helpers ---------------------------------------------------------

_DIGIT_FIXES = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "D": "0",
        "Q": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "i": "1",
        "L": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "Z": "2",
        "z": "2",
        "G": "6",
        "g": "9",
        "T": "7",
    }
)


def fix_digits(text: str) -> str:
    """Map common OCR letter/digit confusions inside a numeric context."""
    return text.translate(_DIGIT_FIXES)


def luhn_valid(digits: str) -> bool:
    """Luhn check (used by the Dominican cédula / licence number check digit)."""
    if not digits.isdigit() or len(digits) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
