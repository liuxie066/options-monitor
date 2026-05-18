from __future__ import annotations

from datetime import date
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_sell_put_yield_enhancement_fetches_put_and_call_without_sell_call(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod
    import src.application.opend_utils as opend_utils

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-06-19", "2026-07-17"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 5, 15))

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=2,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 20, "max_dte": 60, "min_strike": 90, "max_strike": 96},
        sell_call_cfg={},
        yield_enhancement_cfg={
            "enabled": True,
            "min_dte": 20,
            "max_dte": 90,
            "call": {"min_strike": 108, "max_strike": 120},
        },
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert {side.option_type for side in plan.side_plans} == {"put", "call"}
    assert len(plan.merged_specs) == 1
    merged_spec = plan.merged_specs[0]
    assert tuple(merged_spec.option_types) == ("put", "call")
    put_spec = merged_spec
    call_spec = merged_spec
    assert put_spec.explicit_expirations == ["2026-06-19"]
    assert call_spec.explicit_expirations == ["2026-06-19"]

    put_plan = next(side for side in plan.side_plans if side.option_type == "put")
    call_plan = next(side for side in plan.side_plans if side.option_type == "call")
    assert call_plan.min_dte == 20
    assert call_plan.max_dte == 60
    assert "sell_put.max_dte" in call_plan.source_fields
    assert put_plan.strike_window.min_strike == 90.0
    assert put_plan.strike_window.max_strike == 96.0
    assert call_plan.strike_window.min_strike == 108.0
    assert call_plan.strike_window.base_max_strike == 120.0
    assert call_plan.strike_window.max_strike == 122.4


def test_sell_put_yield_enhancement_minimal_config_derives_call_fetch_window(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-06-19"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=1,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 20, "max_dte": 60, "min_strike": 90, "max_strike": 96},
        sell_call_cfg={},
        yield_enhancement_cfg={"enabled": True},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert {side.option_type for side in plan.side_plans} == {"put", "call"}
    assert len(plan.merged_specs) == 1
    assert tuple(plan.merged_specs[0].option_types) == ("put", "call")
    assert plan.merged_specs[0].side_strike_windows["call"] == {
        "min_strike": 103.0,
        "max_strike": 142.8,
    }

    call_plan = next(side for side in plan.side_plans if side.option_type == "call")
    assert call_plan.strike_window.source == "yield_enhancement.call.spot_derived_bounds"
    assert call_plan.strike_window.base_min_strike == 103.0
    assert call_plan.strike_window.base_max_strike == 140.0
    assert call_plan.strike_window.max_strike == 142.8


def test_sell_put_yield_enhancement_merges_with_existing_sell_call_bounds(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-06-19"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=1,
        want_put=True,
        want_call=True,
        sell_put_cfg={"enabled": True, "min_dte": 20, "max_dte": 60, "min_strike": 92, "max_strike": 96},
        sell_call_cfg={"enabled": True, "min_dte": 30, "max_dte": 45, "min_strike": 104, "max_strike": 118},
        yield_enhancement_cfg={
            "enabled": True,
            "call": {"min_strike": 108, "max_strike": 125},
        },
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    call_plan = next(side for side in plan.side_plans if side.option_type == "call")
    assert call_plan.min_dte == 20
    assert call_plan.max_dte == 60
    assert call_plan.strike_window.min_strike == 104.0
    assert call_plan.strike_window.base_max_strike == 125.0
    assert call_plan.strike_window.max_strike == 127.5
