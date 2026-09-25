"""Engine-neutral OCR data model and adapter interface.

Every OCR backend (Tesseract today; PaddleOCR, EasyOCR, cloud engines later) implements
:class:`OCREngine` and returns :class:`OCRPage`. Extractors only ever see ``OCRPage``, so
document parsing logic is written once and reused across engines.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ..imaging import Image

BBox = tuple[int, int, int, int]  # x, y, width, height in preprocessed-image pixels


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float  # 0..1
    bbox: BBox


@dataclass(frozen=True)
class OCRLine:
    words: tuple[OCRWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def confidence(self) -> float:
        if not self.words:
            return 0.0
        return sum(w.confidence for w in self.words) / len(self.words)


@dataclass(frozen=True)
class OCRPage:
    lines: tuple[OCRLine, ...]
    width: int
    height: int

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        words = [w for line in self.lines for w in line.words]
        if not words:
            return 0.0
        return sum(w.confidence for w in words) / len(words)

    @property
    def word_count(self) -> int:
        return sum(len(line.words) for line in self.lines)


@dataclass(frozen=True)
class EngineInfo:
    name: str
    version: str
    languages: tuple[str, ...]
    capabilities: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecognizeOptions:
    languages: tuple[str, ...]
    timeout_seconds: float
    # Engine-agnostic layout hint; adapters map it to their own knob (e.g. Tesseract PSM).
    layout: str = "block"  # block | sparse | single_line


class OCREngine(abc.ABC):
    """Adapter contract for OCR backends.

    Implementations must be thread-safe for concurrent ``recognize`` calls (the worker
    runs them in a thread pool bounded by ``max_concurrency``), must not write image data
    to persistent storage, and must honour ``options.timeout_seconds``.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def info(self) -> EngineInfo:
        """Static engine metadata, including the backend version string."""

    @abc.abstractmethod
    def readiness(self) -> tuple[bool, str]:
        """Return ``(ready, detail)``; must be cheap and side-effect free."""

    @abc.abstractmethod
    def recognize(self, image: Image, options: RecognizeOptions) -> OCRPage:
        """Run OCR on a preprocessed grayscale image."""
