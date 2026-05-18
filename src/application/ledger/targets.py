from __future__ import annotations

from typing import Any

from domain.domain.ledger.position_fields import (
    effective_expiration_ymd,
    effective_strike,
    normalize_account,
    normalize_broker,
    normalize_currency,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def assert_position_lot_target_matches_current_state(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    operation: str,
) -> dict[str, Any]:
    get_record_fields = getattr(repo, "get_record_fields", None)
    if not callable(get_record_fields):
        raise TypeError("option_positions repo does not expose get_record_fields")
    raw_current_fields = get_record_fields(str(record_id))
    if not isinstance(raw_current_fields, dict):
        raise TypeError(f"option_positions repo returned non-dict fields for record_id={record_id}")
    current_fields: dict[str, Any] = raw_current_fields
    comparisons = (
        ("broker", normalize_broker(current_fields.get("broker")), normalize_broker(fields.get("broker"))),
        ("account", normalize_account(current_fields.get("account")), normalize_account(fields.get("account"))),
        ("symbol", _canonical_trade_symbol(current_fields.get("symbol")), _canonical_trade_symbol(fields.get("symbol"))),
        ("option_type", str(current_fields.get("option_type") or "").strip().lower(), str(fields.get("option_type") or "").strip().lower()),
        ("side", str(current_fields.get("side") or "").strip().lower(), str(fields.get("side") or "").strip().lower()),
        ("currency", normalize_currency(current_fields.get("currency")), normalize_currency(fields.get("currency"))),
        ("strike", effective_strike(current_fields), effective_strike(fields)),
        ("expiration_ymd", effective_expiration_ymd(current_fields), effective_expiration_ymd(fields)),
        (
            "source_event_id",
            str(current_fields.get("source_event_id") or "").strip(),
            str(fields.get("source_event_id") or "").strip(),
        ),
    )
    mismatches = [name for name, left, right in comparisons if left != right]
    if mismatches:
        joined = ", ".join(mismatches)
        raise ValueError(f"{operation} target fields do not match current lot state: {record_id} ({joined})")
    return current_fields
