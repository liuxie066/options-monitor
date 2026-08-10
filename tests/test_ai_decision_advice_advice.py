from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import datetime, timezone

from src.application.ai_decision_advice.advice import run_decision_advice
from src.application.ai_decision_advice.advice_store import (
    bindings_match,
    read_advice_records,
)
from src.application.ai_decision_advice.collector import ModelCallResult
from src.application.ai_decision_advice.contexts import (
    FrozenInputs,
    build_fact_registry,
)
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_ADVICE,
    compile_prompt_pack,
)
from src.application.ai_decision_advice.validation import derive_scopes


NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_decision_advice_prompt_uses_account_level_advisor_role():
    prompt = compile_prompt_pack(PROMPT_PACK_ADVICE).prompt

    assert "账户级期权决策顾问" in prompt
    assert "你不是策略引擎，也不是交易执行器" in prompt
    assert "最终决策由用户作出" in prompt
    assert "期权开仓决策建议器" not in prompt


def _frozen(
    *,
    sell_put=None,
    covered_call=None,
    coverage: str = "completed",
) -> FrozenInputs:
    sell_put = [{"candidate_id": "put-1", "rank": 1, "symbol": "NVDA"}] if sell_put is None else sell_put
    covered_call = [{"candidate_id": "call-1", "rank": 1, "symbol": "NVDA"}] if covered_call is None else covered_call
    candidates = {
        "market": "US",
        "sell_put": sell_put,
        "covered_call": covered_call,
    }
    portfolio = {
        "status": "ready",
        "quality": {
            "freshness_status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-08-09T10:00:00+00:00",
        },
        "asset_weights": {"NVDA": 0.7},
        "currency_weights": {"USD": 1.0},
        "cash_and_mmf_weight": 0.3,
        "gaps": [],
    }
    option_positions = {
        "status": "ready",
        "source_observed_at": "2026-08-09T10:00:00+00:00",
        "summary": {
            "total_open_contracts": 0,
            "by_direction_and_type": [],
            "by_expiry": [],
        },
        "candidate_contracts": [],
        "verified_structures": [],
        "gaps": [],
    }
    projections = {
        row["candidate_id"]: {
            "candidate_id": row["candidate_id"],
            "symbol": row["symbol"],
            "strategy_mode": mode,
            "calculation_complete": True,
            "scope_ceiling": None,
            "gaps": [],
        }
        for rows, mode in ((sell_put, "put"), (covered_call, "call"))
        for row in rows
    }
    symbols = sorted({row["symbol"] for row in sell_put} | {row["symbol"] for row in covered_call})
    external = {
        "evidence_as_of": "2026-08-09T11:00:00+00:00",
        "frozen_at": "2026-08-09T11:00:00+00:00",
        "index_hash": "ev-hash",
        "symbols": [
            {
                "symbol": symbol,
                "coverage_ref": f"coverage:{symbol}",
                "coverage": coverage,
                "unavailable_reason": None if coverage == "completed" else "no_evidence",
                "last_checked_at": "2026-08-09T11:00:00+00:00",
                "last_success_at": "2026-08-09T11:00:00+00:00",
                "evidence": [],
            }
            for symbol in symbols
        ],
    }
    fact_registry = build_fact_registry(
        candidates=candidates,
        portfolio=portfolio,
        option_positions=option_positions,
        projections=projections,
        external_evidence=external,
    )
    return FrozenInputs(
        candidates=candidates,
        portfolio=portfolio,
        option_positions=option_positions,
        external_evidence=external,
        candidate_snapshot_hash="c-hash",
        portfolio_context_hash="p-hash",
        option_positions_hash="o-hash",
        external_evidence_hash="ev-hash",
        external_evidence_run_id="er-1",
        projections=projections,
        fact_registry=fact_registry,
        portfolio_distribution_hash="p-hash",
        projection_hash="projection-hash",
        fact_registry_hash="fact-hash",
    )


