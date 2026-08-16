from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

import src.application.ledger.lifecycle_attempt_audit as audit_codec
import src.application.ledger.lifecycle_settlement_semantics as settlement_semantics
import src.application.ledger.repository as repository_module
from src.application.ledger.api import (
    record_lifecycle_attempt_audit_atomically,
)
from src.application.ledger.lifecycle_attempt_audit import (
    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
    LIFECYCLE_ATTEMPT_CHAIN_SCHEMA,
    LIFECYCLE_RECEIPT_SCHEMA,
    build_lifecycle_attempt_audit_envelope,
    build_lifecycle_attempt_run_seal,
    compute_lifecycle_attempt_chain_sha256,
    validate_lifecycle_attempt_audit_envelope,
    validate_lifecycle_attempt_run_seal,
    verify_lifecycle_attempt_run_seal,
)
from src.application.ledger.lifecycle_settlement_semantics import (
    attach_settlement_semantics,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
    _ensure_lifecycle_attempt_audit_schema,
)


INVOCATION_ID = "123e4567-e89b-42d3-a456-426614174000"
INVOCATION_ID_2 = "123e4567-e89b-42d3-a456-426614174001"
SEMANTIC_FINGERPRINT = "11" * 32
SIDECAR_TABLES = {
    "trade_lifecycle_attempt_audit_heads",
    "trade_lifecycle_attempt_audits",
    "trade_lifecycle_observation_spans",
    "trade_lifecycle_receipt_blobs",
}


def _observation(
    *,
    case_id: str = "case-a",
    receipt_note: str | None = None,
    option_position_absent: bool = True,
) -> dict[str, object]:
    observation: dict[str, object] = {
        "schema_version": "broker_settlement_observation.v2",
        "case_id": case_id,
        "account": "lx",
        "futu_account_id": "1001",
        "market": "US",
        "contract_identity": {
            "symbol": "NVDA",
            "option_contract_code": "US.NVDA260821P100000",
            "option_type": "put",
            "position_side": "short",
            "strike": "100.00",
            "expiration_ymd": "2026-08-21",
            "multiplier": 100,
        },
        "target_contracts_by_lot": {"lot-a": 1},
        "frozen_preterminal_remaining_by_lot": {"lot-a": 0},
        "anchor_option_deal_key": "futu:lx:1001:deal-a",
        "anchor_execution_time_ms": 1_000,
        "observed_at_ms": 2_000,
        "settlement_deadline_ms": 1_500,
        "required_sources": ["anchor_option_close"],
        "source_receipts": {
            "anchor_option_close": {
                "status": "complete",
                "coverage_complete": True,
                "pagination_complete": True,
                "rows": [],
            }
        },
        "stock_settlement_candidates": [],
        "broker_option_position_absent": option_position_absent,
        "projection_matches_frozen_remaining": True,
        "reservation_exclusive": True,
        "competing_effective_consumption": False,
        "stock_settlement_present": False,
        "normal_order_present": False,
        "complete": True,
        "incomplete_reason_codes": [],
    }
    if receipt_note is not None:
        observation["receipt_note"] = receipt_note
    return attach_settlement_semantics(observation, evidence_kind="expire_close")


def _case(case_id: str, *, account: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "case_key": case_id,
        "account": account,
        "symbol": "NVDA",
        "status": "waiting_settlement_evidence",
    }


def _repo(tmp_path: Path) -> SQLiteOptionPositionsRepository:
    return SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")


def test_run_seal_sorts_coalesces_and_validates_heads() -> None:
    seal = build_lifecycle_attempt_run_seal(
        account="lx",
        source_id="source-a",
        completed_at_ms=2_000,
        seal_scope="touched_heads",
        reason="ordinary_due",
        heads=[
            {"account": "lx", "case_id": "case-b", "last_ordinal": 1, "chain_sha256": b"\x22" * 32},
            {"account": "lx", "case_id": "case-a", "last_ordinal": 1, "chain_sha256": b"\x11" * 32},
            {"account": "lx", "case_id": "case-b", "last_ordinal": 2, "chain_sha256": b"\x33" * 32},
        ],
    )

    assert [head["case_id"] for head in seal["heads"]] == ["case-a", "case-b"]
    assert seal["heads"][1]["last_ordinal"] == 2
    assert seal["head_count"] == 2
    assert validate_lifecycle_attempt_run_seal(seal) == seal

    tampered = dict(seal, seal_sha256="00" * 32)
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_lifecycle_attempt_run_seal(tampered)


def test_run_seal_allows_empty_checkpoint_and_rejects_cross_account_head() -> None:
    checkpoint = build_lifecycle_attempt_run_seal(
        account="lx",
        source_id="source-a",
        completed_at_ms=2_000,
        seal_scope="all_heads_checkpoint",
        reason="process_startup",
        heads=[],
    )
    assert checkpoint["head_count"] == 0
    assert validate_lifecycle_attempt_run_seal(checkpoint) == checkpoint

    with pytest.raises(ValueError, match="another account"):
        build_lifecycle_attempt_run_seal(
            account="lx",
            source_id="source-a",
            completed_at_ms=2_000,
            seal_scope="touched_heads",
            reason="ordinary_due",
            heads=[
                {"account": "sy", "case_id": "case-a", "last_ordinal": 1, "chain_sha256": b"\x11" * 32}
            ],
        )


def test_run_seal_verifier_distinguishes_touched_and_account_checkpoint_scope() -> None:
    current_heads = [
        {"account": "lx", "case_id": "case-a", "last_ordinal": 1, "chain_sha256": b"\x11" * 32},
        {"account": "lx", "case_id": "case-b", "last_ordinal": 1, "chain_sha256": b"\x22" * 32},
    ]
    touched = build_lifecycle_attempt_run_seal(
        account="lx",
        source_id="source-a",
        completed_at_ms=2_000,
        seal_scope="touched_heads",
        reason="ordinary_due",
        heads=current_heads[:1],
    )
    checkpoint = build_lifecycle_attempt_run_seal(
        account="lx",
        source_id="source-a",
        completed_at_ms=2_000,
        seal_scope="all_heads_checkpoint",
        reason="process_startup",
        heads=current_heads[:1],
    )

    assert verify_lifecycle_attempt_run_seal(touched, current_heads=current_heads)["status"] == "valid"
    invalid = verify_lifecycle_attempt_run_seal(checkpoint, current_heads=current_heads)
    assert invalid["status"] == "invalid"
    assert invalid["mismatch_samples"] == [
        {"code": "checkpoint_head_unsealed", "case_id": "case-b"}
    ]


def _checkpointed_storage_bytes(repo: SQLiteOptionPositionsRepository) -> int:
    with repo._connect() as conn:  # noqa: SLF001 - focused physical-byte contract
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return sum(
        path.stat().st_size
        for path in (
            repo.db_path,
            Path(f"{repo.db_path}-wal"),
            Path(f"{repo.db_path}-shm"),
        )
        if path.exists()
    )


def _invocation(offset: int) -> str:
    return str(uuid.UUID(int=offset, version=4))


