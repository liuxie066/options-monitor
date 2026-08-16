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


def build_lifecycle_case_decision_fact(
    *,
    lifecycle_case: Mapping[str, Any],
    case_resolution: Mapping[str, Any],
    generation_token: Mapping[str, Any],
    read_model: Mapping[str, Any],
    evidence_revision: int,
    evidence_count: int,
    admission_head: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = dict(lifecycle_case)
    resolution = dict(case_resolution)
    token = dict(generation_token)
    model = dict(read_model)
    summary = (
        dict(case.get("derived_summary") or {})
        if isinstance(case.get("derived_summary"), Mapping)
        else {}
    )
    case_id = str(case.get("case_id") or "").strip()
    account = str(case.get("account") or "").strip().lower()
    if not case_id or not account:
        raise CurrentDecisionProjectionError("lifecycle case id and account are required")
    if str(resolution.get("case_id") or "").strip() != case_id:
        raise CurrentDecisionProjectionError("lifecycle resolution case mismatch")
    if str(token.get("case_id") or "").strip() != case_id:
        raise CurrentDecisionProjectionError("lifecycle generation case mismatch")
    if str(model.get("lifecycle_case_id") or "").strip() != case_id:
        raise CurrentDecisionProjectionError("lifecycle read model case mismatch")
    target = {
        str(key): int(value)
        for key, value in sorted(
            dict(case.get("target_contracts_by_lot") or {}).items()
        )
    }
    admission = dict(admission_head or {})
    status = str(case.get("status") or "").strip().lower()
    reason_state = str(summary.get("reason_state") or "").strip().lower()
    if not reason_state:
        reason_state = {
            "ledger_written": "resolved",
            "partially_resolved": "partially_resolved",
            "needs_review": "needs_review",
            "conflict": "conflict",
            "waiting_settlement_evidence": "cause_pending",
            "pending": "not_started",
        }.get(status, "not_started")
    fact = {
        "schema_version": LIFECYCLE_CASE_DECISION_FACT_SCHEMA,
        "case_id": case_id,
        "account": account,
        "market": str(
            case.get("market") or symbol_market(case.get("symbol")) or ""
        ).strip().upper(),
        "contract": {
            "broker": str(case.get("broker") or "").strip().lower(),
            "futu_account_id": str(case.get("futu_account_id") or "").strip()
            or None,
            "symbol": str(case.get("symbol") or "").strip().upper(),
            "option_type": str(case.get("option_type") or "").strip().lower(),
            "position_side": str(case.get("position_side") or "").strip().lower(),
            "strike": _decimal_text(case.get("strike"), field="strike", optional=True),
            "expiration_ymd": str(case.get("expiration_ymd") or "").strip(),
            "contract_key": str(case.get("contract_key") or "").strip(),
        },
        "target_contracts_by_lot": target,
        "status": status,
        "decision": {
            "decision_type": str(case.get("decision_type") or "").strip().lower()
            or None,
            "reason_state": reason_state,
            "close_reason": str(summary.get("close_reason") or "").strip().lower()
            or None,
            "reason_codes": sorted(
                {
                    str(item).strip()
                    for item in (
                        summary.get("lifecycle_reason_codes")
                        or case.get("reason_codes")
                        or []
                    )
                    if str(item).strip()
                }
            ),
            "resolution_revision": int(summary.get("resolution_revision") or 0),
            "state_fingerprint": str(summary.get("state_fingerprint") or "").strip()
            or None,
            "quality_trust_class": _lifecycle_quality_trust_class(case),
        },
        "resolution": {
            "status": str(resolution.get("status") or "").strip().lower(),
            "resolved_contracts_by_lot": dict(
                sorted(dict(model.get("resolved_contracts_by_lot") or {}).items())
            ),
            "remaining_contracts_by_lot": dict(
                sorted(dict(model.get("remaining_contracts_by_lot") or {}).items())
            ),
            "resolved_contracts_by_terminal_type": dict(
                sorted(
                    dict(
                        model.get("resolved_contracts_by_terminal_type") or {}
                    ).items()
                )
            ),
            "requested_reservations_by_lot": dict(
                sorted(
                    dict(
                        resolution.get("requested_reservations_by_lot") or {}
                    ).items()
                )
            ),
            "effective_reservations_by_lot": dict(
                sorted(
                    dict(
                        resolution.get("effective_reservations_by_lot") or {}
                    ).items()
                )
            ),
            "contested_reason_codes": sorted(
                {
                    str(item).strip()
                    for item in resolution.get("reason_codes") or []
                    if str(item).strip()
                }
            ),
            "anchor_facts": sorted(
                [dict(item) for item in resolution.get("anchor_facts") or []],
                key=lambda item: str(item.get("anchor_fact_id") or ""),
            ),
        },
        "timing": {
            "observation_start_ms": model.get("observation_start_ms"),
            "pending_until_ms": model.get("pending_until_ms"),
            "settlement_deadline_ms": model.get("pending_until_ms"),
            "timing_policy_hash": model.get("timing_policy_hash"),
        },
        "evidence": {
            "revision": int(evidence_revision),
            "count": int(evidence_count),
            "admitted_semantic_schema": admission.get("semantic_schema"),
            "admitted_semantic_fingerprint": admission.get("semantic_fingerprint"),
            "admitted_evidence_id": admission.get("evidence_id"),
            "admitted_evidence_count": 1 if admission else 0,
        },
        "generation": {
            "dependency_case_ids": sorted(
                str(item) for item in token.get("dependency_case_ids") or []
            ),
            "target_lot_ids": sorted(
                str(item) for item in token.get("target_lot_ids") or []
            ),
            "generation_token": "",
        },
    }
    fact["generation"]["generation_token"] = (
        _lifecycle_case_current_generation_token(fact)
    )
    fact["fact_sha256"] = _fact_hash(fact)
    return validate_lifecycle_case_decision_fact(fact)


def _lifecycle_quality_trust_class(case: Mapping[str, Any]) -> str:
    status = str(case.get("status") or "").strip().lower()
    decision_type = str(case.get("decision_type") or "").strip().lower()
    if status in {
        "external_adjustment_pending_review",
        "external_adjustment",
        "manual_review",
    } or decision_type in {
        "external_adjustment_pending_review",
        "external_adjustment",
        "manual_review",
    }:
        return "external_review"
    if (
        bool(case.get("legacy_evidence_gap"))
        or case.get("migration_evidence_complete") is False
        or str(case.get("quality_classification") or "").strip().lower()
        == "legacy_evidence_gap"
    ):
        return "legacy_gap"
    return "trusted"


def validate_lifecycle_case_decision_fact(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _CASE_FACT_KEYS:
        raise CurrentDecisionProjectionError("lifecycle case fact shape is invalid")
    item = dict(payload)
    if item["schema_version"] != LIFECYCLE_CASE_DECISION_FACT_SCHEMA:
        raise CurrentDecisionProjectionError("lifecycle case fact schema is invalid")
    case_id = _text(item["case_id"], field="case_id")
    account = _text(item["account"], field="account", lower=True)
    _text(item["market"], field="market", upper=True)

    contract = item["contract"]
    contract_keys = {
        "broker",
        "futu_account_id",
        "symbol",
        "option_type",
        "position_side",
        "strike",
        "expiration_ymd",
        "contract_key",
    }
    if not isinstance(contract, Mapping) or set(contract) != contract_keys:
        raise CurrentDecisionProjectionError("lifecycle contract shape is invalid")
    _text(contract["broker"], field="contract.broker", lower=True)
    _optional_text(contract["futu_account_id"], field="contract.futu_account_id")
    _text(contract["symbol"], field="contract.symbol", upper=True)
    _text(contract["option_type"], field="contract.option_type", lower=True)
    _text(contract["position_side"], field="contract.position_side", lower=True)
    if contract["strike"] is not None:
        if _decimal_text(contract["strike"], field="contract.strike") != contract["strike"]:
            raise CurrentDecisionProjectionError("contract.strike is not canonical")
    _text(contract["expiration_ymd"], field="contract.expiration_ymd")
    _text(contract["contract_key"], field="contract.contract_key")

    target = _integer_map(
        item["target_contracts_by_lot"],
        field="target_contracts_by_lot",
        positive=True,
    )
    if not target:
        raise CurrentDecisionProjectionError("target_contracts_by_lot is required")
    _text(item["status"], field="status", lower=True)

    decision = item["decision"]
    decision_keys = {
        "decision_type",
        "reason_state",
        "close_reason",
        "reason_codes",
        "resolution_revision",
        "state_fingerprint",
        "quality_trust_class",
    }
    if not isinstance(decision, Mapping) or set(decision) != decision_keys:
        raise CurrentDecisionProjectionError("lifecycle decision shape is invalid")
    _optional_text(decision["decision_type"], field="decision_type", lower=True)
    _text(decision["reason_state"], field="reason_state", lower=True)
    _optional_text(decision["close_reason"], field="close_reason", lower=True)
    _text_list(decision["reason_codes"], field="reason_codes")
    _integer(decision["resolution_revision"], field="resolution_revision")
    _sha256(decision["state_fingerprint"], field="state_fingerprint", optional=True)
    if decision["quality_trust_class"] not in {
        "trusted",
        "legacy_gap",
        "external_review",
    }:
        raise CurrentDecisionProjectionError("quality_trust_class is invalid")

    resolution = item["resolution"]
    resolution_keys = {
        "status",
        "resolved_contracts_by_lot",
        "remaining_contracts_by_lot",
        "resolved_contracts_by_terminal_type",
        "requested_reservations_by_lot",
        "effective_reservations_by_lot",
        "contested_reason_codes",
        "anchor_facts",
    }
    if not isinstance(resolution, Mapping) or set(resolution) != resolution_keys:
        raise CurrentDecisionProjectionError("lifecycle resolution shape is invalid")
    _text(resolution["status"], field="resolution.status", lower=True)
    resolved = _integer_map(
        resolution["resolved_contracts_by_lot"],
        field="resolved_contracts_by_lot",
    )
    remaining = _integer_map(
        resolution["remaining_contracts_by_lot"],
        field="remaining_contracts_by_lot",
    )
    terminal = _integer_map(
        resolution["resolved_contracts_by_terminal_type"],
        field="resolved_contracts_by_terminal_type",
    )
    requested = _integer_map(
        resolution["requested_reservations_by_lot"],
        field="requested_reservations_by_lot",
        positive=True,
    )
    effective = _integer_map(
        resolution["effective_reservations_by_lot"],
        field="effective_reservations_by_lot",
        positive=True,
    )
    if set(resolved) != set(target) or set(remaining) != set(target):
        raise CurrentDecisionProjectionError("lifecycle quantity keys mismatch")
    if any(resolved[key] + remaining[key] != target[key] for key in target):
        raise CurrentDecisionProjectionError("lifecycle quantity total mismatch")
    if sum(terminal.values()) != sum(resolved.values()):
        raise CurrentDecisionProjectionError("terminal quantity total mismatch")
    if any(key not in target or value > remaining[key] for key, value in requested.items()):
        raise CurrentDecisionProjectionError("requested reservation exceeds remaining")
    if any(key not in requested or value > requested[key] for key, value in effective.items()):
        raise CurrentDecisionProjectionError("effective reservation exceeds requested")
    _text_list(
        resolution["contested_reason_codes"],
        field="contested_reason_codes",
    )
    _normalize_anchor_facts(resolution["anchor_facts"], case_id=case_id)

    timing = item["timing"]
    timing_keys = {
        "observation_start_ms",
        "pending_until_ms",
        "settlement_deadline_ms",
        "timing_policy_hash",
    }
    if not isinstance(timing, Mapping) or set(timing) != timing_keys:
        raise CurrentDecisionProjectionError("lifecycle timing shape is invalid")
    for field in (
        "observation_start_ms",
        "pending_until_ms",
        "settlement_deadline_ms",
    ):
        _optional_integer(timing[field], field=field, minimum=1)
    _sha256(timing["timing_policy_hash"], field="timing_policy_hash", optional=True)

    evidence = item["evidence"]
    evidence_keys = {
        "revision",
        "count",
        "admitted_semantic_schema",
        "admitted_semantic_fingerprint",
        "admitted_evidence_id",
        "admitted_evidence_count",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != evidence_keys:
        raise CurrentDecisionProjectionError("lifecycle evidence shape is invalid")
    _integer(evidence["revision"], field="evidence.revision")
    count = _integer(evidence["count"], field="evidence.count")
    admitted_count = _integer(
        evidence["admitted_evidence_count"],
        field="admitted_evidence_count",
    )
    admission_fields = (
        evidence["admitted_semantic_schema"],
        evidence["admitted_semantic_fingerprint"],
        evidence["admitted_evidence_id"],
    )
    if admitted_count not in {0, 1} or (admitted_count == 0) != all(
        value is None for value in admission_fields
    ):
        raise CurrentDecisionProjectionError("lifecycle admission shape is invalid")
    if admitted_count:
        _text(admission_fields[0], field="admitted_semantic_schema")
        _sha256(admission_fields[1], field="admitted_semantic_fingerprint")
        _text(admission_fields[2], field="admitted_evidence_id")
        if count < 1:
            raise CurrentDecisionProjectionError("admitted evidence is not counted")

    generation = item["generation"]
    generation_keys = {
        "dependency_case_ids",
        "target_lot_ids",
        "generation_token",
    }
    if not isinstance(generation, Mapping) or set(generation) != generation_keys:
        raise CurrentDecisionProjectionError("lifecycle generation shape is invalid")
    dependency_ids = _text_list(
        generation["dependency_case_ids"],
        field="dependency_case_ids",
    )
    if case_id not in dependency_ids:
        raise CurrentDecisionProjectionError("lifecycle generation omits case")
    target_ids = _text_list(generation["target_lot_ids"], field="target_lot_ids")
    if not set(target).issubset(target_ids):
        raise CurrentDecisionProjectionError("lifecycle generation target mismatch")
    supplied_generation_token = _sha256(
        generation["generation_token"],
        field="generation_token",
    )
    if supplied_generation_token != _lifecycle_case_current_generation_token(item):
        raise CurrentDecisionProjectionError(
            "lifecycle compact generation token mismatch"
        )
    supplied_hash = _sha256(item["fact_sha256"], field="fact_sha256")
    if supplied_hash != _fact_hash(item):
        raise CurrentDecisionProjectionError("lifecycle case fact hash mismatch")
    if account != item["account"]:
        raise CurrentDecisionProjectionError("lifecycle account is not canonical")
    return item


def _lifecycle_admission_from_fact_state(
    fact_state: Mapping[str, Any],
) -> dict[str, Any] | None:
    admission_fields = (
        fact_state.get("admitted_semantic_schema"),
        fact_state.get("admitted_semantic_fingerprint"),
        fact_state.get("admitted_evidence_id"),
    )
    admission = (
        {
            "semantic_schema": admission_fields[0],
            "semantic_fingerprint": admission_fields[1],
            "evidence_id": admission_fields[2],
        }
        if all(value is not None for value in admission_fields)
        else None
    )
    if admission is None and any(value is not None for value in admission_fields):
        raise CurrentDecisionProjectionError("lifecycle admission state is incomplete")
    return admission


def build_initial_lifecycle_case_decision_fact(
    *,
    lifecycle_case: Mapping[str, Any],
    fact_state: Mapping[str, Any],
    resolution: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = dict(lifecycle_case)
    case_id = str(case.get("case_id") or "").strip()
    target = dict(case.get("target_contracts_by_lot") or {})
    summary = (
        dict(case.get("derived_summary") or {})
        if isinstance(case.get("derived_summary"), Mapping)
        else {}
    )
    resolved = dict(summary.get("resolved_contracts_by_lot") or {})
    if not resolved:
        resolved = {lot_id: 0 for lot_id in target}
    remaining = dict(summary.get("remaining_contracts_by_lot") or {})
    if not remaining:
        remaining = {
            lot_id: int(contracts) - int(resolved.get(lot_id, 0))
            for lot_id, contracts in target.items()
        }
    resolution_value = {
        "case_id": case_id,
        "status": "missing",
        "reason_codes": [],
        "requested_reservations_by_lot": {},
        "effective_reservations_by_lot": {},
        "anchor_facts": [],
        **dict(resolution or {}),
    }
    timing_value = dict(timing or {})
    return build_lifecycle_case_decision_fact(
        lifecycle_case=case,
        case_resolution=resolution_value,
        generation_token={
            "case_id": case_id,
            "dependency_case_ids": [case_id],
            "target_lot_ids": sorted(target),
        },
        read_model={
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": resolved,
            "remaining_contracts_by_lot": remaining,
            "resolved_contracts_by_terminal_type": dict(
                summary.get("resolved_contracts_by_terminal_type") or {}
            ),
            "observation_start_ms": timing_value.get(
                "observation_start_ms",
                case.get("observation_start_ms"),
            ),
            "pending_until_ms": timing_value.get(
                "pending_until_ms",
                case.get("pending_until_ms"),
            ),
            "timing_policy_hash": timing_value.get("timing_policy_hash"),
        },
        evidence_revision=int(fact_state.get("evidence_revision") or 0),
        evidence_count=int(fact_state.get("evidence_count") or 0),
        admission_head=_lifecycle_admission_from_fact_state(fact_state),
    )


def advance_lifecycle_case_decision_fact(
    prior_fact: Mapping[str, Any],
    *,
    lifecycle_case: Mapping[str, Any],
    fact_state: Mapping[str, Any],
    resolution: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prior = validate_lifecycle_case_decision_fact(prior_fact)
    case = dict(lifecycle_case)
    case_id = str(case.get("case_id") or "").strip()
    account = str(case.get("account") or "").strip().lower()
    if case_id != prior["case_id"] or account != prior["account"]:
        raise CurrentDecisionProjectionError("lifecycle prior fact binding changed")

    prior_resolution = dict(prior["resolution"])
    resolution_update = dict(resolution or {})
    summary = (
        dict(case.get("derived_summary") or {})
        if isinstance(case.get("derived_summary"), Mapping)
        else {}
    )

    def quantity_map(field: str) -> dict[str, int]:
        value = resolution_update.get(field, summary.get(field))
        return (
            dict(value)
            if isinstance(value, Mapping)
            else dict(prior_resolution[field])
        )

    timing_update = dict(timing or {})
    prior_timing = dict(prior["timing"])
    observation_start_ms = timing_update.get(
        "observation_start_ms",
        prior_timing["observation_start_ms"],
    )
    pending_until_ms = timing_update.get(
        "pending_until_ms",
        prior_timing["pending_until_ms"],
    )
    return build_lifecycle_case_decision_fact(
        lifecycle_case=case,
        case_resolution={
            "case_id": case_id,
            "status": resolution_update.get("status", prior_resolution["status"]),
            "reason_codes": resolution_update.get(
                "reason_codes",
                prior_resolution["contested_reason_codes"],
            ),
            "requested_reservations_by_lot": resolution_update.get(
                "requested_reservations_by_lot",
                prior_resolution["requested_reservations_by_lot"],
            ),
            "effective_reservations_by_lot": resolution_update.get(
                "effective_reservations_by_lot",
                prior_resolution["effective_reservations_by_lot"],
            ),
            "anchor_facts": resolution_update.get(
                "anchor_facts",
                prior_resolution["anchor_facts"],
            ),
        },
        generation_token={
            "case_id": case_id,
            "dependency_case_ids": prior["generation"]["dependency_case_ids"],
            "target_lot_ids": prior["generation"]["target_lot_ids"],
        },
        read_model={
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": quantity_map(
                "resolved_contracts_by_lot"
            ),
            "remaining_contracts_by_lot": quantity_map(
                "remaining_contracts_by_lot"
            ),
            "resolved_contracts_by_terminal_type": quantity_map(
                "resolved_contracts_by_terminal_type"
            ),
            "observation_start_ms": observation_start_ms,
            "pending_until_ms": pending_until_ms,
            "timing_policy_hash": timing_update.get(
                "timing_policy_hash",
                prior_timing["timing_policy_hash"],
            ),
        },
        evidence_revision=int(fact_state.get("evidence_revision") or 0),
        evidence_count=int(fact_state.get("evidence_count") or 0),
        admission_head=_lifecycle_admission_from_fact_state(fact_state),
    )


_ASSIGNED_LOT_KEYS = frozenset(
    {
        "stock_lot_id",
        "source_assignment_event_id",
        "source_option_lot_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "assigned_at_ms",
        "shares_opened",
        "shares_remaining",
        "assignment_price",
        "remaining_cost_basis",
        "basis_policy",
        "strategy",
        "leg_role",
        "strategy_group_id",
        "yield_enhancement_mode",
        "source_option_leg_role",
        "sale_fact_count",
        "sale_fact_chain_sha256",
    }
)
_ASSIGNED_ALLOCATION_KEYS = frozenset(
    {
        "open_event_id",
        "stock_lot_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "shares",
        "start_at_ms",
        "end_at_ms",
        "allocation_status",
        "linkage_basis",
    }
)
_ASSIGNED_LINKAGE_BASES = frozenset({"stock_lot_id", "strategy_group"})
_ASSIGNED_REVIEW_KEYS = frozenset(
    {
        "status",
        "event_id",
        "stock_lot_id",
        "stock_event_id",
        "account",
        "broker",
        "symbol",
        "details_sha256",
    }
)


def _sale_fact_chain(event_ids: Iterable[str]) -> tuple[int, str]:
    chain = bytes(32)
    count = 0
    for event_id in event_ids:
        value = str(event_id or "").strip()
        if not value:
            raise CurrentDecisionProjectionError("assigned sale event id is required")
        chain = hashlib.sha256(
            chain + len(value.encode("utf-8")).to_bytes(4, "big") + value.encode("utf-8")
        ).digest()
        count += 1
    return count, chain.hex()


def compact_assigned_stock_view(
    report: Mapping[str, Any],
    *,
    account: str,
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise CurrentDecisionProjectionError("assigned stock account is required")
    active_open_event_ids = {
        str(
            (item.get("fields") or {}).get("source_event_id")
            or (item.get("fields") or {}).get("open_event_id")
            or item.get("source_event_id")
            or ""
        ).strip()
        for item in current_position_lots
        if isinstance(item, Mapping)
        and isinstance(item.get("fields"), Mapping)
        and int((item.get("fields") or {}).get("contracts_open") or 0) > 0
    }
    lot_rows: list[dict[str, Any]] = []
    raw_lots = report.get("_all_assigned_stock_lots")
    if not isinstance(raw_lots, list):
        raw_lots = report.get("assigned_stock_lots")
    if not isinstance(raw_lots, list):
        raise CurrentDecisionProjectionError("assigned stock report lots are invalid")
    for raw in raw_lots:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("assigned stock report lot is invalid")
        row = dict(raw)
        if str(row.get("account") or "").strip().lower() != account_value:
            continue
        shares_remaining = int(row.get("shares_remaining") or 0)
        if shares_remaining <= 0:
            continue
        sale_ids = [str(value).strip() for value in row.get("sale_event_ids") or []]
        sale_count, sale_chain = _sale_fact_chain(sale_ids)
        lot_rows.append(
            {
                "stock_lot_id": str(row.get("stock_lot_id") or "").strip(),
                "source_assignment_event_id": str(
                    row.get("source_assignment_event_id") or ""
                ).strip(),
                "source_option_lot_id": str(
                    row.get("source_option_lot_id") or ""
                ).strip()
                or None,
                "account": account_value,
                "broker": str(row.get("broker") or "").strip().lower(),
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "currency": str(row.get("currency") or "").strip().upper(),
                "assigned_at_ms": int(row.get("assigned_at_ms") or 0),
                "shares_opened": int(row.get("shares_opened") or 0),
                "shares_remaining": shares_remaining,
                "assignment_price": _decimal_text(
                    row.get("assignment_price"),
                    field="assignment_price",
                ),
                "remaining_cost_basis": _decimal_text(
                    row.get("remaining_stock_cost_basis"),
                    field="remaining_cost_basis",
                ),
                "basis_policy": str(row.get("basis_policy") or "").strip(),
                "strategy": str(row.get("strategy") or "").strip().lower()
                or None,
                "leg_role": str(row.get("leg_role") or "").strip().lower()
                or None,
                "strategy_group_id": str(
                    row.get("strategy_group_id") or ""
                ).strip()
                or None,
                "yield_enhancement_mode": str(
                    row.get("yield_enhancement_mode") or ""
                ).strip().lower()
                or None,
                "source_option_leg_role": str(
                    row.get("source_option_leg_role") or ""
                ).strip().lower()
                or None,
                "sale_fact_count": sale_count,
                "sale_fact_chain_sha256": sale_chain,
            }
        )
    lot_rows.sort(key=lambda item: item["stock_lot_id"])

    allocations: list[dict[str, Any]] = []
    raw_allocations = report.get("covered_call_allocations") or []
    if not isinstance(raw_allocations, list):
        raise CurrentDecisionProjectionError("assigned stock allocations are invalid")
    for raw in raw_allocations:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("assigned stock allocation is invalid")
        row = dict(raw)
        open_event_id = str(row.get("open_event_id") or "").strip()
        if (
            str(row.get("account") or "").strip().lower() != account_value
            or open_event_id not in active_open_event_ids
        ):
            continue
        allocations.append(
            {
                "open_event_id": open_event_id,
                "stock_lot_id": str(row.get("stock_lot_id") or "").strip(),
                "account": account_value,
                "broker": str(row.get("broker") or "").strip().lower(),
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "currency": str(row.get("currency") or "").strip().upper(),
                "shares": int(row.get("shares") or 0),
                "start_at_ms": int(row.get("start_at_ms") or 0),
                "end_at_ms": None,
                "allocation_status": str(
                    row.get("allocation_status") or ""
                ).strip().lower(),
                "linkage_basis": str(row.get("linkage_basis") or "")
                .strip()
                .lower(),
            }
        )
    allocations.sort(
        key=lambda item: (
            item["open_event_id"],
            item["stock_lot_id"],
            item["start_at_ms"],
        )
    )

    review_rows: list[dict[str, Any]] = []
    quote_only = {"missing_quote", "covered_call_unrealized_missing"}
    raw_reviews = report.get("assigned_stock_review_rows") or []
    if not isinstance(raw_reviews, list):
        raise CurrentDecisionProjectionError("assigned stock reviews are invalid")
    for raw in raw_reviews:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("assigned stock review is invalid")
        row = dict(raw)
        status = str(row.get("status") or "").strip().lower()
        row_account = str(row.get("account") or "").strip().lower()
        if status in quote_only or (row_account and row_account != account_value):
            continue
        review_rows.append(
            {
                "status": status,
                "event_id": str(row.get("event_id") or "").strip() or None,
                "stock_lot_id": str(row.get("stock_lot_id") or "").strip()
                or None,
                "stock_event_id": str(row.get("stock_event_id") or "").strip()
                or None,
                "account": account_value,
                "broker": str(row.get("broker") or "").strip().lower() or None,
                "symbol": str(row.get("symbol") or "").strip().upper() or None,
                "details_sha256": canonical_sha256(dict(row.get("details") or {})),
            }
        )
    review_rows.sort(
        key=lambda item: (
            item["status"],
            item["stock_lot_id"] or "",
            item["stock_event_id"] or "",
            item["event_id"] or "",
        )
    )
    sale_count = sum(int(item["sale_fact_count"]) for item in lot_rows)
    sale_chain = canonical_sha256(
        [
            {
                "stock_lot_id": item["stock_lot_id"],
                "sale_fact_count": item["sale_fact_count"],
                "sale_fact_chain_sha256": item["sale_fact_chain_sha256"],
            }
            for item in lot_rows
        ]
    )
    result = {
        "schema_version": CURRENT_ASSIGNED_STOCK_SCHEMA,
        "account": account_value,
        "lots": lot_rows,
        "covered_call_allocations": allocations,
        "review_facts": review_rows,
        "applied_sale_fact_count": sale_count,
        "applied_sale_fact_chain_sha256": sale_chain,
    }
    result["current_view_hash"] = canonical_sha256(result)
    return validate_assigned_stock_fact(result)


def empty_assigned_stock_fact(account: str) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    count, chain = 0, canonical_sha256([])
    result = {
        "schema_version": CURRENT_ASSIGNED_STOCK_SCHEMA,
        "account": account_value,
        "lots": [],
        "covered_call_allocations": [],
        "review_facts": [],
        "applied_sale_fact_count": count,
        "applied_sale_fact_chain_sha256": chain,
    }
    result["current_view_hash"] = canonical_sha256(result)
    return validate_assigned_stock_fact(result)


def validate_assigned_stock_fact(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "account",
        "lots",
        "covered_call_allocations",
        "review_facts",
        "applied_sale_fact_count",
        "applied_sale_fact_chain_sha256",
        "current_view_hash",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise CurrentDecisionProjectionError("assigned stock fact shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_ASSIGNED_STOCK_SCHEMA:
        raise CurrentDecisionProjectionError("assigned stock schema is invalid")
    account = _text(item["account"], field="assigned account", lower=True)
    lots = item["lots"]
    if not isinstance(lots, list):
        raise CurrentDecisionProjectionError("assigned stock lots must be a list")
    lot_ids: list[str] = []
    sale_count = 0
    sale_summaries: list[dict[str, Any]] = []
    for raw in lots:
        if not isinstance(raw, Mapping) or set(raw) != _ASSIGNED_LOT_KEYS:
            raise CurrentDecisionProjectionError("assigned stock lot shape is invalid")
        lot = dict(raw)
        lot_ids.append(_text(lot["stock_lot_id"], field="stock_lot_id"))
        _text(lot["source_assignment_event_id"], field="source_assignment_event_id")
        _optional_text(lot["source_option_lot_id"], field="source_option_lot_id")
        if _text(lot["account"], field="lot account", lower=True) != account:
            raise CurrentDecisionProjectionError("assigned stock lot account mismatch")
        _text(lot["broker"], field="lot broker", lower=True)
        _text(lot["symbol"], field="lot symbol", upper=True)
        _text(lot["currency"], field="lot currency", upper=True)
        _integer(lot["assigned_at_ms"], field="assigned_at_ms", minimum=1)
        opened = _integer(lot["shares_opened"], field="shares_opened", minimum=1)
        remaining = _integer(
            lot["shares_remaining"], field="shares_remaining", minimum=1
        )
        if remaining > opened:
            raise CurrentDecisionProjectionError("assigned shares exceed opened shares")
        for field in ("assignment_price", "remaining_cost_basis"):
            if _nonnegative_decimal_text(lot[field], field=field) != lot[field]:
                raise CurrentDecisionProjectionError(f"{field} is not canonical")
        _text(lot["basis_policy"], field="basis_policy")
        for field in (
            "strategy",
            "leg_role",
            "yield_enhancement_mode",
            "source_option_leg_role",
        ):
            _optional_text(lot[field], field=field, lower=True)
        _optional_text(lot["strategy_group_id"], field="strategy_group_id")
        lot_sale_count = _integer(lot["sale_fact_count"], field="sale_fact_count")
        lot_sale_chain = _sha256(
            lot["sale_fact_chain_sha256"], field="sale_fact_chain_sha256"
        )
        sale_count += lot_sale_count
        sale_summaries.append(
            {
                "stock_lot_id": lot["stock_lot_id"],
                "sale_fact_count": lot_sale_count,
                "sale_fact_chain_sha256": lot_sale_chain,
            }
        )
    if lot_ids != sorted(set(lot_ids)):
        raise CurrentDecisionProjectionError("assigned stock lots are not canonical")

    lot_id_set = set(lot_ids)
    allocations = item["covered_call_allocations"]
    if not isinstance(allocations, list):
        raise CurrentDecisionProjectionError("covered call allocations must be a list")
    allocation_keys: list[tuple[str, str, int]] = []
    for raw in allocations:
        if not isinstance(raw, Mapping) or set(raw) != _ASSIGNED_ALLOCATION_KEYS:
            raise CurrentDecisionProjectionError("covered call allocation shape is invalid")
        allocation = dict(raw)
        open_event_id = _text(allocation["open_event_id"], field="open_event_id")
        stock_lot_id = _text(allocation["stock_lot_id"], field="stock_lot_id")
        if stock_lot_id not in lot_id_set:
            raise CurrentDecisionProjectionError("covered call allocation lot is missing")
        if _text(allocation["account"], field="allocation account", lower=True) != account:
            raise CurrentDecisionProjectionError("covered call allocation account mismatch")
        _text(allocation["broker"], field="allocation broker", lower=True)
        _text(allocation["symbol"], field="allocation symbol", upper=True)
        _text(allocation["currency"], field="allocation currency", upper=True)
        _integer(allocation["shares"], field="allocation shares", minimum=1)
        start = _integer(allocation["start_at_ms"], field="allocation start", minimum=1)
        end = _optional_integer(allocation["end_at_ms"], field="allocation end", minimum=1)
        if end is not None and end < start:
            raise CurrentDecisionProjectionError("covered call allocation time is invalid")
        _text(allocation["allocation_status"], field="allocation_status", lower=True)
        linkage_basis = _text(
            allocation["linkage_basis"], field="linkage_basis", lower=True
        )
        if linkage_basis not in _ASSIGNED_LINKAGE_BASES:
            raise CurrentDecisionProjectionError(
                "covered call linkage basis is invalid"
            )
        allocation_keys.append((open_event_id, stock_lot_id, start))
    if allocation_keys != sorted(set(allocation_keys)):
        raise CurrentDecisionProjectionError("covered call allocations are not canonical")

    reviews = item["review_facts"]
    if not isinstance(reviews, list):
        raise CurrentDecisionProjectionError("assigned review facts must be a list")
    review_keys: list[tuple[str, str, str, str]] = []
    for raw in reviews:
        if not isinstance(raw, Mapping) or set(raw) != _ASSIGNED_REVIEW_KEYS:
            raise CurrentDecisionProjectionError("assigned review fact shape is invalid")
        review = dict(raw)
        status = _text(review["status"], field="review status", lower=True)
        event_id = _optional_text(review["event_id"], field="review event_id")
        lot_id = _optional_text(review["stock_lot_id"], field="review stock_lot_id")
        stock_event_id = _optional_text(
            review["stock_event_id"], field="review stock_event_id"
        )
        if _text(review["account"], field="review account", lower=True) != account:
            raise CurrentDecisionProjectionError("assigned review account mismatch")
        _optional_text(review["broker"], field="review broker", lower=True)
        _optional_text(review["symbol"], field="review symbol", upper=True)
        _sha256(review["details_sha256"], field="review details_sha256")
        review_keys.append((status, lot_id or "", stock_event_id or "", event_id or ""))
    if review_keys != sorted(set(review_keys)):
        raise CurrentDecisionProjectionError("assigned review facts are not canonical")

    if _integer(item["applied_sale_fact_count"], field="applied_sale_fact_count") != sale_count:
        raise CurrentDecisionProjectionError("assigned sale count mismatch")
    if (
        _sha256(
            item["applied_sale_fact_chain_sha256"],
            field="applied_sale_fact_chain_sha256",
        )
        != canonical_sha256(sale_summaries)
    ):
        raise CurrentDecisionProjectionError("assigned sale chain mismatch")
    supplied_hash = _sha256(item["current_view_hash"], field="current_view_hash")
    if supplied_hash != _hash_without(item, "current_view_hash"):
        raise CurrentDecisionProjectionError("assigned stock view hash mismatch")
    return item


def _assigned_fact_with(
    prior: Mapping[str, Any],
    *,
    lots: Sequence[Mapping[str, Any]] | None = None,
    allocations: Sequence[Mapping[str, Any]] | None = None,
    reviews: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    item = validate_assigned_stock_fact(prior)
    next_lots = sorted(
        (dict(row) for row in (lots if lots is not None else item["lots"])),
        key=lambda row: str(row["stock_lot_id"]),
    )
    next_allocations = sorted(
        (
            dict(row)
            for row in (
                allocations
                if allocations is not None
                else item["covered_call_allocations"]
            )
        ),
        key=lambda row: (
            str(row["open_event_id"]),
            str(row["stock_lot_id"]),
            int(row["start_at_ms"]),
        ),
    )
    next_reviews = sorted(
        (dict(row) for row in (reviews if reviews is not None else item["review_facts"])),
        key=lambda row: (
            str(row["status"]),
            str(row.get("stock_lot_id") or ""),
            str(row.get("stock_event_id") or ""),
            str(row.get("event_id") or ""),
        ),
    )
    sale_summaries = [
        {
            "stock_lot_id": row["stock_lot_id"],
            "sale_fact_count": row["sale_fact_count"],
            "sale_fact_chain_sha256": row["sale_fact_chain_sha256"],
        }
        for row in next_lots
    ]
    result = {
        **item,
        "lots": next_lots,
        "covered_call_allocations": next_allocations,
        "review_facts": next_reviews,
        "applied_sale_fact_count": sum(
            int(row["sale_fact_count"]) for row in next_lots
        ),
        "applied_sale_fact_chain_sha256": canonical_sha256(sale_summaries),
    }
    result["current_view_hash"] = _hash_without(result, "current_view_hash")
    return validate_assigned_stock_fact(result)


def _require_final_option_lot(
    current_position_lots: Sequence[Mapping[str, Any]],
    *,
    target_lot_id: str,
    expected_contracts_open: int,
    settlement: Mapping[str, Any],
) -> None:
    lots = _position_lot_fields(current_position_lots)
    fields = lots.get(target_lot_id)
    if fields is None:
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option lot is missing"
        )
    observed = _integer(
        fields.get("contracts_open"),
        field="final option contracts_open",
    )
    if observed != expected_contracts_open:
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option lot mismatch"
        )
    if (
        str(fields.get("account") or "").strip().lower() != settlement["account"]
        or str(fields.get("broker") or "").strip().lower() != settlement["broker"]
        or str(fields.get("symbol") or "").strip().upper() != settlement["symbol"]
        or str(fields.get("currency") or "").strip().upper() != settlement["currency"]
        or str(fields.get("option_type") or "").strip().lower()
        != settlement["option_type"]
        or str(fields.get("side") or "").strip().lower()
        != settlement["position_side"]
    ):
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option identity mismatch"
        )


def _settlement_transition(
    transition: Mapping[str, Any],
    *,
    expected_side: str,
) -> dict[str, Any]:
    required = {
        "kind",
        "terminal_event_id",
        "terminal_type",
        "option_type",
        "position_side",
        "strike",
        "target_option_lot_id",
        "expected_contracts_open_after",
        "contracts",
        "multiplier",
        "account",
        "broker",
        "symbol",
        "currency",
        "stock_settlement",
    }
    if not isinstance(transition, Mapping) or set(transition) != required:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement transition shape is invalid"
        )
    item = dict(transition)
    terminal_type = _text(item["terminal_type"], field="terminal_type", lower=True)
    option_type = _text(item["option_type"], field="option_type", lower=True)
    position_side = _text(
        item["position_side"], field="position_side", lower=True
    )
    side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get((terminal_type, option_type, position_side))
    if side != expected_side:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement option binding is invalid"
        )
    stock = item["stock_settlement"]
    stock_keys = {"side", "shares", "price", "event_time_ms", "fees"}
    if not isinstance(stock, Mapping) or set(stock) != stock_keys:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement facts are invalid"
        )
    stock_row = dict(stock)
    if _text(stock_row["side"], field="stock side", lower=True) != expected_side:
        raise CurrentDecisionProjectionError("assigned-stock settlement side mismatch")
    contracts = _integer(item["contracts"], field="contracts", minimum=1)
    multiplier = _integer(item["multiplier"], field="multiplier", minimum=1)
    shares = _integer(stock_row["shares"], field="shares", minimum=1)
    if shares != contracts * multiplier:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement quantity mismatch"
        )
    stock_row["price"] = _nonnegative_decimal_text(
        stock_row["price"], field="stock price"
    )
    stock_row["fees"] = _nonnegative_decimal_text(stock_row["fees"], field="stock fees")
    stock_row["event_time_ms"] = _integer(
        stock_row["event_time_ms"], field="stock event_time_ms", minimum=1
    )
    item["terminal_event_id"] = _text(
        item["terminal_event_id"], field="terminal_event_id"
    )
    item["target_option_lot_id"] = _text(
        item["target_option_lot_id"], field="target_option_lot_id"
    )
    item["strike"] = _nonnegative_decimal_text(item["strike"], field="strike")
    if item["strike"] == "0":
        raise CurrentDecisionProjectionError("strike must be positive")
    item["expected_contracts_open_after"] = _integer(
        item["expected_contracts_open_after"],
        field="expected_contracts_open_after",
    )
    item["account"] = _text(item["account"], field="account", lower=True)
    item["broker"] = _text(item["broker"], field="broker", lower=True)
    item["symbol"] = _text(item["symbol"], field="symbol", upper=True)
    item["currency"] = _text(item["currency"], field="currency", upper=True)
    item["stock_settlement"] = stock_row
    return item


def update_assigned_stock_fact(
    prior: Mapping[str, Any],
    *,
    transition: Mapping[str, Any],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply one supported compact transition without reading lifetime history."""

    item = validate_assigned_stock_fact(prior)
    if not isinstance(transition, Mapping):
        raise CurrentDecisionProjectionError(
            "assigned-stock transition must be an object"
        )
    kind = str(transition.get("kind") or "").strip().lower()
    if kind == "exact_duplicate":
        if set(transition) != {"kind", "current_view_hash"} or (
            transition.get("current_view_hash") != item["current_view_hash"]
        ):
            raise CurrentDecisionProjectionError(
                "assigned-stock duplicate fact mismatch"
            )
        return item

    lots_by_id = {row["stock_lot_id"]: dict(row) for row in item["lots"]}
    if kind == "buy_settlement":
        if set(transition) != {
            "kind",
            "terminal_event_id",
            "terminal_type",
            "option_type",
            "position_side",
            "strike",
            "target_option_lot_id",
            "expected_contracts_open_after",
            "contracts",
            "multiplier",
            "account",
            "broker",
            "symbol",
            "currency",
            "stock_settlement",
            "strategy_fields",
        }:
            raise CurrentDecisionProjectionError(
                "assigned-stock buy transition shape is invalid"
            )
        settlement_input = dict(transition)
        strategy = settlement_input.pop("strategy_fields")
        settled = _settlement_transition(settlement_input, expected_side="buy")
        strategy_keys = {
            "strategy",
            "leg_role",
            "strategy_group_id",
            "yield_enhancement_mode",
            "source_option_leg_role",
        }
        if not isinstance(strategy, Mapping) or set(strategy) != strategy_keys:
            raise CurrentDecisionProjectionError(
                "assigned-stock strategy binding is invalid"
            )
        strategy_fields = {
            field: _optional_text(
                strategy[field],
                field=field,
                lower=field != "strategy_group_id",
            )
            for field in strategy_keys
        }
        if settled["account"] != item["account"]:
            raise CurrentDecisionProjectionError(
                "assigned-stock settlement account mismatch"
            )
        _require_final_option_lot(
            current_position_lots,
            target_lot_id=settled["target_option_lot_id"],
            expected_contracts_open=settled["expected_contracts_open_after"],
            settlement=settled,
        )
        stock = settled["stock_settlement"]
        stock_lot_id = f"assigned-stock-{settled['terminal_event_id']}"
        price = Decimal(str(stock["price"]))
        shares = int(stock["shares"])
        fees = Decimal(str(stock["fees"]))
        _count, empty_chain = _sale_fact_chain(())
        next_lot = {
            "stock_lot_id": stock_lot_id,
            "source_assignment_event_id": settled["terminal_event_id"],
            "source_option_lot_id": settled["target_option_lot_id"],
            "account": settled["account"],
            "broker": settled["broker"],
            "symbol": settled["symbol"],
            "currency": settled["currency"],
            "assigned_at_ms": stock["event_time_ms"],
            "shares_opened": shares,
            "shares_remaining": shares,
            "assignment_price": _decimal_text(price, field="assignment_price"),
            "remaining_cost_basis": _decimal_text(
                price * shares + fees,
                field="remaining_cost_basis",
            ),
            "basis_policy": "assignment_stock_cost_basis",
            **strategy_fields,
            "sale_fact_count": 0,
            "sale_fact_chain_sha256": empty_chain,
        }
        existing = lots_by_id.get(stock_lot_id)
        if existing is not None:
            if existing == next_lot:
                return item
            raise CurrentDecisionProjectionError(
                "assigned-stock deterministic lot conflict"
            )
        lots_by_id[stock_lot_id] = next_lot
        return _assigned_fact_with(item, lots=lots_by_id.values())

    if kind == "sell_settlement":
        settlement_input = dict(transition)
        stock_lot_id_raw = settlement_input.pop("stock_lot_id", None)
        settled = _settlement_transition(settlement_input, expected_side="sell")
        if stock_lot_id_raw is None:
            raise CurrentDecisionProjectionError(
                "assigned-stock sell transition shape is invalid"
            )
        stock_lot_id = _text(
            stock_lot_id_raw, field="stock_lot_id"
        )
        prior_lot = lots_by_id.get(stock_lot_id)
        if prior_lot is None or prior_lot["account"] != settled["account"]:
            raise CurrentDecisionProjectionError(
                "assigned-stock sell lot binding is missing"
            )
        _require_final_option_lot(
            current_position_lots,
            target_lot_id=settled["target_option_lot_id"],
            expected_contracts_open=settled["expected_contracts_open_after"],
            settlement=settled,
        )
        stock = settled["stock_settlement"]
        if (
            prior_lot["broker"] != settled["broker"]
            or prior_lot["symbol"] != settled["symbol"]
            or prior_lot["currency"] != settled["currency"]
            or int(stock["event_time_ms"]) < int(prior_lot["assigned_at_ms"])
        ):
            raise CurrentDecisionProjectionError(
                "assigned-stock sell identity or time mismatch"
            )
        remaining = int(prior_lot["shares_remaining"]) - int(stock["shares"])
        if remaining < 0:
            raise CurrentDecisionProjectionError(
                "assigned-stock sell exceeds remaining shares"
            )
        if remaining == 0:
            lots_by_id.pop(stock_lot_id)
        else:
            prior_basis = Decimal(str(prior_lot["remaining_cost_basis"]))
            prior_remaining = int(prior_lot["shares_remaining"])
            prior_lot["shares_remaining"] = remaining
            prior_lot["remaining_cost_basis"] = _decimal_text(
                round(float(prior_basis * remaining / prior_remaining), 6),
                field="remaining_cost_basis",
            )
            lots_by_id[stock_lot_id] = prior_lot
        active_open_event_ids = {
            str(fields.get("source_event_id") or fields.get("open_event_id") or "")
            for fields in _position_lot_fields(current_position_lots).values()
            if str(fields.get("status") or "").strip().lower() == "open"
            and int(fields.get("contracts_open") or 0) > 0
        }
        allocations = [
            row
            for row in item["covered_call_allocations"]
            if row["stock_lot_id"] in lots_by_id
            and row["open_event_id"] in active_open_event_ids
        ]
        return _assigned_fact_with(
            item,
            lots=lots_by_id.values(),
            allocations=allocations,
        )

    if kind == "assigned_stock_sale":
        required = {
            "kind",
            "stock_event_id",
            "stock_lot_id",
            "shares",
            "trade_time_ms",
            "lot_after",
        }
        if set(transition) != required:
            raise CurrentDecisionProjectionError(
                "assigned-stock sale transition shape is invalid"
            )
        stock_event_id = _text(
            transition["stock_event_id"], field="stock_event_id"
        )
        stock_lot_id = _text(
            transition["stock_lot_id"], field="stock_lot_id"
        )
        shares = _integer(transition["shares"], field="sale shares", minimum=1)
        trade_time_ms = _integer(
            transition["trade_time_ms"], field="sale trade_time_ms", minimum=1
        )
        prior_lot = lots_by_id.get(stock_lot_id)
        if prior_lot is None or trade_time_ms < int(prior_lot["assigned_at_ms"]):
            raise CurrentDecisionProjectionError(
                "assigned-stock sale lot is missing or backdated"
            )
        remaining = int(prior_lot["shares_remaining"]) - shares
        if remaining < 0:
            raise CurrentDecisionProjectionError(
                "assigned-stock sale exceeds remaining shares"
            )
        supplied_after = transition["lot_after"]
        if remaining == 0:
            if supplied_after is not None:
                raise CurrentDecisionProjectionError(
                    "closed assigned-stock sale after-view mismatch"
                )
            lots_by_id.pop(stock_lot_id)
        else:
            if not isinstance(supplied_after, Mapping):
                raise CurrentDecisionProjectionError(
                    "assigned-stock sale after-view is missing"
                )
            next_lot = dict(prior_lot)
            prior_basis = Decimal(str(prior_lot["remaining_cost_basis"]))
            prior_remaining = int(prior_lot["shares_remaining"])
            next_lot["shares_remaining"] = remaining
            next_lot["remaining_cost_basis"] = _decimal_text(
                round(float(prior_basis * remaining / prior_remaining), 6),
                field="remaining_cost_basis",
            )
            event_bytes = stock_event_id.encode("utf-8")
            next_lot["sale_fact_count"] = int(prior_lot["sale_fact_count"]) + 1
            next_lot["sale_fact_chain_sha256"] = hashlib.sha256(
                bytes.fromhex(str(prior_lot["sale_fact_chain_sha256"]))
                + len(event_bytes).to_bytes(4, "big")
                + event_bytes
            ).hexdigest()
            if dict(supplied_after) != next_lot:
                raise CurrentDecisionProjectionError(
                    "assigned-stock sale after-view mismatch"
                )
            lots_by_id[stock_lot_id] = next_lot
        if any(
            row["stock_lot_id"] == stock_lot_id
            and int(row["shares"]) > remaining
            for row in item["covered_call_allocations"]
        ):
            raise CurrentDecisionProjectionError(
                "assigned-stock sale conflicts with covered-call allocation"
            )
        return _assigned_fact_with(item, lots=lots_by_id.values())

    if kind == "covered_call_linkage":
        if set(transition) != {"kind", "allocations"}:
            raise CurrentDecisionProjectionError(
                "covered-call linkage transition shape is invalid"
            )
        allocations = transition["allocations"]
        if not isinstance(allocations, list):
            raise CurrentDecisionProjectionError(
                "covered-call linkage allocations must be a list"
            )
        active_open_events = {
            str(fields.get("source_event_id") or fields.get("open_event_id") or ""): fields
            for fields in _position_lot_fields(current_position_lots).values()
            if str(fields.get("status") or "").strip().lower() == "open"
            and int(fields.get("contracts_open") or 0) > 0
            and str(
                fields.get("source_event_id") or fields.get("open_event_id") or ""
            ).strip()
        }
        shares_by_stock_lot: dict[str, int] = {}
        shares_by_open_event: dict[str, int] = {}
        for raw in allocations:
            if not isinstance(raw, Mapping):
                raise CurrentDecisionProjectionError(
                    "covered-call linkage option identity is missing"
                )
            row = dict(raw)
            option = active_open_events.get(str(row.get("open_event_id") or ""))
            stock = lots_by_id.get(str(row.get("stock_lot_id") or ""))
            linkage_basis = _text(
                row.get("linkage_basis"), field="linkage_basis", lower=True
            )
            if option is None or stock is None or any(
                str(row.get(field) or "").strip().lower()
                != str(source.get(field) or "").strip().lower()
                for source in (stock, option)
                for field in ("account", "broker", "symbol", "currency")
            ) or (
                str(option.get("option_type") or "").strip().lower() != "call"
                or str(option.get("side") or "").strip().lower() != "short"
            ):
                raise CurrentDecisionProjectionError(
                    "covered-call linkage identity mismatch"
                )
            if linkage_basis not in _ASSIGNED_LINKAGE_BASES or (
                linkage_basis == "strategy_group"
                and (
                    not str(option.get("strategy_group_id") or "").strip()
                    or str(option.get("strategy_group_id") or "").strip()
                    != str(stock.get("strategy_group_id") or "").strip()
                )
            ):
                raise CurrentDecisionProjectionError(
                    "covered-call linkage basis mismatch"
                )
            shares = _integer(row.get("shares"), field="allocation shares", minimum=1)
            open_event_id = str(row["open_event_id"])
            shares_by_open_event[open_event_id] = (
                shares_by_open_event.get(open_event_id, 0) + shares
            )
            stock_lot_id = str(row["stock_lot_id"])
            shares_by_stock_lot[stock_lot_id] = (
                shares_by_stock_lot.get(stock_lot_id, 0) + shares
            )
        if any(
            shares > int(lots_by_id[stock_lot_id]["shares_remaining"])
            for stock_lot_id, shares in shares_by_stock_lot.items()
        ):
            raise CurrentDecisionProjectionError(
                "covered-call linkage stock quantity mismatch"
            )
        if any(
            shares
            > int(active_open_events[open_event_id].get("contracts_open") or 0)
            * int(active_open_events[open_event_id].get("multiplier") or 0)
            for open_event_id, shares in shares_by_open_event.items()
        ):
            raise CurrentDecisionProjectionError(
                "covered-call linkage option quantity mismatch"
            )
        return _assigned_fact_with(item, allocations=allocations)

    raise CurrentDecisionProjectionError(
        f"unsupported assigned-stock transition: {kind or 'missing'}"
    )


def _trade_event_field(event: Any, field: str) -> Any:
    return event.get(field) if isinstance(event, Mapping) else getattr(event, field, None)


def _trade_event_payload(event: Any) -> dict[str, Any]:
    value = _trade_event_field(event, "raw_payload")
    return dict(value) if isinstance(value, Mapping) else {}


def _trade_event_contract(event: Any) -> dict[str, Any]:
    value = _trade_event_field(event, "contract_key")
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else {}


def _positive_integral_number(value: Any, *, field: str) -> int:
    rendered = _decimal_text(value, field=field)
    assert rendered is not None
    number = Decimal(rendered)
    if number <= 0 or number != number.to_integral_value():
        raise CurrentDecisionProjectionError(f"{field} must be a positive integer")
    return int(number)


def _settlement_transition_from_event(
    event: Any,
    *,
    prior: Mapping[str, Any],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    event_type = str(_trade_event_field(event, "event_type") or "").strip().lower()
    if event_type not in {"assignment", "exercise"}:
        raise CurrentDecisionProjectionError("trade event is not a stock settlement")
    payload = _trade_event_payload(event)
    stock_raw = payload.get("stock_settlement")
    if not isinstance(stock_raw, Mapping) or not stock_raw:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement facts are missing"
        )
    stock = dict(stock_raw)
    contract = _trade_event_contract(event)
    target_lot_id = str(
        _trade_event_field(event, "target_lot_id")
        or payload.get("target_lot_id")
        or payload.get("record_id")
        or ""
    ).strip()
    final_fields = _position_lot_fields(current_position_lots).get(target_lot_id)
    if final_fields is None:
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option lot is missing"
        )
    event_time_ms = _integer(
        stock["event_time_ms"]
        if stock.get("event_time_ms") is not None
        else stock["trade_time_ms"]
        if stock.get("trade_time_ms") is not None
        else _trade_event_field(event, "event_time_ms"),
        field="stock event_time_ms",
        minimum=1,
    )
    opened_at_ms = _integer(
        final_fields.get("opened_at"),
        field="final option opened_at",
        minimum=1,
    )
    if event_time_ms < opened_at_ms:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement is backdated"
        )
    multiplier = _positive_integral_number(
        _trade_event_field(event, "multiplier"),
        field="multiplier",
    )
    shares = _integer(
        stock.get("shares") if stock.get("shares") is not None else stock.get("stock_qty"),
        field="stock shares",
        minimum=1,
    )
    expected_side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get(
        (
            event_type,
            str(contract.get("option_type") or "").strip().lower(),
            str(contract.get("position_side") or contract.get("side") or "")
            .strip()
            .lower(),
        )
    )
    if expected_side is None:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement option binding is invalid"
        )
    stock_price = (
        stock.get("price")
        if stock.get("price") is not None
        else stock.get("stock_price")
    )
    raw_stock_fee = (
        stock.get("fees") if stock.get("fees") is not None else stock.get("fee")
    )
    if raw_stock_fee is not None:
        _nonnegative_decimal_text(raw_stock_fee, field="stock fees")
    fee_fact = assigned_stock_fee_fact(
        {
            **stock,
            "account": contract.get("account"),
            "broker": contract.get("broker"),
            "symbol": contract.get("underlying_symbol") or contract.get("symbol"),
            "currency": stock.get("currency") or _trade_event_field(event, "currency"),
            "shares": shares,
            "price": stock_price,
        },
        component=f"{event_type}_stock_fee",
        transaction_kind="assignment" if expected_side == "buy" else "sale",
    )
    common = {
        "terminal_event_id": _text(
            _trade_event_field(event, "event_id"),
            field="terminal_event_id",
        ),
        "terminal_type": event_type,
        "option_type": _text(
            contract.get("option_type"), field="option_type", lower=True
        ),
        "position_side": _text(
            contract.get("position_side") or contract.get("side"),
            field="position_side",
            lower=True,
        ),
        "strike": contract.get("strike"),
        "target_option_lot_id": target_lot_id,
        "expected_contracts_open_after": _integer(
            final_fields.get("contracts_open"),
            field="final option contracts_open",
        ),
        "contracts": _integer(
            _trade_event_field(event, "contracts"),
            field="contracts",
            minimum=1,
        ),
        "multiplier": multiplier,
        "account": _text(contract.get("account"), field="account", lower=True),
        "broker": _text(contract.get("broker"), field="broker", lower=True),
        "symbol": _text(
            contract.get("underlying_symbol") or contract.get("symbol"),
            field="symbol",
            upper=True,
        ),
        "currency": _text(
            stock.get("currency") or _trade_event_field(event, "currency"),
            field="currency",
            upper=True,
        ),
        "stock_settlement": {
            "side": _text(
                stock.get("side") or stock.get("stock_side"),
                field="stock side",
                lower=True,
            ),
            "shares": shares,
            "price": stock_price,
            "event_time_ms": event_time_ms,
            "fees": fee_fact["amount"],
        },
    }
    if expected_side == "buy":
        group_id = str(
            payload.get("strategy_group_id")
            or final_fields.get("strategy_group_id")
            or ""
        ).strip() or None
        source_role = str(
            payload.get("leg_role") or final_fields.get("leg_role") or ""
        ).strip().lower() or None
        return {
            "kind": "buy_settlement",
            **common,
            "strategy_fields": {
                "strategy": str(
                    payload.get("strategy") or final_fields.get("strategy") or ""
                ).strip().lower() or None,
                "leg_role": "assigned_stock" if group_id else None,
                "strategy_group_id": group_id,
                "yield_enhancement_mode": str(
                    payload.get("yield_enhancement_mode")
                    or final_fields.get("yield_enhancement_mode")
                    or ""
                ).strip().lower() or None,
                "source_option_leg_role": source_role,
            },
        }
    if expected_side != "sell":
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement option binding is invalid"
        )

    explicit_stock_lot_id = next(
        (
            str(source.get(key) or "").strip()
            for source in (stock, payload)
            for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id")
            if str(source.get(key) or "").strip()
        ),
        None,
    )
    group_id = str(
        payload.get("strategy_group_id")
        or final_fields.get("strategy_group_id")
        or ""
    ).strip()
    candidates = [
        row
        for row in validate_assigned_stock_fact(prior)["lots"]
        if row["account"] == common["account"]
        and row["broker"] == common["broker"]
        and row["symbol"] == common["symbol"]
        and row["currency"] == common["currency"]
        and int(row["shares_remaining"]) >= shares
        and int(row["assigned_at_ms"]) <= event_time_ms
        and (not group_id or row["strategy_group_id"] == group_id)
    ]
    if explicit_stock_lot_id is not None:
        candidates = [
            row for row in candidates if row["stock_lot_id"] == explicit_stock_lot_id
        ]
    if len(candidates) != 1:
        raise CurrentDecisionProjectionError(
            "assigned-stock sell lot binding is not unique"
        )
    return {
        "kind": "sell_settlement",
        **common,
        "stock_lot_id": candidates[0]["stock_lot_id"],
    }


def _sync_covered_call_allocations(
    prior: Mapping[str, Any],
    *,
    event_mutations: Sequence[tuple[Any, bool]],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    item = validate_assigned_stock_fact(prior)
    lots_by_id = {row["stock_lot_id"]: dict(row) for row in item["lots"]}
    explicit_by_open_event: dict[str, str] = {}
    for event, created in event_mutations:
        if not created or str(_trade_event_field(event, "event_type") or "").strip().lower() != "open":
            continue
        payload = _trade_event_payload(event)
        explicit = next(
            (
                str(payload.get(key) or "").strip()
                for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id")
                if str(payload.get(key) or "").strip()
            ),
            None,
        )
        if explicit:
            explicit_by_open_event[
                _text(_trade_event_field(event, "event_id"), field="open_event_id")
            ] = explicit

    active_calls: list[tuple[str, str, dict[str, Any]]] = []
    for record_id, fields in _position_lot_fields(current_position_lots).items():
        if (
            str(fields.get("status") or "").strip().lower() == "open"
            and int(fields.get("contracts_open") or 0) > 0
            and str(fields.get("option_type") or "").strip().lower() == "call"
            and str(fields.get("side") or "").strip().lower() == "short"
        ):
            open_event_id = str(
                fields.get("source_event_id") or fields.get("open_event_id") or ""
            ).strip()
            if open_event_id:
                active_calls.append((open_event_id, record_id, fields))
    active_calls.sort(
        key=lambda row: (
            int(row[2].get("opened_at") or 0),
            row[0],
            row[1],
        )
    )

    remaining_by_stock = {
        lot_id: int(row["shares_remaining"]) for lot_id, row in lots_by_id.items()
    }
    identity_candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    group_candidates: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in lots_by_id.values():
        identity = (
            row["account"],
            row["broker"],
            row["symbol"],
            row["currency"],
        )
        for index, key in (
            (identity_candidates, identity),
            (group_candidates, (*identity, str(row.get("strategy_group_id") or ""))),
        ):
            candidates = index.setdefault(key, [])
            candidates.append(row)
            candidates.sort(
                key=lambda item: (
                    int(item["assigned_at_ms"]),
                    str(item["stock_lot_id"]),
                )
            )
            del candidates[2:]
    remaining_by_call: dict[str, int] = {}
    for open_event_id, _record_id, fields in active_calls:
        remaining_by_call[open_event_id] = (
            _integer(fields.get("contracts_open"), field="covered call contracts")
            * _positive_integral_number(
                fields.get("multiplier"), field="covered call multiplier"
            )
        )
    prior_linkages: dict[str, list[dict[str, Any]]] = {}
    for row in item["covered_call_allocations"]:
        prior_linkages.setdefault(str(row["open_event_id"]), []).append(row)

    allocations: list[dict[str, Any]] = []
    for open_event_id, _record_id, fields in active_calls:
        required = remaining_by_call[open_event_id]
        explicit = explicit_by_open_event.get(open_event_id)
        group_id = str(fields.get("strategy_group_id") or "").strip()
        opened_at = _integer(
            fields.get("opened_at"),
            field="covered call opened_at",
            minimum=1,
        )
        identity = (
            str(fields.get("account") or "").strip().lower(),
            str(fields.get("broker") or "").strip().lower(),
            str(fields.get("symbol") or "").strip().upper(),
            str(fields.get("currency") or "").strip().upper(),
        )
        base_candidates = [
            row
            for row in identity_candidates.get(identity, ())
            if int(row["assigned_at_ms"]) <= opened_at
        ]
        prior_links = prior_linkages.get(open_event_id, ())
        linkage_basis = "stock_lot_id" if explicit is not None else "strategy_group"
        if explicit is None and prior_links:
            prior_bases = {str(row["linkage_basis"]) for row in prior_links}
            prior_stock_ids = {str(row["stock_lot_id"]) for row in prior_links}
            if prior_bases == {"stock_lot_id"}:
                if len(prior_stock_ids) != 1:
                    raise CurrentDecisionProjectionError(
                        "covered-call linkage identity is not unique"
                    )
                explicit = next(iter(prior_stock_ids))
                linkage_basis = "stock_lot_id"
            elif prior_bases == {"strategy_group"}:
                if len(prior_stock_ids) != 1:
                    raise CurrentDecisionProjectionError(
                        "covered-call linkage identity is not unique"
                    )
                prior_group_id = str(
                    lots_by_id[next(iter(prior_stock_ids))].get(
                        "strategy_group_id"
                    )
                    or ""
                ).strip()
                if not prior_group_id or prior_group_id != group_id:
                    raise CurrentDecisionProjectionError(
                        "covered-call linkage identity mismatch"
                    )
            else:
                raise CurrentDecisionProjectionError(
                    "covered-call linkage basis is ambiguous"
                )
        if explicit is not None:
            candidate = lots_by_id.get(explicit)
            candidates = (
                [candidate]
                if candidate is not None
                and tuple(
                    candidate[field]
                    for field in ("account", "broker", "symbol", "currency")
                )
                == identity
                and int(candidate["assigned_at_ms"]) <= opened_at
                else []
            )
        elif not group_id:
            if base_candidates:
                raise CurrentDecisionProjectionError(
                    "covered-call linkage identity is missing"
                )
            continue
        else:
            candidates = [
                row
                for row in group_candidates.get((*identity, group_id), ())
                if int(row["assigned_at_ms"]) <= opened_at
            ]
            if base_candidates and not candidates:
                raise CurrentDecisionProjectionError(
                    "covered-call linkage identity mismatch"
                )
            if not base_candidates:
                continue
        if len(candidates) != 1:
            raise CurrentDecisionProjectionError(
                "covered-call linkage identity is not unique"
            )
        stock_lot_id = str(candidates[0]["stock_lot_id"])
        if remaining_by_stock[stock_lot_id] < required:
            raise CurrentDecisionProjectionError(
                "covered-call linkage stock quantity mismatch"
            )
        allocations.append(
            {
                "open_event_id": open_event_id,
                "stock_lot_id": stock_lot_id,
                "account": str(fields.get("account") or "").strip().lower(),
                "broker": str(fields.get("broker") or "").strip().lower(),
                "symbol": str(fields.get("symbol") or "").strip().upper(),
                "currency": str(fields.get("currency") or "").strip().upper(),
                "shares": required,
                "start_at_ms": opened_at,
                "end_at_ms": None,
                "allocation_status": "explicit",
                "linkage_basis": linkage_basis,
            }
        )
        remaining_by_stock[stock_lot_id] -= required
        remaining_by_call[open_event_id] = 0
    updated = update_assigned_stock_fact(
        item,
        transition={"kind": "covered_call_linkage", "allocations": allocations},
        current_position_lots=current_position_lots,
    )
    return _assigned_fact_with(
        updated,
        reviews=[
            row
            for row in updated["review_facts"]
            if row["status"] != "covered_call_unallocated"
        ],
    )


def advance_assigned_stock_fact_for_trade_events(
    prior: Mapping[str, Any],
    *,
    event_mutations: Sequence[tuple[Any, bool]],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply created settlement events, then refresh bounded current CC links."""

    item = validate_assigned_stock_fact(prior)
    for event, created in event_mutations:
        if not created:
            continue
        event_type = str(_trade_event_field(event, "event_type") or "").strip().lower()
        if event_type not in {"assignment", "exercise"}:
            continue
        item = update_assigned_stock_fact(
            item,
            transition=_settlement_transition_from_event(
                event,
                prior=item,
                current_position_lots=current_position_lots,
            ),
            current_position_lots=current_position_lots,
        )
    return _sync_covered_call_allocations(
        item,
        event_mutations=event_mutations,
        current_position_lots=current_position_lots,
    )


_COMBO_GROUP_KEYS = frozenset(
    {
        "schema_version",
        "group_id",
        "identity_hash",
        "account",
        "symbol",
        "strategy",
        "original_contracts",
        "expected_roles",
        "active_member_bindings",
        "assigned_stock_lot_ids",
        "status",
        "reason_codes",
        "fact_sha256",
    }
)
_COMBO_MEMBER_KEYS = frozenset(
    {
        "record_id",
        "role",
        "open_event_id",
        "account",
        "symbol",
        "contracts_open",
    }
)


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


def build_current_combo_facts(
    *,
    account: str,
    current_position_lots: Sequence[Mapping[str, Any]],
    identities: Sequence[Mapping[str, Any]],
    assigned_stock: Mapping[str, Any],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    assigned = validate_assigned_stock_fact(assigned_stock)
    if assigned["account"] != account_value:
        raise CurrentDecisionProjectionError("combo assigned-stock account mismatch")
    lots_by_id = _position_lot_fields(current_position_lots)
    assigned_by_group: dict[str, list[str]] = {}
    for lot in assigned["lots"]:
        group_id = str(lot.get("strategy_group_id") or "").strip()
        if group_id:
            assigned_by_group.setdefault(group_id, []).append(lot["stock_lot_id"])

    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for raw_identity in sorted(
        identities,
        key=lambda item: str(item.get("group_id") or ""),
    ):
        identity = dict(raw_identity)
        if str(identity.get("account") or "").strip().lower() != account_value:
            raise CurrentDecisionProjectionError("combo identity account mismatch")
        validation = validate_combo_identity(identity)
        if (
            validation.status != "valid"
            or validation.identity_hash != identity.get("identity_hash")
        ):
            raise CurrentDecisionProjectionError("combo identity is invalid")
        group_id = str(identity.get("group_id") or "").strip()
        if group_id in seen_groups:
            raise CurrentDecisionProjectionError("duplicate combo group identity")
        seen_groups.add(group_id)
        expected = (
            (
                str(identity["funding_put_record_id"]),
                str(identity["funding_put_open_event_id"]),
                "funding_put",
            ),
            (
                str(identity["participation_call_record_id"]),
                str(identity["participation_call_open_event_id"]),
                "participation_call",
            ),
        )
        bindings: list[dict[str, Any]] = []
        reasons: set[str] = set()
        put_open = 0
        call_open = 0
        put_terminal = 0
        call_terminal = 0
        original_contracts = int(identity["original_contracts"])
        for record_id, expected_event_id, expected_role in expected:
            fields = lots_by_id.get(record_id)
            if fields is None:
                continue
            contracts_open = _integer(
                fields.get("contracts_open"),
                field="combo contracts_open",
            )
            role = str(fields.get("leg_role") or "").strip().lower()
            open_event_id = str(
                fields.get("source_event_id") or fields.get("open_event_id") or ""
            ).strip()
            if (
                str(fields.get("account") or "").strip().lower() != account_value
                or str(fields.get("symbol") or "").strip().upper()
                != str(identity["symbol"])
                or str(fields.get("strategy_group_id") or "").strip() != group_id
                or open_event_id != expected_event_id
                or (
                    expected_role == "funding_put"
                    and role not in FUNDING_PUT_ROLES
                )
                or (
                    expected_role == "participation_call"
                    and role not in PARTICIPATION_CALL_ROLES
                )
            ):
                reasons.add("combo_current_member_binding_invalid")
            if contracts_open > original_contracts:
                reasons.add("combo_current_member_quantity_invalid")
            if contracts_open > 0:
                bindings.append(
                    {
                        "record_id": record_id,
                        "role": role,
                        "open_event_id": open_event_id,
                        "account": account_value,
                        "symbol": str(fields.get("symbol") or "").strip().upper(),
                        "contracts_open": contracts_open,
                    }
                )
            if expected_role == "funding_put":
                put_open = contracts_open
                put_terminal = max(0, original_contracts - contracts_open)
            else:
                call_open = contracts_open
                call_terminal = max(0, original_contracts - contracts_open)
        assigned_ids = sorted(assigned_by_group.get(group_id, ()))
        if not bindings and not assigned_ids:
            continue
        status = classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=put_open,
            participation_call_contracts_open=call_open,
            funding_put_terminal_allocated=put_terminal,
            participation_call_terminal_allocated=call_terminal,
            assigned_stock_contracts=1 if assigned_ids else 0,
            evidence_conflict=bool(reasons),
        )
        fact = {
            "schema_version": CURRENT_COMBO_GROUP_FACT_SCHEMA,
            "group_id": group_id,
            "identity_hash": str(identity["identity_hash"]),
            "account": account_value,
            "symbol": str(identity["symbol"]),
            "strategy": str(identity["strategy"]),
            "original_contracts": int(identity["original_contracts"]),
            "expected_roles": ["funding_put", "participation_call"],
            "active_member_bindings": sorted(
                bindings,
                key=lambda item: item["record_id"],
            ),
            "assigned_stock_lot_ids": assigned_ids,
            "status": status,
            "reason_codes": sorted(reasons),
        }
        fact["fact_sha256"] = _fact_hash(fact)
        groups.append(validate_current_combo_group_fact(fact))
    result = {
        "schema_version": CURRENT_COMBO_SCHEMA,
        "current_groups": groups,
    }
    result["current_groups_hash"] = canonical_sha256(result)
    return validate_current_combo_facts(result)


def validate_current_combo_group_fact(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _COMBO_GROUP_KEYS:
        raise CurrentDecisionProjectionError("current combo group shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_COMBO_GROUP_FACT_SCHEMA:
        raise CurrentDecisionProjectionError("current combo group schema is invalid")
    _text(item["group_id"], field="combo group_id")
    _sha256(item["identity_hash"], field="combo identity_hash")
    _text(item["account"], field="combo account", lower=True)
    _text(item["symbol"], field="combo symbol", upper=True)
    _text(item["strategy"], field="combo strategy", lower=True)
    _integer(item["original_contracts"], field="combo original_contracts", minimum=1)
    if item["expected_roles"] != ["funding_put", "participation_call"]:
        raise CurrentDecisionProjectionError("combo expected roles are invalid")
    bindings = item["active_member_bindings"]
    if not isinstance(bindings, list):
        raise CurrentDecisionProjectionError("combo member bindings must be a list")
    binding_ids: list[str] = []
    for raw in bindings:
        if not isinstance(raw, Mapping) or set(raw) != _COMBO_MEMBER_KEYS:
            raise CurrentDecisionProjectionError("combo member binding shape is invalid")
        binding = dict(raw)
        binding_ids.append(_text(binding["record_id"], field="combo record_id"))
        _text(binding["role"], field="combo role", lower=True)
        _text(binding["open_event_id"], field="combo open_event_id")
        if binding["account"] != item["account"]:
            raise CurrentDecisionProjectionError("combo binding account mismatch")
        if binding["symbol"] != item["symbol"]:
            raise CurrentDecisionProjectionError("combo binding symbol mismatch")
        _integer(binding["contracts_open"], field="combo contracts_open", minimum=1)
    if binding_ids != sorted(set(binding_ids)):
        raise CurrentDecisionProjectionError("combo member bindings are not canonical")
    _text_list(item["assigned_stock_lot_ids"], field="assigned_stock_lot_ids")
    _text(item["status"], field="combo status", lower=True)
    _text_list(item["reason_codes"], field="combo reason_codes")
    if _sha256(item["fact_sha256"], field="combo fact_sha256") != _fact_hash(item):
        raise CurrentDecisionProjectionError("combo group fact hash mismatch")
    return item


def validate_current_combo_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "current_groups", "current_groups_hash"}
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise CurrentDecisionProjectionError("current combo facts shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_COMBO_SCHEMA:
        raise CurrentDecisionProjectionError("current combo facts schema is invalid")
    groups = item["current_groups"]
    if not isinstance(groups, list):
        raise CurrentDecisionProjectionError("current combo groups must be a list")
    group_ids = [
        validate_current_combo_group_fact(group)["group_id"]
        for group in groups
    ]
    if group_ids != sorted(set(group_ids)):
        raise CurrentDecisionProjectionError("current combo groups are not canonical")
    if (
        _sha256(item["current_groups_hash"], field="current_groups_hash")
        != _hash_without(item, "current_groups_hash")
    ):
        raise CurrentDecisionProjectionError("current combo facts hash mismatch")
    return item


def arbitrate_lifecycle_case_facts(
    *,
    account: str,
    case_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    facts_by_id: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    for raw in case_facts:
        fact = validate_lifecycle_case_decision_fact(raw)
        case_id = str(fact["case_id"])
        if fact["account"] != account_value or case_id in facts_by_id:
            raise CurrentDecisionProjectionError("lifecycle case fact account or id mismatch")
        facts_by_id[case_id] = fact
        resolution = fact["resolution"]
        resolutions[case_id] = {
            "resolver_schema_version": LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
            "case_id": case_id,
            "status": resolution["status"],
            "anchor_facts": resolution["anchor_facts"],
            "requested_reservations_by_lot": resolution[
                "requested_reservations_by_lot"
            ],
            "effective_reservations_by_lot": resolution[
                "effective_reservations_by_lot"
            ],
            "reason_codes": resolution["contested_reason_codes"],
        }
        resolutions[case_id]["resolution_hash"] = _hash_without(
            resolutions[case_id],
            "resolution_hash",
        )
    arbitration = arbitrate_lifecycle_case_resolutions(
        account=account_value,
        case_resolutions=resolutions,
    )
    effective_facts: list[dict[str, Any]] = []
    for resolution in arbitration["case_resolutions"]:
        case_id = str(resolution["case_id"])
        fact = dict(facts_by_id[case_id])
        fact_resolution = dict(fact["resolution"])
        fact_resolution.update(
            {
                "status": resolution["status"],
                "effective_reservations_by_lot": dict(
                    resolution.get("effective_reservations_by_lot") or {}
                ),
                "contested_reason_codes": list(
                    resolution.get("reason_codes") or []
                ),
            }
        )
        fact["resolution"] = fact_resolution
        fact["generation"] = dict(fact["generation"])
        fact["generation"]["generation_token"] = (
            _lifecycle_case_current_generation_token(fact)
        )
        fact["fact_sha256"] = _fact_hash(fact)
        effective_facts.append(validate_lifecycle_case_decision_fact(fact))
    result = {
        "schema_version": ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA,
        "account": account_value,
        "operational_cases": effective_facts,
        "contested_components": arbitration["contested_components"],
        "arbitration_hash": arbitration["arbitration_hash"],
    }
    result["operational_cases_hash"] = canonical_sha256(effective_facts)
    return result


def _synthetic_allocations(fact: Mapping[str, Any]) -> list[dict[str, Any]]:
    case_id = str(fact["case_id"])
    resolved_by_lot = dict(fact["resolution"]["resolved_contracts_by_lot"])
    remaining_by_type = dict(
        fact["resolution"]["resolved_contracts_by_terminal_type"]
    )
    allocations: list[dict[str, Any]] = []
    index = 0
    for lot_id in sorted(resolved_by_lot):
        lot_remaining = int(resolved_by_lot[lot_id])
        for terminal_type in sorted(remaining_by_type):
            quantity = min(lot_remaining, int(remaining_by_type[terminal_type]))
            if quantity <= 0:
                continue
            index += 1
            evidence_id = f"current-decision:{case_id}:{index}"
            allocations.append(
                {
                    "allocation_id": allocation_id_for(
                        case_id=case_id,
                        evidence_id=evidence_id,
                        target_lot_id=lot_id,
                    ),
                    "case_id": case_id,
                    "evidence_id": evidence_id,
                    "target_lot_id": lot_id,
                    "terminal_type": terminal_type,
                    "contracts_allocated": quantity,
                    "canonical_terminal_event_id": terminal_event_id_for(
                        case_id=case_id,
                        evidence_id=evidence_id,
                        target_lot_id=lot_id,
                        terminal_type=terminal_type,
                        contracts_allocated=quantity,
                    ),
                }
            )
            lot_remaining -= quantity
            remaining_by_type[terminal_type] -= quantity
        if lot_remaining:
            raise CurrentDecisionProjectionError("lifecycle allocation matrix is incoherent")
    if any(remaining_by_type.values()):
        raise CurrentDecisionProjectionError("lifecycle terminal totals are incoherent")
    return allocations


def derive_lifecycle_case_current_view(
    case_fact: Mapping[str, Any],
    *,
    current_position_lots: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    fact = validate_lifecycle_case_decision_fact(case_fact)
    lots = _position_lot_fields(current_position_lots)
    remaining = fact["resolution"]["remaining_contracts_by_lot"]
    quantity_drift = any(
        lot_id not in lots
        or int(lots[lot_id].get("contracts_open") or 0) != expected
        for lot_id, expected in remaining.items()
    )
    conflicts = set(fact["resolution"]["contested_reason_codes"])
    if fact["resolution"]["status"] == "conflict" and not conflicts:
        conflicts.add("lifecycle_compact_resolution_conflict")
    timing = fact["timing"]
    model = derive_lifecycle_read_model(
        expiration_ymd=fact["contract"]["expiration_ymd"],
        market=fact["market"],
        target_contracts_by_lot=fact["target_contracts_by_lot"],
        allocations=_synthetic_allocations(fact),
        accepted_option_close_contracts_by_lot=fact["resolution"][
            "effective_reservations_by_lot"
        ],
        now_ms=_integer(now_ms, field="now_ms", minimum=1),
        conflict_reason_codes=tuple(sorted(conflicts)),
        quantity_drift=quantity_drift,
        observation_start_ms_override=timing["observation_start_ms"],
        pending_until_ms_override=(
            timing["settlement_deadline_ms"] or timing["pending_until_ms"]
        ),
    )
    persisted_status = str(fact["status"])
    persisted_reason = str(fact["decision"]["reason_state"])
    reason_state = (
        persisted_reason
        if persisted_status in {"needs_review", "conflict"}
        and persisted_reason in {"needs_review", "conflict"}
        else model.reason_state
    )
    close_reason = (
        fact["decision"]["close_reason"]
        if reason_state in {"needs_review", "conflict"}
        else model.close_reason
    )
    if model.lifecycle_state == "conflict":
        evidence_status = "conflict"
    elif fact["resolution"]["status"] in {"direct", "bridged"} and any(
        model.reserved_contracts_by_lot.values()
    ):
        evidence_status = "closure_observed_cause_pending"
    elif not fact["resolution"]["anchor_facts"] and not any(
        model.resolved_contracts_by_lot.values()
    ):
        evidence_status = "missing"
    elif any(model.remaining_contracts_by_lot.values()):
        evidence_status = "partial"
    else:
        evidence_status = "complete"
    return {
        "schema_version": "option_lifecycle_read_model.v3",
        "lifecycle_case_id": fact["case_id"],
        "lifecycle_state": model.lifecycle_state,
        "lifecycle_evidence_status": evidence_status,
        "lifecycle_reason_codes": sorted(
            {
                *model.lifecycle_reason_codes,
                *fact["decision"]["reason_codes"],
            }
        ),
        "observation_start_ms": model.observation_start_ms,
        "pending_until_ms": model.pending_until_ms,
        "timing_policy_hash": timing["timing_policy_hash"],
        "target_contracts_by_lot": fact["target_contracts_by_lot"],
        "resolved_contracts_by_lot": model.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": model.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            model.resolved_contracts_by_terminal_type
        ),
        "reserved_contracts_by_lot": model.reserved_contracts_by_lot,
        "closure_fact": model.closure_fact,
        "reason_state": reason_state,
        "close_reason": close_reason,
        "lifecycle_generation_token": fact["generation"]["generation_token"],
        "actionable": model.actionable
        and reason_state
        not in {"cause_pending", "partially_resolved", "needs_review", "conflict"},
    }


def lifecycle_views_by_lot(
    lifecycle: Mapping[str, Any],
    *,
    current_position_lots: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    case_views: dict[str, dict[str, Any]] = {}
    lot_views: dict[str, dict[str, Any]] = {}
    for raw in lifecycle.get("operational_cases") or []:
        view = derive_lifecycle_case_current_view(
            raw,
            current_position_lots=current_position_lots,
            now_ms=now_ms,
        )
        case_id = str(view["lifecycle_case_id"])
        case_views[case_id] = view
        for lot_id in sorted(view["target_contracts_by_lot"]):
            if lot_id not in lot_views:
                lot_views[lot_id] = dict(view)
                continue
            prior = lot_views[lot_id]
            lot_views[lot_id] = {
                **prior,
                "lifecycle_state": "conflict",
                "lifecycle_case_ids": sorted(
                    {
                        str(prior.get("lifecycle_case_id") or ""),
                        case_id,
                    }
                    - {""}
                ),
                "lifecycle_evidence_status": "conflict",
                "lifecycle_reason_codes": sorted(
                    {
                        *prior.get("lifecycle_reason_codes", []),
                        *view.get("lifecycle_reason_codes", []),
                        "reservation_target_overlap",
                    }
                ),
                "reason_state": "conflict",
                "actionable": False,
            }
    return lot_views, case_views


def _quality_detail(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = validate_lifecycle_case_decision_fact(fact)
    return {
        "case_id": item["case_id"],
        "market": item["market"],
        "status": item["status"],
        "trust_class": item["decision"]["quality_trust_class"],
        "evidence_count": item["evidence"]["count"],
        "settlement_deadline_ms": item["timing"]["settlement_deadline_ms"],
        "reason_state": item["decision"]["reason_state"],
        "timing_policy_hash": item["timing"]["timing_policy_hash"],
    }


def _quality_aggregate(
    case_facts: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for raw in case_facts:
        fact = validate_lifecycle_case_decision_fact(raw)
        market = str(fact["market"])
        bucket = buckets.setdefault(
            market,
            {
                "market": market,
                "total_case_count": 0,
                "status_counts": {},
                "trust_class_counts": {},
            },
        )
        bucket["total_case_count"] += 1
        status = str(fact["status"])
        trust = str(fact["decision"]["quality_trust_class"])
        bucket["status_counts"][status] = bucket["status_counts"].get(status, 0) + 1
        bucket["trust_class_counts"][trust] = (
            bucket["trust_class_counts"].get(trust, 0) + 1
        )
    return [
        {
            **bucket,
            "status_counts": dict(sorted(bucket["status_counts"].items())),
            "trust_class_counts": dict(
                sorted(bucket["trust_class_counts"].items())
            ),
        }
        for _market, bucket in sorted(buckets.items())
    ]


def build_lifecycle_quality_fact(
    *,
    account: str,
    all_case_facts: Sequence[Mapping[str, Any]],
    operational_case_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if any(
        not isinstance(item, Mapping)
        or str(item.get("account") or "").strip().lower() != account_value
        for item in (*all_case_facts, *operational_case_facts)
    ):
        raise CurrentDecisionProjectionError("lifecycle quality account mismatch")
    details = sorted(
        (_quality_detail(item) for item in operational_case_facts),
        key=lambda item: item["case_id"],
    )
    result = {
        "schema_version": CURRENT_LIFECYCLE_QUALITY_SCHEMA,
        "account": account_value,
        "aggregate_by_market": _quality_aggregate(all_case_facts),
        "operational_cases": details,
    }
    result["aggregate_fingerprint"] = canonical_sha256(
        result["aggregate_by_market"]
    )
    result["detail_fingerprint"] = canonical_sha256(details)
    return validate_lifecycle_quality_fact(result)


def update_lifecycle_quality_fact(
    prior: Mapping[str, Any],
    *,
    case_mutations: Sequence[
        tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]
    ],
    operational_case_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prior_value = validate_lifecycle_quality_fact(prior)
    counts: dict[str, dict[str, Any]] = {
        row["market"]: {
            "market": row["market"],
            "total_case_count": row["total_case_count"],
            "status_counts": dict(row["status_counts"]),
            "trust_class_counts": dict(row["trust_class_counts"]),
        }
        for row in prior_value["aggregate_by_market"]
    }

    def apply(fact: Mapping[str, Any], delta: int) -> None:
        item = validate_lifecycle_case_decision_fact(fact)
        if item["account"] != prior_value["account"]:
            raise CurrentDecisionProjectionError("lifecycle quality account mismatch")
        market = str(item["market"])
        bucket = counts.setdefault(
            market,
            {
                "market": market,
                "total_case_count": 0,
                "status_counts": {},
                "trust_class_counts": {},
            },
        )
        bucket["total_case_count"] += delta
        for field, value in (
            ("status_counts", str(item["status"])),
            (
                "trust_class_counts",
                str(item["decision"]["quality_trust_class"]),
            ),
        ):
            bucket[field][value] = bucket[field].get(value, 0) + delta
            if bucket[field][value] == 0:
                del bucket[field][value]
            elif bucket[field][value] < 0:
                raise CurrentDecisionProjectionError("lifecycle quality count underflow")
        if bucket["total_case_count"] < 0:
            raise CurrentDecisionProjectionError("lifecycle quality total underflow")

    seen_cases: set[str] = set()
    for old, new in case_mutations:
        identities = {
            str(item.get("case_id") or "").strip()
            for item in (old, new)
            if isinstance(item, Mapping)
        }
        if len(identities) != 1:
            raise CurrentDecisionProjectionError("lifecycle quality mutation id mismatch")
        case_id = next(iter(identities))
        if case_id in seen_cases:
            raise CurrentDecisionProjectionError("duplicate lifecycle quality mutation")
        seen_cases.add(case_id)
        if old is not None:
            apply(old, -1)
        if new is not None:
            apply(new, 1)
    aggregate = [
        {
            **bucket,
            "status_counts": dict(sorted(bucket["status_counts"].items())),
            "trust_class_counts": dict(
                sorted(bucket["trust_class_counts"].items())
            ),
        }
        for market, bucket in sorted(counts.items())
        if bucket["total_case_count"]
    ]
    if any(
        not isinstance(item, Mapping)
        or str(item.get("account") or "").strip().lower()
        != prior_value["account"]
        for item in operational_case_facts
    ):
        raise CurrentDecisionProjectionError("lifecycle quality account mismatch")
    details = sorted(
        (_quality_detail(item) for item in operational_case_facts),
        key=lambda item: item["case_id"],
    )
    result = {
        "schema_version": CURRENT_LIFECYCLE_QUALITY_SCHEMA,
        "account": prior_value["account"],
        "aggregate_by_market": aggregate,
        "operational_cases": details,
        "aggregate_fingerprint": canonical_sha256(aggregate),
        "detail_fingerprint": canonical_sha256(details),
    }
    return validate_lifecycle_quality_fact(result)


def validate_lifecycle_quality_fact(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "account",
        "aggregate_by_market",
        "operational_cases",
        "aggregate_fingerprint",
        "detail_fingerprint",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise CurrentDecisionProjectionError("lifecycle quality shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_LIFECYCLE_QUALITY_SCHEMA:
        raise CurrentDecisionProjectionError("lifecycle quality schema is invalid")
    _text(item["account"], field="quality account", lower=True)
    aggregates = item["aggregate_by_market"]
    if not isinstance(aggregates, list):
        raise CurrentDecisionProjectionError("quality aggregates must be a list")
    markets: list[str] = []
    for raw in aggregates:
        keys = {
            "market",
            "total_case_count",
            "status_counts",
            "trust_class_counts",
        }
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise CurrentDecisionProjectionError("quality aggregate shape is invalid")
        row = dict(raw)
        markets.append(_text(row["market"], field="quality market", upper=True))
        total = _integer(row["total_case_count"], field="total_case_count", minimum=1)
        status_counts = _integer_map(row["status_counts"], field="status_counts", positive=True)
        trust_counts = _integer_map(
            row["trust_class_counts"],
            field="trust_class_counts",
            positive=True,
        )
        if sum(status_counts.values()) != total or sum(trust_counts.values()) != total:
            raise CurrentDecisionProjectionError("quality aggregate total mismatch")
    if markets != sorted(set(markets)):
        raise CurrentDecisionProjectionError("quality aggregates are not canonical")
    details = item["operational_cases"]
    if not isinstance(details, list):
        raise CurrentDecisionProjectionError("quality details must be a list")
    detail_ids: list[str] = []
    for raw in details:
        keys = {
            "case_id",
            "market",
            "status",
            "trust_class",
            "evidence_count",
            "settlement_deadline_ms",
            "reason_state",
            "timing_policy_hash",
        }
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise CurrentDecisionProjectionError("quality detail shape is invalid")
        row = dict(raw)
        detail_ids.append(_text(row["case_id"], field="quality case_id"))
        _text(row["market"], field="quality detail market", upper=True)
        _text(row["status"], field="quality detail status", lower=True)
        if row["trust_class"] not in {"trusted", "legacy_gap", "external_review"}:
            raise CurrentDecisionProjectionError("quality detail trust class is invalid")
        _integer(row["evidence_count"], field="quality evidence_count")
        _optional_integer(
            row["settlement_deadline_ms"],
            field="quality settlement_deadline_ms",
            minimum=1,
        )
        _text(row["reason_state"], field="quality reason_state", lower=True)
        _sha256(
            row["timing_policy_hash"],
            field="quality timing_policy_hash",
            optional=True,
        )
    if detail_ids != sorted(set(detail_ids)):
        raise CurrentDecisionProjectionError("quality details are not canonical")
    if (
        _sha256(item["aggregate_fingerprint"], field="aggregate_fingerprint")
        != canonical_sha256(aggregates)
        or _sha256(item["detail_fingerprint"], field="detail_fingerprint")
        != canonical_sha256(details)
    ):
        raise CurrentDecisionProjectionError("lifecycle quality hash mismatch")
    return item


def derive_lifecycle_quality_view(
    quality: Mapping[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    stored = validate_lifecycle_quality_fact(quality)
    verdict_counts: dict[str, int] = {}
    blocked_counts: dict[str, int] = {}
    verdict_counts_by_market: dict[str, dict[str, int]] = {}
    blocked_counts_by_market: dict[str, dict[str, int]] = {}
    operational_trust: dict[str, dict[str, int]] = {}
    operational_status: dict[str, dict[str, int]] = {}
    details: list[dict[str, Any]] = []

    def add_counts(
        market: str,
        verdict: str,
        blocked: Sequence[str],
        count: int = 1,
    ) -> None:
        if count <= 0:
            return
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + count
        market_verdicts = verdict_counts_by_market.setdefault(market, {})
        market_verdicts[verdict] = market_verdicts.get(verdict, 0) + count
        market_blocked = blocked_counts_by_market.setdefault(market, {})
        for consumer in blocked:
            blocked_counts[consumer] = blocked_counts.get(consumer, 0) + count
            market_blocked[consumer] = market_blocked.get(consumer, 0) + count

    for item in stored["operational_cases"]:
        trust = item["trust_class"]
        market = item["market"]
        market_trust = operational_trust.setdefault(market, {})
        market_trust[trust] = market_trust.get(trust, 0) + 1
        status = item["status"]
        market_status = operational_status.setdefault(market, {})
        market_status[status] = market_status.get(status, 0) + 1
        deadline = item["settlement_deadline_ms"]
        if trust == "external_review":
            verdict = "unavailable"
            blocked = ["close_advice", "lifecycle_report", "option_performance"]
        elif trust == "legacy_gap":
            verdict = "untrusted"
            blocked = ["option_performance"]
        elif item["status"] == "ledger_written":
            verdict = "trusted"
            blocked = []
        elif deadline is None:
            verdict = "unavailable"
            blocked = ["close_advice", "lifecycle_report", "option_performance"]
        elif now_ms <= deadline and status != "conflict":
            verdict = "partial"
            blocked = []
        else:
            verdict = "untrusted"
            blocked = ["close_advice", "lifecycle_report", "option_performance"]
        add_counts(market, verdict, blocked)
        details.append({**item, "dataset_status": verdict, "blocked_consumers": blocked})

    aggregate_markets: set[str] = set()
    terminal_classification = {
        "trusted": ("trusted", ()),
        "legacy_gap": ("untrusted", ("option_performance",)),
        "external_review": (
            "unavailable",
            ("close_advice", "lifecycle_report", "option_performance"),
        ),
    }
    for aggregate in stored["aggregate_by_market"]:
        market = aggregate["market"]
        aggregate_markets.add(market)
        terminal_total = 0
        for trust, total in aggregate["trust_class_counts"].items():
            if trust not in terminal_classification:
                raise CurrentDecisionProjectionError(
                    "quality aggregate trust class is invalid"
                )
            count = int(total) - operational_trust.get(market, {}).get(trust, 0)
            if count < 0:
                raise CurrentDecisionProjectionError(
                    "quality operational trust count exceeds aggregate"
                )
            terminal_total += count
            verdict, blocked = terminal_classification[trust]
            add_counts(market, verdict, blocked, count)
        status_terminal_total = 0
        for status, total in aggregate["status_counts"].items():
            count = int(total) - operational_status.get(market, {}).get(status, 0)
            if count < 0:
                raise CurrentDecisionProjectionError(
                    "quality operational status count exceeds aggregate"
                )
            if count and status in _OPERATIONAL_STATUSES:
                raise CurrentDecisionProjectionError(
                    "quality operational status is missing detail"
                )
            status_terminal_total += count
        if terminal_total != status_terminal_total:
            raise CurrentDecisionProjectionError(
                "quality terminal aggregate count mismatch"
            )
    if (set(operational_trust) | set(operational_status)) - aggregate_markets:
        raise CurrentDecisionProjectionError(
            "quality operational market is missing from aggregate"
        )
    return {
        **stored,
        "aggregate_by_market": [
            {
                **aggregate,
                "dataset_status_counts": dict(
                    sorted(
                        verdict_counts_by_market.get(
                            aggregate["market"],
                            {},
                        ).items()
                    )
                ),
                "blocked_consumer_counts": dict(
                    sorted(
                        blocked_counts_by_market.get(
                            aggregate["market"],
                            {},
                        ).items()
                    )
                ),
            }
            for aggregate in stored["aggregate_by_market"]
        ],
        "operational_cases": details,
        "operational_status_counts": dict(sorted(verdict_counts.items())),
        "blocked_consumer_counts": dict(sorted(blocked_counts.items())),
    }


_PROJECTION_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "normalized_account",
        "projector_implementation_fingerprint",
        "position_binding",
        "source_bindings",
        "lifecycle",
        "combo",
        "assigned_stock",
        "lifecycle_quality",
        "decision_state_fingerprint",
        "updated_at_ms",
    }
)
_POSITION_BINDING_KEYS = frozenset(
    {
        "projector_schema",
        "projector_implementation_fingerprint",
        "position_source_generation",
        "position_lots_generation",
        "position_lots_fingerprint",
        "lot_count",
        "active_lot_count",
    }
)
_SOURCE_BINDING_KEYS = frozenset(_GENERATION_FIELDS)
_LIFECYCLE_KEYS = frozenset(
    {
        "schema_version",
        "account",
        "operational_cases",
        "contested_components",
        "arbitration_hash",
        "operational_cases_hash",
    }
)


def validate_current_lifecycle_facts(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _LIFECYCLE_KEYS:
        raise CurrentDecisionProjectionError("current lifecycle shape is invalid")
    item = dict(payload)
    if item["schema_version"] != ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA:
        raise CurrentDecisionProjectionError("current lifecycle schema is invalid")
    account = _text(item["account"], field="lifecycle account", lower=True)
    facts = item["operational_cases"]
    if not isinstance(facts, list):
        raise CurrentDecisionProjectionError("operational lifecycle cases must be a list")
    case_ids: list[str] = []
    for raw in facts:
        fact = validate_lifecycle_case_decision_fact(raw)
        if fact["account"] != account:
            raise CurrentDecisionProjectionError("operational lifecycle account mismatch")
        case_ids.append(str(fact["case_id"]))
    if case_ids != sorted(set(case_ids)):
        raise CurrentDecisionProjectionError("operational lifecycle cases are not canonical")
    rebuilt = arbitrate_lifecycle_case_facts(account=account, case_facts=facts)
    if item != rebuilt:
        raise CurrentDecisionProjectionError("current lifecycle arbitration mismatch")
    return item


def _decision_state_fingerprint(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"decision_state_fingerprint", "updated_at_ms"}
        }
    )


def validate_current_decision_projection_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _PROJECTION_PAYLOAD_KEYS:
        raise CurrentDecisionProjectionError("current decision payload shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_DECISION_PROJECTION_SCHEMA:
        raise CurrentDecisionProjectionError("current decision schema is invalid")
    account = _text(item["normalized_account"], field="account", lower=True)
    implementation = _sha256(
        item["projector_implementation_fingerprint"],
        field="projector implementation fingerprint",
    )

    binding = item["position_binding"]
    if not isinstance(binding, Mapping) or set(binding) != _POSITION_BINDING_KEYS:
        raise CurrentDecisionProjectionError("position binding shape is invalid")
    if binding["projector_schema"] != POSITION_PROJECTION_SCHEMA:
        raise CurrentDecisionProjectionError("position binding schema is invalid")
    if (
        _sha256(
            binding["projector_implementation_fingerprint"],
            field="position implementation fingerprint",
        )
        != implementation
    ):
        raise CurrentDecisionProjectionError("position implementation mismatch")
    for field in (
        "position_source_generation",
        "position_lots_generation",
        "lot_count",
        "active_lot_count",
    ):
        _integer(binding[field], field=field)
    if int(binding["active_lot_count"]) > int(binding["lot_count"]):
        raise CurrentDecisionProjectionError("active lot count exceeds lot count")
    _sha256(
        binding["position_lots_fingerprint"],
        field="position lots fingerprint",
    )

    sources = item["source_bindings"]
    if not isinstance(sources, Mapping) or set(sources) != _SOURCE_BINDING_KEYS:
        raise CurrentDecisionProjectionError("decision source binding shape is invalid")
    for field in _GENERATION_FIELDS:
        _integer(sources[field], field=f"source_bindings.{field}")

    lifecycle = validate_current_lifecycle_facts(item["lifecycle"])
    combo = validate_current_combo_facts(item["combo"])
    assigned = validate_assigned_stock_fact(item["assigned_stock"])
    quality = validate_lifecycle_quality_fact(item["lifecycle_quality"])
    if lifecycle["account"] != account or assigned["account"] != account:
        raise CurrentDecisionProjectionError("current decision account mismatch")
    if quality["account"] != account or any(
        group["account"] != account for group in combo["current_groups"]
    ):
        raise CurrentDecisionProjectionError("current decision nested account mismatch")
    _integer(item["updated_at_ms"], field="updated_at_ms", minimum=1)
    if (
        _sha256(
            item["decision_state_fingerprint"],
            field="decision_state_fingerprint",
        )
        != _decision_state_fingerprint(item)
    ):
        raise CurrentDecisionProjectionError("decision state fingerprint mismatch")
    return item


def encode_current_decision_projection(
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    item = validate_current_decision_projection_payload(payload)
    payload_bytes = _canonical_json_bytes(item)
    return payload_bytes.decode("utf-8"), _sha256_bytes(payload_bytes)


def current_decision_projection_row(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    item = validate_current_decision_projection_payload(payload)
    payload_json, payload_sha256 = encode_current_decision_projection(item)
    binding = item["position_binding"]
    sources = item["source_bindings"]
    return {
        "account": item["normalized_account"],
        "projection_schema": item["schema_version"],
        "projector_implementation_fingerprint": item[
            "projector_implementation_fingerprint"
        ],
        "built_position_source_generation": binding[
            "position_source_generation"
        ],
        "built_position_lots_generation": binding["position_lots_generation"],
        "position_lots_fingerprint": binding["position_lots_fingerprint"],
        "built_decision_input_generation": sources["generation"],
        "built_case_generation": sources["case_generation"],
        "built_evidence_generation": sources["evidence_generation"],
        "built_allocation_generation": sources["allocation_generation"],
        "built_source_consumption_generation": sources[
            "source_consumption_generation"
        ],
        "built_timing_generation": sources["timing_generation"],
        "built_combo_identity_generation": sources[
            "combo_identity_generation"
        ],
        "built_assigned_stock_generation": sources[
            "assigned_stock_generation"
        ],
        "decision_state_fingerprint": item["decision_state_fingerprint"],
        "payload_sha256": payload_sha256,
        "payload_json": payload_json,
        "updated_at_ms": item["updated_at_ms"],
    }


def encode_lifecycle_case_decision_fact(
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    item = validate_lifecycle_case_decision_fact(payload)
    return _canonical_json_bytes(item).decode("utf-8"), str(item["fact_sha256"])


def read_lifecycle_case_decision_fact(
    repo: SQLiteOptionPositionsRepository,
    *,
    case_id: str,
    conn: Any,
) -> dict[str, Any] | None:
    row = repo.get_current_decision_lifecycle_fact_state(case_id, conn=conn)
    if row is None or row.get("decision_fact_json") is None:
        return None
    return _stored_case_fact(row, account=str(row["account"]))


def write_lifecycle_case_decision_fact(
    repo: SQLiteOptionPositionsRepository,
    *,
    fact: Mapping[str, Any],
    conn: Any,
) -> bool:
    item = validate_lifecycle_case_decision_fact(fact)
    fact_json, fact_hash = encode_lifecycle_case_decision_fact(item)
    return repo.update_trade_lifecycle_case_decision_fact(
        case_id=str(item["case_id"]),
        account=str(item["account"]),
        status=str(item["status"]),
        decision_fact_json=fact_json,
        decision_fact_sha256=fact_hash,
        conn=conn,
    )


def _decode_projection_row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    stored = dict(row)
    payload_json = stored.get("payload_json")
    if not isinstance(payload_json, str):
        raise CurrentDecisionProjectionError("stored decision payload must be text")
    try:
        decoded = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise CurrentDecisionProjectionError("stored decision payload is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise CurrentDecisionProjectionError("stored decision payload must be an object")
    payload = validate_current_decision_projection_payload(decoded)
    canonical_json, payload_sha256 = encode_current_decision_projection(payload)
    if canonical_json != payload_json or stored.get("payload_sha256") != payload_sha256:
        raise CurrentDecisionProjectionError("stored decision payload bytes mismatch")
    expected = current_decision_projection_row(payload)
    for field, value in expected.items():
        if field == "payload_json":
            continue
        if stored.get(field) != value:
            raise CurrentDecisionProjectionError(
                f"stored decision projection field mismatch: {field}"
            )
    return payload


def _required_current_inputs(
    *,
    account: str,
    current_inputs: Mapping[str, Any],
    implementation_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    account_value = _text(account, field="account", lower=True)
    inputs = dict(current_inputs)
    source = inputs.get("source")
    head = inputs.get("head")
    generation = inputs.get("generation")
    lots = inputs.get("lots")
    if not isinstance(source, Mapping) or not isinstance(head, Mapping):
        raise CurrentDecisionProjectionError("trusted position projection is missing")
    if not isinstance(generation, Mapping):
        raise CurrentDecisionProjectionError("decision input generation is missing")
    if not isinstance(lots, list) or any(not isinstance(item, Mapping) for item in lots):
        raise CurrentDecisionProjectionError("current position lots are invalid")
    source_row, head_row, generation_row = dict(source), dict(head), dict(generation)
    implementation = _sha256(
        implementation_fingerprint,
        field="projector implementation fingerprint",
    )
    checks = (
        source_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
        head_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
        source_row.get("projector_implementation_fingerprint") == implementation,
        head_row.get("projector_implementation_fingerprint") == implementation,
        head_row.get("status") == "trusted",
        head_row.get("built_source_generation") == source_row.get("source_generation"),
        head_row.get("built_lots_generation") == head_row.get("lots_generation"),
        source_row.get("sqlite_schema_cookie") == inputs.get("schema_cookie"),
        head_row.get("projection_fingerprint") == inputs.get("lots_fingerprint"),
        head_row.get("lot_count") == inputs.get("lot_count"),
        generation_row.get("account") == account_value,
    )
    if not all(checks):
        raise CurrentDecisionProjectionError("current projection inputs are not trusted")
    for field in (
        "source_generation",
        "lots_generation",
        "built_source_generation",
        "built_lots_generation",
        "lot_count",
    ):
        row = source_row if field == "source_generation" else head_row
        _integer(row.get(field), field=field)
    for field in _GENERATION_FIELDS:
        _integer(generation_row.get(field), field=field)
    _sha256(inputs.get("lots_fingerprint"), field="position lots fingerprint")
    return source_row, head_row, generation_row, [dict(item) for item in lots]


def _active_lot_ids(current_position_lots: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        record_id
        for record_id, fields in _position_lot_fields(current_position_lots).items()
        if str(fields.get("status") or "").strip().lower() == "open"
        and int(fields.get("contracts_open") or 0) > 0
    }


def _referenced_case_fact(
    fact: Mapping[str, Any],
    *,
    referenced_lot_ids: set[str],
) -> bool:
    return (
        str(fact["status"]) in _OPERATIONAL_STATUSES
        or bool(set(fact["target_contracts_by_lot"]) & referenced_lot_ids)
        or bool(fact["resolution"]["requested_reservations_by_lot"])
        or bool(fact["resolution"]["effective_reservations_by_lot"])
    )


def build_current_decision_projection_payload(
    *,
    account: str,
    current_inputs: Mapping[str, Any],
    case_facts: Sequence[Mapping[str, Any]],
    assigned_stock: Mapping[str, Any],
    lifecycle_quality: Mapping[str, Any],
    updated_at_ms: int,
    implementation_fingerprint: str | None = None,
) -> dict[str, Any]:
    account_value = _text(account, field="account", lower=True)
    implementation = implementation_fingerprint
    if implementation is None:
        try:
            implementation = loaded_projector_implementation_fingerprint()
        except ProjectorImplementationUnavailable as exc:
            raise CurrentDecisionProjectionError(
                "projector implementation is unavailable"
            ) from exc
    source, head, generation, lots = _required_current_inputs(
        account=account_value,
        current_inputs=current_inputs,
        implementation_fingerprint=str(implementation),
    )
    assigned = validate_assigned_stock_fact(assigned_stock)
    if assigned["account"] != account_value:
        raise CurrentDecisionProjectionError("assigned stock account mismatch")
    active_lot_ids = _active_lot_ids(lots)
    referenced_lot_ids = {
        *active_lot_ids,
        *(
            str(item["source_option_lot_id"])
            for item in assigned["lots"]
            if item["source_option_lot_id"] is not None
        ),
    }
    validated_case_facts = [
        validate_lifecycle_case_decision_fact(item) for item in case_facts
    ]
    selected = [
        item
        for item in validated_case_facts
        if _referenced_case_fact(item, referenced_lot_ids=referenced_lot_ids)
    ]
    lifecycle = arbitrate_lifecycle_case_facts(
        account=account_value,
        case_facts=selected,
    )
    quality = update_lifecycle_quality_fact(
        lifecycle_quality,
        case_mutations=(),
        operational_case_facts=lifecycle["operational_cases"],
    )
    combo = build_current_combo_facts(
        account=account_value,
        current_position_lots=lots,
        identities=list(current_inputs.get("identities") or []),
        assigned_stock=assigned,
    )
    payload = {
        "schema_version": CURRENT_DECISION_PROJECTION_SCHEMA,
        "normalized_account": account_value,
        "projector_implementation_fingerprint": str(implementation),
        "position_binding": {
            "projector_schema": POSITION_PROJECTION_SCHEMA,
            "projector_implementation_fingerprint": str(implementation),
            "position_source_generation": int(source["source_generation"]),
            "position_lots_generation": int(head["lots_generation"]),
            "position_lots_fingerprint": str(current_inputs["lots_fingerprint"]),
            "lot_count": int(current_inputs["lot_count"]),
            "active_lot_count": len(active_lot_ids),
        },
        "source_bindings": {
            field: int(generation[field]) for field in _GENERATION_FIELDS
        },
        "lifecycle": lifecycle,
        "combo": combo,
        "assigned_stock": assigned,
        "lifecycle_quality": quality,
        "updated_at_ms": _integer(
            updated_at_ms,
            field="updated_at_ms",
            minimum=1,
        ),
    }
    payload["decision_state_fingerprint"] = _decision_state_fingerprint(payload)
    return validate_current_decision_projection_payload(payload)


def _stored_case_fact(row: Mapping[str, Any], *, account: str) -> dict[str, Any]:
    stored = dict(row)
    raw_json = stored.get("decision_fact_json")
    raw_hash = stored.get("decision_fact_sha256")
    if not isinstance(raw_json, str) or not isinstance(raw_hash, str):
        raise CurrentDecisionProjectionError("lifecycle decision fact is missing")
    try:
        decoded = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise CurrentDecisionProjectionError("lifecycle decision fact JSON is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise CurrentDecisionProjectionError("lifecycle decision fact must be an object")
    fact = validate_lifecycle_case_decision_fact(decoded)
    canonical_json, fact_hash = encode_lifecycle_case_decision_fact(fact)
    if canonical_json != raw_json or fact_hash != raw_hash:
        raise CurrentDecisionProjectionError("lifecycle decision fact bytes mismatch")
    if (
        fact["case_id"] != stored.get("case_id")
        or fact["account"] != account
        or fact["account"] != stored.get("account")
        or fact["status"] != stored.get("status")
    ):
        raise CurrentDecisionProjectionError("lifecycle decision fact row mismatch")
    revision = stored.get("evidence_revision")
    count = stored.get("evidence_count")
    if revision is None and count is None:
        revision, count = 0, 0
    if fact["evidence"]["revision"] != revision or fact["evidence"]["count"] != count:
        raise CurrentDecisionProjectionError("lifecycle evidence revision mismatch")
    admission = (
        stored.get("admitted_semantic_schema"),
        stored.get("admitted_semantic_fingerprint"),
        stored.get("admitted_evidence_id"),
    )
    embedded = (
        fact["evidence"]["admitted_semantic_schema"],
        fact["evidence"]["admitted_semantic_fingerprint"],
        fact["evidence"]["admitted_evidence_id"],
    )
    if admission != embedded:
        raise CurrentDecisionProjectionError("lifecycle admission binding mismatch")
    return fact


def _prior_referenced_lot_ids(payload: Mapping[str, Any] | None) -> set[str]:
    if payload is None:
        return set()
    item = validate_current_decision_projection_payload(payload)
    return {
        *(
            lot_id
            for fact in item["lifecycle"]["operational_cases"]
            for lot_id in fact["target_contracts_by_lot"]
        ),
        *(
            str(lot["source_option_lot_id"])
            for lot in item["assigned_stock"]["lots"]
            if lot["source_option_lot_id"] is not None
        ),
        *(
            str(binding["record_id"])
            for group in item["combo"]["current_groups"]
            for binding in group["active_member_bindings"]
        ),
    }


def build_current_decision_projection(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    updated_at_ms: int,
    conn: Any | None = None,
    current_inputs: Mapping[str, Any] | None = None,
    case_mutations: Sequence[
        tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]
    ] = (),
    assigned_stock_after: Mapping[str, Any] | None = None,
    all_quality_case_facts: Sequence[Mapping[str, Any]] | None = None,
    implementation_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build from bounded current rows; lifetime readers are intentionally absent."""

    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_value = _text(account, field="account", lower=True)
    inputs = dict(
        current_inputs
        if current_inputs is not None
        else repo.read_current_decision_projection_inputs(account_value, conn=conn)
    )
    prior_payload = (
        _decode_projection_row_payload(inputs["projection"])
        if isinstance(inputs.get("projection"), Mapping)
        else None
    )
    assigned = (
        validate_assigned_stock_fact(assigned_stock_after)
        if assigned_stock_after is not None
        else (
            validate_assigned_stock_fact(prior_payload["assigned_stock"])
            if prior_payload is not None
            else None
        )
    )
    if assigned is None or assigned["account"] != account_value:
        raise CurrentDecisionProjectionError("trusted assigned-stock fact is required")

    mutation_rows: list[
        tuple[dict[str, Any] | None, dict[str, Any] | None]
    ] = []
    target_ids = {
        *_position_lot_fields(list(inputs.get("lots") or [])).keys(),
        *_prior_referenced_lot_ids(prior_payload),
        *(
            str(item["source_option_lot_id"])
            for item in assigned["lots"]
            if item["source_option_lot_id"] is not None
        ),
    }
    mutation_case_ids: set[str] = set()
    for old_raw, new_raw in case_mutations:
        old = (
            validate_lifecycle_case_decision_fact(old_raw)
            if old_raw is not None
            else None
        )
        new = (
            validate_lifecycle_case_decision_fact(new_raw)
            if new_raw is not None
            else None
        )
        case_ids = {
            str(item["case_id"]) for item in (old, new) if item is not None
        }
        if len(case_ids) != 1 or any(
            item is not None and item["account"] != account_value
            for item in (old, new)
        ):
            raise CurrentDecisionProjectionError("case mutation binding is invalid")
        case_id = next(iter(case_ids))
        if case_id in mutation_case_ids:
            raise CurrentDecisionProjectionError("duplicate case mutation")
        mutation_case_ids.add(case_id)
        for item in (old, new):
            if item is not None:
                target_ids.update(item["target_contracts_by_lot"])
        mutation_rows.append((old, new))

    facts_by_id = {
        fact["case_id"]: fact
        for fact in (
            _stored_case_fact(row, account=account_value)
            for row in repo.list_current_decision_lifecycle_fact_rows(
                account=account_value,
                target_lot_ids=sorted(target_ids),
                conn=conn,
            )
        )
    }
    for old, new in mutation_rows:
        case_id = str((new or old)["case_id"])  # type: ignore[index]
        if new is None:
            facts_by_id.pop(case_id, None)
        else:
            facts_by_id[case_id] = new

    facts = [facts_by_id[case_id] for case_id in sorted(facts_by_id)]
    if all_quality_case_facts is not None:
        quality = build_lifecycle_quality_fact(
            account=account_value,
            all_case_facts=all_quality_case_facts,
            operational_case_facts=facts,
        )
    elif prior_payload is not None:
        quality = update_lifecycle_quality_fact(
            prior_payload["lifecycle_quality"],
            case_mutations=mutation_rows,
            operational_case_facts=facts,
        )
    else:
        raise CurrentDecisionProjectionError("trusted lifecycle quality fact is required")
    return build_current_decision_projection_payload(
        account=account_value,
        current_inputs=inputs,
        case_facts=facts,
        assigned_stock=assigned,
        lifecycle_quality=quality,
        updated_at_ms=updated_at_ms,
        implementation_fingerprint=implementation_fingerprint,
    )


def _oracle_assigned_stock_report(
    rows: Mapping[str, Any],
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    from src.application.ledger.event_codec import import_stored_trade_events
    from src.application.ledger.event_codec import valid_void_target_event_id
    from src.application.ledger.publisher import (
        project_stored_trade_events_to_position_lots,
    )

    event_rows = [
        dict(item)
        for item in rows.get("trade_events") or []
        if int(item.get("event_time_ms") or item.get("trade_time_ms") or 0)
        <= now_ms
        or valid_void_target_event_id(item) is not None
    ]
    events, diagnostics = import_stored_trade_events(event_rows)
    projected = project_stored_trade_events_to_position_lots(event_rows)
    del diagnostics
    current_fields_by_lot_id = {
        item.record_id: item.fields for item in projected.lots
    }
    return project_assigned_stock_lifecycle(
        [assigned_stock_trade_event_row(event) for event in events],
        assignment_option_rows=[
            assigned_stock_allocation_row(item)
            for item in projected.ledger_projection.allocations
        ],
        option_open_lots=[
            assigned_stock_position_lot_row(
                item,
                current_fields=current_fields_by_lot_id.get(item.lot_id),
                at_ms=now_ms,
            )
            for item in projected.ledger_projection.lots
        ],
        assigned_stock_events=[
            dict(item)
            for item in rows.get("account_assigned_stock_events") or []
            if isinstance(item, Mapping)
            and assigned_stock_event_time_ms(item) <= now_ms
        ],
        quote_snapshots=[],
        stock_holdings=None,
        account_norm=account,
        broker_norm=None,
        month=None,
        as_of_ms=now_ms,
    )


def _oracle_lifecycle_case_facts(
    rows: Mapping[str, Any],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    resolution = resolve_lifecycle_account_rows(rows)
    case_ids = [
        str(item.get("case_id") or "")
        for item in resolution.get("generation_tokens") or []
        if isinstance(item, Mapping)
    ]
    from src.application.ledger.queries import (
        lifecycle_case_coherent_facts_many_from_account_snapshot,
    )

    materialized = lifecycle_case_coherent_facts_many_from_account_snapshot(
        {**rows, "account_lifecycle_resolution": resolution},
        case_ids=case_ids,
    )
    revisions = rows.get("account_lifecycle_evidence_revisions")
    admissions = rows.get("account_lifecycle_settlement_admission_heads")
    if not isinstance(revisions, Mapping) or not isinstance(admissions, Mapping):
        raise CurrentDecisionProjectionError(
            "oracle lifecycle revision facts are unavailable"
        )
    case_facts: list[dict[str, Any]] = []
    for case_id in case_ids:
        facts = materialized[case_id]
        case_evidence = list(facts["case_evidence"])
        revision = revisions.get(case_id)
        if revision is None:
            revision_value, evidence_count = 0, len(case_evidence)
        elif isinstance(revision, Mapping):
            revision_value = _integer(
                revision.get("revision"), field="evidence revision"
            )
            raw_count = revision.get("evidence_count")
            evidence_count = (
                len(case_evidence)
                if raw_count is None
                else _integer(raw_count, field="evidence count")
            )
        else:
            raise CurrentDecisionProjectionError(
                "oracle lifecycle revision fact is invalid"
            )
        if evidence_count != len(case_evidence):
            raise CurrentDecisionProjectionError(
                "oracle lifecycle evidence count mismatch"
            )
        lifecycle_case = dict(facts["lifecycle_case"])
        case_resolution = dict(facts["case_resolution"])
        timing_policy = (
            dict(facts["timing_policy"])
            if isinstance(facts.get("timing_policy"), Mapping)
            else None
        )
        try:
            compact_model = derive_lifecycle_read_model(
                expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
                market=str(
                    lifecycle_case.get("market")
                    or symbol_market(lifecycle_case.get("symbol"))
                    or ""
                ),
                target_contracts_by_lot=dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                ),
                allocations=list(facts["case_allocations"]),
                void_event_ids=list(facts["effective_void_event_ids"]),
                accepted_option_close_contracts_by_lot=dict(
                    case_resolution.get("effective_reservations_by_lot") or {}
                ),
                now_ms=now_ms,
                observation_start_ms_override=(
                    int(lifecycle_case["observation_start_ms"])
                    if lifecycle_case.get("observation_start_ms") is not None
                    else None
                ),
                pending_until_ms_override=(
                    int(timing_policy["settlement_deadline_ms"])
                    if timing_policy is not None
                    and timing_policy.get("settlement_deadline_ms") is not None
                    else None
                ),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise CurrentDecisionProjectionError(
                "oracle lifecycle read model is invalid"
            ) from exc
        read_model = {
            "lifecycle_case_id": case_id,
            "resolved_contracts_by_lot": compact_model.resolved_contracts_by_lot,
            "remaining_contracts_by_lot": compact_model.remaining_contracts_by_lot,
            "resolved_contracts_by_terminal_type": (
                compact_model.resolved_contracts_by_terminal_type
            ),
            "observation_start_ms": compact_model.observation_start_ms,
            "pending_until_ms": compact_model.pending_until_ms,
            "timing_policy_hash": (
                _sha256_bytes(_canonical_json_bytes(timing_policy))
                if timing_policy is not None
                else None
            ),
        }
        admission = admissions.get(case_id)
        if admission is not None and not isinstance(admission, Mapping):
            raise CurrentDecisionProjectionError(
                "oracle lifecycle admission fact is invalid"
            )
        case_facts.append(
            build_lifecycle_case_decision_fact(
                lifecycle_case=lifecycle_case,
                case_resolution=case_resolution,
                generation_token=dict(facts["generation_token"]),
                read_model=read_model,
                evidence_revision=revision_value,
                evidence_count=evidence_count,
                admission_head=(dict(admission) if admission is not None else None),
            )
        )
    return case_facts


def _current_decision_projection_oracle(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    now_ms: int,
    assigned_stock_report: Mapping[str, Any] | None,
    conn: Any | None = None,
    allow_schema_cookie_mismatch: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build explicit O(history) migration facts without publishing them."""

    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_value = _text(account, field="account", lower=True)
    instant = _integer(now_ms, field="now_ms", minimum=1)
    owned = conn is None
    active_conn = conn or repo._connect()
    try:
        if owned:
            active_conn.execute("BEGIN")
        rows = repo.read_lifecycle_account_rows(
            account=account_value,
            conn=active_conn,
        )
        current_inputs = repo.read_current_decision_projection_inputs(
            account_value,
            conn=active_conn,
        )
    finally:
        if owned:
            active_conn.rollback()
            active_conn.close()
    if allow_schema_cookie_mismatch and isinstance(
        current_inputs.get("source"), Mapping
    ):
        current_inputs["schema_cookie"] = current_inputs["source"].get(
            "sqlite_schema_cookie"
        )
    if current_inputs.get("generation") is None:
        current_inputs["generation"] = {
            "account": account_value,
            **{field: 0 for field in _GENERATION_FIELDS},
            "updated_at_ms": instant,
        }

    case_facts = _oracle_lifecycle_case_facts(rows, now_ms=instant)

    assigned = compact_assigned_stock_view(
        assigned_stock_report
        if assigned_stock_report is not None
        else _oracle_assigned_stock_report(
            rows,
            account=account_value,
            now_ms=instant,
        ),
        account=account_value,
        current_position_lots=list(current_inputs.get("lots") or []),
    )
    quality = build_lifecycle_quality_fact(
        account=account_value,
        all_case_facts=case_facts,
        operational_case_facts=case_facts,
    )
    return (
        build_current_decision_projection_payload(
            account=account_value,
            current_inputs=current_inputs,
            case_facts=case_facts,
            assigned_stock=assigned,
            lifecycle_quality=quality,
            updated_at_ms=instant,
        ),
        case_facts,
    )


def preview_current_decision_projection_oracle(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    now_ms: int,
    assigned_stock_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the explicit O(history) comparison payload without publishing it."""

    return _current_decision_projection_oracle(
        repo,
        account=account,
        now_ms=now_ms,
        assigned_stock_report=assigned_stock_report,
    )[0]


_DECISION_MIGRATION_REQUIRED_TABLES = (
    "trade_events",
    "position_lots",
    "position_projection_source_state",
    "position_projection_heads",
    "assigned_stock_events",
    "trade_lifecycle_cases",
    "trade_lifecycle_evidence",
    "trade_lifecycle_evidence_revisions",
    "trade_lifecycle_settlement_admission_heads",
    "trade_lifecycle_allocations",
    "trade_lifecycle_source_consumptions",
    "trade_lifecycle_timing_policies",
    "strategy_group_identities",
    "current_decision_input_generations",
    "current_decision_projections",
    "trade_lifecycle_case_targets",
)
_DECISION_MIGRATION_REQUIRED_INDEXES = (
    "idx_position_lots_account_record",
    "idx_assigned_stock_events_account_time",
    "idx_trade_lifecycle_cases_account_status",
    "idx_trade_lifecycle_case_targets_account_lot",
    "idx_strategy_group_identities_account",
)
_DECISION_MIGRATION_REQUIRED_TRIGGERS = (
    "trg_current_decision_assigned_stock_account_insert_guard",
    "trg_current_decision_assigned_stock_account_update_guard",
    "trg_current_decision_assigned_stock_account_delete_guard",
    "trg_current_decision_lifecycle_case_fact_insert_guard",
    "trg_current_decision_lifecycle_case_fact_update_guard",
    "trg_current_decision_case_target_guard",
    "trg_current_decision_case_target_update_guard",
    *(
        f"trg_current_decision_{label}_{operation}"
        for label in (
            "lifecycle_case",
            "lifecycle_evidence",
            "lifecycle_allocation",
            "lifecycle_source_consumption",
            "lifecycle_timing",
            "combo_identity",
            "assigned_stock",
        )
        for operation in ("insert", "update", "delete")
    ),
)
_DECISION_MIGRATION_AUTHORITY_QUERIES = (
    (
        "trade_events",
        "SELECT event_id,account,event_json,trade_time_ms,created_at_ms,updated_at_ms "
        "FROM trade_events ORDER BY trade_time_ms,event_id",
    ),
    (
        "position_lots",
        "SELECT record_id,account,fields_json,source_event_id,expiration,strike,"
        "multiplier,updated_at_ms FROM position_lots ORDER BY record_id",
    ),
    (
        "position_projection_source_state",
        "SELECT singleton_id,source_generation,projector_schema,"
        "projector_implementation_fingerprint,checkpoint_mode,"
        "last_full_verified_source_generation FROM position_projection_source_state "
        "ORDER BY singleton_id",
    ),
    (
        "position_projection_heads",
        "SELECT account,lots_generation,built_source_generation,built_lots_generation,"
        "projection_fingerprint,lot_count,projector_schema,"
        "projector_implementation_fingerprint,status FROM position_projection_heads "
        "ORDER BY account",
    ),
    (
        "assigned_stock_events",
        "SELECT stock_event_id,event_json,trade_time_ms,created_at_ms,updated_at_ms "
        "FROM assigned_stock_events ORDER BY trade_time_ms,stock_event_id",
    ),
    (
        "trade_lifecycle_cases",
        "SELECT case_id,case_key,account,broker,symbol,option_type,position_side,"
        "strike,expiration_ymd,contract_key,status,decision_type,target_lot_ids_json,"
        "target_contracts_by_lot_json,observation_start_ms,pending_until_ms,"
        "created_at_ms,updated_at_ms,raw_json FROM trade_lifecycle_cases "
        "ORDER BY case_id",
    ),
    (
        "trade_lifecycle_evidence",
        "SELECT evidence_id,case_id,source_type,source_event_id,evidence_type,account,"
        "symbol,raw_json,created_at_ms FROM trade_lifecycle_evidence "
        "ORDER BY created_at_ms,evidence_id",
    ),
    (
        "trade_lifecycle_evidence_revisions",
        "SELECT lifecycle_case.case_id,coalesce(revision.revision,0) AS revision "
        "FROM trade_lifecycle_cases AS lifecycle_case "
        "LEFT JOIN trade_lifecycle_evidence_revisions AS revision "
        "ON revision.case_id=lifecycle_case.case_id ORDER BY lifecycle_case.case_id",
    ),
    (
        "trade_lifecycle_settlement_admission_heads",
        "SELECT * FROM trade_lifecycle_settlement_admission_heads ORDER BY case_id",
    ),
    (
        "trade_lifecycle_allocations",
        "SELECT * FROM trade_lifecycle_allocations ORDER BY allocation_id",
    ),
    (
        "trade_lifecycle_source_consumptions",
        "SELECT * FROM trade_lifecycle_source_consumptions ORDER BY source_key",
    ),
    (
        "trade_lifecycle_timing_policies",
        "SELECT * FROM trade_lifecycle_timing_policies ORDER BY case_id",
    ),
    (
        "strategy_group_identities",
        "SELECT * FROM strategy_group_identities ORDER BY group_id",
    ),
)


def _migration_rows_fingerprint(
    conn: Any,
    queries: Sequence[tuple[str, str]],
) -> tuple[str, dict[str, int], int]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    payload_bytes = 0
    for name, query in queries:
        if not _position_migration._table_exists(conn, name):
            counts[name] = 0
            digest.update(_canonical_json_bytes({"table": name, "missing": True}))
            continue
        count = 0
        digest.update(_canonical_json_bytes({"table": name}))
        for row in conn.execute(query):
            payload = _canonical_json_bytes(dict(row))
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
            payload_bytes += len(payload)
        counts[name] = count
    return digest.hexdigest(), counts, payload_bytes


def _migration_source_inventory(conn: Any) -> dict[str, Any]:
    accounts: set[str] = set()
    reasons: list[str] = []
    assigned_accounts: dict[str, str] = {}
    assigned_null_count = 0
    cases: dict[str, dict[str, Any]] = {}
    targets: dict[str, tuple[tuple[str, str, str, int | None], ...]] = {}

    for row in conn.execute(
        "SELECT account FROM position_projection_heads ORDER BY account"
    ):
        account = str(row["account"] or "").strip()
        if not account or account != account.lower():
            reasons.append("position_head_account_invalid")
        else:
            accounts.add(account)

    for row in conn.execute(
        "SELECT case_id,account,raw_json FROM trade_lifecycle_cases ORDER BY case_id"
    ):
        case_id = str(row["case_id"] or "").strip()
        account = str(row["account"] or "").strip()
        try:
            payload = json.loads(str(row["raw_json"] or ""))
            if not isinstance(payload, dict):
                raise ValueError
            if (
                not case_id
                or not account
                or account != account.lower()
                or str(payload.get("case_id") or "").strip() != case_id
                or str(payload.get("account") or "").strip() != account
            ):
                raise ValueError
            normalized = _normalized_lifecycle_case_targets(
                payload,
                case_id=case_id,
                account=account,
            )[2]
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.append(f"lifecycle_case_invalid:{case_id or 'unknown'}")
            continue
        accounts.add(account)
        cases[case_id] = payload
        targets[case_id] = normalized

    for row in conn.execute(
        "SELECT stock_event_id,account,event_json FROM assigned_stock_events "
        "ORDER BY stock_event_id"
    ):
        stock_event_id = str(row["stock_event_id"] or "").strip()
        try:
            payload = json.loads(str(row["event_json"] or ""))
            if not isinstance(payload, dict):
                raise ValueError
            account = str(payload.get("account") or "").strip()
            if not account or account != account.lower():
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.append(
                f"assigned_stock_event_invalid:{stock_event_id or 'unknown'}"
            )
            continue
        stored = row["account"]
        if stored is None:
            assigned_null_count += 1
        elif str(stored) != account:
            reasons.append(f"assigned_stock_account_conflict:{stock_event_id}")
        accounts.add(account)
        assigned_accounts[stock_event_id] = account

    for table in (
        "trade_events",
        "position_lots",
        "strategy_group_identities",
        "current_decision_input_generations",
        "current_decision_projections",
    ):
        for row in conn.execute(f"SELECT account FROM {table} ORDER BY account"):
            account = str(row["account"] or "").strip()
            if not account or account != account.lower():
                reasons.append(f"{table}_account_invalid")
            else:
                accounts.add(account)

    evidence_counts = {
        str(row["case_id"]): int(row["evidence_count"] or 0)
        for row in conn.execute(
            "SELECT lifecycle_case.case_id,count(evidence.evidence_id) AS evidence_count "
            "FROM trade_lifecycle_cases AS lifecycle_case "
            "LEFT JOIN trade_lifecycle_evidence AS evidence "
            "ON evidence.case_id=lifecycle_case.case_id "
            "GROUP BY lifecycle_case.case_id ORDER BY lifecycle_case.case_id"
        )
    }
    return {
        "accounts": tuple(sorted(accounts)),
        "cases": cases,
        "targets": targets,
        "assigned_accounts": assigned_accounts,
        "assigned_null_count": assigned_null_count,
        "evidence_counts": evidence_counts,
        "reasons": reasons,
    }


def _migration_state_summary(
    conn: Any,
    *,
    accounts: Sequence[str],
    payloads: Mapping[str, Mapping[str, Any]],
    facts: Mapping[str, Mapping[str, Any]],
    targets: Mapping[str, Sequence[tuple[str, str, str, int | None]]],
    assigned_accounts: Mapping[str, str],
    evidence_counts: Mapping[str, int],
    implementation: str,
) -> dict[str, Any]:
    indexes = _position_migration._object_names(conn, "index")
    triggers = _position_migration._object_names(conn, "trigger")
    source = conn.execute(
        "SELECT * FROM position_projection_source_state WHERE singleton_id=1"
    ).fetchone()
    missing_indexes = sorted(set(_DECISION_MIGRATION_REQUIRED_INDEXES) - indexes)
    missing_triggers = sorted(set(_DECISION_MIGRATION_REQUIRED_TRIGGERS) - triggers)
    assigned_mismatch = sum(
        1
        for row in conn.execute(
            "SELECT stock_event_id,account FROM assigned_stock_events "
            "ORDER BY stock_event_id"
        )
        if row["account"] != assigned_accounts.get(str(row["stock_event_id"]))
    )
    target_mismatch = 0
    for case_id, expected in targets.items():
        actual = tuple(
            (str(row["case_id"]), str(row["account"]), str(row["target_lot_id"]), row["target_contracts"])
            for row in conn.execute(
                "SELECT case_id,account,target_lot_id,target_contracts "
                "FROM trade_lifecycle_case_targets WHERE case_id=? "
                "ORDER BY target_lot_id",
                (case_id,),
            )
        )
        target_mismatch += actual != tuple(expected)

    fact_mismatch = 0
    for case_id, fact in facts.items():
        encoded, fingerprint = encode_lifecycle_case_decision_fact(fact)
        row = conn.execute(
            "SELECT decision_fact_json,decision_fact_sha256 "
            "FROM trade_lifecycle_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        fact_mismatch += row is None or (
            row["decision_fact_json"], row["decision_fact_sha256"]
        ) != (encoded, fingerprint)

    evidence_count_mismatch = 0
    for case_id, expected in evidence_counts.items():
        row = conn.execute(
            "SELECT evidence_count FROM trade_lifecycle_evidence_revisions "
            "WHERE case_id=?",
            (case_id,),
        ).fetchone()
        evidence_count_mismatch += row is None or row["evidence_count"] != expected

    projection_missing = projection_dirty = projection_mismatch = 0
    for account in accounts:
        storage = conn.execute(
            "SELECT * FROM current_decision_input_generations WHERE account=?",
            (account,),
        ).fetchone()
        projection = conn.execute(
            "SELECT * FROM current_decision_projections WHERE account=?",
            (account,),
        ).fetchone()
        if projection is None:
            projection_missing += 1
            continue
        inputs = conn.execute(
            "SELECT * FROM position_projection_heads WHERE account=?",
            (account,),
        ).fetchone()
        if not _projection_metadata_clean(
            account=account,
            source=dict(source) if source is not None else None,
            head=dict(inputs) if inputs is not None else None,
            generation=dict(storage) if storage is not None else None,
            projection=dict(projection),
            implementation_fingerprint=implementation,
        ):
            projection_dirty += 1
            continue
        if projection["decision_state_fingerprint"] != payloads[account][
            "decision_state_fingerprint"
        ]:
            projection_mismatch += 1

    return {
        "assigned_account_mismatch_count": assigned_mismatch,
        "case_target_mismatch_count": target_mismatch,
        "case_fact_mismatch_count": fact_mismatch,
        "evidence_count_mismatch_count": evidence_count_mismatch,
        "generation_missing_count": sum(
            conn.execute(
                "SELECT 1 FROM current_decision_input_generations WHERE account=?",
                (account,),
            ).fetchone()
            is None
            for account in accounts
        ),
        "projection_missing_count": projection_missing,
        "projection_dirty_count": projection_dirty,
        "projection_mismatch_count": projection_mismatch,
        "missing_indexes": missing_indexes,
        "missing_triggers": missing_triggers,
        "position_schema_cookie_mismatch": (
            source is None
            or source["sqlite_schema_cookie"] != _projection_schema_cookie(conn)
        ),
    }


def _migration_state_clean(summary: Mapping[str, Any]) -> bool:
    return not any(
        (
            int(summary[key])
            for key in (
                "assigned_account_mismatch_count",
                "case_target_mismatch_count",
                "case_fact_mismatch_count",
                "evidence_count_mismatch_count",
                "generation_missing_count",
                "projection_missing_count",
                "projection_dirty_count",
                "projection_mismatch_count",
            )
        )
    ) and not any(
        (
            summary["missing_indexes"],
            summary["missing_triggers"],
            summary["position_schema_cookie_mismatch"],
        )
    )


def _current_decision_migration_inventory_from_conn(
    path: Path,
    conn: Any,
    *,
    now_ms: int,
    implementation: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    tables = _position_migration._object_names(conn, "table")
    missing_tables = sorted(set(_DECISION_MIGRATION_REQUIRED_TABLES) - tables)
    authority_queries = [
        item
        for item in _DECISION_MIGRATION_AUTHORITY_QUERIES
        if item[0] in tables
    ]
    authority_fingerprint, source_counts, source_bytes = (
        _migration_rows_fingerprint(conn, authority_queries)
    )
    public: dict[str, Any] = {
        "store_identity": _position_migration._store_identity(path),
        "loaded_projector_implementation_fingerprint": implementation,
        "now_ms": now_ms,
        "authority_fingerprint": authority_fingerprint,
        "source_counts": source_counts,
        "source_payload_bytes": source_bytes,
        "accounts": [],
        "repair": {},
    }
    if missing_tables:
        reasons = ["required_tables_missing"]
        public.update(
            readiness="not_ready",
            readiness_reasons=reasons,
            missing_tables=missing_tables,
            inventory_fingerprint=canonical_sha256(
                {
                    "store_identity": public["store_identity"],
                    "implementation": implementation,
                    "now_ms": now_ms,
                    "authority_fingerprint": authority_fingerprint,
                }
            ),
        )
        return public, {}

    sources = _migration_source_inventory(conn)
    reasons = list(sources["reasons"])
    repo = _position_migration._repository(path)
    payloads: dict[str, dict[str, Any]] = {}
    facts: dict[str, dict[str, Any]] = {}
    account_rows: list[dict[str, Any]] = []
    for account in sources["accounts"]:
        try:
            payload, account_facts = _current_decision_projection_oracle(
                repo,
                account=account,
                now_ms=now_ms,
                assigned_stock_report=None,
                conn=conn,
                allow_schema_cookie_mismatch=True,
            )
        except Exception as exc:
            reasons.append(f"oracle_unavailable:{account}:{type(exc).__name__}")
            continue
        payloads[account] = payload
        for fact in account_facts:
            case_id = str(fact["case_id"])
            if case_id in facts:
                raise CurrentDecisionProjectionError(
                    "migration oracle returned duplicate lifecycle case"
                )
            facts[case_id] = fact
        head = conn.execute(
            "SELECT built_source_generation,built_lots_generation,"
            "projection_fingerprint,lot_count,status FROM position_projection_heads "
            "WHERE account=?",
            (account,),
        ).fetchone()
        account_rows.append(
            {
                "account": account,
                "position_head": dict(head) if head is not None else None,
                "oracle_decision_state_fingerprint": payload[
                    "decision_state_fingerprint"
                ],
                "oracle_case_fact_count": len(account_facts),
            }
        )
    if len(payloads) != len(sources["accounts"]):
        reasons.append("oracle_inventory_incomplete")
    if set(facts) != set(sources["cases"]):
        reasons.append("lifecycle_case_fact_inventory_incomplete")
    state = (
        _migration_state_summary(
            conn,
            accounts=sources["accounts"],
            payloads=payloads,
            facts=facts,
            targets=sources["targets"],
            assigned_accounts=sources["assigned_accounts"],
            evidence_counts=sources["evidence_counts"],
            implementation=implementation,
        )
        if not reasons
        else {}
    )
    public.update(
        accounts=account_rows,
        repair=state,
        missing_tables=[],
        assigned_stock_legacy_account_count=sources["assigned_null_count"],
        mixed_version_guard_status=(
            "active"
            if "trg_current_decision_assigned_stock_account_insert_guard"
            in _position_migration._object_names(conn, "trigger")
            else "missing"
        ),
        readiness="ready" if not reasons else "not_ready",
        readiness_reasons=sorted(set(reasons)),
        inventory_fingerprint=canonical_sha256(
            {
                "store_identity": public["store_identity"],
                "implementation": implementation,
                "now_ms": now_ms,
                "authority_fingerprint": authority_fingerprint,
            }
        ),
    )
    return public, {
        "sources": sources,
        "payloads": payloads,
        "facts": facts,
        "state": state,
    }


def build_current_decision_projection_migration_inventory(
    sqlite_path: str | Path,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    path = _position_migration._store_path(sqlite_path)
    instant = _integer(
        int(time.time() * 1000) if now_ms is None else now_ms,
        field="now_ms",
        minimum=1,
    )
    implementation, timing = _position_migration._loaded_implementation()
    before = _position_migration._file_sizes(path)
    with _position_migration._read_only_connection(path) as conn:
        inventory, _details = _current_decision_migration_inventory_from_conn(
            path,
            conn,
            now_ms=instant,
            implementation=implementation,
        )
    _position_migration._assert_read_only_persistent_sizes(
        before,
        _position_migration._file_sizes(path),
        operation="current-decision inventory",
    )
    return _position_migration._manifest(
        {
            "schema_version": CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA,
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "inventory",
            "read_only": True,
            "loaded_projector_fingerprint_timing": timing,
            **inventory,
        }
    )


def verify_current_decision_projection_migration(
    sqlite_path: str | Path,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    path = _position_migration._store_path(sqlite_path)
    instant = _integer(
        int(time.time() * 1000) if now_ms is None else now_ms,
        field="now_ms",
        minimum=1,
    )
    implementation, _timing = _position_migration._loaded_implementation()
    before = _position_migration._file_sizes(path)
    with _position_migration._read_only_connection(path) as conn:
        inventory, details = _current_decision_migration_inventory_from_conn(
            path,
            conn,
            now_ms=instant,
            implementation=implementation,
        )
        comparisons: list[dict[str, Any]] = []
        samples: list[dict[str, Any]] = []
        if inventory["readiness"] == "ready":
            for account, expected in details["payloads"].items():
                row = conn.execute(
                    "SELECT * FROM current_decision_projections WHERE account=?",
                    (account,),
                ).fetchone()
                if row is None:
                    status = "proposed"
                else:
                    try:
                        actual = _decode_projection_row_payload(dict(row))
                        status = (
                            "matched"
                            if actual["decision_state_fingerprint"]
                            == expected["decision_state_fingerprint"]
                            else "mismatch"
                        )
                    except CurrentDecisionProjectionError:
                        status = "mismatch"
                comparisons.append({"account": account, "status": status})
                if status == "mismatch" and len(samples) < 10:
                    samples.append(
                        {"account": account, "reason": "stored_projection_mismatch"}
                    )
    _position_migration._assert_read_only_persistent_sizes(
        before,
        _position_migration._file_sizes(path),
        operation="current-decision verify",
    )
    status = (
        "not_ready"
        if inventory["readiness"] != "ready"
        else "mismatch"
        if samples
        else "valid"
    )
    return _position_migration._manifest(
        {
            "schema_version": "current_decision_projection_migration_verify.v1",
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "verify",
            "read_only": True,
            "status": status,
            "inventory_fingerprint": inventory["inventory_fingerprint"],
            "comparison_count": len(comparisons),
            "comparisons": comparisons,
            "mismatch_count": len(samples),
            "mismatch_samples": samples,
            "readiness_reasons": inventory["readiness_reasons"],
        }
    )


def apply_current_decision_projection_migration(
    sqlite_path: str | Path,
    manifest: Mapping[str, Any],
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    supplied = _position_migration._validate_manifest(
        manifest,
        schema=CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA,
    )
    path = _position_migration._store_path(sqlite_path)
    implementation, _timing = _position_migration._loaded_implementation()
    now_ms = _integer(supplied.get("now_ms"), field="manifest now_ms", minimum=1)
    conn = _position_migration._write_connection(path)
    repo = _position_migration._repository(path)
    before = _position_migration._file_sizes(path)
    write_applied = False
    counts: dict[str, int] = {}
    final_state: dict[str, Any] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        current, details = _current_decision_migration_inventory_from_conn(
            path,
            conn,
            now_ms=now_ms,
            implementation=implementation,
        )
        if current["store_identity"] != supplied.get("store_identity"):
            raise ValueError("migration manifest store identity mismatch")
        if implementation != supplied.get(
            "loaded_projector_implementation_fingerprint"
        ):
            raise ValueError("migration manifest projector implementation mismatch")
        if current["authority_fingerprint"] != supplied.get(
            "authority_fingerprint"
        ):
            raise ValueError("migration manifest is stale")
        if current["readiness"] != "ready":
            raise ValueError("migration inventory is not ready")
        _position_migration._fail(failure_hook, "after_manifest_recheck")
        if _migration_state_clean(details["state"]):
            final_state = details["state"]
            conn.rollback()
        else:
            _ensure_current_decision_projection_schema(conn)
            _position_migration._fail(failure_hook, "after_schema")
            accounts = tuple(details["sources"]["accounts"])
            if accounts:
                placeholders = ",".join("?" for _account in accounts)
                conn.execute(
                    f"DELETE FROM current_decision_input_generations "
                    f"WHERE account IN ({placeholders})",
                    accounts,
                )
                conn.executemany(
                    "INSERT INTO current_decision_input_generations ("
                    "account,generation,case_generation,evidence_generation,"
                    "allocation_generation,source_consumption_generation,"
                    "timing_generation,combo_identity_generation,"
                    "assigned_stock_generation,updated_at_ms"
                    ") VALUES (?,0,0,0,0,0,0,0,0,?)",
                    [(account, now_ms) for account in accounts],
                )
            for trigger in (
                "trg_current_decision_assigned_stock_account_update_guard",
                "trg_current_decision_assigned_stock_account_delete_guard",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            assigned_updates = 0
            for stock_event_id, account in details["sources"][
                "assigned_accounts"
            ].items():
                cursor = conn.execute(
                    "UPDATE assigned_stock_events SET account=? "
                    "WHERE stock_event_id=? AND account IS NOT ?",
                    (account, stock_event_id, account),
                )
                assigned_updates += int(cursor.rowcount or 0)
            _ensure_current_decision_projection_schema(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_assigned_stock_events_account_time "
                "ON assigned_stock_events(account,trade_time_ms,stock_event_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_cases_account_status "
                "ON trade_lifecycle_cases(account,status,updated_at_ms DESC,case_id DESC) "
                "WHERE status IN ('pending','waiting_settlement_evidence',"
                "'needs_review','partially_resolved','conflict')"
            )
            conn.execute(
                "UPDATE position_projection_source_state SET sqlite_schema_cookie=?,"
                "updated_at_ms=? WHERE singleton_id=1",
                (_projection_schema_cookie(conn), now_ms),
            )
            evidence_updates = 0
            for case_id, expected_count in details["sources"][
                "evidence_counts"
            ].items():
                row = conn.execute(
                    "SELECT revision,evidence_count "
                    "FROM trade_lifecycle_evidence_revisions WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO trade_lifecycle_evidence_revisions "
                        "(case_id,revision,evidence_count) VALUES (?,0,?)",
                        (case_id, expected_count),
                    )
                    evidence_updates += 1
                elif row["evidence_count"] != expected_count:
                    conn.execute(
                        "UPDATE trade_lifecycle_evidence_revisions "
                        "SET evidence_count=? WHERE case_id=?",
                        (expected_count, case_id),
                    )
                    evidence_updates += 1
            target_updates = 0
            for case_id, expected in details["sources"]["targets"].items():
                actual = tuple(
                    (str(row["case_id"]), str(row["account"]), str(row["target_lot_id"]), row["target_contracts"])
                    for row in conn.execute(
                        "SELECT case_id,account,target_lot_id,target_contracts "
                        "FROM trade_lifecycle_case_targets WHERE case_id=? "
                        "ORDER BY target_lot_id",
                        (case_id,),
                    )
                )
                if actual == tuple(expected):
                    continue
                conn.execute(
                    "DELETE FROM trade_lifecycle_case_targets WHERE case_id=?",
                    (case_id,),
                )
                conn.executemany(
                    "INSERT INTO trade_lifecycle_case_targets "
                    "(case_id,account,target_lot_id,target_contracts) "
                    "VALUES (?,?,?,?)",
                    expected,
                )
                target_updates += 1
            fact_updates = 0
            for fact in details["facts"].values():
                fact_updates += write_lifecycle_case_decision_fact(
                    repo,
                    fact=fact,
                    conn=conn,
                )
            _position_migration._fail(failure_hook, "after_backfill")
            final_payloads: dict[str, dict[str, Any]] = {}
            final_facts: dict[str, dict[str, Any]] = {}
            for account in accounts:
                payload, account_facts = _current_decision_projection_oracle(
                    repo,
                    account=account,
                    now_ms=now_ms,
                    assigned_stock_report=None,
                    conn=conn,
                )
                final_payloads[account] = payload
                final_facts.update(
                    (str(fact["case_id"]), fact) for fact in account_facts
                )
            _position_migration._fail(failure_hook, "before_projection")
            projection_updates = sum(
                repo.upsert_current_decision_projection(
                    current_decision_projection_row(payload),
                    conn=conn,
                )
                for payload in final_payloads.values()
            )
            final_state = _migration_state_summary(
                conn,
                accounts=accounts,
                payloads=final_payloads,
                facts=final_facts,
                targets=details["sources"]["targets"],
                assigned_accounts=details["sources"]["assigned_accounts"],
                evidence_counts=details["sources"]["evidence_counts"],
                implementation=implementation,
            )
            if not _migration_state_clean(final_state):
                raise RuntimeError("current decision migration verification failed")
            counts = {
                "assigned_accounts_backfilled": assigned_updates,
                "evidence_counts_backfilled": evidence_updates,
                "case_targets_rebuilt": target_updates,
                "case_facts_written": fact_updates,
                "projections_written": projection_updates,
            }
            _position_migration._fail(failure_hook, "before_commit")
            conn.commit()
            write_applied = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        _position_migration.secure_sqlite_artifacts(path)
    return _position_migration._manifest(
        {
            "schema_version": "current_decision_projection_migration_apply.v1",
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "apply",
            "write_applied": write_applied,
            "store_identity": _position_migration._store_identity(path),
            "source_manifest_hash": supplied["manifest_hash"],
            "counts": counts,
            "state": final_state,
            "sqlite_bytes": {
                "before": before,
                "after": _position_migration._file_sizes(path),
            },
        }
    )


def current_decision_projection_migration_status(
    sqlite_path: str | Path,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    inventory = build_current_decision_projection_migration_inventory(
        sqlite_path,
        now_ms=now_ms,
    )
    repair = dict(inventory.get("repair") or {})
    account_count = len(inventory.get("accounts") or [])
    missing = int(repair.get("projection_missing_count") or 0)
    dirty = int(repair.get("projection_dirty_count") or 0)
    mismatch = int(repair.get("projection_mismatch_count") or 0)
    if inventory["readiness"] != "ready":
        status = "dirty"
    elif account_count == 0 or missing == account_count:
        status = "absent"
    elif dirty:
        status = "dirty"
    elif mismatch or missing:
        status = "mismatch"
    else:
        status = "clean"
    return _position_migration._manifest(
        {
            "schema_version": "current_decision_projection_migration_status.v1",
            "generated_at_utc": _position_migration._now_iso(),
            "operation": "status",
            "read_only": True,
            "status": status,
            "readiness": "ready" if status == "clean" else "not_ready",
            "readiness_reasons": inventory["readiness_reasons"],
            "account_count": account_count,
            "repair": repair,
            "shadow_status": "eligible" if status == "clean" else "not_ready",
            "performance_status": "formal_artifact_required",
            "inventory_manifest_hash": inventory["manifest_hash"],
            "store_identity": inventory["store_identity"],
            "mixed_version_guard_status": inventory.get(
                "mixed_version_guard_status", "missing"
            ),
        }
    )


def _decision_generations(row: Mapping[str, Any] | None) -> tuple[int, ...]:
    if row is None:
        return tuple(0 for _field in _GENERATION_FIELDS)
    item = dict(row)
    return tuple(_integer(item.get(field), field=field) for field in _GENERATION_FIELDS)


def _projection_bindings_clean(
    *,
    account: str,
    source: Mapping[str, Any] | None,
    head: Mapping[str, Any] | None,
    generation: Mapping[str, Any] | None,
    projection: Mapping[str, Any] | None,
    implementation_fingerprint: str,
) -> bool:
    if any(value is None for value in (source, head, generation, projection)):
        return False
    try:
        source_row = dict(source or {})
        head_row = dict(head or {})
        generation_row = dict(generation or {})
        projection_row = dict(projection or {})
        return all(
            (
                projection_row.get("account") == account,
                projection_row.get("projection_schema")
                == CURRENT_DECISION_PROJECTION_SCHEMA,
                projection_row.get("projector_implementation_fingerprint")
                == implementation_fingerprint,
                source_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
                head_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
                source_row.get("projector_implementation_fingerprint")
                == implementation_fingerprint,
                head_row.get("projector_implementation_fingerprint")
                == implementation_fingerprint,
                head_row.get("status") == "trusted",
                head_row.get("built_source_generation")
                == source_row.get("source_generation"),
                head_row.get("built_lots_generation") == head_row.get("lots_generation"),
                projection_row.get("built_position_source_generation")
                == source_row.get("source_generation"),
                projection_row.get("built_position_lots_generation")
                == head_row.get("lots_generation"),
                projection_row.get("position_lots_fingerprint")
                == head_row.get("projection_fingerprint"),
                projection_row.get("built_decision_input_generation")
                == generation_row.get("generation"),
                projection_row.get("built_case_generation")
                == generation_row.get("case_generation"),
                projection_row.get("built_evidence_generation")
                == generation_row.get("evidence_generation"),
                projection_row.get("built_allocation_generation")
                == generation_row.get("allocation_generation"),
                projection_row.get("built_source_consumption_generation")
                == generation_row.get("source_consumption_generation"),
                projection_row.get("built_timing_generation")
                == generation_row.get("timing_generation"),
                projection_row.get("built_combo_identity_generation")
                == generation_row.get("combo_identity_generation"),
                projection_row.get("built_assigned_stock_generation")
                == generation_row.get("assigned_stock_generation"),
            )
        )
    except (CurrentDecisionProjectionError, TypeError, ValueError):
        return False


def _projection_metadata_clean(
    *,
    account: str,
    source: Mapping[str, Any] | None,
    head: Mapping[str, Any] | None,
    generation: Mapping[str, Any] | None,
    projection: Mapping[str, Any] | None,
    implementation_fingerprint: str,
) -> bool:
    if not _projection_bindings_clean(
        account=account,
        source=source,
        head=head,
        generation=generation,
        projection=projection,
        implementation_fingerprint=implementation_fingerprint,
    ):
        return False
    try:
        return _decode_projection_row_payload(dict(projection or {}))[
            "normalized_account"
        ] == account
    except (CurrentDecisionProjectionError, TypeError, ValueError):
        return False


def capture_current_decision_projection_fence(
    repo: SQLiteOptionPositionsRepository,
    *,
    accounts: Sequence[str],
    conn: Any | None = None,
) -> CurrentDecisionProjectionFence:
    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_values = tuple(
        sorted({_text(value, field="account", lower=True) for value in accounts})
    )
    if not account_values:
        raise CurrentDecisionProjectionError("projection fence accounts are required")
    try:
        implementation = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable as exc:
        raise CurrentDecisionProjectionError(
            "projector implementation is unavailable"
        ) from exc
    state = repo.read_current_decision_projection_fence_inputs(
        account_values,
        conn=conn,
    )
    source = state.get("source")
    if not isinstance(source, Mapping):
        raise CurrentDecisionProjectionError("position source state is missing")
    source_generation = _integer(
        source.get("source_generation"),
        field="position source generation",
    )
    account_states = state.get("accounts")
    if not isinstance(account_states, Mapping):
        raise CurrentDecisionProjectionError("projection fence state is invalid")
    captured: list[CurrentDecisionAccountFence] = []
    for account in account_values:
        raw = account_states.get(account)
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("projection fence account is missing")
        head = raw.get("head")
        generation = raw.get("generation")
        projection = raw.get("projection")
        lots_generation = (
            _integer(head.get("lots_generation"), field="lots_generation")
            if isinstance(head, Mapping)
            else 0
        )
        captured.append(
            CurrentDecisionAccountFence(
                account=account,
                position_lots_generation=lots_generation,
                decision_generations=_decision_generations(
                    generation if isinstance(generation, Mapping) else None
                ),
                projection_present=isinstance(projection, Mapping),
                clean_at_start=_projection_bindings_clean(
                    account=account,
                    source=source,
                    head=head if isinstance(head, Mapping) else None,
                    generation=(
                        generation if isinstance(generation, Mapping) else None
                    ),
                    projection=(
                        projection if isinstance(projection, Mapping) else None
                    ),
                    implementation_fingerprint=implementation,
                ),
            )
        )
    return CurrentDecisionProjectionFence(
        position_source_generation=source_generation,
        accounts=tuple(captured),
    )


def capture_trade_event_decision_projection_fence(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    account: str | None = None,
) -> CurrentDecisionProjectionFence | None:
    """Capture every existing account head before a global event mutation."""

    accounts = set(repo.list_position_projection_accounts(conn=conn))
    if account is not None:
        accounts.add(_text(account, field="account", lower=True))
    return (
        capture_current_decision_projection_fence(
            repo,
            accounts=tuple(accounts),
            conn=conn,
        )
        if accounts
        else None
    )


def read_current_assigned_stock_fact(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    conn: Any,
) -> dict[str, Any]:
    state = repo.read_current_decision_storage_state(account, conn=conn)
    projection = state.get("projection")
    if not isinstance(projection, Mapping):
        raise CurrentDecisionProjectionError(
            "current decision projection is missing assigned-stock state"
        )
    return validate_assigned_stock_fact(
        _decode_projection_row_payload(projection)["assigned_stock"]
    )


def defer_current_decision_projection(
    fence: CurrentDecisionProjectionFence | None,
    *,
    reason: str = "explicit_rebuild_required",
) -> dict[str, Any] | None:
    if fence is None:
        return None
    reason_value = _text(reason, field="projection deferral reason", lower=True)
    return {
        "schema_version": "current_decision_projection_finalize.v1",
        "statuses": {
            item.account: (
                "not_initialized"
                if not item.projection_present
                else "preexisting_dirty"
                if not item.clean_at_start
                else reason_value
            )
            for item in fence.accounts
        },
        "published_accounts": [],
        "projection_dml_count": 0,
    }


def finalize_current_decision_projection(
    repo: SQLiteOptionPositionsRepository,
    *,
    fence: CurrentDecisionProjectionFence,
    updated_at_ms: int,
    conn: Any,
    case_mutations_by_account: Mapping[
        str,
        Sequence[tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]],
    ]
    | None = None,
    assigned_stock_after_by_account: Mapping[str, Mapping[str, Any]] | None = None,
    trade_event_mutations: Sequence[tuple[Any, bool]] = (),
) -> dict[str, Any]:
    """Publish once/account after all owning mutations; caller owns the transaction."""

    if conn is None:
        raise CurrentDecisionProjectionError("projection finalizer requires a transaction")
    if not isinstance(repo, SQLiteOptionPositionsRepository):
        raise CurrentDecisionProjectionError("SQLite repository is required")
    account_fences = {item.account: item for item in fence.accounts}
    if not account_fences:
        raise CurrentDecisionProjectionError("projection fence is empty")
    mutations = dict(case_mutations_by_account or {})
    assigned_after = dict(assigned_stock_after_by_account or {})
    if (set(mutations) | set(assigned_after)) - set(account_fences):
        raise CurrentDecisionProjectionError("projection mutation account is outside fence")
    event_mutations_by_account: dict[str, list[tuple[Any, bool]]] = {}
    for event, created in trade_event_mutations:
        contract = _trade_event_contract(event)
        account = _text(contract.get("account"), field="event account", lower=True)
        if account in account_fences:
            event_mutations_by_account.setdefault(account, []).append(
                (event, bool(created))
            )
    try:
        implementation = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable as exc:
        raise CurrentDecisionProjectionError(
            "projector implementation is unavailable"
        ) from exc
    final_state = repo.read_current_decision_projection_fence_inputs(
        sorted(account_fences),
        conn=conn,
    )
    source = final_state.get("source")
    if not isinstance(source, Mapping):
        raise CurrentDecisionProjectionError("final position source state is missing")
    final_source_generation = _integer(
        source.get("source_generation"),
        field="final position source generation",
    )
    global_change = final_source_generation != fence.position_source_generation
    raw_accounts = final_state.get("accounts")
    if not isinstance(raw_accounts, Mapping):
        raise CurrentDecisionProjectionError("final projection fence state is invalid")

    statuses: dict[str, str] = {}
    to_build: list[str] = []
    for account, begin in sorted(account_fences.items()):
        if not begin.projection_present:
            statuses[account] = "not_initialized"
            continue
        if not begin.clean_at_start:
            statuses[account] = "preexisting_dirty"
            continue
        final = raw_accounts.get(account)
        if not isinstance(final, Mapping):
            raise CurrentDecisionProjectionError("final projection account is missing")
        head = final.get("head")
        generation = final.get("generation")
        projection = final.get("projection")
        if not all(isinstance(value, Mapping) for value in (head, generation, projection)):
            raise CurrentDecisionProjectionError("clean projection disappeared")
        changed = (
            global_change
            or _integer(head.get("lots_generation"), field="lots_generation")
            != begin.position_lots_generation
            or _decision_generations(generation) != begin.decision_generations
        )
        if not changed:
            statuses[account] = "not_required"
            continue
        to_build.append(account)

    rows: dict[str, dict[str, Any]] = {}
    for account in to_build:
        inputs = repo.read_current_decision_projection_inputs(
            account,
            conn=conn,
            include_identities=False,
        )
        event_assigned_after = assigned_after.get(account)
        if event_assigned_after is None and event_mutations_by_account.get(account):
            projection = inputs.get("projection")
            if not isinstance(projection, Mapping):
                raise CurrentDecisionProjectionError(
                    "current decision projection disappeared"
                )
            event_assigned_after = advance_assigned_stock_fact_for_trade_events(
                _decode_projection_row_payload(projection)["assigned_stock"],
                event_mutations=event_mutations_by_account[account],
                current_position_lots=list(inputs.get("lots") or []),
            )
        projection = inputs.get("projection")
        if not isinstance(projection, Mapping):
            raise CurrentDecisionProjectionError(
                "current decision projection disappeared"
            )
        combo_assigned = validate_assigned_stock_fact(
            event_assigned_after
            if event_assigned_after is not None
            else _decode_projection_row_payload(projection)["assigned_stock"]
        )
        group_ids = {
            str(fields.get("strategy_group_id") or "").strip()
            for fields in _position_lot_fields(list(inputs.get("lots") or [])).values()
        } | {
            str(lot.get("strategy_group_id") or "").strip()
            for lot in combo_assigned["lots"]
        }
        inputs["identities"] = [
            identity
            for group_id in sorted(group_ids - {""})
            if (
                identity := repo.get_strategy_group_identity(group_id, conn=conn)
            )
            is not None
        ]
        payload = build_current_decision_projection(
            repo,
            account=account,
            updated_at_ms=updated_at_ms,
            conn=conn,
            current_inputs=inputs,
            case_mutations=mutations.get(account, ()),
            assigned_stock_after=event_assigned_after,
            implementation_fingerprint=implementation,
        )
        rows[account] = current_decision_projection_row(payload)

    for account in to_build:
        repo.upsert_current_decision_projection(rows[account], conn=conn)
        statuses[account] = "published"
    return {
        "schema_version": "current_decision_projection_finalize.v1",
        "statuses": statuses,
        "published_accounts": to_build,
        "projection_dml_count": len(to_build),
    }


def _decision_read_unavailable(
    account: str,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_DECISION_READ_SCHEMA,
        "status": status,
        "account": account,
        "reason": reason,
        "payload": None,
        "position_lots": [],
    }


def read_current_decision_projection(
    repo: Any,
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    account_value = _text(account, field="account", lower=True)
    instant = _integer(now_ms, field="now_ms", minimum=1)
    if not callable(getattr(repo, "read_current_decision_projection_inputs", None)):
        return _decision_read_unavailable(
            account_value,
            status="absent",
            reason="sqlite_repository_required",
        )
    try:
        implementation = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable:
        return _decision_read_unavailable(
            account_value,
            status="data_unavailable",
            reason="projector_implementation_unavailable",
        )
    conn = repo._connect()
    try:
        conn.execute("BEGIN")
        inputs = repo.read_current_decision_projection_inputs(
            account_value,
            conn=conn,
            include_identities=False,
        )
        projection = inputs.get("projection")
        if projection is None:
            return _decision_read_unavailable(
                account_value,
                status="absent",
                reason="decision_projection_missing",
            )
        if not isinstance(projection, Mapping):
            raise CurrentDecisionProjectionError("decision projection row is invalid")
        source, head, generation, lots = _required_current_inputs(
            account=account_value,
            current_inputs=inputs,
            implementation_fingerprint=implementation,
        )
        if not _projection_metadata_clean(
            account=account_value,
            source=source,
            head=head,
            generation=generation,
            projection=projection,
            implementation_fingerprint=implementation,
        ):
            raise CurrentDecisionProjectionError("decision projection is dirty")
        payload = _decode_projection_row_payload(projection)
        lot_views, case_views = lifecycle_views_by_lot(
            payload["lifecycle"],
            current_position_lots=lots,
            now_ms=instant,
        )
        quality = derive_lifecycle_quality_view(
            payload["lifecycle_quality"],
            now_ms=instant,
        )
        for item in quality["operational_cases"]:
            item["reason_state"] = case_views[str(item["case_id"])]["reason_state"]
        return {
            "schema_version": CURRENT_DECISION_READ_SCHEMA,
            "status": "trusted",
            "account": account_value,
            "reason": None,
            "payload": payload,
            "position_lots": lots,
            "lot_count": len(lots),
            "lifecycle_by_lot": lot_views,
            "lifecycle_by_case": case_views,
            "lifecycle_quality": quality,
        }
    except CurrentDecisionProjectionError as exc:
        return _decision_read_unavailable(
            account_value,
            status="data_unavailable",
            reason=str(exc),
        )
    except Exception:
        return _decision_read_unavailable(
            account_value,
            status="data_unavailable",
            reason="current_decision_read_failed",
        )
    finally:
        conn.rollback()
        conn.close()


def verify_current_decision_projection(
    repo: Any,
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    result = read_current_decision_projection(
        repo,
        account=account,
        now_ms=now_ms,
    )
    valid = result["status"] == "trusted"
    return {
        "schema_version": "current_decision_projection_verification.v1",
        "account": result["account"],
        "status": "valid" if valid else result["status"],
        "mismatch_count": 0 if valid else 1,
        "mismatch_samples": []
        if valid
        else [{"reason": result["reason"]}],
    }


__all__ = [
    "CURRENT_ASSIGNED_STOCK_SCHEMA",
    "CURRENT_COMBO_GROUP_FACT_SCHEMA",
    "CURRENT_COMBO_SCHEMA",
    "CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA",
    "CURRENT_DECISION_PROJECTION_SCHEMA",
    "CURRENT_DECISION_READ_SCHEMA",
    "CURRENT_LIFECYCLE_QUALITY_SCHEMA",
    "LIFECYCLE_CASE_DECISION_FACT_SCHEMA",
    "CurrentDecisionAccountFence",
    "CurrentDecisionProjectionError",
    "CurrentDecisionProjectionFence",
    "advance_assigned_stock_fact_for_trade_events",
    "advance_lifecycle_case_decision_fact",
    "apply_current_decision_projection_migration",
    "build_initial_lifecycle_case_decision_fact",
    "build_current_decision_projection_migration_inventory",
    "build_current_combo_facts",
    "build_current_decision_projection",
    "build_current_decision_projection_payload",
    "build_lifecycle_case_decision_fact",
    "build_lifecycle_quality_fact",
    "capture_current_decision_projection_fence",
    "capture_trade_event_decision_projection_fence",
    "compact_assigned_stock_view",
    "current_decision_projection_row",
    "current_decision_projection_migration_status",
    "empty_assigned_stock_fact",
    "encode_current_decision_projection",
    "encode_lifecycle_case_decision_fact",
    "finalize_current_decision_projection",
    "preview_current_decision_projection_oracle",
    "defer_current_decision_projection",
    "read_current_assigned_stock_fact",
    "read_current_decision_projection",
    "read_lifecycle_case_decision_fact",
    "update_assigned_stock_fact",
    "validate_assigned_stock_fact",
    "validate_current_combo_facts",
    "validate_current_decision_projection_payload",
    "validate_lifecycle_case_decision_fact",
    "validate_lifecycle_quality_fact",
    "verify_current_decision_projection",
    "verify_current_decision_projection_migration",
    "write_lifecycle_case_decision_fact",
]
