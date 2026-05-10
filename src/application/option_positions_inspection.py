from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from domain.domain.option_position_lots import (
    effective_expiration_ymd,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_option_type,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.option_positions_v2_service import load_option_positions_v2_records


__all__ = ["build_lot_event_history", "inspect_projection_state"]


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _iso_to_trade_time_ms(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        print(f"[WARN] invalid v2 event_at_utc timestamp: {text}; using null trade_time_ms")
        return None


def _identity_matches_payload(
    payload: dict[str, object],
    *,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    if account and normalize_account(payload.get("account")) != normalize_account(account):
        return False
    if symbol and canonical_contract_symbol(payload.get("symbol")) != canonical_contract_symbol(symbol):
        return False
    if option_type and normalize_option_type(payload.get("option_type")) != normalize_option_type(option_type):
        return False
    if strike is not None:
        current_strike = _safe_float(payload.get("strike"))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd:
        current_expiration = str(payload.get("expiration_ymd") or "").strip() or effective_expiration_ymd(payload)
        if current_expiration != str(expiration_ymd).strip():
            return False
    return True


def _v2_position_effect(event_kind: object) -> str:
    mapping = {
        "open_trade": "open",
        "close_trade": "close",
        "manual_adjustment": "adjust",
    }
    return mapping.get(str(event_kind or "").strip(), str(event_kind or "").strip())


def _related_legacy_void_rows(repo, *, related_event_ids: set[str], record_id: str | None) -> list[dict[str, object]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return []
    rows: list[dict[str, object]] = []
    raw_events = list_trade_events()
    events = raw_events if isinstance(raw_events, list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("position_effect") or "").strip().lower() != "void":
            continue
        payload = event.get("raw_payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        target_event_id = str(payload.get("void_target_event_id") or "").strip()
        target_record_id = str(payload.get("record_id") or "").strip()
        if target_event_id not in related_event_ids and (record_id is None or target_record_id != str(record_id).strip()):
            continue
        rows.append(
            {
                "event_id": str(event.get("event_id") or "").strip(),
                "trade_time_ms": event.get("trade_time_ms"),
                "source_type": event.get("source_type"),
                "source_name": event.get("source_name"),
                "broker": normalize_broker(event.get("broker")),
                "account": normalize_account(event.get("account")) if event.get("account") else None,
                "symbol": event.get("symbol"),
                "option_type": event.get("option_type"),
                "side": event.get("side"),
                "position_effect": "void",
                "contracts": event.get("contracts"),
                "price": event.get("price"),
                "strike": event.get("strike"),
                "expiration_ymd": event.get("expiration_ymd"),
                "currency": event.get("currency"),
                "void_target_event_id": target_event_id or None,
                "adjust_target_source_event_id": None,
                "close_target_source_event_id": None,
                "record_id": target_record_id or record_id,
                "patch": None,
            }
        )
    return rows


def _related_legacy_adjust_rows(
    repo,
    *,
    record_id: str,
    fields: dict[str, object],
) -> list[dict[str, object]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return []
    rows: list[dict[str, object]] = []
    raw_events = list_trade_events()
    events = raw_events if isinstance(raw_events, list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("position_effect") or "").strip().lower() != "adjust":
            continue
        if not _matches_event_selector(
            event,
            record_id=record_id,
            account=_optional_text(fields.get("account")),
            symbol=_optional_text(fields.get("symbol")),
            option_type=_optional_text(fields.get("option_type")),
            strike=effective_strike(fields),
            expiration_ymd=effective_expiration_ymd(fields),
        ):
            continue
        payload = event.get("raw_payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        rows.append(
            {
                "event_id": str(event.get("event_id") or "").strip(),
                "trade_time_ms": event.get("trade_time_ms"),
                "source_type": event.get("source_type"),
                "source_name": event.get("source_name"),
                "broker": normalize_broker(event.get("broker")),
                "account": normalize_account(event.get("account")) if event.get("account") else None,
                "symbol": event.get("symbol"),
                "option_type": event.get("option_type"),
                "side": event.get("side"),
                "position_effect": "adjust",
                "contracts": event.get("contracts"),
                "price": event.get("price"),
                "strike": event.get("strike"),
                "expiration_ymd": event.get("expiration_ymd"),
                "currency": event.get("currency"),
                "void_target_event_id": None,
                "adjust_target_source_event_id": payload.get("adjust_target_source_event_id"),
                "close_target_source_event_id": None,
                "record_id": payload.get("record_id") or record_id,
                "patch": payload.get("patch") if isinstance(payload.get("patch"), dict) else None,
            }
        )
    return rows


def build_lot_event_history(repo, *, base: Path, record_id: str) -> list[dict[str, object]]:
    compat = load_option_positions_v2_records(base=base, repo=repo)
    current = next((item for item in compat.records if str(item.get("record_id") or "").strip() == str(record_id).strip()), None)
    if current is None:
        raise ValueError(f"position lot not found: {record_id}")
    fields = current.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    history: list[dict[str, object]] = []
    related_event_ids: set[str] = set()
    for event in compat.state.events:
        if not _identity_matches_payload(
            event,
            account=fields.get("account"),
            symbol=fields.get("symbol"),
            option_type=fields.get("option_type"),
            strike=effective_strike(fields),
            expiration_ymd=effective_expiration_ymd(fields),
        ):
            continue
        event_id = str(event.get("event_id") or "").strip()
        if event_id:
            related_event_ids.add(event_id)
        history.append(
            {
                "event_id": event_id,
                "trade_time_ms": _iso_to_trade_time_ms(event.get("event_at_utc")),
                "source_type": event.get("source_type"),
                "source_name": event.get("source_name"),
                "broker": normalize_broker(event.get("broker")),
                "account": normalize_account(event.get("account")) if event.get("account") else None,
                "symbol": event.get("symbol"),
                "option_type": event.get("option_type"),
                "side": event.get("side"),
                "position_effect": _v2_position_effect(event.get("event_kind")),
                "contracts": event.get("contracts"),
                "price": None,
                "strike": event.get("strike"),
                "expiration_ymd": event.get("expiration_ymd"),
                "currency": event.get("currency"),
                "void_target_event_id": None,
                "adjust_target_source_event_id": None,
                "close_target_source_event_id": None,
                "record_id": event.get("snapshot_lot_id") or record_id,
                "patch": {"target_contracts": event.get("target_contracts")} if event.get("target_contracts") is not None else None,
            }
        )
    for row in _related_legacy_adjust_rows(repo, record_id=record_id, fields=fields):
        event_id = str(row.get("event_id") or "").strip()
        if event_id and event_id not in related_event_ids:
            related_event_ids.add(event_id)
            history.append(row)
    history.extend(_related_legacy_void_rows(repo, related_event_ids=related_event_ids, record_id=record_id))
    history.sort(key=lambda row: (_safe_int(row.get("trade_time_ms")), str(row.get("event_id") or "")))
    return history


def _matches_lot_selector(
    row: dict[str, object],
    *,
    record_id: str | None,
    feishu_record_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    row_record_id = str(row.get("record_id") or "").strip()
    fields = row.get("fields") or {}
    if not isinstance(fields, dict):
        return False
    if record_id and row_record_id != str(record_id).strip():
        return False
    if feishu_record_id and str(fields.get("feishu_record_id") or "").strip() != str(feishu_record_id).strip():
        return False
    if account and normalize_account(fields.get("account")) != normalize_account(account):
        return False
    if symbol and canonical_contract_symbol(fields.get("symbol")) != canonical_contract_symbol(symbol):
        return False
    if option_type and str(fields.get("option_type") or "").strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = _safe_float(fields.get("strike"))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd:
        current_expiration = exp_ms_to_ymd(fields.get("expiration")) or str(fields.get("expiration") or "")
        current_note = str(fields.get("note") or "")
        if expiration_ymd not in current_note and expiration_ymd not in current_expiration:
            return False
    return True


def _matches_event_selector(
    event: dict[str, object],
    *,
    record_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    payload = event.get("raw_payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if record_id and str(payload.get("record_id") or "").strip() != str(record_id).strip():
        return False
    if account and normalize_account(event.get("account")) != normalize_account(account):
        return False
    if symbol and canonical_contract_symbol(event.get("symbol")) != canonical_contract_symbol(symbol):
        return False
    if option_type and str(event.get("option_type") or "").strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = _safe_float(event.get("strike"))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd and str(event.get("expiration_ymd") or "").strip() != str(expiration_ymd).strip():
        return False
    return True


def _should_show_projected_position(row: dict[str, object]) -> bool:
    if _safe_int(row.get("baseline_contracts")) > 0:
        return True
    if _safe_int(row.get("current_contracts")) > 0:
        return True
    return bool(row.get("applied_events") or row.get("applied_verifications"))


def inspect_projection_state(
    repo,
    *,
    base: Path,
    record_id: str | None = None,
    feishu_record_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
) -> dict[str, object]:
    compat = load_option_positions_v2_records(base=base, repo=repo)
    current_rows = compat.records
    state = compat.state
    projection = state.projection
    baseline_snapshot = state.baseline_snapshot
    events = state.events

    matched_current = [
        row
        for row in current_rows
        if _matches_lot_selector(
            row,
            record_id=record_id,
            feishu_record_id=feishu_record_id,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    matched_record_ids = {str(row.get("record_id") or "").strip() for row in matched_current if str(row.get("record_id") or "").strip()}
    matched_position_keys = {
        str(((row.get("fields") or {}).get("position_key") or "")).strip()
        for row in matched_current
        if isinstance(row.get("fields"), dict)
    }
    matched_projected = [
        row
        for row in (projection.get("positions") or [])
        if _should_show_projected_position(row)
        and (
            str(row.get("position_key") or "").strip() in matched_position_keys
            or _identity_matches_payload(
                row,
                account=account,
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiration_ymd=expiration_ymd,
            )
        )
    ]
    matched_position_keys.update(str(row.get("position_key") or "").strip() for row in matched_projected if str(row.get("position_key") or "").strip())
    baseline_lots = [
        row
        for row in (baseline_snapshot.get("lots") or [])
        if str(row.get("position_key") or "").strip() in matched_position_keys
        or _identity_matches_payload(
            row,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    related_events = [
        {
            "event_id": str(event.get("event_id") or "").strip(),
            "trade_time_ms": _iso_to_trade_time_ms(event.get("event_at_utc")),
            "source_type": event.get("source_type"),
            "source_name": event.get("source_name"),
            "broker": event.get("broker"),
            "account": event.get("account"),
            "symbol": event.get("symbol"),
            "option_type": event.get("option_type"),
            "side": event.get("side"),
            "position_effect": event.get("event_kind"),
            "contracts": event.get("contracts"),
            "price": None,
            "strike": event.get("strike"),
            "expiration_ymd": event.get("expiration_ymd"),
            "currency": event.get("currency"),
            "record_id": event.get("snapshot_lot_id"),
            "close_target_source_event_id": None,
            "adjust_target_source_event_id": None,
            "void_target_event_id": None,
        }
        for event in events
        if str(event.get("position_key") or "").strip() in matched_position_keys
        or _identity_matches_payload(
            event,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    related_events.sort(key=lambda row: (int(row.get("trade_time_ms") or 0), str(row.get("event_id") or "")))

    filtered_diagnostics = [
        item
        for item in (projection.get("diagnostics") or [])
        if str(item.get("position_key") or "").strip() in matched_position_keys
        or str(item.get("event_id") or "").strip() in {str(event.get("event_id") or "").strip() for event in related_events}
    ]
    latest_reconciliation_report = state.latest_reconciliation_report or None
    return {
        "selectors": {
            "record_id": record_id,
            "feishu_record_id": feishu_record_id,
            "account": account,
            "symbol": symbol,
            "option_type": option_type,
            "strike": strike,
            "expiration_ymd": expiration_ymd,
        },
        "matched_record_ids": sorted(matched_record_ids),
        "current_lots": matched_current,
        "projected_lots": matched_projected,
        "persisted_baseline_snapshot_id": state.persisted_baseline_snapshot.get("snapshot_id"),
        "projection_checkpoint_snapshot_id": (state.latest_verification_snapshot or {}).get("snapshot_id"),
        "baseline_snapshot_id": baseline_snapshot.get("snapshot_id"),
        "verification_snapshot_count": len(state.verification_snapshots),
        "accepted_verification_snapshot_count": len(state.accepted_verification_snapshots),
        "latest_verification_snapshot_id": (state.latest_verification_snapshot or {}).get("snapshot_id"),
        "baseline_lots": baseline_lots,
        "related_events": related_events,
        "projection_diagnostics": filtered_diagnostics,
        "all_projection_diagnostic_count": len(projection.get("diagnostics") or []),
        "latest_reconciliation_report": latest_reconciliation_report,
        "latest_reconciliation_summary": (latest_reconciliation_report or {}).get("summary") or {},
    }
