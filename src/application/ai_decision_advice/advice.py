from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.application.ai_decision_advice.advice_store import (
    advice_id_for,
    advice_records_path,
    append_advice_record,
    build_reuse_record,
    find_reusable_completed,
    prompt_fingerprint_for,
)
from src.application.ai_decision_advice.collector import ModelCallResult
from src.application.ai_decision_advice.config import (
    ADVICE_ACCOUNT_BUDGET_SECONDS,
    MODEL,
    PROVIDER,
)
from src.application.ai_decision_advice.contexts import FrozenInputs
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_ADVICE,
    CompiledPromptPack,
    compile_prompt_pack,
    prompt_audit_payload,
)
from src.application.ai_decision_advice.validation import (
    SCHEMA_NAME,
    derive_scopes,
    validate_advice_payload,
    zero_candidate_flags,
)


AdviceModelRunner = Callable[[str, dict[str, Any], dict[str, Any] | None, int], ModelCallResult]

ADVICE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "run_id", "account_ref", "market", "input_bindings", "strategies"],
    "properties": {
        "schema": {"type": "string", "const": SCHEMA_NAME},
        "run_id": {"type": "string"},
        "account_ref": {"type": "string"},
        "market": {"type": "string"},
        "input_bindings": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "candidate_snapshot_hash",
                "portfolio_context_hash",
                "option_positions_hash",
                "external_evidence_hash",
                "external_evidence_run_id",
            ],
            "properties": {
                "candidate_snapshot_hash": {"type": "string"},
                "portfolio_context_hash": {"type": "string"},
                "option_positions_hash": {"type": "string"},
                "external_evidence_hash": {"type": "string"},
                "external_evidence_run_id": {"type": ["string", "null"]},
            },
        },
        "strategies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["strategy_family", "status", "decisions"],
                "properties": {
                    "strategy_family": {"type": "string", "enum": ["sell_put", "covered_call"]},
                    "status": {"type": "string", "enum": ["completed"]},
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "scope_symbol",
                                "baseline_candidate_id",
                                "action",
                                "selected_candidate_id",
                                "rationale",
                                "internal_fact_refs",
                                "external_evidence_refs",
                            ],
                            "properties": {
                                "scope_symbol": {"type": ["string", "null"]},
                                "baseline_candidate_id": {"type": "string"},
                                "action": {
                                    "type": "string",
                                    "enum": ["keep", "switch", "defer", "needs_review"],
                                },
                                "selected_candidate_id": {"type": ["string", "null"]},
                                "rationale": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "risk_mechanism",
                                        "candidate_effect",
                                        "decision_reason",
                                    ],
                                    "properties": {
                                        "risk_mechanism": {"type": "string"},
                                        "candidate_effect": {"type": "string"},
                                        "decision_reason": {"type": "string"},
                                    },
                                },
                                "internal_fact_refs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "external_evidence_refs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class AdviceRunResult:
    """Outcome surfaced to the tick orchestration (plan S6 contract)."""

    status: str  # completed | unavailable | not_applicable
    unavailable_reason: str | None = None
    evidence_as_of: str | None = None
    sell_put: dict[str, Any] | None = None
    covered_call: list[dict[str, Any]] | None = None
    zero_candidate: dict[str, bool] = field(
        default_factory=lambda: {"sell_put": False, "covered_call": False}
    )
    reused: bool = False
    advice_record_id: str | None = None

    def to_brief_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "evidence_as_of": self.evidence_as_of,
            "sell_put": self.sell_put,
            "covered_call": self.covered_call,
            "zero_candidate": dict(self.zero_candidate),
            "reused": self.reused,
            "advice_record_id": self.advice_record_id,
        }

    def to_evidence_index_view(self, frozen: FrozenInputs | None = None) -> dict[str, Any] | None:
        """Frozen external evidence rows for receipt source rendering (15.4)."""

        if frozen is None:
            return None
        return {
            "frozen_at": frozen.external_evidence.get("frozen_at"),
            "symbols": frozen.external_evidence.get("symbols"),
        }


def build_advice_model_input(
    frozen: FrozenInputs,
    *,
    run_id: str,
    account_ref: str,
    market: str,
) -> dict[str, Any]:
    """JSON data input for the advice call (never interpolated into prompts)."""

    return {
        "run_id": run_id,
        "account_ref": account_ref,
        "market": str(market or "").strip().upper(),
        "input_bindings": frozen.input_bindings(),
        "candidates": frozen.candidates,
        "portfolio": frozen.portfolio,
        "option_positions": frozen.option_positions,
        "external_evidence": frozen.external_evidence,
    }


