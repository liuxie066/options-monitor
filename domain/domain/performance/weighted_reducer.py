from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.ledger.economics import OptionEconomicAllocation, fee_fact_for_event
from domain.domain.ledger.events import LedgerDiagnostic, TradeEvent, lot_id_for_open_event
from domain.domain.ledger.fees import FeeBasis, FeeFact
from domain.domain.ledger.lots import PositionLot
from domain.domain.ledger.position_fields import strategy_metadata_fields_from_payload
from domain.domain.ledger.projection import ProjectionResult
from domain.domain.money import quantize_money, to_decimal
from domain.domain.performance.cash_conversion import validate_observed_cash_conversion
from domain.domain.performance.models import (
    CAPITAL_DAYS_QUANTUM,
    MILLISECONDS_PER_DAY,
    MetricStatus,
)
from domain.domain.performance.period import PeriodWindow


_REPORTING_TIMEZONE = ZoneInfo("Asia/Shanghai")
_RATE_QUANTUM = Decimal("0.000000000001")
_ECONOMIC_FAILURES = {
    "currency_conflict",
    "economic_adjust_invalid",
    "economic_adjust_non_conserving",
}
_ISOLATABLE_ERROR_DIAGNOSTICS = {
    "economic_adjust_invalid",
    "economic_adjust_non_conserving",
    "target_economic_units_mismatch",
}
_DIAGNOSTIC_REASONS = {
    "economic_adjust_invalid": "economic_adjust_invalid",
    "economic_adjust_non_conserving": "economic_adjust_non_conserving",
    "combo_membership_incomplete": "strategy_attribution_conflict",
    "csp_lc_membership_incomplete": "strategy_attribution_conflict",
    "cc_lp_membership_incomplete": "strategy_attribution_conflict",
    "target_economic_units_mismatch": "currency_conflict",
}
_EXPIRY_CLOSE_TYPES = {
    "expire_auto_close",
    "expire_close",
    "expiration_close",
    "expiration_zero_close",
    "expired",
}


class PerformanceScopeError(ValueError):
    pass


@dataclass(frozen=True)
class WeightedOptionFact:
    fact_id: str
    open_lot_id: str
    open_event_id: str
    terminal_event_id: str | None
    account: str
    broker: str
    symbol: str
    currency: str
    leg_type: str
    attribution_strategy: str
    strategy_group_id: str | None
    source_stock_lot_id: str | None
    opened_at_ms: int
    terminal_at_ms: int | None
    expiration_ymd: str
    terminal_kind: str | None
    state: str
    strike: Decimal
    multiplier: Decimal
    contracts: int
    opening_option_cash: Decimal
    opening_actual_fee: Decimal | None
    terminal_option_cash: Decimal | None
    terminal_actual_fee: Decimal | None
    option_net_cashflow: Decimal | None
    occupied_capital: Decimal | None
    capital_days: Decimal | None
    win_eligible: bool
    win: bool | None
    status: MetricStatus
    missing: tuple[str, ...] = ()
    cash_missing: tuple[str, ...] = field(default=(), repr=False)
    capital_missing: tuple[str, ...] = field(default=(), repr=False)
    win_missing: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class PerformanceReduction:
    period: PeriodWindow
    facts: tuple[WeightedOptionFact, ...]
    bundle: Mapping[str, Any]
    breakdowns: Mapping[str, tuple[Mapping[str, Any], ...]]
    diagnostics: tuple[LedgerDiagnostic, ...]


