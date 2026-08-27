from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict

from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_contracts import AgentToolError
from src.application.candidate_reject_summary import candidate_rule_label
from src.application.candidate_snapshot_manifest import (
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle_readonly,
    load_latest_candidate_snapshot_bundle_readonly,
)
from src.application.notification_perception_read import (
    iter_notification_perception_events,
)
from src.application.runtime_paths import resolve_runtime_root


_FUNCTION_MODE = {"sell_put": "put", "sell_call": "call"}
_RUN_SELECTORS = {"latest", "latest_notification"}
_DELIVERED_EVENT_KIND = "notification_delivery_completed"


def _local_timezone() -> timezone:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _event_local_date(event: Mapping[str, Any], tz: timezone) -> date | None:
    raw = str(event.get("created_at_utc") or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def _event_visible_to_account(event: Mapping[str, Any], account: str) -> bool:
    send_summary = event.get("send_summary")
    if isinstance(send_summary, Mapping):
        sent = send_summary.get("sent_accounts")
        if isinstance(sent, list) and sent:
            return account in {str(item).strip().lower() for item in sent}
        failure_count = int(send_summary.get("failure_count") or 0)
        if isinstance(sent, list) and not sent and failure_count > 0:
            return False
    accounts = event.get("accounts")
    if isinstance(accounts, list):
        return account in {str(item).strip().lower() for item in accounts}
    return False


def _is_delivered_notification(event: Mapping[str, Any], account: str) -> bool:
    if str(event.get("event_kind") or "").strip() != _DELIVERED_EVENT_KIND:
        return False
    if event.get("no_send") is True:
        return False
    return _event_visible_to_account(event, account)


class NotificationRunResolution(TypedDict, total=False):
    run_id: str
    matched_event_created_at_utc: Any
    truncated: bool
    total_count: int


def _resolve_notification_run(
    *,
    base: Path,
    account: str,
    notification_date: date,
    tz: timezone,
) -> NotificationRunResolution:
    result = iter_notification_perception_events(
        repo_root=base,
        event_kind=_DELIVERED_EVENT_KIND,
    )
    resolution: NotificationRunResolution = {
        "truncated": bool(result.get("truncated")),
        "total_count": int(result.get("total_count") or 0),
    }
    for event in result.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        if not _is_delivered_notification(event, account):
            continue
        if _event_local_date(event, tz) != notification_date:
            continue
        run_id = str(event.get("run_id") or "").strip()
        if run_id:
            resolution["run_id"] = run_id
            resolution["matched_event_created_at_utc"] = event.get("created_at_utc")
            return resolution
    return resolution


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
    base = resolve_runtime_root(
        repo_root=repo_base(),
        runtime_root=payload.get("runtime_root"),
    ).runtime_root
    run_selector = str(payload.get("run_selector") or "").strip().lower() or None
    if run_selector is not None and run_selector not in _RUN_SELECTORS:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported run_selector: {run_selector}",
            hint="Supported selectors: latest, latest_notification.",
        )
    raw_notification_date = str(payload.get("notification_date") or "").strip()
    if raw_notification_date and run_selector != "latest_notification":
        raise AgentToolError(
            code="INPUT_ERROR",
            message="notification_date requires run_selector=latest_notification",
            hint="Pass run_selector=latest_notification to resolve a run from delivered notifications.",
        )
    tz = _local_timezone()
    try:
        notification_date = (
            date.fromisoformat(raw_notification_date)
            if raw_notification_date
            else datetime.now(tz).date()
        )
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="notification_date must be ISO YYYY-MM-DD",
            hint="Pass notification_date as an ISO date, for example 2026-08-13.",
        ) from exc
    run_resolution: dict[str, Any] | None = None
    try:
        if str(payload.get("run_id") or "").strip():
            bundle = load_candidate_snapshot_bundle_readonly(
                base=base,
                run_id=str(payload["run_id"]).strip(),
                account=account,
            )
            run_resolution = {
                "selector": "explicit_run_id",
                "resolved_run_id": str(payload["run_id"]).strip(),
            }
        elif run_selector == "latest_notification":
            resolved = _resolve_notification_run(
                base=base,
                account=account,
                notification_date=notification_date,
                tz=tz,
            )
            if not resolved.get("run_id"):
                reason = (
                    "audit_window_truncated"
                    if resolved.get("truncated")
                    else "no_notification_run"
                )
                raise AgentToolError(
                    code="DEPENDENCY_MISSING",
                    message=(
                        "no delivered notification run found for account "
                        f"{account} on {notification_date.isoformat()}"
                        + (
                            " (notification audit window truncated before reaching that date)"
                            if resolved.get("truncated")
                            else ""
                        )
                    ),
                    details={
                        "reason": reason,
                        "account": account,
                        "notification_date": notification_date.isoformat(),
                        "audit_total_count": resolved.get("total_count"),
                    },
                )
            try:
                bundle = load_candidate_snapshot_bundle_readonly(
                    base=base,
                    run_id=resolved["run_id"],
                    account=account,
                )
            except CandidateSnapshotManifestError as exc:
                raise AgentToolError(
                    code="DEPENDENCY_MISSING",
                    message=str(exc),
                    details={
                        "reason": "snapshot_unavailable_for_notification_run",
                        "account": account,
                        "run_id": resolved["run_id"],
                        "notification_date": notification_date.isoformat(),
                    },
                ) from exc
            run_resolution = {
                "selector": run_selector,
                "notification_date": notification_date.isoformat(),
                "timezone": str(tz),
                "resolved_run_id": resolved["run_id"],
                "matched_event_created_at_utc": resolved.get(
                    "matched_event_created_at_utc"
                ),
            }
        else:
            bundle = load_latest_candidate_snapshot_bundle_readonly(
                base=base,
                account=account,
            )
            run_resolution = {
                "selector": "latest",
                "resolved_run_id": None,
            }
        snapshot = (bundle.get("owners") or {}).get("opening")
        if not isinstance(snapshot, dict):
            raise CandidateSnapshotManifestError(
                "manifest-bound opening candidate snapshot is unavailable"
            )
    except AgentToolError:
        raise
    except CandidateSnapshotManifestError as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message=str(exc),
            details={"account": account, "run_id": payload.get("run_id")},
        ) from exc
    if run_resolution is not None and run_resolution.get("resolved_run_id") is None:
        run_resolution["resolved_run_id"] = snapshot.get("run_id")

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
        "manifest_content_sha256": (bundle.get("manifest") or {}).get(
            "content_sha256"
        ),
        "authority": "terminal_manifest_bound_opening_candidate_snapshot",
        "scan_mode": snapshot.get("scan_mode"),
        "account_display_name": snapshot.get("account_display_name"),
        "executable": snapshot.get("executable"),
    }
    if run_resolution is not None:
        source["run_resolution"] = run_resolution
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
    rejection_reasons: list[str] = []
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
        is_contract = row.get("scope") == "contract"
        is_rejected_contract = is_contract and row.get("status") == "rejected"
        if is_rejected_contract:
            rejection_reasons.extend(normalized_reasons)
            for reason_code in normalized_reasons:
                events.append(
                    _event(
                        row,
                        reason_code,
                        run_id=run_id,
                        account=account,
                    )
                )
        elif is_contract and row.get("status") == "accepted":
            events.append(
                _event(
                    row,
                    "candidate_accepted",
                    run_id=run_id,
                    account=account,
                )
            )
        elif normalized_reasons:
            for reason_code in normalized_reasons:
                events.append(
                    _event(
                        row,
                        reason_code,
                        run_id=run_id,
                        account=account,
                    )
                )
    reason_counts = Counter(reasons)
    rejection_counts = Counter(rejection_reasons)
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
    rejected = (
        row.get("scope") == "contract"
        and row.get("status") == "rejected"
        and reason != "candidate_accepted"
    )
    reject_detail = next(
        (
            dict(item)
            for item in row.get("rejects") or []
            if isinstance(item, Mapping)
            and str(item.get("reason") or "") == reason
        ),
        {},
    )
    return {
        "run_id": run_id,
        "account": account,
        "status": "rejected" if rejected else str(row.get("status") or "accepted"),
        "stage": reject_detail.get("stage") or "recorded_opening_decision",
        "rule": reason,
        "rule_label": candidate_rule_label(reason),
        "is_rejection": rejected,
        "metric_value": reject_detail.get("metric_value"),
        "threshold": reject_detail.get("threshold"),
        "contract_symbol": row.get("contract_symbol"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "message": reject_detail.get("message") or reason,
        "candidate_id": row.get("candidate_id"),
        "decision_hash": row.get("decision_hash"),
        "normalized_input_hash": row.get("normalized_input_hash"),
        "evidence_path": "state/opening_candidate_snapshot.json",
    }
