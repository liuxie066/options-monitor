from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from src.application.ledger.api import (
    LIFECYCLE_ATTEMPT_OUTCOME_CODES,
    canonical_source_economic_payload,
    canonical_source_payload_hash,
    lifecycle_attempt_diagnostic_sha256,
)
from src.application.trades.settlement_attempts import (
    SettlementAttemptOutcome,
    settlement_attempt_updates_after_outcome,
)
from src.infrastructure.private_storage import connect_private_sqlite


SETTLEMENT_ATTEMPT_MIN_LEASE_MS = 120_000
_SETTLEMENT_ATTEMPT_QUERY_BATCH_SIZE = 400
_SETTLEMENT_INVOCATION_STATES = frozenset(
    {
        "reserved",
        "provider_started",
        "provider_finished",
        "ledger_committed",
        "ambiguous_provider_result",
    }
)
_SETTLEMENT_PENDING_FIELDS = (
    "pending_outcome_code",
    "pending_semantic_fingerprint",
    "pending_receipt_sha256",
    "pending_diagnostic_sha256",
    "pending_outcome_kind",
    "pending_reason_code",
    "pending_provider_code",
    "pending_error_class",
    "pending_retry_after_ms",
    "pending_control_now_ms",
)
_SETTLEMENT_COMMITTED_FIELDS = (
    "committed_audit_ordinal",
    "committed_chain_sha256",
)
_SETTLEMENT_PENDING_CONTROL_FIELDS = (
    "classification",
    "outcome_kind",
    "reason_code",
    "provider_code",
    "error_class",
    "next_attempt_at_ms",
    "last_attempt_at_ms",
    "updated_at_ms",
)
_SETTLEMENT_INVOCATION_FIELDS = (
    "invocation_id",
    "invocation_state",
    "invocation_attempted_at_ms",
    *_SETTLEMENT_PENDING_FIELDS,
    *_SETTLEMENT_COMMITTED_FIELDS,
)
_SETTLEMENT_INVOCATION_CLEAR_SQL = ", ".join(
    f"{field} = NULL" for field in _SETTLEMENT_INVOCATION_FIELDS
)
_SETTLEMENT_CONTROL_KIND_BY_AUDIT_KIND = {
    audit_kind: {
        "stale_generation_after_call": "stale_generation",
        "processing_failure_after_call": "unknown_error",
        "legacy_semantic_unavailable_after_call": (
            "legacy_semantic_unavailable"
        ),
    }.get(audit_kind, audit_kind)
    for audit_kind in LIFECYCLE_ATTEMPT_OUTCOME_CODES
}
_SETTLEMENT_AUDIT_KIND_BY_CODE = {
    int(code): audit_kind
    for audit_kind, code in LIFECYCLE_ATTEMPT_OUTCOME_CODES.items()
}


class SettlementAttemptClaimOwnershipLost(RuntimeError):
    """The attempt lease is no longer owned by the active worker."""


def enqueue_trade_payload(
    path: str | Path,
    *,
    payload: dict[str, Any],
    source: str,
    broker_deal_key: str | None = None,
) -> str:
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    deal_id = _payload_deal_id(payload)
    source_text = str(source or "unknown").strip().lower() or "unknown"
    canonical_key = str(broker_deal_key or "").strip()
    economic_payload_hash = (
        _canonical_inbox_economic_hash(
            canonical_key,
            payload,
        )
        if canonical_key
        else None
    )
    identity_status = "bound" if canonical_key else "identity_needs_review"
    identity = canonical_key or (
        f"identity-needs-review|{source_text}|"
        f"{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"
    )
    inbox_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    now_ms = int(time.time() * 1000)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO trade_inbox (
                    inbox_id, source, deal_id, broker_deal_key, identity_status,
                    payload_json, economic_payload_hash, status,
                    attempt_count, received_at_ms, updated_at_ms,
                    last_error, result_status, result_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(inbox_id) DO NOTHING
                """,
                (
                    inbox_id,
                    source_text,
                    deal_id or None,
                    canonical_key or None,
                    identity_status,
                    payload_json,
                    economic_payload_hash,
                    "pending" if canonical_key else "identity_needs_review",
                    now_ms,
                    now_ms,
                    None if canonical_key else "canonical_broker_identity_missing",
                    None if canonical_key else "identity_needs_review",
                    None if canonical_key else "canonical_broker_identity_missing",
                ),
            )
            if canonical_key:
                row = conn.execute(
                    """
                    SELECT payload_json, economic_payload_hash, status
                    FROM trade_inbox
                    WHERE inbox_id = ?
                    """,
                    (inbox_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "trade inbox row disappeared after enqueue"
                    )
                existing_hash = str(
                    row["economic_payload_hash"] or ""
                ).strip()
                if not existing_hash:
                    existing_payload = json.loads(
                        str(row["payload_json"]) or "{}"
                    )
                    existing_hash = (
                        _canonical_inbox_economic_hash(
                            canonical_key,
                            (
                                existing_payload
                                if isinstance(
                                    existing_payload,
                                    dict,
                                )
                                else {}
                            ),
                        )
                    )
                    conn.execute(
                        """
                        UPDATE trade_inbox
                        SET economic_payload_hash = ?
                        WHERE inbox_id = ?
                        """,
                        (existing_hash, inbox_id),
                    )
                if existing_hash != economic_payload_hash:
                    conn.execute(
                        """
                        UPDATE trade_inbox
                        SET status = 'conflict',
                            updated_at_ms = ?,
                            last_error = ?,
                            result_status = 'conflict',
                            result_reason = ?
                        WHERE inbox_id = ?
                        """,
                        (
                            now_ms,
                            "broker_economic_payload_conflict",
                            "broker_economic_payload_conflict",
                            inbox_id,
                        ),
                    )
    return inbox_id


def list_retryable_trade_payloads(
    path: str | Path,
    *,
    limit: int = 100,
    retry_delay_sec: float = 60.0,
    max_attempts: int = 20,
) -> list[dict[str, Any]]:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return []
    cutoff_ms = int(time.time() * 1000 - max(0.0, retry_delay_sec) * 1000)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT inbox_id, source, deal_id, payload_json, attempt_count,
                       received_at_ms, updated_at_ms, last_error
                FROM trade_inbox
                WHERE status = 'pending'
                  AND attempt_count < ?
                  AND (attempt_count = 0 OR updated_at_ms <= ?)
                ORDER BY received_at_ms ASC, inbox_id ASC
                LIMIT ?
                """,
                (int(max_attempts), cutoff_ms, max(1, int(limit))),
            ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]) or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        out.append(
            {
                "inbox_id": str(row["inbox_id"]),
                "source": str(row["source"]),
                "deal_id": str(row["deal_id"] or ""),
                "payload": payload,
                "attempt_count": int(row["attempt_count"] or 0),
                "received_at_ms": int(row["received_at_ms"] or 0),
                "updated_at_ms": int(row["updated_at_ms"] or 0),
                "last_error": str(row["last_error"] or ""),
            }
        )
    return out


def mark_trade_payload_handled(
    path: str | Path,
    *,
    inbox_id: str,
    result: dict[str, Any] | None,
) -> None:
    inbox_path = Path(path)
    now_ms = int(time.time() * 1000)
    result_payload = result if isinstance(result, dict) else {}
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE trade_inbox
                SET status = 'handled',
                    attempt_count = attempt_count + 1,
                    updated_at_ms = ?,
                    last_error = NULL,
                    result_status = ?,
                    result_reason = ?
                WHERE inbox_id = ? AND status != 'handled'
                """,
                (
                    now_ms,
                    str(result_payload.get("status") or "").strip() or None,
                    str(result_payload.get("reason") or "").strip() or None,
                    str(inbox_id),
                ),
            )


def mark_trade_payload_retryable(
    path: str | Path,
    *,
    inbox_id: str,
    error: str | None,
    result: dict[str, Any] | None = None,
) -> None:
    inbox_path = Path(path)
    now_ms = int(time.time() * 1000)
    result_payload = result if isinstance(result, dict) else {}
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE trade_inbox
                SET status = 'pending',
                attempt_count = attempt_count + 1,
                updated_at_ms = ?,
                last_error = ?,
                result_status = ?,
                result_reason = ?
            WHERE inbox_id = ? AND status != 'handled'
            """,
                (
                    now_ms,
                    str(error) if error else None,
                    str(result_payload.get("status") or "exception"),
                    str(result_payload.get("reason") or "callback_exception"),
                    str(inbox_id),
                ),
            )


