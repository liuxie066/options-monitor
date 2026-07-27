from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import scope_for
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
    read_authority_resolution,
)
from src.application.position_advice_input_builder import (
    POSITION_ADVICE_OUTPUT_SCHEMA,
    PositionAdviceInputError,
    build_immutable_input,
    build_with_stable_inputs,
    publish_current_manifest,
    validate_current_manifest_hash,
    write_immutable_json,
)
from src.application.position_advice_current_repository import (
    PositionAdviceCurrentError,
    collect_protected_current_runs_under_global_lock,
)
from src.application.service_cleanup import service_cleanup
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    adopt_source_snapshot,
    build_source_manifest,
    publish_source_receipt,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
IDENTITY = "a" * 64
FINGERPRINT = "d" * 64


def _binding() -> dict[str, object]:
    return build_identity_binding_evidence(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authoring_config_hash="b" * 64,
        market_bindings=[
            {
                "market": "US",
                "generated_config_hash": "c" * 64,
                "source_receipt_hash": "e" * 64,
                "normalized_account": "lx",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": IDENTITY,
                "source_receipt_fresh": True,
            }
        ],
    )


def _trusted_snapshot(fingerprint: str = FINGERPRINT) -> dict[str, object]:
    return {
        "schema_version": "decision_state_snapshot.v2",
        "snapshot_status": "trusted",
        "actionable": True,
        "decision_state_fingerprint": fingerprint,
        "position_lots": [],
    }


def _prepare_run(tmp_path: Path) -> dict[str, object]:
    run_id = "run-1"
    run_root = tmp_path / "output_runs" / run_id
    account_root = run_root / "accounts" / "lx"
    producer = tmp_path / "producer"
    producer.mkdir()
    receipt = publish_source_receipt(
        producer_root=producer,
        receipt_relpath="quotes.receipt.json",
        payload_relpath="payloads/quotes.json",
        payload_bytes=b'{"NVDA": 100}',
        source_kind="quotes",
        producer_schema_version="required_data.v1",
        producer_run_id="prefetch-1",
        producer_scope="global",
        producer_account_run_id=None,
        broker=None,
        account=None,
        portfolio_account_identity_hash=None,
        included_markets=["US"],
        source_native_id="quote-batch-1",
        source_observed_at=NOW.isoformat(),
        completed_at=(NOW + timedelta(seconds=1)).isoformat(),
        producer_policy_hash="f" * 64,
    )
    adopted = adopt_source_snapshot(
        receipt_path=producer / "quotes.receipt.json",
        producer_root=producer,
        consumer_run_root=account_root,
        consumer_account_run_id=run_id,
        now=NOW + timedelta(seconds=2),
    )
    manifest = build_source_manifest(
        account_run_id=run_id,
        portfolio_scope_id=scope_for("lx"),
        portfolio_account_identity_hash=IDENTITY,
        adopted_sources=[adopted],
        required_for_actions={"quotes": ["short_put", "covered_call"]},
    )
    source_manifest_path = account_root / "state" / "position_advice_source_manifest.v2.json"
    write_immutable_json(
        source_manifest_path,
        manifest,
        hash_field="source_manifest_hash",
    )

    applied = apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2_shadow",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        identity_binding_evidence=_binding(),
    )
    resolution = read_authority_resolution(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
    )
    immutable_input = build_immutable_input(
        account_run_id=run_id,
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        portfolio_scope_id=scope_for("lx"),
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        capacity_pool_authority_id=None,
        authority_resolution=resolution,
        source_manifest_relpath="state/position_advice_source_manifest.v2.json",
        source_manifest=manifest,
        decision_state_snapshot=_trusted_snapshot(),
        candidate_inputs={"rows": []},
        economic_inputs={"risk_free_rate": "0.04"},
        built_at=NOW + timedelta(seconds=3),
    )
    input_path = account_root / "state" / "position_advice_input.v2.json"
    write_immutable_json(input_path, immutable_input, hash_field="input_hash")
    advice_payload = {
        "schema_version": POSITION_ADVICE_OUTPUT_SCHEMA,
        "account_run_id": run_id,
        "normalized_account": "lx",
        "portfolio_scope_id": scope_for("lx"),
        "portfolio_account_identity_hash": IDENTITY,
        "source_manifest_hash": manifest["source_manifest_hash"],
        "decision_state_fingerprint": FINGERPRINT,
        "authority_mode": resolution.mode,
        "authority_generation": resolution.generation,
        "authority_policy_hash": resolution.policy_hash,
        "input_hash": immutable_input["input_hash"],
        "actions": [],
    }
    advice = {**advice_payload, "artifact_hash": canonical_sha256(advice_payload)}
    advice_path = account_root / "position_advice.v2.json"
    write_immutable_json(advice_path, advice, hash_field="artifact_hash")
    return {
        "run_id": run_id,
        "run_root": run_root,
        "account_root": account_root,
        "source_manifest": manifest,
        "source_manifest_path": source_manifest_path,
        "input": immutable_input,
        "input_path": input_path,
        "advice": advice,
        "advice_path": advice_path,
        "policy": applied["policy"],
    }


