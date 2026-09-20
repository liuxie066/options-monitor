from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    normalize_account,
    normalize_broker,
    normalize_option_type,
)
from domain.domain.ledger.identity import ContractKey, position_key_for
from domain.domain.option_position_identity import normalize_side
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.api import (
    list_position_lot_snapshots,
    position_projection_verify_state,
    project_trade_event_log,
    trade_event_log,
)
from src.application.trade_time_format import format_trade_time_beijing
from src.application.payload_helpers import optional_text as _optional_text


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


def _lot_contract_value(fields: dict[str, object], nested_key: str) -> object:
    """One contract value from a lot payload: nested ``contract_key`` first.

    The converged payload (``PositionLot.to_dict()``) carries the option contract
    under ``contract_key``; the read model also publishes the same facts as flat
    siblings, and a row written before the shape switch has only those, so the
    flat spelling is read after the nested one. Trade *event* dicts are a
    different layer: they carry the flat spellings, so callers read them
    directly.
    """
    contract_key = fields.get("contract_key")
    contract_key = contract_key if isinstance(contract_key, dict) else {}
    value = contract_key.get(nested_key)
    if value not in (None, ""):
        return value
    value = fields.get(nested_key)
    if value not in (None, ""):
        return value
    for flat_key in _CONTRACT_FLAT_ALIASES.get(nested_key, ()):
        value = fields.get(flat_key)
        if value not in (None, ""):
            return value
    return None


#: Nested ``contract_key`` member -> the flat spellings it replaced.
_CONTRACT_FLAT_ALIASES: dict[str, tuple[str, ...]] = {
    "broker": ("broker",),
    "account": ("account",),
    "underlying_symbol": ("symbol", "underlying_symbol"),
    "option_type": ("option_type",),
    "strike": ("strike",),
    "expiration_ymd": ("expiration_ymd", "exp"),
}


