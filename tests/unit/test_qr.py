from __future__ import annotations

import hashlib

import pytest

from codestra_ocr_workers.qr import classify, decode_payload

from ..fixtures import AUTHORITY_URL, blank_card, card_with_qr

HOSTS = ["licencias.intrantmoto.com"]


def test_decodes_synthetic_qr_on_card() -> None:
    detected, payload = decode_payload(card_with_qr(AUTHORITY_URL))
    assert detected
    assert payload == AUTHORITY_URL


def test_no_qr() -> None:
    assert decode_payload(blank_card()) == (False, None)


def test_keep_going_short_circuits() -> None:
    assert decode_payload(card_with_qr(AUTHORITY_URL), lambda: False) == (False, None)


def test_allowlisted_url() -> None:
    res = classify(AUTHORITY_URL, detected=True, allowed_hosts=HOSTS)
    assert res.allowlisted
    assert res.authority_url == AUTHORITY_URL
    assert res.authority_host == "licencias.intrantmoto.com"
    assert res.digest_sha256 == hashlib.sha256(AUTHORITY_URL.encode()).hexdigest()


@pytest.mark.parametrize(
    "payload",
    [
        "http://licencias.intrantmoto.com/x",
        "https://licencias.intrantmoto.com.evil.example/x",
        "https://user:pw@licencias.intrantmoto.com/x",
        "https://licencias.intrantmoto.com:8443/x",
        "https://evil.example/?next=licencias.intrantmoto.com",
    ],
)
def test_not_allowlisted_urls_hide_payload(payload: str) -> None:
    res = classify(payload, detected=True, allowed_hosts=HOSTS)
    assert res.payload_kind == "url"
    assert not res.allowlisted
    assert res.authority_url is None
    assert res.digest_sha256


def test_host_case_and_trailing_dot() -> None:
    res = classify("https://LICENCIAS.intrantmoto.com./v", detected=True, allowed_hosts=HOSTS)
    assert res.allowlisted


def test_text_payload() -> None:
    res = classify("000-SYNTHETIC-PAYLOAD", detected=True, allowed_hosts=HOSTS)
    assert res.payload_kind == "text"
    assert res.authority_url is None and res.authority_host is None
