from __future__ import annotations

import json
import stat
from datetime import datetime, timezone

from src.application.ai_decision_advice.advice import (
    run_decision_advice,
)
from src.application.ai_decision_advice.advice_store import read_advice_records
from src.application.ai_decision_advice.collector import ModelCallResult
from src.application.ai_decision_advice.contexts import FrozenInputs


NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _frozen(*, sell_put=None, covered_call=None, coverage="completed") -> FrozenInputs:
    sell_put = [{"candidate_id": "put-1", "rank": 1, "symbol": "NVDA"}] if sell_put is None else sell_put
    covered_call = (
        [{"candidate_id": "call-1", "rank": 1, "symbol": "NVDA"}]
        if covered_call is None
        else covered_call
    )
    candidates = {"market": "US", "sell_put": sell_put, "covered_call": covered_call}
    symbols = sorted(
        {row["symbol"] for row in sell_put} | {row["symbol"] for row in covered_call}
    )
    external = {
        "frozen_at": "2026-08-09T11:00:00+00:00",
        "index_hash": "ev-hash",
        "symbols": [{"symbol": symbol, "coverage": coverage, "evidence": []} for symbol in symbols],
    }
    return FrozenInputs(
        candidates=candidates,
        portfolio={"symbol_weights": [], "cash_currencies": ["USD"]},
        option_positions={"open_positions": []},
        external_evidence=external,
        candidate_snapshot_hash="c-hash",
        portfolio_context_hash="p-hash",
        option_positions_hash="o-hash",
        external_evidence_hash="ev-hash",
        external_evidence_run_id="er-1",
    )


def _model_output(frozen: FrozenInputs, *, run_id: str, account_ref: str, action="keep") -> str:
    decisions = []
    if frozen.candidates["sell_put"]:
        baseline = frozen.candidates["sell_put"][0]["candidate_id"]
        decisions.append(
            (
                "sell_put",
                {
                    "scope_symbol": None,
                    "baseline_candidate_id": baseline,
                    "action": action,
                    "selected_candidate_id": baseline if action in {"keep", "switch"} else None,
                    "rationale": {
                        "risk_mechanism": "m",
                        "candidate_effect": "e",
                        "decision_reason": "r",
                    },
                    "internal_fact_refs": [],
                    "external_evidence_refs": [],
                },
            )
        )
    if frozen.candidates["covered_call"]:
        baseline = frozen.candidates["covered_call"][0]["candidate_id"]
        decisions.append(
            (
                "covered_call",
                {
                    "scope_symbol": "NVDA",
                    "baseline_candidate_id": baseline,
                    "action": "keep",
                    "selected_candidate_id": baseline,
                    "rationale": {
                        "risk_mechanism": "m",
                        "candidate_effect": "e",
                        "decision_reason": "r",
                    },
                    "internal_fact_refs": [],
                    "external_evidence_refs": [],
                },
            )
        )
    strategies: dict[str, list] = {}
    for family, decision in decisions:
        strategies.setdefault(family, []).append(decision)
    return json.dumps(
        {
            "schema": "ai_decision_advice.v1",
            "run_id": run_id,
            "account_ref": account_ref,
            "market": "US",
            "input_bindings": frozen.input_bindings(),
            "strategies": [
                {"strategy_family": family, "status": "completed", "decisions": rows}
                for family, rows in strategies.items()
            ],
        },
        ensure_ascii=False,
    )


def _account_ref(run_id: str, account: str) -> str:
    import hashlib

    return hashlib.sha256(f"{run_id}:{account}".encode("utf-8")).hexdigest()[:12]


def _runner_for(frozen: FrozenInputs, run_id: str, account: str, *, calls: list | None = None):
    def runner(instructions, payload, schema, timeout):
        if calls is not None:
            calls.append({"payload": payload, "timeout": timeout})
        text = _model_output(frozen, run_id=run_id, account_ref=_account_ref(run_id, account))
        return ModelCallResult(
            output_text=text,
            usage={"total_tokens": 10, "provider_private": "must-not-persist"},
            response_sha256="a" * 64,
        )

    return runner


def _record_path(tmp_path, run_id, account):
    return tmp_path / "output_runs" / run_id / "accounts" / account / "state" / "ai_decision_advice.jsonl"


def test_completed_run_persists_record(tmp_path):
    frozen = _frozen()
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx"),
        now=NOW,
    )
    assert result.status == "completed"
    assert result.reused is False
    assert result.sell_put["action"] == "keep"
    assert result.sell_put["selected_candidate_id"] == "put-1"
    assert result.covered_call[0]["symbol"] == "NVDA"
    assert result.evidence_as_of == "2026-08-09T11:00:00+00:00"
    assert result.zero_candidate == {"sell_put": False, "covered_call": False}

    records = read_advice_records(_record_path(tmp_path, "run-1", "lx"))
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "completed"
    assert record["account_ref"] == _account_ref("run-1", "lx")
    assert record["versions"]["model"]
    assert record["versions"]["prompt_fingerprint"]
    assert record["input_bindings"]["candidate_snapshot_hash"] == "c-hash"
    assert "raw_response" not in record
    assert record["model_response_audit"]["response_sha256"] == "a" * 64
    assert record["model_response_audit"]["output_char_count"] > 0
    assert record["usage"] == {"total_tokens": 10}
    assert "must-not-persist" not in json.dumps(record)
    record_path = _record_path(tmp_path, "run-1", "lx")
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(record_path.parent.stat().st_mode) == 0o700


