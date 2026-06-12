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

    monkeypatch.setattr(mod, "build_ready_futu_gateway", _build_gateway)

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


def test_refresh_assigned_stock_quote_snapshots_degrades_when_price_missing(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.assigned_stock_quotes as mod

    class _Gateway:
        def get_snapshot(self, codes: list[str]) -> pd.DataFrame:
            return pd.DataFrame([{"code": codes[0]}])

        def close(self) -> None:
            return None

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **_kwargs: _Gateway())

    result = mod.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 100}],
        cfg={},
        base_dir=tmp_path,
    )

    assert result.quote_snapshots == []
    assert result.diagnostics["status"] == "missing_quote"
    assert result.diagnostics["missing_symbols"] == ["NVDA"]
    assert result.diagnostics["errors"][0]["error_code"] == "MISSING_PRICE"
    assert result.warnings == ["assigned stock quote refresh missing: NVDA"]
