from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


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

    result = mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 100}],
        cfg={
            "symbols": [
                {
                    "symbol": "NVDA",
                    "fetch": {"source": "futu", "host": "127.0.0.2", "port": 22222},
                }
            ]
        },
        base_dir=tmp_path,
        now_ms=lambda: 1780000000000,
    )

    assert calls[0]["gateway_kwargs"]["host"] == "127.0.0.2"
    assert calls[0]["gateway_kwargs"]["port"] == 22222
    assert calls[1]["codes"] == ["US.NVDA"]
    assert gateway.closed is True
    assert result.quote_snapshots == [
        {
            "symbol": "NVDA",
            "spot": 98.0,
            "quote_time_ms": 1780000000000,
            "quote_source": "opend_realtime",
            "quote_status": "fresh",
        }
    ]
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

    def _normalize_underlier(symbol: str, *, base_dir: Path) -> _Underlier:
        calls.append({"stage": "normalize", "symbol": symbol, "base_dir": base_dir})
        return _Underlier()

    def _get_spot_opend(_gateway: Any, code: str, **kwargs: Any) -> float:
        calls.append({"stage": "snapshot", "code": code, "base_dir": kwargs.get("base_dir")})
        return 101.0

    monkeypatch.setattr(mod, "build_ready_futu_quote_gateway", lambda **_kwargs: _Gateway())
    monkeypatch.setattr(mod, "normalize_underlier", _normalize_underlier)
    monkeypatch.setattr(mod, "get_spot_opend", _get_spot_opend)

    result = mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 100}],
        cfg={"symbols": [{"symbol": "NVDA", "fetch": {"source": "futu"}}]},
        base_dir=alias_base_dir,
        state_base_dir=state_base_dir,
    )

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

    result = mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 100}],
        cfg={"symbols": [{"symbol": "NVDA", "fetch": {"source": "futu"}}]},
        base_dir=tmp_path,
    )

    assert result.quote_snapshots == []
    assert result.diagnostics["status"] == "missing_quote"
    assert result.diagnostics["missing_symbols"] == ["NVDA"]
    assert result.diagnostics["errors"][0]["error_code"] == "MISSING_PRICE"
    assert result.warnings == ["assigned stock quote refresh missing: NVDA"]


def test_assigned_stock_complete_explicit_override_does_not_require_canonical_route(
    monkeypatch, tmp_path: Path
) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    calls: list[dict[str, Any]] = []

    class _Gateway:
        def get_snapshot(self, codes):
            return [{"code": list(codes)[0], "last_price": 10}]

        def close(self):
            pass

    monkeypatch.setattr(
        mod,
        "build_ready_futu_quote_gateway",
        lambda **kwargs: calls.append(kwargs) or _Gateway(),
    )
    result = mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 1}],
        cfg={},
        host="diagnostic",
        port=22222,
        base_dir=tmp_path,
    )

    assert calls[0]["host"] == "diagnostic"
    assert calls[0]["port"] == 22222
    assert result.diagnostics["route_source"] == "explicit_diagnostic_override"


def test_assigned_stock_partial_override_overlays_canonical_route(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    calls: list[dict[str, Any]] = []

    class _Gateway:
        def get_snapshot(self, codes):
            return [{"code": list(codes)[0], "last_price": 10}]

        def close(self):
            pass

    monkeypatch.setattr(
        mod,
        "build_ready_futu_quote_gateway",
        lambda **kwargs: calls.append(kwargs) or _Gateway(),
    )
    result = mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 1}],
        cfg={"symbols": [{"symbol": "NVDA", "fetch": {"source": "futu", "host": "canonical", "port": 11111}}]},
        port=33333,
        base_dir=tmp_path,
    )

    assert calls[0]["host"] == "canonical"
    assert calls[0]["port"] == 33333
    assert result.diagnostics["route_source"] == "explicit_diagnostic_override"
