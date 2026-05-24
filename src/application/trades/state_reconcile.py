from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.trades.state import load_trade_intake_state, upsert_deal_state, write_trade_intake_state


def reconcile_trade_intake_state(
    *,
    state_path: str | Path,
    repo: Any,
    audit_path: str | Path | None = None,
    deal_ids: list[str] | None = None,
    apply_changes: bool = False,
    load_state_fn: Callable[[str | Path], dict[str, Any]] = load_trade_intake_state,
    write_state_fn: Callable[[str | Path, dict[str, Any]], Any] = write_trade_intake_state,
) -> dict[str, Any]:
    state_file = Path(state_path)
    audit_file = Path(audit_path) if audit_path else None
    state = load_state_fn(state_file)
    requested = _normalize_deal_ids(deal_ids)
    audit_by_deal = _audit_events_by_deal(audit_file)
    ledger_by_deal = _ledger_events_by_deal(repo)
    candidates = _pending_deal_ids(state, requested=requested)

    actions: list[dict[str, Any]] = []
    new_state = {
        "processed_deal_ids": dict(state.get("processed_deal_ids") or {}),
        "failed_deal_ids": dict(state.get("failed_deal_ids") or {}),
        "unresolved_deal_ids": dict(state.get("unresolved_deal_ids") or {}),
    }
    for deal_id in candidates:
        bucket, item = _state_entry(new_state, deal_id)
        if bucket is None:
            actions.append(
                {
                    "deal_id": deal_id,
                    "from_bucket": None,
                    "action": "noop",
                    "reason": "deal_id_not_pending",
                    "write_state": False,
                }
            )
            continue

        ledger_events = ledger_by_deal.get(deal_id) or []
        if ledger_events:
            payload = _processed_payload_from_ledger(
                deal_id=deal_id,
                from_bucket=bucket,
                state_item=item,
                ledger_event=ledger_events[-1],
            )
            actions.append(
                {
                    "deal_id": deal_id,
                    "from_bucket": bucket,
                    "to_bucket": "processed_deal_ids",
                    "action": "mark_processed",
                    "reason": "ledger_event_already_recorded",
                    "ledger_event_id": payload["diagnostics"]["reconciled_ledger_event_id"],
                    "ledger_event_type": payload["diagnostics"]["reconciled_ledger_event_type"],
                    "write_state": True,
                }
            )
            new_state = upsert_deal_state(new_state, bucket="processed_deal_ids", deal_id=deal_id, payload=payload)
            continue

        if _is_ignored_non_option(item, audit_by_deal.get(deal_id) or []):
            payload = _processed_payload_for_ignored_non_option(deal_id=deal_id, from_bucket=bucket, state_item=item)
            actions.append(
                {
                    "deal_id": deal_id,
                    "from_bucket": bucket,
                    "to_bucket": "processed_deal_ids",
                    "action": "mark_skipped",
                    "reason": "not_option_deal",
                    "write_state": True,
                }
            )
            new_state = upsert_deal_state(new_state, bucket="processed_deal_ids", deal_id=deal_id, payload=payload)
            continue

        actions.append(
            {
                "deal_id": deal_id,
                "from_bucket": bucket,
                "action": "keep_pending",
                "reason": "no_reconciliation_evidence",
                "write_state": False,
            }
        )

    writable_actions = [item for item in actions if item.get("write_state")]
    backup_path: Path | None = None
    if apply_changes and writable_actions:
        backup_path = _backup_state_file(state_file)
        write_state_fn(state_file, new_state)

    return {
        "ok": True,
        "state_path": str(state_file),
        "audit_path": str(audit_file) if audit_file else None,
        "requested_deal_ids": requested,
        "pending_before": _bucket_counts(state),
        "pending_after": _bucket_counts(new_state),
        "planned_count": len(writable_actions),
        "applied_count": len(writable_actions) if apply_changes else 0,
        "state_written": bool(apply_changes and writable_actions),
        "actions": actions,
        "backup_path": str(backup_path) if backup_path else None,
    }


