from __future__ import annotations

from domain.domain.close_advice import (
    CloseAdviceInput,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    STRICT_CLOSE_POLICY_VERSION,
    evaluate_close_advice,
    select_close_advice_notification_rows,
)


def _input(**overrides: object) -> CloseAdviceInput:
    values: dict[str, object] = {
        "account": "lx",
        "position_lot_id": "lot-nvda-put-1",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "expiration": "2026-06-30",
        "strike": 100.0,
        "contracts_open": 1,
        "premium": 2.0,
        "bid": 0.07,
        "ask": 0.08,
        "dte": 30,
        "original_dte": 60,
        "multiplier": 100,
        "spot": 120.0,
        "currency": "USD",
        "estimated_open_fee": 0.5,
        "estimated_close_fee": 0.5,
        "fee_calc_status": "schedule_estimate",
        "fee_calc_basis": "test_schedule",
        "delta": -0.06,
    }
    return CloseAdviceInput(**{**values, **overrides})  # type: ignore[arg-type]


def _row(**overrides: object) -> dict:
    """Strict-policy row for one case, on top of the `_input` defaults."""
    return evaluate_close_advice(_input(**overrides))


def test_remaining_yield_policy_closes_only_when_every_gate_passes() -> None:
    row = _row()

    assert row["policy_version"] == STRICT_CLOSE_POLICY_VERSION
    assert row["recommendation_state"] == RECOMMENDATION_CLOSE
    assert row["is_otm"] is True
    assert round(row["opening_net_credit"], 6) == 199.5
    assert round(row["all_in_close_cost"], 6) == 8.5
    assert round(row["net_capture_ratio"], 6) == round(1 - 8.5 / 199.5, 6)
    assert round(row["close_cost_ratio"], 6) == 0.00085
    assert row["remaining_term_ratio"] == 0.5
    assert row["capital_basis"] == 10000.0
    assert row["remaining_max_annualized_return"] == 8.5 / 10000 * 365 / 30


def test_remaining_yield_thresholds_are_inclusive() -> None:
    row = _row(
        premium=50.0, bid=10.0, ask=10.0,
        estimated_open_fee=0.0, estimated_close_fee=0.0,
        dte=365, original_dte=365,
    )

    assert row["opening_net_credit"] == 5000.0
    assert row["all_in_close_cost"] == 1000.0
    assert row["net_capture_ratio"] == 0.8
    assert row["remaining_max_annualized_return"] == 0.1
    assert row["recommendation_state"] == RECOMMENDATION_CLOSE


def test_forward_annualized_example_holds_at_one_dollar_and_closes_at_thirty_cents() -> None:
    common = {
        "strike": 50.0,
        "premium": 5.0,
        "spot": 60.0,
        "dte": 30,
        "estimated_open_fee": 0.0,
        "estimated_close_fee": 0.0,
    }
    hold = _row(**common, bid=1.0, ask=1.0)
    close = _row(**common, bid=0.3, ask=0.3)
    assert hold["net_capture_ratio"] == 0.8
    assert round(hold["remaining_max_annualized_return"], 4) == 0.2433
    assert hold["recommendation_state"] == RECOMMENDATION_HOLD
    assert close["net_capture_ratio"] == 0.94
    assert round(close["remaining_max_annualized_return"], 3) == 0.073
    assert close["recommendation_state"] == RECOMMENDATION_CLOSE


def test_each_failed_economic_gate_holds_instead_of_creating_another_action() -> None:
    scenarios = {
        "option_not_otm": {"spot": 90.0},
        "net_capture_below_threshold": {"ask": 0.50, "bid": 0.49},
        "remaining_annualized_above_threshold": {"dte": 1},
    }

    for expected_flag, overrides in scenarios.items():
        row = _row(**overrides)
        assert row["recommendation_state"] == RECOMMENDATION_HOLD
        assert expected_flag in row["decision_basis"]


def test_call_must_be_otm_under_the_same_strict_policy() -> None:
    close = _row(option_type="call", spot=80.0)
    hold = _row(option_type="call", spot=120.0)

    assert close["recommendation_state"] == RECOMMENDATION_CLOSE
    assert hold["recommendation_state"] == RECOMMENDATION_HOLD
    assert "option_not_otm" in hold["decision_basis"]
    assert close["capital_basis"] == 8000.0
    assert close["remaining_max_annualized_return"] == 8.5 / 8000 * 365 / 30


def test_short_dte_missing_open_date_and_wide_spread_can_close() -> None:
    row = _row(dte=5, original_dte=None, bid=0.0, ask=0.01)
    assert row["recommendation_state"] == RECOMMENDATION_CLOSE
    assert row["remaining_term_ratio"] is None
    assert row["spread_ratio"] == 2.0


