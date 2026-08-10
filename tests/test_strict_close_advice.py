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
    }
    values.update(overrides)
    return CloseAdviceInput(**values)  # type: ignore[arg-type]


def test_strict_policy_closes_only_when_every_gate_passes() -> None:
    row = evaluate_close_advice(_input())

    assert row["policy_version"] == STRICT_CLOSE_POLICY_VERSION
    assert row["recommendation_state"] == RECOMMENDATION_CLOSE
    assert row["is_otm"] is True
    assert round(row["opening_net_credit"], 6) == 199.5
    assert round(row["all_in_close_cost"], 6) == 8.5
    assert round(row["net_capture_ratio"], 6) == round(1 - 8.5 / 199.5, 6)
    assert round(row["close_cost_ratio"], 6) == 0.00085
    assert row["remaining_term_ratio"] == 0.5


def test_strict_policy_thresholds_are_inclusive() -> None:
    row = evaluate_close_advice(
        _input(
            premium=1.01,
            bid=0.085,
            ask=0.09,
            estimated_open_fee=1.0,
            estimated_close_fee=1.0,
            dte=14,
            original_dte=28,
        )
    )

    assert row["opening_net_credit"] == 100.0
    assert row["all_in_close_cost"] == 10.0
    assert row["net_capture_ratio"] == 0.9
    assert row["close_cost_ratio"] == 0.001
    assert row["recommendation_state"] == RECOMMENDATION_CLOSE


def test_each_failed_economic_gate_holds_instead_of_creating_another_action() -> None:
    scenarios = {
        "option_not_otm": {"spot": 90.0},
        "net_capture_below_threshold": {"ask": 0.30, "bid": 0.29},
        "dte_below_threshold": {"dte": 13},
        "remaining_term_below_threshold": {"dte": 29, "original_dte": 60},
        "close_cost_ratio_above_threshold": {
            "strike": 50.0,
            "ask": 0.08,
            "bid": 0.07,
        },
        "spread_too_wide": {"bid": 0.04, "ask": 0.08},
    }

    for expected_flag, overrides in scenarios.items():
        row = evaluate_close_advice(_input(**overrides))
        assert row["recommendation_state"] == RECOMMENDATION_HOLD
        assert expected_flag in row["decision_basis"]


def test_call_must_be_otm_under_the_same_strict_policy() -> None:
    close = evaluate_close_advice(
        _input(option_type="call", spot=80.0)
    )
    hold = evaluate_close_advice(
        _input(option_type="call", spot=120.0)
    )

    assert close["recommendation_state"] == RECOMMENDATION_CLOSE
    assert hold["recommendation_state"] == RECOMMENDATION_HOLD
    assert "option_not_otm" in hold["decision_basis"]


def test_incomplete_quote_fee_or_open_date_is_not_evaluable() -> None:
    for overrides, expected_flag in (
        ({"ask": None}, "missing_ask"),
        ({"fee_calc_status": "unavailable"}, "fee_evidence_unavailable"),
        ({"fee_calc_basis": None}, "fee_evidence_unavailable"),
        ({"currency": None}, "missing_currency"),
        ({"original_dte": None}, "missing_original_dte"),
        ({"dte": 61}, "inconsistent_position_dates"),
    ):
        row = evaluate_close_advice(_input(**overrides))
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
        assert expected_flag in row["data_quality_flags"]


def test_boolean_numeric_evidence_is_not_evaluable() -> None:
    for field, expected_flag in (
        ("premium", "missing_premium"),
        ("bid", "missing_bid"),
        ("ask", "missing_ask"),
        ("dte", "missing_dte"),
        ("original_dte", "missing_original_dte"),
        ("multiplier", "missing_multiplier"),
        ("contracts_open", "missing_contracts_open"),
        ("strike", "missing_strike"),
        ("spot", "missing_spot"),
        ("estimated_open_fee", "fee_evidence_unavailable"),
        ("estimated_close_fee", "fee_evidence_unavailable"),
    ):
        row = evaluate_close_advice(_input(**{field: True}))
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
        assert expected_flag in row["data_quality_flags"]


def test_missing_position_identity_is_not_evaluable() -> None:
    for overrides, expected_flag in (
        ({"account": ""}, "missing_account"),
        ({"position_lot_id": None}, "missing_position_lot_id"),
        ({"symbol": ""}, "missing_symbol"),
    ):
        row = evaluate_close_advice(_input(**overrides))
        assert row["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
        assert expected_flag in row["data_quality_flags"]


def test_notification_selection_uses_only_close_state() -> None:
    close = evaluate_close_advice(_input(symbol="NVDA"))
    hold = evaluate_close_advice(_input(symbol="AMD", dte=13))
    close["evaluation_status"] = "priced"
    hold["evaluation_status"] = "priced"

    selected = select_close_advice_notification_rows(
        [hold, close],
        max_items_per_account=5,
    )

    assert [row["symbol"] for row in selected] == ["NVDA"]


def test_notification_selection_rejects_unversioned_or_legacy_close_rows() -> None:
    strict = evaluate_close_advice(_input(symbol="STRICT"))
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
    strict = evaluate_close_advice(_input(symbol="STRICT"))
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
