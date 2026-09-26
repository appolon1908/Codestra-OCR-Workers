"""Image intake guards and OpenCV preprocessing.

All image data stays in memory. Dimensions are read from the container header *before*
decoding so that decompression bombs are rejected without allocating pixel buffers.
"""

from __future__ import annotations

import base64
import binascii
import os
import struct
from dataclasses import dataclass

# Hard ceiling enforced by OpenCV's own decoder in addition to our header check.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(64_000_000))

import cv2
import numpy as np
from cv2.typing import MatLike

from .errors import ImageTooLargeError, InvalidImageError

Image = MatLike

SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")


@dataclass(frozen=True)
class ImageLimits:
    max_bytes: int
    max_pixels: int
    max_side: int
    min_width: int
    min_height: int


@dataclass(frozen=True)
class DecodedImage:
    pixels: Image
    media_type: str
    width: int
    height: int
    byte_size: int


def decode_base64(value: str, *, max_bytes: int) -> bytes:
    if value.lstrip().startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    value = "".join(value.split())
    # Reject before decoding: 4 base64 chars -> 3 bytes.
    if len(value) * 3 // 4 > max_bytes + 3:
        raise ImageTooLargeError("image exceeds maximum size")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidImageError("image is not valid base64") from exc
    if not raw:
        raise InvalidImageError("image is empty")
    if len(raw) > max_bytes:
        raise ImageTooLargeError("image exceeds maximum size")
    return raw


def sniff_media_type(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    raise InvalidImageError("unsupported image format; expected JPEG, PNG or WebP")


def _png_size(raw: bytes) -> tuple[int, int]:
    if len(raw) < 24 or raw[12:16] != b"IHDR":
        raise InvalidImageError("malformed PNG header")
    width, height = struct.unpack(">II", raw[16:24])
    return int(width), int(height)


_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(raw: bytes) -> tuple[int, int]:
    i = 2
    n = len(raw)
    while i + 4 <= n:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
        if marker in _JPEG_SOF:
            if i + 9 > n:
                break
            height, width = struct.unpack(">HH", raw[i + 5 : i + 9])
            return int(width), int(height)
        i += 2 + seg_len
    raise InvalidImageError("malformed JPEG header")


def _webp_size(raw: bytes) -> tuple[int, int]:
    chunk = raw[12:16]
    if chunk == b"VP8 " and len(raw) >= 30:
        w, h = struct.unpack("<HH", raw[26:30])
        return int(w & 0x3FFF), int(h & 0x3FFF)
    if chunk == b"VP8L" and len(raw) >= 25:
        b = raw[21:25]
        w = 1 + (((b[1] & 0x3F) << 8) | b[0])
        h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return w, h
    if chunk == b"VP8X" and len(raw) >= 30:
        w = 1 + int.from_bytes(raw[24:27], "little")
        h = 1 + int.from_bytes(raw[27:30], "little")
        return w, h
    raise InvalidImageError("malformed WebP header")


def header_dimensions(raw: bytes, media_type: str) -> tuple[int, int]:
    if media_type == "image/png":
        return _png_size(raw)
    if media_type == "image/jpeg":
        return _jpeg_size(raw)
    return _webp_size(raw)


def load_image(
    raw: bytes, limits: ImageLimits, *, declared_media_type: str | None = None
) -> DecodedImage:
    if len(raw) > limits.max_bytes:
        raise ImageTooLargeError("image exceeds maximum size")
    media_type = sniff_media_type(raw)
    if declared_media_type and declared_media_type != media_type:
        raise InvalidImageError("declared media_type does not match image content")
    width, height = header_dimensions(raw, media_type)
    _check_dimensions(width, height, limits)
    pixels = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        raise InvalidImageError("image could not be decoded")
    h, w = pixels.shape[:2]
    _check_dimensions(w, h, limits)
    return DecodedImage(pixels=pixels, media_type=media_type, width=w, height=h, byte_size=len(raw))


def _check_dimensions(width: int, height: int, limits: ImageLimits) -> None:
    if width <= 0 or height <= 0:
        raise InvalidImageError("image has invalid dimensions")
    if width > limits.max_side or height > limits.max_side or width * height > limits.max_pixels:
        raise ImageTooLargeError("image dimensions exceed limits")
    if width < limits.min_width or height < limits.min_height:
        raise InvalidImageError("image resolution is too small for document OCR")


# --- preprocessing ---------------------------------------------------------------------

TARGET_OCR_WIDTH = 1800
MAX_OCR_WIDTH = 2600


@dataclass(frozen=True)
class Preprocessed:
    variant: str
    image: Image
    steps: tuple[str, ...]


def _to_gray(img: Image) -> Image:
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _normalise_scale(gray: Image) -> tuple[Image, str]:
    w = gray.shape[1]
    if w < TARGET_OCR_WIDTH * 0.75:
        scale = TARGET_OCR_WIDTH / w
        return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC), (
            f"upscale:{scale:.2f}"
        )
    if w > MAX_OCR_WIDTH:
        scale = MAX_OCR_WIDTH / w
        return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA), (
            f"downscale:{scale:.2f}"
        )
    return gray, "scale:1.00"


def preprocess(img: Image, variant: str = "clahe") -> Preprocessed:
    """Return an OCR-ready grayscale image.

    ``clahe``: contrast-limited histogram equalisation + edge-preserving denoise; works
    well on laminated cards with glare. ``otsu``: global binarisation, used as a retry
    when the first pass yields low confidence (e.g. strong background guilloche).
    """
    gray = _to_gray(img)
    gray, scale_step = _normalise_scale(gray)
    steps = ["grayscale", scale_step]
    if variant == "clahe":
        gray = cv2.bilateralFilter(gray, 7, 35, 35)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        steps += ["bilateral:7", "clahe:2.0"]
    elif variant == "otsu":
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        steps += ["gaussian:3", "otsu"]
    elif variant != "raw":
        raise ValueError(f"unknown preprocessing variant {variant!r}")
    return Preprocessed(variant=variant, image=gray, steps=tuple(steps))


PREPROCESS_VARIANTS = ("clahe", "otsu", "raw")


def encode_png(img: Image) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise InvalidImageError("could not prepare image for OCR")
    return bytes(buf.tobytes())
