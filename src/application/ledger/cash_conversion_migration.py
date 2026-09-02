from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from domain.domain.ledger import TradeEvent
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.money import to_decimal
from domain.domain.option_position_identity import normalize_currency
from domain.domain.performance.cash_conversion import (
    HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
    MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS,
    OFFICIAL_CARRY_FORWARD_SOURCES,
    validate_observed_cash_conversion,
)
from domain.domain.performance.models import (
    EvidenceSelection,
    FXRateFact,
    select_fx_rate,
)
from src.application.cash_conversion import (
    attach_assigned_stock_sale_cash_conversions,
    build_cash_conversion,
)
from src.application.ledger.event_codec import encode_trade_event_for_storage, import_stored_trade_events
from src.application.ledger.repository import SQLiteOptionPositionsRepository, with_sqlite_repo_transaction


_MAX_CASH_FX_STALENESS_MS = 24 * 60 * 60 * 1000
_FX_EVIDENCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
_TRADE_CASH_FACT_KINDS = frozenset(
    {
        "option_trade_cash_gross",
        "option_fee_cash",
        "stock_settlement_cash_gross",
        "stock_settlement_fee_cash",
    }
)


@dataclass(frozen=True)
class CashConversionChange:
    event_kind: str
    event_id: str
    cash_fact_id: str
    previous_status: str
    conversion: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_kind": self.event_kind,
            "event_id": self.event_id,
            "cash_fact_id": self.cash_fact_id,
            "previous_status": self.previous_status,
            "new_status": self.conversion.get("status"),
            "conversion_id": self.conversion.get("conversion_id"),
            "native_amount": self.conversion.get("native_amount"),
            "native_currency": self.conversion.get("native_currency"),
            "fx_rate": self.conversion.get("fx_rate"),
            "amount_cny": self.conversion.get("amount_cny"),
            "rate_source": self.conversion.get("rate_source"),
            "rate_source_id": self.conversion.get("rate_source_id"),
            "rate_evidence_fact_id": self.conversion.get("rate_evidence_fact_id"),
        }


@dataclass(frozen=True)
class _EventChange:
    event_kind: str
    event_id: str
    previous_json: str
    new_json: str
    conversion_changes: tuple[CashConversionChange, ...]


@dataclass(frozen=True)
class _Plan:
    scanned_event_count: int
    cash_fact_count: int
    existing_observed_count: int
    event_changes: tuple[_EventChange, ...]
    unresolved: tuple[dict[str, Any], ...]

    @property
    def conversion_changes(self) -> tuple[CashConversionChange, ...]:
        return tuple(change for event in self.event_changes for change in event.conversion_changes)


@dataclass(frozen=True)
class CashConversionBackfillResult:
    applied: bool
    batch_id: str | None
    evidence_schema_state: str
    evidence_fx_rate_count: int
    scanned_event_count: int
    cash_fact_count: int
    existing_observed_count: int
    changed_event_count: int
    preview_conversion_count: int
    migrated_conversion_count: int
    unresolved: tuple[dict[str, Any], ...]
    changes: tuple[CashConversionChange, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "batch_id": self.batch_id,
            "evidence_schema_state": self.evidence_schema_state,
            "evidence_fx_rate_count": self.evidence_fx_rate_count,
            "scanned_event_count": self.scanned_event_count,
            "cash_fact_count": self.cash_fact_count,
            "existing_observed_count": self.existing_observed_count,
            "changed_event_count": self.changed_event_count,
            "preview_conversion_count": self.preview_conversion_count,
            "migrated_conversion_count": self.migrated_conversion_count,
            "unresolved_count": len(self.unresolved),
            "unresolved": [dict(item) for item in self.unresolved],
            "changes": [item.to_dict() for item in self.changes],
        }


def backfill_cash_conversions(
    repo: Any,
    evidence_repo: Any,
    *,
    account: str | None = None,
    broker: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    apply: bool = False,
    migrated_at_ms: int,
) -> CashConversionBackfillResult:
    return _migrate_cash_conversions(
        repo,
        evidence_repo,
        account=account,
        broker=broker,
        start_ms=start_ms,
        end_ms=end_ms,
        apply=apply,
        migrated_at_ms=migrated_at_ms,
        replace_superseded=False,
    )


