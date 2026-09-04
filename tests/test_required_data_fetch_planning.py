from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest



@pytest.fixture(autouse=True)
def _freeze_planning_trading_date(monkeypatch) -> None:
    import src.application.opend_utils as opend_utils

    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 4, 30),
    )


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
            "cash_by_currency": {"CNY": 520000.0, "HKD": 18000.0},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 120000.0, "USD": 500.0}},
        },
        "sy": {
            "cash_by_currency": {"CNY": 910000.0},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 260000.0, "USD": 1000.0}},
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
        "lx": {
            "portfolio_source_name": "futu",
            "capacity_authority": {"status": "available"},
            "stocks_by_symbol": {"NVDA": {"avg_cost": 100}},
        },
        "sy": {
            "portfolio_source_name": "futu",
            "capacity_authority": {"status": "available"},
            "stocks_by_symbol": {"NVDA": {"avg_cost": 120}},
        },
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


def test_prefetch_symbol_plan_normalizes_physical_host_identity() -> None:
    from src.application.required_data_prefetch_planning import (
        build_prefetch_symbol_plan,
    )

    plan = build_prefetch_symbol_plan(
        [
            {
                "symbol": "NVDA",
                "fetch": {
                    "source": "futu",
                    "host": "OpenD.EXAMPLE",
                    "port": 11111,
                },
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
            {
                "symbol": "NVDA",
                "fetch": {
                    "source": "futu",
                    "host": "opend.example",
                    "port": 11111,
                },
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
        ]
    )

    assert plan.requested_count == 2
    assert plan.unique_count == 1
    assert plan.deduped_count == 1


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
            "lx": {
                "portfolio_source_name": "futu",
                "capacity_authority": {"status": "available"},
                "stocks_by_symbol": {"NVDA": {"avg_cost": 100}},
            },
            "sy": None,
        },
    )
    merged = build_prefetch_symbol_plan(union["symbols"]).symbol_cfgs[0]
    kwargs = merged["_prefetch_strategy_kwargs"]

    assert set(kwargs["option_types"].split(",")) == {"put", "call"}
    assert kwargs["side_strike_windows"]["put"]


