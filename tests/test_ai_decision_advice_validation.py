from __future__ import annotations

from src.application.ai_decision_advice.validation import (
    derive_scopes,
    validate_advice_payload,
    zero_candidate_flags,
)


def _candidates() -> dict:
    return {
        "market": "US",
        "sell_put": [
            {"candidate_id": "put-1", "rank": 1, "symbol": "NVDA"},
            {"candidate_id": "put-2", "rank": 2, "symbol": "AAPL"},
        ],
        "covered_call": [
            {"candidate_id": "call-1", "rank": 1, "symbol": "NVDA"},
            {"candidate_id": "call-2", "rank": 2, "symbol": "NVDA"},
            {"candidate_id": "call-3", "rank": 1, "symbol": "TSLA"},
        ],
    }


def _evidence(coverage: str = "completed") -> dict:
    return {
        "frozen_at": "2026-08-09T00:00:00+00:00",
        "index_hash": "hash",
        "symbols": [
            {"symbol": "NVDA", "coverage": coverage, "evidence": []},
            {"symbol": "AAPL", "coverage": coverage, "evidence": []},
            {"symbol": "TSLA", "coverage": coverage, "evidence": []},
        ],
    }


def _bindings() -> dict:
    return {
        "candidate_snapshot_hash": "c",
        "portfolio_context_hash": "p",
        "option_positions_hash": "o",
        "external_evidence_hash": "e",
        "external_evidence_run_id": "er-1",
    }


def _payload(decisions: list[dict], *, family: str = "sell_put") -> dict:
    return {
        "schema": "ai_decision_advice.v1",
        "run_id": "run-1",
        "account_ref": "acct-ref",
        "market": "US",
        "input_bindings": _bindings(),
        "strategies": [{"strategy_family": family, "status": "completed", "decisions": decisions}],
    }


def _decision(**overrides) -> dict:
    row = {
        "scope_symbol": None,
        "baseline_candidate_id": "put-1",
        "action": "keep",
        "selected_candidate_id": "put-1",
        "rationale": {
            "risk_mechanism": "m",
            "candidate_effect": "e",
            "decision_reason": "r",
        },
        "internal_fact_refs": [],
        "external_evidence_refs": [],
    }
    row.update(overrides)
    return row


def _validate(payload, **overrides):
    scopes = overrides.pop("scopes", None) or derive_scopes(_candidates(), _evidence())
    return validate_advice_payload(
        payload,
        scopes=scopes,
        run_id="run-1",
        account_ref="acct-ref",
        market="US",
        input_bindings=_bindings(),
        context_complete=overrides.pop("context_complete", True),
    )


def test_derive_scopes_baselines_and_pools():
    scopes = derive_scopes(_candidates(), _evidence())
    assert set(scopes) == {"sell_put", "covered_call:NVDA", "covered_call:TSLA"}
    assert scopes["sell_put"].baseline_candidate_id == "put-1"
    assert scopes["sell_put"].allowed_candidate_ids == frozenset({"put-1", "put-2"})
    assert scopes["covered_call:NVDA"].baseline_candidate_id == "call-1"
    assert scopes["covered_call:NVDA"].allowed_candidate_ids == frozenset({"call-1", "call-2"})
    assert scopes["covered_call:TSLA"].baseline_candidate_id == "call-3"


def test_derive_scopes_evidence_incomplete_when_any_symbol_not_completed():
    evidence = _evidence()
    evidence["symbols"][0]["coverage"] = "stale"
    scopes = derive_scopes(_candidates(), evidence)
    assert scopes["sell_put"].symbol_evidence_complete is False
    assert scopes["covered_call:NVDA"].symbol_evidence_complete is False
    assert scopes["covered_call:TSLA"].symbol_evidence_complete is True


def test_zero_candidate_flags():
    flags = zero_candidate_flags({"sell_put": [], "covered_call": []})
    assert flags == {"sell_put": True, "covered_call": True}
    flags = zero_candidate_flags(_candidates())
    assert flags == {"sell_put": False, "covered_call": False}


def test_keep_happy_path():
    result = _validate(_payload([_decision()]))
    assert result.status == "completed"
    assert result.demotions == []
    decision = result.decisions["sell_put"]
    assert decision["action"] == "keep"
    assert decision["selected_candidate_id"] == "put-1"


