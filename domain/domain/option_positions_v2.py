from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from scripts.option_positions_core.domain import (
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
    normalize_side,
)
from scripts.trade_contract_identity import canonical_contract_symbol


OPTION_POSITIONS_V2_SCHEMA_VERSION = "1.0"
SCHEMA_KIND_POSITION_SNAPSHOT = "option_position_snapshot"
SCHEMA_KIND_POSITION_EVENT = "option_position_event"
SCHEMA_KIND_POSITION_PROJECTION = "option_position_projection"
SCHEMA_KIND_POSITION_RECONCILIATION = "option_position_reconciliation"

SNAPSHOT_TYPE_BASELINE = "baseline"
SNAPSHOT_TYPE_VERIFICATION = "verification"

EVENT_KIND_OPEN_TRADE = "open_trade"
EVENT_KIND_CLOSE_TRADE = "close_trade"
EVENT_KIND_MANUAL_ADJUSTMENT = "manual_adjustment"

ALLOWED_SNAPSHOT_TYPES = {SNAPSHOT_TYPE_BASELINE, SNAPSHOT_TYPE_VERIFICATION}
ALLOWED_EVENT_KINDS = {
    EVENT_KIND_OPEN_TRADE,
    EVENT_KIND_CLOSE_TRADE,
    EVENT_KIND_MANUAL_ADJUSTMENT,
}
ALLOWED_VERIFICATION_STATUS = {"confirmed", "unverified", "disputed"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _normalize_snapshot_type(value: Any) -> str:
    snapshot_type = str(value or "").strip().lower()
    if snapshot_type not in ALLOWED_SNAPSHOT_TYPES:
        raise ValueError("snapshot_type must be baseline or verification")
    return snapshot_type


def _normalize_event_kind(value: Any) -> str:
    event_kind = str(value or "").strip().lower()
    if event_kind not in ALLOWED_EVENT_KINDS:
        raise ValueError(
            "event_kind must be one of: close_trade, manual_adjustment, open_trade"
        )
    return event_kind


def _normalize_verification_status(value: Any) -> str:
    status = str(value or "unverified").strip().lower()
    if status not in ALLOWED_VERIFICATION_STATUS:
        raise ValueError("verification_status must be confirmed, unverified, or disputed")
    return status


def _normalize_contracts(value: Any, *, field_name: str = "contracts") -> int:
    try:
        contracts = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if contracts < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return contracts


def _normalize_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _normalize_expiration_ymd(value: Any) -> str:
    if isinstance(value, (int, float)):
        converted = exp_ms_to_ymd(int(value))
        if converted:
            value = converted
    text = _require_text(value, "expiration_ymd")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("expiration_ymd must be YYYY-MM-DD") from exc
    return text


def _normalize_snapshot_lot_identity(raw: dict[str, Any]) -> dict[str, Any]:
    broker = normalize_broker(raw.get("broker"))
    account = normalize_account(raw.get("account"))
    symbol = canonical_contract_symbol(raw.get("symbol"))
    option_type = normalize_option_type(raw.get("option_type"), strict=True)
    side = normalize_side(raw.get("side"), strict=True)
    expiration_ymd = _normalize_expiration_ymd(raw.get("expiration_ymd"))
    strike = _normalize_float(raw.get("strike"), "strike")
    currency = normalize_currency(raw.get("currency"), strict=True)
    raw_multiplier = raw.get("multiplier")
    multiplier = _normalize_float(raw_multiplier, "multiplier") if raw_multiplier not in (None, "") else None
    if not broker:
        raise ValueError("broker is required")
    if not account:
        raise ValueError("account is required")
    if not symbol:
        raise ValueError("symbol is required")
    return {
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "expiration_ymd": expiration_ymd,
        "strike": strike,
        "currency": currency,
        "multiplier": multiplier,
        "position_key": build_position_key(
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            side=side,
            strike=strike,
            expiration_ymd=expiration_ymd,
        ),
    }


def build_position_key(
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    strike: float,
    expiration_ymd: str,
) -> str:
    symbol_norm = canonical_contract_symbol(symbol)
    strike_txt = format(float(strike), "g")
    expiration_compact = str(expiration_ymd or "").replace("-", "")
    return (
        f"{normalize_broker(broker)}|{normalize_account(account)}|{symbol_norm}|"
        f"{normalize_option_type(option_type, strict=True)}|{normalize_side(side, strict=True)}|"
        f"{strike_txt}|{expiration_compact}"
    )


def normalize_snapshot_lot(raw: dict[str, Any] | Any, *, snapshot_id: str) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    identity = _normalize_snapshot_lot_identity(src)
    contracts = _normalize_contracts(
        src.get("contracts", src.get("contracts_open", 0)),
        field_name="contracts",
    )
    snapshot_lot_id = str(src.get("snapshot_lot_id") or "").strip()
    if not snapshot_lot_id:
        digest = sha256(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "position_key": identity["position_key"],
                    "contracts": contracts,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        snapshot_lot_id = f"{snapshot_id}:{digest}"
    return {
        "snapshot_lot_id": snapshot_lot_id,
        "snapshot_id": snapshot_id,
        **identity,
        "contracts": contracts,
        "verification_status": _normalize_verification_status(src.get("verification_status")),
        "source_record_id": (str(src.get("source_record_id") or "").strip() or None),
        "evidence_ref": (str(src.get("evidence_ref") or "").strip() or None),
        "note": (str(src.get("note") or "").strip() or None),
    }


def normalize_position_snapshot(raw: dict[str, Any] | Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    snapshot_id = _require_text(src.get("snapshot_id"), "snapshot_id")
    snapshot_type = _normalize_snapshot_type(src.get("snapshot_type"))
    lots = src.get("lots")
    if not isinstance(lots, list):
        raise ValueError("lots must be a list")
    normalized_lots = [normalize_snapshot_lot(item, snapshot_id=snapshot_id) for item in lots]
    return {
        "schema_kind": SCHEMA_KIND_POSITION_SNAPSHOT,
        "schema_version": OPTION_POSITIONS_V2_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_type": snapshot_type,
        "snapshot_at_utc": str(src.get("snapshot_at_utc") or utc_now_iso()),
        "source_name": _require_text(src.get("source_name"), "source_name"),
        "source_type": (str(src.get("source_type") or "").strip() or None),
        "note": (str(src.get("note") or "").strip() or None),
        "lots": normalized_lots,
        "lot_count": len(normalized_lots),
    }


def normalize_position_event(raw: dict[str, Any] | Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    event_kind = _normalize_event_kind(src.get("event_kind"))
    identity = _normalize_snapshot_lot_identity(src)
    out = {
        "schema_kind": SCHEMA_KIND_POSITION_EVENT,
        "schema_version": OPTION_POSITIONS_V2_SCHEMA_VERSION,
        "event_id": _require_text(src.get("event_id"), "event_id"),
        "event_kind": event_kind,
        "event_at_utc": str(src.get("event_at_utc") or utc_now_iso()),
        "source_name": _require_text(src.get("source_name"), "source_name"),
        "source_type": (str(src.get("source_type") or "").strip() or None),
        "snapshot_lot_id": (str(src.get("snapshot_lot_id") or "").strip() or None),
        "evidence_ref": (str(src.get("evidence_ref") or "").strip() or None),
        "note": (str(src.get("note") or "").strip() or None),
        **identity,
    }
    if event_kind == EVENT_KIND_MANUAL_ADJUSTMENT:
        out["target_contracts"] = _normalize_contracts(
            src.get("target_contracts"), field_name="target_contracts"
        )
        out["contracts"] = None
    else:
        out["contracts"] = _normalize_contracts(src.get("contracts"), field_name="contracts")
        out["target_contracts"] = None
    return out


def _position_state_from_identity(
    identity: dict[str, Any],
    *,
    baseline_snapshot_id: str | None,
    baseline_snapshot_lot_id: str | None,
    baseline_contracts: int,
) -> dict[str, Any]:
    return {
        "position_key": identity["position_key"],
        "broker": identity["broker"],
        "account": identity["account"],
        "symbol": identity["symbol"],
        "option_type": identity["option_type"],
        "side": identity["side"],
        "expiration_ymd": identity["expiration_ymd"],
        "strike": identity["strike"],
        "currency": identity["currency"],
        "multiplier": identity["multiplier"],
        "baseline_snapshot_id": baseline_snapshot_id,
        "baseline_snapshot_lot_id": baseline_snapshot_lot_id,
        "baseline_contracts": baseline_contracts,
        "current_contracts": baseline_contracts,
        "status": ("open" if baseline_contracts > 0 else "closed"),
        "applied_events": [],
    }


def project_current_positions(
    baseline_snapshot: dict[str, Any] | Any,
    events: list[dict[str, Any] | Any] | None = None,
) -> dict[str, Any]:
    snapshot = normalize_position_snapshot(baseline_snapshot)
    if snapshot["snapshot_type"] != SNAPSHOT_TYPE_BASELINE:
        raise ValueError("baseline snapshot is required for projection")
    normalized_events = [normalize_position_event(item) for item in (events or [])]
    normalized_events.sort(key=lambda item: (str(item["event_at_utc"]), str(item["event_id"])))

    positions: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    for lot in snapshot["lots"]:
        positions[lot["position_key"]] = _position_state_from_identity(
            lot,
            baseline_snapshot_id=snapshot["snapshot_id"],
            baseline_snapshot_lot_id=lot["snapshot_lot_id"],
            baseline_contracts=int(lot["contracts"]),
        )

    for event in normalized_events:
        key = event["position_key"]
        state = positions.get(key)
        if state is None:
            state = _position_state_from_identity(
                event,
                baseline_snapshot_id=None,
                baseline_snapshot_lot_id=event.get("snapshot_lot_id"),
                baseline_contracts=0,
            )
            positions[key] = state

        if event["event_kind"] == EVENT_KIND_OPEN_TRADE:
            state["current_contracts"] += int(event["contracts"] or 0)
        elif event["event_kind"] == EVENT_KIND_CLOSE_TRADE:
            contracts = int(event["contracts"] or 0)
            if state["current_contracts"] <= 0:
                diagnostics.append(
                    {
                        "code": "close_without_open_position",
                        "severity": "error",
                        "event_id": event["event_id"],
                        "position_key": key,
                        "message": "close event does not have an open position to consume",
                    }
                )
                continue
            if contracts > state["current_contracts"]:
                diagnostics.append(
                    {
                        "code": "close_exceeds_current_contracts",
                        "severity": "error",
                        "event_id": event["event_id"],
                        "position_key": key,
                        "message": "close event exceeds current contracts",
                        "details": {
                            "current_contracts": state["current_contracts"],
                            "close_contracts": contracts,
                        },
                    }
                )
            state["current_contracts"] = max(0, state["current_contracts"] - contracts)
        else:
            state["current_contracts"] = int(event["target_contracts"] or 0)

        state["status"] = "open" if state["current_contracts"] > 0 else "closed"
        state["applied_events"].append(
            {
                "event_id": event["event_id"],
                "event_kind": event["event_kind"],
                "event_at_utc": event["event_at_utc"],
                "contracts": event.get("contracts"),
                "target_contracts": event.get("target_contracts"),
                "source_name": event["source_name"],
            }
        )

    sorted_positions = sorted(
        positions.values(),
        key=lambda item: (
            str(item["account"]),
            str(item["symbol"]),
            str(item["expiration_ymd"]),
            float(item["strike"]),
            str(item["side"]),
        ),
    )
    return {
        "schema_kind": SCHEMA_KIND_POSITION_PROJECTION,
        "schema_version": OPTION_POSITIONS_V2_SCHEMA_VERSION,
        "baseline_snapshot_id": snapshot["snapshot_id"],
        "projected_at_utc": utc_now_iso(),
        "positions": sorted_positions,
        "open_position_count": sum(1 for item in sorted_positions if int(item["current_contracts"]) > 0),
        "processed_event_count": len(normalized_events),
        "diagnostics": diagnostics,
    }


def reconcile_snapshot_against_projection(
    verification_snapshot: dict[str, Any] | Any,
    projection: dict[str, Any] | Any,
) -> dict[str, Any]:
    snapshot = normalize_position_snapshot(verification_snapshot)
    if snapshot["snapshot_type"] != SNAPSHOT_TYPE_VERIFICATION:
        raise ValueError("verification snapshot is required for reconciliation")
    proj = projection if isinstance(projection, dict) else {}
    positions = proj.get("positions")
    if not isinstance(positions, list):
        raise ValueError("projection must contain positions list")

    open_positions = {
        str(item.get("position_key") or ""): item
        for item in positions
        if int(item.get("current_contracts") or 0) > 0
    }
    snapshot_lots = {item["position_key"]: item for item in snapshot["lots"]}

    items: list[dict[str, Any]] = []
    for key, lot in snapshot_lots.items():
        current = open_positions.get(key)
        if current is None:
            items.append(
                {
                    "status": "missing_in_projection",
                    "position_key": key,
                    "snapshot_contracts": lot["contracts"],
                    "projected_contracts": 0,
                    "snapshot_lot_id": lot["snapshot_lot_id"],
                }
            )
            continue

        mismatched_fields = []
        for field_name in ("currency", "multiplier"):
            if current.get(field_name) != lot.get(field_name):
                mismatched_fields.append(field_name)
        if mismatched_fields:
            items.append(
                {
                    "status": "field_mismatch",
                    "position_key": key,
                    "snapshot_contracts": lot["contracts"],
                    "projected_contracts": int(current.get("current_contracts") or 0),
                    "mismatched_fields": mismatched_fields,
                }
            )
            continue
        if int(current.get("current_contracts") or 0) != int(lot["contracts"]):
            items.append(
                {
                    "status": "quantity_mismatch",
                    "position_key": key,
                    "snapshot_contracts": lot["contracts"],
                    "projected_contracts": int(current.get("current_contracts") or 0),
                    "applied_event_ids": [
                        str(item.get("event_id") or "")
                        for item in (current.get("applied_events") or [])
                    ],
                }
            )
            continue
        items.append(
            {
                "status": "matched",
                "position_key": key,
                "snapshot_contracts": lot["contracts"],
                "projected_contracts": int(current.get("current_contracts") or 0),
            }
        )

    for key, current in open_positions.items():
        if key in snapshot_lots:
            continue
        items.append(
            {
                "status": "missing_in_snapshot",
                "position_key": key,
                "snapshot_contracts": 0,
                "projected_contracts": int(current.get("current_contracts") or 0),
                "applied_event_ids": [
                    str(item.get("event_id") or "")
                    for item in (current.get("applied_events") or [])
                ],
            }
        )

    items.sort(key=lambda item: (str(item["status"]), str(item["position_key"])))
    summary: dict[str, int] = {}
    for item in items:
        summary[item["status"]] = int(summary.get(item["status"], 0)) + 1
    return {
        "schema_kind": SCHEMA_KIND_POSITION_RECONCILIATION,
        "schema_version": OPTION_POSITIONS_V2_SCHEMA_VERSION,
        "report_id": f"{snapshot['snapshot_id']}@{str(proj.get('baseline_snapshot_id') or 'unknown')}",
        "snapshot_id": snapshot["snapshot_id"],
        "baseline_snapshot_id": proj.get("baseline_snapshot_id"),
        "generated_at_utc": utc_now_iso(),
        "summary": summary,
        "items": items,
    }


def build_baseline_snapshot_from_legacy_records(
    records: list[dict[str, Any]] | Any,
    *,
    snapshot_id: str,
    snapshot_at_utc: str,
    source_name: str = "legacy_position_lots",
) -> dict[str, Any]:
    rows = records if isinstance(records, list) else []
    lots: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in rows:
        row = item if isinstance(item, dict) else {}
        fields = row.get("fields") or {}
        if not isinstance(fields, dict):
            skipped.append({"record_id": row.get("record_id"), "reason": "missing_fields"})
            continue
        try:
            lots.append(
                normalize_snapshot_lot(
                    {
                        "snapshot_lot_id": (str(row.get("record_id") or "").strip() or None),
                        "source_record_id": (str(row.get("record_id") or "").strip() or None),
                        "account": fields.get("account"),
                        "broker": fields.get("broker") or fields.get("market"),
                        "symbol": fields.get("symbol"),
                        "option_type": fields.get("option_type"),
                        "side": fields.get("side"),
                        "strike": fields.get("strike"),
                        "expiration_ymd": (
                            fields.get("expiration_ymd")
                            or fields.get("exp")
                            or exp_ms_to_ymd(fields.get("expiration"))
                            or fields.get("expiration")
                        ),
                        "currency": fields.get("currency"),
                        "multiplier": fields.get("multiplier"),
                        "contracts": fields.get("contracts_open", fields.get("contracts")),
                        "verification_status": "unverified",
                        "evidence_ref": f"legacy_record_id:{row.get('record_id')}",
                    },
                    snapshot_id=snapshot_id,
                )
            )
        except Exception:
            skipped.append({"record_id": row.get("record_id"), "reason": "invalid_contract_fields"})
    snapshot = normalize_position_snapshot(
        {
            "snapshot_id": snapshot_id,
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": snapshot_at_utc,
            "source_name": source_name,
            "source_type": "legacy_import",
            "lots": lots,
        }
    )
    snapshot["skipped_records"] = skipped
    return snapshot


def adapt_legacy_trade_events(events: list[dict[str, Any]] | Any) -> dict[str, Any]:
    rows = events if isinstance(events, list) else []
    normalized_events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in rows:
        event = item if isinstance(item, dict) else {}
        source_type = str(event.get("source_type") or "").strip().lower()
        if source_type == "bootstrap_snapshot":
            skipped.append(
                {
                    "event_id": event.get("event_id"),
                    "reason": "bootstrap_snapshot_is_not_a_post_baseline_event",
                }
            )
            continue

        position_effect = str(event.get("position_effect") or "").strip().lower()
        raw_payload = event.get("raw_payload") or {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        try:
            if position_effect == "open":
                normalized_events.append(
                    normalize_position_event(
                        {
                            "event_id": event.get("event_id"),
                            "event_kind": EVENT_KIND_OPEN_TRADE,
                            "event_at_utc": datetime.fromtimestamp(
                                int(event.get("trade_time_ms") or 0) / 1000,
                                tz=timezone.utc,
                            ).isoformat(),
                            "source_name": event.get("source_name") or "legacy_trade_events",
                            "source_type": event.get("source_type"),
                            "account": event.get("account"),
                            "broker": event.get("broker"),
                            "symbol": event.get("symbol"),
                            "option_type": event.get("option_type"),
                            "side": "short" if str(event.get("side") or "").strip().lower() == "sell" else "long",
                            "strike": event.get("strike"),
                            "expiration_ymd": event.get("expiration_ymd"),
                            "currency": event.get("currency"),
                            "multiplier": event.get("multiplier"),
                            "contracts": event.get("contracts"),
                            "snapshot_lot_id": raw_payload.get("record_id"),
                        }
                    )
                )
            elif position_effect == "close":
                normalized_events.append(
                    normalize_position_event(
                        {
                            "event_id": event.get("event_id"),
                            "event_kind": EVENT_KIND_CLOSE_TRADE,
                            "event_at_utc": datetime.fromtimestamp(
                                int(event.get("trade_time_ms") or 0) / 1000,
                                tz=timezone.utc,
                            ).isoformat(),
                            "source_name": event.get("source_name") or "legacy_trade_events",
                            "source_type": event.get("source_type"),
                            "account": event.get("account"),
                            "broker": event.get("broker"),
                            "symbol": event.get("symbol"),
                            "option_type": event.get("option_type"),
                            "side": "short" if str(event.get("side") or "").strip().lower() == "buy" else "long",
                            "strike": event.get("strike"),
                            "expiration_ymd": event.get("expiration_ymd"),
                            "currency": event.get("currency"),
                            "multiplier": event.get("multiplier"),
                            "contracts": event.get("contracts"),
                            "snapshot_lot_id": raw_payload.get("record_id"),
                        }
                    )
                )
            elif position_effect == "adjust":
                patch = raw_payload.get("patch") or {}
                target_contracts = patch.get("contracts_open", patch.get("contracts"))
                normalized_events.append(
                    normalize_position_event(
                        {
                            "event_id": event.get("event_id"),
                            "event_kind": EVENT_KIND_MANUAL_ADJUSTMENT,
                            "event_at_utc": datetime.fromtimestamp(
                                int(event.get("trade_time_ms") or 0) / 1000,
                                tz=timezone.utc,
                            ).isoformat(),
                            "source_name": event.get("source_name") or "legacy_trade_events",
                            "source_type": event.get("source_type"),
                            "account": event.get("account"),
                            "broker": event.get("broker"),
                            "symbol": event.get("symbol"),
                            "option_type": event.get("option_type"),
                            "side": raw_payload.get("position_side") or "short",
                            "strike": event.get("strike"),
                            "expiration_ymd": event.get("expiration_ymd"),
                            "currency": event.get("currency"),
                            "multiplier": event.get("multiplier"),
                            "target_contracts": target_contracts,
                            "snapshot_lot_id": raw_payload.get("record_id"),
                        }
                    )
                )
            else:
                skipped.append(
                    {
                        "event_id": event.get("event_id"),
                        "reason": f"unsupported_position_effect:{position_effect or 'missing'}",
                    }
                )
        except Exception as exc:
            skipped.append(
                {
                    "event_id": event.get("event_id"),
                    "reason": f"invalid_event:{exc}",
                }
            )
    return {"events": normalized_events, "skipped": skipped}