def _lot_position_side(fields: dict[str, object]) -> str:
    for key in ("position_side", "side"):
        value = str(fields.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def _lot_expiration_ymd(fields: dict[str, object]) -> str:
    return str(_lot_contract_value(fields, "expiration_ymd") or "").strip()


def _lot_last_close_event_id(fields: dict[str, object]) -> str | None:
    """The lot's last close event id.

    ``last_close_event_id`` converged onto ``close_event_ids`` /
    ``last_event_id`` (``write-side-definition.md`` §2). ``close_event_ids`` is
    the lot's own close pointer and is produced by the close transition itself,
    so it is read first; ``last_event_id`` is the same fact for a lot the closing
    event was the last to touch. The retired flat key stays the last resort for a
    row written before the shape switch.
    """
    close_event_ids = fields.get("close_event_ids")
    if isinstance(close_event_ids, (list, tuple)):
        closed = [str(item or "").strip() for item in close_event_ids if str(item or "").strip()]
        if closed:
            return closed[-1]
    # ``last_event_id`` is the same fact for a lot that its closing event was the
    # last to touch, so it is read under the guard that nothing is left open --
    # otherwise it would report the open (or an adjust) event as a close.
    if effective_contracts_open(fields) <= 0:
        last_event_id = str(fields.get("last_event_id") or "").strip()
        if last_event_id:
            return last_event_id
    return str(fields.get("last_close_event_id") or "").strip() or None


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


def _event_to_history_row(event: dict[str, object], *, fallback_lot_id: str | None = None) -> dict[str, object]:
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
            or fallback_lot_id
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


def _event_matches_lot(event: dict[str, object], *, lot_id: str, fields: dict[str, object]) -> bool:
    # ``source_event_id`` converged onto ``open_event_id`` (write-side-definition
    # §2): the lot's source open is the same fact under its new name.
    source_event_id = str(fields.get("open_event_id") or "").strip()
    refs = _event_record_refs(event)
    if str(lot_id).strip() in refs or (source_event_id and source_event_id in refs):
        return True
    return _identity_matches_payload(
        event,
        account=_optional_text(_lot_contract_value(fields, "account")),
        symbol=_optional_text(_lot_contract_value(fields, "underlying_symbol")),
        option_type=_optional_text(_lot_contract_value(fields, "option_type")),
        strike=_safe_float(_lot_contract_value(fields, "strike")),
        expiration_ymd=_lot_expiration_ymd(fields) or None,
    )


def build_lot_event_history(repo, *, base: Path, lot_id: str) -> list[dict[str, object]]:
    _ = base
    requested_lot_id = str(lot_id or "").strip()
    if not requested_lot_id:
        raise ValueError("record_id is required")
    current = next(
        (
            item
            for item in list_position_lot_snapshots(repo)
            if str(item.get("record_id") or "").strip() == requested_lot_id
        ),
        None,
    )
    fields = current.get("fields") or {} if current is not None else {}
    if not isinstance(fields, dict):
        fields = {}
    history = [
        _event_to_history_row(event, fallback_lot_id=lot_id)
        for event in trade_event_log(repo)
        if (
            _event_matches_lot(event, lot_id=requested_lot_id, fields=fields)
            if current is not None
            else requested_lot_id in _event_record_refs(event)
        )
    ]
    if not history:
        raise ValueError(f"position lot or event history not found: {lot_id}")
    history.sort(key=lambda row: (_safe_int(row.get("trade_time_ms")), str(row.get("event_id") or "")))
    return history


def _matches_lot_selector(
    row: dict[str, object],
    *,
    lot_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    row_lot_id = str(row.get("record_id") or "").strip()
    fields = row.get("fields") or {}
    if not isinstance(fields, dict):
        return False
    if lot_id and row_lot_id != str(lot_id).strip():
        return False
    if account and normalize_account(_lot_contract_value(fields, "account")) != normalize_account(account):
        return False
    if symbol and canonical_contract_symbol(_lot_contract_value(fields, "underlying_symbol")) != canonical_contract_symbol(symbol):
        return False
    if option_type and str(_lot_contract_value(fields, "option_type") or "").strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = _safe_float(_lot_contract_value(fields, "strike"))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd:
        current_expiration = _lot_expiration_ymd(fields)
        if str(expiration_ymd).strip() != current_expiration:
            return False
    return True


def _matches_projected_selector(
    row: dict[str, object],
    *,
    lot_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    if lot_id and str(row.get("record_id") or "").strip() != str(lot_id).strip():
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
    lot_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    if lot_id and str(lot_id).strip() not in _event_record_refs(event):
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
            broker=_lot_contract_value(fields, "broker"),
            account=_lot_contract_value(fields, "account"),
            underlying_symbol=_lot_contract_value(fields, "underlying_symbol"),
            option_type=_lot_contract_value(fields, "option_type"),
            strike=_safe_float(_lot_contract_value(fields, "strike")),
            expiration_ymd=_lot_expiration_ymd(fields) or None,
        )
    except Exception:
        return None
    return position_key_for(key, normalize_side(_lot_position_side(fields)))


def _projected_lot_view(row: Any) -> dict[str, object]:
    if not isinstance(row, dict) and callable(getattr(row, "to_dict", None)):
        row = row.to_dict()
    if not isinstance(row, dict):
        row = {}
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    # The view keeps its own flat vocabulary (``_matches_projected_selector``
    # and the ``inspect-projection`` report read it), so the converged payload's
    # keys are re-pointed here rather than renamed.
    symbol = str(_lot_contract_value(fields, "underlying_symbol") or "").strip() or None
    account = str(_lot_contract_value(fields, "account") or "").strip() or None
    return {
        "record_id": str(row.get("record_id") or "").strip(),
        "position_key": str(fields.get("position_key") or _canonical_position_key_from_fields(fields) or "").strip(),
        "broker": normalize_broker(_lot_contract_value(fields, "broker")) or None,
        "account": normalize_account(account) if account else None,
        "symbol": symbol,
        "option_type": str(_lot_contract_value(fields, "option_type") or "").strip() or None,
        "side": _lot_position_side(fields) or None,
        "expiration_ymd": _lot_expiration_ymd(fields) or None,
        "strike": _safe_float(_lot_contract_value(fields, "strike")),
        "currency": fields.get("currency"),
        "multiplier": fields.get("multiplier"),
        "baseline_contracts": None,
        "current_contracts": effective_contracts_open(fields),
        "status": fields.get("status"),
        "source_event_id": str(_lot_contract_value(fields, "open_event_id") or "").strip() or None,
        "last_close_event_id": _lot_last_close_event_id(fields),
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
    lot_id: str | None = None,
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
            lot_id=lot_id,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    matched_lot_ids = {str(row.get("record_id") or "").strip() for row in matched_current if str(row.get("record_id") or "").strip()}
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
                lot_id=lot_id,
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
        for value in (lot_id, account, symbol, option_type, expiration_ymd)
    ) or strike is not None
    related_events = [
        _event_to_history_row(event)
        for event in events
        if any(
            _event_matches_lot(
                event,
                lot_id=str(row.get("record_id") or ""),
                fields=_lot_fields(row),
            )
            for row in matched_current
        )
        or (
            has_event_selector
            and _matches_event_selector(
                event,
                lot_id=lot_id,
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
        or str((item.details or {}).get("target_lot_id") or "").strip() in set(matched_lot_ids)
        or str((item.details or {}).get("lot_id") or "").strip() in set(matched_lot_ids)
    ]
    matched_report_keys = set(matched_position_keys) | set(matched_lot_ids)
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
            "record_id": lot_id,
            "account": account,
            "symbol": symbol,
            "option_type": option_type,
            "strike": strike,
            "expiration_ymd": expiration_ymd,
        },
        "matched_record_ids": sorted(matched_lot_ids),
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