def test_immutable_input_replay_is_idempotent_but_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    prepared = _prepare_run(tmp_path)
    input_path = prepared["input_path"]
    write_immutable_json(
        input_path,
        prepared["input"],
        hash_field="input_hash",
    )
    conflicting = dict(prepared["input"])
    conflicting["candidate_inputs"] = {"rows": [{"id": "changed"}]}
    conflicting["input_hash"] = canonical_sha256(
        {key: value for key, value in conflicting.items() if key != "input_hash"}
    )
    with pytest.raises(PositionAdviceInputError, match="conflicts"):
        write_immutable_json(input_path, conflicting, hash_field="input_hash")


def test_stable_build_retries_once_then_fails_closed() -> None:
    source = {
        "schema_version": "position_advice_source_manifest.v2",
        "source_manifest_hash": "",
    }
    source["source_manifest_hash"] = canonical_sha256(
        {key: value for key, value in source.items() if key != "source_manifest_hash"}
    )
    states = iter(
        [
            _trusted_snapshot("a" * 64),
            _trusted_snapshot("b" * 64),
            _trusted_snapshot("c" * 64),
            _trusted_snapshot("c" * 64),
        ]
    )
    result = build_with_stable_inputs(
        decision_snapshot_reader=lambda: next(states),
        source_manifest_reader=lambda: source,
        build=lambda state, _manifest: {
            "fingerprint": state["decision_state_fingerprint"]
        },
    )
    assert result["attempt"] == 2
    assert result["artifact"]["fingerprint"] == "c" * 64

    changing = iter(
        [
            _trusted_snapshot("a" * 64),
            _trusted_snapshot("b" * 64),
            _trusted_snapshot("c" * 64),
            _trusted_snapshot("d" * 64),
        ]
    )
    with pytest.raises(PositionAdviceInputError, match="input_changed_during_build"):
        build_with_stable_inputs(
            decision_snapshot_reader=lambda: next(changing),
            source_manifest_reader=lambda: source,
            build=lambda _state, _manifest: {},
        )


