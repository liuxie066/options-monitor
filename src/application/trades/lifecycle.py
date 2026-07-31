from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol, normalize_contract_expiration
from src.application.ledger.api import (
    accept_option_close_evidence,
    BrokerTradeOperation,
    canonical_source_economic_payload,
    canonical_source_payload_hash,
    LotCloseResolutionError,
    record_lifecycle_assignment,
    record_lifecycle_exercise,
)
from domain.domain.symbol_identity import symbol_market
from src.application.trades.deal_identity import active_ledger_events, broker_deal_key
from src.application.trades.lifecycle_reconciliation import (
    reconcile_lifecycle_evidence,
)
from src.application.trades.normalizer import NormalizedTradeDeal


ASSIGNMENT_WAITING_STATUS = "waiting_settlement_evidence"
PENDING_STATUSES = {
    "pending",
    ASSIGNMENT_WAITING_STATUS,
    "needs_review",
    "partially_resolved",
}
FINAL_STATUSES = {"ledger_written"}
EARLY_LIFECYCLE_STOCK_OPTION_WINDOW_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class LifecycleTradeResolution:
    handled: bool
    status: str
    action: str | None
    reason: str
    operations: list[BrokerTradeOperation] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def resolve_lifecycle_trade_deal(
    deal: NormalizedTradeDeal,
    *,
    repo: Any,
    apply_changes: bool,
) -> LifecycleTradeResolution | None:
    if _is_stock_settlement_leg(deal):
        evidence = _evidence_from_deal(deal, evidence_type="stock_settlement_leg", case_id=None)
        get_evidence = getattr(
            repo,
            "get_trade_lifecycle_evidence",
            None,
        )
        existing = (
            get_evidence(str(evidence.get("evidence_id") or ""))
            if callable(get_evidence)
            else None
        )
        if (
            not isinstance(existing, dict)
            and not _stock_settlement_has_lifecycle_context(
                repo,
                stock_evidence=evidence,
            )
        ):
            return None
        return _resolve_stock_settlement_leg(deal, repo=repo, apply_changes=apply_changes)
    if _is_zero_price_option_close(deal):
        return _resolve_zero_price_option_close(deal, repo=repo, apply_changes=apply_changes)
    return None


def lifecycle_deal_economic_hash(
    deal: NormalizedTradeDeal,
) -> str | None:
    if _is_stock_settlement_leg(deal):
        role = "stock_settlement"
        payload = _evidence_from_deal(
            deal,
            evidence_type="stock_settlement_leg",
            case_id=None,
        )
    elif _is_zero_price_option_close(deal):
        role = "option_anchor"
        payload = {
            **deal.to_dict(),
            "account": normalize_account(deal.internal_account),
            "futu_account_id": str(
                deal.futu_account_id or ""
            ).strip(),
            "event_time_ms": int(deal.trade_time_ms or 0),
        }
    else:
        return None
    source_key = str(broker_deal_key(deal) or "").strip()
    if not source_key:
        return None
    canonical = canonical_source_economic_payload(
        source_key=source_key,
        source_role=role,
        payload=payload,
    )
    return canonical_source_payload_hash(canonical)


def _resolve_zero_price_option_close(
    deal: NormalizedTradeDeal,
    *,
    repo: Any,
    apply_changes: bool,
) -> LifecycleTradeResolution:
    if not apply_changes:
        return _preview_zero_price_option_close(deal, repo=repo)

    source_event_id = str(broker_deal_key(deal) or "").strip()
    evidence = _evidence_from_deal(
        deal,
        evidence_type="option_zero_price_close",
        case_id=None,
    )
    evidence.update(
        {
            "source_event_id": source_event_id,
            "account": normalize_account(deal.internal_account),
            "futu_account_id": str(deal.futu_account_id or "").strip(),
            "symbol": canonical_contract_symbol(deal.symbol),
            "option_type": normalize_option_type(deal.option_type),
            "position_side": _close_position_side(deal),
            "strike": deal.strike,
            "expiration_ymd": normalize_contract_expiration(
                deal.expiration_ymd
            ),
            "contracts": int(deal.contracts or 0),
            "price": "0",
            "event_time_ms": int(deal.trade_time_ms or 0),
            "received_at_ms": _source_received_at_ms(deal),
            "order_id": str(deal.order_id or "").strip() or None,
        }
    )
    contract_identity = {
        "broker": normalize_broker(deal.broker or "富途"),
        "account": normalize_account(deal.internal_account),
        "futu_account_id": str(deal.futu_account_id or "").strip(),
        "symbol": canonical_contract_symbol(deal.symbol),
        "option_type": normalize_option_type(deal.option_type),
        "position_side": _close_position_side(deal),
        "strike": deal.strike,
        "expiration_ymd": normalize_contract_expiration(
            deal.expiration_ymd
        ),
        "market": symbol_market(deal.symbol),
        "currency": deal.currency,
        "multiplier": deal.multiplier or 100,
    }
    try:
        accepted = accept_option_close_evidence(
            repo,
            contract_identity=contract_identity,
            evidence=evidence,
            apply_changes=apply_changes,
        )
    except ValueError as exc:
        reason = str(exc) or "option_close_evidence_not_accepted"
        retryable = reason in {
            "lifecycle_close_target_not_found",
            "target_lot_quantity_drift",
        }
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason=reason,
            operations=[],
            diagnostics={
                "retryable": retryable,
                "broker_evidence_accepted": False,
                "lifecycle_schema_version": "lifecycle_case.v2",
            },
        )
    accepted_evidence = dict(accepted.get("lifecycle_evidence") or {})
    lifecycle_case = dict(accepted.get("lifecycle_case") or {})
    matching_stock_evidences = _find_matching_stock_evidences(
        repo,
        option_case=lifecycle_case,
        option_evidence=accepted_evidence,
    )
    if matching_stock_evidences:
        results = [
            _write_lifecycle_close_from_case(
                repo,
                case=_case_with_option_evidence_context(
                    lifecycle_case,
                    accepted_evidence,
                ),
                decision_type=str(
                    _lifecycle_decision(
                        _case_with_option_evidence_context(
                            lifecycle_case,
                            accepted_evidence,
                        ),
                        stock_evidence=stock_evidence,
                    )["decision_type"]
                ),
                option_evidence=accepted_evidence,
                stock_evidence=stock_evidence,
                apply_changes=True,
            )
            for stock_evidence in matching_stock_evidences
        ]
        final = results[-1]
        return LifecycleTradeResolution(
            handled=True,
            status=final.status,
            action=final.action,
            reason=final.reason,
            operations=[
                operation
                for result in results
                for operation in result.operations
            ],
            diagnostics={
                **dict(final.diagnostics),
                "broker_evidence_accepted": True,
                "lifecycle_adoption": accepted,
                "settlement_results": [
                    {
                        "status": result.status,
                        "reason": result.reason,
                        "operation_count": len(result.operations),
                    }
                    for result in results
                ],
            },
        )
    target_manifest = dict(
        accepted_evidence.get("target_contracts_by_lot") or {}
    )
    operations = [
        BrokerTradeOperation(
            action="reserve_option_close",
            record_id=lot_id,
            contracts_to_close=int(contracts),
            details={
                "lifecycle_case_id": accepted.get("case_id"),
                "lifecycle_evidence_id": accepted.get("evidence_id"),
                "lifecycle_schema_version": "lifecycle_case.v2",
                "projection_changed": False,
            },
        )
        for lot_id, contracts in sorted(target_manifest.items())
    ]
    return LifecycleTradeResolution(
        handled=True,
        status="unresolved",
        action="lifecycle",
        reason="waiting_settlement_evidence",
        operations=operations,
        diagnostics={
            "retryable": False,
            "broker_evidence_accepted": bool(
                accepted.get("broker_evidence_accepted")
            ),
            "lifecycle_schema_version": "lifecycle_case.v2",
            "lifecycle_adoption": accepted,
        },
    )


