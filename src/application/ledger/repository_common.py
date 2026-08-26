from __future__ import annotations

import hashlib

import json

import sqlite3

import zlib

from contextlib import contextmanager

from dataclasses import dataclass

from pathlib import Path

from typing import Any, Mapping, Protocol, Sequence, cast

from domain.domain.ledger.position_fields import effective_expiration, now_ms

from domain.domain.ledger.position_fingerprint import (
    ordered_position_lots_fingerprint,
)

from domain.domain.symbol_identity import symbol_market

from domain.domain.wheel import normalize_wheel_event

from src.application.ledger.event_codec import (
    encode_trade_event_for_storage,
    stored_trade_event_to_ledger_event,
    trade_event_application_payload,
    trade_event_position_effect,
    valid_void_target_event_id,
)

from src.application.ledger.lifecycle_attempt_audit import (
    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
    LIFECYCLE_RECEIPT_CODEC,
    LIFECYCLE_RECEIPT_CODEC_VERSION,
    LifecycleAttemptAuditEnvelope,
    canonical_lifecycle_observation_bytes,
    compute_lifecycle_attempt_chain_sha256,
    lifecycle_invocation_id_bytes,
    lifecycle_receipt_sha256,
    lifecycle_sha256_bytes,
    validate_lifecycle_attempt_audit_envelope,
    verify_lifecycle_attempt_audit_chain,
)

from src.application.ledger.lifecycle_settlement_semantics import (
    settlement_semantic_from_evidence,
)

from src.application.ledger.position_records import PositionLotRecord

from src.application.ledger.sqlite_row_codec import (
    position_lot_row_to_record,
    read_current_decision_projection_inputs_from_conn,
)

from src.application.ledger.store_resolution import resolve_ledger_store

from src.infrastructure.feishu_bitable import parse_note_kv, safe_float

from src.infrastructure.private_storage import (
    connect_private_sqlite,
    exclusive_private_file_lock,
    private_path,
    secure_sqlite_artifacts,
)

POSITION_PROJECTION_SCHEMA = "position_projection.v1"

_CURRENT_DECISION_GENERATION_COUNTERS = (
    "case_generation",
    "evidence_generation",
    "allocation_generation",
    "source_consumption_generation",
    "timing_generation",
    "combo_identity_generation",
    "assigned_stock_generation",
)

TRADE_EVENTS_COLUMN_CLASSIFICATION = {
    "event_id": "integrity/identity",
    "account": "projection-affecting",
    "event_json": "projection-affecting",
    "trade_time_ms": "projection-affecting",
    "ingest_seq": "integrity/snapshot",
    "market": "projection-affecting",
    "position_effect": "projection-affecting",
    "created_at_ms": "metadata-only",
    "updated_at_ms": "metadata-only",
}

POSITION_LOTS_COLUMN_CLASSIFICATION = {
    "record_id": "integrity/identity",
    "account": "projection-affecting",
    "fields_json": "projection-affecting",
    "source_event_id": "projection-affecting",
    "expiration": "projection-affecting",
    "strike": "projection-affecting",
    "multiplier": "projection-affecting",
    "updated_at_ms": "metadata-only",
}

@dataclass(frozen=True)
class PositionLotDiff:
    added: int
    changed: int
    removed: int
    unchanged: int
    accounts: tuple[str, ...]
    touched_accounts: tuple[str, ...]

    @property
    def lot_count(self) -> int:
        return self.added + self.changed + self.unchanged

@dataclass(frozen=True)
class PositionProjectionAccountSnapshot:
    account: str
    fingerprint: str
    lot_count: int
    records: tuple[dict[str, Any], ...] = ()