def correct_superseded_cash_conversions(
    repo: Any,
    evidence_repo: Any,
    *,
    account: str | None = None,
    broker: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    apply: bool = False,
    migrated_at_ms: int,
) -> CashConversionBackfillResult:
    return _migrate_cash_conversions(
        repo,
        evidence_repo,
        account=account,
        broker=broker,
        start_ms=start_ms,
        end_ms=end_ms,
        apply=apply,
        migrated_at_ms=migrated_at_ms,
        replace_superseded=True,
    )


def _migrate_cash_conversions(
    repo: Any,
    evidence_repo: Any,
    *,
    account: str | None,
    broker: str | None,
    start_ms: int | None,
    end_ms: int | None,
    apply: bool,
    migrated_at_ms: int,
    replace_superseded: bool,
) -> CashConversionBackfillResult:
    evidence = evidence_repo.read_all()
    if evidence.schema_state != "initialized_v1":
        raise ValueError(
            "performance evidence schema must be initialized before cash conversion backfill"
        )
    fx_rates = tuple(evidence.fx_rates)
    scope = {
        "account": str(account or "").strip().lower() or None,
        "broker": str(broker or "").strip() or None,
        "start_ms": int(start_ms) if start_ms is not None else None,
        "end_ms": int(end_ms) if end_ms is not None else None,
    }

    candidate = getattr(repo, "primary_repo", repo)
    if not isinstance(candidate, SQLiteOptionPositionsRepository):
        raise TypeError("cash conversion backfill requires the canonical SQLite ledger")

    if not apply:
        plan = _build_plan(
            candidate,
            fx_rates=fx_rates,
            scope=scope,
            migrated_at_ms=int(migrated_at_ms),
            replace_superseded=replace_superseded,
        )
        return _result_from_plan(
            plan,
            applied=False,
            batch_id=None,
            evidence_schema_state=evidence.schema_state,
            evidence_fx_rate_count=len(fx_rates),
        )

    batch_id: str | None = None

    def _run(sqlite_repo: Any, conn: Any | None) -> _Plan:
        nonlocal batch_id
        if conn is None or not isinstance(sqlite_repo, SQLiteOptionPositionsRepository):
            raise TypeError("cash conversion backfill requires a transactional SQLite ledger")
        plan = _build_plan(
            sqlite_repo,
            fx_rates=fx_rates,
            scope=scope,
            migrated_at_ms=int(migrated_at_ms),
            replace_superseded=replace_superseded,
            conn=conn,
        )
        batch_id = _batch_id(
            plan,
            migrated_at_ms=int(migrated_at_ms),
            prefix="cashfxcorr" if replace_superseded else "cashfxmig",
        )
        _apply_plan(
            conn,
            plan,
            batch_id=batch_id,
            migrated_at_ms=int(migrated_at_ms),
            correction=replace_superseded,
        )
        return plan

    applied_plan = with_sqlite_repo_transaction(candidate, _run)
    return _result_from_plan(
        applied_plan,
        applied=True,
        batch_id=batch_id,
        evidence_schema_state=evidence.schema_state,
        evidence_fx_rate_count=len(fx_rates),
    )


