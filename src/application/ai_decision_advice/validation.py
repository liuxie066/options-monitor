from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.application.ai_decision_advice.contexts import FACT_REGISTRY_SCHEMA
from src.application.ai_decision_advice.evidence_store import COVERAGE_COMPLETED


SCHEMA_NAME = "ai_decision_advice.v1"

ACTIONS = frozenset({"keep", "switch", "defer", "needs_review"})

INPUT_BINDING_KEYS = (
    "candidate_snapshot_hash",
    "portfolio_distribution_hash",
    "option_positions_hash",
    "fact_registry_hash",
    "external_evidence_hash",
    "external_evidence_run_id",
)

SEMANTIC_INPUT_BINDING_KEYS = tuple(key for key in INPUT_BINDING_KEYS if key != "external_evidence_run_id")

_TOP_LEVEL_KEYS = frozenset({"schema", "run_id", "account_ref", "market", "input_bindings", "strategies"})
_STRATEGY_KEYS = frozenset({"strategy_family", "status", "decisions"})
_DECISION_KEYS = frozenset(
    {
        "scope_symbol",
        "baseline_candidate_id",
        "action",
        "selected_candidate_id",
        "rationale",
        "internal_fact_refs",
        "external_evidence_refs",
    }
)
_RATIONALE_KEYS = frozenset({"risk_mechanism", "candidate_effect", "decision_reason"})
_INTERNAL_KINDS = frozenset({"candidate", "projection", "portfolio", "position", "coverage", "gap"})
_FACT_SUPPORT_CLASS = {
    "candidate": "candidate",
    "projection": "risk",
    "portfolio": "risk",
    "position": "risk",
    "coverage": "coverage",
    "evidence": "external_risk",
    "gap": "gap",
}


@dataclass(frozen=True)
class ScopeSpec:
    """One exact decision scope derived from candidates and frozen facts."""

    scope_key: str
    strategy_family: str
    symbol: str | None
    baseline_candidate_id: str
    allowed_candidate_ids: frozenset[str]
    candidate_fact_refs: Mapping[str, str]
    projection_fact_refs: Mapping[str, str]
    required_coverage_refs: frozenset[str]
    allowed_internal_refs: frozenset[str]
    allowed_external_refs: frozenset[str]
    usable_external_refs: frozenset[str]
    risk_internal_refs: frozenset[str]
    gap_refs: frozenset[str]
    portfolio_complete: bool
    option_positions_complete: bool
    projection_complete: bool
    coverage_complete: bool


