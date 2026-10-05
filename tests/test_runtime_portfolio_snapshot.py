from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from scripts import benchmark_runtime_portfolio_snapshot as benchmark_owner
import src.application.ledger.api as ledger_api
from scripts.benchmark_runtime_portfolio_snapshot import (
    CURRENT_SCALE,
    CURRENT_STATE_10X,
    EXPECTED_FIXTURE_PAYLOAD_SHA256,
    FIXTURE_CONTRACT_SHA256,
    FIXTURE_DESCRIPTOR_PATH,
    benchmark_exit_code,
    fixture_contract_sha256,
    fixture_descriptor,
    generate_fixture,
    owner_valid_schema_probe,
    run_profile,
)
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE,
    publish_candidate_snapshot_manifest,
)
from src.application.runtime_portfolio_snapshot import (
    LEDGER_SHADOW_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    MAX_CANONICAL_BYTES,
    SHADOW_SCHEMA_VERSION,
    RuntimePortfolioSnapshotError,
    _build_runtime_portfolio_snapshot,
    assemble_runtime_portfolio_snapshot,
    build_runtime_portfolio_section,
    build_runtime_portfolio_snapshot,
    build_source_status_section,
    canonical_json_bytes,
    load_runtime_portfolio_snapshot,
    publish_runtime_portfolio_snapshot,
    validate_replay_bundle,
    verify_runtime_portfolio_snapshot,
)
from src.application.source_receipts import sha256_bytes
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
)
from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    write_account_run_state_bytes_once_safely,
)


_CONTRACT_HASH = "f180e7bbcdd2f9bdaf6edfc540099b5c54156f3c6971ce83ef55c6fea51099c8"
_INPUT_HASHES = {
    "current_scale": "236afbeba01480b7c978a35bd94c9aeae0888efcdc52fbf025d8a9b8338c6352",
    "current_state_10x": "7839c551f5e185e16ab4e0f7b6543022687c8f9f82fc1686ae4d769b4cb021d8",
}
def _current_scale_kwargs() -> dict:
    return generate_fixture("current_scale")["builder_kwargs"]


def _unavailable_completeness() -> dict:
    return {
        "status": "unavailable",
        "reason_codes": ["ledger_shadow:unavailable"],
    }


def _profile(**overrides) -> dict:  # type: ignore[no-untyped-def]
    return run_profile("current_scale", warmups=0, repetitions=1, **overrides)


def _verified(snapshot: dict, *, expected_run_id: str, expected_account: str, reference_payloads: dict) -> dict:
    return verify_runtime_portfolio_snapshot(
        snapshot,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        reference_payloads=reference_payloads,
    )


def _published(base, snapshot: dict, reference_payloads: dict):  # type: ignore[no-untyped-def]
    return publish_runtime_portfolio_snapshot(
        base=base,
        snapshot=snapshot,
        reference_payloads=reference_payloads,
    )


