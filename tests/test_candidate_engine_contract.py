from __future__ import annotations


def test_candidate_engine_stage_order_matches_strategy_contract() -> None:
    from domain.domain.engine import (
        CANDIDATE_STAGE_ORDER,
        STAGE_HARD_CONSTRAINTS,
        STAGE_INPUT_NORMALIZATION,
        STAGE_RANKING,
        STAGE_RETURN_FLOOR,
        STAGE_RISK_FILTER,
    )

    assert CANDIDATE_STAGE_ORDER == (
        STAGE_INPUT_NORMALIZATION,
        STAGE_HARD_CONSTRAINTS,
        STAGE_RETURN_FLOOR,
        STAGE_RISK_FILTER,
        STAGE_RANKING,
    )


def test_candidate_engine_builds_reject_and_decision_payload() -> None:
    from domain.domain.engine import (
        SCHEMA_KIND_CANDIDATE_DECISION,
        STAGE_RETURN_FLOOR,
        build_candidate_decision,
        build_candidate_reject,
    )

    reject = build_candidate_reject(
        stage=STAGE_RETURN_FLOOR,
        reason="return_annualized",
        message="annualized return below threshold",
        metric_value=0.08,
        threshold=0.1,
    )
    payload = build_candidate_decision(
        mode="put",
        symbol="nvda",
        contract_symbol="NVDA_TEST",
        accepted=False,
        rejects=[reject],
        score=0.08,
        rank_key={"annualized_return": 0.08},
    )

    assert payload["schema_kind"] == SCHEMA_KIND_CANDIDATE_DECISION
    assert payload["mode"] == "put"
    assert payload["symbol"] == "NVDA"
    assert payload["accepted"] is False
    assert payload["rejects"] == [reject]
    assert payload["score"] == 0.08
    assert payload["rank_key"] == {"annualized_return": 0.08}


def test_candidate_engine_rejects_unknown_stage_reason_and_mode() -> None:
    from domain.domain.engine import build_candidate_decision, build_candidate_reject

    try:
        build_candidate_reject(stage="unknown", reason="return_annualized")
        raise AssertionError("expected unsupported stage")
    except ValueError as e:
        assert "unsupported candidate reject stage" in str(e)

    try:
        build_candidate_reject(stage="stage2_return_floor", reason="unknown")
        raise AssertionError("expected unsupported reason")
    except ValueError as e:
        assert "unsupported candidate reject reason" in str(e)

    try:
        build_candidate_decision(mode="straddle", symbol="NVDA", accepted=True)
        raise AssertionError("expected unsupported mode")
    except ValueError as e:
        assert "unsupported candidate strategy mode" in str(e)


def test_candidate_engine_stage0_normalizes_valid_input() -> None:
    from domain.domain.engine import evaluate_candidate_input

    payload = evaluate_candidate_input(
        {
            "symbol": "nvda",
            "option_type": "PUT",
            "expiration": "2026-06-18",
            "dte": "45",
            "spot": "150.25",
            "strike": "140",
            "mid": "2.35",
            "multiplier": "100",
            "delta": "-0.21",
            "currency": "usd",
            "contract_symbol": "NVDA_TEST",
        },
        mode="put",
    )

    assert payload["accepted"] is True
    assert payload["rejects"] == []
    normalized = payload["normalized_input"]
    assert normalized["symbol"] == "NVDA"
    assert normalized["option_type"] == "put"
    assert normalized["dte"] == 45
    assert normalized["spot"] == 150.25
    assert normalized["strike"] == 140.0
    assert normalized["mid"] == 2.35
    assert normalized["multiplier"] == 100.0
    assert normalized["delta"] == -0.21
    assert normalized["currency"] == "USD"


def test_candidate_engine_stage0_rejects_missing_and_mismatched_input() -> None:
    from domain.domain.engine import STAGE_INPUT_NORMALIZATION, evaluate_candidate_input

    payload = evaluate_candidate_input(
        {
            "symbol": "AAPL",
            "option_type": "call",
            "expiration": "2026-06-18",
            "dte": "30",
            "spot": "190",
            "strike": "",
            "mid": "1.25",
        },
        mode="put",
        extra_required_fields=("avg_cost",),
    )

    assert payload["accepted"] is False
    rejects = payload["rejects"]
    assert len(rejects) == 2
    assert rejects[0]["stage"] == STAGE_INPUT_NORMALIZATION
    assert rejects[0]["reason"] == "input_missing"
    assert rejects[0]["threshold"] == ["strike", "multiplier", "avg_cost"]
    assert rejects[1]["reason"] == "input_missing"
    assert rejects[1]["metric_value"] == "call"
    assert rejects[1]["threshold"] == "put"


def test_candidate_engine_stage1_rejects_put_hard_constraints() -> None:
    from domain.domain.engine import STAGE_HARD_CONSTRAINTS, evaluate_candidate_hard_constraints

    payload = evaluate_candidate_hard_constraints(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-18",
            "dte": "95",
            "spot": "150",
            "strike": "155",
            "mid": "2.1",
            "multiplier": "100",
        },
        mode="put",
        max_dte=90,
        max_strike=140,
        put_cash_required=15500,
        put_cash_free=12000,
    )

    assert payload["accepted"] is False
    reasons = [r["reason"] for r in payload["rejects"]]
    assert reasons == ["hard_dte", "hard_strike", "hard_strike", "hard_capacity_put"]
    assert all(r["stage"] == STAGE_HARD_CONSTRAINTS for r in payload["rejects"])