def _preview_zero_price_option_close(
    deal: NormalizedTradeDeal,
    *,
    repo: Any,
) -> LifecycleTradeResolution:
    case = _case_from_option_deal(deal)
    evidence = _evidence_from_deal(
        deal,
        evidence_type="option_zero_price_close",
        case_id=case["case_id"],
    )
    stock_evidence = _find_matching_stock_evidence(
        repo,
        option_case=case,
        option_evidence=evidence,
    )
    decision = _lifecycle_decision(case, stock_evidence=stock_evidence)
    diagnostics = {
        "lifecycle_case": case,
        "lifecycle_evidence": evidence,
        "decision": decision,
        "matching_stock_evidence": stock_evidence,
        "broker_evidence_accepted": False,
        "lifecycle_schema_version": "lifecycle_case.v2",
    }
    decision_type = str(decision.get("decision_type") or "")
    return LifecycleTradeResolution(
        handled=True,
        status="dry_run",
        action=(
            decision_type
            if decision_type in {"assignment", "exercise"}
            else "lifecycle"
        ),
        reason=(
            f"preview_{decision_type}"
            if decision_type in {"assignment", "exercise"}
            else "waiting_settlement_evidence"
        ),
        operations=[
            _lifecycle_operation(
                (
                    f"{decision_type}_preview"
                    if decision_type in {"assignment", "exercise"}
                    else "lifecycle_pending"
                ),
                diagnostics,
            )
        ],
        diagnostics=diagnostics,
    )


def _resolve_stock_settlement_leg(
    deal: NormalizedTradeDeal,
    *,
    repo: Any,
    apply_changes: bool,
) -> LifecycleTradeResolution:
    evidence = _evidence_from_deal(deal, evidence_type="stock_settlement_leg", case_id=None)
    return _resolve_stock_settlement_evidence(
        evidence,
        repo=repo,
        apply_changes=apply_changes,
    )


def reconcile_polled_stock_settlement_evidence(
    repo: Any,
    *,
    evidence: dict[str, Any],
    apply_changes: bool,
    expected_lifecycle_generation_token: str | None = None,
) -> LifecycleTradeResolution:
    payload = dict(evidence or {})
    if (
        str(payload.get("evidence_type") or "").strip().lower()
        != "stock_settlement_leg"
    ):
        raise ValueError(
            "polled lifecycle evidence must be a stock settlement leg"
        )
    return _resolve_stock_settlement_evidence(
        payload,
        repo=repo,
        apply_changes=apply_changes,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
    )


def _resolve_stock_settlement_evidence(
    evidence: dict[str, Any],
    *,
    repo: Any,
    apply_changes: bool,
    expected_lifecycle_generation_token: str | None = None,
) -> LifecycleTradeResolution:
    matching_cases = _find_matching_option_cases(
        repo,
        stock_evidence=evidence,
        statuses=PENDING_STATUSES | FINAL_STATUSES,
    )
    matching_case = matching_cases[0] if len(matching_cases) == 1 else None
    observed_case_id = str(
        evidence.get("observed_case_id") or ""
    ).strip()
    diagnostics = {
        "lifecycle_evidence": evidence,
        "matching_lifecycle_case": matching_case,
        "matching_case_ids": [
            str(item.get("case_id") or "")
            for item in matching_cases
        ],
    }
    if not apply_changes:
        return LifecycleTradeResolution(
            handled=True,
            status="dry_run",
            action="lifecycle",
            reason="preview_stock_settlement_evidence",
            operations=[_lifecycle_operation("stock_settlement_preview", diagnostics)],
            diagnostics=diagnostics,
        )

    # Polled reconciliation carries a prepared generation token and must not
    # create an unbound evidence row before the transaction-local CAS.  The
    # paired lifecycle evidence is persisted later by the atomic writer.
    if not str(expected_lifecycle_generation_token or "").strip():
        try:
            _persist_broker_evidence_once(repo, evidence)
        except ValueError as exc:
            return LifecycleTradeResolution(
                handled=True,
                status="unresolved",
                action="lifecycle",
                reason="broker_evidence_economic_conflict",
                operations=[
                    _lifecycle_operation(
                        "lifecycle_evidence_conflict",
                        {**diagnostics, "error": str(exc)},
                    )
                ],
                diagnostics={
                    **diagnostics,
                    "error": str(exc),
                    "retryable": False,
                },
            )
    if len(matching_cases) > 1:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason="ambiguous_lifecycle_case_match",
            operations=[
                _lifecycle_operation(
                    "lifecycle_case_match_conflict",
                    diagnostics,
                )
            ],
            diagnostics={**diagnostics, "retryable": False},
        )
    if (
        matching_case is not None
        and observed_case_id
        and str(
            matching_case.get("case_id") or ""
        ).strip()
        != observed_case_id
    ):
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason="polled_settlement_case_mismatch",
            operations=[
                _lifecycle_operation(
                    "lifecycle_case_match_conflict",
                    diagnostics,
                )
            ],
            diagnostics={**diagnostics, "retryable": False},
        )
    if not matching_case:
        related_case = _find_contract_related_option_case(
            repo,
            stock_evidence=evidence,
        )
        related_anchor = (
            _first_option_evidence(
                repo,
                str(related_case.get("case_id") or ""),
            )
            if isinstance(related_case, dict)
            else None
        )
        outside_window = related_anchor is not None
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason=(
                "stock_settlement_outside_lifecycle_window"
                if outside_window
                else "stock_settlement_waiting_option_leg"
            ),
            operations=[
                _lifecycle_operation(
                    (
                        "stock_settlement_needs_review"
                        if outside_window
                        else "stock_settlement_pending"
                    ),
                    diagnostics,
                )
            ],
            diagnostics={
                **diagnostics,
                "retryable": not outside_window,
            },
        )
    option_evidence = (
        dict(matching_case.get("_matched_option_evidence") or {})
        or _first_option_evidence(repo, matching_case["case_id"])
    )
    matching_case = {
        key: value
        for key, value in matching_case.items()
        if key != "_matched_option_evidence"
    }
    matching_case = _case_with_option_evidence_context(
        matching_case,
        option_evidence,
    )
    decision = _lifecycle_decision(matching_case, stock_evidence=evidence)
    if decision["decision_type"] not in {"assignment", "exercise"}:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason=str(decision.get("reason") or "stock_settlement_does_not_match_lifecycle"),
            operations=[_lifecycle_operation("lifecycle_needs_review", {**diagnostics, "decision": decision})],
            diagnostics={**diagnostics, "decision": decision, "retryable": True},
        )
    return _write_lifecycle_close_from_case(
        repo,
        case=matching_case,
        decision_type=str(decision["decision_type"]),
        option_evidence=option_evidence,
        stock_evidence=evidence,
        apply_changes=True,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
    )