def _owner_assembly_kwargs() -> dict:
    fixture = generate_fixture("current_scale")["builder_kwargs"]
    bindings = {row["role"]: row for row in fixture["replay_bindings"]}
    references = fixture["reference_payloads"]
    sections = fixture["sections"]
    ledger = sections["ledger_projection"]["facts"]
    current_read = {
        "schema_version": ledger["read_schema_version"],
        "position_lots": ledger["position_lots"],
        **ledger["current_decision"],
    }
    option_manifest = json.loads(references[bindings["prepared_option_positions_context"]["relpath"]])
    option_payload = {
        **sections["cash_occupation"]["facts"],
        "current_decision_shadow": {"status": "matched"},
        "prepared_authority": {
            key: option_manifest[key]
            for key in (
                "run_id",
                "account",
                "account_config_sha256",
                "ledger_generation_sha256",
                "fx_observation_sha256",
                "source_observed_at",
                "application_received_at_utc",
            )
        },
        "current_decision_read": current_read,
        "decision_snapshot_status": "trusted",
        "decision_snapshot_actionable": True,
        "decision_state_fingerprint": ledger["decision_state_fingerprint"],
    }
    option_payload_bytes = canonical_json_bytes(option_payload)
    option_manifest["payload_sha256"] = sha256_bytes(option_payload_bytes)
    option_manifest_bytes = canonical_json_bytes(option_manifest)

    portfolio_manifest = json.loads(references[bindings["prepared_portfolio_context"]["relpath"]])
    portfolio_payload = {
        **sections["broker_cash"]["facts"],
        **sections["broker_positions"]["facts"],
        "source_observed_at": portfolio_manifest["source_as_of_utc"],
        "position_snapshot_input": {"completeness": "complete", "quality": {"status": "ready"}, "errors": []},
    }
    portfolio_payload_bytes = canonical_json_bytes(portfolio_payload)
    portfolio_manifest["payload_sha256"] = sha256_bytes(portfolio_payload_bytes)
    portfolio_manifest["portfolio_context_relpath"] = f"portfolio_context.{portfolio_manifest['payload_sha256']}.json"
    portfolio_manifest_bytes = canonical_json_bytes(portfolio_manifest)

    chosen = fixture["chosen_results"]
    candidate_binding = bindings["candidate_snapshot_manifest"]
    return {
        "run_id": fixture["run_id"],
        "account": fixture["account"],
        "account_config_bytes": references[bindings["account_config"]["relpath"]],
        "prepared_option_manifest_bytes": option_manifest_bytes,
        "prepared_option_payload_bytes": option_payload_bytes,
        "prepared_portfolio_manifest_bytes": portfolio_manifest_bytes,
        "prepared_portfolio_payload_bytes": portfolio_payload_bytes,
        "required_data_manifest_bytes": references[bindings["required_data_snapshot"]["relpath"]],
        "candidate_manifest_bytes": references[candidate_binding["relpath"]],
        "candidate_status_index_bytes": references[chosen["status_index"]["relpath"]],
        "candidate_owner_snapshot_bytes": {
            row["candidate_owner"]: references[row["relpath"]] for row in chosen["owner_snapshots"]
        },
    }


def _wheel_v3_assembly_kwargs(base: Path) -> dict:
    assembly = _owner_assembly_kwargs()
    run_id = assembly["run_id"]
    account = assembly["account"]
    config_hash = sha256_bytes(assembly["account_config_bytes"])
    required_hash = sha256_bytes(assembly["required_data_manifest_bytes"])
    policy_hash = "b" * 64
    account_dir = base / "output_runs" / run_id / "accounts" / account
    (account_dir / "state").mkdir(parents=True)
    expected = []
    for direction in ("call", "put"):
        publish_strategy_scan_status(
            report_dir=account_dir,
            run_id=run_id,
            account=account,
            market="US",
            symbol="NVDA",
            strategy_family="wheel",
            direction=direction,
            status="completed",
            candidate_count=0,
        )
        expected.append({
            "market": "US",
            "symbol": "NVDA",
            "strategy_family": "wheel",
            "direction": direction,
            "strategy_mode": "wheel",
            "candidate_owner": "wheel",
            "account_config_sha256": config_hash,
        })
    publish_strategy_scan_status_index(
        report_dir=account_dir,
        run_id=run_id,
        account=account,
        account_config_sha256=config_hash,
        expected=expected,
        run_mode={"scan_mode": "standard", "executable": True},
    )
    seal_wheel_candidate_snapshot(
        base=base,
        run_id=run_id,
        account=account,
        market="us",
        account_config_sha256=config_hash,
        strategy_policy_sha256=policy_hash,
        dependencies=[
            {"kind": kind, "relpath": None, "sha256": digest}
            for kind, digest in (
                ("required_data", required_hash),
                ("portfolio", "2" * 64),
                ("ledger", "3" * 64),
                ("fx", "4" * 64),
                ("earnings_rv", "5" * 64),
            )
        ],
        scope_results=[
            {"symbol": "NVDA", "direction": direction, "status": "completed", "candidate_count": 0}
            for direction in ("call", "put")
        ],
        batches=[],
        run_mode={"scan_mode": "standard", "executable": True},
    )
    manifest = publish_candidate_snapshot_manifest(
        base=base,
        run_id=run_id,
        account=account,
        strategy_policy_sha256=policy_hash,
    )
    assembly["candidate_manifest_bytes"] = (
        account_dir / "state" / CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE
    ).read_bytes()
    assembly["candidate_status_index_bytes"] = (
        account_dir / manifest["status_index"]["relpath"]
    ).read_bytes()
    assembly["candidate_owner_snapshot_bytes"] = {
        row["candidate_owner"]: (account_dir / row["relpath"]).read_bytes()
        for row in manifest["owner_snapshots"]
    }
    return assembly


