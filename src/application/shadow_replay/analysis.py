from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

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
from src.application.shadow_replay.settlement import is_usable_mark


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
        "outcome_by_bucket": outcome_bucket_stats(candidate_snapshots, outcome_facts),
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
    if candidates and decisions and usable_marks and outcomes:
        return "closed_replay"
    if candidates and decisions and usable_marks:
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
