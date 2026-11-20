from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any


DECISION_STATE_SNAPSHOT_SCHEMA = "decision_state_snapshot.v2"
DECISION_STATE_FINGERPRINT_SCHEMA = "decision_state_fingerprint.v2"
CURRENT_DECISION_READ_SCHEMA = "current_decision_projection_read.v1"

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


@lru_cache(maxsize=512)
def _excluded_key(key: str) -> bool:
    return key in DECISION_FINGERPRINT_EXCLUDED_FIELDS or key.startswith("display_")


@lru_cache(maxsize=512)
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
    _numbers: dict | None = None,
) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if not _is_decimal_field(_field_name, _parent_field):
            return value
        cache_text = _numbers is not None and type(value) is str
        if cache_text:
            normalized = _numbers.get(value)
            if normalized is not None:
                return normalized
        try:
            normalized = _decimal_string(value, field_path=str(_field_name or ""))
        except DecisionStateNormalizationError:
            normalized = value
        if cache_text:
            _numbers[value] = normalized
        return normalized
    kind = type(value)
    if _numbers is not None and (kind is int or kind is float):
        # Repeated quotes and quantities share a spelling within this traversal.
        key = (kind, value)
        normalized = _numbers.get(key)
        if normalized is None:
            normalized = _decimal_string(value, field_path=_field_name or "")
            _numbers[key] = normalized
        return normalized
    if isinstance(value, (int, float, Decimal)):
        return _decimal_string(value, field_path=str(_field_name or ""))
    if kind is dict and all(type(key) is str for key in value):
        return {
            key: canonicalize_decision_value(
                value[key],
                _field_name=key,
                _parent_field=_field_name,
                _numbers=_numbers,
            )
            for key in sorted(value)
            if not _excluded_key(key)
        }
    if isinstance(value, dict):
        return {
            str(key): canonicalize_decision_value(
                item,
                _field_name=str(key),
                _parent_field=_field_name,
                _numbers=_numbers,
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
                _numbers=_numbers,
            )
            for item in value
        ]
        if items and isinstance(items[0], dict):
            groups: dict[str, list[Any]] = {}
            for item in items:
                row_id = _set_like_row_id(item) if isinstance(item, dict) else ""
                if not row_id:
                    break
                groups.setdefault(row_id, []).append(item)
            else:
                # The full row breaks ties only among rows with the same identity.
                ordered = []
                for row_id in sorted(groups):
                    group = groups[row_id]
                    if len(group) > 1:
                        group.sort(key=_canonical_sort_key)
                    ordered.extend(group)
                return ordered
        if isinstance(value, (set, frozenset)):
            return sorted(items, key=_canonical_sort_key)
        return items
    raise DecisionStateNormalizationError(f"unsupported decision state value: {type(value).__name__}")


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _set_like_row_id(value: dict[str, Any]) -> str:
    for field in _SET_LIKE_ID_FIELDS:
        item = value.get(field)
        if item not in (None, ""):
            return f"{field}:{item}"
    return ""


def _encode_normalized(normalized: Any) -> bytes:
    # Normalization already orders every mapping by its string keys.
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    return _encode_normalized(canonicalize_decision_value(value, _numbers={}))


def opening_decision_hashes(opening: dict[str, Any]) -> tuple[str, str]:
    """Fingerprint a decision and its input dictionary using one normalized tree."""
    # A non-string key may stringify to "normalized_input" in the outer digest.
    if any(type(key) is not str for key in opening):
        return canonical_sha256(opening), canonical_sha256(opening["normalized_input"])
    normalized = canonicalize_decision_value(opening, _numbers={})
    return (
        hashlib.sha256(_encode_normalized(normalized)).hexdigest(),
        hashlib.sha256(_encode_normalized(normalized["normalized_input"])).hexdigest(),
    )


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
    "opening_decision_hashes",
    "canonical_sha256",
    "canonicalize_decision_value",
]
