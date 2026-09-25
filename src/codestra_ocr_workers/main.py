"""Process entrypoint: ``codestra-ocr-worker``."""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .config import get_settings
from .logging_setup import configure_logging


def build() -> FastAPI:
    """App factory for ``uvicorn --factory codestra_ocr_workers.main:build``."""
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("OCR_WORKER_HOST", "0.0.0.0"),  # noqa: S104 - container bind
        port=int(os.environ.get("OCR_WORKER_PORT", "8080")),
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        # Bound buffered request bodies at the socket layer too.
        h11_max_incomplete_event_size=64 * 1024,
    )


if __name__ == "__main__":
    run()
