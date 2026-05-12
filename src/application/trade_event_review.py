from __future__ import annotations

from typing import Any

from domain.domain.option_position_ledger import project_position_lot_records_with_diagnostics
from domain.domain.option_position_lots import normalize_account, normalize_broker
from src.application.option_positions_service import (
    build_manual_void_preview,
    build_manual_repair_preview,
    persist_manual_repair_event,
    persist_manual_void_event,
    rebuild_position_lots_from_trade_events,
    require_option_positions_event_write_repo,
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
    projection = project_position_lot_records_with_diagnostics(events)
    out: dict[str, list[dict[str, Any]]] = {}
    for item in projection.diagnostics:
        out.setdefault(item.event_id, []).append(item.to_dict())
    return out


def _projection_preview_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    projection = project_position_lot_records_with_diagnostics(events)
    return {
        "trade_event_count": int(len(events)),
        "position_lot_count": int(len(projection.lots)),
        "projection_diagnostic_count": int(len(projection.diagnostics)),
        "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
    }


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
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
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
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
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
    sqlite_repo = require_option_positions_event_write_repo(repo)
    if apply:
        result = rebuild_position_lots_from_trade_events(sqlite_repo)
        result["mode"] = "applied"
        return result
    events = sqlite_repo.list_trade_events()
    projection = project_position_lot_records_with_diagnostics(events)
    return {
        "mode": "dry_run",
        "trade_event_count": int(len(events)),
        "position_lot_count": int(len(projection.lots)),
        "projection_diagnostic_count": int(len(projection.diagnostics)),
        "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
    }


def preview_void_trade_event(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    preview = build_manual_void_preview(repo, target_event_id=event_id, void_reason=reason)
    events = sqlite_repo.list_trade_events() + [preview["void_event"]]
    return {
        "mode": "dry_run",
        "target_event_id": str(event_id),
        "void_reason": str(reason or ""),
        **preview,
        "projection_preview": _projection_preview_payload(events),
    }


def apply_void_trade_event(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    result = persist_manual_void_event(repo, target_event_id=event_id, void_reason=reason)
    result["mode"] = "applied"
    return result


def preview_repair_trade_event(
    repo: Any,
    *,
    event_id: str,
    overrides: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    preview = build_manual_repair_preview(
        repo,
        target_event_id=event_id,
        overrides=overrides,
        repair_reason=reason,
    )
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events() + [preview["void_event"], preview["repair_event"]]
    return {"mode": "dry_run", **preview, "projection_preview": _projection_preview_payload(events)}


def apply_repair_trade_event(
    repo: Any,
    *,
    event_id: str,
    overrides: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    result = persist_manual_repair_event(
        repo,
        target_event_id=event_id,
        overrides=overrides,
        repair_reason=reason,
    )
    result["mode"] = "applied"
    return result
