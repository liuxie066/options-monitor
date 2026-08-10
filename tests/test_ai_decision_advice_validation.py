from __future__ import annotations

from copy import deepcopy

import pytest

from src.application.ai_decision_advice.contexts import build_fact_registry
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


def _portfolio() -> dict:
    return {
        "status": "ready",
        "quality": {
            "freshness_status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-08-09T00:00:00+00:00",
        },
        "asset_weights": {"NVDA": 0.4, "AAPL": 0.2, "TSLA": 0.1},
        "currency_weights": {"USD": 1.0},
        "cash_and_mmf_weight": 0.3,
        "gaps": [],
    }


def _option_positions() -> dict:
    return {
        "status": "ready",
        "source_observed_at": "2026-08-09T00:00:00+00:00",
        "summary": {
            "total_open_contracts": 2,
            "by_direction_and_type": [],
            "by_expiry": [],
        },
        "candidate_contracts": [],
        "verified_structures": [],
        "gaps": [],
    }


def _projections(candidates: dict) -> dict:
    out: dict[str, dict] = {}
    for family, mode in (("sell_put", "put"), ("covered_call", "call")):
        for row in candidates[family]:
            candidate_id = row["candidate_id"]
            out[candidate_id] = {
                "candidate_id": candidate_id,
                "symbol": row["symbol"],
                "strategy_mode": mode,
                "calculation_complete": True,
                "scope_ceiling": None,
                "gaps": [],
            }
    return out


def _external(*, coverage: str = "completed") -> dict:
    return {
        "frozen_at": "2026-08-09T00:00:00+00:00",
        "index_hash": "hash",
        "symbols": [
            {
                "symbol": symbol,
                "coverage_ref": f"coverage:{symbol}",
                "coverage": coverage,
                "unavailable_reason": None if coverage == "completed" else "no_evidence",
                "last_checked_at": "2026-08-09T00:00:00+00:00",
                "last_success_at": "2026-08-09T00:00:00+00:00",
                "evidence": [
                    {
                        "ref": f"evidence:{symbol.lower()}",
                        "topic": "event",
                        "claim": f"{symbol} material event",
                        "event_status": "developing",
                        "source": {"url": f"https://example.com/{symbol.lower()}"},
                    }
                ],
            }
            for symbol in ("AAPL", "NVDA", "TSLA")
        ],
    }


def _environment(
    *,
    portfolio: dict | None = None,
    option_positions: dict | None = None,
    projections: dict | None = None,
    external: dict | None = None,
) -> tuple[dict, dict, dict]:
    candidates = _candidates()
    portfolio = _portfolio() if portfolio is None else portfolio
    option_positions = _option_positions() if option_positions is None else option_positions
    projections = _projections(candidates) if projections is None else projections
    external = _external() if external is None else external
    registry = build_fact_registry(
        candidates=candidates,
        portfolio=portfolio,
        option_positions=option_positions,
        projections=projections,
        external_evidence=external,
    )
    return candidates, registry, derive_scopes(candidates, registry)


def _bindings() -> dict:
    return {
        "candidate_snapshot_hash": "c",
        "portfolio_distribution_hash": "p",
        "option_positions_hash": "o",
        "fact_registry_hash": "f",
        "external_evidence_hash": "e",
        "external_evidence_run_id": "er-1",
    }


def _decision(
    spec,
    *,
    action: str = "keep",
    selected_candidate_id: str | None | object = ...,
    internal_fact_refs: list[str] | None = None,
    external_evidence_refs: list[str] | None = None,
) -> dict:
    if selected_candidate_id is ...:
        selected_candidate_id = spec.baseline_candidate_id if action == "keep" else None
    if internal_fact_refs is None:
        internal_fact_refs = (
            [
                spec.candidate_fact_refs[spec.baseline_candidate_id],
                spec.projection_fact_refs[spec.baseline_candidate_id],
                *sorted(spec.required_coverage_refs),
            ]
            if action == "keep"
            else []
        )
    return {
        "scope_symbol": spec.symbol,
        "baseline_candidate_id": spec.baseline_candidate_id,
        "action": action,
        "selected_candidate_id": selected_candidate_id,
        "rationale": {
            "risk_mechanism": "risk mechanism",
            "candidate_effect": "candidate effect",
            "decision_reason": "decision reason",
        },
        "internal_fact_refs": internal_fact_refs,
        "external_evidence_refs": external_evidence_refs or [],
    }