def reduce_option_performance(
    projection: ProjectionResult,
    *,
    period: PeriodWindow,
    account: str | None = None,
    accounts: Iterable[str] | None = None,
    broker: str | None = None,
) -> PerformanceReduction:
    """Reduce one fully resolved ledger projection into weighted cohort facts."""

    account_scope = str(account or "").strip().lower() or None
    account_scopes = {str(item or "").strip().lower() for item in accounts or () if str(item or "").strip()}
    if account_scope:
        account_scopes = {account_scope}
    broker_scope = str(broker or "").strip().lower() or None
    diagnostics = tuple(
        item
        for item in projection.diagnostics
        if _diagnostic_in_scope(
            item,
            accounts=account_scopes,
            broker=broker_scope,
        )
    )
    lots_by_id = {lot.lot_id: lot for lot in projection.lots}
    diagnostics = tuple(
        item
        for item in diagnostics
        if _diagnostic_in_period(item, lots_by_id=lots_by_id, period=period)
    )
    if any(item.severity == "error" and item.code not in _ISOLATABLE_ERROR_DIAGNOSTICS for item in diagnostics):
        raise PerformanceScopeError("ledger projection contains an in-scope error")

    diagnostic_missing = tuple(
        sorted({reason for item in diagnostics if (reason := _DIAGNOSTIC_REASONS.get(item.code))})
    )
    affected_lot_missing = _affected_lot_missing(diagnostics)
    failed_lot_ids = {
        lot_id
        for lot_id, missing in affected_lot_missing.items()
        if any(reason in _ECONOMIC_FAILURES for reason in missing)
    }

    opens_by_lot = {lot_id_for_open_event(event): event for event in projection.effective_open_events}
    selected_lot_ids = {
        lot_id
        for lot_id, lot in lots_by_id.items()
        if period.contains(lot.opened_at_ms)
        and (not account_scopes or lot.contract_key.account in account_scopes)
        and (not broker_scope or lot.contract_key.broker == broker_scope)
    }
    if selected_lot_ids - set(opens_by_lot) - failed_lot_ids:
        raise PerformanceScopeError("effective opening facts are incomplete")

    allocations_by_lot: dict[str, list[OptionEconomicAllocation]] = defaultdict(list)
    for allocation in projection.allocations:
        allocations_by_lot[allocation.target_lot_id].append(allocation)
    unknown_lots = set(allocations_by_lot) - set(lots_by_id)
    if unknown_lots:
        raise PerformanceScopeError("economic allocation targets an unknown lot")

    combo_group_counts: dict[str, int] = defaultdict(int)
    for event in projection.effective_open_events:
        metadata = strategy_metadata_fields_from_payload(event.raw_payload)
        group_id = str(metadata.get("strategy_group_id") or "").strip()
        if str(metadata.get("strategy") or "").strip().lower() == "combo_yield" and group_id:
            combo_group_counts[group_id] += 1
    valid_combo_group_ids = {
        group_id for group_id, count in combo_group_counts.items() if count == 2
    }

    facts: list[WeightedOptionFact] = []
    for lot_id in sorted(lots_by_id):
        lot = lots_by_id[lot_id]
        if not period.contains(lot.opened_at_ms):
            continue
        if account_scopes and lot.contract_key.account not in account_scopes:
            continue
        if broker_scope and lot.contract_key.broker != broker_scope:
            continue
        lot_missing = set(affected_lot_missing.get(lot_id, ()))
        if lot_id in failed_lot_ids:
            facts.append(_failed_fact(lot, missing=tuple(sorted(lot_missing))))
            continue
        open_event = opens_by_lot[lot_id]
        attribution_strategy, strategy_group_id, source_stock_lot_id, attribution_missing = _attribution_for_lot(
            lot,
            open_event,
            valid_combo_group_ids=valid_combo_group_ids,
        )
        lot_missing.update(attribution_missing)
        facts.extend(
            _facts_for_lot(
                lot,
                open_event,
                allocations_by_lot.get(lot_id, ()),
                period=period,
                attribution_strategy=attribution_strategy,
                strategy_group_id=strategy_group_id,
                source_stock_lot_id=source_stock_lot_id,
                lot_missing=tuple(sorted(lot_missing)),
            )
        )

    ordered_facts = tuple(sorted(facts, key=_fact_sort_key))
    breakdowns = _breakdowns(
        ordered_facts,
        statistic_days=period.statistic_days,
    )
    bundle = _aggregate_bundle(
        ordered_facts,
        statistic_days=period.statistic_days,
        scoped_missing=(reason for reason in diagnostic_missing if reason != "strategy_attribution_conflict"),
    )
    cny_total = _cny_cashflow_total(
        ordered_facts,
        events=projection.effective_cash_events,
    )
    cny_missing = set(cny_total["missing"])
    bundle = {
        **bundle,
        "option_net_cashflow": {
            **bundle["option_net_cashflow"],
            "cny_total": cny_total,
        },
        "status": MetricStatus.PARTIAL if cny_missing else bundle["status"],
        "missing": tuple(sorted({*bundle["missing"], *cny_missing})),
    }
    partial_breakdown_missing = {
        reason
        for rows in breakdowns.values()
        for row in rows
        if row["status"] == MetricStatus.PARTIAL
        for reason in row["missing"]
    }
    if partial_breakdown_missing:
        bundle = {
            **bundle,
            "status": MetricStatus.PARTIAL,
            "missing": tuple(sorted({*bundle["missing"], *partial_breakdown_missing})),
        }
    return PerformanceReduction(
        period=period,
        facts=ordered_facts,
        bundle=bundle,
        breakdowns=breakdowns,
        diagnostics=diagnostics,
    )