def _assembly_with_current_read(current_read: dict) -> dict:
    assembly = _owner_assembly_kwargs()
    option_payload = json.loads(assembly["prepared_option_payload_bytes"])
    option_payload["current_decision_read"] = current_read
    option_payload["decision_snapshot_status"] = "source_untrusted"
    option_payload["decision_snapshot_actionable"] = False
    option_payload["current_decision_shadow"] = {"status": "unavailable"}
    raw = canonical_json_bytes(option_payload)
    option_manifest = json.loads(assembly["prepared_option_manifest_bytes"])
    option_manifest["payload_sha256"] = sha256_bytes(raw)
    assembly["prepared_option_payload_bytes"] = raw
    assembly["prepared_option_manifest_bytes"] = canonical_json_bytes(option_manifest)
    return assembly


def test_frozen_fixture_contract_hash_is_independent_and_exact() -> None:
    raw = FIXTURE_DESCRIPTOR_PATH.read_bytes()
    descriptor = json.loads(raw)
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    assert raw == encoded + b"\n"
    assert descriptor == fixture_descriptor()
    assert hashlib.sha256(encoded).hexdigest() == _CONTRACT_HASH
    assert FIXTURE_CONTRACT_SHA256 == _CONTRACT_HASH
    assert fixture_contract_sha256() == _CONTRACT_HASH
    assert CURRENT_SCALE == descriptor["current_scale"]
    assert CURRENT_STATE_10X == descriptor["current_state_10x"]


@pytest.mark.parametrize(
    ("fault", "violation"),
    [
        ("missing", "fixture_descriptor_missing"),
        ("drift", "fixture_descriptor_drift"),
        ("hash", "fixture_contract_sha256_mismatch"),
    ],
)
def test_fixture_descriptor_faults_fail_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    violation: str,
) -> None:
    fault_path = tmp_path / FIXTURE_DESCRIPTOR_PATH.name
    if fault != "missing":
        descriptor = json.loads(FIXTURE_DESCRIPTOR_PATH.read_bytes())
        if fault == "drift":
            descriptor["seed"] += 1
        fault_path.write_bytes(canonical_json_bytes(descriptor) + b"\n")
    monkeypatch.setattr(benchmark_owner, "FIXTURE_DESCRIPTOR_PATH", fault_path)
    if fault == "hash":
        monkeypatch.setattr(benchmark_owner, "FIXTURE_CONTRACT_SHA256", "0" * 64)

    receipt = run_profile("current_scale", warmups=0, repetitions=1)

    assert benchmark_exit_code(receipt) == 1
    assert violation in receipt["violations"]
    assert receipt["production_artifact_read_calls"] == 0


def test_deterministic_profiles_pin_input_hash_shape_and_size() -> None:
    fixtures = {profile: generate_fixture(profile) for profile in ("current_scale", "current_state_10x")}
    snapshots = {
        profile: build_runtime_portfolio_snapshot(**fixture["builder_kwargs"]) for profile, fixture in fixtures.items()
    }

    assert {profile: fixture["payload_sha256"] for profile, fixture in fixtures.items()} == _INPUT_HASHES
    assert EXPECTED_FIXTURE_PAYLOAD_SHA256 == _INPUT_HASHES
    assert owner_valid_schema_probe()
    assert all(fixture["fixture_shape_matches"] for fixture in fixtures.values())
    assert all(fixture["fixture_owner_validators_passed"] for fixture in fixtures.values())
    for fixture in fixtures.values():
        current = fixture["builder_kwargs"]["sections"]["ledger_projection"]["facts"]
        assert current["current_decision"]["lifecycle_by_lot"] == {}
        assert current["current_decision"]["lifecycle_by_case"] == {}
        assert current["current_decision"]["lifecycle_quality"]["operational_cases"] == []
    assert all(snapshot["status"] == "trusted" for snapshot in snapshots.values())
    chosen = snapshots["current_scale"]["chosen_results"]
    assert set(chosen["expected_scopes"][0]) == {
        "market",
        "symbol",
        "strategy_family",
        "strategy_mode",
        "candidate_owner",
    }
    candidate_binding = next(
        row for row in snapshots["current_scale"]["replay_bindings"] if row["role"] == "candidate_snapshot_manifest"
    )
    assert chosen["status_index"]["relpath"] != candidate_binding["relpath"]
    assert all(len(canonical_json_bytes(snapshot)) < MAX_CANONICAL_BYTES for snapshot in snapshots.values())
    ratio = fixtures["current_state_10x"]["scalable_bytes"] / fixtures["current_scale"]["scalable_bytes"]
    assert 9.75 <= ratio <= 10.25
    for profile, snapshot in snapshots.items():
        fixture = fixtures[profile]
        assert snapshot == build_runtime_portfolio_snapshot(**fixture["builder_kwargs"])
        assert snapshot == _verified(
            snapshot, expected_run_id="fixture-runtime-0001", expected_account="acct_fixture",
            reference_payloads=fixture["builder_kwargs"]["reference_payloads"],
        )


