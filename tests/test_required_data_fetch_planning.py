from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_tcom_put_fetch_window_is_account_cash_invariant(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod
    import src.application.opend_utils as opend_utils
    from src.application.prefilters import apply_prefilters

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21", "2026-09-18"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 43.07)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 7, 22))

    sell_put = {"enabled": True, "min_dte": 7, "max_dte": 60, "max_strike": 45.0}
    account_contexts = {
        "lx": {
            "cash_by_currency": {"HKD": 666787.5, "USD": 10177.48},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 386500.0, "USD": 8000.0}},
        },
        "sy": {
            "cash_by_currency": {"HKD": 1104646.19},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 213000.0, "USD": 8500.0}},
        },
    }

    resolved: dict[str, dict[str, object]] = {}
    for account, portfolio_ctx in account_contexts.items():
        prefilters = apply_prefilters(
            symbol="TCOM",
            sp=dict(sell_put),
            cc={"enabled": False},
            want_put=True,
            want_call=False,
            portfolio_ctx=portfolio_ctx,
        )
        plan = mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path / "required_data",
            symbol="TCOM",
            limit_expirations=10,
            want_put=prefilters.want_put,
            want_call=False,
            sell_put_cfg=prefilters.sp,
            sell_call_cfg={"enabled": False},
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
        put_plan = next(item for item in plan.side_plans if item.option_type == "put")
        resolved[account] = put_plan.to_debug_dict()

    assert resolved["lx"] == resolved["sy"]
    assert resolved["lx"]["min_strike"] == 34.456
    assert resolved["lx"]["max_strike"] == 43.07
    assert resolved["lx"]["explicit_expirations"] == ["2026-08-21", "2026-09-18"]


def test_cross_account_prefetch_union_is_order_independent_and_covers_call_costs() -> None:
    from src.application.required_data_prefetch_planning import (
        build_cross_account_prefetch_config,
        build_prefetch_symbol_plan,
    )

    config = {
        "symbols": [
            {
                "symbol": "NVDA",
                "broker": "US",
                "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 45},
                "sell_call": {
                    "enabled": True,
                    "min_dte": 7,
                    "max_dte": 45,
                    "min_strike_cost_multiplier": 1.1,
                },
            }
        ]
    }
    contexts = {
        "lx": {"stocks_by_symbol": {"NVDA": {"avg_cost": 100}}},
        "sy": {"stocks_by_symbol": {"NVDA": {"avg_cost": 120}}},
    }

    forward = build_cross_account_prefetch_config(
        base_config=config,
        account_configs={"lx": config, "sy": config},
        prepared_portfolio_contexts=contexts,
    )
    reverse = build_cross_account_prefetch_config(
        base_config=config,
        account_configs={"sy": config, "lx": config},
        prepared_portfolio_contexts={"sy": contexts["sy"], "lx": contexts["lx"]},
    )

    assert forward == reverse
    merged = build_prefetch_symbol_plan(forward["symbols"]).symbol_cfgs[0]
    call_window = merged["_prefetch_strategy_kwargs"]["side_strike_windows"]["call"]
    assert call_window["min_strike"] == pytest.approx(110.0)
    assert call_window["max_strike"] == pytest.approx(161.568)
    assert merged["_prefetch_strategy_kwargs"]["side_strike_windows"]["put"]


def test_cross_account_prefetch_keeps_put_when_one_context_is_unavailable() -> None:
    from src.application.required_data_prefetch_planning import (
        build_cross_account_prefetch_config,
        build_prefetch_symbol_plan,
    )

    config = {
        "symbols": [
            {
                "symbol": "NVDA",
                "broker": "US",
                "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 45},
                "sell_call": {
                    "enabled": True,
                    "min_dte": 7,
                    "max_dte": 45,
                },
            }
        ]
    }
    union = build_cross_account_prefetch_config(
        base_config=config,
        account_configs={"lx": config, "sy": config},
        prepared_portfolio_contexts={
            "lx": {"stocks_by_symbol": {"NVDA": {"avg_cost": 100}}},
            "sy": None,
        },
    )
    merged = build_prefetch_symbol_plan(union["symbols"]).symbol_cfgs[0]
    kwargs = merged["_prefetch_strategy_kwargs"]

    assert set(kwargs["option_types"].split(",")) == {"put", "call"}
    assert kwargs["side_strike_windows"]["put"]


def test_exact_dte_window_preserves_empty_expiration_set() -> None:
    from src.application.opend_symbol_chain_fetching import select_symbol_expirations

    assert select_symbol_expirations(
        expirations_all=["2026-12-18", "2027-01-15"],
        explicit_expirations_norm=[],
        limit_expirations=0,
        min_dte=7,
        max_dte=45,
        today=date(2026, 7, 28),
    ) == []


def test_required_data_plan_fails_closed_when_trading_date_cannot_be_resolved(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21"])
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda market: (_ for _ in ()).throw(RuntimeError("calendar unavailable")),
    )

    with pytest.raises(RuntimeError, match="failed to resolve trading date for NVDA"):
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=0,
            want_put=True,
            want_call=False,
            sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
            sell_call_cfg={"enabled": False},
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )


