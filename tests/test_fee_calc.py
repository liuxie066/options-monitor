from __future__ import annotations

import pytest

from datetime import datetime, timezone

import pandas as pd

from domain.domain.fee_calc import (
    FUTU_HK_FEE_SCHEDULE_URL,
    FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
    calc_futu_hk_terminal_fee,
)


def _formal_metric_row(*, mode: str, currency: str, **overrides):  # type: ignore[no-untyped-def]
    market = "HK" if currency == "HKD" else "US"
    owner = "HK.00700" if market == "HK" else "US.NVDA"
    row = {
        "symbol": "0700.HK" if market == "HK" else "NVDA",
        "market": market,
        "option_type": mode,
        "expiration": "2026-06-19",
        "contract_symbol": f"{owner}-{mode}",
        "currency": currency,
        "dte": 21,
        "strike": 100.0,
        "spot": 110.0,
        "bid": 1.0,
        "ask": 1.2,
        "price_tick": 0.01,
        "implied_volatility": 0.30,
        "term_matched_rv": 0.20,
        "term_matched_rv_status": "ok",
        "underlier_observation_status": "ready",
        "option_standard_type": "STANDARD",
        "stock_owner": owner,
        "stock_type": "DRVT",
        "chain_multiplier": 100,
        "snapshot_multiplier": 100,
        "multiplier": 100,
        "opening_contract_status": "ready",
        "snapshot_received_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    row.update(overrides)
    return row


def test_calc_futu_hk_option_fee_applies_minimum_commission_and_system_fee() -> None:
    from domain.domain.fee_calc import calc_futu_hk_option_fee

    # 交易金额 1 * 100 * 1 = 100，佣金 0.2 = 最低 3.0，另加平台费 15 和系统费 3
    out = calc_futu_hk_option_fee(1.0, contracts=1, multiplier=100, is_sell=True)

    assert out == 21.0


def test_calc_futu_hk_option_fee_scales_system_fee_by_contracts() -> None:
    from domain.domain.fee_calc import calc_futu_hk_option_fee

    # 交易金额 2 * 100 * 3 = 600，佣金 1.2 -> 最低 3.0，平台费 15，系统费 9
    out = calc_futu_hk_option_fee(2.0, contracts=3, multiplier=100, is_sell=False)

    assert out == 27.0


def test_calc_futu_hk_option_fee_waives_tariff_at_exact_one_cent() -> None:
    from domain.domain.fee_calc import calc_futu_hk_option_fee

    out = calc_futu_hk_option_fee(0.01, contracts=3, multiplier=100, is_sell=True)

    assert out == 18.0


def test_calc_futu_us_option_fee_uses_standard_commission_and_sell_regulatory_fees() -> None:
    from domain.domain.fee_calc import calc_futu_us_option_fee

    out = calc_futu_us_option_fee(0.5, contracts=2, multiplier=100, is_sell=True)

    assert round(out, 6) == round(1.99 + 0.6 + 0.026 + 0.04 + 0.36 + 0.0006 + 0.01 + 0.01, 6)


def test_calc_futu_us_option_fee_uses_low_premium_tier_and_buy_has_no_sell_only_fees() -> None:
    from domain.domain.fee_calc import calc_futu_us_option_fee

    out = calc_futu_us_option_fee(0.05, contracts=1, multiplier=100, is_sell=False)

    assert round(out, 6) == round(1.99 + 0.3 + 0.013 + 0.02 + 0.18 + 0.0003, 6)


def test_calc_futu_us_option_fee_caps_occ_fee_per_order() -> None:
    from domain.domain.fee_calc import calc_futu_us_option_fee

    out = calc_futu_us_option_fee(1.0, contracts=3000, multiplier=100, is_sell=False)

    assert out == round(1950.0 + 900.0 + 39.0 + 55.0 + 540.0 + 0.9, 6)


def test_calc_futu_option_fee_requires_positive_multiplier() -> None:
    from domain.domain.fee_calc import calc_futu_option_fee

    with pytest.raises(ValueError) as _caught:
        calc_futu_option_fee("USD", 1.0, contracts=1, multiplier=0, is_sell=True)
    exc = _caught.value
    assert "multiplier" in str(exc)


def test_calc_futu_option_fee_uses_shared_currency_aliases() -> None:
    from domain.domain.fee_calc import calc_futu_option_fee

    out = calc_futu_option_fee("港币", 1.0, contracts=1, multiplier=100, is_sell=True)

    assert out == 21.0


def test_calc_futu_option_fee_rejects_missing_or_unsupported_currency() -> None:
    from domain.domain.fee_calc import calc_futu_option_fee

    for currency in (None, "", "CNY", "EUR"):
        with pytest.raises(ValueError) as _caught:
            calc_futu_option_fee(currency, 1.0, contracts=1, multiplier=100, is_sell=True)
        exc = _caught.value
        assert "USD or HKD" in str(exc)


def test_calc_futu_us_stock_fee_includes_sell_only_regulatory_fees() -> None:
    from domain.domain.fee_calc import calc_futu_us_stock_fee

    sell = calc_futu_us_stock_fee(105.0, shares=100, is_sell=True)
    buy = calc_futu_us_stock_fee(105.0, shares=100, is_sell=False)

    assert sell == 2.5261
    assert buy == 2.2903


def test_calc_futu_hk_stock_fee_rounds_stamp_duty_up() -> None:
    from domain.domain.fee_calc import calc_futu_hk_stock_fee

    out = calc_futu_hk_stock_fee(10.01, shares=100, is_sell=True)

    assert out == round(3.0 + 15.0 + 1001 * 0.000042 + 2.0 + 1001 * 0.0000565 + 1001 * 0.000027 + 1001 * 0.0000015, 6)


def test_broker_trade_normalization_does_not_admit_raw_fee_components_as_actual() -> None:
    from types import SimpleNamespace

    from src.application.ledger.writer import _trade_event_from_normalized_deal

    event = _trade_event_from_normalized_deal(
        SimpleNamespace(
            broker="富途",
            futu_account_id="1",
            internal_account="lx",
            deal_id="deal-fee-1",
            order_id="order-fee-1",
            symbol="NVDA",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=2.5,
            strike=100.0,
            multiplier=100,
            multiplier_source="payload",
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1_780_000_000_000,
            raw_payload={"commission": 1.99, "platform_fee": 0.3},
        )
    )

    assert event.fees == 0
    assert "fee_provenance" not in event.raw_payload


def test_sell_put_compute_metrics_uses_full_fee_formula() -> None:
    from datetime import datetime, timezone
    from src.application.scan_sell_put import compute_metrics

    row = pd.Series(
        _formal_metric_row(
            mode="put",
            currency="USD",
            bid=0.49,
            ask=0.51,
            strike=90.0,
            spot=100.0,
            dte=14,
            snapshot_received_at_utc="2026-04-01T14:59:00Z",
        )
    )

    out = compute_metrics(row, now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc))

    assert out is not None
    assert round(out["futu_fee"], 6) == round(1.99 + 0.6 + 0.013 + 0.02 + 0.18 + 0.0003 + 0.01 + 0.01, 6)
    assert round(out["net_income"], 6) == round(50.0 - out["futu_fee"], 6)


