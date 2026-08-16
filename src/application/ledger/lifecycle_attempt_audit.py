from __future__ import annotations

import hashlib
import json
import math
import uuid
import zlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from src.application.ledger.lifecycle_settlement_semantics import (
    settlement_semantic_from_evidence,
)


LIFECYCLE_ATTEMPT_CHAIN_SCHEMA = "trade_lifecycle_attempt_chain.v1"
LIFECYCLE_ATTEMPT_RUN_SEAL_SCHEMA = "trade_lifecycle_attempt_run_seal.v1"
LIFECYCLE_RECEIPT_SCHEMA = "trade_lifecycle_receipt.v1"
LIFECYCLE_RECEIPT_CODEC = "zlib"
LIFECYCLE_RECEIPT_CODEC_VERSION = 1
LIFECYCLE_ATTEMPT_CHAIN_GENESIS = bytes(32)

LIFECYCLE_ATTEMPT_OUTCOME_CODES = MappingProxyType(
    {
        "observed_complete": 1,
        "observed_incomplete": 2,
        "retryable_error": 3,
        "unknown_error": 4,
        "blocked_account_explicit": 5,
        "stale_generation_after_call": 6,
        "processing_failure_after_call": 7,
        "legacy_semantic_unavailable_after_call": 8,
    }
)
_OBSERVED_OUTCOME_CODES = frozenset((1, 2))
_FAILURE_OUTCOME_CODES = frozenset(range(3, 9))
_RECEIPT_HASH_PREFIX = LIFECYCLE_RECEIPT_SCHEMA.encode("utf-8") + b"\0"
_CHAIN_HASH_PREFIX = LIFECYCLE_ATTEMPT_CHAIN_SCHEMA.encode("utf-8") + b"\0"
_DIAGNOSTIC_HASH_PREFIX = b"trade_lifecycle_attempt_diagnostic.v1\0"
_RUN_SEAL_HASH_PREFIX = LIFECYCLE_ATTEMPT_RUN_SEAL_SCHEMA.encode("utf-8") + b"\0"
_RUN_SEAL_REASONS = {
    "touched_heads": frozenset(("ordinary_due",)),
    "all_heads_checkpoint": frozenset(
        ("process_startup", "cli_apply", "prior_seal_persist_failed")
    ),
}


@dataclass(frozen=True, slots=True)
class LifecycleAttemptAuditEnvelope:
    case_id: str
    invocation_id: bytes
    attempted_at_ms: int
    outcome_kind: str
    outcome_code: int
    semantic_schema: str | None
    semantic_fingerprint: bytes | None
    receipt_sha256: bytes | None
    diagnostic_sha256: bytes | None
    canonical_receipt_bytes: bytes | None
    receipt_compressed_payload: bytes | None
    receipt_uncompressed_bytes: int | None
    receipt_compressed_bytes: int | None
    receipt_codec: str | None
    receipt_codec_version: int | None


def lifecycle_invocation_id_bytes(value: str | uuid.UUID | bytes) -> bytes:
    if isinstance(value, uuid.UUID):
        parsed = value
    elif type(value) is bytes:
        if len(value) != 16:
            raise ValueError("lifecycle invocation_id must be exactly 16 bytes")
        parsed = uuid.UUID(bytes=value)
    elif isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("lifecycle invocation_id must be a canonical UUIDv4") from exc
        if value != str(parsed):
            raise ValueError("lifecycle invocation_id must be canonical lowercase UUID text")
    else:
        raise TypeError("lifecycle invocation_id must be UUID text, uuid.UUID, or bytes")
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError("lifecycle invocation_id must be an RFC 4122 UUIDv4")
    return parsed.bytes


def lifecycle_sha256_bytes(value: str | bytes, *, field: str = "sha256") -> bytes:
    if type(value) is bytes:
        parsed = value
    elif isinstance(value, str):
        if len(value) != 64 or value != value.lower():
            raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
        try:
            parsed = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be 64 lowercase hexadecimal characters") from exc
    else:
        raise TypeError(f"{field} must be raw bytes or lowercase hexadecimal text")
    if len(parsed) != 32:
        raise ValueError(f"{field} must be exactly 32 bytes")
    return parsed


def lifecycle_attempt_outcome_code(outcome_kind: str) -> int:
    value = str(outcome_kind or "").strip()
    try:
        return int(LIFECYCLE_ATTEMPT_OUTCOME_CODES[value])
    except KeyError as exc:
        raise ValueError(f"unsupported lifecycle attempt outcome: {value or '<empty>'}") from exc