def test_cross_account_prefetch_does_not_restore_symbols_removed_from_account_scope() -> None:
    from src.application.required_data_prefetch_planning import (
        build_cross_account_prefetch_config,
    )

    base_config = {
        "symbols": [
            {
                "symbol": "NVDA",
                "broker": "US",
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
            {
                "symbol": "PDD",
                "broker": "US",
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
        ]
    }
    scoped_config = {
        **base_config,
        "symbols": [base_config["symbols"][0]],
    }

    union = build_cross_account_prefetch_config(
        base_config=base_config,
        account_configs={"lx": scoped_config, "sy": scoped_config},
        prepared_portfolio_contexts={"lx": None, "sy": None},
    )

    assert {
        str(item.get("symbol") or "") for item in union["symbols"]
    } == {"NVDA"}


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

    def fail_expiration_provider(*args, **kwargs):
        pytest.fail("expiration provider accessed without a trading date")

    monkeypatch.setattr(mod, "list_option_expirations", fail_expiration_provider)
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda market: (_ for _ in ()).throw(RuntimeError("calendar unavailable")),
    )

    plan = mod.build_required_data_fetch_plan(
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

    assert plan.projection_outcome == "parse_error"
    assert plan.merged_specs == []
    assert plan.expiration_discovery is not None
    assert plan.expiration_discovery.reason_code == "request_identity_invalid"
    assert "calendar unavailable" in str(plan.expiration_discovery.error)


def test_tcom_put_fetch_window_fails_closed_without_opend_spot(monkeypatch, tmp_path: Path) -> None:
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

    assert plan.projection_outcome == "provider_error"
    assert plan.side_plans == []
    assert plan.merged_specs == []
    assert plan.spot_reference is None
    assert plan.spot_observation_complete is False
    assert plan.spot_observation_error == "OpenD spot unavailable for TCOM sell-put recall"


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
    assert "exact spot-based 20% cap" in call_plan.planning_reason


def test_wheel_call_fetch_uses_spot_floor_unbounded_max_and_rv(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *args, **kwargs: ["2026-05-29", "2026-06-12", "2026-06-26"],
    )
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=False,
        want_call=False,
        wheel_call_cfg={
            "enabled": True,
            "min_dte": 30,
            "max_dte": 45,
            "requires_realized_volatility": True,
        },
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(plan.side_plans) == 1
    assert plan.side_plans[0].min_dte == 30
    assert plan.side_plans[0].max_dte == 45
    assert plan.side_plans[0].strike_window.min_strike == 100.0
    assert plan.side_plans[0].strike_window.max_strike is None
    assert plan.require_realized_volatility is True
    assert plan.merged_specs[0].include_realized_volatility is True


def test_wheel_prefetch_demand_is_added_only_for_ready_active_batch() -> None:
    from src.application.required_data_prefetch_planning import (
        merge_wheel_requirements_into_prefetch_config,
    )

    config = {
        "wheel": {
            "enabled": True,
            "accounts": ["lx"],
            "min_dte": 30,
            "max_dte": 45,
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ],
    }
    merged = merge_wheel_requirements_into_prefetch_config(
        base_config=config,
        candidate_config={**config, "symbols": []},
        account_configs={"lx": config},
        wheel_read_models={
            "lx": {
                "batches": [
                    {
                        "symbol": "NVDA",
                        "lifecycle_status": "active",
                        "integrity_status": "trusted",
                        "phase": "ready",
                    },
                    {
                        "symbol": "AAPL",
                        "lifecycle_status": "ended",
                        "integrity_status": "trusted",
                        "phase": "ended",
                    },
                ]
            }
        },
    )

    assert [item["symbol"] for item in merged["symbols"]] == ["NVDA"]
    assert merged["symbols"][0]["_wheel_call"] == {
        "enabled": True,
        "min_dte": 30,
        "max_dte": 45,
        "requires_realized_volatility": True,
    }


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


def test_fetch_plan_does_not_reuse_stale_required_data_spot(monkeypatch, tmp_path: Path) -> None:
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

    assert plan.spot_reference is None
    assert plan.projection_outcome == "provider_error"
    assert plan.side_plans == []
    assert plan.merged_specs == []
    assert plan.spot_observation_error == "OpenD spot unavailable for NVDA sell-put recall"


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
        combo_yield_cfg={"enabled": True},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert plan.merged_specs[0].option_types == ("put", "call")
    assert plan.merged_specs[0].include_realized_volatility is True


def test_fetch_plan_rejects_unexpanded_template_strategy_config(monkeypatch, tmp_path: Path) -> None:
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: ["2026-05-29"])
    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 470.0)

    with pytest.raises(ValueError) as _caught:
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
    exc = _caught.value
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
    assert call_plan.strike_window.max_strike == call_plan.strike_window.base_max_strike
    assert "exact 20% bounds from spot reference" in call_plan.planning_reason


def test_sell_call_min_strike_without_spot_fails_closed(monkeypatch, tmp_path: Path) -> None:
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
    assert call_plan.strike_window.base_min_strike is None
    assert call_plan.strike_window.base_max_strike is None
    assert call_plan.strike_window.min_strike is None
    assert call_plan.strike_window.max_strike is None
    assert call_plan.strike_window.source == "sell_call.no_spot_no_bounds"


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
    assert round(call_plan.strike_window.max_strike or 0.0, 2) == 550.00
    assert "exact spot-based 20% cap" in call_plan.planning_reason


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
    assert call_plan.strike_window.max_strike == 564.00


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
    assert "min(configured max, OpenD spot)" in put_plan.planning_reason


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
    assert all(
        side_plan.required_exact_strikes_by_expiration == {}
        for side_plan in plan.side_plans
    )
    assert all(
        side_plan["required_exact_strikes_by_expiration"] == {}
        for side_plan in plan.to_debug_dict()["side_plans"]
    )


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


def test_required_data_plan_preserves_typed_success_empty_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "list_option_expirations", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 30),
    )

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="0700.HK",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
        sell_call_cfg={"enabled": False},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert plan.projection_outcome == "success_empty"
    assert plan.projected_expirations == []
    assert plan.expiration_discovery is not None
    assert plan.expiration_discovery.outcome == "success_empty"
    assert plan.expiration_discovery.reason_code == "no_expirations"
    assert plan.expiration_discovery.observed_at_utc
    assert plan.expiration_discovery.request_identity["trading_date"] == "2026-07-30"


