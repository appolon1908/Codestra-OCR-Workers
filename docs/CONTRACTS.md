# Contract mapping

## 1. Worker API → Codestra-Document-Schemas `do-driver-licence` v1

`POST /internal/v1/ocr/extract` returns `{request_id, status, review_required, retention,
result, diagnostics}`. **`result`** validates against the vendored
`contracts/codestra-document-schemas/v1/do-driver-licence.schema.json` (tests enforce this on
real responses, including with the real Tesseract engine).

| Contract key | Source |
| --- | --- |
| `scan_id`, `tenant_id` | Echoed from the request (the worker stores nothing) |
| `schema_version` | `1.0.0` (request must ask for a supported version) |
| `document_type` | `driver_license` (`driver_licence` accepted as an input alias) |
| `engine` | `{name, version}` of the engine that ran |
| `fields` | All 14 keys, `null` when not found. Values rejected by the contract are nulled with `MISSING_FIELD`, never coerced |
| `field_evidence` | One `{source: "ocr", confidence, page (1-based), bbox}` per extracted field; bbox normalised `[x_min, y_min, x_max, y_max]`. `raw_text` is intentionally omitted |
| `warnings` | Mapped codes (below), deduplicated per `(code, field)`, with `REVIEW_REQUIRED` appended when review is needed |
| `qr_evidence` | `not_detected`, `unreadable` or `decoded` + `payload_digest` (`sha256:` of the raw UTF-8 payload); `raw_payload` only for allowlisted HTTPS authority URLs |
| `content_digests` | `sha256:` of the exact decoded input bytes per image; role `front`/`back`/`document` |

Normalisation: dates → `YYYY-MM-DD` (calendar-validated, printed `DD/MM/YYYY`); height ft'in or
metres → metres (2 dp); weight kg → lb; blood type → `A+`…`O-`; category → ASCII upper,
contract charset, ≤ 20 chars; document number → 11 digits (Luhn check recorded in
diagnostics; failure lowers confidence and raises `LOW_CONFIDENCE`).

| Worker warning (diagnostics) | Contract warning |
| --- | --- |
| `required_field_missing` (error), `field_missing` (info) | `MISSING_FIELD` |
| `low_confidence`, `checksum_failed`, `low_page_confidence` | `LOW_CONFIDENCE` |
| `date_inconsistent` | `DATE_ORDER` |
| `qr_not_decoded` | `QR_UNREADABLE` |
| `qr_host_not_allowlisted`, `qr_not_url` | `QR_MISMATCH` |
| `document_expired`, `side_missing`, `qr_not_found` | diagnostics only |

Errors: every error body has `code` (worker), `contract_code`, `stage` and `retryable`
matching `error.schema.json`, so a caller can emit an `extract.failed` message directly.

| HTTP | `code` | `contract_code` | retryable |
| --- | --- | --- | --- |
| 401 | `unauthorized` | `INVALID_INPUT` | no |
| 413 | `payload_too_large`, `image_too_large` | `INVALID_INPUT` | no |
| 422 | `invalid_request`, `invalid_image`, `digest_mismatch`, `too_many_images`, `front_image_required` | `INVALID_INPUT` | no |
| 422 | `unsupported_document`, `unsupported_schema_version` | `UNSUPPORTED_DOCUMENT` | no |
| 502/503 | `engine_failed`, `engine_unavailable` | `EXTRACTION_FAILED` | yes |
| 503 | `worker_busy` | `RATE_LIMITED` | yes |
| 504 | `deadline_exceeded` | `TIMEOUT` | yes |
| 500 | `internal_error` | `INTERNAL_ERROR` | no |

The asynchronous `worker-contract.schema.json` (asset IDs over a queue) is out of scope for
this synchronous HTTP worker. A queue consumer can wrap the same pipeline later and reuse
`renderers.to_contract` unchanged.

## 2. Document-Intelligence adapter

`POST /v1/ocr/extract` implements Codestra-Document-Intelligence's private worker contract
(`app/contracts/*.v1.schema.json`, vendored under `contracts/document-intelligence/v1/`), so
its existing `HttpOcrWorker` client works unchanged:

* Request `codestra.ocr.extract-request/v1`; each image's `sha256` is verified against the
  bytes (`digest_mismatch` otherwise) and echoed in `image_digests`.
* Response `codestra.ocr.extraction-result/v1`: `fields` use the same names as above, with
  string values (`height` `"1.68"`, `weight_lb` `"140"`), `confidence` (0 when missing),
  `source: "visual"` and `evidence {side, bbox}`. `quality.overall_confidence` is the mean
  confidence of present fields × coverage of required fields. `quality.warnings` are the contract
  warning codes (`CODE` or `CODE:field`). `qr` is `{present, source_lookup_url}`; the
  URL is only set for allowlisted hosts.
* Auth: `Authorization: Bearer <OCR_WORKER_INTERNAL_TOKEN>`.

## Keeping in sync

Vendored copies and their source commits are listed in `contracts/README.md`. Run
`scripts/sync_contracts.sh`, then the tests. A schema change upstream that breaks the worker
fails `tests/unit/test_api.py`, `tests/unit/test_di_adapter.py` and the integration tests.
