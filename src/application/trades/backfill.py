from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.trade_account_identity import extract_primary_account_id
from src.application.ledger.api import assigned_stock_event_log
from src.application.portfolio_management import portfolio_management_enabled
from src.application.trades.deal_identity import (
    broker_deal_key_from_payload,
    completed_ledger_deal_keys,
    structured_deal_keys_from_assigned_stock_event,
)
from src.infrastructure.futu_history_deals import fetch_opend_history_deals
from src.application.trades.order_fee_sync import fee_target_from_trusted_payload
from src.application.trades.lifecycle_reconciliation import discover_lifecycle_cases
from src.application.trades.inbox_authority import resolve_execution_inbox_path
from src.application.trades.inbox import (
    TRADE_INTAKE_ADAPTER_VERSIONS,
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    mark_trade_payload_handled,
    read_trade_payload,
)
from src.application.trades.state import (
    append_trade_intake_audit,
    is_durable_processed_deal,
    is_retryable_unresolved_deal,
    load_trade_intake_state,
    lookup_deal_state_entry,
    update_trade_intake_state_entries,
    upsert_deal_state,
)
from src.infrastructure.io_utils import atomic_write_json, read_json, utc_now
from src.infrastructure.private_storage import exclusive_private_file_lock


def payload_deal_id(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    execution = payload.get("execution_input") if isinstance(payload.get("execution_input"), dict) else payload
    if execution.get("external_execution_id"):
        return str(execution["external_execution_id"]).strip()
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
    enqueue_fee_target_fn: Callable[[tuple[str, str, str, str]], Any] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    refresh_enabled = bool(apply_changes and dispatch_portfolio_refresh_fn is not None
                           and portfolio_management_enabled(config))
    if apply_changes:
        inbox_path = resolve_execution_inbox_path(
            repo, inbox_path or state_path.with_name("trade_intake_inbox.sqlite3")
        )
    started_at = utc_now()
    now = now_fn() if callable(now_fn) else datetime.now(timezone.utc)
    configured_lookback_hours = float(backfill_config.get("lookback_hours") or 6)
    checkpoint_file = Path(
        checkpoint_path
        or state_path.with_name("trade_intake_backfill_checkpoint.json")
    )
    checkpoint = read_json(checkpoint_file, default={})
    checkpoint_payload = checkpoint if isinstance(checkpoint, dict) else {}
    scopes = _history_checkpoint_scopes(host=host, port=port, account_ids=futu_account_ids)
    scoped_checkpoints = {
        account_id: _scoped_checkpoint(checkpoint_payload, scope)
        for account_id, scope in scopes.items()
    }
    lookback_hours = max(
        (_effective_lookback_hours(configured_lookback_hours=configured_lookback_hours,
                                  checkpoint=entry, now=now)
         for entry in scoped_checkpoints.values()),
        default=configured_lookback_hours,
    )
    checkpoint_diagnostics = {
        "checkpoint_path": str(checkpoint_file),
        "checkpoint_scope_cursors": {
            account_id: entry.get("last_successful_window_end_utc")
            for account_id, entry in scoped_checkpoints.items()
        },
        "checkpoint_scopes": scopes,
        "legacy_checkpoint_unverified": bool(
            checkpoint_payload.get("last_successful_window_end_utc")
            or checkpoint_payload.get("legacy_unverified")
        ),
    }
    append_trade_intake_audit(
        audit_path,
        {
            "phase": "backfill_check_started",
            "source": "backfill",
            "started_at_utc": started_at,
            "lookback_hours": lookback_hours,
            "configured_lookback_hours": configured_lookback_hours,
            **checkpoint_diagnostics,
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
                **checkpoint_diagnostics,
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
    durable_accounts = dict.fromkeys(scopes, True)
    portfolio_refresh_intents: dict[str, dict[str, str]] = {}
    fee_targets: set[tuple[str, str, str, str]] = set()
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
            durable_queue_complete = False
            durable_accounts = dict.fromkeys(scopes, False)
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
        claimed_intent: dict[str, str] | None = None
        if apply_changes:
            try:
                inbox_id = enqueue_trade_payload(
                    inbox_path
                    or state_path.with_name("trade_intake_inbox.sqlite3"),
                    payload=payload,
                    source="backfill",
                    broker_deal_key=deal_key,
                    repo=repo,
                    adapter_version=TRADE_INTAKE_ADAPTER_VERSIONS["backfill"],
                )
            except Exception as exc:
                durable_queue_complete = False
                execution = payload.get("execution_input") if isinstance(payload.get("execution_input"), dict) else payload
                raw_ref = execution.get("broker_account_ref")
                ref = raw_ref if isinstance(raw_ref, dict) else {}
                physical = str(ref.get("external_account_id") or extract_primary_account_id(payload) or "").strip()
                if physical in durable_accounts:
                    durable_accounts[physical] = False
                else:
                    durable_accounts = dict.fromkeys(scopes, False)
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
        if inbox_id:
            inbox_row = read_trade_payload(inbox_path or state_path.with_name("trade_intake_inbox.sqlite3"),
                                          inbox_id=inbox_id)
            if (inbox_row or {}).get("status") == "conflict":
                unresolved_count += 1
                last_result = {"status": "unresolved", "reason": "inbox_conflict", "deal_id": deal_id}
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
        fee_target = (
            fee_target_from_trusted_payload(payload)
            if apply_changes
            else None
        )
        with lock_context:
            state = load_trade_intake_state(state_path)
            canonical_execution = deal_key.startswith("execution:v1:")
            # Standard executions must reach the core content check and durable receipt recovery.
            duplicate_reason = (
                None if canonical_execution
                else _state_duplicate_reason(state, deal_key, legacy_deal_id=deal_id)
            )
            ledger_keys = _ledger_recorded_deal_keys(repo)
            if (
                duplicate_reason is None
                and not canonical_execution
                and deal_key
                and (
                    deal_key in ledger_keys
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
                if fee_target is not None and (
                    deal_key in ledger_keys
                    or is_durable_processed_deal(state, deal_key)
                ):
                    fee_targets.add(fee_target)
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
                    ) if refresh_enabled else None
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
                    inbox_path=inbox_path or state_path.with_name("trade_intake_inbox.sqlite3"),
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
            if inbox_id and refresh_enabled:
                claimed_intent = claim_trade_payload_refresh_intent(
                    inbox_path
                    or state_path.with_name("trade_intake_inbox.sqlite3"),
                    inbox_id=inbox_id,
                )
        last_result = dict(result)
        status = str(result.get("status") or "").strip().lower()
        if fee_target is not None and (
            status == "applied"
            or is_durable_processed_deal(
                load_trade_intake_state(state_path),
                deal_key,
            )
        ):
            fee_targets.add(fee_target)
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
        elif status == "skipped" and str(result.get("reason") or "").strip() in {
            "duplicate", "duplicate_deal_id", "ledger_recorded"
        }:
            skipped_duplicate_count += 1
            append_trade_intake_audit(
                audit_path,
                {
                    "phase": "backfill_skipped_duplicate",
                    "source": "backfill",
                    "deal_id": result.get("deal_id") or deal_id or None,
                    "reason": result["reason"],
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

    fee_target_enqueue_count = 0
    fee_target_enqueue_failed_count = 0
    if enqueue_fee_target_fn is not None:
        for target in sorted(fee_targets):
            try:
                enqueue_fee_target_fn(target)
            except Exception as exc:
                fee_target_enqueue_failed_count += 1
                append_trade_intake_audit(
                    audit_path,
                    {
                        "phase": "backfill_fee_target_enqueue_failed",
                        "source": "backfill",
                        "identity_sha256": hashlib.sha256(
                            chr(31).join(target).encode()
                        ).hexdigest(),
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                fee_target_enqueue_count += 1

    checkpoint_advanced = False
    history_query_complete = _history_query_complete(
        diagnostics,
        expected_account_ids=futu_account_ids,
    )
    scope_updates = {}
    for account_id, scope in scopes.items():
        if (apply_changes and durable_accounts[account_id]
                and _history_query_complete(diagnostics, expected_account_ids=[account_id])):
            scope_updates[_checkpoint_scope_key(scope)] = {
                "scope": scope,
                "last_successful_window_end_utc": str(
                    diagnostics.get("window_end_utc") or now.astimezone(timezone.utc).isoformat()
                ),
                "configured_lookback_hours": configured_lookback_hours,
                "last_effective_lookback_hours": lookback_hours,
                "updated_at_utc": utc_now(),
            }
    advanced_scopes = _advance_history_checkpoints(checkpoint_file, scope_updates) if scope_updates else set()
    checkpoint_advanced = bool(advanced_scopes)
    diagnostics["durable_account_results"] = durable_accounts
    diagnostics["checkpoint_advanced_accounts"] = [
        account_id for account_id, scope in scopes.items()
        if _checkpoint_scope_key(scope) in advanced_scopes
    ]
    diagnostics["history_query_complete"] = history_query_complete
    diagnostics["durable_queue_complete"] = durable_queue_complete
    diagnostics["checkpoint_advanced"] = checkpoint_advanced
    diagnostics["fee_target_count"] = len(fee_targets)
    diagnostics["fee_target_enqueue_count"] = fee_target_enqueue_count
    diagnostics["fee_target_enqueue_failed_count"] = fee_target_enqueue_failed_count
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


def _history_checkpoint_scopes(*, host: str, port: int, account_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Scope the supported Futu REAL/unfiltered history query using its actual endpoint."""
    return {
        account_id: {
            "broker": "futu", "host": str(host).strip().lower(), "port": int(port),
            "physical_account_id": account_id, "environment": "REAL",
            "dataset": "history-deals", "filter": "unfiltered",
        }
        for account_id in sorted({str(value).strip() for value in account_ids if str(value).strip()})
    }


def _checkpoint_scope_key(scope: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scoped_checkpoint(checkpoint: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    entries = checkpoint.get("scopes")
    entry = entries.get(_checkpoint_scope_key(scope)) if isinstance(entries, dict) else None
    return entry if isinstance(entry, dict) and entry.get("scope") == scope else {}


def _advance_history_checkpoints(path: Path, updates: dict[str, dict[str, Any]]) -> set[str]:
    # Re-read under the existing file lock so concurrent sources cannot discard each other's cursors.
    with exclusive_private_file_lock(path.with_suffix(path.suffix + ".lock")):
        saved = read_json(path, default={})
        current = dict(saved) if isinstance(saved, dict) else {}
        saved_scopes = current.get("scopes")
        entries = dict(saved_scopes) if isinstance(saved_scopes, dict) else {}
        advanced = set()
        if current.get("last_successful_window_end_utc"):
            current = {"legacy_unverified": current}
        for key, entry in updates.items():
            previous = entries.get(key)
            if isinstance(previous, dict) and previous.get("scope") == entry["scope"]:
                try:
                    old_end = datetime.fromisoformat(previous["last_successful_window_end_utc"].replace("Z", "+00:00"))
                    new_end = datetime.fromisoformat(entry["last_successful_window_end_utc"].replace("Z", "+00:00"))
                    if old_end >= new_end:
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
            entries[key] = entry
            advanced.add(key)
        if advanced:
            current.update(schema_version="trade_intake_backfill_checkpoint.v2", scopes=entries)
            atomic_write_json(path, current)
        return advanced


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
        return False
    expected = {
        str(value or "").strip()
        for value in expected_account_ids
        if str(value or "").strip()
    }
    successful: set[str] = set()
    incomplete: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("futu_account_id") or "").strip()
        if (
            account_id
            and not raw.get("skipped")
            and not str(raw.get("error") or "").strip()
            and raw.get("ret") in (0, "0")
            and raw.get("coverage_status") == "complete"
            and raw.get("coverage_complete") is True
            and raw.get("pagination_complete") is True
            and raw.get("truncated") is not True
        ):
            successful.add(account_id)
        elif account_id:
            incomplete.add(account_id)
    return bool(expected) and expected.issubset(successful) and not expected.intersection(incomplete)


def _state_duplicate_reason(
    state: dict[str, Any],
    deal_id: str,
    *,
    legacy_deal_id: str | None = None,
) -> str | None:
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
    update_trade_intake_state_entries(state_path, state, deal_ids=(deal_id,))
    return state


def _ledger_recorded_deal_keys(repo: Any) -> set[str]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    keys = completed_ledger_deal_keys(
        item for item in list_trade_events() if isinstance(item, dict)
    ) if callable(list_trade_events) else set()
    for event in assigned_stock_event_log(repo).events:
        if str(event.get("event_type") or "").strip().lower() == "sale":
            keys.update(structured_deal_keys_from_assigned_stock_event(event))
    return keys


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
