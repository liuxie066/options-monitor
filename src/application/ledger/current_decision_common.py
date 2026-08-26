from __future__ import annotations

import hashlib

import json

import time

from dataclasses import dataclass

from decimal import Decimal, InvalidOperation

from pathlib import Path

from typing import Any, Callable, Iterable, Mapping, Sequence

from domain.domain.assigned_stock import (
    assigned_stock_allocation_row,
    assigned_stock_event_time_ms,
    assigned_stock_fee_fact,
    assigned_stock_position_lot_row,
    assigned_stock_trade_event_row,
    project_assigned_stock_lifecycle,
)

from domain.domain.combo_identity import (
    FUNDING_PUT_ROLES,
    PARTICIPATION_CALL_ROLES,
    classify_combo_structure,
    validate_combo_identity,
)

from domain.domain.decision_state_fingerprint import canonical_sha256

from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    terminal_event_id_for,
)

from domain.domain.option_lifecycle import derive_lifecycle_read_model

from domain.domain.symbol_identity import symbol_market

from src.application.ledger.lifecycle_overlay import (
    ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA,
    LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
    LIFECYCLE_GENERATION_TOKEN_SCHEMA,
    arbitrate_lifecycle_case_resolutions,
    resolve_lifecycle_account_rows,
)

from src.application.ledger import position_projection_migration as _position_migration

from src.application.ledger.projector_implementation import (
    ProjectorImplementationUnavailable,
    loaded_projector_implementation_fingerprint,
)

from src.application.ledger.repository import (
    POSITION_PROJECTION_SCHEMA,
    SQLiteOptionPositionsRepository,
    _ensure_current_decision_projection_schema,
    _normalized_lifecycle_case_targets,
    _projection_schema_cookie,
)

CURRENT_DECISION_PROJECTION_SCHEMA = "current_decision_projection.v1"

CURRENT_DECISION_READ_SCHEMA = "current_decision_projection_read.v1"

LIFECYCLE_CASE_DECISION_FACT_SCHEMA = "lifecycle_case_decision_fact.v1"

_LIFECYCLE_CASE_CURRENT_GENERATION_TOKEN_SCHEMA = (
    "lifecycle_case_current_generation_token.v1"
)

CURRENT_COMBO_SCHEMA = "current_combo_facts.v1"

CURRENT_COMBO_GROUP_FACT_SCHEMA = "current_combo_group_fact.v1"

CURRENT_ASSIGNED_STOCK_SCHEMA = "current_assigned_stock.v1"

CURRENT_LIFECYCLE_QUALITY_SCHEMA = "current_lifecycle_quality.v1"

CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA = (
    "current_decision_projection_migration_inventory.v1"
)

_GENERATION_FIELDS = (
    "generation",
    "case_generation",
    "evidence_generation",
    "allocation_generation",
    "source_consumption_generation",
    "timing_generation",
    "combo_identity_generation",
    "assigned_stock_generation",
)

_OPERATIONAL_STATUSES = frozenset(
    {
        "pending",
        "waiting_settlement_evidence",
        "needs_review",
        "partially_resolved",
        "conflict",
    }
)

_CASE_FACT_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "account",
        "market",
        "contract",
        "target_contracts_by_lot",
        "status",
        "decision",
        "resolution",
        "timing",
        "evidence",
        "generation",
        "fact_sha256",
    }
)

_ANCHOR_FACT_KEYS = frozenset(
    {
        "anchor_kind",
        "canonical_case_id",
        "bridge_evidence_id",
        "source_owner_case_id",
        "source_owner_evidence_id",
        "source_key",
        "source_payload_hash",
        "futu_account_id",
        "execution_time_ms",
        "received_at_ms",
        "quantity",
        "target_contracts_by_lot",
        "anchor_fact_id",
        "anchor_fact_hash",
    }
)

class CurrentDecisionProjectionError(ValueError):
    pass

@dataclass(frozen=True)
class CurrentDecisionAccountFence:
    account: str
    position_lots_generation: int
    decision_generations: tuple[int, ...]
    projection_present: bool
    clean_at_start: bool

@dataclass(frozen=True)
class CurrentDecisionProjectionFence:
    position_source_generation: int
    accounts: tuple[CurrentDecisionAccountFence, ...]

def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CurrentDecisionProjectionError(
            "current decision value is not canonical JSON"
        ) from exc

def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def _hash_without(value: Mapping[str, Any], field: str) -> str:
    return canonical_sha256(
        {key: item for key, item in value.items() if key != field}
    )

def _text(value: Any, *, field: str, lower: bool = False, upper: bool = False) -> str:
    if not isinstance(value, str):
        raise CurrentDecisionProjectionError(f"{field} must be text")
    result = value.strip()
    if not result:
        raise CurrentDecisionProjectionError(f"{field} is required")
    if lower and result != result.lower():
        raise CurrentDecisionProjectionError(f"{field} must be lowercase")
    if upper and result != result.upper():
        raise CurrentDecisionProjectionError(f"{field} must be uppercase")
    return result

