from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.lifecycle_allocation import (
    AllocationPlan,
    plan_evidence_allocation,
    resolve_allocations,
)
from domain.domain.option_lifecycle import derive_lifecycle_read_model
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.ledger.api import (
    discover_expired_lifecycle_cases,
    lifecycle_reconciliation_facts,
    record_lifecycle_allocation,
    record_lifecycle_evidence_issue,
)


EARLY_SETTLEMENT_TOLERANCE_MS = 5 * 60 * 1000
TERMINAL_TYPES = frozenset({"assignment", "exercise", "expire_close"})


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
    facts = lifecycle_reconciliation_facts(repo, case_id=case_id)
    lifecycle_case = next(iter(facts["cases"]), None)
    if lifecycle_case is None:
        raise ValueError(f"lifecycle case not found: {case_id}")
    allocations = list(facts["allocations"])
    evidence = list(facts["evidence"])
    lot_fields_by_id = dict(facts["position_lot_fields_by_id"])
    allocation_evidence_ids = {
        str(item.get("evidence_id") or "").strip()
        for item in allocations
        if str(item.get("evidence_id") or "").strip()
    }
    orphan_evidence_ids = sorted(
        {
            str(item.get("evidence_id") or "").strip()
            for item in evidence
            if str(item.get("evidence_id") or "").strip()
        }
        - allocation_evidence_ids
    )
    resolution = resolve_allocations(
        dict(lifecycle_case.get("target_contracts_by_lot") or {}),
        allocations,
    )
    quantity_drift = False
    for lot_id, expected_remaining in resolution.remaining_contracts_by_lot.items():
        try:
            fields = lot_fields_by_id[lot_id]
            actual_remaining = int(fields.get("contracts_open") or 0)
        except (KeyError, TypeError, ValueError):
            quantity_drift = True
            break
        if actual_remaining != expected_remaining:
            quantity_drift = True
            break
    persisted_status = str(lifecycle_case.get("status") or "").strip().lower()
    derived_summary = dict(lifecycle_case.get("derived_summary") or {})
    conflict_reasons = (
        tuple(str(item) for item in derived_summary.get("lifecycle_reason_codes") or ())
        if persisted_status == "conflict"
        else ()
    )
    read_model = derive_lifecycle_read_model(
        expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
        market=str(
            lifecycle_case.get("market")
            or symbol_market(lifecycle_case.get("symbol"))
            or ""
        ),
        target_contracts_by_lot=dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ),
        allocations=allocations,
        now_ms=now_ms,
        conflict_reason_codes=conflict_reasons,
        orphan_evidence=bool(orphan_evidence_ids),
        quantity_drift=quantity_drift,
    )
    terminal_event_ids = sorted(
        str(item.get("canonical_terminal_event_id") or "").strip()
        for item in allocations
        if str(item.get("canonical_terminal_event_id") or "").strip()
    )
    if persisted_status == "conflict":
        evidence_status = "conflict"
    elif orphan_evidence_ids:
        evidence_status = "evidence_without_allocation"
    elif not evidence:
        evidence_status = "missing"
    elif read_model.remaining_contracts_by_lot and any(
        read_model.remaining_contracts_by_lot.values()
    ):
        evidence_status = "partial"
    else:
        evidence_status = "complete"
    return {
        "schema_version": "option_lifecycle_read_model.v2",
        "lifecycle_state": read_model.lifecycle_state,
        "lifecycle_case_id": str(lifecycle_case.get("case_id") or ""),
        "lifecycle_evidence_status": evidence_status,
        "lifecycle_reason_codes": list(read_model.lifecycle_reason_codes),
        "observation_start_ms": read_model.observation_start_ms,
        "pending_until_ms": read_model.pending_until_ms,
        "terminal_event_ids": terminal_event_ids,
        "target_contracts_by_lot": dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ),
        "resolved_contracts_by_lot": read_model.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": read_model.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            read_model.resolved_contracts_by_terminal_type
        ),
        "allocation_ids": sorted(
            str(item.get("allocation_id") or "").strip()
            for item in allocations
            if str(item.get("allocation_id") or "").strip()
        ),
        "orphan_evidence_ids": orphan_evidence_ids,
        "actionable": read_model.actionable,
    }


def reconcile_lifecycle_evidence(
    repo: Any,
    *,
    evidence: dict[str, Any],
    case_id: str | None = None,
    target_lot_id: str | None = None,
    apply_changes: bool = False,
    now_ms: int | None = None,
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
        case_id=case_id,
        evidence_id=str(normalized["evidence_id"]),
    )
    matches = _matching_cases(
        list(facts["cases"]),
        evidence=normalized,
        case_id=case_id,
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
    if str(case_id or "").strip() != matched_case_id:
        facts = lifecycle_reconciliation_facts(
            repo,
            case_id=matched_case_id,
            evidence_id=str(normalized["evidence_id"]),
        )
    lot_fields_by_id = dict(facts["position_lot_fields_by_id"])
    validation_reasons = _validate_evidence_for_case(
        normalized,
        lifecycle_case=lifecycle_case,
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
        )

    allocations = list(facts["allocations"])
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
        )

    resolution = resolve_allocations(
        dict(lifecycle_case.get("target_contracts_by_lot") or {}),
        allocations,
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
            lifecycle_read_model=lifecycle_case_read_model(
                repo,
                case_id=matched_case_id,
                now_ms=now_ms,
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
            lifecycle_read_model=lifecycle_case_read_model(
                repo,
                case_id=matched_case_id,
                now_ms=now_ms,
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
        lifecycle_read_model=lifecycle_case_read_model(
            repo,
            case_id=matched_case_id,
            now_ms=now_ms,
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
        lifecycle_read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
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
        return (
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
        if settlement_time_ms <= 0:
            reasons.add("stock_settlement_time_missing")
        elif (
            observation_start is not None
            and settlement_time_ms
            < int(observation_start) - EARLY_SETTLEMENT_TOLERANCE_MS
        ):
            reasons.add("stock_settlement_before_lifecycle_window")
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
    return TradeEvent(
        event_id=str(allocation.get("canonical_terminal_event_id") or ""),
        event_type=terminal_type,
        event_time_ms=int(evidence.get("event_time_ms") or 0),
        contract_key=contract_key,
        contracts=contracts,
        price=0,
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


__all__ = [
    "EARLY_SETTLEMENT_TOLERANCE_MS",
    "LifecycleReconciliationResult",
    "discover_lifecycle_cases",
    "lifecycle_case_read_model",
    "reconcile_lifecycle_evidence",
]
