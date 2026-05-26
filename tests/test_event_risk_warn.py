from __future__ import annotations

import json
import sys
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
            event_risk_cfg={"enabled": True, "mode": "warn"},
            event_fetcher=lambda _symbol: [{"type": "earnings", "date": "2026-05-01"}],
        )

        assert len(out) == 1
        assert bool(out.iloc[0]["event_flag"]) is True
        assert out.iloc[0]["event_types"] == "earnings"
        assert out.iloc[0]["event_dates"] == "2026-05-01"
        assert out.iloc[0]["event_source_status"] == "ok"
        assert out.iloc[0]["event_source_error"] == ""
        assert out.iloc[0]["reject_stage_candidate"] == "EVENT_WARN"


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

        cache_path = base_dir / "output_shared" / "state" / "event_cache.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entry = cache["symbols"]["AAPL"]
        assert entry["source_status"] == "error"
        assert entry["last_error_type"] == "RuntimeError"
        assert "source unavailable" in entry["last_error"]
        assert "events" not in entry
        assert "fetched_at" not in entry


def test_event_risk_legacy_empty_cache_without_status_is_refetched() -> None:
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
        cache_path = base_dir / "output_shared" / "state" / "event_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "symbols": {
                        "AAPL": {
                            "fetched_at": "2026-05-01T00:00:00+00:00",
                            "events": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        out = annotate_candidates_with_event_risk(
            df,
            base_dir=base_dir,
            event_risk_cfg={"enabled": True, "mode": "warn"},
            event_fetcher=lambda _symbol: [{"type": "earnings", "date": "2026-05-01"}],
        )

        assert bool(out.iloc[0]["event_flag"]) is True
        assert out.iloc[0]["event_source_status"] == "ok"

        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        entry = cache["symbols"]["AAPL"]
        assert entry["source_status"] == "ok"
        assert entry["events"] == [{"type": "earnings", "date": "2026-05-01"}]