def _append_attempt(
    repo: SQLiteOptionPositionsRepository,
    envelope: audit_codec.LifecycleAttemptAuditEnvelope,
    *,
    evidence_id: str | None = None,
    evidence_observation: dict[str, object] | None = None,
) -> dict[str, object]:
    conn = repo._connect()  # noqa: SLF001 - focused transaction contract
    try:
        conn.execute("BEGIN IMMEDIATE")
        if evidence_observation is not None:
            assert evidence_id is not None
            repo.insert_trade_lifecycle_evidence_once(
                {
                    "evidence_id": evidence_id,
                    "case_id": envelope.case_id,
                    "source_type": "broker_settlement_observation",
                    "evidence_type": "expire_close",
                    "account": "lx",
                    "symbol": "NVDA",
                    "semantic_schema": evidence_observation["semantic_schema"],
                    "semantic_fingerprint": evidence_observation[
                        "semantic_fingerprint"
                    ],
                    "semantic_projection": evidence_observation[
                        "semantic_projection"
                    ],
                    "observation": evidence_observation,
                },
                conn=conn,
            )
            created_at_ms = int(
                conn.execute(
                    "SELECT created_at_ms FROM trade_lifecycle_evidence "
                    "WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()[0]
            )
            repo.upsert_trade_lifecycle_settlement_admission_head(
                case_id=envelope.case_id,
                semantic_schema=str(evidence_observation["semantic_schema"]),
                semantic_fingerprint=str(
                    evidence_observation["semantic_fingerprint"]
                ),
                evidence_id=evidence_id,
                evidence_created_at_ms=created_at_ms,
                updated_at_ms=envelope.attempted_at_ms,
                conn=conn,
            )
        result = repo.append_trade_lifecycle_attempt_audit_in_transaction(
            attempt_audit=envelope,
            first_evidence_id=evidence_id,
            conn=conn,
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_genesis_head(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    updated_at_ms: int = 1_000,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO trade_lifecycle_attempt_audit_heads (
          case_id, last_ordinal, chain_sha256, current_span_ordinal,
          last_invocation_id, updated_at_ms
        ) VALUES (?, 0, ?, NULL, NULL, ?)
        """,
        (case_id, LIFECYCLE_ATTEMPT_CHAIN_GENESIS, updated_at_ms),
    )
    return int(cursor.lastrowid)


def _seed_one_observed_attempt(
    repo: SQLiteOptionPositionsRepository,
    *,
    case_id: str = "case-a",
    first_evidence_observation: dict[str, object] | None = None,
    audit_observation: dict[str, object] | None = None,
) -> tuple[bytes, bytes]:
    observation = audit_observation or _observation(case_id=case_id)
    evidence_observation = first_evidence_observation or observation
    assert evidence_observation["semantic_schema"] == observation["semantic_schema"]
    assert evidence_observation["semantic_fingerprint"] == observation["semantic_fingerprint"]
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id=INVOCATION_ID,
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=observation,
    )
    assert envelope.semantic_fingerprint is not None
    assert envelope.receipt_sha256 is not None
    chain = compute_lifecycle_attempt_chain_sha256(
        previous_chain_sha256=LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
        case_id=case_id,
        ordinal=1,
        invocation_id=envelope.invocation_id,
        attempted_at_ms=envelope.attempted_at_ms,
        outcome_code=envelope.outcome_code,
        semantic_fingerprint=envelope.semantic_fingerprint,
        receipt_sha256=envelope.receipt_sha256,
        diagnostic_sha256=None,
    )
    repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "evidence-a",
            "case_id": case_id,
            "source_type": "broker_settlement_observation",
            "evidence_type": "expire_close",
            "account": "lx",
            "symbol": "NVDA",
            "semantic_schema": evidence_observation["semantic_schema"],
            "semantic_fingerprint": evidence_observation["semantic_fingerprint"],
            "semantic_projection": evidence_observation["semantic_projection"],
            "observation": evidence_observation,
        }
    )
    with repo._connect() as conn:  # noqa: SLF001 - focused fixture
        evidence_created_at_ms = int(
            conn.execute(
                "SELECT created_at_ms FROM trade_lifecycle_evidence WHERE evidence_id = 'evidence-a'"
            ).fetchone()[0]
        )
    repo.upsert_trade_lifecycle_settlement_admission_head(
        case_id=case_id,
        semantic_schema=str(evidence_observation["semantic_schema"]),
        semantic_fingerprint=str(evidence_observation["semantic_fingerprint"]),
        evidence_id="evidence-a",
        evidence_created_at_ms=evidence_created_at_ms,
        updated_at_ms=2_000,
    )
    with repo._connect() as conn:  # noqa: SLF001 - focused repository contract
        first_receipt_sha256 = audit_codec.lifecycle_receipt_sha256(
            audit_codec.canonical_lifecycle_observation_bytes(
                evidence_observation
            )
        )
        last_receipt_sha256 = (
            envelope.receipt_sha256
            if first_receipt_sha256 != envelope.receipt_sha256
            else None
        )
        if last_receipt_sha256 is not None:
            conn.execute(
                """
                INSERT INTO trade_lifecycle_receipt_blobs (
                  receipt_sha256, codec, codec_version, uncompressed_bytes,
                  compressed_bytes, compressed_payload, created_at_ms
                ) VALUES (?, 'zlib', 1, ?, ?, ?, 2000)
                """,
                (
                    envelope.receipt_sha256,
                    envelope.receipt_uncompressed_bytes,
                    envelope.receipt_compressed_bytes,
                    envelope.receipt_compressed_payload,
                ),
            )
        cursor = conn.execute(
            """
            INSERT INTO trade_lifecycle_attempt_audit_heads (
              case_id, last_ordinal, chain_sha256, current_span_ordinal,
              last_invocation_id, updated_at_ms
            ) VALUES (?, 1, ?, 1, ?, 2000)
            """,
            (case_id, chain, envelope.invocation_id),
        )
        audit_case_key = int(cursor.lastrowid)
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
            ) VALUES (?, 1, ?, ?, 'evidence-a', ?, 1, 2000, 1, 2000, 1, 0,
                      NULL, ?, NULL)
            """,
            (
                audit_case_key,
                envelope.semantic_schema,
                envelope.semantic_fingerprint,
                first_receipt_sha256,
                last_receipt_sha256,
            ),
        )
        conn.execute(
            """
            INSERT INTO trade_lifecycle_attempt_audits (
              audit_case_key, ordinal, invocation_id, attempted_at_ms,
              outcome_code, semantic_fingerprint, receipt_sha256,
              diagnostic_sha256, span_ordinal
            ) VALUES (?, 1, ?, 2000, 1, ?, ?, NULL, 1)
            """,
            (
                audit_case_key,
                envelope.invocation_id,
                envelope.semantic_fingerprint,
                envelope.receipt_sha256,
            ),
        )
    return chain, envelope.invocation_id


def _seed_second_observed_attempt(
    repo: SQLiteOptionPositionsRepository,
    *,
    semantic_change: bool,
    case_id: str = "case-a",
) -> bytes:
    observation = _observation(
        case_id=case_id,
        receipt_note="second",
        option_position_absent=not semantic_change,
    )
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id=INVOCATION_ID_2,
        attempted_at_ms=2_100,
        outcome_kind="observed_complete",
        observation=observation,
    )
    assert envelope.semantic_fingerprint is not None
    assert envelope.receipt_sha256 is not None
    assert envelope.receipt_compressed_payload is not None
    head = repo.get_trade_lifecycle_attempt_audit_head(case_id=case_id)
    assert head is not None
    chain = compute_lifecycle_attempt_chain_sha256(
        previous_chain_sha256=head["chain_sha256"],
        case_id=case_id,
        ordinal=2,
        invocation_id=envelope.invocation_id,
        attempted_at_ms=envelope.attempted_at_ms,
        outcome_code=envelope.outcome_code,
        semantic_fingerprint=envelope.semantic_fingerprint,
        receipt_sha256=envelope.receipt_sha256,
        diagnostic_sha256=None,
    )

    if semantic_change:
        repo.insert_trade_lifecycle_evidence_once(
            {
                "evidence_id": "evidence-b",
                "case_id": case_id,
                "source_type": "broker_settlement_observation",
                "evidence_type": "expire_close",
                "account": "lx",
                "symbol": "NVDA",
                "semantic_schema": observation["semantic_schema"],
                "semantic_fingerprint": observation["semantic_fingerprint"],
                "semantic_projection": observation["semantic_projection"],
                "observation": observation,
            }
        )
        with repo._connect() as conn:  # noqa: SLF001 - focused fixture
            evidence_created_at_ms = int(
                conn.execute(
                    "SELECT created_at_ms FROM trade_lifecycle_evidence WHERE evidence_id = 'evidence-b'"
                ).fetchone()[0]
            )
        repo.upsert_trade_lifecycle_settlement_admission_head(
            case_id=case_id,
            semantic_schema=str(observation["semantic_schema"]),
            semantic_fingerprint=str(observation["semantic_fingerprint"]),
            evidence_id="evidence-b",
            evidence_created_at_ms=evidence_created_at_ms,
            updated_at_ms=2_100,
        )

    with repo._connect() as conn:  # noqa: SLF001 - focused fixture
        audit_case_key = int(head["audit_case_key"])
        if semantic_change:
            conn.execute(
                """
                UPDATE trade_lifecycle_observation_spans
                SET closed_chain_sha256 = ?, closed_at_ms = 2100
                WHERE audit_case_key = ? AND span_ordinal = 1
                """,
                (head["chain_sha256"], audit_case_key),
            )
            conn.execute(
                """
                INSERT INTO trade_lifecycle_observation_spans (
                  audit_case_key, span_ordinal, semantic_schema,
                  semantic_fingerprint, first_evidence_id,
                  first_evidence_receipt_sha256,
                  first_success_ordinal, first_success_at_ms,
                  last_success_ordinal, last_success_at_ms,
                  successful_observation_count,
                  intervening_failed_attempt_count
                ) VALUES (?, 2, ?, ?, 'evidence-b', ?, 2, 2100, 2, 2100, 1, 0)
                """,
                (
                    audit_case_key,
                    envelope.semantic_schema,
                    envelope.semantic_fingerprint,
                    envelope.receipt_sha256,
                ),
            )
            audit_span_ordinal = 2
        else:
            conn.execute(
                """
                INSERT INTO trade_lifecycle_receipt_blobs (
                  receipt_sha256, codec, codec_version, uncompressed_bytes,
                  compressed_bytes, compressed_payload, created_at_ms
                ) VALUES (?, 'zlib', 1, ?, ?, ?, 2100)
                """,
                (
                    envelope.receipt_sha256,
                    envelope.receipt_uncompressed_bytes,
                    envelope.receipt_compressed_bytes,
                    envelope.receipt_compressed_payload,
                ),
            )
            conn.execute(
                """
                UPDATE trade_lifecycle_observation_spans
                SET last_success_ordinal = 2, last_success_at_ms = 2100,
                    successful_observation_count = 2,
                    last_receipt_sha256 = ?
                WHERE audit_case_key = ? AND span_ordinal = 1
                """,
                (envelope.receipt_sha256, audit_case_key),
            )
            audit_span_ordinal = 1
        conn.execute(
            """
            INSERT INTO trade_lifecycle_attempt_audits (
              audit_case_key, ordinal, invocation_id, attempted_at_ms,
              outcome_code, semantic_fingerprint, receipt_sha256,
              diagnostic_sha256, span_ordinal
            ) VALUES (?, 2, ?, 2100, 1, ?, ?, NULL, ?)
            """,
            (
                audit_case_key,
                envelope.invocation_id,
                envelope.semantic_fingerprint,
                envelope.receipt_sha256,
                audit_span_ordinal,
            ),
        )
        conn.execute(
            """
            UPDATE trade_lifecycle_attempt_audit_heads
            SET last_ordinal = 2, chain_sha256 = ?, current_span_ordinal = ?,
                last_invocation_id = ?, updated_at_ms = 2100
            WHERE audit_case_key = ?
            """,
            (chain, audit_span_ordinal, envelope.invocation_id, audit_case_key),
        )
    return chain


def _normalized_triggers(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["name"]): " ".join(str(row["sql"]).split())
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    }


