from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.trades.deal_identity import (
    broker_deal_key_from_payload,
    completed_ledger_deal_ids,
    completed_ledger_deal_keys,
)
from src.infrastructure.futu_history_deals import fetch_opend_history_deals
from src.application.trades.order_fee_sync import sync_order_fees
from src.application.trades.lifecycle_reconciliation import discover_lifecycle_cases
from src.application.trades.inbox import (
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    mark_trade_payload_handled,
    mark_trade_payload_retryable,
    record_trade_payload_refresh_intent,
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


_FEE_PROVIDER_START_MS = int(
    datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
)


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
    dispatch_portfolio_refresh_fn: Callable[[dict[str, str]], Any] | None = None,
    process_lock: Any | None = None,
    inbox_path: Path | None = None,
    checkpoint_path: Path | None = None,
    history_deals_fn: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = fetch_opend_history_deals,
    fee_sync_fn: Callable[..., dict[str, Any]] = sync_order_fees,
    fee_provider: Any | None = None,
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
    portfolio_refresh_intents: dict[str, dict[str, str]] = {}
    try:
        lifecycle_accounts = _lifecycle_discovery_accounts(
            futu_account_ids=futu_account_ids,
            account_mapping=account_mapping,
        )
        lifecycle_scope_error = None
    except ValueError as exc:
        lifecycle_accounts = ()
        lifecycle_scope_error = f"{type(exc).__name__}: {exc}"
    lifecycle_discovery_before = _lifecycle_discovery_after_backfill_phase(
        repo=repo,
        accounts=lifecycle_accounts,
        scope_error=lifecycle_scope_error,
        observed_at_ms=int(now.astimezone(timezone.utc).timestamp() * 1000),
        apply_changes=apply_changes,
        audit_path=audit_path,
        phase="backfill_lifecycle_discovery_before",
    )
    lock_context = process_lock if process_lock is not None else contextlib.nullcontext()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        payload = _bind_backfill_payload_to_source(
            payload,
            futu_account_ids=futu_account_ids,
            account_mapping=account_mapping,
            host=host,
            port=port,
            observed_at_utc=started_at,
            diagnostics=diagnostics,
        )
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
        if not deal_key:
            unresolved_count += 1
            last_result = {
                "status": "unresolved",
                "action": None,
                "reason": "identity_needs_review",
                "deal_id": deal_id or None,
                "account": None,
                "diagnostics": {
                    "retryable": False,
                    "identity_status": "identity_needs_review",
                },
            }
            append_trade_intake_audit(
                audit_path,
                {
                    "phase": "backfill_identity_needs_review",
                    "source": "backfill",
                    "deal_id": deal_id or None,
                    "inbox_id": inbox_id,
                    "reason": "canonical_broker_identity_missing",
                },
            )
            continue
        with lock_context:
            state = load_trade_intake_state(state_path)
            duplicate_reason = _state_duplicate_reason(
                state,
                deal_key,
                legacy_deal_id=deal_id,
            )
            ledger_keys = _ledger_recorded_deal_keys(repo)
            legacy_ledger_ids = _ledger_recorded_deal_ids(repo)
            if (
                duplicate_reason is None
                and deal_key
                and (
                    deal_key in ledger_keys
                    or deal_id in legacy_ledger_ids
                )
            ):
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
                    intent = claim_trade_payload_refresh_intent(
                        inbox_path
                        or state_path.with_name("trade_intake_inbox.sqlite3"),
                        inbox_id=inbox_id,
                    )
                    if intent is not None:
                        portfolio_refresh_intents.setdefault(
                            intent["account"],
                            intent,
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
                intent = result.get("portfolio_refresh_intent")
                if isinstance(intent, dict):
                    record_trade_payload_refresh_intent(
                        inbox_path
                        or state_path.with_name("trade_intake_inbox.sqlite3"),
                        inbox_id=inbox_id,
                        intent=intent,
                    )
                settle_trade_payload_result(
                    inbox_path
                    or state_path.with_name("trade_intake_inbox.sqlite3"),
                    inbox_id=inbox_id,
                    result=result,
                )
                claimed_intent = claim_trade_payload_refresh_intent(
                    inbox_path
                    or state_path.with_name("trade_intake_inbox.sqlite3"),
                    inbox_id=inbox_id,
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

        if inbox_id and claimed_intent is not None:
            portfolio_refresh_intents.setdefault(
                claimed_intent["account"],
                claimed_intent,
            )

    if dispatch_portfolio_refresh_fn is not None:
        for intent in portfolio_refresh_intents.values():
            try:
                dispatch_portfolio_refresh_fn(intent)
            except Exception:
                pass

    fee_sync_results: list[dict[str, Any]] = []
    fee_selection_after = dict(
        checkpoint_payload.get("fee_selection_after_by_source") or {}
    )
    fee_cursor_advanced = False
    if fee_provider is not None:
        for futu_account_id in sorted(
            {str(value or "").strip() for value in futu_account_ids if str(value or "").strip()}
        ):
            account = str(account_mapping.get(futu_account_id) or "").strip().lower()
            if not account:
                continue
            cursor_key = f"{account}:{futu_account_id}"
            try:
                with lock_context:
                    fee_result = fee_sync_fn(
                        repo,
                        account=account,
                        futu_account_id=futu_account_id,
                        start_ms=_FEE_PROVIDER_START_MS,
                        end_exclusive_ms=int(now.astimezone(timezone.utc).timestamp() * 1000) + 1,
                        provider=fee_provider,
                        apply=apply_changes,
                        observed_at_ms=int(now.astimezone(timezone.utc).timestamp() * 1000),
                        selection_after=fee_selection_after.get(cursor_key),
                        max_orders=400,
                    )
            except Exception as exc:
                fee_result = {
                    "schema_version": "order_fee_sync_receipt.v1",
                    "account": account,
                    "futu_account_id": futu_account_id,
                    "provider_attempted": False,
                    "reason": "fee_sync_failed",
                    "error_type": type(exc).__name__,
                }
            fee_sync_results.append(fee_result)
            if apply_changes and bool(fee_result.get("provider_attempted")):
                proposed = (fee_result.get("selection_cursor") or {}).get("after")
                if proposed not in (None, ""):
                    fee_selection_after[cursor_key] = str(proposed)
                    fee_cursor_advanced = True

    checkpoint_advanced = False
    history_query_complete = _history_query_complete(
        diagnostics,
        expected_account_ids=futu_account_ids,
    )
    checkpoint_update = dict(checkpoint_payload)
    if apply_changes and durable_queue_complete and history_query_complete:
        window_end_utc = str(
            diagnostics.get("window_end_utc")
            or now.astimezone(timezone.utc).isoformat()
        )
        checkpoint_update.update(
            {
                "last_successful_window_end_utc": window_end_utc,
                "configured_lookback_hours": configured_lookback_hours,
                "last_effective_lookback_hours": lookback_hours,
            }
        )
        checkpoint_advanced = True
    if fee_cursor_advanced:
        checkpoint_update["fee_selection_after_by_source"] = fee_selection_after
    if checkpoint_advanced or fee_cursor_advanced:
        checkpoint_update["updated_at_utc"] = utc_now()
        atomic_write_json(checkpoint_file, checkpoint_update)
    diagnostics["history_query_complete"] = history_query_complete
    diagnostics["durable_queue_complete"] = durable_queue_complete
    diagnostics["checkpoint_advanced"] = checkpoint_advanced
    diagnostics["fee_cursor_advanced"] = fee_cursor_advanced
    diagnostics["fee_sync"] = fee_sync_results
    lifecycle_discovery_after = _lifecycle_discovery_after_backfill_phase(
        repo=repo,
        accounts=lifecycle_accounts,
        scope_error=lifecycle_scope_error,
        observed_at_ms=int(now.astimezone(timezone.utc).timestamp() * 1000),
        apply_changes=apply_changes,
        audit_path=audit_path,
        phase="backfill_lifecycle_reconciliation_after",
    )
    diagnostics["lifecycle_reconciliation"] = {
        "before": lifecycle_discovery_before,
        "after": lifecycle_discovery_after,
    }
    lifecycle_discovery_complete = bool(
        lifecycle_discovery_before.get("ok")
        and lifecycle_discovery_after.get("ok")
    )
    diagnostics["lifecycle_discovery_complete"] = lifecycle_discovery_complete

    finished_at = utc_now()
    out = {
        "ok": bool(
            history_query_complete
            and durable_queue_complete
            and lifecycle_discovery_complete
        ),
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
    elif not lifecycle_discovery_complete:
        out["error"] = "lifecycle_discovery_incomplete"
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
    accounts: tuple[str, ...],
    scope_error: str | None,
    observed_at_ms: int,
    apply_changes: bool,
    audit_path: Path,
    phase: str,
) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "schema_version": "lifecycle_discovery_result.v2",
        "observed_at_ms": int(observed_at_ms),
        "account": accounts[0] if len(accounts) == 1 else None,
        "accounts": list(accounts),
        "account_results": [],
        "apply_changes": bool(apply_changes),
        "created_case_ids": [],
        "would_create_case_ids": [],
        "discovered_case_ids": [],
        "refreshed_case_ids": [],
        "would_refresh_case_ids": [],
        "skipped_targeted_lot_ids": [],
    }
    if scope_error:
        aggregate.update(
            {
                "ok": False,
                "reason": "lifecycle_account_scope_incomplete",
                "error": str(scope_error),
            }
        )
        append_trade_intake_audit(
            audit_path,
            {
                "phase": phase,
                "source": "backfill",
                "ok": False,
                "error": str(scope_error),
                "result": aggregate,
            },
        )
        return aggregate

    account_results: list[dict[str, Any]] = []
    aggregate_fields = (
        "created_case_ids",
        "would_create_case_ids",
        "discovered_case_ids",
        "refreshed_case_ids",
        "would_refresh_case_ids",
        "skipped_targeted_lot_ids",
    )
    for account in accounts:
        try:
            result = discover_lifecycle_cases(
                repo,
                account=account,
                observed_at_ms=observed_at_ms,
                apply_changes=apply_changes,
            )
            account_result = {"ok": True, **result}
        except Exception as exc:
            account_result = {
                "ok": False,
                "account": account,
                "apply_changes": bool(apply_changes),
                "error": f"{type(exc).__name__}: {exc}",
            }
        account_results.append(account_result)

    for field in aggregate_fields:
        aggregate[field] = sorted(
            {
                str(item).strip()
                for result in account_results
                for item in result.get(field) or []
                if str(item or "").strip()
            }
        )
    aggregate["account_results"] = account_results
    aggregate["ok"] = all(
        bool(result.get("ok")) for result in account_results
    )
    if not aggregate["ok"]:
        aggregate["reason"] = "lifecycle_account_discovery_failed"
        aggregate["error"] = "one or more account discoveries failed"
    append_trade_intake_audit(
        audit_path,
        {
            "phase": phase,
            "source": "backfill",
            "ok": bool(aggregate["ok"]),
            "result": aggregate,
            **(
                {"error": str(aggregate["error"])}
                if aggregate.get("error")
                else {}
            ),
        },
    )
    return aggregate


def _lifecycle_discovery_accounts(
    *,
    futu_account_ids: list[str],
    account_mapping: dict[str, str],
) -> tuple[str, ...]:
    configured_ids = sorted(
        {
            str(item or "").strip()
            for item in futu_account_ids
            if str(item or "").strip()
        }
    )
    if not configured_ids:
        raise ValueError("backfill lifecycle account scope is empty")
    missing_ids = [
        futu_account_id
        for futu_account_id in configured_ids
        if not str(
            account_mapping.get(futu_account_id) or ""
        ).strip()
    ]
    if missing_ids:
        raise ValueError(
            "backfill lifecycle account scope is incomplete: "
            + ",".join(missing_ids)
        )
    accounts = sorted(
        {
            str(account_mapping[futu_account_id]).strip().lower()
            for futu_account_id in configured_ids
        }
    )
    if not accounts:
        raise ValueError("backfill lifecycle account scope is empty")
    return tuple(accounts)


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


def _state_duplicate_reason(
    state: dict[str, Any],
    deal_id: str,
    *,
    legacy_deal_id: str | None = None,
) -> str | None:
    if is_retryable_unresolved_deal(state, deal_id):
        return None
    entry = lookup_deal_state_entry(state, deal_id)
    legacy_key = str(legacy_deal_id or "").strip()
    if entry is None and legacy_key and legacy_key != deal_id:
        if is_retryable_unresolved_deal(state, legacy_key):
            return None
        entry = lookup_deal_state_entry(state, legacy_key)
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


def _ledger_recorded_deal_keys(repo: Any) -> set[str]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return set()
    return completed_ledger_deal_keys(
        item for item in list_trade_events() if isinstance(item, dict)
    )


def _ledger_recorded_deal_ids(repo: Any) -> set[str]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return set()
    return completed_ledger_deal_ids(
        item
        for item in list_trade_events()
        if isinstance(item, dict) and not _has_canonical_broker_identity(item)
    )


def _has_canonical_broker_identity(event: dict[str, Any]) -> bool:
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    if str(raw_payload.get("external_event_key") or "").strip():
        return True
    account = str(
        event.get("account")
        or raw_payload.get("internal_account")
        or raw_payload.get("account")
        or ""
    ).strip()
    futu_account_id = str(
        raw_payload.get("futu_account_id") or ""
    ).strip()
    return bool(account and futu_account_id)


def _bind_backfill_payload_to_source(
    payload: dict[str, Any],
    *,
    futu_account_ids: list[str],
    account_mapping: dict[str, str],
    host: str,
    port: int,
    observed_at_utc: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    out = dict(payload)
    visible_account_ids = {
        str(out.get(key) or "").strip()
        for key in ("futu_account_id", "trd_acc_id", "trade_acc_id")
        if str(out.get(key) or "").strip()
    }
    configured_ids = {
        str(item or "").strip()
        for item in futu_account_ids
        if str(item or "").strip()
    }
    if not visible_account_ids and len(configured_ids) == 1:
        visible_account_ids = set(configured_ids)
    if len(visible_account_ids) != 1:
        return out
    futu_account_id = next(iter(visible_account_ids))
    if configured_ids and futu_account_id not in configured_ids:
        return out
    account = str(account_mapping.get(futu_account_id) or "").strip().lower()
    if not account:
        return out
    out["futu_account_id"] = futu_account_id
    out.setdefault("trd_acc_id", futu_account_id)
    out.setdefault("internal_account", account)
    out["_trade_intake_source"] = {
        "schema_version": "trade_intake_source.v1",
        "transport": "poll",
        "source_id": account,
        "account": account,
        "futu_account_id": futu_account_id,
        "opend_process": "FutuOpenD",
        "opend_host": str(host),
        "opend_port": int(port),
        "received_at_utc": str(observed_at_utc),
        "query_start_utc": diagnostics.get("window_start_utc"),
        "query_end_utc": diagnostics.get("window_end_utc"),
    }
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