def settle_trade_payload_result(
    path: str | Path,
    *,
    inbox_id: str,
    result: dict[str, Any] | None,
) -> None:
    result_payload = result if isinstance(result, dict) else {}
    diagnostics = (
        result_payload.get("diagnostics")
        if isinstance(result_payload.get("diagnostics"), dict)
        else {}
    )
    lifecycle_pending_or_review = str(
        result_payload.get("reason") or ""
    ).strip().lower() in {
        "waiting_settlement_evidence",
        "awaiting_out_of_order_pair",
        "awaiting_settlement_evidence",
        "lifecycle_conflict_requires_review",
    }
    retryable = (
        str(result_payload.get("status") or "").strip().lower() == "unresolved"
        and bool(diagnostics.get("retryable"))
        and not bool(diagnostics.get("broker_evidence_accepted"))
        and not lifecycle_pending_or_review
    )
    if retryable:
        mark_trade_payload_retryable(
            path,
            inbox_id=inbox_id,
            error=None,
            result=result_payload,
        )
        return
    mark_trade_payload_handled(
        path,
        inbox_id=inbox_id,
        result=result_payload,
    )


def trade_inbox_summary(path: str | Path) -> dict[str, Any]:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return {
            "path": str(inbox_path),
            "pending_count": 0,
            "handled_count": 0,
            "identity_needs_review_count": 0,
            "max_attempt_count": 0,
        }
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS item_count, MAX(attempt_count) AS max_attempt_count
                FROM trade_inbox
                GROUP BY status
                """
            ).fetchall()
    counts = {str(row["status"]): int(row["item_count"] or 0) for row in rows}
    return {
        "path": str(inbox_path),
        "pending_count": counts.get("pending", 0),
        "handled_count": counts.get("handled", 0),
        "identity_needs_review_count": counts.get(
            "identity_needs_review",
            0,
        ),
        "conflict_count": counts.get("conflict", 0),
        "max_attempt_count": max(
            (int(row["max_attempt_count"] or 0) for row in rows),
            default=0,
        ),
    }


def trade_inbox_revision(path: str | Path) -> int:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return 0
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT revision
                FROM trade_inbox_revisions
                WHERE scope = 'summary'
                """
            ).fetchone()
    return int(row["revision"] or 0) if row is not None else 0


def get_settlement_attempt_state(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
) -> dict[str, Any] | None:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return None
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM lifecycle_settlement_attempt_state
                WHERE source_id = ? AND account = ? AND case_id = ?
                """,
                (
                    str(source_id or "").strip(),
                    str(account or "").strip().lower(),
                    str(case_id or "").strip(),
                ),
            ).fetchone()
    return _settlement_attempt_row(row) if row is not None else None


def list_settlement_attempt_states(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    source_key, account_key, normalized_case_ids = (
        _settlement_attempt_scope(
            source_id=source_id,
            account=account,
            case_ids=case_ids,
        )
    )
    inbox_path = Path(path)
    if not inbox_path.exists():
        return {}
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = _fetch_settlement_attempt_rows(
                conn,
                columns="*",
                source_id=source_key,
                account=account_key,
                case_ids=normalized_case_ids,
            )
    return {
        str(row["case_id"]): _settlement_attempt_row(row)
        for row in rows
    }


def upsert_settlement_attempt_state(
    path: str | Path,
    *,
    state: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(state or {})
    source_id = str(payload.get("source_id") or "").strip()
    account = str(payload.get("account") or "").strip().lower()
    case_id = str(payload.get("case_id") or "").strip()
    if not source_id or not account or not case_id:
        raise ValueError("settlement attempt state identity is incomplete")
    if any(payload.get(field) is not None for field in _SETTLEMENT_INVOCATION_FIELDS):
        raise ValueError(
            "generic settlement attempt upsert cannot mutate invocation state"
        )
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                f"""
                INSERT INTO lifecycle_settlement_attempt_state (
                  source_id, account, case_id, case_scope_fingerprint,
                  provider_input_scope_fingerprint,
                  collector_contract_version, capability_fingerprint,
                  classification, outcome_kind, reason_code, provider_code,
                  error_class, attempt_count, no_progress_count,
                  next_attempt_at_ms, last_attempt_at_ms,
                  last_semantic_fingerprint, claim_id, claim_until_ms,
                  updated_at_ms
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(source_id, account, case_id) DO UPDATE SET
                  case_scope_fingerprint = excluded.case_scope_fingerprint,
                  provider_input_scope_fingerprint = excluded.provider_input_scope_fingerprint,
                  collector_contract_version = excluded.collector_contract_version,
                  capability_fingerprint = excluded.capability_fingerprint,
                  classification = excluded.classification,
                  outcome_kind = excluded.outcome_kind,
                  reason_code = excluded.reason_code,
                  provider_code = excluded.provider_code,
                  error_class = excluded.error_class,
                  attempt_count = excluded.attempt_count,
                  no_progress_count = excluded.no_progress_count,
                  next_attempt_at_ms = excluded.next_attempt_at_ms,
                  last_attempt_at_ms = excluded.last_attempt_at_ms,
                  last_semantic_fingerprint = excluded.last_semantic_fingerprint,
                  claim_id = excluded.claim_id,
                  claim_until_ms = excluded.claim_until_ms,
                  updated_at_ms = excluded.updated_at_ms,
                  invocation_writer_epoch =
                    lifecycle_settlement_attempt_state.invocation_writer_epoch + 1,
                  {_SETTLEMENT_INVOCATION_CLEAR_SQL}
                WHERE (
                  lifecycle_settlement_attempt_state.claim_id IS NULL
                  OR lifecycle_settlement_attempt_state.claim_id = ''
                  OR lifecycle_settlement_attempt_state.claim_until_ms IS NULL
                  OR lifecycle_settlement_attempt_state.claim_until_ms <= excluded.updated_at_ms
                  OR lifecycle_settlement_attempt_state.claim_id = excluded.claim_id
                )
                  AND (
                    lifecycle_settlement_attempt_state.invocation_state IS NULL
                    OR lifecycle_settlement_attempt_state.invocation_state = 'ledger_committed'
                  )
                """,
                _settlement_attempt_values(
                    {
                        **payload,
                        "source_id": source_id,
                        "account": account,
                        "case_id": case_id,
                    }
                ),
            )
    stored = get_settlement_attempt_state(
        inbox_path,
        source_id=source_id,
        account=account,
        case_id=case_id,
    )
    if stored is None:
        raise RuntimeError("settlement attempt state disappeared")
    return stored


def claim_settlement_attempt(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    case_scope_fingerprint: str,
    claim_id: str,
    now_ms: int,
    lease_ms: int,
) -> bool:
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    lease_value = max(
        SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
        int(lease_ms or 0),
    )
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET claim_id = ?, claim_until_ms = ?, updated_at_ms = ?
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND case_scope_fingerprint = ?
                  AND classification = 'provider_required'
                  AND invocation_state IS NULL
                  AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= ?)
                  AND (
                    claim_id IS NULL OR claim_id = ''
                    OR claim_until_ms IS NULL OR claim_until_ms <= ?
                    OR claim_id = ?
                  )
                """,
                (
                    str(claim_id or "").strip(),
                    int(now_ms) + lease_value,
                    int(now_ms),
                    str(source_id or "").strip(),
                    str(account or "").strip().lower(),
                    str(case_id or "").strip(),
                    str(case_scope_fingerprint or "").strip(),
                    int(now_ms),
                    int(now_ms),
                    str(claim_id or "").strip(),
                ),
            )
            conn.commit()
            return int(cursor.rowcount or 0) == 1
        except Exception:
            conn.rollback()
            raise


