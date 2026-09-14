from __future__ import annotations

from typing import Any, Iterable

from src.application.ledger.api import (
    applied_execution_association_conflicts,
    broker_external_event_key,
    broker_execution_identity,
    execution_identity_from_input,
    valid_void_target_event_id,
)
from src.application.trades.account_mapping import resolve_internal_account
from domain.domain.trade_account_identity import extract_primary_account_id
from domain.domain.trade_execution import (
    DEAL_ID_FIELDS,
    canonical_trade_execution_content,
    conflicting_execution_associations,
    execution_economic_content,
    execution_source_identity_conflicts,
    ledger_execution_event_set_is_complete,
    ledger_event_economic_fingerprint as ledger_event_economic_fingerprint,
    structured_deal_ids_from_ledger_event as structured_deal_ids_from_ledger_event,
    structured_deal_keys_from_ledger_event as structured_deal_keys_from_ledger_event,
)


def broker_deal_key(deal: Any) -> str:
    """Return the account-scoped durable identity for a normalized broker deal."""

    return broker_execution_identity(deal) or broker_external_event_key(deal)


def broker_deal_key_from_payload(
    payload: dict[str, Any] | None,
    *,
    account_mapping: dict[str, str] | None,
) -> str:
    raw = payload if isinstance(payload, dict) else {}
    execution = raw.get("execution_input") if isinstance(raw.get("execution_input"), dict) else raw
    execution_id = execution_identity_from_input(execution)
    if execution_id:
        return execution_id
    deal_id = ""
    for key in ("deal_id", "dealID", "dealId", "id"):
        deal_id = str(raw.get(key) or "").strip()
        if deal_id:
            break
    futu_account_id = str(extract_primary_account_id(raw) or "").strip()
    execution_id = execution_identity_from_input({
        "broker_account_ref": {
            "broker_id": "futu", "external_account_id": futu_account_id,
            "environment": raw.get("environment") or raw.get("trd_env"),
        },
        "external_id_namespace": raw.get("external_id_namespace") or raw.get("execution_id_namespace"),
        "external_execution_id": deal_id,
    })
    if execution_id:
        return execution_id
    account = str(resolve_internal_account(futu_account_id, account_mapping) or "").strip()
    if deal_id and account and futu_account_id:
        return f"futu:{account}:{futu_account_id}:{deal_id}"
    return ""


def structured_deal_ids_from_assigned_stock_event(event: dict[str, Any]) -> set[str]:
    return _normalized_values(event.get(key) for key in DEAL_ID_FIELDS)


def structured_deal_keys_from_assigned_stock_event(event: dict[str, Any]) -> set[str]:
    return structured_deal_keys_from_ledger_event(
        {"account": event.get("account"), "raw_payload": event}
    )


def active_ledger_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in events if isinstance(item, dict)]
    voided_ids = {
        target
        for item in rows
        for target in [valid_void_target_event_id(item)]
        if target
    }
    return [
        item
        for item in rows
        if str(item.get("event_id") or "").strip() not in voided_ids
        and str(item.get("event_type") or "").strip().lower() != "void"
    ]


def completed_ledger_deal_ids(events: Iterable[dict[str, Any]]) -> set[str]:
    """Return deal IDs whose declared split set is complete."""

    return _completed_ledger_identities(
        events,
        identity_fn=structured_deal_ids_from_ledger_event,
    )


def completed_ledger_deal_keys(events: Iterable[dict[str, Any]]) -> set[str]:
    """Return complete broker deal identities scoped by account when available."""

    return _completed_ledger_identities(
        events,
        identity_fn=structured_deal_keys_from_ledger_event,
    )


def completed_ledger_execution_events(
    events: Iterable[dict[str, Any]], deal: Any,
) -> list[dict[str, Any]]:
    """Resolve one proven execution to its unchanged, complete legacy event set."""
    execution_id = broker_execution_identity(deal)
    if not execution_id:
        return []
    if execution_source_identity_conflicts(deal.raw_payload, deal.execution_input):
        raise ValueError("trade_execution_identity_conflict")
    legacy_key = broker_external_event_key(deal)
    candidates = []
    for event in active_ledger_events(events):
        raw = event.get("raw_payload") or {}
        stored_id = execution_identity_from_input(raw.get("execution_input"))
        aliases = structured_deal_keys_from_ledger_event(event, include_legacy_execution_identity=True)
        if stored_id == execution_id or execution_id in aliases or legacy_key in aliases:
            if stored_id and stored_id != execution_id:
                raise ValueError("trade_execution_identity_conflict")
            candidates.append(event)
    if not candidates:
        return []
    incoming = execution_economic_content(deal.execution_input)
    if applied_execution_association_conflicts(None, execution_id, incoming, applied_events=candidates):
        raise ValueError("trade_execution_applied_association_conflict")
    for event in candidates:
        stored = canonical_trade_execution_content(dict(event.get("raw_payload") or {}))
        if stored.get("errors") or incoming.get("errors"):
            raise ValueError("legacy_execution_evidence_required")
        if stored["economic"] != incoming["economic"] or conflicting_execution_associations(stored, incoming):
            raise ValueError("trade_execution_economic_conflict")
    if not _completed_ledger_identities(candidates, identity_fn=lambda _event: {execution_id}):
        raise ValueError("trade_execution_split_incomplete")
    for event in candidates:
        raw = event.get("raw_payload") or {}
        if execution_identity_from_input(raw.get("execution_input")):
            # Canonical replay retains the original event ownership across label
            # changes. A legacy physical match cannot prove that migration.
            continue
        contract = event.get("contract_key") or {}
        accounts = {
            str(value).strip().lower()
            for value in (event.get("account"), contract.get("account"), raw.get("internal_account"), raw.get("account"))
            if value not in (None, "")
        }
        if accounts != {str(deal.internal_account or "").strip().lower()}:
            raise ValueError("trade_execution_identity_conflict")
    return candidates


def _completed_ledger_identities(
    events: Iterable[dict[str, Any]],
    *,
    identity_fn: Any,
) -> set[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    physical_groups: dict[str, list[dict[str, Any]]] = {}
    for event in active_ledger_events(events):
        for deal_id in identity_fn(event):
            grouped.setdefault(deal_id, []).append(event)
        for key in structured_deal_keys_from_ledger_event(event, include_legacy_execution_identity=True):
            if key.startswith("execution:"):
                physical_groups.setdefault(key, []).append(event)

    conflicting_rows = {
        id(row) for rows in physical_groups.values()
        if not ledger_execution_event_set_is_complete(rows)
        for row in rows
    }
    return {
        deal_id for deal_id, rows in grouped.items()
        if not any(id(row) in conflicting_rows for row in rows)
        and ledger_execution_event_set_is_complete(rows)
    }


def _normalized_values(values: Iterable[Any]) -> set[str]:
    return {
        text
        for value in values
        for text in [str(value or "").strip()]
        if text
    }
