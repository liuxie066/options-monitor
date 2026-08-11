from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from domain.domain.ledger.economics import OptionEconomicAllocation, fee_fact_for_event
from domain.domain.ledger.events import TradeEvent, lot_id_for_open_event
from domain.domain.performance.attribution import resolve_event_attribution
from domain.domain.performance.models import CAPITAL_DAYS_QUANTUM, CapitalExposureSegment, FeeBasis, quantize_money, to_decimal
from domain.domain.performance.period import PeriodWindow

_PNL_BASE_KINDS = (
    "realized_gross",
    "realized_net",
    "opening_unrealized_gross",
    "opening_unrealized_net",
    "ending_unrealized_gross",
    "ending_unrealized_net",
)


def build_strategy_attribution(
    *,
    events: Sequence[TradeEvent],
    allocations: Sequence[OptionEconomicAllocation],
    facts: Sequence[Any],
    capital_segments: Sequence[CapitalExposureSegment],
    period: PeriodWindow,
) -> dict[str, Any]:
    groups, topology_issues = _build_topology(events, allocations)
    facts_by_group: dict[str, list[Any]] = defaultdict(list)
    segments_by_group: dict[str, list[CapitalExposureSegment]] = defaultdict(list)
    coverage_issues = set(topology_issues)
    for fact in facts:
        coverage_issues.update(getattr(fact, "attribution_issues", ()) or ())
        attribution = getattr(fact, "attribution", None)
        if attribution is not None:
            facts_by_group[attribution.strategy_group_id].append(fact)
    for segment in capital_segments:
        coverage_issues.update(segment.attribution_issues)
        if segment.attribution is not None:
            segments_by_group[segment.attribution.strategy_group_id].append(segment)

    rows: list[dict[str, Any]] = []
    for group_id in sorted(groups):
        topology = groups[group_id]
        if topology.get("status") != "ready":
            continue
        group_facts = facts_by_group.get(group_id, ())
        group_segments = segments_by_group.get(group_id, ())
        if not group_facts and not group_segments:
            continue
        rows.append(
            _group_report(
                topology=topology,
                facts=group_facts,
                segments=group_segments,
                period=period,
            )
        )
    attributed_group_ids = {row["strategy_group_id"] for row in rows}
    tagged_unknown = sorted(({*facts_by_group, *segments_by_group} - attributed_group_ids) - set(groups))
    coverage_issues.update(f"strategy_group_topology_missing:{item}" for item in tagged_unknown)
    conservation = _conservation(facts=facts, groups=rows)
    status = "partial" if coverage_issues or any(row["quality"]["status"] == "partial" for row in rows) else "observed"
    return {
        "schema_version": "option_strategy_attribution.v1",
        "groups": rows,
        "coverage": {
            "status": status,
            "group_count": len(rows),
            "issues": sorted(coverage_issues),
        },
        "conservation": conservation,
    }