def test_candidate_engine_stage1_rejects_call_hard_constraints() -> None:
    from domain.domain.engine import evaluate_candidate_hard_constraints

    payload = evaluate_candidate_hard_constraints(
        {
            "symbol": "AAPL",
            "option_type": "call",
            "expiration": "2026-06-18",
            "dte": "30",
            "spot": "200",
            "strike": "204",
            "mid": "1.1",
            "multiplier": "100",
        },
        mode="call",
        min_strike=210,
        call_covered_contracts_available=0,
    )

    assert payload["accepted"] is False
    rejects = payload["rejects"]
    assert [r["reason"] for r in rejects] == ["hard_strike", "hard_capacity_call"]
    assert rejects[0]["message"] == "strike below minimum"
    assert rejects[1]["threshold"] == 1


def test_candidate_engine_stage1_stops_when_stage0_fails() -> None:
    from domain.domain.engine import evaluate_candidate_hard_constraints

    payload = evaluate_candidate_hard_constraints(
        {
            "symbol": "AAPL",
            "option_type": "put",
            "expiration": "2026-06-18",
            "dte": "",
            "spot": "200",
            "strike": "210",
            "mid": "1.1",
        },
        mode="put",
        max_dte=30,
        max_strike=100,
        put_cash_required=999999,
        put_cash_free=1,
    )

    assert payload["accepted"] is False
    assert len(payload["rejects"]) == 1
    assert payload["rejects"][0]["reason"] == "input_missing"
    assert payload["rejects"][0]["threshold"] == ["dte", "multiplier"]


def _accepted_base_candidate(mode: str = "put") -> dict:
    from domain.domain.engine import evaluate_candidate_hard_constraints

    option_type = mode
    strike = "180" if mode == "put" else "220"
    return evaluate_candidate_hard_constraints(
        {
            "symbol": "TSLA",
            "option_type": option_type,
            "expiration": "2026-06-18",
            "dte": "45",
            "spot": "200",
            "strike": strike,
            "mid": "3",
            "multiplier": "100",
        },
        mode=mode,
        min_dte=20,
        max_dte=90,
        put_cash_required=18000,
        put_cash_free=20000,
        call_covered_contracts_available=1,
    )


def test_candidate_engine_stage2_rejects_return_floor() -> None:
    from domain.domain.engine import evaluate_candidate_return_floor

    put = evaluate_candidate_return_floor(
        _accepted_base_candidate("put"),
        min_annualized_return=0.1,
        min_net_income=100,
        annualized_return=0.08,
        net_income=90,
    )
    assert put["accepted"] is False
    assert [r["reason"] for r in put["rejects"]] == ["return_annualized", "return_net_income"]
    assert all(r["stage"] == "stage2_return_floor" for r in put["rejects"])

    call = evaluate_candidate_return_floor(
        _accepted_base_candidate("call"),
        min_annualized_return=0.1,
        min_net_income=100,
        annualized_return=0.11,
        net_income=90,
    )
    assert call["accepted"] is False
    assert [r["reason"] for r in call["rejects"]] == ["return_net_income"]


def test_candidate_engine_stage3_risk_warn_does_not_reject_but_reject_mode_does() -> None:
    from domain.domain.engine import evaluate_candidate_risk_filter

    warn = evaluate_candidate_risk_filter(
        _accepted_base_candidate("put"),
        min_open_interest=50,
        min_volume=10,
        max_spread_ratio=0.3,
        open_interest=60,
        volume=11,
        spread_ratio=0.2,
        event_flag=True,
        event_mode="warn",
    )
    assert warn["accepted"] is True
    assert [r["reason"] for r in warn["rejects"]] == ["risk_event_warn"]

    reject = evaluate_candidate_risk_filter(
        _accepted_base_candidate("put"),
        min_open_interest=50,
        min_volume=10,
        max_spread_ratio=0.3,
        open_interest=20,
        volume=5,
        spread_ratio=0.4,
        event_flag=True,
        event_mode="reject",
    )
    assert reject["accepted"] is False
    assert [r["reason"] for r in reject["rejects"]] == [
        "risk_open_interest",
        "risk_volume",
        "risk_spread",
        "risk_event_reject",
    ]


def test_candidate_engine_stage4_rank_keys_match_put_call_policy() -> None:
    from domain.domain.engine import build_candidate_rank_key, rank_candidate_rows

    put_rows = [
        {"contract_symbol": "A", "annualized_net_return_on_cash_basis": 0.12, "net_income": 100, "delta": -0.10},
        {"contract_symbol": "B", "annualized_net_return_on_cash_basis": 0.10, "net_income": 200, "delta": -0.22},
    ]
    assert [r["contract_symbol"] for r in rank_candidate_rows(put_rows, mode="put")] == ["A", "B"]

    call_rows = [
        {"contract_symbol": "C1", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.20, "net_income": 100, "delta": 0.40},
        {"contract_symbol": "C2", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.21, "net_income": 90, "delta": 0.28},
    ]
    assert [r["contract_symbol"] for r in rank_candidate_rows(call_rows, mode="call")] == ["C1", "C2"]


def test_candidate_engine_rejects_unknown_legacy_reject_rule() -> None:
    from domain.domain.engine import map_legacy_reject_rule, normalize_legacy_reject_log_row

    try:
        map_legacy_reject_rule("legacy_new_rule")
        raise AssertionError("expected unsupported legacy reject rule")
    except ValueError as e:
        assert "unsupported legacy reject rule" in str(e)

    try:
        normalize_legacy_reject_log_row({"reject_rule": "legacy_new_rule"})
        raise AssertionError("expected unsupported legacy reject rule")
    except ValueError as e:
        assert "unsupported legacy reject rule" in str(e)