def _diagnostic_in_scope(
    diagnostic: LedgerDiagnostic,
    *,
    accounts: set[str],
    broker: str | None,
) -> bool:
    if accounts and diagnostic.account and diagnostic.account not in accounts:
        return False
    if broker and diagnostic.broker and diagnostic.broker != broker:
        return False
    return True


def _diagnostic_in_period(
    diagnostic: LedgerDiagnostic,
    *,
    lots_by_id: Mapping[str, PositionLot],
    period: PeriodWindow,
) -> bool:
    details = diagnostic.details if isinstance(diagnostic.details, dict) else {}
    if details.get("cohort_time_unreliable") is True:
        return True
    lot_ids = [str(details.get("target_lot_id") or "").strip()]
    raw_many = details.get("target_lot_ids")
    if isinstance(raw_many, (list, tuple)):
        lot_ids.extend(str(item or "").strip() for item in raw_many)
    lot_ids = [item for item in lot_ids if item]
    if not lot_ids or any(item not in lots_by_id for item in lot_ids):
        return True
    return any(period.contains(lots_by_id[item].opened_at_ms) for item in lot_ids)


def _affected_lot_missing(
    diagnostics: Sequence[LedgerDiagnostic],
) -> dict[str, tuple[str, ...]]:
    affected: dict[str, set[str]] = defaultdict(set)
    for item in diagnostics:
        reason = _DIAGNOSTIC_REASONS.get(item.code)
        if not reason:
            continue
        details = item.details if isinstance(item.details, dict) else {}
        lot_ids = [details.get("target_lot_id")]
        raw_many = details.get("target_lot_ids")
        if isinstance(raw_many, (list, tuple)):
            lot_ids.extend(raw_many)
        for value in lot_ids:
            lot_id = str(value or "").strip()
            if lot_id:
                affected[lot_id].add(reason)
    return {key: tuple(sorted(value)) for key, value in affected.items()}


def _attribution_for_lot(
    lot: PositionLot,
    event: TradeEvent,
    *,
    valid_combo_group_ids: set[str],
) -> tuple[str, str | None, str | None, tuple[str, ...]]:
    metadata = strategy_metadata_fields_from_payload(event.raw_payload)
    strategy = str(metadata.get("strategy") or "").strip().lower()
    role = str(metadata.get("leg_role") or "").strip().lower()
    group_id = str(metadata.get("strategy_group_id") or "").strip() or None
    stock_lot_id = str(metadata.get("source_stock_lot_id") or "").strip() or None
    default = _default_attribution(lot)
    if strategy == "combo_yield" and group_id in valid_combo_group_ids:
        if role in {"funding_put", "participation_call"}:
            return "csp_lc", group_id, None, ()
        if role in {"short_call", "long_put"}:
            return "cc_lp", group_id, None, ()
    if strategy == "wheel" or role == "wheel_call" or stock_lot_id:
        if (
            strategy == "wheel"
            and role == "wheel_call"
            and stock_lot_id
            and lot.contract_key.option_type == "call"
            and lot.contract_key.position_side == "short"
        ):
            return "wheel", group_id, stock_lot_id, ()
        return default, None, None, ("strategy_attribution_conflict",)
    if strategy == "combo_yield" or (group_id and group_id.startswith("combo_yield:")):
        return default, None, None, ("strategy_attribution_conflict",)
    return default, None, None, ()


def _default_attribution(lot: PositionLot) -> str:
    key = lot.contract_key
    if key.position_side == "short":
        return "csp" if key.option_type == "put" else "cc"
    return "unassigned"


def _facts_for_lot(
    lot: PositionLot,
    open_event: TradeEvent,
    allocations: Iterable[OptionEconomicAllocation],
    *,
    period: PeriodWindow,
    attribution_strategy: str,
    strategy_group_id: str | None,
    source_stock_lot_id: str | None,
    lot_missing: tuple[str, ...],
) -> list[WeightedOptionFact]:
    admitted = sorted(
        (item for item in allocations if item.closed_at_ms < period.effective_end_exclusive_at_ms),
        key=lambda item: (item.closed_at_ms, item.allocation_id),
    )
    allocated_contracts = sum(item.contracts for item in admitted)
    remaining = lot.contracts_opened - allocated_contracts
    if remaining < 0:
        raise PerformanceScopeError("allocation contracts exceed the opening lot")

    facts = [
        _terminated_fact(
            lot,
            allocation,
            period=period,
            attribution_strategy=attribution_strategy,
            strategy_group_id=strategy_group_id,
            source_stock_lot_id=source_stock_lot_id,
            lot_missing=lot_missing,
        )
        for allocation in admitted
    ]
    if remaining:
        facts.append(
            _residual_fact(
                lot,
                open_event,
                admitted,
                remaining=remaining,
                period=period,
                attribution_strategy=attribution_strategy,
                strategy_group_id=strategy_group_id,
                source_stock_lot_id=source_stock_lot_id,
                lot_missing=lot_missing,
            )
        )
    return facts


