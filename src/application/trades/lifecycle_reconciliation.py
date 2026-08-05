from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.lifecycle_allocation import (
    AllocationPlan,
    TERMINAL_TYPES,
    plan_evidence_allocation,
    resolve_allocations,
)
from domain.domain.option_lifecycle import derive_lifecycle_read_model
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.ledger.api import (
    discover_expired_lifecycle_cases,
    lifecycle_account_coherent_facts,
    lifecycle_case_coherent_facts,
    lifecycle_case_coherent_facts_many_from_account_snapshot,
    lifecycle_reconciliation_facts,
    record_lifecycle_allocation,
    record_lifecycle_evidence_issue,
)
from src.application.trades.settlement_attempts import (
    SETTLEMENT_OBSERVATION_CONTEXT_KEY,
)
from src.application.trades.close_reason_evidence import (
    canonical_hash,
    derive_effective_lifecycle_timing,
)


EARLY_SETTLEMENT_TOLERANCE_MS = 5 * 60 * 1000


class LifecycleCaseDataError(ValueError):
    """A malformed case-local fact that may be isolated by a batch reader."""


@dataclass(frozen=True)
class LifecycleReconciliationResult:
    status: str
    reason_codes: tuple[str, ...]
    case_id: str | None
    evidence_id: str | None
    terminal_type: str | None
    apply_changes: bool
    allocation_plan: tuple[dict[str, Any], ...] = ()
    ledger_result: dict[str, Any] | None = None
    lifecycle_read_model: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "lifecycle_reconciliation_result.v2",
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "case_id": self.case_id,
            "evidence_id": self.evidence_id,
            "terminal_type": self.terminal_type,
            "apply_changes": self.apply_changes,
            "allocation_plan": [dict(item) for item in self.allocation_plan],
            "ledger_result": (
                dict(self.ledger_result)
                if isinstance(self.ledger_result, dict)
                else None
            ),
            "lifecycle_read_model": (
                dict(self.lifecycle_read_model)
                if isinstance(self.lifecycle_read_model, dict)
                else None
            ),
        }


def discover_lifecycle_cases(
    repo: Any,
    *,
    account: str | None = None,
    observed_at_ms: int | None = None,
    apply_changes: bool = True,
) -> dict[str, Any]:
    return discover_expired_lifecycle_cases(
        repo,
        account=account,
        observed_at_ms=observed_at_ms,
        apply_changes=apply_changes,
    )


