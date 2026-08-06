from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import pandas as pd


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_event_risk_hit_is_flagged_but_not_blocked() -> None:
    from tempfile import TemporaryDirectory

    _add_repo_to_syspath()
    from src.application.event_risk_filter import annotate_candidates_with_event_risk

    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "quote_update_time": "2026-04-01 10:59:00",
                "expiration": "2026-05-15",
                "contract_symbol": "AAPL260515P00100000",
                "strike": 100.0,
            }
        ]
    )

    with TemporaryDirectory() as td:
        out = annotate_candidates_with_event_risk(
            df,
            base_dir=Path(td),
            event_risk_cfg={"enabled": True, "mode": "warn", "as_of_date": "2026-04-01"},
            event_fetcher=lambda _symbol: [{"type": "earnings", "date": "2026-05-01"}],
        )

        assert len(out) == 1
        assert bool(out.iloc[0]["event_flag"]) is True
        assert out.iloc[0]["event_types"] == "earnings"
        assert out.iloc[0]["event_dates"] == "2026-05-01"
        assert out.iloc[0]["event_source_status"] == "ok"
        assert out.iloc[0]["event_source_error"] == ""
        assert out.iloc[0]["reject_stage_candidate"] == "EVENT_WARN"


def test_sell_put_event_risk_reject_removes_candidate_and_records_reject(tmp_path: Path) -> None:
    _add_repo_to_syspath()
    from src.application.scan_sell_put import run_sell_put_scan

    parsed = tmp_path / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "quote_update_time": "2026-04-01 10:59:00",
                "option_type": "put",
                "expiration": "2026-05-15",
                "contract_symbol": "AAPL_PUT",
                "currency": "USD",
                "dte": 30,
                "strike": 95,
                "spot": 100,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": -0.2,
                "multiplier": 100,
            }
        ]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)
    output = tmp_path / "sell_put_candidates.csv"

    out = run_sell_put_scan(
        symbols=["AAPL"],
        input_root=tmp_path,
        output=output,
        min_annualized_net_return=0.01,
        min_net_income=1,
        min_open_interest=1,
        min_volume=1,
        event_risk_cfg={
            "enabled": True,
            "mode": "reject",
            "as_of_date": "2026-04-01",
            "snapshot": {
                "symbols": {
                    "AAPL": {
                        "source_status": "ok",
                        "events": [{"type": "earnings", "date": "2026-05-01"}],
                    }
                }
            },
        },
        quote_freshness_now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
        quiet=True,
    )

    assert out.empty
    reject_log = pd.read_csv(output.with_name("sell_put_candidates_reject_log.csv"))
    assert list(reject_log["reject_rule"]) == ["event_risk_reject"]

    trace_path = output.with_name("candidate_filter_trace.jsonl")
    trace_rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    matching = [row for row in trace_rows if row.get("contract_symbol") == "AAPL_PUT"]
    assert [row["status"] for row in matching] == ["rejected"]
    assert [row["rule"] for row in matching] == ["risk_event_reject"]


def test_event_risk_mode_rejects_unknown_value() -> None:
    import pytest

    _add_repo_to_syspath()
    from src.application.event_risk_filter import normalize_event_risk_cfg

    with pytest.raises(ValueError, match="event_risk.mode must be one of"):
        normalize_event_risk_cfg({"mode": "drop"})


def test_event_risk_uses_current_position_window() -> None:
    from tempfile import TemporaryDirectory

    _add_repo_to_syspath()
    from src.application.event_risk_filter import annotate_candidates_with_event_risk

    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "expiration": "2026-05-15",
                "contract_symbol": "AAPL260515P00100000",
                "strike": 100.0,
            }
        ]
    )

    events = [
        {"type": "earnings", "date": "2026-04-14"},
        {"type": "earnings", "date": "2026-05-01"},
        {"type": "earnings", "date": "2026-05-16"},
    ]
    with TemporaryDirectory() as td:
        out = annotate_candidates_with_event_risk(
            df,
            base_dir=Path(td),
            event_risk_cfg={"enabled": True, "mode": "warn", "as_of_date": "2026-04-15"},
            event_fetcher=lambda _symbol: events,
        )

        assert bool(out.iloc[0]["event_flag"]) is True
        assert out.iloc[0]["event_types"] == "earnings"
        assert out.iloc[0]["event_dates"] == "2026-05-01"


def test_event_risk_fetch_error_is_not_cached_as_empty_events() -> None:
    from tempfile import TemporaryDirectory

    _add_repo_to_syspath()
    from src.application.event_risk_filter import annotate_candidates_with_event_risk

    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "expiration": "2026-05-15",
                "contract_symbol": "AAPL260515P00100000",
                "strike": 100.0,
            }
        ]
    )

    def fail_fetcher(_symbol: str) -> list[dict]:
        raise RuntimeError("source unavailable")

    with TemporaryDirectory() as td:
        base_dir = Path(td)
        out = annotate_candidates_with_event_risk(
            df,
            base_dir=base_dir,
            event_risk_cfg={"enabled": True, "mode": "warn"},
            event_fetcher=fail_fetcher,
        )

        assert bool(out.iloc[0]["event_flag"]) is False
        assert out.iloc[0]["event_source_status"] == "error"
        assert "RuntimeError: source unavailable" in out.iloc[0]["event_source_error"]

        cache_path = base_dir / "output_shared" / "state" / "event_store.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entry = cache["symbols"]["yfinance:AAPL"]
        assert entry["source_status"] == "error"
        assert entry["last_error_type"] == "RuntimeError"
        assert "source unavailable" in entry["last_error"]
        assert "events" not in entry
        assert "fetched_at" not in entry


def test_event_risk_ok_empty_events_is_distinct_from_source_error() -> None:
    from tempfile import TemporaryDirectory

    _add_repo_to_syspath()
    from src.application.event_risk_filter import annotate_candidates_with_event_risk

    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "expiration": "2026-05-15",
                "contract_symbol": "AAPL260515P00100000",
                "strike": 100.0,
            }
        ]
    )

    with TemporaryDirectory() as td:
        base_dir = Path(td)
        out = annotate_candidates_with_event_risk(
            df,
            base_dir=base_dir,
            event_risk_cfg={"enabled": True, "mode": "warn"},
            event_fetcher=lambda _symbol: [],
        )

        assert bool(out.iloc[0]["event_flag"]) is False
        assert out.iloc[0]["event_source_status"] == "ok"

        cache_path = base_dir / "output_shared" / "state" / "event_store.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entry = cache["symbols"]["yfinance:AAPL"]
        assert entry["source_status"] == "ok"
        assert entry["events"] == []


def test_yfinance_event_source_marks_all_source_failures_as_error(monkeypatch) -> None:
    import pytest

    _add_repo_to_syspath()
    from src.application.events.source_yfinance import EventSourceError, fetch_symbol_events_yfinance

    class FailingTicker:
        def __init__(self, _symbol: str) -> None:
            pass

        def get_earnings_dates(self, *, limit: int) -> pd.DataFrame:
            raise RuntimeError("earnings unavailable")

        @property
        def calendar(self) -> pd.DataFrame:
            raise RuntimeError("calendar unavailable")

        def get_dividends(self) -> pd.Series:
            raise RuntimeError("dividends unavailable")

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=FailingTicker))

    with pytest.raises(EventSourceError, match="earnings_dates:RuntimeError"):
        fetch_symbol_events_yfinance("AAPL")