def test_assembler_consumes_one_exact_owner_bundle() -> None:
    assembly = _owner_assembly_kwargs()

    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)

    assert snapshot["status"] == "trusted"
    assert snapshot["ledger_shadow"] == {
        "schema_version": LEDGER_SHADOW_SCHEMA_VERSION,
        "status": "matched",
    }
    assert snapshot == _verified(
        snapshot, expected_run_id=assembly["run_id"], expected_account=assembly["account"],
        reference_payloads=references,
    )


def test_assembler_publishes_and_verifies_directional_wheel_bundle(tmp_path: Path) -> None:
    assembly = _wheel_v3_assembly_kwargs(tmp_path)

    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)

    candidate_binding = next(
        row for row in snapshot["replay_bindings"]
        if row["role"] == "candidate_snapshot_manifest"
    )
    assert candidate_binding["schema_version"] == "candidate_snapshot_manifest.v4"
    assert candidate_binding["relpath"] == "state/candidate_snapshot_manifest.v4.json"
    assert {row["direction"] for row in snapshot["chosen_results"]["expected_scopes"]} == {
        "call", "put",
    }
    assert snapshot == _verified(
        snapshot, expected_run_id=assembly["run_id"], expected_account=assembly["account"],
        reference_payloads=references,
    )
    path = _published(tmp_path, snapshot, references)
    assert path.is_file()
    assert load_runtime_portfolio_snapshot(
        base=tmp_path, run_id=assembly["run_id"], account=assembly["account"],
        reference_payloads=references,
    ) == snapshot


def test_assembler_rejects_historical_experience_index_for_new_snapshot() -> None:
    assembly = _owner_assembly_kwargs()
    status = json.loads(assembly["candidate_status_index_bytes"])
    status["schema_version"] = "strategy_scan_status_index.v3"
    status_content = {key: value for key, value in status.items() if key != "content_sha256"}
    status["content_sha256"] = sha256_bytes(canonical_json_bytes(status_content))
    assembly["candidate_status_index_bytes"] = canonical_json_bytes(status)
    manifest = json.loads(assembly["candidate_manifest_bytes"])
    manifest["status_index"] = {
        "schema_version": status["schema_version"],
        "relpath": "strategy_scan_status_index.v3.json",
        "sha256": sha256_bytes(assembly["candidate_status_index_bytes"]),
        "content_sha256": status["content_sha256"],
    }
    manifest["content_sha256"] = canonical_sha256(
        {key: value for key, value in manifest.items() if key != "content_sha256"}
    )
    assembly["candidate_manifest_bytes"] = canonical_json_bytes(manifest)

    with pytest.raises(RuntimePortfolioSnapshotError, match="candidate snapshot manifest"):
        assemble_runtime_portfolio_snapshot(**assembly)


def test_assembler_rejects_directional_wheel_reference_corruption(tmp_path: Path) -> None:
    assembly = _wheel_v3_assembly_kwargs(tmp_path)
    owner = json.loads(assembly["candidate_owner_snapshot_bytes"]["wheel"])
    owner["scope_results"][1]["direction"] = "call"
    assembly["candidate_owner_snapshot_bytes"]["wheel"] = canonical_json_bytes(owner)

    with pytest.raises(RuntimePortfolioSnapshotError):
        assemble_runtime_portfolio_snapshot(**assembly)


def test_directional_wheel_rejects_candidate_receipt_schema_drift(tmp_path: Path) -> None:
    assembly = _wheel_v3_assembly_kwargs(tmp_path)
    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)
    sections = deepcopy(snapshot["sections"])
    receipts = sections["source_status"]["facts"]
    receipts["candidate_results"]["owner_schema_version"] = "candidate_snapshot_manifest.v1"
    sections["source_status"] = build_source_status_section(
        account=assembly["account"], owner_receipts=receipts,
    )

    with pytest.raises(RuntimePortfolioSnapshotError, match="candidate_results"):
        build_runtime_portfolio_snapshot(
            run_id=assembly["run_id"],
            account=assembly["account"],
            sections=sections,
            replay_bindings=snapshot["replay_bindings"],
            chosen_results=snapshot["chosen_results"],
            reference_payloads=references,
            ledger_shadow=snapshot["ledger_shadow"],
        )