def lifecycle_case_read_model(
    repo: Any,
    *,
    case_id: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    facts = lifecycle_case_coherent_facts(repo, case_id=case_id)
    return build_lifecycle_case_read_model_from_resolved_facts(
        lifecycle_case=dict(facts["lifecycle_case"]),
        case_resolution=dict(facts["case_resolution"]),
        generation_token=dict(facts["generation_token"]),
        allocations=list(facts["case_allocations"]),
        timing_policy=(
            dict(facts["timing_policy"])
            if isinstance(facts.get("timing_policy"), dict)
            else None
        ),
        position_lot_fields_by_id=dict(
            facts["position_lot_fields_by_id"]
        ),
        void_event_ids=tuple(facts["effective_void_event_ids"]),
        now_ms=now_ms,
    )


def lifecycle_case_read_models_for_account(
    repo: Any,
    *,
    account: str,
    now_ms: int | None = None,
    settlement_context_case_ids: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Build every case model from one coherent account materialization."""

    facts = lifecycle_account_coherent_facts(
        repo,
        account=str(account or "").strip().lower(),
    )
    cases = [
        dict(item)
        for item in facts.get("account_lifecycle_cases") or []
        if isinstance(item, dict)
    ]
    allocations = [
        dict(item)
        for item in facts.get("account_lifecycle_allocations") or []
        if isinstance(item, dict)
    ]
    timing_policies = [
        dict(item)
        for item in facts.get("account_lifecycle_timing_policies") or []
        if isinstance(item, dict)
    ]
    position_lots = [
        dict(item)
        for item in facts.get("account_position_lots") or []
        if isinstance(item, dict)
    ]
    account_resolution = facts.get("account_lifecycle_resolution")
    if not isinstance(account_resolution, Mapping):
        raise LifecycleCaseDataError(
            "lifecycle_account_resolution_unavailable"
        )

    cases_by_id = {
        str(item.get("case_id") or "").strip(): item
        for item in cases
        if str(item.get("case_id") or "").strip()
    }
    allocations_by_case: dict[str, list[dict[str, Any]]] = {}
    for item in allocations:
        allocations_by_case.setdefault(
            str(item.get("case_id") or "").strip(),
            [],
        ).append(item)
    timing_by_case = {
        str(item.get("case_id") or "").strip(): item
        for item in timing_policies
        if str(item.get("case_id") or "").strip()
    }
    lots_by_id = {
        str(item.get("record_id") or "").strip(): dict(
            item.get("fields") or {}
        )
        for item in position_lots
        if str(item.get("record_id") or "").strip()
    }
    tokens_by_case = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in account_resolution.get("generation_tokens") or []
        if isinstance(item, Mapping)
        and str(item.get("case_id") or "").strip()
    }
    context_case_ids = {
        str(item or "").strip()
        for item in settlement_context_case_ids
        if str(item or "").strip()
    }
    resolved_case_ids = {
        str(item.get("case_id") or "").strip()
        for item in account_resolution.get("case_resolutions") or []
        if isinstance(item, Mapping)
        and str(item.get("case_id") or "").strip()
    }
    available_context_ids = (
        context_case_ids
        & set(cases_by_id)
        & set(tokens_by_case)
        & resolved_case_ids
    )
    context_facts_by_case = (
        lifecycle_case_coherent_facts_many_from_account_snapshot(
            facts,
            case_ids=sorted(available_context_ids),
        )
        if available_context_ids
        else {}
    )
    output: dict[str, dict[str, Any]] = {}
    for raw_resolution in account_resolution.get("case_resolutions") or []:
        if not isinstance(raw_resolution, Mapping):
            continue
        case_resolution = dict(raw_resolution)
        case_id = str(case_resolution.get("case_id") or "").strip()
        lifecycle_case = cases_by_id.get(case_id)
        generation_token = tokens_by_case.get(case_id)
        if lifecycle_case is None or generation_token is None:
            continue
        try:
            model = build_lifecycle_case_read_model_from_resolved_facts(
                lifecycle_case=lifecycle_case,
                case_resolution=case_resolution,
                generation_token=generation_token,
                allocations=allocations_by_case.get(case_id, []),
                timing_policy=timing_by_case.get(case_id),
                position_lot_fields_by_id=lots_by_id,
                void_event_ids=tuple(
                    facts.get("effective_void_event_ids") or ()
                ),
                now_ms=now_ms,
            )
            if case_id in context_case_ids:
                case_facts = context_facts_by_case[case_id]
                model[SETTLEMENT_OBSERVATION_CONTEXT_KEY] = (
                    _settlement_observation_context(case_facts)
                )
            output[case_id] = model
        except LifecycleCaseDataError as exc:
            output[case_id] = _isolated_case_error_model(
                lifecycle_case=lifecycle_case,
                case_id=case_id,
                generation_token=generation_token,
                error=exc,
            )
    return output


def _settlement_observation_context(
    case_facts: Mapping[str, Any],
) -> dict[str, Any]:
    lifecycle_case = dict(case_facts.get("lifecycle_case") or {})
    target_lot_ids = {
        str(item or "").strip()
        for item in dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        )
        if str(item or "").strip()
    }
    lot_fields = dict(
        case_facts.get("position_lot_fields_by_id") or {}
    )
    return {
        "schema_version": "settlement_observation_context.v1",
        "lifecycle_case": lifecycle_case,
        "case_resolution": dict(
            case_facts.get("case_resolution") or {}
        ),
        "generation_token": dict(
            case_facts.get("generation_token") or {}
        ),
        "case_allocations": [
            dict(item)
            for item in case_facts.get("case_allocations") or []
            if isinstance(item, Mapping)
        ],
        "timing_policy": (
            dict(case_facts["timing_policy"])
            if isinstance(case_facts.get("timing_policy"), Mapping)
            else None
        ),
        "position_lot_fields_by_id": {
            lot_id: dict(lot_fields.get(lot_id) or {})
            for lot_id in sorted(target_lot_ids)
        },
        "effective_void_event_ids": list(
            case_facts.get("effective_void_event_ids") or []
        ),
        "validated_anchors": [
            dict(item)
            for item in case_facts.get("validated_anchors") or []
            if isinstance(item, Mapping)
        ],
        "anchor_source_claims": [
            dict(item)
            for item in case_facts.get("anchor_source_claims") or []
            if isinstance(item, Mapping)
        ],
    }


def build_lifecycle_read_models_from_resolved_account(
    *,
    cases: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    timing_policies: list[dict[str, Any]],
    position_lots: list[dict[str, Any]],
    account_resolution: Mapping[str, Any],
    void_event_ids: tuple[str, ...] | list[str] = (),
    now_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Build lot models from one already-resolved account generation."""

    cases_by_id = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in cases
        if isinstance(item, dict)
        and str(item.get("case_id") or "").strip()
    }
    allocations_by_case: dict[str, list[dict[str, Any]]] = {}
    for item in allocations:
        if not isinstance(item, dict):
            continue
        allocations_by_case.setdefault(
            str(item.get("case_id") or "").strip(),
            [],
        ).append(dict(item))
    timing_by_case = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in timing_policies
        if isinstance(item, dict)
        and str(item.get("case_id") or "").strip()
    }
    lots_by_id = {
        str(item.get("record_id") or "").strip(): dict(item.get("fields") or {})
        for item in position_lots
        if isinstance(item, dict)
        and str(item.get("record_id") or "").strip()
    }
    tokens_by_case = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in account_resolution.get("generation_tokens") or []
        if isinstance(item, Mapping)
        and str(item.get("case_id") or "").strip()
    }
    output: dict[str, dict[str, Any]] = {}
    for raw_resolution in account_resolution.get("case_resolutions") or []:
        if not isinstance(raw_resolution, Mapping):
            continue
        case_resolution = dict(raw_resolution)
        case_id = str(case_resolution.get("case_id") or "").strip()
        lifecycle_case = cases_by_id.get(case_id)
        generation_token = tokens_by_case.get(case_id)
        if lifecycle_case is None or generation_token is None:
            continue
        target_ids = sorted(
            str(item or "").strip()
            for item in dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            )
            if str(item or "").strip()
        )
        try:
            model = build_lifecycle_case_read_model_from_resolved_facts(
                lifecycle_case=lifecycle_case,
                case_resolution=case_resolution,
                generation_token=generation_token,
                allocations=allocations_by_case.get(case_id, []),
                timing_policy=timing_by_case.get(case_id),
                position_lot_fields_by_id=lots_by_id,
                void_event_ids=tuple(void_event_ids),
                now_ms=now_ms,
            )
        except LifecycleCaseDataError as exc:
            model = _isolated_case_error_model(
                lifecycle_case=lifecycle_case,
                case_id=case_id,
                generation_token=generation_token,
                error=exc,
            )
        for lot_id in target_ids:
            if lot_id not in output:
                output[lot_id] = dict(model)
                continue
            prior = output[lot_id]
            case_ids = sorted(
                {
                    str(prior.get("lifecycle_case_id") or ""),
                    case_id,
                }
                - {""}
            )
            output[lot_id] = {
                **prior,
                "lifecycle_state": "conflict",
                "lifecycle_case_ids": case_ids,
                "lifecycle_evidence_status": "conflict",
                "lifecycle_reason_codes": sorted(
                    {
                        *prior.get("lifecycle_reason_codes", []),
                        *model.get("lifecycle_reason_codes", []),
                        "reservation_target_overlap",
                    }
                ),
                "reason_state": "conflict",
                "actionable": False,
            }
    return output


