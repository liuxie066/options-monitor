"""Required-data fetches may opt into a closed-market reference without query mode."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from src.application import opend_symbol_fetching as symbol_fetching
from src.application import required_data_fetching as fetching
from src.application.required_data_planning import RequiredDataFetchSpec

SPEC = RequiredDataFetchSpec(
    symbol="US.NVDA", limit_expirations=0, host="127.0.0.1", port=11111,
    option_types=("put",), explicit_expirations=["2026-11-27"],
    min_dte=None, max_dte=None, side_strike_windows={},
    include_realized_volatility=False, trading_date=None)

REFERENCE = {
    "reference_date": "2026-11-10",
    "valid_until_utc": "2026-11-11T14:30:00+00:00",
    "market_state": "AFTER_HOURS_BEGIN",
}


def _closed_observation() -> dict:
    return {
        "schema_version": "opening_underlier_observation.v1",
        "code": "US.NVDA", "market": "US", "last_price": 168.89,
        "update_time": "2026-11-10 18:32:10.786",
        "observed_at_utc": "2026-11-10T23:32:10.786000+00:00",
        "age_seconds": 0.5, "market_state": "AFTER_HOURS_BEGIN",
        "sec_status": "NORMAL", "suspension": False,
        "status": "market_closed", "reason_code": "market_closed",
    }


def _offline_chain(**_: object) -> symbol_fetching.SymbolOptionChainResult:
    return symbol_fetching.SymbolOptionChainResult(
        rows=[], expirations_all=["2026-11-27"], expirations_pick=["2026-11-27"],
        fetch_meta={"status": "ok", "source_outcome": "success_empty",
                    "reason_code": "no_contract_rows"},
        frame=pd.DataFrame())


def _fetch(monkeypatch, tmp_path, *, allow: bool) -> tuple[dict, list[int]]:
    calls: list[int] = []

    def fake_reference(request, observation, gateway, now):
        calls.append(1)
        return dict(REFERENCE)

    monkeypatch.setattr(symbol_fetching, "fetch_symbol_option_chain", _offline_chain)
    monkeypatch.setattr(symbol_fetching, "_closed_market_reference", fake_reference)
    payload = symbol_fetching.fetch_symbol_request(symbol_fetching.FetchSymbolRequest(
        symbol="US.NVDA", limit_expirations=0,
        explicit_expirations=["2026-11-27"], trading_date="2026-11-10",
        underlier_observation=_closed_observation(),
        base_dir=Path(tmp_path), gateway=object(), no_retry=True,
        allow_closed_market_reference=allow))
    return payload, calls


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


def test_opted_in_required_data_fetch_binds_the_reference_without_query_mode(
        monkeypatch, tmp_path):
    payload, calls = _fetch(monkeypatch, tmp_path, allow=True)
    assert calls == [1]
    assert payload["meta"]["closed_market_reference"] == REFERENCE


def test_default_required_data_fetch_never_resolves_the_reference(monkeypatch, tmp_path):
    payload, calls = _fetch(monkeypatch, tmp_path, allow=False)
    assert calls == []
    assert "closed_market_reference" not in payload["meta"]
