"""Write the canonical OpenAPI document (``openapi/openapi.json``)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .app import create_app
from .config import Settings
from .engines.registry import EngineRegistry


def render() -> str:
    # Deterministic settings so the committed spec does not depend on the host env.
    settings = Settings(_env_file=None)
    app = create_app(settings, EngineRegistry.from_settings(settings))
    spec: dict[str, Any] = app.openapi()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="openapi/openapi.json")
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()
    path = Path(args.output)
    rendered = render()
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            raise SystemExit(f"{path} is out of date; run codestra-ocr-export-openapi")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
