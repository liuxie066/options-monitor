from __future__ import annotations

from dataclasses import replace
import pandas as pd
from pathlib import Path


def _write_required_data_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _expiration_discovery(trading_date: str):  # type: ignore[no-untyped-def]
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )

    return OptionExpirationDiscoveryResult(
        outcome="success_rows",
        reason_code=None,
        expirations=[],
        observed_at_utc="2026-05-20T01:00:00Z",
        completed_at_utc="2026-05-20T01:00:01Z",
        request_identity={"trading_date": trading_date},
    )


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


def test_strategy_bounds_coverage_requires_rv_when_requested(tmp_path: Path) -> None:
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
        require_realized_volatility=True,
    ) is False

    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "NVDA_required_data.csv",
        [
            {
                "option_type": "put",
                "expiration": "2026-06-19",
                "dte": 30,
                "strike": 80,
                "realized_volatility_estimate": 0.24,
            },
        ],
    )

    assert required_data_csv_covers_strategy_bounds(
        parsed=parsed,
        option_types="put",
        require_realized_volatility=True,
    ) is True


def test_fetch_plan_coverage_requires_spot_reference_match(tmp_path: Path) -> None:
    from src.application.required_data_coverage import required_data_csv_covers_fetch_plan
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        RequiredDataFetchSpec,
        StrikeWindowPlan,
    )

    side_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=20,
        max_dte=60,
        explicit_expirations=["2026-06-19"],
        strike_window=StrikeWindowPlan(
            min_strike=80,
            max_strike=100,
            source="test",
            base_min_strike=80,
            base_max_strike=100,
        ),
        planning_reason="test",
    )
    fetch_plan = RequiredDataFetchPlanBundle(
        symbol="NVDA",
        spot_reference=100.0,
        side_plans=[side_plan],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol="NVDA",
                limit_expirations=1,
                host="127.0.0.1",
                port=11111,
                option_types=("put",),
                explicit_expirations=["2026-06-19"],
                min_dte=20,
                max_dte=60,
                side_strike_windows={"put": {"min_strike": 80, "max_strike": 100}},
                side_plans=[side_plan],
                trading_date="2026-05-20",
            )
        ],
        expiration_discovery=_expiration_discovery("2026-05-20"),
        require_realized_volatility=False,
    )
    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "NVDA_required_data.csv",
        [
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 80, "spot": 100.0},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 90, "spot": 100.0},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 100, "spot": 100.0},
        ],
    )

    assert required_data_csv_covers_fetch_plan(parsed=parsed, fetch_plan=fetch_plan) is True

    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "NVDA_required_data.csv",
        [
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 80, "spot": 99.5},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 90, "spot": 99.5},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 100, "spot": 99.5},
        ],
    )

    assert required_data_csv_covers_fetch_plan(parsed=parsed, fetch_plan=fetch_plan) is False


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
                trading_date="2026-05-20",
            )
        ],
        expiration_discovery=_expiration_discovery("2026-05-20"),
        require_realized_volatility=False,
    )

    assert required_data_csv_covers_fetch_plan(parsed=parsed, fetch_plan=fetch_plan) is False


def test_fetch_plan_coverage_rejects_bounded_range_missing_lower_edge(tmp_path: Path) -> None:
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
            {"option_type": "call", "expiration": "2026-06-29", "dte": 21, "strike": 550},
            {"option_type": "call", "expiration": "2026-06-29", "dte": 21, "strike": 600},
            {"option_type": "call", "expiration": "2026-06-29", "dte": 21, "strike": 670},
        ],
    )
    side_plan = OptionSideFetchPlan(
        option_type="call",
        min_dte=20,
        max_dte=90,
        explicit_expirations=["2026-06-29"],
        strike_window=StrikeWindowPlan(
            min_strike=444.8,
            max_strike=673.2,
            source="test",
            base_min_strike=444.8,
            base_max_strike=673.2,
        ),
        planning_reason="test",
    )
    fetch_plan = RequiredDataFetchPlanBundle(
        symbol="0700.HK",
        spot_reference=444.8,
        side_plans=[side_plan],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol="0700.HK",
                limit_expirations=1,
                host="127.0.0.1",
                port=11111,
                option_types=("call",),
                explicit_expirations=["2026-06-29"],
                min_dte=20,
                max_dte=90,
                side_strike_windows={"call": {"min_strike": 444.8, "max_strike": 673.2}},
                side_plans=[side_plan],
                trading_date="2026-06-08",
            )
        ],
        expiration_discovery=_expiration_discovery("2026-06-08"),
        require_realized_volatility=False,
    )

    assert required_data_csv_covers_fetch_plan(parsed=parsed, fetch_plan=fetch_plan) is False