def test_codec_builds_receipt_once_and_chain_has_a_frozen_unambiguous_preimage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _observation()
    dump_calls = 0
    compress_calls = 0
    real_dumps = audit_codec.json.dumps
    real_compress = audit_codec.zlib.compress

    def counted_dumps(*args: object, **kwargs: object) -> str:
        nonlocal dump_calls
        dump_calls += 1
        return real_dumps(*args, **kwargs)

    def counted_compress(payload: bytes) -> bytes:
        nonlocal compress_calls
        compress_calls += 1
        return real_compress(payload)

    monkeypatch.setattr(audit_codec.json, "dumps", counted_dumps)
    monkeypatch.setattr(audit_codec.zlib, "compress", counted_compress)
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=INVOCATION_ID,
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=observation,
    )

    expected_receipt = real_dumps(
        observation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert dump_calls == 1
    assert compress_calls == 1
    assert envelope.canonical_receipt_bytes == expected_receipt
    assert envelope.receipt_sha256 == hashlib.sha256(
        LIFECYCLE_RECEIPT_SCHEMA.encode("utf-8") + b"\0" + expected_receipt
    ).digest()
    assert zlib.decompress(envelope.receipt_compressed_payload or b"") == expected_receipt
    assert envelope.receipt_uncompressed_bytes == len(expected_receipt)
    assert envelope.receipt_compressed_bytes == len(envelope.receipt_compressed_payload or b"")

    assert envelope.semantic_fingerprint is not None
    assert envelope.receipt_sha256 is not None
    computed = compute_lifecycle_attempt_chain_sha256(
        previous_chain_sha256=LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
        case_id="case-a",
        ordinal=1,
        invocation_id=INVOCATION_ID,
        attempted_at_ms=2_000,
        outcome_code=1,
        semantic_fingerprint=envelope.semantic_fingerprint,
        receipt_sha256=envelope.receipt_sha256,
        diagnostic_sha256=None,
    )
    case_bytes = b"case-a"
    manual_preimage = b"".join(
        (
            LIFECYCLE_ATTEMPT_CHAIN_SCHEMA.encode("utf-8") + b"\0",
            LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
            len(case_bytes).to_bytes(4, "big"),
            case_bytes,
            (1).to_bytes(8, "big"),
            uuid.UUID(INVOCATION_ID).bytes,
            (2_000).to_bytes(8, "big"),
            (1).to_bytes(2, "big"),
            envelope.semantic_fingerprint,
            envelope.receipt_sha256,
            LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
        )
    )
    assert computed == hashlib.sha256(manual_preimage).digest()

    failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=INVOCATION_ID,
        attempted_at_ms=2_001,
        outcome_kind="retryable_error",
        reason_code="timeout",
        provider_code=None,
        error_class="TimeoutError",
    )
    assert failure.receipt_sha256 is None
    assert failure.semantic_fingerprint is None
    assert failure.diagnostic_sha256 is not None


