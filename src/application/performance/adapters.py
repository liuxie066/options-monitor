from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.ledger import ContractKey, OptionEconomicAllocation, TradeEvent
from src.application.ledger import api as ledger_api


@dataclass(frozen=True)
class LedgerPerformanceInputs:
    events: tuple[TradeEvent, ...]
    allocations: tuple[OptionEconomicAllocation, ...]
    diagnostics: tuple[dict[str, Any], ...]


def load_ledger_performance_inputs(repo: Any) -> LedgerPerformanceInputs:
    rows = ledger_api.trade_event_log(repo)
    projection = ledger_api.project_trade_event_log(rows)
    metadata_by_event_id = {
        str(row.get("event_id") or "").strip(): _diagnostic_metadata(row)
        for row in rows
        if str(row.get("event_id") or "").strip()
    }
    events: list[TradeEvent] = []
    adapter_diagnostics: list[dict[str, Any]] = []
    for row in rows:
        try:
            events.append(_trade_event_from_application_payload(row))
        except (TypeError, ValueError) as exc:
            adapter_diagnostics.append(
                {
                    "event_id": str(row.get("event_id") or "").strip(),
                    "severity": "error",
                    "code": "performance_event_decode_failed",
                    "message": str(exc),
                    **_diagnostic_metadata(row),
                }
            )
    diagnostics = []
    for item in projection.diagnostics:
        payload = item.to_dict()
        payload.update(metadata_by_event_id.get(item.event_id, {}))
        diagnostics.append(payload)
    diagnostics.extend(adapter_diagnostics)
    return LedgerPerformanceInputs(
        events=tuple(events),
        allocations=tuple(projection.ledger_projection.allocations),
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
    )


def _trade_event_from_application_payload(payload: dict[str, Any]) -> TradeEvent:
    raw_key = payload.get("contract_key")
    if not isinstance(raw_key, dict):
        raise ValueError("contract_key must be an object")
    raw_payload = dict(payload.get("raw_payload") or {})
    if isinstance(payload.get("fee_provenance"), dict) and "fee_provenance" not in raw_payload:
        raw_payload["fee_provenance"] = dict(payload["fee_provenance"])
    return TradeEvent(
        event_id=str(payload.get("event_id") or "").strip(),
        event_type=str(payload.get("event_type") or "").strip(),
        event_time_ms=int(payload.get("event_time_ms") or payload.get("trade_time_ms") or 0),
        contract_key=ContractKey.from_values(
            broker=raw_key.get("broker"),
            account=raw_key.get("account"),
            underlying_symbol=raw_key.get("underlying_symbol") or raw_key.get("symbol"),
            option_type=raw_key.get("option_type"),
            position_side=raw_key.get("position_side") or raw_key.get("side"),
            strike=raw_key.get("strike"),
            expiration_ymd=raw_key.get("expiration_ymd") or raw_key.get("expiration"),
        ),
        contracts=int(payload.get("contracts") or 0),
        price=float(payload.get("price") or 0),
        currency=str(payload.get("currency") or ""),
        source=str(payload.get("source") or payload.get("source_name") or ""),
        multiplier=float(payload.get("multiplier") or 0),
        fees=float(payload.get("fees") or 0),
        target_lot_id=_optional_id(payload.get("target_lot_id")),
        target_event_id=_optional_id(payload.get("target_event_id")),
        lot_id=_optional_id(payload.get("lot_id")),
        raw_payload=raw_payload,
    )


def _optional_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _diagnostic_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    raw_key = payload.get("contract_key")
    contract_key = raw_key if isinstance(raw_key, dict) else {}
    try:
        event_time_ms = int(payload.get("event_time_ms") or payload.get("trade_time_ms") or 0)
    except (TypeError, ValueError):
        event_time_ms = 0
    return {
        "event_time_ms": event_time_ms,
        "account": str(contract_key.get("account") or payload.get("account") or "").strip().lower(),
        "broker": str(contract_key.get("broker") or payload.get("broker") or "").strip(),
    }


def _dedupe_diagnostics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (
            str(item.get("event_id") or ""),
            str(item.get("code") or ""),
            str(item.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(item))
    return out


__all__ = ["LedgerPerformanceInputs", "load_ledger_performance_inputs"]
