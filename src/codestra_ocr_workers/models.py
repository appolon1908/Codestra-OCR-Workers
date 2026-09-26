"""Wire models.

Three groups:

* **Worker API** (``/internal/v1/ocr/extract``): request + response envelope carrying a
  ``result`` that conforms to Codestra-Document-Schemas ``do-driver-licence`` v1 and a
  ``diagnostics`` block with worker-specific detail.
* **Document-Intelligence adapter** (``/v1/ocr/extract``): ``codestra.ocr.extract-request/v1``
  -> ``codestra.ocr.extraction-result/v1`` as consumed by Codestra-Document-Intelligence.
* Health / capabilities / errors.

Vendored copies of both upstream JSON Schemas live in ``contracts/`` and tests validate
real responses against them. ``openapi/openapi.json`` is checked for exact match in CI.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Static wire ceiling (runtime limit is OCR_WORKER_MAX_IMAGE_BYTES, which may be lower).
MAX_BASE64_CHARS = 22_369_624  # base64 length of 16 MiB

SideLiteral = Literal["front", "back", "unknown"]
MediaTypeLiteral = Literal["image/jpeg", "image/png", "image/webp"]
Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Confidence = Annotated[float, Field(ge=0, le=1)]
IsoDate = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]


def _is_none(value: Any) -> bool:
    return value is None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- worker API: request ---------------------------------------------------------------


class ImageInput(_Strict):
    side: SideLiteral = Field(
        default="unknown",
        description="Card side. With two 'unknown' images the first is treated as front.",
    )
    media_type: MediaTypeLiteral | None = Field(
        default=None, description="Optional; must match the sniffed content type if given."
    )
    sha256: Sha256Hex | None = Field(
        default=None, description="Optional lowercase hex digest; verified against the bytes."
    )
    content_base64: str = Field(
        min_length=16,
        max_length=MAX_BASE64_CHARS,
        description="Base64 image bytes (data: URLs accepted). Never persisted.",
    )


class ExtractOptions(_Strict):
    engine: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_\-]{1,32}$",
        description="OCR engine name; defaults to the worker's default engine.",
    )
    decode_qr: bool = True


class ExtractRequest(_Strict):
    scan_id: Id = Field(description="Caller correlation ID, echoed into the result.")
    tenant_id: Id = Field(description="Caller tenant ID, echoed; the worker stores nothing.")
    request_id: Id | None = None
    document_type: str = Field(
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
        examples=["driver_license"],
        description="'driver_licence' is accepted as an alias of 'driver_license'.",
    )
    country: str = Field(pattern=r"^[A-Z]{2}$", examples=["DO"])
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$", examples=["1.0.0"])
    images: list[ImageInput] = Field(min_length=1, max_length=8)
    options: ExtractOptions = Field(default_factory=ExtractOptions)


# --- worker API: Codestra-Document-Schemas do-driver-licence v1 result -----------------


class ContractEngine(_Strict):
    name: Id
    version: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")]


class ContractEvidence(_Strict):
    source: Literal["ocr", "qr", "derived"]
    confidence: Confidence
    page: int | None = Field(default=None, ge=1, le=100, exclude_if=_is_none)
    bbox: list[Confidence] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        exclude_if=_is_none,
        description="Normalised [x_min, y_min, x_max, y_max].",
    )


class LicenceFields(_Strict):
    full_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    document_number: (
        Annotated[
            str,
            StringConstraints(pattern=r"^(?:[0-9]{11}|[0-9]{3}-[0-9]{7}-[0-9])$"),
        ]
        | None
    ) = Field(description="Transient only; never persist or log the clear value.")
    address: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None
    height: Annotated[float, Field(ge=0.3, le=3)] | None = Field(description="Metres.")
    weight_lb: Annotated[float, Field(ge=1, le=1500)] | None
    sex: Literal["M", "F", "X"] | None
    blood_type: Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"] | None
    birth_date: IsoDate | None
    issue_date: IsoDate | None
    expiry_date: IsoDate | None
    category: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9 ./\-]{0,19}$")] | None
    restriction: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    first_issue_date: IsoDate | None
    card_serial: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9\-]{0,39}$")] | None
    )


ContractWarningCode = Literal[
    "LOW_CONFIDENCE",
    "MISSING_FIELD",
    "QR_UNREADABLE",
    "QR_MISMATCH",
    "UNSUPPORTED_LAYOUT",
    "DATE_ORDER",
    "REVIEW_REQUIRED",
]


class ContractWarning(_Strict):
    code: ContractWarningCode
    field: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$", exclude_if=_is_none)
    severity: Literal["info", "warning", "error"]


class QRDecoded(_Strict):
    status: Literal["decoded"]
    payload_digest: Digest
    raw_payload: str | None = Field(
        default=None,
        max_length=8192,
        exclude_if=_is_none,
        description="Only included for HTTPS URLs on an allowlisted issuing-authority host.",
    )


class QRNotDecoded(_Strict):
    status: Literal["not_detected", "unreadable"]


class ContentDigest(_Strict):
    role: Literal["front", "back", "document"]
    digest: Digest


class DoDriverLicenceResult(_Strict):
    """Codestra-Document-Schemas ``do-driver-licence.schema.json`` (contract 1.0.0)."""

    scan_id: Id
    tenant_id: Id
    schema_version: Literal["1.0.0"]
    document_type: Literal["driver_license"]
    country: Literal["DO"]
    engine: ContractEngine
    fields: LicenceFields
    field_evidence: dict[str, list[ContractEvidence]]
    warnings: list[ContractWarning] = Field(max_length=100)
    qr_evidence: QRDecoded | QRNotDecoded
    content_digests: list[ContentDigest] = Field(min_length=1, max_length=10)


# --- worker API: diagnostics and envelope ----------------------------------------------


class EngineReport(BaseModel):
    name: str
    version: str
    languages: list[str]


class FieldDiagnostics(BaseModel):
    status: Literal["extracted", "low_confidence", "missing"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    sensitivity: Literal["public", "personal", "restricted"]
    method: Literal["label", "pattern", "layout", "qr"] | None = None
    label: str | None = None
    side: SideLiteral | None = None
    checks: dict[str, bool] = Field(default_factory=dict)


class WarningModel(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    field: str | None = None


class QRModel(BaseModel):
    scanned: bool
    detected: bool
    decoded: bool
    digest_sha256: str | None = Field(default=None, description="Hex SHA-256 of the payload")
    payload_kind: Literal["url", "text"] | None = None
    authority_host: str | None = None
    allowlisted: bool = False
    authority_url: str | None = Field(
        default=None, description="Only set for HTTPS URLs on an allowlisted host. Never fetched."
    )
    fetched: Literal[False] = False


class PageReport(BaseModel):
    index: int
    side: SideLiteral
    media_type: MediaTypeLiteral
    width: int
    height: int
    mean_confidence: float
    word_count: int
    preprocessing: list[str]
    attempts: int


class Timings(BaseModel):
    decode_ms: float
    ocr_ms: float
    qr_ms: float
    extract_ms: float
    total_ms: float


class Diagnostics(BaseModel):
    schema_id: str
    engine: EngineReport
    fields: dict[str, FieldDiagnostics]
    warnings: list[WarningModel]
    qr: QRModel
    pages: list[PageReport]
    timings: Timings


class ExtractResponse(BaseModel):
    request_id: str
    status: Literal["succeeded", "partial"]
    review_required: bool
    retention: Literal["transient"] = "transient"
    result: DoDriverLicenceResult
    diagnostics: Diagnostics


# --- Document-Intelligence adapter (codestra.ocr.*/v1) ---------------------------------

DI_REQUEST_REF = "codestra.ocr.extract-request/v1"
DI_RESULT_REF = "codestra.ocr.extraction-result/v1"


class DIImage(_Strict):
    side: Literal["front", "back"]
    media_type: Literal["image/jpeg", "image/png"]
    sha256: Sha256Hex
    content_base64: str = Field(max_length=MAX_BASE64_CHARS)


class DIExtractRequest(_Strict):
    schema_ref: Literal["codestra.ocr.extract-request/v1"]
    request_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
    document_type: str
    country: str = Field(pattern=r"^[A-Z]{2,3}$")
    expected_response_schema_ref: Literal["codestra.ocr.extraction-result/v1"]
    images: list[DIImage] = Field(min_length=1, max_length=2)


class DIWorker(_Strict):
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    engine: str | None = Field(default=None, max_length=64, exclude_if=_is_none)


class DIEvidence(_Strict):
    side: Literal["front", "back"] | None = Field(default=None, exclude_if=_is_none)
    bbox: list[Confidence] | None = Field(
        default=None, min_length=4, max_length=4, exclude_if=_is_none
    )


class DIField(_Strict):
    value: str | None = Field(max_length=512)
    confidence: Confidence
    source: Literal["visual", "mrz", "barcode", "qr"] | None = Field(
        default=None, exclude_if=_is_none
    )
    evidence: DIEvidence | None = Field(default=None, exclude_if=_is_none)


class DIImageDigests(_Strict):
    front: Sha256Hex
    back: Sha256Hex | None = Field(default=None, exclude_if=_is_none)


class DIQuality(_Strict):
    overall_confidence: Confidence
    warnings: list[Annotated[str, StringConstraints(max_length=128)]] = Field(max_length=32)


class DIQR(_Strict):
    present: bool
    source_lookup_url: str | None = Field(default=None, max_length=2048)


class DIExtractionResult(_Strict):
    schema_ref: Literal["codestra.ocr.extraction-result/v1"]
    request_id: str
    worker: DIWorker
    document_type: str
    country: str
    fields: dict[str, DIField]
    image_digests: DIImageDigests
    quality: DIQuality
    qr: DIQR


# --- errors / health / capabilities ----------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(description="Worker error code (stable, snake_case).")
    contract_code: Literal[
        "INVALID_INPUT",
        "UNSUPPORTED_DOCUMENT",
        "EXTRACTION_FAILED",
        "TIMEOUT",
        "RATE_LIMITED",
        "INTERNAL_ERROR",
    ] = Field(description="Codestra-Document-Schemas error.schema.json code.")
    stage: Literal["ingest", "extract", "validate", "review"]
    retryable: bool
    message: str = Field(description="Static, value-free description.")
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class CheckResult(BaseModel):
    ok: bool
    detail: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


class EngineCapability(BaseModel):
    name: str
    version: str
    languages: list[str]
    capabilities: list[str]
    ready: bool
    default: bool


class FieldCapability(BaseModel):
    name: str
    kind: Literal["string", "number", "date", "enum"]
    required: bool
    sensitivity: Literal["public", "personal", "restricted"]
    expected_side: SideLiteral
    unit: str | None
    description: str


class DocumentCapability(BaseModel):
    country: str
    document_type: str
    document_type_aliases: list[str]
    schema_id: str
    schema_versions: list[str]
    sides: list[SideLiteral]
    fields: list[FieldCapability]


class LimitsCapability(BaseModel):
    max_images: int
    max_image_bytes: int
    max_request_bytes: int
    max_image_pixels: int
    max_image_side: int
    min_image_width: int
    min_image_height: int
    request_timeout_seconds: float
    max_concurrency: int
    supported_media_types: list[MediaTypeLiteral]


class QRCapability(BaseModel):
    decoder: str
    allowed_hosts: list[str]
    fetches_urls: Literal[False] = False


class ContractCapability(BaseModel):
    endpoint: str
    request: str
    response: str


class CapabilitiesResponse(BaseModel):
    service: str
    version: str
    engines: list[EngineCapability]
    documents: list[DocumentCapability]
    contracts: list[ContractCapability]
    limits: LimitsCapability
    qr: QRCapability
    low_confidence_threshold: float
