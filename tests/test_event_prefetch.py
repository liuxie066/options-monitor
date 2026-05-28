from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def _cfg() -> dict:
    return {
        "templates": {
            "put_base": {"sell_put": {"event_risk": {"enabled": True}}},
            "call_base": {"sell_call": {"enabled": True, "event_risk": {"enabled": True}}},
        },
        "symbols": [
            {
                "symbol": "AAPL",
                "use": ["put_base", "call_base"],
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": True},
                "yield_enhancement": {"enabled": True},
            }
        ],
    }


def test_event_prefetch_fetches_symbol_once_across_strategy_lanes(tmp_path: Path) -> None:
    from src.application.events.prefetch import prefetch_event_data

    calls: list[str] = []

    def fetcher(symbol: str) -> list[dict]:
        calls.append(symbol)
        return [{"type": "earnings", "date": "2026-05-01"}]

    out = prefetch_event_data(
        base=tmp_path,
        cfg=_cfg(),
        snapshot_path=tmp_path / "run_state" / "event_snapshot.json",
        fetcher=fetcher,
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )

    assert calls == ["AAPL"]
    assert out["summary"]["unique_symbols_total"] == 1
    assert out["summary"]["fetch_attempts"] == 1
    assert out["symbols"]["AAPL"]["source_status"] == "ok"


def test_event_prefetch_provider_cooldown_stops_same_run_fanout(tmp_path: Path) -> None:
    from src.application.events.source_yfinance import EventSourceError
    from src.application.events.prefetch import prefetch_event_data

    cfg = _cfg()
    cfg["symbols"].append({"symbol": "MSFT", "use": ["put_base"], "sell_put": {"enabled": True}})
    calls: list[str] = []

    def fetcher(symbol: str) -> list[dict]:
        calls.append(symbol)
        raise EventSourceError("YFRateLimitError: Too Many Requests", error_code="rate_limited")

    out = prefetch_event_data(
        base=tmp_path,
        cfg=cfg,
        snapshot_path=tmp_path / "run_state" / "event_snapshot.json",
        fetcher=fetcher,
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )

    assert calls == ["AAPL"]
    assert out["summary"]["fetch_attempts"] == 1
    assert out["summary"]["provider_cooldown"] == 1
    assert out["summary"]["rate_limited"] == 2
    assert out["symbols"]["AAPL"]["source_status"] == "error"
    assert out["symbols"]["MSFT"]["source_status"] == "error"


def test_event_prefetch_uses_stale_after_refresh_failure(tmp_path: Path) -> None:
    from src.application.events.prefetch import prefetch_event_data

    snapshot_path = tmp_path / "run_state" / "event_snapshot.json"
    prefetch_event_data(
        base=tmp_path,
        cfg=_cfg(),
        snapshot_path=snapshot_path,
        fetcher=lambda _symbol: [{"type": "earnings", "date": "2026-05-01"}],
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )

    def fail_fetcher(_symbol: str) -> list[dict]:
        raise RuntimeError("source unavailable")

    out = prefetch_event_data(
        base=tmp_path,
        cfg=_cfg(),
        snapshot_path=snapshot_path,
        fetcher=fail_fetcher,
        force_refresh=True,
        now=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )

    item = out["symbols"]["AAPL"]
    assert item["source_status"] == "stale"
    assert item["events"] == [{"type": "earnings", "date": "2026-05-01"}]
    assert "source unavailable" in item["source_error"]


def test_event_annotation_without_snapshot_fails_closed_input() -> None:
    from src.application.events.annotator import annotate_candidates_with_event_snapshot

    df = pd.DataFrame([{"symbol": "AAPL", "expiration": "2026-05-15"}])
    out = annotate_candidates_with_event_snapshot(df, snapshot={}, event_risk_cfg={"enabled": True})

    assert out.iloc[0]["event_source_status"] == "error"
    assert out.iloc[0]["event_source_error"] == "event snapshot missing for symbol"
