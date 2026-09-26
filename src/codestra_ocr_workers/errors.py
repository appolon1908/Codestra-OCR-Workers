"""Typed worker errors mapped to stable API error codes."""

from __future__ import annotations


class WorkerError(Exception):
    status_code = 500
    code = "internal_error"
    # Codestra-Document-Schemas error.schema.json mapping.
    contract_code = "INTERNAL_ERROR"
    stage = "extract"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class InvalidImageError(WorkerError):
    status_code = 422
    code = "invalid_image"
    contract_code = "INVALID_INPUT"
    stage = "ingest"
    retryable = False


class ImageTooLargeError(WorkerError):
    status_code = 413
    code = "image_too_large"
    contract_code = "INVALID_INPUT"
    stage = "ingest"
    retryable = False


class UnsupportedDocumentError(WorkerError):
    status_code = 422
    code = "unsupported_document"
    contract_code = "UNSUPPORTED_DOCUMENT"
    stage = "ingest"
    retryable = False


class EngineUnavailableError(WorkerError):
    status_code = 503
    code = "engine_unavailable"
    contract_code = "EXTRACTION_FAILED"
    stage = "extract"
    retryable = True


class EngineFailedError(WorkerError):
    status_code = 502
    code = "engine_failed"
    contract_code = "EXTRACTION_FAILED"
    stage = "extract"
    retryable = True


class WorkerBusyError(WorkerError):
    status_code = 503
    code = "worker_busy"
    contract_code = "RATE_LIMITED"
    stage = "ingest"
    retryable = True


class DeadlineExceededError(WorkerError):
    status_code = 504
    code = "deadline_exceeded"
    contract_code = "TIMEOUT"
    stage = "extract"
    retryable = True


class UnauthorizedError(WorkerError):
    status_code = 401
    code = "unauthorized"
    contract_code = "INVALID_INPUT"
    stage = "ingest"