def _failed_fact(
    lot: PositionLot,
    *,
    missing: tuple[str, ...],
) -> WeightedOptionFact:
    return WeightedOptionFact(
        fact_id=f"failed:{lot.lot_id}",
        open_lot_id=lot.lot_id,
        open_event_id=lot.open_event_id,
        terminal_event_id=None,
        account=lot.contract_key.account,
        broker=lot.contract_key.broker,
        symbol=lot.contract_key.underlying_symbol,
        currency=lot.currency,
        leg_type=_leg_type(lot),
        attribution_strategy=_default_attribution(lot),
        strategy_group_id=None,
        source_stock_lot_id=None,
        opened_at_ms=lot.opened_at_ms,
        terminal_at_ms=None,
        expiration_ymd=lot.contract_key.expiration_ymd,
        terminal_kind=None,
        state="failed",
        strike=to_decimal(lot.contract_key.strike, field_name="strike"),
        multiplier=to_decimal(lot.multiplier, field_name="multiplier"),
        contracts=lot.contracts_opened,
        opening_option_cash=_opening_cash(lot, lot.contracts_opened),
        opening_actual_fee=None,
        terminal_option_cash=None,
        terminal_actual_fee=None,
        option_net_cashflow=None,
        occupied_capital=None,
        capital_days=None,
        win_eligible=False,
        win=None,
        status=MetricStatus.PARTIAL,
        missing=missing,
        cash_missing=missing,
        capital_missing=missing,
        win_missing=missing,
    )


def _terminated_fact(
    lot: PositionLot,
    allocation: OptionEconomicAllocation,
    *,
    period: PeriodWindow,
    attribution_strategy: str,
    strategy_group_id: str | None,
    source_stock_lot_id: str | None,
    lot_missing: tuple[str, ...],
) -> WeightedOptionFact:
    terminal_kind = _terminal_kind(allocation)
    unresolved = (
        terminal_kind == "conflicting"
        and _expiration_end_ms(lot.contract_key.expiration_ymd) <= period.effective_end_exclusive_at_ms
    )
    cash_missing = {
        *_fee_missing(allocation.allocated_open_fee),
        *_fee_missing(allocation.close_fee, exercise=terminal_kind == "exercise"),
    }
    net_cash = None
    if not cash_missing:
        assert allocation.allocated_open_fee.amount is not None
        assert allocation.close_fee.amount is not None
        net_cash = quantize_money(
            allocation.open_amount_gross
            + allocation.close_amount_gross
            - allocation.allocated_open_fee.amount
            - allocation.close_fee.amount
        )
    leg_type = _leg_type(lot)
    win_eligible, win, win_missing = _terminal_win(
        leg_type,
        terminal_kind=terminal_kind,
        net_cash=net_cash,
        cash_missing=cash_missing,
    )
    occupied_capital, capital_days, capital_missing = _capital(
        lot,
        contracts=allocation.contracts,
        opened_at_ms=lot.opened_at_ms,
        end_at_ms=allocation.closed_at_ms,
        unresolved_reason=("terminal_evidence_conflict" if unresolved else None),
    )
    missing = tuple(sorted({*lot_missing, *cash_missing, *capital_missing, *win_missing}))
    return WeightedOptionFact(
        fact_id=allocation.allocation_id,
        open_lot_id=lot.lot_id,
        open_event_id=allocation.open_event_id,
        terminal_event_id=allocation.close_event_id,
        account=lot.contract_key.account,
        broker=lot.contract_key.broker,
        symbol=lot.contract_key.underlying_symbol,
        currency=lot.currency,
        leg_type=leg_type,
        attribution_strategy=attribution_strategy,
        strategy_group_id=strategy_group_id,
        source_stock_lot_id=source_stock_lot_id,
        opened_at_ms=lot.opened_at_ms,
        terminal_at_ms=allocation.closed_at_ms,
        expiration_ymd=lot.contract_key.expiration_ymd,
        terminal_kind=terminal_kind,
        state="unresolved_after_expiry" if unresolved else "terminated",
        strike=to_decimal(lot.contract_key.strike, field_name="strike"),
        multiplier=to_decimal(lot.multiplier, field_name="multiplier"),
        contracts=allocation.contracts,
        opening_option_cash=allocation.open_amount_gross,
        opening_actual_fee=_actual_fee(allocation.allocated_open_fee),
        terminal_option_cash=allocation.close_amount_gross,
        terminal_actual_fee=_actual_fee(allocation.close_fee),
        option_net_cashflow=net_cash,
        occupied_capital=occupied_capital,
        capital_days=capital_days,
        win_eligible=win_eligible,
        win=win,
        status=MetricStatus.PARTIAL if missing else MetricStatus.OBSERVED,
        missing=missing,
        cash_missing=tuple(sorted(cash_missing)),
        capital_missing=tuple(sorted(capital_missing)),
        win_missing=tuple(sorted(win_missing)),
    )


