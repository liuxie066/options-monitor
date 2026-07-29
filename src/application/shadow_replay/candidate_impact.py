from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from math import ceil
from pathlib import Path
from typing import Any

from src.application.shadow_replay.candidate_analysis import analyze_rows
from src.application.shadow_replay.capture import (
    ShadowReplaySourceSelection,
    accepted_candidate_snapshots,
    candidate_paths_from_selection,
    candidate_snapshots_from_filter_decisions,
    dedupe_snapshots,
    filter_decision_rows,
    mark_paths_from_selection,
    outcome_paths_from_selection,
    read_replay_rows,
    reject_log_paths_from_selection,
    trace_paths_from_selection,
)
from src.application.shadow_replay.common import (
    MARK_PATH_SCHEMA_VERSION,
    OUTCOME_FACT_SCHEMA_VERSION,
    bind_legacy_decision_evidence,
    dataset_dir_from_arg,
    dataset_read_lock,
    decision_instance_key,
    first_float,
    freeze_decision_identities,
    instrument_key,
    normal_status,
    read_jsonl,
    resolve_output_path,
    safe_rel,
    safety_payload,
    text,
    utc_now,
    validate_dataset_integrity,
    write_json,
    write_text_artifact,
)
from src.application.shadow_replay.parameter_sets import (
    CURRENT_UNDERWRITING_PROFILE,
    LEGACY_SHORT_VOL_PROFILE,
    ParameterSet,
    ParameterVariant,
    load_parameter_set,
)
from src.application.shadow_replay.settlement import is_usable_mark


CANDIDATE_IMPACT_SCHEMA_VERSION = "shadow_replay_candidate_impact.v1"
ACCEPTED_STATUSES = {"accepted", "notified"}
REJECTED_STATUSES = {"rejected", "post_filtered", "ranked_below"}
PARAMETER_FIELD_MAP = {
    "min_iv_rv_ratio": "iv_rv_ratio",
    "min_iv_minus_rv": "iv_minus_rv",
    "min_iv_rv_percentile": "iv_rv_ratio",
    "min_dte": "dte",
    "max_dte": "dte",
    "min_annualized_return": "annualized_return",
}
DEFAULT_IV_RV_HISTORY_SAMPLES = 20


