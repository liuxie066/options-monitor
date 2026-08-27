from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from domain.domain.fee_calc import (
    FUTU_HK_FEE_SCHEDULE_URL,
    FUTU_US_FEE_SCHEDULE_URL,
    calc_futu_stock_fee,
    estimate_futu_executed_option_fee,
)
from domain.domain.ledger import TradeEvent, fee_fact_for_event
from domain.domain.option_position_identity import normalize_broker
from domain.domain.performance.engine import cash_facts_for_trade_event
from domain.domain.performance.models import (
    FeeBasis,
    FeeComponent,
    canonical_decimal_text,
    quantize_money,
    to_decimal,
)
from src.application.cash_conversion import build_cash_conversion
from src.application.ledger.current_decision_projection import (
    capture_current_decision_projection_fence,
    compact_assigned_stock_view,
    finalize_current_decision_projection,
)
from src.application.ledger.event_codec import encode_trade_event_for_storage
from src.application.ledger.order_fee_semantics import zero_option_fee_lifecycle_reason
from src.application.ledger.position_projection_runtime import (
    run_position_projection_in_transaction,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
    with_sqlite_repo_transaction,
)
from src.application.ledger.assigned_stock_projection import (
    project_assigned_stock_lifecycle_from_rows,
)


_MONEY = Decimal("0.000001")


@dataclass(frozen=True)
class ActualOrderFee:
    broker: str
    account: str
    futu_account_id: str
    order_id: str
    amount: Decimal
    currency: str
    event_kind: str
    dealt_quantity: Decimal
    observed_at_ms: int
    provider_batch_id: str | None = None
    fee_details_sha256: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActualOrderFee":
        amount = _money(value.get("fee_amount"), field="fee_amount")
        if amount < 0:
            raise ValueError("fee_amount must be non-negative")
        event_kind = str(value.get("event_kind") or "").strip().lower()
        if event_kind not in {"option_trade", "assigned_stock_sale"}:
            raise ValueError("fee observation event_kind is invalid")
        dealt_quantity = _money(
            value.get("dealt_quantity"),
            field="dealt_quantity",
        )
        if dealt_quantity <= 0 or dealt_quantity != dealt_quantity.to_integral_value():
            raise ValueError("fee observation dealt_quantity must be a positive integer")
        currency = str(value.get("currency") or "").strip().upper()
        if currency not in {"CNY", "HKD", "USD"}:
            raise ValueError("fee observation currency is invalid")
        return cls(
            broker=_required_text(value.get("broker"), field="broker"),
            account=_required_text(value.get("account"), field="account").lower(),
            futu_account_id=_required_text(
                value.get("futu_account_id"), field="futu_account_id"
            ),
            order_id=_required_text(value.get("order_id"), field="order_id"),
            amount=amount,
            currency=currency,
            event_kind=event_kind,
            dealt_quantity=dealt_quantity,
            observed_at_ms=_positive_int(
                value.get("observed_at_ms"), field="observed_at_ms"
            ),
            provider_batch_id=_optional_text(value.get("provider_batch_id")),
            fee_details_sha256=_optional_sha256(value.get("fee_details_sha256")),
        )

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (
            self.broker,
            self.account,
            self.futu_account_id,
            self.order_id,
        )


@dataclass(frozen=True)
class _Change:
    event_kind: str
    event_id: str
    account: str
    before_json: str
    after_json: str
    before_basis: str
    after_basis: str
    provider_batch_id: str | None = None
    provider_observed_at_ms: int | None = None
    fee_details_sha256: str | None = None


@dataclass(frozen=True)
class _Unit:
    identity: str
    changes: tuple[_Change, ...]