def _payload(scopes: dict, decisions: dict[str, dict] | None = None) -> dict:
    decisions = decisions or {scope_key: _decision(spec) for scope_key, spec in scopes.items()}
    by_family: dict[str, list[dict]] = {}
    for scope_key, row in decisions.items():
        family = "sell_put" if scope_key == "sell_put" else "covered_call"
        by_family.setdefault(family, []).append(row)
    return {
        "schema": "ai_decision_advice.v1",
        "run_id": "run-1",
        "account_ref": "acct-ref",
        "market": "US",
        "input_bindings": _bindings(),
        "strategies": [
            {
                "strategy_family": family,
                "status": "completed",
                "decisions": rows,
            }
            for family, rows in sorted(by_family.items())
        ],
    }


def _validate(payload: dict, scopes: dict):
    return validate_advice_payload(
        payload,
        scopes=scopes,
        run_id="run-1",
        account_ref="acct-ref",
        market="US",
        input_bindings=_bindings(),
    )


def test_derive_scopes_baselines_pools_and_registry_boundaries():
    _, _, scopes = _environment()
    assert set(scopes) == {"sell_put", "covered_call:NVDA", "covered_call:TSLA"}
    assert scopes["sell_put"].baseline_candidate_id == "put-1"
    assert scopes["sell_put"].allowed_candidate_ids == frozenset({"put-1", "put-2"})
    assert scopes["covered_call:NVDA"].allowed_candidate_ids == frozenset({"call-1", "call-2"})
    assert scopes["covered_call:TSLA"].baseline_candidate_id == "call-3"
    assert scopes["covered_call:NVDA"].required_coverage_refs == frozenset(
        {"coverage:AAPL", "coverage:NVDA", "coverage:TSLA"}
    )
    assert scopes["covered_call:NVDA"].allowed_external_refs == frozenset({"evidence:nvda"})


def test_invalid_or_cross_bound_registry_membership_fails_closed():
    candidates, registry, _ = _environment()
    changed = deepcopy(registry)
    fact = next(row for row in changed["facts"] if row["id"] == "candidate:put-1")
    fact["scope"] = "covered_call:NVDA"
    with pytest.raises(ValueError, match="candidate fact mismatch"):
        derive_scopes(candidates, changed)


def test_zero_candidate_flags():
    assert zero_candidate_flags({"sell_put": [], "covered_call": []}) == {
        "sell_put": True,
        "covered_call": True,
    }


def test_exact_scope_happy_path_accepts_all_keep_actions():
    _, _, scopes = _environment()
    result = _validate(_payload(scopes), scopes)
    assert result.status == "completed"
    assert result.demotions == []
    assert set(result.decisions) == set(scopes)
    assert all(row["action"] == "keep" for row in result.decisions.values())


@pytest.mark.parametrize("shape", ["missing", "duplicate", "extra"])
def test_missing_duplicate_or_extra_scope_is_incomplete_output(shape: str):
    _, _, scopes = _environment()
    payload = _payload(scopes)
    covered = next(row for row in payload["strategies"] if row["strategy_family"] == "covered_call")
    if shape == "missing":
        covered["decisions"].pop()
    elif shape == "duplicate":
        covered["decisions"].append(deepcopy(covered["decisions"][0]))
    else:
        extra = deepcopy(covered["decisions"][0])
        extra["scope_symbol"] = "MSFT"
        extra["baseline_candidate_id"] = "call-msft"
        extra["selected_candidate_id"] = "call-msft"
        covered["decisions"].append(extra)
    result = _validate(payload, scopes)
    assert result.status == "unavailable"
    assert result.error == "incomplete_output"


def test_binding_shape_and_values_are_exact():
    _, _, scopes = _environment()
    legacy = _payload(scopes)
    legacy["input_bindings"]["portfolio_context_hash"] = legacy["input_bindings"].pop("portfolio_distribution_hash")
    result = _validate(legacy, scopes)
    assert result.status == "unavailable"
    assert result.error == "input_bindings fields mismatch"

    changed = _payload(scopes)
    changed["input_bindings"]["fact_registry_hash"] = "changed"
    result = _validate(changed, scopes)
    assert result.status == "unavailable"
    assert result.error == "input_bindings.fact_registry_hash mismatch"