@pytest.mark.parametrize(
    ("discovery_value", "expected_outcome"),
    [
        (RuntimeError("provider unavailable"), "provider_error"),
        ({"unexpected": "mapping"}, "parse_error"),
    ],
)
def test_required_data_plan_preserves_discovery_failure_type(
    monkeypatch,
    tmp_path: Path,
    discovery_value,
    expected_outcome: str,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    def discover(*args, **kwargs):
        if isinstance(discovery_value, Exception):
            raise discovery_value
        return discovery_value

    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(mod, "list_option_expirations", discover)
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 30),
    )

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
        sell_call_cfg={"enabled": False},
    )

    assert plan.projection_outcome == expected_outcome
    assert plan.expiration_discovery is not None
    assert plan.expiration_discovery.outcome == expected_outcome
    assert plan.expiration_discovery_complete is False


def test_required_data_plan_fails_closed_when_discovery_rows_project_to_no_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *args, **kwargs: ["2027-12-17"],
    )
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 30),
    )

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
        sell_call_cfg={"enabled": False},
    )

    assert plan.expiration_discovery is not None
    assert plan.expiration_discovery.outcome == "success_rows"
    assert plan.projected_expirations == []
    assert plan.projection_outcome == "projection_empty"


def test_required_data_plan_memoizes_discovery_by_physical_binding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    calls: list[str] = []
    asof_dates: list[str] = []
    trading_date_calls = 0

    def discover(symbol: str, **kwargs):
        calls.append(symbol)
        asof_dates.append(kwargs["asof_date"])
        return ["2026-08-21"]

    def resolve_trading_date(_market):
        nonlocal trading_date_calls
        trading_date_calls += 1
        if trading_date_calls > 1:
            pytest.fail("cached discovery re-read the trading date")
        return date(2026, 7, 30)

    monkeypatch.setattr(mod, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(mod, "list_option_expirations", discover)
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        resolve_trading_date,
    )
    cache = {}
    plans = [
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=0,
            want_put=True,
            want_call=False,
            sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
            sell_call_cfg={"enabled": False},
            fetch_source="futu",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            expiration_discovery_cache=cache,
        )
        for _account in ("lx", "sy")
    ]

    assert calls == ["NVDA"]
    assert asof_dates == ["2026-07-30"]
    assert trading_date_calls == 1
    assert plans[0].expiration_discovery is plans[1].expiration_discovery
    assert plans[0].projected_expirations == ["2026-08-21"]
    assert plans[0].merged_specs[0].trading_date == "2026-07-30"
    assert plans[1].merged_specs[0].trading_date == "2026-07-30"


def test_required_data_plan_memoizes_missing_spot_by_binding_and_date(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    spot_calls: list[str] = []

    def observe_spot(symbol: str, **_kwargs):
        spot_calls.append(symbol)
        return None

    monkeypatch.setattr(mod, "get_underlier_spot", observe_spot)
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 30),
    )
    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *_args, **_kwargs: ["2026-08-21"],
    )
    expiration_cache: dict[tuple[object, ...], object] = {}
    spot_cache: dict[tuple[object, ...], float | None] = {}

    plans = [
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=0,
            want_put=True,
            want_call=False,
            sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
            sell_call_cfg={"enabled": False},
            fetch_source="futu",
            fetch_host=fetch_host,
            fetch_port=11111,
            expiration_discovery_cache=expiration_cache,
            spot_observation_cache=spot_cache,
        )
        for fetch_host in ("OpenD.EXAMPLE", "opend.example")
    ]

    assert spot_calls == ["NVDA"]
    assert list(spot_cache) == [
        ("NVDA", "futu", "opend.example", 11111, "2026-07-30")
    ]
    assert list(spot_cache.values()) == [None]
    assert all(plan.spot_reference is None for plan in plans)
    assert all(plan.spot_observation_complete is False for plan in plans)
    assert all(plan.projection_outcome == "provider_error" for plan in plans)
    assert all(
        plan.spot_observation_error == "OpenD spot unavailable for NVDA sell-put recall"
        for plan in plans
    )


