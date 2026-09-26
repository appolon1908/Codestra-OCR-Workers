"""Local Tesseract adapter.

Image bytes are piped through stdin, and TSV output is read from stdout, so no image or
text ever touches the filesystem. ``OMP_THREAD_LIMIT`` keeps each call single-threaded so
the worker's concurrency limit maps directly onto CPU usage.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import threading
from collections import defaultdict

from ..errors import DeadlineExceededError, EngineFailedError, EngineUnavailableError
from ..imaging import Image, encode_png
from .base import EngineInfo, OCREngine, OCRLine, OCRPage, OCRWord, RecognizeOptions

_LAYOUT_TO_PSM = {"block": 6, "sparse": 11, "single_line": 7}
_VERSION_RE = re.compile(r"tesseract\s+v?(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)


class TesseractEngine(OCREngine):
    name = "tesseract"

    def __init__(
        self,
        cmd: str = "tesseract",
        languages: tuple[str, ...] = ("spa", "eng"),
        default_psm: int = 6,
        threads: int = 1,
    ) -> None:
        self._cmd = cmd
        self._languages = languages
        self._default_psm = default_psm
        self._threads = threads
        self._lock = threading.Lock()
        self._probe: tuple[str, tuple[str, ...]] | None = None

    # -- metadata --------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "TESSDATA_PREFIX", "LANG"}}
        env["OMP_THREAD_LIMIT"] = str(self._threads)
        return env

    def _run_meta(self, *args: str) -> str:
        binary = shutil.which(self._cmd)
        if binary is None:
            raise EngineUnavailableError("tesseract binary not found")
        try:
            proc = subprocess.run(
                [binary, *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=self._env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EngineUnavailableError("tesseract binary not executable") from exc
        return proc.stdout + "\n" + proc.stderr

    def probe(self) -> tuple[str, tuple[str, ...]]:
        with self._lock:
            if self._probe is None:
                version_out = self._run_meta("--version")
                match = _VERSION_RE.search(version_out)
                version = match.group(1) if match else "unknown"
                langs_out = self._run_meta("--list-langs")
                installed = tuple(
                    sorted(
                        line.strip()
                        for line in langs_out.splitlines()
                        if line.strip() and " " not in line.strip() and ":" not in line
                    )
                )
                self._probe = (version, installed)
            return self._probe

    def info(self) -> EngineInfo:
        try:
            version, installed = self.probe()
        except EngineUnavailableError:
            version, installed = "unavailable", ()
        langs = tuple(lang for lang in self._languages if lang in installed)
        return EngineInfo(
            name=self.name,
            version=version,
            languages=langs,
            capabilities=("word_confidence", "word_bbox", "layout_hint"),
        )

    def readiness(self) -> tuple[bool, str]:
        try:
            version, installed = self.probe()
        except EngineUnavailableError as exc:
            return False, exc.message
        missing = [lang for lang in self._languages if lang not in installed]
        if missing:
            return False, f"missing tesseract language packs: {','.join(missing)}"
        return True, f"tesseract {version}"

    # -- recognition -----------------------------------------------------------------

    def recognize(self, image: Image, options: RecognizeOptions) -> OCRPage:
        binary = shutil.which(self._cmd)
        if binary is None:
            raise EngineUnavailableError("tesseract binary not found")
        _, installed = self.probe()
        langs = [lang for lang in options.languages if lang in installed] or ["eng"]
        psm = _LAYOUT_TO_PSM.get(options.layout, self._default_psm)
        argv = [
            binary,
            "stdin",
            "stdout",
            "-l",
            "+".join(langs),
            "--psm",
            str(psm),
            "--oem",
            "1",
            "-c",
            "preserve_interword_spaces=1",
            "tsv",
        ]
        if options.timeout_seconds <= 0:
            raise DeadlineExceededError("deadline exceeded before OCR")
        try:
            proc = subprocess.run(
                argv,
                input=encode_png(image),
                capture_output=True,
                timeout=options.timeout_seconds,
                check=False,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise DeadlineExceededError("tesseract timed out") from exc
        except OSError as exc:
            raise EngineUnavailableError("tesseract could not be started") from exc
        if proc.returncode != 0:
            # stderr may echo recognised text; never propagate it.
            raise EngineFailedError(f"tesseract exited with status {proc.returncode}")
        h, w = image.shape[:2]
        return parse_tsv(proc.stdout.decode("utf-8", errors="replace"), width=w, height=h)


def parse_tsv(tsv: str, *, width: int, height: int) -> OCRPage:
    """Convert Tesseract TSV (level 5 = word rows) into an :class:`OCRPage`."""
    grouped: dict[tuple[int, int, int, int], list[tuple[int, OCRWord]]] = defaultdict(list)
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf") or -1)
            key = (
                int(row["page_num"]),
                int(row["block_num"]),
                int(row["par_num"]),
                int(row["line_num"]),
            )
            bbox = (int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]))
            order = int(row["word_num"])
        except (KeyError, ValueError):
            continue
        if conf < 0:
            continue
        grouped[key].append(
            (order, OCRWord(text=text, confidence=min(conf, 100.0) / 100.0, bbox=bbox))
        )
    lines = []
    for entries in grouped.values():  # insertion order == Tesseract reading order
        words = tuple(word for _, word in sorted(entries, key=lambda item: item[0]))
        lines.append(OCRLine(words=words))
    return OCRPage(lines=tuple(lines), width=width, height=height)
