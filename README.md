# Codestra-OCR-Workers

Internal OCR worker runtime and engine adapter layer for Codestra document intelligence.

This service is **not** the public document API. It accepts bounded document images from
a trusted internal caller (Codestra-Document-Intelligence), runs a pluggable OCR engine,
and returns a **transient** structured extraction. It never persists images, never fetches
QR URLs, and owns no client or business records. Identity, review, hashing of document
numbers and persistence belong to the caller.

```
Document-Intelligence ──(internal network, bearer token)──▶ OCR worker ──▶ tesseract (subprocess, stdin/stdout)
                                                              │
                                                              └─ OpenCV: decode guards, preprocessing, QR
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Liveness |
| `GET` | `/readyz`, `/health/ready` | Readiness: default engine binary + language packs present (503 otherwise) |
| `GET` | `/metrics` | Prometheus metrics (no document values in labels) |
| `GET` | `/v1/capabilities` | Engines + versions, document types, schema versions, fields, contracts, limits |
| `POST` | `/internal/v1/ocr/extract` | Worker API; `result` conforms to Codestra-Document-Schemas `do-driver-licence` v1 |
| `POST` | `/v1/ocr/extract` | Document-Intelligence adapter: `codestra.ocr.extract-request/v1` → `codestra.ocr.extraction-result/v1` |

The canonical spec is [`openapi/openapi.json`](openapi/openapi.json); CI fails if it differs
from the app by a single byte (`codestra-ocr-export-openapi --check`). Interactive docs are
disabled.

Both extract routes require `Authorization: Bearer <token>` or `X-Internal-Token: <token>`
when `OCR_WORKER_INTERNAL_TOKEN` is set (always set it outside local development). Probes
and `/metrics` are unauthenticated for the orchestrator/Prometheus and must not be exposed
beyond the internal network.

### Worker API example

```json
POST /internal/v1/ocr/extract
{
  "scan_id": "scan-0001", "tenant_id": "tenant-a",
  "document_type": "driver_license", "country": "DO", "schema_version": "1.0.0",
  "images": [
    {"side": "front", "media_type": "image/jpeg", "sha256": "<hex>", "content_base64": "..."},
    {"side": "back",  "media_type": "image/jpeg", "content_base64": "..."}
  ],
  "options": {"engine": "tesseract", "decode_qr": true}
}
```

Response (abridged):

```json
{
  "request_id": "…", "status": "succeeded", "review_required": false, "retention": "transient",
  "result": {                                   // validates against do-driver-licence.schema.json
    "scan_id": "scan-0001", "tenant_id": "tenant-a", "schema_version": "1.0.0",
    "document_type": "driver_license", "country": "DO",
    "engine": {"name": "tesseract", "version": "5.3.0"},
    "fields": {"full_name": "…", "document_number": "…", "height": 1.68, "weight_lb": 140, "…": "…"},
    "field_evidence": {"document_number": [{"source": "ocr", "confidence": 0.91, "page": 1, "bbox": [0.1, 0.8, 0.5, 0.84]}]},
    "warnings": [],
    "qr_evidence": {"status": "decoded", "payload_digest": "sha256:…", "raw_payload": "https://licencias.intrantmoto.com/…"},
    "content_digests": [{"role": "front", "digest": "sha256:…"}, {"role": "back", "digest": "sha256:…"}]
  },
  "diagnostics": {"engine": {…, "languages": ["spa", "eng"]}, "fields": {…status/checks…}, "warnings": […], "qr": {…allowlist…}, "pages": […], "timings": {…}}
}
```

See [docs/CONTRACTS.md](docs/CONTRACTS.md) for field normalisation, warning mapping and the
Document-Intelligence adapter.

## Dominican driver licence extractor

Fields: full name, 11-digit document number (transient, `restricted`), address, height (m),
weight (lb), sex, blood type, birth/issue/expiry dates, category, restriction, first issue
date, card serial, and the QR code (payload digest; the payload itself is returned only when
it is an HTTPS URL on an allowlisted issuing-authority host).

The parsing approach is conceptually ported from FACE-ID's ID-scan parser
(label-anchored patterns, 7-digit reverse serial, reverse-side QR), with no runtime
dependency on FACE-ID. It adds OCR digit repair (`O→0`, `l→1`, …), Luhn validation of
the document number, ISO dates, unit normalisation, a layout fallback for the name, an
unlabelled-date fallback, side fallbacks, and per-field confidence plus evidence (page, bbox,
anchoring label, word confidence). Missing required fields produce `status: "partial"`;
low confidence, checksum failures, date-order problems and QR mismatches produce warnings
and `review_required: true`. OCR output is advisory and never proves authenticity.

## Engines

`OCREngine` ([engines/base.py](src/codestra_ocr_workers/engines/base.py)) is the only
contract between the runtime and a backend. Engines return an engine-neutral `OCRPage`
(lines → words with confidence and bbox), so extractors, guards, metrics and both API
surfaces are shared. Tesseract is built in; PaddleOCR/EasyOCR/cloud engines plug in through an
entry point without a separate worker. See [docs/ENGINES.md](docs/ENGINES.md).

The engine name and version are reported in every result, in `/v1/capabilities` and in
readiness details.

## Privacy and safety controls

* **No image persistence.** Images are decoded in memory; Tesseract receives PNG bytes on
  stdin and writes TSV to stdout, so no temp files are created (asserted in tests). The runtime
  image works with a read-only root filesystem and a small `/tmp` tmpfs.
* **Log redaction.** Logs are structured JSON with shapes only (counts, codes, durations).
  A mandatory filter additionally masks digit runs, URLs, e-mails, base64 blobs and
  uppercase name/address runs, drops string extras, and replaces exception messages with
  their type. Validation errors echo locations/types only, never input values.
* **QR.** Decoded with OpenCV only; URLs are never fetched. The payload's SHA-256 is always
  returned; the payload itself only for `https` + allowlisted host + no credentials + port 443.
* **Resource guards.** Request body cap (including chunked uploads), per-image byte cap,
  header-level dimension check before decode (decompression-bomb safe, plus OpenCV's own
  pixel cap), minimum resolution, image count, format sniffing (JPEG/PNG/WebP), declared
  media-type and SHA-256 verification, per-request deadline propagated to the engine
  subprocess timeout, and a concurrency semaphore with bounded queue wait (503 `worker_busy`
  + `Retry-After`).
* **Errors** carry a worker code plus the Codestra-Document-Schemas `error.schema.json`
  `contract_code`, `stage` and `retryable`; messages are static and value-free.
* **Container.** Non-root UID 10001, `tini` as PID 1, no shell login, `HEALTHCHECK`.

## Configuration

All settings are environment variables with the `OCR_WORKER_` prefix; see
[`.env.example`](.env.example) and [config.py](src/codestra_ocr_workers/config.py).

| Variable | Default | Notes |
| --- | --- | --- |
| `INTERNAL_TOKEN` | unset | Shared service token (mount from OpenBao) |
| `MAX_IMAGES` | 2 | Images per request |
| `MAX_IMAGE_BYTES` | 8 MiB | Per decoded image |
| `MAX_REQUEST_BYTES` | 24 MiB | Whole HTTP body |
| `MAX_IMAGE_PIXELS` / `MAX_IMAGE_SIDE` | 40 MP / 10000 | Checked from headers before decode |
| `MIN_IMAGE_WIDTH` / `MIN_IMAGE_HEIGHT` | 320 / 200 | |
| `REQUEST_TIMEOUT_SECONDS` | 30 | End-to-end deadline |
| `MAX_CONCURRENCY` / `QUEUE_TIMEOUT_SECONDS` | 2 / 2 | Size to CPU count; each Tesseract call is single-threaded |
| `ENGINES` / `DEFAULT_ENGINE` | `tesseract` | Comma-separated |
| `TESSERACT_LANGUAGES` | `spa,eng` | |
| `LOW_CONFIDENCE_THRESHOLD` | 0.6 | Field/page warning threshold |
| `QR_ALLOWED_HOSTS` | `licencias.intrantmoto.com` | Comma-separated |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.lock && .venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy
.venv/bin/codestra-ocr-export-openapi --check     # regenerate without --check after API changes
.venv/bin/pytest                                   # integration tests skip without tesseract
```

The full gate, including the real-Tesseract integration tests, runs in Docker:

```bash
docker build --target test .          # ruff, format, mypy --strict, OpenAPI exact match, pytest
docker build -t codestra-ocr-workers .
docker run --rm --read-only --tmpfs /tmp --cap-drop ALL -p 127.0.0.1:8080:8080 \
  -e OCR_WORKER_INTERNAL_TOKEN=dev-token codestra-ocr-workers
```

A GitHub Actions template running the same Docker gate plus a runtime smoke test lives in
[`ci/github-actions.yml`](ci/github-actions.yml). It is not active yet: pushing workflows needs a
token with `workflow` scope.

All test fixtures are synthetic: invented names, an unissued `000-` municipality prefix for
document numbers, and card images rendered at test time. No real identity documents or
customer data are used or committed (image files are gitignored).

## Status

Foundation branch `mission/document-intelligence-foundation-20260925`. Not deployed; exposure
is internal-only behind Document-Intelligence, with Middleware V3 as the cross-system
authority (see [docs/STANDALONE_ARCHITECTURE.md](docs/STANDALONE_ARCHITECTURE.md)).
