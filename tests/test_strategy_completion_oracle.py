"""Exact real-entry regression against immutable pre-amendment synthetic output."""

import hashlib
import json
from pathlib import Path

import pytest
import strategy_completion_oracle as oracle

from domain.domain.short_vol_assessment import CANDIDATE_POSITION_SIZING_BASIS

FIXTURE = json.loads((Path(__file__).parent / "fixtures/strategy_completion_baseline.json").read_text())


@pytest.fixture(scope="module")
def actual(tmp_path_factory):
    from src.application.opening_candidate_snapshot import opening_candidate_input, ranked_opening_candidates

    original = oracle._seal_snapshot

    def legacy_view(*args, **kwargs):
        snap = original(*args, **kwargs)
        assert snap["schema_version"] == "opening_candidate_snapshot.v5"
        decisions = []
        for decision in snap["candidate_decisions"]:
            assert decision["schema_version"] == "opening_candidate_decision.v2"
            assert "normalized_input" not in decision
            decisions.append({**decision, "schema_version": "opening_candidate_decision.v1",
                              "normalized_input": opening_candidate_input(decision)})
        assert all("facts" not in row for row in snap["ranked_candidates"])
        return {**snap, "candidate_decisions": decisions,
                "ranked_candidates": [{k: v for k, v in row.items() if k != "strategy_core_status"}
                                      for row in ranked_opening_candidates(snap)]}

    # Keep the archived generator and fixture hashes intact; project only the
    # new persistence layout at its boundary before comparing all business fields.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(oracle, "_seal_snapshot", legacy_view)
        return oracle.capture(tmp_path_factory.mktemp("strategy-completion-oracle"))


def test_oracle_provenance():
    assert FIXTURE["baseline"] == "59684da956c5f56dcb1fd894887bd107907d771a"
    assert FIXTURE["synthetic"] is True
    assert FIXTURE["omitted_keys"] == sorted(oracle.OMITTED)
    assert hashlib.sha256(Path(oracle.__file__).read_bytes()).hexdigest() == FIXTURE["generator_sha256"]


@pytest.mark.parametrize("case", sorted(FIXTURE["cases"]))
def test_real_steps_match_pre_amendment_source(case, actual):
    observed = actual[case]
    clock = oracle.old.NOW.isoformat()
    for row in observed["finals"]:
        assert row["decision_now_utc"] == clock
    for collection in ("decisions", "sealed_decisions"):
        for decision in observed[collection]:
            assert decision["normalized_input"]["decision_now_utc"] == clock
            assert decision["opening_decision"]["normalized_input"]["decision_now_utc"] == clock

    signed_delta = -0.3 if case.startswith("put_") else 0.3
    observed = _checked_current_projection(observed, clock=clock, delta=signed_delta)
    expected = _historical_input_variant(FIXTURE["cases"][case], delta=signed_delta)
    # Every original business field and candidate/reject order remains compared.
    assert observed == expected


