from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Callable

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


COMBO_GROUP_EXPERIMENT_SCHEMA_VERSION = "strategy_lab_combo_yield_group_experiment.v1"

_ACCEPTED_STATUSES = {"accepted", "notified"}


def run_combo_yield_group_experiment(
    *,
    candidate_snapshots: list[dict[str, Any]],
    min_sample: int = 30,
) -> dict[str, Any]:
    sample_floor = max(1, int(min_sample))
    combo_rows = [row for row in candidate_snapshots if strategy_family(row) == "combo_yield"]
    groups = _combo_groups(combo_rows)
    ready_groups = [group for group in groups if group["ready_for_group_experiment"]]
    variants = _evaluate_variants(groups=ready_groups)
    scorecard = _scorecard(variants=variants)
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
            "sample_floor_met": len(ready_groups) >= sample_floor,
            "variant_count": len(variants),
            "best_variant": scorecard.get("best_variant"),
            "optimization_claim": "observed_group_universe_only",
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
        "group_universe": {
            "groups": groups[:50],
            "blockers": dict(blocker_counts.most_common(20)),
        },
        "variants": variants,
        "scorecard": scorecard,
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
    }


def _combo_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing: list[dict[str, Any]] = []
    for row in rows:
        group_id = text(row.get("strategy_group_id"))
        if group_id:
            grouped[group_id].append(row)
        else:
            missing.append(row)

    groups = [_group_payload(group_id=group_id, legs=legs) for group_id, legs in sorted(grouped.items())]
    for idx, row in enumerate(missing, start=1):
        groups.append(
            _group_payload(
                group_id=f"missing-group-{idx}:{instrument_key(row) or idx}",
                strategy_group_id=None,
                legs=[row],
                extra_blockers=["combo_yield_group_identity_missing"],
            )
        )
    return groups


def _group_payload(
    *,
    group_id: str,
    strategy_group_id: str | None = None,
    legs: list[dict[str, Any]],
    extra_blockers: list[str] | None = None,
) -> dict[str, Any]:
    blockers = list(extra_blockers or [])
    if len(legs) < 2 and "combo_yield_group_has_too_few_legs" not in blockers:
        blockers.append("combo_yield_group_has_too_few_legs")
    roles = {text(row.get("leg_role")).lower() for row in legs if text(row.get("leg_role"))}
    if not roles:
        blockers.append("combo_yield_leg_role_missing")
    blockers.extend(_identity_blockers(legs))
    metrics, missing_metrics = _group_metrics(legs)
    if missing_metrics:
        blockers.append("combo_yield_group_metric_missing")
    status = _group_status(legs)
    return {
        "group_id": group_id,
        "strategy_group_id": group_id if strategy_group_id is None and not extra_blockers else strategy_group_id,
        "decision_status": status,
        "leg_count": len(legs),
        "leg_roles": sorted(roles),
        "candidate_ids": [instrument_key(row) for row in legs if instrument_key(row)],
        "metrics": metrics,
        "missing_metrics": missing_metrics,
        "blockers": _dedupe(blockers),
        "ready_for_group_experiment": not blockers,
    }


