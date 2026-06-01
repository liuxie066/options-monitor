from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest


class FakeFutuEventGateway:
    def __init__(self) -> None:
        self.closed = False
        self.split_calls = []

    def get_financials_earnings_price_history(self, code: str):
        assert code == "US.NVDA"
        return pd.DataFrame(
            [
                {
                    "pub_trading_day_str": "2026-06-01",
                    "period_text": "Q1",
                    "is_current": True,
                }
            ]
        )

    def get_corporate_actions_dividends(self, code: str):
        assert code == "US.NVDA"
        return {"dividend_list": [{"ex_date": 20260605, "record_date": "2026-06-06"}]}

    def get_corporate_actions_stock_splits(self, code: str, *, next_key=None, num=None):
        assert code == "US.NVDA"
        self.split_calls.append({"next_key": next_key, "num": num})
        if next_key is None:
            return {"next_key": "page-2", "split_list": [{"ex_date_str": "2026/06/10", "rate": "2:1"}]}
        return {"next_key": "-1", "split_list": [{"ex_date_str": "2026-06-12", "rate": "3:1"}]}

    def close(self) -> None:
        self.closed = True


def test_fetch_symbol_events_futu_normalizes_earnings_dividends_and_splits() -> None:
    from src.application.events.source_futu import fetch_symbol_events_futu

    gateway = FakeFutuEventGateway()
    events = fetch_symbol_events_futu("NVDA", gateway=gateway, close_gateway=False)

    assert [(item["type"], item["date"]) for item in events] == [
        ("earnings", "2026-06-01"),
        ("ex_dividend", "2026-06-05"),
        ("split", "2026-06-10"),
        ("split", "2026-06-12"),
    ]
    assert all(item["source"] == "futu" for item in events)
    assert gateway.closed is False
    assert gateway.split_calls == [{"next_key": None, "num": 50}, {"next_key": "page-2", "num": 50}]


def test_fetch_symbol_events_futu_reports_old_sdk_capability_gap() -> None:
    from src.application.events.source_futu import fetch_symbol_events_futu
    from src.application.events.source_yfinance import EventSourceError

    class OldGateway:
        def get_financials_earnings_price_history(self, _code: str):
            raise AttributeError("get_financials_earnings_price_history unavailable; upgrade futu-api")

        def get_corporate_actions_dividends(self, _code: str):
            raise AttributeError("get_corporate_actions_dividends unavailable; upgrade futu-api")

        def get_corporate_actions_stock_splits(self, _code: str, *, next_key=None, num=None):
            raise AttributeError("get_corporate_actions_stock_splits unavailable; upgrade futu-api")

    with pytest.raises(EventSourceError) as exc:
        fetch_symbol_events_futu("NVDA", gateway=OldGateway())

    assert exc.value.error_code == "capability_missing"


def test_event_prefetch_can_use_futu_provider_with_explicit_fetcher(tmp_path: Path) -> None:
    from src.application.events.prefetch import prefetch_event_data

    cfg = {
        "templates": {"put_base": {"sell_put": {"event_risk": {"enabled": True}}}},
        "symbols": [
            {
                "symbol": "AAPL",
                "use": ["put_base"],
                "sell_put": {"enabled": True, "strategy": "short_vol"},
            }
        ],
    }

    out = prefetch_event_data(
        base=tmp_path,
        cfg=cfg,
        snapshot_path=tmp_path / "run_state" / "event_snapshot.json",
        provider="futu",
        fetcher=lambda _symbol: [{"type": "earnings", "date": "2026-06-01"}],
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert out["provider"] == "futu"
    assert out["symbols"]["AAPL"]["provider"] == "futu"


def test_event_source_probe_cli_handler_splits_symbol_args() -> None:
    from src.interfaces.cli.event_source_ops import handle_event_source_command
    from src.interfaces.cli.main import parse_args

    args = parse_args(["event-source", "probe", "--provider", "futu", "--symbols", "NVDA,0700.HK", "AAPL"])
    calls = []

    def fake_probe(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "provider": kwargs["provider"], "symbols": {}}

    out = handle_event_source_command(args, probe_event_source_fn=fake_probe)

    assert out["ok"] is True
    assert calls == [
        {
            "provider": "futu",
            "symbols": ["NVDA", "0700.HK", "AAPL"],
            "host": "127.0.0.1",
            "port": 11111,
        }
    ]


def test_event_source_probe_all_collects_provider_results(monkeypatch) -> None:
    from src.application.events import probe as probe_mod

    monkeypatch.setattr(
        probe_mod,
        "_probe_futu",
        lambda **_kwargs: {
            "ok": True,
            "provider": "futu",
            "symbols": {"NVDA": {"ok": True, "event_count": 2}},
        },
    )
    monkeypatch.setattr(
        probe_mod,
        "_probe_yfinance",
        lambda **_kwargs: {
            "ok": False,
            "provider": "yfinance",
            "symbols": {"NVDA": {"ok": False, "error_code": "rate_limited"}},
        },
    )

    out = probe_mod.probe_event_source(provider="all", symbols=["NVDA"])

    assert out["ok"] is True
    assert out["provider"] == "all"
    assert out["symbols"]["NVDA"]["ok"] is True
    assert out["symbols"]["NVDA"]["source_results"]["futu"]["ok"] is True
    assert out["symbols"]["NVDA"]["source_results"]["yfinance"]["error_code"] == "rate_limited"
    assert out["summary"]["provider_ok"] == {"futu": True, "yfinance": False}
