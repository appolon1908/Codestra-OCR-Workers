# OCR engine abstraction

The runtime (HTTP API, guards, deadline, concurrency, metrics, QR, extractors, contract
renderers) is engine-agnostic. Adding an engine means adding **one adapter class**, not a
new worker.

## Contract

```python
class OCREngine(abc.ABC):
    name: str
    def info(self) -> EngineInfo: ...                 # name, version, languages, capabilities
    def readiness(self) -> tuple[bool, str]: ...       # cheap, side-effect free
    def recognize(self, image, options: RecognizeOptions) -> OCRPage: ...
```

* `image` is the OpenCV-preprocessed grayscale array (`uint8`). Engines needing colour or
  their own preprocessing may declare that in `capabilities`; the pipeline still passes the
  preprocessed image today.
* `RecognizeOptions` carries `languages` (ISO 639-2, e.g. `spa`), `timeout_seconds` (the
  remaining request deadline, which the engine **must** honour) and an engine-neutral
  `layout` hint (`block` | `sparse` | `single_line`).
* `OCRPage` → `OCRLine` → `OCRWord(text, confidence 0..1, bbox x,y,w,h)` in
  preprocessed-image pixels, in reading order. Extractors build evidence from these, so
  engines that return polygons should convert them to axis-aligned boxes.
* Implementations must be thread-safe (calls run in a bounded thread pool), must not write
  image or text data to persistent storage, and must not log recognised text.
* Raise `EngineUnavailableError` (not installed or not ready), `EngineFailedError` (backend
  error) or `DeadlineExceededError` (timeout). Never put recognised text in exception messages.

## Registering an engine

Built-ins live in `engines/registry.py::BUILTIN_FACTORIES`. Out-of-tree engines ship as a
separate package exposing a factory `(Settings) -> OCREngine`:

```toml
# pyproject.toml of codestra-ocr-paddle
[project.entry-points."codestra_ocr_workers.engines"]
paddleocr = "codestra_ocr_paddle:build_engine"
```

Enable it with `OCR_WORKER_ENGINES=tesseract,paddleocr` (and optionally
`OCR_WORKER_DEFAULT_ENGINE=paddleocr`). Callers can select it per request with
`options.engine`. Readiness, capabilities, metrics (`engine` label) and the reported
`engine.name`/`engine.version` pick it up automatically.

### PaddleOCR sketch

```python
class PaddleOCREngine(OCREngine):
    name = "paddleocr"
    def __init__(self, lang: str = "es"): self._ocr = PaddleOCR(lang=lang, use_angle_cls=True)
    def info(self): return EngineInfo("paddleocr", paddleocr.__version__, ("spa",), ("word_confidence", "word_bbox"))
    def readiness(self): return True, "paddleocr loaded"
    def recognize(self, image, options):
        # PaddleOCR returns [(polygon, (text, score)), ...] per text line; split lines into
        # words (or treat each line as one word) and convert polygons to x,y,w,h.
        ...
```

EasyOCR follows the same pattern (`reader.readtext(image)` → `(polygon, text, score)`).
Heavy ML engines should be enabled only in images that ship their models; keep
`MAX_CONCURRENCY` aligned with available CPU/GPU memory. Timeouts for in-process engines
need a process pool or a sidecar, since Python threads cannot be killed. Tesseract avoids
this by running as a subprocess with a hard timeout.

## Adding a document type

Implement `extractors.base.Extractor` (fields, schema id/versions, `extract(pages, qr,
threshold)`) and register it in `extractors/registry.py`. Extractors see only `OCRPage`, so
they work with every engine.