def _residual_fact(
    lot: PositionLot,
    open_event: TradeEvent,
    admitted: Sequence[OptionEconomicAllocation],
    *,
    remaining: int,
    period: PeriodWindow,
    attribution_strategy: str,
    strategy_group_id: str | None,
    source_stock_lot_id: str | None,
    lot_missing: tuple[str, ...],
) -> WeightedOptionFact:
    total_open_cash = _opening_cash(lot, lot.contracts_opened)
    opening_cash = quantize_money(total_open_cash - sum((item.open_amount_gross for item in admitted), Decimal(0)))
    total_open_fee = fee_fact_for_event(open_event)
    cash_missing = set(_fee_missing(total_open_fee))
    opening_actual_fee = None
    net_cash = None
    if not cash_missing:
        assert total_open_fee.amount is not None
        allocated = sum(
            (item.allocated_open_fee.amount or Decimal(0) for item in admitted),
            Decimal(0),
        )
        opening_actual_fee = quantize_money(total_open_fee.amount - allocated)
        if opening_actual_fee < 0:
            raise PerformanceScopeError("opening fee allocations do not conserve")
        net_cash = quantize_money(opening_cash - opening_actual_fee)

    unresolved = _expiration_end_ms(lot.contract_key.expiration_ymd) <= (period.effective_end_exclusive_at_ms)
    state = "unresolved_after_expiry" if unresolved else "open"
    capital_end = period.effective_end_exclusive_at_ms
    occupied_capital, capital_days, capital_missing = _capital(
        lot,
        contracts=remaining,
        opened_at_ms=lot.opened_at_ms,
        end_at_ms=capital_end,
        unresolved_reason=("terminal_evidence_missing" if unresolved else None),
    )
    win_missing = {"terminal_evidence_missing"} if unresolved else set()
    split_missing = {"terminal_evidence_missing"} if unresolved else set()
    missing = tuple(
        sorted(
            {
                *lot_missing,
                *cash_missing,
                *capital_missing,
                *win_missing,
                *split_missing,
            }
        )
    )
    return WeightedOptionFact(
        fact_id=f"residual:{lot.lot_id}:{period.effective_end_exclusive_at_ms}",
        open_lot_id=lot.lot_id,
        open_event_id=lot.open_event_id,
        terminal_event_id=None,
        account=lot.contract_key.account,
        broker=lot.contract_key.broker,
        symbol=lot.contract_key.underlying_symbol,
        currency=lot.currency,
        leg_type=_leg_type(lot),
        attribution_strategy=attribution_strategy,
        strategy_group_id=strategy_group_id,
        source_stock_lot_id=source_stock_lot_id,
        opened_at_ms=lot.opened_at_ms,
        terminal_at_ms=None,
        expiration_ymd=lot.contract_key.expiration_ymd,
        terminal_kind=None,
        state=state,
        strike=to_decimal(lot.contract_key.strike, field_name="strike"),
        multiplier=to_decimal(lot.multiplier, field_name="multiplier"),
        contracts=remaining,
        opening_option_cash=opening_cash,
        opening_actual_fee=opening_actual_fee,
        terminal_option_cash=None,
        terminal_actual_fee=None,
        option_net_cashflow=net_cash,
        occupied_capital=occupied_capital,
        capital_days=capital_days,
        win_eligible=False,
        win=None,
        status=MetricStatus.PARTIAL if missing else MetricStatus.OBSERVED,
        missing=missing,
        cash_missing=tuple(sorted(cash_missing)),
        capital_missing=tuple(sorted(capital_missing)),
        win_missing=tuple(sorted(win_missing)),
    )


def _leg_type(lot: PositionLot) -> str:
    prefix = "sell" if lot.contract_key.position_side == "short" else "buy"
    return f"{prefix}_{lot.contract_key.option_type}"


