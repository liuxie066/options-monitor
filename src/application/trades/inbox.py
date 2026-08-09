from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from src.application.ledger.api import (
    canonical_source_economic_payload,
    canonical_source_payload_hash,
)
from src.infrastructure.private_storage import connect_private_sqlite


SETTLEMENT_ATTEMPT_MIN_LEASE_MS = 120_000
_SETTLEMENT_ATTEMPT_QUERY_BATCH_SIZE = 400


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
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
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
                  updated_at_ms = excluded.updated_at_ms
                WHERE lifecycle_settlement_attempt_state.claim_id IS NULL
                   OR lifecycle_settlement_attempt_state.claim_id = ''
                   OR lifecycle_settlement_attempt_state.claim_until_ms IS NULL
                   OR lifecycle_settlement_attempt_state.claim_until_ms <= excluded.updated_at_ms
                   OR lifecycle_settlement_attempt_state.claim_id = excluded.claim_id
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
                SET claim_until_ms = ?
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
            row = conn.execute(
                """
                SELECT *
                FROM lifecycle_settlement_attempt_state
                WHERE source_id = ? AND account = ? AND case_id = ?
                """,
                (source_key, account_key, case_key),
            ).fetchone()
            if row is None:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement attempt state is unavailable"
                )
            current = _settlement_attempt_row(row)
            if str(current.get("claim_id") or "") != claim_key:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement attempt claim ownership changed"
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
                """
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
                    updated_at_ms = ?
                WHERE source_id = ? AND account = ? AND case_id = ?
                  AND claim_id = ?
                """,
                (*values[3:], source_key, account_key, case_key, claim_key),
            )
            if int(cursor.rowcount or 0) != 1:
                raise SettlementAttemptClaimOwnershipLost(
                    "settlement attempt claim ownership changed"
                )
            stored = conn.execute(
                """
                SELECT *
                FROM lifecycle_settlement_attempt_state
                WHERE source_id = ? AND account = ? AND case_id = ?
                """,
                (source_key, account_key, case_key),
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if stored is None:
        raise RuntimeError("settlement attempt state disappeared")
    return _settlement_attempt_row(stored)


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
            "eligible_count": 0,
            "earliest_next_attempt_at_ms": None,
            "last_state_change": None,
        }
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = _fetch_settlement_attempt_rows(
                conn,
                columns=(
                    "case_id, classification, outcome_kind, reason_code, "
                    "provider_code, error_class, next_attempt_at_ms, "
                    "claim_id, claim_until_ms, updated_at_ms"
                ),
                source_id=source_key,
                account=account_key,
                case_ids=normalized_case_ids,
            )
    provider_rows = [
        row
        for row in rows
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
        rows,
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
    return {
        key: row[key]
        for key in row.keys()
    }


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
    "mark_trade_payload_handled",
    "mark_trade_payload_retryable",
    "renew_settlement_attempt_claim",
    "renew_settlement_provider_batch_claim",
    "release_settlement_provider_batch_claim",
    "require_trade_inbox_store_readable",
    "settle_trade_payload_result",
    "settlement_attempt_summary",
    "trade_inbox_revision",
    "trade_inbox_summary",
    "upsert_settlement_attempt_state",
]
