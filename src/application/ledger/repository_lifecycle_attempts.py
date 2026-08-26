from __future__ import annotations

from .repository_schema import (
    Any,
    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
    LIFECYCLE_RECEIPT_CODEC,
    LIFECYCLE_RECEIPT_CODEC_VERSION,
    LifecycleAttemptAuditEnvelope,
    canonical_lifecycle_observation_bytes,
    compute_lifecycle_attempt_chain_sha256,
    json,
    lifecycle_invocation_id_bytes,
    lifecycle_receipt_sha256,
    lifecycle_sha256_bytes,
    now_ms,
    settlement_semantic_from_evidence,
    sqlite3,
    validate_lifecycle_attempt_audit_envelope,
    verify_lifecycle_attempt_audit_chain,
    zlib,
)

class LifecycleAttemptRepositoryMixin:
    def get_trade_lifecycle_attempt_audit_head(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT audit_case_key, case_id, last_ordinal, chain_sha256,
                       current_span_ordinal, last_invocation_id, updated_at_ms
                FROM trade_lifecycle_attempt_audit_heads
                WHERE case_id = ?
                """,
                (case_value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_trade_lifecycle_attempt_audit_heads_for_account(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("lifecycle attempt audit account is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT lifecycle_case.account AS account,
                       audit_head.audit_case_key, audit_head.case_id,
                       audit_head.last_ordinal, audit_head.chain_sha256,
                       audit_head.current_span_ordinal,
                       audit_head.last_invocation_id,
                       audit_head.updated_at_ms
                FROM trade_lifecycle_cases AS lifecycle_case
                JOIN trade_lifecycle_attempt_audit_heads AS audit_head
                  ON audit_head.case_id = lifecycle_case.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY lifecycle_case.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trade_lifecycle_attempt_audit_by_invocation(
        self,
        *,
        case_id: str,
        invocation_id: str | bytes,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        invocation_bytes = lifecycle_invocation_id_bytes(invocation_id)
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT lifecycle_case.account AS account,
                       audit.audit_case_key, audit_head.case_id AS case_id,
                       audit.ordinal,
                       audit.invocation_id, audit.attempted_at_ms,
                       audit.outcome_code, audit.semantic_fingerprint,
                       audit.receipt_sha256, audit.diagnostic_sha256,
                       audit.span_ordinal, span.semantic_schema,
                       audit_head.last_ordinal,
                       audit_head.chain_sha256,
                       audit_head.last_invocation_id
                FROM trade_lifecycle_attempt_audits AS audit
                JOIN trade_lifecycle_attempt_audit_heads AS audit_head
                  ON audit_head.audit_case_key = audit.audit_case_key
                LEFT JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = audit_head.case_id
                LEFT JOIN trade_lifecycle_observation_spans AS span
                  ON span.audit_case_key = audit.audit_case_key
                 AND span.span_ordinal = audit.span_ordinal
                WHERE audit_head.case_id = ?
                  AND audit.invocation_id = ?
                """,
                (case_value, invocation_bytes),
            ).fetchone()
        return dict(row) if row is not None else None

    def match_trade_lifecycle_attempt_audit_invocation(
        self,
        attempt_audit: LifecycleAttemptAuditEnvelope,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        validate_lifecycle_attempt_audit_envelope(attempt_audit)
        stored = self.get_trade_lifecycle_attempt_audit_by_invocation(
            case_id=attempt_audit.case_id,
            invocation_id=attempt_audit.invocation_id,
            conn=conn,
        )
        if stored is None:
            return None
        expected = {
            "attempted_at_ms": attempt_audit.attempted_at_ms,
            "outcome_code": attempt_audit.outcome_code,
            "semantic_schema": attempt_audit.semantic_schema,
            "semantic_fingerprint": attempt_audit.semantic_fingerprint,
            "receipt_sha256": attempt_audit.receipt_sha256,
            "diagnostic_sha256": attempt_audit.diagnostic_sha256,
        }
        mismatched = [
            field
            for field, value in expected.items()
            if stored.get(field) != value
        ]
        if mismatched:
            raise ValueError(
                "lifecycle attempt invocation replay mismatch: "
                + ",".join(mismatched)
            )
        stored_invocation = lifecycle_invocation_id_bytes(
            stored.get("last_invocation_id")
        )
        stored_chain = lifecycle_sha256_bytes(
            stored.get("chain_sha256"),
            field="chain_sha256",
        )
        stored_ordinal = stored.get("ordinal")
        stored_last_ordinal = stored.get("last_ordinal")
        if (
            type(stored_ordinal) is not int
            or stored_ordinal < 1
            or type(stored_last_ordinal) is not int
            or stored_last_ordinal != stored_ordinal
            or stored_invocation != attempt_audit.invocation_id
        ):
            raise ValueError(
                "historical lifecycle attempt invocation requires explicit "
                "reconciliation"
            )
        return {
            "audit_ordinal": stored_ordinal,
            "audit_chain_sha256": stored_chain.hex(),
            "audit_idempotent": True,
            "audit_span_ordinal": stored.get("span_ordinal"),
            "_cleanup_receipt_sha256": None,
        }

    def append_trade_lifecycle_attempt_audit_in_transaction(
        self,
        *,
        attempt_audit: LifecycleAttemptAuditEnvelope,
        first_evidence_id: str | None = None,
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        replay = self.match_trade_lifecycle_attempt_audit_invocation(
            attempt_audit,
            conn=conn,
        )
        if replay is not None:
            return replay

        evidence_id = str(first_evidence_id or "").strip()
        observed = attempt_audit.outcome_code in (1, 2)
        if observed and not evidence_id:
            raise ValueError(
                "observed lifecycle attempt requires admitted first evidence"
            )
        if not observed and evidence_id:
            raise ValueError(
                "failed lifecycle attempt cannot carry admitted evidence"
            )

        head = conn.execute(
            """
            SELECT audit_case_key, last_ordinal, chain_sha256,
                   current_span_ordinal, last_invocation_id
            FROM trade_lifecycle_attempt_audit_heads
            WHERE case_id = ?
            """,
            (attempt_audit.case_id,),
        ).fetchone()
        if head is None:
            if conn.execute(
                "SELECT 1 FROM trade_lifecycle_cases WHERE case_id = ?",
                (attempt_audit.case_id,),
            ).fetchone() is None:
                raise ValueError(
                    f"lifecycle case not found: {attempt_audit.case_id}"
                )
            cursor = conn.execute(
                """
                INSERT INTO trade_lifecycle_attempt_audit_heads (
                  case_id, last_ordinal, chain_sha256, current_span_ordinal,
                  last_invocation_id, updated_at_ms
                ) VALUES (?, 0, ?, NULL, NULL, ?)
                """,
                (
                    attempt_audit.case_id,
                    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
                    int(now_ms()),
                ),
            )
            audit_case_key = int(cursor.lastrowid)
            last_ordinal = 0
            previous_chain = LIFECYCLE_ATTEMPT_CHAIN_GENESIS
            current_span_ordinal: int | None = None
        else:
            audit_case_key = head["audit_case_key"]
            last_ordinal = head["last_ordinal"]
            current_span_ordinal = head["current_span_ordinal"]
            if type(audit_case_key) is not int or audit_case_key < 1:
                raise ValueError("lifecycle attempt audit head key is invalid")
            if type(last_ordinal) is not int or last_ordinal < 0:
                raise ValueError("lifecycle attempt audit head ordinal is invalid")
            previous_chain = lifecycle_sha256_bytes(
                head["chain_sha256"],
                field="chain_sha256",
            )
            if current_span_ordinal is not None and (
                type(current_span_ordinal) is not int
                or current_span_ordinal < 1
            ):
                raise ValueError(
                    "lifecycle attempt audit current span is invalid"
                )
            if last_ordinal == 0 and (
                previous_chain != LIFECYCLE_ATTEMPT_CHAIN_GENESIS
                or current_span_ordinal is not None
                or head["last_invocation_id"] is not None
            ):
                raise ValueError("lifecycle attempt audit genesis head is invalid")
            if last_ordinal > 0:
                lifecycle_invocation_id_bytes(head["last_invocation_id"])

        current_span = None
        if current_span_ordinal is not None:
            current_span = conn.execute(
                """
                SELECT semantic_schema, semantic_fingerprint,
                       first_evidence_id, first_evidence_receipt_sha256,
                       last_receipt_sha256, closed_chain_sha256, closed_at_ms
                FROM trade_lifecycle_observation_spans
                WHERE audit_case_key = ? AND span_ordinal = ?
                """,
                (audit_case_key, current_span_ordinal),
            ).fetchone()
            if (
                current_span is None
                or current_span["closed_chain_sha256"] is not None
                or current_span["closed_at_ms"] is not None
            ):
                raise ValueError(
                    "lifecycle attempt audit current span is missing or closed"
                )
        elif conn.execute(
            """
            SELECT 1
            FROM trade_lifecycle_observation_spans
            WHERE audit_case_key = ?
            LIMIT 1
            """,
            (audit_case_key,),
        ).fetchone() is not None:
            raise ValueError("lifecycle attempt audit head lost its current span")

        ordinal = last_ordinal + 1
        chain = compute_lifecycle_attempt_chain_sha256(
            previous_chain_sha256=previous_chain,
            case_id=attempt_audit.case_id,
            ordinal=ordinal,
            invocation_id=attempt_audit.invocation_id,
            attempted_at_ms=attempt_audit.attempted_at_ms,
            outcome_code=attempt_audit.outcome_code,
            semantic_fingerprint=attempt_audit.semantic_fingerprint,
            receipt_sha256=attempt_audit.receipt_sha256,
            diagnostic_sha256=attempt_audit.diagnostic_sha256,
        )
        cleanup_receipt: bytes | None = None
        audit_span_ordinal: int | None = None

        def ensure_receipt_blob() -> None:
            assert attempt_audit.receipt_sha256 is not None
            assert attempt_audit.receipt_codec is not None
            assert attempt_audit.receipt_codec_version is not None
            assert attempt_audit.receipt_uncompressed_bytes is not None
            assert attempt_audit.receipt_compressed_bytes is not None
            assert attempt_audit.receipt_compressed_payload is not None
            assert attempt_audit.canonical_receipt_bytes is not None
            inserted = conn.execute(
                """
                INSERT INTO trade_lifecycle_receipt_blobs (
                  receipt_sha256, codec, codec_version, uncompressed_bytes,
                  compressed_bytes, compressed_payload, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_sha256) DO NOTHING
                """,
                (
                    attempt_audit.receipt_sha256,
                    attempt_audit.receipt_codec,
                    attempt_audit.receipt_codec_version,
                    attempt_audit.receipt_uncompressed_bytes,
                    attempt_audit.receipt_compressed_bytes,
                    attempt_audit.receipt_compressed_payload,
                    int(now_ms()),
                ),
            )
            if inserted.rowcount == 1:
                return
            stored = conn.execute(
                """
                SELECT codec, codec_version, uncompressed_bytes,
                       compressed_bytes, compressed_payload
                FROM trade_lifecycle_receipt_blobs
                WHERE receipt_sha256 = ?
                """,
                (attempt_audit.receipt_sha256,),
            ).fetchone()
            if stored is None:
                raise ValueError("lifecycle receipt blob insert was lost")
            if (
                stored["codec"] != LIFECYCLE_RECEIPT_CODEC
                or stored["codec_version"] != LIFECYCLE_RECEIPT_CODEC_VERSION
                or stored["uncompressed_bytes"]
                != attempt_audit.receipt_uncompressed_bytes
                or stored["compressed_bytes"]
                != attempt_audit.receipt_compressed_bytes
                or stored["compressed_payload"]
                != attempt_audit.receipt_compressed_payload
            ):
                raise ValueError("lifecycle receipt blob immutable conflict")
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(
                stored["compressed_payload"],
                int(stored["uncompressed_bytes"]) + 1,
            )
            if (
                decoded != attempt_audit.canonical_receipt_bytes
                or not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
            ):
                raise ValueError("lifecycle receipt blob content mismatch")

        if not observed:
            if current_span_ordinal is not None:
                cursor = conn.execute(
                    """
                    UPDATE trade_lifecycle_observation_spans
                    SET intervening_failed_attempt_count =
                          intervening_failed_attempt_count + 1
                    WHERE audit_case_key = ? AND span_ordinal = ?
                      AND closed_chain_sha256 IS NULL AND closed_at_ms IS NULL
                    """,
                    (audit_case_key, current_span_ordinal),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "lifecycle attempt failure span update was lost"
                    )
        else:
            assert attempt_audit.semantic_schema is not None
            assert attempt_audit.semantic_fingerprint is not None
            assert attempt_audit.receipt_sha256 is not None
            same_span = current_span is not None and (
                current_span["semantic_schema"] == attempt_audit.semantic_schema
                and current_span["semantic_fingerprint"]
                == attempt_audit.semantic_fingerprint
            )
            if same_span:
                if current_span["first_evidence_id"] != evidence_id:
                    raise ValueError(
                        "lifecycle attempt admitted evidence changed within span"
                    )
                commitment = lifecycle_sha256_bytes(
                    current_span["first_evidence_receipt_sha256"],
                    field="first_evidence_receipt_sha256",
                )
                new_last_receipt = (
                    None
                    if attempt_audit.receipt_sha256 == commitment
                    else attempt_audit.receipt_sha256
                )
                if new_last_receipt is not None:
                    ensure_receipt_blob()
                old_last_receipt = (
                    None
                    if current_span["last_receipt_sha256"] is None
                    else lifecycle_sha256_bytes(
                        current_span["last_receipt_sha256"],
                        field="last_receipt_sha256",
                    )
                )
                cursor = conn.execute(
                    """
                    UPDATE trade_lifecycle_observation_spans
                    SET last_success_ordinal = ?, last_success_at_ms = ?,
                        successful_observation_count =
                          successful_observation_count + 1,
                        last_receipt_sha256 = ?
                    WHERE audit_case_key = ? AND span_ordinal = ?
                      AND closed_chain_sha256 IS NULL AND closed_at_ms IS NULL
                    """,
                    (
                        ordinal,
                        attempt_audit.attempted_at_ms,
                        new_last_receipt,
                        audit_case_key,
                        current_span_ordinal,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "lifecycle attempt observation span update was lost"
                    )
                if (
                    old_last_receipt is not None
                    and old_last_receipt != new_last_receipt
                ):
                    cleanup_receipt = old_last_receipt
                audit_span_ordinal = current_span_ordinal
            else:
                evidence_row = conn.execute(
                    """
                    SELECT case_id, raw_json
                    FROM trade_lifecycle_evidence
                    WHERE evidence_id = ?
                    """,
                    (evidence_id,),
                ).fetchone()
                if (
                    evidence_row is None
                    or evidence_row["case_id"] != attempt_audit.case_id
                ):
                    raise ValueError(
                        "lifecycle attempt first evidence is missing or misbound"
                    )
                try:
                    evidence = json.loads(evidence_row["raw_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "lifecycle attempt first evidence JSON is invalid"
                    ) from exc
                if type(evidence) is not dict:
                    raise ValueError(
                        "lifecycle attempt first evidence must be an object"
                    )
                _semantic, evidence_fingerprint = (
                    settlement_semantic_from_evidence(evidence)
                )
                observation = evidence.get("observation")
                if type(observation) is not dict:
                    raise ValueError(
                        "lifecycle attempt first evidence observation is invalid"
                    )
                evidence_schema = str(
                    observation.get("semantic_schema") or ""
                ).strip()
                if (
                    evidence_schema != attempt_audit.semantic_schema
                    or lifecycle_sha256_bytes(
                        evidence_fingerprint,
                        field="first_evidence_semantic_fingerprint",
                    )
                    != attempt_audit.semantic_fingerprint
                ):
                    raise ValueError(
                        "lifecycle attempt first evidence semantic mismatch"
                    )
                commitment = lifecycle_receipt_sha256(
                    canonical_lifecycle_observation_bytes(observation)
                )
                new_last_receipt = (
                    None
                    if attempt_audit.receipt_sha256 == commitment
                    else attempt_audit.receipt_sha256
                )
                if new_last_receipt is not None:
                    ensure_receipt_blob()
                if current_span_ordinal is not None:
                    cursor = conn.execute(
                        """
                        UPDATE trade_lifecycle_observation_spans
                        SET closed_chain_sha256 = ?, closed_at_ms = ?
                        WHERE audit_case_key = ? AND span_ordinal = ?
                          AND closed_chain_sha256 IS NULL
                          AND closed_at_ms IS NULL
                        """,
                        (
                            previous_chain,
                            attempt_audit.attempted_at_ms,
                            audit_case_key,
                            current_span_ordinal,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(
                            "lifecycle attempt prior span close was lost"
                        )
                    audit_span_ordinal = current_span_ordinal + 1
                else:
                    audit_span_ordinal = 1
                conn.execute(
                    """
                    INSERT INTO trade_lifecycle_observation_spans (
                      audit_case_key, span_ordinal, semantic_schema,
                      semantic_fingerprint, first_evidence_id,
                      first_evidence_receipt_sha256,
                      first_success_ordinal, first_success_at_ms,
                      last_success_ordinal, last_success_at_ms,
                      successful_observation_count,
                      intervening_failed_attempt_count,
                      closed_chain_sha256, last_receipt_sha256, closed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0,
                              NULL, ?, NULL)
                    """,
                    (
                        audit_case_key,
                        audit_span_ordinal,
                        attempt_audit.semantic_schema,
                        attempt_audit.semantic_fingerprint,
                        evidence_id,
                        commitment,
                        ordinal,
                        attempt_audit.attempted_at_ms,
                        ordinal,
                        attempt_audit.attempted_at_ms,
                        new_last_receipt,
                    ),
                )

        conn.execute(
            """
            INSERT INTO trade_lifecycle_attempt_audits (
              audit_case_key, ordinal, invocation_id, attempted_at_ms,
              outcome_code, semantic_fingerprint, receipt_sha256,
              diagnostic_sha256, span_ordinal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_case_key,
                ordinal,
                attempt_audit.invocation_id,
                attempt_audit.attempted_at_ms,
                attempt_audit.outcome_code,
                attempt_audit.semantic_fingerprint,
                attempt_audit.receipt_sha256,
                attempt_audit.diagnostic_sha256,
                audit_span_ordinal,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE trade_lifecycle_attempt_audit_heads
            SET last_ordinal = ?, chain_sha256 = ?,
                current_span_ordinal = ?, last_invocation_id = ?,
                updated_at_ms = ?
            WHERE audit_case_key = ? AND last_ordinal = ?
              AND chain_sha256 = ?
            """,
            (
                ordinal,
                chain,
                current_span_ordinal if not observed else audit_span_ordinal,
                attempt_audit.invocation_id,
                int(now_ms()),
                audit_case_key,
                last_ordinal,
                previous_chain,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("lifecycle attempt audit head CAS failed")
        return {
            "audit_ordinal": ordinal,
            "audit_chain_sha256": chain.hex(),
            "audit_idempotent": False,
            "audit_span_ordinal": audit_span_ordinal,
            "_cleanup_receipt_sha256": cleanup_receipt,
        }

    def delete_unreferenced_trade_lifecycle_receipt_blob(
        self,
        receipt_sha256: str | bytes,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        receipt_hash = lifecycle_sha256_bytes(
            receipt_sha256,
            field="receipt_sha256",
        )
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                """
                DELETE FROM trade_lifecycle_receipt_blobs
                WHERE receipt_sha256 = ?
                  AND NOT EXISTS (
                    SELECT 1
                    FROM trade_lifecycle_observation_spans
                    WHERE last_receipt_sha256 = ?
                  )
                """,
                (receipt_hash, receipt_hash),
            )
        return cursor.rowcount == 1

    def list_trade_lifecycle_attempt_audits(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT audit.audit_case_key, audit.ordinal,
                       audit.invocation_id, audit.attempted_at_ms,
                       audit.outcome_code, audit.semantic_fingerprint,
                       audit.receipt_sha256, audit.diagnostic_sha256,
                       audit.span_ordinal
                FROM trade_lifecycle_attempt_audits AS audit
                JOIN trade_lifecycle_attempt_audit_heads AS audit_head
                  ON audit_head.audit_case_key = audit.audit_case_key
                WHERE audit_head.case_id = ?
                ORDER BY audit.ordinal ASC
                """,
                (case_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def verify_trade_lifecycle_attempt_audit_case(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        with self._optional_conn(conn) as active_conn:
            head = self.get_trade_lifecycle_attempt_audit_head(
                case_id=case_value,
                conn=active_conn,
            )
            audits = self.list_trade_lifecycle_attempt_audits(
                case_id=case_value,
                conn=active_conn,
            )
            audit_case_key = head.get("audit_case_key") if head is not None else None
            spans: list[dict[str, Any]] = []
            receipt_blobs: list[dict[str, Any]] = []
            settlement_evidence: list[dict[str, Any]] = []
            if audit_case_key is not None:
                spans = [
                    dict(row)
                    for row in active_conn.execute(
                        """
                        SELECT span.audit_case_key, span.span_ordinal,
                               span.semantic_schema, span.semantic_fingerprint,
                               span.first_evidence_id,
                               span.first_evidence_receipt_sha256,
                               span.first_success_ordinal,
                               span.first_success_at_ms,
                               span.last_success_ordinal,
                               span.last_success_at_ms,
                               span.successful_observation_count,
                               span.intervening_failed_attempt_count,
                               span.closed_chain_sha256,
                               span.last_receipt_sha256, span.closed_at_ms,
                               evidence.evidence_id AS first_evidence_fk_id,
                               evidence.case_id AS first_evidence_case_id,
                               evidence.created_at_ms AS first_evidence_created_at_ms
                        FROM trade_lifecycle_observation_spans AS span
                        LEFT JOIN trade_lifecycle_evidence AS evidence
                          ON evidence.evidence_id = span.first_evidence_id
                        WHERE span.audit_case_key = ?
                        ORDER BY span.span_ordinal ASC
                        """,
                        (audit_case_key,),
                    ).fetchall()
                ]
                settlement_evidence = [
                    dict(row)
                    for row in active_conn.execute(
                        """
                        WITH first_span AS (
                          SELECT first_evidence_id
                          FROM trade_lifecycle_observation_spans
                          WHERE audit_case_key = ?
                          ORDER BY span_ordinal ASC
                          LIMIT 1
                        ), first_evidence AS (
                          SELECT evidence.created_at_ms, evidence.rowid
                          FROM trade_lifecycle_evidence AS evidence
                          JOIN first_span
                            ON first_span.first_evidence_id = evidence.evidence_id
                        )
                        SELECT evidence.evidence_id, evidence.case_id,
                               evidence.created_at_ms,
                               evidence.raw_json
                        FROM trade_lifecycle_evidence AS evidence
                        CROSS JOIN first_evidence
                        WHERE evidence.case_id = ?
                          AND evidence.source_type = 'broker_settlement_observation'
                          AND (
                            evidence.created_at_ms > first_evidence.created_at_ms
                            OR (
                              evidence.created_at_ms = first_evidence.created_at_ms
                              AND evidence.rowid >= first_evidence.rowid
                            )
                          )
                        ORDER BY evidence.created_at_ms ASC, evidence.rowid ASC
                        """,
                        (audit_case_key, case_value),
                    ).fetchall()
                ]
                receipt_blobs = [
                    dict(row)
                    for row in active_conn.execute(
                        """
                        SELECT blob.receipt_sha256, blob.codec,
                               blob.codec_version, blob.uncompressed_bytes,
                               blob.compressed_bytes, blob.compressed_payload,
                               blob.created_at_ms
                        FROM trade_lifecycle_receipt_blobs AS blob
                        JOIN (
                          SELECT DISTINCT last_receipt_sha256
                          FROM trade_lifecycle_observation_spans
                          WHERE audit_case_key = ?
                            AND last_receipt_sha256 IS NOT NULL
                        ) AS referenced
                          ON referenced.last_receipt_sha256 = blob.receipt_sha256
                        ORDER BY blob.receipt_sha256 ASC
                        """,
                        (audit_case_key,),
                    ).fetchall()
                ]
            admission_head = self.get_trade_lifecycle_settlement_admission_head(
                case_id=case_value,
                conn=active_conn,
            )
            foreign_key_rows: list[dict[str, Any]] = []
            if head is not None and active_conn.execute(
                "SELECT 1 FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_value,),
            ).fetchone() is None:
                foreign_key_rows.append(
                    {
                        "table": "trade_lifecycle_attempt_audit_heads",
                        "fkid": 0,
                    }
                )
            existing_blob_hashes = {
                row["receipt_sha256"] for row in receipt_blobs
            }
            for span in spans:
                if span["first_evidence_fk_id"] is None:
                    foreign_key_rows.append(
                        {
                            "table": "trade_lifecycle_observation_spans",
                            "fkid": 1,
                        }
                    )
                last_receipt_sha256 = span["last_receipt_sha256"]
                if (
                    last_receipt_sha256 is not None
                    and last_receipt_sha256 not in existing_blob_hashes
                ):
                    foreign_key_rows.append(
                        {
                            "table": "trade_lifecycle_observation_spans",
                            "fkid": 0,
                        }
                    )

        return verify_lifecycle_attempt_audit_chain(
            case_id=case_value,
            head=head,
            audit_rows=audits,
            span_rows=spans,
            evidence_rows=settlement_evidence,
            receipt_blob_rows=receipt_blobs,
            admission_head=admission_head,
            foreign_key_rows=foreign_key_rows,
        )