def test_required_data_plan_consumes_typed_unavailable_prefill_without_io(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod
    from src.application.opening_quote_evidence import (
        OPENING_UNDERLIER_OBSERVATION_SCHEMA,
        OpeningUnderlierObservation,
    )

    identity = mod.freeze_required_data_planning_identity(
        base=tmp_path,
        symbol="NVDA",
        source="futu",
        host="OpenD.EXAMPLE",
        port=11111,
    )
    unavailable = OpeningUnderlierObservation(
        schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA,
        code="US.NVDA",
        market="US",
        last_price=None,
        update_time=None,
        observed_at_utc=None,
        age_seconds=None,
        market_state=None,
        sec_status=None,
        suspension=None,
        status="data_unavailable",
        reason_code="underlier_identity_mismatch",
    )
    spot_cache = {identity.cache_key: unavailable}

    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: pytest.fail("planning identity re-read the clock"),
    )
    monkeypatch.setattr(
        mod,
        "get_underlier_spot",
        lambda *_args, **_kwargs: pytest.fail("cached observation refetched spot"),
    )
    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *_args, **_kwargs: ["2026-05-15"],
    )

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
        sell_call_cfg={"enabled": False},
        fetch_source="futu",
        fetch_host="OpenD.EXAMPLE",
        fetch_port=11111,
        spot_observation_cache=spot_cache,
        planning_identity=identity,
    )

    assert plan.underlier_observation is unavailable
    assert plan.spot_observation_complete is False
    assert plan.projection_outcome == "provider_error"


def test_required_data_plan_rejects_cached_discovery_date_drift_without_io(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )

    def fail_io(*args, **kwargs):
        pytest.fail("invalid cached discovery triggered external or clock I/O")

    monkeypatch.setattr(opend_utils, "get_trading_date", fail_io)
    monkeypatch.setattr(mod, "get_underlier_spot", fail_io)
    monkeypatch.setattr(mod, "list_option_expirations", fail_io)
    cache = {
        (
            "NVDA",
            "futu",
            "127.0.0.1",
            11111,
            "2026-07-30",
        ): OptionExpirationDiscoveryResult(
            outcome="success_rows",
            reason_code=None,
            expirations=["2026-08-21"],
            observed_at_utc="2026-07-30T01:00:00Z",
            completed_at_utc="2026-07-30T01:00:01Z",
            request_identity={
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": "2026-07-31",
            },
        )
    }

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
        sell_call_cfg={"enabled": False},
        fetch_source="futu",
        fetch_host="127.0.0.1",
        fetch_port=11111,
        expiration_discovery_cache=cache,
    )

    assert plan.projection_outcome == "parse_error"
    assert plan.merged_specs == []
    assert plan.expiration_discovery is not None
    assert plan.expiration_discovery.reason_code == (
        "expiration_discovery_cache_identity_invalid"
    )


def _patch_position_plan_sources(
    monkeypatch,
    *,
    spot_reference: float | None = 110.0,
) -> None:
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as mod

    expirations = ["2026-08-07", "2026-08-21", "2026-09-18"]
    monkeypatch.setattr(
        mod,
        "list_option_expirations",
        lambda *args, **kwargs: expirations,
    )
    monkeypatch.setattr(
        mod,
        "get_underlier_spot",
        lambda *args, **kwargs: spot_reference,
    )
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 27),
    )


def test_position_requirements_preserve_sorted_expiry_local_exact_strikes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    _patch_position_plan_sources(monkeypatch)
    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=False,
        want_call=False,
        sell_put_cfg={"enabled": False},
        sell_call_cfg={"enabled": False},
        position_requirements=[
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": "105",
            },
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": "100",
            },
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 90,
            },
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 100.0,
            },
        ],
    )

    assert len(plan.side_plans) == 1
    side_plan = plan.side_plans[0]
    assert side_plan.min_dte == 25
    assert side_plan.max_dte == 53
    assert side_plan.strike_window.min_strike == 90.0
    assert side_plan.strike_window.max_strike == 105.0
    assert side_plan.required_exact_strikes_by_expiration == {
        "2026-08-21": [90.0, 100.0],
        "2026-09-18": [105.0],
    }
    assert side_plan.to_debug_dict()[
        "required_exact_strikes_by_expiration"
    ] == {
        "2026-08-21": [90.0, 100.0],
        "2026-09-18": [105.0],
    }


def test_strategy_merge_retains_interior_position_exact_strike(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    _patch_position_plan_sources(monkeypatch)
    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={
            "enabled": True,
            "min_dte": 7,
            "max_dte": 45,
            "min_strike": 80,
            "max_strike": 120,
        },
        sell_call_cfg={"enabled": False},
        position_requirements=[
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": "100",
            }
        ],
    )

    assert len(plan.side_plans) == 1
    side_plan = plan.side_plans[0]
    assert side_plan.strike_window.base_min_strike == 88.0
    assert side_plan.strike_window.base_max_strike == 110.0
    assert side_plan.required_exact_strikes_by_expiration == {
        "2026-08-21": [100.0]
    }
    assert plan.merged_specs[0].side_plans[0] is side_plan