def build_lifecycle_case_read_model_from_resolved_facts(
    *,
    lifecycle_case: dict[str, Any],
    case_resolution: dict[str, Any],
    generation_token: dict[str, Any],
    allocations: list[dict[str, Any]],
    timing_policy: dict[str, Any] | None,
    position_lot_fields_by_id: dict[str, dict[str, Any]],
    void_event_ids: tuple[str, ...] | list[str],
    now_ms: int | None,
) -> dict[str, Any]:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    if not case_id or str(case_resolution.get("case_id") or "") != case_id:
        raise LifecycleCaseDataError("lifecycle_case_resolution_mismatch")
    try:
        target_manifest = {
            str(key): int(value)
            for key, value in dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            ).items()
        }
        resolution = resolve_allocations(
            target_manifest,
            allocations,
            void_event_ids=void_event_ids,
        )
        quantity_drift = any(
            lot_id not in position_lot_fields_by_id
            or int(
                position_lot_fields_by_id[lot_id].get("contracts_open")
                or 0
            )
            != expected_remaining
            for lot_id, expected_remaining in (
                resolution.remaining_contracts_by_lot.items()
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise LifecycleCaseDataError(
            "lifecycle_case_quantity_invalid"
        ) from exc

    anchor_facts = [
        dict(item)
        for item in case_resolution.get("anchor_facts") or []
        if isinstance(item, Mapping)
    ]
    effective_timing: dict[str, Any] | None = None
    timing_error: str | None = None
    if timing_policy is not None and anchor_facts:
        try:
            effective_timing = derive_effective_lifecycle_timing(
                policy=timing_policy,
                option_close_evidence=[
                    {
                        "evidence_type": "option_zero_price_close",
                        "received_at_ms": item.get("received_at_ms"),
                    }
                    for item in anchor_facts
                ],
            )
        except (TypeError, ValueError, OverflowError) as exc:
            timing_error = str(exc)

    persisted_status = str(
        lifecycle_case.get("status") or ""
    ).strip().lower()
    summary = (
        dict(lifecycle_case.get("derived_summary") or {})
        if isinstance(lifecycle_case.get("derived_summary"), dict)
        else {}
    )
    conflict_reasons = set(
        str(item)
        for item in case_resolution.get("reason_codes") or []
        if str(item or "").strip()
    )
    if persisted_status == "conflict":
        conflict_reasons.update(
            str(item)
            for item in summary.get("lifecycle_reason_codes") or []
            if str(item or "").strip()
        )
    if timing_error and case_resolution.get("status") in {"direct", "bridged"}:
        conflict_reasons.add("lifecycle_effective_timing_invalid")

    try:
        read_model = derive_lifecycle_read_model(
            expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
            market=str(
                lifecycle_case.get("market")
                or symbol_market(lifecycle_case.get("symbol"))
                or ""
            ),
            target_contracts_by_lot=target_manifest,
            allocations=allocations,
            void_event_ids=void_event_ids,
            accepted_option_close_contracts_by_lot=dict(
                case_resolution.get("effective_reservations_by_lot") or {}
            ),
            now_ms=now_ms,
            conflict_reason_codes=tuple(sorted(conflict_reasons)),
            quantity_drift=quantity_drift,
            observation_start_ms_override=(
                int(lifecycle_case["observation_start_ms"])
                if lifecycle_case.get("observation_start_ms") is not None
                else None
            ),
            pending_until_ms_override=(
                int(
                    (effective_timing or timing_policy or {}).get(
                        "settlement_deadline_ms"
                    )
                )
                if (effective_timing or timing_policy or {}).get(
                    "settlement_deadline_ms"
                )
                is not None
                else None
            ),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifecycleCaseDataError(
            "lifecycle_case_read_model_invalid"
        ) from exc

    effective_allocations = [
        item
        for item in allocations
        if not bool(item.get("voided"))
        and str(item.get("canonical_terminal_event_id") or "").strip()
        not in set(void_event_ids)
    ]
    anchor_status = str(case_resolution.get("status") or "missing")
    if read_model.lifecycle_state == "conflict":
        evidence_status = "conflict"
    elif anchor_status in {"direct", "bridged"} and any(
        read_model.reserved_contracts_by_lot.values()
    ):
        evidence_status = "closure_observed_cause_pending"
    elif anchor_status == "missing" and not effective_allocations:
        evidence_status = "missing"
    elif any(read_model.remaining_contracts_by_lot.values()):
        evidence_status = "partial"
    else:
        evidence_status = "complete"

    persisted_reason_state = str(
        summary.get("reason_state") or ""
    ).strip().lower()
    effective_reason_state = (
        persisted_reason_state
        if persisted_status in {"needs_review", "conflict"}
        and persisted_reason_state in {"needs_review", "conflict"}
        else read_model.reason_state
    )
    effective_reason_codes = sorted(
        {
            *read_model.lifecycle_reason_codes,
            *(
                str(item)
                for item in summary.get("lifecycle_reason_codes") or []
                if str(item or "").strip()
            ),
        }
    )
    effective_close_reason = (
        str(summary.get("close_reason") or "").strip()
        if effective_reason_state in {"needs_review", "conflict"}
        else ""
    ) or read_model.close_reason
    return {
        "schema_version": "option_lifecycle_read_model.v3",
        "lifecycle_state": read_model.lifecycle_state,
        "lifecycle_case_id": case_id,
        "lifecycle_evidence_status": evidence_status,
        "lifecycle_reason_codes": effective_reason_codes,
        "observation_start_ms": read_model.observation_start_ms,
        "pending_until_ms": read_model.pending_until_ms,
        "pairing_until_ms": (
            int(effective_timing["pairing_until_ms"])
            if effective_timing is not None
            else None
        ),
        "first_option_close_received_at_ms": (
            int(effective_timing["first_option_close_received_at_ms"])
            if effective_timing is not None
            else None
        ),
        "timing_policy_hash": (
            str(effective_timing["timing_policy_hash"])
            if effective_timing is not None
            else (
                canonical_hash(timing_policy)
                if timing_policy is not None
                else None
            )
        ),
        "timing_error": timing_error,
        "terminal_event_ids": sorted(
            str(item.get("canonical_terminal_event_id") or "").strip()
            for item in effective_allocations
            if str(item.get("canonical_terminal_event_id") or "").strip()
        ),
        "target_contracts_by_lot": target_manifest,
        "resolved_contracts_by_lot": read_model.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": read_model.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            read_model.resolved_contracts_by_terminal_type
        ),
        "reserved_contracts_by_lot": read_model.reserved_contracts_by_lot,
        "closure_fact": read_model.closure_fact,
        "reason_state": effective_reason_state,
        "close_reason": effective_close_reason,
        "allocation_ids": sorted(
            str(item.get("allocation_id") or "").strip()
            for item in effective_allocations
            if str(item.get("allocation_id") or "").strip()
        ),
        "voided_terminal_event_ids": sorted(
            {
                str(item.get("canonical_terminal_event_id") or "").strip()
                for item in allocations
                if str(item.get("canonical_terminal_event_id") or "").strip()
                in set(void_event_ids)
            }
        ),
        "reservation_evidence_ids": sorted(
            str(item.get("source_owner_evidence_id") or "").strip()
            for item in anchor_facts
            if str(item.get("source_owner_evidence_id") or "").strip()
            and any(read_model.reserved_contracts_by_lot.values())
        ),
        "orphan_evidence_ids": [],
        "lifecycle_generation_token": str(
            generation_token.get("generation_token") or ""
        ),
        "actionable": (
            read_model.actionable
            and effective_reason_state
            not in {
                "cause_pending",
                "partially_resolved",
                "needs_review",
                "conflict",
            }
        ),
    }


def _isolated_case_error_model(
    *,
    lifecycle_case: dict[str, Any],
    case_id: str,
    generation_token: dict[str, Any],
    error: LifecycleCaseDataError,
) -> dict[str, Any]:
    return {
        "schema_version": "option_lifecycle_read_model.v3",
        "lifecycle_state": "conflict",
        "lifecycle_case_id": case_id,
        "lifecycle_evidence_status": "conflict",
        "lifecycle_reason_codes": [str(error)],
        "target_contracts_by_lot": dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ),
        "resolved_contracts_by_lot": {},
        "remaining_contracts_by_lot": {},
        "resolved_contracts_by_terminal_type": {},
        "reserved_contracts_by_lot": {},
        "closure_fact": "unknown",
        "reason_state": "conflict",
        "close_reason": None,
        "allocation_ids": [],
        "terminal_event_ids": [],
        "voided_terminal_event_ids": [],
        "reservation_evidence_ids": [],
        "orphan_evidence_ids": [],
        "pairing_until_ms": None,
        "pending_until_ms": None,
        "timing_error": str(error),
        "lifecycle_generation_token": str(
            generation_token.get("generation_token") or ""
        ),
        "actionable": False,
    }


def reconcile_lifecycle_evidence(
    repo: Any,
    *,
    evidence: dict[str, Any],
    case_id: str | None = None,
    target_lot_id: str | None = None,
    apply_changes: bool = False,
    now_ms: int | None = None,
    expected_resolution_revision: int | None = None,
    expected_lifecycle_generation_token: str | None = None,
    correction_void_events: tuple[Any, ...] = (),
    notification_transition_type: str | None = None,
    refresh_read_model: bool = True,
) -> LifecycleReconciliationResult:
    try:
        normalized = _normalize_evidence(evidence)
    except ValueError as exc:
        return _result(
            status="needs_review",
            reasons=(str(exc),),
            case_id=case_id,
            evidence=evidence,
            terminal_type=evidence.get("terminal_type") or evidence.get("evidence_type"),
            apply_changes=apply_changes,
        )
    facts = lifecycle_reconciliation_facts(
        repo,
        evidence_id=str(normalized["evidence_id"]),
    )
    broker_settlement_pair = (
        str(normalized.get("source_type") or "").strip().lower()
        == "broker_settlement_pair"
        and str(normalized.get("terminal_type") or "")
        in {"assignment", "exercise"}
    )
    matches = _matching_cases(
        list(facts["cases"]),
        evidence=normalized,
        case_id=None if broker_settlement_pair else case_id,
        target_lot_id=target_lot_id or normalized.get("target_lot_id"),
    )
    if not matches:
        return _result(
            status="needs_review",
            reasons=("lifecycle_case_not_found",),
            case_id=case_id,
            evidence=normalized,
            terminal_type=normalized["terminal_type"],
            apply_changes=apply_changes,
        )
    if len(matches) != 1:
        return _result(
            status="conflict",
            reasons=("ambiguous_lifecycle_case_match",),
            case_id=None,
            evidence=normalized,
            terminal_type=normalized["terminal_type"],
            apply_changes=apply_changes,
        )
    lifecycle_case = matches[0]
    matched_case_id = str(lifecycle_case.get("case_id") or "")
    facts = lifecycle_reconciliation_facts(
        repo,
        case_id=matched_case_id,
        evidence_id=str(normalized["evidence_id"]),
    )
    lot_fields_by_id = dict(facts["position_lot_fields_by_id"])
    validation_reasons = _validate_evidence_for_case(
        normalized,
        lifecycle_case=lifecycle_case,
        timing_policy=(
            repo.get_trade_lifecycle_timing_policy(
                matched_case_id
            )
            if callable(
                getattr(
                    repo,
                    "get_trade_lifecycle_timing_policy",
                    None,
                )
            )
            else None
        ),
    )
    if validation_reasons:
        return _record_issue_result(
            repo,
            lifecycle_case=lifecycle_case,
            evidence=normalized,
            status="conflict",
            reason_codes=validation_reasons,
            apply_changes=apply_changes,
            now_ms=now_ms,
            expected_lifecycle_generation_token=(
                expected_lifecycle_generation_token
            ),
            refresh_read_model=refresh_read_model,
        )

    allocations = list(facts["allocations"])
    void_event_ids = tuple(
        sorted(
            {
                *(
                    str(item)
                    for item in (
                        facts.get("effective_void_event_ids")
                        or ()
                    )
                    if str(item or "").strip()
                ),
                *(
                    str(
                        getattr(
                            item,
                            "target_event_id",
                            "",
                        )
                        or ""
                    ).strip()
                    for item in correction_void_events
                    if str(
                        getattr(
                            item,
                            "target_event_id",
                            "",
                        )
                        or ""
                    ).strip()
                ),
            }
        )
    )
    evidence_id = str(normalized["evidence_id"])
    evidence_allocations = [
        item
        for item in allocations
        if str(item.get("evidence_id") or "").strip() == evidence_id
    ]
    existing_evidence = facts.get("requested_evidence")
    if existing_evidence is not None and not evidence_allocations:
        return _record_issue_result(
            repo,
            lifecycle_case=lifecycle_case,
            evidence=normalized,
            status="needs_review",
            reason_codes=("evidence_without_allocation",),
            apply_changes=apply_changes,
            now_ms=now_ms,
            expected_lifecycle_generation_token=(
                expected_lifecycle_generation_token
            ),
            refresh_read_model=refresh_read_model,
        )

    resolution = resolve_allocations(
        dict(lifecycle_case.get("target_contracts_by_lot") or {}),
        allocations,
        void_event_ids=void_event_ids,
    )
    terminal_type = str(normalized["terminal_type"])
    if evidence_allocations:
        replay_reasons: set[str] = set()
        if sum(
            int(item.get("contracts_allocated") or 0)
            for item in evidence_allocations
        ) != int(normalized["contracts"]):
            replay_reasons.add("lifecycle_evidence_allocation_replay_conflict")
        if {
            str(item.get("terminal_type") or "").strip().lower()
            for item in evidence_allocations
        } != {terminal_type}:
            replay_reasons.add("lifecycle_evidence_allocation_replay_conflict")
        if replay_reasons:
            return _result(
                status="conflict",
                reasons=tuple(replay_reasons),
                case_id=matched_case_id,
                evidence=normalized,
                terminal_type=terminal_type,
                apply_changes=apply_changes,
            )
        replay_events = [
            _terminal_event(
                lot_fields_by_id,
                lifecycle_case=lifecycle_case,
                evidence=normalized,
                allocation=allocation,
            )
            for allocation in evidence_allocations
        ]
        replay_status = (
            "ledger_written"
            if resolution.remaining_contracts == 0
            else "partially_resolved"
        )
        replay_summary = {
            "target_contracts_by_lot": resolution.target_contracts_by_lot,
            "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
            "remaining_contracts_by_lot": resolution.remaining_contracts_by_lot,
            "resolved_contracts_by_terminal_type": (
                resolution.resolved_contracts_by_terminal_type
            ),
            "reason_state": (
                "resolved"
                if replay_status == "ledger_written"
                else "partially_resolved"
            ),
            "close_reason": _public_close_reason(terminal_type),
        }
        ledger_result = None
        if apply_changes:
            ledger_result = record_lifecycle_allocation(
                repo,
                case_id=matched_case_id,
                evidence=normalized,
                terminal_events=replay_events,
                allocations=evidence_allocations,
                derived_status=replay_status,
                derived_summary=replay_summary,
                expected_resolution_revision=(
                    expected_resolution_revision
                ),
                expected_lifecycle_generation_token=(
                    expected_lifecycle_generation_token
                ),
                correction_void_events=list(
                    correction_void_events
                ),
                notification_transition_type=(
                    notification_transition_type
                ),
            )
        return LifecycleReconciliationResult(
            status="idempotent" if apply_changes else "dry_run",
            reason_codes=(),
            case_id=matched_case_id,
            evidence_id=evidence_id,
            terminal_type=terminal_type,
            apply_changes=apply_changes,
            allocation_plan=tuple(dict(item) for item in evidence_allocations),
            ledger_result=ledger_result,
            lifecycle_read_model=_refreshed_lifecycle_case_read_model(
                repo,
                case_id=matched_case_id,
                now_ms=now_ms,
                refresh=refresh_read_model,
            ),
        )
    existing_terminal_types = set(
        resolution.resolved_contracts_by_terminal_type
    )
    if (
        terminal_type in {"assignment", "exercise"}
        and "expire_close" in existing_terminal_types
    ):
        return _record_issue_result(
            repo,
            lifecycle_case=lifecycle_case,
            evidence=normalized,
            status="conflict",
            reason_codes=("late_settlement_conflicts_with_expire_close",),
            apply_changes=apply_changes,
            now_ms=now_ms,
            expected_lifecycle_generation_token=(
                expected_lifecycle_generation_token
            ),
            refresh_read_model=refresh_read_model,
        )
    plan = plan_evidence_allocation(
        case_id=matched_case_id,
        evidence_id=evidence_id,
        terminal_type=terminal_type,
        contracts=normalized["contracts"],
        remaining_contracts_by_lot=resolution.remaining_contracts_by_lot,
        target_lot_id=target_lot_id or normalized.get("target_lot_id"),
    )
    if plan.status != "planned":
        return _record_issue_result(
            repo,
            lifecycle_case=lifecycle_case,
            evidence=normalized,
            status="conflict",
            reason_codes=plan.reason_codes or ("allocation_plan_failed",),
            apply_changes=apply_changes,
            now_ms=now_ms,
            plan=plan,
            expected_lifecycle_generation_token=(
                expected_lifecycle_generation_token
            ),
            refresh_read_model=refresh_read_model,
        )
    event_rows = [
        _terminal_event(
            lot_fields_by_id,
            lifecycle_case=lifecycle_case,
            evidence=normalized,
            allocation=allocation,
        )
        for allocation in plan.allocations
    ]
    combined_resolution = resolve_allocations(
        dict(lifecycle_case.get("target_contracts_by_lot") or {}),
        [*allocations, *plan.allocations],
        void_event_ids=void_event_ids,
    )
    derived_status = (
        "ledger_written"
        if combined_resolution.remaining_contracts == 0
        else "partially_resolved"
    )
    derived_summary = {
        "target_contracts_by_lot": combined_resolution.target_contracts_by_lot,
        "resolved_contracts_by_lot": combined_resolution.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": combined_resolution.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            combined_resolution.resolved_contracts_by_terminal_type
        ),
        "reason_state": (
            "resolved"
            if derived_status == "ledger_written"
            else "partially_resolved"
        ),
        "close_reason": _public_close_reason(terminal_type),
    }
    if not apply_changes:
        return LifecycleReconciliationResult(
            status="dry_run",
            reason_codes=(),
            case_id=matched_case_id,
            evidence_id=evidence_id,
            terminal_type=terminal_type,
            apply_changes=False,
            allocation_plan=tuple(dict(item) for item in plan.allocations),
            lifecycle_read_model=_refreshed_lifecycle_case_read_model(
                repo,
                case_id=matched_case_id,
                now_ms=now_ms,
                refresh=refresh_read_model,
            ),
        )
    ledger_result = record_lifecycle_allocation(
        repo,
        case_id=matched_case_id,
        evidence=normalized,
        terminal_events=event_rows,
        allocations=[dict(item) for item in plan.allocations],
        derived_status=derived_status,
        derived_summary=derived_summary,
        expected_resolution_revision=expected_resolution_revision,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
        correction_void_events=list(correction_void_events),
        notification_transition_type=notification_transition_type,
    )
    return LifecycleReconciliationResult(
        status="applied",
        reason_codes=(),
        case_id=matched_case_id,
        evidence_id=evidence_id,
        terminal_type=terminal_type,
        apply_changes=True,
        allocation_plan=tuple(dict(item) for item in plan.allocations),
        ledger_result=ledger_result,
        lifecycle_read_model=_refreshed_lifecycle_case_read_model(
            repo,
            case_id=matched_case_id,
            now_ms=now_ms,
            refresh=refresh_read_model,
        ),
    )


