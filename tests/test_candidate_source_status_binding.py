from __future__ import annotations

from hashlib import sha256
import json

import pytest

from src.application.candidate_evidence_history import _validate_historical_source_status_bytes
from src.application.candidate_snapshot_manifest import (
    CandidateSnapshotManifestError,
    _validate_source_status_bytes,
)


# Fixed UTF-8, compact JSON, nested same-name field and signed zero.
CONTENT_DIGEST = "5b28747133e6c87283e8fb490815fd86baca96eb7fab3795e6ec224101c2df97"
PAYLOAD = {
    "说明": "汉字",
    "value": -0.0,
    "nested": {"content_sha256": "保留"},
    "content_sha256": CONTENT_DIGEST,
}


@pytest.fixture(params=["current", "historical-wheel"])
def validator(request):
    if request.param == "current":
        return _validate_source_status_bytes, "candidate source status"
    return _validate_historical_source_status_bytes, "historical candidate Wheel source status"


def _row(encoded: bytes, digest: str = CONTENT_DIGEST) -> dict:
    return {
        "strategy_family": "wheel",
        "source_status_sha256": sha256(encoded).hexdigest(),
        "source_status_content_sha256": digest,
    }


@pytest.mark.parametrize("sort_keys, ensure_ascii", [(False, False), (True, True)])
def test_source_content_digest_preserves_fixed_json_contract(validator, sort_keys, ensure_ascii):
    validate, _ = validator
    encoded = json.dumps(PAYLOAD, sort_keys=sort_keys, ensure_ascii=ensure_ascii, indent=2).encode()
    validate(_row(encoded), encoded)


@pytest.mark.parametrize("corruption", ["row", "payload", "both", "raw-bytes"])
def test_source_binding_rejects_tampering(validator, corruption):
    validate, context = validator
    payload = dict(PAYLOAD)
    if corruption in {"payload", "both"}:
        payload["content_sha256"] = "0" * 64
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    row = _row(encoded, "0" * 64 if corruption in {"row", "both"} else CONTENT_DIGEST)
    if corruption == "raw-bytes":
        encoded += b" "
    reason = "hash mismatch" if corruption == "raw-bytes" else "content binding mismatch"
    # Historical raw-byte binding deliberately has no Wheel qualifier.
    if corruption == "raw-bytes":
        context = context.replace("Wheel ", "")
    with pytest.raises(CandidateSnapshotManifestError) as exc:
        validate(row, encoded)
    assert str(exc.value) == f"{context} {reason}"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_source_content_binding_rejects_nonfinite_numbers(validator, token):
    validate, _ = validator
    encoded = ('{"value":' + token + ',"content_sha256":"' + CONTENT_DIGEST + '"}').encode()
    with pytest.raises(ValueError, match="Out of range float values"):
        validate(_row(encoded), encoded)


@pytest.mark.parametrize("encoded, reason", [(b"not-json", "unreadable"), (b"[]", "invalid")])
def test_source_byte_binding_precedes_json_validation(validator, encoded, reason):
    validate, context = validator
    row = _row(encoded)
    with pytest.raises(CandidateSnapshotManifestError) as exc:
        validate({**row, "source_status_sha256": "0" * 64}, encoded)
    assert str(exc.value) == context.replace("Wheel ", "") + " hash mismatch"
    with pytest.raises(CandidateSnapshotManifestError) as exc:
        validate(row, encoded)
    assert str(exc.value) == f"{context} is {reason}"