def test_tcom_put_fetch_window_falls_back_to_configured_max_without_spot(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod
    import src.application.opend_utils as opend_utils

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21", "2026-09-18"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: None)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 7, 22))

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path / "required_data",
        symbol="TCOM",
        limit_expirations=10,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 60, "max_strike": 45.0},
        sell_call_cfg={"enabled": False},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    put_plan = next(item for item in plan.side_plans if item.option_type == "put")
    assert put_plan.strike_window.min_strike == 36.0
    assert put_plan.strike_window.max_strike == 45.0


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
    assert plan.merged_specs[0].include_realized_volatility is True


def test_fetch_plan_prefers_live_spot_over_existing_required_data(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    required_data_dir = tmp_path / "required_data"
    parsed = required_data_dir / "parsed" / "NVDA_required_data.csv"
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text("option_type,expiration,dte,strike,spot\nput,2026-06-19,30,80,80.15\n", encoding="utf-8")
    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-06-19"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 79.8)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=required_data_dir,
        symbol="NVDA",
        limit_expirations=1,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "max_strike": 80},
        sell_call_cfg={"enabled": False},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert plan.spot_reference == 79.8
    assert plan.side_plans[0].strike_window.max_strike == 79.8


def test_fetch_plan_falls_back_to_existing_spot_when_live_spot_missing(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    required_data_dir = tmp_path / "required_data"
    parsed = required_data_dir / "parsed" / "NVDA_required_data.csv"
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text("option_type,expiration,dte,strike,spot\nput,2026-06-19,30,80,80.15\n", encoding="utf-8")
    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-06-19"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: None)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=required_data_dir,
        symbol="NVDA",
        limit_expirations=1,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "max_strike": 80},
        sell_call_cfg={"enabled": False},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert plan.spot_reference == 80.15
    assert plan.side_plans[0].strike_window.max_strike == 80.0


def test_sell_put_underwriting_fetch_plan_requires_realized_volatility(monkeypatch, tmp_path: Path) -> None:
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
        sell_put_cfg={"enabled": True, "strategy": "insurance_underwriting"},
        sell_call_cfg={"enabled": False},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(plan.merged_specs) == 1
    assert plan.merged_specs[0].option_types == ("put",)
    assert plan.merged_specs[0].include_realized_volatility is True


def test_combo_only_fetch_plan_requires_funding_put_realized_volatility(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=0,
        want_put=False,
        want_call=False,
        sell_put_cfg={"enabled": False},
        sell_call_cfg={"enabled": False},
        yield_enhancement_cfg={"enabled": True},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert plan.merged_specs[0].option_types == ("put", "call")
    assert plan.merged_specs[0].include_realized_volatility is True


def test_fetch_plan_rejects_unexpanded_template_strategy_config(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    try:
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=1,
            want_put=True,
            want_call=False,
            sell_put_cfg={"enabled": True},
            sell_call_cfg={"enabled": False},
            symbol_cfg={
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True},
            },
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
        raise AssertionError("expected unresolved strategy config failure")
    except ValueError as exc:
        assert "apply templates/profiles" in str(exc)


def test_sell_call_underwriting_fetch_plan_requires_realized_volatility(monkeypatch, tmp_path: Path) -> None:
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
        sell_put_cfg={"enabled": False},
        sell_call_cfg={"enabled": True, "strategy": "insurance_underwriting", "min_dte": 10, "max_dte": 60},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(plan.merged_specs) == 1
    assert plan.merged_specs[0].option_types == ("call",)
    assert plan.merged_specs[0].include_realized_volatility is True


def test_fetch_plan_forwards_opend_discovery_rate_limits(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    spot_calls: list[dict[str, object]] = []
    expiration_calls: list[dict[str, object]] = []

    def _get_underlier_spot(*args, **kwargs):  # type: ignore[no-untyped-def]
        spot_calls.append(dict(kwargs))
        return 470.0

    def _list_option_expirations(*args, **kwargs):  # type: ignore[no-untyped-def]
        expiration_calls.append(dict(kwargs))
        return ["2026-05-29"]

    monkeypatch.setattr(mod, "get_underlier_spot", _get_underlier_spot)
    monkeypatch.setattr(mod, "list_option_expirations", _list_option_expirations)

    mod.build_required_data_fetch_plan(
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
        snapshot_max_wait_sec=21,
        snapshot_window_sec=22,
        snapshot_max_calls=23,
        expiration_max_wait_sec=31,
        expiration_window_sec=32,
        expiration_max_calls=33,
    )

    assert spot_calls[0]["snapshot_max_wait_sec"] == 21
    assert spot_calls[0]["snapshot_window_sec"] == 22
    assert spot_calls[0]["snapshot_max_calls"] == 23
    assert expiration_calls[0]["expiration_max_wait_sec"] == 31
    assert expiration_calls[0]["expiration_window_sec"] == 32
    assert expiration_calls[0]["expiration_max_calls"] == 33


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
    assert round(call_plan.strike_window.base_min_strike or 0.0, 2) == 470.00
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
    assert round(call_plan.strike_window.base_min_strike or 0.0, 2) == 470.00
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
    assert round(call_plan.strike_window.base_min_strike or 0.0, 2) == 470.00
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
    assert round(put_plan.strike_window.max_strike or 0.0, 2) == 470.00


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
    import src.application.opend_utils as opend_utils

    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *args, **kwargs: ["2026-05-09", "2026-05-29", "2026-06-26", "2026-08-28"],
    )
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda _market: date(2026, 4, 30))

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