def _opening_cash(lot: PositionLot, contracts: int) -> Decimal:
    amount = quantize_money(
        to_decimal(lot.premium_open, field_name="premium")
        * to_decimal(lot.multiplier, field_name="multiplier")
        * Decimal(contracts)
    )
    return amount if lot.contract_key.position_side == "short" else -amount


def _actual_fee(fee: FeeFact) -> Decimal | None:
    return fee.amount if fee.basis == FeeBasis.ACTUAL else None


def _fee_missing(fee: FeeFact, *, exercise: bool = False) -> tuple[str, ...]:
    if fee.basis == FeeBasis.ACTUAL:
        return ()
    if fee.basis == FeeBasis.ESTIMATED:
        return ("fee_estimated",)
    return (("exercise_fee_missing" if exercise else "fee_missing"),)


def _terminal_kind(allocation: OptionEconomicAllocation) -> str:
    raw = str(allocation.close_type or "").strip().lower()
    if raw in _EXPIRY_CLOSE_TYPES:
        return "expiry"
    if raw in {"assignment", "exercise", "buy_to_close", "sell_to_close"}:
        return raw
    if raw == "close":
        return "buy_to_close" if allocation.position_side == "short" else "sell_to_close"
    return "conflicting"


def _terminal_win(
    leg_type: str,
    *,
    terminal_kind: str,
    net_cash: Decimal | None,
    cash_missing: set[str],
) -> tuple[bool, bool | None, set[str]]:
    if leg_type.startswith("sell_"):
        if terminal_kind == "expiry":
            return True, True, set()
        if terminal_kind == "assignment":
            return True, False, set()
        if terminal_kind == "buy_to_close":
            if net_cash is None:
                return False, None, set(cash_missing)
            return True, net_cash > 0, set()
        return False, None, {"terminal_evidence_conflict"}
    if terminal_kind == "expiry":
        return True, False, set()
    if terminal_kind == "sell_to_close":
        if net_cash is None:
            return False, None, set(cash_missing)
        return True, net_cash > 0, set()
    if terminal_kind == "exercise":
        return False, None, set()
    return False, None, {"terminal_evidence_conflict"}


def _capital(
    lot: PositionLot,
    *,
    contracts: int,
    opened_at_ms: int,
    end_at_ms: int,
    unresolved_reason: str | None,
) -> tuple[Decimal | None, Decimal | None, set[str]]:
    if contracts <= 0 or end_at_ms < opened_at_ms:
        return None, None, {"capital_identity_missing"}
    multiplier = to_decimal(lot.multiplier, field_name="multiplier")
    if lot.contract_key.position_side == "short":
        basis = to_decimal(lot.contract_key.strike, field_name="strike") * multiplier
    else:
        basis = to_decimal(lot.premium_open, field_name="premium") * multiplier
    occupied = quantize_money(basis * Decimal(contracts))
    if occupied <= 0:
        return occupied, None, {"capital_non_positive"}
    if unresolved_reason:
        return occupied, None, {unresolved_reason}
    capital_days = (occupied * Decimal(end_at_ms - opened_at_ms) / MILLISECONDS_PER_DAY).quantize(CAPITAL_DAYS_QUANTUM)
    return occupied, capital_days, set()


def _expiration_end_ms(expiration_ymd: str) -> int:
    expiration = date.fromisoformat(expiration_ymd)
    return int(
        datetime.combine(
            expiration + timedelta(days=1),
            time.min,
            tzinfo=_REPORTING_TIMEZONE,
        ).timestamp()
        * 1000
    )


def _aggregate_bundle(
    facts: Sequence[WeightedOptionFact],
    *,
    statistic_days: Decimal,
    scoped_missing: Iterable[str] = (),
) -> dict[str, Any]:
    currencies = sorted({fact.currency for fact in facts})
    cashflow = {"by_currency": {currency: _cashflow_for_currency(facts, currency=currency) for currency in currencies}}
    short_win = _win_rate(facts, side="sell")
    long_win = _win_rate(facts, side="buy")
    returns = {
        "by_currency": {
            currency: _return_for_currency(
                facts,
                currency=currency,
                statistic_days=statistic_days,
                total_cash=cashflow["by_currency"][currency]["total"],
            )
            for currency in currencies
        }
    }
    child_missing = {
        *scoped_missing,
        *short_win["missing"],
        *long_win["missing"],
    }
    for components in cashflow["by_currency"].values():
        child_missing.update(reason for component in components.values() for reason in component["missing"])
    for item in returns["by_currency"].values():
        child_missing.update(item["missing"])
    partial = (
        bool(child_missing)
        or any(
            component["status"] == MetricStatus.PARTIAL
            for components in cashflow["by_currency"].values()
            for component in components.values()
        )
        or short_win["status"] == MetricStatus.PARTIAL
        or long_win["status"] == MetricStatus.PARTIAL
        or any(item["status"] == MetricStatus.PARTIAL for item in returns["by_currency"].values())
    )
    return {
        "option_net_cashflow": cashflow,
        "sell_option_win_rate": short_win,
        "buy_option_win_rate": long_win,
        "option_return": returns,
        "status": MetricStatus.PARTIAL if partial else MetricStatus.OBSERVED,
        "missing": tuple(sorted(child_missing)),
    }