def _result(
    *,
    status: str,
    reasons: tuple[str, ...],
    case_id: str | None,
    evidence: dict[str, Any],
    terminal_type: Any,
    apply_changes: bool,
) -> LifecycleReconciliationResult:
    return LifecycleReconciliationResult(
        status=status,
        reason_codes=tuple(sorted(set(str(item) for item in reasons if str(item)))),
        case_id=str(case_id or "").strip() or None,
        evidence_id=str(evidence.get("evidence_id") or "").strip() or None,
        terminal_type=str(terminal_type or "").strip().lower() or None,
        apply_changes=apply_changes,
    )


def _record_issue_result(
    repo: Any,
    *,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
    status: str,
    reason_codes: tuple[str, ...],
    apply_changes: bool,
    now_ms: int | None,
    plan: AllocationPlan | None = None,
    expected_lifecycle_generation_token: str | None = None,
    refresh_read_model: bool = True,
) -> LifecycleReconciliationResult:
    case_id = str(lifecycle_case.get("case_id") or "")
    ledger_result = None
    if apply_changes:
        ledger_result = record_lifecycle_evidence_issue(
            repo,
            case_id=case_id,
            evidence=evidence,
            status=status,
            reason_codes=list(reason_codes),
            expected_lifecycle_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
    return LifecycleReconciliationResult(
        status=status,
        reason_codes=tuple(sorted(set(reason_codes))),
        case_id=case_id,
        evidence_id=str(evidence.get("evidence_id") or "") or None,
        terminal_type=str(evidence.get("terminal_type") or "") or None,
        apply_changes=apply_changes,
        allocation_plan=tuple(
            dict(item) for item in (plan.allocations if plan is not None else ())
        ),
        ledger_result=ledger_result,
        lifecycle_read_model=_refreshed_lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
            refresh=refresh_read_model,
        ),
    )