def _write_lifecycle_close_from_case(
    repo: Any,
    *,
    case: dict[str, Any],
    decision_type: str,
    option_evidence: dict[str, Any] | None,
    stock_evidence: dict[str, Any] | None,
    apply_changes: bool,
    expected_lifecycle_generation_token: str | None = None,
) -> LifecycleTradeResolution:
    if not apply_changes:
        raise ValueError("lifecycle close write requires apply_changes")
    normalized_decision = str(decision_type or "").strip().lower()
    if normalized_decision not in {"assignment", "exercise"}:
        raise ValueError("lifecycle close decision_type must be assignment or exercise")
    stock = dict(stock_evidence or {})
    event_time_ms = max(
        int(case.get("event_time_ms") or 0),
        int(stock.get("trade_time_ms") or 0),
    ) or None
    v2_result = _write_v2_lifecycle_close_from_case(
        repo,
        case=case,
        decision_type=normalized_decision,
        option_evidence=option_evidence,
        stock_evidence=stock,
        event_time_ms=event_time_ms,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
    )
    if v2_result is not None:
        return v2_result
    settlement_contracts = _stock_settlement_contracts(case, stock)
    case_contracts = int(case.get("contracts") or 0)
    if settlement_contracts < case_contracts:
        diagnostics = {
            "lifecycle_case": case,
            "option_evidence": option_evidence,
            "stock_evidence": stock_evidence,
            "settlement_contracts": settlement_contracts,
            "case_contracts": case_contracts,
            "retryable": True,
        }
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action=normalized_decision,
            reason="partial_settlement_requires_v2_case",
            operations=[
                _lifecycle_operation(
                    f"{normalized_decision}_waiting_v2_case",
                    diagnostics,
                )
            ],
            diagnostics=diagnostics,
        )
    try:
        record_fn = record_lifecycle_assignment if normalized_decision == "assignment" else record_lifecycle_exercise
        ledger_result = record_fn(
            repo,
            broker=case.get("broker") or "富途",
            account=case.get("account"),
            symbol=case.get("symbol"),
            option_type=case.get("option_type"),
            position_side=case.get("position_side"),
            strike=case.get("strike"),
            expiration_ymd=case.get("expiration_ymd"),
            contracts_to_close=int(case.get("contracts") or 0),
            event_time_ms=event_time_ms,
            case_id=str(case.get("case_id") or ""),
            evidence_ids=[
                str(item.get("evidence_id") or "").strip()
                for item in (option_evidence, stock_evidence)
                if isinstance(item, dict) and str(item.get("evidence_id") or "").strip()
            ],
            stock_settlement={
                "source_event_id": stock.get("source_event_id"),
                "side": stock.get("side"),
                "shares": stock.get("stock_qty"),
                "price": stock.get("stock_price"),
            },
        )
    except LotCloseResolutionError as exc:
        conflict_event = _find_conflicting_expire_close_event(repo, case)
        failed = _case_with_decision(
            case,
            status="conflict" if conflict_event else "needs_review",
            decision_type=normalized_decision,
        )
        _upsert_case(repo, failed)
        diagnostics = {
            "lifecycle_case": failed,
            "option_evidence": option_evidence,
            "stock_evidence": stock_evidence,
            "conflict_event": conflict_event,
            "close_target_error": {
                "code": exc.code,
                "message": str(exc),
                "selector": exc.selector.to_dict(),
                "candidates": [item.to_dict() for item in exc.candidates],
                "remaining_contracts": exc.remaining_contracts,
            },
            "retryable": True,
        }
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action=normalized_decision,
            reason=f"{normalized_decision}_after_expire_close_conflict" if conflict_event else f"{normalized_decision}_close_target_unresolved",
            operations=[_lifecycle_operation(f"{normalized_decision}_needs_review", diagnostics)],
            diagnostics=diagnostics,
        )

    close_target_resolution = ledger_result["close_target_resolution"]
    operations = list(ledger_result["operations"])
    written = _case_with_decision(
        case,
        status="ledger_written",
        decision_type=normalized_decision,
        target_lot_ids=list(close_target_resolution.record_ids),
    )
    _upsert_case(repo, written)
    diagnostics = {
        "lifecycle_case": written,
        "option_evidence": option_evidence,
        "stock_evidence": stock_evidence,
        "decision": {"decision_type": normalized_decision},
        "close_target_resolution": close_target_resolution.to_dict(),
    }
    return LifecycleTradeResolution(
        handled=True,
        status="applied",
        action=normalized_decision,
        reason=f"{normalized_decision}_recorded",
        operations=operations,
        diagnostics=diagnostics,
    )