@dataclass
class ValidationResult:
    status: str
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    demotions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def derive_scopes(
    candidates: Mapping[str, Any],
    fact_registry: Mapping[str, Any],
) -> dict[str, ScopeSpec]:
    """Derive the exact SP/CC scopes and their auditable fact boundaries.

    The fact registry is structural input, not model opinion. Invalid or
    incomplete registry membership raises ``ValueError`` so the account run
    can fail closed before a model action is accepted.
    """

    rows_by_scope: dict[str, list[Mapping[str, Any]]] = {}
    candidate_scope: dict[str, str] = {}
    candidate_family: dict[str, str] = {}
    candidate_symbol: dict[str, str] = {}

    for family in ("sell_put", "covered_call"):
        rows = candidates.get(family)
        if not isinstance(rows, list):
            raise ValueError(f"candidate family {family} must be an array")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"candidate family {family} row is invalid")
            candidate_id = _required_text(row.get("candidate_id"), "candidate_id")
            symbol = _required_text(row.get("symbol"), "candidate symbol")
            if candidate_id in candidate_scope:
                raise ValueError("candidate identity is duplicated")
            scope_key = "sell_put" if family == "sell_put" else f"covered_call:{symbol}"
            candidate_scope[candidate_id] = scope_key
            candidate_family[candidate_id] = family
            candidate_symbol[candidate_id] = symbol
            rows_by_scope.setdefault(scope_key, []).append(row)

    facts = _index_fact_registry(
        fact_registry,
        expected_scopes=frozenset(rows_by_scope),
    )
    expected_candidate_refs = {f"candidate:{candidate_id}" for candidate_id in candidate_scope}
    expected_projection_refs = {f"projection:{candidate_id}" for candidate_id in candidate_scope}
    if {ref for ref, fact in facts.items() if fact["kind"] == "candidate"} != expected_candidate_refs:
        raise ValueError("candidate fact membership mismatch")
    if {ref for ref, fact in facts.items() if fact["kind"] == "projection"} != expected_projection_refs:
        raise ValueError("projection fact membership mismatch")

    for candidate_id, scope_key in candidate_scope.items():
        candidate_ref = f"candidate:{candidate_id}"
        candidate_fact = facts[candidate_ref]
        expected_row = next(row for row in rows_by_scope[scope_key] if str(row.get("candidate_id")) == candidate_id)
        if candidate_fact["scope"] != scope_key or candidate_fact["data"] != dict(expected_row):
            raise ValueError(f"candidate fact mismatch: {candidate_id}")

        projection_ref = f"projection:{candidate_id}"
        projection_fact = facts[projection_ref]
        projection = projection_fact["data"]
        expected_mode = "put" if candidate_family[candidate_id] == "sell_put" else "call"
        if (
            projection_fact["scope"] != scope_key
            or projection.get("candidate_id") != candidate_id
            or projection.get("strategy_mode") != expected_mode
            or projection.get("symbol") != candidate_symbol[candidate_id]
        ):
            raise ValueError(f"projection fact mismatch: {candidate_id}")

    portfolio_fact = facts.get("portfolio:distribution")
    if portfolio_fact is None or portfolio_fact["kind"] != "portfolio":
        raise ValueError("portfolio distribution fact is missing")
    if any(fact["kind"] == "portfolio" and ref != "portfolio:distribution" for ref, fact in facts.items()):
        raise ValueError("portfolio fact membership mismatch")

    position_summary = facts.get("position:summary")
    if position_summary is None or position_summary["kind"] != "position":
        raise ValueError("position summary fact is missing")

    coverage_facts = {ref: fact for ref, fact in facts.items() if fact["kind"] == "coverage"}
    coverage_by_symbol: dict[str, tuple[str, str]] = {}
    for ref, fact in coverage_facts.items():
        symbol = _required_text(fact["data"].get("symbol"), "coverage symbol")
        if ref != f"coverage:{symbol}" or symbol in coverage_by_symbol:
            raise ValueError("coverage fact identity mismatch")
        coverage_by_symbol[symbol] = (
            ref,
            str(fact["data"].get("coverage") or ""),
        )
    missing_candidate_coverage = set(candidate_symbol.values()) - set(coverage_by_symbol)
    if missing_candidate_coverage:
        raise ValueError("candidate coverage fact is missing")

    evidence_refs_by_symbol: dict[str, set[str]] = {}
    for ref, fact in facts.items():
        if fact["kind"] != "evidence":
            continue
        symbol = _required_text(fact["data"].get("symbol"), "evidence symbol")
        if symbol not in coverage_by_symbol:
            raise ValueError("evidence fact has no coverage fact")
        evidence_refs_by_symbol.setdefault(symbol, set()).add(ref)

    gap_facts = {ref: fact for ref, fact in facts.items() if fact["kind"] == "gap"}
    portfolio_complete = _portfolio_is_complete(portfolio_fact["data"])
    option_positions_complete = _option_positions_are_complete(position_summary["data"], gap_facts)
    all_coverage_refs = frozenset(coverage_facts)
    coverage_complete = all(status == COVERAGE_COMPLETED for _, status in coverage_by_symbol.values())
    account_internal_refs = {
        ref for ref, fact in facts.items() if fact["kind"] in {"portfolio", "position", "coverage"}
    }
    account_risk_refs = {ref for ref, fact in facts.items() if fact["kind"] in {"portfolio", "position"}}

    scopes: dict[str, ScopeSpec] = {}
    for scope_key, rows in sorted(rows_by_scope.items()):
        family = "sell_put" if scope_key == "sell_put" else "covered_call"
        symbol = None if family == "sell_put" else scope_key.split(":", 1)[1]
        candidate_ids = tuple(str(row["candidate_id"]) for row in rows)
        candidate_refs = {candidate_id: f"candidate:{candidate_id}" for candidate_id in candidate_ids}
        projection_refs = {candidate_id: f"projection:{candidate_id}" for candidate_id in candidate_ids}
        scoped_gap_refs = {ref for ref, fact in gap_facts.items() if fact["scope"] in {"account", scope_key}}
        scope_symbols = (
            {candidate_symbol[candidate_id] for candidate_id in candidate_ids}
            if family == "sell_put"
            else {str(symbol)}
        )
        allowed_external_refs = {
            ref
            for candidate_scope_symbol in scope_symbols
            for ref in evidence_refs_by_symbol.get(candidate_scope_symbol, set())
        }
        usable_external_refs = {
            ref
            for ref in allowed_external_refs
            if coverage_by_symbol[str(facts[ref]["data"]["symbol"])][1] == COVERAGE_COMPLETED
        }
        scope_projection_refs = set(projection_refs.values())
        projection_complete = all(_projection_is_complete(facts[ref]["data"]) for ref in scope_projection_refs)
        allowed_internal_refs = (
            set(candidate_refs.values()) | scope_projection_refs | account_internal_refs | scoped_gap_refs
        )
        scopes[scope_key] = ScopeSpec(
            scope_key=scope_key,
            strategy_family=family,
            symbol=symbol,
            baseline_candidate_id=candidate_ids[0],
            allowed_candidate_ids=frozenset(candidate_ids),
            candidate_fact_refs=candidate_refs,
            projection_fact_refs=projection_refs,
            required_coverage_refs=all_coverage_refs,
            allowed_internal_refs=frozenset(allowed_internal_refs),
            allowed_external_refs=frozenset(allowed_external_refs),
            usable_external_refs=frozenset(usable_external_refs),
            risk_internal_refs=frozenset(account_risk_refs | scope_projection_refs),
            gap_refs=frozenset(scoped_gap_refs),
            portfolio_complete=portfolio_complete,
            option_positions_complete=option_positions_complete,
            projection_complete=projection_complete,
            coverage_complete=coverage_complete,
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
) -> ValidationResult:
    """Validate structure, exact scope cardinality and fact-supported actions."""

    if not isinstance(payload, dict):
        return _unavailable("output must be a JSON object")
    if set(payload) != _TOP_LEVEL_KEYS:
        return _unavailable("output top-level fields mismatch")
    if payload.get("schema") != SCHEMA_NAME:
        return _unavailable(f"schema must be {SCHEMA_NAME}")
    if payload.get("run_id") != run_id:
        return _unavailable("run_id mismatch")
    if payload.get("account_ref") != account_ref:
        return _unavailable("account_ref mismatch")
    if not isinstance(payload.get("market"), str) or (
        str(payload["market"]).strip().upper() != str(market).strip().upper()
    ):
        return _unavailable("market mismatch")

    if set(input_bindings) != set(INPUT_BINDING_KEYS):
        return _unavailable("frozen input_bindings fields mismatch")
    payload_bindings = payload.get("input_bindings")
    if not isinstance(payload_bindings, Mapping):
        return _unavailable("input_bindings must be an object")
    if set(payload_bindings) != set(INPUT_BINDING_KEYS):
        return _unavailable("input_bindings fields mismatch")
    for key in INPUT_BINDING_KEYS:
        if payload_bindings.get(key) != input_bindings.get(key):
            return _unavailable(f"input_bindings.{key} mismatch")

    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return _unavailable("strategies must be an array")
    expected_families = {spec.strategy_family for spec in scopes.values()}
    if len(strategies) != len(expected_families):
        return _unavailable("incomplete_output")

    raw_decisions: dict[str, Mapping[str, Any]] = {}
    seen_families: set[str] = set()
    for strategy in strategies:
        if not isinstance(strategy, dict) or set(strategy) != _STRATEGY_KEYS:
            return _unavailable("strategy structure mismatch")
        family = strategy.get("strategy_family")
        if not isinstance(family, str) or family not in {"sell_put", "covered_call"}:
            return _unavailable("incomplete_output")
        if family in seen_families or family not in expected_families:
            return _unavailable("incomplete_output")
        seen_families.add(family)
        if strategy.get("status") != "completed":
            return _unavailable("strategy status must be completed")
        decisions = strategy.get("decisions")
        if not isinstance(decisions, list):
            return _unavailable("decisions must be an array")
        for decision in decisions:
            structural_error = _decision_structure_error(decision)
            if structural_error is not None:
                return _unavailable(structural_error)
            scope_key = _scope_key_for(family, decision)
            if scope_key is None or scope_key in raw_decisions:
                return _unavailable("incomplete_output")
            raw_decisions[scope_key] = decision

    if seen_families != expected_families or set(raw_decisions) != set(scopes):
        return _unavailable("incomplete_output")

    result = ValidationResult(status="completed", decisions={})
    for scope_key in sorted(scopes):
        result.decisions[scope_key] = _validate_decision(
            raw_decisions[scope_key],
            spec=scopes[scope_key],
            demotions=result.demotions,
        )
    return result