def enrich_order_fees(
    repo: Any,
    *,
    account: str,
    start_ms: int,
    end_exclusive_ms: int,
    actual_fees: Sequence[Mapping[str, Any] | ActualOrderFee] = (),
    apply: bool = False,
    applied_at_ms: int,
) -> dict[str, Any]:
    """Freeze legacy estimates and upgrade admitted order groups to actual fees."""

    candidate = getattr(repo, "primary_repo", repo)
    if not isinstance(candidate, SQLiteOptionPositionsRepository):
        raise TypeError("order fee enrichment requires the canonical SQLite ledger")
    account_value = _required_text(account, field="account").lower()
    start = _positive_int(start_ms, field="start_ms")
    end = _positive_int(end_exclusive_ms, field="end_exclusive_ms")
    if end <= start:
        raise ValueError("fee enrichment range must be positive")
    observations = tuple(
        item if isinstance(item, ActualOrderFee) else ActualOrderFee.from_mapping(item)
        for item in actual_fees
    )
    duplicate_identities = _duplicates(item.identity for item in observations)
    if duplicate_identities:
        raise ValueError("duplicate actual fee observation identity")
    wrong_account = [item.identity for item in observations if item.account != account_value]
    if wrong_account:
        raise ValueError("actual fee observation account is outside scope")

    units, unresolved, passive_outcomes, basis_before = _build_units(
        candidate,
        account=account_value,
        start_ms=start,
        end_exclusive_ms=end,
        actual_fees=observations,
        applied_at_ms=int(applied_at_ms),
    )
    outcomes: list[dict[str, Any]] = [dict(item) for item in passive_outcomes]
    if not apply:
        outcomes.extend(
            {
                "identity": unit.identity,
                "status": "preview",
                "event_count": len(unit.changes),
                "event_kinds": sorted({change.event_kind for change in unit.changes}),
                "after_basis": sorted({change.after_basis for change in unit.changes}),
            }
            for unit in units
        )
    else:
        for unit in units:
            try:
                result = with_sqlite_repo_transaction(
                    candidate,
                    lambda sqlite_repo, conn, current=unit: _apply_unit(
                        sqlite_repo,
                        conn,
                        current,
                        applied_at_ms=int(applied_at_ms),
                    ),
                    require_projection_publication=True,
                )
            except Exception as exc:
                outcomes.append(
                    {
                        "identity": unit.identity,
                        "status": "rolled_back",
                        "event_count": len(unit.changes),
                        "event_kinds": sorted(
                            {change.event_kind for change in unit.changes}
                        ),
                        "reason": "fee_enrichment_unit_rolled_back",
                        "error_type": type(exc).__name__,
                    }
                )
            else:
                outcomes.append(result)

    return _receipt(
        account=account_value,
        start_ms=start,
        end_exclusive_ms=end,
        applied=bool(apply),
        units=units,
        outcomes=outcomes,
        unresolved=unresolved,
        basis_before=basis_before,
    )


