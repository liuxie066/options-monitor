from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from domain.domain.trade_execution import (
    epoch_milliseconds_instant,
    execution_economic_content,
    execution_source_identity_conflicts,
)
from src.application.ledger.api import (
    build_source_consumption_claim,
    canonical_source_economic_payload,
    canonical_source_payload_hash,
    execution_identity_from_input,
    assigned_stock_event_log,
    lifecycle_account_coherent_facts,
    proven_lifecycle_terminal_events as _proven_lifecycle_terminal_events,
    stock_claim_matches_lifecycle_terminal_events as _stock_claim_matches_terminal,
    open_trade_reconciliation_evidence_repo,
)
from src.application.trades.deal_identity import (
    active_ledger_events,
    completed_ledger_deal_keys,
    structured_deal_keys_from_assigned_stock_event,
    structured_deal_keys_from_ledger_event,
)
from src.application.trades.state import (
    load_trade_intake_state,
    compare_and_update_trade_intake_state_entries,
    upsert_deal_state,
)


TERMINAL_EVIDENCE_REASONS = {
    "ledger_event_already_recorded",
    "assigned_stock_sale_event_recorded",
    "lifecycle_case_already_recorded",
}
PENDING_LIFECYCLE_STATUSES = {
    "pending",
    "waiting_settlement_evidence",
    "needs_review",
    "partially_resolved",
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
            "delegated_lifecycle_pending_count": 0,
            "delegated_lifecycle_pending_deal_ids": [],
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
    delegated_deal_ids = sorted(
        {
            str(item.get("deal_id") or "").strip()
            for item in actions
            if str(item.get("reason") or "") == "lifecycle_pending_delegated"
            and str(item.get("deal_id") or "").strip()
        }
    )
    pending_before = result.get("pending_before") if isinstance(result.get("pending_before"), dict) else {}
    pending_after = result.get("pending_after") if isinstance(result.get("pending_after"), dict) else {}
    pending_after_count = _pending_bucket_count(pending_after)
    return {
        "available": True,
        "reason": None,
        "terminal_evidence_found": terminal_count > 0,
        "terminal_evidence_count": terminal_count,
        "ignored_non_option_count": ignored_count,
        "delegated_lifecycle_pending_count": len(delegated_deal_ids),
        "delegated_lifecycle_pending_deal_ids": delegated_deal_ids,
        "stale_state_count": int(result.get("planned_count") or 0),
        "pending_before_count": _pending_bucket_count(pending_before),
        "pending_after_reconcile_count": pending_after_count,
        "actionable_pending_after_reconcile_count": max(
            0,
            pending_after_count - len(delegated_deal_ids),
        ),
    }


def reconciled_source_matches_deal(action: dict[str, Any], deal: Any) -> bool:
    """Bind a proven reconciliation action to the current inbox's broker economics."""
    raw = deal.to_dict()
    execution = raw.get("execution_input") or {}
    if execution_source_identity_conflicts(raw.get("raw_payload") or {}, execution):
        return False
    ref = execution.get("broker_account_ref") or {}
    account, physical, deal_id = raw.get("internal_account"), raw.get("futu_account_id"), raw.get("deal_id")
    if (not account or not physical or not deal_id
            or not execution_identity_from_input(execution)
            or ref.get("broker_id") != "futu" or ref.get("environment") != "REAL"
            or ref.get("external_account_id") != physical
            or execution.get("external_id_namespace") != "futu.deal"
            or str(execution.get("external_execution_id")) != str(deal_id)):
        return False
    instrument = execution.get("instrument_ref") or {}
    try:
        execution_time = epoch_milliseconds_instant(raw.get("trade_time_ms"))
    except (TypeError, ValueError, OverflowError):
        return False
    if (ref.get("account_label") not in (None, account)
            or execution.get("side") != raw.get("side")
            or not _same_decimal(execution.get("quantity"), raw.get("contracts"))
            or not _same_decimal(execution.get("price"), raw.get("price"))
            or execution.get("occurred_at_utc") != execution_time
            or instrument.get("asset_type") != raw.get("asset_type")
            or instrument.get("symbol") != raw.get("symbol")):
        return False
    if raw.get("asset_type") == "option" and (
            execution.get("position_effect") != raw.get("position_effect")
            or instrument.get("option_type") != raw.get("option_type")
            or instrument.get("expiration_ymd") != raw.get("expiration_ymd")
            or not _same_decimal(instrument.get("strike"), raw.get("strike"))
            or not _same_decimal(instrument.get("multiplier"), raw.get("multiplier"))):
        return False
    source_key = f"futu:{account}:{physical}:{deal_id}"
    reason = action.get("reason")
    if reason == "lifecycle_case_already_recorded":
        expected = action.get("source_payload")
        if (not isinstance(expected, dict) or action.get("source_key") != source_key
                or canonical_source_payload_hash(expected) != action.get("source_payload_hash")):
            return False
        role = expected.get("source_role")
        if role == "option_anchor":
            if raw.get("asset_type") != "option" or raw.get("position_effect") != "close" or raw.get("side") not in {"buy", "sell"}:
                return False
            # Closing buy covers a short; closing sell disposes of a long.
            raw["position_side"] = "short" if raw["side"] == "buy" else "long"
            required = ("option_type", "position_side", "strike", "expiration_ymd", "multiplier")
        elif role == "stock_settlement" and raw.get("asset_type") == "stock":
            required = ()
        else:
            return False
    elif reason == "assigned_stock_sale_event_recorded":
        event = action.get("assigned_stock_event") or {}
        if (raw.get("asset_type") != "stock" or raw.get("side") != "sell"
                or event.get("event_type") != "sale" or event.get("side") != "sell"
                or source_key not in structured_deal_keys_from_assigned_stock_event(event)):
            return False
        stored_execution = event.get("execution_input")
        if stored_execution:
            if execution_identity_from_input(stored_execution) != execution_identity_from_input(execution):
                return False
            stored, incoming = execution_economic_content(stored_execution), execution_economic_content(execution)
            if stored.get("errors") or incoming.get("errors") or stored.get("economic") != incoming.get("economic"):
                return False
        expected = event
        role, required = "stock_settlement", ()
    else:
        return False
    if not _positive_contract_count(raw.get("contracts")) or not _positive_contract_count(raw.get("trade_time_ms")):
        return False
    try:
        actual = canonical_source_economic_payload(source_key=source_key, source_role=role,
            payload={**raw, "account": account, "clearing_date": (raw.get("raw_payload") or {}).get("clearing_date") or (raw.get("raw_payload") or {}).get("settlement_date")})
        proven = canonical_source_economic_payload(source_key=source_key, source_role=role, payload=expected)
    except (TypeError, ValueError):
        return False
    required = ("account", "futu_account_id", "symbol", "side", "quantity", "price", "execution_time_ms", *required)
    if any(actual.get(field) is None or proven.get(field) is None or actual[field] != proven[field] for field in required):
        return False
    # Old source claims may omit ancillary fields; any recorded fact still binds.
    if any(proven.get(field) is not None and actual.get(field) != proven[field] for field in ("order_id", "clearing_date")):
        return False
    if reason == "assigned_stock_sale_event_recorded" and expected.get("currency") is not None and raw.get("currency") != expected["currency"]:
        return False
    return True


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
    update_state_fn: Callable[..., Any] = compare_and_update_trade_intake_state_entries,
    before_state_update: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    state_file = Path(state_path)
    audit_file = Path(audit_path) if audit_path else None
    state = load_state_fn(state_file)
    requested = _normalize_deal_ids(deal_ids)
    audit_by_deal = _audit_events_by_deal(audit_file)
    ledger_by_deal = _ledger_events_by_deal(repo)
    assigned_stock_by_deal = _assigned_stock_events_by_deal(repo)
    lifecycle_by_deal = _completed_lifecycle_cases_by_deal(repo)
    delegated_lifecycle_by_deal = _delegated_lifecycle_cases_by_deal(repo)
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
                    "assigned_stock_event": dict(assigned_stock_events[-1]),
                    "write_state": True,
                }
            )
            new_state = upsert_deal_state(new_state, bucket="processed_deal_ids", deal_id=deal_id, payload=payload)
            continue

        lifecycle_entries = [
            entry for entry in lifecycle_by_deal.get(_lifecycle_lookup_key(deal_id, item), [])
            if _lifecycle_source_matches_state(entry, deal_id=deal_id, state_item=item)
        ]
        if len(lifecycle_entries) == 1:
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
                    "lifecycle_terminal_types": payload["diagnostics"]["reconciled_lifecycle_terminal_types"],
                    "source_key": lifecycle_entries[0]["source_key"],
                    "terminal_event_ids": lifecycle_entries[0]["terminal_event_ids"],
                    "source_payload_hash": lifecycle_entries[0]["source_payload_hash"],
                    "source_payload": lifecycle_entries[0]["source_payload"],
                    "write_state": True,
                }
            )
            new_state = upsert_deal_state(new_state, bucket="processed_deal_ids", deal_id=deal_id, payload=payload)
            continue

        delegated_entries = _filter_evidence_for_state_item(
            delegated_lifecycle_by_deal.get(deal_id) or [],
            state_item=item,
        )
        if len(delegated_entries) == 1:
            delegated = delegated_entries[0]
            case = delegated.get("case") if isinstance(delegated.get("case"), dict) else {}
            actions.append(
                {
                    "deal_id": deal_id,
                    "from_bucket": bucket,
                    "action": "keep_pending",
                    "reason": "lifecycle_pending_delegated",
                    "lifecycle_case_id": case.get("case_id"),
                    "lifecycle_status": case.get("status"),
                    "lifecycle_anchor_kind": delegated.get("anchor_kind"),
                    "write_state": False,
                }
            )
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
    final_state = new_state
    applied_keys: tuple[str, ...] = ()
    allowed = {str(item["deal_id"]) for item in writable_actions}
    if writable_actions and before_state_update is not None:
        allowed &= set(before_state_update(state, new_state, actions))
        for action in writable_actions:
            if action["deal_id"] not in allowed:
                action["write_state"] = False
                action.setdefault("deferred_reason", "before_state_update_rejected")
                bucket, original = _state_entry(state, action["deal_id"])
                new_state = upsert_deal_state(new_state, bucket=bucket, deal_id=action["deal_id"], payload=original)
        final_state = new_state
    if apply_changes and allowed:
        backup_path = _backup_state_file(state_file)
        applied_keys = tuple(update_state_fn(
            state_file, new_state, deal_ids=sorted(allowed), expected_state=state,
        ))
        final_state = load_state_fn(state_file)

    return {
        "ok": True,
        "state_path": str(state_file),
        "audit_path": str(audit_file) if audit_file else None,
        "requested_deal_ids": requested,
        "pending_before": _bucket_counts(state),
        "pending_after": _bucket_counts(final_state),
        "planned_count": len(allowed),
        "applied_count": len(applied_keys),
        "applied_deal_ids": list(applied_keys),
        "state_written": bool(applied_keys),
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
        raw = event.get("raw_payload") or {}
        if raw.get("case_id") or raw.get("allocation_id"):
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
    return sorted(structured_deal_keys_from_assigned_stock_event(event))


def _completed_lifecycle_cases_by_deal(repo: Any) -> dict[str, list[dict[str, Any]]]:
    """Index only coherent anchors with fully allocated, active terminal effects."""
    list_cases = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_cases):
        return {}
    accounts = {_evidence_account(case) for case in _dict_rows(list_cases())}
    out: dict[str, list[dict[str, Any]]] = {}
    for account in sorted(accounts - {""}):
        try:
            facts = lifecycle_account_coherent_facts(repo, account=account)
        except Exception:
            # Unavailable evidence never authorizes clearing a pending entry.
            continue
        cases = {row["case_id"]: row for row in facts["account_lifecycle_cases"]}
        evidence = {row["evidence_id"]: row for row in facts["account_lifecycle_evidence"]}
        claims = _dict_rows(facts.get("account_lifecycle_source_consumptions"))
        for resolution in facts["account_lifecycle_resolution"].get("case_resolutions", []):
            case = cases.get(resolution.get("case_id"))
            if not case or resolution.get("status") not in {"direct", "bridged"}:
                continue
            terminal = _proven_lifecycle_terminal_events(case, facts=facts)
            if not terminal:
                continue
            terminal_types = sorted({row["event_type"] for row in terminal})
            anchors = _dict_rows(resolution.get("anchor_facts"))
            candidates = []
            for anchor in anchors:
                candidates.extend(claim for claim in claims if (
                    claim.get("source_key") == anchor.get("source_key")
                    and claim.get("source_payload_hash") == anchor.get("source_payload_hash")
                    and claim.get("owner_evidence_id") == anchor.get("source_owner_evidence_id")
                    and claim.get("case_id") == anchor.get("source_owner_case_id")
                    and claim.get("source_role") == "option_anchor"
                ))
            candidates.extend(claim for claim in claims if (
                claim.get("case_id") == case["case_id"]
                and claim.get("source_role") == "stock_settlement"
                and _stock_claim_matches_terminal(claim, terminal, case=case)
            ))
            for claim in candidates:
                owner = evidence.get(claim.get("owner_evidence_id"))
                if not owner:
                    continue
                source_key = str(claim.get("source_key") or "")
                entry = {
                    "case": {**case, "decision_type": terminal_types[0] if len(terminal_types) == 1 else "mixed"},
                    "evidence": owner, "source_key": source_key,
                    "source_payload": claim["source_payload"],
                    "source_payload_hash": claim["source_payload_hash"],
                    "terminal_event_ids": [row["event_id"] for row in terminal],
                    "terminal_types": terminal_types,
                }
                keys = _deal_ids_from_source_key(source_key)
                execution = (owner.get("raw") or {}).get("execution_input")
                if isinstance(execution, dict):
                    ref = execution.get("broker_account_ref") or {}
                    parts = source_key.split(":", 3)
                    if (len(parts) == 4 and ref.get("broker_id") == "futu"
                            and ref.get("external_account_id") == parts[2]
                            and ref.get("environment") == "REAL"
                            and execution.get("external_id_namespace") == "futu.deal"
                            and str(execution.get("external_execution_id")) == parts[3]):
                        identity = execution_identity_from_input(execution)
                        if identity:
                            keys.append(identity)
                            entry["execution_identity"] = identity
                for key in keys:
                    out.setdefault(key, []).append(entry)
    return out


