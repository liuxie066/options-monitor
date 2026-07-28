from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.trades.deal_identity import (
    broker_deal_key_from_payload,
    completed_ledger_deal_keys,
)
from src.application.trades.history_backfill import fetch_opend_history_deals
from src.application.trades.lifecycle_reconciliation import discover_lifecycle_cases
from src.application.trades.inbox import (
    enqueue_trade_payload,
    mark_trade_payload_handled,
    mark_trade_payload_retryable,
    settle_trade_payload_result,
)
from src.application.trades.state import (
    append_trade_intake_audit,
    is_retryable_unresolved_deal,
    load_trade_intake_state,
    lookup_deal_state_entry,
    upsert_deal_state,
    write_trade_intake_state,
)
from src.infrastructure.io_utils import atomic_write_json, read_json, utc_now


def payload_deal_id(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("deal_id", "dealID", "dealId", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def run_history_backfill(
    *,
    repo: Any,
    state_path: Path,
    audit_path: Path,
    account_mapping: dict[str, str],
    futu_account_ids: list[str],
    apply_changes: bool,
    host: str,
    port: int,
    config: dict[str, Any],
    config_path: Path,
    runtime_root: Path,
    backfill_config: dict[str, Any],
    on_result_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
    process_payload_fn: Callable[..., dict[str, Any]],
    on_stock_holdings_sync_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    process_lock: Any | None = None,
    inbox_path: Path | None = None,
    checkpoint_path: Path | None = None,
    history_deals_fn: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = fetch_opend_history_deals,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    started_at = utc_now()
    now = now_fn() if callable(now_fn) else datetime.now(timezone.utc)
    configured_lookback_hours = float(backfill_config.get("lookback_hours") or 6)
    checkpoint_file = Path(
        checkpoint_path
        or state_path.with_name("trade_intake_backfill_checkpoint.json")
    )
    checkpoint = read_json(checkpoint_file, default={})
    checkpoint_payload = checkpoint if isinstance(checkpoint, dict) else {}
    lookback_hours = _effective_lookback_hours(
        configured_lookback_hours=configured_lookback_hours,
        checkpoint=checkpoint_payload,
        now=now,
    )
    append_trade_intake_audit(
        audit_path,
        {
            "phase": "backfill_check_started",
            "source": "backfill",
            "started_at_utc": started_at,
            "lookback_hours": lookback_hours,
            "configured_lookback_hours": configured_lookback_hours,
            "checkpoint_path": str(checkpoint_file),
            "checkpoint_window_end_utc": checkpoint_payload.get(
                "last_successful_window_end_utc"
            ),
        },
    )
    try:
        payloads, diagnostics = history_deals_fn(
            host=host,
            port=port,
            futu_account_ids=futu_account_ids,
            lookback_hours=lookback_hours,
            now=now,
        )
        diagnostics = dict(diagnostics or {})
        diagnostics.update(
            {
                "configured_lookback_hours": configured_lookback_hours,
                "effective_lookback_hours": lookback_hours,
                "checkpoint_path": str(checkpoint_file),
                "checkpoint_window_end_utc": checkpoint_payload.get(
                    "last_successful_window_end_utc"
                ),
            }
        )
    except Exception as exc:
        finished_at = utc_now()
        error = f"{type(exc).__name__}: {exc}"
        append_trade_intake_audit(
            audit_path,
            {
                "phase": "backfill_failed",
                "source": "backfill",
                "started_at_utc": started_at,
                "finished_at_utc": finished_at,
                "error": error,
            },
        )
        return {
            "ok": False,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at,
            "deal_count": 0,
            "applied_count": 0,
            "skipped_duplicate_count": 0,
            "failed_count": 1,
            "unresolved_count": 0,
            "error": error,
        }

    applied_count = 0
    skipped_duplicate_count = 0
    failed_count = 0
    unresolved_count = 0
    last_result: dict[str, Any] | None = None
    durable_queue_complete = True
    lifecycle_discovery_before = _lifecycle_discovery_after_backfill_phase(
        repo=repo,
        account=None,
        observed_at_ms=int(now.astimezone(timezone.utc).timestamp() * 1000),
        apply_changes=apply_changes,
        audit_path=audit_path,
        phase="backfill_lifecycle_discovery_before",
    )
    lock_context = process_lock if process_lock is not None else contextlib.nullcontext()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        deal_id = payload_deal_id(payload)
        deal_key = broker_deal_key_from_payload(
            payload,
            account_mapping=account_mapping,
        )
        inbox_id: str | None = None
        if apply_changes:
            try:
                inbox_id = enqueue_trade_payload(
                    inbox_path
                    or state_path.with_name("trade_intake_inbox.sqlite3"),
                    payload=payload,
                    source="backfill",
                    broker_deal_key=deal_key,
                )
            except Exception as exc:
                durable_queue_complete = False
                failed_count += 1
                append_trade_intake_audit(
                    audit_path,
                    {
                        "phase": "backfill_inbox_failed",
                        "source": "backfill",
                        "deal_id": deal_id or None,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue
        append_trade_intake_audit(
            audit_path,
            {
                "phase": "backfill_received",
                "source": "backfill",
                "deal_id": deal_id or None,
                "payload": payload,
            },
        )
        with lock_context:
            state = load_trade_intake_state(state_path)
            duplicate_reason = _state_duplicate_reason(state, deal_key)
            ledger_ids = _ledger_recorded_deal_ids(repo)
            if duplicate_reason is None and deal_key and deal_key in ledger_ids:
                duplicate_reason = "ledger_event_already_recorded"
                state = _record_ledger_duplicate_state(
                    state=state,
                    state_path=state_path,
                    deal_id=deal_key,
                    apply_changes=apply_changes,
                )
            if duplicate_reason is not None:
                skipped_duplicate_count += 1
                append_trade_intake_audit(
                    audit_path,
                    {
                        "phase": "backfill_skipped_duplicate",
                        "source": "backfill",
                        "deal_id": deal_id or None,
                        "reason": duplicate_reason,
                    },
                )
                last_result = {
                    "status": "skipped",
                    "action": None,
                    "reason": duplicate_reason,
                    "deal_id": deal_id or None,
                    "account": None,
                }
                if inbox_id:
                    mark_trade_payload_handled(
                        inbox_path
                        or state_path.with_name("trade_intake_inbox.sqlite3"),
                        inbox_id=inbox_id,
                        result=last_result,
                    )
                continue

            try:
                result = process_payload_fn(
                    payload,
                    repo=repo,
                    state_path=state_path,
                    audit_path=audit_path,
                    account_mapping=account_mapping,
                    futu_account_ids=futu_account_ids,
                    apply_changes=apply_changes,
                    host=host,
                    port=port,
                    config=config,
                    config_path=config_path,
                    runtime_root=runtime_root,
                    on_result_fn=on_result_fn,
                    on_stock_holdings_sync_fn=on_stock_holdings_sync_fn,
                    source="backfill",
                )
            except Exception as exc:
                failed_count += 1
                error = f"{type(exc).__name__}: {exc}"
                last_result = {
                    "status": "failed",
                    "action": None,
                    "reason": "backfill_pipeline_exception",
                    "deal_id": deal_id or None,
                    "account": None,
                    "error": error,
                }
                if inbox_id:
                    mark_trade_payload_retryable(
                        inbox_path
                        or state_path.with_name("trade_intake_inbox.sqlite3"),
                        inbox_id=inbox_id,
                        error=error,
                    )
                append_trade_intake_audit(
                    audit_path,
                    {
                        "phase": "backfill_pipeline_failed",
                        "source": "backfill",
                        "deal_id": deal_id or None,
                        "error": error,
                    },
                )
                continue
            if inbox_id:
                settle_trade_payload_result(
                    inbox_path
                    or state_path.with_name("trade_intake_inbox.sqlite3"),
                    inbox_id=inbox_id,
                    result=result,
                )
        last_result = dict(result)
        status = str(result.get("status") or "").strip().lower()
        if status == "applied":
            applied_count += 1
            append_trade_intake_audit(
                audit_path,
                {
                    "phase": "backfill_applied",
                    "source": "backfill",
                    "deal_id": result.get("deal_id") or deal_id or None,
                    "action": result.get("action"),
                    "reason": result.get("reason"),
                },
            )
        elif status == "skipped" and str(result.get("reason") or "").strip() == "duplicate_deal_id":
            skipped_duplicate_count += 1
            append_trade_intake_audit(
                audit_path,
                {
                    "phase": "backfill_skipped_duplicate",
                    "source": "backfill",
                    "deal_id": result.get("deal_id") or deal_id or None,
                    "reason": "duplicate_deal_id",
                },
            )
        elif status == "unresolved":
            unresolved_count += 1
        elif status == "failed":
            failed_count += 1

    checkpoint_advanced = False
    history_query_complete = _history_query_complete(
        diagnostics,
        expected_account_ids=futu_account_ids,
    )
    if apply_changes and durable_queue_complete and history_query_complete:
        window_end_utc = str(
            diagnostics.get("window_end_utc")
            or now.astimezone(timezone.utc).isoformat()
        )
        atomic_write_json(
            checkpoint_file,
            {
                "last_successful_window_end_utc": window_end_utc,
                "configured_lookback_hours": configured_lookback_hours,
                "last_effective_lookback_hours": lookback_hours,
                "updated_at_utc": utc_now(),
            },
        )
        checkpoint_advanced = True
    diagnostics["history_query_complete"] = history_query_complete
    diagnostics["durable_queue_complete"] = durable_queue_complete
    diagnostics["checkpoint_advanced"] = checkpoint_advanced
    lifecycle_discovery_after = _lifecycle_discovery_after_backfill_phase(
        repo=repo,
        account=None,
        observed_at_ms=int(now.astimezone(timezone.utc).timestamp() * 1000),
        apply_changes=apply_changes,
        audit_path=audit_path,
        phase="backfill_lifecycle_reconciliation_after",
    )
    diagnostics["lifecycle_reconciliation"] = {
        "before": lifecycle_discovery_before,
        "after": lifecycle_discovery_after,
    }

    finished_at = utc_now()
    out = {
        "ok": bool(history_query_complete and durable_queue_complete),
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "deal_count": len(payloads),
        "applied_count": applied_count,
        "skipped_duplicate_count": skipped_duplicate_count,
        "failed_count": failed_count,
        "unresolved_count": unresolved_count,
        "diagnostics": diagnostics,
        "last_result": _result_summary(last_result or {}),
    }
    if not history_query_complete:
        out["error"] = "history_query_incomplete"
    elif not durable_queue_complete:
        out["error"] = "durable_inbox_incomplete"
    append_trade_intake_audit(
        audit_path,
        {
            "phase": "backfill_check_finished",
            "source": "backfill",
            **out,
        },
    )
    return out


def _lifecycle_discovery_after_backfill_phase(
    *,
    repo: Any,
    account: str | None,
    observed_at_ms: int,
    apply_changes: bool,
    audit_path: Path,
    phase: str,
) -> dict[str, Any]:
    try:
        result = discover_lifecycle_cases(
            repo,
            account=account,
            observed_at_ms=observed_at_ms,
            apply_changes=apply_changes,
        )
        append_trade_intake_audit(
            audit_path,
            {
                "phase": phase,
                "source": "backfill",
                "ok": True,
                "result": result,
            },
        )
        return {"ok": True, **result}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        append_trade_intake_audit(
            audit_path,
            {
                "phase": phase,
                "source": "backfill",
                "ok": False,
                "error": error,
            },
        )
        return {
            "ok": False,
            "apply_changes": bool(apply_changes),
            "error": error,
        }


def _effective_lookback_hours(
    *,
    configured_lookback_hours: float,
    checkpoint: dict[str, Any],
    now: datetime,
) -> float:
    configured = float(configured_lookback_hours)
    raw_cursor = str(checkpoint.get("last_successful_window_end_utc") or "").strip()
    if not raw_cursor:
        return configured
    try:
        cursor = datetime.fromisoformat(raw_cursor.replace("Z", "+00:00"))
    except ValueError:
        return configured
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    elapsed_hours = (now_utc - cursor.astimezone(timezone.utc)).total_seconds() / 3600
    if elapsed_hours <= 0:
        return configured
    return max(configured, elapsed_hours + 1.0)


def _history_query_complete(
    diagnostics: dict[str, Any],
    *,
    expected_account_ids: list[str],
) -> bool:
    rows = diagnostics.get("account_results")
    if not isinstance(rows, list):
        return True
    expected = {
        str(value or "").strip()
        for value in expected_account_ids
        if str(value or "").strip()
    }
    successful: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("futu_account_id") or "").strip()
        if (
            account_id
            and not raw.get("skipped")
            and not str(raw.get("error") or "").strip()
            and raw.get("ret") in (0, "0")
        ):
            successful.add(account_id)
    return not expected or expected.issubset(successful)


def _state_duplicate_reason(state: dict[str, Any], deal_id: str) -> str | None:
    if is_retryable_unresolved_deal(state, deal_id):
        return None
    entry = lookup_deal_state_entry(state, deal_id)
    if entry is None:
        return None
    bucket, _payload = entry
    return f"state:{bucket}"


def _record_ledger_duplicate_state(
    *,
    state: dict[str, Any],
    state_path: Path,
    deal_id: str,
    apply_changes: bool,
) -> dict[str, Any]:
    if not apply_changes or not deal_id or lookup_deal_state_entry(state, deal_id) is not None:
        return state
    state = upsert_deal_state(
        state,
        bucket="processed_deal_ids",
        deal_id=deal_id,
        payload={
            "status": "reconciled",
            "action": None,
            "account": None,
            "applied_record_ids": [],
            "reason": "ledger_event_already_recorded",
            "diagnostics": {"source": "backfill", "reconciled_from": "ledger_duplicate_precheck"},
        },
    )
    write_trade_intake_state(state_path, state)
    return state


def _ledger_recorded_deal_ids(repo: Any) -> set[str]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return set()
    return completed_ledger_deal_keys(
        item for item in list_trade_events() if isinstance(item, dict)
    )


def _result_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    data = result if isinstance(result, dict) else {}
    return {
        "status": data.get("status"),
        "action": data.get("action"),
        "reason": data.get("reason"),
        "deal_id": data.get("deal_id"),
        "account": data.get("account"),
    }