def test_fetch_plan_coverage_requires_rv_for_short_vol_spec(tmp_path: Path) -> None:
    from src.application.required_data_coverage import (
        required_data_csv_covers_fetch_plan,
        required_data_frame_covers_fetch_plan,
        required_data_frame_covers_fetch_plan_debug,
    )
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        RequiredDataFetchSpec,
        StrikeWindowPlan,
    )

    parsed = _write_required_data_csv(
        tmp_path / "parsed" / "NVDA_required_data.csv",
        [
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 80},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 90},
            {"option_type": "put", "expiration": "2026-06-19", "dte": 30, "strike": 100},
        ],
    )
    side_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=20,
        max_dte=60,
        explicit_expirations=["2026-06-19"],
        strike_window=StrikeWindowPlan(
            min_strike=80,
            max_strike=100,
            source="test",
            base_min_strike=80,
            base_max_strike=100,
        ),
        planning_reason="test",
    )
    fetch_plan = RequiredDataFetchPlanBundle(
        symbol="NVDA",
        spot_reference=100,
        side_plans=[side_plan],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol="NVDA",
                limit_expirations=1,
                host="127.0.0.1",
                port=11111,
                option_types=("put",),
                explicit_expirations=["2026-06-19"],
                min_dte=20,
                max_dte=60,
                side_strike_windows={"put": {"min_strike": 80, "max_strike": 100}},
                include_realized_volatility=True,
                side_plans=[side_plan],
                trading_date="2026-05-20",
            )
        ],
        expiration_discovery=_expiration_discovery("2026-05-20"),
        require_realized_volatility=True,
    )

    assert required_data_csv_covers_fetch_plan(parsed=parsed, fetch_plan=fetch_plan) is False

    valid_rows = pd.DataFrame(
        [
            {
                "option_type": "put",
                "expiration": "2026-06-19",
                "dte": 30,
                "strike": strike,
                "spot": 100.0,
                "realized_volatility_estimate": 0.24,
            }
            for strike in (80, 90, 100)
        ]
    )
    assert required_data_frame_covers_fetch_plan(df=valid_rows, fetch_plan=fetch_plan) is True
    assert required_data_frame_covers_fetch_plan_debug(valid_rows, fetch_plan.to_debug_dict()) is True

    mismatched_date_plan = replace(
        fetch_plan,
        merged_specs=[
            replace(
                fetch_plan.merged_specs[0],
                trading_date="2026-05-21",
            )
        ],
    )
    assert required_data_frame_covers_fetch_plan(
        df=valid_rows,
        fetch_plan=mismatched_date_plan,
    ) is False
    assert required_data_frame_covers_fetch_plan_debug(
        valid_rows,
        mismatched_date_plan.to_debug_dict(),
    ) is False

    mismatched_rv_plan = replace(
        fetch_plan,
        require_realized_volatility=False,
    )
    assert required_data_frame_covers_fetch_plan(
        df=valid_rows,
        fetch_plan=mismatched_rv_plan,
    ) is False
    assert required_data_frame_covers_fetch_plan_debug(
        valid_rows,
        mismatched_rv_plan.to_debug_dict(),
    ) is False

    empty_executable_plan = replace(
        fetch_plan,
        merged_specs=[
            replace(
                fetch_plan.merged_specs[0],
                explicit_expirations=[],
            )
        ],
    )
    assert required_data_frame_covers_fetch_plan(
        df=valid_rows,
        fetch_plan=empty_executable_plan,
    ) is False
    assert required_data_frame_covers_fetch_plan_debug(
        valid_rows,
        empty_executable_plan.to_debug_dict(),
    ) is False

    invalid_rv_type_plan = replace(
        fetch_plan,
        require_realized_volatility=1,  # type: ignore[arg-type]
    )
    assert required_data_frame_covers_fetch_plan(
        df=valid_rows,
        fetch_plan=invalid_rv_type_plan,
    ) is False
    assert required_data_frame_covers_fetch_plan_debug(
        valid_rows,
        invalid_rv_type_plan.to_debug_dict(),
    ) is False


def test_required_rv_coverage_requires_every_value_to_be_finite_and_positive() -> None:
    from src.application.required_data_coverage import required_data_frame_covers_strategy_bounds

    base_row = {
        "option_type": "put",
        "expiration": "2026-06-19",
        "dte": 30,
        "strike": 100,
    }
    for invalid in (None, float("nan"), float("inf"), 0.0, -0.1, "bad"):
        frame = pd.DataFrame([{**base_row, "realized_volatility_estimate": invalid}])
        assert required_data_frame_covers_strategy_bounds(
            df=frame,
            option_types="put",
            require_realized_volatility=True,
        ) is False

    mixed = pd.DataFrame(
        [
            {**base_row, "realized_volatility_estimate": 0.24},
            {**base_row, "strike": 105, "realized_volatility_estimate": 0.0},
        ]
    )
    assert required_data_frame_covers_strategy_bounds(
        df=mixed,
        option_types="put",
        require_realized_volatility=True,
    ) is False