def _build_plan(
    repo: SQLiteOptionPositionsRepository,
    *,
    fx_rates: Sequence[FXRateFact],
    scope: Mapping[str, Any],
    migrated_at_ms: int,
    replace_superseded: bool = False,
    conn: Any | None = None,
) -> _Plan:
    raw_trade_events = repo.list_trade_events(conn=conn)
    trade_events, diagnostics = import_stored_trade_events(raw_trade_events)
    decode_errors = {
        str(item.event_id or "")
        for item in diagnostics
        if str(item.severity or "").lower() == "error"
    }
    voided_event_ids = {
        str(event.target_event_id)
        for event in trade_events
        if event.event_type == "void" and event.target_event_id
    }
    scanned = 0
    cash_fact_count = 0
    existing_observed = 0
    event_changes: list[_EventChange] = []
    unresolved: list[dict[str, Any]] = [
        {
            "event_kind": "trade_event",
            "event_id": event_id,
            "cash_fact_id": None,
            "reason": "trade_event_decode_failed",
        }
        for event_id in sorted(decode_errors)
        if event_id
    ]

    for event in trade_events:
        if event.event_id in voided_event_ids or not _trade_event_in_scope(event, scope):
            continue
        facts = [
            fact
            for fact in cash_facts_for_trade_event(event)
            if fact.fact_kind in _TRADE_CASH_FACT_KINDS
        ]
        if not facts:
            continue
        scanned += 1
        cash_fact_count += len(facts)
        conversions = (
            dict(event.raw_payload.get("cash_conversions") or {})
            if isinstance(event.raw_payload, Mapping)
            else {}
        )
        conversion_changes: list[CashConversionChange] = []
        for fact in facts:
            existing = conversions.get(fact.fact_kind)
            if _existing_conversion_is_observed(existing, fact=fact):
                existing_observed += 1
                if not replace_superseded:
                    continue
                conversion = _conversion_from_evidence(
                    cash_fact_id=fact.fact_id,
                    amount=fact.amount,
                    currency=str(fact.currency or ""),
                    effective_at_ms=fact.effective_at_ms,
                    fx_rates=fx_rates,
                    migrated_at_ms=int(migrated_at_ms),
                )
                if not _is_superseding_conversion(existing, conversion, fx_rates=fx_rates):
                    continue
                conversions[fact.fact_kind] = conversion
                conversion_changes.append(
                    CashConversionChange(
                        event_kind="trade_event",
                        event_id=event.event_id,
                        cash_fact_id=fact.fact_id,
                        previous_status="observed",
                        conversion=conversion,
                    )
                )
                continue
            if replace_superseded:
                continue
            if isinstance(existing, Mapping) and str(existing.get("status") or "").lower() not in {
                "",
                "pending",
                "observed",
            }:
                unresolved.append(
                    _unresolved(
                        "trade_event",
                        event.event_id,
                        fact.fact_id,
                        "existing_conversion_invalid_or_non_pending",
                    )
                )
                continue
            if fact.amount is None:
                unresolved.append(
                    _unresolved(
                        "trade_event",
                        event.event_id,
                        fact.fact_id,
                        fact.missing_reason or "cash_amount_unavailable",
                    )
                )
                continue
            conversion = _conversion_from_evidence(
                cash_fact_id=fact.fact_id,
                amount=fact.amount,
                currency=str(fact.currency or ""),
                effective_at_ms=fact.effective_at_ms,
                fx_rates=fx_rates,
                migrated_at_ms=int(migrated_at_ms),
            )
            if conversion["status"] != "observed":
                unresolved.append(
                    _unresolved(
                        "trade_event",
                        event.event_id,
                        fact.fact_id,
                        str(conversion.get("missing_reason") or "event_time_fx_unavailable"),
                    )
                )
                continue
            conversions[fact.fact_kind] = conversion
            conversion_changes.append(
                CashConversionChange(
                    event_kind="trade_event",
                    event_id=event.event_id,
                    cash_fact_id=fact.fact_id,
                    previous_status=_conversion_status(existing),
                    conversion=conversion,
                )
            )
        if conversion_changes:
            raw_payload = dict(event.raw_payload or {})
            raw_payload["cash_conversions"] = conversions
            updated = replace(event, raw_payload=raw_payload)
            event_changes.append(
                _EventChange(
                    event_kind="trade_event",
                    event_id=event.event_id,
                    previous_json=encode_trade_event_for_storage(event).event_json,
                    new_json=encode_trade_event_for_storage(updated).event_json,
                    conversion_changes=tuple(conversion_changes),
                )
            )

    assigned_events = repo.list_assigned_stock_events(conn=conn)
    for event in assigned_events:
        if not _assigned_event_in_scope(event, scope):
            continue
        event_id = str(event.get("stock_event_id") or event.get("event_id") or "").strip()
        if not event_id:
            unresolved.append(_unresolved("assigned_stock_event", "", None, "stock_event_id_missing"))
            continue
        candidates = _assigned_conversion_candidates(
            event,
            fx_rates=fx_rates,
            migrated_at_ms=int(migrated_at_ms),
        )
        if not candidates:
            continue
        scanned += 1
        cash_fact_count += len(candidates)
        existing_conversions = (
            dict(event.get("cash_conversions") or {})
            if isinstance(event.get("cash_conversions"), Mapping)
            else {}
        )
        updated_conversions = dict(existing_conversions)
        conversion_changes = []
        for fact_kind, conversion in candidates.items():
            cash_fact_id = str(conversion.get("cash_fact_id") or "")
            existing = existing_conversions.get(fact_kind)
            if _existing_conversion_is_observed_for_candidate(existing, conversion):
                existing_observed += 1
                if not replace_superseded:
                    continue
                if not _is_superseding_conversion(existing, conversion, fx_rates=fx_rates):
                    continue
                updated_conversions[fact_kind] = conversion
                conversion_changes.append(
                    CashConversionChange(
                        event_kind="assigned_stock_event",
                        event_id=event_id,
                        cash_fact_id=cash_fact_id,
                        previous_status="observed",
                        conversion=conversion,
                    )
                )
                continue
            if replace_superseded:
                continue
            if isinstance(existing, Mapping) and str(existing.get("status") or "").lower() not in {
                "",
                "pending",
                "observed",
            }:
                unresolved.append(
                    _unresolved(
                        "assigned_stock_event",
                        event_id,
                        cash_fact_id,
                        "existing_conversion_invalid_or_non_pending",
                    )
                )
                continue
            if conversion.get("status") != "observed":
                unresolved.append(
                    _unresolved(
                        "assigned_stock_event",
                        event_id,
                        cash_fact_id,
                        str(conversion.get("missing_reason") or "event_time_fx_unavailable"),
                    )
                )
                continue
            updated_conversions[fact_kind] = conversion
            conversion_changes.append(
                CashConversionChange(
                    event_kind="assigned_stock_event",
                    event_id=event_id,
                    cash_fact_id=cash_fact_id,
                    previous_status=_conversion_status(existing),
                    conversion=conversion,
                )
            )
        if conversion_changes:
            updated_event = dict(event)
            updated_event["cash_conversions"] = updated_conversions
            event_changes.append(
                _EventChange(
                    event_kind="assigned_stock_event",
                    event_id=event_id,
                    previous_json=json.dumps(event, ensure_ascii=False, sort_keys=True),
                    new_json=json.dumps(updated_event, ensure_ascii=False, sort_keys=True),
                    conversion_changes=tuple(conversion_changes),
                )
            )

    return _Plan(
        scanned_event_count=scanned,
        cash_fact_count=cash_fact_count,
        existing_observed_count=existing_observed,
        event_changes=tuple(event_changes),
        unresolved=tuple(unresolved),
    )


