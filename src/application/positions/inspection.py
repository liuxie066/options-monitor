from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_option_type,
)
from domain.domain.ledger.identity import ContractKey
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.api import (
    list_position_lot_snapshots,
    position_projection_verify_state,
    project_trade_event_log,
    trade_event_log,
)
from src.application.trade_time_format import format_trade_time_beijing


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


def _event_payload(event: dict[str, object]) -> dict[str, object]:
    payload = event.get("raw_payload")
    return payload if isinstance(payload, dict) else {}


def _lot_fields(row: dict[str, object]) -> dict[str, object]:
    fields = row.get("fields")
    return fields if isinstance(fields, dict) else {}


def _event_record_refs(event: dict[str, object]) -> set[str]:
    payload = _event_payload(event)
    refs = {
        str(event.get("event_id") or "").strip(),
        str(payload.get("record_id") or "").strip(),
        str(payload.get("target_lot_id") or "").strip(),
        str(payload.get("lot_record_id") or "").strip(),
        str(payload.get("lot_id") or "").strip(),
        str(payload.get("close_target_source_event_id") or "").strip(),
        str(payload.get("adjust_target_source_event_id") or "").strip(),
        str(payload.get("void_target_event_id") or "").strip(),
        str(payload.get("target_event_id") or "").strip(),
    }
    refs.update(f"lot_{item}" for item in list(refs) if item and not item.startswith("lot_"))
    return {item for item in refs if item}


def _event_to_history_row(event: dict[str, object], *, fallback_record_id: str | None = None) -> dict[str, object]:
    payload = _event_payload(event)
    trade_time_ms = event.get("trade_time_ms")
    row = {
        "event_id": str(event.get("event_id") or "").strip(),
        "trade_time_ms": trade_time_ms,
        "source_type": event.get("source_type"),
        "source_name": event.get("source_name"),
        "broker": normalize_broker(_optional_text(event.get("broker"))),
        "account": normalize_account(event.get("account")) if event.get("account") else None,
        "symbol": event.get("symbol"),
        "option_type": event.get("option_type"),
        "side": event.get("side"),
        "position_effect": event.get("position_effect"),
        "contracts": event.get("contracts"),
        "price": event.get("price"),
        "strike": event.get("strike"),
        "expiration_ymd": event.get("expiration_ymd"),
        "currency": event.get("currency"),
        "void_target_event_id": payload.get("void_target_event_id") or payload.get("target_event_id"),
        "adjust_target_source_event_id": payload.get("adjust_target_source_event_id"),
        "close_target_source_event_id": payload.get("close_target_source_event_id"),
        "record_id": (
            payload.get("record_id")
            or payload.get("target_lot_id")
            or payload.get("lot_record_id")
            or fallback_record_id
        ),
        "patch": payload.get("patch") if isinstance(payload.get("patch"), dict) else None,
    }
    trade_time_beijing = format_trade_time_beijing(trade_time_ms)
    if trade_time_beijing is not None:
        row["trade_time_beijing"] = trade_time_beijing
    return row


def _lot_with_beijing_time_fields(row: dict[str, object]) -> dict[str, object]:
    out = dict(row)
    fields = row.get("fields")
    if not isinstance(fields, dict):
        return out
    copied_fields = dict(fields)
    for key in ("opened_at", "closed_at", "last_action_at"):
        formatted = format_trade_time_beijing(copied_fields.get(key))
        if formatted is not None:
            copied_fields[f"{key}_beijing"] = formatted
    out["fields"] = copied_fields
    return out


def _event_matches_lot(event: dict[str, object], *, record_id: str, fields: dict[str, object]) -> bool:
    source_event_id = str(fields.get("source_event_id") or "").strip()
    refs = _event_record_refs(event)
    if str(record_id).strip() in refs or (source_event_id and source_event_id in refs):
        return True
    return _identity_matches_payload(
        event,
        account=_optional_text(fields.get("account")),
        symbol=_optional_text(fields.get("symbol")),
        option_type=_optional_text(fields.get("option_type")),
        strike=effective_strike(fields),
        expiration_ymd=effective_expiration_ymd(fields),
    )


