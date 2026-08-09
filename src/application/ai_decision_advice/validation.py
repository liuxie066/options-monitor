from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.application.ai_decision_advice.evidence_store import COVERAGE_COMPLETED


SCHEMA_NAME = "ai_decision_advice.v1"

ACTIONS = frozenset({"keep", "switch", "defer", "needs_review"})

_RATIONALE_KEYS = ("risk_mechanism", "candidate_effect", "decision_reason")


@dataclass(frozen=True)
class ScopeSpec:
    """One decision scope derived from the frozen candidate snapshot."""

    scope_key: str
    strategy_family: str
    symbol: str | None
    baseline_candidate_id: str
    allowed_candidate_ids: frozenset[str]
    symbol_evidence_complete: bool


@dataclass
class ValidationResult:
    status: str
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    demotions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def derive_scopes(
    candidates: Mapping[str, Any],
    external_evidence: Mapping[str, Any],
) -> dict[str, ScopeSpec]:
    """Derive decision scopes and allowed candidate pools (docs 9.2).

    Sell Put: one scope over the shared-cash pool. Covered Call: one scope per
    underlying symbol. Baselines are the Candidate Engine rank-1 candidates.
    ``symbol_evidence_complete`` is True when every symbol in the scope pool has
    completed evidence coverage.
    """

    coverage_by_symbol: dict[str, str] = {}
    for row in external_evidence.get("symbols") or []:
        if isinstance(row, Mapping):
            coverage_by_symbol[str(row.get("symbol") or "")] = str(row.get("coverage") or "")

    def complete(rows: list[Mapping[str, Any]]) -> bool:
        return all(
            coverage_by_symbol.get(str(row.get("symbol") or "")) == COVERAGE_COMPLETED
            for row in rows
        )

    scopes: dict[str, ScopeSpec] = {}
    sell_put_rows = [row for row in candidates.get("sell_put") or [] if isinstance(row, Mapping)]
    if sell_put_rows:
        scopes["sell_put"] = ScopeSpec(
            scope_key="sell_put",
            strategy_family="sell_put",
            symbol=None,
            baseline_candidate_id=str(sell_put_rows[0].get("candidate_id") or ""),
            allowed_candidate_ids=frozenset(str(row.get("candidate_id") or "") for row in sell_put_rows),
            symbol_evidence_complete=complete(sell_put_rows),
        )
    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in candidates.get("covered_call") or []:
        if isinstance(row, Mapping):
            by_symbol.setdefault(str(row.get("symbol") or ""), []).append(row)
    for symbol, rows in sorted(by_symbol.items()):
        scopes[f"covered_call:{symbol}"] = ScopeSpec(
            scope_key=f"covered_call:{symbol}",
            strategy_family="covered_call",
            symbol=symbol,
            baseline_candidate_id=str(rows[0].get("candidate_id") or ""),
            allowed_candidate_ids=frozenset(str(row.get("candidate_id") or "") for row in rows),
            symbol_evidence_complete=complete(rows),
        )
    return scopes


def zero_candidate_flags(candidates: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "sell_put": not bool(candidates.get("sell_put")),
        "covered_call": not bool(candidates.get("covered_call")),
    }


def validate_advice_payload(
    payload: Any,
    *,
    scopes: Mapping[str, ScopeSpec],
    run_id: str,
    account_ref: str,
    market: str,
    input_bindings: Mapping[str, Any],
    context_complete: bool,
) -> ValidationResult:
    """Validate the model output and apply demotion rules (docs 9.7, 10).

    Structural or binding mismatches fail the whole output (``unavailable``;
    at most one in-budget structure repair is attempted by the caller).
    Semantic problems demote individual decisions to ``needs_review`` or
    ``defer`` instead of fabricating actions.
    """

    if not isinstance(payload, dict):
        return ValidationResult(status="unavailable", error="output must be a JSON object")
    if str(payload.get("schema") or "") != SCHEMA_NAME:
        return ValidationResult(status="unavailable", error=f"schema must be {SCHEMA_NAME}")
    if str(payload.get("run_id") or "") != str(run_id):
        return ValidationResult(status="unavailable", error="run_id mismatch")
    if str(payload.get("account_ref") or "") != str(account_ref):
        return ValidationResult(status="unavailable", error="account_ref mismatch")
    if str(payload.get("market") or "").strip().upper() != str(market).strip().upper():
        return ValidationResult(status="unavailable", error="market mismatch")
    payload_bindings = payload.get("input_bindings")
    if not isinstance(payload_bindings, Mapping):
        return ValidationResult(status="unavailable", error="input_bindings must be an object")
    for key, expected in input_bindings.items():
        if expected is None:
            continue
        if payload_bindings.get(key) != expected:
            return ValidationResult(status="unavailable", error=f"input_bindings.{key} mismatch")
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return ValidationResult(status="unavailable", error="strategies must be an array")

    result = ValidationResult(status="completed", decisions={})
    for strategy in strategies:
        if not isinstance(strategy, dict):
            return ValidationResult(status="unavailable", error="each strategy must be an object")
        family = str(strategy.get("strategy_family") or "")
        if family not in {"sell_put", "covered_call"}:
            return ValidationResult(status="unavailable", error=f"unknown strategy_family: {family!r}")
        decisions = strategy.get("decisions")
        if not isinstance(decisions, list):
            return ValidationResult(status="unavailable", error="decisions must be an array")
        for decision in decisions:
            if not isinstance(decision, dict):
                return ValidationResult(status="unavailable", error="each decision must be an object")
            scope_key = _scope_key_for(family, decision)
            if scope_key in result.decisions:
                return ValidationResult(
                    status="unavailable", error=f"duplicate decision for scope: {scope_key}"
                )
            result.decisions[scope_key] = _validate_decision(
                decision,
                scope_key=scope_key,
                scopes=scopes,
                context_complete=context_complete,
                demotions=result.demotions,
            )
    return result


