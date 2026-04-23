from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd


def test_fetch_spot_with_fallback_parses_last_json_line(tmp_path: Path, monkeypatch) -> None:
    from scripts import pm_bridge

    pm_root = tmp_path / "portfolio-management"
    pm_python = pm_root / ".venv" / "bin" / "python"
    pm_python.parent.mkdir(parents=True, exist_ok=True)
    pm_python.write_text("", encoding="utf-8")

    def _check_output(cmd, cwd=None, timeout=None):
        assert cmd[0] == str(pm_python)
        assert cwd == str(pm_root)
        assert timeout == 12
        return b"rate cache warm\n{\"price\": 456.78}\n"

    monkeypatch.setattr("subprocess.check_output", _check_output)
    monkeypatch.setattr(pm_bridge, "_fetch_spot_from_yahoo", lambda ticker, log=None: None)

    out = pm_bridge.fetch_spot_with_fallback("nvda", pm_root=pm_root)

    assert out == 456.78


def test_fetch_spot_with_fallback_logs_when_python_missing(tmp_path: Path, monkeypatch) -> None:
    from scripts import pm_bridge

    messages: list[str] = []
    pm_root = tmp_path / "portfolio-management"
    pm_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pm_bridge, "_fetch_spot_from_yahoo", lambda ticker, log=None: None)

    out = pm_bridge.fetch_spot_with_fallback("NVDA", pm_root=pm_root, log=messages.append)

    assert out is None
    assert any("python not found" in msg for msg in messages)


def test_fetch_spot_with_fallback_uses_yahoo_provider_by_default(monkeypatch) -> None:
    from scripts import pm_bridge

    monkeypatch.setattr(pm_bridge, "_fetch_spot_from_yahoo", lambda ticker, log=None: 321.0 if ticker == "NVDA" else None)

    out = pm_bridge.fetch_spot_with_fallback("NVDA")

    assert out == 321.0


def test_fetch_spot_with_fallback_logs_when_yahoo_and_legacy_fallback_unavailable(monkeypatch) -> None:
    from scripts import pm_bridge

    messages: list[str] = []
    monkeypatch.setattr(pm_bridge, "_fetch_spot_from_yahoo", lambda ticker, log=None: None)
    monkeypatch.delenv("OM_PM_ROOT", raising=False)
    out = pm_bridge.fetch_spot_with_fallback("NVDA", log=messages.append)

    assert out is None
    assert any("legacy pm spot fallback disabled" in msg for msg in messages)


def test_resolve_spot_fallback_enabled_prefers_new_key() -> None:
    from scripts.pm_bridge import resolve_spot_fallback_enabled

    out = resolve_spot_fallback_enabled(
        {"spot_from_yahoo": False, "spot_from_portfolio_management": True},
        symbol="NVDA",
    )

    assert out is False


def test_fetch_symbol_uses_spot_fallback_for_us_symbols(monkeypatch) -> None:
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

    monkeypatch.setattr(mod, "build_futu_gateway", lambda **_kwargs: _Gateway())
    monkeypatch.setattr(
        mod,
        "normalize_underlier",
        lambda _symbol: SimpleNamespace(code="US.NVDA", market="US", currency="USD"),
    )
    monkeypatch.setattr(mod, "get_trading_date", lambda _market: pd.Timestamp("2026-04-21").date())
    monkeypatch.setattr(mod, "fetch_spot_with_fallback", lambda ticker: 123.45 if ticker == "NVDA" else None)

    out = mod.fetch_symbol("NVDA", spot_from_yahoo=True, base_dir=Path("."))

    assert out["spot"] == 123.45
    assert out["underlier_code"] == "US.NVDA"
    assert out["rows"][0]["spot"] == 123.45