def test_zero_candidate_short_circuits_without_model_call(tmp_path):
    calls: list = []
    frozen = _frozen(sell_put=[], covered_call=[])
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx", calls=calls),
        now=NOW,
    )
    assert calls == []
    assert result.status == "not_applicable"
    assert result.unavailable_reason == "zero_candidate"
    assert result.zero_candidate == {"sell_put": True, "covered_call": True}
    assert result.sell_put is None
    assert result.covered_call is None


def test_missing_model_runner_is_unavailable(tmp_path):
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=_frozen(),
        model_runner=None,
        now=NOW,
    )
    assert result.status == "unavailable"
    assert result.unavailable_reason == "provider_not_configured"


def test_invalid_output_then_successful_repair(tmp_path):
    frozen = _frozen()
    calls: list = []
    good = _model_output(frozen, run_id="run-1", account_ref=_account_ref("run-1", "lx"))
    outputs = iter(["not-json", good])

    def runner(instructions, payload, schema, timeout):
        calls.append(payload)
        return ModelCallResult(output_text=next(outputs), usage={})

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=runner,
        now=NOW,
    )
    assert result.status == "completed"
    assert len(calls) == 2
    assert "previous_output_error" in calls[1]
    record = read_advice_records(_record_path(tmp_path, "run-1", "lx"))[0]
    assert record["repair_attempted"] is True


def test_invalid_output_twice_is_unavailable(tmp_path):
    frozen = _frozen()

    def runner(instructions, payload, schema, timeout):
        return ModelCallResult(output_text="{broken", usage={})

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=runner,
        now=NOW,
    )
    assert result.status == "unavailable"
    assert "invalid_output" in (result.unavailable_reason or "")


def test_provider_exception_is_unavailable(tmp_path):
    def runner(instructions, payload, schema, timeout):
        raise TimeoutError("slow")

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=_frozen(),
        model_runner=runner,
        now=NOW,
    )
    assert result.status == "unavailable"
    assert result.unavailable_reason == "provider_error:TimeoutError"


def test_budget_timeout_before_call_is_unavailable(tmp_path):
    ticks = iter([0.0, 100.0])

    def runner(instructions, payload, schema, timeout):  # pragma: no cover
        raise AssertionError("must not be called")

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=_frozen(),
        model_runner=runner,
        now=NOW,
        monotonic=lambda: next(ticks),
    )
    assert result.status == "unavailable"
    assert result.unavailable_reason == "timeout"


def test_reuse_when_inputs_and_versions_unchanged(tmp_path):
    frozen = _frozen()
    first = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx"),
        now=NOW,
    )
    assert first.status == "completed"

    def forbidden_runner(instructions, payload, schema, timeout):  # pragma: no cover
        raise AssertionError("reuse must not call the model")

    second = run_decision_advice(
        output_root=tmp_path,
        run_id="run-2",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=forbidden_runner,
        now=NOW,
    )
    assert second.status == "completed"
    assert second.reused is True
    records = read_advice_records(_record_path(tmp_path, "run-2", "lx"))
    assert len(records) == 1
    assert records[0]["reused"] is True
    assert records[0]["reuse_of_advice_id"] == first.advice_record_id


def test_no_reuse_when_candidate_hash_changes(tmp_path):
    frozen = _frozen()
    run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx"),
        now=NOW,
    )
    changed = FrozenInputs(
        **{**frozen.__dict__, "candidate_snapshot_hash": "different"}
    )
    calls: list = []
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-2",
        account="lx",
        market="us",
        frozen=changed,
        model_runner=_runner_for(changed, "run-2", "lx", calls=calls),
        now=NOW,
    )
    assert result.reused is False
    assert len(calls) == 1


def test_no_reuse_when_prompt_version_changes(tmp_path, monkeypatch):
    frozen = _frozen()
    run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx"),
        now=NOW,
    )

    import src.application.ai_decision_advice.prompts as prompts_module

    monkeypatch.setattr(prompts_module, "PROMPT_VERSION", "ai_decision_advice.prompts.v2")
    import src.application.ai_decision_advice.advice as advice_module

    monkeypatch.setattr(advice_module, "compile_prompt_pack", prompts_module.compile_prompt_pack)

    calls: list = []
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-2",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-2", "lx", calls=calls),
        now=NOW,
    )
    assert result.reused is False
    assert len(calls) == 1


def test_keep_demoted_to_defer_when_evidence_stale(tmp_path):
    frozen = _frozen(coverage="stale")
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx"),
        now=NOW,
    )
    assert result.status == "completed"
    assert result.sell_put["action"] == "defer"
    assert result.sell_put["selected_candidate_id"] is None
    record = read_advice_records(_record_path(tmp_path, "run-1", "lx"))[0]
    assert record["demotions"][0]["reason"] == "evidence_incomplete"


def test_zero_cc_family_returns_empty_list_not_none(tmp_path):
    # sell_put has candidates, covered_call is legally zero: the brief view
    # contract uses None for "legal zero" and [] for a present-but-empty
    # family list must never appear; here covered_call must be None and
    # sell_put a real decision (plan S6 contract).
    frozen = _frozen(covered_call=[])
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, "run-1", "lx"),
        now=NOW,
    )
    assert result.status == "completed"
    assert result.covered_call is None
    assert result.sell_put is not None
    assert result.zero_candidate == {"sell_put": False, "covered_call": True}