def run_shadow_replay_candidate_impact(
    *,
    repo_root: str | Path,
    params: str | Path | dict[str, Any] | ParameterSet,
    dataset: str | Path | None = None,
    runs_root: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    market: str | None = None,
    min_sample: int = 30,
    output_format: str = "json",
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Compare candidate-set impact for explicit threshold variants."""

    return _run_shadow_replay_candidate_impact(
        repo_root=repo_root,
        params=params,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
        min_sample=min_sample,
        output_format=output_format,
        output=output,
        schema_version=CANDIDATE_IMPACT_SCHEMA_VERSION,
    )


def load_shadow_replay_observed_evidence(
    *,
    repo_root: str | Path,
    dataset: str | Path | None = None,
    runs_root: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    """Load observed replay evidence from a dataset or scanned-run window."""

    base = Path(repo_root).expanduser().resolve()
    account_filter = _account_filter(accounts)
    market_filter = text(market).lower() or None
    if dataset is not None and str(dataset).strip():
        evidence = _load_dataset_evidence(dataset, base=base)
        coverage = _dataset_coverage(evidence=evidence, dataset=dataset)
    else:
        start = _parse_date_arg(start_date, label="start_date") if start_date else None
        end = _parse_date_arg(end_date, label="end_date") if end_date else None
        if start and end and start > end:
            raise ValueError("start_date cannot be after end_date")
        evidence, coverage = _load_run_window_evidence(
            base=base,
            runs_root=runs_root,
            start=start,
            end=end,
        )
    evidence["mark_snapshots"] = bind_legacy_decision_evidence(
        list(evidence["candidate_snapshots"]),
        list(evidence["mark_snapshots"]),
    )
    evidence["outcome_facts"] = bind_legacy_decision_evidence(
        list(evidence["candidate_snapshots"]),
        list(evidence["outcome_facts"]),
    )
    candidate_snapshots = _filter_candidates(
        evidence["candidate_snapshots"],
        accounts=account_filter,
        market=market_filter,
    )
    filter_decisions = _filter_candidates(
        evidence["filter_decisions"],
        accounts=account_filter,
        market=market_filter,
    )
    mark_snapshots = _filter_replay_observations(
        evidence["mark_snapshots"],
        scoped_candidates=candidate_snapshots,
        accounts=account_filter,
        market=market_filter,
    )
    outcome_facts = _filter_replay_observations(
        evidence["outcome_facts"],
        scoped_candidates=candidate_snapshots,
        accounts=account_filter,
        market=market_filter,
    )
    return {
        "candidate_snapshots": candidate_snapshots,
        "filter_decisions": filter_decisions,
        "mark_snapshots": mark_snapshots,
        "outcome_facts": outcome_facts,
        "source": evidence.get("source") or {},
        "coverage": coverage,
        "filters": {
            "accounts": sorted(account_filter) if account_filter else [],
            "market": market_filter,
            "market_filter_applied": bool(market_filter),
        },
    }


def _run_shadow_replay_candidate_impact(
    *,
    repo_root: str | Path,
    params: str | Path | dict[str, Any] | ParameterSet,
    dataset: str | Path | None,
    runs_root: str | Path | None,
    start_date: str | None,
    end_date: str | None,
    accounts: list[str] | tuple[str, ...] | None,
    market: str | None,
    min_sample: int,
    output_format: str,
    output: str | Path | None,
    schema_version: str,
) -> dict[str, Any]:
    """Run a read-only candidate-impact comparison over observed replay evidence."""

    base = Path(repo_root).expanduser().resolve()
    parameter_set = load_parameter_set(params)
    sample_floor = max(1, int(min_sample))
    account_filter = _account_filter(accounts)
    market_filter = text(market).lower() or None
    format_norm = text(output_format).lower() or "json"
    if format_norm not in {"json", "markdown"}:
        raise ValueError("output_format must be json or markdown")

    evidence = load_shadow_replay_observed_evidence(
        repo_root=base,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
    )
    coverage = evidence["coverage"]
    candidate_snapshots = evidence["candidate_snapshots"]
    filter_decisions = evidence["filter_decisions"]
    mark_snapshots = evidence["mark_snapshots"]
    outcome_facts = evidence["outcome_facts"]
    scoped_candidates = [
        row
        for row in candidate_snapshots
        if _parameter_profile_for_candidate(row) == CURRENT_UNDERWRITING_PROFILE
    ]
    scoped_candidates, iv_rv_history = _enrich_iv_rv_history_percentiles(scoped_candidates)
    evidence_quality = _parameter_evidence_quality(
        scoped_candidates,
        required_fields=_required_parameter_fields(parameter_set),
    )
    baseline = _baseline_payload(
        candidates=scoped_candidates,
        filter_decisions=filter_decisions,
        marks=mark_snapshots,
        outcomes=outcome_facts,
        min_sample=sample_floor,
    )
    variants = [
        _variant_payload(
            variant=variant,
            baseline_candidates=scoped_candidates,
            marks=mark_snapshots,
            outcomes=outcome_facts,
            min_sample=sample_floor,
        )
        for variant in parameter_set.variants
    ]
    data_mode = _data_mode(mark_snapshots, outcome_facts)
    gates = _gate_payload(
        coverage=coverage,
        baseline=baseline,
        variants=variants,
        data_mode=data_mode,
        min_sample=sample_floor,
        evidence_quality=evidence_quality,
    )
    candidate_impact = _candidate_impact_payload(
        baseline=baseline,
        variants=variants,
        gates=gates,
    )
    closed_replay_comparison = _closed_replay_comparison(
        baseline=baseline,
        variants=variants,
        gates=gates,
    )
    recommendation = _recommendation(
        data_mode=data_mode,
        evidence_quality=evidence_quality,
        gates=gates,
        candidate_impact=candidate_impact,
        closed_replay_comparison=closed_replay_comparison,
    )
    result: dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at_utc": utc_now(),
        "data_mode": data_mode,
        "universe_scope": "observed_run_universe",
        "coverage": coverage,
        "filters": {
            "accounts": sorted(account_filter) if account_filter else [],
            "market": market_filter,
            "market_filter_applied": bool(market_filter),
        },
        "parameters": parameter_set.to_payload(),
        "summary": {
            "candidate_snapshot_count": len(candidate_snapshots),
            "underwriting_candidate_count": len(scoped_candidates),
            "out_of_scope_candidate_count": max(0, len(candidate_snapshots) - len(scoped_candidates)),
            "mark_path_snapshot_count": len(mark_snapshots),
            "usable_mark_path_snapshot_count": sum(1 for row in mark_snapshots if is_usable_mark(row)),
            "outcome_fact_count": len(outcome_facts),
            "complete_closed_outcome_fact_count": sum(
                1 for row in outcome_facts if _complete_closed_outcome_eligibility(row)[0]
            ),
            "min_sample": sample_floor,
            "variant_count": len(variants),
            "parameter_complete_candidate_count": evidence_quality["complete_candidate_count"],
            "iv_rv_history_available_candidate_count": iv_rv_history["history_available_candidate_count"],
        },
        "evidence_quality": evidence_quality,
        "iv_rv_history": iv_rv_history,
        "baseline": baseline,
        "variants": variants,
        "gates": gates,
        "candidate_impact": candidate_impact,
        "closed_replay_comparison": closed_replay_comparison,
        "recommendation": recommendation,
        "safety": safety_payload(writes_local_dataset=False),
    }
    if format_norm == "markdown":
        result["report_markdown"] = _render_markdown(result)
    if output:
        output_path = resolve_output_path(output)
        if format_norm == "markdown":
            write_text_artifact(
                output_path,
                result.get("report_markdown") or _render_markdown(result),
            )
        else:
            write_json(output_path, result)
    return result


def _load_dataset_evidence(dataset: str | Path, *, base: Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    with dataset_read_lock(dataset_dir):
        integrity = validate_dataset_integrity(dataset_dir, require_manifest=False)
        candidates = freeze_decision_identities(
            read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
        )
        result = {
            "candidate_snapshots": candidates,
            "filter_decisions": read_jsonl(dataset_dir / "filter_decisions.jsonl"),
            "mark_snapshots": bind_legacy_decision_evidence(
                candidates,
                read_jsonl(dataset_dir / "mark_path_snapshots.jsonl"),
            ),
            "outcome_facts": bind_legacy_decision_evidence(
                candidates,
                read_jsonl(dataset_dir / "outcome_facts.jsonl"),
            ),
            "source": {
                "mode": "dataset",
                "dataset_dir": safe_rel(dataset_dir, base=base),
            },
            "integrity": integrity,
        }
        validate_dataset_integrity(dataset_dir, require_manifest=False)
        return result


def _dataset_coverage(*, evidence: dict[str, Any], dataset: str | Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    candidate_count = len(evidence["candidate_snapshots"])
    integrity = evidence.get("integrity") or {}
    integrity_verified = integrity.get("status") == "verified"
    return {
        "mode": "dataset",
        "dataset_dir": str(dataset_dir),
        "requested_start_date": None,
        "requested_end_date": None,
        "available_scanned_runs": None,
        "selected_scanned_runs": None,
        "strict_backtest_allowed": candidate_count > 0 and integrity_verified,
        "reason": (
            "dataset_candidate_universe_ready"
            if candidate_count > 0 and integrity_verified
            else "dataset_integrity_unverified"
            if candidate_count > 0
            else "candidate_universe_missing"
        ),
        "dataset_integrity": integrity,
        "missing_prefix_days": 0,
    }


def _load_run_window_evidence(
    *,
    base: Path,
    runs_root: str | Path | None,
    start: date | None,
    end: date | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(runs_root).expanduser().resolve() if runs_root else (base / "output_runs").resolve()
    all_runs = _discover_runs(root)
    evidence_runs = [row for row in all_runs if row["has_scan_artifacts"]]
    selected = [row for row in evidence_runs if _in_window(row["run_date"], start=start, end=end)]
    candidate_snapshots: list[dict[str, Any]] = []
    filter_decisions: list[dict[str, Any]] = []
    mark_snapshots: list[dict[str, Any]] = []
    outcome_facts: list[dict[str, Any]] = []
    for run in selected:
        run_evidence = _run_evidence(base=base, runs_root=root, run_dir=run["path"])
        candidate_snapshots.extend(run_evidence["candidate_snapshots"])
        filter_decisions.extend(run_evidence["filter_decisions"])
        mark_snapshots.extend(run_evidence["mark_snapshots"])
        outcome_facts.extend(run_evidence["outcome_facts"])
    coverage = _run_window_coverage(
        root=root,
        all_runs=all_runs,
        evidence_runs=evidence_runs,
        selected=selected,
        start=start,
        end=end,
    )
    return (
        {
            "candidate_snapshots": dedupe_snapshots(candidate_snapshots),
            "filter_decisions": filter_decisions,
            "mark_snapshots": mark_snapshots,
            "outcome_facts": outcome_facts,
            "source": {"mode": "runs", "runs_root": safe_rel(root, base=base)},
        },
        coverage,
    )


def _run_evidence(*, base: Path, runs_root: Path, run_dir: Path) -> dict[str, Any]:
    selection = ShadowReplaySourceSelection(repo_root=base, runs_root=runs_root, run_dir=run_dir)
    candidate_paths = candidate_paths_from_selection(selection)
    trace_paths = trace_paths_from_selection(selection)
    reject_log_paths = reject_log_paths_from_selection(selection)
    mark_paths = mark_paths_from_selection(selection)
    outcome_paths = outcome_paths_from_selection(selection)
    accepted = accepted_candidate_snapshots(candidate_paths, base=base)
    decisions = filter_decision_rows(trace_paths, reject_log_paths, base=base)
    rejected = candidate_snapshots_from_filter_decisions(decisions)
    candidates = dedupe_snapshots(accepted + rejected)
    for row in candidates + decisions:
        row.setdefault("run_id", run_dir.name)
    marks = read_replay_rows(mark_paths, schema_version=MARK_PATH_SCHEMA_VERSION, base=base)
    outcomes = read_replay_rows(outcome_paths, schema_version=OUTCOME_FACT_SCHEMA_VERSION, base=base)
    for row in marks + outcomes:
        row.setdefault("run_id", run_dir.name)
    return {
        "candidate_snapshots": candidates,
        "filter_decisions": decisions,
        "mark_snapshots": bind_legacy_decision_evidence(candidates, marks),
        "outcome_facts": bind_legacy_decision_evidence(candidates, outcomes),
    }


def _discover_runs(root: Path) -> list[dict[str, Any]]:
    if not root.exists() or not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted((path.resolve() for path in root.iterdir() if path.is_dir()), key=lambda item: item.name):
        run_date = _run_date(child.name)
        selection = ShadowReplaySourceSelection(repo_root=root.parent, runs_root=root, run_dir=child)
        candidate_count = len(candidate_paths_from_selection(selection))
        trace_count = len(trace_paths_from_selection(selection))
        reject_count = len(reject_log_paths_from_selection(selection))
        has_scan_artifacts = bool(candidate_count or trace_count or reject_count)
        out.append(
            {
                "run_id": child.name,
                "path": child,
                "run_date": run_date,
                "has_scan_artifacts": has_scan_artifacts,
                "candidate_path_count": candidate_count,
                "trace_path_count": trace_count,
                "reject_log_path_count": reject_count,
            }
        )
    return out


def _run_window_coverage(
    *,
    root: Path,
    all_runs: list[dict[str, Any]],
    evidence_runs: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    start: date | None,
    end: date | None,
) -> dict[str, Any]:
    selected_dates = sorted({row["run_date"] for row in selected if row["run_date"] is not None})
    first_selected = selected_dates[0] if selected_dates else None
    missing_prefix_days = 0
    reason = "scan_artifacts_ready"
    strict = bool(selected)
    if not selected:
        strict = False
        reason = "no_scan_artifacts_in_requested_window"
    elif start and first_selected and first_selected > start:
        strict = False
        missing_prefix_days = (first_selected - start).days
        reason = "requested_start_date_has_no_scan_artifacts"
    elif start and first_selected is None:
        strict = False
        reason = "requested_start_date_has_no_scan_artifacts"

    first_available = next((row["run_date"] for row in evidence_runs if row["run_date"] is not None), None)
    last_available = next((row["run_date"] for row in reversed(evidence_runs) if row["run_date"] is not None), None)
    return {
        "mode": "runs",
        "runs_root": str(root),
        "requested_start_date": start.isoformat() if start else None,
        "requested_end_date": end.isoformat() if end else None,
        "all_run_count": len(all_runs),
        "available_scanned_runs": len(evidence_runs),
        "selected_scanned_runs": len(selected),
        "selected_run_ids": [row["run_id"] for row in selected[:50]],
        "first_available_scanned_run_date": first_available.isoformat() if first_available else None,
        "last_available_scanned_run_date": last_available.isoformat() if last_available else None,
        "first_selected_scanned_run_date": first_selected.isoformat() if first_selected else None,
        "selected_scanned_dates": [value.isoformat() for value in selected_dates[:30]],
        "strict_backtest_allowed": strict,
        "missing_prefix_days": missing_prefix_days,
        "reason": reason,
    }


def _baseline_payload(
    *,
    candidates: list[dict[str, Any]],
    filter_decisions: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    min_sample: int,
) -> dict[str, Any]:
    status_counts = Counter(_status_group(row) for row in candidates)
    analysis = analyze_rows(
        candidate_snapshots=candidates,
        filter_decisions=filter_decisions,
        mark_snapshots=marks,
        outcome_facts=outcomes,
        min_sample=min_sample,
    )
    closed_metrics = _closed_lifecycle_metrics(candidates, outcomes, min_sample=min_sample)
    return {
        "name": "production_observed",
        "candidate_count": len(candidates),
        "accepted_count": int(status_counts.get("accepted", 0)),
        "rejected_count": int(status_counts.get("rejected", 0)),
        "status_counts": dict(sorted(status_counts.items())),
        "analysis_summary": analysis["summary"],
        "insurance_metrics": analysis["insurance_metrics"],
        "outcome_stats": analysis["outcome_stats"],
        "closed_lifecycle_metrics": closed_metrics,
        "decision_quality": analysis["decision_quality"]["summary"],
    }


def _variant_payload(
    *,
    variant: ParameterVariant,
    baseline_candidates: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    min_sample: int,
) -> dict[str, Any]:
    candidate_universe = [
        row
        for row in baseline_candidates
        if variant.strategy_family is None or _candidate_strategy_family(row) == variant.strategy_family
    ]
    evaluated_rows: list[dict[str, Any]] = []
    synthetic_decisions: list[dict[str, Any]] = []
    newly_accepted: list[dict[str, Any]] = []
    newly_rejected: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    safety_counts: Counter[str] = Counter()
    safety_rejected_count = 0
    missing_field_counts: Counter[str] = Counter()
    history_mode_counts: Counter[str] = Counter()
    for row in candidate_universe:
        evaluation = _evaluate_candidate(row, variant=variant)
        reason_counts.update(evaluation["reasons"])
        safety_counts.update(evaluation["safety_reasons"])
        if evaluation["safety_reasons"]:
            safety_rejected_count += 1
        history_mode_counts.update([evaluation["iv_rv_history_mode"]])
        missing_field_counts.update(reason for reason in evaluation["reasons"] if reason.endswith("_missing"))
        candidate = dict(row)
        candidate["status"] = evaluation["status"]
        candidate["variant_name"] = variant.name
        candidate["variant_reasons"] = evaluation["reasons"]
        candidate.update(variant.profiles.get(_parameter_profile_for_candidate(row), {}))
        evaluated_rows.append(candidate)
        baseline_status = _status_group(row)
        if baseline_status == "rejected" and evaluation["status"] == "accepted":
            newly_accepted.append(_candidate_change(row, reasons=evaluation["reasons"]))
        elif baseline_status == "accepted" and evaluation["status"] == "rejected":
            newly_rejected.append(_candidate_change(row, reasons=evaluation["reasons"]))
        if evaluation["status"] == "rejected":
            synthetic_decisions.append(
                {
                    "contract_symbol": row.get("contract_symbol"),
                    "symbol": row.get("symbol"),
                    "status": "rejected",
                    "rule": evaluation["reasons"][0] if evaluation["reasons"] else "parameter_rejected",
                    "variant_name": variant.name,
                }
            )
    status_counts = Counter(_status_group(row) for row in evaluated_rows)
    analysis = analyze_rows(
        candidate_snapshots=evaluated_rows,
        filter_decisions=synthetic_decisions,
        mark_snapshots=marks,
        outcome_facts=outcomes,
        min_sample=min_sample,
    )
    history_requested = any(
        "min_iv_rv_percentile" in params
        for params in variant.profiles.values()
    )
    history_evaluated_count = int(history_mode_counts.get("evaluated", 0))
    history_status = (
        "not_requested"
        if not history_requested
        else ("evaluated" if history_evaluated_count > 0 else "insufficient_history")
    )
    variant_payload = variant.to_payload()
    return {
        "name": variant.name,
        "strategy_family": variant.strategy_family,
        "parameters": variant_payload["profiles"],
        "changed_fields": variant_payload["changed_fields"],
        "production_closed_replay_eligible": variant_payload["production_closed_replay_eligible"],
        "production_closed_replay_reason": variant_payload["production_closed_replay_reason"],
        "candidate_count": len(evaluated_rows),
        "accepted_count": int(status_counts.get("accepted", 0)),
        "rejected_count": int(status_counts.get("rejected", 0)),
        "newly_accepted_count": len(newly_accepted),
        "newly_rejected_count": len(newly_rejected),
        "safety_violation_count": 0,
        "safety_rejected_count": safety_rejected_count,
        "status_counts": dict(sorted(status_counts.items())),
        "top_reasons": dict(reason_counts.most_common(20)),
        "safety_reasons": dict(safety_counts.most_common(20)),
        "missing_fields": dict(missing_field_counts.most_common(20)),
        "iv_rv_history_modes": dict(sorted(history_mode_counts.items())),
        "iv_rv_history_status": history_status,
        "comparison_eligible": not history_requested or history_evaluated_count > 0,
        "newly_accepted_samples": newly_accepted[:20],
        "newly_rejected_samples": newly_rejected[:20],
        "analysis_summary": analysis["summary"],
        "insurance_metrics": analysis["insurance_metrics"],
        "outcome_stats": analysis["outcome_stats"],
        "closed_lifecycle_metrics": _closed_lifecycle_metrics(
            evaluated_rows,
            outcomes,
            min_sample=min_sample,
        ),
        "decision_quality": analysis["decision_quality"]["summary"],
    }


def _closed_lifecycle_metrics(
    candidates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    min_sample: int,
) -> dict[str, Any]:
    outcomes_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        key = decision_instance_key(outcome)
        if key:
            outcomes_by_key[key].append(outcome)

    accepted = [row for row in candidates if _status_group(row) == "accepted"]
    lifecycle_rows: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()
    missing_outcome_count = 0
    for candidate in accepted:
        key = decision_instance_key(candidate)
        matched = outcomes_by_key.get(key) or []
        if not matched:
            missing_outcome_count += 1
            continue
        eligible = None
        candidate_blockers: set[str] = set()
        for outcome in matched:
            allowed, reasons = _complete_closed_outcome_eligibility(outcome)
            if allowed:
                eligible = outcome
                break
            candidate_blockers.update(reasons)
        if eligible is None:
            blocker_counts.update(candidate_blockers or {"complete_closed_outcome_missing"})
            continue
        lifecycle_rows.append(eligible)

    pnl_values = [float(first_float(row, "lifecycle_pnl_net") or 0.0) for row in lifecycle_rows]
    capital_days_values = [float(first_float(row, "capital_days") or 0.0) for row in lifecycle_rows]
    efficiency_values = [
        float(first_float(row, "annualized_capital_efficiency") or 0.0)
        if first_float(row, "annualized_capital_efficiency") is not None
        else float(first_float(row, "lifecycle_pnl_net") or 0.0) * 365.0 / float(first_float(row, "capital_days") or 1.0)
        for row in lifecycle_rows
    ]
    total_pnl = sum(pnl_values)
    total_capital_days = sum(capital_days_values)
    count = len(lifecycle_rows)
    outcome_counts = Counter(text(row.get("outcome")).lower() or "unknown" for row in lifecycle_rows)
    fee_basis_counts = Counter(text(row.get("fee_basis")).lower() or "unknown" for row in lifecycle_rows)
    allocation_counts = Counter(
        text(row.get("covered_call_allocation_status") or row.get("allocation_quality")).lower() or "none"
        for row in lifecycle_rows
    )
    symbol_capital_days: dict[str, float] = defaultdict(float)
    for row, capital_days in zip(lifecycle_rows, capital_days_values, strict=True):
        symbol_capital_days[text(row.get("symbol") or row.get("underlying_symbol")).upper() or "UNKNOWN"] += capital_days
    concentration_weights = [value / total_capital_days for value in symbol_capital_days.values()] if total_capital_days > 0 else []
    tail = _closed_lifecycle_tail(efficiency_values)
    return {
        "metric_basis": "accepted_complete_closed_lifecycle",
        "accepted_candidate_count": len(accepted),
        "complete_closed_count": count,
        "missing_outcome_count": missing_outcome_count,
        "incomplete_outcome_count": max(0, len(accepted) - missing_outcome_count - count),
        "blocker_counts": dict(blocker_counts.most_common()),
        "min_sample": int(min_sample),
        "sample_ready": count >= int(min_sample),
        "lifecycle_pnl_net_total": round(total_pnl, 6),
        "capital_days_total": round(total_capital_days, 6),
        "weighted_annualized_capital_efficiency": (
            round(total_pnl * 365.0 / total_capital_days, 8)
            if total_capital_days > 0
            else None
        ),
        "negative_outcome_count": sum(1 for value in pnl_values if value < 0),
        "negative_outcome_rate": round(sum(1 for value in pnl_values if value < 0) / count, 6) if count else None,
        "assignment_count": int(outcome_counts.get("assigned_at_expiry", 0)),
        "assignment_rate": round(outcome_counts.get("assigned_at_expiry", 0) / count, 6) if count else None,
        "called_away_count": int(outcome_counts.get("called_away_at_expiry", 0)),
        "called_away_rate": round(outcome_counts.get("called_away_at_expiry", 0) / count, 6) if count else None,
        "exercise_rate": round(
            (outcome_counts.get("assigned_at_expiry", 0) + outcome_counts.get("called_away_at_expiry", 0)) / count,
            6,
        ) if count else None,
        "tail_risk": tail,
        "concentration": {
            "top_symbol_capital_days_ratio": round(max(concentration_weights), 6) if concentration_weights else None,
            "capital_days_hhi": round(sum(value * value for value in concentration_weights), 6) if concentration_weights else None,
            "symbol_count": len(symbol_capital_days),
        },
        "fee_basis_counts": dict(sorted(fee_basis_counts.items())),
        "allocation_quality_counts": dict(sorted(allocation_counts.items())),
        "runtime_config_write_allowed": False,
    }


def _complete_closed_outcome_eligibility(outcome: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if text(outcome.get("lifecycle_quality")).lower() != "complete_closed":
        reasons.append("lifecycle_quality_not_complete_closed")
    if first_float(outcome, "lifecycle_pnl_net") is None:
        reasons.append("lifecycle_pnl_net_missing")
    capital_days = first_float(outcome, "capital_days")
    if capital_days is None or capital_days <= 0:
        reasons.append("capital_days_missing")
    fee_basis = text(outcome.get("fee_basis")).lower()
    if fee_basis not in {"actual", "estimated", "mixed"}:
        reasons.append("fee_basis_incomplete")
    if _has_missing_fee_components(outcome.get("fee_missing_components")):
        reasons.append("fee_components_missing")
    allocation = text(outcome.get("covered_call_allocation_status") or outcome.get("allocation_quality")).lower()
    if allocation in {"unallocated", "mixed", "ambiguous", "missing"}:
        reasons.append("allocation_quality_incomplete")
    return not reasons, reasons


def _has_missing_fee_components(value: Any) -> bool:
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    normalized = text(value).lower()
    return normalized not in {"", "[]", "{}", "none", "null", "nan"}


def _closed_lifecycle_tail(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"observation_count": 0, "var_90": None, "cvar_90": None, "worst": None}
    ordered = sorted(values)
    tail_count = max(1, int(ceil(len(ordered) * 0.10)))
    tail = ordered[:tail_count]
    return {
        "observation_count": len(ordered),
        "tail_observation_count": tail_count,
        "var_90": round(tail[-1], 8),
        "cvar_90": round(sum(tail) / len(tail), 8),
        "worst": round(ordered[0], 8),
    }


def _required_parameter_fields(parameter_set: ParameterSet) -> list[str]:
    fields: set[str] = set()
    for variant in parameter_set.variants:
        for params in variant.profiles.values():
            for key in params:
                field = PARAMETER_FIELD_MAP.get(key)
                if field:
                    fields.add(field)
    return sorted(fields)


def _parameter_evidence_quality(
    candidates: list[dict[str, Any]],
    *,
    required_fields: list[str],
) -> dict[str, Any]:
    candidate_count = len(candidates)
    coverage = {
        field: _field_coverage(candidates, field)
        for field in required_fields
    }
    missing_required = [
        field
        for field, item in coverage.items()
        if int(item.get("present_count") or 0) < candidate_count
    ]
    complete_count = sum(1 for row in candidates if all(_has_parameter_field(row, field) for field in required_fields))
    return {
        "required_fields": required_fields,
        "candidate_count": candidate_count,
        "complete_candidate_count": complete_count,
        "complete_ratio": round(complete_count / candidate_count, 6) if candidate_count else 0.0,
        "missing_required_fields": missing_required,
        "parameter_fields_ready": bool(candidate_count and not missing_required),
        "field_coverage": coverage,
    }


def _field_coverage(candidates: list[dict[str, Any]], field: str) -> dict[str, Any]:
    present = sum(1 for row in candidates if _has_parameter_field(row, field))
    total = len(candidates)
    return {
        "present_count": present,
        "missing_count": max(0, total - present),
        "coverage_ratio": round(present / total, 6) if total else 0.0,
    }


def _has_parameter_field(row: dict[str, Any], field: str) -> bool:
    return first_float(row, field) is not None


def _evaluate_candidate(row: dict[str, Any], *, variant: ParameterVariant) -> dict[str, Any]:
    profile = _parameter_profile_for_candidate(row)
    params = variant.profiles.get(profile)
    if not params:
        return {
            "status": "rejected",
            "reasons": ["strategy_profile_out_of_scope"],
            "safety_reasons": [],
            "iv_rv_history_mode": "not_requested",
        }
    safety = _safety_reasons(row)
    safety.extend(_preserved_production_gate_reasons(row, params=params))
    safety = list(dict.fromkeys(safety))
    if safety:
        return {
            "status": "rejected",
            "reasons": safety,
            "safety_reasons": safety,
            "iv_rv_history_mode": (
                "safety_rejected"
                if "min_iv_rv_percentile" in params
                else "not_requested"
            ),
        }
    reasons: list[str] = []
    _check_min(row, params, "min_iv_rv_ratio", "iv_rv_ratio", reasons)
    _check_min(row, params, "min_iv_minus_rv", "iv_minus_rv", reasons)
    history_mode = _check_iv_rv_percentile(row, params, reasons)
    _check_min(row, params, "min_dte", "dte", reasons)
    _check_max(row, params, "max_dte", "dte", reasons)
    _check_min(row, params, "min_annualized_return", "annualized_return", reasons)
    status = "rejected" if reasons else "accepted"
    return {
        "status": status,
        "reasons": reasons or ["parameter_pass"],
        "safety_reasons": [],
        "iv_rv_history_mode": history_mode,
    }


def _check_min(row: dict[str, Any], params: dict[str, float], param_key: str, field: str, reasons: list[str]) -> None:
    if param_key not in params:
        return
    value = first_float(row, field)
    if value is None:
        reasons.append(f"{field}_missing")
    elif value < params[param_key]:
        reasons.append(f"{field}_below_{param_key}")


def _check_max(row: dict[str, Any], params: dict[str, float], param_key: str, field: str, reasons: list[str]) -> None:
    if param_key not in params:
        return
    value = first_float(row, field)
    if value is None:
        reasons.append(f"{field}_missing")
    elif value > params[param_key]:
        reasons.append(f"{field}_above_{param_key}")


def _check_iv_rv_percentile(row: dict[str, Any], params: dict[str, float], reasons: list[str]) -> str:
    if "min_iv_rv_percentile" not in params:
        return "not_requested"
    required_samples = int(params.get("min_iv_rv_history_samples") or DEFAULT_IV_RV_HISTORY_SAMPLES)
    history_samples = int(first_float(row, "iv_rv_history_sample_count") or 0)
    if history_samples < required_samples:
        return "fallback_absolute_floor"
    percentile = first_float(row, "iv_rv_history_percentile")
    if percentile is None:
        reasons.append("iv_rv_history_percentile_missing")
    elif percentile < params["min_iv_rv_percentile"]:
        reasons.append("iv_rv_history_percentile_below_minimum")
    return "evaluated"


def _safety_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not instrument_key(row):
        reasons.append("instrument_identity_missing")
    event_reason = _event_risk_reason(row)
    if event_reason:
        reasons.append(event_reason)
    spread = first_float(row, "spread_ratio")
    max_spread = first_float(row, "max_spread_ratio") or 0.30
    if spread is not None and spread > max_spread:
        reasons.append("spread_ratio_above_safety_floor")
    premium = first_float(row, "net_income", "net_income_cny")
    if premium is not None and premium <= 0:
        reasons.append("premium_not_positive")
    open_interest = first_float(row, "open_interest")
    volume = first_float(row, "volume")
    if open_interest is not None and volume is not None and open_interest <= 0 and volume <= 0:
        reasons.append("liquidity_zero")
    cash_required = first_float(row, "cash_required_cny", "assignment_notional_cny")
    cash_available = first_float(
        row,
        "cash_free_cny",
        "cash_free_total_cny",
        "cash_free_usd",
    )
    if (
        cash_required is not None
        and cash_available is not None
        and cash_required > cash_available
    ):
        reasons.append("cash_capacity_insufficient")
    if _candidate_strategy_family(row) == "covered_call":
        covered = first_float(
            row,
            "covered_contracts_available",
            "covered_quantity",
            "covered_share_quantity",
            "shares_available_for_cover",
        )
        contracts = first_float(row, "contracts", "contract_count") or 1.0
        if covered is None:
            reasons.append("covered_call_coverage_context_missing")
        elif (
            first_float(row, "covered_contracts_available") is not None
            and covered < contracts
        ):
            reasons.append("covered_call_capacity_insufficient")
        elif (
            first_float(row, "covered_contracts_available") is None
            and covered < contracts * 100.0
        ):
            reasons.append("covered_call_capacity_insufficient")
        if first_float(
            row,
            "cost_basis",
            "underlying_cost_basis",
            "avg_cost",
            "average_cost",
            "cost_basis_floor",
        ) is None:
            reasons.append("covered_call_cost_basis_context_missing")
    return reasons


def _preserved_production_gate_reasons(
    row: dict[str, Any],
    *,
    params: dict[str, float],
) -> list[str]:
    baseline_status = _status_group(row)
    if baseline_status != "rejected":
        return []
    raw_status = normal_status(row.get("status"))
    if raw_status == "ranked_below":
        return ["production_rank_truncation_not_replayed"]
    rule = text(row.get("filter_rule") or row.get("rule")).lower()
    if not rule:
        return ["original_rejection_gate_unmodeled"]
    tunable_keys: set[str] = set()
    if (
        "iv_rv_ratio" in rule
        or "iv-rv-ratio" in rule
        or "vol_edge_ratio" in rule
    ):
        tunable_keys.update({"min_iv_rv_ratio", "min_iv_rv_percentile"})
    if (
        "iv_minus_rv" in rule
        or "iv-rv-spread" in rule
        or "vol_edge_spread" in rule
    ):
        tunable_keys.add("min_iv_minus_rv")
    if "dte" in rule or "expiration" in rule:
        tunable_keys.update({"min_dte", "max_dte"})
    if "annualized" in rule or "return" in rule:
        tunable_keys.add("min_annualized_return")
    if tunable_keys and tunable_keys.intersection(params):
        return []
    return [f"preserved_production_gate:{rule}"]


def _event_risk_reason(row: dict[str, Any]) -> str | None:
    values = [
        row.get("event_risk_status"),
        row.get("event_status"),
        row.get("event_source_status"),
        row.get("event_risk"),
        row.get("has_event_before_expiry"),
    ]
    for raw in values:
        value = text(raw).lower()
        if not value:
            continue
        if value in {"true", "yes", "1"}:
            return "event_risk_before_expiry"
        if any(token in value for token in ("unavailable", "missing", "failed", "before_expiry", "event_risk")):
            return "event_risk_not_acceptable"
    return None


def _enrich_iv_rv_history_percentiles(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enriched = [dict(row) for row in candidates]
    grouped: dict[tuple[str, str, str], dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for idx, row in enumerate(enriched):
        group_key = _iv_rv_history_group(row)
        order_key = _iv_rv_history_order(row)
        if group_key is None or not order_key or first_float(row, "iv_rv_ratio") is None:
            row["iv_rv_history_sample_count"] = 0
            row["iv_rv_history_percentile"] = None
            continue
        grouped[group_key][order_key].append(idx)

    for group_key, rows_by_run in grouped.items():
        history: list[float] = []
        for order_key in sorted(rows_by_run):
            indexes = rows_by_run[order_key]
            for idx in indexes:
                value = first_float(enriched[idx], "iv_rv_ratio")
                enriched[idx]["iv_rv_history_scope"] = {
                    "symbol": group_key[0],
                    "option_type": group_key[1],
                    "dte_bucket": group_key[2],
                }
                enriched[idx]["iv_rv_history_sample_count"] = len(history)
                enriched[idx]["iv_rv_history_percentile"] = _empirical_percentile(value, history)
            history.extend(
                value
                for idx in indexes
                if (value := first_float(enriched[idx], "iv_rv_ratio")) is not None
            )

    sample_counts = [int(first_float(row, "iv_rv_history_sample_count") or 0) for row in enriched]
    return enriched, {
        "scope": "symbol_option_type_dte_bucket_prior_runs",
        "candidate_count": len(enriched),
        "history_group_count": len(grouped),
        "history_available_candidate_count": sum(1 for value in sample_counts if value > 0),
        "history_missing_candidate_count": sum(1 for value in sample_counts if value <= 0),
        "max_history_sample_count": max(sample_counts, default=0),
        "lookahead_allowed": False,
    }


def _iv_rv_history_group(row: dict[str, Any]) -> tuple[str, str, str] | None:
    symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
    option_type = text(row.get("option_type") or row.get("mode")).lower()
    dte = first_float(row, "dte")
    if not symbol or option_type not in {"put", "call"} or dte is None:
        return None
    return symbol, option_type, _history_dte_bucket(dte)


def _iv_rv_history_order(row: dict[str, Any]) -> str:
    run_id = text(row.get("run_id"))
    if run_id:
        return run_id
    for part in Path(text(row.get("source_path"))).parts:
        if len(part) >= 8 and part[:8].isdigit():
            return part
    return ""


def _history_dte_bucket(value: float) -> str:
    if value < 14:
        return "<14"
    if value < 30:
        return "14-29"
    if value < 45:
        return "30-44"
    if value < 60:
        return "45-59"
    return "60+"


def _empirical_percentile(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    below = sum(1 for item in history if item < value)
    equal = sum(1 for item in history if item == value)
    return round((below + (0.5 * equal)) / len(history), 6)


def _filter_candidates(
    rows: list[dict[str, Any]],
    *,
    accounts: set[str],
    market: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        account = text(row.get("account")).lower()
        if accounts and account not in accounts:
            continue
        if market and not _market_matches(row, market):
            continue
        out.append(row)
    return out


def _filter_replay_observations(
    rows: list[dict[str, Any]],
    *,
    scoped_candidates: list[dict[str, Any]],
    accounts: set[str],
    market: str | None,
) -> list[dict[str, Any]]:
    if not accounts and not market:
        return list(rows)
    scoped_keys = {
        decision_instance_key(row)
        for row in scoped_candidates
        if decision_instance_key(row)
    }
    if not scoped_keys:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        key = decision_instance_key(row)
        if not key or key not in scoped_keys:
            continue
        account = text(row.get("account")).lower()
        if accounts and account not in accounts:
            continue
        if market and not _market_matches(row, market):
            continue
        out.append(row)
    return out


def _market_matches(row: dict[str, Any], market: str) -> bool:
    symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
    if not symbol:
        return False
    if market == "hk":
        return symbol.endswith(".HK")
    if market == "us":
        return not symbol.endswith(".HK")
    return True


def _recommendation(
    *,
    data_mode: str,
    evidence_quality: dict[str, Any],
    gates: dict[str, Any],
    candidate_impact: dict[str, Any],
    closed_replay_comparison: dict[str, Any],
) -> dict[str, Any]:
    candidate_gate = gates.get("candidate_impact") or {}
    field_gate = gates.get("parameter_fields") or {}
    if not bool(candidate_gate.get("allowed")):
        return {
            "status": "not_ready",
            "reason": candidate_gate.get("reason") or "candidate_impact_not_ready",
            "next_action": candidate_gate.get("next_action") or "review_backtest_evidence",
            "missing_required_fields": evidence_quality.get("missing_required_fields"),
            "complete_candidate_count": evidence_quality.get("complete_candidate_count"),
            "candidate_count": evidence_quality.get("candidate_count"),
        }
    if data_mode != "closed_replay":
        reason = (
            "complete_closed_lifecycle_evidence_missing"
            if data_mode == "outcome_incomplete"
            else "outcome_evidence_missing"
        )
        return {
            "status": "ready_for_live_shadow_candidate_review",
            "reason": reason,
            "candidate_impact_allowed": True,
            "candidate_impact_reason": candidate_impact.get("reason"),
            "candidate_review_variant": candidate_impact.get("best_variant_by_new_accepts"),
            "candidate_review_basis": "newly_accepted_count_only",
            "parameter_field_status": field_gate.get("status"),
            "missing_required_fields": evidence_quality.get("missing_required_fields"),
            "production_recommendation_allowed": False,
            "next_action": "review_candidate_impact_and_collect_fee_complete_closed_lifecycle_outcomes",
        }
    production_gate = gates.get("production_recommendation") or {}
    if not bool(production_gate.get("allowed")):
        return {
            "status": "ready_for_live_shadow_outcome_review",
            "reason": production_gate.get("reason") or "outcome_review_not_ready",
            "candidate_impact_allowed": True,
            "candidate_review_variant": candidate_impact.get("best_variant_by_new_accepts"),
            "candidate_review_basis": "newly_accepted_count_only",
            "parameter_field_status": field_gate.get("status"),
            "production_recommendation_allowed": False,
            "next_action": production_gate.get("next_action") or "collect_complete_outcome_evidence",
        }
    return {
        "status": "ready_for_manual_closed_replay_review",
        "reason": "complete_closed_outcome_review_ready",
        "candidate_impact_allowed": True,
        "candidate_review_variant": closed_replay_comparison.get("suggested_variant_for_manual_review"),
        "candidate_review_basis": "complete_closed_weighted_capital_efficiency_with_risk_metrics",
        "parameter_field_status": field_gate.get("status"),
        "production_recommendation_allowed": True,
        "runtime_config_write_allowed": False,
        "next_action": "manually_review_single_parameter_variant_then_run_live_shadow",
    }


def _gate_payload(
    *,
    coverage: dict[str, Any],
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
    data_mode: str,
    min_sample: int,
    evidence_quality: dict[str, Any],
) -> dict[str, Any]:
    scan_gate = _scan_artifact_gate(coverage)
    sample_gate = _sample_size_gate(baseline, min_sample=min_sample)
    field_gate = _parameter_field_gate(evidence_quality, min_sample=min_sample)
    candidate_gate = _candidate_impact_gate(
        scan_gate=scan_gate,
        sample_gate=sample_gate,
        field_gate=field_gate,
        variants=variants,
    )
    lifecycle_gate = _closed_lifecycle_gate(
        baseline=baseline,
        variants=variants,
        min_sample=min_sample,
    )
    production_gate = _production_recommendation_gate(
        candidate_gate=candidate_gate,
        lifecycle_gate=lifecycle_gate,
        data_mode=data_mode,
        baseline=baseline,
        variants=variants,
    )
    return {
        "scan_artifacts": scan_gate,
        "sample_size": sample_gate,
        "parameter_fields": field_gate,
        "candidate_impact": candidate_gate,
        "closed_lifecycle_evidence": lifecycle_gate,
        "production_recommendation": production_gate,
    }


def _scan_artifact_gate(coverage: dict[str, Any]) -> dict[str, Any]:
    allowed = bool(coverage.get("strict_backtest_allowed"))
    return {
        "allowed": allowed,
        "status": "pass" if allowed else "fail",
        "reason": "scan_artifacts_ready" if allowed else (coverage.get("reason") or "coverage_not_strict"),
        "next_action": None if allowed else "collect_scan_artifacts_for_requested_window",
    }


def _sample_size_gate(baseline: dict[str, Any], *, min_sample: int) -> dict[str, Any]:
    candidate_count = int(baseline.get("candidate_count") or 0)
    allowed = candidate_count >= int(min_sample)
    return {
        "allowed": allowed,
        "status": "pass" if allowed else "fail",
        "reason": "sample_size_ready" if allowed else "sample_size_below_min_sample",
        "candidate_count": candidate_count,
        "min_sample": int(min_sample),
        "next_action": None if allowed else "collect_more_scan_artifacts",
    }


def _parameter_field_gate(evidence_quality: dict[str, Any], *, min_sample: int) -> dict[str, Any]:
    candidate_count = int(evidence_quality.get("candidate_count") or 0)
    complete_count = int(evidence_quality.get("complete_candidate_count") or 0)
    missing_required = list(evidence_quality.get("missing_required_fields") or [])
    allowed = complete_count >= int(min_sample)
    if not allowed:
        status = "fail"
        reason = "parameter_fields_missing"
        next_action = "collect_candidate_parameter_fields"
    elif missing_required:
        status = "warn"
        reason = "parameter_fields_partially_available"
        next_action = "treat_filter_only_counts_as_lower_bound_and_collect_missing_fields"
    else:
        status = "pass"
        reason = "parameter_fields_complete"
        next_action = None
    return {
        "allowed": allowed,
        "status": status,
        "reason": reason,
        "candidate_count": candidate_count,
        "complete_candidate_count": complete_count,
        "complete_ratio": evidence_quality.get("complete_ratio"),
        "min_sample": int(min_sample),
        "missing_required_fields": missing_required,
        "next_action": next_action,
    }


def _candidate_impact_gate(
    *,
    scan_gate: dict[str, Any],
    sample_gate: dict[str, Any],
    field_gate: dict[str, Any],
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    for gate in (scan_gate, sample_gate, field_gate):
        if not bool(gate.get("allowed")):
            return {
                "allowed": False,
                "status": "not_ready",
                "reason": gate.get("reason") or "candidate_impact_not_ready",
                "next_action": gate.get("next_action") or "review_backtest_evidence",
            }
    eligible_variants = [row for row in variants if bool(row.get("comparison_eligible", True))]
    if not eligible_variants:
        return {
            "allowed": False,
            "status": "not_ready",
            "reason": "iv_rv_history_insufficient",
            "next_action": "collect_more_prior_run_iv_rv_history",
        }
    if max(int(row.get("accepted_count") or 0) for row in eligible_variants) <= 0:
        return {
            "allowed": False,
            "status": "not_ready",
            "reason": "no_variant_accepts_candidates",
            "next_action": "review_parameter_hypothesis",
        }
    limitations: list[str] = []
    if str(field_gate.get("status") or "") == "warn":
        limitations.append("parameter_fields_partial_counts_are_lower_bound")
    return {
        "allowed": True,
        "status": "ready",
        "reason": "filter_counterfactual_available",
        "next_action": None,
        "limitations": limitations,
    }


def _closed_lifecycle_gate(
    *,
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
    min_sample: int,
) -> dict[str, Any]:
    baseline_metrics = baseline.get("closed_lifecycle_metrics") or {}
    baseline_count = int(baseline_metrics.get("complete_closed_count") or 0)
    ready_variants: list[str] = []
    ineligible_variants: dict[str, str] = {}
    for variant in variants:
        name = str(variant.get("name") or "")
        if not bool(variant.get("production_closed_replay_eligible")):
            ineligible_variants[name] = str(
                variant.get("production_closed_replay_reason")
                or "closed_replay_requires_exactly_one_production_parameter"
            )
            continue
        metrics = variant.get("closed_lifecycle_metrics") or {}
        if int(metrics.get("complete_closed_count") or 0) < int(min_sample):
            ineligible_variants[name] = "complete_closed_sample_below_min_sample"
            continue
        ready_variants.append(name)
    baseline_ready = baseline_count >= int(min_sample)
    allowed = baseline_ready and bool(ready_variants)
    if not baseline_ready:
        reason = "baseline_complete_closed_sample_below_min_sample"
        next_action = "collect_more_fee_complete_closed_baseline_lifecycles"
    elif not ready_variants:
        reason = "no_single_parameter_variant_has_complete_closed_sample"
        next_action = "use_single_parameter_variants_and_collect_complete_closed_lifecycles"
    else:
        reason = "complete_closed_lifecycle_comparison_ready"
        next_action = None
    return {
        "allowed": allowed,
        "status": "pass" if allowed else "fail",
        "reason": reason,
        "baseline_complete_closed_count": baseline_count,
        "min_sample": int(min_sample),
        "ready_variants": ready_variants,
        "ineligible_variants": ineligible_variants,
        "next_action": next_action,
    }


def _production_recommendation_gate(
    *,
    candidate_gate: dict[str, Any],
    lifecycle_gate: dict[str, Any],
    data_mode: str,
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    if not bool(candidate_gate.get("allowed")):
        return {
            "allowed": False,
            "status": "blocked",
            "reason": "candidate_impact_not_ready",
            "next_action": candidate_gate.get("next_action") or "review_backtest_evidence",
            "runtime_config_write_allowed": False,
        }
    if data_mode != "closed_replay":
        return {
            "allowed": False,
            "status": "blocked",
            "reason": "complete_closed_lifecycle_evidence_missing",
            "next_action": "collect_fee_complete_closed_lifecycle_outcomes",
            "runtime_config_write_allowed": False,
        }
    if not bool(lifecycle_gate.get("allowed")):
        return {
            "allowed": False,
            "status": "blocked",
            "reason": lifecycle_gate.get("reason") or "complete_closed_lifecycle_evidence_missing",
            "next_action": lifecycle_gate.get("next_action") or "collect_fee_complete_closed_lifecycle_outcomes",
            "ready_variants": lifecycle_gate.get("ready_variants") or [],
            "runtime_config_write_allowed": False,
        }
    baseline_ready = bool((baseline.get("analysis_summary") or {}).get("manual_strategy_review_ready"))
    lifecycle_ready = set(lifecycle_gate.get("ready_variants") or [])
    variant_eligibility: dict[str, dict[str, Any]] = {}
    ready_variants: list[str] = []
    for row in variants:
        name = str(row.get("name") or "")
        blockers: list[str] = []
        if name not in lifecycle_ready:
            blockers.append("complete_closed_lifecycle_gate_failed")
        if not bool(row.get("production_closed_replay_eligible")):
            blockers.append(
                str(
                    row.get("production_closed_replay_reason")
                    or "single_production_parameter_required"
                )
            )
        if not bool((row.get("analysis_summary") or {}).get("manual_strategy_review_ready")):
            blockers.append("variant_outcome_review_not_ready")
        allowed = not blockers
        variant_eligibility[name] = {
            "allowed": allowed,
            "blockers": blockers,
            "strategy_family": row.get("strategy_family"),
        }
        if allowed:
            ready_variants.append(name)
    if not baseline_ready:
        return {
            "allowed": False,
            "status": "blocked",
            "reason": "baseline_outcome_review_not_ready",
            "next_action": "collect_complete_baseline_mark_and_lifecycle_outcomes",
            "baseline_review_ready": False,
            "ready_variants": ready_variants,
            "variant_eligibility": variant_eligibility,
            "runtime_config_write_allowed": False,
        }
    if not ready_variants:
        return {
            "allowed": False,
            "status": "blocked",
            "reason": "variant_outcome_review_not_ready",
            "next_action": "collect_complete_variant_mark_and_lifecycle_outcomes",
            "baseline_review_ready": True,
            "ready_variants": [],
            "variant_eligibility": variant_eligibility,
            "runtime_config_write_allowed": False,
        }
    return {
        "allowed": True,
        "status": "review_ready",
        "reason": "outcome_review_ready",
        "next_action": "compare_variant_outcomes_then_run_live_shadow",
        "baseline_review_ready": True,
        "ready_variants": ready_variants,
        "variant_eligibility": variant_eligibility,
        "runtime_config_write_allowed": False,
    }


def _candidate_impact_payload(
    *,
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
    gates: dict[str, Any],
) -> dict[str, Any]:
    gate = gates.get("candidate_impact") or {}
    variant_summaries = [
        {
            "name": row.get("name"),
            "accepted_count": int(row.get("accepted_count") or 0),
            "newly_accepted_count": int(row.get("newly_accepted_count") or 0),
            "newly_rejected_count": int(row.get("newly_rejected_count") or 0),
            "safety_violation_count": int(row.get("safety_violation_count") or 0),
            "safety_rejected_count": int(row.get("safety_rejected_count") or 0),
            "iv_rv_history_status": row.get("iv_rv_history_status"),
            "comparison_eligible": bool(row.get("comparison_eligible", True)),
        }
        for row in variants
    ]
    best_by_new = None
    best_by_total = None
    eligible_summaries = [row for row in variant_summaries if row["comparison_eligible"]]
    if eligible_summaries:
        best_by_new = max(
            eligible_summaries,
            key=lambda row: (row["newly_accepted_count"], row["accepted_count"]),
        )
        best_by_total = max(
            eligible_summaries,
            key=lambda row: (row["accepted_count"], row["newly_accepted_count"]),
        )
    return {
        "status": gate.get("status") or "not_ready",
        "reason": gate.get("reason"),
        "allowed": bool(gate.get("allowed")),
        "baseline_accepted_count": int(baseline.get("accepted_count") or 0),
        "baseline_candidate_count": int(baseline.get("candidate_count") or 0),
        "best_variant_by_new_accepts": best_by_new.get("name") if best_by_new else None,
        "best_variant_by_total_accepts": best_by_total.get("name") if best_by_total else None,
        "variant_summaries": variant_summaries,
        "limitations": list(gate.get("limitations") or []),
    }


def _closed_replay_comparison(
    *,
    baseline: dict[str, Any],
    variants: list[dict[str, Any]],
    gates: dict[str, Any],
) -> dict[str, Any]:
    lifecycle_gate = gates.get("closed_lifecycle_evidence") or {}
    production_gate = gates.get("production_recommendation") or {}
    ready_names = set(lifecycle_gate.get("ready_variants") or [])
    baseline_metrics = baseline.get("closed_lifecycle_metrics") or {}
    comparisons: list[dict[str, Any]] = []
    for variant in variants:
        name = str(variant.get("name") or "")
        metrics = variant.get("closed_lifecycle_metrics") or {}
        comparisons.append(
            {
                "name": name,
                "changed_fields": list(variant.get("changed_fields") or []),
                "production_eligible": name in ready_names,
                "metrics": metrics,
                "deltas_vs_baseline": _closed_metric_deltas(baseline_metrics, metrics),
            }
        )
    eligible = [row for row in comparisons if row["production_eligible"]]
    suggested = max(eligible, key=_closed_variant_sort_key) if eligible else None
    return {
        "status": "review_ready" if bool(production_gate.get("allowed")) else "not_ready",
        "reason": production_gate.get("reason"),
        "baseline_metrics": baseline_metrics,
        "variant_comparisons": comparisons,
        "suggested_variant_for_manual_review": suggested.get("name") if suggested and production_gate.get("allowed") else None,
        "selection_basis": [
            "weighted_annualized_capital_efficiency",
            "lifecycle_pnl_net_total",
            "negative_outcome_rate",
            "tail_cvar_90",
            "top_symbol_capital_days_ratio",
        ],
        "writes_runtime_config": False,
    }


def _closed_metric_deltas(baseline: dict[str, Any], variant: dict[str, Any]) -> dict[str, float | None]:
    fields = {
        "weighted_annualized_capital_efficiency": (baseline, variant),
        "lifecycle_pnl_net_total": (baseline, variant),
        "negative_outcome_rate": (baseline, variant),
        "assignment_rate": (baseline, variant),
        "exercise_rate": (baseline, variant),
    }
    out: dict[str, float | None] = {}
    for field, (left, right) in fields.items():
        base_value = first_float(left, field)
        variant_value = first_float(right, field)
        out[field] = round(variant_value - base_value, 8) if base_value is not None and variant_value is not None else None
    for field, section, key in (
        ("tail_cvar_90", "tail_risk", "cvar_90"),
        ("top_symbol_capital_days_ratio", "concentration", "top_symbol_capital_days_ratio"),
    ):
        base_value = first_float(baseline.get(section) or {}, key)
        variant_value = first_float(variant.get(section) or {}, key)
        out[field] = round(variant_value - base_value, 8) if base_value is not None and variant_value is not None else None
    return out


def _closed_variant_sort_key(row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    metrics = row.get("metrics") or {}
    efficiency = first_float(metrics, "weighted_annualized_capital_efficiency")
    total_pnl = first_float(metrics, "lifecycle_pnl_net_total")
    negative_rate = first_float(metrics, "negative_outcome_rate")
    tail_cvar = first_float(metrics.get("tail_risk") or {}, "cvar_90")
    concentration = first_float(metrics.get("concentration") or {}, "top_symbol_capital_days_ratio")
    return (
        efficiency if efficiency is not None else float("-inf"),
        total_pnl if total_pnl is not None else float("-inf"),
        -(negative_rate if negative_rate is not None else float("inf")),
        tail_cvar if tail_cvar is not None else float("-inf"),
        -(concentration if concentration is not None else float("inf")),
    )


def _data_mode(marks: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> str:
    has_usable_mark = any(is_usable_mark(row) for row in marks)
    if has_usable_mark and any(_complete_closed_outcome_eligibility(row)[0] for row in outcomes):
        return "closed_replay"
    if outcomes and has_usable_mark:
        return "outcome_incomplete"
    if has_usable_mark:
        return "path_only"
    return "filter_only"


def _status_group(row: dict[str, Any]) -> str:
    status = normal_status(row.get("status"))
    if status in ACCEPTED_STATUSES:
        return "accepted"
    if status in REJECTED_STATUSES:
        return "rejected"
    return status or "unknown"


def _strategy_profile(row: dict[str, Any]) -> str:
    raw = text(row.get("strategy_profile") or row.get("profile") or row.get("strategy_mode")).lower()
    if raw in {"short_vol", "short-vol", "volatility_premium", "vol_premium"}:
        return LEGACY_SHORT_VOL_PROFILE
    return raw or "unknown"


def _parameter_profile_for_candidate(row: dict[str, Any]) -> str:
    profile = _strategy_profile(row)
    if profile in {CURRENT_UNDERWRITING_PROFILE, LEGACY_SHORT_VOL_PROFILE}:
        return CURRENT_UNDERWRITING_PROFILE
    return profile


def _candidate_strategy_family(row: dict[str, Any]) -> str:
    raw = text(
        row.get("strategy_family")
        or row.get("function")
        or row.get("strategy_name")
        or row.get("strategy")
    ).lower().replace("-", "_")
    if raw in {"sell_put", "put"}:
        return "sell_put"
    if raw in {"sell_call", "covered_call", "call"}:
        return "covered_call"
    mode = text(row.get("option_type") or row.get("mode")).lower()
    if mode == "put":
        return "sell_put"
    if mode == "call":
        return "covered_call"
    return "unknown"


def _candidate_change(row: dict[str, Any], *, reasons: list[str]) -> dict[str, Any]:
    return {
        "run_id": row.get("run_id"),
        "account": row.get("account"),
        "symbol": row.get("symbol"),
        "contract_symbol": row.get("contract_symbol"),
        "option_type": row.get("option_type") or row.get("mode"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "dte": row.get("dte"),
        "abs_delta": row.get("abs_delta"),
        "iv_rv_ratio": row.get("iv_rv_ratio"),
        "iv_minus_rv": row.get("iv_minus_rv"),
        "iv_rv_history_percentile": row.get("iv_rv_history_percentile"),
        "iv_rv_history_sample_count": row.get("iv_rv_history_sample_count"),
        "annualized_return": row.get("annualized_return"),
        "baseline_status": _status_group(row),
        "baseline_rule": row.get("filter_rule"),
        "variant_reasons": reasons,
    }


def _account_filter(accounts: list[str] | tuple[str, ...] | None) -> set[str]:
    return {text(account).lower() for account in accounts or [] if text(account)}


def _parse_date_arg(value: str, *, label: str) -> date:
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def _run_date(run_id: str) -> date | None:
    text_value = str(run_id).strip()
    if len(text_value) >= 8 and text_value[:8].isdigit():
        try:
            return datetime.strptime(text_value[:8], "%Y%m%d").date()
        except Exception:
            return None
    return None


def _in_window(value: date | None, *, start: date | None, end: date | None) -> bool:
    if value is None:
        return start is None and end is None
    if start and value < start:
        return False
    if end and value > end:
        return False
    return True


def _render_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    coverage = result["coverage"]
    recommendation = result["recommendation"]
    gates = result.get("gates") or {}
    candidate_impact = result.get("candidate_impact") or {}
    lines = [
        "# Shadow Replay Parameter Backtest",
        "",
        f"- Data mode: {result['data_mode']}",
        f"- Universe: {result['universe_scope']}",
        f"- Coverage: {coverage.get('reason')} / strict={coverage.get('strict_backtest_allowed')}",
        f"- Samples: underwriting {summary['underwriting_candidate_count']} / all {summary['candidate_snapshot_count']}",
        f"- Parameter fields: complete {summary.get('parameter_complete_candidate_count')} / {summary['underwriting_candidate_count']}",
        f"- Baseline accepted: {result['baseline']['accepted_count']}",
        "",
        "## Gates",
        f"- Candidate impact: {(gates.get('candidate_impact') or {}).get('status')} / {(gates.get('candidate_impact') or {}).get('reason')}",
        f"- Production recommendation: {(gates.get('production_recommendation') or {}).get('status')} / {(gates.get('production_recommendation') or {}).get('reason')}",
        f"- Parameter fields: {(gates.get('parameter_fields') or {}).get('status')} / {(gates.get('parameter_fields') or {}).get('reason')}",
        "",
        "## Candidate Impact",
        f"- Status: {candidate_impact.get('status')} / allowed={candidate_impact.get('allowed')}",
        f"- Best by newly accepted: {candidate_impact.get('best_variant_by_new_accepts')}",
        f"- Best by total accepted: {candidate_impact.get('best_variant_by_total_accepts')}",
        f"- Limitations: {candidate_impact.get('limitations') or []}",
        "",
        "## Variants",
    ]
    for variant in result["variants"]:
        lines.extend(
            [
                "",
                f"### {variant['name']}",
                f"- Accepted: {variant['accepted_count']} ({variant['newly_accepted_count']} newly accepted)",
                f"- Rejected: {variant['rejected_count']} ({variant['newly_rejected_count']} newly rejected)",
                f"- IV/RV history: {variant.get('iv_rv_history_status')} / {variant.get('iv_rv_history_modes')}",
                f"- Top reasons: {variant['top_reasons']}",
            ]
        )
    lines.extend(
        [
            "",
            "## Recommendation",
            f"- Status: {recommendation.get('status')}",
            f"- Reason: {recommendation.get('reason')}",
            f"- Next action: {recommendation.get('next_action')}",
        ]
    )
    missing_required = recommendation.get("missing_required_fields")
    if missing_required:
        lines.append(f"- Missing required fields: {missing_required}")
    return "\n".join(lines) + "\n"
