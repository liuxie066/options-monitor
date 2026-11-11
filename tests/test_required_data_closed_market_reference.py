"""Required-data fetches may opt into a closed-market reference without query mode."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.application import opend_symbol_fetching as symbol_fetching
from src.application import required_data_fetching as fetching
from src.application.required_data_planning import RequiredDataFetchSpec

SPEC = RequiredDataFetchSpec(
    symbol="US.NVDA", limit_expirations=0, host="127.0.0.1", port=11111,
    option_types=("put",), explicit_expirations=["2026-11-27"],
    min_dte=None, max_dte=None, side_strike_windows={},
    include_realized_volatility=False, trading_date=None)


def test_build_fetch_request_defaults_to_no_closed_market_reference():
    request = fetching.build_fetch_request_from_spec(spec=SPEC)
    assert request.allow_closed_market_reference is False


def test_build_fetch_request_carries_the_explicit_opt_in():
    request = fetching.build_fetch_request_from_spec(
        spec=SPEC, allow_closed_market_reference=True)
    assert request.allow_closed_market_reference is True


def test_execute_required_data_maps_the_flag_to_the_symbol_fetch(monkeypatch):
    captured = {}

    def fake_fetch(request, **kwargs):
        captured["request"] = request
        return {"rows": []}

    monkeypatch.setattr(fetching, "fetch_symbol_request", fake_fetch)
    fetching.execute_required_data_opend(
        base=Path("/tmp"),
        request=fetching.RequiredDataFetchRequest(
            symbol="US.NVDA", limit_expirations=0,
            explicit_expirations=["2026-11-27"],
            allow_closed_market_reference=True))
    assert captured["request"].allow_closed_market_reference is True


def test_closed_market_reference_is_not_gated_on_query_mode():
    closed = SimpleNamespace(status="market_closed")
    ready = SimpleNamespace(status="ready")
    enabled = SimpleNamespace(allow_closed_market_reference=True)
    disabled = SimpleNamespace(allow_closed_market_reference=False)
    assert symbol_fetching._should_resolve_closed_market_reference(enabled, closed)
    assert not symbol_fetching._should_resolve_closed_market_reference(disabled, closed)
    assert not symbol_fetching._should_resolve_closed_market_reference(enabled, ready)
