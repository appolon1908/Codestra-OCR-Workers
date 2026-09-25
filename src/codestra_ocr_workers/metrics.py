"""Prometheus metrics. Labels are low-cardinality and never carry document content."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info

from . import __version__

_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        r = self.registry
        self.info = Info("ocr_worker", "OCR worker build information", registry=r)
        self.info.info({"version": __version__})
        self.requests = Counter(
            "ocr_worker_extract_requests_total",
            "Extraction requests by outcome",
            ["outcome", "document_type", "country"],
            registry=r,
        )
        self.duration = Histogram(
            "ocr_worker_extract_duration_seconds",
            "End-to-end extraction latency",
            ["document_type"],
            buckets=_DURATION_BUCKETS,
            registry=r,
        )
        self.engine_duration = Histogram(
            "ocr_worker_engine_duration_seconds",
            "Per-image OCR engine latency",
            ["engine"],
            buckets=_DURATION_BUCKETS,
            registry=r,
        )
        self.inflight = Gauge("ocr_worker_inflight_requests", "Extractions in progress", registry=r)
        self.rejections = Counter(
            "ocr_worker_rejections_total",
            "Requests rejected by resource guards",
            ["reason"],
            registry=r,
        )
        self.fields = Counter(
            "ocr_worker_field_status_total",
            "Field extraction outcomes",
            ["document_type", "field", "status"],
            registry=r,
        )
        self.qr = Counter("ocr_worker_qr_total", "QR decode outcomes", ["result"], registry=r)
        self.ready = Gauge(
            "ocr_worker_engine_ready", "Engine readiness (1=ready)", ["engine"], registry=r
        )
