from __future__ import annotations

import json
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
                "sell_put": {"enabled": True, "strategy": "short_vol"},
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


def test_event_prefetch_includes_yield_enhancement_for_return_first() -> None:
    from src.application.events.prefetch import build_event_prefetch_plan

    cfg = _cfg()
    cfg["symbols"][0]["sell_put"]["strategy"] = "return_first"

    plan = build_event_prefetch_plan(cfg)

    assert plan["symbols"] == ["AAPL"]
    assert "yield_enhancement" in plan["reasons"]["AAPL"]


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


def test_event_prefetch_resolves_primary_fallback_chain(tmp_path: Path) -> None:
    from src.application.events.prefetch import prefetch_event_data
    from src.application.events.source_yfinance import EventSourceError

    cfg = _cfg()
    cfg["runtime"] = {
        "event_risk_source": {
            "mode": "primary_fallback",
            "default_provider": "futu",
            "providers": {
                "futu": {"enabled": True, "role": "primary"},
                "yfinance": {"enabled": True, "role": "fallback"},
            },
            "market_rules": {"us": {"chain": ["futu", "yfinance"]}},
        }
    }
    calls: list[tuple[str, str]] = []

    def futu_fetcher(symbol: str) -> list[dict]:
        calls.append(("futu", symbol))
        raise EventSourceError("OpenD source unavailable", error_code="source_error")

    def yfinance_fetcher(symbol: str) -> list[dict]:
        calls.append(("yfinance", symbol))
        return [{"type": "earnings", "date": "2026-06-01"}]

    out = prefetch_event_data(
        base=tmp_path,
        cfg=cfg,
        snapshot_path=tmp_path / "run_state" / "event_snapshot.json",
        fetchers={"futu": futu_fetcher, "yfinance": yfinance_fetcher},
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    item = out["symbols"]["AAPL"]
    assert out["provider"] == "resolved"
    assert item["source_status"] == "ok_with_fallback"
    assert item["selected_provider"] == "yfinance"
    assert item["events"] == [{"type": "earnings", "date": "2026-06-01"}]
    assert item["source_results"]["futu"]["source_status"] == "error"
    assert item["source_results"]["yfinance"]["source_status"] == "ok"
    assert out["summary"]["fallback_used"] == 1
    assert calls == [("futu", "AAPL"), ("yfinance", "AAPL")]


def test_event_prefetch_market_rules_keep_hk_on_futu(tmp_path: Path) -> None:
    from src.application.events.prefetch import prefetch_event_data

    cfg = _cfg()
    cfg["symbols"] = [
        {"symbol": "0700.HK", "use": ["put_base"], "sell_put": {"enabled": True, "strategy": "short_vol"}}
    ]
    cfg["runtime"] = {
        "event_risk_source": {
            "mode": "primary_fallback",
            "default_provider": "futu",
            "providers": {
                "futu": {"enabled": True, "role": "primary"},
                "yfinance": {"enabled": True, "role": "fallback"},
            },
            "market_rules": {
                "hk": {"chain": ["futu"]},
                "us": {"chain": ["futu", "yfinance"]},
            },
        }
    }
    calls: list[tuple[str, str]] = []

    def futu_fetcher(symbol: str) -> list[dict]:
        calls.append(("futu", symbol))
        return [{"type": "earnings", "date": "2026-06-01"}]

    def yfinance_fetcher(symbol: str) -> list[dict]:
        calls.append(("yfinance", symbol))
        return [{"type": "earnings", "date": "2026-06-02"}]

    out = prefetch_event_data(
        base=tmp_path,
        cfg=cfg,
        snapshot_path=tmp_path / "run_state" / "event_snapshot.json",
        fetchers={"futu": futu_fetcher, "yfinance": yfinance_fetcher},
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    item = out["symbols"]["0700.HK"]
    assert item["source_status"] == "ok"
    assert item["selected_provider"] == "futu"
    assert item["provider_chain"] == ["futu"]
    assert calls == [("futu", "0700.HK")]


def test_event_annotation_without_snapshot_fails_closed_input() -> None:
    from src.application.events.annotator import annotate_candidates_with_event_snapshot

    df = pd.DataFrame([{"symbol": "AAPL", "expiration": "2026-05-15"}])
    out = annotate_candidates_with_event_snapshot(df, snapshot={}, event_risk_cfg={"enabled": True})

    assert out.iloc[0]["event_source_status"] == "error"
    assert out.iloc[0]["event_source_error"] == "event snapshot missing for symbol"


def test_resolved_event_risk_config_preserves_runtime_snapshot_path(tmp_path: Path) -> None:
    from domain.domain.candidate_defaults import resolve_event_risk_config
    from src.application.event_risk_filter import annotate_candidates_with_event_risk

    snapshot_path = tmp_path / "state" / "event_snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "yfinance",
                "symbols": {
                    "AAPL": {
                        "symbol": "AAPL",
                        "events": [],
                        "source_status": "error",
                        "source_error": "EventSourceError: rate limited",
                        "error_code": "rate_limited",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = resolve_event_risk_config(
        {"enabled": True, "mode": "warn", "snapshot_path": str(snapshot_path)}
    )

    out = annotate_candidates_with_event_risk(
        pd.DataFrame([{"symbol": "AAPL", "expiration": "2026-05-15"}]),
        base_dir=tmp_path,
        event_risk_cfg=cfg,
    )

    assert cfg["snapshot_path"] == str(snapshot_path)
    assert out.iloc[0]["event_source_status"] == "error"
    assert out.iloc[0]["event_source_error"] == "EventSourceError: rate limited"