def reserve_settlement_attempt_invocation(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    case_scope_fingerprint: str,
    claim_id: str,
    now_ms: int,
    lease_ms: int,
) -> dict[str, Any] | None:
    """Claim one provider attempt and durably reserve its UUIDv4."""

    source_key = str(source_id or "").strip()
    account_key = str(account or "").strip().lower()
    case_key = str(case_id or "").strip()
    scope_key = str(case_scope_fingerprint or "").strip()
    claim_key = str(claim_id or "").strip()
    if not all((source_key, account_key, case_key, scope_key, claim_key)):
        raise ValueError("settlement invocation reservation scope is incomplete")
    now_value = _positive_int(now_ms, field="now_ms")
    lease_value = max(
        SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
        int(lease_ms or 0),
    )
    candidate_invocation = str(uuid.uuid4())
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET claim_id = ?, claim_until_ms = ?, updated_at_ms = ?,
                    invocation_id = CASE
                      WHEN invocation_state = 'reserved'
                      THEN invocation_id
                      ELSE ?
                    END,
                    invocation_state = 'reserved',
                    invocation_writer_epoch = invocation_writer_epoch + 1,
                    invocation_attempted_at_ms = NULL,
                    pending_outcome_code = NULL,
                    pending_semantic_fingerprint = NULL,
                    pending_receipt_sha256 = NULL,
                    pending_diagnostic_sha256 = NULL,
                    pending_outcome_kind = NULL,
                    pending_reason_code = NULL,
                    pending_provider_code = NULL,
                    pending_error_class = NULL,
                    pending_retry_after_ms = NULL,
                    pending_control_now_ms = NULL,
                    committed_audit_ordinal = NULL,
                    committed_chain_sha256 = NULL
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND case_scope_fingerprint = ?
                  AND classification = 'provider_required'
                  AND (
                    invocation_state IS NULL
                    OR invocation_state IN ('reserved', 'ledger_committed')
                  )
                  AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= ?)
                  AND (
                    claim_id IS NULL OR claim_id = ''
                    OR claim_until_ms IS NULL OR claim_until_ms <= ?
                    OR claim_id = ?
                  )
                """,
                (
                    claim_key,
                    now_value + lease_value,
                    now_value,
                    candidate_invocation,
                    source_key,
                    account_key,
                    case_key,
                    scope_key,
                    now_value,
                    now_value,
                    claim_key,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                conn.commit()
                return None
            result = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def mark_settlement_attempt_provider_started(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    claim_id: str,
    invocation_id: str,
    attempted_at_ms: int,
) -> dict[str, Any]:
    """CAS one reserved invocation immediately before its first provider I/O."""

    source_key = _required_text(source_id, field="source_id")
    account_key = _required_text(account, field="account").lower()
    case_key = _required_text(case_id, field="case_id")
    claim_key = _required_text(claim_id, field="claim_id")
    invocation_key = _canonical_uuid_text(invocation_id)
    attempted_value = _positive_int(
        attempted_at_ms,
        field="attempted_at_ms",
    )
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET invocation_state = 'provider_started',
                    invocation_writer_epoch = invocation_writer_epoch + 1,
                    invocation_attempted_at_ms = ?,
                    updated_at_ms = ?
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND claim_id = ? AND invocation_id = ?
                  AND invocation_state = 'reserved'
                """,
                (
                    attempted_value,
                    attempted_value,
                    source_key,
                    account_key,
                    case_key,
                    claim_key,
                    invocation_key,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation provider-start CAS failed"
                )
            result = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def finish_settlement_attempt_provider_invocation(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    claim_id: str,
    invocation_id: str,
    outcome: SettlementAttemptOutcome,
    outcome_code: int,
    semantic_fingerprint: bytes | None,
    receipt_sha256: bytes | None,
    diagnostic_sha256: bytes | None,
    control_now_ms: int,
) -> dict[str, Any]:
    """Persist compact provider output while retaining the pre-attempt base."""

    source_key = _required_text(source_id, field="source_id")
    account_key = _required_text(account, field="account").lower()
    case_key = _required_text(case_id, field="case_id")
    claim_key = _required_text(claim_id, field="claim_id")
    invocation_key = _canonical_uuid_text(invocation_id)
    control_now_value = _positive_int(
        control_now_ms,
        field="control_now_ms",
    )
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            if (
                str(current.get("claim_id") or "") != claim_key
                or current.get("invocation_id") != invocation_key
                or current.get("invocation_state")
                not in {"provider_started", "provider_finished"}
            ):
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation provider-finish CAS failed"
                )
            stored_values = _settlement_provider_finished_values(
                current,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
                outcome=outcome,
                outcome_code=outcome_code,
                semantic_fingerprint=semantic_fingerprint,
                receipt_sha256=receipt_sha256,
                diagnostic_sha256=diagnostic_sha256,
                control_now_ms=control_now_value,
            )
            if current.get("invocation_state") == "provider_finished":
                mismatched = _settlement_pending_mismatches(
                    current,
                    stored_values,
                )
                if mismatched:
                    raise ValueError(
                        "settlement provider-finish replay mismatch: "
                        + ",".join(mismatched)
                    )
                conn.commit()
                return current
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET classification = ?, outcome_kind = ?, reason_code = ?,
                    provider_code = ?, error_class = ?,
                    next_attempt_at_ms = ?, last_attempt_at_ms = ?,
                    updated_at_ms = ?, invocation_state = 'provider_finished',
                    invocation_writer_epoch = invocation_writer_epoch + 1,
                    pending_outcome_code = ?,
                    pending_semantic_fingerprint = ?,
                    pending_receipt_sha256 = ?,
                    pending_diagnostic_sha256 = ?,
                    pending_outcome_kind = ?, pending_reason_code = ?,
                    pending_provider_code = ?, pending_error_class = ?,
                    pending_retry_after_ms = ?, pending_control_now_ms = ?,
                    committed_audit_ordinal = NULL,
                    committed_chain_sha256 = NULL
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND claim_id = ? AND invocation_id = ?
                  AND invocation_state = 'provider_started'
                """,
                (
                    *(
                        stored_values[field]
                        for field in _SETTLEMENT_PENDING_CONTROL_FIELDS
                    ),
                    outcome_code,
                    semantic_fingerprint,
                    receipt_sha256,
                    diagnostic_sha256,
                    outcome.kind,
                    outcome.reason_code,
                    outcome.provider_code,
                    outcome.error_class,
                    outcome.retry_after_ms,
                    control_now_value,
                    source_key,
                    account_key,
                    case_key,
                    claim_key,
                    invocation_key,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation provider-finish CAS failed"
                )
            result = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def replace_finished_settlement_attempt_provider_invocation(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    claim_id: str,
    invocation_id: str,
    outcome: SettlementAttemptOutcome,
    outcome_code: int,
    semantic_fingerprint: bytes | None,
    receipt_sha256: bytes | None,
    diagnostic_sha256: bytes | None,
    control_now_ms: int,
) -> dict[str, Any]:
    """CAS-replace one uncommitted provider result after reclassification."""

    source_key = _required_text(source_id, field="source_id")
    account_key = _required_text(account, field="account").lower()
    case_key = _required_text(case_id, field="case_id")
    claim_key = _required_text(claim_id, field="claim_id")
    invocation_key = _canonical_uuid_text(invocation_id)
    control_now_value = _positive_int(
        control_now_ms,
        field="control_now_ms",
    )
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            if (
                str(current.get("claim_id") or "") != claim_key
                or current.get("invocation_id") != invocation_key
                or current.get("invocation_state")
                != "provider_finished"
                or current.get("committed_audit_ordinal") is not None
                or current.get("committed_chain_sha256") is not None
            ):
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation provider-result replacement CAS failed"
                )
            stored_values = _settlement_provider_finished_values(
                current,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
                outcome=outcome,
                outcome_code=outcome_code,
                semantic_fingerprint=semantic_fingerprint,
                receipt_sha256=receipt_sha256,
                diagnostic_sha256=diagnostic_sha256,
                control_now_ms=control_now_value,
            )
            if not _settlement_pending_mismatches(
                current,
                stored_values,
            ):
                conn.commit()
                return current
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET classification = ?, outcome_kind = ?, reason_code = ?,
                    provider_code = ?, error_class = ?,
                    next_attempt_at_ms = ?, last_attempt_at_ms = ?,
                    updated_at_ms = ?,
                    invocation_writer_epoch = invocation_writer_epoch + 1,
                    pending_outcome_code = ?,
                    pending_semantic_fingerprint = ?,
                    pending_receipt_sha256 = ?,
                    pending_diagnostic_sha256 = ?,
                    pending_outcome_kind = ?, pending_reason_code = ?,
                    pending_provider_code = ?, pending_error_class = ?,
                    pending_retry_after_ms = ?, pending_control_now_ms = ?
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND claim_id = ? AND invocation_id = ?
                  AND invocation_state = 'provider_finished'
                  AND committed_audit_ordinal IS NULL
                  AND committed_chain_sha256 IS NULL
                """,
                (
                    *(
                        stored_values[field]
                        for field in _SETTLEMENT_PENDING_CONTROL_FIELDS
                    ),
                    outcome_code,
                    semantic_fingerprint,
                    receipt_sha256,
                    diagnostic_sha256,
                    outcome.kind,
                    outcome.reason_code,
                    outcome.provider_code,
                    outcome.error_class,
                    outcome.retry_after_ms,
                    control_now_value,
                    source_key,
                    account_key,
                    case_key,
                    claim_key,
                    invocation_key,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation provider-result replacement CAS failed"
                )
            result = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _settlement_provider_finished_values(
    current: Mapping[str, Any],
    *,
    source_id: str,
    account: str,
    case_id: str,
    outcome: SettlementAttemptOutcome,
    outcome_code: int,
    semantic_fingerprint: bytes | None,
    receipt_sha256: bytes | None,
    diagnostic_sha256: bytes | None,
    control_now_ms: int,
) -> dict[str, Any]:
    if type(outcome) is not SettlementAttemptOutcome:
        raise TypeError("settlement provider outcome is invalid")
    if (
        outcome.source_id != source_id
        or outcome.account != account
        or outcome.case_id != case_id
    ):
        raise ValueError("settlement provider outcome identity mismatch")
    if (
        outcome.contract_version
        != current.get("collector_contract_version")
        or outcome.capability_fingerprint
        != current.get("capability_fingerprint")
    ):
        raise ValueError("settlement provider outcome contract mismatch")
    pending = {
        **current,
        "invocation_state": "provider_finished",
        "pending_outcome_code": outcome_code,
        "pending_semantic_fingerprint": semantic_fingerprint,
        "pending_receipt_sha256": receipt_sha256,
        "pending_diagnostic_sha256": diagnostic_sha256,
        "pending_outcome_kind": outcome.kind,
        "pending_reason_code": outcome.reason_code,
        "pending_provider_code": outcome.provider_code,
        "pending_error_class": outcome.error_class,
        "pending_retry_after_ms": outcome.retry_after_ms,
        "pending_control_now_ms": control_now_ms,
        "committed_audit_ordinal": None,
        "committed_chain_sha256": None,
    }
    _validate_pending_settlement_outcome(pending)
    projected = _pending_settlement_control_updates(pending)
    return {
        **pending,
        **{
            field: projected[field]
            for field in _SETTLEMENT_PENDING_CONTROL_FIELDS
        },
    }