def _write_v2_lifecycle_close_from_case(
    repo: Any,
    *,
    case: dict[str, Any],
    decision_type: str,
    option_evidence: dict[str, Any] | None,
    stock_evidence: dict[str, Any],
    event_time_ms: int | None,
    expected_lifecycle_generation_token: str | None = None,
) -> LifecycleTradeResolution | None:
    option_source_id = str(
        (option_evidence or {}).get("source_event_id") or ""
    ).strip()
    stock_source_id = str(stock_evidence.get("source_event_id") or "").strip()
    evidence_seed = "|".join(
        (
            str(decision_type),
            option_source_id,
            stock_source_id,
            str(case.get("account") or ""),
            str(case.get("symbol") or ""),
            str(case.get("expiration_ymd") or ""),
        )
    )
    evidence_id = _stable_id("ev2", evidence_seed)
    raw_option = (
        dict((option_evidence or {}).get("raw") or {})
        if isinstance((option_evidence or {}).get("raw"), dict)
        else {}
    )
    evidence = {
        "evidence_id": evidence_id,
        "case_id": str(case.get("case_id") or "").strip() or None,
        "source_type": "broker_settlement_pair",
        "source_event_id": "|".join(
            item for item in (option_source_id, stock_source_id) if item
        ),
        "evidence_type": decision_type,
        "terminal_type": decision_type,
        "account": case.get("account"),
        "symbol": case.get("symbol"),
        "option_type": case.get("option_type"),
        "position_side": case.get("position_side"),
        "strike": case.get("strike"),
        "expiration_ymd": case.get("expiration_ymd"),
        "contracts": _stock_settlement_contracts(case, stock_evidence),
        "event_time_ms": int(event_time_ms or 0),
        "option_event_time_ms": int(
            (option_evidence or {}).get("event_time_ms")
            or (option_evidence or {}).get("trade_time_ms")
            or 0
        ),
        "currency": raw_option.get("currency"),
        "stock_settlement": {
            "source_event_id": stock_source_id,
            "futu_account_id": stock_evidence.get("futu_account_id"),
            "symbol": stock_evidence.get("symbol"),
            "side": stock_evidence.get("side"),
            "shares": abs(int(stock_evidence.get("stock_qty") or 0)),
            "price": stock_evidence.get("stock_price"),
            "event_time_ms": int(stock_evidence.get("trade_time_ms") or 0),
            "order_id": stock_evidence.get("order_id"),
            "clearing_date": stock_evidence.get("clearing_date"),
        },
        "source_evidence_ids": sorted(
            str(item.get("evidence_id") or "").strip()
            for item in (option_evidence, stock_evidence)
            if isinstance(item, dict) and str(item.get("evidence_id") or "").strip()
        ),
    }
    result = reconcile_lifecycle_evidence(
        repo,
        evidence=evidence,
        apply_changes=True,
        now_ms=int(event_time_ms or 0) or None,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
    )
    if (
        result.status == "needs_review"
        and result.reason_codes == ("lifecycle_case_not_found",)
    ):
        return None
    diagnostics = {
        "lifecycle_v2": result.to_dict(),
        "option_evidence": option_evidence,
        "stock_evidence": stock_evidence,
    }
    if result.status in {"applied", "idempotent"}:
        read_model = (
            dict(result.lifecycle_read_model)
            if isinstance(result.lifecycle_read_model, dict)
            else {}
        )
        remaining_by_lot = dict(read_model.get("remaining_contracts_by_lot") or {})
        partially_resolved = any(
            int(value or 0) > 0 for value in remaining_by_lot.values()
        )
        if not _is_v2_lifecycle_case(case):
            legacy_mirror = _case_with_decision(
                case,
                status=(
                    "partially_resolved"
                    if partially_resolved
                    else "ledger_written"
                ),
                decision_type=decision_type,
                target_lot_ids=[
                    str(item.get("target_lot_id") or "").strip()
                    for item in result.allocation_plan
                    if str(item.get("target_lot_id") or "").strip()
                ],
            )
            legacy_mirror["linked_v2_case_id"] = result.case_id
            _upsert_case(repo, legacy_mirror)
        operations = [
            BrokerTradeOperation(
                action=f"record_{decision_type}",
                record_id=str(item.get("target_lot_id") or "").strip() or None,
                contracts_to_close=int(item.get("contracts_allocated") or 0),
                event_id=str(
                    item.get("canonical_terminal_event_id") or ""
                ).strip()
                or None,
                details={
                    "lifecycle_case_id": result.case_id,
                    "lifecycle_evidence_id": result.evidence_id,
                    "lifecycle_allocation_id": item.get("allocation_id"),
                    "lifecycle_schema_version": "lifecycle_case.v2",
                },
            )
            for item in result.allocation_plan
        ]
        return LifecycleTradeResolution(
            handled=True,
            status="applied" if result.status == "applied" else "skipped",
            action=decision_type,
            reason=(
                (
                    f"{decision_type}_partially_recorded"
                    if partially_resolved
                    else f"{decision_type}_recorded"
                )
                if result.status == "applied"
                else "lifecycle_already_written_v2"
            ),
            operations=operations,
            diagnostics=diagnostics,
        )
    if not _is_v2_lifecycle_case(case):
        legacy_mirror = _case_with_decision(
            case,
            status=(
                "conflict"
                if result.status == "conflict"
                else "needs_review"
            ),
            decision_type=decision_type,
        )
        legacy_mirror["linked_v2_case_id"] = result.case_id
        _upsert_case(repo, legacy_mirror)
    return LifecycleTradeResolution(
        handled=True,
        status="unresolved",
        action=decision_type,
        reason=(
            result.reason_codes[0]
            if result.reason_codes
            else "lifecycle_v2_requires_review"
        ),
        operations=[_lifecycle_operation("lifecycle_v2_requires_review", diagnostics)],
        diagnostics={**diagnostics, "retryable": result.status == "needs_review"},
    )


def resolve_lifecycle_expired_unassigned(
    repo: Any,
    *,
    case_id: str | None = None,
    deal_id: str | None = None,
    apply_changes: bool,
) -> LifecycleTradeResolution:
    del repo, apply_changes
    return LifecycleTradeResolution(
        handled=True,
        status="unresolved",
        action="expire_close",
        reason="manual_expiration_confirmation_retired",
        operations=[],
        diagnostics={
            "case_id": case_id,
            "deal_id": deal_id,
            "retryable": False,
            "required_path": (
                "complete broker settlement observation or "
                "lifecycle resolve/correct"
            ),
        },
    )


