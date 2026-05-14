from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.candidate_filter_trace import (
    CANDIDATE_FILTER_FUNCTIONS,
    TRACE_STATUS_ORDER,
    read_candidate_filter_trace,
)


def candidate_filter_explain_tool(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    mask_path: Callable[[str | Path | None], str | None],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="symbol is required",
            hint="Pass symbol together with run_id/account or trace_path.",
        )
    account = str(payload.get("account") or "").strip().lower()
    function_filter = str(payload.get("function") or "").strip().lower()
    if function_filter and function_filter not in CANDIDATE_FILTER_FUNCTIONS:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported function: {function_filter}",
            hint=f"Supported functions: {', '.join(CANDIDATE_FILTER_FUNCTIONS)}.",
        )

    trace_paths = _trace_paths(payload, repo_base=repo_base)
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
        if str(row.get("symbol") or "").strip().upper() == symbol
        and (not account or str(row.get("account") or "").strip().lower() == account)
        and (not function_filter or str(row.get("function") or "").strip().lower() == function_filter)
    ]
    if not matching:
        warnings.append("no_matching_trace_rows: no trace rows matched symbol/account/function")

    functions = [function_filter] if function_filter else list(CANDIDATE_FILTER_FUNCTIONS)
    summaries = [_summarize_function(fn, [row for row in matching if str(row.get("function") or "") == fn]) for fn in functions]

    status_counts = Counter(str(row.get("status") or "") for row in matching)
    function_counts = Counter(str(row.get("function") or "") for row in matching)
    return (
        {
            "symbol": symbol,
            "account": account or None,
            "trace_count": len(matching),
            "status_counts": dict(status_counts),
            "function_counts": dict(function_counts),
            "functions": summaries,
        },
        warnings,
        {"source_files": source_files},
    )


def _trace_paths(payload: dict[str, Any], *, repo_base: Callable[[], Path]) -> list[Path]:
    base = repo_base()
    explicit: list[Any] = []
    if payload.get("trace_path"):
        explicit.append(payload.get("trace_path"))
    raw_trace_paths = payload.get("trace_paths")
    if isinstance(raw_trace_paths, list):
        explicit.extend(raw_trace_paths)
    if explicit:
        return [path for path in (_resolve_path(value, base=base) for value in explicit) if path.exists()]

    account = str(payload.get("account") or "").strip().lower()
    candidates: list[Path] = []
    if payload.get("run_dir"):
        run_dir = _resolve_path(payload.get("run_dir"), base=base)
    elif payload.get("run_id"):
        run_dir = (base / "output_runs" / str(payload.get("run_id")).strip()).resolve()
    else:
        run_dir = None

    if run_dir is not None:
        if account:
            candidates.append(run_dir / "accounts" / account / "candidate_filter_trace.jsonl")
        else:
            candidates.extend(sorted(run_dir.glob("accounts/*/candidate_filter_trace.jsonl")))

    if payload.get("report_dir"):
        candidates.append(_resolve_path(payload.get("report_dir"), base=base) / "candidate_filter_trace.jsonl")

    candidates.append(base / "output" / "reports" / "candidate_filter_trace.jsonl")
    candidates.append(base / "output" / "agent_plugin" / "reports" / "candidate_filter_trace.jsonl")

    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _summarize_function(function_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "function": function_name,
            "status": "not_observed",
            "reason_counts": {},
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
    return {
        "function": function_name,
        "status": str(ordered[0].get("status") or "unknown"),
        "reason_counts": dict(reason_counts),
        "events": [_event_summary(row) for row in ordered[:20]],
    }


def _event_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": row.get("status"),
        "stage": row.get("stage"),
        "rule": row.get("rule"),
        "metric_value": row.get("metric_value"),
        "threshold": row.get("threshold"),
        "contract_symbol": row.get("contract_symbol"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "message": row.get("message"),
        "evidence_path": row.get("evidence_path"),
    }


def _resolve_path(value: Any, *, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path
