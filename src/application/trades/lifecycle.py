from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
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
    BrokerTradeOperation,
    LotCloseResolutionError,
    preview_lifecycle_expire_close,
    record_lifecycle_assignment,
    record_lifecycle_exercise,
    record_lifecycle_expire_close,
)
from src.application.trades.normalizer import NormalizedTradeDeal


ASSIGNMENT_WAITING_STATUS = "waiting_settlement_evidence"
PENDING_STATUSES = {"pending", ASSIGNMENT_WAITING_STATUS, "needs_review"}
FINAL_STATUSES = {"ledger_written"}


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
        if not _stock_settlement_has_lifecycle_context(repo, stock_evidence=evidence):
            return None
        return _resolve_stock_settlement_leg(deal, repo=repo, apply_changes=apply_changes)
    if _is_zero_price_option_close(deal):
        return _resolve_zero_price_option_close(deal, repo=repo, apply_changes=apply_changes)
    return None


def _resolve_zero_price_option_close(
    deal: NormalizedTradeDeal,
    *,
    repo: Any,
    apply_changes: bool,
) -> LifecycleTradeResolution:
    case = _case_from_option_deal(deal)
    evidence = _evidence_from_deal(deal, evidence_type="option_zero_price_close", case_id=case["case_id"])
    existing_case = _get_case_by_key(repo, str(case.get("case_key") or ""))
    if existing_case and str(existing_case.get("status") or "").strip().lower() in FINAL_STATUSES:
        if apply_changes:
            _upsert_evidence(repo, evidence)
        diagnostics = {
            "lifecycle_case": existing_case,
            "lifecycle_evidence": evidence,
        }
        return LifecycleTradeResolution(
            handled=True,
            status="skipped",
            action=str(existing_case.get("decision_type") or "lifecycle"),
            reason="lifecycle_already_written",
            operations=[_lifecycle_operation("lifecycle_already_written", diagnostics)],
            diagnostics=diagnostics,
        )
    if existing_case and str(existing_case.get("status") or "").strip().lower() == "conflict":
        diagnostics = {
            "lifecycle_case": existing_case,
            "lifecycle_evidence": evidence,
        }
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action=str(existing_case.get("decision_type") or "lifecycle"),
            reason="lifecycle_conflict_requires_review",
            operations=[_lifecycle_operation("lifecycle_conflict", diagnostics)],
            diagnostics={**diagnostics, "retryable": False},
        )
    stock_evidence = _find_matching_stock_evidence(repo, option_case=case)
    decision = _lifecycle_decision(case, stock_evidence=stock_evidence)
    diagnostics = {
        "lifecycle_case": case,
        "lifecycle_evidence": evidence,
        "decision": decision,
        "matching_stock_evidence": stock_evidence,
    }
    if not apply_changes:
        return LifecycleTradeResolution(
            handled=True,
            status="dry_run",
            action=decision["decision_type"] if decision["decision_type"] in {"assignment", "exercise"} else "lifecycle",
            reason=f"preview_{decision['decision_type']}" if decision["decision_type"] in {"assignment", "exercise"} else "waiting_settlement_evidence",
            operations=[_lifecycle_operation(f"{decision['decision_type']}_preview" if decision["decision_type"] in {"assignment", "exercise"} else "lifecycle_pending", diagnostics)],
            diagnostics=diagnostics,
        )

    _upsert_case(repo, case)
    _upsert_evidence(repo, evidence)
    if decision["decision_type"] not in {"assignment", "exercise"}:
        waiting = _case_with_decision(case, status=ASSIGNMENT_WAITING_STATUS, decision_type="needs_review")
        _upsert_case(repo, waiting)
        diagnostics["lifecycle_case"] = waiting
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason="waiting_settlement_evidence",
            operations=[_lifecycle_operation("lifecycle_pending", diagnostics)],
            diagnostics={**diagnostics, "retryable": True},
        )
    return _write_lifecycle_close_from_case(
        repo,
        case=case,
        decision_type=str(decision["decision_type"]),
        option_evidence=evidence,
        stock_evidence=stock_evidence,
        apply_changes=True,
    )


