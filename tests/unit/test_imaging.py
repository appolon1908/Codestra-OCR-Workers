from __future__ import annotations

import base64
import struct
import zlib

import cv2
import numpy as np
import pytest

from codestra_ocr_workers.errors import ImageTooLargeError, InvalidImageError
from codestra_ocr_workers.imaging import (
    ImageLimits,
    decode_base64,
    header_dimensions,
    load_image,
    preprocess,
    sniff_media_type,
)

from ..fixtures import blank_card, encode

LIMITS = ImageLimits(
    max_bytes=2_000_000, max_pixels=4_000_000, max_side=4000, min_width=320, min_height=200
)


def _png_header_only(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


@pytest.mark.parametrize("ext", [".png", ".jpg", ".webp"])
def test_header_dimensions_match_decoder(ext: str) -> None:
    raw = encode(blank_card(812, 513), ext)
    media = sniff_media_type(raw)
    assert header_dimensions(raw, media) == (812, 513)
    decoded = load_image(raw, LIMITS)
    assert (decoded.width, decoded.height) == (812, 513)
    assert decoded.media_type == media


def test_webp_lossless_header() -> None:
    ok, buf = cv2.imencode(".webp", blank_card(640, 400), [cv2.IMWRITE_WEBP_QUALITY, 101])
    assert ok
    raw = bytes(buf.tobytes())
    assert header_dimensions(raw, "image/webp") == (640, 400)


def test_decompression_bomb_rejected_from_header() -> None:
    with pytest.raises(ImageTooLargeError):
        load_image(_png_header_only(60_000, 60_000), LIMITS)


def test_too_small_rejected() -> None:
    with pytest.raises(InvalidImageError, match="too small"):
        load_image(encode(blank_card(100, 80)), LIMITS)


def test_byte_limit() -> None:
    raw = encode(np.random.default_rng(0).integers(0, 255, (1500, 1500, 3), dtype=np.uint8))
    with pytest.raises(ImageTooLargeError):
        load_image(raw, LIMITS)
    with pytest.raises(ImageTooLargeError):
        decode_base64(base64.b64encode(raw).decode(), max_bytes=LIMITS.max_bytes)


def test_unsupported_format_and_garbage() -> None:
    with pytest.raises(InvalidImageError, match="unsupported"):
        sniff_media_type(b"GIF89a....")
    with pytest.raises(InvalidImageError):
        load_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, LIMITS)


def test_declared_media_type_mismatch() -> None:
    with pytest.raises(InvalidImageError, match="media_type"):
        load_image(encode(blank_card()), LIMITS, declared_media_type="image/jpeg")


def test_decode_base64_variants() -> None:
    raw = encode(blank_card())
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    assert decode_base64(data_url, max_bytes=10_000_000) == raw
    wrapped = "\n".join(base64.b64encode(raw).decode()[i : i + 76] for i in range(0, 200, 76))
    assert decode_base64(wrapped, max_bytes=10_000_000)
    with pytest.raises(InvalidImageError):
        decode_base64("not base64!!", max_bytes=100)
    with pytest.raises(InvalidImageError):
        decode_base64("", max_bytes=100)


@pytest.mark.parametrize("variant", ["clahe", "otsu", "raw"])
def test_preprocess_variants(variant: str) -> None:
    out = preprocess(blank_card(700, 440), variant)
    assert out.image.ndim == 2
    assert out.image.shape[1] == 1800  # upscaled for OCR
    assert out.steps[0] == "grayscale"
    assert out.steps[1].startswith("upscale")


def test_preprocess_downscales_large_and_rejects_unknown() -> None:
    assert preprocess(blank_card(4000, 2500), "raw").image.shape[1] == 2600
    with pytest.raises(ValueError, match="unknown"):
        preprocess(blank_card(), "nope")
