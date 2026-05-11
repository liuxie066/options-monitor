from __future__ import annotations

from src.application import close_advice_runner as close_mod
from src.application import option_chain_fetching as ocf_mod
from src.application.multi_tick import prefetch_coordinator as prefetch_coord_mod


def test_close_advice_runner_rate_limit_reason_compatibility() -> None:
    payload = {
        "meta": {
            "status": "error",
            "error_code": "RATE_LIMIT",
            "error": "频率太高",
        },
        "rows": [],
    }
    assert close_mod._fetch_payload_error_reason(payload, prefix="required_data_fetch_error") == "required_data_fetch_error_rate_limit"
    assert close_mod.classify_opend_error({"error_code": "x", "message": "too frequent"}).is_rate_limit is True
    assert close_mod.classify_opend_error(RuntimeError("最多10次")).is_rate_limit is True


def test_required_data_prefetch_rate_limit_payload_hints_compatible() -> None:
    cases = [
        {"error_code": "RATE_LIMIT", "message": "x"},
        {"message": "rate limit"},
        {"message": "too frequent"},
        {"message": "频率太高"},
        {"message": "最多10次"},
        {"message": "频率限制"},
        {"message": "请求过快"},
    ]
    for payload in cases:
        assert prefetch_coord_mod._is_opend_rate_limit_payload(payload) is True


def test_rate_gate_cache_key_includes_max_wait_sec(tmp_path) -> None:
    """Different max_wait_sec MUST NOT collide on the same cached gate.

    Regression: an earlier cache key (path, max_calls, window_sec) caused
    instances with different max_wait_sec to share one OpenDRateGate, so the
    second instance silently inherited the first's wait budget and could
    raise spurious 'rate limit wait budget exceeded' errors.
    """
    state_path = tmp_path / "gate.json"
    a = ocf_mod.FileRateLimiter(
        state_path=state_path,
        max_calls=2,
        window_sec=1.0,
        max_wait_sec=5.0,
        label="a",
    )
    b = ocf_mod.FileRateLimiter(
        state_path=state_path,
        max_calls=2,
        window_sec=1.0,
        max_wait_sec=10.0,
        label="b",
    )
    assert a._gate is not b._gate, (
        "Cache key must include max_wait_sec to avoid budget cross-contamination"
    )