def _checked_current_projection(value, *, clock, delta):
    """Assert the new contract before projecting only fields absent in the old oracle."""
    additions = {
        "decision_now_utc": clock,
        "delta_source": "opend_market_snapshot.option_delta",
        "delta_unit": "fraction",
        "delta_reason_code": None,
        "policy_max_abs_delta": 0.35,
    }
    if isinstance(value, dict):
        value = dict(value)
        if "same_symbol_ranking_context" in value:
            context = value.pop("same_symbol_ranking_context")
            assert context == {
                "status": "unavailable", "reason": "opening_price_account_mismatch",
                "prices": [], "account": "lx", "market": "us", "symbol": value["symbol"],
                "mode": "put" if delta < 0 else "call", "currency": "USD",
                "broker": None, "as_of": None, "decision_state_fingerprint": None,
            }
        if "net_sale_price" in value:
            assert delta > 0
            assert value.pop("net_sale_price") == round(
                value["strike"] + value["net_income"] / value["multiplier"], 6)
        if "net_delivery_tier" in value:
            tier = value.pop("net_delivery_tier")
            message = "无法判断档位：持仓成本证据不足，沿用原排序"
            assert tier == {
                "mode": "put" if delta < 0 else "call", "min_step_pct": 0.02,
                "baseline_net_price": None, "threshold_net_price": None, "currency": "USD",
                "applied": False, "reason": "positions_unavailable",
                "reason_detail": "opening_price_account_mismatch", "qualifies": None, "priority": False,
                "candidate_net_price": round(value["strike"] + (-1 if delta < 0 else 1) * value["net_income"] / 100, 6),
                "price_basis": "current_limit_estimate_opening_fees_only", "message": message,
            }
            assert value["rank_reason"].startswith(message + "；")
            value["rank_reason"] = value["rank_reason"][len(message) + 1:]
        if "candidate_count" in value and "ranking" in value:
            # Summary explanations are new; verify their full tier before projecting.
            _checked_current_projection(value.pop("ranking"), clock=clock, delta=delta)
        if value.get("position_sizing_basis") is not None:
            assert value["position_sizing_basis"] == CANDIDATE_POSITION_SIZING_BASIS
        for key, expected in additions.items():
            if key in value:
                assert value[key] == expected, key
        if "delta" in value:
            assert value["delta"] == (None if value.get("candidate_count") == 0 else delta)
        if "decision_now_utc" in value:
            # Actual scan/decision facts must carry qualified current-source evidence.
            assert all(key in value for key in ("delta", "delta_source", "delta_unit", "delta_reason_code"))
        if value.get("reason") in {"risk_delta", "risk_delta_unavailable"}:
            pytest.fail("qualified synthetic 0.3 must pass the enabled default Delta gate")
        return {key: _checked_current_projection(child, clock=clock, delta=delta)
                for key, child in value.items() if key not in additions}
    if isinstance(value, list):
        return [_checked_current_projection(child, clock=clock, delta=delta) for child in value]
    return value


def _historical_input_variant(value, *, delta, mode=None):
    """Copy archived output with the approved synthetic input and sizing basis changes.

    The historical generator imported helpers whose Delta was unavailable. Current
    helpers supply signed 0.3, so the default gate stays enabled. The candidate
    sizing basis now identifies directional scenarios; these fixtures have no
    opposite-direction assignments, so all numeric sizing facts stay compared.
    Strike rejects now name their direction; the archived ``hard_strike`` is
    projected only when metric/threshold prove the violated bound. Archived bytes
    remain unchanged. Stale snapshot errors also require the new specific reason.
    """
    if isinstance(value, dict):
        if "delta" in value:
            assert value["delta"] is None
        if value.get("position_sizing_basis") is not None:
            assert value["position_sizing_basis"] == "non_option_net_assets_before_assignment_fees.v2"
        resolved_mode = (
            value.get("mode") if value.get("mode") in {"put", "call"} else mode
        )
        out = {}
        for key, child in value.items():
            if key == "delta":
                out[key] = None if value.get("candidate_count") == 0 else delta
            elif key == "position_sizing_basis" and child is not None:
                out[key] = CANDIDATE_POSITION_SIZING_BASIS
            elif key == "rejects" and resolved_mode is not None and isinstance(child, list):
                out[key] = _historical_strike_reject_variant(
                    child, mode=resolved_mode, delta=delta
                )
            else:
                out[key] = _historical_input_variant(
                    child, delta=delta, mode=resolved_mode
                )
        if (value.get("reason") == "evidence_unavailable"
                and value.get("message") == "OpenD option snapshot is stale relative to the decision moment"):
            metric = out["metric_value"]
            out["metric_value"] = {**metric, "metric_value": {
                **metric["metric_value"], "reason_code": "option_snapshot_stale",
            }}
        return out
    if isinstance(value, list):
        return [_historical_input_variant(child, delta=delta, mode=mode) for child in value]
    return value