@pytest.mark.parametrize("shape", ["fallback", "sparse", "full"])
def test_assembler_seals_unavailable_current_read_without_inventing_facts(tmp_path, shape: str) -> None:
    full = json.loads(_owner_assembly_kwargs()["prepared_option_payload_bytes"])["current_decision_read"]
    reason = "current_projection_unavailable"
    if shape == "fallback":
        current_read = {"status": "data_unavailable", "reason": reason}
    elif shape == "sparse":
        current_read = {
            "schema_version": full["schema_version"],
            "status": "data_unavailable",
            "account": full["account"],
            "reason": reason,
            "payload": None,
            "position_lots": [],
        }
    else:
        current_read = {**full, "status": "data_unavailable", "reason": reason, "payload": None}
    assembly = _assembly_with_current_read(current_read)

    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)

    assert snapshot["status"] == "data_unavailable"
    ledger = snapshot["sections"]["ledger_projection"]
    assert ledger["facts"]["current_decision"]["reason"] == reason
    assert reason in ledger["completeness"]["reason_codes"]
    assert ledger["completeness"]["status"] == "unavailable"
    assert snapshot["sections"]["cash_occupation"]["completeness"]["status"] == "unavailable"
    if shape == "fallback":
        assert ledger["facts"]["read_schema_version"] is None
        assert ledger["facts"]["position_lots"] is None
        assert ledger["facts"]["current_decision"]["lot_count"] is None
    elif shape == "sparse":
        assert ledger["facts"]["position_lots"] == []
        assert ledger["facts"]["current_decision"]["lot_count"] is None
    assert _verified(snapshot, expected_run_id=assembly["run_id"], expected_account=assembly["account"], reference_payloads=references) == snapshot
    path = _published(tmp_path, snapshot, references)
    assert path.read_bytes() == canonical_json_bytes(snapshot)


@pytest.mark.parametrize("mutation", ["trusted_fallback", "foreign_sparse", "payload_sparse", "lots_sparse"])
def test_assembler_rejects_invalid_sparse_current_read(mutation: str) -> None:
    full = json.loads(_owner_assembly_kwargs()["prepared_option_payload_bytes"])["current_decision_read"]
    read = {
        "schema_version": full["schema_version"], "status": "data_unavailable",
        "account": full["account"], "reason": "read_unavailable", "payload": None,
        "position_lots": [],
    }
    if mutation == "trusted_fallback":
        read = {"status": "trusted", "reason": "read_unavailable"}
    elif mutation == "foreign_sparse":
        read["account"] = "other_account"
    elif mutation == "payload_sparse":
        read["payload"] = {}
    else:
        read["position_lots"] = [{"id": "unexpected"}]

    with pytest.raises(RuntimePortfolioSnapshotError):
        assemble_runtime_portfolio_snapshot(**_assembly_with_current_read(read))


def test_runtime_snapshot_preserves_prepared_owner_binding() -> None:
    assembly = _owner_assembly_kwargs()
    manifest = json.loads(assembly["prepared_option_manifest_bytes"])
    manifest["schema_version"] = "prepared_option_positions_context"
    assembly["prepared_option_manifest_bytes"] = canonical_json_bytes(manifest)

    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)
    binding = next(
        row
        for row in snapshot["replay_bindings"]
        if row["role"] == "prepared_option_positions_context"
    )

    assert binding["schema_version"] == "prepared_option_positions_context"
    assert binding["relpath"] == (
        "state/prepared_option_positions_context.json"
    )
    assert snapshot == _verified(
        snapshot, expected_run_id=assembly["run_id"], expected_account=assembly["account"],
        reference_payloads=references,
    )


