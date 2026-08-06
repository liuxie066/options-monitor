from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_contracts import AgentToolError
from src.application.candidate_reject_summary import candidate_rule_label
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    load_latest_opening_candidate_snapshot,
    load_opening_candidate_snapshot,
)


_FUNCTION_MODE = {"sell_put": "put", "sell_call": "call"}


def candidate_filter_explain_tool(
    payload: dict[str, Any],
    *,
    repo_base: Callable[[], Path],
    mask_path: Callable[[str | Path | None], str | None],
    symbol_aliases: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    raw_symbol = str(payload.get("symbol") or "").strip()
    account = str(payload.get("account") or "").strip().lower()
    if not raw_symbol or not account:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="symbol and account are required",
            hint="Pass the logical account and symbol; run_id is optional.",
        )
    symbol = (
        canonical_symbol(raw_symbol, symbol_aliases=symbol_aliases)
        or raw_symbol.upper()
    )
    function_filter = str(payload.get("function") or "").strip().lower()
    if function_filter and function_filter not in _FUNCTION_MODE:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported opening function: {function_filter}",
            hint="Supported functions: sell_put, sell_call.",
        )
    base = Path(payload.get("runtime_root") or repo_base()).resolve()
    try:
        if str(payload.get("run_id") or "").strip():
            snapshot = load_opening_candidate_snapshot(
                base=base,
                run_id=str(payload["run_id"]).strip(),
                account=account,
            )
        else:
            snapshot = load_latest_opening_candidate_snapshot(
                base=base,
                account=account,
            )
    except OpeningCandidateSnapshotError as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message=str(exc),
            details={"account": account, "run_id": payload.get("run_id")},
        ) from exc

    requested_mode = _FUNCTION_MODE.get(function_filter)
    scoped = [
        dict(item)
        for item in snapshot.get("scope_results") or []
        if isinstance(item, Mapping)
        and str(item.get("symbol") or "").upper() == symbol
        and (
            requested_mode is None
            or str(item.get("strategy_mode") or "") == requested_mode
        )
    ]
    functions = [function_filter] if function_filter else ["sell_put", "sell_call"]
    summaries = [
        _summarize_function(
            function,
            [
                item
                for item in scoped
                if item.get("strategy_mode") == _FUNCTION_MODE[function]
            ],
            run_id=str(snapshot.get("run_id") or "") or None,
            account=account,
        )
        for function in functions
    ]
    status_counts = Counter(str(item.get("status") or "unknown") for item in scoped)
    function_counts = Counter(
        "sell_put" if item.get("strategy_mode") == "put" else "sell_call"
        for item in scoped
    )
    source = {
        "path": mask_path("state/opening_candidate_snapshot.json"),
        "rows": len(scoped),
        "run_ids": [snapshot.get("run_id")],
        "content_sha256": snapshot.get("content_sha256"),
    }
    return (
        {
            "symbol": symbol,
            "raw_symbol": raw_symbol,
            "canonical_symbol": symbol,
            "account": account,
            "scope": {
                "account": account,
                "account_semantics": "opening_candidate_snapshot",
            },
            "opening_status": snapshot.get("opening_status"),
            "evidence_status": "available",
            "conclusion_status": "supported" if scoped else "indeterminate",
            "trace_count": len(scoped),
            "run_ids": [snapshot.get("run_id")],
            "status_counts": dict(status_counts),
            "function_counts": dict(function_counts),
            "functions": summaries,
        },
        ([] if scoped else ["no_matching_snapshot_scope"]),
        {"source_files": [source]},
    )


def _summarize_function(
    function_name: str,
    rows: list[dict[str, Any]],
    *,
    run_id: str | None,
    account: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    events: list[dict[str, Any]] = []
    for row in rows:
        row_reasons = list(row.get("reason_codes") or [])
        reason = str(row.get("reason_code") or "").strip()
        if reason:
            row_reasons.append(reason)
        normalized_reasons = sorted(
            {str(item) for item in row_reasons if str(item).strip()}
        )
        reasons.extend(normalized_reasons)
        if normalized_reasons:
            for reason_code in normalized_reasons:
                events.append(
                    _event(
                        row,
                        reason_code,
                        run_id=run_id,
                        account=account,
                    )
                )
        else:
            events.append(
                _event(
                    row,
                    "candidate_accepted",
                    run_id=run_id,
                    account=account,
                )
            )
    reason_counts = Counter(reasons)
    rejection_counts = Counter(
        reason
        for reason in reasons
        if reason not in {"all_decisions_captured", "candidate_accepted"}
    )
    return {
        "function": function_name,
        "status": _function_status(rows),
        "reason_counts": dict(reason_counts),
        "reason_labels": {
            reason: candidate_rule_label(reason) for reason in sorted(reason_counts)
        },
        "rejection_reason_counts": dict(rejection_counts),
        "rejection_reasons": [
            {
                "rule": reason,
                "label": candidate_rule_label(reason),
                "count": count,
            }
            for reason, count in rejection_counts.most_common(8)
        ],
        "events": events[:20],
    }


def _function_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "not_observed"
    contract_rows = [row for row in rows if row.get("scope") == "contract"]
    if any(row.get("status") == "accepted" for row in contract_rows):
        return "accepted"
    if contract_rows:
        return "rejected"
    return str(rows[0].get("status") or "unknown")


def _event(
    row: Mapping[str, Any],
    reason: str,
    *,
    run_id: str | None,
    account: str,
) -> dict[str, Any]:
    rejected = reason not in {"candidate_accepted", "all_decisions_captured"}
    return {
        "run_id": run_id,
        "account": account,
        "status": "rejected" if rejected else str(row.get("status") or "accepted"),
        "stage": "recorded_opening_decision",
        "rule": reason,
        "rule_label": candidate_rule_label(reason),
        "is_rejection": rejected,
        "metric_value": None,
        "threshold": None,
        "contract_symbol": row.get("contract_symbol"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "message": reason,
        "evidence_path": "state/opening_candidate_snapshot.json",
    }