def test_near_expiry_low_delta_holds_only_with_calendar_session_evidence() -> None:
    common = {"dte": 4, "bid": 0.0, "ask": 0.01, "delta": -0.05}
    hold = _row(**common, remaining_trading_sessions=3)
    assert hold["recommendation_state"] == RECOMMENDATION_HOLD
    assert hold["decision_basis"] == "near_expiry_far_otm_hold"
    assert hold["dte"] == 4  # Annualization still uses calendar days.
    assert hold["remaining_trading_sessions"] == 3

    assert _row(**common, remaining_trading_sessions=1)["recommendation_state"] == RECOMMENDATION_HOLD
    assert _row(**common, remaining_trading_sessions=0)["recommendation_state"] == RECOMMENDATION_HOLD

    for overrides in (
        {"remaining_trading_sessions": 4},
        {"remaining_trading_sessions": 2, "delta": -0.06},
    ):
        row = _row(**{**common, **overrides})
        assert row["recommendation_state"] == RECOMMENDATION_CLOSE

    for overrides in (
        {"remaining_trading_sessions": -1},
        {"remaining_trading_sessions": None},
        {"remaining_trading_sessions": 2, "delta": None},
    ):
        row = _row(**{**common, **overrides})
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE

    assert _row(**common, remaining_trading_sessions_min=2,
                remaining_trading_sessions_max=3)["recommendation_state"] == RECOMMENDATION_HOLD
    assert _row(**common, remaining_trading_sessions_min=3,
                remaining_trading_sessions_max=4)["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
    assert _row(**common, remaining_trading_sessions_min=4,
                remaining_trading_sessions_max=5)["recommendation_state"] == RECOMMENDATION_CLOSE

    call = _row(
        option_type="call", spot=80.0, dte=4, bid=0.0, ask=0.01,
        delta=0.04, remaining_trading_sessions=2,
    )
    assert call["recommendation_state"] == RECOMMENDATION_HOLD


def test_incomplete_quote_fee_or_invalid_open_date_is_not_evaluable() -> None:
    for overrides, expected_flag in (
        ({"ask": None}, "missing_ask"),
        ({"fee_calc_status": "unavailable"}, "fee_evidence_unavailable"),
        ({"fee_calc_basis": None}, "fee_evidence_unavailable"),
        ({"currency": None}, "missing_currency"),
        ({"original_dte": -1}, "invalid_original_dte"),
        ({"dte": 61}, "inconsistent_position_dates"),
    ):
        row = _row(**overrides)
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
        assert expected_flag in row["data_quality_flags"]


def test_boolean_numeric_evidence_is_not_evaluable() -> None:
    for field, expected_flag in (
        ("premium", "missing_premium"),
        ("bid", "missing_bid"),
        ("ask", "missing_ask"),
        ("dte", "missing_dte"),
        ("original_dte", "invalid_original_dte"),
        ("multiplier", "missing_multiplier"),
        ("contracts_open", "missing_contracts_open"),
        ("strike", "missing_strike"),
        ("spot", "missing_spot"),
        ("estimated_open_fee", "fee_evidence_unavailable"),
        ("estimated_close_fee", "fee_evidence_unavailable"),
    ):
        row = _row(**{field: True})
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
        assert expected_flag in row["data_quality_flags"]


def test_missing_position_identity_is_not_evaluable() -> None:
    for overrides, expected_flag in (
        ({"account": ""}, "missing_account"),
        ({"position_lot_id": None}, "missing_position_lot_id"),
        ({"symbol": ""}, "missing_symbol"),
    ):
        row = _row(**overrides)
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
        assert expected_flag in row["data_quality_flags"]


def test_overflowed_economic_result_fails_closed() -> None:
    row = _row(premium=1e308, multiplier=1e308)
    assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
    assert "invalid_economic_denominator" in row["data_quality_flags"]


def test_remaining_yield_orders_close_rows_before_capture_ratio() -> None:
    low_yield = _row(symbol="LOW", ask=0.02, bid=0.02)
    high_capture = _row(symbol="HIGH", premium=10, ask=0.08, bid=0.07)
    assert low_yield["net_capture_ratio"] < high_capture["net_capture_ratio"]
    selected = select_close_advice_notification_rows([high_capture, low_yield])
    assert [row["symbol"] for row in selected] == ["LOW", "HIGH"]


def test_notification_selection_uses_only_close_state() -> None:
    close = _row(symbol="NVDA")
    hold = _row(symbol="AMD", ask=0.50, bid=0.49)
    close["evaluation_status"] = "priced"
    hold["evaluation_status"] = "priced"

    selected = select_close_advice_notification_rows(
        [hold, close],
        max_items_per_account=5,
    )

    assert [row["symbol"] for row in selected] == ["NVDA"]


def test_notification_selection_rejects_unversioned_or_legacy_close_rows() -> None:
    strict = _row(symbol="STRICT")
    unversioned = {**strict, "symbol": "UNVERSIONED"}
    unversioned.pop("policy_version")
    legacy = {
        **strict,
        "symbol": "LEGACY",
        "policy_version": "legacy_close_policy.v1",
    }

    selected = select_close_advice_notification_rows(
        [unversioned, legacy, strict],
        max_items_per_account=5,
    )

    assert [row["symbol"] for row in selected] == ["STRICT"]


def test_notification_selection_rejects_close_without_complete_evidence() -> None:
    strict = _row(symbol="STRICT")
    missing = {**strict, "symbol": "MISSING"}
    missing.pop("decision_evidence_status")
    inconsistent = {
        **strict,
        "symbol": "INCONSISTENT",
        "decision_evidence_status": "not_evaluable",
    }

    selected = select_close_advice_notification_rows(
        [missing, inconsistent, strict],
        max_items_per_account=5,
    )

    assert [row["symbol"] for row in selected] == ["STRICT"]


def test_notification_selection_rejects_close_without_new_metrics() -> None:
    complete = _row(symbol="COMPLETE")
    no_basis = {**complete, "symbol": "NO_BASIS", "capital_basis": None}
    no_yield = {**complete, "symbol": "NO_YIELD", "remaining_max_annualized_return": None}
    infinite_yield = {**complete, "symbol": "INFINITE", "remaining_max_annualized_return": float("inf")}
    selected = select_close_advice_notification_rows(
        [no_basis, no_yield, infinite_yield, complete]
    )
    assert [row["symbol"] for row in selected] == ["COMPLETE"]