def test_codec_validates_complete_envelopes_and_rejects_corruption() -> None:
    observed = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=INVOCATION_ID,
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=_observation(),
    )
    failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=INVOCATION_ID_2,
        attempted_at_ms=2_001,
        outcome_kind="retryable_error",
        reason_code="timeout",
        error_class="TimeoutError",
    )

    validate_lifecycle_attempt_audit_envelope(observed)
    validate_lifecycle_attempt_audit_envelope(failure)

    with pytest.raises(ValueError, match="compressed payload mismatch"):
        validate_lifecycle_attempt_audit_envelope(
            replace(
                observed,
                receipt_compressed_payload=b"not-zlib",
                receipt_compressed_bytes=len(b"not-zlib"),
            )
        )
    with pytest.raises(ValueError, match="carries observation fields"):
        validate_lifecycle_attempt_audit_envelope(
            replace(failure, semantic_schema="unexpected")
        )


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (
            lambda: build_lifecycle_attempt_audit_envelope(
                case_id="case-a",
                invocation_id=INVOCATION_ID.upper(),
                attempted_at_ms=1,
                outcome_kind="retryable_error",
            ),
            "canonical lowercase",
        ),
        (
            lambda: build_lifecycle_attempt_audit_envelope(
                case_id="case-a",
                invocation_id=INVOCATION_ID,
                attempted_at_ms=1,
                outcome_kind="future_outcome",
            ),
            "unsupported lifecycle attempt outcome",
        ),
        (
            lambda: build_lifecycle_attempt_audit_envelope(
                case_id="case-a",
                invocation_id=INVOCATION_ID,
                attempted_at_ms=1,
                outcome_kind="observed_complete",
                observation={**_observation(), "semantic_fingerprint": "11" * 31},
            ),
            "64 lowercase hexadecimal",
        ),
        (
            lambda: build_lifecycle_attempt_audit_envelope(
                case_id="case-a",
                invocation_id=INVOCATION_ID,
                attempted_at_ms=1,
                outcome_kind="observed_complete",
                observation={**_observation(), "bad": float("nan")},
            ),
            "non-finite",
        ),
        (
            lambda: compute_lifecycle_attempt_chain_sha256(
                previous_chain_sha256=LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
                case_id="case-a",
                ordinal=0,
                invocation_id=INVOCATION_ID,
                attempted_at_ms=1,
                outcome_code=1,
                semantic_fingerprint=bytes(32),
                receipt_sha256=bytes(32),
                diagnostic_sha256=None,
            ),
            "ordinal must be >= 1",
        ),
        (
            lambda: compute_lifecycle_attempt_chain_sha256(
                previous_chain_sha256=b"x" * 32,
                case_id="case-a",
                ordinal=1,
                invocation_id=INVOCATION_ID,
                attempted_at_ms=1,
                outcome_code=1,
                semantic_fingerprint=bytes(32),
                receipt_sha256=bytes(32),
                diagnostic_sha256=None,
            ),
            "must extend chain genesis",
        ),
    ],
)
def test_codec_fails_closed_on_noncanonical_or_unknown_inputs(
    call: object,
    match: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        call()  # type: ignore[operator]


def test_additive_schema_is_idempotent_without_changing_business_triggers(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    with repo._connect() as conn:  # noqa: SLF001 - additive upgrade contract
        conn.executescript(
            """
            DROP TRIGGER trg_trade_lifecycle_observation_spans_evidence_case_insert;
            DROP TRIGGER trg_trade_lifecycle_observation_spans_evidence_case_update;
            DROP TABLE trade_lifecycle_observation_spans;
            DROP TABLE trade_lifecycle_attempt_audits;
            DROP TABLE trade_lifecycle_receipt_blobs;
            DROP TABLE trade_lifecycle_attempt_audit_heads;
            """
        )
        business_before = _normalized_triggers(conn)
        _ensure_lifecycle_attempt_audit_schema(conn)
        first_cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        _ensure_lifecycle_attempt_audit_schema(conn)
        second_cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        all_after = _normalized_triggers(conn)
        business_after = {
            name: sql
            for name, sql in all_after.items()
            if not name.startswith("trg_trade_lifecycle_observation_spans_evidence_case_")
        }

        assert business_after == business_before
        assert second_cookie == first_cookie
        assert {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }.issuperset(SIDECAR_TABLES)
        table_sql = {
            str(row["name"]): str(row["sql"])
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "WITHOUT ROWID" in table_sql["trade_lifecycle_attempt_audits"].upper()
        assert "WITHOUT ROWID" in table_sql["trade_lifecycle_observation_spans"].upper()
        frozen_storage_columns = {
            "trade_lifecycle_attempt_audit_heads": (
                "audit_case_key",
                "last_ordinal",
                "chain_sha256",
                "current_span_ordinal",
                "last_invocation_id",
                "updated_at_ms",
            ),
            "trade_lifecycle_attempt_audits": (
                "audit_case_key",
                "ordinal",
                "invocation_id",
                "attempted_at_ms",
                "outcome_code",
                "semantic_fingerprint",
                "receipt_sha256",
                "diagnostic_sha256",
                "span_ordinal",
            ),
            "trade_lifecycle_observation_spans": (
                "audit_case_key",
                "span_ordinal",
                "semantic_fingerprint",
                "first_evidence_receipt_sha256",
                "first_success_ordinal",
                "first_success_at_ms",
                "last_success_ordinal",
                "last_success_at_ms",
                "successful_observation_count",
                "intervening_failed_attempt_count",
                "closed_chain_sha256",
                "last_receipt_sha256",
                "closed_at_ms",
            ),
            "trade_lifecycle_receipt_blobs": (
                "receipt_sha256",
                "codec_version",
                "uncompressed_bytes",
                "compressed_bytes",
                "compressed_payload",
                "created_at_ms",
            ),
        }
        for table, columns in frozen_storage_columns.items():
            normalized_sql = table_sql[table].lower().replace(" ", "")
            assert all(f"typeof({column})" in normalized_sql for column in columns)
        assert set(all_after) - set(business_after) == {
            "trg_trade_lifecycle_observation_spans_evidence_case_insert",
            "trg_trade_lifecycle_observation_spans_evidence_case_update",
        }


def test_schema_constraints_case_trigger_and_business_revisions_are_clean(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    repo.upsert_trade_lifecycle_case(_case("case-b", account="sy"))
    repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "evidence-b",
            "case_id": "case-b",
            "source_type": "test",
            "evidence_type": "settlement_observation",
            "account": "sy",
            "symbol": "NVDA",
        }
    )
    with repo._connect() as conn:  # noqa: SLF001 - schema constraint contract
        audit_case_key = _insert_genesis_head(conn, case_id="case-a")
        revision_before = conn.execute(
            "SELECT revision FROM trade_lifecycle_evidence_revisions WHERE case_id = 'case-b'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="evidence case mismatch"):
            conn.execute(
                """
                INSERT INTO trade_lifecycle_observation_spans (
                  audit_case_key, span_ordinal, semantic_schema,
                  semantic_fingerprint, first_evidence_id,
                  first_evidence_receipt_sha256,
                  first_success_ordinal, first_success_at_ms,
                  last_success_ordinal, last_success_at_ms,
                  successful_observation_count,
                  intervening_failed_attempt_count
                ) VALUES (?, 1, 'semantic.v1', ?, 'evidence-b', ?,
                          1, 1000, 1, 1000, 1, 0)
                """,
                (audit_case_key, bytes(32), bytes(32)),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO trade_lifecycle_attempt_audits (
                  audit_case_key, ordinal, invocation_id, attempted_at_ms,
                  outcome_code, semantic_fingerprint, receipt_sha256,
                  diagnostic_sha256, span_ordinal
                ) VALUES (?, 0, ?, 1000, 9, NULL, NULL, NULL, NULL)
                """,
                (audit_case_key, uuid.UUID(INVOCATION_ID).bytes),
            )
        revision_after = conn.execute(
            "SELECT revision FROM trade_lifecycle_evidence_revisions WHERE case_id = 'case-b'"
        ).fetchone()[0]
        assert revision_after == revision_before
    repo.assert_foreign_keys_clean()


def test_account_head_query_uses_case_account_index_and_reads_no_attempt_rows(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    for case_id, account in (("case-c", "lx"), ("case-a", "lx"), ("case-b", "sy")):
        repo.upsert_trade_lifecycle_case(_case(case_id, account=account))
    with repo._connect() as conn:  # noqa: SLF001 - query-plan contract
        for case_id in ("case-c", "case-a", "case-b"):
            _insert_genesis_head(conn, case_id=case_id)
        query_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT audit_head.audit_case_key, audit_head.case_id,
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
            ("lx",),
        ).fetchall()

    heads = repo.list_trade_lifecycle_attempt_audit_heads_for_account(account="lx")
    assert [head["case_id"] for head in heads] == ["case-a", "case-c"]
    assert {head["account"] for head in heads} == {"lx"}
    assert all(head["last_ordinal"] == 0 for head in heads)
    details = [str(row["detail"]) for row in query_plan]
    assert any("idx_trade_lifecycle_cases_lookup" in detail for detail in details)
    assert any("sqlite_autoindex_trade_lifecycle_attempt_audit_heads_1" in detail for detail in details)
    assert all("trade_lifecycle_attempt_audits" not in detail for detail in details)


def test_offline_verifier_replays_chain_and_detects_head_or_span_corruption(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    chain, invocation = _seed_one_observed_attempt(repo)

    head = repo.get_trade_lifecycle_attempt_audit_head(case_id="case-a")
    assert head is not None
    assert head["chain_sha256"] == chain
    assert repo.get_trade_lifecycle_attempt_audit_by_invocation(
        case_id="case-a",
        invocation_id=INVOCATION_ID,
    )["invocation_id"] == invocation
    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")
    assert verified["status"] == "valid"
    assert verified["audit_count"] == 1
    assert verified["span_count"] == 1
    assert verified["foreign_key_violation_count"] == 0
    assert verified["mismatch_samples"] == []

    with repo._connect() as conn:  # noqa: SLF001 - scoped FK corruption fixture
        conn.execute("PRAGMA foreign_keys = OFF")
        _insert_genesis_head(conn, case_id="case-orphan")
    still_valid = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")
    orphan = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-orphan")
    assert still_valid["status"] == "valid"
    assert still_valid["foreign_key_violation_count"] == 0
    assert orphan["status"] == "invalid"
    assert orphan["foreign_key_violation_count"] == 1
    assert orphan["mismatch_samples"][0]["code"] == "sidecar_foreign_key_violation"

    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        conn.execute(
            """
            UPDATE trade_lifecycle_attempt_audit_heads
            SET chain_sha256 = ?
            WHERE case_id = 'case-a'
            """,
            (b"x" * 32,),
        )
    corrupted = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")
    assert corrupted["status"] == "invalid"
    assert corrupted["mismatch_samples"][0]["code"] == "head_chain_mismatch"

    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        conn.execute(
            "UPDATE trade_lifecycle_attempt_audit_heads SET chain_sha256 = ? WHERE case_id = 'case-a'",
            (chain,),
        )
        conn.execute("DELETE FROM trade_lifecycle_observation_spans")
    missing_span = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")
    assert missing_span["status"] == "invalid"
    assert any(sample["code"] == "audit_span_missing" for sample in missing_span["mismatch_samples"])


@pytest.mark.parametrize("semantic_change", [False, True])
def test_offline_verifier_accepts_retained_blob_or_closed_semantic_span(
    tmp_path: Path,
    semantic_change: bool,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    _seed_second_observed_attempt(repo, semantic_change=semantic_change)

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "valid"
    assert verified["audit_count"] == 2
    assert verified["span_count"] == (2 if semantic_change else 1)
    assert verified["referenced_receipt_blob_count"] == (0 if semantic_change else 1)


def test_offline_verifier_accepts_pre_phase2_evidence_with_new_receipt(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    first_evidence_observation = _observation(receipt_note="pre-phase2")
    first_audit_observation = _observation(receipt_note="first-sidecar")
    assert (
        first_evidence_observation["semantic_fingerprint"]
        == first_audit_observation["semantic_fingerprint"]
    )
    assert audit_codec.canonical_lifecycle_observation_bytes(
        first_evidence_observation
    ) != audit_codec.canonical_lifecycle_observation_bytes(first_audit_observation)
    _seed_one_observed_attempt(
        repo,
        first_evidence_observation=first_evidence_observation,
        audit_observation=first_audit_observation,
    )

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "valid"
    assert verified["audit_count"] == 1
    assert verified["span_count"] == 1
    assert verified["referenced_receipt_blob_count"] == 1


@pytest.mark.parametrize("corruption", ["evidence_receipt", "commitment"])
def test_offline_verifier_rejects_first_evidence_receipt_commitment_corruption(
    tmp_path: Path,
    corruption: str,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    _seed_second_observed_attempt(repo, semantic_change=False)
    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        if corruption == "evidence_receipt":
            row = conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = 'evidence-a'"
            ).fetchone()
            evidence = json.loads(str(row["raw_json"]))
            evidence["observation"]["receipt_note"] = "tampered"
            conn.execute(
                "UPDATE trade_lifecycle_evidence SET raw_json = ? WHERE evidence_id = 'evidence-a'",
                (json.dumps(evidence, ensure_ascii=False, sort_keys=True),),
            )
        else:
            conn.execute(
                """
                UPDATE trade_lifecycle_observation_spans
                SET first_evidence_receipt_sha256 = ?
                """,
                (b"x" * 32,),
            )

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "invalid"
    assert any(
        sample["code"] == "span_first_evidence_receipt_commitment_mismatch"
        for sample in verified["mismatch_samples"]
    )


def test_offline_verifier_keeps_span_numbering_separate_from_attempt_ordinal(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id="123e4567-e89b-42d3-a456-426614174002",
        attempted_at_ms=1_900,
        outcome_kind="retryable_error",
        reason_code="timeout",
        error_class="TimeoutError",
    )
    head = repo.get_trade_lifecycle_attempt_audit_head(case_id="case-a")
    observed = repo.get_trade_lifecycle_attempt_audit_by_invocation(
        case_id="case-a",
        invocation_id=INVOCATION_ID,
    )
    assert head is not None and observed is not None
    failure_chain = compute_lifecycle_attempt_chain_sha256(
        previous_chain_sha256=LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
        case_id="case-a",
        ordinal=1,
        invocation_id=failure.invocation_id,
        attempted_at_ms=failure.attempted_at_ms,
        outcome_code=failure.outcome_code,
        semantic_fingerprint=None,
        receipt_sha256=None,
        diagnostic_sha256=failure.diagnostic_sha256,
    )
    final_chain = compute_lifecycle_attempt_chain_sha256(
        previous_chain_sha256=failure_chain,
        case_id="case-a",
        ordinal=2,
        invocation_id=observed["invocation_id"],
        attempted_at_ms=observed["attempted_at_ms"],
        outcome_code=observed["outcome_code"],
        semantic_fingerprint=observed["semantic_fingerprint"],
        receipt_sha256=observed["receipt_sha256"],
        diagnostic_sha256=None,
    )
    with repo._connect() as conn:  # noqa: SLF001 - focused fixture
        audit_case_key = int(head["audit_case_key"])
        conn.execute(
            """
            UPDATE trade_lifecycle_attempt_audits SET ordinal = 2
            WHERE audit_case_key = ? AND ordinal = 1
            """,
            (audit_case_key,),
        )
        conn.execute(
            """
            INSERT INTO trade_lifecycle_attempt_audits (
              audit_case_key, ordinal, invocation_id, attempted_at_ms,
              outcome_code, semantic_fingerprint, receipt_sha256,
              diagnostic_sha256, span_ordinal
            ) VALUES (?, 1, ?, 1900, 3, NULL, NULL, ?, NULL)
            """,
            (audit_case_key, failure.invocation_id, failure.diagnostic_sha256),
        )
        conn.execute(
            """
            UPDATE trade_lifecycle_observation_spans
            SET first_success_ordinal = 2, last_success_ordinal = 2
            WHERE audit_case_key = ? AND span_ordinal = 1
            """,
            (audit_case_key,),
        )
        conn.execute(
            """
            UPDATE trade_lifecycle_attempt_audit_heads
            SET last_ordinal = 2, chain_sha256 = ?, last_invocation_id = ?
            WHERE audit_case_key = ?
            """,
            (final_chain, observed["invocation_id"], audit_case_key),
        )

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "valid"
    assert verified["audit_count"] == 2
    assert verified["span_count"] == 1


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    [
        ("success_count", "span_success_count_mismatch"),
        ("failure_gap", "span_failure_gap_mismatch"),
        ("closed_boundary", "span_closed_chain_mismatch"),
    ],
)
def test_offline_verifier_detects_span_aggregate_or_boundary_corruption(
    tmp_path: Path,
    corruption: str,
    expected_code: str,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    if corruption == "closed_boundary":
        _seed_second_observed_attempt(repo, semantic_change=True)
    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        if corruption == "success_count":
            conn.execute(
                "UPDATE trade_lifecycle_observation_spans SET successful_observation_count = 2"
            )
        elif corruption == "failure_gap":
            conn.execute(
                "UPDATE trade_lifecycle_observation_spans SET intervening_failed_attempt_count = 1"
            )
        else:
            conn.execute(
                """
                UPDATE trade_lifecycle_observation_spans
                SET closed_chain_sha256 = ?
                WHERE span_ordinal = 1
                """,
                (b"x" * 32,),
            )

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "invalid"
    assert any(sample["code"] == expected_code for sample in verified["mismatch_samples"])


@pytest.mark.parametrize(
    "corruption",
    ["decompress", "hash", "uncompressed_bytes", "compressed_bytes"],
)
def test_offline_verifier_detects_receipt_blob_corruption(
    tmp_path: Path,
    corruption: str,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    _seed_second_observed_attempt(repo, semantic_change=False)
    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        if corruption == "decompress":
            conn.execute(
                """
                UPDATE trade_lifecycle_receipt_blobs
                SET compressed_payload = ?, compressed_bytes = ?
                """,
                (b"not-zlib", len(b"not-zlib")),
            )
        elif corruption == "hash":
            tampered = build_lifecycle_attempt_audit_envelope(
                case_id="case-a",
                invocation_id="123e4567-e89b-42d3-a456-426614174002",
                attempted_at_ms=2_200,
                outcome_kind="observed_complete",
                observation=_observation(receipt_note="tampered"),
            )
            conn.execute(
                """
                UPDATE trade_lifecycle_receipt_blobs
                SET uncompressed_bytes = ?, compressed_bytes = ?,
                    compressed_payload = ?
                """,
                (
                    tampered.receipt_uncompressed_bytes,
                    tampered.receipt_compressed_bytes,
                    tampered.receipt_compressed_payload,
                ),
            )
        elif corruption == "uncompressed_bytes":
            conn.execute(
                """
                UPDATE trade_lifecycle_receipt_blobs
                SET uncompressed_bytes = uncompressed_bytes + 1
                """
            )
        else:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                """
                UPDATE trade_lifecycle_receipt_blobs
                SET compressed_bytes = compressed_bytes + 1
                """
            )
            conn.execute("PRAGMA ignore_check_constraints = OFF")

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "invalid"
    assert any(sample["code"] == "invalid_receipt_blob" for sample in verified["mismatch_samples"])
    assert len(verified["mismatch_samples"]) <= 10


def test_offline_verifier_detects_evidence_suffix_and_admission_corruption(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    observation = _observation(option_position_absent=False)
    repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "evidence-unspanned",
            "case_id": "case-a",
            "source_type": "broker_settlement_observation",
            "evidence_type": "expire_close",
            "account": "lx",
            "symbol": "NVDA",
            "semantic_schema": observation["semantic_schema"],
            "semantic_fingerprint": observation["semantic_fingerprint"],
            "semantic_projection": observation["semantic_projection"],
            "observation": observation,
        }
    )
    _seed_second_observed_attempt(repo, semantic_change=True)

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")
    codes = {sample["code"] for sample in verified["mismatch_samples"]}

    assert verified["status"] == "invalid"
    assert "settlement_evidence_span_count_mismatch" in codes
    assert "span_evidence_sequence_mismatch" in codes
    assert "latest_admission_span_mismatch" not in codes


def test_offline_verifier_matches_admission_to_first_evidence_creation_time(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        conn.execute(
            """
            UPDATE trade_lifecycle_settlement_admission_heads
            SET evidence_created_at_ms = evidence_created_at_ms + 1
            WHERE case_id = 'case-a'
            """
        )

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "invalid"
    assert any(
        sample["code"] == "latest_admission_span_mismatch"
        for sample in verified["mismatch_samples"]
    )


def test_sidecar_storage_types_reject_text_and_verifier_bounds_malformed_rows(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    _seed_one_observed_attempt(repo)
    _seed_second_observed_attempt(repo, semantic_change=False)
    with repo._connect() as conn:  # noqa: SLF001 - raw storage contract
        mutations = (
            ("UPDATE trade_lifecycle_attempt_audit_heads SET last_ordinal = 'x'", ()),
            (
                "UPDATE trade_lifecycle_attempt_audit_heads SET chain_sha256 = ?",
                ("h" * 32,),
            ),
            (
                "UPDATE trade_lifecycle_attempt_audit_heads SET last_invocation_id = ?",
                ("u" * 16,),
            ),
            ("UPDATE trade_lifecycle_attempt_audits SET attempted_at_ms = 'x'", ()),
            (
                "UPDATE trade_lifecycle_attempt_audits SET invocation_id = ?",
                ("u" * 16,),
            ),
            (
                "UPDATE trade_lifecycle_observation_spans SET successful_observation_count = 'x'",
                (),
            ),
            (
                "UPDATE trade_lifecycle_observation_spans SET semantic_fingerprint = ?",
                ("h" * 32,),
            ),
            (
                "UPDATE trade_lifecycle_observation_spans "
                "SET first_evidence_receipt_sha256 = ?",
                ("h" * 32,),
            ),
            ("UPDATE trade_lifecycle_receipt_blobs SET uncompressed_bytes = 'x'", ()),
            (
                "UPDATE trade_lifecycle_receipt_blobs SET receipt_sha256 = ?",
                ("h" * 32,),
            ),
            ("UPDATE trade_lifecycle_receipt_blobs SET compressed_payload = 'text'", ()),
        )
        for sql, params in mutations:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE trade_lifecycle_observation_spans SET successful_observation_count = 'x'"
        )
        conn.execute("PRAGMA ignore_check_constraints = OFF")

    verified = repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")

    assert verified["status"] == "invalid"
    assert len(verified["mismatch_samples"]) <= 10
    assert any(sample["code"] == "invalid_span_row" for sample in verified["mismatch_samples"])


def test_atomic_sidecar_writer_tracks_initial_failure_gap_and_exact_replay(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    first_failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(1),
        attempted_at_ms=1_900,
        outcome_kind="retryable_error",
        reason_code="provider_busy",
    )
    observation = _observation()
    first_success = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(2),
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=observation,
    )
    intervening_failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(3),
        attempted_at_ms=2_100,
        outcome_kind="unknown_error",
        error_class="TimeoutError",
    )
    repeated_observation = _observation(receipt_note="second receipt")
    second_success = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(4),
        attempted_at_ms=2_200,
        outcome_kind="observed_complete",
        observation=repeated_observation,
    )

    assert _append_attempt(repo, first_failure)["audit_ordinal"] == 1
    assert _append_attempt(
        repo,
        first_success,
        evidence_id="evidence-a",
        evidence_observation=observation,
    )["audit_ordinal"] == 2
    assert _append_attempt(repo, intervening_failure)["audit_ordinal"] == 3
    written = _append_attempt(
        repo,
        second_success,
        evidence_id="evidence-a",
    )
    assert written["audit_ordinal"] == 4
    assert written["audit_idempotent"] is False

    with repo._connect() as conn:  # noqa: SLF001 - focused row contract
        before = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in sorted(SIDECAR_TABLES)
        )
        span = dict(
            conn.execute(
                "SELECT * FROM trade_lifecycle_observation_spans"
            ).fetchone()
        )
    bytes_before = _checkpointed_storage_bytes(repo)
    replay = _append_attempt(
        repo,
        second_success,
        evidence_id="evidence-a",
    )
    with repo._connect() as conn:  # noqa: SLF001 - focused row contract
        after = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in sorted(SIDECAR_TABLES)
        )
    bytes_after = _checkpointed_storage_bytes(repo)

    assert replay["audit_idempotent"] is True
    assert replay["audit_ordinal"] == 4
    assert before == after
    assert bytes_before == bytes_after
    assert span["first_success_ordinal"] == 2
    assert span["last_success_ordinal"] == 4
    assert span["successful_observation_count"] == 2
    assert span["intervening_failed_attempt_count"] == 1
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id="case-a"
    )["status"] == "valid"

    with pytest.raises(ValueError, match="invocation replay mismatch"):
        _append_attempt(
            repo,
            replace(second_success, attempted_at_ms=2_201),
            evidence_id="evidence-a",
        )
    with repo._connect() as conn:  # noqa: SLF001 - mismatch zero-write proof
        mismatch_after = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in sorted(SIDECAR_TABLES)
        )
    assert mismatch_after == before
    assert _checkpointed_storage_bytes(repo) == bytes_before

    later_failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(5),
        attempted_at_ms=2_300,
        outcome_kind="retryable_error",
        reason_code="provider_busy",
    )
    _append_attempt(repo, later_failure)
    with pytest.raises(ValueError, match="historical lifecycle attempt"):
        _append_attempt(
            repo,
            second_success,
            evidence_id="evidence-a",
        )


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("noncanonical_json", "JSON is not canonical"),
        ("stale_semantic_metadata", "semantic fingerprint mismatch"),
    ],
)
def test_atomic_sidecar_writer_rejects_invalid_receipt_before_commit(
    tmp_path: Path,
    corruption: str,
    match: str,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    case_before = repo.get_trade_lifecycle_case("case-a")
    observation = _observation()
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(10),
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=observation,
    )
    if corruption == "noncanonical_json":
        receipt = json.dumps(
            observation,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
    else:
        stale_observation = json.loads(json.dumps(observation))
        stale_observation["broker_option_position_absent"] = False
        receipt = audit_codec.canonical_lifecycle_observation_bytes(
            stale_observation
        )
    compressed = zlib.compress(receipt)
    invalid = replace(
        envelope,
        receipt_sha256=audit_codec.lifecycle_receipt_sha256(receipt),
        canonical_receipt_bytes=receipt,
        receipt_compressed_payload=compressed,
        receipt_uncompressed_bytes=len(receipt),
        receipt_compressed_bytes=len(compressed),
    )

    with pytest.raises(ValueError, match=match):
        _append_attempt(
            repo,
            invalid,
            evidence_id="evidence-a",
            evidence_observation=observation,
        )

    with repo._connect() as conn:  # noqa: SLF001 - zero-commit proof
        assert all(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in SIDECAR_TABLES
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM trade_lifecycle_evidence_revisions "
            "WHERE case_id = 'case-a'"
        ).fetchone()[0] == 0
    assert repo.list_trade_lifecycle_evidence(case_id="case-a") == []
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id="case-a"
    ) is None
    assert repo.get_trade_lifecycle_case("case-a") == case_before


def test_atomic_sidecar_writer_moves_one_live_receipt_and_cleans_old_blob(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    admitted = _observation(receipt_note="R0")
    receipt_r1 = _observation(receipt_note="R1")
    receipt_r2 = _observation(receipt_note="R2")
    first = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(11),
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=receipt_r1,
    )
    second = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(12),
        attempted_at_ms=2_100,
        outcome_kind="observed_complete",
        observation=receipt_r2,
    )

    first_result = _append_attempt(
        repo,
        first,
        evidence_id="evidence-a",
        evidence_observation=admitted,
    )
    second_result = _append_attempt(
        repo,
        second,
        evidence_id="evidence-a",
    )

    assert first_result["_cleanup_receipt_sha256"] is None
    assert second_result["_cleanup_receipt_sha256"] == first.receipt_sha256
    assert repo.delete_unreferenced_trade_lifecycle_receipt_blob(
        first.receipt_sha256
    )
    assert not repo.delete_unreferenced_trade_lifecycle_receipt_blob(
        first.receipt_sha256
    )
    with repo._connect() as conn:  # noqa: SLF001 - focused blob contract
        hashes = {
            row[0]
            for row in conn.execute(
                "SELECT receipt_sha256 FROM trade_lifecycle_receipt_blobs"
            )
        }
        span = dict(
            conn.execute(
                "SELECT * FROM trade_lifecycle_observation_spans"
            ).fetchone()
        )
    assert hashes == {second.receipt_sha256}
    assert span["first_evidence_receipt_sha256"] not in hashes
    assert span["last_receipt_sha256"] == second.receipt_sha256
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id="case-a"
    )["status"] == "valid"


def test_atomic_sidecar_writer_creates_three_spans_for_a_b_a(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    observations = (
        _observation(option_position_absent=True),
        _observation(option_position_absent=False),
        _observation(option_position_absent=True),
    )
    results: list[dict[str, object]] = []
    for index, observation in enumerate(observations, start=1):
        envelope = build_lifecycle_attempt_audit_envelope(
            case_id="case-a",
            invocation_id=_invocation(20 + index),
            attempted_at_ms=2_000 + index,
            outcome_kind="observed_complete",
            observation=observation,
        )
        results.append(
            _append_attempt(
                repo,
                envelope,
                evidence_id=f"evidence-{index}",
                evidence_observation=observation,
            )
        )

    with repo._connect() as conn:  # noqa: SLF001 - focused span contract
        spans = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM trade_lifecycle_observation_spans "
                "ORDER BY span_ordinal"
            )
        ]
    assert [row["span_ordinal"] for row in spans] == [1, 2, 3]
    assert spans[0]["closed_chain_sha256"].hex() == results[0][
        "audit_chain_sha256"
    ]
    assert spans[1]["closed_chain_sha256"].hex() == results[1][
        "audit_chain_sha256"
    ]
    assert spans[2]["closed_chain_sha256"] is None
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id="case-a"
    )["status"] == "valid"


def test_atomic_sidecar_writer_opens_new_span_for_semantic_schema_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    current = _observation()
    _append_attempt(
        repo,
        build_lifecycle_attempt_audit_envelope(
            case_id="case-a",
            invocation_id=_invocation(30),
            attempted_at_ms=2_000,
            outcome_kind="observed_complete",
            observation=current,
        ),
        evidence_id="evidence-v1",
        evidence_observation=current,
    )

    upgraded_projection = {
        **current["semantic_projection"],
        "schema_version": "settlement_observation_semantic.v2",
    }
    upgraded = {
        **current,
        "semantic_schema": upgraded_projection["schema_version"],
        "semantic_projection": upgraded_projection,
        "semantic_fingerprint": settlement_semantics.canonical_hash(
            upgraded_projection
        ),
    }

    def versioned_semantic(evidence: dict[str, object]):
        observation = evidence["observation"]
        assert isinstance(observation, dict)
        return observation["semantic_projection"], observation["semantic_fingerprint"]

    monkeypatch.setattr(
        audit_codec,
        "settlement_semantic_from_evidence",
        versioned_semantic,
    )
    monkeypatch.setattr(
        repository_module,
        "settlement_semantic_from_evidence",
        versioned_semantic,
    )
    _append_attempt(
        repo,
        build_lifecycle_attempt_audit_envelope(
            case_id="case-a",
            invocation_id=_invocation(31),
            attempted_at_ms=3_000,
            outcome_kind="observed_complete",
            observation=upgraded,
        ),
        evidence_id="evidence-v2",
        evidence_observation=upgraded,
    )

    with repo._connect() as conn:  # noqa: SLF001 - focused span contract
        spans = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM trade_lifecycle_observation_spans "
                "ORDER BY span_ordinal"
            )
        ]
    assert [row["semantic_schema"] for row in spans] == [
        "settlement_observation_semantic.v1",
        "settlement_observation_semantic.v2",
    ]
    assert spans[0]["closed_chain_sha256"] is not None
    assert spans[1]["closed_chain_sha256"] is None
    assert repo.verify_trade_lifecycle_attempt_audit_case(case_id="case-a")[
        "status"
    ] == "valid"


def test_audit_only_writer_persists_failure_without_business_or_evidence(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    case_before = repo.get_trade_lifecycle_case("case-a")
    failure = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(31),
        attempted_at_ms=2_000,
        outcome_kind="stale_generation_after_call",
        reason_code="generation_changed",
    )

    first = record_lifecycle_attempt_audit_atomically(
        repo,
        attempt_audit=failure,
    )
    replay = record_lifecycle_attempt_audit_atomically(
        repo,
        attempt_audit=failure,
    )

    assert first["audit_ordinal"] == 1
    assert first["audit_idempotent"] is False
    assert replay["audit_ordinal"] == 1
    assert replay["audit_idempotent"] is True
    assert repo.get_trade_lifecycle_case("case-a") == case_before
    assert repo.list_trade_lifecycle_evidence(case_id="case-a") == []
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id="case-a"
    )["status"] == "valid"

    observed = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(32),
        attempted_at_ms=2_100,
        outcome_kind="observed_complete",
        observation=_observation(),
    )
    with pytest.raises(ValueError, match="only failed or stale"):
        record_lifecycle_attempt_audit_atomically(
            repo,
            attempt_audit=observed,
        )


def test_atomic_sidecar_writer_keeps_identical_n_compact(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    observation = _observation()
    for offset in range(5):
        envelope = build_lifecycle_attempt_audit_envelope(
            case_id="case-a",
            invocation_id=_invocation(40 + offset),
            attempted_at_ms=2_000 + offset,
            outcome_kind="observed_complete",
            observation=observation,
        )
        _append_attempt(
            repo,
            envelope,
            evidence_id="evidence-a",
            evidence_observation=observation if offset == 0 else None,
        )

    with repo._connect() as conn:  # noqa: SLF001 - focused compactness contract
        counts = {
            "audits": conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_attempt_audits"
            ).fetchone()[0],
            "spans": conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_observation_spans"
            ).fetchone()[0],
            "blobs": conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_receipt_blobs"
            ).fetchone()[0],
            "evidence": conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_evidence"
            ).fetchone()[0],
        }
    assert counts == {"audits": 5, "spans": 1, "blobs": 0, "evidence": 1}
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id="case-a"
    )["status"] == "valid"


def test_shared_receipt_blob_is_not_deleted_when_latest_span_moves(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    admitted_a = _observation(receipt_note="A admitted")
    shared_a = _observation(receipt_note="shared A receipt")
    observation_b = _observation(option_position_absent=False)
    moved_a = _observation(receipt_note="moved A receipt")

    first_a = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(51),
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=shared_a,
    )
    b = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(52),
        attempted_at_ms=2_100,
        outcome_kind="observed_complete",
        observation=observation_b,
    )
    second_a = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(53),
        attempted_at_ms=2_200,
        outcome_kind="observed_complete",
        observation=shared_a,
    )
    moved = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(54),
        attempted_at_ms=2_300,
        outcome_kind="observed_complete",
        observation=moved_a,
    )

    _append_attempt(
        repo,
        first_a,
        evidence_id="evidence-a1",
        evidence_observation=admitted_a,
    )
    _append_attempt(
        repo,
        b,
        evidence_id="evidence-b",
        evidence_observation=observation_b,
    )
    _append_attempt(
        repo,
        second_a,
        evidence_id="evidence-a2",
        evidence_observation=admitted_a,
    )
    move_result = _append_attempt(
        repo,
        moved,
        evidence_id="evidence-a2",
    )

    assert move_result["_cleanup_receipt_sha256"] == first_a.receipt_sha256
    assert not repo.delete_unreferenced_trade_lifecycle_receipt_blob(
        first_a.receipt_sha256
    )
    with repo._connect() as conn:  # noqa: SLF001 - focused shared-reference contract
        references = int(
            conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_observation_spans "
                "WHERE last_receipt_sha256 = ?",
                (first_a.receipt_sha256,),
            ).fetchone()[0]
        )
        stored = conn.execute(
            "SELECT 1 FROM trade_lifecycle_receipt_blobs "
            "WHERE receipt_sha256 = ?",
            (first_a.receipt_sha256,),
        ).fetchone()
    assert references == 1
    assert stored is not None
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id="case-a"
    )["status"] == "valid"


@pytest.mark.parametrize(
    ("operation", "table"),
    (
        ("INSERT", "trade_lifecycle_receipt_blobs"),
        ("INSERT", "trade_lifecycle_observation_spans"),
        ("INSERT", "trade_lifecycle_attempt_audits"),
        ("UPDATE", "trade_lifecycle_attempt_audit_heads"),
    ),
)
def test_atomic_sidecar_writer_rolls_back_every_component_failure(
    tmp_path: Path,
    operation: str,
    table: str,
) -> None:
    repo = _repo(tmp_path)
    repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    admitted = _observation(receipt_note="R0")
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id="case-a",
        invocation_id=_invocation(60),
        attempted_at_ms=2_000,
        outcome_kind="observed_complete",
        observation=_observation(receipt_note="R1"),
    )
    repo.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "evidence-a",
            "case_id": "case-a",
            "source_type": "broker_settlement_observation",
            "evidence_type": "expire_close",
            "account": "lx",
            "symbol": "NVDA",
            "semantic_schema": admitted["semantic_schema"],
            "semantic_fingerprint": admitted["semantic_fingerprint"],
            "semantic_projection": admitted["semantic_projection"],
            "observation": admitted,
        }
    )
    conn = repo._connect()  # noqa: SLF001 - focused crash contract
    try:
        conn.execute(
            f"""
            CREATE TEMP TRIGGER injected_sidecar_failure
            BEFORE {operation} ON {table}
            BEGIN
              SELECT RAISE(ABORT, 'injected sidecar failure');
            END
            """
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="injected sidecar failure"):
            repo.append_trade_lifecycle_attempt_audit_in_transaction(
                attempt_audit=envelope,
                first_evidence_id="evidence-a",
                conn=conn,
            )
        conn.rollback()
        counts = {
            table_name: int(
                conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            )
            for table_name in SIDECAR_TABLES
        }
    finally:
        conn.close()
    assert counts == {table_name: 0 for table_name in SIDECAR_TABLES}