def _settlement_pending_mismatches(
    current: Mapping[str, Any],
    stored_values: Mapping[str, Any],
) -> list[str]:
    return [
        field
        for field in (
            *_SETTLEMENT_PENDING_FIELDS,
            *_SETTLEMENT_PENDING_CONTROL_FIELDS,
        )
        if current.get(field) != stored_values.get(field)
    ]


def reconcile_settlement_attempt_invocation(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    invocation_id: str,
    audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify restart state or finish an exact compact ledger receipt."""

    source_key = _required_text(source_id, field="source_id")
    account_key = _required_text(account, field="account").lower()
    case_key = _required_text(case_id, field="case_id")
    invocation_key = _canonical_uuid_text(invocation_id)
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            if current.get("invocation_id") != invocation_key:
                raise ValueError("settlement invocation identity mismatch")
            state = str(current.get("invocation_state") or "")
            if state == "reserved":
                if audit is not None:
                    raise ValueError(
                        "reserved settlement invocation conflicts with audit"
                    )
                conn.commit()
                return current
            if state == "ledger_committed" and audit is None:
                conn.commit()
                return current
            if state == "provider_started" or (
                state == "provider_finished" and audit is None
            ):
                conn.execute(
                    """
                    UPDATE lifecycle_settlement_attempt_state
                    SET invocation_state = 'ambiguous_provider_result',
                        invocation_writer_epoch = invocation_writer_epoch + 1,
                        claim_id = NULL, claim_until_ms = NULL
                    WHERE source_id = ? AND account = ? AND case_id = ?
                      AND invocation_id = ? AND invocation_state = ?
                    """,
                    (
                        source_key,
                        account_key,
                        case_key,
                        invocation_key,
                        state,
                    ),
                )
                result = _read_settlement_attempt_row(
                    conn,
                    source_id=source_key,
                    account=account_key,
                    case_id=case_key,
                )
                conn.commit()
                return result
            if state == "ambiguous_provider_result":
                conn.commit()
                return current
            if state not in {"provider_finished", "ledger_committed"}:
                raise ValueError("settlement invocation state is not reconcilable")

            ordinal, chain = _match_settlement_invocation_audit(
                current,
                audit,
            )
            if state == "ledger_committed":
                if (
                    current.get("committed_audit_ordinal") != ordinal
                    or current.get("committed_chain_sha256") != chain
                ):
                    raise ValueError("settlement committed audit receipt mismatch")
                conn.commit()
                return current

            projected = _pending_settlement_control_updates(current)
            merged = {
                **current,
                **projected,
                "claim_id": None,
                "claim_until_ms": None,
                "invocation_state": "ledger_committed",
                "committed_audit_ordinal": ordinal,
                "committed_chain_sha256": chain,
            }
            _validate_settlement_invocation_fields(merged)
            values = _settlement_attempt_values(merged)
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET case_scope_fingerprint = ?,
                    provider_input_scope_fingerprint = ?,
                    collector_contract_version = ?,
                    capability_fingerprint = ?, classification = ?,
                    outcome_kind = ?, reason_code = ?, provider_code = ?,
                    error_class = ?, attempt_count = ?, no_progress_count = ?,
                    next_attempt_at_ms = ?, last_attempt_at_ms = ?,
                    last_semantic_fingerprint = ?, claim_id = NULL,
                    claim_until_ms = NULL, updated_at_ms = ?,
                    invocation_state = 'ledger_committed',
                    invocation_writer_epoch = invocation_writer_epoch + 1,
                    committed_audit_ordinal = ?,
                    committed_chain_sha256 = ?
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND invocation_id = ?
                  AND invocation_state = 'provider_finished'
                """,
                (
                    *values[3:17],
                    values[19],
                    ordinal,
                    chain,
                    source_key,
                    account_key,
                    case_key,
                    invocation_key,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation reconciliation CAS failed"
                )
            result = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def claim_settlement_provider_batch(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    claim_id: str,
    now_ms: int,
    lease_ms: int,
) -> bool:
    """Claim one source/account provider batch without appending history."""

    source_key = str(source_id or "").strip()
    account_key = str(account or "").strip().lower()
    claim_key = str(claim_id or "").strip()
    if not source_key or not account_key or not claim_key:
        raise ValueError("settlement provider batch claim scope is incomplete")
    lease_value = max(
        SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
        int(lease_ms or 0),
    )
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                INSERT INTO lifecycle_settlement_provider_batch_leases (
                  source_id, account, claim_id, claim_until_ms, updated_at_ms
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, account) DO UPDATE SET
                  claim_id = excluded.claim_id,
                  claim_until_ms = excluded.claim_until_ms,
                  updated_at_ms = excluded.updated_at_ms
                WHERE lifecycle_settlement_provider_batch_leases.claim_until_ms
                        <= excluded.updated_at_ms
                   OR lifecycle_settlement_provider_batch_leases.claim_id
                        = excluded.claim_id
                """,
                (
                    source_key,
                    account_key,
                    claim_key,
                    int(now_ms) + lease_value,
                    int(now_ms),
                ),
            )
            conn.commit()
            return int(cursor.rowcount or 0) == 1
        except Exception:
            conn.rollback()
            raise


def renew_settlement_provider_batch_claim(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    claim_id: str,
    now_ms: int,
    lease_ms: int,
) -> bool:
    lease_value = max(
        SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
        int(lease_ms or 0),
    )
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_provider_batch_leases
                SET claim_until_ms = ?
                WHERE source_id = ? AND account = ? AND claim_id = ?
                """,
                (
                    int(now_ms) + lease_value,
                    str(source_id or "").strip(),
                    str(account or "").strip().lower(),
                    str(claim_id or "").strip(),
                ),
            )
            conn.commit()
            return int(cursor.rowcount or 0) == 1
        except Exception:
            conn.rollback()
            raise


