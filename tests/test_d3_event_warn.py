from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_d3_event_hit_is_flagged_but_not_blocked() -> None:
    from tempfile import TemporaryDirectory

    _add_repo_to_syspath()
    from scripts.d3_event_filter import annotate_candidates_with_d3_events

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
        out = annotate_candidates_with_d3_events(
            df,
            base_dir=Path(td),
            d3_event_cfg={"enabled": True, "mode": "warn"},
            event_fetcher=lambda _symbol: [{"type": "earnings", "date": "2026-05-01"}],
        )

        assert len(out) == 1
        assert bool(out.iloc[0]["event_flag"]) is True
        assert out.iloc[0]["event_types"] == "earnings"
        assert out.iloc[0]["event_dates"] == "2026-05-01"
        assert out.iloc[0]["reject_stage_candidate"] == "D3_EVENT_WARN"

