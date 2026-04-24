from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd


def test_fetch_spot_with_fallback_logs_removed_message(tmp_path: Path) -> None:
    from scripts import pm_bridge

    messages: list[str] = []
    out = pm_bridge.fetch_spot_with_fallback("NVDA", pm_root=tmp_path, log=messages.append)

    assert out is None
    assert any("external spot fallback removed" in msg for msg in messages)


def test_resolve_spot_fallback_enabled_always_false() -> None:
    from scripts.pm_bridge import resolve_spot_fallback_enabled

    out = resolve_spot_fallback_enabled(
        {"spot_from_yahoo": False, "spot_from_portfolio_management": True},
        symbol="NVDA",
    )

    assert out is False


def test_fetch_symbol_keeps_us_spot_missing_without_fallback(monkeypatch) -> None:
    import scripts.fetch_market_data_opend as mod

    class _Gateway:
        @staticmethod
        def ensure_quote_ready():
            return None

        @staticmethod
        def get_option_expiration_dates(_code):
            return pd.DataFrame([{"strike_time": "2026-05-15"}])

        @staticmethod
        def get_option_chain(code=None, start=None, end=None, is_force_refresh=False):
            return pd.DataFrame(
                [
                    {
                        "code": f"{code}260515P00100000",
                        "strike_time": "2026-05-15",
                        "strike_price": 100.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    }
                ]
            )

        @staticmethod
        def get_snapshot(_codes):
            return pd.DataFrame()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **_kwargs: _Gateway())
    monkeypatch.setattr(
        mod,
        "normalize_underlier",
        lambda _symbol: SimpleNamespace(code="US.NVDA", market="US", currency="USD"),
    )
    monkeypatch.setattr(mod, "get_trading_date", lambda _market: pd.Timestamp("2026-04-21").date())
    out = mod.fetch_symbol("NVDA", base_dir=Path("."))

    assert out["spot"] is None
    assert out["underlier_code"] == "US.NVDA"
    assert out["rows"][0]["spot"] is None