def test_sell_call_compute_metrics_uses_full_fee_formula() -> None:
    from src.application.scan_sell_call import compute_metrics

    row = pd.Series(
        _formal_metric_row(
            mode="call",
            currency="HKD",
            bid=7.9,
            ask=8.1,
            strike=480.0,
            spot=500.0,
            dte=21,
        )
    )

    out = compute_metrics(row, avg_cost=430.0)

    assert out is not None
    assert out["futu_fee"] == 21.0
    assert out["net_income"] == 779.0
    assert round(out["annualized_net_premium_return"], 6) == round((779.0 / (500.0 * 100)) * (365 / 21), 6)
    assert round(out["if_exercised_total_return"], 6) == round((((480.0 - 430.0) * 100) + 779.0) / (430.0 * 100), 6)


_STANDARD_FIXED_PLAN = {
    "commission_free": False,
    "platform_fee": 15.0,
    "fee_plan_ref": "futu_hk_standard_fixed",
}
_TERMINAL_RESULT_KEYS = {
    "kind",
    "currency",
    "source",
    "schedule_version",
    "complete",
    "basis",
    "amount",
    "reason",
    "fee_plan_ref",
    "missing_plan_facts",
    "components",
    "estimated_components",
    "estimated_amount",
    "estimated_basis",
}


def _assert_terminal_result_contract(out: dict) -> None:
    assert set(out) == _TERMINAL_RESULT_KEYS
    if out["amount"] is not None:
        assert out["amount"] == round(sum(out["components"].values()), 6)
    if out["estimated_amount"] is not None:
        assert out["estimated_amount"] == round(sum(out["estimated_components"].values()), 6)


def test_hk_terminal_assignment_standard_fixed_plan_hand_computed() -> None:
    out = calc_futu_hk_terminal_fee(
        "assignment",
        order_price=450.0,
        shares=100,
        contracts=1,
        account_fee_plan=_STANDARD_FIXED_PLAN,
    )

    assert out["complete"] is True
    assert out["basis"] == "estimated"
    assert out["amount"] == 79.215
    assert out["components"]["exercise_fee"] == 0.0
    assert out["source"] == FUTU_HK_FEE_SCHEDULE_URL
    assert out["schedule_version"] == FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
    assert out["fee_plan_ref"] == "futu_hk_standard_fixed"
    assert out["estimated_amount"] == 79.215
    _assert_terminal_result_contract(out)


