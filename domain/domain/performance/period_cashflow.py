"""Event-time option cash only; cohort returns remain owned by weighted_reducer."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Iterable, Mapping

from domain.domain.ledger.cash_facts import OptionFeeCashEvent, TradeCashFact, cash_facts_for_trade_event
from domain.domain.ledger.economics import fee_fact_for_event
from domain.domain.ledger.events import CLOSE_EVENT_TYPES, TradeEvent, lot_id_for_open_event
from domain.domain.ledger.fees import FeeBasis
from domain.domain.ledger.projection import ProjectionResult
from domain.domain.money import quantize_money
from domain.domain.option_position_identity import normalize_broker, normalize_currency
from domain.domain.performance.cash_conversion import validate_observed_cash_conversion
from domain.domain.performance.period import PeriodWindow
from domain.domain.strategy_membership import resolve_option_strategy_membership, resolve_strategy_metadata

PERIOD_OPTION_NET_CASHFLOW_SCHEMA = "period_option_net_cashflow.v1"
_ATTRIBUTION_DIAGNOSTICS = frozenset({
    "cc_lp_membership_incomplete", "combo_membership_incomplete", "csp_lc_membership_incomplete",
})


def reduce_period_option_cashflow(
    projection: ProjectionResult,
    *,
    period: PeriodWindow,
    accounts: Iterable[str] | None,
    broker: str | None = None,
    fee_cash_events: Iterable[OptionFeeCashEvent] = (),
    fee_mode: str = "actual",
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Reduce resolved option cash facts once, with fail-closed evidence quality."""
    start, end = period.effective_start_at_ms, period.effective_end_exclusive_at_ms
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("period must be a nonnegative half-open window with positive duration")
    if fee_mode not in {"actual", "simulated"}:
        raise ValueError("fee_mode must be actual or simulated")
    if isinstance(accounts, (str, bytes)):
        raise ValueError("accounts must be a collection")
    scope = None if accounts is None else tuple(accounts)
    if scope is not None and (not scope or any(not isinstance(v, str) or not v.strip() for v in scope)):
        raise ValueError("accounts must contain nonblank account names")
    scope = None if scope is None else {v.strip().lower() for v in scope}
    if broker is not None and (not isinstance(broker, str) or not broker.strip()):
        raise ValueError("broker must be nonblank")
    broker_scope = normalize_broker(broker) if broker is not None else None

    def in_scope(item: Any) -> bool:
        key = item.contract_key
        return (scope is None or key.account in scope) and (not broker_scope or key.broker == broker_scope)

    events: dict[str, TradeEvent] = {}
    for event in projection.effective_cash_events:
        if event.event_id in events and events[event.event_id].to_dict() != event.to_dict():
            raise ValueError(f"conflicting cash event identity: {event.event_id}")
        events[event.event_id] = event
    fees: dict[str, OptionFeeCashEvent] = {}
    replacements: dict[str, OptionFeeCashEvent] = {}
    for fee in fee_cash_events:
        if not isinstance(fee, OptionFeeCashEvent):
            raise ValueError("fee_cash_events must contain OptionFeeCashEvent values")
        if fee.fee_id in fees:
            if fees[fee.fee_id].to_dict() != fee.to_dict():
                raise ValueError(f"conflicting fee identity: {fee.fee_id}")
            continue
        event = events.get(fee.source_event_id)
        if event is None or event.asset_type != "option" or event.event_type not in {"open", *CLOSE_EVENT_TYPES}:
            raise ValueError(f"fee must link to an effective option trade: {fee.fee_id}")
        if fee.replaces_attached_fee:
            if fee.effective_at_ms != event.event_time_ms:
                raise ValueError("replacement fee effective time must equal linked trade time")
            if event.event_id in replacements:
                raise ValueError("at most one attached fee replacement per trade")
            replacements[event.event_id] = fee
        fees[fee.fee_id] = fee

    opens = {lot_id_for_open_event(event): event for event in projection.effective_open_events}
    group_counts: Counter[str] = Counter()
    for event in opens.values():
        resolved = resolve_strategy_metadata(event.raw_payload, source_id=event.event_id)
        group = resolved.metadata.strategy_group_id or ""
        if not resolved.issues and group and (
            resolved.metadata.strategy == "combo_yield" or group.startswith("combo_yield:")
        ):
            group_counts[group] += 1
    valid_groups = {group for group, count in group_counts.items() if count == 2}
    lots = {lot.lot_id: lot for lot in projection.lots}
    event_lots: dict[str, set[str]] = {}
    for event in events.values():
        event_lots[event.event_id] = ({lot_id_for_open_event(event)} if event.event_type == "open"
                                    else {event.target_lot_id} if event.target_lot_id else set())
    for allocation in projection.allocations:
        event_lots.setdefault(allocation.close_event_id, set()).add(allocation.target_lot_id)

    diagnostics: list[tuple[Any, set[str]]] = []
    for diagnostic in projection.diagnostics:
        if scope is not None and diagnostic.account and diagnostic.account not in scope:
            continue
        if broker_scope and diagnostic.broker and normalize_broker(diagnostic.broker) != broker_scope:
            continue
        details = diagnostic.details if isinstance(diagnostic.details, Mapping) else {}
        targets = {str(details["target_lot_id"])} if details.get("target_lot_id") else set()
        if isinstance(details.get("target_lot_ids"), (list, tuple)):
            targets.update(str(v) for v in details["target_lot_ids"] if v)
        targets.update(event_lots.get(diagnostic.event_id, ()))
        if targets and targets <= lots.keys() and not any(in_scope(lots[v]) for v in targets):
            continue
        related = [event for event in events.values() if event.event_id == diagnostic.event_id
                   or event_lots.get(event.event_id, set()) & targets]
        related = [event for event in related if in_scope(event)]
        # Absent facts or unreliable adjusted times cannot prove the damage is outside the window.
        located = diagnostic.event_id in events and related and (not targets or targets <= opens.keys())
        if located and not details.get("cohort_time_unreliable"):
            related_ids = {event.event_id for event in related}
            if not any(period.contains(event.event_time_ms) for event in related) and not any(
                fee.source_event_id in related_ids and period.contains(fee.effective_at_ms) for fee in fees.values()
            ):
                continue
        diagnostics.append((diagnostic, targets))
    economic_missing: set[str] = set()
    unscoped_economic_missing: set[str] = set()
    economic_by_currency: dict[str, set[str]] = defaultdict(set)
    for diagnostic, targets in diagnostics:
        if diagnostic.severity not in {"error", "warning"} or diagnostic.code in _ATTRIBUTION_DIAGNOSTICS:
            continue
        reason = "currency_conflict" if diagnostic.code == "target_economic_units_mismatch" else diagnostic.code
        economic_missing.add(reason)
        related = [event for event in events.values() if in_scope(event) and (
            event.event_id == diagnostic.event_id or event_lots.get(event.event_id, set()) & targets
        )]
        if (targets and not targets <= lots.keys()) or (not targets and not related):
            unscoped_economic_missing.add(reason)
            continue
        currencies = [lots[target].currency for target in targets if in_scope(lots[target])]
        currencies.extend(event.currency for event in related)
        details = diagnostic.details if isinstance(diagnostic.details, Mapping) else {}
        # Suppressed terminal events can name a second damaged currency in the projector diagnostic.
        currencies.extend(details[key] for key in ("lot_currency", "event_currency") if key in details)
        related_ids = {event.event_id for event in related}
        currencies.extend(fee.currency for fee in fees.values()
                          if fee.source_event_id in related_ids and period.contains(fee.effective_at_ms))
        try:
            normalized = {normalize_currency(currency, strict=True) for currency in currencies}
        except ValueError:
            normalized = set()
        if not normalized:
            unscoped_economic_missing.add(reason)
        else:
            for currency in normalized:
                economic_by_currency[currency].add(reason)

    facts: list[dict[str, Any]] = []

    def append_fact(cash: TradeCashFact, event: TradeEvent, *, basis: str,
                    fact_id: str | None = None, source: str | None = None) -> None:
        if not period.contains(cash.effective_at_ms):
            return
        missing: set[str] = set()
        currency = None
        try:
            currency = normalize_currency(cash.currency, strict=True)
        except ValueError:
            missing.add("option_currency_missing")
        amount = cash.amount
        if amount is None:
            missing.add("actual_fee_missing" if cash.fact_kind == "option_fee_cash" else "option_cash_missing")
        if cash.fact_kind == "option_fee_cash" and basis != "actual" and fee_mode == "actual":
            missing.add("actual_fee_missing")
        if cash.fact_kind == "option_trade_cash_gross" and event.event_type in {
            "assignment", "exercise", "expire_close",
        } and event.price != 0:
            missing.add("physical_settlement_option_cash_invalid")
        if missing:
            amount = None
        identity = fact_id or cash.fact_id
        cny_amount, cny_missing = None, set(missing)
        if not missing:
            if amount == 0 or currency == "CNY":
                cny_amount = amount
            elif not isinstance(cash.cash_conversion, Mapping):
                cny_missing.add("cash_conversion_missing")
            else:
                conversion = cash.cash_conversion
                if conversion.get("status") != "observed":
                    cny_missing.add("cash_conversion_pending" if conversion.get("status") == "pending"
                                    else "cash_conversion_invalid")
                else:
                    cny_amount, issue = validate_observed_cash_conversion(
                        conversion, cash_fact_id=identity, native_amount=amount,
                        native_currency=currency, effective_at_ms=cash.effective_at_ms,
                    )
                    if issue or cny_amount is None:
                        cny_missing.add(f"cash_conversion_corrupt:{issue or 'unknown'}")
        memberships, attribution_missing = [], set()
        targets = event_lots.get(event.event_id, set())
        for target in sorted(targets):
            opening = opens.get(target)
            if opening is None:
                attribution_missing.add("strategy_opening_missing")
                continue
            membership = resolve_option_strategy_membership(
                opening.contract_key, opening.position_side, opening.raw_payload,
                valid_combo_group_ids=valid_groups, source_id=opening.event_id,
            )
            memberships.append({"open_lot_id": target, **membership.to_dict()})
            attribution_missing.update(membership.issues)
        if not targets:
            attribution_missing.add("strategy_opening_missing")
        for diagnostic, affected in diagnostics:
            if diagnostic.code in _ATTRIBUTION_DIAGNOSTICS and (
                not affected or affected & targets or diagnostic.event_id == event.event_id
            ):
                attribution_missing.add("strategy_attribution_conflict")
        single = memberships[0] if len(memberships) == 1 else {}
        facts.append({
            "account": event.contract_key.account, "broker": event.contract_key.broker,
            "symbol": event.contract_key.underlying_symbol, "source_event_id": event.event_id,
            "fact_id": identity, "fact_kind": cash.fact_kind, "effective_at_ms": cash.effective_at_ms,
            "currency": currency, "amount": amount, "basis": basis, "source": source,
            "missing": tuple(sorted(missing)), "cny_amount": cny_amount,
            "cny_missing": tuple(sorted(cny_missing)), "attribution": tuple(memberships),
            "attribution_strategy": single.get("strategy"), "leg_type": single.get("leg_type"),
            "strategy_group_id": single.get("strategy_group_id"),
            "attribution_missing": tuple(sorted(attribution_missing)),
        })

    for event in events.values():
        if event.asset_type != "option" or not in_scope(event):
            continue
        fee = fee_fact_for_event(event)
        for cash in cash_facts_for_trade_event(event):
            if cash.fact_kind not in {"option_trade_cash_gross", "option_fee_cash"}:
                continue
            if cash.fact_kind == "option_fee_cash" and event.event_id in replacements:
                continue
            append_fact(cash, event, basis=fee.basis.value if cash.fact_kind == "option_fee_cash" else "actual",
                        source=fee.source if cash.fact_kind == "option_fee_cash" else event.source)
    for fee in fees.values():
        event = events[fee.source_event_id]
        if in_scope(event):
            append_fact(TradeCashFact(
                fact_kind="option_fee_cash", effective_at_ms=fee.effective_at_ms, currency=fee.currency,
                source_event_id=event.event_id, amount=fee.amount, cash_conversion=fee.cash_conversion,
            ), event, basis=fee.basis.value, fact_id=fee.fact_id, source=fee.source)
    facts.sort(key=lambda fact: (fact["effective_at_ms"], fact["fact_id"]))
    identities: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if fact["fact_id"] in identities and identities[fact["fact_id"]] != fact:
            raise ValueError(f"conflicting cash fact identity: {fact['fact_id']}")
        identities[fact["fact_id"]] = fact
    ordered = tuple(identities.values())
    unknown_currency = {reason for fact in ordered if fact["currency"] is None for reason in fact["missing"]}
    global_missing = unscoped_economic_missing | unknown_currency

    def component(selected: Iterable[Mapping[str, Any]], *, currency: str | None = None,
                  cny: bool = False) -> dict[str, Any]:
        selected = tuple(selected)
        missing_key, amount_key = ("cny_missing", "cny_amount") if cny else ("missing", "amount")
        scoped_missing = economic_missing if cny else economic_by_currency.get(currency, set())
        missing = global_missing | scoped_missing | {reason for fact in selected for reason in fact[missing_key]}
        estimated = any(fact["basis"] == FeeBasis.ESTIMATED for fact in selected)
        return {
            "amount": None if missing else quantize_money(sum((fact[amount_key] for fact in selected), Decimal(0))),
            "status": "partial" if missing else "estimated" if estimated else "observed",
            "missing": tuple(sorted(missing)),
        }

    currencies = sorted({fact["currency"] for fact in ordered if fact["currency"] is not None}
                        | economic_by_currency.keys())
    by_currency = {}
    for currency in currencies:
        selected = [fact for fact in ordered if fact["currency"] == currency]
        by_currency[currency] = {
            "total": component(selected, currency=currency),
            "gross": component((fact for fact in selected if fact["fact_kind"] == "option_trade_cash_gross"),
                               currency=currency),
            "fees": component((fact for fact in selected if fact["fact_kind"] == "option_fee_cash"), currency=currency),
        }
    cny_total = {"currency": "CNY", **component(ordered, cny=True)}
    missing = global_missing | set(cny_total["missing"]) | {
        reason for components in by_currency.values() for value in components.values() for reason in value["missing"]
    }
    return {
        "schema_version": PERIOD_OPTION_NET_CASHFLOW_SCHEMA,
        "selection": "cash_event_effective_time",
        "time_policy": "trade_event_time_with_explicit_fee_effective_time",
        "fee_mode": fee_mode, "by_currency": by_currency, "cny_total": cny_total,
        "status": "partial" if missing else "estimated" if cny_total["status"] == "estimated" else "observed",
        "missing": tuple(sorted(missing)), "fact_count": len(ordered),
    }, ordered


__all__ = ["PERIOD_OPTION_NET_CASHFLOW_SCHEMA", "reduce_period_option_cashflow"]