def _positive_contract_count(value: Any) -> int:
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number <= 0 or number != number.to_integral_value():
            return 0
        return int(number)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0


def _same_decimal(left: Any, right: Any) -> bool:
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return not isinstance(left, bool) and not isinstance(right, bool) and a.is_finite() and b.is_finite() and a == b
    except (InvalidOperation, TypeError, ValueError):
        return False


def _lifecycle_lookup_key(deal_id: str, state_item: dict[str, Any]) -> str:
    if not deal_id.startswith("execution:"):
        return deal_id
    evidence = (state_item.get("diagnostics") or {}).get("lifecycle_evidence") or {}
    execution = (evidence.get("raw") or {}).get("execution_input") or {}
    ref = execution.get("broker_account_ref") or {}
    if (execution_identity_from_input(execution) != deal_id
            or ref.get("broker_id") != "futu" or ref.get("environment") != "REAL"
            or ref.get("external_account_id") != state_item.get("futu_account_id")
            or execution.get("external_id_namespace") != "futu.deal"
            or str(execution.get("external_execution_id")) != state_item.get("source_deal_id")):
        return deal_id
    key = f"futu:{state_item.get('account')}:{ref['external_account_id']}:{execution['external_execution_id']}"
    return key if evidence.get("source_event_id") == key else deal_id


