from __future__ import annotations

from datetime import date
from pathlib import Path


def _stub(monkeypatch, expirations, trading_date=date(2026, 5, 15)):
    """Pin the discovery/spot reads; ``trading_date=None`` leaves the clock alone."""
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: expirations)
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    if trading_date is not None:
        monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: trading_date)
    return mod


def _plan(tmp_path: Path, **overrides):
    """``build_required_data_fetch_plan`` with the kwargs these cases share."""
    import src.application.required_data_planning as mod

    kwargs = dict(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=1,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "strategy": "insurance_underwriting", "min_dte": 20, "max_dte": 60, "min_strike": 90, "max_strike": 96},
        sell_call_cfg={},
        combo_yield_cfg={"enabled": True},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )
    kwargs.update(overrides)
    return mod.build_required_data_fetch_plan(**kwargs)


def _side(plan, option_type: str):
    return next(side for side in plan.side_plans if side.option_type == option_type)


def test_sell_put_combo_yield_fetches_put_and_call_without_sell_call(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, ["2026-06-19", "2026-07-17"])

    plan = _plan(
        tmp_path,
        limit_expirations=2,
        combo_yield_cfg={"enabled": True, "min_dte": 20, "max_dte": 90, "call": {"min_strike": 108, "max_strike": 120}},
    )

    assert {side.option_type for side in plan.side_plans} == {"put", "call"}
    assert len(plan.merged_specs) == 1
    merged_spec = plan.merged_specs[0]
    assert tuple(merged_spec.option_types) == ("put", "call")
    assert merged_spec.include_realized_volatility is True
    assert merged_spec.explicit_expirations == ["2026-06-19"]

    put_plan = _side(plan, "put")
    call_plan = _side(plan, "call")
    assert call_plan.min_dte == 20
    assert call_plan.max_dte == 60
    assert "sell_put.max_dte" in call_plan.source_fields
    assert put_plan.strike_window.min_strike == 90.0
    assert put_plan.strike_window.max_strike == 96.0
    assert call_plan.strike_window.min_strike == 108.0
    assert call_plan.strike_window.base_max_strike == 120.0
    assert call_plan.strike_window.max_strike == 122.4


def test_sell_put_combo_yield_minimal_config_derives_call_fetch_window(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, ["2026-06-19"])

    plan = _plan(tmp_path)

    assert {side.option_type for side in plan.side_plans} == {"put", "call"}
    assert len(plan.merged_specs) == 1
    assert tuple(plan.merged_specs[0].option_types) == ("put", "call")
    assert plan.merged_specs[0].side_strike_windows["call"] == {
        "min_strike": 100.0,
        "max_strike": 142.8,
    }

    call_plan = _side(plan, "call")
    assert call_plan.strike_window.source == "combo_yield.call.spot_derived_bounds"
    assert call_plan.strike_window.base_min_strike == 100.0
    assert call_plan.strike_window.base_max_strike == 140.0
    assert call_plan.strike_window.max_strike == 142.8


def test_combo_yield_fetch_plan_declares_put_and_call_without_sell_put(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, ["2026-06-19"])

    plan = _plan(
        tmp_path,
        want_put=False,
        sell_put_cfg={
            "enabled": False,
            "strategy": "insurance_underwriting",
            "min_dte": 20,
            "max_dte": 60,
            "min_strike": 90,
            "max_strike": 96,
        },
    )

    assert {side.option_type for side in plan.side_plans} == {"put", "call"}
    assert len(plan.merged_specs) == 1
    assert tuple(plan.merged_specs[0].option_types) == ("put", "call")
    put_plan = _side(plan, "put")
    call_plan = _side(plan, "call")
    assert put_plan.strike_window.max_strike == 96.0
    assert call_plan.strike_window.source == "combo_yield.call.spot_derived_bounds"


def test_sell_put_combo_yield_merges_with_existing_sell_call_bounds(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, ["2026-06-19"], trading_date=None)

    plan = _plan(
        tmp_path,
        want_call=True,
        sell_put_cfg={
            "enabled": True,
            "strategy": "insurance_underwriting",
            "min_dte": 20,
            "max_dte": 60,
            "min_strike": 92,
            "max_strike": 96,
        },
        sell_call_cfg={"enabled": True, "min_dte": 30, "max_dte": 45, "min_strike": 104, "max_strike": 118},
        combo_yield_cfg={
            "enabled": True,
            "call": {"min_strike": 108, "max_strike": 125},
        },
    )

    call_plan = _side(plan, "call")
    assert call_plan.min_dte == 20
    assert call_plan.max_dte == 60
    assert call_plan.strike_window.min_strike == 104.0
    assert call_plan.strike_window.base_max_strike == 125.0
    assert call_plan.strike_window.max_strike == 127.5


def test_strategy_expiration_plan_is_not_truncated_by_legacy_limit(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, ["2026-06-19", "2026-07-17", "2026-08-21"], trading_date=date(2026, 6, 1))

    plan = _plan(
        tmp_path,
        sell_put_cfg={
            "enabled": True,
            "min_dte": 1,
            "max_dte": 90,
            "max_strike": 100,
        },
        sell_call_cfg=None,
        combo_yield_cfg=None,
    )

    put_plan = _side(plan, "put")
    assert put_plan.explicit_expirations == ["2026-06-19", "2026-07-17", "2026-08-21"]
    assert plan.merged_specs[0].limit_expirations == 0