def canonical_lifecycle_observation_bytes(observation: Mapping[str, Any]) -> bytes:
    if type(observation) is not dict:
        raise TypeError("lifecycle observation must be a plain JSON object")
    _validate_json_value(observation, path="observation")
    return json.dumps(
        observation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def lifecycle_receipt_sha256(canonical_receipt_bytes: bytes) -> bytes:
    if type(canonical_receipt_bytes) is not bytes:
        raise TypeError("canonical lifecycle receipt must be bytes")
    return hashlib.sha256(_RECEIPT_HASH_PREFIX + canonical_receipt_bytes).digest()


def build_lifecycle_attempt_run_seal(
    *,
    account: str,
    source_id: str,
    completed_at_ms: int,
    heads: Sequence[Mapping[str, Any]],
    seal_scope: str,
    reason: str,
) -> dict[str, Any]:
    return _lifecycle_attempt_run_seal(
        account=account,
        source_id=source_id,
        completed_at_ms=completed_at_ms,
        heads=heads,
        seal_scope=seal_scope,
        reason=reason,
        require_head_account=True,
    )


def validate_lifecycle_attempt_run_seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    if type(payload) is not dict:
        raise TypeError("lifecycle attempt run seal must be a plain JSON object")
    expected_fields = {
        "schema_version",
        "seal_scope",
        "reason",
        "account",
        "source_id",
        "completed_at_ms",
        "head_count",
        "heads",
        "seal_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("lifecycle attempt run seal fields are not canonical")
    _nonnegative_sqlite_int(payload.get("head_count"), field="head_count")
    provided_hash = lifecycle_sha256_bytes(
        payload.get("seal_sha256"),  # type: ignore[arg-type]
        field="seal_sha256",
    ).hex()
    heads = payload.get("heads")
    if type(heads) is not list:
        raise ValueError("lifecycle attempt run seal heads must be a JSON array")
    canonical = _lifecycle_attempt_run_seal(
        account=payload.get("account"),  # type: ignore[arg-type]
        source_id=payload.get("source_id"),  # type: ignore[arg-type]
        completed_at_ms=payload.get("completed_at_ms"),  # type: ignore[arg-type]
        heads=heads,
        seal_scope=payload.get("seal_scope"),  # type: ignore[arg-type]
        reason=payload.get("reason"),  # type: ignore[arg-type]
        require_head_account=False,
    )
    if payload.get("head_count") != canonical["head_count"]:
        raise ValueError("lifecycle attempt run seal head_count mismatch")
    if provided_hash != canonical["seal_sha256"]:
        raise ValueError("lifecycle attempt run seal hash mismatch")
    if dict(payload) != canonical:
        raise ValueError("lifecycle attempt run seal payload is not canonical")
    return canonical


def _lifecycle_attempt_run_seal(
    *,
    account: Any,
    source_id: Any,
    completed_at_ms: Any,
    heads: Sequence[Mapping[str, Any]],
    seal_scope: Any,
    reason: Any,
    require_head_account: bool,
) -> dict[str, Any]:
    account_value = _required_text(account, field="account")
    if account_value != account_value.lower():
        raise ValueError("lifecycle attempt run seal account must be lowercase")
    source_value = _required_text(source_id, field="source_id")
    completed_value = _positive_sqlite_int(completed_at_ms, field="completed_at_ms")
    scope_value = _required_text(seal_scope, field="seal_scope")
    reason_value = _required_text(reason, field="reason")
    if reason_value not in _RUN_SEAL_REASONS.get(scope_value, ()):
        raise ValueError("lifecycle attempt run seal scope/reason is unsupported")
    canonical_heads = _canonical_run_seal_heads(
        heads,
        account=account_value,
        require_head_account=require_head_account,
    )
    if scope_value == "touched_heads" and not canonical_heads:
        raise ValueError("touched-head lifecycle attempt run seal cannot be empty")
    metadata = (
        LIFECYCLE_ATTEMPT_RUN_SEAL_SCHEMA,
        scope_value,
        reason_value,
        account_value,
        source_value,
        completed_value,
        len(canonical_heads),
    )
    hasher = hashlib.sha256()
    hasher.update(_RUN_SEAL_HASH_PREFIX)
    hasher.update(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    hasher.update(b"\n")
    for head in canonical_heads:
        hasher.update(
            json.dumps(
                (head["case_id"], head["last_ordinal"], head["chain_sha256"]),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        hasher.update(b"\n")
    return {
        "schema_version": LIFECYCLE_ATTEMPT_RUN_SEAL_SCHEMA,
        "seal_scope": scope_value,
        "reason": reason_value,
        "account": account_value,
        "source_id": source_value,
        "completed_at_ms": completed_value,
        "head_count": len(canonical_heads),
        "heads": canonical_heads,
        "seal_sha256": hasher.hexdigest(),
    }


def verify_lifecycle_attempt_run_seal(
    payload: Mapping[str, Any],
    *,
    current_heads: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    seal = validate_lifecycle_attempt_run_seal(payload)
    current = _canonical_run_seal_heads(
        current_heads,
        account=seal["account"],
        require_head_account=True,
    )
    current_by_case = {head["case_id"]: head for head in current}
    mismatch_count = 0
    samples: list[dict[str, Any]] = []

    def mismatch(code: str, **details: Any) -> None:
        nonlocal mismatch_count
        mismatch_count += 1
        if len(samples) < 10:
            samples.append({"code": code, **details})

    sealed_case_ids = set()
    for sealed in seal["heads"]:
        case_id = sealed["case_id"]
        sealed_case_ids.add(case_id)
        current_head = current_by_case.get(case_id)
        if current_head is None:
            mismatch("sealed_head_missing", case_id=case_id)
        elif current_head != sealed:
            mismatch("sealed_head_changed", case_id=case_id)
    if seal["seal_scope"] == "all_heads_checkpoint":
        for case_id in sorted(set(current_by_case) - sealed_case_ids):
            mismatch("checkpoint_head_unsealed", case_id=case_id)
    return {
        "schema_version": "trade_lifecycle_attempt_run_seal_verify.v1",
        "status": "valid" if mismatch_count == 0 else "invalid",
        "account": seal["account"],
        "seal_scope": seal["seal_scope"],
        "seal_sha256": seal["seal_sha256"],
        "sealed_head_count": seal["head_count"],
        "current_head_count": len(current),
        "mismatch_count": mismatch_count,
        "mismatch_samples": samples,
    }


def _canonical_run_seal_heads(
    heads: Sequence[Mapping[str, Any]],
    *,
    account: str,
    require_head_account: bool,
) -> list[dict[str, Any]]:
    if not isinstance(heads, Sequence) or isinstance(heads, (str, bytes, bytearray)):
        raise TypeError("lifecycle attempt run seal heads must be a sequence")
    by_case: dict[str, tuple[int, bytes]] = {}
    for raw_head in heads:
        if not isinstance(raw_head, Mapping):
            raise TypeError("lifecycle attempt run seal head must be an object")
        if require_head_account:
            head_account = _required_text(raw_head.get("account"), field="head.account")
            if head_account != account:
                raise ValueError("lifecycle attempt run seal head belongs to another account")
        case_id = _required_text(raw_head.get("case_id"), field="head.case_id")
        ordinal = _nonnegative_sqlite_int(raw_head.get("last_ordinal"), field="head.last_ordinal")
        chain = lifecycle_sha256_bytes(
            raw_head.get("chain_sha256"),  # type: ignore[arg-type]
            field="head.chain_sha256",
        )
        if (ordinal == 0) != (chain == LIFECYCLE_ATTEMPT_CHAIN_GENESIS):
            raise ValueError("lifecycle attempt run seal head genesis is invalid")
        previous = by_case.get(case_id)
        if previous is None or ordinal > previous[0]:
            by_case[case_id] = (ordinal, chain)
        elif ordinal == previous[0] and chain != previous[1]:
            raise ValueError("lifecycle attempt run seal duplicate head conflicts")

    return [
        {
            "case_id": case_id,
            "last_ordinal": ordinal,
            "chain_sha256": chain.hex(),
        }
        for case_id, (ordinal, chain) in sorted(by_case.items())
    ]


def lifecycle_attempt_diagnostic_sha256(
    *,
    reason_code: str | None,
    provider_code: str | None,
    error_class: str | None,
) -> bytes:
    fields = tuple(
        _diagnostic_text(value, field=field)
        for field, value in (
            ("reason_code", reason_code),
            ("provider_code", provider_code),
            ("error_class", error_class),
        )
    )
    payload = json.dumps(
        (1, *fields),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_DIAGNOSTIC_HASH_PREFIX + payload).digest()


def build_lifecycle_attempt_audit_envelope(
    *,
    case_id: str,
    invocation_id: str | uuid.UUID | bytes,
    attempted_at_ms: int,
    outcome_kind: str,
    observation: Mapping[str, Any] | None = None,
    reason_code: str | None = None,
    provider_code: str | None = None,
    error_class: str | None = None,
) -> LifecycleAttemptAuditEnvelope:
    case_value = _required_text(case_id, field="case_id")
    attempted_value = _positive_sqlite_int(attempted_at_ms, field="attempted_at_ms")
    outcome_value = str(outcome_kind or "").strip()
    outcome_code = lifecycle_attempt_outcome_code(outcome_value)
    invocation_bytes = lifecycle_invocation_id_bytes(invocation_id)

    if outcome_code in _OBSERVED_OUTCOME_CODES:
        if observation is None:
            raise ValueError("observed lifecycle attempt requires an observation")
        if any(value not in (None, "") for value in (reason_code, provider_code, error_class)):
            raise ValueError("observed lifecycle attempt cannot carry failure diagnostics")
        semantic_schema = _required_text(
            observation.get("semantic_schema"),
            field="observation.semantic_schema",
        )
        semantic_fingerprint = lifecycle_sha256_bytes(
            observation.get("semantic_fingerprint"),  # type: ignore[arg-type]
            field="observation.semantic_fingerprint",
        )
        canonical_receipt = canonical_lifecycle_observation_bytes(observation)
        receipt_hash = lifecycle_receipt_sha256(canonical_receipt)
        compressed = zlib.compress(canonical_receipt)
        return LifecycleAttemptAuditEnvelope(
            case_id=case_value,
            invocation_id=invocation_bytes,
            attempted_at_ms=attempted_value,
            outcome_kind=outcome_value,
            outcome_code=outcome_code,
            semantic_schema=semantic_schema,
            semantic_fingerprint=semantic_fingerprint,
            receipt_sha256=receipt_hash,
            diagnostic_sha256=None,
            canonical_receipt_bytes=canonical_receipt,
            receipt_compressed_payload=compressed,
            receipt_uncompressed_bytes=len(canonical_receipt),
            receipt_compressed_bytes=len(compressed),
            receipt_codec=LIFECYCLE_RECEIPT_CODEC,
            receipt_codec_version=LIFECYCLE_RECEIPT_CODEC_VERSION,
        )

    if observation is not None:
        raise ValueError("failed lifecycle attempt cannot carry an observation receipt")
    return LifecycleAttemptAuditEnvelope(
        case_id=case_value,
        invocation_id=invocation_bytes,
        attempted_at_ms=attempted_value,
        outcome_kind=outcome_value,
        outcome_code=outcome_code,
        semantic_schema=None,
        semantic_fingerprint=None,
        receipt_sha256=None,
        diagnostic_sha256=lifecycle_attempt_diagnostic_sha256(
            reason_code=reason_code,
            provider_code=provider_code,
            error_class=error_class,
        ),
        canonical_receipt_bytes=None,
        receipt_compressed_payload=None,
        receipt_uncompressed_bytes=None,
        receipt_compressed_bytes=None,
        receipt_codec=None,
        receipt_codec_version=None,
    )


def validate_lifecycle_attempt_audit_envelope(
    envelope: LifecycleAttemptAuditEnvelope,
) -> None:
    if type(envelope) is not LifecycleAttemptAuditEnvelope:
        raise TypeError("lifecycle attempt audit envelope is invalid")
    _required_text(envelope.case_id, field="case_id")
    if (
        lifecycle_invocation_id_bytes(envelope.invocation_id)
        != envelope.invocation_id
    ):
        raise ValueError("lifecycle attempt invocation_id is not canonical bytes")
    _positive_sqlite_int(envelope.attempted_at_ms, field="attempted_at_ms")
    if lifecycle_attempt_outcome_code(envelope.outcome_kind) != envelope.outcome_code:
        raise ValueError("lifecycle attempt outcome mapping mismatch")

    if envelope.outcome_code in _OBSERVED_OUTCOME_CODES:
        semantic_schema = _required_text(
            envelope.semantic_schema,
            field="semantic_schema",
        )
        semantic_fingerprint = lifecycle_sha256_bytes(
            envelope.semantic_fingerprint,  # type: ignore[arg-type]
            field="semantic_fingerprint",
        )
        receipt_hash = lifecycle_sha256_bytes(
            envelope.receipt_sha256,  # type: ignore[arg-type]
            field="receipt_sha256",
        )
        if envelope.diagnostic_sha256 is not None:
            raise ValueError(
                "observed lifecycle attempt cannot carry a diagnostic hash"
            )
        canonical_receipt = envelope.canonical_receipt_bytes
        compressed_payload = envelope.receipt_compressed_payload
        if type(canonical_receipt) is not bytes:
            raise ValueError("observed lifecycle receipt must be bytes")
        if type(compressed_payload) is not bytes:
            raise ValueError("observed lifecycle compressed receipt must be bytes")
        uncompressed_bytes = _nonnegative_sqlite_int(
            envelope.receipt_uncompressed_bytes,
            field="receipt_uncompressed_bytes",
        )
        compressed_bytes = _positive_sqlite_int(
            envelope.receipt_compressed_bytes,
            field="receipt_compressed_bytes",
        )
        if envelope.receipt_codec != LIFECYCLE_RECEIPT_CODEC:
            raise ValueError("lifecycle receipt codec is unsupported")
        if envelope.receipt_codec_version != LIFECYCLE_RECEIPT_CODEC_VERSION:
            raise ValueError("lifecycle receipt codec version is unsupported")
        if uncompressed_bytes != len(canonical_receipt):
            raise ValueError("lifecycle receipt uncompressed byte count mismatch")
        if compressed_bytes != len(compressed_payload):
            raise ValueError("lifecycle receipt compressed byte count mismatch")
        decompressor = zlib.decompressobj()
        try:
            decoded = decompressor.decompress(
                compressed_payload,
                uncompressed_bytes + 1,
            )
        except zlib.error as exc:
            raise ValueError(
                "lifecycle receipt compressed payload mismatch"
            ) from exc
        if (
            decoded != canonical_receipt
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise ValueError("lifecycle receipt compressed payload mismatch")
        if lifecycle_receipt_sha256(canonical_receipt) != receipt_hash:
            raise ValueError("lifecycle receipt content hash mismatch")
        try:
            observation = json.loads(canonical_receipt)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("lifecycle receipt JSON is invalid") from exc
        if type(observation) is not dict:
            raise ValueError("lifecycle receipt must contain a JSON object")
        if canonical_lifecycle_observation_bytes(observation) != canonical_receipt:
            raise ValueError("lifecycle receipt JSON is not canonical")
        semantic_projection, computed_fingerprint = settlement_semantic_from_evidence(
            {"observation": observation}
        )
        if observation.get("semantic_schema") != semantic_schema:
            raise ValueError("lifecycle receipt semantic schema mismatch")
        if (
            semantic_projection.get("schema_version") != semantic_schema
            or lifecycle_sha256_bytes(
                computed_fingerprint,
                field="computed.semantic_fingerprint",
            )
            != semantic_fingerprint
        ):
            raise ValueError("lifecycle receipt semantic fingerprint mismatch")
        return

    if envelope.outcome_code in _FAILURE_OUTCOME_CODES:
        if any(
            value is not None
            for value in (
                envelope.semantic_schema,
                envelope.semantic_fingerprint,
                envelope.receipt_sha256,
                envelope.canonical_receipt_bytes,
                envelope.receipt_compressed_payload,
                envelope.receipt_uncompressed_bytes,
                envelope.receipt_compressed_bytes,
                envelope.receipt_codec,
                envelope.receipt_codec_version,
            )
        ):
            raise ValueError("failed lifecycle attempt carries observation fields")
        lifecycle_sha256_bytes(
            envelope.diagnostic_sha256,  # type: ignore[arg-type]
            field="diagnostic_sha256",
        )
        return
    raise ValueError("lifecycle attempt outcome_code is not recognized by mapping v1")


def compute_lifecycle_attempt_chain_sha256(
    *,
    previous_chain_sha256: str | bytes,
    case_id: str,
    ordinal: int,
    invocation_id: str | uuid.UUID | bytes,
    attempted_at_ms: int,
    outcome_code: int,
    semantic_fingerprint: str | bytes | None,
    receipt_sha256: str | bytes | None,
    diagnostic_sha256: str | bytes | None,
) -> bytes:
    previous = lifecycle_sha256_bytes(previous_chain_sha256, field="previous_chain_sha256")
    case_bytes = _required_text(case_id, field="case_id").encode("utf-8")
    if len(case_bytes) > 0xFFFFFFFF:
        raise ValueError("case_id is too long for lifecycle attempt chain v1")
    ordinal_value = _positive_sqlite_int(ordinal, field="ordinal")
    if ordinal_value == 1 and previous != LIFECYCLE_ATTEMPT_CHAIN_GENESIS:
        raise ValueError("lifecycle attempt ordinal one must extend chain genesis")
    attempted_value = _positive_sqlite_int(attempted_at_ms, field="attempted_at_ms")
    outcome_value = _known_outcome_code(outcome_code)
    invocation_bytes = lifecycle_invocation_id_bytes(invocation_id)
    hashes = tuple(
        LIFECYCLE_ATTEMPT_CHAIN_GENESIS
        if value is None
        else lifecycle_sha256_bytes(value, field=field)
        for field, value in (
            ("semantic_fingerprint", semantic_fingerprint),
            ("receipt_sha256", receipt_sha256),
            ("diagnostic_sha256", diagnostic_sha256),
        )
    )
    preimage = b"".join(
        (
            _CHAIN_HASH_PREFIX,
            previous,
            len(case_bytes).to_bytes(4, "big"),
            case_bytes,
            ordinal_value.to_bytes(8, "big"),
            invocation_bytes,
            attempted_value.to_bytes(8, "big"),
            outcome_value.to_bytes(2, "big"),
            *hashes,
        )
    )
    return hashlib.sha256(preimage).digest()


def verify_lifecycle_attempt_audit_chain(
    *,
    case_id: str,
    head: Mapping[str, Any] | None,
    audit_rows: Sequence[Mapping[str, Any]],
    span_rows: Sequence[Mapping[str, Any]] = (),
    evidence_rows: Sequence[Mapping[str, Any]] = (),
    receipt_blob_rows: Sequence[Mapping[str, Any]] = (),
    admission_head: Mapping[str, Any] | None = None,
    foreign_key_rows: Sequence[Mapping[str, Any]] = (),
    mismatch_limit: int = 10,
) -> dict[str, Any]:
    case_value = _required_text(case_id, field="case_id")
    if mismatch_limit < 1:
        raise ValueError("mismatch_limit must be >= 1")
    samples: list[dict[str, Any]] = []
    mismatch_count = 0

    def mismatch(code: str, **context: Any) -> None:
        nonlocal mismatch_count
        mismatch_count += 1
        if len(samples) < mismatch_limit:
            samples.append({"code": code, **context})

    for row in foreign_key_rows:
        try:
            foreign_key_id = _nonnegative_sqlite_int(
                row.get("fkid"),
                field="foreign_key_id",
            )
        except (TypeError, ValueError):
            foreign_key_id = -1
        mismatch(
            "sidecar_foreign_key_violation",
            table=str(row.get("table") or "")[:80],
            foreign_key_id=foreign_key_id,
        )

    if head is None:
        if audit_rows:
            mismatch("audit_rows_without_head", count=len(audit_rows))
        if span_rows:
            mismatch("span_rows_without_head", count=len(span_rows))
        if receipt_blob_rows:
            mismatch("receipt_blobs_without_head", count=len(receipt_blob_rows))
        return {
            "schema_version": "trade_lifecycle_attempt_audit_verify.v1",
            "case_id": case_value,
            "status": "invalid" if mismatch_count else "absent",
            "audit_count": len(audit_rows),
            "span_count": len(span_rows),
            "referenced_receipt_blob_count": len(receipt_blob_rows),
            "foreign_key_violation_count": len(foreign_key_rows),
            "computed_last_ordinal": 0,
            "computed_chain_sha256": LIFECYCLE_ATTEMPT_CHAIN_GENESIS.hex(),
            "mismatch_count": mismatch_count,
            "mismatch_samples": samples,
        }

    head_case_id = str(head.get("case_id") or "")
    if head_case_id != case_value:
        mismatch("head_case_mismatch", stored_case_id=head_case_id[:128])
    try:
        audit_case_key = _positive_sqlite_int(
            head.get("audit_case_key"),
            field="audit_case_key",
        )
        stored_last_ordinal = _nonnegative_sqlite_int(
            head.get("last_ordinal"),
            field="last_ordinal",
        )
        stored_chain = lifecycle_sha256_bytes(
            head.get("chain_sha256"),  # type: ignore[arg-type]
            field="chain_sha256",
        )
        stored_current_span = _optional_positive_sqlite_int(
            head.get("current_span_ordinal"),
            field="current_span_ordinal",
        )
        _positive_sqlite_int(head.get("updated_at_ms"), field="updated_at_ms")
    except (TypeError, ValueError) as exc:
        mismatch("invalid_head", detail=str(exc))
        audit_case_key = -1
        stored_last_ordinal = -1
        stored_chain = None
        stored_current_span = None

    spans: list[dict[str, Any]] = []
    spans_by_ordinal: dict[int, dict[str, Any]] = {}
    for row in span_rows:
        try:
            span_ordinal = _positive_sqlite_int(
                row.get("span_ordinal"),
                field="span_ordinal",
            )
            row_case_key = _positive_sqlite_int(
                row.get("audit_case_key"),
                field="audit_case_key",
            )
            semantic_schema = _required_text(
                row.get("semantic_schema"),
                field="semantic_schema",
            )
            semantic_fingerprint = lifecycle_sha256_bytes(
                row.get("semantic_fingerprint"),  # type: ignore[arg-type]
                field="semantic_fingerprint",
            )
            first_evidence_id = _required_text(
                row.get("first_evidence_id"),
                field="first_evidence_id",
            )
            first_evidence_receipt = lifecycle_sha256_bytes(
                row.get("first_evidence_receipt_sha256"),  # type: ignore[arg-type]
                field="first_evidence_receipt_sha256",
            )
            first_success_ordinal = _positive_sqlite_int(
                row.get("first_success_ordinal"),
                field="first_success_ordinal",
            )
            first_success_at_ms = _positive_sqlite_int(
                row.get("first_success_at_ms"),
                field="first_success_at_ms",
            )
            last_success_ordinal = _positive_sqlite_int(
                row.get("last_success_ordinal"),
                field="last_success_ordinal",
            )
            last_success_at_ms = _positive_sqlite_int(
                row.get("last_success_at_ms"),
                field="last_success_at_ms",
            )
            success_count = _positive_sqlite_int(
                row.get("successful_observation_count"),
                field="successful_observation_count",
            )
            failure_count = _nonnegative_sqlite_int(
                row.get("intervening_failed_attempt_count"),
                field="intervening_failed_attempt_count",
            )
            closed_chain = _optional_sha256(
                row.get("closed_chain_sha256"),
                field="closed_chain_sha256",
            )
            last_receipt = _optional_sha256(
                row.get("last_receipt_sha256"),
                field="last_receipt_sha256",
            )
            closed_at_ms = _optional_positive_sqlite_int(
                row.get("closed_at_ms"),
                field="closed_at_ms",
            )
            if row_case_key != audit_case_key:
                raise ValueError("span audit_case_key does not match head")
            if span_ordinal != len(spans) + 1:
                raise ValueError("span ordinals must be continuous from one")
            if last_success_ordinal < first_success_ordinal:
                raise ValueError("span last success precedes first success")
            if last_success_at_ms < first_success_at_ms:
                raise ValueError("span last success time precedes first success")
            if (closed_chain is None) != (closed_at_ms is None):
                raise ValueError("span close fields must both be null or both be present")
        except (TypeError, ValueError) as exc:
            mismatch("invalid_span_row", detail=str(exc))
            continue
        parsed = {
            "span_ordinal": span_ordinal,
            "semantic_schema": semantic_schema,
            "semantic_fingerprint": semantic_fingerprint,
            "first_evidence_id": first_evidence_id,
            "first_evidence_receipt_sha256": first_evidence_receipt,
            "first_success_ordinal": first_success_ordinal,
            "first_success_at_ms": first_success_at_ms,
            "last_success_ordinal": last_success_ordinal,
            "last_success_at_ms": last_success_at_ms,
            "successful_observation_count": success_count,
            "intervening_failed_attempt_count": failure_count,
            "closed_chain_sha256": closed_chain,
            "last_receipt_sha256": last_receipt,
            "closed_at_ms": closed_at_ms,
            "first_evidence_case_id": row.get("first_evidence_case_id"),
            "first_evidence_created_at_ms": row.get(
                "first_evidence_created_at_ms"
            ),
            "actual_success_count": 0,
            "actual_failure_count": 0,
            "actual_first_ordinal": None,
            "actual_first_at_ms": None,
            "actual_last_ordinal": None,
            "actual_last_at_ms": None,
            "actual_last_receipt": None,
        }
        spans.append(parsed)
        spans_by_ordinal[span_ordinal] = parsed

    boundary_spans: dict[int, dict[str, Any]] = {}
    for index, span in enumerate(spans):
        if index + 1 == len(spans):
            if span["closed_chain_sha256"] is not None:
                mismatch("current_span_is_closed", span_ordinal=span["span_ordinal"])
            continue
        next_span = spans[index + 1]
        boundary_ordinal = int(next_span["first_success_ordinal"])
        if span["closed_chain_sha256"] is None:
            mismatch("prior_span_is_open", span_ordinal=span["span_ordinal"])
        if boundary_ordinal <= int(span["last_success_ordinal"]):
            mismatch("span_boundary_overlap", span_ordinal=span["span_ordinal"])
        if (
            span["semantic_schema"] == next_span["semantic_schema"]
            and span["semantic_fingerprint"] == next_span["semantic_fingerprint"]
        ):
            mismatch("adjacent_span_semantic_duplicate", span_ordinal=span["span_ordinal"])
        boundary_spans[boundary_ordinal] = span

    expected_current_span = spans[-1]["span_ordinal"] if spans else None
    if stored_current_span != expected_current_span:
        mismatch(
            "head_current_span_mismatch",
            stored=stored_current_span,
            computed=expected_current_span,
        )

    chain = LIFECYCLE_ATTEMPT_CHAIN_GENESIS
    expected_ordinal = 1
    invocation_ids: set[bytes] = set()
    last_invocation_id: bytes | None = None
    computed_last_ordinal = 0
    active_span_ordinal: int | None = None
    chain_complete = True
    for row in audit_rows:
        try:
            ordinal = _positive_sqlite_int(row.get("ordinal"), field="ordinal")
            row_case_key = _positive_sqlite_int(
                row.get("audit_case_key"),
                field="audit_case_key",
            )
            invocation = lifecycle_invocation_id_bytes(row.get("invocation_id"))  # type: ignore[arg-type]
            attempted_at_ms = _positive_sqlite_int(
                row.get("attempted_at_ms"),
                field="attempted_at_ms",
            )
            semantic = _optional_sha256(row.get("semantic_fingerprint"), field="semantic_fingerprint")
            receipt = _optional_sha256(row.get("receipt_sha256"), field="receipt_sha256")
            diagnostic = _optional_sha256(row.get("diagnostic_sha256"), field="diagnostic_sha256")
            outcome = _known_outcome_code(row.get("outcome_code"))
            _validate_outcome_fields(
                outcome_code=outcome,
                semantic_fingerprint=semantic,
                receipt_sha256=receipt,
                diagnostic_sha256=diagnostic,
                span_ordinal=row.get("span_ordinal"),
            )
            span_ordinal = _optional_positive_sqlite_int(
                row.get("span_ordinal"),
                field="span_ordinal",
            )
            if row_case_key != audit_case_key:
                raise ValueError("audit audit_case_key does not match head")
        except (TypeError, ValueError) as exc:
            mismatch("invalid_audit_row", detail=str(exc))
            chain_complete = False
            continue

        if ordinal != expected_ordinal:
            mismatch("audit_ordinal_gap", expected=expected_ordinal, actual=ordinal)
            chain_complete = False
        expected_ordinal = ordinal + 1
        computed_last_ordinal = ordinal
        if invocation in invocation_ids:
            mismatch("duplicate_invocation_id", ordinal=ordinal)
        invocation_ids.add(invocation)
        last_invocation_id = invocation

        closing_span = boundary_spans.pop(ordinal, None)
        if closing_span is not None:
            if chain_complete and closing_span["closed_chain_sha256"] != chain:
                mismatch(
                    "span_closed_chain_mismatch",
                    span_ordinal=closing_span["span_ordinal"],
                )
            if closing_span["closed_at_ms"] != attempted_at_ms:
                mismatch(
                    "span_closed_at_mismatch",
                    span_ordinal=closing_span["span_ordinal"],
                )

        if outcome in _OBSERVED_OUTCOME_CODES:
            span = spans_by_ordinal.get(span_ordinal)
            if span is None:
                mismatch("audit_span_missing", ordinal=ordinal)
            else:
                if active_span_ordinal is None:
                    if (
                        span_ordinal != 1
                        or ordinal != span["first_success_ordinal"]
                    ):
                        mismatch("audit_span_boundary_mismatch", ordinal=ordinal)
                elif span_ordinal != active_span_ordinal:
                    if (
                        span_ordinal != active_span_ordinal + 1
                        or ordinal != span["first_success_ordinal"]
                    ):
                        mismatch("audit_span_sequence_mismatch", ordinal=ordinal)
                active_span_ordinal = span_ordinal
                if semantic != span["semantic_fingerprint"]:
                    mismatch("audit_span_semantic_mismatch", ordinal=ordinal)
                if span["actual_success_count"] == 0:
                    span["actual_first_ordinal"] = ordinal
                    span["actual_first_at_ms"] = attempted_at_ms
                span["actual_success_count"] += 1
                span["actual_last_ordinal"] = ordinal
                span["actual_last_at_ms"] = attempted_at_ms
                span["actual_last_receipt"] = receipt
        elif active_span_ordinal is not None:
            span = spans_by_ordinal.get(active_span_ordinal)
            if span is not None:
                span["actual_failure_count"] += 1

        if chain_complete:
            try:
                chain = compute_lifecycle_attempt_chain_sha256(
                    previous_chain_sha256=chain,
                    case_id=case_value,
                    ordinal=ordinal,
                    invocation_id=invocation,
                    attempted_at_ms=attempted_at_ms,
                    outcome_code=outcome,
                    semantic_fingerprint=semantic,
                    receipt_sha256=receipt,
                    diagnostic_sha256=diagnostic,
                )
            except (TypeError, ValueError) as exc:
                mismatch("invalid_audit_chain_input", ordinal=ordinal, detail=str(exc))
                chain_complete = False

    for boundary_ordinal, span in boundary_spans.items():
        mismatch(
            "span_boundary_audit_missing",
            span_ordinal=span["span_ordinal"],
            boundary_ordinal=boundary_ordinal,
        )
    if stored_last_ordinal != computed_last_ordinal:
        mismatch("head_ordinal_mismatch", stored=stored_last_ordinal, computed=computed_last_ordinal)
    if chain_complete and stored_chain is not None and stored_chain != chain:
        mismatch("head_chain_mismatch", stored=stored_chain.hex(), computed=chain.hex())
    stored_last_invocation = head.get("last_invocation_id")
    if stored_last_ordinal == 0:
        if stored_chain is not None and stored_chain != LIFECYCLE_ATTEMPT_CHAIN_GENESIS:
            mismatch("invalid_genesis_chain")
        if stored_last_invocation is not None:
            mismatch("genesis_last_invocation_present")
    else:
        try:
            parsed_last_invocation = lifecycle_invocation_id_bytes(stored_last_invocation)  # type: ignore[arg-type]
            if last_invocation_id is not None and parsed_last_invocation != last_invocation_id:
                mismatch("head_last_invocation_mismatch")
        except (TypeError, ValueError) as exc:
            mismatch("invalid_head_last_invocation", detail=str(exc))

    evidence_states: list[dict[str, Any] | None] = []
    for row in evidence_rows:
        try:
            evidence_id = _required_text(
                row.get("evidence_id"),
                field="evidence.evidence_id",
            )
            if row.get("case_id") != case_value:
                raise ValueError("settlement evidence belongs to another case")
            evidence_created_at_ms = _positive_sqlite_int(
                row.get("created_at_ms"),
                field="evidence.created_at_ms",
            )
            raw_evidence = row.get("raw_json")
            if not isinstance(raw_evidence, str):
                raise ValueError("settlement evidence JSON is unavailable")
            evidence = json.loads(raw_evidence)
            if type(evidence) is not dict:
                raise ValueError("settlement evidence must be a JSON object")
            semantic_projection, fingerprint_hex = settlement_semantic_from_evidence(
                evidence
            )
            semantic_schema = _required_text(
                semantic_projection.get("schema_version"),
                field="evidence.semantic_schema",
            )
            semantic_fingerprint = lifecycle_sha256_bytes(
                fingerprint_hex,
                field="evidence.semantic_fingerprint",
            )
            receipt_hash = lifecycle_receipt_sha256(
                canonical_lifecycle_observation_bytes(
                    evidence.get("observation")  # type: ignore[arg-type]
                )
            )
            evidence_states.append(
                {
                    "evidence_id": evidence_id,
                    "created_at_ms": evidence_created_at_ms,
                    "semantic_schema": semantic_schema,
                    "semantic_fingerprint": semantic_fingerprint,
                    "receipt_sha256": receipt_hash,
                    "evidence_type": evidence.get("evidence_type"),
                }
            )
        except Exception as exc:
            mismatch("invalid_settlement_evidence_sequence", detail=str(exc))
            evidence_states.append(None)
    if len(evidence_states) != len(spans):
        mismatch(
            "settlement_evidence_span_count_mismatch",
            evidence_count=len(evidence_states),
            span_count=len(spans),
        )

    for span in spans:
        span_ordinal = int(span["span_ordinal"])
        for stored_field, actual_field, code in (
            ("first_success_ordinal", "actual_first_ordinal", "span_first_ordinal_mismatch"),
            ("first_success_at_ms", "actual_first_at_ms", "span_first_time_mismatch"),
            ("last_success_ordinal", "actual_last_ordinal", "span_last_ordinal_mismatch"),
            ("last_success_at_ms", "actual_last_at_ms", "span_last_time_mismatch"),
            (
                "successful_observation_count",
                "actual_success_count",
                "span_success_count_mismatch",
            ),
            (
                "intervening_failed_attempt_count",
                "actual_failure_count",
                "span_failure_gap_mismatch",
            ),
        ):
            if span[stored_field] != span[actual_field]:
                mismatch(code, span_ordinal=span_ordinal)

        evidence = (
            evidence_states[span_ordinal - 1]
            if span_ordinal <= len(evidence_states)
            else None
        )
        if evidence is not None:
            if (
                span["first_evidence_case_id"] != case_value
                or span["first_evidence_id"] != evidence["evidence_id"]
                or span["semantic_schema"] != evidence["semantic_schema"]
                or span["semantic_fingerprint"] != evidence["semantic_fingerprint"]
                or span["first_evidence_created_at_ms"] != evidence["created_at_ms"]
            ):
                mismatch("span_evidence_sequence_mismatch", span_ordinal=span_ordinal)
            if (
                evidence["receipt_sha256"]
                != span["first_evidence_receipt_sha256"]
            ):
                mismatch(
                    "span_first_evidence_receipt_commitment_mismatch",
                    span_ordinal=span_ordinal,
                )
            span["evidence_type"] = evidence["evidence_type"]
        else:
            span["evidence_type"] = None

        first_receipt = span["first_evidence_receipt_sha256"]
        actual_last_receipt = span["actual_last_receipt"]
        if actual_last_receipt is not None:
            expected_last_reference = (
                None if first_receipt == actual_last_receipt else actual_last_receipt
            )
            if span["last_receipt_sha256"] != expected_last_reference:
                mismatch("span_last_receipt_reference_mismatch", span_ordinal=span_ordinal)

    if spans:
        latest_span = spans[-1]
        try:
            if admission_head is None:
                raise ValueError("settlement admission head is missing")
            admission_case_id = _required_text(
                admission_head.get("case_id"),
                field="admission.case_id",
            )
            admission_schema = _required_text(
                admission_head.get("semantic_schema"),
                field="admission.semantic_schema",
            )
            admission_fingerprint = lifecycle_sha256_bytes(
                admission_head.get("semantic_fingerprint"),  # type: ignore[arg-type]
                field="admission.semantic_fingerprint",
            )
            admission_evidence_id = _required_text(
                admission_head.get("evidence_id"),
                field="admission.evidence_id",
            )
            admission_evidence_created_at_ms = _positive_sqlite_int(
                admission_head.get("evidence_created_at_ms"),
                field="admission.evidence_created_at_ms",
            )
            _positive_sqlite_int(
                admission_head.get("updated_at_ms"),
                field="admission.updated_at_ms",
            )
            if (
                admission_case_id != case_value
                or admission_schema != latest_span["semantic_schema"]
                or admission_fingerprint != latest_span["semantic_fingerprint"]
                or admission_evidence_id != latest_span["first_evidence_id"]
                or admission_evidence_created_at_ms
                != latest_span["first_evidence_created_at_ms"]
            ):
                mismatch("latest_admission_span_mismatch")
        except (TypeError, ValueError) as exc:
            mismatch("invalid_latest_admission", detail=str(exc))

    spans_by_blob: dict[bytes, list[dict[str, Any]]] = {}
    for span in spans:
        receipt_hash = span["last_receipt_sha256"]
        if receipt_hash is not None:
            spans_by_blob.setdefault(receipt_hash, []).append(span)
    expected_blob_hashes = set(spans_by_blob)
    valid_blob_hashes: set[bytes] = set()
    for row in receipt_blob_rows:
        try:
            receipt_hash = lifecycle_sha256_bytes(
                row.get("receipt_sha256"),  # type: ignore[arg-type]
                field="blob.receipt_sha256",
            )
            if row.get("codec") != LIFECYCLE_RECEIPT_CODEC:
                raise ValueError("receipt blob codec is unsupported")
            codec_version = _positive_sqlite_int(
                row.get("codec_version"),
                field="blob.codec_version",
            )
            if codec_version != LIFECYCLE_RECEIPT_CODEC_VERSION:
                raise ValueError("receipt blob codec version is unsupported")
            uncompressed_bytes = _nonnegative_sqlite_int(
                row.get("uncompressed_bytes"),
                field="blob.uncompressed_bytes",
            )
            compressed_bytes = _positive_sqlite_int(
                row.get("compressed_bytes"),
                field="blob.compressed_bytes",
            )
            compressed_payload = row.get("compressed_payload")
            if type(compressed_payload) is not bytes:
                raise ValueError("receipt blob compressed payload must be bytes")
            _positive_sqlite_int(row.get("created_at_ms"), field="blob.created_at_ms")
            if compressed_bytes != len(compressed_payload):
                raise ValueError("receipt blob compressed byte count mismatch")
            decompressor = zlib.decompressobj()
            canonical_receipt = decompressor.decompress(
                compressed_payload,
                uncompressed_bytes + 1,
            )
            if (
                len(canonical_receipt) != uncompressed_bytes
                or not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
            ):
                raise ValueError("receipt blob decompression or byte count mismatch")
            if lifecycle_receipt_sha256(canonical_receipt) != receipt_hash:
                raise ValueError("receipt blob content hash mismatch")
            observation = json.loads(canonical_receipt)
            if type(observation) is not dict:
                raise ValueError("receipt blob must contain a JSON object")
            if canonical_lifecycle_observation_bytes(observation) != canonical_receipt:
                raise ValueError("receipt blob JSON is not canonical")
            if receipt_hash in valid_blob_hashes:
                raise ValueError("duplicate receipt blob row")
            for span in spans_by_blob.get(receipt_hash, ()):
                semantic_projection, fingerprint_hex = settlement_semantic_from_evidence(
                    {
                        "evidence_type": span["evidence_type"],
                        "observation": observation,
                    }
                )
                blob_fingerprint = lifecycle_sha256_bytes(
                    fingerprint_hex,
                    field="blob.semantic_fingerprint",
                )
                if (
                    semantic_projection.get("schema_version")
                    != span["semantic_schema"]
                    or blob_fingerprint != span["semantic_fingerprint"]
                ):
                    mismatch(
                        "receipt_blob_span_semantic_mismatch",
                        span_ordinal=span["span_ordinal"],
                    )
            valid_blob_hashes.add(receipt_hash)
        except Exception as exc:
            mismatch("invalid_receipt_blob", detail=str(exc))

    for receipt_hash in expected_blob_hashes:
        if receipt_hash not in valid_blob_hashes:
            mismatch(
                "referenced_receipt_blob_missing",
                receipt_sha256=receipt_hash.hex(),
            )

    return {
        "schema_version": "trade_lifecycle_attempt_audit_verify.v1",
        "case_id": case_value,
        "status": "valid" if mismatch_count == 0 else "invalid",
        "audit_count": len(audit_rows),
        "span_count": len(span_rows),
        "referenced_receipt_blob_count": len(expected_blob_hashes),
        "foreign_key_violation_count": len(foreign_key_rows),
        "computed_last_ordinal": computed_last_ordinal,
        "computed_chain_sha256": chain.hex() if chain_complete else None,
        "mismatch_count": mismatch_count,
        "mismatch_samples": samples,
    }


def _validate_json_value(value: Any, *, path: str) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} contains a non-string object key")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _diagnostic_text(value: str | None, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text or null")
    return value.strip()


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty text without surrounding whitespace")
    return value


def _positive_sqlite_int(value: Any, *, field: str) -> int:
    parsed = _nonnegative_sqlite_int(value, field=field)
    if parsed < 1:
        raise ValueError(f"{field} must be >= 1")
    return parsed


def _nonnegative_sqlite_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value < 0 or value > (2**63 - 1):
        raise ValueError(f"{field} must be a non-negative SQLite integer")
    return value


def _known_outcome_code(value: Any) -> int:
    if type(value) is not int or value not in LIFECYCLE_ATTEMPT_OUTCOME_CODES.values():
        raise ValueError("lifecycle attempt outcome_code is not recognized by mapping v1")
    return value


def _optional_sha256(value: Any, *, field: str) -> bytes | None:
    return None if value is None else lifecycle_sha256_bytes(value, field=field)


def _optional_positive_sqlite_int(value: Any, *, field: str) -> int | None:
    return None if value is None else _positive_sqlite_int(value, field=field)


def _validate_outcome_fields(
    *,
    outcome_code: int,
    semantic_fingerprint: bytes | None,
    receipt_sha256: bytes | None,
    diagnostic_sha256: bytes | None,
    span_ordinal: Any,
) -> None:
    if outcome_code in _OBSERVED_OUTCOME_CODES:
        if semantic_fingerprint is None or receipt_sha256 is None or diagnostic_sha256 is not None:
            raise ValueError("observed lifecycle audit hash fields are incomplete")
        _positive_sqlite_int(span_ordinal, field="span_ordinal")
        return
    if outcome_code in _FAILURE_OUTCOME_CODES:
        if semantic_fingerprint is not None or receipt_sha256 is not None or diagnostic_sha256 is None:
            raise ValueError("failed lifecycle audit hash fields are invalid")
        if span_ordinal is not None:
            raise ValueError("failed lifecycle audit cannot reference an observation span")
        return
    raise ValueError("lifecycle attempt outcome_code is not recognized by mapping v1")
