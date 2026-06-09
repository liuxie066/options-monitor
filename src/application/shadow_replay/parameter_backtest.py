from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.application.shadow_replay.analysis import analyze_rows
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
    abs_first_float,
    dataset_dir_from_arg,
    first_float,
    instrument_key,
    normal_status,
    read_jsonl,
    resolve_output_path,
    safe_rel,
    safety_payload,
    text,
    utc_now,
    write_json,
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
PARAMETER_BACKTEST_SCHEMA_VERSION = "shadow_replay_parameter_backtest.v1"
ACCEPTED_STATUSES = {"accepted", "notified"}
REJECTED_STATUSES = {"rejected", "post_filtered", "ranked_below"}
PARAMETER_FIELD_MAP = {
    "min_iv_rv_ratio": "iv_rv_ratio",
    "min_iv_minus_rv": "iv_minus_rv",
    "min_abs_delta": "abs_delta",
    "max_abs_delta": "abs_delta",
    "min_dte": "dte",
    "max_dte": "dte",
    "min_annualized_return": "annualized_return",
}


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


def run_shadow_replay_parameter_backtest(
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
    """Compatibility wrapper for the legacy parameter-backtest entrypoint."""

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
        schema_version=PARAMETER_BACKTEST_SCHEMA_VERSION,
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
    recommendation = _recommendation(
        data_mode=data_mode,
        evidence_quality=evidence_quality,
        gates=gates,
        candidate_impact=candidate_impact,
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
            "min_sample": sample_floor,
            "variant_count": len(variants),
            "parameter_complete_candidate_count": evidence_quality["complete_candidate_count"],
        },
        "evidence_quality": evidence_quality,
        "baseline": baseline,
        "variants": variants,
        "gates": gates,
        "candidate_impact": candidate_impact,
        "recommendation": recommendation,
        "safety": safety_payload(writes_local_dataset=False),
    }
    if format_norm == "markdown":
        result["report_markdown"] = _render_markdown(result)
    if output:
        output_path = resolve_output_path(output)
        if format_norm == "markdown":
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(result.get("report_markdown") or _render_markdown(result), encoding="utf-8")
        else:
            write_json(output_path, result)
    return result


