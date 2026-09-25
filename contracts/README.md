# Vendored contracts

Read-only copies of upstream JSON Schemas. The worker does not load them at runtime;
tests validate real worker responses against them so drift fails CI.

| File | Upstream | Commit |
| --- | --- | --- |
| `codestra-document-schemas/v1/do-driver-licence.schema.json` | `Codestra-Document-Schemas` `codestra_document_schemas/schemas/v1/` | `093f9bc` |
| `codestra-document-schemas/v1/error.schema.json` | same | `093f9bc` |
| `document-intelligence/v1/ocr-extract-request.v1.schema.json` | `Codestra-Document-Intelligence` `app/contracts/` | `cd0f6b4` |
| `document-intelligence/v1/ocr-extraction-result.v1.schema.json` | same | `cd0f6b4` |

Refresh with `scripts/sync_contracts.sh` (it copies committed `HEAD` content only), update the
commit column, then run `pytest tests/unit`. Never edit these files by hand.