def _build_topology(
    events: Sequence[TradeEvent],
    allocations: Sequence[OptionEconomicAllocation],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    groups: dict[str, dict[str, Any]] = {}
    issues: set[str] = set()
    for event in events:
        if event.event_type != "open":
            continue
        lot_id = lot_id_for_open_event(event)
        resolved = resolve_event_attribution(event, lifecycle_source_id=lot_id)
        issues.update(resolved.issues)
        attribution = resolved.attribution
        if attribution is None:
            continue
        bucket = groups.setdefault(
            attribution.strategy_group_id,
            {
                "strategy_group_id": attribution.strategy_group_id,
                "strategy": attribution.strategy,
                "expiry_structure": attribution.expiry_structure,
                "funding_puts": [],
                "participation_calls": [],
                "status": "pending",
                "issues": [],
            },
        )
        if bucket["expiry_structure"] is None:
            bucket["expiry_structure"] = attribution.expiry_structure
        elif attribution.expiry_structure and bucket["expiry_structure"] != attribution.expiry_structure:
            bucket["issues"].append("expiry_structure_conflict")
        entry = {"event": event, "lot_id": lot_id, "attribution": attribution}
        if attribution.leg_role == "funding_put":
            bucket["funding_puts"].append(entry)
        elif attribution.leg_role == "participation_call":
            bucket["participation_calls"].append(entry)

    allocations_by_lot: dict[str, list[OptionEconomicAllocation]] = defaultdict(list)
    for allocation in allocations:
        allocations_by_lot[allocation.target_lot_id].append(allocation)
    for group_id, bucket in groups.items():
        if len(bucket["funding_puts"]) != 1:
            bucket["issues"].append(f"funding_put_count:{len(bucket['funding_puts'])}")
        if len(bucket["participation_calls"]) != 1:
            bucket["issues"].append(f"participation_call_count:{len(bucket['participation_calls'])}")
        if bucket["issues"]:
            bucket["status"] = "partial"
            issues.update(f"strategy_group_invalid:{group_id}:{item}" for item in bucket["issues"])
            continue
        put = bucket["funding_puts"][0]
        call = bucket["participation_calls"][0]
        put_event = put["event"]
        call_event = call["event"]
        if put_event.contract_key.account != call_event.contract_key.account:
            bucket["issues"].append("account_mismatch")
        if put_event.contract_key.underlying_symbol != call_event.contract_key.underlying_symbol:
            bucket["issues"].append("symbol_mismatch")
        if str(put_event.currency).upper() != str(call_event.currency).upper():
            bucket["issues"].append("currency_mismatch")
        if Decimal(str(put_event.multiplier)) != Decimal(str(call_event.multiplier)):
            bucket["issues"].append("multiplier_mismatch")
        if put_event.contract_key.option_type != "put" or put_event.contract_key.position_side != "short":
            bucket["issues"].append("funding_put_contract_invalid")
        if call_event.contract_key.option_type != "call" or call_event.contract_key.position_side != "long":
            bucket["issues"].append("participation_call_contract_invalid")
        structure = str(bucket.get("expiry_structure") or "").strip().lower()
        if structure in ("", "same_expiry") and (
            put_event.contract_key.expiration_ymd != call_event.contract_key.expiration_ymd
        ):
            bucket["issues"].append("same_expiry_mismatch")
        if structure and structure != "same_expiry":
            bucket["issues"].append("unsupported_expiry_structure")
        if bucket["issues"]:
            bucket["status"] = "partial"
            issues.update(f"strategy_group_invalid:{group_id}:{item}" for item in bucket["issues"])
            continue
        put["allocations"] = sorted(
            allocations_by_lot.get(put["lot_id"], ()),
            key=lambda item: (item.closed_at_ms, item.allocation_id),
        )
        call["allocations"] = sorted(
            allocations_by_lot.get(call["lot_id"], ()),
            key=lambda item: (item.closed_at_ms, item.allocation_id),
        )
        put["closed_at_ms"] = _full_close_at(put_event.contracts, put["allocations"])
        call["closed_at_ms"] = _full_close_at(call_event.contracts, call["allocations"])
        bucket["status"] = "ready"
    return groups, issues


def _full_close_at(opened_contracts: int, allocations: Sequence[OptionEconomicAllocation]) -> int | None:
    remaining = int(opened_contracts)
    for allocation in allocations:
        remaining -= int(allocation.contracts)
        if remaining == 0:
            return int(allocation.closed_at_ms)
        if remaining < 0:
            return None
    return None


def _group_report(
    *,
    topology: Mapping[str, Any],
    facts: Sequence[Any],
    segments: Sequence[CapitalExposureSegment],
    period: PeriodWindow,
) -> dict[str, Any]:
    put = topology["funding_puts"][0]
    call = topology["participation_calls"][0]
    put_facts = [item for item in facts if getattr(item.attribution, "leg_role", "") == "funding_put"]
    call_facts = [item for item in facts if getattr(item.attribution, "leg_role", "") == "participation_call"]
    put_segments = [item for item in segments if getattr(item.attribution, "leg_role", "") == "funding_put"]
    call_segments = [item for item in segments if getattr(item.attribution, "leg_role", "") == "participation_call"]
    stock_facts = [item for item in facts if getattr(item.attribution, "leg_role", "") == "assigned_stock"]
    stock_segments = [item for item in segments if getattr(item.attribution, "leg_role", "") == "assigned_stock"]
    put_pnl = _pnl_report(put_facts)
    call_pnl = _pnl_report(call_facts)
    group_pnl = _pnl_report(facts)
    put_capital = _capital_report(put_segments, period=period, pnl=put_pnl)
    call_capital = _capital_report(call_segments, period=period, pnl=call_pnl)
    group_capital = _capital_report(segments, period=period, pnl=group_pnl)
    tail = _residual_tail(call=call, put=put, call_segments=call_segments, call_pnl=call_pnl, period=period)
    issues = sorted(
        {
            *(item for fact in facts for item in (getattr(fact, "attribution_issues", ()) or ())),
            *(item for segment in segments for item in segment.attribution_issues),
        }
    )
    metric_issues = _group_metric_issues(
        group_pnl=group_pnl,
        put_pnl=put_pnl,
        call_pnl=call_pnl,
        group_capital=group_capital,
    )
    quality_issues = sorted({*issues, *metric_issues})
    return {
        "strategy_group_id": topology["strategy_group_id"],
        "strategy": topology["strategy"],
        "expiry_structure": topology.get("expiry_structure"),
        "funding_cycles": [
            {
                "funding_cycle_id": put["attribution"].lifecycle_id,
                "open_event_id": put["event"].event_id,
                "lot_id": put["lot_id"],
                "opened_at_ms": put["event"].event_time_ms,
                "closed_at_ms": put.get("closed_at_ms"),
                "pnl": put_pnl,
                "capital": put_capital,
            }
        ],
        "participation_lifecycles": [
            {
                "participation_lifecycle_id": call["attribution"].lifecycle_id,
                "open_event_id": call["event"].event_id,
                "lot_id": call["lot_id"],
                "opened_at_ms": call["event"].event_time_ms,
                "closed_at_ms": call.get("closed_at_ms"),
                "pnl": call_pnl,
                "capital": call_capital,
            }
        ],
        "residual_tails": [] if tail is None else [tail],
        "assigned_stock_lifecycles": _assigned_stock_lifecycles(
            facts=stock_facts,
            segments=stock_segments,
            period=period,
        ),
        "funding": _funding_snapshot(put["event"], call["event"]),
        "pnl": group_pnl,
        "capital": group_capital,
        "quality": {"status": "partial" if quality_issues else "observed", "issues": quality_issues},
    }


def _group_metric_issues(
    *,
    group_pnl: Mapping[str, Any],
    put_pnl: Mapping[str, Any],
    call_pnl: Mapping[str, Any],
    group_capital: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    for label, report in (("group", group_pnl), ("funding_put", put_pnl), ("participation_call", call_pnl)):
        for metric in ("period_total_gross", "period_total_net"):
            status = str((report.get(metric) or {}).get("status") or "not_observed")
            if status != "observed":
                issues.append(f"{label}_{metric}_{status}")
    efficiency_status = str(
        (group_capital.get("period_total_net_annualized_efficiency") or {}).get("status")
        or "not_observed"
    )
    if efficiency_status != "observed":
        issues.append(f"group_efficiency_{efficiency_status}")
    return issues


def _assigned_stock_lifecycles(
    *,
    facts: Sequence[Any],
    segments: Sequence[CapitalExposureSegment],
    period: PeriodWindow,
) -> list[dict[str, Any]]:
    lifecycle_ids = sorted(
        {
            attribution.lifecycle_id
            for item in (*facts, *segments)
            if (attribution := getattr(item, "attribution", None)) is not None
            and attribution.leg_role == "assigned_stock"
        }
    )
    rows: list[dict[str, Any]] = []
    for lifecycle_id in lifecycle_ids:
        lifecycle_facts = [item for item in facts if getattr(item.attribution, "lifecycle_id", "") == lifecycle_id]
        lifecycle_segments = [item for item in segments if getattr(item.attribution, "lifecycle_id", "") == lifecycle_id]
        pnl = _pnl_report(lifecycle_facts)
        rows.append(
            {
                "assigned_stock_lifecycle_id": lifecycle_id,
                "pnl": pnl,
                "capital": _capital_report(lifecycle_segments, period=period, pnl=pnl),
            }
        )
    return rows


def _funding_snapshot(put: TradeEvent, call: TradeEvent) -> dict[str, Any]:
    if str(put.currency).upper() != str(call.currency).upper():
        return {"scope": "group_lifetime_opening_snapshot", "status": "partial", "reason": "currency_mismatch"}
    put_credit = _open_gross(put)
    call_debit = _open_gross(call)
    put_fee = fee_fact_for_event(put)
    call_fee = fee_fact_for_event(call)
    funded = min(put_credit, call_debit)
    return {
        "scope": "group_lifetime_opening_snapshot",
        "status": "observed",
        "currency": str(put.currency).upper(),
        "source_event_ids": [put.event_id, call.event_id],
        "put_open_credit_gross": float(put_credit),
        "call_open_debit_gross": float(call_debit),
        "call_cost_funded_by_put": float(funded),
        "funding_surplus": float(quantize_money(put_credit - call_debit)),
        "funding_ratio": float(put_credit / call_debit) if call_debit > 0 else None,
        "fee_quality": {
            "put_open": put_fee.basis.value,
            "call_open": call_fee.basis.value,
            "net_funding_observed": put_fee.basis == FeeBasis.ACTUAL and call_fee.basis == FeeBasis.ACTUAL,
        },
    }


def _open_gross(event: TradeEvent) -> Decimal:
    return quantize_money(
        to_decimal(event.price, field_name="price")
        * to_decimal(event.multiplier, field_name="multiplier")
        * Decimal(int(event.contracts))
    )


def _pnl_report(facts: Sequence[Any]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, Decimal]] = {}
    missing_by_kind: dict[str, list[str]] = {}
    fact_ids_by_kind: dict[str, list[str]] = {}
    for kind in _PNL_BASE_KINDS:
        sums: dict[str, Decimal] = defaultdict(Decimal)
        missing: list[str] = []
        ids: list[str] = []
        for fact in facts:
            if fact.fact_kind != kind:
                continue
            ids.append(fact.fact_id)
            if fact.amount is None or not fact.currency:
                missing.append(fact.fact_id)
            else:
                sums[fact.currency] += to_decimal(fact.amount, field_name=kind)
        by_kind[kind] = dict(sums)
        missing_by_kind[kind] = missing
        fact_ids_by_kind[kind] = ids
    output = {kind: _amount_envelope(by_kind[kind], missing_by_kind[kind], fact_ids_by_kind[kind]) for kind in _PNL_BASE_KINDS}
    output["period_total_gross"] = _combine_pnl(
        realized=by_kind["realized_gross"],
        opening=by_kind["opening_unrealized_gross"],
        ending=by_kind["ending_unrealized_gross"],
        missing=[*missing_by_kind["realized_gross"], *missing_by_kind["opening_unrealized_gross"], *missing_by_kind["ending_unrealized_gross"]],
        fact_ids=[*fact_ids_by_kind["realized_gross"], *fact_ids_by_kind["opening_unrealized_gross"], *fact_ids_by_kind["ending_unrealized_gross"]],
    )
    output["period_total_net"] = _combine_pnl(
        realized=by_kind["realized_net"],
        opening=by_kind["opening_unrealized_net"],
        ending=by_kind["ending_unrealized_net"],
        missing=[*missing_by_kind["realized_net"], *missing_by_kind["opening_unrealized_net"], *missing_by_kind["ending_unrealized_net"]],
        fact_ids=[*fact_ids_by_kind["realized_net"], *fact_ids_by_kind["opening_unrealized_net"], *fact_ids_by_kind["ending_unrealized_net"]],
    )
    return output


def _combine_pnl(*, realized: Mapping[str, Decimal], opening: Mapping[str, Decimal], ending: Mapping[str, Decimal], missing: list[str], fact_ids: list[str]) -> dict[str, Any]:
    currencies = sorted({*realized, *opening, *ending})
    values = {currency: quantize_money(realized.get(currency, Decimal(0)) + ending.get(currency, Decimal(0)) - opening.get(currency, Decimal(0))) for currency in currencies}
    return _amount_envelope(values, missing, fact_ids)


def _amount_envelope(values: Mapping[str, Decimal], missing: Sequence[str], fact_ids: Sequence[str]) -> dict[str, Any]:
    if values and missing:
        status = "partial"
    elif values:
        status = "observed"
    elif missing:
        status = "partial"
    else:
        status = "not_observed"
    return {
        "by_currency": {currency: float(quantize_money(value)) for currency, value in sorted(values.items())},
        "status": status,
        "missing_fact_ids": sorted(set(missing)),
        "fact_ids": sorted(set(fact_ids)),
    }


def _capital_report(segments: Sequence[CapitalExposureSegment], *, period: PeriodWindow, pnl: Mapping[str, Any]) -> dict[str, Any]:
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for segment in segments:
        value = segment.capital_days(
            period_start_at_ms=period.effective_start_at_ms,
            period_end_exclusive_at_ms=period.effective_end_exclusive_at_ms,
        )
        if value > 0:
            sums[segment.currency] += value
    duration_days = Decimal(period.effective_end_exclusive_at_ms - period.effective_start_at_ms) / Decimal(86_400_000)
    rounded = {currency: value.quantize(CAPITAL_DAYS_QUANTUM, rounding=ROUND_HALF_UP) for currency, value in sorted(sums.items())}
    average = {currency: quantize_money(value / duration_days) for currency, value in rounded.items()} if duration_days > 0 else {}
    return {
        "capital_basis": "notional_days_v1",
        "capital_days_by_currency": {currency: float(value) for currency, value in rounded.items()},
        "average_incremental_capital_by_currency": {currency: float(value) for currency, value in average.items()},
        "period_total_net_annualized_efficiency": _efficiency(pnl.get("period_total_net", {}), rounded),
        "segment_source_ids": sorted({item.source_id for item in segments}),
    }


def _efficiency(pnl: Mapping[str, Any], capital_days: Mapping[str, Decimal]) -> dict[str, Any]:
    if pnl.get("status") != "observed":
        return {"by_currency": {}, "status": "partial" if pnl.get("status") == "partial" else "not_observed", "reason": "pnl_scope_unavailable"}
    values: dict[str, float] = {}
    missing: list[str] = []
    pnl_values = pnl.get("by_currency") if isinstance(pnl.get("by_currency"), Mapping) else {}
    for currency, amount in pnl_values.items():
        denominator = capital_days.get(currency, Decimal(0))
        if denominator <= 0:
            missing.append(f"zero_capital_days:{currency}")
            continue
        value = (to_decimal(amount, field_name="pnl") / denominator * Decimal(365)).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        values[currency] = float(value)
    return {"by_currency": values, "status": "partial" if missing else "observed" if values else "not_observed", "missing": missing}


def _residual_tail(*, call: Mapping[str, Any], put: Mapping[str, Any], call_segments: Sequence[CapitalExposureSegment], call_pnl: Mapping[str, Any], period: PeriodWindow) -> dict[str, Any] | None:
    tail_start = put.get("closed_at_ms")
    if not tail_start:
        return None
    tail_end = call.get("closed_at_ms") or period.effective_end_exclusive_at_ms
    if tail_end <= tail_start:
        return None
    tail_segments = [
        CapitalExposureSegment(
            account=item.account,
            broker=item.broker,
            symbol=item.symbol,
            currency=item.currency,
            exposure_kind=item.exposure_kind,
            source_id=item.source_id,
            start_at_ms=max(item.start_at_ms, tail_start),
            end_at_ms=item.end_at_ms,
            notional=item.notional,
            quantity=item.quantity,
            incremental=item.incremental,
            attribution=item.attribution,
            attribution_issues=item.attribution_issues,
        )
        for item in call_segments
        if item.end_at_ms > tail_start
    ]
    isolated = period.effective_start_at_ms >= tail_start
    tail_pnl = call_pnl if isolated else {"period_total_net": {"by_currency": {}, "status": "not_observed", "reason": "transition_mark_required"}}
    quality_issues = ["transition_mark_required"]
    if isolated:
        quality_issues = []
        for metric in ("period_total_gross", "period_total_net"):
            status = str((tail_pnl.get(metric) or {}).get("status") or "not_observed")
            if status != "observed":
                quality_issues.append(f"residual_tail_{metric}_{status}")
    return {
        "residual_tail_id": f"residual_tail:{call['attribution'].strategy_group_id}:{put['allocations'][-1].close_event_id if put['allocations'] else tail_start}",
        "start_at_ms": tail_start,
        "end_at_ms": tail_end,
        "pnl": tail_pnl,
        "capital": _capital_report(tail_segments, period=period, pnl=tail_pnl),
        "quality": {"status": "partial" if quality_issues else "observed", "issues": quality_issues},
    }


def _conservation(*, facts: Sequence[Any], groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attributed = [fact for fact in facts if getattr(fact, "attribution", None) is not None]
    result: dict[str, Any] = {}
    for kind in _PNL_BASE_KINDS:
        source: dict[str, Decimal] = defaultdict(Decimal)
        missing = False
        fact_ids: list[str] = []
        for fact in attributed:
            if fact.fact_kind != kind:
                continue
            fact_ids.append(fact.fact_id)
            if fact.amount is None or not fact.currency:
                missing = True
            else:
                source[fact.currency] += to_decimal(fact.amount, field_name=kind)
        grouped: dict[str, Decimal] = defaultdict(Decimal)
        for group in groups:
            envelope = group["pnl"].get(kind, {})
            for currency, amount in (envelope.get("by_currency") or {}).items():
                grouped[currency] += to_decimal(amount, field_name=kind)
        currencies = sorted({*source, *grouped})
        residual = {currency: quantize_money(source.get(currency, Decimal(0)) - grouped.get(currency, Decimal(0))) for currency in currencies}
        ok = not missing and all(value == 0 for value in residual.values())
        result[kind] = {
            "status": "observed" if ok else "partial" if fact_ids else "not_observed",
            "source_by_currency": {key: float(quantize_money(value)) for key, value in sorted(source.items())},
            "grouped_by_currency": {key: float(quantize_money(value)) for key, value in sorted(grouped.items())},
            "residual_by_currency": {key: float(value) for key, value in residual.items()},
            "fact_ids": sorted(set(fact_ids)),
        }
    for suffix in ("gross", "net"):
        realized = result[f"realized_{suffix}"]
        opening = result[f"opening_unrealized_{suffix}"]
        ending = result[f"ending_unrealized_{suffix}"]
        result[f"period_total_{suffix}"] = _period_total_conservation(
            realized=realized,
            opening=opening,
            ending=ending,
            groups=groups,
            metric=f"period_total_{suffix}",
        )
    return result


def _period_total_conservation(
    *,
    realized: Mapping[str, Any],
    opening: Mapping[str, Any],
    ending: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    metric: str,
) -> dict[str, Any]:
    component_statuses = {str(realized.get("status")), str(opening.get("status")), str(ending.get("status"))}
    source_realized = realized.get("source_by_currency") or {}
    source_opening = opening.get("source_by_currency") or {}
    source_ending = ending.get("source_by_currency") or {}
    currencies = sorted({*source_realized, *source_opening, *source_ending})
    source = {
        currency: quantize_money(
            to_decimal(source_realized.get(currency, 0), field_name=metric)
            + to_decimal(source_ending.get(currency, 0), field_name=metric)
            - to_decimal(source_opening.get(currency, 0), field_name=metric)
        )
        for currency in currencies
    }
    grouped: dict[str, Decimal] = defaultdict(Decimal)
    for group in groups:
        for currency, amount in (group["pnl"].get(metric, {}).get("by_currency") or {}).items():
            grouped[currency] += to_decimal(amount, field_name=metric)
    all_currencies = sorted({*source, *grouped})
    residual = {
        currency: quantize_money(source.get(currency, Decimal(0)) - grouped.get(currency, Decimal(0)))
        for currency in all_currencies
    }
    complete = "partial" not in component_statuses and all(value == 0 for value in residual.values())
    observed = bool(source or grouped)
    return {
        "status": "observed" if complete and observed else "partial" if observed or "partial" in component_statuses else "not_observed",
        "source_by_currency": {key: float(value) for key, value in source.items()},
        "grouped_by_currency": {key: float(quantize_money(value)) for key, value in sorted(grouped.items())},
        "residual_by_currency": {key: float(value) for key, value in residual.items()},
        "component_metrics": [f"realized_{metric.rsplit('_', 1)[-1]}", f"opening_unrealized_{metric.rsplit('_', 1)[-1]}", f"ending_unrealized_{metric.rsplit('_', 1)[-1]}"],
    }


__all__ = ["build_strategy_attribution"]