def _case_from_option_deal(deal: NormalizedTradeDeal) -> dict[str, Any]:
    broker = normalize_broker(deal.broker or "富途")
    account = normalize_account(deal.internal_account)
    symbol = canonical_contract_symbol(deal.symbol)
    option_type = normalize_option_type(deal.option_type)
    position_side = _close_position_side(deal)
    expiration = normalize_contract_expiration(deal.expiration_ymd)
    strike = float(deal.strike) if deal.strike is not None else None
    contracts = int(deal.contracts or 0)
    multiplier = int(deal.multiplier or 100)
    case_key = _case_key(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration,
    )
    return {
        "case_id": _stable_id("lc", case_key),
        "case_key": case_key,
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "position_side": position_side,
        "strike": strike,
        "expiration_ymd": expiration,
        "contracts": contracts,
        "multiplier": multiplier,
        "status": "pending",
        "decision_type": None,
        "target_lot_ids": [],
        "pending_until_ms": None,
        "event_time_ms": int(deal.trade_time_ms or 0),
        "raw": {"option_deal": deal.to_dict()},
    }


def _evidence_from_deal(
    deal: NormalizedTradeDeal,
    *,
    evidence_type: str,
    case_id: str | None,
) -> dict[str, Any]:
    source_event_id = str(broker_deal_key(deal) or "").strip()
    evidence_id = _stable_id("ev", f"{evidence_type}|{source_event_id or deal.to_dict()}")
    raw = deal.to_dict()
    out = {
        "evidence_id": evidence_id,
        "case_id": str(case_id or "").strip() or None,
        # Push and history polling are transport provenance for the same
        # immutable broker deal, not distinct evidence identities.
        "source_type": "futu_broker_deal",
        "source_event_id": source_event_id or None,
        "evidence_type": evidence_type,
        "account": normalize_account(deal.internal_account),
        "futu_account_id": str(deal.futu_account_id or "").strip() or None,
        "symbol": canonical_contract_symbol(deal.symbol),
        "side": deal.side,
        "trade_time_ms": int(deal.trade_time_ms or 0),
        "order_id": str(deal.order_id or "").strip() or None,
        "clearing_date": str(
            deal.raw_payload.get("clearing_date")
            or deal.raw_payload.get("settlement_date")
            or ""
        ).strip()
        or None,
        "raw": raw,
    }
    if evidence_type == "stock_settlement_leg":
        out.update(
            {
                "stock_qty": int(deal.contracts or 0),
                "stock_price": float(deal.price or 0.0),
            }
        )
    return out


def _source_received_at_ms(deal: NormalizedTradeDeal) -> int:
    raw = dict(deal.raw_payload or {})
    source = raw.get("_trade_intake_source")
    source_context = dict(source) if isinstance(source, dict) else {}
    raw_received = str(
        source_context.get("received_at_utc") or ""
    ).strip()
    if raw_received:
        try:
            parsed = datetime.fromisoformat(
                raw_received.replace("Z", "+00:00")
            )
            if parsed.tzinfo is not None:
                return int(parsed.timestamp() * 1000)
        except ValueError:
            pass
    return int(deal.trade_time_ms or 0)


def _lifecycle_decision(case: dict[str, Any], *, stock_evidence: dict[str, Any] | None) -> dict[str, Any]:
    lifecycle_type = _lifecycle_close_type(case)
    if lifecycle_type and _stock_matches_lifecycle_close(case, stock_evidence):
        return {"decision_type": lifecycle_type, "reason": "matched_stock_settlement_leg"}
    return {"decision_type": "needs_review", "reason": "waiting_settlement_evidence"}


def _get_case_by_key(repo: Any, case_key: str) -> dict[str, Any] | None:
    get_fn = getattr(repo, "get_trade_lifecycle_case_by_key", None)
    if not callable(get_fn):
        return None
    try:
        row = get_fn(case_key)
    except Exception:
        return None
    return dict(row) if isinstance(row, dict) else None


