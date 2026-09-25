"""Engine- and contract-neutral extraction pipeline.

guards -> decode -> digest -> preprocess -> OCR (with retry variant) -> QR -> parse.
Both API surfaces (worker API and Document-Intelligence adapter) call :meth:`run` and only
differ in how they render :class:`PipelineResult`.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from .config import Settings
from .deadline import Deadline
from .engines.base import EngineInfo, OCREngine, OCRPage, RecognizeOptions
from .engines.registry import EngineRegistry
from .errors import EngineUnavailableError, InvalidImageError
from .extractors.base import (
    ExtractionOutput,
    Extractor,
    FieldResult,
    PageInput,
    Severity,
    Side,
)
from .extractors.registry import get_extractor
from .imaging import Image, ImageLimits, decode_base64, load_image, preprocess
from .metrics import Metrics
from .qr import QRResult, classify, decode_payload


@dataclass(frozen=True)
class ImageJob:
    side: str  # front | back | unknown
    content_base64: str
    media_type: str | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True)
class PageMeta:
    index: int
    side: Side
    media_type: str
    width: int
    height: int
    sha256: str
    preprocessing: tuple[str, ...]
    attempts: int
    mean_confidence: float
    word_count: int


@dataclass
class Timing:
    decode: float = 0.0
    ocr: float = 0.0
    qr: float = 0.0
    extract: float = 0.0
    total: float = 0.0


@dataclass
class PipelineResult:
    extractor: Extractor
    schema_version: str
    engine: EngineInfo
    pages: list[PageInput]
    meta: list[PageMeta]
    output: ExtractionOutput
    qr: QRResult
    qr_scanned: bool
    timing: Timing = field(default_factory=Timing)

    @property
    def required_missing(self) -> bool:
        return any(r.spec.required and not r.present for r in self.output.fields.values())

    @property
    def review_required(self) -> bool:
        return self.required_missing or any(
            w.severity in (Severity.WARNING, Severity.ERROR) for w in self.output.warnings
        )

    @property
    def status(self) -> str:
        return "partial" if self.required_missing else "succeeded"


def assign_sides(requested: list[str]) -> list[Side]:
    """Resolve 'unknown' sides for the common 1-2 image case (front first)."""
    sides = [Side(s) for s in requested]
    if len(sides) > 2:
        return sides
    for i, side in enumerate(sides):
        if side is Side.UNKNOWN:
            taken = set(sides)
            if Side.FRONT not in taken:
                sides[i] = Side.FRONT
            elif Side.BACK not in taken:
                sides[i] = Side.BACK
    return sides


class ExtractionPipeline:
    def __init__(self, settings: Settings, engines: EngineRegistry, metrics: Metrics) -> None:
        self.settings = settings
        self.engines = engines
        self.metrics = metrics
        self.limits = ImageLimits(
            max_bytes=settings.max_image_bytes,
            max_pixels=settings.max_image_pixels,
            max_side=settings.max_image_side,
            min_width=settings.min_image_width,
            min_height=settings.min_image_height,
        )

    def _recognize(
        self, engine: OCREngine, pixels: Image, deadline: Deadline
    ) -> tuple[OCRPage, tuple[str, ...], int]:
        languages = tuple(self.settings.tesseract_languages)
        best: tuple[OCRPage, tuple[str, ...]] | None = None
        attempts = 0
        for variant in ("clahe", "otsu"):
            if best is not None and (
                best[0].mean_confidence >= self.settings.retry_preprocessing_below
                or deadline.remaining() < 2.0
            ):
                break
            prep = preprocess(pixels, variant)
            timeout = deadline.check("ocr")
            started = time.perf_counter()
            page = engine.recognize(
                prep.image, RecognizeOptions(languages=languages, timeout_seconds=timeout)
            )
            self.metrics.engine_duration.labels(engine.name).observe(time.perf_counter() - started)
            attempts += 1
            if best is None or page.mean_confidence > best[0].mean_confidence:
                best = (page, (f"variant:{variant}", *prep.steps))
        assert best is not None  # noqa: S101 - the loop always runs at least once
        return best[0], best[1], attempts

    def run(
        self,
        *,
        document_type: str,
        country: str,
        schema_version: str | None,
        images: list[ImageJob],
        engine_name: str | None = None,
        decode_qr: bool = True,
    ) -> PipelineResult:
        started = time.perf_counter()
        deadline = Deadline(self.settings.request_timeout_seconds)
        timing = Timing()
        if len(images) > self.settings.max_images:
            raise InvalidImageError(
                f"at most {self.settings.max_images} images per request", code="too_many_images"
            )
        extractor = get_extractor(country, document_type, schema_version)
        engine = self.engines.get(engine_name)
        ready, _detail = engine.readiness()
        if not ready:
            raise EngineUnavailableError(f"engine {engine.name} is not ready")
        info = engine.info()

        sides = assign_sides([img.side for img in images])
        pages: list[PageInput] = []
        meta: list[PageMeta] = []
        qr_detected, qr_payload = False, None
        qr_scanned = False
        for index, (job, side) in enumerate(zip(images, sides, strict=True)):
            t0 = time.perf_counter()
            raw = decode_base64(job.content_base64, max_bytes=self.limits.max_bytes)
            digest = hashlib.sha256(raw).hexdigest()
            if job.expected_sha256 is not None and job.expected_sha256 != digest:
                raise InvalidImageError(
                    f"image {index} sha256 does not match its content", code="digest_mismatch"
                )
            decoded = load_image(raw, self.limits, declared_media_type=job.media_type)
            del raw
            timing.decode += time.perf_counter() - t0
            deadline.check("decode")

            t0 = time.perf_counter()
            page, steps, attempts = self._recognize(engine, decoded.pixels, deadline)
            timing.ocr += time.perf_counter() - t0

            # The QR sits on the reverse; only scan the front when it is the sole image.
            if decode_qr and qr_payload is None and (side is not Side.FRONT or len(images) == 1):
                t0 = time.perf_counter()
                qr_scanned = True
                detected, qr_payload = decode_payload(
                    decoded.pixels, lambda: not deadline.expired()
                )
                qr_detected = qr_detected or detected
                timing.qr += time.perf_counter() - t0

            pages.append(PageInput(index=index, side=side, page=page))
            meta.append(
                PageMeta(
                    index=index,
                    side=side,
                    media_type=decoded.media_type,
                    width=decoded.width,
                    height=decoded.height,
                    sha256=digest,
                    preprocessing=steps,
                    attempts=attempts,
                    mean_confidence=round(page.mean_confidence, 4),
                    word_count=page.word_count,
                )
            )
            del decoded

        qr = classify(
            qr_payload, detected=qr_detected, allowed_hosts=self.settings.qr_allowed_hosts
        )
        if qr_scanned:
            self.metrics.qr.labels(_qr_metric(qr)).inc()

        t0 = time.perf_counter()
        deadline.check("extract")
        output = extractor.extract(
            pages, qr if qr_scanned else None, self.settings.low_confidence_threshold
        )
        timing.extract = time.perf_counter() - t0
        timing.total = time.perf_counter() - started

        result = PipelineResult(
            extractor=extractor,
            schema_version=schema_version or extractor.schema_versions[-1],
            engine=info,
            pages=pages,
            meta=meta,
            output=output,
            qr=qr,
            qr_scanned=qr_scanned,
            timing=timing,
        )
        threshold = self.settings.low_confidence_threshold
        for name, res in output.fields.items():
            self.metrics.fields.labels(
                extractor.document_type, name, field_status(res, threshold)
            ).inc()
        return result


def field_status(res: FieldResult, threshold: float) -> str:
    if not res.present:
        return "missing"
    if res.confidence is not None and res.confidence < threshold:
        return "low_confidence"
    return "extracted"


def _qr_metric(qr: QRResult) -> str:
    if not qr.detected:
        return "not_found"
    if not qr.decoded:
        return "not_decoded"
    if qr.allowlisted:
        return "allowlisted"
    return "not_allowlisted" if qr.payload_kind == "url" else "text"
