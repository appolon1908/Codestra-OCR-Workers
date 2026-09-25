"""QR decoding with OpenCV. Payloads are never fetched or logged."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import cv2

from .imaging import Image

MAX_QR_PAYLOAD_CHARS = 2048


@dataclass(frozen=True)
class QRResult:
    detected: bool
    decoded: bool
    digest_sha256: str | None = None
    payload_kind: str | None = None  # url | text
    authority_host: str | None = None
    allowlisted: bool = False
    authority_url: str | None = None  # only populated for allowlisted HTTPS URLs


def _candidates(img: Image) -> Iterator[Image]:
    yield img
    h, w = img.shape[:2]
    # Dominican licence QR sits in the upper-left of the reverse; try that crop first.
    yield img[: max(1, int(h * 0.60)), : max(1, int(w * 0.56))]
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield binary
    if max(h, w) < 2000:
        for scale in (1.5, 2.0):
            yield cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def decode_payload(
    img: Image, keep_going: Callable[[], bool] | None = None
) -> tuple[bool, str | None]:
    """Return ``(detected, payload)``."""
    detector = cv2.QRCodeDetector()
    detected = False
    for candidate in _candidates(img):
        if keep_going is not None and not keep_going():
            break
        try:
            data, points, _ = detector.detectAndDecode(candidate)
        except cv2.error:
            continue
        if points is not None:
            detected = True
        if data:
            return True, data.strip()[:MAX_QR_PAYLOAD_CHARS]
    return detected, None


def classify(payload: str | None, *, detected: bool, allowed_hosts: list[str]) -> QRResult:
    if payload is None:
        return QRResult(detected=detected, decoded=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    try:
        parts = urlsplit(payload)
    except ValueError:
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return QRResult(detected=True, decoded=True, digest_sha256=digest, payload_kind="text")
    host = parts.hostname.lower().rstrip(".")
    allowlisted = (
        parts.scheme.lower() == "https"
        and host in allowed_hosts
        and parts.username is None
        and parts.password is None
        and parts.port in (None, 443)
    )
    return QRResult(
        detected=True,
        decoded=True,
        digest_sha256=digest,
        payload_kind="url",
        authority_host=host,
        allowlisted=allowlisted,
        authority_url=payload if allowlisted else None,
    )
