from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any


DECISION_STATE_SNAPSHOT_SCHEMA = "decision_state_snapshot.v2"
DECISION_STATE_FINGERPRINT_SCHEMA = "decision_state_fingerprint.v2"

# These fields are operational metadata. Unknown fields are deliberately retained.
DECISION_FINGERPRINT_EXCLUDED_FIELDS = frozenset(
    {
        "last_action_at",
        "created_at",
        "created_at_ms",
        "updated_at",
        "updated_at_ms",
    }
)

_SET_LIKE_ID_FIELDS = (
    "event_id",
    "record_id",
    "case_id",
    "evidence_id",
    "allocation_id",
    "stock_event_id",
    "group_id",
    "strategy_group_id",
)

_DECIMAL_FIELD_NAMES = frozenset(
    {
        "amount",
        "cash_secured",
        "contracts",
        "contracts_allocated",
        "contracts_closed",
        "contracts_open",
        "cost_basis",
        "coverage_basis",
        "multiplier",
        "original_contracts",
        "premium",
        "quantity",
        "shares",
        "shares_locked",
        "strike",
    }
)


class DecisionStateNormalizationError(ValueError):
    pass


def _decimal_string(value: Any, *, field_path: str = "") -> str:
    if isinstance(value, float) and not math.isfinite(value):
        raise DecisionStateNormalizationError(
            "non-finite numeric value in decision state"
            + (f" at {field_path}" if field_path else "")
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DecisionStateNormalizationError(
            "invalid numeric value in decision state"
            + (f" at {field_path}" if field_path else "")
        ) from exc
    if not number.is_finite():
        raise DecisionStateNormalizationError(
            "non-finite numeric value in decision state"
            + (f" at {field_path}" if field_path else "")
        )
    if number == 0:
        return "0"
    normalized = number.normalize()
    rendered = format(normalized, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _excluded_key(key: str) -> bool:
    return key in DECISION_FINGERPRINT_EXCLUDED_FIELDS or key.startswith("display_")


def _is_decimal_field(field_name: str | None, parent_field: str | None) -> bool:
    if parent_field and parent_field.endswith("_by_lot"):
        return True
    if not field_name:
        return False
    return (
        field_name in _DECIMAL_FIELD_NAMES
        or field_name.endswith(("_amount", "_contracts", "_quantity", "_shares"))
        or field_name.endswith(("_premium", "_basis", "_secured", "_locked"))
    )


def canonicalize_decision_value(
    value: Any,
    *,
    _field_name: str | None = None,
    _parent_field: str | None = None,
) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if _is_decimal_field(_field_name, _parent_field):
            try:
                return _decimal_string(value, field_path=str(_field_name or ""))
            except DecisionStateNormalizationError:
                pass
        return value
    if isinstance(value, (int, float, Decimal)):
        return _decimal_string(value, field_path=str(_field_name or ""))
    if isinstance(value, dict):
        return {
            str(key): canonicalize_decision_value(
                item,
                _field_name=str(key),
                _parent_field=_field_name,
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _excluded_key(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [
            canonicalize_decision_value(
                item,
                _field_name=_field_name,
                _parent_field=_parent_field,
            )
            for item in value
        ]
        if items and all(isinstance(item, dict) and _set_like_row_id(item) for item in items):
            return sorted(items, key=_set_like_row_sort_key)
        if isinstance(value, (set, frozenset)):
            return sorted(items, key=_canonical_sort_key)
        return items
    raise DecisionStateNormalizationError(f"unsupported decision state value: {type(value).__name__}")


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _set_like_row_sort_key(value: dict[str, Any]) -> tuple[str, str]:
    row_id = _set_like_row_id(value)
    return (row_id, _canonical_sort_key(value))


def _set_like_row_id(value: dict[str, Any]) -> str:
    for field in _SET_LIKE_ID_FIELDS:
        item = value.get(field)
        if item not in (None, ""):
            return f"{field}:{item}"
    return ""


def canonical_json_bytes(value: Any) -> bytes:
    normalized = canonicalize_decision_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_decision_state_fingerprint(snapshot: dict[str, Any]) -> str:
    payload = dict(snapshot or {})
    payload["schema_version"] = DECISION_STATE_SNAPSHOT_SCHEMA
    return canonical_sha256(payload)


__all__ = [
    "DECISION_FINGERPRINT_EXCLUDED_FIELDS",
    "DECISION_STATE_FINGERPRINT_SCHEMA",
    "DECISION_STATE_SNAPSHOT_SCHEMA",
    "DecisionStateNormalizationError",
    "build_decision_state_fingerprint",
    "canonical_json_bytes",
    "canonical_sha256",
    "canonicalize_decision_value",
]
