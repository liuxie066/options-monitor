from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import scope_for
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    adopt_source_snapshot,
    build_source_manifest,
    publish_source_receipt,
    sha256_bytes,
    validate_source_manifest,
    validate_source_receipt,
)
from src.infrastructure.position_advice_manifest_lock import (
    PositionAdviceLockError,
    manifest_file_lock,
    position_advice_manifest_locks,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
IDENTITY_HASH = "a" * 64
POLICY_HASH = "b" * 64


def _publish(
    root: Path,
    *,
    kind: str,
    source_native_id: str,
    observed_at: datetime = NOW,
    producer_scope: str = "global",
    producer_account_run_id: str | None = None,
    account: str | None = None,
    dependencies: list[dict[str, object]] | None = None,
    capacity_pool_authority_id: str | None = None,
) -> tuple[Path, dict[str, object]]:
    receipt_path = root / f"{kind}.receipt.json"
    receipt = publish_source_receipt(
        producer_root=root,
        receipt_relpath=receipt_path.name,
        payload_relpath=f"payloads/{kind}.json",
        payload_bytes=json.dumps({"kind": kind, "native": source_native_id}).encode(),
        source_kind=kind,
        producer_schema_version=f"{kind}.v1",
        producer_run_id=f"producer-{kind}",
        producer_scope=producer_scope,
        producer_account_run_id=producer_account_run_id,
        broker="futu" if account else None,
        account=account,
        portfolio_account_identity_hash=IDENTITY_HASH if account else None,
        included_markets=["US"],
        source_native_id=source_native_id,
        source_observed_at=observed_at.isoformat(),
        completed_at=(observed_at + timedelta(seconds=2)).isoformat(),
        producer_policy_hash=POLICY_HASH,
        dependencies=dependencies or [],
        capacity_pool_authority_id=capacity_pool_authority_id,
    )
    return receipt_path, receipt


def _dependency(
    receipt_path: Path,
    receipt: dict[str, object],
    *,
    root: Path,
) -> dict[str, object]:
    validated = validate_source_receipt(
        receipt,
        producer_root=root,
        now=NOW + timedelta(seconds=10),
    )
    return {
        "source_kind": receipt["source_kind"],
        "snapshot_id": receipt["snapshot_id"],
        "receipt_hash": sha256_bytes(receipt_path.read_bytes()),
        "payload_sha256": receipt["payload_sha256"],
        "expires_at": validated["expires_at"],
    }


def _build_valid_manifest(
    tmp_path: Path,
) -> tuple[Path, dict[str, object]]:
    producer = tmp_path / "producer"
    producer.mkdir()
    run_root = tmp_path / "run"
    quote_path, quote = _publish(
        producer,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    candidate_path, _candidate = _publish(
        producer,
        kind="candidate_decisions",
        source_native_id="candidate-batch-1",
        producer_scope="account",
        producer_account_run_id="run-1",
        account="lx",
        dependencies=[_dependency(quote_path, quote, root=producer)],
    )
    adopted_quote = adopt_source_snapshot(
        receipt_path=quote_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(seconds=10),
    )
    adopted_candidate = adopt_source_snapshot(
        receipt_path=candidate_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(seconds=10),
        expected_account="lx",
        expected_identity_hash=IDENTITY_HASH,
    )
    return run_root, build_source_manifest(
        account_run_id="run-1",
        portfolio_scope_id=scope_for("lx"),
        portfolio_account_identity_hash=IDENTITY_HASH,
        adopted_sources=[adopted_quote, adopted_candidate],
        required_for_actions={
            "quotes": ["covered_call", "short_put"],
            "candidate_decisions": ["short_put"],
        },
    )


def test_source_receipt_is_payload_first_deterministic_and_ignores_mtime(tmp_path: Path) -> None:
    receipt_path, receipt = _publish(
        tmp_path,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    payload_path = tmp_path / str(receipt["payload_relpath"])
    before = validate_source_receipt(
        receipt,
        producer_root=tmp_path,
        now=NOW + timedelta(minutes=1),
    )
    os.utime(payload_path, (2_000_000_000, 2_000_000_000))
    after = validate_source_receipt(
        json.loads(receipt_path.read_text()),
        producer_root=tmp_path,
        now=NOW + timedelta(minutes=1),
    )

    assert before["snapshot_id"] == after["snapshot_id"] == receipt["snapshot_id"]
    assert before["source_observed_at"] == "2026-07-27T10:00:00Z"
    assert after["expires_at"] == "2026-07-27T10:30:00Z"


def test_receipt_fails_closed_for_incomplete_stale_hash_and_symlink(tmp_path: Path) -> None:
    _receipt_path, receipt = _publish(
        tmp_path,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    with pytest.raises(PositionAdviceSourceError, match="incomplete"):
        validate_source_receipt(
            {**receipt, "completed": False},
            producer_root=tmp_path,
            now=NOW,
        )
    with pytest.raises(PositionAdviceSourceError, match="stale"):
        validate_source_receipt(
            receipt,
            producer_root=tmp_path,
            now=NOW + timedelta(minutes=30),
        )

    payload_path = tmp_path / str(receipt["payload_relpath"])
    payload_path.write_text("changed")
    with pytest.raises(PositionAdviceSourceError, match="payload hash mismatch"):
        validate_source_receipt(receipt, producer_root=tmp_path, now=NOW)

    target = tmp_path / "target.json"
    target.write_text("{}")
    payload_path.unlink()
    payload_path.symlink_to(target)
    with pytest.raises(PositionAdviceSourceError, match="symlink"):
        validate_source_receipt(receipt, producer_root=tmp_path, now=NOW)


def test_adoption_copies_exact_bytes_and_manifest_closes_dependencies(tmp_path: Path) -> None:
    producer = tmp_path / "producer"
    producer.mkdir()
    run_root = tmp_path / "output_runs" / "run-1"
    quote_path, quote = _publish(
        producer,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    quote_dep = _dependency(quote_path, quote, root=producer)
    candidate_path, _candidate = _publish(
        producer,
        kind="candidate_decisions",
        source_native_id="candidate-batch-1",
        producer_scope="account",
        producer_account_run_id="run-1",
        account="lx",
        dependencies=[quote_dep],
    )

    adopted_quote = adopt_source_snapshot(
        receipt_path=quote_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(seconds=10),
    )
    adopted_candidate = adopt_source_snapshot(
        receipt_path=candidate_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(seconds=10),
        expected_account="lx",
        expected_identity_hash=IDENTITY_HASH,
    )
    source_payload = run_root / adopted_quote["payload_relpath"]
    original_payload = producer / str(quote["payload_relpath"])
    assert source_payload.read_bytes() == original_payload.read_bytes()
    assert source_payload.stat().st_ino != original_payload.stat().st_ino

    manifest = build_source_manifest(
        account_run_id="run-1",
        portfolio_scope_id=scope_for("lx"),
        portfolio_account_identity_hash=IDENTITY_HASH,
        adopted_sources=[adopted_quote, adopted_candidate],
        required_for_actions={
            "quotes": ["short_put", "covered_call"],
            "candidate_decisions": ["short_put"],
        },
    )
    validated = validate_source_manifest(
        manifest,
        consumer_run_root=run_root,
        now=NOW + timedelta(minutes=1),
        expected_account_run_id="run-1",
        expected_scope_id=scope_for("lx"),
        expected_identity_hash=IDENTITY_HASH,
    )
    assert validated["completed"] is True
    assert [item["source_kind"] for item in validated["source_manifest"]] == [
        "candidate_decisions",
        "quotes",
    ]


def test_adoption_rejects_symlinked_and_hardlinked_source_receipt(
    tmp_path: Path,
) -> None:
    producer = tmp_path / "producer"
    producer.mkdir()
    receipt_path, _receipt = _publish(
        producer,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    moved_receipt = producer / "moved-receipt.json"
    receipt_path.replace(moved_receipt)
    receipt_path.symlink_to(moved_receipt)

    with pytest.raises(PositionAdviceSourceError, match="symlink"):
        adopt_source_snapshot(
            receipt_path=receipt_path,
            producer_root=producer,
            consumer_run_root=tmp_path / "run-symlink",
            consumer_account_run_id="run-1",
            now=NOW + timedelta(seconds=10),
        )

    receipt_path.unlink()
    os.link(moved_receipt, receipt_path)
    with pytest.raises(PositionAdviceSourceError, match="hardlink"):
        adopt_source_snapshot(
            receipt_path=receipt_path,
            producer_root=producer,
            consumer_run_root=tmp_path / "run-hardlink",
            consumer_account_run_id="run-1",
            now=NOW + timedelta(seconds=10),
        )


def test_manifest_rejects_missing_dependency_and_non_fx_skew(tmp_path: Path) -> None:
    producer = tmp_path / "producer"
    producer.mkdir()
    run_root = tmp_path / "run"
    quote_path, quote = _publish(
        producer,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    quote_dep = _dependency(quote_path, quote, root=producer)
    candidate_path, _candidate = _publish(
        producer,
        kind="candidate_decisions",
        source_native_id="candidate-batch-1",
        observed_at=NOW + timedelta(minutes=6),
        producer_scope="account",
        producer_account_run_id="run-1",
        account="lx",
        dependencies=[quote_dep],
    )
    adopted_candidate = adopt_source_snapshot(
        receipt_path=candidate_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(minutes=6, seconds=10),
        expected_account="lx",
        expected_identity_hash=IDENTITY_HASH,
    )
    with pytest.raises(PositionAdviceSourceError, match="dependency is not adopted"):
        build_source_manifest(
            account_run_id="run-1",
            portfolio_scope_id=scope_for("lx"),
            portfolio_account_identity_hash=IDENTITY_HASH,
            adopted_sources=[adopted_candidate],
        )

    adopted_quote = adopt_source_snapshot(
        receipt_path=quote_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(minutes=6, seconds=10),
    )
    portfolio_path, _portfolio = _publish(
        producer,
        kind="portfolio",
        source_native_id="portfolio-1",
        observed_at=NOW + timedelta(minutes=6),
        producer_scope="account",
        producer_account_run_id="run-1",
        account="lx",
    )
    adopted_portfolio = adopt_source_snapshot(
        receipt_path=portfolio_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(minutes=6, seconds=10),
        expected_account="lx",
        expected_identity_hash=IDENTITY_HASH,
    )
    manifest = build_source_manifest(
        account_run_id="run-1",
        portfolio_scope_id=scope_for("lx"),
        portfolio_account_identity_hash=IDENTITY_HASH,
        adopted_sources=[adopted_quote, adopted_candidate, adopted_portfolio],
    )
    with pytest.raises(PositionAdviceSourceError, match="skew"):
        validate_source_manifest(
            manifest,
            consumer_run_root=run_root,
            now=NOW + timedelta(minutes=6, seconds=10),
            expected_account_run_id="run-1",
            expected_scope_id=scope_for("lx"),
            expected_identity_hash=IDENTITY_HASH,
        )


def test_manifest_locks_are_bounded_and_follow_global_scope_order(tmp_path: Path) -> None:
    global_lock = (
        tmp_path
        / "output_shared"
        / "state"
        / "position_advice"
        / ".manifest.lock"
    )
    with manifest_file_lock(global_lock, mode="exclusive"):
        with pytest.raises(PositionAdviceLockError, match="timed out"):
            with manifest_file_lock(
                global_lock,
                mode="shared",
                timeout_seconds=0.01,
                poll_interval_seconds=0.005,
            ):
                pass

    with position_advice_manifest_locks(
        base=tmp_path,
        portfolio_scope_id=scope_for("lx"),
        global_mode="shared",
        scope_mode="exclusive",
    ):
        assert global_lock.exists()
        assert (
            global_lock.parent
            / scope_for("lx")
            / ".current.lock"
        ).exists()


def test_account_source_requires_complete_account_identity(tmp_path: Path) -> None:
    with pytest.raises(PositionAdviceSourceError, match="lacks broker, account"):
        _publish(
            tmp_path,
            kind="candidate_decisions",
            source_native_id="candidate-batch-1",
            producer_scope="account",
            producer_account_run_id="run-1",
        )


def test_source_publication_is_write_once(tmp_path: Path) -> None:
    receipt_path, receipt = _publish(
        tmp_path,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    original_receipt = receipt_path.read_bytes()
    original_payload = (
        tmp_path / str(receipt["payload_relpath"])
    ).read_bytes()

    with pytest.raises(PositionAdviceSourceError, match="destination conflicts"):
        _publish(
            tmp_path,
            kind="quotes",
            source_native_id="quote-batch-2",
        )

    assert receipt_path.read_bytes() == original_receipt
    assert (
        tmp_path / str(receipt["payload_relpath"])
    ).read_bytes() == original_payload


def test_manifest_fields_are_bound_to_adopted_receipt(tmp_path: Path) -> None:
    run_root, manifest = _build_valid_manifest(tmp_path)
    tampered = json.loads(json.dumps(manifest))
    tampered["source_manifest"][0]["source_observed_at"] = (
        NOW + timedelta(seconds=1)
    ).isoformat()
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "source_manifest_hash"
    }
    tampered["source_manifest_hash"] = canonical_sha256(unsigned)

    with pytest.raises(
        PositionAdviceSourceError,
        match="source_observed_at does not match adopted receipt",
    ):
        validate_source_manifest(
            tampered,
            consumer_run_root=run_root,
            now=NOW + timedelta(minutes=1),
            expected_account_run_id="run-1",
            expected_scope_id=scope_for("lx"),
            expected_identity_hash=IDENTITY_HASH,
        )


def test_manifest_rejects_cross_snapshot_payload_path(tmp_path: Path) -> None:
    run_root, manifest = _build_valid_manifest(tmp_path)
    tampered = json.loads(json.dumps(manifest))
    first, second = tampered["source_manifest"]
    first["payload_relpath"] = second["payload_relpath"]
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "source_manifest_hash"
    }
    tampered["source_manifest_hash"] = canonical_sha256(unsigned)

    with pytest.raises(PositionAdviceSourceError, match="adopted snapshot"):
        validate_source_manifest(
            tampered,
            consumer_run_root=run_root,
            now=NOW + timedelta(minutes=1),
            expected_account_run_id="run-1",
            expected_scope_id=scope_for("lx"),
            expected_identity_hash=IDENTITY_HASH,
        )


def test_action_requirements_must_reference_adopted_sources(tmp_path: Path) -> None:
    producer = tmp_path / "producer"
    producer.mkdir()
    run_root = tmp_path / "run"
    receipt_path, _receipt = _publish(
        producer,
        kind="quotes",
        source_native_id="quote-batch-1",
    )
    adopted = adopt_source_snapshot(
        receipt_path=receipt_path,
        producer_root=producer,
        consumer_run_root=run_root,
        consumer_account_run_id="run-1",
        now=NOW + timedelta(seconds=10),
    )

    with pytest.raises(PositionAdviceSourceError, match="was not adopted"):
        build_source_manifest(
            account_run_id="run-1",
            portfolio_scope_id=scope_for("lx"),
            portfolio_account_identity_hash=IDENTITY_HASH,
            adopted_sources=[adopted],
            required_for_actions={"fx": ["short_put"]},
        )
