from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from domain.domain.risk_capacity import compute_sell_call_share_capacity, compute_sell_put_cash_capacity

from src.application.shadow_replay.common import (
    ANALYSIS_SCHEMA_VERSION,
    dataset_dir_from_arg,
    first_float,
    float_or_none,
    instrument_key,
    normal_status,
    read_jsonl,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    write_json,
)
from src.application.shadow_replay.settlement import is_complete_closed_outcome, is_usable_mark


WHEEL_TRANSITION_OUTCOMES = {"assigned_at_expiry", "called_away_at_expiry"}
WHEEL_LIFECYCLE_RISK_SCHEMA_VERSION = "shadow_replay_wheel_lifecycle_risk.v1"
MIN_EMPIRICAL_TAIL_SAMPLES = 30
LIFECYCLE_PNL_FIELDS = (
    "lifecycle_pnl",
    "assignment_lifecycle_pnl",
    "callaway_lifecycle_pnl",
)


def analyze_shadow_replay_dataset(
    *,
    dataset: str | Path,
    min_sample: int = 30,
    output: str | Path | None = None,
) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    analysis = analyze_rows(
        candidate_snapshots=read_jsonl(dataset_dir / "candidate_snapshots.jsonl"),
        filter_decisions=read_jsonl(dataset_dir / "filter_decisions.jsonl"),
        mark_snapshots=read_jsonl(dataset_dir / "mark_path_snapshots.jsonl"),
        outcome_facts=read_jsonl(dataset_dir / "outcome_facts.jsonl"),
        min_sample=max(1, int(min_sample)),
    )
    analysis.update(
        {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "dataset_dir": str(dataset_dir),
            "generated_at_utc": utc_now(),
            "safety": safety_payload(writes_local_dataset=False),
        }
    )
    if output:
        write_json(resolve_output_path(output), analysis)
    return analysis


def analyze_rows(
    *,
    candidate_snapshots: list[dict[str, Any]],
    filter_decisions: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]],
    outcome_facts: list[dict[str, Any]],
    min_sample: int,
) -> dict[str, Any]:
    sample_floor = max(1, int(min_sample))
    status_counts = Counter(str(row.get("status") or "unknown") for row in candidate_snapshots)
    rejected_count = sum(status_counts.get(status, 0) for status in ("rejected", "post_filtered", "ranked_below"))
    usable_mark_count = sum(1 for row in mark_snapshots if is_usable_mark(row))
    recommendation = _shadow_recommendation(
        candidate_count=len(candidate_snapshots),
        rejected_count=rejected_count,
        filter_decision_count=len(filter_decisions),
        mark_count=len(mark_snapshots),
        usable_mark_count=usable_mark_count,
        outcome_count=len(outcome_facts),
        min_sample=sample_floor,
    )
    checks = _evidence_checks(
        candidate_count=len(candidate_snapshots),
        rejected_count=rejected_count,
        filter_decision_count=len(filter_decisions),
        mark_count=len(mark_snapshots),
        usable_mark_count=usable_mark_count,
        outcome_count=len(outcome_facts),
    )
    quality = decision_quality(candidate_snapshots, mark_snapshots, outcome_facts, min_sample=sample_floor)
    quality_summary = quality["summary"]
    advice_gate = parameter_advice_gate(quality)
    review_readiness = review_readiness_gate(quality, advice_gate=advice_gate)
    checks.update(
        {
            "has_instrument_identity": bool(advice_gate["has_instrument_identity"]),
            "instrument_identity_ready_count": int(advice_gate["instrument_identity_ready_count"]),
            "instrument_identity_missing_count": int(advice_gate["instrument_identity_missing_count"]),
            "strategy_profile_ready_count": int(advice_gate["strategy_profile_ready_count"]),
            "strategy_profile_missing_count": int(advice_gate["strategy_profile_missing_count"]),
            "trace_only_evidence": bool(advice_gate["trace_only_evidence"]),
            "usable_mark_ready_count": int(advice_gate["usable_mark_ready_count"]),
            "usable_mark_missing_count": int(advice_gate["usable_mark_missing_count"]),
            "outcome_ready_count": int(advice_gate["outcome_ready_count"]),
            "outcome_missing_count": int(advice_gate["outcome_missing_count"]),
        }
    )
    return {
        "summary": {
            "status": recommendation["status"],
            "reason": recommendation["reason"],
            "candidate_snapshot_count": len(candidate_snapshots),
            "filter_decision_count": len(filter_decisions),
            "rankable_snapshot_count": sum(1 for row in candidate_snapshots if str(row.get("mode") or "") in {"put", "call"}),
            "mark_path_snapshot_count": len(mark_snapshots),
            "usable_mark_path_snapshot_count": usable_mark_count,
            "missing_mark_path_snapshot_count": max(0, len(mark_snapshots) - usable_mark_count),
            "outcome_fact_count": len(outcome_facts),
            "min_sample": sample_floor,
            "accepted_count": status_counts.get("accepted", 0),
            "rejected_count": status_counts.get("rejected", 0),
            "post_filtered_count": status_counts.get("post_filtered", 0),
            "ranked_below_count": status_counts.get("ranked_below", 0),
            "counterfactual_candidate_count": rejected_count,
            "evidence_level": _evidence_level(candidate_snapshots, filter_decisions, mark_snapshots, outcome_facts),
            "review_readiness_status": review_readiness["status"],
            "manual_strategy_review_ready": review_readiness["manual_strategy_review_ready"],
            "decision_quality_status": advice_gate["status"],
            "bad_decision_count": quality_summary["bad_decision_count"],
            "inconclusive_count": quality_summary["inconclusive_count"],
            "parameter_advice_allowed": advice_gate["parameter_advice_allowed"],
        },
        "evidence_checks": checks,
        "bucket_stats": {
            "dte": _bucket_stats(candidate_snapshots, "dte", _dte_bucket),
            "abs_delta": _bucket_stats(candidate_snapshots, "abs_delta", _delta_bucket),
            "iv_rv_ratio": _bucket_stats(candidate_snapshots, "iv_rv_ratio", _iv_rv_bucket),
            "spread_ratio": _bucket_stats(candidate_snapshots, "spread_ratio", _spread_bucket),
            "single_trade_concentration": _bucket_stats(candidate_snapshots, "single_trade_concentration", _concentration_bucket),
        },
        "filter_decisions": decision_summary(filter_decisions),
        "outcome_coverage": outcome_coverage(candidate_snapshots, mark_snapshots, outcome_facts),
        "path_risk": path_risk_stats(candidate_snapshots, mark_snapshots),
        "outcome_stats": outcome_stats(candidate_snapshots, outcome_facts),
        "insurance_metrics": insurance_metrics(
            candidate_snapshots,
            mark_snapshots,
            outcome_facts,
            min_sample=sample_floor,
        ),
        "wheel_lifecycle_risk": wheel_lifecycle_risk(candidate_snapshots),
        "outcome_by_bucket": outcome_bucket_stats(candidate_snapshots, outcome_facts),
        "decision_quality": quality,
        "review_readiness": review_readiness,
        "parameter_advice_gate": advice_gate,
        "recommendations": [recommendation],
    }


def decision_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(row.get("status") or "unknown") for row in decisions)
    rule_counts = Counter(str(row.get("rule") or "") for row in decisions if str(row.get("rule") or ""))
    stage_counts = Counter(str(row.get("stage") or "") for row in decisions if str(row.get("stage") or ""))
    function_counts = Counter(str(row.get("function") or "") for row in decisions if str(row.get("function") or ""))
    return {
        "status_counts": dict(status_counts.most_common(20)),
        "stage_counts": dict(stage_counts.most_common(30)),
        "rule_counts": dict(rule_counts.most_common(30)),
        "function_counts": dict(function_counts.most_common(20)),
    }


def outcome_coverage(candidates: list[dict[str, Any]], marks: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_keys = {instrument_key(row) for row in candidates}
    mark_keys = {instrument_key(row) for row in marks}
    usable_mark_keys = {instrument_key(row) for row in marks if is_usable_mark(row)}
    outcome_keys = {instrument_key(row) for row in outcomes}
    candidate_keys.discard("")
    mark_keys.discard("")
    usable_mark_keys.discard("")
    outcome_keys.discard("")
    return {
        "candidate_instrument_count": len(candidate_keys),
        "marked_instrument_count": len(candidate_keys & mark_keys),
        "usable_marked_instrument_count": len(candidate_keys & usable_mark_keys),
        "outcome_instrument_count": len(candidate_keys & outcome_keys),
        "unmarked_instrument_count": max(0, len(candidate_keys - mark_keys)),
        "missing_usable_mark_instrument_count": max(0, len(candidate_keys - usable_mark_keys)),
        "missing_outcome_instrument_count": max(0, len(candidate_keys - outcome_keys)),
    }


def path_risk_stats(candidates: list[dict[str, Any]], marks: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_status = _candidate_status_by_instrument(candidates)
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"mark_count": 0, "instruments": set(), "pnl_values": []})
    for row in marks:
        key = instrument_key(row)
        status = candidate_status.get(key, "unknown")
        pnl = first_float(row, "unrealized_pnl", "counterfactual_pnl", "pnl", "mark_pnl")
        payload = grouped[status]
        payload["mark_count"] += 1
        if key:
            payload["instruments"].add(key)
        if pnl is not None:
            payload["pnl_values"].append(pnl)
    return {"by_status": _risk_payload(grouped)}