def _scope_key_for(family: str, decision: Mapping[str, Any]) -> str:
    if family == "sell_put":
        return "sell_put"
    symbol = decision.get("scope_symbol")
    return f"covered_call:{symbol or ''}"


def _validate_decision(
    decision: Mapping[str, Any],
    *,
    scope_key: str,
    scopes: Mapping[str, ScopeSpec],
    context_complete: bool,
    demotions: list[dict[str, Any]],
) -> dict[str, Any]:
    spec = scopes.get(scope_key)
    baseline = str(decision.get("baseline_candidate_id") or "")
    selected_raw = decision.get("selected_candidate_id")
    selected = str(selected_raw) if selected_raw is not None else None
    action = str(decision.get("action") or "")

    rationale_raw = decision.get("rationale")
    rationale = (
        {key: str(rationale_raw.get(key) or "") for key in _RATIONALE_KEYS}
        if isinstance(rationale_raw, Mapping)
        else {key: "" for key in _RATIONALE_KEYS}
    )
    source_refs = {
        "internal_fact_refs": _string_list(decision.get("internal_fact_refs")),
        "external_evidence_refs": _string_list(decision.get("external_evidence_refs")),
    }

    def demote(reason: str, to_action: str = "needs_review") -> dict[str, Any]:
        demotions.append(
            {"scope": scope_key, "from_action": action, "to_action": to_action, "reason": reason}
        )
        return _decision_row(spec, to_action, baseline, None, rationale, source_refs, scope_key)

    if spec is None:
        return demote("unknown_scope")
    if baseline != spec.baseline_candidate_id:
        return demote("baseline_mismatch")
    if selected is not None and selected not in spec.allowed_candidate_ids:
        # Candidate ids are strategy-bound: a Covered Call scope must never
        # reference a Sell Put candidate (and vice versa), even as an
        # already-demoted artifact (docs 9.2).
        return demote("switch_out_of_pool" if action == "switch" else "selected_out_of_strategy_pool")
    if action not in ACTIONS:
        return demote("unsupported_action")
    if action == "keep" and selected != baseline:
        return demote("keep_selected_mismatch")
    if action in {"defer", "needs_review"} and selected is not None:
        return demote("selected_forbidden_for_action")
    if action == "switch":
        if not selected:
            return demote("switch_missing_selected")
        if selected not in spec.allowed_candidate_ids:
            # Rejected/unknown candidate id or Covered Call cross-symbol switch:
            # the same-underlying scope defines the allowed pool.
            return demote("switch_out_of_pool")
    if not context_complete and action != "needs_review":
        # Missing portfolio / option-position context caps every scope at
        # needs_review (docs 9.7).
        return demote("context_missing")
    if action == "keep" and not spec.symbol_evidence_complete:
        # Evidence incomplete (stale / no_evidence / identity_unavailable):
        # keep is never allowed; rendered as defer pending evidence.
        return demote("evidence_incomplete", to_action="defer")
    return _decision_row(spec, action, baseline, selected, rationale, source_refs, scope_key)


def _decision_row(
    spec: ScopeSpec | None,
    action: str,
    baseline: str,
    selected: str | None,
    rationale: dict[str, str],
    source_refs: dict[str, list[str]],
    scope_key: str,
) -> dict[str, Any]:
    return {
        "scope": scope_key,
        "strategy_family": spec.strategy_family if spec else scope_key.split(":", 1)[0],
        "symbol": spec.symbol if spec else None,
        "action": action,
        "baseline_candidate_id": baseline or None,
        "selected_candidate_id": selected if action in {"keep", "switch"} else None,
        "rationale": rationale,
        "source_refs": source_refs,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]