def _load_dataset_evidence(dataset: str | Path, *, base: Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    return {
        "candidate_snapshots": read_jsonl(dataset_dir / "candidate_snapshots.jsonl"),
        "filter_decisions": read_jsonl(dataset_dir / "filter_decisions.jsonl"),
        "mark_snapshots": read_jsonl(dataset_dir / "mark_path_snapshots.jsonl"),
        "outcome_facts": read_jsonl(dataset_dir / "outcome_facts.jsonl"),
        "source": {
            "mode": "dataset",
            "dataset_dir": safe_rel(dataset_dir, base=base),
        },
    }


def _dataset_coverage(*, evidence: dict[str, Any], dataset: str | Path) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    candidate_count = len(evidence["candidate_snapshots"])
    return {
        "mode": "dataset",
        "dataset_dir": str(dataset_dir),
        "requested_start_date": None,
        "requested_end_date": None,
        "available_scanned_runs": None,
        "selected_scanned_runs": None,
        "strict_backtest_allowed": candidate_count > 0,
        "reason": "dataset_candidate_universe_ready" if candidate_count > 0 else "candidate_universe_missing",
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
    return {
        "candidate_snapshots": candidates,
        "filter_decisions": decisions,
        "mark_snapshots": read_replay_rows(mark_paths, schema_version=MARK_PATH_SCHEMA_VERSION, base=base),
        "outcome_facts": read_replay_rows(outcome_paths, schema_version=OUTCOME_FACT_SCHEMA_VERSION, base=base),
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
    return {
        "name": "production_observed",
        "candidate_count": len(candidates),
        "accepted_count": int(status_counts.get("accepted", 0)),
        "rejected_count": int(status_counts.get("rejected", 0)),
        "status_counts": dict(sorted(status_counts.items())),
        "analysis_summary": analysis["summary"],
        "insurance_metrics": analysis["insurance_metrics"],
        "outcome_stats": analysis["outcome_stats"],
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
    evaluated_rows: list[dict[str, Any]] = []
    synthetic_decisions: list[dict[str, Any]] = []
    newly_accepted: list[dict[str, Any]] = []
    newly_rejected: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    safety_counts: Counter[str] = Counter()
    missing_field_counts: Counter[str] = Counter()
    for row in baseline_candidates:
        evaluation = _evaluate_candidate(row, variant=variant)
        reason_counts.update(evaluation["reasons"])
        safety_counts.update(evaluation["safety_reasons"])
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
    return {
        "name": variant.name,
        "parameters": variant.to_payload()["profiles"],
        "candidate_count": len(evaluated_rows),
        "accepted_count": int(status_counts.get("accepted", 0)),
        "rejected_count": int(status_counts.get("rejected", 0)),
        "newly_accepted_count": len(newly_accepted),
        "newly_rejected_count": len(newly_rejected),
        "safety_violation_count": sum(safety_counts.values()),
        "status_counts": dict(sorted(status_counts.items())),
        "top_reasons": dict(reason_counts.most_common(20)),
        "safety_reasons": dict(safety_counts.most_common(20)),
        "missing_fields": dict(missing_field_counts.most_common(20)),
        "newly_accepted_samples": newly_accepted[:20],
        "newly_rejected_samples": newly_rejected[:20],
        "analysis_summary": analysis["summary"],
        "insurance_metrics": analysis["insurance_metrics"],
        "outcome_stats": analysis["outcome_stats"],
        "decision_quality": analysis["decision_quality"]["summary"],
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
    if field == "abs_delta":
        return abs_first_float(row, "abs_delta", "delta") is not None
    return first_float(row, field) is not None


def _evaluate_candidate(row: dict[str, Any], *, variant: ParameterVariant) -> dict[str, Any]:
    profile = _parameter_profile_for_candidate(row)
    params = variant.profiles.get(profile)
    if not params:
        return {"status": "rejected", "reasons": ["strategy_profile_out_of_scope"], "safety_reasons": []}
    safety = _safety_reasons(row)
    if safety:
        return {"status": "rejected", "reasons": safety, "safety_reasons": safety}
    reasons: list[str] = []
    _check_min(row, params, "min_iv_rv_ratio", "iv_rv_ratio", reasons)
    _check_min(row, params, "min_iv_minus_rv", "iv_minus_rv", reasons)
    _check_min_abs_delta(row, params, reasons)
    _check_max_abs_delta(row, params, reasons)
    _check_min(row, params, "min_dte", "dte", reasons)
    _check_max(row, params, "max_dte", "dte", reasons)
    _check_min(row, params, "min_annualized_return", "annualized_return", reasons)
    status = "rejected" if reasons else "accepted"
    return {"status": status, "reasons": reasons or ["parameter_pass"], "safety_reasons": []}


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


def _check_min_abs_delta(row: dict[str, Any], params: dict[str, float], reasons: list[str]) -> None:
    if "min_abs_delta" not in params:
        return
    value = abs_first_float(row, "abs_delta", "delta")
    if value is None:
        reasons.append("abs_delta_missing")
    elif value < params["min_abs_delta"]:
        reasons.append("abs_delta_below_min_abs_delta")


def _check_max_abs_delta(row: dict[str, Any], params: dict[str, float], reasons: list[str]) -> None:
    if "max_abs_delta" not in params:
        return
    value = abs_first_float(row, "abs_delta", "delta")
    if value is None:
        reasons.append("abs_delta_missing")
    elif value > params["max_abs_delta"]:
        reasons.append("abs_delta_above_max_abs_delta")


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
    concentration = first_float(row, "single_trade_concentration")
    max_concentration = first_float(row, "max_single_trade_nav_pct") or 0.05
    if concentration is not None and concentration > max_concentration:
        reasons.append("single_trade_concentration_above_safety_floor")
    premium = first_float(row, "net_income", "net_income_cny")
    if premium is not None and premium <= 0:
        reasons.append("premium_not_positive")
    open_interest = first_float(row, "open_interest")
    volume = first_float(row, "volume")
    if open_interest is not None and volume is not None and open_interest <= 0 and volume <= 0:
        reasons.append("liquidity_zero")
    return reasons


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


def _filter_candidates(
    rows: list[dict[str, Any]],
    *,
    accounts: set[str],
    market: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        account = text(row.get("account")).lower()
        if accounts and account and account not in accounts:
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
    scoped_keys = {instrument_key(row) for row in scoped_candidates if instrument_key(row)}
    if not scoped_keys:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        key = instrument_key(row)
        if not key or key not in scoped_keys:
            continue
        account = text(row.get("account")).lower()
        if accounts and account and account not in accounts:
            continue
        if market and not _market_matches(row, market):
            continue
        out.append(row)
    return out


def _market_matches(row: dict[str, Any], market: str) -> bool:
    symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
    if not symbol:
        return True
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
        return {
            "status": "ready_for_live_shadow_candidate_review",
            "reason": "outcome_evidence_missing",
            "candidate_impact_allowed": True,
            "candidate_impact_reason": candidate_impact.get("reason"),
            "candidate_variant": candidate_impact.get("best_variant_by_new_accepts"),
            "parameter_field_status": field_gate.get("status"),
            "missing_required_fields": evidence_quality.get("missing_required_fields"),
            "production_recommendation_allowed": False,
            "next_action": "review_candidate_impact_and_collect_mark_outcomes_before_production_change",
        }
    return {
        "status": "ready_for_live_shadow_review",
        "reason": "closed_replay_available",
        "candidate_impact_allowed": True,
        "candidate_variant": candidate_impact.get("best_variant_by_new_accepts"),
        "parameter_field_status": field_gate.get("status"),
        "production_recommendation_allowed": True,
        "next_action": "review_variant_then_run_live_shadow_before_production_change",
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
    production_gate = _production_recommendation_gate(
        candidate_gate=candidate_gate,
        data_mode=data_mode,
    )
    return {
        "scan_artifacts": scan_gate,
        "sample_size": sample_gate,
        "parameter_fields": field_gate,
        "candidate_impact": candidate_gate,
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
    if not variants or max(int(row.get("accepted_count") or 0) for row in variants) <= 0:
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


def _production_recommendation_gate(*, candidate_gate: dict[str, Any], data_mode: str) -> dict[str, Any]:
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
            "reason": "outcome_evidence_missing",
            "next_action": "collect_mark_outcomes_before_parameter_recommendation",
            "runtime_config_write_allowed": False,
        }
    return {
        "allowed": True,
        "status": "review_ready",
        "reason": "closed_replay_available",
        "next_action": "human_review_then_live_shadow",
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
        }
        for row in variants
    ]
    best_by_new = None
    best_by_total = None
    if variant_summaries:
        best_by_new = max(
            variant_summaries,
            key=lambda row: (row["newly_accepted_count"], row["accepted_count"]),
        )
        best_by_total = max(
            variant_summaries,
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


def _data_mode(marks: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> str:
    if outcomes and any(is_usable_mark(row) for row in marks):
        return "closed_replay"
    if any(is_usable_mark(row) for row in marks):
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
