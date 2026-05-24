from __future__ import annotations

from typing import Any, Mapping

from src.application.strategy_lab.dataset_contracts import StrategyLabDataset


def run_strategy_lab_preflight(
    dataset: StrategyLabDataset,
    *,
    min_candidate_sample: int = 5,
    min_outcome_sample: int = 5,
    min_trace_or_reject_sample: int = 1,
) -> dict[str, Any]:
    summary = dataset.summary()
    candidate_count = int(summary["candidate_count"])
    outcome_count = int(summary["outcome_count"])
    trace_or_reject_count = int(summary["trace_count"]) + int(summary["reject_count"])
    missing: list[str] = []
    next_actions: list[str] = []
    warnings: list[str] = list(dataset.warnings)

    if candidate_count < min_candidate_sample:
        missing.append("candidate_samples")
        next_actions.append("collect_more_candidate_runs")
    if outcome_count < min_outcome_sample:
        missing.append("close_or_expiry_outcomes")
        next_actions.append("extend_date_window_or_wait_for_more_lifecycle_outcomes")
    if trace_or_reject_count < min_trace_or_reject_sample:
        missing.append("filter_or_reject_evidence")
        next_actions.append("enable_candidate_trace_or_reject_log_collection")
    if not _has_locked_cash(dataset.candidates):
        warnings.append("locked_cash_fields_incomplete")

    status = "not_evaluable" if missing else "evaluable"
    reason = missing[0] if missing else None
    return {
        "status": status,
        "reason": reason,
        "sample": {
            "candidate_count": candidate_count,
            "outcome_count": outcome_count,
            "reject_count": int(summary["reject_count"]),
            "trace_count": int(summary["trace_count"]),
            "trade_event_count": int(summary["trade_event_count"]),
            "position_lot_count": int(summary["position_lot_count"]),
        },
        "thresholds": {
            "min_candidate_sample": int(min_candidate_sample),
            "min_outcome_sample": int(min_outcome_sample),
            "min_trace_or_reject_sample": int(min_trace_or_reject_sample),
        },
        "missing": missing,
        "next_actions": _dedupe(next_actions),
        "warnings": _dedupe(warnings),
    }


def _has_locked_cash(rows: tuple[Mapping[str, Any], ...]) -> bool:
    for row in rows:
        if row.get("locked_cash") not in (None, ""):
            return True
        raw = row.get("raw")
        if isinstance(raw, Mapping):
            for key in ("locked_cash", "cash_required", "required_cash", "cash_basis", "cash_required_usd", "cash_required_cny"):
                if raw.get(key) not in (None, ""):
                    return True
    return False


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out