def _model_output(
    frozen: FrozenInputs,
    *,
    run_id: str,
    account_ref: str,
    omit_scope: str | None = None,
) -> str:
    scopes = derive_scopes(frozen.candidates, frozen.fact_registry)
    by_family: dict[str, list[dict]] = {}
    for scope_key, spec in scopes.items():
        if scope_key == omit_scope:
            continue
        by_family.setdefault(spec.strategy_family, []).append(
            {
                "scope_symbol": spec.symbol,
                "baseline_candidate_id": spec.baseline_candidate_id,
                "action": "keep",
                "selected_candidate_id": spec.baseline_candidate_id,
                "rationale": {
                    "risk_mechanism": "no material incremental risk",
                    "candidate_effect": "baseline remains suitable",
                    "decision_reason": "keep candidate engine rank one",
                },
                "internal_fact_refs": [
                    spec.candidate_fact_refs[spec.baseline_candidate_id],
                    spec.projection_fact_refs[spec.baseline_candidate_id],
                    *sorted(spec.required_coverage_refs),
                ],
                "external_evidence_refs": [],
            }
        )
    return json.dumps(
        {
            "schema": "ai_decision_advice.v1",
            "run_id": run_id,
            "account_ref": account_ref,
            "market": "US",
            "input_bindings": frozen.input_bindings(),
            "strategies": [
                {
                    "strategy_family": family,
                    "status": "completed",
                    "decisions": rows,
                }
                for family, rows in sorted(by_family.items())
            ],
        },
        ensure_ascii=False,
    )


def _runner_for(frozen: FrozenInputs, *, calls: list | None = None):
    def runner(instructions, payload, schema, timeout):
        if calls is not None:
            calls.append(
                {
                    "instructions": instructions,
                    "payload": payload,
                    "schema": schema,
                    "timeout": timeout,
                }
            )
        return ModelCallResult(
            output_text=_model_output(
                frozen,
                run_id=payload["run_id"],
                account_ref=payload["account_ref"],
            ),
            usage={"total_tokens": 10, "provider_private": "must-not-persist"},
            response_sha256="a" * 64,
        )

    return runner


def _record_path(tmp_path, run_id, account="lx"):
    return tmp_path / "output_runs" / run_id / "accounts" / account / "state" / "ai_decision_advice.jsonl"


def test_completed_run_uses_new_model_input_and_persists_private_record(tmp_path, monkeypatch):
    import src.application.ai_decision_advice.advice as advice_module

    monkeypatch.setattr(advice_module.secrets, "token_urlsafe", lambda _: "new-ref")
    frozen = _frozen()
    calls: list = []
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, calls=calls),
        now=NOW,
    )
    assert result.status == "completed"
    assert result.reused is False
    assert result.sell_put["action"] == "keep"
    assert result.covered_call[0]["symbol"] == "NVDA"
    assert result.evidence_as_of == "2026-08-09T11:00:00+00:00"

    model_input = calls[0]["payload"]
    assert model_input["account_ref"] == "new-ref"
    assert {
        "portfolio_distribution",
        "projections",
        "fact_registry",
    } <= set(model_input)
    assert "portfolio" not in model_input
    assert "portfolio_context_hash" not in model_input["input_bindings"]
    assert calls[0]["schema"]["properties"]["input_bindings"]["required"] == [
        "candidate_snapshot_hash",
        "portfolio_distribution_hash",
        "option_positions_hash",
        "fact_registry_hash",
        "external_evidence_hash",
        "external_evidence_run_id",
    ]

    records = read_advice_records(_record_path(tmp_path, "run-1"))
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "completed"
    assert record["account_ref"] == "new-ref"
    assert record["evidence_as_of"] == "2026-08-09T11:00:00+00:00"
    assert record["input_bindings"]["fact_registry_hash"] == "fact-hash"
    assert "raw_response" not in record
    assert record["model_response_audit"]["response_sha256"] == "a" * 64
    assert record["usage"] == {"total_tokens": 10}
    assert "must-not-persist" not in json.dumps(record)
    record_path = _record_path(tmp_path, "run-1")
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
        model_runner=_runner_for(frozen, calls=calls),
        now=NOW,
    )
    assert calls == []
    assert result.status == "not_applicable"
    assert result.unavailable_reason == "zero_candidate"
    assert result.zero_candidate == {"sell_put": True, "covered_call": True}
    assert result.sell_put is None
    assert result.covered_call is None


def test_invalid_fact_registry_fails_closed_without_model_call(tmp_path):
    frozen = _frozen()
    invalid = replace(frozen, fact_registry={"schema_version": "bad", "facts": []})
    calls: list = []
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=invalid,
        model_runner=_runner_for(invalid, calls=calls),
        now=NOW,
    )
    assert calls == []
    assert result.status == "unavailable"
    assert result.unavailable_reason == "fact_registry_invalid"