def build_lot_event_history(repo, *, base: Path, record_id: str) -> list[dict[str, object]]:
    _ = base
    current = next(
        (
            item
            for item in list_position_lot_snapshots(repo)
            if str(item.get("record_id") or "").strip() == str(record_id).strip()
        ),
        None,
    )
    if current is None:
        raise ValueError(f"position lot not found: {record_id}")
    fields = current.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    history = [
        _event_to_history_row(event, fallback_record_id=record_id)
        for event in trade_event_log(repo)
        if _event_matches_lot(event, record_id=record_id, fields=fields)
    ]
    history.sort(key=lambda row: (_safe_int(row.get("trade_time_ms")), str(row.get("event_id") or "")))
    return history


def _matches_lot_selector(
    row: dict[str, object],
    *,
    record_id: str | None,
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
        current_expiration = effective_expiration_ymd(fields) or exp_ms_to_ymd(fields.get("expiration")) or str(fields.get("expiration") or "")
        current_note = str(fields.get("note") or "")
        if expiration_ymd not in current_note and expiration_ymd not in current_expiration:
            return False
    return True


def _matches_projected_selector(
    row: dict[str, object],
    *,
    record_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    if record_id and str(row.get("record_id") or "").strip() != str(record_id).strip():
        return False
    if account and normalize_account(row.get("account")) != normalize_account(account):
        return False
    if symbol and canonical_contract_symbol(row.get("symbol")) != canonical_contract_symbol(symbol):
        return False
    if option_type and str(row.get("option_type") or "").strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = _safe_float(row.get("strike"))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd and str(row.get("expiration_ymd") or "").strip() != str(expiration_ymd).strip():
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
    if record_id and str(record_id).strip() not in _event_record_refs(event):
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


def _canonical_position_key_from_fields(fields: dict[str, object]) -> str | None:
    try:
        key = ContractKey.from_values(
            broker=fields.get("broker") or fields.get("market"),
            account=fields.get("account"),
            underlying_symbol=fields.get("symbol") or fields.get("underlying_symbol"),
            option_type=fields.get("option_type"),
            position_side=fields.get("side") or fields.get("position_side"),
            strike=effective_strike(fields),
            expiration_ymd=fields.get("expiration_ymd") or effective_expiration_ymd(fields),
        )
    except Exception:
        return None
    return key.position_key


def _projected_lot_view(row: Any) -> dict[str, object]:
    if not isinstance(row, dict) and callable(getattr(row, "to_dict", None)):
        row = row.to_dict()
    if not isinstance(row, dict):
        row = {}
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    return {
        "record_id": str(row.get("record_id") or "").strip(),
        "position_key": str(fields.get("position_key") or _canonical_position_key_from_fields(fields) or "").strip(),
        "broker": normalize_broker(fields.get("broker")),
        "account": normalize_account(fields.get("account")) if fields.get("account") else None,
        "symbol": fields.get("symbol"),
        "option_type": fields.get("option_type"),
        "side": fields.get("side"),
        "expiration_ymd": fields.get("expiration_ymd") or effective_expiration_ymd(fields),
        "strike": effective_strike(fields),
        "currency": fields.get("currency"),
        "multiplier": fields.get("multiplier"),
        "baseline_contracts": None,
        "current_contracts": effective_contracts_open(fields),
        "status": fields.get("status"),
        "source_event_id": fields.get("source_event_id"),
        "last_close_event_id": fields.get("last_close_event_id"),
    }


def _report_matches_position_keys(report: dict[str, object] | None, keys: set[str]) -> bool:
    if not report or not keys:
        return False
    items = report.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and (
            str(item.get("position_key") or "").strip() in keys
            or str(item.get("record_id") or "").strip() in keys
        )
        for item in items
    )


def inspect_projection_state(
    repo,
    *,
    base: Path,
    record_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
) -> dict[str, object]:
    current_rows = list_position_lot_snapshots(repo)
    events = trade_event_log(repo)
    projection = project_trade_event_log(events)
    projected_rows = projection.lots

    matched_current = [
        row
        for row in current_rows
        if _matches_lot_selector(
            row,
            record_id=record_id,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    matched_record_ids = {str(row.get("record_id") or "").strip() for row in matched_current if str(row.get("record_id") or "").strip()}
    matched_position_keys = {str(_lot_fields(row).get("position_key") or "").strip() for row in matched_current}
    matched_position_keys.update(
        key
        for row in matched_current
        for key in [_canonical_position_key_from_fields(_lot_fields(row))]
        if key
    )
    projected_views = [_projected_lot_view(row) for row in projected_rows]
    matched_projected = [
        row
        for row in projected_views
        if (
            str(row.get("position_key") or "").strip() in matched_position_keys
            or _matches_projected_selector(
                row,
                record_id=record_id,
                account=account,
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiration_ymd=expiration_ymd,
            )
        )
    ]
    matched_position_keys.update(str(row.get("position_key") or "").strip() for row in matched_projected if str(row.get("position_key") or "").strip())
    baseline_lots: list[dict[str, object]] = []
    has_event_selector = any(
        value is not None and str(value).strip()
        for value in (record_id, account, symbol, option_type, expiration_ymd)
    ) or strike is not None
    related_events = [
        _event_to_history_row(event)
        for event in events
        if any(
            _event_matches_lot(
                event,
                record_id=str(row.get("record_id") or ""),
                fields=_lot_fields(row),
            )
            for row in matched_current
        )
        or (
            has_event_selector
            and _matches_event_selector(
                event,
                record_id=record_id,
                account=account,
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiration_ymd=expiration_ymd,
            )
        )
    ]
    related_events.sort(key=lambda row: (_safe_int(row.get("trade_time_ms")), str(row.get("event_id") or "")))

    filtered_diagnostics = [
        item.to_dict()
        for item in projection.diagnostics
        if str(item.event_id or "").strip() in {str(event.get("event_id") or "").strip() for event in related_events}
        or str((item.details or {}).get("target_lot_id") or "").strip() in set(matched_record_ids)
        or str((item.details or {}).get("lot_id") or "").strip() in set(matched_record_ids)
    ]
    matched_report_keys = set(matched_position_keys) | set(matched_record_ids)
    projection_verify_state = position_projection_verify_state(base)
    projection_verify_report = projection_verify_state.get("latest_projection_verify_report")
    latest_projection_verify_report = None
    if isinstance(projection_verify_report, dict) and _report_matches_position_keys(projection_verify_report, matched_report_keys):
        latest_projection_verify_report = projection_verify_report
    projection_verify_checkpoint = projection_verify_state.get("latest_projection_verify_checkpoint")
    projection_verify_checkpoint_id = (
        projection_verify_checkpoint.get("checkpoint_id") if isinstance(projection_verify_checkpoint, dict) else None
    )
    return {
        "selectors": {
            "record_id": record_id,
            "account": account,
            "symbol": symbol,
            "option_type": option_type,
            "strike": strike,
            "expiration_ymd": expiration_ymd,
        },
        "matched_record_ids": sorted(matched_record_ids),
        "current_lots": [_lot_with_beijing_time_fields(row) for row in matched_current],
        "projected_lots": matched_projected,
        "projection_verify_checkpoint_id": projection_verify_checkpoint_id,
        "baseline_lots": baseline_lots,
        "related_events": related_events,
        "projection_diagnostics": filtered_diagnostics,
        "all_projection_diagnostic_count": len(projection.diagnostics),
        "latest_projection_verify_report": latest_projection_verify_report,
        "latest_projection_verify_summary": (latest_projection_verify_report or {}).get("summary") or {},
    }
