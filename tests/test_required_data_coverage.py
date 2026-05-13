from __future__ import annotations

import pandas as pd
from pathlib import Path


def _write_required_data_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_strategy_bounds_coverage_requires_requested_side_and_strikes(tmp_path: Path) -> None:
    from src.application.required_data_coverage import required_data_csv_covers_strategy_bounds

    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "NVDA_required_data.csv",
        [
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 80},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 90},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 100},
            {"option_type": "put", "expiration": "2026-07-17", "dte": 60, "strike": 80},
            {"option_type": "put", "expiration": "2026-07-17", "dte": 60, "strike": 90},
            {"option_type": "put", "expiration": "2026-07-17", "dte": 60, "strike": 100},
        ],
    )

    assert required_data_csv_covers_strategy_bounds(
        parsed=parsed,
        option_types="put",
        min_dte=20,
        max_dte=60,
        side_strike_windows={"put": {"min_strike": 80, "max_strike": 100}},
    ) is True

    assert required_data_csv_covers_strategy_bounds(
        parsed=parsed,
        option_types="call",
        min_dte=20,
        max_dte=60,
        side_strike_windows={"call": {"min_strike": 110, "max_strike": 130}},
    ) is False


def test_strategy_bounds_coverage_requires_requested_max_dte(tmp_path: Path) -> None:
    from src.application.required_data_coverage import required_data_csv_covers_strategy_bounds

    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "NVDA_required_data.csv",
        [
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 80},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 90},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 100},
        ],
    )

    assert required_data_csv_covers_strategy_bounds(
        parsed=parsed,
        option_types="put",
        min_dte=20,
        max_dte=60,
        side_strike_windows={"put": {"min_strike": 80, "max_strike": 100}},
    ) is False


def test_fetch_plan_coverage_requires_each_requested_expiration(tmp_path: Path) -> None:
    from src.application.required_data_coverage import required_data_csv_covers_fetch_plan
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        RequiredDataFetchSpec,
        StrikeWindowPlan,
    )

    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "0700.HK_required_data.csv",
        [
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 360},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 400},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 450},
        ],
    )
    side_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=20,
        max_dte=60,
        explicit_expirations=["2026-06-19", "2026-07-17"],
        strike_window=StrikeWindowPlan(
            min_strike=360,
            max_strike=450,
            source="test",
            base_min_strike=360,
            base_max_strike=450,
        ),
        planning_reason="test",
    )
    fetch_plan = RequiredDataFetchPlanBundle(
        symbol="0700.HK",
        spot_reference=400,
        side_plans=[side_plan],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol="0700.HK",
                limit_expirations=2,
                host="127.0.0.1",
                port=11111,
                option_types=("put",),
                explicit_expirations=["2026-06-19", "2026-07-17"],
                min_dte=20,
                max_dte=60,
                side_strike_windows={"put": {"min_strike": 360, "max_strike": 450}},
                side_plans=[side_plan],
            )
        ],
    )

    assert required_data_csv_covers_fetch_plan(parsed=parsed, fetch_plan=fetch_plan) is False