def test_canonical_bytes_and_immutable_publication_are_stable(tmp_path) -> None:
    kwargs = _current_scale_kwargs()
    snapshot = build_runtime_portfolio_snapshot(**kwargs)
    path = _published(tmp_path, snapshot, kwargs["reference_payloads"])
    adopted = _published(tmp_path, snapshot, kwargs["reference_payloads"])

    assert path == adopted
    assert path.read_bytes() == canonical_json_bytes(snapshot)
    assert (
        load_runtime_portfolio_snapshot(
            base=tmp_path,
            run_id="fixture-runtime-0001",
            account="acct_fixture",
            reference_payloads=kwargs["reference_payloads"],
        )
        == snapshot
    )
    assert canonical_json_bytes({"值": 1, "a": 1.25}) == canonical_json_bytes({"a": 1.25, "值": 1})

    changed_sections = deepcopy(kwargs["sections"])
    shadow = {**kwargs["ledger_shadow"], "status": "unavailable"}
    unavailable = _unavailable_completeness()
    changed_sections["ledger_projection"]["completeness"] = unavailable
    owners = deepcopy(changed_sections["source_status"]["facts"])
    owners["ledger_projection"]["completeness"] = unavailable
    changed_sections["source_status"] = build_source_status_section(account="acct_fixture", owner_receipts=owners)
    conflicting = build_runtime_portfolio_snapshot(
        **{
            **kwargs,
            "sections": changed_sections,
            "ledger_shadow": shadow,
        }
    )
    assert conflicting["status"] == "data_unavailable"
    with pytest.raises(AccountRunConfigError) as conflict:
        _published(tmp_path, conflicting, kwargs["reference_payloads"])
    assert conflict.value.code == "ACCOUNT_RUN_STATE_CONFLICT"


@pytest.mark.parametrize(
    "case",
    [
        "seal",
        "expected_run",
        "expected_account",
        "reference",
        "latest",
        "receipt_time",
        "extra",
    ],
)
def test_verifier_rejects_tampered_trust_boundaries(case: str) -> None:
    kwargs = _current_scale_kwargs()
    snapshot = build_runtime_portfolio_snapshot(**kwargs)
    candidate = deepcopy(snapshot)
    references = dict(kwargs["reference_payloads"])
    expected_run = "fixture-runtime-0001"
    expected_account = "acct_fixture"
    if case == "seal":
        candidate["seal"]["content_sha256"] = "0" * 64
    elif case == "expected_run":
        expected_run = "different-run"
    elif case == "expected_account":
        expected_account = "different_account"
    elif case == "reference":
        path = next(iter(references))
        references[path] = b"tampered"
    elif case == "latest":
        candidate["replay_bindings"][0]["relpath"] = "latest/config.json"
    elif case == "receipt_time":
        candidate["sections"]["broker_cash"]["application_received_at_utc"] = "2026-08-16T00:00:04+00:00"
    else:
        candidate["unexpected"] = True

    with pytest.raises(RuntimePortfolioSnapshotError):
        _verified(
            candidate, expected_run_id=expected_run, expected_account=expected_account,
            reference_payloads=references,
        )


@pytest.mark.parametrize("shadow_status", ["mismatched", "unavailable"])
def test_shadow_failure_is_bounded_metadata_and_fails_closed(shadow_status: str) -> None:
    kwargs = _current_scale_kwargs()
    shadow = {**kwargs["ledger_shadow"], "status": shadow_status}
    sections = deepcopy(kwargs["sections"])
    unavailable = {"status": "unavailable", "reason_codes": [f"ledger_shadow:{shadow_status}"]}
    sections["ledger_projection"]["completeness"] = unavailable
    owners = deepcopy(sections["source_status"]["facts"])
    owners["ledger_projection"]["completeness"] = unavailable
    sections["source_status"] = build_source_status_section(account="acct_fixture", owner_receipts=owners)
    snapshot = build_runtime_portfolio_snapshot(**{**kwargs, "sections": sections, "ledger_shadow": shadow})
    assert snapshot["ledger_shadow"] == shadow
    assert "legacy_comparison" not in snapshot
    assert snapshot["status"] == "data_unavailable"
    assert snapshot["reason_codes"] == [
        f"ledger_shadow:{shadow_status}",
        "section_completeness:ledger_projection:unavailable",
        "section_completeness:source_status:unavailable",
    ]
    tampered = deepcopy(snapshot)
    tampered["ledger_shadow"]["status"] = "matched"
    with pytest.raises(RuntimePortfolioSnapshotError):
        _verified(tampered, expected_run_id=kwargs["run_id"], expected_account=kwargs["account"], reference_payloads=kwargs["reference_payloads"])


