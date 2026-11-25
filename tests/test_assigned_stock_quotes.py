from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from datetime import datetime
from src.application.opening_quote_evidence import normalize_underlier_observation


def _ready(price=98.0):
    return normalize_underlier_observation(
        code='US.NVDA', market='US',
        snapshot_row={'code': 'US.NVDA', 'last_price': price, 'update_time': '2026-11-25 09:59:30',
                      'sec_status': 'NORMAL', 'suspension': False},
        market_state_row={'code': 'US.NVDA', 'market_state': 'MORNING'},
        now_utc=datetime.fromisoformat('2026-11-25T15:00:00+00:00'))


class _ListGateway:
    """Gateway whose snapshot feed returns a plain list, as the OpenD path does."""

    def get_snapshot(self, codes):
        return [{"code": list(codes)[0], "last_price": 10}]

    def close(self):
        pass


def _refresh(mod, *, shares: float = 100, **kwargs):
    return mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": shares}], **kwargs
    )


def test_refresh_assigned_stock_quote_snapshots_reuses_gateway(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    calls: list[dict[str, Any]] = []

    class _Gateway:
        closed = False

        def get_snapshot(self, codes: list[str]) -> pd.DataFrame:
            calls.append({"codes": list(codes)})
            return pd.DataFrame([{"code": codes[0], "last_price": 98.0}])

        def close(self) -> None:
            self.closed = True

    gateway = _Gateway()

    def _build_gateway(**kwargs: Any) -> _Gateway:
        calls.append({"gateway_kwargs": dict(kwargs)})
        return gateway

    monkeypatch.setattr(mod, "build_ready_futu_quote_gateway", _build_gateway)

    monkeypatch.setattr(mod, "get_underlier_observations", lambda gw, codes, **kw:
                        calls.append({"codes": codes}) or {codes[0]: _ready()})
    result = _refresh(mod, cfg={"symbols": [{"symbol": "NVDA",
                                             "fetch": {"source": "futu", "host": "127.0.0.2", "port": 22222}}]},
                      base_dir=tmp_path, now_ms=lambda: 1795618800000)

    assert calls[0]["gateway_kwargs"]["host"] == "127.0.0.2"
    assert calls[0]["gateway_kwargs"]["port"] == 22222
    assert calls[1]["codes"] == ["US.NVDA"]
    assert gateway.closed is True
    quote = result.quote_snapshots[0]
    assert quote['symbol'] == 'NVDA' and quote['spot'] == 98.0
    assert quote['quote_time_ms'] == 1795618770000
    assert quote['quote_observed_at_ms'] == 1795618800000
    assert quote['quote_source'] == 'opend_realtime' and quote['quote_status'] == 'ready'
    assert mod.assigned_stock_quote_is_usable(quote, 1795618800000)
    assert result.diagnostics["status"] == "ok"
    assert result.warnings == []


def test_refresh_assigned_stock_quote_snapshots_separates_alias_and_state_base_dirs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    alias_base_dir = tmp_path / "repo"
    state_base_dir = tmp_path / "runtime"
    calls: list[dict[str, Any]] = []

    class _Gateway:
        def close(self) -> None:
            return None

    class _Underlier:
        code = "US.NVDA"
        market = "US"

    def _normalize_underlier(symbol: str, *, base_dir: Path) -> _Underlier:
        calls.append({"stage": "normalize", "symbol": symbol, "base_dir": base_dir})
        return _Underlier()

    def _observe(_gateway: Any, codes: list[str], **kwargs: Any):
        calls.append({"stage": "snapshot", "code": codes[0], "base_dir": kwargs.get("base_dir")})
        return {codes[0]: _ready(101.0)}

    monkeypatch.setattr(mod, "build_ready_futu_quote_gateway", lambda **_kwargs: _Gateway())
    monkeypatch.setattr(mod, "normalize_underlier", _normalize_underlier)
    monkeypatch.setattr(mod, "get_underlier_observations", _observe)

    result = _refresh(mod, cfg={"symbols": [{"symbol": "NVDA", "fetch": {"source": "futu"}}]},
                      base_dir=alias_base_dir, state_base_dir=state_base_dir, now_ms=lambda: 1795618800000)

    assert result.diagnostics["status"] == "ok"
    assert calls == [
        {"stage": "normalize", "symbol": "NVDA", "base_dir": alias_base_dir},
        {"stage": "snapshot", "code": "US.NVDA", "base_dir": state_base_dir},
    ]


def test_refresh_assigned_stock_quote_snapshots_degrades_when_price_missing(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    class _Gateway:
        def get_snapshot(self, codes: list[str]) -> pd.DataFrame:
            return pd.DataFrame([{"code": codes[0]}])

        def close(self) -> None:
            return None

    monkeypatch.setattr(mod, "build_ready_futu_quote_gateway", lambda **_kwargs: _Gateway())

    result = _refresh(mod, cfg={"symbols": [{"symbol": "NVDA", "fetch": {"source": "futu"}}]}, base_dir=tmp_path)

    assert result.quote_snapshots == []
    assert result.diagnostics["status"] == "missing_quote"
    assert result.diagnostics["missing_symbols"] == ["NVDA"]
    assert result.diagnostics["errors"][0]["error_code"] == "quote_price_invalid"
    assert result.warnings == ["assigned stock quote refresh missing: NVDA"]


def test_assigned_stock_complete_explicit_override_does_not_require_canonical_route(
    monkeypatch, tmp_path: Path
) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "build_ready_futu_quote_gateway",
        lambda **kwargs: calls.append(kwargs) or _ListGateway(),
    )
    result = _refresh(mod, shares=1, cfg={}, host="diagnostic", port=22222, base_dir=tmp_path)

    assert calls[0]["host"] == "diagnostic"
    assert calls[0]["port"] == 22222
    assert result.diagnostics["route_source"] == "explicit_diagnostic_override"


def test_assigned_stock_partial_override_overlays_canonical_route(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "build_ready_futu_quote_gateway",
        lambda **kwargs: calls.append(kwargs) or _ListGateway(),
    )
    result = _refresh(mod, shares=1, cfg={"symbols": [{"symbol": "NVDA", "fetch": {"source": "futu", "host": "canonical",
                                                                                 "port": 11111}}]},
                      port=33333, base_dir=tmp_path)

    assert calls[0]["host"] == "canonical"
    assert calls[0]["port"] == 33333
    assert result.diagnostics["route_source"] == "explicit_diagnostic_override"


def test_evidence_refresh_keeps_market_time_and_revalidates(monkeypatch, tmp_path):
    from datetime import datetime
    from src.application.opening_quote_evidence import normalize_underlier_observation
    import src.application.positions.assigned_stock_quotes as mod

    clock = datetime.fromisoformat('2026-11-25T15:00:00+00:00')
    stamp = int(clock.timestamp() * 1000)
    observation = normalize_underlier_observation(
        code='US.NVDA', market='US',
        snapshot_row={'code': 'US.NVDA', 'last_price': 100, 'update_time': '2026-11-25 09:59:30',
                      'sec_status': 'NORMAL', 'suspension': False},
        market_state_row={'code': 'US.NVDA', 'market_state': 'MORNING'}, now_utc=clock,
    )
    monkeypatch.setattr(mod, 'build_ready_futu_quote_gateway', lambda **kw: _ListGateway())
    monkeypatch.setattr(mod, 'get_underlier_observations', lambda *a, **kw: {'US.NVDA': observation})
    result = _refresh(mod, cfg={}, host='localhost', port=11111, base_dir=tmp_path,
                      now_ms=lambda: stamp, preserve_evidence=True)
    quote = result.quote_snapshots[0]
    assert quote['quote_time_ms'] == stamp - 30_000
    assert quote['quote_observed_at_ms'] == stamp
    assert mod.assigned_stock_quote_is_usable(quote, stamp)
    assert not mod.assigned_stock_quote_is_usable(quote, stamp + 600_000)
    assert not mod.assigned_stock_quote_is_usable({'spot': 100, 'quote_status': 'fresh'}, stamp)


def test_quote_evidence_cannot_be_reused_for_another_price_or_symbol():
    from datetime import datetime
    from src.application.opening_quote_evidence import normalize_underlier_observation
    from src.application.positions.assigned_stock_quotes import assigned_stock_quote_is_usable

    clock = datetime.fromisoformat('2026-11-25T15:00:00+00:00')
    stamp = int(clock.timestamp() * 1000)
    observation = normalize_underlier_observation(
        code='US.NVDA', market='US',
        snapshot_row={'code': 'US.NVDA', 'last_price': 100, 'update_time': '2026-11-25 09:59:30',
                      'sec_status': 'NORMAL', 'suspension': False},
        market_state_row={'code': 'US.NVDA', 'market_state': 'MORNING'}, now_utc=clock,
    ).to_dict()
    quote = {'symbol': 'NVDA', 'spot': 100, 'quote_time_ms': stamp - 30_000,
             'quote_observation': observation}
    assert assigned_stock_quote_is_usable(quote, stamp)
    assert not assigned_stock_quote_is_usable(quote | {'spot': 101}, stamp)
    assert not assigned_stock_quote_is_usable(quote | {'symbol': 'FUTU'}, stamp)
    assert not assigned_stock_quote_is_usable(quote | {'quote_time_ms': stamp}, stamp)


def test_closed_market_quote_requires_complete_original_calendar_proof():
    from datetime import datetime
    from src.application.positions.assigned_stock_quotes import assigned_stock_quote_is_usable

    def stamp(value):
        return int(datetime.fromisoformat(value).timestamp() * 1000)

    original = stamp('2026-10-23T07:56:00+00:00')
    valuation = stamp('2026-10-24T07:56:00+00:00')
    market = {
        'stocks': [{'symbol': '0700.HK', 'currency': 'HKD', 'price': '400',
                    'source': 'frozen-close', 'effective_at_ms': original,
                    'available_at_ms': original, 'valid_until_ms': original + 300_000}],
        'options': [], 'fx': [],
        'closed_market_carry': {
            'schema_version': 'om.closed-market-carry.v1', 'observation_at_ms': original,
            'calendars': {'HK': {'start': '2026-10-23', 'end': '2026-10-27',
                'source_observed_at_utc': '2026-10-22T00:00:00Z',
                'receipt': {'retcode': 0, 'coverage_complete': True, 'pagination_complete': True,
                    'page_count': 1, 'rows': [
                        {'time': day, 'trade_date_type': 'WHOLE'}
                        for day in ('2026-10-23', '2026-10-26', '2026-10-27')]}}},
        },
    }
    quote = {'symbol': '0700.HK', 'spot': '400', 'quote_time_ms': original,
             'market_evidence': market}
    assert assigned_stock_quote_is_usable(quote, valuation)
    market['closed_market_carry']['calendars']['HK']['receipt']['coverage_complete'] = False
    assert not assigned_stock_quote_is_usable(quote, valuation)


def test_frozen_market_mark_currency_and_time_must_match_lot_quote():
    from src.application.positions.assigned_stock_quotes import assigned_stock_quote_is_usable

    at = 1780000000000
    mark = {'symbol': 'NVDA', 'currency': 'USD', 'price': '100', 'source': 'frozen',
            'effective_at_ms': at - 10, 'available_at_ms': at - 5, 'valid_until_ms': at + 1000}
    market = {'stocks': [mark], 'options': [], 'fx': []}
    quote = {'symbol': 'NVDA', 'spot': '100', 'quote_time_ms': at - 10, 'market_evidence': market}
    assert assigned_stock_quote_is_usable(quote, at, expected_currency='USD')
    assert not assigned_stock_quote_is_usable(quote | {'quote_time_ms': at}, at, expected_currency='USD')
    mark['currency'] = 'HKD'
    assert not assigned_stock_quote_is_usable(quote, at, expected_currency='USD')
    assert not assigned_stock_quote_is_usable(quote, at)
