from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest

from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.option_chain_fetching import OptionChainRateLimitExceeded


def test_running_provider_does_not_hold_the_shared_gate(
    tmp_path: Path,
) -> None:
    provider_started = threading.Event()
    provider_release = threading.Event()
    results: list[str] = []

    def slow_provider() -> str:
        provider_started.set()
        assert provider_release.wait(timeout=2.0)
        return "first"

    thread = threading.Thread(
        target=lambda: results.append(
            rate_limited_opend_call(
                base_dir=tmp_path,
                endpoint="market_snapshot",
                window_sec=10.0,
                max_calls=2,
                max_wait_sec=0.01,
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
                call=lambda: "second",
            )
            == "second"
        )
    finally:
        provider_release.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert results == ["first"]


def test_provider_rate_limit_updates_shared_endpoint_cooldown(
    tmp_path: Path,
) -> None:
    def rate_limited_provider() -> None:
        raise RuntimeError("rate limit")

    with pytest.raises(RuntimeError, match="rate limit"):
        rate_limited_opend_call(
            base_dir=tmp_path,
            endpoint="option_chain",
            window_sec=0.2,
            max_calls=3,
            max_wait_sec=0.01,
            call=rate_limited_provider,
        )

    provider_calls: list[str] = []
    with pytest.raises(OptionChainRateLimitExceeded):
        rate_limited_opend_call(
            base_dir=tmp_path,
            endpoint="option_chain",
            window_sec=0.2,
            max_calls=3,
            max_wait_sec=0.01,
            call=lambda: provider_calls.append("unexpected"),
        )
    assert provider_calls == []


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
        history_kline_max_calls=2,
        history_kline_max_wait_sec=0.01,
    )

    provider_calls: list[str] = []
    rate_limited_opend_call(
        base_dir=tmp_path,
        endpoint="history_kline",
        window_sec=10.0,
        max_calls=2,
        max_wait_sec=0.01,
        call=lambda: provider_calls.append("second"),
    )
    with pytest.raises(OptionChainRateLimitExceeded):
        rate_limited_opend_call(
            base_dir=tmp_path,
            endpoint="history_kline",
            window_sec=10.0,
            max_calls=2,
            max_wait_sec=0.01,
            call=lambda: provider_calls.append("unexpected"),
        )
    assert provider_calls == ["second"]
