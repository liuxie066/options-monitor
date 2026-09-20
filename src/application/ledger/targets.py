from __future__ import annotations

from typing import Any

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
    normalize_broker,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import canonical_contract_symbol


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _contract_key(fields: dict[str, Any]) -> dict[str, Any]:
    contract_key = fields.get("contract_key")
    return contract_key if isinstance(contract_key, dict) else {}


def _contract_value(fields: dict[str, Any], nested_key: str, flat_key: str | None = None) -> Any:
    value = _contract_key(fields).get(nested_key)
    if value not in (None, ""):
        return value
    return fields.get(flat_key or nested_key)


def assert_position_lot_target_matches_current_state(
    repo: Any,
    *,
    lot_id: str,
    fields: dict[str, Any],
    operation: str,
    current_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if current_fields is None:
        get_record_fields = getattr(repo, "get_record_fields", None)
        if not callable(get_record_fields):
            raise TypeError("option_positions repo does not expose get_record_fields")
        raw_current_fields = get_record_fields(str(lot_id))
        if not isinstance(raw_current_fields, dict):
            raise TypeError(f"option_positions repo returned non-dict fields for record_id={lot_id}")
        current_fields = raw_current_fields
    comparisons = (
        ("broker", normalize_broker(_contract_value(current_fields, "broker")), normalize_broker(_contract_value(fields, "broker"))),
        ("account", normalize_account(_contract_value(current_fields, "account")), normalize_account(_contract_value(fields, "account"))),
        ("symbol", _canonical_trade_symbol(_contract_value(current_fields, "underlying_symbol", "symbol")), _canonical_trade_symbol(_contract_value(fields, "underlying_symbol", "symbol"))),
        ("option_type", str(_contract_value(current_fields, "option_type") or "").strip().lower(), str(_contract_value(fields, "option_type") or "").strip().lower()),
        ("side", str(current_fields.get("position_side") or current_fields.get("side") or "").strip().lower(), str(fields.get("position_side") or fields.get("side") or "").strip().lower()),
        ("currency", normalize_currency(current_fields.get("currency")), normalize_currency(fields.get("currency"))),
        ("strike", effective_strike(current_fields), effective_strike(fields)),
        ("expiration_ymd", effective_expiration_ymd(current_fields), effective_expiration_ymd(fields)),
        ("multiplier", effective_multiplier(current_fields), effective_multiplier(fields)),
        (
            "open_event_id",
            str(current_fields.get("open_event_id") or current_fields.get("source_event_id") or "").strip(),
            str(fields.get("open_event_id") or fields.get("source_event_id") or "").strip(),
        ),
        (
            "status",
            str(current_fields.get("status") or "").strip().lower(),
            str(fields.get("status") or "").strip().lower(),
        ),
        ("contracts_open", effective_contracts_open(current_fields), effective_contracts_open(fields)),
        (
            # What this pair can see on today's callers is narrower than its name:
            # ``commands`` hands over the row it just read (``fields=current_fields``),
            # and the batch path re-reads the row in-transaction and CASes the whole
            # dict first — so on those paths it compares the row with itself, and on
            # a converged row both sides read "". It stays because the function's
            # contract is target-vs-current and the drifted-group rejection is pinned
            # by ``tests/test_option_positions_legacy_retirement``; what would newly
            # reject is a target carrying the read model's event-attached family
            # against a converged raw row, which no caller passes today.
            "strategy_group_id",
            str(current_fields.get("strategy_group_id") or "").strip(),
            str(fields.get("strategy_group_id") or "").strip(),
        ),
    )
    mismatches = [name for name, left, right in comparisons if left != right]
    if mismatches:
        joined = ", ".join(mismatches)
        raise ValueError(f"{operation} target fields do not match current lot state: {lot_id} ({joined})")
    return current_fields
