from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.candidate_filter_trace_discovery import (
    CandidateFilterTraceDiscovery,
    discover_candidate_filter_trace_paths,
)
from src.application.candidate_filter_trace import (
    CANDIDATE_FILTER_FUNCTIONS,
    TRACE_STATUS_ORDER,
    read_candidate_filter_trace,
)
from src.application.candidate_reject_summary import candidate_rule_label


_REJECTION_STATUSES = {"rejected", "reject", "filtered", "post_filtered", "excluded", "blocked", "skip", "skipped"}


def candidate_filter_explain_tool(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    mask_path: Callable[[str | Path | None], str | None],
    symbol_aliases: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    raw_symbol = str(payload.get("symbol") or "").strip()
    if not raw_symbol:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="symbol is required",
            hint="Pass symbol with optional account/function/run_id; trace discovery uses the runtime root automatically.",
        )
    symbol = _symbol_for_match(raw_symbol, symbol_aliases=symbol_aliases)
    account = str(payload.get("account") or "").strip().lower()
    function_filter = str(payload.get("function") or "").strip().lower()
    if function_filter and function_filter not in CANDIDATE_FILTER_FUNCTIONS:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported function: {function_filter}",
            hint=f"Supported functions: {', '.join(CANDIDATE_FILTER_FUNCTIONS)}.",
        )

    trace_discovery = discover_candidate_filter_trace_paths(payload, repo_base=repo_base)
    trace_paths = list(trace_discovery.paths)
    warnings: list[str] = []
    if not trace_paths:
        warnings.append("no_trace_files: candidate_filter_trace.jsonl not found")

    loaded_rows: list[dict[str, Any]] = []
    source_files: list[dict[str, Any]] = []
    for path in trace_paths:
        rows = read_candidate_filter_trace(path)
        if rows:
            source_files.append({"path": mask_path(path), "rows": len(rows)})
            loaded_rows.extend(rows)

    matching = [
        row
        for row in loaded_rows
        if _symbol_for_match(row.get("symbol"), symbol_aliases=symbol_aliases) == symbol
        and (not account or str(row.get("account") or "").strip().lower() == account)
        and (not function_filter or str(row.get("function") or "").strip().lower() == function_filter)
    ]
    if not matching:
        warnings.append("no_matching_trace_rows: no trace rows matched symbol/account/function")

    functions = [function_filter] if function_filter else list(CANDIDATE_FILTER_FUNCTIONS)
    summaries = [_summarize_function(fn, [row for row in matching if str(row.get("function") or "") == fn]) for fn in functions]

    status_counts = Counter(str(row.get("status") or "") for row in matching)
    function_counts = Counter(str(row.get("function") or "") for row in matching)
    evidence_status = "available" if matching else ("trace_files_missing" if not trace_paths else "no_matching_rows")
    return (
        {
            "symbol": symbol,
            "raw_symbol": raw_symbol,
            "canonical_symbol": symbol,
            "account": account or None,
            "scope": {
                "account": account or None,
                "account_semantics": "scan_scope",
            },
            "evidence_status": evidence_status,
            "conclusion_status": "supported" if matching else "indeterminate",
            "trace_count": len(matching),
            "status_counts": dict(status_counts),
            "function_counts": dict(function_counts),
            "functions": summaries,
        },
        warnings,
        {
            "source_files": source_files,
            "trace_discovery": _trace_discovery_meta(trace_discovery, mask_path=mask_path),
        },
    )


def _symbol_for_match(value: Any, *, symbol_aliases: Mapping[str, Any] | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return canonical_symbol(raw, symbol_aliases=symbol_aliases) or raw.upper()


def _trace_paths(payload: dict[str, Any], *, repo_base: Callable[[], Path]) -> list[Path]:
    return list(discover_candidate_filter_trace_paths(payload, repo_base=repo_base).paths)


def _trace_discovery_meta(
    discovery: CandidateFilterTraceDiscovery,
    *,
    mask_path: Callable[[str | Path | None], str | None],
) -> dict[str, Any]:
    return {
        "strategy": "explicit_paths" if discovery.explicit_paths else "runtime_roots_latest_runs",
        "roots": [mask_path(path) for path in discovery.roots],
        "run_dirs": [mask_path(path) for path in discovery.run_dirs[:10]],
        "run_dir_count": len(discovery.run_dirs),
        "considered_path_count": len(discovery.considered_paths),
        "matched_file_count": len(discovery.paths),
    }


def _summarize_function(function_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "function": function_name,
            "status": "not_observed",
            "reason_counts": {},
            "reason_labels": {},
            "rejection_reason_counts": {},
            "rejection_reasons": [],
            "events": [],
        }
    ordered = sorted(
        rows,
        key=lambda row: (
            TRACE_STATUS_ORDER.get(str(row.get("status") or ""), 99),
            str(row.get("stage") or ""),
            str(row.get("rule") or ""),
        ),
    )
    reason_counts = Counter(str(row.get("rule") or "unknown") for row in rows)
    rejection_reason_counts = Counter(str(row.get("rule") or "unknown") for row in rows if _is_rejection_row(row))
    reason_labels = {rule: candidate_rule_label(rule) for rule in sorted(reason_counts)}
    return {
        "function": function_name,
        "status": str(ordered[0].get("status") or "unknown"),
        "reason_counts": dict(reason_counts),
        "reason_labels": reason_labels,
        "rejection_reason_counts": dict(rejection_reason_counts),
        "rejection_reasons": [
            {"rule": rule, "label": candidate_rule_label(rule), "count": count}
            for rule, count in rejection_reason_counts.most_common(8)
        ],
        "events": [_event_summary(row) for row in ordered[:20]],
    }


def _event_summary(row: dict[str, Any]) -> dict[str, Any]:
    rule = str(row.get("rule") or "").strip()
    return {
        "status": row.get("status"),
        "stage": row.get("stage"),
        "rule": row.get("rule"),
        "rule_label": candidate_rule_label(rule) if rule else None,
        "is_rejection": _is_rejection_row(row),
        "metric_value": row.get("metric_value"),
        "threshold": row.get("threshold"),
        "contract_symbol": row.get("contract_symbol"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "message": row.get("message"),
        "evidence_path": row.get("evidence_path"),
    }


def _is_rejection_row(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    rule = str(row.get("rule") or "").strip().lower()
    return status in _REJECTION_STATUSES and rule not in {"candidate_accepted", "accepted"}