def test_historical_v1_loads_but_cannot_be_published_and_bad_v2_blocks_fallback(tmp_path) -> None:
    kwargs = _current_scale_kwargs()
    rows = []
    for name in ("broker_cash", "broker_positions", "cash_occupation", "chosen_results", "ledger_projection"):
        value = kwargs["chosen_results"] if name == "chosen_results" else kwargs["sections"][name]["facts"]
        digest = sha256_bytes(canonical_json_bytes(value))
        rows.append({"section": name, "legacy_sha256": digest, "compact_sha256": digest, "mismatch_count": 0})
    historical = _build_runtime_portfolio_snapshot(
        run_id=kwargs["run_id"], account=kwargs["account"],
        sections=kwargs["sections"], replay_bindings=kwargs["replay_bindings"],
        chosen_results=kwargs["chosen_results"],
        receipt={"schema_version": SHADOW_SCHEMA_VERSION, "status": "matched", "mismatch_count": 0,
                 "mismatch_samples": [], "sections": rows},
        receipt_key="legacy_comparison", schema_version=LEGACY_SCHEMA_VERSION,
        reference_payloads=kwargs["reference_payloads"],
    )
    write_account_run_state_bytes_once_safely(
        base=tmp_path,
        run_id="fixture-runtime-0001",
        account="acct_fixture",
        name="runtime_portfolio_snapshot.v1.json",
        payload=canonical_json_bytes(historical),
    )
    assert load_runtime_portfolio_snapshot(
        base=tmp_path, run_id=kwargs["run_id"], account=kwargs["account"],
        reference_payloads=kwargs["reference_payloads"],
    ) == historical
    with pytest.raises(RuntimePortfolioSnapshotError, match="read-only"):
        _published(tmp_path, historical, kwargs["reference_payloads"])

    write_account_run_state_bytes_once_safely(
        base=tmp_path, run_id=kwargs["run_id"], account=kwargs["account"],
        name="runtime_portfolio_snapshot.v2.json", payload=b"{}",
    )

    with pytest.raises(RuntimePortfolioSnapshotError):
        load_runtime_portfolio_snapshot(
            base=tmp_path,
            run_id="fixture-runtime-0001",
            account="acct_fixture",
            reference_payloads=kwargs["reference_payloads"],
        )


def test_canonical_encoder_rejects_non_finite_numbers() -> None:
    with pytest.raises(RuntimePortfolioSnapshotError):
        canonical_json_bytes({"bad": float("nan")})


def test_policy_bearing_source_freshness_cannot_drift_from_owner() -> None:
    kwargs = _current_scale_kwargs()
    sections = deepcopy(kwargs["sections"])
    owners = deepcopy(sections["source_status"]["facts"])
    owners["required_data"]["freshness"] = {
        "authority": "required_data_snapshot",
        "status": "unavailable_stale",
        "reason_codes": ["required_data_stale"],
    }
    sections["source_status"] = build_source_status_section(account="acct_fixture", owner_receipts=owners)

    with pytest.raises(RuntimePortfolioSnapshotError):
        build_runtime_portfolio_snapshot(**{**kwargs, "sections": sections})


def test_current_decision_cannot_self_promote_completeness() -> None:
    kwargs = _current_scale_kwargs()
    sections = deepcopy(kwargs["sections"])
    ledger = sections["ledger_projection"]
    facts = deepcopy(ledger["facts"])
    facts["current_decision"].update({"status": "data_unavailable", "reason": "projection_dirty", "payload": None})
    sections["ledger_projection"] = build_runtime_portfolio_section(
        "ledger_projection",
        account="acct_fixture",
        source_observed_at_utc=ledger["source_observed_at_utc"],
        application_received_at_utc=ledger["application_received_at_utc"],
        facts=facts,
        completeness_status="complete",
    )

    with pytest.raises(RuntimePortfolioSnapshotError):
        build_runtime_portfolio_snapshot(**{**kwargs, "sections": sections})


