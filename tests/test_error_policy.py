from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from domain.domain.error_policy import (
    ERR_2FA_REQUIRED,
    ERR_TIMEOUT,
    ERR_UPSTREAM_UNAVAILABLE,
    classify_failure,
)


def test_timeout_classification() -> None:
    out = classify_failure(error_code="OPEND_API_ERROR", message="read timed out", upstream="opend")
    assert out["error_code"] == ERR_TIMEOUT
    assert out["category"] == "timeout"
    assert out["fallback_allowed"] is False


def test_two_factor_classification() -> None:
    out = classify_failure(error_code="OPEND_NEEDS_PHONE_VERIFY", message="waiting phone verification code", upstream="opend")
    assert out["error_code"] == ERR_2FA_REQUIRED
    assert out["category"] == "2fa"
    assert out["fallback_allowed"] is False


def test_upstream_unavailable_classification() -> None:
    out = classify_failure(error_code="OPEND_NOT_READY", message="not ready", upstream="opend")
    assert out["error_code"] == ERR_UPSTREAM_UNAVAILABLE
    assert out["category"] == "upstream_unavailable"
    assert out["retryable"] is True


def main() -> None:
    test_timeout_classification()
    test_two_factor_classification()
    test_upstream_unavailable_classification()
    print("OK (error-policy)")


if __name__ == "__main__":
    main()
