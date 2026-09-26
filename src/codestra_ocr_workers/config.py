"""Runtime configuration, sourced from ``OCR_WORKER_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Comma-separated env values ("spa,eng") instead of JSON arrays.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OCR_WORKER_", extra="ignore")

    service_name: str = "codestra-ocr-worker"
    environment: str = "development"
    log_level: str = "INFO"

    # Shared secret for service-to-service calls. When set, every /internal/* and
    # /v1/* request must carry it in the X-Internal-Token header.
    internal_token: SecretStr | None = None

    # Resource guards.
    max_images: int = Field(default=2, ge=1, le=8)
    max_image_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_request_bytes: int = Field(default=24 * 1024 * 1024, ge=1024)
    max_image_pixels: int = Field(default=40_000_000, ge=10_000)
    max_image_side: int = Field(default=10_000, ge=100)
    min_image_width: int = Field(default=320, ge=16)
    min_image_height: int = Field(default=200, ge=16)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_concurrency: int = Field(default=2, ge=1)
    queue_timeout_seconds: float = Field(default=2.0, ge=0)

    # Engines.
    engines: CsvList = Field(default_factory=lambda: ["tesseract"])
    default_engine: str = "tesseract"
    tesseract_cmd: str = "tesseract"
    tesseract_languages: CsvList = Field(default_factory=lambda: ["spa", "eng"])
    tesseract_psm: int = Field(default=6, ge=0, le=13)
    engine_threads: int = Field(default=1, ge=1)

    # Extraction.
    low_confidence_threshold: float = Field(default=0.60, ge=0, le=1)
    retry_preprocessing_below: float = Field(default=0.55, ge=0, le=1)
    qr_allowed_hosts: CsvList = Field(default_factory=lambda: ["licencias.intrantmoto.com"])

    @field_validator("engines", "tesseract_languages", "qr_allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("qr_allowed_hosts")
    @classmethod
    def _lower_hosts(cls, value: list[str]) -> list[str]:
        return [host.lower().rstrip(".") for host in value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