def _normalize_deal_ids(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        deal_id = str(value or "").strip()
        if deal_id and deal_id not in seen:
            out.append(deal_id)
            seen.add(deal_id)
    return out


def _pending_deal_ids(state: dict[str, Any], *, requested: list[str]) -> list[str]:
    if requested:
        return list(requested)
    out: list[str] = []
    for bucket_name in ("failed_deal_ids", "unresolved_deal_ids"):
        bucket = state.get(bucket_name)
        if isinstance(bucket, dict):
            out.extend(str(key) for key in bucket.keys())
    return out


def _state_entry(state: dict[str, Any], deal_id: str) -> tuple[str | None, dict[str, Any]]:
    for bucket_name in ("failed_deal_ids", "unresolved_deal_ids"):
        bucket = state.get(bucket_name)
        if isinstance(bucket, dict) and isinstance(bucket.get(deal_id), dict):
            return bucket_name, dict(bucket[deal_id])
    return None, {}


def _bucket_counts(state: dict[str, Any]) -> dict[str, int]:
    return {
        name: len(state.get(name) or {}) if isinstance(state.get(name), dict) else 0
        for name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids")
    }


def _ledger_events_by_deal(repo: Any) -> dict[str, list[dict[str, Any]]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for event in list_trade_events():
        if not isinstance(event, dict):
            continue
        for deal_id in _deal_ids_from_ledger_event(event):
            out.setdefault(deal_id, []).append(event)
    return out


def _deal_ids_from_ledger_event(event: dict[str, Any]) -> list[str]:
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    values = [
        raw_payload.get("source_deal_id"),
        raw_payload.get("deal_id"),
        raw_payload.get("futu_deal_id"),
    ]
    out = _normalize_deal_ids([str(item) for item in values if item not in (None, "")])
    event_id = str(event.get("event_id") or "").strip()
    for token in event_id.replace(":", "-").split("-"):
        if token.isdigit() and len(token) >= 12 and token not in out:
            out.append(token)
    return out


def _audit_events_by_deal(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None or not path.exists() or not path.is_file():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        for deal_id in _deal_ids_from_audit_event(event):
            out.setdefault(deal_id, []).append(event)
    return out


def _deal_ids_from_audit_event(event: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("deal_id",):
        if event.get(key) not in (None, ""):
            values.append(str(event.get(key)))
    for section_name in ("payload", "deal", "result"):
        section = event.get(section_name)
        if isinstance(section, dict):
            for key in ("deal_id", "dealID", "id"):
                if section.get(key) not in (None, ""):
                    values.append(str(section.get(key)))
    return _normalize_deal_ids(values)


def _is_ignored_non_option(state_item: dict[str, Any], audit_events: list[dict[str, Any]]) -> bool:
    if str(state_item.get("reason") or "").strip() == "not_option_deal":
        return True
    for event in audit_events:
        reason = str(event.get("reason") or "").strip()
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        result_reason = str(result.get("reason") or "").strip()
        if reason == "not_option_deal" or result_reason == "not_option_deal":
            return True
    return False


def _processed_payload_from_ledger(
    *,
    deal_id: str,
    from_bucket: str,
    state_item: dict[str, Any],
    ledger_event: dict[str, Any],
) -> dict[str, Any]:
    raw = ledger_event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    record_id = str(ledger_event.get("target_lot_id") or raw_payload.get("record_id") or "").strip()
    event_type = str(ledger_event.get("event_type") or "").strip()
    action = str(state_item.get("action") or "").strip() or _action_from_event_type(event_type)
    return {
        "status": "reconciled",
        "action": action or None,
        "account": state_item.get("account") or ledger_event.get("account"),
        "applied_record_ids": [record_id] if record_id else [],
        "reason": "ledger_event_already_recorded",
        "diagnostics": {
            "reconciled_from_bucket": from_bucket,
            "reconciled_ledger_event_id": ledger_event.get("event_id"),
            "reconciled_ledger_event_type": event_type,
            "reconciled_source_deal_id": deal_id,
            "previous_status": state_item.get("status"),
            "previous_reason": state_item.get("reason"),
        },
    }


def _processed_payload_for_ignored_non_option(*, deal_id: str, from_bucket: str, state_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "skipped",
        "action": state_item.get("action"),
        "account": state_item.get("account"),
        "applied_record_ids": [],
        "reason": "not_option_deal",
        "diagnostics": {
            "reconciled_from_bucket": from_bucket,
            "reconciled_source_deal_id": deal_id,
            "previous_status": state_item.get("status"),
            "previous_reason": state_item.get("reason"),
        },
    }


def _action_from_event_type(event_type: str) -> str | None:
    if event_type == "open":
        return "open"
    if event_type in {"close", "expire_close", "assignment", "exercise"}:
        return "close"
    return None


def _backup_state_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup)
    return backup