def _refreshed_lifecycle_case_read_model(
    repo: Any,
    *,
    case_id: str,
    now_ms: int | None,
    refresh: bool,
) -> dict[str, Any] | None:
    if not refresh:
        return None
    return lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=now_ms,
    )


def _matching_cases(
    cases: list[dict[str, Any]],
    *,
    evidence: dict[str, Any],
    case_id: str | None,
    target_lot_id: str | None,
) -> list[dict[str, Any]]:
    requested_case_id = str(case_id or "").strip()
    if requested_case_id:
        candidates = [
            lifecycle_case
            for lifecycle_case in cases
            if str(lifecycle_case.get("case_id") or "").strip()
            == requested_case_id
        ]
    else:
        candidates = list(cases)
    target_lot = str(target_lot_id or "").strip()
    matches: list[dict[str, Any]] = []
    for lifecycle_case in candidates:
        if not isinstance(lifecycle_case, dict):
            continue
        if str(lifecycle_case.get("schema_version") or "").strip() != "lifecycle_case.v2":
            continue
        if target_lot and target_lot not in dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ):
            continue
        if _case_matches_evidence(lifecycle_case, evidence):
            matches.append(dict(lifecycle_case))
    return sorted(matches, key=lambda item: str(item.get("case_id") or ""))


def _case_matches_evidence(
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    try:
        matches = (
            str(lifecycle_case.get("account") or "").strip().lower()
            == evidence["account"]
            and canonical_symbol(lifecycle_case.get("symbol")) == evidence["symbol"]
            and str(lifecycle_case.get("option_type") or "").strip().lower()
            == evidence["option_type"]
            and str(lifecycle_case.get("position_side") or "").strip().lower()
            == evidence["position_side"]
            and Decimal(str(lifecycle_case.get("strike")))
            == Decimal(str(evidence["strike"]))
            and str(lifecycle_case.get("expiration_ymd") or "").strip()
            == evidence["expiration_ymd"]
        )
        if not matches:
            return False
        if (
            str(evidence.get("source_type") or "").strip().lower()
            != "broker_settlement_pair"
        ):
            return True
        stock = dict(evidence.get("stock_settlement") or {})
        return (
            bool(str(lifecycle_case.get("futu_account_id") or "").strip())
            and str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip()
            == str(stock.get("futu_account_id") or "").strip()
        )
    except (InvalidOperation, TypeError, ValueError):
        return False


def _normalize_evidence(raw: dict[str, Any]) -> dict[str, Any]:
    payload = dict(raw or {})
    contract = dict(payload.get("contract_key") or {})
    evidence_id = str(payload.get("evidence_id") or "").strip()
    source_type = str(payload.get("source_type") or "").strip()
    source_event_id = str(payload.get("source_event_id") or "").strip()
    terminal_type = str(
        payload.get("terminal_type") or payload.get("evidence_type") or ""
    ).strip().lower()
    account = str(
        payload.get("account") or contract.get("account") or ""
    ).strip().lower()
    symbol = canonical_symbol(
        payload.get("symbol") or contract.get("underlying_symbol")
    )
    option_type = str(
        payload.get("option_type") or contract.get("option_type") or ""
    ).strip().lower()
    position_side = str(
        payload.get("position_side") or contract.get("position_side") or ""
    ).strip().lower()
    expiration_ymd = str(
        payload.get("expiration_ymd") or contract.get("expiration_ymd") or ""
    ).strip()
    if not evidence_id:
        raise ValueError("evidence_id_missing")
    if not source_type or not source_event_id:
        raise ValueError("evidence_source_identity_missing")
    if terminal_type not in TERMINAL_TYPES:
        raise ValueError("terminal_type_invalid")
    if not account or not symbol or option_type not in {"put", "call"}:
        raise ValueError("evidence_contract_identity_incomplete")
    if position_side not in {"short", "long"} or not expiration_ymd:
        raise ValueError("evidence_contract_identity_incomplete")
    try:
        strike = Decimal(
            str(payload.get("strike") if payload.get("strike") is not None else contract.get("strike"))
        )
        contracts = Decimal(str(payload.get("contracts")))
        event_time_ms = int(
            payload.get("event_time_ms")
            or payload.get("observed_at_ms")
            or 0
        )
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("evidence_quantity_or_time_invalid") from exc
    if (
        not strike.is_finite()
        or not contracts.is_finite()
        or contracts <= 0
        or contracts != contracts.to_integral_value()
        or event_time_ms <= 0
    ):
        raise ValueError("evidence_quantity_or_time_invalid")
    return {
        **payload,
        "case_id": payload.get("case_id"),
        "evidence_id": evidence_id,
        "source_type": source_type,
        "source_event_id": source_event_id,
        "evidence_type": terminal_type,
        "terminal_type": terminal_type,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "position_side": position_side,
        "strike": float(strike),
        "expiration_ymd": expiration_ymd,
        "contracts": int(contracts),
        "event_time_ms": event_time_ms,
        "target_lot_id": str(payload.get("target_lot_id") or "").strip() or None,
    }


def _validate_evidence_for_case(
    evidence: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
    timing_policy: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if not _case_matches_evidence(lifecycle_case, evidence):
        reasons.add("evidence_contract_identity_mismatch")
    terminal_type = str(evidence.get("terminal_type") or "")
    option_type = str(lifecycle_case.get("option_type") or "").strip().lower()
    position_side = str(lifecycle_case.get("position_side") or "").strip().lower()
    if terminal_type == "assignment" and position_side != "short":
        reasons.add("assignment_requires_short_option")
    if terminal_type == "exercise" and position_side != "long":
        reasons.add("exercise_requires_long_option")
    if terminal_type in {"assignment", "exercise"}:
        stock = dict(evidence.get("stock_settlement") or {})
        broker_pair = (
            str(
                evidence.get("source_type") or ""
            ).strip().lower()
            == "broker_settlement_pair"
        )
        if broker_pair:
            case_futu_account_id = str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip()
            stock_futu_account_id = str(
                stock.get("futu_account_id") or ""
            ).strip()
            if (
                not case_futu_account_id
                or not stock_futu_account_id
                or case_futu_account_id
                != stock_futu_account_id
            ):
                reasons.add(
                    "stock_settlement_futu_account_mismatch"
                )
        stock_side = _stock_side(stock.get("side"))
        expected_side = {
            ("assignment", "put", "short"): "buy",
            ("assignment", "call", "short"): "sell",
            ("exercise", "call", "long"): "buy",
            ("exercise", "put", "long"): "sell",
        }.get((terminal_type, option_type, position_side))
        if not expected_side or stock_side != expected_side:
            reasons.add("stock_settlement_side_mismatch")
        try:
            shares = Decimal(str(stock.get("shares")))
            multiplier = Decimal(str(lifecycle_case.get("multiplier") or 100))
            expected_shares = multiplier * int(evidence["contracts"])
        except (InvalidOperation, TypeError, ValueError):
            reasons.add("stock_settlement_quantity_invalid")
        else:
            if (
                not shares.is_finite()
                or shares <= 0
                or shares != expected_shares
            ):
                reasons.add("stock_settlement_quantity_mismatch")
        stock_symbol = canonical_symbol(stock.get("symbol"))
        if stock_symbol and stock_symbol != canonical_symbol(
            lifecycle_case.get("symbol")
        ):
            reasons.add("stock_settlement_symbol_mismatch")
        if broker_pair:
            try:
                stock_price = Decimal(str(stock.get("price")))
                strike = Decimal(
                    str(lifecycle_case.get("strike"))
                )
            except (InvalidOperation, TypeError, ValueError):
                reasons.add("stock_settlement_price_invalid")
            else:
                if (
                    not stock_price.is_finite()
                    or not strike.is_finite()
                    or stock_price != strike
                ):
                    reasons.add(
                        "stock_settlement_price_mismatch"
                    )
        try:
            settlement_time_ms = int(
                stock.get("event_time_ms")
                or stock.get("observed_at_ms")
                or evidence.get("event_time_ms")
                or 0
            )
        except (TypeError, ValueError, OverflowError):
            settlement_time_ms = 0
        observation_start = lifecycle_case.get("observation_start_ms")
        try:
            option_event_time_ms = int(
                evidence.get("option_event_time_ms") or 0
            )
        except (TypeError, ValueError, OverflowError):
            option_event_time_ms = 0
        if settlement_time_ms <= 0:
            reasons.add("stock_settlement_time_missing")
        elif (
            observation_start is not None
            and settlement_time_ms
            < int(observation_start) - EARLY_SETTLEMENT_TOLERANCE_MS
            and (
                option_event_time_ms <= 0
                or abs(settlement_time_ms - option_event_time_ms)
                > EARLY_SETTLEMENT_TOLERANCE_MS
            )
        ):
            reasons.add("stock_settlement_before_lifecycle_window")
        if broker_pair:
            deadline_ms = 0
            if isinstance(timing_policy, dict):
                try:
                    deadline_ms = int(
                        timing_policy.get(
                            "settlement_deadline_ms"
                        )
                        or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    deadline_ms = 0
            near_option_event = (
                option_event_time_ms > 0
                and abs(
                    settlement_time_ms
                    - option_event_time_ms
                )
                <= EARLY_SETTLEMENT_TOLERANCE_MS
            )
            if deadline_ms <= 0 and not near_option_event:
                reasons.add("settlement_deadline_unavailable")
            elif (
                deadline_ms > 0
                and settlement_time_ms > deadline_ms
            ):
                reasons.add("stock_settlement_after_deadline")
    return tuple(sorted(reasons))


def _terminal_event(
    lot_fields_by_id: dict[str, dict[str, Any]],
    *,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
    allocation: dict[str, Any],
) -> TradeEvent:
    lot_id = str(allocation.get("target_lot_id") or "")
    terminal_type = str(allocation.get("terminal_type") or "").strip().lower()
    try:
        fields = dict(lot_fields_by_id[lot_id])
    except KeyError as exc:
        raise ValueError(
            f"lifecycle target lot not found: {lot_id}"
        ) from exc
    contract_key = ContractKey.from_values(
        broker=lifecycle_case.get("broker") or fields.get("broker"),
        account=lifecycle_case.get("account") or fields.get("account"),
        underlying_symbol=lifecycle_case.get("symbol") or fields.get("symbol"),
        option_type=lifecycle_case.get("option_type") or fields.get("option_type"),
        position_side=(
            lifecycle_case.get("position_side")
            or fields.get("position_side")
            or fields.get("side")
        ),
        strike=lifecycle_case.get("strike") or fields.get("strike"),
        expiration_ymd=(
            lifecycle_case.get("expiration_ymd")
            or fields.get("expiration_ymd")
        ),
    )
    contracts = int(allocation.get("contracts_allocated") or 0)
    event_price = (
        float(evidence.get("price") or 0)
        if terminal_type == "close"
        else 0.0
    )
    if terminal_type == "close" and event_price <= 0:
        raise ValueError(
            "trade_close requires a positive broker execution price"
        )
    return TradeEvent(
        event_id=str(allocation.get("canonical_terminal_event_id") or ""),
        event_type=terminal_type,
        event_time_ms=int(evidence.get("event_time_ms") or 0),
        contract_key=contract_key,
        contracts=contracts,
        price=event_price,
        currency=str(evidence.get("currency") or fields.get("currency") or ""),
        source="lifecycle_reconciliation",
        multiplier=float(
            lifecycle_case.get("multiplier")
            or fields.get("multiplier")
            or 100
        ),
        target_lot_id=lot_id,
        raw_payload={
            "schema_version": "lifecycle_terminal_event.v2",
            "source": "om option lifecycle",
            "record_id": lot_id,
            "target_lot_id": lot_id,
            "close_type": (
                "expire_auto_close"
                if terminal_type == "expire_close"
                else terminal_type
            ),
            "close_reason": terminal_type,
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "evidence_id": str(evidence.get("evidence_id") or ""),
            "allocation_id": str(allocation.get("allocation_id") or ""),
            "contracts": contracts,
            "source_type": str(evidence.get("source_type") or ""),
            "source_event_id": str(evidence.get("source_event_id") or ""),
            "stock_settlement": dict(evidence.get("stock_settlement") or {}),
        },
    )


def _stock_side(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"buy", "bought", "buy_to_open", "buy_to_close", "买入", "買入"}:
        return "buy"
    if raw in {"sell", "sold", "sell_to_open", "sell_to_close", "卖出", "賣出"}:
        return "sell"
    return raw


def _public_close_reason(terminal_type: str) -> str:
    return {
        "close": "trade_close",
        "assignment": "assignment",
        "exercise": "exercise",
        "expire_close": "expiration_no_settlement",
    }.get(str(terminal_type or "").strip().lower(), "")


__all__ = [
    "EARLY_SETTLEMENT_TOLERANCE_MS",
    "LifecycleReconciliationResult",
    "discover_lifecycle_cases",
    "lifecycle_case_read_model",
    "lifecycle_case_read_models_for_account",
    "reconcile_lifecycle_evidence",
]
