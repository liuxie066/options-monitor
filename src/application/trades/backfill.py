from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.trades.history_backfill import fetch_opend_history_deals
from src.application.trades.state import (
    append_trade_intake_audit,
    is_retryable_unresolved_deal,
    load_trade_intake_state,
    lookup_deal_state_entry,
    upsert_deal_state,
    write_trade_intake_state,
)
from src.infrastructure.io_utils import utc_now


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
    history_deals_fn: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = fetch_opend_history_deals,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    started_at = utc_now()
    now = now_fn() if callable(now_fn) else datetime.now(timezone.utc)
    lookback_hours = float(backfill_config.get("lookback_hours") or 6)
    append_trade_intake_audit(
        audit_path,
        {
            "phase": "backfill_check_started",
            "source": "backfill",
            "started_at_utc": started_at,
            "lookback_hours": lookback_hours,
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
    lock_context = process_lock if process_lock is not None else contextlib.nullcontext()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        deal_id = payload_deal_id(payload)
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
            duplicate_reason = _state_duplicate_reason(state, deal_id)
            ledger_ids = _ledger_recorded_deal_ids(repo)
            if duplicate_reason is None and deal_id and deal_id in ledger_ids:
                duplicate_reason = "ledger_event_already_recorded"
                state = _record_ledger_duplicate_state(
                    state=state,
                    state_path=state_path,
                    deal_id=deal_id,
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
                continue

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

    finished_at = utc_now()
    out = {
        "ok": True,
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
    append_trade_intake_audit(
        audit_path,
        {
            "phase": "backfill_check_finished",
            "source": "backfill",
            **out,
        },
    )
    return out


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
    out: set[str] = set()
    for event in list_trade_events():
        if isinstance(event, dict):
            out.update(_deal_ids_from_ledger_event(event))
    return out


def _deal_ids_from_ledger_event(event: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    event_id = str(event.get("event_id") or "").strip()
    if event_id:
        out.add(event_id)
        for token in event_id.replace(":", "-").split("-"):
            if token.isdigit() and len(token) >= 12:
                out.add(token)
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    for key in ("source_deal_id", "deal_id", "futu_deal_id"):
        value = str(raw_payload.get(key) or "").strip()
        if value:
            out.add(value)
    stock_settlement = raw_payload.get("stock_settlement")
    if isinstance(stock_settlement, dict):
        value = str(stock_settlement.get("source_event_id") or "").strip()
        if value:
            out.add(value)
    return out


def _result_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    data = result if isinstance(result, dict) else {}
    return {
        "status": data.get("status"),
        "action": data.get("action"),
        "reason": data.get("reason"),
        "deal_id": data.get("deal_id"),
        "account": data.get("account"),
    }
