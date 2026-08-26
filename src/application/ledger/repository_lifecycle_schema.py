from __future__ import annotations

from .repository_common import (
    _add_column_if_missing,
    sqlite3,
)

def _ensure_notification_outbox_v2(conn: sqlite3.Connection) -> None:
    table = "trade_lifecycle_notification_outbox"
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    create_sql = """
        CREATE TABLE {table_name} (
          outbox_id TEXT PRIMARY KEY,
          case_id TEXT NOT NULL,
          transition_type TEXT NOT NULL,
          resolution_revision INTEGER NOT NULL,
          delivery_revision INTEGER NOT NULL DEFAULT 0,
          transition_key TEXT NOT NULL,
          state_fingerprint TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          provider_message_id TEXT,
          claim_id TEXT,
          claimed_at_ms INTEGER,
          send_started_at_ms INTEGER,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at_ms INTEGER,
          last_error TEXT,
          provider_receipt_json TEXT,
          created_at_ms INTEGER NOT NULL,
          updated_at_ms INTEGER NOT NULL,
          confirmed_at_ms INTEGER,
          UNIQUE(transition_key, delivery_revision),
          UNIQUE(case_id, transition_type, resolution_revision, delivery_revision),
          UNIQUE(case_id, transition_type, state_fingerprint, delivery_revision)
        )
    """
    if existing is None:
        conn.execute(create_sql.format(table_name=table))
        return

    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    required = {
        "delivery_revision",
        "transition_key",
        "state_fingerprint",
    }
    unique_indexes: set[tuple[str, ...]] = set()
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not bool(index["unique"]):
            continue
        name = str(index["name"])
        unique_indexes.add(
            tuple(
                str(row["name"])
                for row in conn.execute(f"PRAGMA index_info({name})").fetchall()
            )
        )
    expected_unique = {
        ("transition_key", "delivery_revision"),
        (
            "case_id",
            "transition_type",
            "resolution_revision",
            "delivery_revision",
        ),
        (
            "case_id",
            "transition_type",
            "state_fingerprint",
            "delivery_revision",
        ),
    }
    if required.issubset(columns) and expected_unique.issubset(unique_indexes):
        return

    replacement = f"{table}_v2_rebuild"
    conn.execute(f"DROP TABLE IF EXISTS {replacement}")
    conn.execute(create_sql.format(table_name=replacement))
    conn.execute(
        f"""
        INSERT INTO {replacement} (
          outbox_id, case_id, transition_type, resolution_revision,
          delivery_revision, transition_key, state_fingerprint, status,
          payload_json, payload_hash, provider_message_id, claim_id,
          claimed_at_ms, send_started_at_ms, attempt_count,
          next_attempt_at_ms, last_error, provider_receipt_json,
          created_at_ms, updated_at_ms, confirmed_at_ms
        )
        SELECT
          outbox_id, case_id, transition_type, resolution_revision,
          0, 'legacy:' || outbox_id, payload_hash, status,
          payload_json, payload_hash, provider_message_id, claim_id,
          claimed_at_ms, send_started_at_ms, attempt_count,
          next_attempt_at_ms, last_error, provider_receipt_json,
          created_at_ms, updated_at_ms, confirmed_at_ms
        FROM {table}
        """
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO {table}")

def _ensure_notification_delivery_batches_v1(
    conn: sqlite3.Connection,
) -> None:
    _add_column_if_missing(
        conn,
        "trade_lifecycle_notification_outbox",
        "delivery_batch_id",
        "TEXT",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS
        trade_lifecycle_notification_delivery_batches (
          batch_id TEXT PRIMARY KEY,
          route_fingerprint TEXT NOT NULL,
          provider TEXT NOT NULL,
          channel TEXT NOT NULL,
          target_fingerprint TEXT NOT NULL,
          renderer_version TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          member_count INTEGER NOT NULL CHECK(member_count > 0),
          first_intent_created_at_ms INTEGER NOT NULL,
          last_intent_created_at_ms INTEGER NOT NULL,
          provider_message_id TEXT,
          claim_id TEXT,
          claimed_at_ms INTEGER,
          send_started_at_ms INTEGER,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at_ms INTEGER,
          last_error TEXT,
          provider_receipt_json TEXT,
          created_at_ms INTEGER NOT NULL,
          updated_at_ms INTEGER NOT NULL,
          confirmed_at_ms INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_lifecycle_delivery_batches_dispatch
        ON trade_lifecycle_notification_delivery_batches(
          status, next_attempt_at_ms, created_at_ms, batch_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_lifecycle_delivery_batches_route
        ON trade_lifecycle_notification_delivery_batches(
          route_fingerprint, send_started_at_ms, created_at_ms, batch_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_lifecycle_outbox_delivery_batch
        ON trade_lifecycle_notification_outbox(
          delivery_batch_id, created_at_ms, outbox_id
        )
        """
    )

def _ensure_lifecycle_delivery_status_revision_v1(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_status_revisions (
          scope TEXT PRIMARY KEY,
          revision INTEGER NOT NULL CHECK(revision >= 0)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO trade_lifecycle_status_revisions (scope, revision)
        VALUES ('delivery', 0)
        ON CONFLICT(scope) DO NOTHING
        """
    )
    status_tables = {
        "cases": "trade_lifecycle_cases",
        "evidence": "trade_lifecycle_evidence",
        "timing": "trade_lifecycle_timing_policies",
        "outbox": "trade_lifecycle_notification_outbox",
        "batches": "trade_lifecycle_notification_delivery_batches",
        "receipts": "trade_lifecycle_migration_receipts",
    }
    for alias, table in status_tables.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS
                trg_lifecycle_delivery_status_{alias}_{operation.lower()}
                AFTER {operation} ON {table}
                BEGIN
                  INSERT INTO trade_lifecycle_status_revisions (
                    scope, revision
                  ) VALUES ('delivery', 1)
                  ON CONFLICT(scope) DO UPDATE SET
                    revision = revision + 1;
                END
                """
            )

def _ensure_lifecycle_attempt_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_attempt_audit_heads (
          audit_case_key INTEGER PRIMARY KEY CHECK(typeof(audit_case_key) = 'integer'),
          case_id TEXT NOT NULL UNIQUE,
          last_ordinal INTEGER NOT NULL
            CHECK(typeof(last_ordinal) = 'integer' AND last_ordinal >= 0),
          chain_sha256 BLOB NOT NULL
            CHECK(typeof(chain_sha256) = 'blob' AND length(chain_sha256) = 32),
          current_span_ordinal INTEGER
            CHECK(
              current_span_ordinal IS NULL OR (
                typeof(current_span_ordinal) = 'integer'
                AND current_span_ordinal >= 1
              )
            ),
          last_invocation_id BLOB
            CHECK(
              last_invocation_id IS NULL OR (
                typeof(last_invocation_id) = 'blob'
                AND length(last_invocation_id) = 16
              )
            ),
          updated_at_ms INTEGER NOT NULL
            CHECK(typeof(updated_at_ms) = 'integer' AND updated_at_ms >= 1),
          FOREIGN KEY(case_id) REFERENCES trade_lifecycle_cases(case_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_attempt_audits (
          audit_case_key INTEGER NOT NULL CHECK(typeof(audit_case_key) = 'integer'),
          ordinal INTEGER NOT NULL
            CHECK(typeof(ordinal) = 'integer' AND ordinal >= 1),
          invocation_id BLOB NOT NULL
            CHECK(typeof(invocation_id) = 'blob' AND length(invocation_id) = 16),
          attempted_at_ms INTEGER NOT NULL
            CHECK(typeof(attempted_at_ms) = 'integer' AND attempted_at_ms >= 1),
          outcome_code INTEGER NOT NULL
            CHECK(typeof(outcome_code) = 'integer' AND outcome_code BETWEEN 1 AND 8),
          semantic_fingerprint BLOB
            CHECK(
              semantic_fingerprint IS NULL OR (
                typeof(semantic_fingerprint) = 'blob'
                AND length(semantic_fingerprint) = 32
              )
            ),
          receipt_sha256 BLOB
            CHECK(
              receipt_sha256 IS NULL OR (
                typeof(receipt_sha256) = 'blob'
                AND length(receipt_sha256) = 32
              )
            ),
          diagnostic_sha256 BLOB
            CHECK(
              diagnostic_sha256 IS NULL OR (
                typeof(diagnostic_sha256) = 'blob'
                AND length(diagnostic_sha256) = 32
              )
            ),
          span_ordinal INTEGER
            CHECK(
              span_ordinal IS NULL OR (
                typeof(span_ordinal) = 'integer' AND span_ordinal >= 1
              )
            ),
          PRIMARY KEY(audit_case_key, ordinal),
          UNIQUE(audit_case_key, invocation_id),
          FOREIGN KEY(audit_case_key)
            REFERENCES trade_lifecycle_attempt_audit_heads(audit_case_key),
          CHECK(
            (
              outcome_code IN (1, 2)
              AND semantic_fingerprint IS NOT NULL
              AND receipt_sha256 IS NOT NULL
              AND diagnostic_sha256 IS NULL
              AND span_ordinal IS NOT NULL
            ) OR (
              outcome_code IN (3, 4, 5, 6, 7, 8)
              AND semantic_fingerprint IS NULL
              AND receipt_sha256 IS NULL
              AND diagnostic_sha256 IS NOT NULL
              AND span_ordinal IS NULL
            )
          )
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_receipt_blobs (
          receipt_sha256 BLOB PRIMARY KEY
            CHECK(typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32),
          codec TEXT NOT NULL CHECK(codec = 'zlib'),
          codec_version INTEGER NOT NULL
            CHECK(typeof(codec_version) = 'integer' AND codec_version = 1),
          uncompressed_bytes INTEGER NOT NULL
            CHECK(typeof(uncompressed_bytes) = 'integer' AND uncompressed_bytes >= 0),
          compressed_bytes INTEGER NOT NULL
            CHECK(typeof(compressed_bytes) = 'integer' AND compressed_bytes >= 1),
          compressed_payload BLOB NOT NULL CHECK(typeof(compressed_payload) = 'blob'),
          created_at_ms INTEGER NOT NULL
            CHECK(typeof(created_at_ms) = 'integer' AND created_at_ms >= 1),
          CHECK(compressed_bytes = length(compressed_payload))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_observation_spans (
          audit_case_key INTEGER NOT NULL CHECK(typeof(audit_case_key) = 'integer'),
          span_ordinal INTEGER NOT NULL
            CHECK(typeof(span_ordinal) = 'integer' AND span_ordinal >= 1),
          semantic_schema TEXT NOT NULL CHECK(semantic_schema != ''),
          semantic_fingerprint BLOB NOT NULL
            CHECK(
              typeof(semantic_fingerprint) = 'blob'
              AND length(semantic_fingerprint) = 32
            ),
          first_evidence_id TEXT NOT NULL,
          first_evidence_receipt_sha256 BLOB NOT NULL
            CHECK(
              typeof(first_evidence_receipt_sha256) = 'blob'
              AND length(first_evidence_receipt_sha256) = 32
            ),
          first_success_ordinal INTEGER NOT NULL
            CHECK(typeof(first_success_ordinal) = 'integer' AND first_success_ordinal >= 1),
          first_success_at_ms INTEGER NOT NULL
            CHECK(typeof(first_success_at_ms) = 'integer' AND first_success_at_ms >= 1),
          last_success_ordinal INTEGER NOT NULL
            CHECK(
              typeof(last_success_ordinal) = 'integer'
              AND last_success_ordinal >= first_success_ordinal
            ),
          last_success_at_ms INTEGER NOT NULL
            CHECK(
              typeof(last_success_at_ms) = 'integer'
              AND last_success_at_ms >= first_success_at_ms
            ),
          successful_observation_count INTEGER NOT NULL
            CHECK(
              typeof(successful_observation_count) = 'integer'
              AND successful_observation_count >= 1
            ),
          intervening_failed_attempt_count INTEGER NOT NULL
            CHECK(
              typeof(intervening_failed_attempt_count) = 'integer'
              AND intervening_failed_attempt_count >= 0
            ),
          closed_chain_sha256 BLOB
            CHECK(
              closed_chain_sha256 IS NULL OR (
                typeof(closed_chain_sha256) = 'blob'
                AND length(closed_chain_sha256) = 32
              )
            ),
          last_receipt_sha256 BLOB
            CHECK(
              last_receipt_sha256 IS NULL OR (
                typeof(last_receipt_sha256) = 'blob'
                AND length(last_receipt_sha256) = 32
              )
            ),
          closed_at_ms INTEGER
            CHECK(
              closed_at_ms IS NULL OR (
                typeof(closed_at_ms) = 'integer'
                AND closed_at_ms >= last_success_at_ms
              )
            ),
          PRIMARY KEY(audit_case_key, span_ordinal),
          FOREIGN KEY(audit_case_key)
            REFERENCES trade_lifecycle_attempt_audit_heads(audit_case_key),
          FOREIGN KEY(first_evidence_id)
            REFERENCES trade_lifecycle_evidence(evidence_id),
          FOREIGN KEY(last_receipt_sha256)
            REFERENCES trade_lifecycle_receipt_blobs(receipt_sha256),
          CHECK(
            (closed_chain_sha256 IS NULL AND closed_at_ms IS NULL)
            OR (closed_chain_sha256 IS NOT NULL AND closed_at_ms IS NOT NULL)
          )
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_observation_spans_last_receipt
        ON trade_lifecycle_observation_spans(last_receipt_sha256)
        """
    )
    for operation in ("INSERT", "UPDATE OF audit_case_key, first_evidence_id"):
        suffix = "insert" if operation == "INSERT" else "update"
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS
            trg_trade_lifecycle_observation_spans_evidence_case_{suffix}
            BEFORE {operation} ON trade_lifecycle_observation_spans
            WHEN NOT EXISTS (
              SELECT 1
              FROM trade_lifecycle_attempt_audit_heads AS audit_head
              JOIN trade_lifecycle_evidence AS evidence
                ON evidence.evidence_id = NEW.first_evidence_id
              WHERE audit_head.audit_case_key = NEW.audit_case_key
                AND evidence.case_id = audit_head.case_id
            )
            BEGIN
              SELECT RAISE(ABORT, 'lifecycle observation span evidence case mismatch');
            END
            """
        )
