from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.ledger.api import (
    assigned_stock_event_log,
    open_trade_reconciliation_evidence_repo,
)
from src.application.trades.deal_identity import (
    active_ledger_events,
    completed_ledger_deal_keys,
    structured_deal_ids_from_assigned_stock_event,
    structured_deal_keys_from_ledger_event,
)
from src.application.trades.state import load_trade_intake_state, upsert_deal_state, write_trade_intake_state


TERMINAL_EVIDENCE_REASONS = {
    "ledger_event_already_recorded",
    "assigned_stock_sale_event_recorded",
    "lifecycle_case_already_recorded",
}


def preview_trade_intake_reconciliation_from_sqlite(
    *,
    state_path: str | Path,
    sqlite_path: str | Path,
    audit_path: str | Path | None = None,
) -> dict[str, Any]:
    """Summarize stale intake state against the canonical ledger without opening a write-capable repo."""
    ledger_path = Path(sqlite_path)
    if not ledger_path.exists() or not ledger_path.is_file():
        return {
            "available": False,
            "reason": "ledger_sqlite_not_found",
            "terminal_evidence_found": False,
            "terminal_evidence_count": 0,
            "stale_state_count": 0,
        }
    result = reconcile_trade_intake_state(
        state_path=state_path,
        repo=open_trade_reconciliation_evidence_repo(ledger_path),
        audit_path=audit_path,
        apply_changes=False,
    )
    actions = [item for item in result.get("actions") or [] if isinstance(item, dict)]
    terminal_count = sum(
        1
        for item in actions
        if str(item.get("reason") or "") in TERMINAL_EVIDENCE_REASONS
    )
    ignored_count = sum(
        1
        for item in actions
        if str(item.get("reason") or "") == "not_option_deal"
    )
    pending_before = result.get("pending_before") if isinstance(result.get("pending_before"), dict) else {}
    pending_after = result.get("pending_after") if isinstance(result.get("pending_after"), dict) else {}
    return {
        "available": True,
        "reason": None,
        "terminal_evidence_found": terminal_count > 0,
        "terminal_evidence_count": terminal_count,
        "ignored_non_option_count": ignored_count,
        "stale_state_count": int(result.get("planned_count") or 0),
        "pending_before_count": _pending_bucket_count(pending_before),
        "pending_after_reconcile_count": _pending_bucket_count(pending_after),
    }