def _find_matching_stock_evidence(
    repo: Any,
    *,
    option_case: dict[str, Any],
    option_evidence: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    rows = _find_matching_stock_evidences(
        repo,
        option_case=option_case,
        option_evidence=option_evidence,
    )
    return rows[-1] if rows else None


def _find_matching_stock_evidences(
    repo: Any,
    *,
    option_case: dict[str, Any],
    option_evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    list_fn = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return []
    match_case = _case_with_option_evidence_context(
        option_case,
        option_evidence,
    )
    rows = list_fn(account=option_case.get("account"), symbol=option_case.get("symbol"))
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("evidence_type") or "") != "stock_settlement_leg":
            continue
        global_matches = _find_matching_option_cases(
            repo,
            stock_evidence=dict(row),
            statuses=PENDING_STATUSES | FINAL_STATUSES,
        )
        if (
            len(global_matches) == 1
            and str(global_matches[0].get("case_id") or "").strip()
            == str(option_case.get("case_id") or "").strip()
            and _stock_matches_lifecycle_close(match_case, row)
        ):
            out.append(dict(row))
    return sorted(
        out,
        key=lambda item: (
            int(item.get("trade_time_ms") or 0),
            str(item.get("evidence_id") or ""),
        ),
    )


def _find_matching_option_case(
    repo: Any,
    *,
    stock_evidence: dict[str, Any],
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    rows = _find_matching_option_cases(
        repo,
        stock_evidence=stock_evidence,
        statuses=statuses,
    )
    return rows[0] if len(rows) == 1 else None


def _find_matching_option_cases(
    repo: Any,
    *,
    stock_evidence: dict[str, Any],
    statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    list_fn = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_fn):
        return []
    allowed_statuses = set(statuses or PENDING_STATUSES)
    rows = list_fn()
    matches: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "").strip().lower() not in allowed_statuses:
            continue
        if normalize_account(row.get("account")) != normalize_account(stock_evidence.get("account")):
            continue
        if canonical_contract_symbol(row.get("symbol")) != canonical_contract_symbol(stock_evidence.get("symbol")):
            continue
        option_evidence = _matching_option_evidence_for_stock(
            repo,
            lifecycle_case=_case_with_timing_policy(repo, dict(row)),
            stock_evidence=stock_evidence,
        )
        if option_evidence is not None:
            matches.append(
                {
                    **_case_with_timing_policy(repo, dict(row)),
                    "_matched_option_evidence": option_evidence,
                }
            )
    return sorted(
        matches,
        key=lambda item: str(item.get("case_id") or ""),
    )


def _case_with_timing_policy(
    repo: Any,
    lifecycle_case: dict[str, Any],
) -> dict[str, Any]:
    out = dict(lifecycle_case)
    case_id = str(out.get("case_id") or "").strip()
    get_policy = getattr(
        repo,
        "get_trade_lifecycle_timing_policy",
        None,
    )
    if not case_id or not callable(get_policy):
        return out
    policy = get_policy(case_id)
    if isinstance(policy, dict):
        deadline = int(policy.get("settlement_deadline_ms") or 0)
        if deadline > 0:
            out["settlement_deadline_ms"] = deadline
    return out


def _find_contract_related_option_case(
    repo: Any,
    *,
    stock_evidence: dict[str, Any],
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    list_fn = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_fn):
        return None
    allowed_statuses = set(statuses or (PENDING_STATUSES | FINAL_STATUSES))
    rows = list_fn()
    for row in rows:
        if str(row.get("status") or "").strip().lower() not in allowed_statuses:
            continue
        if normalize_account(row.get("account")) != normalize_account(stock_evidence.get("account")):
            continue
        if canonical_contract_symbol(row.get("symbol")) != canonical_contract_symbol(stock_evidence.get("symbol")):
            continue
        if _stock_matches_lifecycle_contract_terms(row, stock_evidence, strict_price=True):
            return dict(row)
    return None


def _stock_settlement_has_lifecycle_context(repo: Any, *, stock_evidence: dict[str, Any]) -> bool:
    if _find_matching_option_cases(
        repo,
        stock_evidence=stock_evidence,
        statuses=PENDING_STATUSES | FINAL_STATUSES,
    ):
        return True
    related_case = _find_contract_related_option_case(
        repo,
        stock_evidence=stock_evidence,
    )
    if related_case is not None:
        option_evidence = _first_option_evidence(
            repo,
            str(related_case.get("case_id") or ""),
        )
        if option_evidence is not None:
            option_time_ms = _lifecycle_case_event_time_ms(
                _case_with_option_evidence_context(
                    related_case,
                    option_evidence,
                )
            )
            stock_time_ms = _stock_trade_time_ms(stock_evidence)
            if (
                option_time_ms > 0
                and stock_time_ms
                < option_time_ms - EARLY_LIFECYCLE_STOCK_OPTION_WINDOW_MS
            ):
                return False
        return True
    list_lots = getattr(repo, "list_position_lots", None)
    if not callable(list_lots):
        return False
    try:
        lots = list_lots()
    except Exception:
        return False
    for item in list(lots or []):
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
        if not isinstance(fields, dict):
            continue
        status = str(fields.get("status") or "").strip().lower()
        close_type = str(fields.get("close_type") or "").strip().lower()
        contracts = effective_contracts_open(fields)
        if status == "close" and close_type in {"expire_auto_close", "expire_close", "expiration_zero_close"}:
            try:
                contracts = int(fields.get("contracts_closed") or fields.get("contracts") or 0)
            except Exception:
                contracts = 0
        elif status != "open":
            continue
        if contracts <= 0:
            continue
        expiration_ymd = effective_expiration_ymd(fields)
        case = {
            "account": normalize_account(fields.get("account")),
            "symbol": canonical_contract_symbol(fields.get("symbol")),
            "option_type": normalize_option_type(fields.get("option_type")),
            "position_side": str(fields.get("side") or "").strip().lower(),
            "strike": effective_strike(fields),
            "expiration_ymd": expiration_ymd,
            "contracts": contracts,
            "multiplier": int(effective_multiplier(fields) or 100),
        }
        if _stock_matches_lifecycle_open_lot_context(case, stock_evidence):
            return True
    return False


def _first_option_evidence(repo: Any, case_id: str) -> dict[str, Any] | None:
    list_fn = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return None
    rows = list_fn(case_id=case_id)
    for row in rows:
        if str(row.get("evidence_type") or "") == "option_zero_price_close":
            return dict(row)
    return None


def _matching_option_evidence_for_stock(
    repo: Any,
    *,
    lifecycle_case: dict[str, Any],
    stock_evidence: dict[str, Any],
) -> dict[str, Any] | None:
    list_fn = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return None
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    rows = list_fn(case_id=case_id) if case_id else []
    candidates = [
        dict(item)
        for item in rows
        if str(item.get("evidence_type") or "").strip().lower()
        == "option_zero_price_close"
        and _stock_matches_lifecycle_close(
            _case_with_option_evidence_context(lifecycle_case, item),
            stock_evidence,
        )
    ]
    if not candidates:
        return None
    stock_time = _stock_trade_time_ms(stock_evidence)
    return min(
        candidates,
        key=lambda item: (
            abs(
                stock_time
                - int(
                    item.get("event_time_ms")
                    or item.get("trade_time_ms")
                    or 0
                )
            ),
            str(item.get("evidence_id") or ""),
        ),
    )


def _case_with_option_evidence_context(
    lifecycle_case: dict[str, Any],
    option_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(lifecycle_case or {})
    evidence = dict(option_evidence or {})
    evidence_contracts = int(evidence.get("contracts") or 0)
    out["contracts"] = (
        evidence_contracts
        if evidence_contracts > 0
        else _lifecycle_case_contracts(out)
    )
    event_time_ms = int(
        evidence.get("event_time_ms")
        or evidence.get("trade_time_ms")
        or 0
    )
    if event_time_ms > 0:
        out["event_time_ms"] = event_time_ms
    return out


def _lifecycle_case_contracts(case: dict[str, Any]) -> int:
    target_manifest = case.get("target_contracts_by_lot")
    if isinstance(target_manifest, dict) and target_manifest:
        try:
            return sum(max(int(value or 0), 0) for value in target_manifest.values())
        except (TypeError, ValueError):
            return 0
    try:
        return max(int(case.get("contracts") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _is_v2_lifecycle_case(case: dict[str, Any]) -> bool:
    return (
        str(case.get("schema_version") or "").strip()
        == "lifecycle_case.v2"
    )


def _find_conflicting_expire_close_event(repo: Any, case: dict[str, Any]) -> dict[str, Any] | None:
    list_events = getattr(repo, "list_trade_events", None)
    if not callable(list_events):
        return None
    try:
        rows = list_events()
    except Exception:
        return None
    for event in reversed(list(rows or [])):
        if not isinstance(event, dict):
            continue
        if str(event.get("event_type") or "").strip().lower() != "expire_close":
            continue
        if normalize_account(event.get("account")) != normalize_account(case.get("account")):
            continue
        if canonical_contract_symbol(event.get("symbol")) != canonical_contract_symbol(case.get("symbol")):
            continue
        if str(event.get("option_type") or "").strip().lower() != str(case.get("option_type") or "").strip().lower():
            continue
        contract_key = event.get("contract_key") if isinstance(event.get("contract_key"), dict) else {}
        event_position_side = str(contract_key.get("position_side") or event.get("position_side") or "").strip().lower()
        if event_position_side != str(case.get("position_side") or "").strip().lower():
            continue
        if normalize_contract_expiration(event.get("expiration_ymd")) != normalize_contract_expiration(case.get("expiration_ymd")):
            continue
        try:
            if abs(float(event.get("strike")) - float(case.get("strike"))) > 1e-9:
                continue
        except Exception:
            continue
        return dict(event)
    return None


def _find_adoptable_expire_close_events(
    repo: Any,
    *,
    case: dict[str, Any],
) -> list[dict[str, Any]]:
    list_events = getattr(repo, "list_trade_events", None)
    if not callable(list_events):
        return []
    try:
        rows = active_ledger_events(list_events())
    except Exception:
        return []

    matches: list[dict[str, Any]] = []
    for event in rows:
        if str(event.get("event_type") or "").strip().lower() != "expire_close":
            continue
        raw = event.get("raw_payload")
        raw_payload = raw if isinstance(raw, dict) else {}
        if any(
            str(raw_payload.get(key) or "").strip()
            for key in ("source_deal_id", "deal_id", "futu_deal_id")
        ):
            continue
        if not _event_matches_lifecycle_case(event, case=case):
            continue
        target_lot_id = str(
            event.get("target_lot_id")
            or raw_payload.get("target_lot_id")
            or raw_payload.get("record_id")
            or ""
        ).strip()
        if not target_lot_id:
            return []
        normalized = dict(event)
        normalized["target_lot_id"] = target_lot_id
        matches.append(normalized)

    expected_contracts = int(case.get("contracts") or 0)
    if expected_contracts <= 0 or not matches:
        return []
    if len({str(item["target_lot_id"]) for item in matches}) != len(matches):
        return []
    if sum(int(item.get("contracts") or 0) for item in matches) != expected_contracts:
        return []
    return sorted(
        matches,
        key=lambda item: (
            int(item.get("event_time_ms") or item.get("trade_time_ms") or 0),
            str(item.get("event_id") or ""),
        ),
    )


def _event_matches_lifecycle_case(
    event: dict[str, Any],
    *,
    case: dict[str, Any],
) -> bool:
    contract_key = event.get("contract_key")
    key = contract_key if isinstance(contract_key, dict) else {}
    if normalize_account(event.get("account") or key.get("account")) != normalize_account(case.get("account")):
        return False
    if canonical_contract_symbol(event.get("symbol") or key.get("underlying_symbol")) != canonical_contract_symbol(case.get("symbol")):
        return False
    if normalize_option_type(event.get("option_type") or key.get("option_type")) != normalize_option_type(case.get("option_type")):
        return False
    event_side = str(
        event.get("position_side")
        or key.get("position_side")
        or ""
    ).strip().lower()
    if event_side != str(case.get("position_side") or "").strip().lower():
        return False
    if normalize_contract_expiration(
        event.get("expiration_ymd") or key.get("expiration_ymd")
    ) != normalize_contract_expiration(case.get("expiration_ymd")):
        return False
    try:
        return abs(float(event.get("strike") or key.get("strike")) - float(case.get("strike"))) <= 1e-9
    except (TypeError, ValueError):
        return False


def _lifecycle_close_type(case: dict[str, Any]) -> str | None:
    option_type = str(case.get("option_type") or "").strip().lower()
    position_side = str(case.get("position_side") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return None
    if position_side == "short":
        return "assignment"
    if position_side == "long":
        return "exercise"
    return None


def _expected_stock_side_for_lifecycle(case: dict[str, Any]) -> str:
    option_type = str(case.get("option_type") or "").strip().lower()
    position_side = str(case.get("position_side") or "").strip().lower()
    if position_side == "short":
        return "buy" if option_type == "put" else "sell" if option_type == "call" else ""
    if position_side == "long":
        return "buy" if option_type == "call" else "sell" if option_type == "put" else ""
    return ""


def _stock_matches_lifecycle_close(case: dict[str, Any], stock_evidence: dict[str, Any] | None) -> bool:
    if not _stock_matches_lifecycle_contract_terms(case, stock_evidence, strict_price=True):
        return False
    stock_trade_time_ms = _stock_trade_time_ms(stock_evidence)
    if stock_trade_time_ms <= 0:
        return False
    if _stock_trade_near_option_event(case, stock_trade_time_ms):
        return True
    try:
        observation_start_ms = int(
            case.get("observation_start_ms") or 0
        )
        settlement_deadline_ms = int(
            case.get("settlement_deadline_ms") or 0
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        observation_start_ms > 0
        and settlement_deadline_ms > 0
        and observation_start_ms
        <= stock_trade_time_ms
        <= settlement_deadline_ms
    ):
        return True
    return (
        settlement_deadline_ms > 0
        and stock_trade_time_ms > settlement_deadline_ms
        and str(case.get("status") or "").strip().lower()
        in FINAL_STATUSES
    )


def _stock_matches_lifecycle_open_lot_context(case: dict[str, Any], stock_evidence: dict[str, Any] | None) -> bool:
    if not _stock_matches_lifecycle_contract_terms(
        case,
        stock_evidence,
        strict_price=True,
        require_futu_account=False,
    ):
        return False
    stock_trade_time_ms = _stock_trade_time_ms(stock_evidence)
    expiration_ymd = normalize_contract_expiration(case.get("expiration_ymd"))
    if not expiration_ymd:
        return False
    if _trade_time_on_or_after_expiration_ymd(stock_trade_time_ms, expiration_ymd):
        return True
    return stock_trade_time_ms > 0


def _stock_matches_lifecycle_contract_terms(
    case: dict[str, Any],
    stock_evidence: dict[str, Any] | None,
    *,
    strict_price: bool,
    require_futu_account: bool = True,
) -> bool:
    if not isinstance(stock_evidence, dict):
        return False
    side = str(stock_evidence.get("side") or "").strip().lower()
    expected_side = _expected_stock_side_for_lifecycle(case)
    if not expected_side:
        return False
    if side != expected_side:
        return False
    case_futu_account_id = str(
        case.get("futu_account_id") or ""
    ).strip()
    evidence_futu_account_id = str(
        stock_evidence.get("futu_account_id") or ""
    ).strip()
    if require_futu_account and (
        not case_futu_account_id
        or not evidence_futu_account_id
        or case_futu_account_id != evidence_futu_account_id
    ):
        return False
    try:
        multiplier = int(case.get("multiplier") or 100)
        expected_qty = _lifecycle_case_contracts(case) * multiplier
        actual_qty = abs(int(stock_evidence.get("stock_qty") or 0))
    except Exception:
        return False
    if (
        expected_qty <= 0
        or actual_qty <= 0
        or actual_qty > expected_qty
        or multiplier <= 0
        or actual_qty % multiplier != 0
    ):
        return False
    try:
        strike = Decimal(str(case.get("strike")))
        price = Decimal(str(stock_evidence.get("stock_price")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not strike.is_finite() or not price.is_finite():
        return False
    return price == strike


def _stock_settlement_contracts(
    case: dict[str, Any],
    stock_evidence: dict[str, Any],
) -> int:
    multiplier = int(case.get("multiplier") or 100)
    shares = abs(int(stock_evidence.get("stock_qty") or 0))
    if multiplier <= 0 or shares <= 0 or shares % multiplier != 0:
        raise ValueError("stock settlement shares must be a positive contract multiple")
    return shares // multiplier


def _stock_trade_time_ms(stock_evidence: dict[str, Any] | None) -> int:
    if not isinstance(stock_evidence, dict):
        return 0
    try:
        return int(stock_evidence.get("trade_time_ms") or 0)
    except Exception:
        return 0


def _stock_trade_near_option_event(case: dict[str, Any], stock_trade_time_ms: int) -> bool:
    if stock_trade_time_ms <= 0:
        return False
    option_trade_time_ms = _lifecycle_case_event_time_ms(case)
    if option_trade_time_ms <= 0:
        return False
    return abs(stock_trade_time_ms - option_trade_time_ms) <= EARLY_LIFECYCLE_STOCK_OPTION_WINDOW_MS


def _lifecycle_case_event_time_ms(case: dict[str, Any]) -> int:
    for raw in (case.get("event_time_ms"),):
        try:
            value = int(raw or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    raw_payload = case.get("raw") if isinstance(case.get("raw"), dict) else {}
    option_deal = raw_payload.get("option_deal") if isinstance(raw_payload.get("option_deal"), dict) else {}
    try:
        return int(option_deal.get("trade_time_ms") or 0)
    except Exception:
        return 0


def _is_stock_settlement_leg(deal: NormalizedTradeDeal) -> bool:
    if getattr(deal, "option_type", None):
        return False
    if not getattr(deal, "symbol", None) or not getattr(
        deal,
        "internal_account",
        None,
    ):
        return False
    if str(getattr(deal, "side", None) or "").strip().lower() not in {
        "buy",
        "sell",
    }:
        return False
    try:
        return int(getattr(deal, "contracts", None) or 0) > 0 and float(
            getattr(deal, "price", None) or 0.0
        ) > 0.0
    except Exception:
        return False


def _is_zero_price_option_close(deal: NormalizedTradeDeal) -> bool:
    if (
        str(getattr(deal, "position_effect", None) or "")
        .strip()
        .lower()
        != "close"
    ):
        return False
    if not getattr(deal, "option_type", None):
        return False
    try:
        if float(getattr(deal, "price")) != 0.0:
            return False
    except Exception:
        return False
    if not normalize_contract_expiration(
        getattr(deal, "expiration_ymd", None)
    ):
        return False
    if not getattr(deal, "trade_time_ms", None):
        return False
    return True


def _trade_time_on_or_after_expiration_ymd(trade_time_ms: int, expiration_ymd: str | None) -> bool:
    expiration = normalize_contract_expiration(expiration_ymd)
    if not expiration:
        return False
    try:
        expiration_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    except ValueError:
        return False
    try:
        ts = int(trade_time_ms or 0)
    except Exception:
        return False
    if ts <= 0:
        return False
    for tz_name in ("America/New_York", "Asia/Shanghai"):
        trade_date = datetime.fromtimestamp(ts / 1000, tz=ZoneInfo(tz_name)).date()
        if trade_date >= expiration_date:
            return True
    return False


def _close_position_side(deal: NormalizedTradeDeal) -> str:
    side = str(deal.side or "").strip().lower()
    if side == "buy":
        return "short"
    if side == "sell":
        return "long"
    return normalize_side(side)


def _case_key(
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    position_side: str,
    strike: float | None,
    expiration_ymd: str | None,
) -> str:
    strike_key = "" if strike is None else f"{float(strike):.6f}".rstrip("0").rstrip(".")
    return "|".join(
        [
            str(broker or ""),
            str(account or ""),
            str(symbol or ""),
            str(option_type or ""),
            str(position_side or ""),
            strike_key,
            str(expiration_ymd or ""),
        ]
    )


def _case_with_decision(
    case: dict[str, Any],
    *,
    status: str,
    decision_type: str | None,
    target_lot_ids: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(case)
    out["status"] = str(status)
    out["decision_type"] = decision_type
    if target_lot_ids is not None:
        out["target_lot_ids"] = list(target_lot_ids)
    return out


def _upsert_case(repo: Any, case: dict[str, Any]) -> bool:
    fn = getattr(repo, "upsert_trade_lifecycle_case", None)
    if not callable(fn):
        return False
    return bool(fn(case))


def _persist_broker_evidence_once(
    repo: Any,
    evidence: dict[str, Any],
) -> bool:
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    source_key = str(
        evidence.get("source_event_id") or ""
    ).strip()
    existing_fn = getattr(
        repo,
        "get_trade_lifecycle_evidence",
        None,
    )
    insert_fn = getattr(
        repo,
        "insert_trade_lifecycle_evidence_once",
        None,
    )
    if (
        not evidence_id
        or not source_key
        or not callable(existing_fn)
        or not callable(insert_fn)
    ):
        return False
    incoming_payload = canonical_source_economic_payload(
        source_key=source_key,
        source_role="stock_settlement",
        payload=evidence,
    )
    existing = existing_fn(evidence_id)
    if isinstance(existing, dict):
        existing_payload = canonical_source_economic_payload(
            source_key=str(
                existing.get("source_event_id") or ""
            ),
            source_role="stock_settlement",
            payload=existing,
        )
        if canonical_source_payload_hash(
            existing_payload
        ) != canonical_source_payload_hash(incoming_payload):
            raise ValueError(
                "lifecycle evidence economic payload conflict"
            )
        return False
    return bool(insert_fn(evidence))


def _lifecycle_operation(action: str, diagnostics: dict[str, Any]) -> BrokerTradeOperation:
    case = diagnostics.get("lifecycle_case") or diagnostics.get("matching_lifecycle_case") or {}
    return BrokerTradeOperation(
        action=action,
        record_id=None,
        details={
            "case_id": case.get("case_id") if isinstance(case, dict) else None,
            "diagnostics": diagnostics,
        },
    )


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


__all__ = [
    "LifecycleTradeResolution",
    "lifecycle_deal_economic_hash",
    "reconcile_polled_stock_settlement_evidence",
    "resolve_lifecycle_expired_unassigned",
    "resolve_lifecycle_trade_deal",
]
