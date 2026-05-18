from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.events import LedgerDiagnostic, validate_trade_event
from src.application.ledger.migration import legacy_trade_event_to_ledger_event


@dataclass(frozen=True)
class EncodedTradeEvent:
    event: TradeEvent | None
    payload: dict[str, Any]
    event_json: str
    event_id_value: str = ""
    event_time_ms_value: int = 0

    @property
    def event_id(self) -> str:
        return self.event.event_id if self.event is not None else self.event_id_value

    @property
    def event_time_ms(self) -> int:
        return int(self.event.event_time_ms) if self.event is not None else int(self.event_time_ms_value)


def encode_trade_event_for_storage(item: Any) -> EncodedTradeEvent:
    event, diagnostics = stored_trade_event_to_ledger_event(item)
    errors = [diag for diag in diagnostics if diag.severity == "error"]
    if event is None or errors:
        passthrough = _encode_legacy_bootstrap_passthrough(item)
        if passthrough is not None:
            return passthrough
        codes = ", ".join(diag.code for diag in errors) or "event_decode_failed"
        raise ValueError(f"trade event could not be encoded for storage: {codes}")
    validation_errors = [diag for diag in validate_trade_event(event) if diag.severity == "error"]
    if validation_errors:
        codes = ", ".join(diag.code for diag in validation_errors)
        raise ValueError(f"trade event failed validation: {codes}")
    payload = event.to_dict()
    return EncodedTradeEvent(
        event=event,
        payload=payload,
        event_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def import_stored_trade_events(events: list[Any]) -> tuple[list[TradeEvent], list[LedgerDiagnostic]]:
    imported: list[TradeEvent] = []
    diagnostics: list[LedgerDiagnostic] = []
    for item in events:
        event, item_diagnostics = stored_trade_event_to_ledger_event(item)
        diagnostics.extend(item_diagnostics)
        if event is not None:
            imported.append(event)
    return imported, diagnostics


def stored_trade_event_to_ledger_event(item: Any) -> tuple[TradeEvent | None, list[LedgerDiagnostic]]:
    payload = trade_event_payload_dict(item)
    if _is_canonical_payload(payload):
        return _canonical_payload_to_ledger_event(payload)
    return legacy_trade_event_to_ledger_event(item)


def trade_event_payload_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if isinstance(item, TradeEvent):
        return item.to_dict()
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return dict(value) if isinstance(value, dict) else {}
    try:
        value = asdict(item)
    except TypeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def trade_event_application_payload(item: Any) -> dict[str, Any]:
    payload = trade_event_payload_dict(item)
    if not _is_canonical_payload(payload):
        return payload
    event, diagnostics = _canonical_payload_to_ledger_event(payload)
    if event is None or any(diag.severity == "error" for diag in diagnostics):
        return payload
    return _canonical_event_to_application_payload(event, stored_payload=payload)


def trade_event_sort_time_ms(item: Any) -> int:
    event, diagnostics = stored_trade_event_to_ledger_event(item)
    if event is not None and not any(diag.severity == "error" for diag in diagnostics):
        return int(event.event_time_ms)
    payload = trade_event_payload_dict(item)
    try:
        return int(payload.get("trade_time_ms") or payload.get("event_time_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _is_canonical_payload(payload: dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("contract_key"), dict)
        and str(payload.get("event_type") or "").strip() != ""
        and payload.get("event_time_ms") not in (None, "")
    )


def _encode_legacy_bootstrap_passthrough(item: Any) -> EncodedTradeEvent | None:
    payload = trade_event_payload_dict(item)
    if str(payload.get("source_type") or "").strip().lower() != "bootstrap_snapshot":
        return None
    raw_payload = payload.get("raw_payload")
    if not isinstance(raw_payload, dict):
        return None
    fields = raw_payload.get("fields")
    record_id = str(raw_payload.get("lot_record_id") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id or not record_id or not isinstance(fields, dict):
        return None
    event_time_ms = trade_event_sort_time_ms(payload)
    return EncodedTradeEvent(
        event=None,
        payload=payload,
        event_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        event_id_value=event_id,
        event_time_ms_value=event_time_ms,
    )


def _canonical_payload_to_ledger_event(payload: dict[str, Any]) -> tuple[TradeEvent | None, list[LedgerDiagnostic]]:
    event_id = str(payload.get("event_id") or "").strip()
    diagnostics: list[LedgerDiagnostic] = []
    try:
        contract_key = _contract_key_from_payload(payload.get("contract_key"))
        event = TradeEvent(
            event_id=event_id,
            event_type=str(payload.get("event_type") or "").strip(),
            event_time_ms=int(payload.get("event_time_ms") or 0),
            contract_key=contract_key,
            contracts=int(payload.get("contracts") or 0),
            price=float(payload.get("price") or 0.0),
            currency=str(payload.get("currency") or ""),
            source=str(payload.get("source") or ""),
            multiplier=float(payload.get("multiplier") or 0.0),
            fees=float(payload.get("fees") or 0.0),
            target_lot_id=_optional_id(payload.get("target_lot_id")),
            target_event_id=_optional_id(payload.get("target_event_id")),
            lot_id=_optional_id(payload.get("lot_id")),
            raw_payload=dict(payload.get("raw_payload") or {}),
        )
    except Exception as exc:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event_id,
                severity="error",
                code="canonical_event_decode_failed",
                message="canonical trade event could not be decoded",
                details={"error": str(exc)},
            )
        )
        return None, diagnostics
    diagnostics.extend(validate_trade_event(event))
    return event, diagnostics


def _contract_key_from_payload(raw: Any) -> ContractKey:
    if isinstance(raw, ContractKey):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("contract_key must be a JSON object")
    return ContractKey.from_values(
        broker=raw.get("broker"),
        account=raw.get("account"),
        underlying_symbol=raw.get("underlying_symbol") or raw.get("symbol"),
        option_type=raw.get("option_type"),
        position_side=raw.get("position_side") or raw.get("side"),
        strike=raw.get("strike"),
        expiration_ymd=raw.get("expiration_ymd") or raw.get("expiration"),
    )


def _canonical_event_to_application_payload(event: TradeEvent, *, stored_payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(stored_payload)
    contract_key = event.contract_key
    out.setdefault("trade_time_ms", int(event.event_time_ms))
    out.setdefault("source_name", event.source)
    out.setdefault("source_type", event.raw_payload.get("source_type") or event.source)
    out.setdefault("broker", contract_key.broker)
    out.setdefault("account", contract_key.account)
    out.setdefault("symbol", contract_key.underlying_symbol)
    out.setdefault("option_type", contract_key.option_type)
    out.setdefault("side", _legacy_trade_side(event))
    out.setdefault("position_effect", _legacy_position_effect(event.event_type))
    out.setdefault("strike", contract_key.strike)
    out.setdefault("multiplier", event.multiplier)
    out.setdefault("expiration_ymd", contract_key.expiration_ymd)
    return out


def _legacy_trade_side(event: TradeEvent) -> str:
    raw_side = str(event.raw_payload.get("side") or "").strip().lower()
    if raw_side:
        return raw_side
    position_side = event.contract_key.position_side
    if event.event_type == "open":
        return "sell" if position_side == "short" else "buy"
    if event.event_type in {"close", "expire_close", "assignment", "exercise"}:
        return "buy" if position_side == "short" else "sell"
    return position_side


def _legacy_position_effect(event_type: str) -> str:
    if event_type == "open":
        return "open"
    if event_type in {"close", "expire_close", "assignment", "exercise"}:
        return "close"
    return event_type


def _optional_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


__all__ = [
    "EncodedTradeEvent",
    "encode_trade_event_for_storage",
    "import_stored_trade_events",
    "stored_trade_event_to_ledger_event",
    "trade_event_application_payload",
    "trade_event_payload_dict",
    "trade_event_sort_time_ms",
]