def _cashflow_for_currency(
    facts: Sequence[WeightedOptionFact],
    *,
    currency: str,
) -> dict[str, Any]:
    selected = [fact for fact in facts if fact.currency == currency]
    unresolved = [fact for fact in selected if fact.state == "unresolved_after_expiry"]
    return {
        "total": _cash_component(selected),
        "open": _cash_component(
            [fact for fact in selected if fact.state == "open"],
            split_unknown=unresolved,
        ),
        "terminated": _cash_component(
            [fact for fact in selected if fact.state == "terminated"],
            split_unknown=unresolved,
        ),
    }


def _cny_cashflow_total(
    facts: Sequence[WeightedOptionFact],
    *,
    events: Sequence[TradeEvent],
) -> dict[str, Any]:
    missing = {reason for fact in facts for reason in fact.cash_missing}
    events_by_id = {event.event_id: event for event in events}
    amount = Decimal(0)
    component_keys: set[tuple[str, str]] = set()
    for fact in facts:
        if fact.state == "failed":
            continue
        components = (
            (fact.open_event_id, "option_trade_cash_gross", fact.opening_option_cash),
            (
                fact.open_event_id,
                "option_fee_cash",
                -fact.opening_actual_fee if fact.opening_actual_fee is not None else None,
            ),
            (fact.terminal_event_id, "option_trade_cash_gross", fact.terminal_option_cash),
            (
                fact.terminal_event_id,
                "option_fee_cash",
                -fact.terminal_actual_fee if fact.terminal_actual_fee is not None else None,
            ),
        )
        for event_id, fact_kind, native_amount in components:
            if not event_id or native_amount is None:
                continue
            component_keys.add((event_id, fact_kind))
    for event_id, fact_kind in sorted(component_keys):
        amount_cny, issue = _validated_cny_amount(
            events_by_id.get(event_id),
            fact_kind=fact_kind,
        )
        if amount_cny is None:
            missing.add(issue or "cash_conversion_missing")
            continue
        amount = quantize_money(amount + amount_cny)
    return {
        "currency": "CNY",
        "amount": None if missing else amount,
        "status": MetricStatus.PARTIAL if missing else MetricStatus.OBSERVED,
        "missing": tuple(sorted(missing)),
    }


def _validated_cny_amount(
    event: TradeEvent | None,
    *,
    fact_kind: str,
) -> tuple[Decimal | None, str | None]:
    if event is None:
        return None, "cash_conversion_event_missing"
    cash_fact = next(
        (
            item
            for item in cash_facts_for_trade_event(event)
            if item.fact_kind == fact_kind
        ),
        None,
    )
    if cash_fact is None or cash_fact.amount is None or not cash_fact.currency:
        return None, "option_cash_missing"
    conversion = cash_fact.cash_conversion
    if not isinstance(conversion, Mapping):
        return None, "cash_conversion_missing"
    status = str(conversion.get("status") or "").strip().lower()
    if status != "observed":
        state = "pending" if status == "pending" else "invalid"
        return None, f"cash_conversion_{state}"
    amount_cny, issue = validate_observed_cash_conversion(
        conversion,
        cash_fact_id=cash_fact.fact_id,
        native_amount=cash_fact.amount,
        native_currency=cash_fact.currency,
        effective_at_ms=cash_fact.effective_at_ms,
    )
    if amount_cny is None or issue is not None:
        return None, f"cash_conversion_corrupt:{issue or 'unknown'}"
    return amount_cny, None


def _cash_component(
    facts: Sequence[WeightedOptionFact],
    *,
    split_unknown: Sequence[WeightedOptionFact] = (),
) -> dict[str, Any]:
    missing = {
        *(reason for fact in facts for reason in fact.cash_missing),
        *(reason for fact in split_unknown for reason in fact.cash_missing),
    }
    if split_unknown:
        missing.update(
            reason
            for fact in split_unknown
            for reason in fact.missing
            if reason in {"terminal_evidence_conflict", "terminal_evidence_missing"}
        )
    amount = None
    if not missing:
        amount = quantize_money(sum((fact.option_net_cashflow or Decimal(0) for fact in facts), Decimal(0)))
    return {
        "amount": amount,
        "status": MetricStatus.PARTIAL if missing else MetricStatus.OBSERVED,
        "missing": tuple(sorted(missing)),
    }