def test_run_account_and_market_bindings_fail_whole_output():
    _, _, scopes = _environment()
    for field, value in (
        ("run_id", "other"),
        ("account_ref", "other"),
        ("market", "HK"),
    ):
        payload = _payload(scopes)
        payload[field] = value
        assert _validate(payload, scopes).status == "unavailable"


@pytest.mark.parametrize(
    ("field", "ref", "reason"),
    [
        ("internal_fact_refs", "evidence:nvda", "invalid_internal_fact_ref_prefix"),
        ("external_evidence_refs", "candidate:put-1", "invalid_external_evidence_ref_prefix"),
        (
            "internal_fact_refs",
            "portfolio:another-account",
            "unresolved_or_out_of_scope_internal_fact_ref",
        ),
        (
            "external_evidence_refs",
            "evidence:another-account",
            "unresolved_or_out_of_scope_external_evidence_ref",
        ),
    ],
)
def test_unknown_or_wrong_namespace_ref_demotes_one_scope(field: str, ref: str, reason: str):
    _, _, scopes = _environment()
    decisions = {scope: _decision(spec) for scope, spec in scopes.items()}
    decisions["sell_put"][field].append(ref)
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.status == "completed"
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.decisions["covered_call:NVDA"]["action"] == "keep"
    assert result.demotions == [
        {
            "scope": "sell_put",
            "from_action": "keep",
            "to_action": "needs_review",
            "reason": reason,
        }
    ]