def _resolve_stock_settlement_leg(
    deal: NormalizedTradeDeal,
    *,
    repo: Any,
    apply_changes: bool,
) -> LifecycleTradeResolution:
    evidence = _evidence_from_deal(deal, evidence_type="stock_settlement_leg", case_id=None)
    matching_case = _find_matching_option_case(repo, stock_evidence=evidence)
    final_case = _find_matching_option_case(repo, stock_evidence=evidence, statuses=FINAL_STATUSES)
    diagnostics = {
        "lifecycle_evidence": evidence,
        "matching_lifecycle_case": matching_case or final_case,
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

    _upsert_evidence(repo, evidence)
    if final_case:
        return LifecycleTradeResolution(
            handled=True,
            status="skipped",
            action=str(final_case.get("decision_type") or "lifecycle"),
            reason="lifecycle_already_written",
            operations=[_lifecycle_operation("lifecycle_already_written", diagnostics)],
            diagnostics=diagnostics,
        )
    if not matching_case:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="lifecycle",
            reason="stock_settlement_waiting_option_leg",
            operations=[_lifecycle_operation("stock_settlement_pending", diagnostics)],
            diagnostics={**diagnostics, "retryable": True},
        )
    option_evidence = _first_option_evidence(repo, matching_case["case_id"])
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
    )


def _write_lifecycle_close_from_case(
    repo: Any,
    *,
    case: dict[str, Any],
    decision_type: str,
    option_evidence: dict[str, Any] | None,
    stock_evidence: dict[str, Any] | None,
    apply_changes: bool,
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


def resolve_lifecycle_expired_unassigned(
    repo: Any,
    *,
    case_id: str | None = None,
    deal_id: str | None = None,
    apply_changes: bool,
) -> LifecycleTradeResolution:
    case, option_evidence, reason = _resolve_expiry_case_selector(repo, case_id=case_id, deal_id=deal_id)
    if not case:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="expire_close",
            reason=reason or "lifecycle_case_not_found",
            operations=[],
            diagnostics={"case_id": case_id, "deal_id": deal_id, "retryable": False},
        )
    if not option_evidence:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="expire_close",
            reason="option_zero_price_evidence_not_found",
            operations=[],
            diagnostics={"lifecycle_case": case, "deal_id": deal_id, "retryable": False},
        )

    normalized_status = str(case.get("status") or "").strip().lower()
    decision_type = str(case.get("decision_type") or "").strip().lower()
    diagnostics = {
        "lifecycle_case": case,
        "option_evidence": option_evidence,
    }
    if normalized_status in FINAL_STATUSES:
        return LifecycleTradeResolution(
            handled=True,
            status="skipped",
            action=decision_type or "expire_close",
            reason="lifecycle_already_written",
            operations=[_lifecycle_operation("lifecycle_already_written", diagnostics)],
            diagnostics=diagnostics,
        )
    if normalized_status == "conflict":
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="expire_close",
            reason="lifecycle_conflict_requires_review",
            operations=[_lifecycle_operation("lifecycle_conflict", diagnostics)],
            diagnostics={**diagnostics, "retryable": False},
        )
    if normalized_status not in PENDING_STATUSES:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="expire_close",
            reason=f"unsupported_lifecycle_status:{normalized_status or '-'}",
            operations=[_lifecycle_operation("lifecycle_status_unsupported", diagnostics)],
            diagnostics={**diagnostics, "retryable": False},
        )

    stock_evidence = _find_matching_stock_evidence(repo, option_case=case)
    diagnostics["matching_stock_evidence"] = stock_evidence
    if stock_evidence:
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="expire_close",
            reason="stock_settlement_evidence_present",
            operations=[_lifecycle_operation("lifecycle_expire_close_blocked", diagnostics)],
            diagnostics={**diagnostics, "retryable": False},
        )

    event_time_ms = _expiry_close_event_time_ms(case, option_evidence)
    evidence_ids = [
        str(option_evidence.get("evidence_id") or "").strip(),
    ]
    evidence_ids = [item for item in evidence_ids if item]
    kwargs = {
        "broker": case.get("broker") or "富途",
        "account": case.get("account"),
        "symbol": case.get("symbol"),
        "option_type": case.get("option_type"),
        "position_side": case.get("position_side"),
        "strike": case.get("strike"),
        "expiration_ymd": case.get("expiration_ymd"),
        "contracts_to_close": int(case.get("contracts") or 0),
        "event_time_ms": event_time_ms,
    }
    try:
        if not apply_changes:
            preview = preview_lifecycle_expire_close(repo, **kwargs)
            operations = [BrokerTradeOperation.from_payload(item) for item in preview.get("operations") or []]
            return LifecycleTradeResolution(
                handled=True,
                status="dry_run",
                action="expire_close",
                reason="preview_expire_close",
                operations=operations,
                diagnostics={**diagnostics, "preview": preview, "decision": {"decision_type": "expire_close"}},
            )

        ledger_result = record_lifecycle_expire_close(
            repo,
            **kwargs,
            case_id=str(case.get("case_id") or ""),
            evidence_ids=evidence_ids,
            close_reason="expired_unassigned",
        )
    except LotCloseResolutionError as exc:
        failed = _case_with_decision(case, status="needs_review", decision_type="expire_close")
        if apply_changes:
            _upsert_case(repo, failed)
        error_payload = {
            "code": exc.code,
            "message": str(exc),
            "selector": exc.selector.to_dict(),
            "candidates": [item.to_dict() for item in exc.candidates],
            "remaining_contracts": exc.remaining_contracts,
        }
        return LifecycleTradeResolution(
            handled=True,
            status="unresolved",
            action="expire_close",
            reason="expire_close_target_unresolved",
            operations=[_lifecycle_operation("expire_close_needs_review", {**diagnostics, "lifecycle_case": failed})],
            diagnostics={**diagnostics, "lifecycle_case": failed, "close_target_error": error_payload, "retryable": True},
        )

    close_target_resolution = ledger_result["close_target_resolution"]
    operations = list(ledger_result["operations"])
    written = _case_with_decision(
        case,
        status="ledger_written",
        decision_type="expire_close",
        target_lot_ids=list(close_target_resolution.record_ids),
    )
    _upsert_case(repo, written)
    return LifecycleTradeResolution(
        handled=True,
        status="applied",
        action="expire_close",
        reason="expire_close_recorded",
        operations=operations,
        diagnostics={
            **diagnostics,
            "lifecycle_case": written,
            "decision": {"decision_type": "expire_close"},
            "close_target_resolution": close_target_resolution.to_dict(),
        },
    )