def _index_fact_registry(
    fact_registry: Mapping[str, Any],
    *,
    expected_scopes: frozenset[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(fact_registry, Mapping) or set(fact_registry) != {
        "schema_version",
        "facts",
    }:
        raise ValueError("fact registry structure is invalid")
    if fact_registry.get("schema_version") != FACT_REGISTRY_SCHEMA:
        raise ValueError("fact registry schema mismatch")
    raw_facts = fact_registry.get("facts")
    if not isinstance(raw_facts, list):
        raise ValueError("fact registry facts must be an array")

    facts: dict[str, dict[str, Any]] = {}
    allowed_scopes = {"account", *expected_scopes}
    for raw in raw_facts:
        if not isinstance(raw, Mapping) or set(raw) != {
            "id",
            "kind",
            "scope",
            "support_class",
            "data",
        }:
            raise ValueError("fact registry row is invalid")
        fact_id = _required_text(raw.get("id"), "fact id")
        kind = _required_text(raw.get("kind"), "fact kind")
        scope = _required_text(raw.get("scope"), "fact scope")
        support_class = _required_text(raw.get("support_class"), "fact support class")
        data = raw.get("data")
        if fact_id in facts:
            raise ValueError("fact registry identity is duplicated")
        if kind not in _FACT_SUPPORT_CLASS or fact_id.split(":", 1)[0] != kind:
            raise ValueError(f"fact kind/prefix mismatch: {fact_id}")
        if support_class != _FACT_SUPPORT_CLASS[kind]:
            raise ValueError(f"fact support class mismatch: {fact_id}")
        if scope not in allowed_scopes or not isinstance(data, Mapping):
            raise ValueError(f"fact scope/data mismatch: {fact_id}")
        if kind in {"portfolio", "position", "coverage", "evidence"} and scope != "account":
            raise ValueError(f"account fact scope mismatch: {fact_id}")
        facts[fact_id] = {
            "id": fact_id,
            "kind": kind,
            "scope": scope,
            "support_class": support_class,
            "data": dict(data),
        }
    return facts


def _portfolio_is_complete(data: Mapping[str, Any]) -> bool:
    quality = data.get("quality")
    return (
        data.get("status") == "ready"
        and isinstance(quality, Mapping)
        and quality.get("freshness_status") == "fresh"
        and quality.get("trust_status") == "trusted"
        and not _has_gaps(data.get("gaps"))
    )


def _option_positions_are_complete(
    summary: Mapping[str, Any],
    gap_facts: Mapping[str, Mapping[str, Any]],
) -> bool:
    count = summary.get("total_open_contracts")
    has_option_gap = any(fact["data"].get("source") == "option_positions" for fact in gap_facts.values())
    return isinstance(count, int) and not isinstance(count, bool) and count >= 0 and not has_option_gap


def _projection_is_complete(data: Mapping[str, Any]) -> bool:
    return (
        data.get("calculation_complete") is True
        and data.get("scope_ceiling") is None
        and not _has_gaps(data.get("gaps"))
    )


def _has_gaps(value: Any) -> bool:
    return not isinstance(value, list) or bool(value)


def _decision_structure_error(decision: Any) -> str | None:
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        return "decision structure mismatch"
    if not isinstance(decision.get("baseline_candidate_id"), str) or not decision.get("baseline_candidate_id"):
        return "baseline_candidate_id must be a non-empty string"
    if not isinstance(decision.get("action"), str) or not decision.get("action"):
        return "action must be a non-empty string"
    selected = decision.get("selected_candidate_id")
    if selected is not None and (not isinstance(selected, str) or not selected):
        return "selected_candidate_id must be a string or null"
    rationale = decision.get("rationale")
    if not isinstance(rationale, Mapping) or set(rationale) != _RATIONALE_KEYS:
        return "rationale structure mismatch"
    if any(not isinstance(rationale.get(key), str) for key in _RATIONALE_KEYS):
        return "rationale values must be strings"
    for key in ("internal_fact_refs", "external_evidence_refs"):
        refs = decision.get(key)
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
            return f"{key} must contain non-empty strings"
    return None


def _scope_key_for(family: str, decision: Mapping[str, Any]) -> str | None:
    symbol = decision.get("scope_symbol")
    if family == "sell_put":
        return "sell_put" if symbol is None else None
    if not isinstance(symbol, str) or not symbol:
        return None
    return f"covered_call:{symbol}"


def _validate_decision(
    decision: Mapping[str, Any],
    *,
    spec: ScopeSpec,
    demotions: list[dict[str, Any]],
) -> dict[str, Any]:
    action = str(decision["action"])
    baseline = str(decision["baseline_candidate_id"])
    selected = decision.get("selected_candidate_id")
    rationale = {
        key: str(decision["rationale"][key]) for key in ("risk_mechanism", "candidate_effect", "decision_reason")
    }
    internal_refs = list(decision["internal_fact_refs"])
    external_refs = list(decision["external_evidence_refs"])
    valid_internal_refs = _unique_refs(ref for ref in internal_refs if ref in spec.allowed_internal_refs)
    valid_external_refs = _unique_refs(ref for ref in external_refs if ref in spec.allowed_external_refs)

    def demote(reason: str) -> dict[str, Any]:
        demotions.append(
            {
                "scope": spec.scope_key,
                "from_action": action,
                "to_action": "needs_review",
                "reason": reason,
            }
        )
        refs = list(valid_internal_refs)
        if not any(ref in spec.gap_refs for ref in refs) and spec.gap_refs:
            refs.append(sorted(spec.gap_refs)[0])
        return _decision_row(
            spec,
            "needs_review",
            None,
            rationale,
            {
                "internal_fact_refs": refs,
                "external_evidence_refs": valid_external_refs,
            },
        )

    if action not in ACTIONS:
        return demote("unsupported_action")
    if baseline != spec.baseline_candidate_id:
        return demote("baseline_mismatch")
    if selected is not None and selected not in spec.allowed_candidate_ids:
        return demote("switch_out_of_pool" if action == "switch" else "selected_out_of_strategy_pool")
    if len(internal_refs) != len(set(internal_refs)) or len(external_refs) != len(set(external_refs)):
        return demote("duplicate_fact_refs")
    if any(ref.split(":", 1)[0] not in _INTERNAL_KINDS for ref in internal_refs):
        return demote("invalid_internal_fact_ref_prefix")
    if any(not ref.startswith("evidence:") for ref in external_refs):
        return demote("invalid_external_evidence_ref_prefix")
    if any(ref not in spec.allowed_internal_refs for ref in internal_refs):
        return demote("unresolved_or_out_of_scope_internal_fact_ref")
    if any(ref not in spec.allowed_external_refs for ref in external_refs):
        return demote("unresolved_or_out_of_scope_external_evidence_ref")
    if any(not value.strip() for value in rationale.values()):
        return demote("rationale_incomplete")

    if action == "keep":
        if selected != baseline:
            return demote("keep_selected_mismatch")
    elif action == "switch":
        if selected is None:
            return demote("switch_missing_selected")
        if selected == baseline:
            return demote("switch_same_as_baseline")
    elif selected is not None:
        return demote("selected_forbidden_for_action")

    quality_reason = _quality_ceiling_reason(spec, action)
    if quality_reason is not None and action != "needs_review":
        return demote(quality_reason)

    internal_set = set(valid_internal_refs)
    external_set = set(valid_external_refs)
    usable_external = external_set & set(spec.usable_external_refs)
    if action == "keep":
        required = {
            spec.candidate_fact_refs[baseline],
            spec.projection_fact_refs[baseline],
            *spec.required_coverage_refs,
        }
        if not required <= internal_set:
            return demote("keep_missing_required_fact_refs")
    elif action == "switch":
        assert isinstance(selected, str)
        required_candidates = {
            spec.candidate_fact_refs[baseline],
            spec.candidate_fact_refs[selected],
        }
        has_risk = bool((internal_set & set(spec.risk_internal_refs)) or usable_external)
        if not required_candidates <= internal_set or not has_risk:
            return demote("switch_missing_fact_support")
    elif action == "defer":
        has_risk = bool((internal_set & set(spec.risk_internal_refs)) or usable_external)
        if not has_risk:
            return demote("defer_missing_fact_support")
    else:
        risk_refs = internal_set & set(spec.risk_internal_refs)
        has_gap_or_conflict = bool((internal_set & set(spec.gap_refs)) or usable_external or len(risk_refs) >= 2)
        if not has_gap_or_conflict:
            return demote("needs_review_missing_gap_or_conflict")

    return _decision_row(
        spec,
        action,
        selected if isinstance(selected, str) else None,
        rationale,
        {
            "internal_fact_refs": valid_internal_refs,
            "external_evidence_refs": valid_external_refs,
        },
    )


def _quality_ceiling_reason(spec: ScopeSpec, action: str) -> str | None:
    if not spec.portfolio_complete:
        return "portfolio_context_incomplete"
    if not spec.option_positions_complete:
        return "option_positions_context_incomplete"
    if not spec.projection_complete:
        return "projection_incomplete"
    if action == "keep" and not spec.coverage_complete:
        return "evidence_coverage_incomplete"
    return None


def _decision_row(
    spec: ScopeSpec,
    action: str,
    selected: str | None,
    rationale: dict[str, str],
    source_refs: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "scope": spec.scope_key,
        "strategy_family": spec.strategy_family,
        "symbol": spec.symbol,
        "action": action,
        "baseline_candidate_id": spec.baseline_candidate_id,
        "selected_candidate_id": selected if action in {"keep", "switch"} else None,
        "rationale": rationale,
        "source_refs": source_refs,
    }


def _unique_refs(refs: Any) -> list[str]:
    return list(dict.fromkeys(refs))


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is missing")
    return value


def _unavailable(error: str) -> ValidationResult:
    return ValidationResult(status="unavailable", error=error)