def _conversion_from_evidence(
    *,
    cash_fact_id: str,
    amount: Decimal,
    currency: str,
    effective_at_ms: int,
    fx_rates: Sequence[FXRateFact],
    migrated_at_ms: int,
) -> dict[str, Any]:
    native_currency = normalize_currency(currency)
    native_amount = to_decimal(amount, field_name="cash conversion amount")
    if native_amount == 0 or native_currency == "CNY":
        return build_cash_conversion(
            cash_fact_id=cash_fact_id,
            amount=native_amount,
            currency=native_currency,
            fx_payload=None,
            effective_at_ms=int(effective_at_ms),
            observed_at_ms=int(migrated_at_ms),
            rate_source_id=(
                f"identity:{native_currency}:zero"
                if native_amount == 0
                else f"identity:{native_currency}"
            ),
        )
    selected = _select_cash_fx_rate(
        fx_rates,
        base_currency=native_currency,
        at_ms=int(effective_at_ms),
    )
    if selected.fact is None:
        pending = build_cash_conversion(
            cash_fact_id=cash_fact_id,
            amount=native_amount,
            currency=native_currency,
            fx_payload=None,
            effective_at_ms=int(effective_at_ms),
            observed_at_ms=int(migrated_at_ms),
        )
        pending["missing_reason"] = (
            f"{native_currency}CNY event-time FX {selected.status}: "
            f"{selected.reason or 'evidence unavailable'}"
        )
        return pending
    rate = selected.fact
    assert isinstance(rate, FXRateFact)
    timestamp = datetime.fromtimestamp(
        int(rate.effective_at_ms) / 1000,
        tz=timezone.utc,
    ).isoformat()
    return build_cash_conversion(
        cash_fact_id=cash_fact_id,
        amount=native_amount,
        currency=native_currency,
        fx_payload={
            "rates": {f"{native_currency}CNY": str(rate.rate)},
            "timestamp": timestamp,
            "source": rate.source,
        },
        effective_at_ms=int(effective_at_ms),
        observed_at_ms=int(rate.observed_at_ms),
        rate_source=rate.source,
        rate_source_id=rate.source_id,
        rate_evidence_fact_id=str(rate.fact_id),
        method=(
            HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD
            if int(selected.staleness_ms or 0) > _MAX_CASH_FX_STALENESS_MS
            else "historical_fx_evidence_backfill"
        ),
        max_rate_distance_ms=(
            MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS
            if int(selected.staleness_ms or 0) > _MAX_CASH_FX_STALENESS_MS
            else _MAX_CASH_FX_STALENESS_MS
        ),
    )