def release_settlement_provider_batch_claim(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    claim_id: str,
) -> None:
    """Release only the named provider-batch owner."""

    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                DELETE FROM lifecycle_settlement_provider_batch_leases
                WHERE source_id = ? AND account = ? AND claim_id = ?
                """,
                (
                    str(source_id or "").strip(),
                    str(account or "").strip().lower(),
                    str(claim_id or "").strip(),
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement provider batch claim ownership changed"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def renew_settlement_attempt_claim(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    case_scope_fingerprint: str,
    claim_id: str,
    now_ms: int,
    lease_ms: int,
) -> bool:
    """Extend an existing claim without changing its status timestamp."""

    lease_value = max(
        SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
        int(lease_ms or 0),
    )
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET claim_until_ms = ?,
                    invocation_writer_epoch = invocation_writer_epoch + CASE
                      WHEN invocation_state IS NULL THEN 0 ELSE 1
                    END
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND case_scope_fingerprint = ?
                  AND classification = 'provider_required'
                  AND claim_id = ?
                """,
                (
                    int(now_ms) + lease_value,
                    str(source_id or "").strip(),
                    str(account or "").strip().lower(),
                    str(case_id or "").strip(),
                    str(case_scope_fingerprint or "").strip(),
                    str(claim_id or "").strip(),
                ),
            )
            conn.commit()
            return int(cursor.rowcount or 0) == 1
        except Exception:
            conn.rollback()
            raise