def _resolve_expiry_case_selector(
    repo: Any,
    *,
    case_id: str | None,
    deal_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    normalized_case_id = str(case_id or "").strip()
    normalized_deal_id = str(deal_id or "").strip()
    if normalized_case_id:
        case = _get_case_by_id(repo, normalized_case_id)
        if not case:
            return None, None, "lifecycle_case_not_found"
        return case, _first_option_evidence(repo, normalized_case_id), None
    if not normalized_deal_id:
        return None, None, "missing_lifecycle_case_selector"
    evidence = _find_option_evidence_by_deal_id(repo, normalized_deal_id)
    if not evidence:
        return None, None, "option_zero_price_evidence_not_found"
    evidence_case_id = str(evidence.get("case_id") or "").strip()
    if not evidence_case_id:
        return None, evidence, "option_zero_price_evidence_missing_case_id"
    case = _get_case_by_id(repo, evidence_case_id)
    if not case:
        return None, evidence, "lifecycle_case_not_found"
    return case, evidence, None


def _get_case_by_id(repo: Any, case_id: str) -> dict[str, Any] | None:
    get_fn = getattr(repo, "get_trade_lifecycle_case", None)
    if callable(get_fn):
        try:
            row = get_fn(case_id)
        except Exception:
            row = None
        if isinstance(row, dict):
            return dict(row)
    list_fn = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_fn):
        return None
    try:
        rows = list_fn()
    except Exception:
        rows = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("case_id") or "").strip() == case_id:
            return dict(row)
    return None


def _find_option_evidence_by_deal_id(repo: Any, deal_id: str) -> dict[str, Any] | None:
    list_fn = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return None
    try:
        rows = list_fn()
    except Exception:
        rows = []
    for row in reversed(list(rows or [])):
        if not isinstance(row, dict):
            continue
        if str(row.get("evidence_type") or "") != "option_zero_price_close":
            continue
        if deal_id in _deal_ids_from_lifecycle_evidence_payload(row):
            return dict(row)
    return None


