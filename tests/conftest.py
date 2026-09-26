from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from codestra_ocr_workers.app import create_app
from codestra_ocr_workers.config import Settings
from codestra_ocr_workers.engines.base import OCREngine
from codestra_ocr_workers.engines.registry import EngineRegistry

from .fixtures import FakeEngine

ClientFactory = Callable[..., TestClient]


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"engines": ["fake"], "default_engine": "fake"}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture
def client_factory() -> ClientFactory:
    def factory(
        engine: OCREngine | None = None, *, raise_server_exceptions: bool = True, **settings: object
    ) -> TestClient:
        cfg = make_settings(**settings)
        engine = engine or FakeEngine()
        app = create_app(cfg, EngineRegistry({"fake": engine}, "fake"))
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    return factory


@pytest.fixture
def client(client_factory: ClientFactory) -> TestClient:
    return client_factory()


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def contract_validator(relative: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def assert_valid(relative: str, instance: object) -> None:
    errors = sorted(contract_validator(relative).iter_errors(instance), key=lambda e: e.path)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]
