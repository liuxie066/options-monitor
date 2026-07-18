from __future__ import annotations

"""Read-only Combo Yield group outcome evaluator."""

from collections import Counter, defaultdict
from datetime import date
from typing import Any

from src.application.shadow_replay.common import (
    abs_first_float,
    first_float,
    instrument_key,
    normal_status,
    safety_payload,
    text,
    utc_now,
)
from src.application.strategy_lab.decisions import strategy_family
from src.application.shadow_replay.settlement import mark_time


COMBO_GROUP_EXPERIMENT_SCHEMA_VERSION = "strategy_lab_combo_yield_group_experiment.v1"

_ACCEPTED_STATUSES = {"accepted", "notified"}
_SAME_EXPIRY_PAIR = "same_expiry_pair"
_STAGGERED_EXPIRY_PAIR = "staggered_expiry_pair"


def run_combo_yield_group_experiment(
    *,
    candidate_snapshots: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]] | None = None,
    outcome_facts: list[dict[str, Any]] | None = None,
    min_sample: int = 30,
) -> dict[str, Any]:
    sample_floor = max(1, int(min_sample))
    combo_rows = [row for row in candidate_snapshots if strategy_family(row) == "combo_yield"]
    groups = _combo_groups(
        combo_rows,
        mark_snapshots=list(mark_snapshots or []),
        outcome_facts=list(outcome_facts or []),
    )
    ready_groups = [group for group in groups if group["ready_for_group_experiment"]]
    evaluable_groups = [
        group for group in ready_groups if (group.get("outcome_evaluation") or {}).get("status") == "evaluable"
    ]
    scorecard = _scorecard(groups=ready_groups, min_sample=sample_floor)
    blocker_counts = Counter()
    for group in groups:
        blocker_counts.update(str(blocker) for blocker in group.get("blockers") or [] if str(blocker))
    status = _status(combo_count=len(combo_rows), ready_count=len(ready_groups), min_sample=sample_floor)
    return {
        "schema_version": COMBO_GROUP_EXPERIMENT_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "summary": {
            "status": status,
            "min_sample": sample_floor,
            "combo_candidate_snapshot_count": len(combo_rows),
            "group_count": len(groups),
            "ready_group_count": len(ready_groups),
            "evaluable_group_count": len(evaluable_groups),
            "sample_floor_met": len(ready_groups) >= sample_floor,
            "outcome_sample_floor_met": len(evaluable_groups) >= sample_floor,
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
        "group_universe": {
            "groups": groups[:50],
            "blockers": dict(blocker_counts.most_common(20)),
        },
        "scorecard": scorecard,
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
    }


def _combo_groups(
    rows: list[dict[str, Any]],
    *,
    mark_snapshots: list[dict[str, Any]],
    outcome_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing: list[dict[str, Any]] = []
    for row in rows:
        group_id = text(row.get("strategy_group_id"))
        if group_id:
            grouped[group_id].append(row)
        else:
            missing.append(row)

    marks_by_key = _rows_by_instrument(mark_snapshots)
    outcomes_by_key = _rows_by_instrument(outcome_facts)
    groups = [
        _group_payload(
            group_id=group_id,
            legs=legs,
            marks_by_key=marks_by_key,
            outcomes_by_key=outcomes_by_key,
        )
        for group_id, legs in sorted(grouped.items())
    ]
    for idx, row in enumerate(missing, start=1):
        groups.append(
            _group_payload(
                group_id=f"missing-group-{idx}:{instrument_key(row) or idx}",
                strategy_group_id=None,
                legs=[row],
                extra_blockers=["combo_yield_group_identity_missing"],
                marks_by_key=marks_by_key,
                outcomes_by_key=outcomes_by_key,
            )
        )
    return groups


def _group_payload(
    *,
    group_id: str,
    strategy_group_id: str | None = None,
    legs: list[dict[str, Any]],
    extra_blockers: list[str] | None = None,
    marks_by_key: dict[str, list[dict[str, Any]]],
    outcomes_by_key: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    blockers = list(extra_blockers or [])
    blockers.extend(_leg_blockers(legs))
    roles = {text(row.get("leg_role")).lower() for row in legs if text(row.get("leg_role"))}
    structure_mode, structure_blockers = _group_structure_mode(legs)
    blockers.extend(structure_blockers)
    blockers.extend(_identity_blockers(legs, structure_mode=structure_mode))
    metrics, missing_metrics = _group_metrics(legs)
    if missing_metrics:
        blockers.append("combo_yield_group_metric_missing")
    group_blockers = _dedupe(blockers)
    status = _group_status(legs)
    outcome_evaluation = _group_outcome_evaluation(
        legs=legs,
        metrics=metrics,
        group_blockers=group_blockers,
        structure_mode=structure_mode,
        marks_by_key=marks_by_key,
        outcomes_by_key=outcomes_by_key,
    )
    return {
        "group_id": group_id,
        "strategy_group_id": group_id if strategy_group_id is None and not extra_blockers else strategy_group_id,
        "decision_status": status,
        "structure_mode": structure_mode,
        "leg_count": len(legs),
        "leg_roles": sorted(roles),
        "candidate_ids": [instrument_key(row) for row in legs if instrument_key(row)],
        "metrics": metrics,
        "outcome_evaluation": outcome_evaluation,
        "missing_metrics": missing_metrics,
        "blockers": group_blockers,
        "ready_for_group_experiment": not group_blockers,
    }


def _rows_by_instrument(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = instrument_key(row)
        if key:
            out[key].append(row)
    return out


def _leg_blockers(legs: list[dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    if len(legs) != 2:
        blockers.append("combo_yield_group_leg_count_invalid")
    funding_puts = [row for row in legs if text(row.get("leg_role")).lower() == "funding_put"]
    participation_calls = [
        row for row in legs if text(row.get("leg_role")).lower() == "participation_call"
    ]
    if len(funding_puts) != 1:
        blockers.append("combo_yield_funding_put_leg_invalid")
    if len(participation_calls) != 1:
        blockers.append("combo_yield_participation_call_leg_invalid")
    if len(funding_puts) == 1:
        put_leg = funding_puts[0]
        if text(put_leg.get("option_type") or put_leg.get("mode")).lower() != "put":
            blockers.append("combo_yield_funding_put_option_type_invalid")
        if text(put_leg.get("side") or put_leg.get("position_side")).lower() != "short":
            blockers.append("combo_yield_funding_put_side_invalid")
    if len(participation_calls) == 1:
        call_leg = participation_calls[0]
        if text(call_leg.get("option_type") or call_leg.get("mode")).lower() != "call":
            blockers.append("combo_yield_participation_call_option_type_invalid")
        if text(call_leg.get("side") or call_leg.get("position_side")).lower() != "long":
            blockers.append("combo_yield_participation_call_side_invalid")
    contracts = [instrument_key(row) for row in legs if instrument_key(row)]
    if len(set(contracts)) != len(contracts):
        blockers.append("combo_yield_contract_duplicate")
    return blockers


def _group_structure_mode(legs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    modes = {text(row.get("structure_mode")).lower() for row in legs if text(row.get("structure_mode"))}
    if not modes:
        return _SAME_EXPIRY_PAIR, []
    if len(modes) > 1:
        return sorted(modes)[0], ["combo_yield_structure_mode_mismatch"]
    mode = next(iter(modes))
    if mode not in {_SAME_EXPIRY_PAIR, _STAGGERED_EXPIRY_PAIR}:
        return mode, ["combo_yield_structure_mode_invalid"]
    return mode, []


def _identity_blockers(legs: list[dict[str, Any]], *, structure_mode: str) -> list[str]:
    blockers: list[str] = []
    for key, missing_blocker, mismatch_blocker in (
        ("symbol", "combo_yield_symbol_missing", "combo_yield_symbol_mismatch"),
        ("account", "combo_yield_account_missing", "combo_yield_account_mismatch"),
        ("multiplier", "combo_yield_multiplier_missing", "combo_yield_multiplier_mismatch"),
    ):
        values = [_identity_value(row, key) for row in legs]
        if not values or any(value is None for value in values):
            blockers.append(missing_blocker)
        elif len(set(values)) > 1:
            blockers.append(mismatch_blocker)

    expirations = [_identity_value(row, "expiration") for row in legs]
    if not expirations or any(value is None for value in expirations):
        blockers.append("combo_yield_expiration_missing")
    elif structure_mode == _SAME_EXPIRY_PAIR:
        if len(set(expirations)) > 1:
            blockers.append("combo_yield_expiration_mismatch")
    elif structure_mode == _STAGGERED_EXPIRY_PAIR:
        put_leg = _role_leg(legs, option_type="put")
        call_leg = _role_leg(legs, option_type="call")
        put_expiration = _expiration_date(put_leg)
        call_expiration = _expiration_date(call_leg)
        if put_expiration is None or call_expiration is None:
            blockers.append("combo_yield_expiration_invalid")
        elif call_expiration <= put_expiration:
            blockers.append("combo_yield_expiration_order_invalid")
    return blockers


def _expiration_date(row: dict[str, Any] | None) -> date | None:
    value = _identity_value(row or {}, "expiration")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _identity_value(row: dict[str, Any], key: str) -> str | float | None:
    if key == "symbol":
        return text(row.get("symbol") or row.get("underlying_symbol")).upper() or None
    if key == "account":
        return text(row.get("account")).lower() or None
    if key == "expiration":
        return text(row.get("expiration") or row.get("exp")) or None
    if key == "multiplier":
        return first_float(row, "multiplier", "contract_multiplier")
    return None


def _group_metrics(legs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    put_leg = _role_leg(legs, option_type="put")
    call_leg = _role_leg(legs, option_type="call")
    net_premium, net_premium_missing = _net_premium(legs)
    spot = _first_metric(legs, "spot", "underlying_price")
    put_strike = first_float(put_leg or {}, "strike") if put_leg else None
    call_strike = first_float(call_leg or {}, "strike") if call_leg else None
    put_contracts = first_float(put_leg or {}, "contracts", "contract_count", "quantity", "qty") if put_leg else None
    put_multiplier = first_float(put_leg or {}, "multiplier", "contract_multiplier") if put_leg else None
    downside = None
    if put_strike is not None and put_contracts is not None and put_multiplier is not None and net_premium is not None:
        downside = (put_strike * put_contracts * put_multiplier) - net_premium
    premium_to_downside = None
    if net_premium is not None and downside is not None and downside > 0:
        premium_to_downside = net_premium / downside
    put_distance = None
    if spot and put_strike is not None:
        put_distance = (spot - put_strike) / spot
    call_distance = None
    if spot and call_strike is not None:
        call_distance = (call_strike - spot) / spot
    dte = _first_metric(legs, "dte")
    metrics = {
        "net_premium": _round_or_none(net_premium),
        "put_net_credit": _round_or_none(_first_metric(legs, "put_net_credit")),
        "call_total_cost": _round_or_none(_first_metric(legs, "call_total_cost")),
        "combo_net_credit": _round_or_none(_first_metric(legs, "combo_net_credit")),
        "net_credit_retention": _round_or_none(_first_metric(legs, "net_credit_retention")),
        "call_cost_to_put_credit": _round_or_none(_first_metric(legs, "call_cost_to_put_credit")),
        "put_strike": _round_or_none(put_strike),
        "call_strike": _round_or_none(call_strike),
        "spot": _round_or_none(spot),
        "put_distance_pct": _round_or_none(put_distance),
        "call_distance_pct": _round_or_none(call_distance),
        "max_downside_exposure": _round_or_none(downside),
        "premium_to_downside_ratio": _round_or_none(premium_to_downside),
        "dte": _round_or_none(dte),
        "abs_put_delta": _round_or_none(abs_first_float(put_leg or {}, "abs_delta", "delta") if put_leg else None),
        "abs_call_delta": _round_or_none(abs_first_float(call_leg or {}, "abs_delta", "delta") if call_leg else None),
        "upside_participation_score": _round_or_none(_upside_participation_score(call_distance)),
        "funding_quality_score": _round_or_none(premium_to_downside),
    }
    missing = []
    for key in (
        "net_premium",
        "put_strike",
        "call_strike",
        "spot",
        "put_distance_pct",
        "call_distance_pct",
        "max_downside_exposure",
        "premium_to_downside_ratio",
    ):
        if metrics.get(key) is None:
            missing.append(key)
    if net_premium_missing and "net_premium" not in missing:
        missing.append("net_premium")
    return metrics, missing


def _role_leg(legs: list[dict[str, Any]], *, option_type: str) -> dict[str, Any] | None:
    role = "funding_put" if option_type == "put" else "participation_call"
    matching = [row for row in legs if text(row.get("leg_role")).lower() == role]
    return matching[0] if len(matching) == 1 else None


def _net_premium(legs: list[dict[str, Any]]) -> tuple[float | None, bool]:
    group_values = {
        value for row in legs if (value := first_float(row, "combo_net_credit")) is not None
    }
    if len(group_values) == 1:
        return next(iter(group_values)), False
    if len(group_values) > 1:
        return None, True
    out = 0.0
    missing = False
    used = False
    for row in legs:
        income = first_float(row, "net_income", "net_credit")
        sign = _premium_sign(row)
        if income is not None and sign is not None:
            out += sign * abs(income)
            used = True
            continue
        premium = first_float(row, "mid", "option_mid", "mid_price")
        contracts = first_float(row, "contracts", "contract_count", "quantity", "qty")
        multiplier = first_float(row, "multiplier", "contract_multiplier")
        if premium is None or contracts is None or multiplier is None or sign is None:
            missing = True
            continue
        out += sign * premium * contracts * multiplier
        used = True
    return (out if used else None), missing


def _premium_sign(row: dict[str, Any]) -> float | None:
    side = text(row.get("side") or row.get("position_side")).lower()
    if side.startswith(("short", "sell", "write")):
        return 1.0
    if side.startswith(("long", "buy")):
        return -1.0
    role = text(row.get("leg_role")).lower()
    if any(hint in role for hint in ("long", "participation")):
        return -1.0
    if any(hint in role for hint in ("short", "funding", "covered")):
        return 1.0
    return None


def _first_metric(legs: list[dict[str, Any]], *keys: str) -> float | None:
    for row in legs:
        value = first_float(row, *keys)
        if value is not None:
            return value
    return None


def _group_outcome_evaluation(
    *,
    legs: list[dict[str, Any]],
    metrics: dict[str, Any],
    group_blockers: list[str],
    structure_mode: str,
    marks_by_key: dict[str, list[dict[str, Any]]],
    outcomes_by_key: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    blockers = list(group_blockers)
    if structure_mode == _STAGGERED_EXPIRY_PAIR:
        blockers.append("combo_yield_multi_horizon_outcome_evidence_insufficient")
    realized_pnls: list[float] = []
    outcome_labels: list[str] = []
    for leg in legs:
        key = instrument_key(leg)
        outcomes = outcomes_by_key.get(key) or []
        if not outcomes:
            blockers.append(f"combo_yield_outcome_missing:{key or 'unknown'}")
            continue
        if len(outcomes) != 1:
            blockers.append(f"combo_yield_outcome_duplicate:{key or 'unknown'}")
            continue
        outcome = outcomes[0]
        pnl = first_float(outcome, "realized_pnl", "counterfactual_pnl", "pnl", "net_pnl")
        if pnl is None:
            blockers.append(f"combo_yield_outcome_pnl_missing:{key or 'unknown'}")
            continue
        realized_pnls.append(pnl)
        outcome_labels.append(text(outcome.get("outcome") or outcome.get("settlement") or outcome.get("status")))

    mark_path, mark_blockers = _synchronized_group_mark_path(legs=legs, marks_by_key=marks_by_key)
    blockers.extend(mark_blockers)
    capital_at_risk = first_float(metrics, "max_downside_exposure")
    if capital_at_risk is None or capital_at_risk <= 0:
        blockers.append("combo_yield_group_capital_at_risk_missing")

    realized_pnl = sum(realized_pnls) if len(realized_pnls) == len(legs) and legs else None
    max_adverse_pnl = min((row["group_pnl"] for row in mark_path), default=None)
    return_on_capital = (
        realized_pnl / capital_at_risk
        if realized_pnl is not None and capital_at_risk is not None and capital_at_risk > 0
        else None
    )
    max_adverse_return = (
        max_adverse_pnl / capital_at_risk
        if max_adverse_pnl is not None and capital_at_risk is not None and capital_at_risk > 0
        else None
    )
    return {
        "status": "evaluable" if not blockers else "not_evaluable",
        "blockers": _dedupe(blockers),
        "outcome_leg_count": len(realized_pnls),
        "outcome_labels": outcome_labels,
        "realized_pnl": _round_or_none(realized_pnl),
        "capital_at_risk": _round_or_none(capital_at_risk),
        "return_on_capital": _round_or_none(return_on_capital),
        "common_mark_count": len(mark_path),
        "max_adverse_pnl": _round_or_none(max_adverse_pnl),
        "max_adverse_return_on_capital": _round_or_none(max_adverse_return),
        "mark_path": mark_path,
    }


def _synchronized_group_mark_path(
    *,
    legs: list[dict[str, Any]],
    marks_by_key: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    blockers: list[str] = []
    per_leg: list[dict[str, float]] = []
    for leg in legs:
        key = instrument_key(leg)
        by_time: dict[str, float] = {}
        for mark in marks_by_key.get(key) or []:
            timestamp = mark_time(mark)
            pnl = first_float(mark, "unrealized_pnl", "counterfactual_pnl", "pnl", "mark_pnl")
            if timestamp and pnl is not None:
                by_time[timestamp] = pnl
        if not by_time:
            blockers.append(f"combo_yield_mark_path_missing:{key or 'unknown'}")
        per_leg.append(by_time)
    if blockers or not per_leg:
        return [], blockers
    common_times = set(per_leg[0])
    for leg_marks in per_leg[1:]:
        common_times &= set(leg_marks)
    if not common_times:
        return [], ["combo_yield_synchronized_mark_path_missing"]
    return [
        {"mark_at": timestamp, "group_pnl": round(sum(leg[timestamp] for leg in per_leg), 6)}
        for timestamp in sorted(common_times)
    ], []


def _scorecard(*, groups: list[dict[str, Any]], min_sample: int) -> dict[str, Any]:
    rows = [
        {
            "strategy_group_id": group.get("strategy_group_id"),
            "decision_status": group.get("decision_status"),
            **(group.get("outcome_evaluation") or {}),
        }
        for group in groups
    ]
    evaluable = [row for row in rows if row.get("status") == "evaluable"]
    if not rows:
        status, reason = "not_ready", "combo_group_universe_missing"
    elif len(evaluable) < min_sample:
        status, reason = "not_evaluable", "complete_group_outcome_sample_below_minimum"
    else:
        status, reason = "ready", "complete_group_outcomes_evaluable"
    return {
        "status": status,
        "reason": reason,
        "rows": rows,
        "group_outcome_metrics": _aggregate_group_outcomes(evaluable),
        "limitations": [
            "observed_combo_groups_are_evaluated_without_generated_parameter_variants",
            "incomplete_group_outcomes_are_not_evaluable",
            "combo_yield_does_not_emit_single_leg_parameter_patch",
        ],
    }


def _aggregate_group_outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    realized = [float(row["realized_pnl"]) for row in rows if row.get("realized_pnl") is not None]
    returns = [float(row["return_on_capital"]) for row in rows if row.get("return_on_capital") is not None]
    adverse = [float(row["max_adverse_pnl"]) for row in rows if row.get("max_adverse_pnl") is not None]
    adverse_returns = [
        float(row["max_adverse_return_on_capital"])
        for row in rows
        if row.get("max_adverse_return_on_capital") is not None
    ]
    return {
        "evaluable_group_count": len(rows),
        "realized_pnl_total": _round_or_none(sum(realized)) if realized else None,
        "realized_pnl_avg": _round_or_none(sum(realized) / len(realized)) if realized else None,
        "return_on_capital_avg": _round_or_none(sum(returns) / len(returns)) if returns else None,
        "return_on_capital_worst": _round_or_none(min(returns)) if returns else None,
        "max_adverse_pnl_worst": _round_or_none(min(adverse)) if adverse else None,
        "max_adverse_return_on_capital_worst": (
            _round_or_none(min(adverse_returns)) if adverse_returns else None
        ),
    }


def _status(*, combo_count: int, ready_count: int, min_sample: int) -> str:
    if combo_count <= 0:
        return "not_ready"
    if ready_count <= 0:
        return "not_ready"
    if ready_count < min_sample:
        return "partial_ready"
    return "ready"


def _group_status(legs: list[dict[str, Any]]) -> str:
    statuses = {normal_status(row.get("status")) for row in legs}
    if statuses and statuses <= _ACCEPTED_STATUSES:
        return "accepted"
    if statuses == {"rejected"}:
        return "rejected"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed" if statuses else "unknown"


def _upside_participation_score(call_distance: float | None) -> float | None:
    if call_distance is None:
        return None
    return max(0.0, 1.0 - max(0.0, call_distance))


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out