def outcome_stats(candidates: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_status = _candidate_status_by_instrument(candidates)
    grouped: dict[str, dict[str, Any]] = defaultdict(_empty_outcome_group)
    for row in outcomes:
        key = instrument_key(row)
        status = candidate_status.get(key, "unknown")
        pnl = first_float(row, "realized_pnl", "counterfactual_pnl", "pnl", "net_pnl")
        payload = grouped[status]
        outcome = text(row.get("outcome") or row.get("status") or row.get("settlement"))
        _record_outcome_payload(payload, instrument_key=key, pnl=pnl, outcome=outcome)
    return {"by_status": _outcome_payload(grouped)}


def outcome_bucket_stats(candidates: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_by_key = _candidate_by_instrument(candidates)
    dimensions: dict[str, tuple[str, Any]] = {
        "dte": ("dte", _dte_bucket),
        "abs_delta": ("abs_delta", _delta_bucket),
        "iv_rv_ratio": ("iv_rv_ratio", _iv_rv_bucket),
        "spread_ratio": ("spread_ratio", _spread_bucket),
        "single_trade_concentration": ("single_trade_concentration", _concentration_bucket),
    }
    out: dict[str, Any] = {}
    for dimension, (field, bucket_fn) in dimensions.items():
        bucketed: dict[str, dict[str, Any]] = defaultdict(lambda: {"all": _empty_outcome_group(), "by_status": defaultdict(_empty_outcome_group)})
        for row in outcomes:
            key = instrument_key(row)
            candidate = candidate_by_key.get(key)
            if not candidate:
                continue
            label = bucket_fn(float_or_none(candidate.get(field)))
            status = normal_status(candidate.get("status"))
            pnl = first_float(row, "realized_pnl", "counterfactual_pnl", "pnl", "net_pnl")
            outcome = text(row.get("outcome") or row.get("status") or row.get("settlement"))
            payload = bucketed[label]
            _record_outcome_payload(payload["all"], instrument_key=key, pnl=pnl, outcome=outcome)
            _record_outcome_payload(payload["by_status"][status], instrument_key=key, pnl=pnl, outcome=outcome)
        out[dimension] = {
            label: {
                **_summarize_outcome_group(payload["all"]),
                "by_status": _outcome_payload(payload["by_status"]),
            }
            for label, payload in sorted(bucketed.items())
        }
    return out


def insurance_metrics(
    candidates: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    min_sample: int,
) -> dict[str, Any]:
    candidate_by_key = _candidate_by_instrument(candidates)
    adverse_by_key = _max_adverse_pnl_by_instrument(marks)
    by_status: dict[str, dict[str, Any]] = defaultdict(_empty_insurance_group)
    by_mode: dict[str, dict[str, Any]] = defaultdict(_empty_insurance_group)
    by_mode_status: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(_empty_insurance_group)
    )
    for row in outcomes:
        key = instrument_key(row)
        candidate = candidate_by_key.get(key) or {}
        sample = _insurance_sample(candidate=candidate, outcome=row, instrument_key=key, max_adverse_pnl=adverse_by_key.get(key))
        _record_insurance_payload(by_status[sample["status"]], sample)
        _record_insurance_payload(by_mode[sample["mode"]], sample)
        _record_insurance_payload(by_mode_status[sample["mode"]][sample["status"]], sample)
    return {
        "by_status": _insurance_payload(by_status, min_sample=min_sample),
        "by_mode": _insurance_payload(by_mode, min_sample=min_sample),
        "by_mode_status": {
            mode: _insurance_payload(grouped, min_sample=min_sample)
            for mode, grouped in sorted(by_mode_status.items())
        },
        "by_bucket": insurance_bucket_stats(candidate_by_key, adverse_by_key, outcomes, min_sample=min_sample),
    }


def wheel_lifecycle_risk(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    identity_missing_count = 0
    for row in _wheel_candidate_scenarios(candidates):
        account = text(row.get("account")).lower()
        symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
        if not account or not symbol:
            identity_missing_count += 1
            continue
        grouped[(account, symbol)].append(row)

    rows: list[dict[str, Any]] = []
    for (account, symbol), group_rows in sorted(grouped.items()):
        put_rows = [row for row in group_rows if _wheel_mode(row) == "put"]
        call_rows = [row for row in group_rows if _wheel_mode(row) == "call"]
        sell_put = _sell_put_wheel_risk(put_rows) if put_rows else None
        covered_call = _covered_call_wheel_risk(call_rows) if call_rows else None
        sections = [section for section in (sell_put, covered_call) if section is not None]
        rows.append(
            {
                "account": account,
                "symbol": symbol,
                "status": (
                    "evaluable"
                    if sections and all(section["status"] == "evaluable" for section in sections)
                    else "not_evaluable"
                ),
                "sell_put": sell_put,
                "covered_call": covered_call,
            }
        )

    evaluable_count = sum(1 for row in rows if row["status"] == "evaluable")
    not_evaluable_count = len(rows) - evaluable_count
    if not rows or evaluable_count == 0:
        status = "not_evaluable"
    elif not_evaluable_count:
        status = "partial"
    else:
        status = "evaluable"
    return {
        "schema_version": WHEEL_LIFECYCLE_RISK_SCHEMA_VERSION,
        "summary": {
            "status": status,
            "account_symbol_count": len(rows),
            "evaluable_account_symbol_count": evaluable_count,
            "not_evaluable_account_symbol_count": not_evaluable_count,
            "identity_missing_candidate_count": identity_missing_count,
            "scenario_basis": "one_candidate_contract_at_a_time",
            "production_gate_applied": False,
        },
        "by_account_symbol": rows,
        "limitations": [
            "candidate_contracts_are_alternative_scenarios_not_simultaneous_positions",
            "account_context_is_read_only_and_not_a_production_gate",
            "missing_or_inconsistent_capacity_fields_are_not_evaluable",
        ],
    }


def _wheel_candidate_scenarios(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in candidates:
        if text(row.get("source_kind")) == "filter_decision" or _strategy_family(row) == "combo_yield":
            continue
        mode = _wheel_mode(row)
        if mode not in {"put", "call"}:
            continue
        key = (
            text(row.get("account")).lower(),
            text(row.get("symbol") or row.get("underlying_symbol")).upper(),
            mode,
            text(row.get("contract_symbol") or row.get("option_symbol")).upper(),
            text(row.get("expiration") or row.get("exp")),
            text(row.get("strike")),
        )
        target = merged.setdefault(key, {})
        for field, value in row.items():
            if _wheel_value_missing(target.get(field)) and not _wheel_value_missing(value):
                target[field] = value
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in merged.values():
        grouped[
            (
                text(row.get("account")).lower(),
                text(row.get("symbol") or row.get("underlying_symbol")).upper(),
                _wheel_mode(row),
            )
        ].append(row)
    out: list[dict[str, Any]] = []
    for rows in grouped.values():
        labeled = [row for row in rows if "_candidates_labeled.csv" in text(row.get("source_path")).lower()]
        out.extend(labeled or rows)
    return out


def _wheel_value_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _wheel_mode(row: dict[str, Any]) -> str:
    return text(row.get("option_type") or row.get("mode")).lower()


def _sell_put_wheel_risk(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nav, nav_issue = _wheel_context_number(rows, "portfolio_nav_cny", "nav_cny")
    stock, stock_issue = _wheel_context_number(rows, "existing_stock_value_cny_symbol")
    existing_symbol, symbol_issue = _wheel_context_number(rows, "existing_short_put_assignment_cny_symbol")
    existing_total, total_issue = _wheel_context_number(rows, "existing_short_put_assignment_cny_total")
    issues = {
        "portfolio_nav_cny": nav_issue,
        "existing_stock_value_cny_symbol": stock_issue,
        "existing_short_put_assignment_cny_symbol": symbol_issue,
        "existing_short_put_assignment_cny_total": total_issue,
    }
    missing_fields = [field for field, issue in issues.items() if issue == "missing"]
    inconsistent_fields = [field for field, issue in issues.items() if issue == "inconsistent"]

    obligations: list[float] = []
    post_symbol_exposure: list[float] = []
    post_account_obligation: list[float] = []
    symbol_nav_ratios: list[float] = []
    account_nav_ratios: list[float] = []
    cash_supported_count = 0
    cash_insufficient_count = 0
    cash_not_evaluable_count = 0
    cash_basis_counts: Counter[str] = Counter()
    for row in rows:
        obligation = first_float(row, "assignment_notional_cny", "cash_required_cny")
        if obligation is None:
            cash_not_evaluable_count += 1
            continue
        obligations.append(obligation)
        if stock is not None and existing_symbol is not None:
            post_symbol_exposure.append(stock + existing_symbol + obligation)
        if existing_total is not None:
            post_account_obligation.append(existing_total + obligation)
        if nav is not None and nav > 0:
            if stock is not None and existing_symbol is not None:
                symbol_nav_ratios.append((stock + existing_symbol + obligation) / nav)
            if existing_total is not None:
                account_nav_ratios.append((existing_total + obligation) / nav)

        capacity = compute_sell_put_cash_capacity(
            cash_required_cny=first_float(row, "cash_required_cny", "assignment_notional_cny"),
            cash_free_cny=first_float(row, "cash_free_cny"),
            cash_free_total_cny=first_float(row, "cash_free_total_cny"),
            cash_required_usd=first_float(row, "cash_required_usd"),
            cash_free_usd=first_float(row, "cash_free_usd"),
        )
        if capacity.basis is None:
            cash_not_evaluable_count += 1
        else:
            cash_basis_counts[capacity.basis] += 1
            if capacity.accepted:
                cash_supported_count += 1
            else:
                cash_insufficient_count += 1

    if len(obligations) != len(rows):
        missing_fields.append("assignment_notional_cny")
    if cash_not_evaluable_count:
        missing_fields.append("cash_capacity_context")
    return {
        "status": "evaluable" if not missing_fields and not inconsistent_fields else "not_evaluable",
        "candidate_scenario_count": len(rows),
        "portfolio_nav_cny": nav,
        "existing_stock_value_cny_symbol": stock,
        "existing_short_put_assignment_cny_symbol": existing_symbol,
        "existing_short_put_assignment_cny_total": existing_total,
        "candidate_assignment_obligation_cny": _wheel_range(obligations),
        "post_assignment_symbol_exposure_cny": _wheel_range(post_symbol_exposure),
        "post_assignment_account_obligation_cny": _wheel_range(post_account_obligation),
        "post_assignment_symbol_nav_ratio": _wheel_range(symbol_nav_ratios),
        "post_assignment_account_obligation_nav_ratio": _wheel_range(account_nav_ratios),
        "cash_capacity": {
            "supported_scenario_count": cash_supported_count,
            "insufficient_scenario_count": cash_insufficient_count,
            "not_evaluable_scenario_count": cash_not_evaluable_count,
            "basis_counts": dict(cash_basis_counts),
        },
        "missing_fields": sorted(set(missing_fields)),
        "inconsistent_fields": sorted(set(inconsistent_fields)),
    }


def _covered_call_wheel_risk(rows: list[dict[str, Any]]) -> dict[str, Any]:
    shares_total, total_issue = _wheel_context_number(rows, "shares_total", "shares")
    shares_locked, locked_issue = _wheel_context_number(rows, "shares_locked")
    explicit_available, available_issue = _wheel_context_number(rows, "shares_available_for_cover")
    issues = {
        "shares_total": total_issue,
        "shares_locked": locked_issue,
        "shares_available_for_cover": available_issue,
    }
    missing_fields = [field for field, issue in issues.items() if issue == "missing" and field != "shares_available_for_cover"]
    inconsistent_fields = [field for field, issue in issues.items() if issue == "inconsistent"]
    shares_available = explicit_available
    if shares_available is None and shares_total is not None and shares_locked is not None:
        shares_available = max(0.0, shares_total - shares_locked)

    called_away_shares: list[float] = []
    called_away_ratios: list[float] = []
    post_locked_shares: list[float] = []
    post_locked_ratios: list[float] = []
    available_contracts: list[float] = []
    supported_count = 0
    insufficient_count = 0
    not_evaluable_count = 0
    for row in rows:
        multiplier = first_float(row, "multiplier", "contract_multiplier")
        contracts = first_float(row, "contracts", "contract_count") or 1.0
        if multiplier is None or multiplier <= 0 or shares_total is None or shares_locked is None:
            not_evaluable_count += 1
            continue
        called = multiplier * contracts
        called_away_shares.append(called)
        post_locked_shares.append(shares_locked + called)
        if shares_total > 0:
            called_away_ratios.append(called / shares_total)
            post_locked_ratios.append((shares_locked + called) / shares_total)
        capacity = compute_sell_call_share_capacity(
            shares_total=shares_total,
            shares_locked=shares_locked,
            shares_available_for_cover=shares_available,
            multiplier=multiplier,
        )
        available_contracts.append(float(capacity.covered_contracts_available))
        if capacity.covered_contracts_available >= contracts:
            supported_count += 1
        else:
            insufficient_count += 1

    if not_evaluable_count:
        missing_fields.append("multiplier_or_share_capacity_context")
    if shares_total is not None and shares_total <= 0:
        missing_fields.append("positive_shares_total")
    return {
        "status": "evaluable" if not missing_fields and not inconsistent_fields else "not_evaluable",
        "candidate_scenario_count": len(rows),
        "shares_total": shares_total,
        "shares_locked": shares_locked,
        "shares_available_for_cover": shares_available,
        "locked_share_ratio": (shares_locked / shares_total) if shares_total and shares_locked is not None else None,
        "candidate_called_away_shares": _wheel_range(called_away_shares),
        "candidate_called_away_share_ratio": _wheel_range(called_away_ratios),
        "post_candidate_locked_shares": _wheel_range(post_locked_shares),
        "post_candidate_locked_share_ratio": _wheel_range(post_locked_ratios),
        "covered_contracts_available": _wheel_range(available_contracts),
        "share_capacity": {
            "supported_scenario_count": supported_count,
            "insufficient_scenario_count": insufficient_count,
            "not_evaluable_scenario_count": not_evaluable_count,
        },
        "missing_fields": sorted(set(missing_fields)),
        "inconsistent_fields": sorted(set(inconsistent_fields)),
    }


def _wheel_context_number(rows: list[dict[str, Any]], *keys: str) -> tuple[float | None, str | None]:
    values = [value for row in rows if (value := first_float(row, *keys)) is not None]
    if not values:
        return None, "missing"
    if len({round(value, 6) for value in values}) > 1:
        return None, "inconsistent"
    return values[0], None


def _wheel_range(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def decision_quality(
    candidates: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    min_sample: int,
) -> dict[str, Any]:
    sample_floor = max(1, int(min_sample))
    outcome_by_key = {instrument_key(row): row for row in outcomes if instrument_key(row)}
    adverse_by_key = _max_adverse_pnl_by_instrument(marks)
    usable_mark_keys = {instrument_key(row) for row in marks if is_usable_mark(row) and instrument_key(row)}
    sample_count = len(candidates)
    force_inconclusive = sample_count < sample_floor
    samples: list[dict[str, Any]] = []
    label_counts: Counter[str] = Counter()
    by_strategy_profile: dict[str, Counter[str]] = defaultdict(Counter)
    source_kind_counts: Counter[str] = Counter()
    instrument_identity_missing_count = 0
    instrument_identity_ready_count = 0
    strategy_profile_missing_count = 0
    strategy_profile_ready_count = 0
    outcome_missing_count = 0
    outcome_ready_count = 0
    usable_mark_missing_count = 0
    usable_mark_ready_count = 0
    for candidate in candidates:
        key = instrument_key(candidate)
        has_identity = _has_instrument_identity(candidate)
        if has_identity:
            instrument_identity_ready_count += 1
        else:
            instrument_identity_missing_count += 1
        if _strategy_profile(candidate) == "unknown":
            strategy_profile_missing_count += 1
        else:
            strategy_profile_ready_count += 1
        if has_identity and key and key not in outcome_by_key:
            outcome_missing_count += 1
        elif has_identity and key:
            outcome_ready_count += 1
        if has_identity and key and key not in usable_mark_keys:
            usable_mark_missing_count += 1
        elif has_identity and key:
            usable_mark_ready_count += 1
        source_kind_counts[str(candidate.get("source_kind") or "unknown")] += 1
        outcome = outcome_by_key.get(key) if key else None
        sample = _decision_quality_sample(
            candidate=candidate,
            outcome=outcome,
            max_adverse_pnl=adverse_by_key.get(key),
            force_inconclusive=force_inconclusive,
            sample_floor=sample_floor,
            sample_count=sample_count,
        )
        label = str(sample.get("label") or "inconclusive")
        profile = str(sample.get("strategy_profile") or "unknown")
        label_counts[label] += 1
        by_strategy_profile[profile][label] += 1
        if len(samples) < 50:
            samples.append(sample)
    bad_count = int(label_counts.get("bad_accept", 0) + label_counts.get("bad_reject", 0))
    result = {
        "summary": {
            "sample_count": sample_count,
            "min_sample": sample_floor,
            "parameter_advice_allowed": False,
            "shadow_dry_run_only": True,
            "label_counts": dict(sorted(label_counts.items())),
            "bad_decision_count": bad_count,
            "inconclusive_count": int(label_counts.get("inconclusive", 0)),
            "instrument_identity_ready_count": instrument_identity_ready_count,
            "instrument_identity_missing_count": instrument_identity_missing_count,
            "has_instrument_identity": instrument_identity_ready_count > 0,
            "strategy_profile_ready_count": strategy_profile_ready_count,
            "strategy_profile_missing_count": strategy_profile_missing_count,
            "outcome_ready_count": outcome_ready_count,
            "outcome_missing_count": outcome_missing_count,
            "usable_mark_ready_count": usable_mark_ready_count,
            "usable_mark_missing_count": usable_mark_missing_count,
            "source_kind_counts": dict(sorted(source_kind_counts.items())),
        },
        "by_strategy_profile": {profile: dict(sorted(counts.items())) for profile, counts in sorted(by_strategy_profile.items())},
        "samples": samples,
    }
    advice_gate = parameter_advice_gate(result)
    review_readiness = review_readiness_gate(result, advice_gate=advice_gate)
    result["summary"]["parameter_advice_allowed"] = advice_gate["parameter_advice_allowed"]
    result["summary"]["review_readiness_status"] = review_readiness["status"]
    result["summary"]["manual_strategy_review_ready"] = review_readiness["manual_strategy_review_ready"]
    return result


def parameter_advice_gate(quality: dict[str, Any]) -> dict[str, Any]:
    summary = quality.get("summary") if isinstance(quality, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    label_counts_raw = summary.get("label_counts")
    label_counts = label_counts_raw if isinstance(label_counts_raw, dict) else {}
    by_profile_raw = quality.get("by_strategy_profile") if isinstance(quality, dict) else {}
    by_profile = by_profile_raw if isinstance(by_profile_raw, dict) else {}
    sample_count = int(summary.get("sample_count") or 0)
    min_sample = max(1, int(summary.get("min_sample") or 1))
    bad_accept_count = int(label_counts.get("bad_accept") or 0)
    bad_reject_count = int(label_counts.get("bad_reject") or 0)
    bad_decision_count = bad_accept_count + bad_reject_count
    inconclusive_count = int(label_counts.get("inconclusive") or 0)
    inconclusive_rate = round(inconclusive_count / sample_count, 4) if sample_count > 0 else None
    instrument_identity_ready_count = int(summary.get("instrument_identity_ready_count") or 0)
    instrument_identity_missing_count = int(summary.get("instrument_identity_missing_count") or 0)
    strategy_profile_ready_count = int(summary.get("strategy_profile_ready_count") or 0)
    strategy_profile_missing_count = int(summary.get("strategy_profile_missing_count") or 0)
    outcome_ready_count = int(summary.get("outcome_ready_count") or 0)
    outcome_missing_count = int(summary.get("outcome_missing_count") or 0)
    usable_mark_ready_count = int(summary.get("usable_mark_ready_count") or 0)
    usable_mark_missing_count = int(summary.get("usable_mark_missing_count") or 0)
    source_kind_counts_raw = summary.get("source_kind_counts")
    source_kind_counts = source_kind_counts_raw if isinstance(source_kind_counts_raw, dict) else {}
    strategy_profiles = sorted(profile for profile in by_profile if profile and profile != "unknown")
    sample_floor_met = sample_count >= min_sample
    has_strategy_profile_breakdown = bool(strategy_profiles)
    has_bad_decision_signal = bad_decision_count > 0
    inconclusive_too_high = bool(inconclusive_rate is not None and inconclusive_rate > 0.50)
    candidate_universe_missing = sample_count <= 0
    trace_only_evidence = (
        sample_count > 0
        and int(source_kind_counts.get("candidate_csv") or 0) == 0
        and int(source_kind_counts.get("filter_decision") or 0) > 0
    )
    blockers: list[str] = []
    if candidate_universe_missing:
        blockers.append("candidate_universe_missing")
    elif not sample_floor_met:
        blockers.append("sample_size_below_min_sample")
    if sample_floor_met and instrument_identity_ready_count < min_sample:
        blockers.append("instrument_identity_missing")
    if not has_strategy_profile_breakdown:
        blockers.append("strategy_profile_breakdown_missing")
    elif sample_floor_met and strategy_profile_ready_count < min_sample:
        blockers.append("strategy_profile_missing")
    if sample_floor_met and trace_only_evidence:
        blockers.append("trace_only_evidence")
    if sample_floor_met and usable_mark_ready_count < min_sample:
        blockers.append("usable_mark_path_missing")
    if sample_floor_met and outcome_ready_count < min_sample:
        blockers.append("outcome_fact_missing")
    if not has_bad_decision_signal:
        blockers.append("bad_decision_signal_missing")
    if inconclusive_too_high:
        blockers.append("inconclusive_rate_too_high")
    allowed = not blockers
    return {
        "status": "ready_for_parameter_review" if allowed else "not_ready_for_parameter_review",
        "parameter_advice_allowed": allowed,
        "shadow_dry_run_only": True,
        "sample_count": sample_count,
        "min_sample": min_sample,
        "sample_floor_met": sample_floor_met,
        "candidate_universe_missing": candidate_universe_missing,
        "strategy_profiles": strategy_profiles,
        "has_strategy_profile_breakdown": has_strategy_profile_breakdown,
        "instrument_identity_ready_count": instrument_identity_ready_count,
        "instrument_identity_missing_count": instrument_identity_missing_count,
        "has_instrument_identity": instrument_identity_ready_count > 0,
        "strategy_profile_ready_count": strategy_profile_ready_count,
        "strategy_profile_missing_count": strategy_profile_missing_count,
        "trace_only_evidence": trace_only_evidence,
        "usable_mark_ready_count": usable_mark_ready_count,
        "usable_mark_missing_count": usable_mark_missing_count,
        "outcome_ready_count": outcome_ready_count,
        "outcome_missing_count": outcome_missing_count,
        "bad_accept_count": bad_accept_count,
        "bad_reject_count": bad_reject_count,
        "bad_decision_count": bad_decision_count,
        "has_bad_decision_signal": has_bad_decision_signal,
        "inconclusive_count": inconclusive_count,
        "inconclusive_rate": inconclusive_rate,
        "inconclusive_too_high": inconclusive_too_high,
        "blockers": blockers,
    }


def review_readiness_gate(
    quality: dict[str, Any],
    *,
    advice_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility-safe review-readiness view over the legacy advice gate."""

    gate = advice_gate if isinstance(advice_gate, dict) else parameter_advice_gate(quality)
    ready = bool(gate.get("parameter_advice_allowed"))
    return {
        "status": "ready_for_manual_strategy_review" if ready else "not_ready_for_manual_strategy_review",
        "manual_strategy_review_ready": ready,
        "review_scope": "offline_strategy_evidence_review",
        "shadow_dry_run_only": bool(gate.get("shadow_dry_run_only", True)),
        "sample_count": int(gate.get("sample_count") or 0),
        "min_sample": int(gate.get("min_sample") or 0),
        "sample_floor_met": bool(gate.get("sample_floor_met")),
        "candidate_universe_missing": bool(gate.get("candidate_universe_missing")),
        "strategy_profiles": list(gate.get("strategy_profiles") or []),
        "has_strategy_profile_breakdown": bool(gate.get("has_strategy_profile_breakdown")),
        "instrument_identity_ready_count": int(gate.get("instrument_identity_ready_count") or 0),
        "instrument_identity_missing_count": int(gate.get("instrument_identity_missing_count") or 0),
        "strategy_profile_ready_count": int(gate.get("strategy_profile_ready_count") or 0),
        "strategy_profile_missing_count": int(gate.get("strategy_profile_missing_count") or 0),
        "trace_only_evidence": bool(gate.get("trace_only_evidence")),
        "usable_mark_ready_count": int(gate.get("usable_mark_ready_count") or 0),
        "usable_mark_missing_count": int(gate.get("usable_mark_missing_count") or 0),
        "outcome_ready_count": int(gate.get("outcome_ready_count") or 0),
        "outcome_missing_count": int(gate.get("outcome_missing_count") or 0),
        "bad_accept_count": int(gate.get("bad_accept_count") or 0),
        "bad_reject_count": int(gate.get("bad_reject_count") or 0),
        "bad_decision_count": int(gate.get("bad_decision_count") or 0),
        "has_bad_decision_signal": bool(gate.get("has_bad_decision_signal")),
        "inconclusive_count": int(gate.get("inconclusive_count") or 0),
        "inconclusive_rate": gate.get("inconclusive_rate"),
        "inconclusive_too_high": bool(gate.get("inconclusive_too_high")),
        "blockers": list(gate.get("blockers") or []),
        "compatibility": {
            "legacy_field": "parameter_advice_gate",
            "legacy_status": gate.get("status"),
            "legacy_allowed_field": "parameter_advice_allowed",
            "legacy_allowed": ready,
        },
    }


def _decision_quality_sample(
    *,
    candidate: dict[str, Any],
    outcome: dict[str, Any] | None,
    max_adverse_pnl: float | None,
    force_inconclusive: bool,
    sample_floor: int,
    sample_count: int,
) -> dict[str, Any]:
    key = instrument_key(candidate)
    status = normal_status(candidate.get("status"))
    profile = _strategy_profile(candidate)
    family = _strategy_family(candidate)
    option_pnl = first_float(outcome or {}, "realized_pnl", "counterfactual_pnl", "pnl", "net_pnl") if outcome else None
    lifecycle_pnl = first_float(outcome or {}, *LIFECYCLE_PNL_FIELDS) if outcome else None
    decision_pnl = _decision_outcome_pnl(outcome)
    base = {
        "instrument_key": key,
        "symbol": text(candidate.get("symbol")),
        "status": status,
        "strategy_family": family,
        "strategy_profile": profile,
        "realized_pnl": option_pnl,
        "lifecycle_pnl": lifecycle_pnl,
        "decision_pnl": decision_pnl,
        "max_adverse_pnl": max_adverse_pnl,
    }
    if force_inconclusive:
        return {
            **base,
            "label": "inconclusive",
            "confidence": "low",
            "reasons": ["sample_size_below_min_sample"],
            "sample_count": sample_count,
            "min_sample": sample_floor,
        }
    if profile not in {"insurance_underwriting", "return_first"}:
        return {**base, "label": "inconclusive", "confidence": "low", "reasons": ["strategy_profile_missing_or_unknown"]}
    if status not in {"accepted", "rejected", "post_filtered", "ranked_below"}:
        return {**base, "label": "inconclusive", "confidence": "low", "reasons": ["decision_status_not_classifiable"]}
    if outcome is None:
        return {**base, "label": "inconclusive", "confidence": "low", "reasons": ["outcome_fact_missing"]}

    ex_ante = _ex_ante_quality(candidate, profile=profile)
    path = _path_quality(
        candidate,
        outcome=outcome,
        pnl=decision_pnl,
        max_adverse_pnl=max_adverse_pnl,
    )
    rejected_like = status in {"rejected", "post_filtered", "ranked_below"}
    if status == "accepted":
        if ex_ante["status"] == "fail" or path["status"] == "fail":
            return {
                **base,
                "label": "bad_accept",
                "confidence": _quality_confidence(ex_ante, path),
                "reasons": ex_ante["reasons"] + path["reasons"],
            }
        if ex_ante["status"] == "unknown":
            return {**base, "label": "inconclusive", "confidence": "low", "reasons": ex_ante["reasons"] + path["reasons"]}
        if path["status"] == "unknown":
            return {**base, "label": "inconclusive", "confidence": "low", "reasons": ex_ante["reasons"] + path["reasons"]}
        return {
            **base,
            "label": "good_accept",
            "confidence": _quality_confidence(ex_ante, path),
            "reasons": ex_ante["reasons"] + path["reasons"],
        }
    if rejected_like:
        if ex_ante["status"] == "fail":
            return {
                **base,
                "label": "good_reject",
                "confidence": _quality_confidence(ex_ante, path),
                "reasons": ex_ante["reasons"],
            }
        if ex_ante["status"] == "pass" and path["status"] == "unknown":
            return {**base, "label": "inconclusive", "confidence": "low", "reasons": ex_ante["reasons"] + path["reasons"]}
        if ex_ante["status"] == "pass" and decision_pnl is not None and decision_pnl > 0:
            return {
                **base,
                "label": "bad_reject",
                "confidence": _quality_confidence(ex_ante, path),
                "reasons": ex_ante["reasons"] + ["positive_outcome_after_ex_ante_pass"],
            }
        return {**base, "label": "inconclusive", "confidence": "low", "reasons": ex_ante["reasons"] + path["reasons"]}
    return {**base, "label": "inconclusive", "confidence": "low", "reasons": ["decision_status_not_classifiable"]}


def _strategy_profile(candidate: dict[str, Any]) -> str:
    raw = text(
        candidate.get("strategy_profile")
        or candidate.get("profile")
        or candidate.get("strategy_mode")
    ).lower()
    if raw in {"insurance_underwriting", "short_vol", "short-vol", "volatility_premium", "vol_premium"}:
        return "insurance_underwriting"
    if raw in {"return_first", "return-first", "income", "yield_first"}:
        return "return_first"
    return "unknown"


def _has_instrument_identity(candidate: dict[str, Any]) -> bool:
    if text(candidate.get("contract_symbol") or candidate.get("option_symbol")):
        return True
    return bool(
        text(candidate.get("symbol") or candidate.get("underlying_symbol"))
        and text(candidate.get("option_type") or candidate.get("mode"))
        and text(candidate.get("expiration") or candidate.get("exp"))
        and text(candidate.get("strike"))
    )


def _strategy_family(candidate: dict[str, Any]) -> str:
    raw = text(candidate.get("strategy_family") or candidate.get("function") or candidate.get("strategy_name")).lower()
    if raw in {"sell_put", "put"}:
        return "sell_put"
    if raw in {"sell_call", "covered_call", "call"}:
        return "sell_call"
    if raw in {"combo_yield", "yield_enhancement", "income_upside_enhancement", "vol_convexity_enhancement"}:
        return "combo_yield"
    return raw or "unknown"


def _ex_ante_quality(candidate: dict[str, Any], *, profile: str) -> dict[str, Any]:
    if profile == "insurance_underwriting":
        return _underwriting_ex_ante_quality(candidate)
    if profile == "return_first":
        return _return_first_ex_ante_quality(candidate)
    return {"status": "unknown", "reasons": ["strategy_profile_missing_or_unknown"]}


def _underwriting_ex_ante_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    checked = 0
    iv_rv = first_float(candidate, "iv_rv_ratio", "iv_to_rv_ratio")
    min_iv_rv = first_float(candidate, "min_iv_rv_ratio", "short_vol_min_iv_rv_ratio") or 1.10
    if iv_rv is not None:
        checked += 1
        if iv_rv < min_iv_rv:
            reasons.append("iv_rv_ratio_below_minimum")
    iv_minus_rv = first_float(candidate, "iv_minus_rv", "iv_rv_spread", "iv_minus_realized_volatility")
    min_iv_minus_rv = first_float(candidate, "min_iv_minus_rv", "short_vol_min_iv_minus_rv") or 0.05
    if iv_minus_rv is not None:
        checked += 1
        if iv_minus_rv < min_iv_minus_rv:
            reasons.append("iv_minus_rv_below_minimum")
    spread = first_float(candidate, "spread_ratio", "bid_ask_spread_ratio")
    max_spread = first_float(candidate, "max_spread_ratio") or 0.30
    if spread is not None:
        checked += 1
        if spread > max_spread:
            reasons.append("spread_ratio_above_maximum")
    event_reason = _event_risk_reason(candidate)
    if event_reason:
        checked += 1
        reasons.append(event_reason)
    if reasons:
        return {"status": "fail", "reasons": reasons}
    if checked <= 0:
        return {"status": "unknown", "reasons": ["short_vol_ex_ante_fields_missing"]}
    return {"status": "pass", "reasons": ["short_vol_ex_ante_quality_pass"]}


def _return_first_ex_ante_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    checked = 0
    net_income = first_float(candidate, "net_income", "premium", "net_premium", "premium_received")
    if net_income is not None:
        checked += 1
        if net_income <= 0:
            reasons.append("net_income_not_positive")
    annualized = first_float(candidate, "annualized_net_return_on_cash_basis", "annualized_return", "annualized_net_return")
    if annualized is not None:
        checked += 1
        if annualized <= 0:
            reasons.append("annualized_return_not_positive")
    dte = first_float(candidate, "dte")
    if dte is not None:
        checked += 1
        if dte <= 0:
            reasons.append("dte_not_positive")
    spread = first_float(candidate, "spread_ratio", "bid_ask_spread_ratio")
    max_spread = first_float(candidate, "max_spread_ratio") or 0.50
    if spread is not None:
        checked += 1
        if spread > max_spread:
            reasons.append("spread_ratio_above_maximum")
    if reasons:
        return {"status": "fail", "reasons": reasons}
    if checked <= 0:
        return {"status": "unknown", "reasons": ["return_first_ex_ante_fields_missing"]}
    return {"status": "pass", "reasons": ["return_first_ex_ante_quality_pass"]}


def _event_risk_reason(candidate: dict[str, Any]) -> str | None:
    raw_values = [
        candidate.get("event_risk_status"),
        candidate.get("event_status"),
        candidate.get("event_source_status"),
        candidate.get("event_risk"),
        candidate.get("has_event_before_expiry"),
    ]
    text_values = [text(value).lower() for value in raw_values if text(value)]
    for value in text_values:
        if value in {"true", "yes", "1"}:
            return "event_risk_before_expiry"
        if any(token in value for token in ("unavailable", "missing", "failed", "source_fail", "before_expiry", "event_risk")):
            return "event_risk_not_acceptable"
    return None


def _decision_outcome_pnl(outcome: dict[str, Any] | None) -> float | None:
    if not outcome:
        return None
    outcome_name = text(outcome.get("outcome")).lower()
    if outcome_name in WHEEL_TRANSITION_OUTCOMES:
        return first_float(outcome, *LIFECYCLE_PNL_FIELDS)
    return first_float(outcome, "realized_pnl", "counterfactual_pnl", "pnl", "net_pnl")


def _path_quality(
    candidate: dict[str, Any],
    *,
    outcome: dict[str, Any] | None,
    pnl: float | None,
    max_adverse_pnl: float | None,
) -> dict[str, Any]:
    outcome_name = text((outcome or {}).get("outcome")).lower()
    if outcome_name in WHEEL_TRANSITION_OUTCOMES and pnl is None:
        return {
            "status": "unknown",
            "reasons": [f"{outcome_name}_lifecycle_pnl_missing"],
        }
    premium = first_float(
        candidate,
        "net_income",
        "net_credit",
        "entry_credit",
        "premium_received_gross",
        "premium_income",
        "premium",
    )
    reasons: list[str] = []
    if outcome_name in WHEEL_TRANSITION_OUTCOMES:
        max_adverse_pnl = None
    if premium is not None and premium > 0:
        if max_adverse_pnl is not None and max_adverse_pnl < 0 and abs(max_adverse_pnl) > premium * 3:
            reasons.append("path_adverse_loss_exceeds_premium_budget")
        if pnl is not None and pnl < 0 and abs(pnl) > premium * 3:
            reasons.append("realized_loss_exceeds_premium_budget")
    if reasons:
        return {"status": "fail", "reasons": reasons}
    return {"status": "pass", "reasons": ["path_quality_not_disqualifying"]}


def _quality_confidence(ex_ante: dict[str, Any], path: dict[str, Any]) -> str:
    if ex_ante.get("status") == "unknown":
        return "low"
    if path.get("status") == "unknown":
        return "medium"
    return "medium"


def insurance_bucket_stats(
    candidate_by_key: dict[str, dict[str, Any]],
    adverse_by_key: dict[str, float],
    outcomes: list[dict[str, Any]],
    *,
    min_sample: int,
) -> dict[str, Any]:
    dimensions: dict[str, tuple[str, Any]] = {
        "dte": ("dte", _dte_bucket),
        "abs_delta": ("abs_delta", _delta_bucket),
        "iv_rv_ratio": ("iv_rv_ratio", _iv_rv_bucket),
        "spread_ratio": ("spread_ratio", _spread_bucket),
        "single_trade_concentration": ("single_trade_concentration", _concentration_bucket),
    }
    out: dict[str, Any] = {}
    for dimension, (field, bucket_fn) in dimensions.items():
        bucketed: dict[str, dict[str, Any]] = defaultdict(lambda: {"all": _empty_insurance_group(), "by_status": defaultdict(_empty_insurance_group)})
        for row in outcomes:
            key = instrument_key(row)
            candidate = candidate_by_key.get(key)
            if not candidate:
                continue
            label = bucket_fn(float_or_none(candidate.get(field)))
            sample = _insurance_sample(candidate=candidate, outcome=row, instrument_key=key, max_adverse_pnl=adverse_by_key.get(key))
            payload = bucketed[label]
            _record_insurance_payload(payload["all"], sample)
            _record_insurance_payload(payload["by_status"][sample["status"]], sample)
        out[dimension] = {
            label: {
                **_summarize_insurance_group(payload["all"], min_sample=min_sample),
                "by_status": _insurance_payload(payload["by_status"], min_sample=min_sample),
            }
            for label, payload in sorted(bucketed.items())
        }
    return out


def _shadow_recommendation(
    *,
    candidate_count: int,
    rejected_count: int,
    filter_decision_count: int,
    mark_count: int,
    usable_mark_count: int,
    outcome_count: int,
    min_sample: int,
) -> dict[str, Any]:
    base = {"writes_runtime_config": False, "writes_trade_state": False, "sends_notifications": False}
    if candidate_count <= 0:
        return {**base, "status": "not_ready", "reason": "candidate_universe_missing", "sample_size": 0, "min_sample": min_sample}
    if candidate_count < min_sample:
        return {
            **base,
            "status": "not_ready",
            "reason": "candidate_snapshot_count_below_min_sample",
            "sample_size": candidate_count,
            "min_sample": min_sample,
        }
    if filter_decision_count <= 0 or rejected_count <= 0:
        return {
            **base,
            "status": "evidence_incomplete",
            "reason": "rejected_universe_missing",
            "sample_size": candidate_count,
            "min_sample": min_sample,
            "survivorship_bias_risk": "high",
        }
    if mark_count <= 0:
        return {
            **base,
            "status": "not_ready",
            "reason": "mark_path_snapshots_missing",
            "sample_size": candidate_count,
            "min_sample": min_sample,
        }
    if usable_mark_count <= 0:
        return {
            **base,
            "status": "not_ready",
            "reason": "usable_mark_path_snapshots_missing",
            "sample_size": candidate_count,
            "min_sample": min_sample,
        }
    if outcome_count <= 0:
        return {
            **base,
            "status": "not_ready",
            "reason": "outcome_facts_missing",
            "sample_size": candidate_count,
            "min_sample": min_sample,
        }
    return {
        **base,
        "status": "needs_human_review",
        "reason": "shadow_replay_ready_for_manual_review",
        "sample_size": candidate_count,
        "min_sample": min_sample,
    }


def _evidence_checks(
    *,
    candidate_count: int,
    rejected_count: int,
    filter_decision_count: int,
    mark_count: int,
    usable_mark_count: int,
    outcome_count: int,
) -> dict[str, Any]:
    final_candidates_only = candidate_count > 0 and filter_decision_count <= 0 and rejected_count <= 0
    return {
        "has_candidate_universe": candidate_count > 0,
        "has_filter_decisions": filter_decision_count > 0,
        "has_rejected_or_post_filtered_samples": rejected_count > 0,
        "has_mark_path_snapshots": mark_count > 0,
        "has_usable_mark_path_snapshots": usable_mark_count > 0,
        "has_outcome_facts": outcome_count > 0,
        "final_candidates_only": final_candidates_only,
        "survivorship_bias_risk": "high" if final_candidates_only or rejected_count <= 0 else ("medium" if outcome_count <= 0 else "low"),
    }


def _bucket_stats(rows: list[dict[str, Any]], key: str, bucket_fn: Any) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "accepted": 0, "rejected": 0, "post_filtered": 0, "symbols": Counter()})
    for row in rows:
        value = float_or_none(row.get(key))
        label = bucket_fn(value)
        bucket = buckets[label]
        bucket["count"] += 1
        status = str(row.get("status") or "")
        if status == "accepted":
            bucket["accepted"] += 1
        elif status == "post_filtered":
            bucket["post_filtered"] += 1
        elif status in {"rejected", "ranked_below"}:
            bucket["rejected"] += 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            bucket["symbols"][symbol] += 1
    return {
        label: {
            "count": int(payload["count"]),
            "accepted": int(payload["accepted"]),
            "rejected": int(payload["rejected"]),
            "post_filtered": int(payload["post_filtered"]),
            "top_symbols": dict(payload["symbols"].most_common(10)),
        }
        for label, payload in sorted(buckets.items())
    }


def _candidate_by_instrument(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    priority = {"accepted": 4, "rejected": 3, "post_filtered": 3, "ranked_below": 2, "unknown": 1}
    for row in candidates:
        key = instrument_key(row)
        if not key:
            continue
        status = normal_status(row.get("status"))
        current = out.get(key)
        current_status = normal_status(current.get("status")) if isinstance(current, dict) else "unknown"
        if current is None or priority.get(status, 0) > priority.get(current_status, 0):
            out[key] = row
    return out


def _candidate_status_by_instrument(candidates: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    priority = {"accepted": 4, "rejected": 3, "post_filtered": 3, "ranked_below": 2, "unknown": 1}
    for row in candidates:
        key = instrument_key(row)
        if not key:
            continue
        status = normal_status(row.get("status"))
        current = out.get(key)
        if current is None or priority.get(status, 0) > priority.get(current, 0):
            out[key] = status
    return out


def _empty_outcome_group() -> dict[str, Any]:
    return {"outcome_count": 0, "instruments": set(), "pnl_values": [], "outcomes": Counter()}


def _record_outcome_payload(payload: dict[str, Any], *, instrument_key: str, pnl: float | None, outcome: str) -> None:
    payload["outcome_count"] += 1
    if instrument_key:
        payload["instruments"].add(instrument_key)
    if pnl is not None:
        payload["pnl_values"].append(pnl)
    if outcome:
        payload["outcomes"][outcome] += 1


def _empty_insurance_group() -> dict[str, Any]:
    return {
        "outcome_count": 0,
        "instruments": set(),
        "outcomes": Counter(),
        "pnl_basis_counts": Counter(),
        "lifecycle_transition_count": 0,
        "lifecycle_pnl_observation_count": 0,
        "lifecycle_pnl_values": [],
        "lifecycle_return_on_capital_values": [],
        "premium_values": [],
        "pnl_values": [],
        "pnl_premium_pairs": [],
        "liability_cost_pairs": [],
        "capital_pairs": [],
        "return_on_capital_values": [],
        "adverse_values": [],
        "adverse_premium_pairs": [],
        "adverse_return_on_capital_values": [],
    }


def _insurance_sample(
    *,
    candidate: dict[str, Any],
    outcome: dict[str, Any],
    instrument_key: str,
    max_adverse_pnl: float | None,
) -> dict[str, Any]:
    mode = _insurance_mode(candidate, outcome)
    status = normal_status(candidate.get("status") or outcome.get("candidate_status"))
    outcome_label = text(outcome.get("outcome") or outcome.get("settlement") or outcome.get("status")).lower()
    lifecycle_transition = outcome_label in WHEEL_TRANSITION_OUTCOMES
    if lifecycle_transition:
        pnl = first_float(outcome, *LIFECYCLE_PNL_FIELDS)
        value_basis = "native"
        pnl_basis = "wheel_lifecycle" if pnl is not None else "wheel_lifecycle_missing"
    else:
        pnl, value_basis = _outcome_pnl_with_basis(outcome)
        pnl_basis = f"option_{value_basis}" if pnl is not None else "option_missing"
    premium = _entry_premium(candidate, value_basis=value_basis)
    capital = _capital_at_risk(candidate, mode=mode, value_basis=value_basis)
    liability_cost = None
    if premium is not None and premium > 0 and pnl is not None:
        liability_cost = max(premium - pnl, 0.0)
    return {
        "instrument_key": instrument_key,
        "status": status,
        "mode": mode,
        "outcome": outcome_label,
        "pnl_basis": pnl_basis,
        "lifecycle_transition": lifecycle_transition,
        "premium": premium if premium is not None and premium > 0 else None,
        "pnl": pnl,
        "liability_cost": liability_cost,
        "capital": capital if capital is not None and capital > 0 else None,
        "max_adverse_pnl": max_adverse_pnl,
    }


def _record_insurance_payload(payload: dict[str, Any], sample: dict[str, Any]) -> None:
    payload["outcome_count"] += 1
    key = text(sample.get("instrument_key"))
    if key:
        payload["instruments"].add(key)
    outcome = text(sample.get("outcome"))
    if outcome:
        payload["outcomes"][outcome] += 1
    payload["pnl_basis_counts"][text(sample.get("pnl_basis")) or "unknown"] += 1
    if sample.get("lifecycle_transition"):
        payload["lifecycle_transition_count"] += 1
        if sample.get("pnl") is not None:
            payload["lifecycle_pnl_observation_count"] += 1
            payload["lifecycle_pnl_values"].append(float(sample["pnl"]))
            if sample.get("capital") is not None:
                payload["lifecycle_return_on_capital_values"].append(
                    float(sample["pnl"]) / float(sample["capital"])
                )
    premium = sample.get("premium")
    pnl = sample.get("pnl")
    liability_cost = sample.get("liability_cost")
    capital = sample.get("capital")
    adverse = sample.get("max_adverse_pnl")
    if premium is not None:
        payload["premium_values"].append(float(premium))
    if pnl is not None:
        payload["pnl_values"].append(float(pnl))
    if premium is not None and pnl is not None:
        payload["pnl_premium_pairs"].append((float(pnl), float(premium)))
    if premium is not None and liability_cost is not None:
        payload["liability_cost_pairs"].append((float(liability_cost), float(premium)))
    if premium is not None and capital is not None:
        payload["capital_pairs"].append((float(premium), float(capital)))
    if pnl is not None and capital is not None:
        payload["return_on_capital_values"].append(float(pnl) / float(capital))
    if adverse is not None:
        adverse_value = float(adverse)
        payload["adverse_values"].append(adverse_value)
        if capital is not None:
            payload["adverse_return_on_capital_values"].append(adverse_value / float(capital))
        if premium is not None and adverse_value < 0:
            payload["adverse_premium_pairs"].append((abs(adverse_value), float(premium)))


def _insurance_payload(grouped: dict[str, dict[str, Any]], *, min_sample: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, payload in sorted(grouped.items()):
        out[label] = _summarize_insurance_group(payload, min_sample=min_sample)
    return out


def _summarize_insurance_group(payload: dict[str, Any], *, min_sample: int) -> dict[str, Any]:
    premium_values = list(payload["premium_values"])
    pnl_values = list(payload["pnl_values"])
    pnl_premium_pairs = list(payload["pnl_premium_pairs"])
    liability_cost_pairs = list(payload["liability_cost_pairs"])
    capital_pairs = list(payload["capital_pairs"])
    return_on_capital_values = list(payload["return_on_capital_values"])
    adverse_values = list(payload["adverse_values"])
    adverse_premium_pairs = list(payload["adverse_premium_pairs"])
    lifecycle_pnl_values = list(payload["lifecycle_pnl_values"])
    lifecycle_return_on_capital_values = list(payload["lifecycle_return_on_capital_values"])
    adverse_return_on_capital_values = list(payload["adverse_return_on_capital_values"])
    outcome_count = int(payload["outcome_count"])
    negative_margin_count = sum(1 for value in pnl_values if value < 0)
    liability_cost_count = sum(1 for cost, _premium in liability_cost_pairs if cost > 0)
    premium_for_pnl = sum(premium for _pnl, premium in pnl_premium_pairs)
    pnl_with_premium = sum(pnl for pnl, _premium in pnl_premium_pairs)
    liability_cost_total = sum(cost for cost, _premium in liability_cost_pairs)
    premium_for_cost = sum(premium for _cost, premium in liability_cost_pairs)
    premium_for_capital = sum(premium for premium, _capital in capital_pairs)
    capital_total = sum(capital for _premium, capital in capital_pairs)
    adverse_loss_total = sum(loss for loss, _premium in adverse_premium_pairs)
    premium_for_adverse = sum(premium for _loss, premium in adverse_premium_pairs)
    outcomes = payload["outcomes"]
    assignment_count = int(outcomes.get("assigned_at_expiry", 0))
    called_away_count = int(outcomes.get("called_away_at_expiry", 0))
    expired_worthless_count = int(outcomes.get("expired_worthless", 0))
    exercise_count = assignment_count + called_away_count + int(outcomes.get("expired_in_the_money", 0))
    lifecycle_transition_count = int(payload["lifecycle_transition_count"])
    lifecycle_pnl_observation_count = int(payload["lifecycle_pnl_observation_count"])
    return {
        "instrument_count": len(payload["instruments"]),
        "outcome_count": outcome_count,
        "premium_observation_count": len(premium_values),
        "premium_collected_total": sum(premium_values) if premium_values else None,
        "premium_collected_avg": (sum(premium_values) / len(premium_values)) if premium_values else None,
        "pnl_observation_count": len(pnl_values),
        "pnl_basis_counts": dict(payload["pnl_basis_counts"].most_common()),
        "realized_pnl_total": sum(pnl_values) if pnl_values else None,
        "realized_pnl_avg": (sum(pnl_values) / len(pnl_values)) if pnl_values else None,
        "underwriting_margin": (pnl_with_premium / premium_for_pnl) if premium_for_pnl > 0 else None,
        "liability_cost_observation_count": len(liability_cost_pairs),
        "liability_cost_total": liability_cost_total if liability_cost_pairs else None,
        "loss_ratio": (liability_cost_total / premium_for_cost) if premium_for_cost > 0 else None,
        "liability_cost_count": liability_cost_count,
        "liability_cost_rate": (liability_cost_count / len(liability_cost_pairs)) if liability_cost_pairs else None,
        "negative_margin_count": negative_margin_count,
        "negative_margin_rate": (negative_margin_count / len(pnl_values)) if pnl_values else None,
        "lifecycle_transition_count": lifecycle_transition_count,
        "lifecycle_pnl_observation_count": lifecycle_pnl_observation_count,
        "lifecycle_pnl_missing_count": lifecycle_transition_count - lifecycle_pnl_observation_count,
        "lifecycle_pnl_total": sum(lifecycle_pnl_values) if lifecycle_pnl_values else None,
        "lifecycle_pnl_avg": (
            sum(lifecycle_pnl_values) / len(lifecycle_pnl_values) if lifecycle_pnl_values else None
        ),
        "lifecycle_return_on_capital_observation_count": len(lifecycle_return_on_capital_values),
        "lifecycle_return_on_capital_avg": (
            sum(lifecycle_return_on_capital_values) / len(lifecycle_return_on_capital_values)
            if lifecycle_return_on_capital_values
            else None
        ),
        "exercise_count": exercise_count,
        "exercise_rate": (exercise_count / outcome_count) if outcome_count > 0 else None,
        "assignment_count": assignment_count,
        "assignment_rate": (assignment_count / outcome_count) if outcome_count > 0 else None,
        "called_away_count": called_away_count,
        "called_away_rate": (called_away_count / outcome_count) if outcome_count > 0 else None,
        "expired_worthless_count": expired_worthless_count,
        "expired_worthless_rate": (expired_worthless_count / outcome_count) if outcome_count > 0 else None,
        "capital_observation_count": len(capital_pairs),
        "capital_at_risk_total": capital_total if capital_pairs else None,
        "premium_to_capital": (premium_for_capital / capital_total) if capital_total > 0 else None,
        "return_on_capital_observation_count": len(return_on_capital_values),
        "return_on_capital_avg": (
            sum(return_on_capital_values) / len(return_on_capital_values) if return_on_capital_values else None
        ),
        "tail_risk": _empirical_tail_risk(return_on_capital_values, min_sample=min_sample),
        "max_adverse_pnl_observation_count": len(adverse_values),
        "max_adverse_pnl_worst": min(adverse_values) if adverse_values else None,
        "max_adverse_return_on_capital_observation_count": len(adverse_return_on_capital_values),
        "max_adverse_return_on_capital_worst": (
            min(adverse_return_on_capital_values) if adverse_return_on_capital_values else None
        ),
        "path_adverse_loss_total": adverse_loss_total if adverse_premium_pairs else None,
        "path_adverse_loss_to_premium": (adverse_loss_total / premium_for_adverse) if premium_for_adverse > 0 else None,
        "outcome_counts": dict(outcomes.most_common(20)),
    }


def _empirical_tail_risk(values: list[float], *, min_sample: int) -> dict[str, Any]:
    required = max(MIN_EMPIRICAL_TAIL_SAMPLES, int(min_sample))
    if len(values) < required:
        return {
            "status": "not_evaluable",
            "metric": "return_on_capital",
            "confidence_level": 0.90,
            "required_observation_count": required,
            "observation_count": len(values),
            "tail_observation_count": 0,
            "var_90": None,
            "cvar_90": None,
        }
    ordered = sorted(values)
    tail_count = max(1, (len(ordered) + 9) // 10)
    tail = ordered[:tail_count]
    return {
        "status": "evaluable",
        "metric": "return_on_capital",
        "confidence_level": 0.90,
        "required_observation_count": required,
        "observation_count": len(ordered),
        "tail_observation_count": tail_count,
        "var_90": tail[-1],
        "cvar_90": sum(tail) / tail_count,
    }


def _max_adverse_pnl_by_instrument(marks: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in marks:
        key = instrument_key(row)
        if not key:
            continue
        pnl = first_float(row, "unrealized_pnl", "counterfactual_pnl", "pnl", "mark_pnl")
        if pnl is None:
            continue
        current = out.get(key)
        if current is None or pnl < current:
            out[key] = pnl
    return out


def _insurance_mode(candidate: dict[str, Any], outcome: dict[str, Any]) -> str:
    mode = text(candidate.get("option_type") or candidate.get("mode") or outcome.get("option_type") or outcome.get("mode")).lower()
    if mode in {"put", "call"}:
        return mode
    return "unknown"


def _entry_premium(candidate: dict[str, Any], *, value_basis: str) -> float | None:
    cny_value = first_float(
        candidate,
        "net_income_cny",
        "net_credit_cny",
        "entry_credit_cny",
        "premium_received_gross_cny",
        "premium_income_cny",
        "premium_cny",
    )
    native_value = first_float(
        candidate,
        "net_income",
        "net_credit",
        "entry_credit",
        "premium_received_gross",
        "premium_income",
        "premium",
    )
    if value_basis == "cny" and cny_value is not None:
        return cny_value
    if native_value is not None:
        return native_value
    return cny_value


def _outcome_pnl_with_basis(outcome: dict[str, Any]) -> tuple[float | None, str]:
    cny_value = first_float(outcome, "realized_pnl_cny", "counterfactual_pnl_cny", "pnl_cny", "net_pnl_cny")
    if cny_value is not None:
        return cny_value, "cny"
    return first_float(outcome, "realized_pnl", "counterfactual_pnl", "pnl", "net_pnl"), "native"


def _capital_at_risk(candidate: dict[str, Any], *, mode: str, value_basis: str) -> float | None:
    cny_value = first_float(
        candidate,
        "assignment_notional_cny",
        "cash_required_cny",
        "covered_notional_cny",
        "underlying_notional_cny",
        "capital_at_risk_cny",
    )
    native_value = first_float(
        candidate,
        "cash_required",
        "assignment_notional",
        "covered_notional",
        "underlying_notional",
        "notional",
    )
    inferred_value = _inferred_capital_at_risk(candidate, mode=mode)
    if value_basis == "cny" and cny_value is not None:
        return cny_value
    if native_value is not None:
        return native_value
    if inferred_value is not None:
        return inferred_value
    return cny_value


def _inferred_capital_at_risk(candidate: dict[str, Any], *, mode: str) -> float | None:
    contracts = first_float(candidate, "contracts", "contract_count") or 1.0
    multiplier = first_float(candidate, "multiplier", "contract_multiplier")
    strike = first_float(candidate, "strike")
    spot = first_float(candidate, "spot", "underlying_price")
    if multiplier is None or strike is None:
        return None
    if mode == "call" and spot is not None:
        return spot * multiplier * contracts
    return strike * multiplier * contracts


def _risk_payload(grouped: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for status, payload in sorted(grouped.items()):
        pnl_values = list(payload["pnl_values"])
        out[status] = {
            "instrument_count": len(payload["instruments"]),
            "mark_count": int(payload["mark_count"]),
            "pnl_observation_count": len(pnl_values),
            "max_adverse_pnl": min(pnl_values) if pnl_values else None,
            "max_favorable_pnl": max(pnl_values) if pnl_values else None,
        }
    return out


def _outcome_payload(grouped: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for status, payload in sorted(grouped.items()):
        out[status] = _summarize_outcome_group(payload)
    return out


def _summarize_outcome_group(payload: dict[str, Any]) -> dict[str, Any]:
    pnl_values = list(payload["pnl_values"])
    win_count = sum(1 for value in pnl_values if value > 0)
    loss_count = sum(1 for value in pnl_values if value < 0)
    return {
        "instrument_count": len(payload["instruments"]),
        "outcome_count": int(payload["outcome_count"]),
        "pnl_observation_count": len(pnl_values),
        "realized_pnl_total": sum(pnl_values) if pnl_values else None,
        "realized_pnl_avg": (sum(pnl_values) / len(pnl_values)) if pnl_values else None,
        "win_rate": (win_count / len(pnl_values)) if pnl_values else None,
        "win_count": win_count,
        "loss_count": loss_count,
        "outcome_counts": dict(payload["outcomes"].most_common(20)),
    }


def _evidence_level(candidates: list[dict[str, Any]], decisions: list[dict[str, Any]], marks: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> str:
    usable_marks = [row for row in marks if is_usable_mark(row)]
    if candidates and decisions and usable_marks:
        if any(is_complete_closed_outcome(row) for row in outcomes):
            return "closed_replay"
        if outcomes:
            return "outcome_incomplete"
        return "marked_universe"
    if candidates and decisions:
        return "candidate_and_reject_universe"
    if candidates:
        return "candidate_only"
    if decisions:
        return "reject_trace_only"
    return "limited"


def _dte_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 14:
        return "<14"
    if value < 30:
        return "14-29"
    if value < 45:
        return "30-44"
    if value < 60:
        return "45-59"
    return "60+"


def _delta_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.10:
        return "<0.10"
    if value < 0.15:
        return "0.10-0.15"
    if value < 0.20:
        return "0.15-0.20"
    if value < 0.30:
        return "0.20-0.30"
    return "0.30+"


def _iv_rv_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1.0:
        return "<1.00"
    if value < 1.15:
        return "1.00-1.15"
    if value < 1.30:
        return "1.15-1.30"
    return "1.30+"


def _spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.10:
        return "<0.10"
    if value < 0.20:
        return "0.10-0.20"
    if value < 0.40:
        return "0.20-0.40"
    return "0.40+"


def _concentration_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.03:
        return "<3%"
    if value < 0.05:
        return "3%-5%"
    if value < 0.08:
        return "5%-8%"
    return "8%+"
