"""Engine registry.

Built-in engines are registered here; additional engines (PaddleOCR, EasyOCR, ...) can be
shipped as separate packages exposing an ``codestra_ocr_workers.engines`` entry point that
resolves to a factory ``(Settings) -> OCREngine``. The worker runtime, guards, metrics and
extractors are shared — no per-engine worker is needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

from ..config import Settings
from ..errors import EngineUnavailableError
from .base import OCREngine
from .tesseract import TesseractEngine

log = logging.getLogger(__name__)

EngineFactory = Callable[[Settings], OCREngine]
ENTRY_POINT_GROUP = "codestra_ocr_workers.engines"


def _tesseract(settings: Settings) -> OCREngine:
    return TesseractEngine(
        cmd=settings.tesseract_cmd,
        languages=tuple(settings.tesseract_languages),
        default_psm=settings.tesseract_psm,
        threads=settings.engine_threads,
    )


BUILTIN_FACTORIES: dict[str, EngineFactory] = {"tesseract": _tesseract}


def _plugin_factory(name: str) -> EngineFactory | None:
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name != name:
            continue
        factory: EngineFactory = ep.load()
        return factory
    return None


class EngineRegistry:
    def __init__(self, engines: dict[str, OCREngine], default: str) -> None:
        if default not in engines:
            raise EngineUnavailableError(f"default engine {default!r} is not enabled")
        self._engines = engines
        self.default = default

    @classmethod
    def from_settings(cls, settings: Settings) -> EngineRegistry:
        engines: dict[str, OCREngine] = {}
        for name in settings.engines:
            factory = BUILTIN_FACTORIES.get(name) or _plugin_factory(name)
            if factory is None:
                raise EngineUnavailableError(f"unknown OCR engine {name!r}")
            engines[name] = factory(settings)
        return cls(engines, settings.default_engine)

    def names(self) -> list[str]:
        return list(self._engines)

    def get(self, name: str | None) -> OCREngine:
        key = name or self.default
        engine = self._engines.get(key)
        if engine is None:
            raise EngineUnavailableError(f"OCR engine {key!r} is not enabled on this worker")
        return engine

    def all(self) -> list[OCREngine]:
        return list(self._engines.values())
