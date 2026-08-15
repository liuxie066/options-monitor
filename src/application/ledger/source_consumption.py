from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.symbol_identity import canonical_symbol
from src.application.payload_helpers import optional_text as _optional_text


SOURCE_CONSUMPTION_SCHEMA = "trade_lifecycle_source_consumption.v1"
SOURCE_PAYLOAD_SCHEMA = "broker_source_economic_payload.v1"
SOURCE_ROLES = frozenset({"option_anchor", "stock_settlement"})


def canonical_source_economic_payload(
    *,
    source_key: str,
    source_role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the allowlisted broker-economic facts used for source ownership."""

    key = _canonical_source_key(source_key)
    role = str(source_role or "").strip().lower()
    if role not in SOURCE_ROLES:
        raise ValueError("lifecycle source role is invalid")
    source = dict(payload or {})
    contract = (
        dict(source.get("contract_key") or {})
        if isinstance(source.get("contract_key"), dict)
        else {}
    )
    key_account, key_futu_account_id = _source_key_binding(key)
    account = str(
        source.get("account")
        or source.get("internal_account")
        or contract.get("account")
        or ""
    ).strip().lower()
    futu_account_id = str(source.get("futu_account_id") or "").strip()
    if account != key_account or futu_account_id != key_futu_account_id:
        raise ValueError("lifecycle source account binding mismatch")

    symbol_raw = (
        source.get("symbol")
        or source.get("underlying_symbol")
        or contract.get("underlying_symbol")
    )
    symbol = canonical_symbol(symbol_raw) or str(symbol_raw or "").strip().upper()
    side = str(
        source.get("side")
        or source.get("trade_side")
        or source.get("trd_side")
        or ""
    ).strip().lower()
    quantity = _first_value(
        source,
        "contracts",
        "shares",
        "quantity",
        "qty",
        "stock_qty",
    )
    execution_time = _first_value(
        source,
        "execution_time_ms",
        "event_time_ms",
        "trade_time_ms",
    )
    canonical = {
        "schema_version": SOURCE_PAYLOAD_SCHEMA,
        "source_key": key,
        "source_role": role,
        "account": account,
        "futu_account_id": futu_account_id,
        "contract_key": _optional_text(
            source.get("contract_key")
            if isinstance(source.get("contract_key"), str)
            else source.get("contract_key_string")
        ),
        "symbol": symbol or None,
        "option_type": _optional_lower(
            source.get("option_type") or contract.get("option_type")
        ),
        "position_side": _optional_lower(
            source.get("position_side") or contract.get("position_side")
        ),
        "strike": _optional_decimal(
            source.get("strike")
            if source.get("strike") is not None
            else contract.get("strike")
        ),
        "expiration_ymd": _optional_text(
            source.get("expiration_ymd") or contract.get("expiration_ymd")
        ),
        "multiplier": _optional_decimal(source.get("multiplier")),
        "side": side or None,
        "quantity": _optional_decimal(quantity),
        "price": _optional_decimal(
            source.get("price")
            if source.get("price") is not None
            else source.get("stock_price")
        ),
        "execution_time_ms": _optional_integer(execution_time),
        "order_id": _optional_text(source.get("order_id")),
        "clearing_date": _optional_text(
            source.get("clearing_date") or source.get("settlement_date")
        ),
    }
    return canonical


def canonical_source_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_source_consumption_claim(
    *,
    source_key: str,
    case_id: str,
    owner_evidence_id: str,
    source_role: str,
    economic_payload: dict[str, Any],
) -> dict[str, Any]:
    key = _canonical_source_key(source_key)
    case_value = str(case_id or "").strip()
    evidence_value = str(owner_evidence_id or "").strip()
    if not case_value or not evidence_value:
        raise ValueError("lifecycle source owner identity is incomplete")
    canonical = canonical_source_economic_payload(
        source_key=key,
        source_role=source_role,
        payload=economic_payload,
    )
    return {
        "schema_version": SOURCE_CONSUMPTION_SCHEMA,
        "source_key": key,
        "case_id": case_value,
        "owner_evidence_id": evidence_value,
        "source_role": str(source_role).strip().lower(),
        "source_payload_hash": canonical_source_payload_hash(canonical),
        "source_payload": canonical,
    }


def _canonical_source_key(value: Any) -> str:
    key = str(value or "").strip()
    parts = key.split(":", 3)
    if (
        len(parts) != 4
        or parts[0].lower() != "futu"
        or not parts[1]
        or not parts[2]
        or not parts[3]
    ):
        raise ValueError("canonical broker source key is required")
    return f"futu:{parts[1].lower()}:{parts[2]}:{parts[3]}"


def _source_key_binding(value: str) -> tuple[str, str]:
    _broker, account, futu_account_id, _deal_id = value.split(":", 3)
    return account, futu_account_id


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _optional_lower(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _optional_decimal(value: Any) -> str | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("broker source decimal field is invalid") from exc
    if not number.is_finite():
        raise ValueError("broker source decimal field is invalid")
    normalized = number.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _optional_integer(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("broker source timestamp is invalid") from exc


__all__ = [
    "SOURCE_CONSUMPTION_SCHEMA",
    "SOURCE_PAYLOAD_SCHEMA",
    "SOURCE_ROLES",
    "build_source_consumption_claim",
    "canonical_source_economic_payload",
    "canonical_source_payload_hash",
]
