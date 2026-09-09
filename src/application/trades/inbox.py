from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from domain.domain.source_evidence import build_source_evidence
from domain.domain.trade_account_identity import extract_primary_account_id

from src.application.ledger.api import (
    LIFECYCLE_ATTEMPT_OUTCOME_CODES,
    applied_execution_association_conflicts,
    read_execution_event_candidates,
    execution_identity_from_input,
    with_sqlite_repo_writer_lock,
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
TRADE_EVIDENCE_SET_REF_PREFIX = "trade-inbox-evidence-set:v1:"
LEGACY_ADAPTER_VERSION = "legacy/unversioned"
_UNOBSERVED_RECEIPT_RESULT = object()
TRADE_INTAKE_ADAPTER_VERSIONS = {
    "push": "om.trade-intake.push.v1",
    "backfill": "om.trade-intake.history.v1",
    "file": "om.trade-intake.execution-jsonl.v1",
}
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
    repo: Any = None,
    delivery_purpose: str | None = None,
    adapter_version: str = LEGACY_ADAPTER_VERSION,
) -> str:
    """Persist every source version before any economic use; never clear conflicts."""
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    deal_id = _payload_deal_id(payload)
    source_text = str(source or "unknown").strip().lower() or "unknown"
    canonical_key = str(broker_deal_key or "").strip()
    content = _inbox_execution_content(canonical_key, payload) if canonical_key else {}
    economic_hash = _execution_content_hash(content) if canonical_key else None
    identity = canonical_key or f"identity-needs-review|{source_text}|{payload_hash}"
    inbox_id = hashlib.sha256(identity.encode()).hexdigest()
    purpose = delivery_purpose or ("historical" if source_text == "file" else "live")
    if purpose not in {"historical", "live"}:
        raise ValueError("invalid trade delivery purpose")
    now_ms = int(time.time() * 1000)
    with with_sqlite_repo_writer_lock(repo), closing(_connect(inbox_path)) as conn, conn:
        _ensure_schema(conn)
        existing = conn.execute("SELECT inbox_id FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        # Grant recovery only when this durable reception precedes the economic effect.
        # Existing rows (including migrated rows with 0) never gain permission on replay.
        receipt_recovery_allowed = int(
            existing is None and _has_persisted_execution(repo, canonical_key, payload) is False
        )
        conn.execute(
            """INSERT INTO trade_inbox (
                inbox_id, source, deal_id, broker_deal_key, identity_status,
                payload_json, economic_payload_hash, status, attempt_count,
                received_at_ms, updated_at_ms, last_error, result_status,
                result_reason, delivery_purpose, receipt_recovery_allowed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(inbox_id) DO NOTHING""",
            (inbox_id, source_text, deal_id or None, canonical_key or None,
             "bound" if canonical_key else "identity_needs_review", payload_json,
             economic_hash, "pending" if canonical_key else "identity_needs_review",
             now_ms, now_ms, None if canonical_key else "canonical_broker_identity_missing",
             None, None, purpose, receipt_recovery_allowed),
        )
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        assert row is not None
        # Retain the pre-migration payload too; an old hash is not a new-contract fingerprint.
        original = json.loads(row["payload_json"])
        original_json = str(row["payload_json"])
        prior_evidence = conn.execute(
            "SELECT payload_json FROM trade_inbox_evidence WHERE inbox_id = ?", (inbox_id,)
        ).fetchall()
        known_before = _known_execution_associations(
            canonical_key, [original, *(json.loads(item[0]) for item in prior_evidence)]
        ) if canonical_key else {}
        for raw_json, raw_source, raw_received_at_ms, raw_adapter_version in (
            (
                original_json,
                str(row["source"]),
                int(row["received_at_ms"]),
                adapter_version if existing is None else LEGACY_ADAPTER_VERSION,
            ),
            (payload_json, source_text, now_ms, adapter_version),
        ):
            _insert_trade_payload_evidence(
                conn,
                inbox_id=inbox_id,
                source=raw_source,
                payload_json=raw_json,
                received_at_ms=raw_received_at_ms,
                broker_deal_key=canonical_key,
                adapter_version=raw_adapter_version,
            )
        if canonical_key:
            from domain.domain.trade_execution import conflicting_execution_associations
            original_content = _inbox_execution_content(canonical_key, original)
            original_hash = _execution_content_hash(original_content)
            # Missing associations may be enriched, but every known value remains evidence.
            evidence = conn.execute(
                "SELECT payload_json FROM trade_inbox_evidence WHERE inbox_id = ?", (inbox_id,)
            ).fetchall()
            conflict = original_hash != economic_hash or any(
                conflicting_execution_associations(
                    _inbox_execution_content(canonical_key, json.loads(item[0])), content
                ) for item in evidence
            )
            applied_conflicts = applied_execution_association_conflicts(repo, canonical_key, content)
            if conflict or applied_conflicts:
                reason = ("broker_economic_payload_conflict" if conflict else
                          "broker_applied_association_conflict")
                conn.execute(
                    """UPDATE trade_inbox SET status = 'conflict',
                    payload_version = payload_version + 1, claim_id = NULL, claim_until_ms = NULL,
                    updated_at_ms = ?, last_error = ?,
                    result_status = 'conflict', result_reason = ?
                    WHERE inbox_id = ? AND status != 'conflict'""", (now_ms, reason, reason, inbox_id),
                )
            elif str(row["economic_payload_hash"] or "") != original_hash:
                conn.execute("UPDATE trade_inbox SET economic_payload_hash = ? WHERE inbox_id = ?",
                             (original_hash, inbox_id))
            if not conflict and not applied_conflicts and any(
                value is not None and name not in known_before
                for name, value in content.get("associations", {}).items()
            ):
                prior_effect = _has_persisted_execution(repo, canonical_key, payload)
                suppress_delivery = prior_effect is not False or (
                    row["result_status"] == "skipped" and row["result_reason"] == "not_option_deal"
                )
                conn.execute(
                    """UPDATE trade_inbox SET payload_version = payload_version + 1,
                       claim_id = NULL, claim_until_ms = NULL, updated_at_ms = ?,
                       next_attempt_at_ms = CASE WHEN status = 'handled' THEN 0
                           ELSE next_attempt_at_ms END,
                       receipt_recovery_allowed = CASE
                           WHEN status = 'handled' AND ? THEN 0
                           ELSE receipt_recovery_allowed END,
                       last_error = CASE WHEN status = 'handled' THEN 'execution_association_enrichment'
                           ELSE last_error END,
                       status = 'pending'
                       WHERE inbox_id = ? AND status IN ('pending', 'handled')""",
                    (now_ms, suppress_delivery, inbox_id),
                )
    return inbox_id


def trade_payload_evidence_ref(inbox_id: str) -> str:
    value = str(inbox_id or "").strip()
    if not value:
        raise ValueError("inbox_id is required")
    return TRADE_EVIDENCE_SET_REF_PREFIX + value


def read_trade_source_evidence(
    path: str | Path,
    *,
    evidence_ref: str,
    read_only: bool = False,
) -> list[dict[str, Any]]:
    """Resolve an execution evidence-set ref, or one direct evidence ref."""

    inbox_path = Path(path)
    if not inbox_path.exists():
        return []
    ref = str(evidence_ref or "").strip()
    if ref.startswith(TRADE_EVIDENCE_SET_REF_PREFIX):
        column, value = "e.inbox_id", ref.removeprefix(TRADE_EVIDENCE_SET_REF_PREFIX)
    elif ref.startswith("source-evidence:v1:"):
        column, value = "e.evidence_id", ref
    else:
        raise ValueError("unsupported trade source evidence ref")
    if not value:
        raise ValueError("trade source evidence ref is incomplete")
    connection = (
        sqlite3.connect(f"{inbox_path.resolve().as_uri()}?mode=ro", uri=True)
        if read_only
        else _connect(inbox_path)
    )
    connection.row_factory = sqlite3.Row
    with closing(connection) as conn:
        if not read_only:
            _ensure_schema(conn)
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(trade_inbox_evidence)")}
        if column.endswith("evidence_id") and "evidence_id" not in columns:
            return []
        rows = conn.execute(
            f"""SELECT e.*, i.broker_deal_key
                FROM trade_inbox_evidence e
                LEFT JOIN trade_inbox i ON i.inbox_id = e.inbox_id
                WHERE {column} = ?
                ORDER BY e.received_at_ms, e.source, e.payload_hash""",
            (value,),
        ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        envelope = (
            json.loads(row["evidence_json"])
            if "evidence_json" in row.keys() and row["evidence_json"]
            else _build_trade_source_evidence(
                inbox_id=str(row["inbox_id"]),
                source=str(row["source"]),
                payload=payload,
                payload_hash=str(row["payload_hash"]),
                received_at_ms=int(row["received_at_ms"]),
                broker_deal_key=str(row["broker_deal_key"] or ""),
                adapter_version=LEGACY_ADAPTER_VERSION,
            )
        )
        out.append({**envelope, "raw_payload": payload})
    return out


def _insert_trade_payload_evidence(
    conn: sqlite3.Connection,
    *,
    inbox_id: str,
    source: str,
    payload_json: str,
    received_at_ms: int,
    broker_deal_key: str,
    adapter_version: str,
) -> None:
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    evidence = _build_trade_source_evidence(
        inbox_id=inbox_id,
        source=source,
        payload=json.loads(payload_json),
        payload_hash=payload_hash,
        received_at_ms=received_at_ms,
        broker_deal_key=broker_deal_key,
        adapter_version=adapter_version,
    )
    conn.execute(
        """INSERT INTO trade_inbox_evidence
        (inbox_id, source, payload_hash, payload_json, received_at_ms, evidence_id, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(inbox_id, source, payload_hash) DO NOTHING""",
        (
            inbox_id,
            source,
            payload_hash,
            payload_json,
            int(received_at_ms),
            evidence["evidence_id"],
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        ),
    )


def _build_trade_source_evidence(
    *,
    inbox_id: str,
    source: str,
    payload: dict[str, Any],
    payload_hash: str,
    received_at_ms: int,
    broker_deal_key: str,
    adapter_version: str,
) -> dict[str, Any]:
    execution = payload.get("execution_input")
    execution = execution if isinstance(execution, Mapping) else payload
    source_context = payload.get("_trade_intake_source")
    source_context = source_context if isinstance(source_context, Mapping) else {}
    file_context = payload.get("_trade_intake_file_evidence")
    file_context = file_context if isinstance(file_context, Mapping) else {}
    account_ref = execution.get("broker_account_ref")
    account_ref = account_ref if isinstance(account_ref, Mapping) else {}
    source_id = (
        source_context.get("source_id")
        or (f"file:sha256:{file_context['file_sha256']}" if file_context.get("file_sha256") else None)
        or source
    )
    account = (
        account_ref.get("broker_account_id")
        or payload.get("broker_account_id")
        or source_context.get("account")
        or account_ref.get("account_label")
        or payload.get("internal_account")
        or payload.get("account")
    )
    data_type = str(execution.get("data_type") or payload.get("data_type") or "execution").strip().lower()
    if data_type in {"deal", "fill", "trade"}:
        data_type = "execution"
    namespace = execution.get("external_id_namespace") or payload.get("external_id_namespace")
    record_id = (
        execution.get("external_execution_id")
        or payload.get("external_execution_id")
        or _payload_deal_id(payload)
    )
    source_record_identity = str(broker_deal_key or "").strip()
    if not source_record_identity and namespace and record_id:
        source_record_identity = f"{namespace}:{record_id}"
    if not source_record_identity:
        source_record_identity = f"payload-sha256:{payload_hash}"
    payload_version = execution.get("schema_version") or payload.get("schema_version") or "provider/unversioned"
    original_time = next(
        (
            execution.get(name)
            for name in ("source_time", "occurred_at_utc")
            if execution.get(name) is not None
        ),
        None,
    )
    if original_time is None:
        original_time = next(
            (payload.get(name) for name in ("trade_time_ms", "create_time", "updated_time") if payload.get(name) is not None),
            None,
        )
    source_timezone = execution.get("source_timezone") or payload.get("source_timezone")
    if not source_timezone and execution.get("occurred_at_utc"):
        source_timezone = "UTC"
    if not source_timezone and source_context.get("opend_process") == "FutuOpenD":
        source_timezone = "Asia/Shanghai"
    if not source_timezone and payload.get("create_time") is not None and adapter_version in {
        TRADE_INTAKE_ADAPTER_VERSIONS["push"],
        TRADE_INTAKE_ADAPTER_VERSIONS["backfill"],
    }:
        source_timezone = "Asia/Shanghai"
    evidence = build_source_evidence(
        source=source,
        source_id=str(source_id),
        account=str(account) if account is not None else None,
        data_type=data_type,
        source_record_identity=source_record_identity,
        payload_version=str(payload_version),
        content_digest=payload_hash,
        received_at_ms=received_at_ms,
        original_time=original_time,
        source_timezone=str(source_timezone) if source_timezone is not None else None,
        adapter_version=adapter_version,
    )
    return {**evidence, "evidence_set_ref": trade_payload_evidence_ref(inbox_id)}


def _has_persisted_execution(repo: Any, execution_id: str, payload: dict[str, Any]) -> bool | None:
    """Return False only when complete local reads prove no prior execution effect."""
    from src.application.trades.deal_identity import (
        broker_deal_key_from_payload,
        structured_deal_ids_from_assigned_stock_event,
        structured_deal_ids_from_ledger_event,
    )

    candidate = getattr(repo, "primary_repo", repo)
    reads = [getattr(candidate, name, None) for name in ("list_trade_events", "list_assigned_stock_events")]
    if not execution_id.startswith("execution:v1:") or not all(callable(read) for read in reads):
        return None
    source = payload.get("execution_input") if isinstance(payload.get("execution_input"), dict) else payload
    source_id = str(source.get("external_execution_id") or _payload_deal_id(source)).strip()
    source_namespace = str(source.get("external_id_namespace") or source.get("execution_id_namespace") or "").strip()
    source_account = _inbox_execution_content(execution_id, payload)["economic"]["account"]
    uncertain = False
    for rows in read_execution_event_candidates(repo, execution_id):
        # Missing/protocol-only readers can retain evidence, but cannot authorize delivery.
        if not isinstance(rows, (list, tuple)):
            uncertain = True
            continue
        for event in rows:
            if not isinstance(event, dict):
                uncertain = True
                continue
            raw = event if event.get("stock_event_id") else event.get("raw_payload") or {}
            if not isinstance(raw, dict):
                uncertain = True
                continue
            source_ids = (structured_deal_ids_from_assigned_stock_event(event) if event.get("stock_event_id")
                          else structured_deal_ids_from_ledger_event(event))
            source_ids.update(filter(None, (str(raw.get("external_execution_id") or "").strip(), _payload_deal_id(raw))))
            # Ledger's account field may be an internal label, never physical-account proof.
            identity_raw = {name: value for name, value in raw.items() if name != "account"}
            stored_id = execution_identity_from_input(raw.get("execution_input"))
            stored_ids = {stored_id} if stored_id else {
                broker_deal_key_from_payload({**identity_raw, "deal_id": value}, account_mapping=None)
                for value in source_ids
            }
            if execution_id in stored_ids:
                return True
            if stored_ids and all(value.startswith("execution:v1:") for value in stored_ids):
                continue
            if not source_id or source_id not in source_ids:
                continue
            # A matching old fill without complete scope is ambiguous, not a new fill.
            stored_account = _inbox_execution_content("", identity_raw)["economic"]["account"]
            if any(stored_account.get(name) and source_account.get(name)
                   and stored_account[name] != source_account[name]
                   for name in ("broker_id", "external_account_id", "environment")):
                continue
            stored_namespace = str(raw.get("external_id_namespace") or raw.get("execution_id_namespace") or "").strip()
            if stored_namespace and source_namespace and stored_namespace != source_namespace:
                continue
            uncertain = True
    return None if uncertain else False


def claim_trade_payload(path: str | Path, *, inbox_id: str, repo: Any = None,
                        lease_ms: int = 120_000, owner: str = "worker") -> dict[str, Any] | None:
    now_ms = int(time.time() * 1000)
    token = uuid.uuid4().hex
    with with_sqlite_repo_writer_lock(repo), closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        changed = conn.execute(
            """UPDATE trade_inbox SET claim_id = ?, claim_until_ms = ?, claim_owner = ?,
                attempt_count = attempt_count + 1, next_attempt_at_ms = ?
            WHERE inbox_id = ? AND status = 'pending' AND attempt_count < 20
              AND (claim_id IS NULL OR claim_until_ms <= ?)
              AND next_attempt_at_ms <= ?
              AND (result_json IS NULL OR json_extract(result_json, '$.receipt_kind') IS NULL
                   OR json_extract(result_json, '$.receipt_kind') = 'pending_retry'
                   OR (last_error = 'execution_association_enrichment'
                       AND json_extract(result_json, '$.receipt_kind') IN ('recorded', 'manual_required')))""",
            (token, now_ms + max(1, int(lease_ms)), str(owner), now_ms + 60_000, inbox_id, now_ms, now_ms),
        ).rowcount
        if not changed:
            return None
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        evidence = conn.execute(
            "SELECT payload_json FROM trade_inbox_evidence WHERE inbox_id = ?", (inbox_id,)
        ).fetchall()
        payload = json.loads(row["payload_json"])
        source_key = str(row["broker_deal_key"] or "")
        original_associations = _known_execution_associations(source_key, [payload])
        associations = _known_execution_associations(
            source_key, [payload, *(json.loads(item[0]) for item in evidence)],
        )
        return {**dict(row), "payload": payload,
                "association_enrichment": bool(row["result_json"] and not row["receipt_recovery_allowed"]),
                "new_associations": bool(source_key.startswith("execution:v1:")
                                         and associations.keys() - original_associations.keys()),
                "associations": associations}


def mark_trade_payload_review(path: str | Path, *, inbox_id: str, errors: list[str], repo: Any = None) -> None:
    with with_sqlite_repo_writer_lock(repo), closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        conn.execute(
            """UPDATE trade_inbox SET status = 'identity_needs_review', last_error = ?,
               claim_id = NULL, claim_until_ms = NULL WHERE inbox_id = ? AND status = 'pending'""",
            (json.dumps(errors, ensure_ascii=False), inbox_id),
        )


class TradePayloadClaimLost(RuntimeError):
    """A conflict, lease takeover or completed result invalidated this worker."""


@contextmanager
def trade_payload_commit_scope(path: str | Path, *, claim: Mapping[str, Any], repo: Any):
    # ponytail: reuse the ledger-wide lock; consider finer locks only after measuring contention.
    with with_sqlite_repo_writer_lock(repo):
        with closing(_connect(Path(path))) as conn:
            _ensure_schema(conn)
            row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (claim["inbox_id"],)).fetchone()
            conn.commit()
            if (row is None or row["status"] != "pending"
                    or row["claim_id"] != claim["claim_id"]
                    or row["payload_version"] != claim["payload_version"]
                    or row["economic_payload_hash"] != claim["economic_payload_hash"]):
                raise TradePayloadClaimLost("trade inbox claim no longer permits economic commit")
        # The Inbox connection must close before a Ledger writer opens its SQL transaction.
        yield


def _receipt_envelope(raw: str | None) -> dict[str, Any] | None:
    value = json.loads(raw) if raw else None
    if value is not None and not isinstance(value, dict):
        raise ValueError("invalid trade receipt envelope")
    if value is not None and "schema_version" in value:
        if (value["schema_version"] != 2 or not isinstance(value.get("receipts"), dict)
                or value.get("current_result_key") not in value["receipts"]):
            raise ValueError("unsupported trade receipt envelope")
    return value


def _trade_payload_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json"))
    out["result"] = json.loads(out.pop("result_json", None) or "null")
    envelope = _receipt_envelope(out.pop("receipt_json", None))
    out["receipt_envelope"] = envelope
    out["receipt"] = (envelope["receipts"][envelope["current_result_key"]]
                      if envelope and envelope.get("schema_version") == 2 else envelope)
    return out


def read_trade_payload(path: str | Path, *, inbox_id: str, read_only: bool = False) -> dict[str, Any] | None:
    if not Path(path).exists():
        return None
    connection = (sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
                  if read_only else _connect(Path(path)))
    connection.row_factory = sqlite3.Row
    with closing(connection) as conn, conn:
        if not read_only:
            _ensure_schema(conn)
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
    return _trade_payload_row(row) if row is not None else None


def _trade_result_policy(row: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = result.get("diagnostics") or {}
    status = result.get("status")
    retryable = (status in {"failed", "unresolved"} and bool(diagnostics.get("retryable"))
                 and not diagnostics.get("broker_evidence_accepted")
                 and result.get("reason") not in {"waiting_settlement_evidence", "awaiting_out_of_order_pair",
                                                  "awaiting_settlement_evidence", "lifecycle_conflict_requires_review"})
    count = int(row["attempt_count"])
    kind = result.get("receipt_kind")
    if kind is None:
        kind = ("verification_pending" if diagnostics.get("verification_pending") else
                "recorded" if status == "applied" else
                "pending_retry" if retryable and count < 20 else
                "manual_required" if status in {"failed", "unresolved"} else None)
    if kind not in {None, "pending_retry", "recorded", "manual_required", "verification_pending"}:
        raise ValueError("invalid trade receipt result kind")
    if kind == "pending_retry" and count >= 20:
        kind = "manual_required"
    retryable = bool(retryable and count < 20 and kind == "pending_retry")
    return {**result, "receipt_kind": kind, "receipt_result_key": kind,
            "retry_policy": {"attempt_count": count, "max_attempts": 20,
                             "remaining": max(0, 20 - count), "retryable": retryable,
                             "retry_delay_sec": 60}}


def _prepare_trade_receipt_result(conn: sqlite3.Connection, row: Mapping[str, Any],
                                   result: dict[str, Any]) -> dict[str, Any]:
    enriched = _trade_result_policy(row, result)
    kind = enriched["receipt_kind"]
    envelope = _receipt_envelope(row["receipt_json"])
    old_result = json.loads(row["result_json"] or "null") or {}
    if envelope is not None and envelope.get("schema_version") != 2:
        # Old delivery evidence can only be assigned semantics from its saved business result.
        old_kind = _trade_result_policy(row, old_result)["receipt_kind"]
        old_diagnostics = old_result.get("diagnostics") or {}
        old_failed = old_result.get("status") == "failed" or (
            old_result.get("status") == "unresolved" and old_diagnostics.get("retryable")
            and not old_diagnostics.get("broker_evidence_accepted")
            and not old_diagnostics.get("verification_pending"))
        if not old_kind or not old_failed:
            enriched["receipt_suppression_reason"] = "legacy_receipt_history_unproven"
            envelope = None
            kind = None
        else:
            envelope = {"schema_version": 2, "current_result_key": old_kind,
                        "receipts": {old_kind: {**envelope, "result_key": old_kind,
                                                "receipt_kind": old_kind, "business_result": old_result,
                                                "attempt_count": int(bool(envelope.get("attempt_id"))),
                                                "legacy": True}}}
    elif envelope is None and (not row["receipt_recovery_allowed"] or (
            row["status"] == "handled" and old_result.get("status") == "applied"
            and not old_result.get("receipt_kind"))):
        # New caller claims can process legacy rows, but cannot authorize historical delivery.
        enriched["receipt_suppression_reason"] = "legacy_receipt_history_unproven"
        kind = None
    lifecycle_owned = (result.get("receipt_notification_owner") == "lifecycle_outbox"
                       or (result.get("diagnostics") or {}).get("notification_authority") == "lifecycle_outbox")
    if lifecycle_owned:
        kind = None
        if envelope and envelope.get("schema_version") == 2:
            previous = envelope["receipts"][envelope["current_result_key"]]
            previous.update(superseded_by="lifecycle_outbox", stop_reason="lifecycle_outbox_handoff")
    if kind is not None and row["delivery_purpose"] == "live":
        envelope = envelope or {"schema_version": 2, "current_result_key": kind, "receipts": {}}
        receipts = envelope["receipts"]
        current = envelope["current_result_key"]
        if current == "recorded" and kind != "recorded" and not (
                kind == "verification_pending" and (result.get("diagnostics") or {}).get("verification_pending")):
            raise TradePayloadClaimLost("recorded trade result cannot regress")
        if current != kind and current in receipts:
            receipts[current].update(superseded_by=kind, stop_reason="result_superseded")
        if kind not in receipts:
            receipts[kind] = {"result_key": kind, "receipt_kind": kind,
                              "receipt_id": f"trade-receipt:{row['inbox_id']}" + (f":{kind}" if receipts else ""),
                              "status": "pending", "attempt_count": 0,
                              "business_result": enriched,
                              "payload": enriched.get("_receipt_payload"),
                              "payload_version": row["payload_version"],
                              "economic_payload_hash": row["economic_payload_hash"],
                              "created_at_ms": int(time.time() * 1000)}
        # Revisited semantics retain their original send evidence and frozen content.
        receipts[kind].pop("superseded_by", None)
        if receipts[kind].get("stop_reason") == "result_superseded":
            receipts[kind].pop("stop_reason")
        envelope["current_result_key"] = kind
        legacy_evidence = result.get("_receipt_legacy_evidence") or {}
        if (kind == "recorded" and legacy_evidence.get("blocked")
                and receipts[kind].get("status") not in {"sent", "unknown"}):
            confirmed = legacy_evidence.get("status") == "confirmed"
            receipts[kind].update(status="sent" if confirmed else "unknown",
                                  stop_reason="legacy_compensation_owned",
                                  result={"delivery_confirmed": confirmed,
                                          "legacy_compensation_evidence": legacy_evidence})
        receipt_json = json.dumps(envelope, ensure_ascii=False, default=str)
    else:
        receipt_json = (json.dumps(envelope, ensure_ascii=False, default=str)
                        if lifecycle_owned and envelope and envelope.get("schema_version") == 2
                        else row["receipt_json"])
    conn.execute("""UPDATE trade_inbox SET result_json = ?, result_status = ?, result_reason = ?,
                    receipt_json = ? WHERE inbox_id = ?""",
                 (json.dumps(enriched, ensure_ascii=False, default=str), enriched.get("status"),
                  enriched.get("reason"), receipt_json, row["inbox_id"]))
    return enriched


def save_trade_payload_result(path: str | Path, *, claim: Mapping[str, Any],
                              result: dict[str, Any]) -> dict[str, Any]:
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (claim["inbox_id"],)).fetchone()
        if (row is None or row["status"] != "pending" or row["claim_id"] != claim["claim_id"]
                or row["payload_version"] != claim["payload_version"]
                or row["economic_payload_hash"] != claim["economic_payload_hash"]):
            raise TradePayloadClaimLost("trade result claim lost")
        enriched = _prepare_trade_receipt_result(conn, row, result)
        intent = result.get("portfolio_refresh_intent")
        if isinstance(intent, Mapping):
            _record_trade_payload_refresh_intent(conn, inbox_id=claim["inbox_id"], intent=intent)
        return enriched


def prepare_trade_receipt_result(path: str | Path, *, inbox_id: str, result: dict[str, Any],
                                 expected_payload_version: int | None = None,
                                 expected_result: Any = _UNOBSERVED_RECEIPT_RESULT) -> dict[str, Any]:
    """Reconcile readback without an economic claim; compare the exact observed result."""
    if expected_payload_version is None and expected_result is _UNOBSERVED_RECEIPT_RESULT:
        raise TradePayloadClaimLost("receipt recovery requires observed result or payload version")
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        if (row is None or row["status"] == "conflict"
                or (row["claim_id"] and int(row["claim_until_ms"] or 0) > int(time.time() * 1000))
                or (expected_payload_version is not None and row["payload_version"] != expected_payload_version)
                or (expected_result is not _UNOBSERVED_RECEIPT_RESULT
                    and json.loads(row["result_json"] or "null") != expected_result)):
            raise TradePayloadClaimLost("receipt recovery observation changed")
        enriched = _prepare_trade_receipt_result(conn, row, result)
        # Readback can settle a crashed claim without consuming another economic attempt.
        status = "pending" if enriched["receipt_kind"] in {"pending_retry", "verification_pending"} else "handled"
        next_attempt_at_ms = (int(time.time() * 1000) + 60_000
                              if enriched["receipt_kind"] == "verification_pending"
                              else row["next_attempt_at_ms"])
        conn.execute("""UPDATE trade_inbox SET status = ?, claim_id = NULL, claim_until_ms = NULL,
                        updated_at_ms = ?, next_attempt_at_ms = ? WHERE inbox_id = ?""",
                     (status, int(time.time() * 1000), next_attempt_at_ms, inbox_id))
        return enriched


def begin_trade_receipt_attempt(path: str | Path, *, inbox_id: str,
                                route: dict[str, Any], message: str,
                                claim: Mapping[str, Any] | None = None,
                                result_key: str | None = None) -> dict[str, Any]:
    """Claim the current semantic result; mark unknown before any external I/O."""
    now_ms = int(time.time() * 1000)
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        if claim is not None and (
            row is None or row["status"] != "pending" or row["claim_id"] != claim.get("claim_id")
            or row["payload_version"] != claim.get("payload_version")
            or row["economic_payload_hash"] != claim.get("economic_payload_hash")
        ):
            raise TradePayloadClaimLost("trade receipt claim no longer permits delivery")
        if row is None or row["status"] == "conflict" or row["delivery_purpose"] == "historical":
            return {"claimed": False, "status": "suppressed"}
        if claim is None and row["claim_id"] and int(row["claim_until_ms"] or 0) > now_ms:
            return {"claimed": False, "status": "pending", "reason": "economic_claim_active"}
        envelope = _receipt_envelope(row["receipt_json"])
        if envelope is None:
            return {"claimed": False, "status": "suppressed", "reason": "durable_receipt_intent_missing"}
        if envelope.get("schema_version") != 2:
            return {**envelope, "claimed": False}
        key = envelope["current_result_key"]
        frozen = envelope["receipts"][key]
        if result_key is not None and result_key != key:
            raise TradePayloadClaimLost("trade receipt result superseded")
        if frozen.get("status") not in {"pending", "failed"} or frozen.get("stop_reason"):
            return {**frozen, "claimed": False}
        if frozen.get("route") is not None and frozen["route"] != route:
            frozen["stop_reason"] = "route_changed_requires_review"
        elif int(frozen.get("attempt_count", 0)) >= 20:
            frozen["stop_reason"] = "notification_attempts_exhausted"
        elif int(frozen.get("next_attempt_at_ms", 0)) > now_ms:
            return {**frozen, "claimed": False}
        elif not route:
            return {**frozen, "claimed": False, "reason": "route_unavailable"}
        else:
            if frozen.get("attempt_id"):
                frozen.setdefault("attempts", []).append({name: frozen.get(name) for name in
                    ("attempt_id", "attempted_at_ms", "status", "result")})
            frozen.update(status="unknown", attempt_id=uuid.uuid4().hex, attempted_at_ms=now_ms,
                          attempt_count=int(frozen.get("attempt_count", 0)) + 1,
                          route=frozen.get("route", route), message=frozen.get("message", message))
            frozen.pop("result", None)
        conn.execute("UPDATE trade_inbox SET receipt_json = ? WHERE inbox_id = ?",
                     (json.dumps(envelope, ensure_ascii=False), inbox_id))
        return {**frozen, "claimed": not bool(frozen.get("stop_reason"))}


def finish_trade_receipt_attempt(path: str | Path, *, inbox_id: str, attempt_id: str,
                                 result: dict[str, Any]) -> None:
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT receipt_json FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        envelope = _receipt_envelope(row[0]) if row else None
        entries = list(envelope["receipts"].values()) if envelope and envelope.get("schema_version") == 2 else [envelope]
        attempts = [attempt for entry in entries if entry
                    for attempt in [entry, *entry.get("attempts", [])]]
        frozen = next((attempt for attempt in attempts if attempt.get("attempt_id") == attempt_id), None)
        if frozen is None:
            raise TradePayloadClaimLost("trade receipt attempt changed")
        # A repeated/contradictory callback cannot reopen confirmed or explicitly rejected attempts.
        if frozen.get("result") is not None:
            return
        status = ("sent" if result.get("delivery_confirmed") else
                  "failed" if result.get("explicit_pre_acceptance_failure") else "unknown")
        frozen.update(result=result, status=status)
        if status == "failed":
            frozen["next_attempt_at_ms"] = int(time.time() * 1000) + 60_000
            if int(frozen.get("attempt_count", 0)) >= 20:
                frozen["stop_reason"] = "notification_attempts_exhausted"
        conn.execute("UPDATE trade_inbox SET receipt_json = ? WHERE inbox_id = ?",
                     (json.dumps(envelope, ensure_ascii=False), inbox_id))


def list_trade_receipt_recovery_rows(path: str | Path, *, account_ids: Iterable[str],
                                     limit: int = 100) -> list[dict[str, Any]]:
    allowed = {str(value).strip() for value in account_ids if str(value).strip()}
    if not allowed or not Path(path).exists():
        return []
    now_ms = int(time.time() * 1000)
    out = []
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        rows = conn.execute("""SELECT * FROM trade_inbox WHERE delivery_purpose = 'live'
            AND receipt_recovery_allowed = 1 AND source IN ('push', 'backfill')
            AND status != 'conflict' AND (claim_id IS NULL OR claim_until_ms <= ?)
            AND (result_json IS NOT NULL OR attempt_count > 0)
            ORDER BY updated_at_ms, received_at_ms, inbox_id""", (now_ms,))
        for row in rows:
            item = _trade_payload_row(row)
            payload = item["payload"]
            execution = payload.get("execution_input") or payload
            ref = execution.get("broker_account_ref") or {}
            physical = str(ref.get("external_account_id") or extract_primary_account_id(payload) or "").strip()
            if physical not in allowed:
                continue
            receipt = item["receipt"] or {}
            if (item["status"] == "pending" and item["attempt_count"] == 0
                    and (item["result"] or {}).get("receipt_kind") != "verification_pending"):
                # An unclaimed/resumed execution belongs to processing, not receipt readback.
                continue
            needs_readback = (item["status"] == "pending" and (item["attempt_count"] > 0
                              or (item["result"] or {}).get("receipt_kind") == "verification_pending"))
            if needs_readback and item["next_attempt_at_ms"] > now_ms:
                continue
            if not needs_readback and receipt:
                if receipt.get("status") not in {"pending", "failed"} or receipt.get("stop_reason"):
                    continue
                if int(receipt.get("next_attempt_at_ms", 0)) > now_ms:
                    continue
            if not receipt and item["status"] == "handled" and item["result"] and item["result"].get("status") == "applied":
                # Only a saved new-writer semantic result can prove the pre-intent crash window.
                if not item["result"].get("receipt_kind"):
                    continue
            out.append(item)
            if len(out) >= max(1, int(limit)):
                break
    return out


def resume_trade_payload(path: str | Path, *, inbox_id: str, operator: str, repo: Any = None) -> bool:
    if not str(operator).strip():
        raise ValueError("explicit operator is required")
    with with_sqlite_repo_writer_lock(repo), closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        changed = conn.execute(
            """UPDATE trade_inbox SET status = 'pending', attempt_count = 0, next_attempt_at_ms = 0,
               claim_id = NULL, claim_until_ms = NULL,
               last_error = ?, updated_at_ms = ?, result_json = CASE WHEN result_json IS NULL THEN NULL
                   WHEN json_extract(result_json, '$.receipt_kind') = 'verification_pending' THEN result_json
                   ELSE json_set(result_json, '$.receipt_kind', 'pending_retry', '$.retry_policy.retryable', json('true')) END
               WHERE inbox_id = ? AND (status = 'pending' OR (status = 'handled'
                   AND json_extract(result_json, '$.receipt_kind') = 'manual_required'))""",
            (f"resumed_by:{operator}", int(time.time() * 1000), inbox_id),
        ).rowcount
        if changed:
            conn.execute("INSERT INTO trade_inbox_recovery (inbox_id, operator, resumed_at_ms) VALUES (?, ?, ?)",
                         (inbox_id, operator, int(time.time() * 1000)))
        return bool(changed)


def list_retryable_trade_payloads(
    path: str | Path,
    *,
    limit: int = 100,
    retry_delay_sec: float = 60.0,
    max_attempts: int = 20,
    account_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return []
    allowed = None if account_ids is None else {str(value).strip() for value in account_ids if str(value).strip()}
    if allowed == set():
        return []
    row_limit = max(1, int(limit))
    out: list[dict[str, Any]] = []
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
                  AND (claim_id IS NULL OR claim_until_ms <= ?)
                  AND (result_json IS NULL OR json_extract(result_json, '$.receipt_kind') IS NULL
                       OR json_extract(result_json, '$.receipt_kind') = 'pending_retry'
                   OR (last_error = 'execution_association_enrichment'
                       AND json_extract(result_json, '$.receipt_kind') IN ('recorded', 'manual_required')))
                  AND (attempt_count = 0 OR next_attempt_at_ms - 60000 <= ?)
                ORDER BY received_at_ms ASC, inbox_id ASC
                LIMIT ?
                """,
                (int(max_attempts), int(time.time() * 1000), cutoff_ms, row_limit if allowed is None else -1),
            )
            # ponytail: scoped recovery is O(n); add an account index if backlog measurements require it.
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"]) or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if allowed is not None:
                    execution = payload.get("execution_input") or payload
                    execution = execution if isinstance(execution, dict) else {}
                    ref = execution.get("broker_account_ref") or {}
                    ref = ref if isinstance(ref, dict) else {}
                    physical_account = str(ref.get("external_account_id") or extract_primary_account_id(payload) or "").strip()
                    if physical_account not in allowed:
                        continue
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
                if len(out) >= row_limit:
                    break
    return out


def _settle_trade_payload(path: str | Path, *, inbox_id: str, result: dict[str, Any] | None,
                           claim: Mapping[str, Any] | None, retry: bool, error: str | None = None) -> None:
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM trade_inbox WHERE inbox_id = ?", (inbox_id,)).fetchone()
        if (row is None or row["status"] != "pending"
                or row["claim_id"] != (claim or {}).get("claim_id")
                or (claim is not None and row["payload_version"] != claim.get("payload_version"))):
            return
        if result is None:
            result = json.loads(row["result_json"] or "null")
            if result is None:
                result = {"status": "unresolved", "reason": "callback_exception",
                          "diagnostics": {"verification_pending": True, "retryable": False}}
        enriched = _prepare_trade_receipt_result(conn, row, result)
        if enriched["receipt_kind"] == "recorded":
            retry = False
        elif enriched["receipt_kind"] == "verification_pending":
            retry = True
        elif enriched["receipt_kind"] == "manual_required":
            retry = False
        conn.execute("""UPDATE trade_inbox SET status = ?, updated_at_ms = ?, last_error = ?,
                        claim_id = NULL, claim_until_ms = NULL, next_attempt_at_ms = ? WHERE inbox_id = ?""",
                     ("pending" if retry else "handled", int(time.time() * 1000), error,
                      int(time.time() * 1000) + 60_000, inbox_id))


def mark_trade_payload_handled(path: str | Path, *, inbox_id: str,
                                result: dict[str, Any] | None,
                                claim: Mapping[str, Any] | None = None) -> None:
    _settle_trade_payload(path, inbox_id=inbox_id, result=result, claim=claim, retry=False)


def mark_trade_payload_retryable(path: str | Path, *, inbox_id: str, error: str | None,
                                  result: dict[str, Any] | None = None,
                                  claim: Mapping[str, Any] | None = None) -> None:
    _settle_trade_payload(path, inbox_id=inbox_id, result=result, claim=claim, retry=True, error=error)


def settle_trade_payload_result(path: str | Path, *, inbox_id: str, result: dict[str, Any] | None,
                                 claim: Mapping[str, Any] | None = None) -> None:
    diagnostics = (result or {}).get("diagnostics") or {}
    _settle_trade_payload(path, inbox_id=inbox_id, result=result, claim=claim,
                          retry=bool(diagnostics.get("retryable")))


def record_trade_payload_refresh_intent(
    path: str | Path,
    *,
    inbox_id: str,
    intent: Mapping[str, Any],
) -> None:
    with closing(_connect(Path(path))) as conn, conn:
        _ensure_schema(conn)
        _record_trade_payload_refresh_intent(conn, inbox_id=inbox_id, intent=intent)


def _record_trade_payload_refresh_intent(
    conn: sqlite3.Connection, *, inbox_id: str, intent: Mapping[str, Any],
) -> None:
    normalized = _normalize_portfolio_refresh_intent(intent)
    intent_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        """UPDATE trade_inbox
           SET portfolio_refresh_intent_json = COALESCE(portfolio_refresh_intent_json, ?)
           WHERE inbox_id = ?""",
        (intent_json, str(inbox_id)),
    )
    if cursor.rowcount != 1:
        raise ValueError("trade inbox row not found")
    row = conn.execute(
        "SELECT portfolio_refresh_intent_json FROM trade_inbox WHERE inbox_id = ?",
        (str(inbox_id),),
    ).fetchone()
    stored = json.loads(str(row["portfolio_refresh_intent_json"]))
    if _normalize_portfolio_refresh_intent(stored) != normalized:
        raise ValueError("trade inbox portfolio refresh intent conflict")


def list_unclaimed_trade_payload_refresh_intents(
    path: str | Path,
    *,
    account_mapping: Mapping[str, str],
    limit: int = 100,
) -> list[dict[str, str]]:
    """Read completed live stock hints that have never entered the PM call boundary."""
    inbox_path = Path(path)
    allowed = {str(key).strip(): str(value).strip().lower()
               for key, value in account_mapping.items() if str(key).strip() and str(value).strip()}
    if not inbox_path.exists() or not allowed:
        return []
    out: list[dict[str, str]] = []
    with closing(sqlite3.connect(f"{inbox_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT inbox_id, payload_json, portfolio_refresh_intent_json
               FROM trade_inbox WHERE status = 'handled' AND delivery_purpose = 'live'
                 AND source IN ('push', 'backfill')
                 AND portfolio_refresh_intent_json IS NOT NULL
                 AND portfolio_refresh_attempted_at_ms IS NULL
               ORDER BY received_at_ms, inbox_id""",
        )
        for row in rows:
            payload = json.loads(row["payload_json"])
            execution = payload.get("execution_input") or payload
            ref = execution.get("broker_account_ref") or {}
            physical = str(ref.get("external_account_id") or extract_primary_account_id(payload) or "").strip()
            intent = _normalize_portfolio_refresh_intent(json.loads(row["portfolio_refresh_intent_json"]))
            if allowed.get(physical) != intent["account"]:
                continue
            out.append({"inbox_id": str(row["inbox_id"]), **intent})
            if len(out) >= max(1, int(limit)):
                break
    return out


def claim_trade_payload_refresh_intent(
    path: str | Path,
    *,
    inbox_id: str,
) -> dict[str, str] | None:
    inbox_path = Path(path)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE trade_inbox
                SET portfolio_refresh_attempted_at_ms = ?
                WHERE inbox_id = ?
                  AND status != 'conflict'
                  AND portfolio_refresh_intent_json IS NOT NULL
                  AND portfolio_refresh_attempted_at_ms IS NULL
                """,
                (int(time.time() * 1000), str(inbox_id)),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                """
                SELECT portfolio_refresh_intent_json
                FROM trade_inbox
                WHERE inbox_id = ?
                """,
                (str(inbox_id),),
            ).fetchone()
            if row is None:
                raise RuntimeError("trade inbox row disappeared after claim")
            return _normalize_portfolio_refresh_intent(
                json.loads(str(row["portfolio_refresh_intent_json"]))
            )


def _normalize_portfolio_refresh_intent(
    intent: Mapping[str, Any],
) -> dict[str, str]:
    account = str(intent.get("account") or "").strip().lower()
    request_id = str(intent.get("request_id") or "").strip()
    if not account or not request_id:
        raise ValueError("portfolio refresh intent is incomplete")
    return {"account": account, "request_id": request_id}


def trade_inbox_summary(path: str | Path) -> dict[str, Any]:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return {
            "path": str(inbox_path),
            "pending_count": 0,
            "conflict_count": 0,
            "retryable_count": 0,
            "exhausted_count": 0,
            "handled_count": 0,
            "identity_needs_review_count": 0,
            "max_attempt_count": 0,
            "receipt_status_counts": {},
            "receipt_kind_counts": {},
            "receipt_attention": [],
        }
    receipt_status_counts: dict[str, int] = {}
    receipt_kind_counts: dict[str, int] = {}
    receipt_attention = []
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            for item in conn.execute("SELECT inbox_id, receipt_json FROM trade_inbox WHERE receipt_json IS NOT NULL"):
                envelope = _receipt_envelope(item["receipt_json"])
                if not envelope:
                    continue
                receipt = (envelope["receipts"][envelope["current_result_key"]]
                           if envelope.get("schema_version") == 2 else envelope)
                status = str(receipt.get("status") or "unknown")
                kind = str(receipt.get("receipt_kind") or "legacy")
                receipt_status_counts[status] = receipt_status_counts.get(status, 0) + 1
                receipt_kind_counts[kind] = receipt_kind_counts.get(kind, 0) + 1
                if len(receipt_attention) < 20 and (status == "unknown" or receipt.get("stop_reason")):
                    receipt_attention.append({"inbox_id": item["inbox_id"], "receipt_kind": kind,
                        "status": status, "attempt_count": receipt.get("attempt_count", 0),
                        "last_attempt_at_ms": receipt.get("attempted_at_ms"),
                        "reason": receipt.get("stop_reason") or "delivery_requires_verification",
                        "next_action": "verify_delivery_before_explicit_compensation"})
            eligibility = conn.execute(
                """SELECT SUM(status = 'pending' AND attempt_count < 20 AND (
                              result_json IS NULL OR json_extract(result_json, '$.receipt_kind') IS NULL
                              OR json_extract(result_json, '$.receipt_kind') = 'pending_retry'
                              OR (last_error = 'execution_association_enrichment'
                                  AND json_extract(result_json, '$.receipt_kind') IN ('recorded', 'manual_required')))) AS eligible,
                          SUM(attempt_count >= 20 AND (status = 'pending'
                              OR json_extract(result_json, '$.receipt_kind') = 'manual_required')) AS exhausted
                   FROM trade_inbox"""
            ).fetchone()
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
        "receipt_status_counts": receipt_status_counts,
        "receipt_kind_counts": receipt_kind_counts,
        "receipt_attention": receipt_attention,
        "retryable_count": int(eligibility["eligible"] or 0),
        "exhausted_count": int(eligibility["exhausted"] or 0),
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
    conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
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
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
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
            result_reason TEXT,
            portfolio_refresh_intent_json TEXT,
            portfolio_refresh_attempted_at_ms INTEGER
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
    _add_column_if_missing(
        conn,
        "trade_inbox",
        "portfolio_refresh_intent_json",
        "TEXT",
    )
    _add_column_if_missing(
        conn,
        "trade_inbox",
        "portfolio_refresh_attempted_at_ms",
        "INTEGER",
    )
    # Economic retry due time must survive metadata/enrichment updates to updated_at_ms.
    retry_deadline_missing = "next_attempt_at_ms" not in {
        row["name"] for row in conn.execute("PRAGMA table_info(trade_inbox)")}
    for column, sql_type in (
        ("next_attempt_at_ms", "INTEGER NOT NULL DEFAULT 0"),
        ("payload_version", "INTEGER NOT NULL DEFAULT 1"),
        ("claim_id", "TEXT"), ("claim_until_ms", "INTEGER"), ("claim_owner", "TEXT"),
        ("result_json", "TEXT"), ("receipt_json", "TEXT"),
        ("receipt_recovery_allowed", "INTEGER NOT NULL DEFAULT 0"),
        ("delivery_purpose", "TEXT NOT NULL DEFAULT 'live'"),
    ):
        _add_column_if_missing(conn, "trade_inbox", column, sql_type)
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_inbox_evidence (
        inbox_id TEXT NOT NULL, source TEXT NOT NULL, payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL, received_at_ms INTEGER NOT NULL,
        evidence_id TEXT, evidence_json TEXT,
        PRIMARY KEY (inbox_id, source, payload_hash))""")
    _add_column_if_missing(conn, "trade_inbox_evidence", "evidence_id", "TEXT")
    _add_column_if_missing(conn, "trade_inbox_evidence", "evidence_json", "TEXT")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_trade_inbox_evidence_missing_envelope
        ON trade_inbox_evidence(inbox_id)
        WHERE evidence_id IS NULL OR evidence_json IS NULL"""
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_inbox_recovery (
        inbox_id TEXT NOT NULL, operator TEXT NOT NULL, resumed_at_ms INTEGER NOT NULL)""")
    for table in ("trade_inbox", "trade_inbox_evidence", "trade_inbox_recovery"):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            continue
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"{table}_{operation.lower()}_writer_guard"
            guard = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)).fetchone()
            if guard and "trade_inbox_writer_version() != 2" in guard[0]:
                continue
            if guard and "trade_inbox_writer_version() != 1" not in guard[0]:
                raise ValueError("unsupported trade inbox writer guard")
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
                WHEN trade_inbox_writer_version() != 2
                BEGIN SELECT RAISE(ABORT, 'trade inbox requires compatible writer'); END""")
    if retry_deadline_missing:
        conn.execute("UPDATE trade_inbox SET next_attempt_at_ms = updated_at_ms + 60000 WHERE attempt_count > 0")
    _migrate_trade_source_evidence(conn)
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_inbox_evidence_id
        ON trade_inbox_evidence(evidence_id) WHERE evidence_id IS NOT NULL"""
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_inbox_recovery (
        inbox_id TEXT NOT NULL, operator TEXT NOT NULL, resumed_at_ms INTEGER NOT NULL)""")
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


def _inbox_execution_content(source_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    from src.application.trades.normalizer import canonical_trade_execution_content
    parts = str(source_key).split(":", 3)
    source = dict(payload)
    if len(parts) == 4:
        broker, account, physical, _deal = parts
        source.setdefault("broker", broker)
        source.setdefault("internal_account", account)
        source.setdefault("futu_account_id", physical)
    return canonical_trade_execution_content(source)


def _execution_content_hash(content: Mapping[str, Any]) -> str:
    value = {key: content.get(key) for key in ("version", "economic")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _known_execution_associations(source_key: str, payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        name: value for payload in payloads
        for name, value in _inbox_execution_content(source_key, payload).get("associations", {}).items()
        if value is not None
    }


def _canonical_inbox_economic_hash(source_key: str, payload: dict[str, Any]) -> str:
    return _execution_content_hash(_inbox_execution_content(source_key, payload))


def _migrate_trade_source_evidence(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT e.rowid AS evidence_rowid, e.*, i.broker_deal_key
        FROM trade_inbox_evidence e
        LEFT JOIN trade_inbox i ON i.inbox_id = e.inbox_id
        WHERE e.evidence_id IS NULL OR e.evidence_json IS NULL"""
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        evidence = _build_trade_source_evidence(
            inbox_id=str(row["inbox_id"]),
            source=str(row["source"]),
            payload=payload,
            payload_hash=str(row["payload_hash"]),
            received_at_ms=int(row["received_at_ms"]),
            broker_deal_key=str(row["broker_deal_key"] or ""),
            adapter_version=LEGACY_ADAPTER_VERSION,
        )
        conn.execute(
            """UPDATE trade_inbox_evidence SET evidence_id = ?, evidence_json = ?
            WHERE rowid = ?""",
            (
                evidence["evidence_id"],
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                int(row["evidence_rowid"]),
            ),
        )


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
    "LEGACY_ADAPTER_VERSION",
    "TRADE_INTAKE_ADAPTER_VERSIONS",
    "SETTLEMENT_ATTEMPT_MIN_LEASE_MS",
    "TRADE_EVIDENCE_SET_REF_PREFIX",
    "SettlementAttemptClaimOwnershipLost",
    "claim_trade_payload_refresh_intent",
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
    "read_trade_source_evidence",
    "record_trade_payload_refresh_intent",
    "replace_finished_settlement_attempt_provider_invocation",
    "release_settlement_provider_batch_claim",
    "reserve_settlement_attempt_invocation",
    "require_trade_inbox_store_readable",
    "settle_trade_payload_result",
    "settlement_attempt_summary",
    "trade_inbox_revision",
    "trade_inbox_summary",
    "trade_payload_evidence_ref",
    "upsert_settlement_attempt_state",
]