def test_hk_terminal_exercise_adds_two_hkd_per_contract() -> None:
    out = calc_futu_hk_terminal_fee(
        "exercise",
        order_price=450.0,
        shares=100,
        contracts=1,
        account_fee_plan=_STANDARD_FIXED_PLAN,
    )

    assert out["complete"] is True
    assert out["amount"] == 81.215
    assert out["components"]["exercise_fee"] == 2.0
    _assert_terminal_result_contract(out)


def test_hk_terminal_expired_worthless_is_zero() -> None:
    out = calc_futu_hk_terminal_fee(
        "expired_worthless",
        contracts=1,
        account_fee_plan=_STANDARD_FIXED_PLAN,
    )

    assert out["complete"] is True
    assert out["basis"] == "estimated"
    assert out["amount"] == 0.0
    assert out["estimated_amount"] == 0.0
    assert out["fee_plan_ref"] is None
    _assert_terminal_result_contract(out)


def test_hk_terminal_assignment_commission_free_plan_zeroes_commission() -> None:
    out = calc_futu_hk_terminal_fee(
        "assignment",
        order_price=450.0,
        shares=100,
        contracts=1,
        account_fee_plan={
            "commission_free": True,
            "platform_fee": 15.0,
            "fee_plan_ref": "futu_hk_commission_free",
        },
    )

    assert out["complete"] is True
    # Standard fixed total 79.215 includes 13.5 commission; commission-free drops it.
    assert out["amount"] == round(79.215 - 13.5, 6)
    assert out["fee_plan_ref"] == "futu_hk_commission_free"
    # Audit estimate keeps the standard fixed non-commission-free number.
    assert out["estimated_amount"] == 79.215
    _assert_terminal_result_contract(out)


def test_hk_terminal_missing_plan_facts_fail_closed_but_keep_estimate() -> None:
    for plan in (
        None,
        {},
        {"commission_free": False, "platform_fee": 15.0},
        {"commission_free": False, "fee_plan_ref": "x"},
        {"platform_fee": 15.0, "fee_plan_ref": "x"},
    ):
        out = calc_futu_hk_terminal_fee(
            "assignment",
            order_price=450.0,
            shares=100,
            contracts=1,
            account_fee_plan=plan,
        )
        assert out["complete"] is False
        assert out["basis"] == "missing"
        assert out["amount"] is None
        assert out["reason"] == "hk_account_fee_plan_missing"
        assert out["estimated_amount"] == 79.215
        assert out["missing_plan_facts"]
        _assert_terminal_result_contract(out)


def test_hk_terminal_rejects_lossy_or_nonfinite_economic_inputs() -> None:
    for field, value in (
        ("order_price", True),
        ("order_price", float("nan")),
        ("order_price", float("inf")),
        ("order_price", 1e308),
        ("shares", True),
        ("shares", 100.5),
        ("shares", float("inf")),
        ("contracts", True),
        ("contracts", 1.5),
        ("contracts", -1),
    ):
        inputs = {"order_price": 450.0, "shares": 100, "contracts": 1}
        inputs[field] = value
        out = calc_futu_hk_terminal_fee(
            "assignment",
            **inputs,
            account_fee_plan=_STANDARD_FIXED_PLAN,
        )
        assert out["complete"] is False
        assert out["reason"] == "stock_fee_inputs_incomplete"
        assert out["amount"] is None
        assert out["estimated_amount"] is None
        _assert_terminal_result_contract(out)


def test_hk_terminal_rejects_invalid_plan_fact_types() -> None:
    for plan in (
        {"commission_free": 1, "platform_fee": 15.0, "fee_plan_ref": "x"},
        {"commission_free": False, "platform_fee": "15", "fee_plan_ref": "x"},
        {"commission_free": False, "platform_fee": float("inf"), "fee_plan_ref": "x"},
        {"commission_free": False, "platform_fee": 15.0, "fee_plan_ref": 1},
    ):
        out = calc_futu_hk_terminal_fee(
            "assignment",
            order_price=450.0,
            shares=100,
            contracts=1,
            account_fee_plan=plan,
        )
        assert out["complete"] is False
        assert out["reason"] == "hk_account_fee_plan_missing"
        assert out["amount"] is None
        assert out["estimated_amount"] == 79.215
        _assert_terminal_result_contract(out)


def test_hk_terminal_rejects_unknown_kind() -> None:
    try:
        calc_futu_hk_terminal_fee(
            "sale",
            order_price=1.0,
            shares=1,
            contracts=1,
            account_fee_plan=_STANDARD_FIXED_PLAN,
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError for unsupported terminal kind")
