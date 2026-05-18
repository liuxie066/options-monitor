from __future__ import annotations

from typing import Any

from domain.domain.ledger.position_fields import normalize_account, normalize_broker
from src.application.ledger.api import (
    preview_trade_event_repair,
    preview_trade_event_void,
    project_trade_event_log,
    record_trade_event_repair,
    record_trade_event_void,
    refresh_position_lot_projection,
    trade_event_log,
)


def _event_status(event: dict[str, Any], *, voided_event_ids: set[str], diagnostic_event_ids: set[str]) -> str:
    event_id = str(event.get("event_id") or "").strip()
    if str(event.get("position_effect") or "").strip().lower() == "void":
        return "void_event"
    if event_id in voided_event_ids:
        return "voided"
    if event_id in diagnostic_event_ids:
        return "needs_review"
    return "active"


def _projection_diagnostics_by_event(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    projection = project_trade_event_log(events)
    out: dict[str, list[dict[str, Any]]] = {}
    for item in projection.diagnostics:
        out.setdefault(item.event_id, []).append(item.to_dict())
    return out


def _voided_event_ids(events: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for event in events:
        if str(event.get("position_effect") or "").strip().lower() != "void":
            continue
        raw_payload = event.get("raw_payload") or {}
        if not isinstance(raw_payload, dict):
            continue
        target = str(raw_payload.get("void_target_event_id") or "").strip()
        if target:
            out.add(target)
    return out


def list_trade_event_reviews(
    repo: Any,
    *,
    status: str = "all",
    broker: str | None = None,
    account: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    events = trade_event_log(repo)
    diagnostics_by_event = _projection_diagnostics_by_event(events)
    voided_ids = _voided_event_ids(events)
    diagnostic_ids = set(diagnostics_by_event)
    normalized_status = str(status or "all").strip().lower()
    normalized_broker = normalize_broker(broker) if broker else None
    normalized_account = normalize_account(account) if account else None
    rows: list[dict[str, Any]] = []
    for event in reversed(events):
        event_broker = normalize_broker(event.get("broker"))
        event_account = normalize_account(event.get("account"))
        if normalized_broker and event_broker != normalized_broker:
            continue
        if normalized_account and event_account != normalized_account:
            continue
        row_status = _event_status(event, voided_event_ids=voided_ids, diagnostic_event_ids=diagnostic_ids)
        if normalized_status != "all" and row_status != normalized_status:
            continue
        rows.append(
            {
                "event_id": event.get("event_id"),
                "status": row_status,
                "trade_time_ms": event.get("trade_time_ms"),
                "source_type": event.get("source_type"),
                "source_name": event.get("source_name"),
                "broker": event_broker,
                "account": event_account,
                "symbol": event.get("symbol"),
                "option_type": event.get("option_type"),
                "side": event.get("side"),
                "position_effect": event.get("position_effect"),
                "contracts": event.get("contracts"),
                "price": event.get("price"),
                "strike": event.get("strike"),
                "expiration_ymd": event.get("expiration_ymd"),
                "currency": event.get("currency"),
                "diagnostics": diagnostics_by_event.get(str(event.get("event_id") or "").strip(), []),
            }
        )
        if len(rows) >= max(int(limit), 1):
            break
    return rows


def show_trade_event_review(repo: Any, *, event_id: str) -> dict[str, Any]:
    events = trade_event_log(repo)
    target_id = str(event_id or "").strip()
    event = next((item for item in events if str(item.get("event_id") or "").strip() == target_id), None)
    if event is None:
        raise ValueError(f"trade event not found: {event_id}")
    diagnostics_by_event = _projection_diagnostics_by_event(events)
    status = _event_status(
        event,
        voided_event_ids=_voided_event_ids(events),
        diagnostic_event_ids=set(diagnostics_by_event),
    )
    return {
        "event": event,
        "status": status,
        "diagnostics": diagnostics_by_event.get(target_id, []),
    }


def replay_trade_events(repo: Any, *, apply: bool) -> dict[str, Any]:
    if apply:
        raw_result = refresh_position_lot_projection(repo)
        result = dict(raw_result) if isinstance(raw_result, dict) else raw_result.to_dict()
        result["mode"] = "applied"
        return result
    events = trade_event_log(repo)
    projection = project_trade_event_log(events)
    return {
        "mode": "dry_run",
        "trade_event_count": int(len(events)),
        "position_lot_count": int(len(projection.lots)),
        "projection_diagnostic_count": int(len(projection.diagnostics)),
        "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
    }


def preview_void_trade_event(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    return preview_trade_event_void(repo, event_id=event_id, reason=reason)


def apply_void_trade_event(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    return record_trade_event_void(repo, event_id=event_id, reason=reason)


def preview_repair_trade_event(
    repo: Any,
    *,
    event_id: str,
    overrides: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return preview_trade_event_repair(repo, event_id=event_id, overrides=overrides, reason=reason)


def apply_repair_trade_event(
    repo: Any,
    *,
    event_id: str,
    overrides: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return record_trade_event_repair(repo, event_id=event_id, overrides=overrides, reason=reason)