def complete_settlement_attempt(
    path: str | Path,
    *,
    source_id: str,
    account: str,
    case_id: str,
    claim_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    source_key = str(source_id or "").strip()
    account_key = str(account or "").strip().lower()
    case_key = str(case_id or "").strip()
    claim_key = str(claim_id or "").strip()
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            if str(current.get("claim_id") or "") != claim_key:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement attempt claim ownership changed"
                )
            if current.get("invocation_state") not in {None, "reserved"}:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement invocation requires exact audit reconciliation"
                )
            merged = {
                **current,
                **dict(updates or {}),
                "source_id": source_key,
                "account": account_key,
                "case_id": case_key,
                "claim_id": None,
                "claim_until_ms": None,
            }
            values = _settlement_attempt_values(merged)
            cursor = conn.execute(
                f"""
                UPDATE lifecycle_settlement_attempt_state
                SET case_scope_fingerprint = ?,
                    provider_input_scope_fingerprint = ?,
                    collector_contract_version = ?,
                    capability_fingerprint = ?,
                    classification = ?,
                    outcome_kind = ?,
                    reason_code = ?,
                    provider_code = ?,
                    error_class = ?,
                    attempt_count = ?,
                    no_progress_count = ?,
                    next_attempt_at_ms = ?,
                    last_attempt_at_ms = ?,
                    last_semantic_fingerprint = ?,
                    claim_id = ?,
                    claim_until_ms = ?,
                    updated_at_ms = ?,
                    invocation_writer_epoch = invocation_writer_epoch + CASE
                      WHEN invocation_state IS NULL THEN 0 ELSE 1
                    END,
                    {_SETTLEMENT_INVOCATION_CLEAR_SQL}
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND claim_id = ?
                """,
                (*values[3:], source_key, account_key, case_key, claim_key),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement attempt claim ownership changed"
                )
            result = _read_settlement_attempt_row(
                conn,
                source_id=source_key,
                account=account_key,
                case_id=case_key,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def settlement_attempt_summary(
    path: str | Path,
    *,
    source_id: str,
    now_ms: int,
    account: str,
    case_ids: Iterable[str],
) -> dict[str, Any]:
    source_key, account_key, normalized_case_ids = (
        _settlement_attempt_scope(
            source_id=source_id,
            account=account,
            case_ids=case_ids,
        )
    )
    inbox_path = Path(path)
    if not inbox_path.exists():
        return {
            "source_id": source_key,
            "provider_required_count": 0,
            "blocked_count": 0,
            "disabled_count": 0,
            "backoff_count": 0,
            "claimed_count": 0,
            "ambiguous_provider_result_count": 0,
            "eligible_count": 0,
            "earliest_next_attempt_at_ms": None,
            "last_state_change": None,
        }
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = _fetch_settlement_attempt_rows(
                conn,
                columns="*",
                source_id=source_key,
                account=account_key,
                case_ids=normalized_case_ids,
            )
    validated_rows = [_settlement_attempt_row(row) for row in rows]
    provider_rows = [
        row
        for row in validated_rows
        if str(row["classification"] or "") == "provider_required"
    ]
    blocked = [
        row
        for row in provider_rows
        if str(row["outcome_kind"] or "").startswith("blocked_")
        or str(row["outcome_kind"] or "")
        == "legacy_semantic_unavailable"
    ]
    disabled = [
        row
        for row in provider_rows
        if str(row["outcome_kind"] or "") == "disabled"
    ]
    claimed = [
        row
        for row in provider_rows
        if str(row["claim_id"] or "")
        and int(row["claim_until_ms"] or 0) > int(now_ms)
    ]
    ambiguous = [
        row
        for row in validated_rows
        if str(row["invocation_state"] or "")
        == "ambiguous_provider_result"
    ]
    backoff = [
        row
        for row in provider_rows
        if row["next_attempt_at_ms"] is not None
        and int(row["next_attempt_at_ms"]) > int(now_ms)
    ]
    eligible = [
        row
        for row in provider_rows
        if not str(row["outcome_kind"] or "").startswith("blocked_")
        and str(row["outcome_kind"] or "")
        != "legacy_semantic_unavailable"
        and str(row["outcome_kind"] or "") != "disabled"
        and str(row["invocation_state"] or "") not in {
            "provider_started",
            "provider_finished",
            "ambiguous_provider_result",
        }
        and not (
            str(row["claim_id"] or "")
            and int(row["claim_until_ms"] or 0) > int(now_ms)
        )
        and not (
            row["next_attempt_at_ms"] is not None
            and int(row["next_attempt_at_ms"]) > int(now_ms)
        )
    ]
    next_values = [
        int(row["next_attempt_at_ms"])
        for row in provider_rows
        if row["next_attempt_at_ms"] is not None
        and int(row["next_attempt_at_ms"]) > int(now_ms)
    ]
    latest_state = max(
        validated_rows,
        key=lambda row: (
            int(row["updated_at_ms"] or 0),
            str(row["case_id"] or ""),
        ),
        default=None,
    )
    return {
        "source_id": source_key,
        "provider_required_count": len(provider_rows),
        "blocked_count": len(blocked),
        "disabled_count": len(disabled),
        "backoff_count": len(backoff),
        "claimed_count": len(claimed),
        "ambiguous_provider_result_count": len(ambiguous),
        "eligible_count": len(eligible),
        "earliest_next_attempt_at_ms": min(next_values)
        if next_values
        else None,
        "last_state_change": (
            {
                "case_id": str(latest_state["case_id"] or ""),
                "outcome_kind": str(
                    latest_state["outcome_kind"] or ""
                )
                or None,
                "reason_code": str(
                    latest_state["reason_code"] or ""
                )
                or None,
                "provider_code": str(
                    latest_state["provider_code"] or ""
                )
                or None,
                "error_class": str(
                    latest_state["error_class"] or ""
                )
                or None,
                "updated_at_ms": int(
                    latest_state["updated_at_ms"] or 0
                ),
            }
            if latest_state is not None
            else None
        ),
    }


def require_trade_inbox_store_readable(path: str | Path) -> None:
    """Prove that a control-table failure is not whole-inbox corruption."""

    inbox_path = Path(path)
    if not inbox_path.exists():
        raise sqlite3.OperationalError("trade inbox database is unavailable")
    with closing(_connect(inbox_path)) as conn:
        check = conn.execute("PRAGMA quick_check(1)").fetchone()
        if check is None or str(check[0] or "").strip().lower() != "ok":
            raise sqlite3.DatabaseError("trade inbox quick_check failed")
        conn.execute("SELECT 1 FROM trade_inbox LIMIT 1").fetchone()


def _connect(path: Path) -> sqlite3.Connection:
    conn = connect_private_sqlite(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_settlement_attempt_rows(
    conn: sqlite3.Connection,
    *,
    columns: str,
    source_id: str,
    account: str,
    case_ids: tuple[str, ...],
) -> list[sqlite3.Row]:
    if not case_ids:
        return []
    rows: list[sqlite3.Row] = []
    for offset in range(
        0,
        len(case_ids),
        _SETTLEMENT_ATTEMPT_QUERY_BATCH_SIZE,
    ):
        batch = case_ids[
            offset : offset + _SETTLEMENT_ATTEMPT_QUERY_BATCH_SIZE
        ]
        placeholders = ", ".join("?" for _ in batch)
        rows.extend(
            conn.execute(
                f"""
                SELECT {columns}
                FROM lifecycle_settlement_attempt_state
                WHERE source_id = ? AND account = ?
                  AND case_id IN ({placeholders})
                """,
                [source_id, account, *batch],
            ).fetchall()
        )
    return rows


def _settlement_attempt_scope(
    *,
    source_id: str,
    account: str,
    case_ids: Iterable[str],
) -> tuple[str, str, tuple[str, ...]]:
    source_key = str(source_id or "").strip()
    account_key = str(account or "").strip().lower()
    if not source_key or not account_key:
        raise ValueError("settlement attempt read scope is incomplete")
    values = (case_ids,) if isinstance(case_ids, str) else case_ids
    normalized_case_ids = tuple(
        dict.fromkeys(
            value
            for raw_case_id in values
            if (value := str(raw_case_id or "").strip())
        )
    )
    return source_key, account_key, normalized_case_ids


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_inbox (
            inbox_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            deal_id TEXT,
            broker_deal_key TEXT,
            identity_status TEXT NOT NULL DEFAULT 'bound',
            payload_json TEXT NOT NULL,
            economic_payload_hash TEXT,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            received_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            last_error TEXT,
            result_status TEXT,
            result_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_inbox_revisions (
            scope TEXT PRIMARY KEY,
            revision INTEGER NOT NULL CHECK(revision >= 0)
        )
        """
    )
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS
            trg_trade_inbox_summary_{operation.lower()}
            AFTER {operation} ON trade_inbox
            BEGIN
              INSERT INTO trade_inbox_revisions (scope, revision)
              VALUES ('summary', 1)
              ON CONFLICT(scope) DO UPDATE SET
                revision = revision + 1;
            END
            """
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_settlement_attempt_state (
            source_id TEXT NOT NULL,
            account TEXT NOT NULL,
            case_id TEXT NOT NULL,
            case_scope_fingerprint TEXT NOT NULL,
            provider_input_scope_fingerprint TEXT,
            collector_contract_version TEXT NOT NULL,
            capability_fingerprint TEXT NOT NULL,
            classification TEXT NOT NULL,
            outcome_kind TEXT,
            reason_code TEXT,
            provider_code TEXT,
            error_class TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            no_progress_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at_ms INTEGER,
            last_attempt_at_ms INTEGER,
            last_semantic_fingerprint TEXT,
            claim_id TEXT,
            claim_until_ms INTEGER,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(source_id, account, case_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_settlement_provider_batch_leases (
            source_id TEXT NOT NULL,
            account TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            claim_until_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(source_id, account)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lifecycle_settlement_attempt_due
        ON lifecycle_settlement_attempt_state(
          source_id, classification, next_attempt_at_ms, claim_until_ms
        )
        """
    )
    _add_column_if_missing(conn, "trade_inbox", "broker_deal_key", "TEXT")
    _add_column_if_missing(
        conn,
        "trade_inbox",
        "identity_status",
        "TEXT NOT NULL DEFAULT 'bound'",
    )
    _add_column_if_missing(
        conn,
        "trade_inbox",
        "economic_payload_hash",
        "TEXT",
    )
    existing_attempt_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(lifecycle_settlement_attempt_state)"
        ).fetchall()
    }
    for column, sql_type in (
        (
            "invocation_writer_epoch",
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(typeof(invocation_writer_epoch) = 'integer' "
            "AND invocation_writer_epoch >= 0)",
        ),
        (
            "invocation_id",
            "TEXT CHECK(invocation_id IS NULL OR "
            "(typeof(invocation_id) = 'text' AND length(invocation_id) = 36))",
        ),
        (
            "invocation_state",
            "TEXT CHECK(invocation_state IS NULL OR invocation_state IN "
            "('reserved', 'provider_started', 'provider_finished', "
            "'ledger_committed', 'ambiguous_provider_result'))",
        ),
        (
            "invocation_attempted_at_ms",
            "INTEGER CHECK(invocation_attempted_at_ms IS NULL OR "
            "(typeof(invocation_attempted_at_ms) = 'integer' "
            "AND invocation_attempted_at_ms > 0))",
        ),
        (
            "pending_outcome_code",
            "INTEGER CHECK(pending_outcome_code IS NULL OR "
            "(typeof(pending_outcome_code) = 'integer' "
            "AND pending_outcome_code BETWEEN 1 AND 8))",
        ),
        *(
            (
                column,
                f"BLOB CHECK({column} IS NULL OR "
                f"(typeof({column}) = 'blob' AND length({column}) = 32))",
            )
            for column in (
                "pending_semantic_fingerprint",
                "pending_receipt_sha256",
                "pending_diagnostic_sha256",
                "committed_chain_sha256",
            )
        ),
        *(
            (
                column,
                f"TEXT CHECK({column} IS NULL OR typeof({column}) = 'text')",
            )
            for column in (
                "pending_outcome_kind",
                "pending_reason_code",
                "pending_provider_code",
                "pending_error_class",
            )
        ),
        (
            "pending_retry_after_ms",
            "INTEGER CHECK(pending_retry_after_ms IS NULL OR "
            "(typeof(pending_retry_after_ms) = 'integer' "
            "AND pending_retry_after_ms >= 0))",
        ),
        (
            "pending_control_now_ms",
            "INTEGER CHECK(pending_control_now_ms IS NULL OR "
            "(typeof(pending_control_now_ms) = 'integer' "
            "AND pending_control_now_ms > 0))",
        ),
        (
            "committed_audit_ordinal",
            "INTEGER CHECK(committed_audit_ordinal IS NULL OR "
            "(typeof(committed_audit_ordinal) = 'integer' "
            "AND committed_audit_ordinal > 0))",
        ),
    ):
        if column not in existing_attempt_columns:
            conn.execute(
                "ALTER TABLE lifecycle_settlement_attempt_state "
                f"ADD COLUMN {column} {sql_type}"
            )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_lifecycle_settlement_attempt_invocation_writer_fence
        BEFORE UPDATE ON lifecycle_settlement_attempt_state
        WHEN (
          OLD.invocation_state IS NOT NULL
          OR NEW.invocation_state IS NOT NULL
        ) AND (
          typeof(NEW.invocation_writer_epoch) != 'integer'
          OR NEW.invocation_writer_epoch
             != OLD.invocation_writer_epoch + 1
        )
        BEGIN
          SELECT RAISE(
            ABORT,
            'lifecycle settlement invocation requires current writer'
          );
        END
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_inbox_retry
        ON trade_inbox(status, updated_at_ms, received_at_ms)
        """
    )


def _payload_deal_id(payload: dict[str, Any]) -> str:
    for key in ("deal_id", "dealID", "dealId", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _canonical_inbox_economic_hash(
    source_key: str,
    payload: dict[str, Any],
) -> str:
    _broker, account, futu_account_id, _deal_id = (
        str(source_key).split(":", 3)
    )
    source = {
        **dict(payload or {}),
        "account": account,
        "futu_account_id": futu_account_id,
        "symbol": (
            payload.get("symbol")
            or payload.get("code")
            or payload.get("stock_code")
        ),
        "contracts": (
            payload.get("contracts")
            if payload.get("contracts") is not None
            else payload.get("qty")
            if payload.get("qty") is not None
            else payload.get("quantity")
        ),
        "event_time_ms": (
            payload.get("event_time_ms")
            or payload.get("trade_time_ms")
            or payload.get("execution_time_ms")
        ),
    }
    role = (
        "option_anchor"
        if (
            source.get("option_type")
            or source.get("optionType")
            or source.get("strike")
        )
        else "stock_settlement"
    )
    canonical = canonical_source_economic_payload(
        source_key=source_key,
        source_role=role,
        payload=source,
    )
    return canonical_source_payload_hash(canonical)


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    sql_type: str,
) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _read_settlement_attempt_row(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    account: str,
    case_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM lifecycle_settlement_attempt_state
        WHERE source_id = ? AND account = ? AND case_id = ?
        """,
        (source_id, account, case_id),
    ).fetchone()
    if row is None:
        raise SettlementAttemptClaimOwnershipLost(
            "settlement attempt state is unavailable"
        )
    return _settlement_attempt_row(row)


def _pending_settlement_control_updates(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    outcome = SettlementAttemptOutcome(
        kind=str(row["pending_outcome_kind"]),
        source_id=str(row["source_id"]),
        account=str(row["account"]),
        case_id=str(row["case_id"]),
        contract_version=str(row["collector_contract_version"]),
        capability_fingerprint=str(row["capability_fingerprint"]),
        reason_code=row.get("pending_reason_code"),
        provider_code=row.get("pending_provider_code"),
        error_class=row.get("pending_error_class"),
        retry_after_ms=row.get("pending_retry_after_ms"),
    )
    semantic = row.get("pending_semantic_fingerprint")
    return settlement_attempt_updates_after_outcome(
        row,
        outcome=outcome,
        now_ms=int(row["pending_control_now_ms"]),
        case_scope_fingerprint_value=str(
            row["case_scope_fingerprint"]
        ),
        provider_input_scope_fingerprint_value=str(
            row.get("provider_input_scope_fingerprint") or ""
        ),
        semantic_fingerprint=(
            semantic.hex() if type(semantic) is bytes else None
        ),
        provider_attempted=True,
    )


def _assert_pending_control_projection(
    row: Mapping[str, Any],
    projected: Mapping[str, Any],
) -> None:
    mismatched = [
        field
        for field in _SETTLEMENT_PENDING_CONTROL_FIELDS
        if row.get(field) != projected.get(field)
    ]
    if mismatched:
        raise ValueError(
            "pending settlement control projection mismatch: "
            + ",".join(mismatched)
        )


def _match_settlement_invocation_audit(
    current: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
) -> tuple[int, bytes]:
    if not isinstance(audit, Mapping):
        raise ValueError("settlement invocation audit receipt is unavailable")
    if audit.get("case_id") != current.get("case_id"):
        raise ValueError("settlement invocation audit case mismatch")
    expected_invocation = uuid.UUID(
        _canonical_uuid_text(current.get("invocation_id"))
    ).bytes
    if _audit_invocation_bytes(audit.get("invocation_id")) != expected_invocation:
        raise ValueError("settlement invocation audit identity mismatch")
    if (
        type(audit.get("attempted_at_ms")) is not int
        or audit.get("attempted_at_ms")
        != current.get("invocation_attempted_at_ms")
        or type(audit.get("outcome_code")) is not int
        or audit.get("outcome_code") != current.get("pending_outcome_code")
    ):
        raise ValueError("settlement invocation audit scalar mismatch")
    for field, pending_field in (
        ("semantic_fingerprint", "pending_semantic_fingerprint"),
        ("receipt_sha256", "pending_receipt_sha256"),
        ("diagnostic_sha256", "pending_diagnostic_sha256"),
    ):
        if _optional_sha256_blob(audit.get(field), field=field) != current.get(
            pending_field
        ):
            raise ValueError(f"settlement invocation audit {field} mismatch")

    ordinal = _positive_int(audit.get("ordinal"), field="audit.ordinal")
    last_ordinal = _positive_int(
        audit.get("last_ordinal"),
        field="audit.last_ordinal",
    )
    if ordinal != last_ordinal:
        raise ValueError("settlement invocation audit is not the current head")
    if (
        _audit_invocation_bytes(audit.get("last_invocation_id"))
        != expected_invocation
    ):
        raise ValueError("settlement invocation audit head identity mismatch")
    chain = _sha256_blob(
        audit.get("chain_sha256"),
        field="audit.chain_sha256",
    )
    span_ordinal = audit.get("span_ordinal")
    if int(current["pending_outcome_code"]) in (1, 2):
        _positive_int(span_ordinal, field="audit.span_ordinal")
    elif span_ordinal is not None:
        raise ValueError("failed settlement invocation audit carries a span")
    return ordinal, chain


def _audit_invocation_bytes(value: Any) -> bytes:
    if type(value) is bytes:
        if len(value) != 16:
            raise ValueError("settlement audit invocation_id must be UUIDv4 bytes")
        parsed = uuid.UUID(bytes=value)
        if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
            raise ValueError("settlement audit invocation_id must be UUIDv4 bytes")
        return value
    return uuid.UUID(_canonical_uuid_text(value)).bytes


def _optional_sha256_blob(value: Any, *, field: str) -> bytes | None:
    return None if value is None else _sha256_blob(value, field=field)


def _settlement_attempt_values(
    payload: dict[str, Any],
) -> tuple[Any, ...]:
    return (
        str(payload.get("source_id") or "").strip(),
        str(payload.get("account") or "").strip().lower(),
        str(payload.get("case_id") or "").strip(),
        str(payload.get("case_scope_fingerprint") or "").strip(),
        str(payload.get("provider_input_scope_fingerprint") or "").strip()
        or None,
        str(payload.get("collector_contract_version") or "").strip(),
        str(payload.get("capability_fingerprint") or "").strip(),
        str(payload.get("classification") or "unknown").strip(),
        str(payload.get("outcome_kind") or "").strip() or None,
        str(payload.get("reason_code") or "").strip() or None,
        str(payload.get("provider_code") or "").strip() or None,
        str(payload.get("error_class") or "").strip() or None,
        int(payload.get("attempt_count") or 0),
        int(payload.get("no_progress_count") or 0),
        (
            int(payload["next_attempt_at_ms"])
            if payload.get("next_attempt_at_ms") is not None
            else None
        ),
        (
            int(payload["last_attempt_at_ms"])
            if payload.get("last_attempt_at_ms") is not None
            else None
        ),
        str(payload.get("last_semantic_fingerprint") or "").strip()
        or None,
        str(payload.get("claim_id") or "").strip() or None,
        (
            int(payload["claim_until_ms"])
            if payload.get("claim_until_ms") is not None
            else None
        ),
        int(payload.get("updated_at_ms") or int(time.time() * 1000)),
    )


def _settlement_attempt_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        key: row[key]
        for key in row.keys()
    }
    _validate_settlement_invocation_fields(result)
    return result


def _validate_settlement_invocation_fields(
    row: Mapping[str, Any],
) -> None:
    writer_epoch = row.get("invocation_writer_epoch")
    if type(writer_epoch) is not int or writer_epoch < 0:
        raise ValueError(
            "settlement invocation_writer_epoch must be a nonnegative integer"
        )
    state_value = row.get("invocation_state")
    if state_value is None:
        if any(row.get(field) is not None for field in _SETTLEMENT_INVOCATION_FIELDS):
            raise ValueError(
                "settlement invocation fields require invocation_state"
            )
        return
    if type(state_value) is not str or state_value not in _SETTLEMENT_INVOCATION_STATES:
        raise ValueError("settlement invocation_state is invalid")
    _canonical_uuid_text(row.get("invocation_id"))

    if state_value == "reserved":
        _require_null_fields(
            row,
            (
                "invocation_attempted_at_ms",
                *_SETTLEMENT_PENDING_FIELDS,
                *_SETTLEMENT_COMMITTED_FIELDS,
            ),
        )
        return

    _positive_int(
        row.get("invocation_attempted_at_ms"),
        field="invocation_attempted_at_ms",
    )
    if state_value == "provider_started":
        _require_null_fields(
            row,
            (*_SETTLEMENT_PENDING_FIELDS, *_SETTLEMENT_COMMITTED_FIELDS),
        )
        return

    has_pending = row.get("pending_outcome_code") is not None
    if state_value == "ambiguous_provider_result" and not has_pending:
        _require_null_fields(
            row,
            (*_SETTLEMENT_PENDING_FIELDS, *_SETTLEMENT_COMMITTED_FIELDS),
        )
        if row.get("claim_id") is not None or row.get("claim_until_ms") is not None:
            raise ValueError("ambiguous settlement invocation cannot remain claimed")
        return
    _validate_pending_settlement_outcome(row)
    for field, pending_field in (
        ("outcome_kind", "pending_outcome_kind"),
        ("reason_code", "pending_reason_code"),
        ("provider_code", "pending_provider_code"),
        ("error_class", "pending_error_class"),
    ):
        if row.get(field) != row.get(pending_field):
            raise ValueError(
                f"pending settlement control field mismatch: {field}"
            )
    if state_value in {
        "provider_finished",
        "ambiguous_provider_result",
    }:
        _assert_pending_control_projection(
            row,
            _pending_settlement_control_updates(row),
        )

    if state_value == "ledger_committed":
        _positive_int(
            row.get("committed_audit_ordinal"),
            field="committed_audit_ordinal",
        )
        _sha256_blob(
            row.get("committed_chain_sha256"),
            field="committed_chain_sha256",
        )
        if row.get("claim_id") is not None or row.get("claim_until_ms") is not None:
            raise ValueError("committed settlement invocation cannot remain claimed")
        return
    _require_null_fields(row, _SETTLEMENT_COMMITTED_FIELDS)
    if state_value == "ambiguous_provider_result" and (
        row.get("claim_id") is not None
        or row.get("claim_until_ms") is not None
    ):
        raise ValueError("ambiguous settlement invocation cannot remain claimed")


def _validate_pending_settlement_outcome(
    row: Mapping[str, Any],
) -> None:
    outcome_code = _positive_int(
        row.get("pending_outcome_code"),
        field="pending_outcome_code",
    )
    audit_kind = _SETTLEMENT_AUDIT_KIND_BY_CODE.get(outcome_code)
    if audit_kind is None:
        raise ValueError("pending settlement outcome_code is unknown")
    control_kind = _required_text(
        row.get("pending_outcome_kind"),
        field="pending_outcome_kind",
    )
    if control_kind != _SETTLEMENT_CONTROL_KIND_BY_AUDIT_KIND[audit_kind]:
        raise ValueError("pending settlement outcome kind/code mismatch")
    _positive_int(
        row.get("pending_control_now_ms"),
        field="pending_control_now_ms",
    )
    for field in (
        "pending_reason_code",
        "pending_provider_code",
        "pending_error_class",
    ):
        _optional_text(row.get(field), field=field)
    retry_after = row.get("pending_retry_after_ms")
    if retry_after is not None and (
        type(retry_after) is not int or retry_after < 0
    ):
        raise ValueError("pending_retry_after_ms must be a nonnegative integer")

    semantic = row.get("pending_semantic_fingerprint")
    receipt = row.get("pending_receipt_sha256")
    diagnostic = row.get("pending_diagnostic_sha256")
    if outcome_code in (1, 2):
        _sha256_blob(semantic, field="pending_semantic_fingerprint")
        _sha256_blob(receipt, field="pending_receipt_sha256")
        if diagnostic is not None:
            raise ValueError(
                "observed pending settlement outcome carries diagnostic hash"
            )
        return
    if semantic is not None or receipt is not None:
        raise ValueError(
            "failed pending settlement outcome carries observation hashes"
        )
    diagnostic_value = _sha256_blob(
        diagnostic,
        field="pending_diagnostic_sha256",
    )
    expected_diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=row.get("pending_reason_code"),
        provider_code=row.get("pending_provider_code"),
        error_class=row.get("pending_error_class"),
    )
    if diagnostic_value != expected_diagnostic:
        raise ValueError("pending settlement diagnostic hash mismatch")


def _canonical_uuid_text(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("settlement invocation_id must be canonical UUIDv4 text")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            "settlement invocation_id must be canonical UUIDv4 text"
        ) from exc
    if (
        value != str(parsed)
        or parsed.version != 4
        or parsed.variant != uuid.RFC_4122
    ):
        raise ValueError("settlement invocation_id must be canonical UUIDv4 text")
    return value


def _sha256_blob(value: Any, *, field: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{field} must be exactly 32 bytes")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _required_text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty normalized text")
    return value


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field)


def _require_null_fields(
    row: Mapping[str, Any],
    fields: Iterable[str],
) -> None:
    present = [field for field in fields if row.get(field) is not None]
    if present:
        raise ValueError(
            "settlement invocation fields must be null: "
            + ",".join(present)
        )


__all__ = [
    "SETTLEMENT_ATTEMPT_MIN_LEASE_MS",
    "SettlementAttemptClaimOwnershipLost",
    "enqueue_trade_payload",
    "claim_settlement_attempt",
    "claim_settlement_provider_batch",
    "complete_settlement_attempt",
    "get_settlement_attempt_state",
    "list_settlement_attempt_states",
    "list_retryable_trade_payloads",
    "finish_settlement_attempt_provider_invocation",
    "mark_trade_payload_handled",
    "mark_trade_payload_retryable",
    "renew_settlement_attempt_claim",
    "renew_settlement_provider_batch_claim",
    "mark_settlement_attempt_provider_started",
    "reconcile_settlement_attempt_invocation",
    "replace_finished_settlement_attempt_provider_invocation",
    "release_settlement_provider_batch_claim",
    "reserve_settlement_attempt_invocation",
    "require_trade_inbox_store_readable",
    "settle_trade_payload_result",
    "settlement_attempt_summary",
    "trade_inbox_revision",
    "trade_inbox_summary",
    "upsert_settlement_attempt_state",
]
