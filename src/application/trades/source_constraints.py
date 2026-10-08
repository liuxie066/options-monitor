from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any


TRADE_EVIDENCE_SET_REF_PREFIX = "trade-inbox-evidence-set:v1:"


def read_account_trade_source_constraints(
    path: str | Path,
    *,
    account: str,
    trade_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Read the account's durable source versions without changing the Inbox."""
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from domain.domain.trade_execution import structured_deal_keys_from_ledger_event

    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("account is required")
    events = [dict(event) for event in trade_events]
    related_keys: set[str] = set()
    required_inbox_ids: set[str] = set()
    has_account_events = False
    for event in events:
        if str(event.get("account") or "").strip().lower() != account_value:
            continue
        raw = event.get("raw_payload")
        if not isinstance(raw, Mapping):
            continue
        execution = raw.get("execution_input")
        for holder in (raw, execution if isinstance(execution, Mapping) else {}):
            for ref in holder.get("evidence_refs") or []:
                if isinstance(ref, str) and ref.startswith(TRADE_EVIDENCE_SET_REF_PREFIX):
                    required_inbox_ids.add(ref.removeprefix(TRADE_EVIDENCE_SET_REF_PREFIX))
        keys = structured_deal_keys_from_ledger_event(dict(event))
        if keys:
            has_account_events = True
            related_keys.update(keys)
        elif raw.get("execution_input") or raw.get("futu_account_id"):
            has_account_events = True

    inbox_path = Path(path)
    if not inbox_path.exists():
        return {
            "status": "unavailable" if has_account_events else "trusted",
            "reason_codes": ["trade_source_inbox_unavailable"] if has_account_events else [],
            "evidence_fingerprint": canonical_sha256([]),
            "inbox_ids": [],
        }
    with closing(sqlite3.connect(f"{inbox_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        required = {"trade_inbox", "trade_inbox_evidence"}
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not required.issubset(tables):
            raise ValueError("trade source inbox schema unavailable")
        # ponytail: full Inbox scan; add an indexed account key if measured snapshot latency grows.
        rows = conn.execute(
            """SELECT i.inbox_id, i.broker_deal_key, i.payload_json,
                      i.status, i.result_reason, i.payload_version,
                      e.source, e.payload_hash, e.evidence_id, e.evidence_json
               FROM trade_inbox i
               LEFT JOIN trade_inbox_evidence e ON e.inbox_id = i.inbox_id
               ORDER BY i.inbox_id, e.source, e.payload_hash"""
        ).fetchall()

    evidence: list[dict[str, Any]] = []
    reasons: set[str] = set()
    inbox_ids: set[str] = set()
    source_rows: dict[str, dict[str, str]] = {}
    row_conflicts: dict[str, str] = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        execution = payload.get("execution_input")
        execution = execution if isinstance(execution, Mapping) else payload
        account_ref = execution.get("broker_account_ref")
        account_ref = account_ref if isinstance(account_ref, Mapping) else {}
        label = str(
            account_ref.get("account_label")
            or payload.get("internal_account")
            or (payload.get("_trade_intake_source") or {}).get("account")
            or payload.get("account")
            or ""
        ).strip().lower()
        key = str(row["broker_deal_key"] or "")
        if label != account_value and key not in related_keys:
            continue
        if label and label != account_value:
            reasons.add("trade_source_account_conflict")
        inbox_id = str(row["inbox_id"])
        inbox_ids.add(inbox_id)
        source_rows[inbox_id] = {"inbox_id": inbox_id, "broker_deal_key": key}
        evidence.append({
            "inbox_id": str(row["inbox_id"]),
            "broker_deal_key": key,
            "inbox_status": str(row["status"]),
            "result_reason": str(row["result_reason"] or ""),
            "payload_version": int(row["payload_version"]),
            "source": str(row["source"] or ""),
            "payload_hash": str(row["payload_hash"] or ""),
            "evidence_id": str(row["evidence_id"] or ""),
            "evidence_json": str(row["evidence_json"] or ""),
        })
        if not row["payload_hash"] or not row["evidence_id"]:
            reasons.add("trade_source_evidence_missing")
        if row["status"] == "conflict":
            row_conflicts[inbox_id] = str(row["result_reason"] or "trade_source_conflict")
    evidence_fingerprint = canonical_sha256(evidence)
    targets = {
        str(event.get("event_id") or ""): event
        for event in events
        if str(event.get("account") or "").strip().lower() == account_value
    }
    repair_pairs = {
        (str(raw.get("repair_event_id") or ""), str(raw.get("void_target_event_id") or ""))
        for event in events
        if str(event.get("account") or "").strip().lower() == account_value
        if isinstance((raw := event.get("raw_payload")), Mapping)
        and raw.get("mode") == "manual_repair_void"
    }
    resolved_ids: set[str] = set()
    for event in events:
        raw = event.get("raw_payload")
        if not isinstance(raw, Mapping) or raw.get("mode") != "manual_repair":
            continue
        target_id = str(raw.get("repair_target_event_id") or "")
        target = targets.get(target_id)
        resolution = raw.get("resolved_trade_source_evidence")
        if (
            target is None or event.get("account") != account_value
            or (str(event.get("event_id") or ""), target_id) not in repair_pairs
            or not isinstance(resolution, Mapping)
            or resolution.get("evidence_fingerprint") != evidence_fingerprint
        ):
            continue
        target_keys = structured_deal_keys_from_ledger_event(target)
        target_raw = target.get("raw_payload") or {}
        target_execution = target_raw.get("execution_input") if isinstance(target_raw, Mapping) else None
        target_refs = {
            ref.removeprefix(TRADE_EVIDENCE_SET_REF_PREFIX)
            for holder in (target_raw, target_execution)
            if isinstance(holder, Mapping)
            for ref in holder.get("evidence_refs") or []
            if isinstance(ref, str) and ref.startswith(TRADE_EVIDENCE_SET_REF_PREFIX)
        }
        prior_resolution = target_raw.get("resolved_trade_source_evidence") if isinstance(target_raw, Mapping) else None
        if isinstance(prior_resolution, Mapping):
            target_refs.update(str(value) for value in prior_resolution.get("inbox_ids") or [])
        allowed_ids = {
            inbox_id for inbox_id, source_row in source_rows.items()
            if inbox_id in target_refs or source_row["broker_deal_key"] in target_keys
        }
        resolved_ids.update(str(value) for value in resolution.get("inbox_ids") or []
                            if str(value) in allowed_ids)
    reasons.update(reason for inbox_id, reason in row_conflicts.items() if inbox_id not in resolved_ids)
    if required_inbox_ids - inbox_ids:
        reasons.add("trade_source_inbox_incomplete")
    return {
        "status": "trusted" if not reasons else "conflict",
        "reason_codes": sorted(reasons),
        "evidence_fingerprint": evidence_fingerprint,
        "inbox_ids": sorted(inbox_ids),
        "source_rows": sorted(source_rows.values(), key=lambda item: item["inbox_id"]),
    }


def execution_chronology_key(payload: dict[str, Any]) -> float:
    from datetime import datetime
    from domain.domain.trade_execution import canonical_trade_execution_content
    occurred = canonical_trade_execution_content(payload)["economic"].get("occurred_at_utc")
    try:
        return datetime.fromisoformat(str(occurred).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return float("-inf")