class OptionPositionsReadRepo(Protocol):
    def list_position_lots(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...

class OptionPositionsEventReadRepo(OptionPositionsReadRepo, Protocol):
    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...
    def list_trade_events_page(self, **kwargs: Any) -> dict[str, Any]: ...

class OptionPositionsEventWriteRepo(OptionPositionsEventReadRepo, Protocol):
    def upsert_trade_event(self, event: Any, *, conn: sqlite3.Connection | None = None) -> bool: ...
    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int: ...

class PositionProjectionPublicationRepo(OptionPositionsEventWriteRepo, Protocol):
    def apply_position_lot_diff(
        self,
        records: Sequence[PositionLotRecord],
        *,
        remove_missing: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> PositionLotDiff: ...
    def publish_full_position_projection_heads(
        self,
        *,
        implementation_fingerprint: str,
        known_accounts: Sequence[str],
        changed_accounts: Sequence[str],
        full_verified: bool = True,
        publish_source_implementation: bool = True,
        readiness_prevalidated: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool, str | None]: ...

class AssignedStockEventRepo(Protocol):
    def list_assigned_stock_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...
    def upsert_assigned_stock_event(self, event: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> bool: ...

def _load_data_config(data_config: Path) -> dict[str, Any]:
    if not data_config.exists():
        return {}
    cfg = json.loads(data_config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("data config must be a JSON object")
    return cfg

def option_positions_bootstrap_from_feishu_enabled(data_config: Path) -> bool:
    _load_data_config(data_config)
    return False

def resolve_option_positions_sqlite_path(data_config: Path) -> Path:
    path = resolve_ledger_store(data_config).sqlite_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def _validate_position_lot_fields(*, record_id: str, fields: dict[str, Any]) -> None:
    option_type = str(fields.get("option_type") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return
    expiration = fields.get("expiration")
    strike = safe_float(fields.get("strike"))
    missing: list[str] = []
    if expiration in (None, ""):
        missing.append("expiration")
    if strike is None:
        missing.append("strike")
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"incomplete option position lot {record_id}: missing {joined}")

def _position_lot_contract_scalars(fields: dict[str, Any]) -> tuple[int | None, float | None, float | None]:
    expiration_ms, _ = effective_expiration(fields)
    strike = safe_float(fields.get("strike"))
    multiplier = safe_float(fields.get("multiplier"))
    if multiplier is None:
        multiplier = safe_float(parse_note_kv(fields.get("note") or "", "multiplier"))
    return expiration_ms, strike, multiplier

def _position_lot_storage_values(
    record: PositionLotRecord,
) -> tuple[str, str, str, str | None, int | None, float | None, float | None]:
    if not isinstance(record, PositionLotRecord):
        raise TypeError("replace_position_lots requires PositionLotRecord records")
    record_id = record.record_id
    fields = record.fields
    _validate_position_lot_fields(record_id=record_id, fields=fields)
    account = str(fields.get("account") or "").strip()
    if not account:
        raise ValueError(f"position lot account is required: record_id={record_id}")
    if account != account.lower():
        raise ValueError(f"position lot account must be lowercase: record_id={record_id}")
    fields_json = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
    source_event_id = str(fields.get("source_event_id")) if fields.get("source_event_id") else None
    return (
        record_id,
        account,
        fields_json,
        source_event_id,
        int(expiration_ms) if expiration_ms is not None else None,
        float(strike) if strike is not None else None,
        float(multiplier) if multiplier is not None else None,
    )

def _canonical_existing_fields_json(raw: Any) -> str | None:
    try:
        fields = json.loads(str(raw or "{}"))
        if not isinstance(fields, dict):
            return None
        return json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

def _storage_scalar_matches(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) < 1e-9

def _same_lifecycle_evidence_source(existing_raw_json: Any, payload: dict[str, Any]) -> bool:
    try:
        existing = json.loads(str(existing_raw_json or "{}"))
    except Exception:
        return False
    if not isinstance(existing, dict):
        return False
    for key in ("source_type", "source_event_id", "evidence_type"):
        if str(existing.get(key) or "").strip() != str(payload.get(key) or "").strip():
            return False
    return True

def _normalized_lifecycle_case_targets(
    payload: dict[str, Any],
    *,
    case_id: str,
    account: str,
) -> tuple[tuple[str, ...], dict[str, int], tuple[tuple[str, str, str, int | None], ...]]:
    target_lot_ids_raw = payload.get("target_lot_ids") or []
    if not isinstance(target_lot_ids_raw, (list, tuple)):
        raise ValueError("trade lifecycle case target_lot_ids must be a list")
    target_lot_ids = tuple(str(value or "").strip() for value in target_lot_ids_raw)
    if any(not value for value in target_lot_ids) or len(set(target_lot_ids)) != len(target_lot_ids):
        raise ValueError("trade lifecycle case target_lot_ids are invalid")
    target_contracts_raw = payload.get("target_contracts_by_lot") or {}
    if not isinstance(target_contracts_raw, dict):
        raise ValueError("trade lifecycle case target_contracts_by_lot must be an object")
    target_contracts: dict[str, int] = {}
    for key, value in target_contracts_raw.items():
        lot_id = str(key or "").strip()
        if not lot_id or type(value) is not int or value <= 0:
            raise ValueError("trade lifecycle case target contract count is invalid")
        target_contracts[lot_id] = value
    all_lot_ids = tuple(sorted(set(target_lot_ids) | set(target_contracts)))
    return (
        target_lot_ids,
        target_contracts,
        tuple((case_id, account, lot_id, target_contracts.get(lot_id)) for lot_id in all_lot_ids),
    )

def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _json_object(value: Any) -> dict[str, Any]:
    payload = json.loads(str(value) or "{}")
    if not isinstance(payload, dict):
        raise ValueError("stored ledger JSON value must be an object")
    return dict(payload)

def _normalize_combo_pair_inference_payload(
    inference: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(inference or {})
    required = (
        "inference_id",
        "schema_version",
        "algorithm_version",
        "account",
        "symbol",
        "market",
        "market_date",
        "put_record_id",
        "put_open_event_id",
        "call_record_id",
        "call_open_event_id",
        "evidence_grade",
        "input_snapshot_hash",
        "status",
        "strategy_group_id",
    )
    missing = [
        field
        for field in required
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        raise ValueError(
            "combo pair inference missing fields: " + ",".join(missing)
        )
    status = str(payload["status"]).strip().lower()
    allowed_statuses = {
        "proposal_ready",
        "ambiguous",
        "user_confirmed",
        "user_rejected",
        "expired_unresolved",
        "superseded",
    }
    if status not in allowed_statuses:
        raise ValueError(f"unsupported combo pair inference status: {status}")
    try:
        expires_at_ms = int(payload.get("proposal_expires_at_ms") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "combo pair inference proposal_expires_at_ms must be numeric"
        ) from exc
    if expires_at_ms <= 0:
        raise ValueError(
            "combo pair inference proposal_expires_at_ms must be > 0"
        )
    payload.update(
        {
            "inference_id": str(payload["inference_id"]).strip(),
            "schema_version": str(payload["schema_version"]).strip(),
            "algorithm_version": str(payload["algorithm_version"]).strip(),
            "account": str(payload["account"]).strip().lower(),
            "symbol": str(payload["symbol"]).strip().upper(),
            "market": str(payload["market"]).strip().upper(),
            "market_date": str(payload["market_date"]).strip(),
            "put_record_id": str(payload["put_record_id"]).strip(),
            "put_open_event_id": str(payload["put_open_event_id"]).strip(),
            "call_record_id": str(payload["call_record_id"]).strip(),
            "call_open_event_id": str(payload["call_open_event_id"]).strip(),
            "evidence_grade": str(payload["evidence_grade"]).strip().lower(),
            "candidate_occurrence_ids": _canonical_text_values(
                payload.get("candidate_occurrence_ids")
            ),
            "candidate_exposure_ids": _canonical_text_values(
                payload.get("candidate_exposure_ids")
            ),
            "input_snapshot_hash": str(payload["input_snapshot_hash"]).strip(),
            "status": status,
            "proposal_expires_at_ms": expires_at_ms,
            "evidence": [
                dict(item)
                for item in (payload.get("evidence") or [])
                if isinstance(item, dict)
            ],
            "alternative_inference_ids": _canonical_text_values(
                payload.get("alternative_inference_ids")
            ),
            "strategy_group_id": str(payload["strategy_group_id"]).strip(),
        }
    )
    return payload

def _canonical_text_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("combo pair inference ID collection must be a sequence")
    return sorted({str(item).strip() for item in value if str(item).strip()})

def _assert_same_combo_pair_inference_identity(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    immutable_fields = (
        "inference_id",
        "schema_version",
        "account",
        "symbol",
        "market",
        "market_date",
        "put_record_id",
        "put_open_event_id",
        "call_record_id",
        "call_open_event_id",
    )
    conflicts = [
        field
        for field in immutable_fields
        if str(existing.get(field) or "").strip()
        != str(candidate.get(field) or "").strip()
    ]
    if conflicts:
        raise ValueError(
            "combo pair inference identity conflict: " + ",".join(conflicts)
        )

def _combo_pair_inference_sql_values(
    payload: dict[str, Any],
    *,
    raw_json: str,
) -> tuple[Any, ...]:
    return (
        str(payload["inference_id"]),
        str(payload["schema_version"]),
        str(payload["algorithm_version"]),
        str(payload["account"]),
        str(payload["symbol"]),
        str(payload["market"]),
        str(payload["market_date"]),
        str(payload["put_record_id"]),
        str(payload["put_open_event_id"]),
        str(payload["call_record_id"]),
        str(payload["call_open_event_id"]),
        str(payload["evidence_grade"]),
        _json_text(payload["candidate_occurrence_ids"]),
        _json_text(payload["candidate_exposure_ids"]),
        str(payload["input_snapshot_hash"]),
        str(payload["status"]),
        int(payload["proposal_expires_at_ms"]),
        _json_text(payload["evidence"]),
        _json_text(payload["alternative_inference_ids"]),
        str(payload["strategy_group_id"]),
        payload.get("identity_hash"),
        payload.get("put_adoption_event_id"),
        payload.get("call_adoption_event_id"),
        payload.get("put_void_event_id"),
        payload.get("call_void_event_id"),
        payload.get("decision_at_ms"),
        payload.get("decision_by"),
        payload.get("decision_reason"),
        int(payload["created_at_ms"]),
        int(payload["updated_at_ms"]),
        raw_json,
    )

def _notification_outbox_row(row: sqlite3.Row) -> dict[str, Any]:
    provider_receipt = (
        _json_object(row["provider_receipt_json"])
        if row["provider_receipt_json"]
        else None
    )
    return {
        "outbox_id": str(row["outbox_id"]),
        "case_id": str(row["case_id"]),
        "transition_type": str(row["transition_type"]),
        "resolution_revision": int(row["resolution_revision"]),
        "delivery_revision": int(row["delivery_revision"] or 0),
        "transition_key": str(row["transition_key"]),
        "state_fingerprint": str(row["state_fingerprint"]),
        "status": str(row["status"]),
        "delivery_batch_id": row["delivery_batch_id"],
        "payload": _json_object(row["payload_json"]),
        "payload_hash": str(row["payload_hash"]),
        "provider_message_id": row["provider_message_id"],
        "claim_id": row["claim_id"],
        "claimed_at_ms": row["claimed_at_ms"],
        "send_started_at_ms": row["send_started_at_ms"],
        "attempt_count": int(row["attempt_count"] or 0),
        "next_attempt_at_ms": row["next_attempt_at_ms"],
        "last_error": row["last_error"],
        "provider_receipt": provider_receipt,
        "created_at_ms": int(row["created_at_ms"]),
        "updated_at_ms": int(row["updated_at_ms"]),
        "confirmed_at_ms": row["confirmed_at_ms"],
    }

def _notification_delivery_batch_row(
    row: sqlite3.Row,
) -> dict[str, Any]:
    provider_receipt = (
        _json_object(row["provider_receipt_json"])
        if row["provider_receipt_json"]
        else None
    )
    return {
        "batch_id": str(row["batch_id"]),
        "route_fingerprint": str(row["route_fingerprint"]),
        "provider": str(row["provider"]),
        "channel": str(row["channel"]),
        "target_fingerprint": str(row["target_fingerprint"]),
        "renderer_version": str(row["renderer_version"]),
        "status": str(row["status"]),
        "payload": _json_object(row["payload_json"]),
        "payload_hash": str(row["payload_hash"]),
        "member_count": int(row["member_count"]),
        "first_intent_created_at_ms": int(
            row["first_intent_created_at_ms"]
        ),
        "last_intent_created_at_ms": int(
            row["last_intent_created_at_ms"]
        ),
        "provider_message_id": row["provider_message_id"],
        "claim_id": row["claim_id"],
        "claimed_at_ms": row["claimed_at_ms"],
        "send_started_at_ms": row["send_started_at_ms"],
        "attempt_count": int(row["attempt_count"] or 0),
        "next_attempt_at_ms": row["next_attempt_at_ms"],
        "last_error": row["last_error"],
        "provider_receipt": provider_receipt,
        "created_at_ms": int(row["created_at_ms"]),
        "updated_at_ms": int(row["updated_at_ms"]),
        "confirmed_at_ms": row["confirmed_at_ms"],
    }

def _lifecycle_case_immutable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "case_id": str(payload.get("case_id") or "").strip(),
        "case_key": str(payload.get("case_key") or "").strip(),
        "account": str(payload.get("account") or "").strip().lower(),
        "broker": str(payload.get("broker") or "").strip().lower(),
        "futu_account_id": str(
            payload.get("futu_account_id") or ""
        ).strip(),
        "contract_key": payload.get("contract_key"),
        "position_side": str(payload.get("position_side") or "").strip().lower(),
        "expiration_ymd": str(payload.get("expiration_ymd") or "").strip(),
        "target_contracts_by_lot": dict(payload.get("target_contracts_by_lot") or {}),
        "observation_start_ms": payload.get("observation_start_ms"),
        "pending_until_ms": payload.get("pending_until_ms"),
    }
