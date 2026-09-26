"""Render a :class:`PipelineResult` into the two supported wire contracts."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from . import __version__
from .extractors.base import FieldResult, Severity, Side, WarningCode
from .models import (
    DI_RESULT_REF,
    DIQR,
    ContentDigest,
    ContractEngine,
    ContractEvidence,
    ContractWarning,
    Diagnostics,
    DIEvidence,
    DIExtractionResult,
    DIField,
    DIImageDigests,
    DIQuality,
    DIWorker,
    DoDriverLicenceResult,
    EngineReport,
    ExtractResponse,
    FieldDiagnostics,
    LicenceFields,
    PageReport,
    QRDecoded,
    QRModel,
    QRNotDecoded,
    Timings,
    WarningModel,
)
from .pipeline import PipelineResult, field_status

# Worker warning -> Codestra-Document-Schemas warning code (None: diagnostics only).
CONTRACT_WARNING: dict[WarningCode, str | None] = {
    WarningCode.REQUIRED_FIELD_MISSING: "MISSING_FIELD",
    WarningCode.FIELD_MISSING: "MISSING_FIELD",
    WarningCode.LOW_CONFIDENCE: "LOW_CONFIDENCE",
    WarningCode.CHECKSUM_FAILED: "LOW_CONFIDENCE",
    WarningCode.LOW_PAGE_CONFIDENCE: "LOW_CONFIDENCE",
    WarningCode.DATE_INCONSISTENT: "DATE_ORDER",
    WarningCode.QR_NOT_DECODED: "QR_UNREADABLE",
    WarningCode.QR_NOT_URL: "QR_MISMATCH",
    WarningCode.QR_HOST_NOT_ALLOWLISTED: "QR_MISMATCH",
    WarningCode.DOCUMENT_EXPIRED: None,
    WarningCode.SIDE_MISSING: None,
    WarningCode.QR_NOT_FOUND: None,
}
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_VERSION_SAFE = re.compile(r"[^A-Za-z0-9._+\-]")


def _safe_version(version: str) -> str:
    cleaned = _VERSION_SAFE.sub("-", version)[:64]
    return cleaned if cleaned[:1].isalnum() else "unknown"


def _norm_bbox(res: FieldResult, result: PipelineResult) -> list[float] | None:
    if res.evidence is None or res.evidence.bbox is None:
        return None
    page = result.pages[res.evidence.page_index].page
    if page.width <= 0 or page.height <= 0:
        return None
    x, y, w, h = res.evidence.bbox

    def clamp(v: float) -> float:
        return round(min(1.0, max(0.0, v)), 4)

    return [
        clamp(x / page.width),
        clamp(y / page.height),
        clamp((x + w) / page.width),
        clamp((y + h) / page.height),
    ]


def contract_warnings(result: PipelineResult) -> list[ContractWarning]:
    merged: dict[tuple[str, str | None], str] = {}
    for w in result.output.warnings:
        code = CONTRACT_WARNING.get(w.code)
        if code is None:
            continue
        key = (code, w.field)
        sev = w.severity.value
        if key not in merged or _SEVERITY_RANK[sev] > _SEVERITY_RANK[merged[key]]:
            merged[key] = sev
    if result.review_required:
        merged[("REVIEW_REQUIRED", None)] = Severity.WARNING.value
    return [
        ContractWarning(code=code, field=field, severity=sev)
        for (code, field), sev in list(merged.items())[:100]
    ]


def _licence_fields(values: dict[str, Any]) -> tuple[LicenceFields, list[str]]:
    """Build contract fields; values the contract rejects are nulled, never coerced."""
    rejected: list[str] = []
    while True:
        try:
            return LicenceFields(**values), rejected
        except ValidationError as exc:
            bad = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            bad &= set(values)
            if not bad or all(values[b] is None for b in bad):
                raise
            for name in bad:
                values[name] = None
                rejected.append(name)


def to_contract(result: PipelineResult, *, scan_id: str, tenant_id: str) -> DoDriverLicenceResult:
    values: dict[str, Any] = {name: res.value for name, res in result.output.fields.items()}
    fields, rejected = _licence_fields(values)
    evidence: dict[str, list[ContractEvidence]] = {}
    for name, res in result.output.fields.items():
        if not res.present or name in rejected or res.confidence is None or res.evidence is None:
            continue
        evidence[name] = [
            ContractEvidence(
                source="ocr",
                confidence=res.confidence,
                page=res.evidence.page_index + 1,
                bbox=_norm_bbox(res, result),
            )
        ]
    warnings = contract_warnings(result)
    for name in rejected:
        warnings.insert(0, ContractWarning(code="MISSING_FIELD", field=name, severity="warning"))

    qr = result.qr
    qr_evidence: QRDecoded | QRNotDecoded
    if not result.qr_scanned or not qr.detected:
        qr_evidence = QRNotDecoded(status="not_detected")
    elif not qr.decoded or qr.digest_sha256 is None:
        qr_evidence = QRNotDecoded(status="unreadable")
    else:
        qr_evidence = QRDecoded(
            status="decoded",
            payload_digest=f"sha256:{qr.digest_sha256}",
            raw_payload=qr.authority_url,
        )

    digests: list[ContentDigest] = []
    for m in result.meta:
        role = m.side.value if m.side in (Side.FRONT, Side.BACK) else "document"
        item = ContentDigest(role=role, digest=f"sha256:{m.sha256}")
        if item not in digests:
            digests.append(item)

    return DoDriverLicenceResult(
        scan_id=scan_id,
        tenant_id=tenant_id,
        schema_version="1.0.0",
        document_type="driver_license",
        country="DO",
        engine=ContractEngine(
            name=result.engine.name, version=_safe_version(result.engine.version)
        ),
        fields=fields,
        field_evidence=evidence,
        warnings=warnings[:100],
        qr_evidence=qr_evidence,
        content_digests=digests[:10],
    )


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def to_diagnostics(result: PipelineResult, threshold: float) -> Diagnostics:
    fields: dict[str, FieldDiagnostics] = {}
    for name, res in result.output.fields.items():
        ev = res.evidence
        fields[name] = FieldDiagnostics(
            status=field_status(res, threshold),
            confidence=res.confidence,
            sensitivity=res.spec.sensitivity.value,
            method=ev.method if ev else None,
            label=ev.label if ev else None,
            side=ev.side.value if ev else None,
            checks=res.checks,
        )
    qr = result.qr
    t = result.timing
    return Diagnostics(
        schema_id=result.extractor.schema_id,
        engine=EngineReport(
            name=result.engine.name,
            version=result.engine.version,
            languages=list(result.engine.languages),
        ),
        fields=fields,
        warnings=[
            WarningModel(
                code=w.code.value, severity=w.severity.value, message=w.message, field=w.field
            )
            for w in result.output.warnings
        ],
        qr=QRModel(
            scanned=result.qr_scanned,
            detected=qr.detected,
            decoded=qr.decoded,
            digest_sha256=qr.digest_sha256,
            payload_kind=qr.payload_kind,
            authority_host=qr.authority_host,
            allowlisted=qr.allowlisted,
            authority_url=qr.authority_url,
        ),
        pages=[
            PageReport(
                index=m.index,
                side=m.side.value,
                media_type=m.media_type,
                width=m.width,
                height=m.height,
                mean_confidence=m.mean_confidence,
                word_count=m.word_count,
                preprocessing=list(m.preprocessing),
                attempts=m.attempts,
            )
            for m in result.meta
        ],
        timings=Timings(
            decode_ms=_ms(t.decode),
            ocr_ms=_ms(t.ocr),
            qr_ms=_ms(t.qr),
            extract_ms=_ms(t.extract),
            total_ms=_ms(t.total),
        ),
    )


def to_worker_response(
    result: PipelineResult, *, request_id: str, scan_id: str, tenant_id: str, threshold: float
) -> ExtractResponse:
    return ExtractResponse(
        request_id=request_id,
        status=result.status,
        review_required=result.review_required,
        result=to_contract(result, scan_id=scan_id, tenant_id=tenant_id),
        diagnostics=to_diagnostics(result, threshold),
    )


# --- Document-Intelligence adapter ------------------------------------------------------


def _di_value(value: str | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)[:512]


def overall_confidence(result: PipelineResult) -> float:
    fields = list(result.output.fields.values())
    present = [f.confidence for f in fields if f.present and f.confidence is not None]
    required = [f for f in fields if f.spec.required]
    if not present or not required:
        return 0.0
    coverage = sum(f.present for f in required) / len(required)
    return round(sum(present) / len(present) * coverage, 4)


def to_di_response(
    result: PipelineResult,
    *,
    request_id: str,
    document_type: str,
    country: str,
    service_name: str,
) -> DIExtractionResult:
    fields: dict[str, DIField] = {}
    for name, res in result.output.fields.items():
        evidence = None
        if res.present and res.evidence is not None:
            side = res.evidence.side.value if res.evidence.side is not Side.UNKNOWN else None
            evidence = DIEvidence(side=side, bbox=_norm_bbox(res, result))
        fields[name] = DIField(
            value=_di_value(res.value),
            confidence=res.confidence if res.present and res.confidence is not None else 0.0,
            source="visual" if res.present else None,
            evidence=evidence,
        )
    by_side = {m.side: m.sha256 for m in result.meta}
    warnings = [f"{w.code}:{w.field}" if w.field else w.code for w in contract_warnings(result)][
        :32
    ]
    engine = f"{result.engine.name}-{_safe_version(result.engine.version)}"[:64]
    return DIExtractionResult(
        schema_ref=DI_RESULT_REF,
        request_id=request_id,
        worker=DIWorker(name=service_name[:64], version=__version__, engine=engine),
        document_type=document_type,
        country=country,
        fields=fields,
        image_digests=DIImageDigests(front=by_side[Side.FRONT], back=by_side.get(Side.BACK)),
        quality=DIQuality(overall_confidence=overall_confidence(result), warnings=warnings),
        qr=DIQR(present=result.qr.detected, source_lookup_url=result.qr.authority_url),
    )