def _identity_blockers(legs: list[dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for key, blocker in (
        ("symbol", "combo_yield_symbol_mismatch"),
        ("account", "combo_yield_account_mismatch"),
        ("expiration", "combo_yield_expiration_mismatch"),
        ("multiplier", "combo_yield_multiplier_mismatch"),
    ):
        values = {_identity_value(row, key) for row in legs if _identity_value(row, key) is not None}
        if len(values) > 1:
            blockers.append(blocker)
    return blockers


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
    typed = [row for row in legs if text(row.get("option_type") or row.get("mode")).lower() == option_type]
    if not typed:
        return None
    if option_type == "put":
        preferred = [row for row in typed if "funding" in text(row.get("leg_role")).lower()]
        return (preferred or typed)[0]
    preferred = [
        row
        for row in typed
        if any(hint in text(row.get("leg_role")).lower() for hint in ("participation", "covered", "call"))
    ]
    return (preferred or typed)[0]


def _net_premium(legs: list[dict[str, Any]]) -> tuple[float | None, bool]:
    values = [value for row in legs if (value := first_float(row, "net_income", "net_credit", "combo_net_credit")) is not None]
    if values:
        return sum(values), False
    out = 0.0
    missing = False
    used = False
    for row in legs:
        premium = first_float(row, "mid", "option_mid", "mid_price")
        contracts = first_float(row, "contracts", "contract_count", "quantity", "qty")
        multiplier = first_float(row, "multiplier", "contract_multiplier")
        sign = _premium_sign(row)
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


def _evaluate_variants(*, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not groups:
        return []
    specs = _variant_specs(groups)
    baseline_accepted = {group["group_id"] for group in groups if group.get("decision_status") in _ACCEPTED_STATUSES}
    variants = []
    for spec in specs:
        rows = _variant_rows(groups=groups, spec=spec)
        accepted_ids = {row["group_id"] for row in rows["accepted_groups"]}
        newly_accepted = sorted(accepted_ids - baseline_accepted)
        newly_rejected = sorted(baseline_accepted - accepted_ids)
        objective_components = _objective_components(
            accepted_groups=rows["accepted_groups"],
            newly_accepted_count=len(newly_accepted),
            newly_rejected_count=len(newly_rejected),
            missing_metric_count=rows["missing_metric_count"],
            safety_violation_count=rows["safety_violation_count"],
        )
        variants.append(
            {
                "name": spec["name"],
                "parameters": spec["parameters"],
                "accepted_group_count": len(accepted_ids),
                "newly_accepted_group_count": len(newly_accepted),
                "newly_rejected_group_count": len(newly_rejected),
                "missing_metric_count": rows["missing_metric_count"],
                "safety_violation_count": rows["safety_violation_count"],
                "objective_score": objective_components["objective_score"],
                "objective_components": objective_components,
                "status": "blocked" if rows["safety_violation_count"] else "candidate_review",
                "accepted_group_samples": rows["accepted_groups"][:10],
                "newly_accepted_group_ids": newly_accepted[:20],
                "newly_rejected_group_ids": newly_rejected[:20],
            }
        )
    variants.sort(key=lambda row: (row["status"] == "blocked", -float(row["objective_score"]), str(row["name"])))
    return variants


def _variant_specs(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metric_values = _metric_values(groups)
    return [
        {
            "name": "combo_yield_relax_put_distance",
            "parameters": {
                "min_put_distance_pct": _quantile(metric_values["put_distance_pct"], 0.10),
                "min_net_premium": _quantile(metric_values["net_premium"], 0.20),
            },
        },
        {
            "name": "combo_yield_tighten_put_risk",
            "parameters": {
                "min_put_distance_pct": _quantile(metric_values["put_distance_pct"], 0.60),
                "max_abs_put_delta": _quantile(metric_values["abs_put_delta"], 0.50),
            },
        },
        {
            "name": "combo_yield_increase_net_premium_floor",
            "parameters": {
                "min_net_premium": _quantile(metric_values["net_premium"], 0.60),
                "min_premium_to_downside_ratio": _quantile(metric_values["premium_to_downside_ratio"], 0.40),
            },
        },
        {
            "name": "combo_yield_increase_upside_participation",
            "parameters": {
                "max_call_distance_pct": _quantile(metric_values["call_distance_pct"], 0.40),
                "min_net_premium": _quantile(metric_values["net_premium"], 0.20),
            },
        },
        {
            "name": "combo_yield_balanced_yield_upside",
            "parameters": {
                "min_put_distance_pct": _quantile(metric_values["put_distance_pct"], 0.40),
                "max_call_distance_pct": _quantile(metric_values["call_distance_pct"], 0.60),
                "min_premium_to_downside_ratio": _quantile(metric_values["premium_to_downside_ratio"], 0.50),
            },
        },
    ]


def _metric_values(groups: list[dict[str, Any]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for group in groups:
        metrics = group.get("metrics") or {}
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key].append(float(value))
    return out


def _variant_rows(*, groups: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    rules = _rules_from_parameters(spec.get("parameters") or {})
    accepted = []
    missing_metric_count = 0
    safety_violation_count = 0
    for group in groups:
        if group.get("blockers"):
            safety_violation_count += 1
            continue
        missing = False
        passed = True
        for metric, predicate in rules:
            value = (group.get("metrics") or {}).get(metric)
            if value is None:
                missing = True
                passed = False
                continue
            if not predicate(float(value)):
                passed = False
        if missing:
            missing_metric_count += 1
        if passed:
            accepted.append(group)
    return {
        "accepted_groups": accepted,
        "missing_metric_count": missing_metric_count,
        "safety_violation_count": safety_violation_count,
    }


def _rules_from_parameters(parameters: dict[str, Any]) -> list[tuple[str, Callable[[float], bool]]]:
    rules: list[tuple[str, Callable[[float], bool]]] = []
    for key, value in parameters.items():
        if value is None:
            continue
        threshold = float(value)
        if key == "min_put_distance_pct":
            rules.append(("put_distance_pct", lambda actual, threshold=threshold: actual >= threshold))
        elif key == "max_call_distance_pct":
            rules.append(("call_distance_pct", lambda actual, threshold=threshold: actual <= threshold))
        elif key == "min_net_premium":
            rules.append(("net_premium", lambda actual, threshold=threshold: actual >= threshold))
        elif key == "min_premium_to_downside_ratio":
            rules.append(("premium_to_downside_ratio", lambda actual, threshold=threshold: actual >= threshold))
        elif key == "max_abs_put_delta":
            rules.append(("abs_put_delta", lambda actual, threshold=threshold: actual <= threshold))
    return rules


def _objective_components(
    *,
    accepted_groups: list[dict[str, Any]],
    newly_accepted_count: int,
    newly_rejected_count: int,
    missing_metric_count: int,
    safety_violation_count: int,
) -> dict[str, float]:
    avg_premium_ratio = _avg_metric(accepted_groups, "premium_to_downside_ratio")
    avg_upside = _avg_metric(accepted_groups, "upside_participation_score")
    avg_put_distance = _avg_metric(accepted_groups, "put_distance_pct")
    objective_score = round(
        (100.0 * avg_premium_ratio)
        + (5.0 * avg_upside)
        + (2.0 * avg_put_distance)
        + newly_accepted_count
        - (0.5 * newly_rejected_count)
        - (0.25 * missing_metric_count)
        - (100.0 * safety_violation_count),
        6,
    )
    return {
        "objective_score": objective_score,
        "avg_premium_to_downside_ratio": _round_or_zero(avg_premium_ratio),
        "avg_upside_participation_score": _round_or_zero(avg_upside),
        "avg_put_distance_pct": _round_or_zero(avg_put_distance),
        "newly_accepted_group_count": float(newly_accepted_count),
        "newly_rejected_group_count": float(newly_rejected_count),
        "missing_metric_count": float(missing_metric_count),
        "safety_violation_count": float(safety_violation_count),
    }


def _scorecard(*, variants: list[dict[str, Any]]) -> dict[str, Any]:
    best = next((row for row in variants if row.get("status") != "blocked"), None)
    rows = [
        {
            "variant": row.get("name"),
            "strategy_family": "combo_yield",
            "objective_score": row.get("objective_score"),
            "accepted_group_count": row.get("accepted_group_count"),
            "newly_accepted_group_count": row.get("newly_accepted_group_count"),
            "newly_rejected_group_count": row.get("newly_rejected_group_count"),
            "missing_metric_count": row.get("missing_metric_count"),
            "safety_violation_count": row.get("safety_violation_count"),
            "status": row.get("status"),
        }
        for row in variants
    ]
    return {
        "status": "ready" if rows else "not_ready",
        "reason": "observed_group_universe_scorecard" if rows else "combo_group_universe_missing",
        "rows": rows,
        "best_variant": rows[0] if best else None,
        "optimization_claim": "observed_group_universe_only",
        "limitations": [
            "scorecard_is_not_production_recommendation",
            "combo_yield_group_experiment_reuses_observed_run_universe_only",
            "combo_yield_does_not_emit_single_leg_parameter_patch",
        ],
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
    if statuses & _ACCEPTED_STATUSES:
        return "accepted"
    if "rejected" in statuses:
        return "rejected"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed" if statuses else "unknown"


def _avg_metric(groups: list[dict[str, Any]], key: str) -> float:
    values = [
        float(value)
        for group in groups
        if (value := (group.get("metrics") or {}).get(key)) is not None
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    idx = round((len(sorted_values) - 1) * max(0.0, min(1.0, fraction)))
    return _round_or_none(sorted_values[idx])


def _upside_participation_score(call_distance: float | None) -> float | None:
    if call_distance is None:
        return None
    return max(0.0, 1.0 - max(0.0, call_distance))


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _round_or_zero(value: Any) -> float:
    return round(float(value or 0.0), 6)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out
