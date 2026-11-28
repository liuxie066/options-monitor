"""Wire compatibility of the shared continuation protocol."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from src.application.ledger.api import (
    TradeEventPaginationError,
    decode_evidence_cursor,
    encode_evidence_cursor,
)
from src.infrastructure import signed_cursor


def test_ledger_compatibility_exports_share_the_canonical_codec() -> None:
    assert encode_evidence_cursor is signed_cursor.encode_cursor
    assert decode_evidence_cursor is signed_cursor.decode_cursor
    assert TradeEventPaginationError is signed_cursor.SignedCursorError


def _token(raw: bytes, key: str = "fixture-key") -> str:
    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"{b64(raw)}.{b64(hmac.new(key.encode(), raw, hashlib.sha256).digest())}"


def test_cursor_preserves_wire_bytes_and_expiry_boundary() -> None:
    raw = '{"binding":"账户","exp":1900,"iat":100,"offset":2}'.encode()
    token = encode_evidence_cursor({"binding": "账户", "offset": 2}, " fixture-key ", now=100)
    assert token == _token(raw)
    assert decode_evidence_cursor(token, "fixture-key", now=1899) == json.loads(raw)
    with pytest.raises(TradeEventPaginationError) as exc:
        decode_evidence_cursor(token, "fixture-key", now=1900)
    assert exc.value.code == "cursor_expired"


@pytest.mark.parametrize(("raw", "code"), [
    (b"[]", "invalid_cursor"),
    (b'{"iat":true,"exp":1900}', "invalid_cursor"),
    (b'{"iat":101,"exp":1900}', "invalid_cursor"),
    (b'{"iat":100,"exp":"1900"}', "invalid_cursor"),
])
def test_signed_invalid_state_is_rejected(raw: bytes, code: str) -> None:
    with pytest.raises(TradeEventPaginationError) as exc:
        decode_evidence_cursor(_token(raw), "fixture-key", now=100)
    assert exc.value.code == code


def test_cursor_rejects_wrong_key_and_missing_key() -> None:
    token = encode_evidence_cursor({}, "fixture-key", now=100)
    with pytest.raises(TradeEventPaginationError) as exc:
        decode_evidence_cursor(token, "other-key", now=100)
    assert exc.value.code == "invalid_cursor_signature"
    for operation, value in ((encode_evidence_cursor, {}), (decode_evidence_cursor, token)):
        with pytest.raises(TradeEventPaginationError) as exc:
            operation(value, " ", now=100)
        assert exc.value.code == "cursor_key_unavailable"