def run_decision_advice(
    *,
    output_root: Path,
    run_id: str,
    account: str,
    market: str,
    frozen: FrozenInputs,
    model_runner: AdviceModelRunner | None,
    budget_seconds: int = ADVICE_ACCOUNT_BUDGET_SECONDS,
    context_complete: bool = True,
    now: datetime | None = None,
    monotonic: Callable[[], float] | None = None,
    compiled_prompt: CompiledPromptPack | None = None,
) -> AdviceRunResult:
    """Run or reuse one account's AI Decision Advice (docs 9, 12.3, 13).

    - Legal zero candidates short-circuit before any model call (docs 9.8).
    - Reuse requires equal semantic input hashes and identical prompt / model /
      schema versions (docs 13.2).
    - The model call plus at most one structure repair share the account
      budget; timeout or invalid output degrades to ``unavailable`` without
      blocking receipts.
    """

    recorded_at = (now or datetime.now(timezone.utc)).isoformat()
    clock = monotonic or time.monotonic
    deadline = clock() + float(budget_seconds)
    account_ref = hashlib.sha256(
        f"{run_id}:{account}".encode("utf-8")
    ).hexdigest()[:12]
    evidence_as_of = str(frozen.external_evidence.get("frozen_at") or "") or None
    bindings = frozen.input_bindings()
    flags = zero_candidate_flags(frozen.candidates)
    scopes = derive_scopes(frozen.candidates, frozen.external_evidence)
    compiled = compiled_prompt or compile_prompt_pack(PROMPT_PACK_ADVICE)
    prompt_fingerprint = prompt_fingerprint_for(compiled)
    versions = {
        "provider": PROVIDER,
        "model": MODEL,
        "schema_name": SCHEMA_NAME,
        "prompt_fingerprint": prompt_fingerprint,
        "prompt": prompt_audit_payload(compiled),
    }
    record_path = advice_records_path(Path(output_root) / "output_runs" / run_id, account)
    advice_id = advice_id_for(run_id, account_ref, recorded_at)

    def persist(record: dict[str, Any]) -> None:
        append_advice_record(record_path, record)

    base_record = {
        "kind": "advice_record",
        "schema": SCHEMA_NAME,
        "advice_id": advice_id,
        "run_id": run_id,
        "account_ref": account_ref,
        "market": str(market or "").strip().upper(),
        "recorded_at": recorded_at,
        "input_bindings": dict(bindings),
        "versions": versions,
        "zero_candidate": dict(flags),
    }

    def result_for(record: Mapping[str, Any], *, reused: bool) -> AdviceRunResult:
        decisions = record.get("decisions") or {}
        # None = legal zero candidates (docs 9.8); an empty covered_call list
        # means the family had no candidates while sell_put may still exist.
        sell_put = None if flags["sell_put"] else decisions.get("sell_put")
        covered_call = [
            decisions[key]
            for key in sorted(decisions)
            if str(key).startswith("covered_call:")
        ]
        if flags["covered_call"]:
            covered_call = None
        return AdviceRunResult(
            status=str(record.get("status") or "unavailable"),
            unavailable_reason=record.get("unavailable_reason"),
            evidence_as_of=evidence_as_of,
            sell_put=sell_put,
            covered_call=covered_call,
            zero_candidate=dict(flags),
            reused=reused,
            advice_record_id=str(record.get("advice_id") or "") or None,
        )

    if not scopes:
        # Legal zero candidates: no model call, no action, deterministic
        # display only (docs 9.8).
        record = {
            **base_record,
            "status": "not_applicable",
            "unavailable_reason": "zero_candidate",
            "reused": False,
            "decisions": {},
            "demotions": [],
        }
        persist(record)
        return result_for(record, reused=False)

    prior = find_reusable_completed(
        Path(output_root),
        account=account,
        bindings=bindings,
        prompt_fingerprint=prompt_fingerprint,
    )
    if prior is not None:
        record = build_reuse_record(
            prior,
            advice_id=advice_id,
            run_id=run_id,
            account_ref=account_ref,
            recorded_at=recorded_at,
            bindings=bindings,
        )
        persist(record)
        return result_for(record, reused=True)

    if model_runner is None:
        record = {
            **base_record,
            "status": "unavailable",
            "unavailable_reason": "provider_not_configured",
            "reused": False,
            "decisions": {},
            "demotions": [],
        }
        persist(record)
        return result_for(record, reused=False)

    model_input = build_advice_model_input(
        frozen, run_id=run_id, account_ref=account_ref, market=market
    )
    raw_response: dict[str, Any] | None = None
    usage: dict[str, Any] = {}
    repair_attempted = False
    failure_reason: str | None = None
    validated = None
    for attempt in (1, 2):
        remaining = deadline - clock()
        if remaining <= 0:
            failure_reason = "timeout"
            break
        try:
            call = model_runner(
                compiled.prompt,
                model_input,
                ADVICE_OUTPUT_SCHEMA,
                max(1, int(remaining)),
            )
        except Exception as exc:  # provider/network failure -> unavailable
            failure_reason = f"provider_error:{type(exc).__name__}"
            break
        raw_response = call.raw_response
        usage = dict(call.usage or {})
        try:
            parsed = json.loads(call.output_text or "null")
            candidate_result = validate_advice_payload(
                parsed,
                scopes=scopes,
                run_id=run_id,
                account_ref=account_ref,
                market=market,
                input_bindings=bindings,
                context_complete=context_complete,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            failure_reason = f"invalid_output:{type(exc).__name__}"
            candidate_result = None
        if candidate_result is not None and candidate_result.status != "unavailable":
            validated = candidate_result
            break
        failure_reason = (candidate_result.error if candidate_result else failure_reason) or "invalid_output"
        if attempt == 1 and clock() < deadline:
            # One in-budget structure repair: resubmit with the error noted in
            # the data input; still no heuristic parsing (docs 10).
            repair_attempted = True
            model_input = {
                **model_input,
                "previous_output_error": failure_reason,
            }
            continue
        break

    if validated is None:
        record = {
            **base_record,
            "status": "unavailable",
            "unavailable_reason": failure_reason or "invalid_output",
            "reused": False,
            "decisions": {},
            "demotions": [],
            "repair_attempted": repair_attempted,
            "raw_response": raw_response,
            "usage": usage,
        }
        persist(record)
        return result_for(record, reused=False)

    record = {
        **base_record,
        "status": "completed",
        "unavailable_reason": None,
        "reused": False,
        "decisions": validated.decisions,
        "demotions": validated.demotions,
        "repair_attempted": repair_attempted,
        "raw_response": raw_response,
        "usage": usage,
    }
    persist(record)
    return result_for(record, reused=False)
