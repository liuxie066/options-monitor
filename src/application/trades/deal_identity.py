from __future__ import annotations

from typing import Any, Iterable

from src.application.ledger.api import (
    broker_external_event_key,
    valid_void_target_event_id,
)
from src.application.trades.account_mapping import resolve_internal_account
from domain.domain.trade_account_identity import extract_primary_account_id


DEAL_ID_FIELDS = ("source_deal_id", "deal_id", "futu_deal_id")


def broker_deal_key(deal: Any) -> str:
    """Return the account-scoped durable identity for a normalized broker deal."""

    return broker_external_event_key(deal)


def broker_deal_key_from_payload(
    payload: dict[str, Any] | None,
    *,
    account_mapping: dict[str, str] | None,
) -> str:
    raw = payload if isinstance(payload, dict) else {}
    deal_id = ""
    for key in ("deal_id", "dealID", "dealId", "id"):
        deal_id = str(raw.get(key) or "").strip()
        if deal_id:
            break
    futu_account_id = str(extract_primary_account_id(raw) or "").strip()
    account = str(resolve_internal_account(futu_account_id, account_mapping) or "").strip()
    if deal_id and account and futu_account_id:
        return f"futu:{account}:{futu_account_id}:{deal_id}"
    return deal_id


def structured_deal_ids_from_ledger_event(event: dict[str, Any]) -> set[str]:
    """Return only broker deal identifiers stored in authoritative fields."""

    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    out = _normalized_values(raw_payload.get(key) for key in DEAL_ID_FIELDS)
    stock_settlement = raw_payload.get("stock_settlement")
    if isinstance(stock_settlement, dict):
        out.update(_normalized_values([stock_settlement.get("source_event_id")]))
    return out


def structured_deal_keys_from_ledger_event(event: dict[str, Any]) -> set[str]:
    """Return account-scoped broker identities, falling back only for legacy rows."""

    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    external_key = str(raw_payload.get("external_event_key") or "").strip()
    if external_key:
        return {external_key}
    deal_ids = structured_deal_ids_from_ledger_event(event)
    account = str(
        event.get("account")
        or raw_payload.get("internal_account")
        or raw_payload.get("account")
        or ""
    ).strip().lower()
    futu_account_id = str(raw_payload.get("futu_account_id") or "").strip()
    if account and futu_account_id:
        return {
            f"futu:{account}:{futu_account_id}:{deal_id}"
            for deal_id in deal_ids
        }
    return deal_ids


def structured_deal_ids_from_assigned_stock_event(event: dict[str, Any]) -> set[str]:
    return _normalized_values(event.get(key) for key in DEAL_ID_FIELDS)


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


def _completed_ledger_identities(
    events: Iterable[dict[str, Any]],
    *,
    identity_fn: Any,
) -> set[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in active_ledger_events(events):
        for deal_id in identity_fn(event):
            grouped.setdefault(deal_id, []).append(event)

    out: set[str] = set()
    for deal_id, rows in grouped.items():
        metadata_rows = [
            raw
            for row in rows
            for raw in [_deal_completion_payload(row)]
            if raw is not None
        ]
        if not metadata_rows:
            if len(rows) == 1 or _legacy_split_set_is_complete(rows):
                out.add(deal_id)
            continue
        if len(metadata_rows) != len(rows):
            continue
        expected_split_count = _consistent_positive_int(metadata_rows, "split_count")
        expected_contracts = _consistent_positive_int(metadata_rows, "expected_contracts")
        if expected_split_count is None or expected_contracts is None:
            continue
        split_indexes = {
            index
            for item in metadata_rows
            for index in [_positive_int(item.get("split_index"))]
            if index is not None
        }
        allocated_contracts = sum(
            int(_positive_int(item.get("allocated_contracts")) or 0)
            for item in metadata_rows
        )
        if (
            len(rows) == expected_split_count
            and split_indexes == set(range(1, expected_split_count + 1))
            and allocated_contracts == expected_contracts
        ):
            out.add(deal_id)
    return out


def _deal_completion_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    value = raw_payload.get("broker_deal_completion")
    return dict(value) if isinstance(value, dict) else None


def _legacy_split_set_is_complete(rows: list[dict[str, Any]]) -> bool:
    resolutions: list[dict[str, Any]] = []
    target_lot_ids: set[str] = set()
    for event in rows:
        raw = event.get("raw_payload")
        raw_payload = raw if isinstance(raw, dict) else {}
        resolution = raw_payload.get("close_target_resolution")
        if not isinstance(resolution, dict):
            return False
        resolutions.append(dict(resolution))
        target_lot_id = str(
            event.get("target_lot_id")
            or raw_payload.get("target_lot_id")
            or raw_payload.get("record_id")
            or ""
        ).strip()
        if not target_lot_id:
            return False
        target_lot_ids.add(target_lot_id)

    declared_target_sets = {
        tuple(
            sorted(
                str(value or "").strip()
                for value in list(item.get("record_ids") or [])
                if str(value or "").strip()
            )
        )
        for item in resolutions
    }
    declared_contracts = {
        _positive_int(item.get("contracts_to_close"))
        for item in resolutions
    }
    if len(declared_target_sets) != 1 or len(declared_contracts) != 1:
        return False
    expected_targets = set(next(iter(declared_target_sets)))
    expected_contracts = next(iter(declared_contracts))
    if expected_contracts is None:
        return False
    return (
        expected_targets == target_lot_ids
        and len(rows) == len(target_lot_ids)
        and sum(int(event.get("contracts") or 0) for event in rows)
        == expected_contracts
    )


def _consistent_positive_int(rows: list[dict[str, Any]], key: str) -> int | None:
    values = {_positive_int(item.get(key)) for item in rows}
    if None in values or len(values) != 1:
        return None
    return next(iter(values))


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _normalized_values(values: Iterable[Any]) -> set[str]:
    return {
        text
        for value in values
        for text in [str(value or "").strip()]
        if text
    }
