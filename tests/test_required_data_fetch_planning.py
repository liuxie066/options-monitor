from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_sell_call_min_strike_builds_configured_bounds_plan(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29", "2026-06-26"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=2,
        want_put=False,
        want_call=True,
        sell_put_cfg={},
        sell_call_cfg={"enabled": True, "min_dte": 10, "max_dte": 60, "min_strike": 505},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(plan.side_plans) == 1
    call_plan = plan.side_plans[0]
    assert call_plan.option_type == "call"
    assert call_plan.strike_window.base_min_strike == 505.0
    assert call_plan.strike_window.min_strike == 505.0
    assert call_plan.strike_window.max_strike is not None
    assert call_plan.strike_window.max_strike > 505.0
    assert round(call_plan.strike_window.base_max_strike or 0.0, 2) == 606.00
    assert "near/far bounds" in call_plan.planning_reason


def test_sell_call_without_strikes_derives_bounds_from_spot(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=False,
        want_call=True,
        sell_put_cfg={},
        sell_call_cfg={"enabled": True},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    call_plan = plan.side_plans[0]
    assert round(call_plan.strike_window.base_min_strike or 0.0, 2) == 484.10
    assert round(call_plan.strike_window.base_max_strike or 0.0, 2) == 564.00
    assert call_plan.strike_window.max_strike is not None
    assert call_plan.strike_window.max_strike > (call_plan.strike_window.base_max_strike or 0.0)
    assert "derive sell_call near/far bounds from spot" in call_plan.planning_reason


def test_sell_call_min_strike_without_spot_still_binds_fetch_max(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: None)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=False,
        want_call=True,
        sell_put_cfg={},
        sell_call_cfg={"enabled": True, "min_strike": 505},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    call_plan = plan.side_plans[0]
    assert call_plan.strike_window.base_min_strike == 505.0
    assert call_plan.strike_window.base_max_strike is not None
    assert round(call_plan.strike_window.base_max_strike or 0.0, 2) == 606.00
    assert call_plan.strike_window.max_strike is not None


def test_sell_call_max_strike_only_keeps_configured_far_bound(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=False,
        want_call=True,
        sell_put_cfg={},
        sell_call_cfg={"enabled": True, "max_strike": 550},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    call_plan = plan.side_plans[0]
    assert call_plan.strike_window.base_min_strike is None
    assert round(call_plan.strike_window.base_max_strike or 0.0, 2) == 550.00
    assert round(call_plan.strike_window.max_strike or 0.0, 2) == 561.00
    assert "near/far bounds" in call_plan.planning_reason


def test_sell_call_without_strikes_uses_spot_20pct_max(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=False,
        want_call=True,
        sell_put_cfg={},
        sell_call_cfg={"enabled": True},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    call_plan = plan.side_plans[0]
    assert round(call_plan.strike_window.base_min_strike or 0.0, 2) == 484.10
    assert round(call_plan.strike_window.base_max_strike or 0.0, 2) == 564.00
    assert call_plan.strike_window.max_strike is not None
    assert call_plan.strike_window.max_strike > 564.00


def test_sell_put_max_strike_only_derives_far_bound_from_near_bound(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 10, "max_dte": 60, "max_strike": 460},
        sell_call_cfg={},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    put_plan = plan.side_plans[0]
    assert put_plan.option_type == "put"
    assert put_plan.strike_window.base_min_strike == 368.0
    assert put_plan.strike_window.min_strike == 368.0
    assert put_plan.strike_window.max_strike == 460.0
    assert "far bound from configured near bound" in put_plan.planning_reason


def test_sell_put_min_strike_only_keeps_direct_lower_bound(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 10, "max_dte": 60, "min_strike": 420},
        sell_call_cfg={},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    put_plan = plan.side_plans[0]
    assert round(put_plan.strike_window.base_min_strike or 0.0, 2) == 420.00
    assert round(put_plan.strike_window.min_strike or 0.0, 2) == 420.00
    assert put_plan.strike_window.max_strike is None


def test_put_and_call_same_expirations_merge_into_single_request(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29", "2026-06-26"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=1,
        want_put=True,
        want_call=True,
        sell_put_cfg={"enabled": True, "min_dte": 10, "max_dte": 60, "min_strike": 420, "max_strike": 460},
        sell_call_cfg={"enabled": True, "min_dte": 10, "max_dte": 60, "min_strike": 505},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(plan.merged_specs) == 1
    spec = plan.merged_specs[0]
    assert set(spec.option_types) == {"put", "call"}
    assert spec.side_strike_windows["put"]["max_strike"] == 460.0
    assert spec.side_strike_windows["call"]["min_strike"] == 505.0


def test_put_and_call_different_expirations_split_requests(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *args, **kwargs: ["2026-05-09", "2026-05-29", "2026-06-26", "2026-08-28"],
    )
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=2,
        want_put=True,
        want_call=True,
        sell_put_cfg={"enabled": True, "min_dte": 1, "max_dte": 30, "min_strike": 420, "max_strike": 460},
        sell_call_cfg={"enabled": True, "min_dte": 40, "max_dte": 120, "min_strike": 505},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(plan.merged_specs) == 2
    assert all(len(spec.option_types) == 1 for spec in plan.merged_specs)
