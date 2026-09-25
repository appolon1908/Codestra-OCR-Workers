"""FastAPI application factory for the internal OCR worker."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import Settings, get_settings
from .engines.registry import EngineRegistry
from .errors import InvalidImageError, UnauthorizedError, WorkerBusyError, WorkerError
from .extractors.registry import all_extractors
from .imaging import SUPPORTED_MEDIA_TYPES
from .metrics import Metrics
from .models import (
    DI_REQUEST_REF,
    DI_RESULT_REF,
    CapabilitiesResponse,
    CheckResult,
    ContractCapability,
    DIExtractionResult,
    DIExtractRequest,
    DocumentCapability,
    EngineCapability,
    ErrorResponse,
    ExtractRequest,
    ExtractResponse,
    FieldCapability,
    HealthResponse,
    LimitsCapability,
    QRCapability,
    ReadinessResponse,
)
from .pipeline import ExtractionPipeline, ImageJob, PipelineResult
from .renderers import to_di_response, to_worker_response

log = logging.getLogger("codestra_ocr_workers")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": desc}
    for code, desc in {
        401: "Missing or invalid internal token",
        413: "Payload or image exceeds limits",
        422: "Invalid request, image or unsupported document/schema",
        502: "OCR engine failed",
        503: "Worker busy or engine unavailable",
        504: "Extraction deadline exceeded",
    }.items()
}
DESCRIPTION = (
    "Internal-only service. Accepts bounded document images, runs a pluggable OCR engine "
    "(Tesseract + OpenCV preprocessing by default) and returns transient, schema-mapped "
    "extraction results with per-field confidence and evidence. It never persists images, "
    "never fetches QR URLs and does not own client or business records."
)


def _error(
    status: int,
    code: str,
    message: str,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    *,
    contract_code: str = "INVALID_INPUT",
    stage: str = "ingest",
    retryable: bool = False,
) -> JSONResponse:
    body = ErrorResponse.model_validate(
        {
            "error": {
                "code": code,
                "contract_code": contract_code,
                "stage": stage,
                "retryable": retryable,
                "message": message,
                "request_id": request_id,
            }
        }
    )
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


class BodySizeLimitMiddleware:
    """Rejects bodies above ``max_bytes`` before the application buffers them.

    Declared ``Content-Length`` is checked up front (the ASGI server enforces that the
    body matches it). Bodies without a length (chunked) are pre-read up to the limit and
    replayed, so the application never sees more than ``max_bytes``.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, on_reject: Any = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.on_reject = on_reject

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.on_reject is not None:
            self.on_reject()
        response = _error(413, "payload_too_large", "request body exceeds limit")
        await response(scope, receive, send)