def test_legacy_only_or_missing_new_binding_fails_before_model_call(tmp_path):
    frozen = _frozen()
    for index, invalid in enumerate(
        (
            replace(frozen, portfolio_distribution_hash=""),
            replace(frozen, fact_registry_hash=""),
        ),
        start=1,
    ):
        calls: list = []
        result = run_decision_advice(
            output_root=tmp_path,
            run_id=f"legacy-run-{index}",
            account="lx",
            market="us",
            frozen=invalid,
            model_runner=_runner_for(invalid, calls=calls),
            now=NOW,
        )
        assert calls == []
        assert result.status == "unavailable"
        assert result.unavailable_reason == "input_bindings_invalid"


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


def test_incomplete_scope_then_successful_repair_uses_same_budget(tmp_path, monkeypatch):
    import src.application.ai_decision_advice.advice as advice_module

    monkeypatch.setattr(advice_module.secrets, "token_urlsafe", lambda _: "new-ref")
    frozen = _frozen()
    calls: list = []
    outputs = iter(
        [
            _model_output(
                frozen,
                run_id="run-1",
                account_ref="new-ref",
                omit_scope="covered_call:NVDA",
            ),
            _model_output(frozen, run_id="run-1", account_ref="new-ref"),
        ]
    )
    ticks = iter([0.0, 1.0, 11.0])

    def runner(instructions, payload, schema, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        return ModelCallResult(output_text=next(outputs), usage={})

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=runner,
        budget_seconds=30,
        monotonic=lambda: next(ticks),
        now=NOW,
    )
    assert result.status == "completed"
    assert [call["timeout"] for call in calls] == [29, 19]
    assert calls[1]["payload"]["previous_output_error"] == "incomplete_output"
    record = read_advice_records(_record_path(tmp_path, "run-1"))[0]
    assert record["repair_attempted"] is True


def test_expired_shared_budget_does_not_claim_a_repair_call(tmp_path, monkeypatch):
    import src.application.ai_decision_advice.advice as advice_module

    monkeypatch.setattr(advice_module.secrets, "token_urlsafe", lambda _: "new-ref")
    frozen = _frozen()
    calls: list = []
    ticks = iter([0.0, 1.0, 30.0])

    def runner(instructions, payload, schema, timeout):
        calls.append(payload)
        return ModelCallResult(
            output_text=_model_output(
                frozen,
                run_id="run-1",
                account_ref="new-ref",
                omit_scope="covered_call:NVDA",
            ),
            usage={},
        )

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=runner,
        budget_seconds=30,
        monotonic=lambda: next(ticks),
        now=NOW,
    )
    assert result.status == "unavailable"
    assert result.unavailable_reason == "timeout"
    assert len(calls) == 1
    record = read_advice_records(_record_path(tmp_path, "run-1"))[0]
    assert record["repair_attempted"] is False


def test_incomplete_scope_after_one_repair_is_unavailable(tmp_path, monkeypatch):
    import src.application.ai_decision_advice.advice as advice_module

    monkeypatch.setattr(advice_module.secrets, "token_urlsafe", lambda _: "new-ref")
    frozen = _frozen()

    def runner(instructions, payload, schema, timeout):
        return ModelCallResult(
            output_text=_model_output(
                frozen,
                run_id="run-1",
                account_ref="new-ref",
                omit_scope="covered_call:NVDA",
            ),
            usage={},
        )

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
    assert result.unavailable_reason == "incomplete_output"
    record = read_advice_records(_record_path(tmp_path, "run-1"))[0]
    assert record["repair_attempted"] is True


def test_invalid_json_twice_is_unavailable(tmp_path):
    def runner(instructions, payload, schema, timeout):
        return ModelCallResult(output_text="{broken", usage={})

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
    assert result.unavailable_reason == "invalid_output"