@pytest.mark.parametrize("case", ["foreign_account", "duplicate_json", "chosen_split"])
def test_replay_bundle_drift_fails_closed(case: str) -> None:
    kwargs = _current_scale_kwargs()
    bindings = deepcopy(kwargs["replay_bindings"])
    chosen = deepcopy(kwargs["chosen_results"])
    payloads = dict(kwargs["reference_payloads"])
    if case in {"foreign_account", "duplicate_json"}:
        binding = next(row for row in bindings if row["role"] == "account_config")
        if case == "foreign_account":
            payload = json.loads(payloads[binding["relpath"]])
            payload["portfolio"]["account"] = "other_account"
            raw = canonical_json_bytes(payload)
        else:
            raw = b'{"portfolio":{"account":"acct_fixture"},"portfolio":{}}'
        binding["sha256"] = sha256_bytes(raw)
        payloads[binding["relpath"]] = raw
    else:
        chosen["owner_snapshots"][0]["opening_status"] = "data_unavailable"

    with pytest.raises(RuntimePortfolioSnapshotError):
        validate_replay_bundle(
            expected_run_id="fixture-runtime-0001",
            expected_account="acct_fixture",
            replay_bindings=bindings,
            chosen_results=chosen,
            reference_payloads=payloads,
        )


def test_benchmark_gate_measures_valid_path_and_faults() -> None:
    receipt = _profile()
    assert benchmark_exit_code(receipt) == 0
    assert receipt["violations"] == []
    assert receipt["forbidden_history_structural_reference_count"] == 0
    assert receipt["forbidden_history_executable_spy_calls"] == 0
    assert receipt["forbidden_history_reader_calls"] == 0
    assert receipt["production_artifact_read_calls"] == 0
    assert receipt["ledger_shadow_matches"]

    def owner_drift(builder_kwargs: dict) -> None:
        chosen = builder_kwargs["chosen_results"]
        relpath = chosen["owner_snapshots"][0]["relpath"]
        builder_kwargs["reference_payloads"][relpath] = b"{}"

    owner_fault = _profile(fixture_mutator=owner_drift)
    history_fault = _profile(executable_probe=lambda: ledger_api.preview_current_decision_projection_oracle())
    assert benchmark_exit_code(owner_fault) == 1
    assert "fixture_owner_validators_passed" in owner_fault["violations"]
    assert benchmark_exit_code(history_fault) == 1
    assert history_fault["forbidden_history_executable_spy_calls"] == 1
    assert history_fault["production_artifact_read_calls"] == 0
    assert "forbidden_history_executable_spy_calls" in history_fault["violations"]


@pytest.mark.parametrize("positions_complete", [True, False])
def test_v2_cash_preserves_verdict_and_binds_real_position_completeness(positions_complete):
    from cash_evidence_helpers import cash_portfolio
    assembly = _owner_assembly_kwargs()
    original = json.loads(assembly["prepared_portfolio_payload_bytes"])
    original["position_snapshot_input"]["completeness"] = "complete" if positions_complete else "partial"
    for key in ("capacity_authority", "filters", "source_account_identifiers", "portfolio_source_name"):
        original.pop(key, None)
    payload = cash_portfolio(original)
    raw = canonical_json_bytes(payload)
    manifest = json.loads(assembly["prepared_portfolio_manifest_bytes"])
    manifest["payload_sha256"] = sha256_bytes(raw)
    manifest["portfolio_context_relpath"] = f"portfolio_context.{manifest['payload_sha256']}.json"
    assembly.update(prepared_portfolio_payload_bytes=raw, prepared_portfolio_manifest_bytes=canonical_json_bytes(manifest))
    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)
    cash = snapshot["sections"]["broker_cash"]
    assert cash["schema_version"] == "runtime_portfolio_snapshot.broker_cash.v2"
    assert cash["facts"]["cash_snapshot"] == payload["cash_snapshot"]
    assert snapshot["sections"]["broker_positions"]["completeness"]["status"] == ("complete" if positions_complete else "unavailable")
    assert _verified(snapshot, expected_run_id=assembly["run_id"], expected_account=assembly["account"], reference_payloads=references) == snapshot
    # Even a recomputed section hash cannot detach the projection from its original source.
    cash["facts"]["cash_balance_reliable"] = not cash["facts"]["cash_balance_reliable"]
    cash["content_sha256"] = sha256_bytes(canonical_json_bytes(cash["facts"]))
    with pytest.raises(RuntimePortfolioSnapshotError, match="projection differs"):
        _verified(snapshot, expected_run_id=assembly["run_id"], expected_account=assembly["account"], reference_payloads=references)


def test_legacy_cash_schema_rejects_mixed_v2_fields():
    kwargs = _current_scale_kwargs()
    kwargs["sections"]["broker_cash"]["facts"]["cash_snapshot"] = None
    with pytest.raises(RuntimePortfolioSnapshotError):
        build_runtime_portfolio_snapshot(**kwargs)