def query_trade_receipts(path: str | Path, *, accounts: list[str], query: dict[str, Any]) -> list[dict[str, Any]]:
    """Read semantic receipt history; never claim, recover, migrate, or send."""
    from src.application.receipt_query import MAX_SOURCE_ROWS, receipt_event, receipt_matches
    if not Path(path).is_file():
        raise FileNotFoundError("trade_inbox_missing")
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        clauses = ["receipt_json IS NOT NULL"]
        args: list[Any] = []
        if query.get("deal_id"):
            clauses.append("deal_id = ?")
            args.append(query["deal_id"])
        if query.get("event_id"):
            event_id = str(query["event_id"]).removeprefix("trade_inbox:")
            clauses.append("(json_extract(receipt_json, '$.receipt_id') = ? OR EXISTS (SELECT 1 FROM json_each(receipt_json, '$.receipts') WHERE json_extract(value, '$.receipt_id') = ?))")
            args.extend((event_id, event_id))
        from datetime import datetime
        if query.get("start_time"):
            clauses.append("updated_at_ms >= ?")
            args.append(int(datetime.fromisoformat(query["start_time"]).timestamp() * 1000))
        if query.get("end_time"):
            clauses.append("received_at_ms <= ?")
            args.append(int(datetime.fromisoformat(query["end_time"]).timestamp() * 1000))
        rows = conn.execute("SELECT inbox_id, length(payload_json) + coalesce(length(receipt_json), 0) AS size FROM trade_inbox WHERE " + " AND ".join(clauses) + " ORDER BY received_at_ms DESC, inbox_id DESC LIMIT ?", [*args, MAX_SOURCE_ROWS + 1]).fetchall()
    if len(rows) > MAX_SOURCE_ROWS:
        raise ValueError("trade_inbox_query_needs_narrowing")
    result = []
    for summary in rows:
        if summary["size"] > 262144:
            raise ValueError("trade_receipt_size_limit")
        row = read_trade_payload(path, inbox_id=summary["inbox_id"], read_only=True)
        if row is None:
            raise ValueError("trade_receipt_changed")
        envelope = row.get("receipt_envelope") or {}
        entries = envelope.get("receipts", {}) if envelope.get("schema_version") == 2 else {"legacy": envelope}
        payload = row.get("payload") or {}
        for result_key, receipt in entries.items():
            business = receipt.get("business_result") or {}
            saved = receipt.get("payload") or {}
            labels = {str(value).strip().lower() for value in (
                payload.get("internal_account"), payload.get("account"), saved.get("internal_account"),
                saved.get("account"), business.get("account"), business.get("internal_account")) if value}
            execution = payload.get("execution") or payload
            label = (execution.get("broker_account_ref") or {}).get("account_label")
            if label:
                labels.add(str(label).strip().lower())
            if len(labels) != 1:
                raise ValueError("trade_receipt_account_unlinkable")
            account = next(iter(labels))
            if account not in accounts:
                continue
            row_event = receipt_event(source="trade_inbox", event_id=receipt.get("receipt_id") or row["inbox_id"] + ":" + result_key,
                account=account, market=saved.get("market") or payload.get("market") or business.get("market") or (execution.get("instrument_ref") or {}).get("market") or ((saved.get("execution_input") or {}).get("instrument_ref") or {}).get("market"), kind="trade",
                occurred=receipt.get("created_at_ms") or row["received_at_ms"], recorded=row["updated_at_ms"],
                revision=receipt.get("payload_version") or row.get("payload_version"), body=receipt.get("message"),
                business_result=business or None, delivery=receipt.get("status"),
                deal_id=str(row.get("deal_id") or saved.get("deal_id") or "") or None,
                symbol=saved.get("symbol") or payload.get("symbol") or business.get("symbol") or (execution.get("instrument_ref") or {}).get("symbol"),
                run_id=business.get("run_id"), diagnostic_code=business.get("reason"),
                related={"superseded_by": receipt.get("superseded_by")})
            if receipt_matches(row_event, query):
                result.append(row_event)
    return result