def test_call_max_only_strategy_stays_lower_unbounded_when_merged_with_position(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    _patch_position_plan_sources(monkeypatch, spot_reference=None)
    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=False,
        want_call=True,
        sell_put_cfg={"enabled": False},
        sell_call_cfg={
            "enabled": True,
            "min_dte": 7,
            "max_dte": 45,
            "max_strike": 100,
        },
        position_requirements=[
            {
                "planning_status": "ready",
                "option_type": "call",
                "expiration": "2026-08-21",
                "strike": 120,
            }
        ],
    )

    side_plan = plan.side_plans[0]
    assert side_plan.option_type == "call"
    assert side_plan.strike_window.min_strike is None
    assert side_plan.strike_window.max_strike is None
    assert side_plan.strike_window.base_min_strike is None
    assert side_plan.strike_window.base_max_strike is None
    assert side_plan.required_exact_strikes_by_expiration == {
        "2026-08-21": [120.0]
    }


def test_put_min_only_strategy_uses_spot_upper_bound_when_merged_with_position(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    _patch_position_plan_sources(monkeypatch, spot_reference=110.0)
    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={
            "enabled": True,
            "min_dte": 7,
            "max_dte": 45,
            "min_strike": 100,
        },
        sell_call_cfg={"enabled": False},
        position_requirements=[
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 80,
            }
        ],
    )

    side_plan = plan.side_plans[0]
    assert side_plan.option_type == "put"
    assert side_plan.strike_window.min_strike == 80.0
    assert side_plan.strike_window.max_strike == 110.0
    assert side_plan.strike_window.base_min_strike == 100.0
    assert side_plan.strike_window.base_max_strike == 110.0
    assert side_plan.required_exact_strikes_by_expiration == {
        "2026-08-21": [80.0]
    }


def test_position_expiration_outside_strategy_dte_expands_plan_and_is_coverable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pandas as pd

    import src.application.required_data_planning as mod
    from src.application.required_data_coverage import (
        required_data_frame_covers_fetch_plan,
        required_data_frame_covers_fetch_plan_debug,
    )
    from src.application.required_data_plan_identity import (
        build_required_data_expected_fetch_contract,
    )

    _patch_position_plan_sources(monkeypatch, spot_reference=110.0)
    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=True,
        want_call=False,
        sell_put_cfg={
            "enabled": True,
            "min_dte": 7,
            "max_dte": 20,
            "min_strike": 80,
            "max_strike": 100,
        },
        sell_call_cfg={"enabled": False},
        position_requirements=[
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-09-18",
                "strike": 120,
            }
        ],
    )

    side_plan = plan.side_plans[0]
    assert side_plan.explicit_expirations == [
        "2026-08-07",
        "2026-09-18",
    ]
    assert side_plan.min_dte == 7
    assert side_plan.max_dte == 53
    assert plan.merged_specs[0].min_dte == 7
    assert plan.merged_specs[0].max_dte == 53
    rows = pd.DataFrame(
        [
            {
                "option_type": "put",
                "expiration": expiration,
                "dte": dte,
                "strike": strike,
                "spot": 110,
                    "term_matched_rv": 0.24,
                    "term_matched_rv_status": "ok",
                    "term_matched_rv_reason": None,
            }
            for expiration, dte, strikes in (
                ("2026-08-07", 11, (80, 100)),
                ("2026-09-18", 53, (80, 100, 120)),
            )
            for strike in strikes
        ]
    )
    contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=plan.to_debug_dict(),
        source="opend",
        host="127.0.0.1",
        port=11111,
    )

    assert required_data_frame_covers_fetch_plan(
        df=rows,
        fetch_plan=plan,
    )
    assert required_data_frame_covers_fetch_plan_debug(
        rows,
        contract["fetch_plan"],
    )