def _select_cash_fx_rate(
    fx_rates: Sequence[FXRateFact],
    *,
    base_currency: str,
    at_ms: int,
) -> EvidenceSelection:
    selected = select_fx_rate(
        list(fx_rates),
        base_currency=base_currency,
        at_ms=int(at_ms),
        max_staleness_ms=_MAX_CASH_FX_STALENESS_MS,
    )
    if selected.status != "stale":
        return selected

    carried = select_fx_rate(
        list(fx_rates),
        base_currency=base_currency,
        at_ms=int(at_ms),
        max_staleness_ms=MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS,
    )
    rate = carried.fact
    if not isinstance(rate, FXRateFact):
        return selected
    carry_dates = rate.quality.get("carry_forward_dates")
    event_date = datetime.fromtimestamp(
        int(at_ms) / 1000,
        tz=_FX_EVIDENCE_TIMEZONE,
    ).date().isoformat()
    if (
        rate.source not in OFFICIAL_CARRY_FORWARD_SOURCES
        or rate.quality.get("official") is not True
        or not isinstance(carry_dates, (list, tuple))
        or event_date not in {str(item) for item in carry_dates}
    ):
        return selected
    return carried


def _assigned_conversion_candidates(
    event: Mapping[str, Any],
    *,
    fx_rates: Sequence[FXRateFact],
    migrated_at_ms: int,
) -> dict[str, dict[str, Any]]:
    event_time_ms = int(event.get("trade_time_ms") or event.get("event_time_ms") or 0)
    currency = normalize_currency(event.get("currency"))
    selected = _select_cash_fx_rate(
        fx_rates,
        base_currency=currency,
        at_ms=event_time_ms,
    )
    fx_payload: dict[str, Any] | None = None
    rate: FXRateFact | None = selected.fact if isinstance(selected.fact, FXRateFact) else None
    if rate is not None:
        fx_payload = {
            "rates": {f"{currency}CNY": str(rate.rate)},
            "timestamp": datetime.fromtimestamp(
                int(rate.effective_at_ms) / 1000,
                tz=timezone.utc,
            ).isoformat(),
        }
    generated = attach_assigned_stock_sale_cash_conversions(
        event,
        fx_payload=fx_payload,
        observed_at_ms=int(rate.observed_at_ms if rate is not None else event_time_ms),
    ).get("cash_conversions")
    if not isinstance(generated, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for fact_kind, raw_conversion in generated.items():
        if not isinstance(raw_conversion, Mapping):
            continue
        conversion = dict(raw_conversion)
        native_amount = to_decimal(
            conversion.get("native_amount"),
            field_name="assigned stock cash amount",
        )
        if native_amount != 0 and currency != "CNY":
            conversion = _conversion_from_evidence(
                cash_fact_id=str(conversion.get("cash_fact_id") or ""),
                amount=native_amount,
                currency=currency,
                effective_at_ms=event_time_ms,
                fx_rates=fx_rates,
                migrated_at_ms=int(migrated_at_ms),
            )
        out[str(fact_kind)] = conversion
    return out


def _trade_event_in_scope(event: TradeEvent, scope: Mapping[str, Any]) -> bool:
    return _in_scope(
        account=event.contract_key.account,
        broker=event.contract_key.broker,
        event_time_ms=event.event_time_ms,
        scope=scope,
    )


def _assigned_event_in_scope(event: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    return _in_scope(
        account=event.get("account"),
        broker=event.get("broker"),
        event_time_ms=int(event.get("trade_time_ms") or event.get("event_time_ms") or 0),
        scope=scope,
    )


def _in_scope(
    *,
    account: Any,
    broker: Any,
    event_time_ms: int,
    scope: Mapping[str, Any],
) -> bool:
    scoped_account = str(scope.get("account") or "").lower()
    if scoped_account and str(account or "").strip().lower() != scoped_account:
        return False
    scoped_broker = str(scope.get("broker") or "")
    if scoped_broker and str(broker or "").strip() != scoped_broker:
        return False
    start_ms = scope.get("start_ms")
    if start_ms is not None and int(event_time_ms) < int(start_ms):
        return False
    end_ms = scope.get("end_ms")
    return end_ms is None or int(event_time_ms) <= int(end_ms)


def _existing_conversion_is_observed(existing: Any, *, fact: Any) -> bool:
    if not isinstance(existing, Mapping):
        return False
    amount_cny, issue = validate_observed_cash_conversion(
        existing,
        cash_fact_id=fact.fact_id,
        native_amount=fact.amount,
        native_currency=str(fact.currency or ""),
        effective_at_ms=int(fact.effective_at_ms),
    )
    return issue is None and amount_cny is not None


def _existing_conversion_is_observed_for_candidate(
    existing: Any,
    candidate: Mapping[str, Any],
) -> bool:
    if not isinstance(existing, Mapping):
        return False
    amount_cny, issue = validate_observed_cash_conversion(
        existing,
        cash_fact_id=str(candidate.get("cash_fact_id") or ""),
        native_amount=candidate.get("native_amount"),
        native_currency=str(candidate.get("native_currency") or ""),
        effective_at_ms=int(candidate.get("effective_at_ms") or 0),
    )
    return issue is None and amount_cny is not None


def _is_superseding_conversion(
    existing: Any,
    candidate: Mapping[str, Any],
    *,
    fx_rates: Sequence[FXRateFact],
) -> bool:
    if not isinstance(existing, Mapping) or str(candidate.get("status") or "") != "observed":
        return False
    old_fact_id = str(existing.get("rate_evidence_fact_id") or "").strip()
    new_fact_id = str(candidate.get("rate_evidence_fact_id") or "").strip()
    if not old_fact_id or not new_fact_id or old_fact_id == new_fact_id:
        return False
    by_id = {str(item.fact_id): item for item in fx_rates}
    cursor = by_id.get(new_fact_id)
    seen: set[str] = set()
    while cursor is not None and cursor.supersedes_fact_id:
        target_id = str(cursor.supersedes_fact_id)
        if target_id == old_fact_id:
            return True
        if target_id in seen:
            return False
        seen.add(target_id)
        cursor = by_id.get(target_id)
    return False


def _conversion_status(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "missing"
    return str(value.get("status") or "invalid").strip().lower()


def _unresolved(
    event_kind: str,
    event_id: str,
    cash_fact_id: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "event_kind": event_kind,
        "event_id": event_id,
        "cash_fact_id": cash_fact_id,
        "reason": str(reason),
    }


def _batch_id(plan: _Plan, *, migrated_at_ms: int, prefix: str = "cashfxmig") -> str:
    identities = [
        (item.event_kind, item.event_id, item.cash_fact_id, item.conversion.get("conversion_id"))
        for item in plan.conversion_changes
    ]
    digest = hashlib.sha256(
        json.dumps(identities, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}_{int(migrated_at_ms)}_{digest}"


def _apply_plan(
    conn: Any,
    plan: _Plan,
    *,
    batch_id: str,
    migrated_at_ms: int,
    correction: bool = False,
) -> None:
    audit_table = (
        "cash_conversion_correction_audit"
        if correction
        else "cash_conversion_backfill_audit"
    )
    primary_key = (
        "UNIQUE(event_kind, event_id, cash_fact_id, rate_evidence_fact_id)"
        if correction
        else "PRIMARY KEY(event_kind, event_id, cash_fact_id)"
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {audit_table}(
          batch_id TEXT NOT NULL,
          event_kind TEXT NOT NULL,
          event_id TEXT NOT NULL,
          cash_fact_id TEXT NOT NULL,
          previous_conversion_json TEXT,
          new_conversion_json TEXT NOT NULL,
          previous_event_sha256 TEXT NOT NULL,
          new_event_sha256 TEXT NOT NULL,
          rate_evidence_fact_id TEXT,
          migrated_at_ms INTEGER NOT NULL,
          {primary_key}
        )
        """
    )
    for event_change in plan.event_changes:
        table = "trade_events" if event_change.event_kind == "trade_event" else "assigned_stock_events"
        id_column = "event_id" if event_change.event_kind == "trade_event" else "stock_event_id"
        current = conn.execute(
            f"SELECT event_json FROM {table} WHERE {id_column} = ?",
            (event_change.event_id,),
        ).fetchone()
        if current is None or str(current["event_json"]) != event_change.previous_json:
            raise ValueError(
                f"cash conversion migration concurrency conflict: "
                f"{event_change.event_kind}={event_change.event_id}"
            )
        updated = conn.execute(
            f"""
            UPDATE {table}
            SET event_json = ?, updated_at_ms = ?
            WHERE {id_column} = ? AND event_json = ?
            """,
            (
                event_change.new_json,
                int(migrated_at_ms),
                event_change.event_id,
                event_change.previous_json,
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError(
                f"cash conversion migration update failed: "
                f"{event_change.event_kind}={event_change.event_id}"
            )
        before_payload = json.loads(event_change.previous_json)
        before_conversions = (
            before_payload.get("raw_payload", {}).get("cash_conversions", {})
            if event_change.event_kind == "trade_event"
            else before_payload.get("cash_conversions", {})
        )
        previous_hash = hashlib.sha256(event_change.previous_json.encode("utf-8")).hexdigest()
        new_hash = hashlib.sha256(event_change.new_json.encode("utf-8")).hexdigest()
        for conversion_change in event_change.conversion_changes:
            previous = None
            if isinstance(before_conversions, Mapping):
                fact_kind = conversion_change.cash_fact_id.split(":", 1)[0]
                previous = before_conversions.get(fact_kind)
            conn.execute(
                f"""
                INSERT INTO {audit_table}(
                  batch_id, event_kind, event_id, cash_fact_id,
                  previous_conversion_json, new_conversion_json,
                  previous_event_sha256, new_event_sha256,
                  rate_evidence_fact_id, migrated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    conversion_change.event_kind,
                    conversion_change.event_id,
                    conversion_change.cash_fact_id,
                    (
                        json.dumps(previous, ensure_ascii=False, sort_keys=True)
                        if previous is not None
                        else None
                    ),
                    json.dumps(conversion_change.conversion, ensure_ascii=False, sort_keys=True),
                    previous_hash,
                    new_hash,
                    conversion_change.conversion.get("rate_evidence_fact_id"),
                    int(migrated_at_ms),
                ),
            )


def _result_from_plan(
    plan: _Plan,
    *,
    applied: bool,
    batch_id: str | None,
    evidence_schema_state: str,
    evidence_fx_rate_count: int,
) -> CashConversionBackfillResult:
    changes = plan.conversion_changes
    return CashConversionBackfillResult(
        applied=applied,
        batch_id=batch_id,
        evidence_schema_state=evidence_schema_state,
        evidence_fx_rate_count=evidence_fx_rate_count,
        scanned_event_count=plan.scanned_event_count,
        cash_fact_count=plan.cash_fact_count,
        existing_observed_count=plan.existing_observed_count,
        changed_event_count=len(plan.event_changes),
        preview_conversion_count=len(changes),
        migrated_conversion_count=len(changes) if applied else 0,
        unresolved=plan.unresolved,
        changes=changes,
    )


__all__ = [
    "CashConversionBackfillResult",
    "backfill_cash_conversions",
    "correct_superseded_cash_conversions",
]