def _deal_ids_from_lifecycle_evidence_payload(evidence: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("source_event_id", "deal_id"):
        raw = str(evidence.get(key) or "").strip()
        if raw:
            values.add(raw)
    raw_payload = evidence.get("raw") if isinstance(evidence.get("raw"), dict) else {}
    for key in ("deal_id", "source_deal_id", "futu_deal_id"):
        raw = str(raw_payload.get(key) or "").strip()
        if raw:
            values.add(raw)
    nested = raw_payload.get("raw_payload")
    if isinstance(nested, dict):
        for key in ("deal_id", "source_deal_id", "futu_deal_id"):
            raw = str(nested.get(key) or "").strip()
            if raw:
                values.add(raw)
    return values


def _expiry_close_event_time_ms(case: dict[str, Any], option_evidence: dict[str, Any]) -> int | None:
    values: list[int] = []
    for raw in (case.get("event_time_ms"), option_evidence.get("trade_time_ms")):
        try:
            value = int(raw or 0)
        except Exception:
            value = 0
        if value > 0:
            values.append(value)
    return max(values) if values else None


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
    source_event_id = str(deal.deal_id or "").strip()
    evidence_id = _stable_id("ev", f"{evidence_type}|{source_event_id or deal.to_dict()}")
    raw = deal.to_dict()
    out = {
        "evidence_id": evidence_id,
        "case_id": str(case_id or "").strip() or None,
        "source_type": "futu_trade_push",
        "source_event_id": source_event_id or None,
        "evidence_type": evidence_type,
        "account": normalize_account(deal.internal_account),
        "symbol": canonical_contract_symbol(deal.symbol),
        "side": deal.side,
        "trade_time_ms": int(deal.trade_time_ms or 0),
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


def _find_matching_stock_evidence(repo: Any, *, option_case: dict[str, Any]) -> dict[str, Any] | None:
    list_fn = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return None
    rows = list_fn(account=option_case.get("account"), symbol=option_case.get("symbol"))
    for row in reversed(rows):
        if str(row.get("evidence_type") or "") != "stock_settlement_leg":
            continue
        if _stock_matches_lifecycle_close(option_case, row):
            return dict(row)
    return None


def _find_matching_option_case(
    repo: Any,
    *,
    stock_evidence: dict[str, Any],
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    list_fn = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_fn):
        return None
    allowed_statuses = set(statuses or PENDING_STATUSES)
    rows = list_fn()
    for row in rows:
        if str(row.get("status") or "").strip().lower() not in allowed_statuses:
            continue
        if normalize_account(row.get("account")) != normalize_account(stock_evidence.get("account")):
            continue
        if canonical_contract_symbol(row.get("symbol")) != canonical_contract_symbol(stock_evidence.get("symbol")):
            continue
        if _stock_matches_lifecycle_close(row, stock_evidence):
            return dict(row)
    return None


def _stock_settlement_has_lifecycle_context(repo: Any, *, stock_evidence: dict[str, Any]) -> bool:
    if _find_matching_option_case(repo, stock_evidence=stock_evidence) is not None:
        return True
    if _find_matching_option_case(repo, stock_evidence=stock_evidence, statuses=FINAL_STATUSES) is not None:
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
        if not _trade_time_on_or_after_expiration_ymd(
            int(stock_evidence.get("trade_time_ms") or 0),
            expiration_ymd,
        ):
            continue
        case = {
            "account": normalize_account(fields.get("account")),
            "symbol": canonical_contract_symbol(fields.get("symbol")),
            "option_type": normalize_option_type(fields.get("option_type")),
            "position_side": str(fields.get("side") or "").strip().lower(),
            "strike": effective_strike(fields),
            "contracts": contracts,
            "multiplier": int(effective_multiplier(fields) or 100),
        }
        if _stock_matches_lifecycle_close(case, stock_evidence):
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
    if not isinstance(stock_evidence, dict):
        return False
    side = str(stock_evidence.get("side") or "").strip().lower()
    expected_side = _expected_stock_side_for_lifecycle(case)
    if not expected_side:
        return False
    if side != expected_side:
        return False
    try:
        expected_qty = int(case.get("contracts") or 0) * int(case.get("multiplier") or 100)
        actual_qty = abs(int(stock_evidence.get("stock_qty") or 0))
    except Exception:
        return False
    if expected_qty <= 0 or actual_qty != expected_qty:
        return False
    try:
        strike = float(case.get("strike"))
        price = float(stock_evidence.get("stock_price"))
    except Exception:
        return False
    tolerance = max(0.01, abs(strike) * 0.001)
    return abs(price - strike) <= tolerance


def _is_stock_settlement_leg(deal: NormalizedTradeDeal) -> bool:
    if deal.option_type:
        return False
    if not deal.symbol or not deal.internal_account:
        return False
    if str(deal.side or "").strip().lower() not in {"buy", "sell"}:
        return False
    try:
        return int(deal.contracts or 0) > 0 and float(deal.price or 0.0) > 0.0
    except Exception:
        return False


def _is_zero_price_option_close(deal: NormalizedTradeDeal) -> bool:
    if str(deal.position_effect or "").strip().lower() != "close":
        return False
    if not deal.option_type:
        return False
    try:
        if float(deal.price) != 0.0:
            return False
    except Exception:
        return False
    if not normalize_contract_expiration(deal.expiration_ymd):
        return False
    if not deal.trade_time_ms:
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


def _upsert_evidence(repo: Any, evidence: dict[str, Any]) -> bool:
    fn = getattr(repo, "upsert_trade_lifecycle_evidence", None)
    if not callable(fn):
        return False
    return bool(fn(evidence))


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
    "resolve_lifecycle_expired_unassigned",
    "resolve_lifecycle_trade_deal",
]