def _lifecycle_source_matches_state(entry: dict[str, Any], *, deal_id: str, state_item: dict[str, Any]) -> bool:
    if not _evidence_matches_state_item(entry, state_item=state_item):
        return False
    parts = entry["source_key"].split(":", 3)
    if len(parts) != 4 or state_item.get("account") != parts[1]:
        return False
    physical = state_item.get("futu_account_id")
    if physical and str(physical) != parts[2]:
        return False
    if state_item.get("source_deal_id") and str(state_item["source_deal_id"]) != parts[3]:
        return False
    if deal_id.startswith("execution:"):
        if entry.get("execution_identity") == deal_id:
            return str(physical or "") == parts[2]
        evidence = (state_item.get("diagnostics") or {}).get("lifecycle_evidence") or {}
        if (entry["source_key"] != _lifecycle_lookup_key(deal_id, state_item)
                or evidence.get("evidence_id") not in (entry["evidence"].get("source_evidence_ids") or [])):
            return False
        try:
            claim = build_source_consumption_claim(
                source_key=entry["source_key"], case_id=entry["case"]["case_id"],
                owner_evidence_id=entry["evidence"]["evidence_id"],
                source_role=entry["source_payload"]["source_role"], economic_payload=evidence.get("raw") or {},
            )
        except (KeyError, TypeError, ValueError):
            return False
        return claim["source_payload_hash"] == entry["source_payload_hash"]
    if deal_id != entry["source_key"] and (deal_id != parts[3] or str(physical or "") != parts[2]):
        return False
    return True