def test_successful_discovery_without_trading_date_rejects_position_dte(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )

    _patch_position_plan_sources(monkeypatch)
    monkeypatch.setattr(
        mod,
        "discover_option_expirations",
        lambda *args, **kwargs: OptionExpirationDiscoveryResult(
            outcome="success_rows",
            reason_code=None,
            expirations=["2026-08-21"],
            observed_at_utc="2026-07-27T01:00:00Z",
            completed_at_utc="2026-07-27T01:00:01Z",
            request_identity={
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": None,
            },
        ),
    )

    with pytest.raises(mod.RequiredDataPlanningError) as exc_info:
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=0,
            want_put=False,
            want_call=False,
            sell_put_cfg={"enabled": False},
            sell_call_cfg={"enabled": False},
            position_requirements=[
                {
                    "planning_status": "ready",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": 100,
                }
            ],
        )

    assert exc_info.value.reason_code == "position_dte_unavailable"


def test_position_expiration_before_trading_date_is_rejected(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    _patch_position_plan_sources(monkeypatch)
    with pytest.raises(mod.RequiredDataPlanningError) as exc_info:
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=0,
            want_put=False,
            want_call=False,
            sell_put_cfg={"enabled": False},
            sell_call_cfg={"enabled": False},
            position_requirements=[
                {
                    "planning_status": "ready",
                    "option_type": "put",
                    "expiration": "2026-07-20",
                    "strike": 100,
                }
            ],
        )

    assert exc_info.value.reason_code == (
        "position_expiration_before_trading_date"
    )


def test_failed_discovery_without_trading_date_preserves_failure_outcome(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )

    _patch_position_plan_sources(monkeypatch)
    monkeypatch.setattr(
        mod,
        "discover_option_expirations",
        lambda *args, **kwargs: OptionExpirationDiscoveryResult(
            outcome="provider_error",
            reason_code="expiration_discovery_failed",
            expirations=[],
            observed_at_utc=None,
            completed_at_utc="2026-07-27T01:00:01Z",
            request_identity={
                "symbol": "NVDA",
                "underlier": None,
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": None,
            },
            error="provider unavailable",
        ),
    )

    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=False,
        want_call=False,
        sell_put_cfg={"enabled": False},
        sell_call_cfg={"enabled": False},
        position_requirements=[
            {
                "planning_status": "ready",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 100,
            }
        ],
    )

    assert plan.projection_outcome == "provider_error"
    assert plan.side_plans[0].min_dte is None
    assert plan.side_plans[0].max_dte is None


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("option_type", "PUT"),
        ("option_type", ""),
        ("expiration", "2026-8-21"),
        ("expiration", "2026-08-21 "),
        ("strike", True),
        ("strike", 0),
        ("strike", float("nan")),
        ("strike", float("inf")),
        ("strike", "bad"),
    ],
)
def test_malformed_ready_position_requirement_fails_before_provider_access(
    monkeypatch,
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
) -> None:
    import src.application.required_data_planning as mod

    def fail_provider_access(*args, **kwargs):
        pytest.fail("provider/cache access occurred before planning validation")

    monkeypatch.setattr(mod, "get_underlier_spot", fail_provider_access)
    monkeypatch.setattr(
        mod,
        "discover_option_expirations",
        fail_provider_access,
    )
    malformed = {
        "planning_status": "ready",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": "100",
    }
    malformed[field_name] = invalid_value

    with pytest.raises(mod.RequiredDataPlanningError) as exc_info:
        mod.build_required_data_fetch_plan(
            base=tmp_path,
            required_data_dir=tmp_path,
            symbol="NVDA",
            limit_expirations=0,
            want_put=False,
            want_call=False,
            sell_put_cfg={"enabled": False},
            sell_call_cfg={"enabled": False},
            position_requirements=[
                {
                    "planning_status": "ready",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": 95,
                },
                malformed,
            ],
        )

    assert exc_info.value.reason_code == (
        "invalid_ready_position_requirement"
    )
    assert exc_info.value.requirement_index == 1
    assert exc_info.value.field_name == field_name


def test_explicit_non_ready_position_requirement_remains_excluded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as mod

    _patch_position_plan_sources(monkeypatch)
    plan = mod.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol="NVDA",
        limit_expirations=0,
        want_put=False,
        want_call=False,
        sell_put_cfg={"enabled": False},
        sell_call_cfg={"enabled": False},
        position_requirements=[
            {
                "planning_status": "unavailable",
                "option_type": "unsupported",
                "expiration": "not-a-date",
                "strike": True,
            }
        ],
    )

    assert plan.side_plans == []