def _build_units(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    start_ms: int,
    end_exclusive_ms: int,
    actual_fees: Sequence[ActualOrderFee],
    applied_at_ms: int,
) -> tuple[
    tuple[_Unit, ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    dict[str, dict[str, int]],
]:
    trade_rows = repo.list_trade_events()
    stock_rows = repo.list_assigned_stock_events()
    trade_events: list[TradeEvent] = []
    unresolved: list[dict[str, Any]] = []
    passive_outcomes: list[dict[str, Any]] = []
    for row in trade_rows:
        try:
            event = TradeEvent.from_dict(row)
        except (TypeError, ValueError) as exc:
            unresolved.append(
                {
                    "event_kind": "option_trade",
                    "event_id": str(row.get("event_id") or ""),
                    "reason": "trade_event_decode_failed",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        trade_events.append(event)
    voided = {
        str(event.target_event_id)
        for event in trade_events
        if event.event_type == "void" and event.target_event_id
    }
    option_events = [
        event
        for event in trade_events
        if event.event_id not in voided
        and event.event_type
        in {"open", "close", "expire_close", "assignment", "exercise"}
        and event.contract_key.account == account
    ]
    stock_events = [
        dict(row)
        for row in stock_rows
        if str(row.get("account") or "").strip().lower() == account
        and str(row.get("event_type") or "").strip().lower() == "sale"
    ]
    basis_before = _fee_basis_event_counts(
        option_events,
        stock_events,
        start_ms=start_ms,
        end_exclusive_ms=end_exclusive_ms,
    )
    option_groups = _group_options_by_order(option_events)
    stock_groups = _group_stocks_by_order(stock_events)
    units: list[_Unit] = []
    provider_observed_event_ids: set[tuple[str, str]] = set()

    for observation in actual_fees:
        option_group = option_groups.get(observation.identity, ())
        stock_group = stock_groups.get(observation.identity, ())
        provider_observed_event_ids.update(
            ("option_trade", _event_id(row)) for row in option_group
        )
        provider_observed_event_ids.update(
            ("assigned_stock_sale", _event_id(row)) for row in stock_group
        )
        if option_group and stock_group:
            unresolved.append(_order_issue(observation, "order_identity_cross_type_conflict"))
            continue
        if not option_group and not stock_group:
            unresolved.append(_order_issue(observation, "order_group_missing"))
            continue
        rows: Sequence[Any] = option_group or stock_group
        event_kind = "option_trade" if option_group else "assigned_stock_sale"
        if observation.event_kind != event_kind:
            unresolved.append(_order_issue(observation, "order_event_kind_changed_after_admission"))
            continue
        ledger_quantity = Decimal(
            sum(event.contracts for event in option_group)
            if option_group
            else int(stock_group[0].get("shares") or 0)
        )
        if ledger_quantity != observation.dealt_quantity:
            unresolved.append(_order_issue(observation, "order_quantity_changed_after_admission"))
            continue
        times = [_event_time_ms(item) for item in rows]
        if min(times) < start_ms or max(times) >= end_exclusive_ms:
            unresolved.append(
                {
                    **_order_issue(observation, "order_group_outside_requested_range"),
                    "group_min_event_time_ms": min(times),
                    "group_max_event_time_ms": max(times),
                    "row_count": len(rows),
                }
            )
            continue
        currencies = {_event_currency(item) for item in rows}
        if currencies != {observation.currency}:
            unresolved.append(_order_issue(observation, "order_currency_mismatch"))
            continue
        if option_group:
            if len({_option_contract_identity(item) for item in option_group}) != 1:
                unresolved.append(_order_issue(observation, "combo_fee_allocation_unproven"))
                continue
            changes, conflict = _actual_option_changes(
                option_group,
                observation=observation,
                applied_at_ms=applied_at_ms,
            )
            event_kind = "option_trade"
        else:
            if len(stock_group) != 1:
                unresolved.append(_order_issue(observation, "stock_sale_order_group_unsupported"))
                continue
            changes, conflict = _actual_stock_changes(
                stock_group,
                observation=observation,
                applied_at_ms=applied_at_ms,
            )
            event_kind = "assigned_stock_sale"
        if conflict:
            unresolved.append(_order_issue(observation, conflict))
            passive_outcomes.append(
                {
                    "identity": _unit_identity("actual", observation.identity),
                    "status": "conflict",
                    "event_count": len(rows),
                    "event_kinds": [event_kind],
                    "reason": conflict,
                }
            )
            continue
        if changes:
            units.append(
                _Unit(
                    identity=_unit_identity("actual", observation.identity),
                    changes=tuple(changes),
                )
            )
        else:
            passive_outcomes.append(
                {
                    "identity": _unit_identity("actual", observation.identity),
                    "status": "no_op",
                    "event_count": len(rows),
                    "event_kinds": [event_kind],
                    "after_basis": [FeeBasis.ACTUAL.value],
                }
            )

    for event in option_events:
        zero_reason = zero_option_fee_lifecycle_reason(event)
        if (
            ("option_trade", event.event_id) in provider_observed_event_ids
            or not _in_range(event, start_ms, end_exclusive_ms)
            or not _is_bare_fee(event.raw_payload, event.fees)
            or not zero_reason
        ):
            continue
        source = (
            "option_assignment_lifecycle"
            if event.event_type == "assignment"
            else "option_expiry_lifecycle"
        )
        updated = _option_event_with_fee(
            event,
            basis=FeeBasis.ACTUAL,
            amount=Decimal(0),
            provenance={
                "source": source,
                "reason": zero_reason,
                "frozen_at_ms": int(applied_at_ms),
            },
            applied_at_ms=applied_at_ms,
        )
        units.append(
            _Unit(
                identity=f"actual-zero:option:{event.event_id}",
                changes=(
                    _trade_change(
                        event,
                        updated,
                        before_basis=FeeBasis.MISSING.value,
                        after_basis=FeeBasis.ACTUAL.value,
                    ),
                ),
            )
        )

    formula_option_groups = _formula_option_groups(option_events)
    for identity, events in sorted(formula_option_groups.items()):
        if any(
            ("option_trade", event.event_id) in provider_observed_event_ids
            for event in events
        ):
            continue
        if not any(_in_range(event, start_ms, end_exclusive_ms) for event in events):
            continue
        if not all(_in_range(event, start_ms, end_exclusive_ms) for event in events):
            unresolved.append(
                {
                    "event_kind": "option_trade",
                    "event_ids": [event.event_id for event in events],
                    "reason": "formula_group_outside_requested_range",
                }
            )
            continue
        if not all(_is_bare_fee(event.raw_payload, event.fees) for event in events):
            continue
        changes, reason = _estimated_option_changes(
            events,
            applied_at_ms=applied_at_ms,
        )
        if changes:
            units.append(_Unit(identity=f"formula:option:{identity}", changes=tuple(changes)))
        elif reason is not None:
            unresolved.append(
                {
                    "event_kind": "option_trade",
                    **(
                        {"event_id": events[0].event_id}
                        if len(events) == 1
                        else {"event_ids": [event.event_id for event in events]}
                    ),
                    "reason": reason,
                }
            )

    for event in stock_events:
        event_id = _event_id(event)
        if ("assigned_stock_sale", event_id) in provider_observed_event_ids:
            continue
        if not _in_range(event, start_ms, end_exclusive_ms):
            continue
        if not _is_bare_fee(event, event.get("fees") or 0):
            continue
        change, reason = _estimated_stock_change(
            event,
            applied_at_ms=applied_at_ms,
        )
        if change is not None:
            units.append(
                _Unit(identity=f"formula:stock:{event_id}", changes=(change,))
            )
        elif reason is not None:
            unresolved.append(
                {
                    "event_kind": "assigned_stock_sale",
                    "event_id": event_id,
                    "reason": reason,
                }
            )
    return tuple(units), tuple(unresolved), tuple(passive_outcomes), basis_before


def _actual_option_changes(
    events: Sequence[TradeEvent],
    *,
    observation: ActualOrderFee,
    applied_at_ms: int,
) -> tuple[list[_Change], str | None]:
    ordered = sorted(events, key=lambda item: (item.event_time_ms, item.event_id))
    allocations = _allocate(observation.amount, [event.contracts for event in ordered])
    changes: list[_Change] = []
    for event, amount in zip(ordered, allocations, strict=True):
        before = fee_fact_for_event(event)
        if before.basis == FeeBasis.ACTUAL:
            if before.amount != amount:
                return [], "actual_fee_conflict"
            continue
        updated = _option_event_with_fee(
            event,
            basis=FeeBasis.ACTUAL,
            amount=amount,
            provenance={
                "source": "opend.order_fee_query",
                "reason": "provider_reported_order_fee",
                "provider_observed_at_ms": observation.observed_at_ms,
                "provider_batch_id": observation.provider_batch_id,
                "fee_details_sha256": observation.fee_details_sha256,
            },
            applied_at_ms=applied_at_ms,
        )
        changes.append(
            _trade_change(
                event,
                updated,
                before_basis=before.basis.value,
                after_basis=FeeBasis.ACTUAL.value,
                observation=observation,
            )
        )
    return changes, None


def _actual_stock_changes(
    events: Sequence[Mapping[str, Any]],
    *,
    observation: ActualOrderFee,
    applied_at_ms: int,
) -> tuple[list[_Change], str | None]:
    event = dict(events[0])
    before = _stock_fee_fact(event)
    if before.basis == FeeBasis.ACTUAL:
        return (
            ([], None)
            if before.amount == observation.amount
            else ([], "actual_fee_conflict")
        )
    updated = _stock_event_with_fee(
        event,
        basis=FeeBasis.ACTUAL,
        amount=observation.amount,
        provenance={
            "source": "opend.order_fee_query",
            "reason": "provider_reported_order_fee",
            "provider_observed_at_ms": observation.observed_at_ms,
            "provider_batch_id": observation.provider_batch_id,
            "fee_details_sha256": observation.fee_details_sha256,
        },
        applied_at_ms=applied_at_ms,
    )
    return [
        _stock_change(
            event,
            updated,
            before_basis=before.basis.value,
            after_basis=FeeBasis.ACTUAL.value,
            observation=observation,
        )
    ], None


def _estimated_option_changes(
    events: Sequence[TradeEvent], *, applied_at_ms: int
) -> tuple[list[_Change], str | None]:
    ordered = sorted(events, key=lambda item: (item.event_time_ms, item.event_id))
    if any(normalize_broker(item.contract_key.broker) != "富途" for item in ordered):
        return [], "unsupported_broker_fee_schedule"
    first = ordered[0]
    comparable = {
        (
            item.currency,
            item.price,
            item.multiplier,
            item.contract_key.position_side,
            item.event_type,
        )
        for item in ordered
    }
    if len(comparable) != 1:
        return [], "source_deal_fee_inputs_conflict"
    try:
        estimate = estimate_futu_executed_option_fee(
            first.currency,
            first.price,
            contracts=sum(item.contracts for item in ordered),
            multiplier=int(first.multiplier),
            is_sell=(
                first.contract_key.position_side == "short"
                if first.event_type == "open"
                else first.contract_key.position_side == "long"
            ),
        )
    except (TypeError, ValueError):
        return [], "option_fee_estimate_failed"
    allocations = _allocate(
        _money(estimate.amount, field="estimated fee"),
        [event.contracts for event in ordered],
    )
    changes: list[_Change] = []
    for event, amount in zip(ordered, allocations, strict=True):
        updated = _option_event_with_fee(
            event,
            basis=FeeBasis.ESTIMATED,
            amount=amount,
            provenance={
                "source": "formula",
                "reason": "executed_option_fee_formula",
                "formula_version": estimate.fee_schedule_version,
                "formula_basis": estimate.fee_basis,
                "schedule_reference": estimate.fee_schedule_url,
                "frozen_at_ms": int(applied_at_ms),
            },
            applied_at_ms=applied_at_ms,
        )
        changes.append(
            _trade_change(
                event,
                updated,
                before_basis=FeeBasis.MISSING.value,
                after_basis=FeeBasis.ESTIMATED.value,
            )
        )
    return changes, None


def _estimated_stock_change(
    event: Mapping[str, Any], *, applied_at_ms: int
) -> tuple[_Change | None, str | None]:
    row = dict(event)
    if normalize_broker(row.get("broker")) != "富途":
        return None, "unsupported_broker_fee_schedule"
    currency = _event_currency(row)
    schedule = (
        FUTU_HK_FEE_SCHEDULE_URL
        if currency == "HKD"
        else FUTU_US_FEE_SCHEDULE_URL
        if currency == "USD"
        else None
    )
    if schedule is None:
        return None, "stock_fee_schedule_unsupported"
    try:
        amount = _money(
            calc_futu_stock_fee(
                currency,
                float(row.get("price") or 0),
                shares=int(row.get("shares") or 0),
                is_sell=True,
            ),
            field="estimated stock fee",
        )
    except (TypeError, ValueError):
        return None, "stock_fee_estimate_failed"
    updated = _stock_event_with_fee(
        row,
        basis=FeeBasis.ESTIMATED,
        amount=amount,
        provenance={
            "source": schedule,
            "reason": "executed_stock_fee_formula",
            "formula_version": "futu_executed_stock_fee.v1",
            "schedule_reference": schedule,
            "frozen_at_ms": int(applied_at_ms),
        },
        applied_at_ms=applied_at_ms,
    )
    return (
        _stock_change(
            row,
            updated,
            before_basis=FeeBasis.MISSING.value,
            after_basis=FeeBasis.ESTIMATED.value,
        ),
        None,
    )


def _option_event_with_fee(
    event: TradeEvent,
    *,
    basis: FeeBasis,
    amount: Decimal,
    provenance: Mapping[str, Any],
    applied_at_ms: int,
) -> TradeEvent:
    from dataclasses import replace

    raw = dict(event.raw_payload or {})
    raw["fee_provenance"] = {
        "basis": basis.value,
        "amount": canonical_decimal_text(amount),
        **{key: value for key, value in provenance.items() if value is not None},
    }
    updated = replace(
        event,
        fees=float(amount) if basis == FeeBasis.ACTUAL else 0.0,
        raw_payload=raw,
    )
    return _replace_option_fee_conversion(
        updated,
        prior_conversions=event.raw_payload.get("cash_conversions"),
        applied_at_ms=applied_at_ms,
    )


def _stock_event_with_fee(
    event: Mapping[str, Any],
    *,
    basis: FeeBasis,
    amount: Decimal,
    provenance: Mapping[str, Any],
    applied_at_ms: int,
) -> dict[str, Any]:
    out = dict(event)
    out["fees"] = float(amount) if basis == FeeBasis.ACTUAL else 0.0
    out["fee_provenance"] = {
        "basis": basis.value,
        "amount": canonical_decimal_text(amount),
        **{key: value for key, value in provenance.items() if value is not None},
    }
    conversions = dict(out.get("cash_conversions") or {})
    prior = conversions.get("assigned_stock_sale_fee_cash")
    conversions["assigned_stock_sale_fee_cash"] = _conversion_for_amount(
        fact_id=f"assigned_stock_sale_fee_cash:{_event_id(out)}",
        amount=-amount,
        currency=_event_currency(out),
        effective_at_ms=_event_time_ms(out),
        previous=prior,
        applied_at_ms=applied_at_ms,
    )
    out["cash_conversions"] = conversions
    return out


def _replace_option_fee_conversion(
    event: TradeEvent,
    *,
    prior_conversions: Any,
    applied_at_ms: int,
) -> TradeEvent:
    from dataclasses import replace

    fee_fact = next(
        (
            fact
            for fact in cash_facts_for_trade_event(event)
            if fact.fact_kind == "option_fee_cash"
        ),
        None,
    )
    if fee_fact is None or fee_fact.amount is None:
        return event
    raw = dict(event.raw_payload or {})
    conversions = dict(prior_conversions or {}) if isinstance(prior_conversions, Mapping) else {}
    conversions["option_fee_cash"] = _conversion_for_amount(
        fact_id=fee_fact.fact_id,
        amount=fee_fact.amount,
        currency=event.currency,
        effective_at_ms=event.event_time_ms,
        previous=conversions.get("option_fee_cash"),
        applied_at_ms=applied_at_ms,
    )
    raw["cash_conversions"] = conversions
    return replace(event, raw_payload=raw)


def _conversion_for_amount(
    *,
    fact_id: str,
    amount: Decimal,
    currency: str,
    effective_at_ms: int,
    previous: Any,
    applied_at_ms: int,
) -> dict[str, Any]:
    prior = dict(previous) if isinstance(previous, Mapping) else {}
    fx_rate = prior.get("fx_rate")
    timestamp = prior.get("rate_timestamp")
    fx_payload = None
    if fx_rate not in (None, "") and timestamp not in (None, ""):
        fx_payload = {
            "rates": {f"{currency}CNY": fx_rate},
            "timestamp": timestamp,
        }
    return build_cash_conversion(
        cash_fact_id=fact_id,
        amount=amount,
        currency=currency,
        fx_payload=fx_payload,
        effective_at_ms=effective_at_ms,
        observed_at_ms=int(prior.get("observed_at_ms") or applied_at_ms),
        rate_source=prior.get("rate_source"),
        rate_source_id=prior.get("rate_source_id"),
        rate_evidence_fact_id=prior.get("rate_evidence_fact_id"),
        method=prior.get("method"),
    )


def _trade_change(
    before: TradeEvent,
    after: TradeEvent,
    *,
    before_basis: str,
    after_basis: str,
    observation: ActualOrderFee | None = None,
) -> _Change:
    return _Change(
        event_kind="option_trade",
        event_id=before.event_id,
        account=before.contract_key.account,
        before_json=encode_trade_event_for_storage(before).event_json,
        after_json=encode_trade_event_for_storage(after).event_json,
        before_basis=before_basis,
        after_basis=after_basis,
        provider_batch_id=observation.provider_batch_id if observation else None,
        provider_observed_at_ms=observation.observed_at_ms if observation else None,
        fee_details_sha256=observation.fee_details_sha256 if observation else None,
    )


def _stock_change(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    before_basis: str,
    after_basis: str,
    observation: ActualOrderFee | None = None,
) -> _Change:
    return _Change(
        event_kind="assigned_stock_sale",
        event_id=_event_id(before),
        account=str(before.get("account") or "").strip().lower(),
        before_json=_json(before),
        after_json=_json(after),
        before_basis=before_basis,
        after_basis=after_basis,
        provider_batch_id=observation.provider_batch_id if observation else None,
        provider_observed_at_ms=observation.observed_at_ms if observation else None,
        fee_details_sha256=observation.fee_details_sha256 if observation else None,
    )


def _apply_unit(
    repo: SQLiteOptionPositionsRepository,
    conn: Any,
    unit: _Unit,
    *,
    applied_at_ms: int,
) -> dict[str, Any]:
    if conn is None:
        raise TypeError("fee enrichment requires SQLite transaction authority")
    accounts = sorted({change.account for change in unit.changes})
    fence = capture_current_decision_projection_fence(repo, accounts=accounts, conn=conn)
    _ensure_audit_table(conn)
    for change in unit.changes:
        table, column = (
            ("trade_events", "event_id")
            if change.event_kind == "option_trade"
            else ("assigned_stock_events", "stock_event_id")
        )
        row = conn.execute(
            f"SELECT event_json FROM {table} WHERE {column} = ?",
            (change.event_id,),
        ).fetchone()
        if row is None or str(row["event_json"]) != change.before_json:
            raise ValueError(f"fee enrichment CAS conflict: {change.event_id}")
        updated = conn.execute(
            f"UPDATE {table} SET event_json = ?, updated_at_ms = ? "
            f"WHERE {column} = ? AND event_json = ?",
            (change.after_json, applied_at_ms, change.event_id, change.before_json),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError(f"fee enrichment update failed: {change.event_id}")
        _insert_audit(conn, unit=unit, change=change, applied_at_ms=applied_at_ms)

    option_changed = any(change.event_kind == "option_trade" for change in unit.changes)
    projection = (
        run_position_projection_in_transaction(
            repo,
            (),
            conn=conn,
            mode="forced_full",
        )
        if option_changed
        else None
    )
    assigned_after = _assigned_after_by_account(
        repo,
        conn=conn,
        accounts=accounts,
        as_of_ms=applied_at_ms,
    )
    decision = finalize_current_decision_projection(
        repo,
        fence=fence,
        updated_at_ms=applied_at_ms,
        conn=conn,
        assigned_stock_after_by_account=assigned_after,
    )
    for change in unit.changes:
        table, column = (
            ("trade_events", "event_id")
            if change.event_kind == "option_trade"
            else ("assigned_stock_events", "stock_event_id")
        )
        row = conn.execute(
            f"SELECT event_json FROM {table} WHERE {column} = ?",
            (change.event_id,),
        ).fetchone()
        if row is None or str(row["event_json"]) != change.after_json:
            raise ValueError(f"fee enrichment readback failed: {change.event_id}")
    repo.assert_foreign_keys_clean(conn=conn)
    return {
        "identity": unit.identity,
        "status": "committed",
        "event_count": len(unit.changes),
        "event_kinds": sorted({change.event_kind for change in unit.changes}),
        "after_basis": sorted({change.after_basis for change in unit.changes}),
        "projection_source_generation": (
            repo.read_position_projection_source_state(conn=conn).get(
                "source_generation"
            )
            if projection is not None
            else None
        ),
        "decision_projection": decision,
    }


def _assigned_after_by_account(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    accounts: Sequence[str],
    as_of_ms: int,
) -> dict[str, dict[str, Any]]:
    trade_events = repo.list_trade_events(conn=conn)
    stock_events = repo.list_assigned_stock_events(conn=conn)
    lots = repo.list_position_lots(conn=conn)
    result: dict[str, dict[str, Any]] = {}
    for account in accounts:
        report = project_assigned_stock_lifecycle_from_rows(
            {
                "trade_events": trade_events,
                "account_assigned_stock_events": [
                    item
                    for item in stock_events
                    if str(item.get("account") or "").strip().lower() == account
                ],
            },
            account=account,
            as_of_ms=as_of_ms,
        )
        result[account] = compact_assigned_stock_view(
            report,
            account=account,
            current_position_lots=[
                item
                for item in lots
                if str((item.get("fields") or {}).get("account") or "").strip().lower()
                == account
            ],
        )
    return result


def _ensure_audit_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_fee_enrichment_audit(
          change_id TEXT PRIMARY KEY,
          event_kind TEXT NOT NULL,
          provider_batch_id TEXT,
          write_unit_identity_sha256 TEXT NOT NULL,
          event_id TEXT NOT NULL,
          before_event_sha256 TEXT NOT NULL,
          after_event_sha256 TEXT NOT NULL,
          before_basis TEXT NOT NULL,
          after_basis TEXT NOT NULL,
          provider_observed_at_ms INTEGER,
          fee_details_sha256 TEXT,
          applied_at_ms INTEGER NOT NULL,
          UNIQUE(event_kind, event_id, after_event_sha256)
        )
        """
    )


def _insert_audit(
    conn: Any,
    *,
    unit: _Unit,
    change: _Change,
    applied_at_ms: int,
) -> None:
    before_hash = _sha256(change.before_json)
    after_hash = _sha256(change.after_json)
    change_id = "feechg_" + _sha256(
        "\x1f".join((change.event_kind, change.event_id, before_hash, after_hash))
    )[:24]
    conn.execute(
        """
        INSERT INTO broker_fee_enrichment_audit(
          change_id,event_kind,provider_batch_id,write_unit_identity_sha256,
          event_id,before_event_sha256,after_event_sha256,before_basis,
          after_basis,provider_observed_at_ms,fee_details_sha256,applied_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            change_id,
            change.event_kind,
            change.provider_batch_id,
            _sha256(unit.identity),
            change.event_id,
            before_hash,
            after_hash,
            change.before_basis,
            change.after_basis,
            change.provider_observed_at_ms,
            change.fee_details_sha256,
            int(applied_at_ms),
        ),
    )


def _receipt(
    *,
    account: str,
    start_ms: int,
    end_exclusive_ms: int,
    applied: bool,
    units: Sequence[_Unit],
    outcomes: Sequence[Mapping[str, Any]],
    unresolved: Sequence[Mapping[str, Any]],
    basis_before: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    status_by_kind: dict[str, dict[str, int]] = {}
    for item in outcomes:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        kinds = [str(value) for value in item.get("event_kinds") or []]
        for kind in kinds:
            bucket = status_by_kind.setdefault(kind, {})
            bucket[status] = bucket.get(status, 0) + 1
    basis_after = {
        kind: {basis: int(count) for basis, count in counts.items()}
        for kind, counts in basis_before.items()
    }
    effective_status = {
        str(item.get("identity") or ""): str(item.get("status") or "")
        for item in outcomes
    }
    newly_frozen_estimated = 0
    for unit in units:
        if effective_status.get(unit.identity) not in {"preview", "committed"}:
            continue
        for change in unit.changes:
            for kind in ("total", change.event_kind):
                bucket = basis_after[kind]
                bucket[change.before_basis] -= 1
                bucket[change.after_basis] += 1
            if (
                change.before_basis == FeeBasis.MISSING.value
                and change.after_basis == FeeBasis.ESTIMATED.value
            ):
                newly_frozen_estimated += 1
    reason_counts: dict[str, int] = {}
    for item in unresolved:
        reason = str(item.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "schema_version": "order_fee_enrichment_receipt.v1",
        "applied": bool(applied),
        "account": account,
        "start_ms": start_ms,
        "end_exclusive_ms": end_exclusive_ms,
        "unit_count": len(units),
        "event_count": sum(len(unit.changes) for unit in units),
        "observed_event_count": sum(
            int(item.get("event_count") or 0) for item in outcomes
        ),
        "status_counts": status_counts,
        "status_counts_by_event_kind": status_by_kind,
        "basis_counts": basis_after["total"],
        "basis_counts_by_event_kind": {
            kind: counts for kind, counts in basis_after.items() if kind != "total"
        },
        "fee_basis_event_counts_before": {
            kind: dict(counts) for kind, counts in basis_before.items()
        },
        "fee_basis_event_counts_after": basis_after,
        "newly_frozen_estimated_event_count": newly_frozen_estimated,
        "reason_counts": reason_counts,
        "unresolved": [dict(item) for item in unresolved],
        "outcomes": [dict(item) for item in outcomes],
    }


def _fee_basis_event_counts(
    option_events: Sequence[TradeEvent],
    stock_events: Sequence[Mapping[str, Any]],
    *,
    start_ms: int,
    end_exclusive_ms: int,
) -> dict[str, dict[str, int]]:
    basis_values = tuple(item.value for item in FeeBasis)
    counts = {"total": {basis: 0 for basis in basis_values}}
    for event_kind, rows in (
        ("option_trade", option_events),
        ("assigned_stock_sale", stock_events),
    ):
        in_range = [row for row in rows if _in_range(row, start_ms, end_exclusive_ms)]
        if not in_range:
            continue
        bucket = counts.setdefault(event_kind, {basis: 0 for basis in basis_values})
        for row in in_range:
            basis = (
                fee_fact_for_event(row).basis.value
                if isinstance(row, TradeEvent)
                else _stock_fee_fact(row).basis.value
            )
            counts["total"][basis] += 1
            bucket[basis] += 1
    return counts


def _group_options_by_order(
    events: Sequence[TradeEvent],
) -> dict[tuple[str, str, str, str], tuple[TradeEvent, ...]]:
    grouped: dict[tuple[str, str, str, str], list[TradeEvent]] = {}
    for event in events:
        raw = event.raw_payload or {}
        identity = _order_identity(
            event.contract_key.broker,
            event.contract_key.account,
            raw.get("futu_account_id"),
            raw.get("order_id"),
        )
        if identity is not None:
            grouped.setdefault(identity, []).append(event)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_stocks_by_order(
    events: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str, str], tuple[dict[str, Any], ...]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        identity = _order_identity(
            event.get("broker"),
            event.get("account"),
            event.get("futu_account_id"),
            event.get("order_id"),
        )
        if identity is not None:
            grouped.setdefault(identity, []).append(dict(event))
    return {key: tuple(value) for key, value in grouped.items()}


def _formula_option_groups(
    events: Sequence[TradeEvent],
) -> dict[str, tuple[TradeEvent, ...]]:
    grouped: dict[str, list[TradeEvent]] = {}
    for event in events:
        if event.event_type not in {"open", "close"}:
            continue
        raw_payload = event.raw_payload or {}
        source_deal_id = str(
            raw_payload.get("source_deal_id") or raw_payload.get("fee_order_group_id") or ""
        ).strip()
        key = (
            f"deal:{source_deal_id}"
            if event.event_type == "close" and source_deal_id
            else f"event:{event.event_id}"
        )
        grouped.setdefault(key, []).append(event)
    return {key: tuple(value) for key, value in grouped.items()}


def _stock_fee_fact(event: Mapping[str, Any]) -> Any:
    from domain.domain.ledger import fee_fact_from_persisted_evidence

    return fee_fact_from_persisted_evidence(
        event_id=_event_id(event),
        component=FeeComponent.STOCK_SALE,
        provenance=event.get("fee_provenance"),
        compatibility_amount=event.get("fees") or 0,
    )


def _is_bare_fee(payload: Any, fees: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and "fee_provenance" not in payload
        and _money(fees, field="fees") == 0
    )


def _allocate(total: Decimal, weights: Sequence[int]) -> tuple[Decimal, ...]:
    quantities = [int(value) for value in weights]
    denominator = sum(quantities)
    if not quantities or denominator <= 0 or any(value <= 0 for value in quantities):
        raise ValueError("fee allocation quantities must be positive")
    allocated = Decimal(0)
    out: list[Decimal] = []
    for index, quantity in enumerate(quantities):
        amount = (
            (total - allocated).quantize(_MONEY)
            if index == len(quantities) - 1
            else (total * Decimal(quantity) / Decimal(denominator)).quantize(_MONEY)
        )
        out.append(amount)
        allocated = (allocated + amount).quantize(_MONEY)
    if allocated != total.quantize(_MONEY):
        raise ValueError("fee allocation does not conserve total")
    return tuple(out)


def _option_contract_identity(event: TradeEvent) -> tuple[Any, ...]:
    key = event.contract_key
    return (
        key.broker,
        key.account,
        key.underlying_symbol,
        key.option_type,
        key.position_side,
        key.strike,
        key.expiration_ymd,
        event.currency,
        event.multiplier,
    )


def _order_identity(
    broker: Any, account: Any, futu_account_id: Any, order_id: Any
) -> tuple[str, str, str, str] | None:
    values = tuple(
        str(value or "").strip()
        for value in (broker, account, futu_account_id, order_id)
    )
    if not all(values):
        return None
    return values[0], values[1].lower(), values[2], values[3]


def _unit_identity(prefix: str, identity: Sequence[str]) -> str:
    return f"{prefix}:{_sha256(chr(31).join(identity))[:24]}"


def _order_issue(observation: ActualOrderFee, reason: str) -> dict[str, Any]:
    return {
        "event_kind": "order_group",
        "identity_sha256": _sha256(chr(31).join(observation.identity)),
        "reason": reason,
    }


def _event_id(value: Any) -> str:
    if isinstance(value, TradeEvent):
        return value.event_id
    return str(value.get("stock_event_id") or value.get("event_id") or "").strip()


def _event_time_ms(value: Any) -> int:
    if isinstance(value, TradeEvent):
        return value.event_time_ms
    return int(value.get("trade_time_ms") or value.get("event_time_ms") or 0)


def _event_currency(value: Any) -> str:
    if isinstance(value, TradeEvent):
        return value.currency
    return str(value.get("currency") or "").strip().upper()


def _in_range(value: Any, start_ms: int, end_exclusive_ms: int) -> bool:
    instant = _event_time_ms(value)
    return start_ms <= instant < end_exclusive_ms


def _money(value: Any, *, field: str) -> Decimal:
    return quantize_money(to_decimal(value, field_name=field))


def _positive_int(value: Any, *, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_sha256(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("fee_details_sha256 must be lowercase SHA-256")
    return text


def _duplicates(values: Sequence[Any] | Any) -> set[Any]:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["ActualOrderFee", "enrich_order_fees"]