def _delegated_lifecycle_cases_by_deal(repo: Any) -> dict[str, list[dict[str, Any]]]:
    """Return pending lifecycle owners accepted by the canonical overlay."""
    list_cases = getattr(repo, "list_trade_lifecycle_cases", None)
    if not callable(list_cases):
        return {}
    cases = _dict_rows(list_cases())
    accounts = sorted(
        {
            str(item.get("account") or "").strip().lower()
            for item in cases
            if str(item.get("account") or "").strip()
            and str(item.get("status") or "").strip().lower()
            in PENDING_LIFECYCLE_STATUSES
        }
    )
    if not accounts:
        return {}
    candidate = getattr(repo, "primary_repo", repo)
    if not callable(getattr(candidate, "read_lifecycle_account_rows", None)):
        # A repository without the coherent reader cannot prove delegation. Keep
        # pending rows actionable while preserving terminal-evidence repair.
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        facts = lifecycle_account_coherent_facts(repo, account=account)
        cases_by_id = {
            str(item.get("case_id") or "").strip(): dict(item)
            for item in facts.get("account_lifecycle_cases") or []
            if isinstance(item, dict)
            and str(item.get("case_id") or "").strip()
        }
        evidence_by_id = {
            str(item.get("evidence_id") or "").strip(): dict(item)
            for item in facts.get("account_lifecycle_evidence") or []
            if isinstance(item, dict)
            and str(item.get("evidence_id") or "").strip()
        }
        resolution = facts.get("account_lifecycle_resolution")
        if not isinstance(resolution, dict):
            raise TypeError("canonical lifecycle resolution is unavailable")
        for case_resolution in resolution.get("case_resolutions") or []:
            if not isinstance(case_resolution, dict):
                continue
            case_id = str(case_resolution.get("case_id") or "").strip()
            lifecycle_case = cases_by_id.get(case_id)
            if (
                not isinstance(lifecycle_case, dict)
                or str(lifecycle_case.get("status") or "").strip().lower()
                not in PENDING_LIFECYCLE_STATUSES
                or str(case_resolution.get("status") or "").strip().lower()
                not in {"direct", "bridged"}
            ):
                continue
            for anchor in case_resolution.get("anchor_facts") or []:
                if not isinstance(anchor, dict):
                    continue
                source_key = str(anchor.get("source_key") or "").strip()
                owner_evidence_id = str(
                    anchor.get("source_owner_evidence_id") or ""
                ).strip()
                if not source_key:
                    continue
                entry = {
                    "case": dict(lifecycle_case),
                    "evidence": dict(evidence_by_id.get(owner_evidence_id) or {}),
                    "source_key": source_key,
                    "anchor_kind": anchor.get("anchor_kind"),
                }
                for deal_id in _deal_ids_from_source_key(source_key):
                    out.setdefault(deal_id, []).append(entry)
    return out


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)]


