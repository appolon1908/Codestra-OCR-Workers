# Missions 06-10 — Worker Department
06 Engine protocol: common request/result/error interface independent of PaddleOCR/fallback implementation.
07 Dispatch logic: primary engine first; fallback only by policy/quality trigger, not arbitrary client selection.
08 Execution state: bounded timeout, retry classification, poison/dead-letter outcome and correlation propagation.
09 Resource logic: CPU preprocessing separated from GPU-capable OCR execution; concurrency is configuration-controlled.
10 Result provenance: engine/model/version, page coordinates, timings, confidence and normalized error codes returned.

Implementation branch target after architecture baseline is committed: feature/worker-foundation-v1.