def test_provider_exception_and_budget_timeout_are_unavailable(tmp_path):
    def failing_runner(instructions, payload, schema, timeout):
        raise TimeoutError("slow")

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="provider-run",
        account="lx",
        market="us",
        frozen=_frozen(),
        model_runner=failing_runner,
        now=NOW,
    )
    assert result.unavailable_reason == "provider_error:TimeoutError"

    ticks = iter([0.0, 100.0])

    def forbidden_runner(instructions, payload, schema, timeout):  # pragma: no cover
        raise AssertionError("must not be called")

    result = run_decision_advice(
        output_root=tmp_path,
        run_id="timeout-run",
        account="lx",
        market="us",
        frozen=_frozen(),
        model_runner=forbidden_runner,
        now=NOW,
        monotonic=lambda: next(ticks),
    )
    assert result.unavailable_reason == "timeout"


def test_reuse_requires_semantic_bindings_and_creates_new_anonymous_ref(tmp_path, monkeypatch):
    import src.application.ai_decision_advice.advice as advice_module

    refs = iter(["first-private-ref", "second-private-ref"])
    monkeypatch.setattr(advice_module.secrets, "token_urlsafe", lambda _: next(refs))
    frozen = _frozen()
    first = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen),
        now=NOW,
    )
    assert first.status == "completed"

    refreshed = replace(frozen, external_evidence_run_id="er-2")

    def forbidden_runner(instructions, payload, schema, timeout):  # pragma: no cover
        raise AssertionError("reuse must not call the model")

    second = run_decision_advice(
        output_root=tmp_path,
        run_id="run-2",
        account="lx",
        market="us",
        frozen=refreshed,
        model_runner=forbidden_runner,
        now=NOW,
    )
    assert second.status == "completed"
    assert second.reused is True
    records = read_advice_records(_record_path(tmp_path, "run-2"))
    assert records[0]["account_ref"] == "second-private-ref"
    assert records[0]["input_bindings"]["external_evidence_run_id"] == "er-2"
    assert records[0]["evidence_as_of"] == "2026-08-09T11:00:00+00:00"
    assert records[0]["reuse_of_advice_id"] == first.advice_record_id
    assert "first-private-ref" not in json.dumps(records[0])


def test_legacy_binding_shape_never_matches_for_reuse():
    frozen = _frozen()
    legacy = dict(frozen.input_bindings())
    legacy["portfolio_context_hash"] = legacy.pop("portfolio_distribution_hash")
    assert bindings_match({"input_bindings": legacy}, frozen.input_bindings()) is False


def test_fact_registry_change_prevents_reuse(tmp_path):
    frozen = _frozen()
    run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen),
        now=NOW,
    )
    changed = replace(frozen, fact_registry_hash="different")
    calls: list = []
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-2",
        account="lx",
        market="us",
        frozen=changed,
        model_runner=_runner_for(changed, calls=calls),
        now=NOW,
    )
    assert result.reused is False
    assert len(calls) == 1


def test_prompt_version_change_prevents_reuse(tmp_path, monkeypatch):
    frozen = _frozen()
    run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen),
        now=NOW,
    )

    import src.application.ai_decision_advice.prompts as prompts_module

    monkeypatch.setattr(
        prompts_module,
        "PROMPT_VERSION",
        "ai_decision_advice.prompts.v2",
    )
    import src.application.ai_decision_advice.advice as advice_module

    monkeypatch.setattr(
        advice_module,
        "compile_prompt_pack",
        prompts_module.compile_prompt_pack,
    )
    calls: list = []
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-2",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen, calls=calls),
        now=NOW,
    )
    assert result.reused is False
    assert len(calls) == 1


def test_keep_with_unavailable_evidence_becomes_needs_review(tmp_path):
    frozen = _frozen(coverage="no_evidence")
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen),
        now=NOW,
    )
    assert result.status == "completed"
    assert result.sell_put["action"] == "needs_review"
    assert result.sell_put["selected_candidate_id"] is None
    record = read_advice_records(_record_path(tmp_path, "run-1"))[0]
    assert record["demotions"][0]["reason"] == "evidence_coverage_incomplete"


def test_legal_zero_covered_call_family_returns_none(tmp_path):
    frozen = _frozen(covered_call=[])
    result = run_decision_advice(
        output_root=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        frozen=frozen,
        model_runner=_runner_for(frozen),
        now=NOW,
    )
    assert result.status == "completed"
    assert result.covered_call is None
    assert result.sell_put is not None
    assert result.zero_candidate == {"sell_put": False, "covered_call": True}