def test_keep_with_incomplete_evidence_demotes_to_defer():
    scopes = derive_scopes(_candidates(), _evidence(coverage="stale"))
    result = _validate(_payload([_decision()]), scopes=scopes)
    assert result.status == "completed"
    decision = result.decisions["sell_put"]
    assert decision["action"] == "defer"
    assert decision["selected_candidate_id"] is None
    assert result.demotions[0]["reason"] == "evidence_incomplete"


def test_context_missing_caps_every_action_at_needs_review():
    for action, selected in (("keep", "put-1"), ("switch", "put-2"), ("defer", None)):
        result = _validate(
            _payload([_decision(action=action, selected_candidate_id=selected)]),
            context_complete=False,
        )
        assert result.decisions["sell_put"]["action"] == "needs_review"
        assert result.demotions[0]["reason"] == "context_missing"


def test_switch_to_unknown_or_rejected_candidate_demotes():
    result = _validate(_payload([_decision(action="switch", selected_candidate_id="put-999")]))
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "switch_out_of_pool"


def test_cc_cross_symbol_switch_demotes():
    scopes = derive_scopes(_candidates(), _evidence())
    decision = _decision(
        scope_symbol="NVDA",
        baseline_candidate_id="call-1",
        action="switch",
        selected_candidate_id="call-3",  # belongs to TSLA scope
    )
    result = _validate(_payload([decision], family="covered_call"), scopes=scopes)
    row = result.decisions["covered_call:NVDA"]
    assert row["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "switch_out_of_pool"


def test_cross_strategy_selected_reference_demotes():
    # A Covered Call scope must never reference a Sell Put candidate id.
    scopes = derive_scopes(_candidates(), _evidence())
    decision = _decision(
        scope_symbol="NVDA",
        baseline_candidate_id="call-1",
        action="switch",
        selected_candidate_id="put-1",
    )
    result = _validate(_payload([decision], family="covered_call"), scopes=scopes)
    row = result.decisions["covered_call:NVDA"]
    assert row["action"] == "needs_review"
    assert row["selected_candidate_id"] is None
    assert result.demotions[0]["reason"] == "switch_out_of_pool"


def test_cc_same_symbol_switch_passes():
    scopes = derive_scopes(_candidates(), _evidence())
    decision = _decision(
        scope_symbol="NVDA",
        baseline_candidate_id="call-1",
        action="switch",
        selected_candidate_id="call-2",
    )
    result = _validate(_payload([decision], family="covered_call"), scopes=scopes)
    assert result.status == "completed"
    row = result.decisions["covered_call:NVDA"]
    assert row["action"] == "switch"
    assert row["selected_candidate_id"] == "call-2"


def test_defer_with_selected_demotes():
    result = _validate(_payload([_decision(action="defer", selected_candidate_id="put-1")]))
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "selected_forbidden_for_action"


def test_baseline_mismatch_demotes():
    result = _validate(_payload([_decision(baseline_candidate_id="put-2")]))
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "baseline_mismatch"


def test_unknown_scope_demotes():
    scopes = derive_scopes(_candidates(), _evidence())
    decision = _decision(
        scope_symbol="MSFT",
        baseline_candidate_id="call-x",
        action="keep",
        selected_candidate_id="call-x",
    )
    result = _validate(_payload([decision], family="covered_call"), scopes=scopes)
    assert result.decisions["covered_call:MSFT"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "unknown_scope"


def test_schema_mismatch_is_unavailable():
    payload = _payload([_decision()])
    payload["schema"] = "other.v9"
    result = _validate(payload)
    assert result.status == "unavailable"
    assert "schema" in (result.error or "")


def test_binding_mismatch_is_unavailable():
    payload = _payload([_decision()])
    payload["input_bindings"]["portfolio_context_hash"] = "changed"
    result = _validate(payload)
    assert result.status == "unavailable"
    assert "portfolio_context_hash" in (result.error or "")


def test_run_id_and_account_ref_mismatch_are_unavailable():
    payload = _payload([_decision()])
    payload["run_id"] = "other-run"
    assert _validate(payload).status == "unavailable"
    payload = _payload([_decision()])
    payload["account_ref"] = "someone-else"
    assert _validate(payload).status == "unavailable"


def test_duplicate_scope_is_unavailable():
    result = _validate(_payload([_decision(), _decision()]))
    assert result.status == "unavailable"
    assert "duplicate" in (result.error or "")
