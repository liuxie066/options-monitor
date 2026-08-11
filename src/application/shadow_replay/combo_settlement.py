from __future__ import annotations

"""Counterfactual settlement for research-only Combo Yield pair identities."""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from domain.domain.fee_calc import calc_futu_stock_fee
from src.application.shadow_replay.combo_variants import (
    COMBO_PAIR_OUTCOME_SCHEMA_VERSION,
)
from src.application.shadow_replay.common import (
    dataset_dir_from_arg,
    dataset_write_lock,
    first_float,
    read_jsonl,
    refresh_dataset_manifest,
    safety_payload,
    text,
    utc_now,
    validate_dataset_integrity,
    write_jsonl,
)


def settle_combo_pair_outcomes(
    *,
    decisions: Iterable[dict[str, Any]],
    marks: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Settle selected baseline/variant pairs, failing closed on evidence gaps."""

    marks_by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in marks:
        pair_id = text(source.get("shadow_combo_pair_id"))
        if pair_id:
            marks_by_pair[pair_id].append(dict(source))

    outcomes: list[dict[str, Any]] = []
    for decision in decisions:
        pair_id = text(decision.get("shadow_combo_pair_id"))
        selected_variants = [
            text(item.get("variant_id"))
            for item in decision.get("variant_decisions") or []
            if isinstance(item, dict) and bool(item.get("selected"))
        ]
        selectors = (["baseline"] if bool(decision.get("baseline_selected")) else []) + selected_variants
        for selector in selectors:
            outcomes.append(
                _settle_one(
                    dict(decision),
                    marks_by_pair.get(pair_id, []),
                    selector=selector,
                )
            )
    return sorted(
        outcomes,
        key=lambda row: (
            text(row.get("shadow_combo_pair_id")),
            text(row.get("selector")),
        ),
    )


def settle_combo_pair_dataset(
    *,
    dataset: str | Path,
    write: bool = False,
    replace: bool = False,
    _lock: bool = True,
) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    if write and _lock:
        with dataset_write_lock(dataset_dir):
            validate_dataset_integrity(dataset_dir)
            result = settle_combo_pair_dataset(
                dataset=dataset,
                write=True,
                replace=replace,
                _lock=False,
            )
            result["dataset_integrity"] = refresh_dataset_manifest(dataset_dir)["integrity"]
            return result
    decisions = read_jsonl(dataset_dir / "combo_pair_decisions.jsonl")
    marks = read_jsonl(dataset_dir / "combo_pair_mark_paths.jsonl")
    existing = [] if replace else read_jsonl(dataset_dir / "combo_pair_outcomes.jsonl")
    generated = settle_combo_pair_outcomes(decisions=decisions, marks=marks)
    generated_keys = {
        (text(row.get("shadow_combo_pair_id")), text(row.get("selector")))
        for row in generated
    }
    merged = [
        row
        for row in existing
        if (text(row.get("shadow_combo_pair_id")), text(row.get("selector"))) not in generated_keys
    ] + generated
    if write:
        write_jsonl(dataset_dir / "combo_pair_outcomes.jsonl", merged)
        from src.application.shadow_replay.combo_variants import (
            refresh_combo_pair_facet_manifest,
        )

        refresh_combo_pair_facet_manifest(dataset_dir)
    return {
        "schema_version": "shadow_combo_pair_settlement.v1",
        "dataset_dir": str(dataset_dir),
        "generated_at_utc": utc_now(),
        "summary": {
            "decision_count": len(decisions),
            "mark_count": len(marks),
            "generated_outcome_count": len(generated),
            "complete_outcome_count": sum(
                1 for row in generated if row.get("evidence_status") == "complete"
            ),
            "unavailable_outcome_count": sum(
                1 for row in generated if row.get("evidence_status") != "complete"
            ),
            "written": bool(write),
            "replace": bool(replace),
        },
        "generated_outcomes": generated,
        "variant_scorecards": build_combo_variant_scorecards(generated),
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }


def build_combo_variant_scorecards(
    outcomes: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare selectors only on their intersection of complete decision instances."""

    rows = [dict(row) for row in outcomes]
    complete_by_selector: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    all_by_selector: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        selector = text(row.get("selector"))
        instance = _decision_instance(row)
        if not selector or not instance:
            continue
        all_by_selector[selector].add(instance)
        if row.get("evidence_status") == "complete":
            complete_by_selector[selector][instance] = row
    selectors = sorted(all_by_selector)
    common = (
        set.intersection(*(set(complete_by_selector[name]) for name in selectors))
        if selectors
        else set()
    )
    cards: list[dict[str, Any]] = []
    for selector in selectors:
        matched = [complete_by_selector[selector][key] for key in sorted(common)]
        pnls = [first_float(row, "full_shadow_group_pnl") for row in matched]
        pnls = [value for value in pnls if value is not None]
        drawdowns = [first_float(row, "maximum_observed_drawdown") for row in matched]
        drawdowns = [value for value in drawdowns if value is not None]
        increments = [first_float(row, "combo_incremental_vs_put_only") for row in matched]
        increments = [value for value in increments if value is not None]
        call_pnls = [first_float(row, "call_pnl") for row in matched]
        call_pnls = [value for value in call_pnls if value is not None]
        spreads = [first_float(row, "entry_max_leg_spread_ratio") for row in matched]
        spreads = [value for value in spreads if value is not None]
        cards.append(
            {
                "selector": selector,
                "total_instance_count": len(all_by_selector[selector]),
                "complete_instance_count": len(complete_by_selector[selector]),
                "identical_complete_instance_count": len(common),
                "coverage_rate": (
                    len(complete_by_selector[selector]) / len(all_by_selector[selector])
                    if all_by_selector[selector]
                    else 0.0
                ),
                "missing_evidence_rate": (
                    1.0 - len(complete_by_selector[selector]) / len(all_by_selector[selector])
                    if all_by_selector[selector]
                    else None
                ),
                "matched_full_shadow_group_pnl_total": sum(pnls) if pnls else None,
                "matched_full_shadow_group_pnl_mean": (
                    sum(pnls) / len(pnls) if pnls else None
                ),
                "matched_maximum_observed_drawdown_worst": (
                    min(drawdowns) if drawdowns else None
                ),
                "matched_combo_incremental_vs_put_only_mean": (
                    sum(increments) / len(increments) if increments else None
                ),
                "matched_call_loss_rate": (
                    sum(1 for value in call_pnls if value < 0) / len(call_pnls)
                    if call_pnls
                    else None
                ),
                "matched_entry_max_leg_spread_ratio_mean": (
                    sum(spreads) / len(spreads) if spreads else None
                ),
            }
        )
    return cards


def _settle_one(
    decision: dict[str, Any],
    marks: list[dict[str, Any]],
    *,
    selector: str,
) -> dict[str, Any]:
    base = {
        "schema_version": COMBO_PAIR_OUTCOME_SCHEMA_VERSION,
        "shadow_combo_pair_id": decision.get("shadow_combo_pair_id"),
        "dataset_id": decision.get("dataset_id"),
        "account": decision.get("account"),
        "symbol": decision.get("symbol"),
        "structure_mode": decision.get("structure_mode"),
        "selector": selector,
        "entry_observed_at_utc": decision.get("entry_observed_at_utc"),
        "settlement_model": "expiry_assignment_model",
        "call_expiration_model": "cash_equivalent_intrinsic",
        "assigned_stock_close_model": "notional_liquidation_at_call_settlement_spot",
        "entry_put_spread_ratio": first_float(decision, "put_spread_ratio"),
        "entry_call_spread_ratio": first_float(decision, "call_spread_ratio"),
        "entry_max_leg_spread_ratio": max(
            [
                value
                for value in (
                    first_float(decision, "put_spread_ratio"),
                    first_float(decision, "call_spread_ratio"),
                )
                if value is not None
            ],
            default=None,
        ),
    }
    missing = _base_missing(decision)
    put_spot_mark = _authoritative_spot_mark(
        marks,
        horizon="put_expiry",
        expiration=decision.get("put_expiration"),
    )
    call_spot_mark = _authoritative_spot_mark(
        marks,
        horizon="put_expiry",
        expiration=decision.get("call_expiration"),
    )
    if put_spot_mark is None:
        missing.append("authoritative_put_expiry_spot_missing")
    if call_spot_mark is None:
        missing.append("authoritative_call_expiry_spot_missing")
    put_expiry_at = _mark_time(put_spot_mark) if put_spot_mark is not None else None
    call_expiry_at = _mark_time(call_spot_mark) if call_spot_mark is not None else None
    entry_at = _strict_utc(decision.get("entry_observed_at_utc"))
    if entry_at is None:
        missing.append("entry_observed_at_utc_missing")
    if put_expiry_at is None:
        missing.append("put_expiration_missing")
    if call_expiry_at is None:
        missing.append("call_expiration_missing")
    if missing:
        return _unavailable(base, missing)

    assert put_spot_mark is not None
    assert call_spot_mark is not None
    assert entry_at is not None and put_expiry_at is not None and call_expiry_at is not None
    multiplier = float(first_float(decision, "multiplier") or 0.0)
    put_strike = float(first_float(decision, "put_strike") or 0.0)
    call_strike = float(first_float(decision, "call_strike") or 0.0)
    put_credit = float(first_float(decision, "put_net_credit") or 0.0)
    call_cost = float(first_float(decision, "call_total_cost") or 0.0)
    put_spot = float(first_float(put_spot_mark, "spot") or 0.0)
    call_spot = float(first_float(call_spot_mark, "spot") or 0.0)
    assigned = put_spot < put_strike
    liquidation_fee = first_float(decision, "stock_liquidation_fee")
    if assigned and liquidation_fee is None:
        try:
            liquidation_fee = calc_futu_stock_fee(
                text(decision.get("currency")),
                call_spot,
                shares=int(multiplier),
                is_sell=True,
            )
        except ValueError:
            return _unavailable(base, ["stock_liquidation_fee_unavailable"])
    liquidation_fee = float(liquidation_fee or 0.0)

    put_intrinsic = max(put_strike - put_spot, 0.0) * multiplier
    call_intrinsic = max(call_spot - call_strike, 0.0) * multiplier
    put_pnl = put_credit - put_intrinsic
    call_pnl = call_intrinsic - call_cost
    stock_pnl = (
        (call_spot - put_spot) * multiplier - liquidation_fee
        if assigned
        else 0.0
    )
    full_group_pnl = put_pnl + call_pnl + stock_pnl
    funding_horizon_pnl = put_pnl + call_intrinsic - call_cost

    put_days = _days(entry_at, put_expiry_at)
    call_days = _days(entry_at, call_expiry_at)
    stock_days = _days(put_expiry_at, call_expiry_at) if assigned else 0.0
    if min(put_days, call_days, stock_days) < 0:
        return _unavailable(base, ["invalid_capital_interval"])
    capital = {
        "currency": decision.get("currency"),
        "funding_put_capital_days": put_strike * multiplier * put_days,
        "participation_call_capital_days": call_cost * call_days,
        "assigned_stock_capital_days": put_strike * multiplier * stock_days,
    }
    capital["group_capital_days"] = sum(
        float(capital[key])
        for key in (
            "funding_put_capital_days",
            "participation_call_capital_days",
            "assigned_stock_capital_days",
        )
    )
    drawdown, drawdown_quality = _maximum_observed_drawdown(decision, marks)
    stresses, stress_complete = _early_assignment_stresses(
        decision,
        marks,
        final_spot=call_spot,
        call_intrinsic=call_intrinsic,
        liquidation_fee=liquidation_fee,
        call_expiry_at=call_expiry_at,
    )
    return {
        **base,
        "evidence_status": "complete",
        "unavailable_reasons": [],
        "put_expiry_spot": put_spot,
        "call_expiry_spot": call_spot,
        "put_assignment_state": "assigned_stock" if assigned else "expired_otm",
        "post_put_expiry_state": "terminal",
        "put_pnl": put_pnl,
        "call_pnl": call_pnl,
        "assigned_stock_continuation_pnl": stock_pnl,
        "stock_liquidation_fee": liquidation_fee,
        "stock_liquidation_fee_model": (
            "decision_explicit"
            if decision.get("stock_liquidation_fee") is not None
            else "canonical_futu_estimate"
        ),
        "call_cost_funded_by_put": call_cost,
        "funding_horizon_pnl": funding_horizon_pnl,
        "participation_horizon_pnl": call_pnl,
        "full_shadow_group_pnl": full_group_pnl,
        "put_only_baseline_pnl": put_pnl,
        "combo_incremental_vs_put_only": full_group_pnl - put_pnl,
        "capital_days": capital,
        "maximum_observed_drawdown": drawdown,
        "drawdown_evidence_status": drawdown_quality,
        "early_assignment_stress_envelope": stresses,
        "early_assignment_stress_status": (
            "complete" if stress_complete else "incomplete"
        ),
        "safety": safety_payload(writes_local_dataset=False),
    }


def _base_missing(decision: dict[str, Any]) -> list[str]:
    missing = []
    for field in (
        "multiplier",
        "put_strike",
        "call_strike",
        "put_net_credit",
        "call_total_cost",
        "currency",
    ):
        value = decision.get(field)
        if value in (None, ""):
            missing.append(f"{field}_missing")
    return missing


def _authoritative_spot_mark(
    marks: list[dict[str, Any]],
    *,
    horizon: str,
    expiration: Any,
) -> dict[str, Any] | None:
    expected_date = text(expiration)[:10]
    candidates = [
        row
        for row in marks
        if text(row.get("horizon")).lower() == horizon
        and first_float(row, "spot") is not None
        and bool(row.get("settlement_authority"))
        and text(row.get("mark_quality")).lower() in {"usable", "settlement"}
        and _mark_time(row).date().isoformat() == expected_date
    ]
    return min(candidates, key=_mark_time) if candidates else None


def _maximum_observed_drawdown(
    decision: dict[str, Any],
    marks: list[dict[str, Any]],
) -> tuple[float | None, str]:
    by_time: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in marks:
        if text(row.get("horizon")).lower() != "intermediate":
            continue
        key = text(row.get("marked_at_utc") or row.get("observed_at_utc"))
        role = text(row.get("leg_role")).lower()
        if key and role:
            by_time[key][role] = row
    values: list[tuple[str, float]] = []
    multiplier = first_float(decision, "multiplier")
    put_credit = first_float(decision, "put_net_credit")
    call_cost = first_float(decision, "call_total_cost")
    if multiplier is None or put_credit is None or call_cost is None:
        return None, "incomplete"
    for legs in by_time.values():
        put_ask = first_float(legs.get("funding_put") or {}, "ask")
        call_bid = first_float(legs.get("participation_call") or {}, "bid")
        put_fee = first_float(legs.get("funding_put") or {}, "future_close_fee")
        call_fee = first_float(legs.get("participation_call") or {}, "future_close_fee")
        if put_ask is None or call_bid is None or put_fee is None or call_fee is None:
            continue
        marked_at = text(
            (legs.get("funding_put") or {}).get("marked_at_utc")
            or (legs.get("funding_put") or {}).get("observed_at_utc")
        )
        values.append(
            (
                marked_at,
                put_credit
                - put_ask * multiplier
                - put_fee
                + call_bid * multiplier
                - call_fee
                - call_cost,
            )
        )
    if not values:
        return None, "incomplete"
    peak = 0.0
    worst = 0.0
    for _marked_at, value in sorted(values):
        worst = min(worst, value - peak)
        peak = max(peak, value)
    return worst, "complete"


def _early_assignment_stresses(
    decision: dict[str, Any],
    marks: list[dict[str, Any]],
    *,
    final_spot: float,
    call_intrinsic: float,
    liquidation_fee: float,
    call_expiry_at: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    strike = float(first_float(decision, "put_strike") or 0.0)
    multiplier = float(first_float(decision, "multiplier") or 0.0)
    put_credit = float(first_float(decision, "put_net_credit") or 0.0)
    call_cost = float(first_float(decision, "call_total_cost") or 0.0)
    out: list[dict[str, Any]] = []
    observed_intermediate = False
    for row in marks:
        if text(row.get("horizon")).lower() != "intermediate":
            continue
        if text(row.get("leg_role")).lower() != "underlying":
            continue
        observed_intermediate = True
        spot = first_float(row, "spot")
        assigned_at = _strict_utc(row.get("marked_at_utc") or row.get("observed_at_utc"))
        if spot is None or assigned_at is None or spot >= strike:
            continue
        stock_days = _days(assigned_at, call_expiry_at)
        if stock_days < 0:
            continue
        out.append(
            {
                "scenario": "early_assignment_stress",
                "probability_weighted": False,
                "assigned_at_utc": assigned_at.isoformat().replace("+00:00", "Z"),
                "assignment_spot": spot,
                "full_shadow_group_pnl": (
                    put_credit
                    + (final_spot - strike) * multiplier
                    - liquidation_fee
                    + call_intrinsic
                    - call_cost
                ),
                "assigned_stock_capital_days": strike * multiplier * stock_days,
            }
        )
    return out, observed_intermediate


def _unavailable(base: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        **base,
        "evidence_status": "unavailable",
        "unavailable_reasons": list(dict.fromkeys(reasons)),
        "funding_horizon_pnl": None,
        "participation_horizon_pnl": None,
        "full_shadow_group_pnl": None,
        "put_only_baseline_pnl": None,
        "capital_days": None,
        "maximum_observed_drawdown": None,
        "early_assignment_stress_status": "unavailable",
        "early_assignment_stress_envelope": [],
        "safety": safety_payload(writes_local_dataset=False),
    }


def _decision_instance(row: dict[str, Any]) -> str:
    return "|".join(
        (
            text(row.get("dataset_id")),
            text(row.get("account")),
            text(row.get("symbol")),
            text(row.get("entry_observed_at_utc")),
        )
    )


def _strict_utc(value: Any) -> datetime | None:
    raw = text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _mark_time(row: dict[str, Any]) -> datetime:
    return _strict_utc(row.get("marked_at_utc") or row.get("observed_at_utc")) or datetime.max.replace(
        tzinfo=timezone.utc
    )


def _days(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400.0


__all__ = [
    "build_combo_variant_scorecards",
    "settle_combo_pair_dataset",
    "settle_combo_pair_outcomes",
]