def _deal_ids_from_source_key(source_key: str) -> list[str]:
    parts = str(source_key or "").strip().split(":", 3)
    if len(parts) != 4:
        return []
    return _normalize_deal_ids([source_key, parts[3]])


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
        **state_item,
        "status": "reconciled",
        "action": action or None,
        "account": state_item.get("account") or ledger_event.get("account"),
        "applied_record_ids": [record_id] if record_id else [],
        "reason": "ledger_event_already_recorded",
        "diagnostics": {
            **dict(state_item.get("diagnostics") or {}),
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
        **state_item,
        "status": "reconciled",
        "action": action,
        "account": state_item.get("account") or assigned_stock_event.get("account"),
        "applied_record_ids": [stock_lot_id] if stock_lot_id else [],
        "reason": "assigned_stock_sale_event_recorded",
        "diagnostics": {
            **dict(state_item.get("diagnostics") or {}),
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
    target_lot_ids = sorted(case["target_contracts_by_lot"])
    action = str(state_item.get("action") or "").strip() or decision_type or None
    return {
        **state_item,
        "status": "reconciled",
        "action": action,
        "account": state_item.get("account") or case.get("account"),
        "applied_record_ids": target_lot_ids,
        "reason": "lifecycle_case_already_recorded",
        "diagnostics": {
            **dict(state_item.get("diagnostics") or {}),
            "reconciled_from_bucket": from_bucket,
            "reconciled_lifecycle_case_id": case.get("case_id"),
            "reconciled_lifecycle_status": case.get("status"),
            "reconciled_source_key": lifecycle_entry["source_key"],
            "reconciled_source_payload_hash": lifecycle_entry["source_payload_hash"],
            "reconciled_terminal_event_ids": lifecycle_entry["terminal_event_ids"],
            "reconciled_lifecycle_terminal_types": lifecycle_entry["terminal_types"],
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
            **dict(state_item.get("diagnostics") or {}),
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