def test_cross_scope_candidate_fact_ref_demotes_covered_call_only():
    _, _, scopes = _environment()
    decisions = {scope: _decision(spec) for scope, spec in scopes.items()}
    decisions["covered_call:NVDA"]["internal_fact_refs"].append("candidate:call-3")
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["covered_call:NVDA"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == ("unresolved_or_out_of_scope_internal_fact_ref")


def test_valid_internal_risk_fact_supports_sell_put_switch():
    _, _, scopes = _environment()
    spec = scopes["sell_put"]
    switch = _decision(
        spec,
        action="switch",
        selected_candidate_id="put-2",
        internal_fact_refs=[
            "candidate:put-1",
            "candidate:put-2",
            "projection:put-2",
        ],
    )
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = switch
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "switch"
    assert result.decisions["sell_put"]["selected_candidate_id"] == "put-2"
    assert result.demotions == []


def test_completed_external_evidence_supports_switch_without_internal_risk():
    _, _, scopes = _environment()
    spec = scopes["sell_put"]
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = _decision(
        spec,
        action="switch",
        selected_candidate_id="put-2",
        internal_fact_refs=["candidate:put-1", "candidate:put-2"],
        external_evidence_refs=["evidence:aapl"],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "switch"
    assert result.demotions == []


def test_covered_call_switch_must_stay_on_same_symbol():
    _, _, scopes = _environment()
    spec = scopes["covered_call:NVDA"]
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["covered_call:NVDA"] = _decision(
        spec,
        action="switch",
        selected_candidate_id="call-3",
        internal_fact_refs=[
            "candidate:call-1",
            "candidate:call-3",
            "projection:call-1",
        ],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["covered_call:NVDA"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "switch_out_of_pool"


def test_covered_call_switch_within_same_symbol_is_accepted():
    _, _, scopes = _environment()
    spec = scopes["covered_call:NVDA"]
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["covered_call:NVDA"] = _decision(
        spec,
        action="switch",
        selected_candidate_id="call-2",
        internal_fact_refs=[
            "candidate:call-1",
            "candidate:call-2",
            "projection:call-2",
        ],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["covered_call:NVDA"]["action"] == "switch"
    assert result.decisions["covered_call:NVDA"]["selected_candidate_id"] == "call-2"
    assert result.demotions == []


def test_internal_portfolio_fact_alone_can_support_defer():
    _, _, scopes = _environment()
    spec = scopes["sell_put"]
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = _decision(
        spec,
        action="defer",
        internal_fact_refs=["portfolio:distribution"],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "defer"
    assert result.demotions == []


def test_external_coverage_gap_alone_does_not_support_defer():
    candidates, registry, scopes = _environment(external=_external(coverage="no_evidence"))
    assert candidates and registry
    spec = scopes["sell_put"]
    coverage_gap = next(ref for ref in spec.gap_refs if ref.startswith("gap:coverage:"))
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = _decision(
        spec,
        action="defer",
        internal_fact_refs=[coverage_gap],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    sell_put_demotion = next(row for row in result.demotions if row["scope"] == "sell_put")
    assert sell_put_demotion["reason"] == "defer_missing_fact_support"


def test_evidence_from_incomplete_coverage_does_not_support_defer():
    _, _, scopes = _environment(external=_external(coverage="no_evidence"))
    spec = scopes["sell_put"]
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = _decision(
        spec,
        action="defer",
        external_evidence_refs=["evidence:nvda"],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    sell_put_demotion = next(row for row in result.demotions if row["scope"] == "sell_put")
    assert sell_put_demotion["reason"] == "defer_missing_fact_support"


def test_keep_with_incomplete_coverage_demotes_to_needs_review_not_defer():
    _, _, scopes = _environment(external=_external(coverage="no_evidence"))
    result = _validate(_payload(scopes), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.decisions["sell_put"]["selected_candidate_id"] is None
    assert result.demotions[0]["reason"] == "evidence_coverage_incomplete"
    assert all(row["to_action"] == "needs_review" for row in result.demotions)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("portfolio", "portfolio_context_incomplete"),
        ("options", "option_positions_context_incomplete"),
        ("projection", "projection_incomplete"),
    ],
)
def test_context_quality_ceilings_are_enforced_by_validator(kind: str, reason: str):
    kwargs: dict = {}
    if kind == "portfolio":
        portfolio = _portfolio()
        portfolio.update(status="degraded", gaps=["portfolio_degraded"])
        portfolio["quality"]["freshness_status"] = "stale"
        kwargs["portfolio"] = portfolio
    elif kind == "options":
        options = _option_positions()
        options.update(status="unavailable", gaps=["option_positions_unavailable:ledger"])
        options["summary"]["total_open_contracts"] = None
        kwargs["option_positions"] = options
    else:
        candidates = _candidates()
        projections = _projections(candidates)
        projections["put-1"].update(
            calculation_complete=False,
            scope_ceiling="needs_review",
            gaps=["candidate_multiplier_missing"],
        )
        kwargs["projections"] = projections
    _, _, scopes = _environment(**kwargs)
    result = _validate(_payload(scopes), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert any(row["reason"] == reason for row in result.demotions)
    assert result.decisions["sell_put"]["source_refs"]["internal_fact_refs"]


def test_needs_review_accepts_specific_gap_or_internal_conflict():
    portfolio = _portfolio()
    portfolio.update(status="degraded", gaps=["portfolio_degraded"])
    portfolio["quality"]["freshness_status"] = "stale"
    _, _, scopes = _environment(portfolio=portfolio)
    spec = scopes["sell_put"]
    gap_ref = sorted(spec.gap_refs)[0]
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = _decision(
        spec,
        action="needs_review",
        internal_fact_refs=[gap_ref],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert not any(row["scope"] == "sell_put" for row in result.demotions)

    _, _, complete_scopes = _environment()
    spec = complete_scopes["sell_put"]
    decisions = {scope: _decision(item) for scope, item in complete_scopes.items()}
    decisions["sell_put"] = _decision(
        spec,
        action="needs_review",
        internal_fact_refs=["portfolio:distribution", "projection:put-1"],
    )
    result = _validate(_payload(complete_scopes, decisions), complete_scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.demotions == []


def test_unsupported_action_and_duplicate_refs_demote_without_fabricated_candidate():
    _, _, scopes = _environment()
    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"] = _decision(
        scopes["sell_put"],
        action="execute",
        selected_candidate_id="put-1",
        internal_fact_refs=["candidate:put-1"],
    )
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.decisions["sell_put"]["selected_candidate_id"] is None
    assert result.demotions[0]["reason"] == "unsupported_action"

    decisions = {scope: _decision(item) for scope, item in scopes.items()}
    decisions["sell_put"]["internal_fact_refs"].append("candidate:put-1")
    result = _validate(_payload(scopes, decisions), scopes)
    assert result.decisions["sell_put"]["action"] == "needs_review"
    assert result.demotions[0]["reason"] == "duplicate_fact_refs"