def create_app(settings: Settings | None = None, engines: EngineRegistry | None = None) -> FastAPI:
    settings = settings or get_settings()
    metrics = Metrics()
    registry = engines or EngineRegistry.from_settings(settings)
    pipeline = ExtractionPipeline(settings, registry, metrics)
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    app = FastAPI(
        title="Codestra OCR Worker",
        version=__version__,
        summary="Internal OCR worker runtime and engine adapter layer.",
        description=DESCRIPTION,
        docs_url=None,
        redoc_url=None,
    )

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                summary=app.summary,
                description=app.description,
                routes=app.routes,
            )
            # Validation errors use ErrorResponse; drop FastAPI's unused default models.
            components = schema.get("components", {}).get("schemas", {})
            for name in ("HTTPValidationError", "ValidationError"):
                components.pop(name, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = _openapi  # type: ignore[method-assign]
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.engines = registry
    app.state.pipeline = pipeline
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        on_reject=lambda: metrics.rejections.labels("request_bytes").inc(),
    )

    def require_token(request: Request) -> None:
        """Accept ``Authorization: Bearer`` or ``X-Internal-Token``.

        Headers are read directly so the token never appears as an OpenAPI parameter.
        """
        expected = settings.internal_token
        if expected is None:
            return
        supplied = request.headers.get("x-internal-token")
        auth = request.headers.get("authorization", "")
        if supplied is None and auth[:7].lower() == "bearer ":
            supplied = auth[7:].strip()
        if supplied is None or not hmac.compare_digest(
            supplied.encode(), expected.get_secret_value().encode()
        ):
            raise UnauthorizedError("missing or invalid internal token")

    @app.exception_handler(WorkerError)
    async def _worker_error(request: Request, exc: WorkerError) -> JSONResponse:
        headers = {"Retry-After": "1"} if isinstance(exc, WorkerBusyError) else None
        return _error(
            exc.status_code,
            exc.code,
            exc.message,
            getattr(request.state, "request_id", None),
            headers,
            contract_code=exc.contract_code,
            stage=exc.stage,
            retryable=exc.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo locations and types only: validation inputs may contain image/OCR data.
        problems = [
            {"loc": [str(p) for p in err.get("loc", ())], "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return _error(
            422,
            "invalid_request",
            json.dumps(problems)[:2000],
            getattr(request.state, "request_id", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "unhandled error",
            extra={"request_id": getattr(request.state, "request_id", None)},
            exc_info=exc,
        )
        return _error(
            500,
            "internal_error",
            "internal error",
            getattr(request.state, "request_id", None),
            contract_code="INTERNAL_ERROR",
            stage="extract",
        )

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if _REQUEST_ID_RE.match(incoming) else str(uuid.uuid4())
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def _run(
        request_id: str, document_type: str, country: str, n_images: int, **kwargs: Any
    ) -> PipelineResult:
        """Admission control + threadpool execution shared by both extract routes."""
        started = time.perf_counter()
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=settings.queue_timeout_seconds)
        except TimeoutError:
            metrics.rejections.labels("concurrency").inc()
            metrics.requests.labels("worker_busy", document_type, country).inc()
            raise WorkerBusyError("worker at capacity; retry later") from None
        metrics.inflight.inc()
        outcome = "error"
        try:
            # The deadline is enforced inside the pipeline (engine subprocess timeouts and
            # stage checks), so the semaphore is only released once work really stops.
            result = await run_in_threadpool(
                pipeline.run, document_type=document_type, country=country, **kwargs
            )
            outcome = result.status
            log.info(
                "extraction completed",
                extra={
                    "request_id": request_id,
                    "outcome": outcome,
                    "document_type": result.extractor.document_type,
                    "country": country,
                    "engine": result.engine.name,
                    "images": n_images,
                    "fields_extracted": sum(f.present for f in result.output.fields.values()),
                    "warnings": len(result.output.warnings),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return result
        except WorkerError as exc:
            outcome = exc.code
            log.warning(
                "extraction rejected",
                extra={
                    "request_id": request_id,
                    "outcome": exc.code,
                    "document_type": document_type,
                    "country": country,
                },
            )
            raise
        finally:
            semaphore.release()
            metrics.inflight.dec()
            metrics.requests.labels(outcome, document_type, country).inc()
            metrics.duration.labels(document_type).observe(time.perf_counter() - started)

    @app.get("/healthz", response_model=HealthResponse, tags=["health"], summary="Liveness probe")
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=settings.service_name, version=__version__)

    async def _readiness() -> JSONResponse:
        checks: dict[str, CheckResult] = {}
        for engine in registry.all():
            ok, detail = await run_in_threadpool(engine.readiness)
            metrics.ready.labels(engine.name).set(1 if ok else 0)
            checks[f"engine:{engine.name}"] = CheckResult(ok=ok, detail=detail)
        default_ok = checks[f"engine:{registry.default}"].ok
        body = ReadinessResponse(status="ready" if default_ok else "not_ready", checks=checks)
        return JSONResponse(status_code=200 if default_ok else 503, content=body.model_dump())

    readiness_responses: dict[int | str, dict[str, Any]] = {
        503: {"model": ReadinessResponse, "description": "Default engine not ready"}
    }

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        tags=["health"],
        summary="Readiness probe",
        responses=readiness_responses,
    )
    async def readyz() -> JSONResponse:
        return await _readiness()

    @app.get(
        "/health/ready",
        response_model=ReadinessResponse,
        tags=["health"],
        summary="Readiness probe (alias)",
        responses=readiness_responses,
    )
    async def health_ready() -> JSONResponse:
        return await _readiness()

    @app.get(
        "/metrics",
        tags=["health"],
        summary="Prometheus metrics",
        response_class=Response,
        responses={
            200: {"content": {"text/plain": {}}, "description": "Prometheus exposition format"}
        },
    )
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get(
        "/v1/capabilities",
        response_model=CapabilitiesResponse,
        tags=["capabilities"],
        summary="Engines, document types, schema versions, contracts and limits",
        dependencies=[Depends(require_token)],
        responses={401: ERROR_RESPONSES[401]},
    )
    async def capabilities() -> CapabilitiesResponse:
        engines_out = []
        for engine in registry.all():
            info = await run_in_threadpool(engine.info)
            ready, _ = await run_in_threadpool(engine.readiness)
            engines_out.append(
                EngineCapability(
                    name=info.name,
                    version=info.version,
                    languages=list(info.languages),
                    capabilities=list(info.capabilities),
                    ready=ready,
                    default=engine.name == registry.default,
                )
            )
        documents = [
            DocumentCapability(
                country=ex.country,
                document_type=ex.document_type,
                document_type_aliases=list(ex.document_type_aliases),
                schema_id=ex.schema_id,
                schema_versions=list(ex.schema_versions),
                sides=[s.value for s in ex.expected_sides],
                fields=[
                    FieldCapability(
                        name=f.name,
                        kind=f.kind,
                        required=f.required,
                        sensitivity=f.sensitivity.value,
                        expected_side=f.expected_side.value,
                        unit=f.unit,
                        description=f.description,
                    )
                    for f in ex.fields
                ],
            )
            for ex in all_extractors()
        ]
        return CapabilitiesResponse(
            service=settings.service_name,
            version=__version__,
            engines=engines_out,
            documents=documents,
            contracts=[
                ContractCapability(
                    endpoint="/internal/v1/ocr/extract",
                    request="ExtractRequest (this OpenAPI)",
                    response="result: " + documents[0].schema_id,
                ),
                ContractCapability(
                    endpoint="/v1/ocr/extract", request=DI_REQUEST_REF, response=DI_RESULT_REF
                ),
            ],
            limits=LimitsCapability(
                max_images=settings.max_images,
                max_image_bytes=settings.max_image_bytes,
                max_request_bytes=settings.max_request_bytes,
                max_image_pixels=settings.max_image_pixels,
                max_image_side=settings.max_image_side,
                min_image_width=settings.min_image_width,
                min_image_height=settings.min_image_height,
                request_timeout_seconds=settings.request_timeout_seconds,
                max_concurrency=settings.max_concurrency,
                supported_media_types=list(SUPPORTED_MEDIA_TYPES),
            ),
            qr=QRCapability(
                decoder="opencv.QRCodeDetector", allowed_hosts=settings.qr_allowed_hosts
            ),
            low_confidence_threshold=settings.low_confidence_threshold,
        )

    @app.post(
        "/internal/v1/ocr/extract",
        response_model=ExtractResponse,
        tags=["ocr"],
        summary="Extract fields; result conforms to Codestra-Document-Schemas v1",
        dependencies=[Depends(require_token)],
        responses=ERROR_RESPONSES,
    )
    async def extract(body: ExtractRequest, request: Request) -> ExtractResponse:
        request_id = body.request_id or request.state.request_id
        request.state.request_id = request_id
        result = await _run(
            request_id,
            body.document_type,
            body.country,
            len(body.images),
            schema_version=body.schema_version,
            images=[
                ImageJob(i.side, i.content_base64, i.media_type, i.sha256) for i in body.images
            ],
            engine_name=body.options.engine,
            decode_qr=body.options.decode_qr,
        )
        return to_worker_response(
            result,
            request_id=request_id,
            scan_id=body.scan_id,
            tenant_id=body.tenant_id,
            threshold=settings.low_confidence_threshold,
        )

    @app.post(
        "/v1/ocr/extract",
        response_model=DIExtractionResult,
        tags=["ocr"],
        summary="Document-Intelligence adapter (codestra.ocr.extract-request/v1)",
        dependencies=[Depends(require_token)],
        responses=ERROR_RESPONSES,
    )
    async def extract_di(body: DIExtractRequest, request: Request) -> DIExtractionResult:
        request.state.request_id = (
            body.request_id if _REQUEST_ID_RE.match(body.request_id) else request.state.request_id
        )
        if not any(img.side == "front" for img in body.images):
            raise InvalidImageError("a front image is required", code="front_image_required")
        result = await _run(
            request.state.request_id,
            body.document_type,
            body.country,
            len(body.images),
            schema_version=None,
            images=[
                ImageJob(i.side, i.content_base64, i.media_type, i.sha256) for i in body.images
            ],
        )
        return to_di_response(
            result,
            request_id=body.request_id,
            document_type=body.document_type,
            country=body.country,
            service_name=settings.service_name,
        )

    return app