def _win_rate(
    facts: Sequence[WeightedOptionFact],
    *,
    side: str,
) -> dict[str, Any]:
    selected = [fact for fact in facts if fact.leg_type.startswith(f"{side}_")]
    missing = {reason for fact in selected for reason in fact.win_missing}
    eligible = sum(fact.contracts for fact in selected if fact.win_eligible)
    winning = sum(fact.contracts for fact in selected if fact.win_eligible and fact.win)
    if missing:
        status = MetricStatus.PARTIAL
        rate = None
    elif eligible == 0:
        status = MetricStatus.NOT_APPLICABLE
        rate = None
    else:
        status = MetricStatus.OBSERVED
        rate = (Decimal(winning) / Decimal(eligible)).quantize(_RATE_QUANTUM)
    return {
        "winning_contracts": winning,
        "eligible_contracts": eligible,
        "rate": rate,
        "status": status,
        "missing": tuple(sorted(missing)),
    }


def _return_for_currency(
    facts: Sequence[WeightedOptionFact],
    *,
    currency: str,
    statistic_days: Decimal,
    total_cash: Mapping[str, Any],
) -> dict[str, Any]:
    selected = [fact for fact in facts if fact.currency == currency]
    missing = {
        *(reason for fact in selected for reason in fact.capital_missing),
        *total_cash["missing"],
    }
    capital_days = None
    average = None
    rate = None
    annualized = None
    if not missing:
        capital_days = sum((fact.capital_days or Decimal(0) for fact in selected), Decimal(0)).quantize(
            CAPITAL_DAYS_QUANTUM
        )
        cash = total_cash["amount"]
        assert cash is not None
        if capital_days <= 0:
            missing.add("capital_non_positive")
        else:
            average = quantize_money(capital_days / statistic_days)
            rate = (cash * statistic_days / capital_days).quantize(_RATE_QUANTUM)
            annualized = (cash * Decimal(365) / capital_days).quantize(_RATE_QUANTUM)
    status = MetricStatus.PARTIAL if missing else MetricStatus.OBSERVED
    return {
        "capital_days": capital_days,
        "average_occupied_capital": average,
        "rate": rate,
        "annualized_rate": annualized,
        "status": status,
        "missing": tuple(sorted(missing)),
    }


def _breakdowns(
    facts: Sequence[WeightedOptionFact],
    *,
    statistic_days: Decimal,
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    local_open = lambda fact: datetime.fromtimestamp(
        fact.opened_at_ms / 1000,
        tz=_REPORTING_TIMEZONE,
    )
    dimensions: tuple[tuple[str, Callable[[WeightedOptionFact], str | None]], ...] = (
        ("opening_years", lambda fact: f"{local_open(fact).year:04d}"),
        ("opening_months", lambda fact: local_open(fact).strftime("%Y-%m")),
        ("accounts", lambda fact: fact.account),
        ("currencies", lambda fact: fact.currency),
        ("leg_types", lambda fact: fact.leg_type),
        ("attribution_strategies", lambda fact: fact.attribution_strategy),
        (
            "parent_universes",
            lambda fact: "csp" if fact.leg_type == "sell_put" else "cc" if fact.leg_type == "sell_call" else None,
        ),
        ("symbols", lambda fact: fact.symbol),
    )
    result: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for name, key_fn in dimensions:
        grouped: dict[str, list[WeightedOptionFact]] = defaultdict(list)
        for fact in facts:
            key = key_fn(fact)
            if key is not None:
                grouped[key].append(fact)
        result[name] = tuple(
            {
                "key": key,
                **_aggregate_bundle(
                    grouped[key],
                    statistic_days=statistic_days,
                    scoped_missing=(
                        ("strategy_attribution_conflict",)
                        if name == "attribution_strategies"
                        and any("strategy_attribution_conflict" in fact.missing for fact in grouped[key])
                        else ()
                    ),
                ),
            }
            for key in sorted(grouped)
        )
    return result


def _fact_sort_key(fact: WeightedOptionFact) -> tuple[Any, ...]:
    return (
        fact.opened_at_ms,
        fact.open_lot_id,
        fact.terminal_at_ms if fact.terminal_at_ms is not None else 2**63,
        fact.fact_id,
    )


__all__ = [
    "PerformanceReduction",
    "PerformanceScopeError",
    "WeightedOptionFact",
    "reduce_option_performance",
]