def _pending_bucket_count(counts: dict[str, Any]) -> int:
    return sum(
        int(counts.get(name) or 0)
        for name in ("failed_deal_ids", "unresolved_deal_ids")
    )


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
    assigned_stock_by_deal = _assigned_stock_events_by_deal(repo)
    lifecycle_by_deal = _completed_lifecycle_cases_by_deal(repo)
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

        ledger_events = _filter_evidence_for_state_item(ledger_by_deal.get(deal_id) or [], state_item=item)
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

        assigned_stock_events = _filter_evidence_for_state_item(assigned_stock_by_deal.get(deal_id) or [], state_item=item)
        if assigned_stock_events:
            payload = _processed_payload_from_assigned_stock_event(
                deal_id=deal_id,
                from_bucket=bucket,
                state_item=item,
                assigned_stock_event=assigned_stock_events[-1],
            )
            actions.append(
                {
                    "deal_id": deal_id,
                    "from_bucket": bucket,
                    "to_bucket": "processed_deal_ids",
                    "action": "mark_processed",
                    "reason": "assigned_stock_sale_event_recorded",
                    "assigned_stock_event_id": payload["diagnostics"]["reconciled_assigned_stock_event_id"],
                    "write_state": True,
                }
            )
            new_state = upsert_deal_state(new_state, bucket="processed_deal_ids", deal_id=deal_id, payload=payload)
            continue

        lifecycle_entries = _filter_evidence_for_state_item(lifecycle_by_deal.get(deal_id) or [], state_item=item)
        if lifecycle_entries:
            payload = _processed_payload_from_lifecycle(
                deal_id=deal_id,
                from_bucket=bucket,
                state_item=item,
                lifecycle_entry=lifecycle_entries[-1],
            )
            actions.append(
                {
                    "deal_id": deal_id,
                    "from_bucket": bucket,
                    "to_bucket": "processed_deal_ids",
                    "action": "mark_processed",
                    "reason": "lifecycle_case_already_recorded",
                    "lifecycle_case_id": payload["diagnostics"]["reconciled_lifecycle_case_id"],
                    "lifecycle_decision_type": payload["diagnostics"]["reconciled_lifecycle_decision_type"],
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
    rows = [item for item in list_trade_events() if isinstance(item, dict)]
    complete_ids = completed_ledger_deal_keys(rows)
    out: dict[str, list[dict[str, Any]]] = {}
    for event in active_ledger_events(rows):
        if not isinstance(event, dict):
            continue
        for deal_id in _deal_ids_from_ledger_event(event):
            if deal_id not in complete_ids:
                continue
            out.setdefault(deal_id, []).append(event)
    return out


def _filter_evidence_for_state_item(events: list[dict[str, Any]], *, state_item: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in events if _evidence_matches_state_item(event, state_item=state_item)]


def _evidence_matches_state_item(event: dict[str, Any], *, state_item: dict[str, Any]) -> bool:
    state_account = str(state_item.get("account") or "").strip().lower()
    state_source = str(state_item.get("source") or "").strip().lower()
    event_account = _evidence_account(event)
    event_source = _evidence_source(event)
    if state_account and event_account and state_account != event_account:
        return False
    if state_source and event_source and state_source != event_source:
        return False
    return True


def _evidence_account(event: dict[str, Any]) -> str:
    values: list[Any] = [event.get("account"), event.get("internal_account")]
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    values.extend([raw_payload.get("account"), raw_payload.get("internal_account")])
    case = event.get("case")
    if isinstance(case, dict):
        values.extend([case.get("account"), case.get("internal_account")])
    evidence = event.get("evidence")
    if isinstance(evidence, dict):
        values.extend([evidence.get("account"), evidence.get("internal_account")])
        nested_raw = evidence.get("raw")
        if isinstance(nested_raw, dict):
            values.extend([nested_raw.get("account"), nested_raw.get("internal_account")])
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def _evidence_source(event: dict[str, Any]) -> str:
    values: list[Any] = [event.get("source")]
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    values.extend([raw_payload.get("source"), raw_payload.get("trade_source")])
    evidence = event.get("evidence")
    if isinstance(evidence, dict):
        values.append(evidence.get("source"))
        nested_raw = evidence.get("raw")
        if isinstance(nested_raw, dict):
            values.extend([nested_raw.get("source"), nested_raw.get("trade_source")])
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def _deal_ids_from_ledger_event(event: dict[str, Any]) -> list[str]:
    return sorted(structured_deal_keys_from_ledger_event(event))


def _assigned_stock_events_by_deal(repo: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for event in assigned_stock_event_log(repo).events:
        for deal_id in _deal_ids_from_assigned_stock_event(event):
            out.setdefault(deal_id, []).append(dict(event))
    return out


def _deal_ids_from_assigned_stock_event(event: dict[str, Any]) -> list[str]:
    external_key = str(event.get("external_event_key") or "").strip()
    if external_key:
        return [external_key]
    return sorted(structured_deal_ids_from_assigned_stock_event(event))


def _completed_lifecycle_cases_by_deal(repo: Any) -> dict[str, list[dict[str, Any]]]:
    list_cases = getattr(repo, "list_trade_lifecycle_cases", None)
    list_evidence = getattr(repo, "list_trade_lifecycle_evidence", None)
    if not callable(list_cases) or not callable(list_evidence):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for case in list_cases():
        if not isinstance(case, dict):
            continue
        status = str(case.get("status") or "").strip().lower()
        decision_type = str(case.get("decision_type") or "").strip().lower()
        if status != "ledger_written" or decision_type not in {"assignment", "exercise", "expire_close"}:
            continue
        case_id = str(case.get("case_id") or "").strip()
        if not case_id:
            continue
        try:
            evidence_rows = list_evidence(case_id=case_id)
        except Exception:
            evidence_rows = []
        for evidence in evidence_rows:
            if not isinstance(evidence, dict):
                continue
            deal_ids = _deal_ids_from_lifecycle_evidence(evidence)
            if not deal_ids:
                continue
            entry = {
                "case": dict(case),
                "evidence": dict(evidence),
            }
            for deal_id in deal_ids:
                out.setdefault(deal_id, []).append(entry)
    return out


def _deal_ids_from_lifecycle_evidence(evidence: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("source_event_id", "deal_id"):
        if evidence.get(key) not in (None, ""):
            values.append(str(evidence.get(key)))
    raw = evidence.get("raw")
    raw_payload = raw if isinstance(raw, dict) else {}
    for key in ("deal_id", "source_deal_id", "futu_deal_id"):
        if raw_payload.get(key) not in (None, ""):
            values.append(str(raw_payload.get(key)))
    nested = raw_payload.get("raw_payload")
    if isinstance(nested, dict):
        for key in ("deal_id", "source_deal_id", "futu_deal_id"):
            if nested.get(key) not in (None, ""):
                values.append(str(nested.get(key)))
    return _normalize_deal_ids(values)


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


def _processed_payload_from_assigned_stock_event(
    *,
    deal_id: str,
    from_bucket: str,
    state_item: dict[str, Any],
    assigned_stock_event: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(assigned_stock_event.get("stock_event_id") or assigned_stock_event.get("event_id") or "").strip()
    stock_lot_id = str(assigned_stock_event.get("target_stock_lot_id") or assigned_stock_event.get("stock_lot_id") or "").strip()
    action = str(state_item.get("action") or "").strip() or "assigned_stock_sale"
    return {
        "status": "reconciled",
        "action": action,
        "account": state_item.get("account") or assigned_stock_event.get("account"),
        "applied_record_ids": [stock_lot_id] if stock_lot_id else [],
        "reason": "assigned_stock_sale_event_recorded",
        "diagnostics": {
            "reconciled_from_bucket": from_bucket,
            "reconciled_assigned_stock_event_id": event_id,
            "reconciled_source_deal_id": deal_id,
            "reconciled_target_stock_lot_id": stock_lot_id or None,
            "previous_status": state_item.get("status"),
            "previous_reason": state_item.get("reason"),
        },
    }


def _processed_payload_from_lifecycle(
    *,
    deal_id: str,
    from_bucket: str,
    state_item: dict[str, Any],
    lifecycle_entry: dict[str, Any],
) -> dict[str, Any]:
    case = lifecycle_entry.get("case") if isinstance(lifecycle_entry.get("case"), dict) else {}
    evidence = lifecycle_entry.get("evidence") if isinstance(lifecycle_entry.get("evidence"), dict) else {}
    decision_type = str(case.get("decision_type") or "").strip().lower()
    raw_target_lot_ids = case.get("target_lot_ids")
    target_lot_ids = (
        [str(item).strip() for item in raw_target_lot_ids if str(item or "").strip()]
        if isinstance(raw_target_lot_ids, list)
        else []
    )
    action = str(state_item.get("action") or "").strip() or decision_type or None
    return {
        "status": "reconciled",
        "action": action,
        "account": state_item.get("account") or case.get("account"),
        "applied_record_ids": target_lot_ids,
        "reason": "lifecycle_case_already_recorded",
        "diagnostics": {
            "reconciled_from_bucket": from_bucket,
            "reconciled_lifecycle_case_id": case.get("case_id"),
            "reconciled_lifecycle_status": case.get("status"),
            "reconciled_lifecycle_decision_type": decision_type,
            "reconciled_lifecycle_evidence_id": evidence.get("evidence_id"),
            "reconciled_lifecycle_evidence_type": evidence.get("evidence_type"),
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