def _optional_text(
    value: Any,
    *,
    field: str,
    lower: bool = False,
    upper: bool = False,
) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, lower=lower, upper=upper)

def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CurrentDecisionProjectionError(
            f"{field} must be an integer >= {minimum}"
        )
    return value

def _optional_integer(
    value: Any,
    *,
    field: str,
    minimum: int = 0,
) -> int | None:
    if value is None:
        return None
    return _integer(value, field=field, minimum=minimum)

def _sha256(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = _text(value, field=field, lower=True)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CurrentDecisionProjectionError(f"{field} must be lowercase sha256")
    return text

def _decimal_text(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise CurrentDecisionProjectionError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CurrentDecisionProjectionError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise CurrentDecisionProjectionError(f"{field} must be finite")
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

def _nonnegative_decimal_text(value: Any, *, field: str) -> str:
    rendered = _decimal_text(value, field=field)
    assert rendered is not None
    if Decimal(rendered) < 0:
        raise CurrentDecisionProjectionError(f"{field} must be nonnegative")
    return rendered

def _integer_map(
    value: Any,
    *,
    field: str,
    positive: bool = False,
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise CurrentDecisionProjectionError(f"{field} must be an object")
    out: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = _text(raw_key, field=f"{field} key")
        if key in out:
            raise CurrentDecisionProjectionError(f"duplicate {field} key")
        out[key] = _integer(
            raw_value,
            field=f"{field}.{key}",
            minimum=1 if positive else 0,
        )
    return dict(sorted(out.items()))

def _text_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise CurrentDecisionProjectionError(f"{field} must be a list")
    items = [_text(item, field=field) for item in value]
    if items != sorted(set(items)):
        raise CurrentDecisionProjectionError(f"{field} must be sorted and unique")
    return items

def _fact_hash(payload: Mapping[str, Any]) -> str:
    return _hash_without(payload, "fact_sha256")

def _lifecycle_case_current_generation_token(
    payload: Mapping[str, Any],
) -> str:
    generation = dict(payload.get("generation") or {})
    generation.pop("generation_token", None)
    case_fact = {
        key: item
        for key, item in payload.items()
        if key not in {"fact_sha256", "generation"}
    }
    case_fact["generation"] = generation
    return canonical_sha256(
        {
            "schema_version": _LIFECYCLE_CASE_CURRENT_GENERATION_TOKEN_SCHEMA,
            "case_fact": case_fact,
        }
    )

def _normalize_anchor_facts(
    value: Any,
    *,
    case_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CurrentDecisionProjectionError("resolution.anchor_facts must be a list")
    anchors: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _ANCHOR_FACT_KEYS:
            raise CurrentDecisionProjectionError("lifecycle anchor fact shape is invalid")
        item = dict(raw)
        if _text(item["canonical_case_id"], field="canonical_case_id") != case_id:
            raise CurrentDecisionProjectionError("lifecycle anchor case mismatch")
        _text(item["anchor_kind"], field="anchor_kind")
        _optional_text(item["bridge_evidence_id"], field="bridge_evidence_id")
        _text(item["source_owner_case_id"], field="source_owner_case_id")
        _text(item["source_owner_evidence_id"], field="source_owner_evidence_id")
        _text(item["source_key"], field="source_key")
        _sha256(item["source_payload_hash"], field="source_payload_hash")
        _text(item["futu_account_id"], field="futu_account_id")
        _integer(item["execution_time_ms"], field="execution_time_ms", minimum=1)
        _integer(item["received_at_ms"], field="received_at_ms", minimum=1)
        _integer(item["quantity"], field="quantity", minimum=1)
        _integer_map(
            item["target_contracts_by_lot"],
            field="anchor target_contracts_by_lot",
            positive=True,
        )
        _sha256(item["anchor_fact_id"], field="anchor_fact_id")
        if item["anchor_fact_hash"] != _hash_without(item, "anchor_fact_hash"):
            raise CurrentDecisionProjectionError("lifecycle anchor fact hash mismatch")
        anchors.append(item)
    if [item["anchor_fact_id"] for item in anchors] != sorted(
        {item["anchor_fact_id"] for item in anchors}
    ):
        raise CurrentDecisionProjectionError("lifecycle anchor facts are not canonical")
    return anchors

def _position_lot_fields(
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in current_position_lots:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("current position lot must be an object")
        record_id = str(raw.get("record_id") or raw.get("lot_id") or "").strip()
        fields = raw.get("fields")
        if not record_id or not isinstance(fields, Mapping) or record_id in out:
            raise CurrentDecisionProjectionError("current position lot identity is invalid")
        out[record_id] = dict(fields)
    return out