def test_current_switch_rechecks_source_state_and_authority_atomically(
    tmp_path: Path,
) -> None:
    prepared = _prepare_run(tmp_path)
    published = publish_current_manifest(
        base=tmp_path,
        run_id=str(prepared["run_id"]),
        account_run_root=Path(prepared["account_root"]),
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        source_manifest_relpath=Path(prepared["source_manifest_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        advice_artifact_relpath=Path(prepared["advice_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        input_artifact_relpath=Path(prepared["input_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        expected_decision_state_fingerprint=FINGERPRINT,
        decision_snapshot_reader=_trusted_snapshot,
        now=NOW + timedelta(minutes=1),
    )
    validate_current_manifest_hash(published["manifest"])
    current_before = Path(published["path"]).read_bytes()
    assert published["manifest"]["authority_mode"] == "v2_shadow"
    assert published["manifest"]["account_run_id"] == "run-1"

    with pytest.raises(PositionAdviceInputError, match="input_changed"):
        publish_current_manifest(
            base=tmp_path,
            run_id="run-1",
            account_run_root=Path(prepared["account_root"]),
            normalized_account="lx",
            broker="futu",
            included_markets=["US"],
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            source_manifest_relpath=Path(prepared["source_manifest_path"])
            .relative_to(prepared["run_root"])
            .as_posix(),
            advice_artifact_relpath=Path(prepared["advice_path"])
            .relative_to(prepared["run_root"])
            .as_posix(),
            input_artifact_relpath=Path(prepared["input_path"])
            .relative_to(prepared["run_root"])
            .as_posix(),
            expected_decision_state_fingerprint=FINGERPRINT,
            decision_snapshot_reader=lambda: _trusted_snapshot("e" * 64),
            now=NOW + timedelta(minutes=1),
        )
    assert Path(published["path"]).read_bytes() == current_before


def test_current_switch_rejects_stale_sources_and_identity_drift(tmp_path: Path) -> None:
    prepared = _prepare_run(tmp_path)
    common = {
        "base": tmp_path,
        "run_id": "run-1",
        "account_run_root": Path(prepared["account_root"]),
        "normalized_account": "lx",
        "broker": "futu",
        "included_markets": ["US"],
        "source_manifest_relpath": Path(prepared["source_manifest_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        "advice_artifact_relpath": Path(prepared["advice_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        "input_artifact_relpath": Path(prepared["input_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        "expected_decision_state_fingerprint": FINGERPRINT,
        "decision_snapshot_reader": _trusted_snapshot,
    }
    with pytest.raises(PositionAdviceInputError, match="authority"):
        publish_current_manifest(
            **common,
            normalized_portfolio_source="external_holdings",
            portfolio_account_identity_hash=IDENTITY,
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(PositionAdviceSourceError, match="stale"):
        publish_current_manifest(
            **common,
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            now=NOW + timedelta(minutes=30),
        )


def test_cleanup_protects_valid_current_and_fails_closed_for_malformed_current(
    tmp_path: Path,
) -> None:
    prepared = _prepare_run(tmp_path)
    published = publish_current_manifest(
        base=tmp_path,
        run_id="run-1",
        account_run_root=Path(prepared["account_root"]),
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        source_manifest_relpath=Path(prepared["source_manifest_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        advice_artifact_relpath=Path(prepared["advice_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        input_artifact_relpath=Path(prepared["input_path"])
        .relative_to(prepared["run_root"])
        .as_posix(),
        expected_decision_state_fingerprint=FINGERPRINT,
        decision_snapshot_reader=_trusted_snapshot,
        now=NOW + timedelta(minutes=1),
    )
    protected = collect_protected_current_runs_under_global_lock(base=tmp_path)
    assert protected == {Path(prepared["run_root"]).resolve()}

    newer = tmp_path / "output_runs" / "run-new"
    newer.mkdir(parents=True)
    os.utime(prepared["run_root"], (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_780_000_000, 1_780_000_000))
    releases = tmp_path / "apps" / "releases"
    active = releases / "1.0.2"
    old = releases / "1.0.1"
    for release in (active, old):
        release.mkdir(parents=True)
        (release / "VERSION").write_text(f"{release.name}\n")
    current_link = tmp_path / "apps" / "current"
    current_link.symlink_to(active, target_is_directory=True)

    cleanup = service_cleanup(
        repo_root=current_link,
        releases_root=releases,
        runtime_root=tmp_path,
        cleanup_output_runs=True,
        output_runs_keep_days=1,
        output_runs_keep_count=1,
        now=NOW,
    )
    protected_reasons = {
        Path(item["path"]).name: item["reason"]
        for item in cleanup["output_runs_cleanup"]["protected_runs"]
    }
    assert protected_reasons["run-1"] == "position_advice_current_manifest"

    Path(prepared["advice_path"]).write_text("{}")
    blocked = service_cleanup(
        repo_root=current_link,
        releases_root=releases,
        runtime_root=tmp_path,
        cleanup_output_runs=True,
        output_runs_keep_days=1,
        output_runs_keep_count=1,
        confirm=True,
        now=NOW,
    )
    assert blocked["ok"] is False
    assert blocked["status"] == "position_advice_manifest_invalid"
    assert Path(prepared["run_root"]).exists()
    assert old.exists()
    with pytest.raises(PositionAdviceCurrentError, match="hash mismatch"):
        collect_protected_current_runs_under_global_lock(base=tmp_path)
