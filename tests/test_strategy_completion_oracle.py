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
        "policy_max_abs_delta": 0.4,
    }
    if isinstance(value, dict):
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


def _historical_input_variant(value, *, delta):
    """Copy archived output with the approved synthetic input and sizing basis changes.

    The historical generator imported helpers whose Delta was unavailable. Current
    helpers supply signed 0.3, so the default gate stays enabled. The candidate
    sizing basis now identifies directional scenarios; these fixtures have no
    opposite-direction assignments, so all numeric sizing facts stay compared.
    Archived bytes remain unchanged.
    """
    if isinstance(value, dict):
        if "delta" in value:
            assert value["delta"] is None
        if value.get("position_sizing_basis") is not None:
            assert value["position_sizing_basis"] == "non_option_net_assets_before_assignment_fees.v2"
        return {key: (None if value.get("candidate_count") == 0 else delta)
                if key == "delta" else CANDIDATE_POSITION_SIZING_BASIS
                if key == "position_sizing_basis" and child is not None
                else _historical_input_variant(child, delta=delta)
                for key, child in value.items()}
    if isinstance(value, list):
        return [_historical_input_variant(child, delta=delta) for child in value]
    return value


@pytest.mark.parametrize("change", [
    {"delta": 0.4001}, {"delta_source": None}, {"delta_unit": "percent"},
    {"delta_reason_code": "delta_source_unverified"}, {"policy_max_abs_delta": None},
    {"position_sizing_basis": "non_option_net_assets_before_assignment_fees.v2"},
])
def test_oracle_projection_does_not_hide_contract_regressions(change):
    row = {"delta": 0.3, "delta_source": "opend_market_snapshot.option_delta",
           "delta_unit": "fraction", "delta_reason_code": None, "policy_max_abs_delta": 0.4,
           "decision_now_utc": oracle.old.NOW.isoformat(), **change}
    with pytest.raises(AssertionError):
        _checked_current_projection(row, clock=oracle.old.NOW.isoformat(), delta=0.3)



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
