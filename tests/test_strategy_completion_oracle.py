"""Exact real-entry regression against immutable pre-amendment synthetic output."""

import hashlib
import json
from pathlib import Path

import pytest
import strategy_completion_oracle as oracle

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

    def without_recorded_clock(value):
        if isinstance(value, dict):
            if "decision_now_utc" in value:
                assert value["decision_now_utc"] == clock
            return {k: without_recorded_clock(v) for k, v in value.items() if k != "decision_now_utc"}
        if isinstance(value, list):
            return [without_recorded_clock(v) for v in value]
        return value

    # The immutable pre-clock oracle still compares every original business field.
    assert without_recorded_clock(observed) == FIXTURE["cases"][case]


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
