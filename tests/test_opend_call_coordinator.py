from __future__ import annotations

import threading
import time
import fcntl
from datetime import date
from pathlib import Path

import pytest

from src.application.opend_call_coordinator import (
    LowPriorityOpenDCallDeferred,
    opend_endpoint_limiter_state_path,
    rate_limited_opend_call,
    try_low_priority_opend_call,
)
from src.application.option_chain_fetching import OptionChainRateLimitExceeded


def _low_priority_call(base_dir: Path, call):  # noqa: ANN001, ANN202
    return try_low_priority_opend_call(
        base_dir=base_dir,
        endpoint="history_kline",
        window_sec=10.0,
        max_calls=3,
        production_reserve_calls=1,
        call=call,
    )


def test_low_priority_call_defers_without_using_production_reserve(
    tmp_path: Path,
) -> None:
    provider_calls: list[str] = []

    for value in ("experiment-1", "experiment-2"):
        assert _low_priority_call(tmp_path, lambda value=value: provider_calls.append(value) or value) == value

    started = time.monotonic()
    with pytest.raises(LowPriorityOpenDCallDeferred) as raised:
        _low_priority_call(tmp_path, lambda: provider_calls.append("unexpected"))
    assert time.monotonic() - started < 1.0
    assert raised.value.reason_code == "opend_low_priority_deferred"
    assert provider_calls == ["experiment-1", "experiment-2"]

    assert (
        rate_limited_opend_call(
            base_dir=tmp_path,
            endpoint="history_kline",
            window_sec=10.0,
            max_calls=3,
            max_wait_sec=0.1,
            call=lambda: "production",
        )
        == "production"
    )


def test_running_low_priority_provider_does_not_hold_the_production_gate(
    tmp_path: Path,
) -> None:
    provider_started = threading.Event()
    provider_release = threading.Event()
    results: list[str] = []

    def slow_provider() -> str:
        provider_started.set()
        assert provider_release.wait(timeout=2.0)
        return "experiment"

    thread = threading.Thread(
        target=lambda: results.append(
            try_low_priority_opend_call(
                base_dir=tmp_path,
                endpoint="market_snapshot",
                window_sec=10.0,
                max_calls=2,
                production_reserve_calls=1,
                call=slow_provider,
            )
        )
    )
    thread.start()
    assert provider_started.wait(timeout=1.0)
    try:
        assert (
            rate_limited_opend_call(
                base_dir=tmp_path,
                endpoint="market_snapshot",
                window_sec=10.0,
                max_calls=2,
                max_wait_sec=0.1,
                call=lambda: "production",
            )
            == "production"
        )
    finally:
        provider_release.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert results == ["experiment"]


def test_low_priority_call_does_not_wait_for_shared_state_lock(tmp_path: Path) -> None:
    state_path = opend_endpoint_limiter_state_path(tmp_path, "history_kline")
    state_path.parent.mkdir(parents=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    provider_calls: list[str] = []

    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(LowPriorityOpenDCallDeferred):
            try_low_priority_opend_call(
                base_dir=tmp_path,
                endpoint="history_kline",
                window_sec=10.0,
                max_calls=3,
                production_reserve_calls=1,
                call=lambda: provider_calls.append("unexpected"),
            )
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert provider_calls == []


def test_low_priority_rate_limit_updates_shared_endpoint_cooldown(
    tmp_path: Path,
) -> None:
    def rate_limited_provider() -> None:
        raise RuntimeError("rate limit")

    with pytest.raises(RuntimeError, match="rate limit"):
        try_low_priority_opend_call(
            base_dir=tmp_path,
            endpoint="option_chain",
            window_sec=0.2,
            max_calls=3,
            production_reserve_calls=1,
            call=rate_limited_provider,
        )

    with pytest.raises(OptionChainRateLimitExceeded):
        rate_limited_opend_call(
            base_dir=tmp_path,
            endpoint="option_chain",
            window_sec=0.2,
            max_calls=3,
            max_wait_sec=0.01,
            call=lambda: "must-not-run",
        )


def test_production_realized_volatility_uses_shared_history_kline_budget(
    tmp_path: Path,
) -> None:
    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    class Gateway:
        def request_history_kline(self, **_kwargs: object) -> dict[str, object]:
            return {"data": [], "page_req_key": None}

    fetch_realized_volatility_snapshot(
        Gateway(),
        underlier_code="US.NVDA",
        trading_day=date(2026, 8, 30),
        base_dir=tmp_path,
        history_kline_window_sec=10.0,
        history_kline_max_calls=3,
        history_kline_max_wait_sec=0.1,
    )

    assert _low_priority_call(tmp_path, lambda: "experiment") == "experiment"
    with pytest.raises(LowPriorityOpenDCallDeferred):
        _low_priority_call(tmp_path, lambda: "must-not-run")