def _historical_strike_reject_variant(rejects, *, mode, delta):
    out = []
    for reject in rejects:
        metric = reject.get("metric_value") if isinstance(reject, dict) else None
        threshold = reject.get("threshold") if isinstance(reject, dict) else None
        if (
            isinstance(reject, dict)
            and reject.get("reason") == "hard_strike"
            and isinstance(metric, (int, float))
            and not isinstance(metric, bool)
            and isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
        ):
            direction = "above_max" if metric > threshold else "below_min"
            out.append({**reject, "reason": f"{mode}_strike_{direction}"})
        else:
            out.append(_historical_input_variant(reject, delta=delta, mode=mode))
    return out


@pytest.mark.parametrize("change", [
    {"delta": 0.3001}, {"delta_source": None}, {"delta_unit": "percent"},
    {"delta_reason_code": "delta_source_unverified"}, {"policy_max_abs_delta": None},
    {"policy_max_abs_delta": 0.3}, {"policy_max_abs_delta": 0.4},
    {"position_sizing_basis": "non_option_net_assets_before_assignment_fees.v2"},
    {"same_symbol_ranking_context": {"status": "available"}},
    {"net_delivery_tier": {"applied": True}},
])
def test_oracle_projection_does_not_hide_contract_regressions(change):
    row = {"symbol": "NVDA", "strike": 100, "net_income": 200,
           "delta": 0.3, "delta_source": "opend_market_snapshot.option_delta",
           "delta_unit": "fraction", "delta_reason_code": None, "policy_max_abs_delta": 0.35,
           "decision_now_utc": oracle.old.NOW.isoformat(), **change}
    with pytest.raises(AssertionError):
        _checked_current_projection(row, clock=oracle.old.NOW.isoformat(), delta=0.3)


def test_oracle_strike_reason_projection_requires_provable_direction():
    below = _historical_input_variant(
        {"mode": "call", "rejects": [{"reason": "hard_strike", "metric_value": 83.0, "threshold": 104.0}]},
        delta=0.3,
    )
    assert below["rejects"][0]["reason"] == "call_strike_below_min"
    above = _historical_input_variant(
        {"mode": "put", "rejects": [{"reason": "hard_strike", "metric_value": 110.0, "threshold": 100.0}]},
        delta=0.3,
    )
    assert above["rejects"][0]["reason"] == "put_strike_above_max"
    unavailable = _historical_input_variant(
        {"mode": "put", "rejects": [{"reason": "hard_strike", "metric_value": None, "threshold": 100.0}]},
        delta=0.3,
    )
    assert unavailable["rejects"][0]["reason"] == "hard_strike"



@pytest.mark.parametrize(
    "mode,field",
    [
        ("put", "period_net_return_on_cash_basis"),
        ("call", "period_net_premium_return"),
    ],
)
def test_baseline_preserves_input_dependent_remainder_order(mode, field):
    from domain.domain.engine.candidate_engine import rank_candidate_rows

    rows = [
        {"symbol": symbol, "contract_symbol": f"{symbol}-{number}", field: value}
        for symbol, number, value in (
            ("NVDA", 1, 0.04),
            ("NVDA", 2, 0.01),
            ("AMD", 1, 0.08),
            ("AMD", 2, 0.02),
        )
    ]
    once = rank_candidate_rows(rows, mode=mode)
    twice = rank_candidate_rows(once, mode=mode)
    assert [r["contract_symbol"] for r in once] == ["AMD-1", "NVDA-1", "NVDA-2", "AMD-2"]
    assert [r["contract_symbol"] for r in twice] == ["AMD-1", "NVDA-1", "AMD-2", "NVDA-2"]


def test_real_capacity_paths_use_shared_core_owner(tmp_path, monkeypatch):
    from domain.domain import strategy_core

    original = strategy_core.calculate_capacity
    calls = []

    def tracked(row, **kwargs):
        calls.append((kwargs["mode"], type(kwargs["capacity_inputs"])))
        return original(row, **kwargs)

    monkeypatch.setattr(strategy_core, "calculate_capacity", tracked)
    oracle.capture(tmp_path)
    assert ("put", strategy_core.PutCapacityInputs) in calls
    assert ("call", strategy_core.CallCapacityInputs) in calls
