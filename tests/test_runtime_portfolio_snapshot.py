from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

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
from src.application.runtime_portfolio_snapshot import (
    MAX_CANONICAL_BYTES,
    RuntimePortfolioSnapshotError,
    assemble_runtime_portfolio_snapshot,
    build_runtime_portfolio_section,
    build_runtime_portfolio_snapshot,
    build_source_status_section,
    canonical_json_bytes,
    compare_runtime_portfolio_snapshot,
    load_runtime_portfolio_snapshot,
    publish_runtime_portfolio_snapshot,
    validate_replay_bundle,
    verify_runtime_portfolio_snapshot,
)
from src.application.source_receipts import sha256_bytes
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    write_account_run_state_bytes_once_safely,
)


_CONTRACT_HASH = "f180e7bbcdd2f9bdaf6edfc540099b5c54156f3c6971ce83ef55c6fea51099c8"
_INPUT_HASHES = {
    "current_scale": "344f3d943f56981174974f64cd5965a5345d057d1a7dfe1aabfa33379d17d4f7",
    "current_state_10x": "8e057aa0dc95b4af636ef50bfcd9905f691961cc9c3e08f660a0d36096ec4e09",
}


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
        assert snapshot == verify_runtime_portfolio_snapshot(
            snapshot,
            expected_run_id="fixture-runtime-0001",
            expected_account="acct_fixture",
            reference_payloads=fixture["builder_kwargs"]["reference_payloads"],
        )


def test_assembler_consumes_one_exact_owner_bundle() -> None:
    assembly = _owner_assembly_kwargs()

    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)

    assert snapshot["status"] == "trusted"
    assert snapshot["legacy_comparison"]["status"] == "matched"
    assert snapshot == verify_runtime_portfolio_snapshot(
        snapshot,
        expected_run_id=assembly["run_id"],
        expected_account=assembly["account"],
        reference_payloads=references,
    )


def test_runtime_snapshot_preserves_v2_prepared_owner_binding() -> None:
    assembly = _owner_assembly_kwargs()
    manifest = json.loads(assembly["prepared_option_manifest_bytes"])
    manifest["schema_version"] = "prepared_option_positions_context.v2"
    assembly["prepared_option_manifest_bytes"] = canonical_json_bytes(manifest)

    snapshot, references = assemble_runtime_portfolio_snapshot(**assembly)
    binding = next(
        row
        for row in snapshot["replay_bindings"]
        if row["role"] == "prepared_option_positions_context"
    )

    assert binding["schema_version"] == "prepared_option_positions_context.v2"
    assert binding["relpath"] == (
        "state/prepared_option_positions_context.v2.json"
    )
    assert snapshot == verify_runtime_portfolio_snapshot(
        snapshot,
        expected_run_id=assembly["run_id"],
        expected_account=assembly["account"],
        reference_payloads=references,
    )


def test_canonical_bytes_and_immutable_publication_are_stable(tmp_path) -> None:
    fixture = generate_fixture("current_scale")
    kwargs = fixture["builder_kwargs"]
    snapshot = build_runtime_portfolio_snapshot(**kwargs)
    path = publish_runtime_portfolio_snapshot(
        base=tmp_path,
        snapshot=snapshot,
        reference_payloads=kwargs["reference_payloads"],
    )
    adopted = publish_runtime_portfolio_snapshot(
        base=tmp_path,
        snapshot=snapshot,
        reference_payloads=kwargs["reference_payloads"],
    )

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
    legacy = {
        name: deepcopy(changed_sections[name]["facts"])
        for name in (
            "ledger_projection",
            "broker_cash",
            "broker_positions",
            "cash_occupation",
        )
    }
    comparison = compare_runtime_portfolio_snapshot(
        sections=changed_sections,
        chosen_results=kwargs["chosen_results"],
        legacy_section_facts=legacy,
        legacy_chosen_results=kwargs["chosen_results"],
        ledger_shadow_status="unavailable",
    )
    unavailable = {
        "status": "unavailable",
        "reason_codes": ["legacy_comparison:unavailable"],
    }
    changed_sections["ledger_projection"]["completeness"] = unavailable
    owners = deepcopy(changed_sections["source_status"]["facts"])
    owners["ledger_projection"]["completeness"] = unavailable
    changed_sections["source_status"] = build_source_status_section(account="acct_fixture", owner_receipts=owners)
    conflicting = build_runtime_portfolio_snapshot(
        **{
            **kwargs,
            "sections": changed_sections,
            "legacy_comparison": comparison,
        }
    )
    assert conflicting["status"] == "data_unavailable"
    with pytest.raises(AccountRunConfigError) as conflict:
        publish_runtime_portfolio_snapshot(
            base=tmp_path,
            snapshot=conflicting,
            reference_payloads=kwargs["reference_payloads"],
        )
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
    fixture = generate_fixture("current_scale")
    kwargs = fixture["builder_kwargs"]
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
        verify_runtime_portfolio_snapshot(
            candidate,
            expected_run_id=expected_run,
            expected_account=expected_account,
            reference_payloads=references,
        )


def test_shadow_mismatch_is_bounded_metadata_and_fails_closed() -> None:
    fixture = generate_fixture("current_scale")
    kwargs = fixture["builder_kwargs"]
    legacy = {
        name: deepcopy(kwargs["sections"][name]["facts"])
        for name in (
            "ledger_projection",
            "broker_cash",
            "broker_positions",
            "cash_occupation",
        )
    }
    legacy["broker_cash"]["filters"] = {"market": "hk"}
    comparison = compare_runtime_portfolio_snapshot(
        sections=kwargs["sections"],
        chosen_results=kwargs["chosen_results"],
        legacy_section_facts=legacy,
        legacy_chosen_results=kwargs["chosen_results"],
        ledger_shadow_status="unavailable",
    )

    assert comparison["status"] == "unavailable"
    assert comparison["mismatch_count"] == 2
    assert len(comparison["mismatch_samples"]) <= 10
    assert set(comparison["mismatch_samples"][0]) == {
        "section",
        "key",
        "reason",
        "legacy_sha256",
        "compact_sha256",
    }
    sections = deepcopy(kwargs["sections"])
    unavailable = {
        "status": "unavailable",
        "reason_codes": ["legacy_comparison:unavailable"],
    }
    sections["ledger_projection"]["completeness"] = unavailable
    owners = deepcopy(sections["source_status"]["facts"])
    owners["ledger_projection"]["completeness"] = unavailable
    sections["source_status"] = build_source_status_section(account="acct_fixture", owner_receipts=owners)
    snapshot = build_runtime_portfolio_snapshot(**{**kwargs, "sections": sections, "legacy_comparison": comparison})
    assert snapshot["status"] == "data_unavailable"
    assert snapshot["reason_codes"] == [
        "legacy_comparison:unavailable",
        "section_completeness:ledger_projection:unavailable",
        "section_completeness:source_status:unavailable",
    ]


def test_loader_rejects_invalid_present_artifact_without_fallback(tmp_path) -> None:
    fixture = generate_fixture("current_scale")
    references = fixture["builder_kwargs"]["reference_payloads"]
    write_account_run_state_bytes_once_safely(
        base=tmp_path,
        run_id="fixture-runtime-0001",
        account="acct_fixture",
        name="runtime_portfolio_snapshot.v1.json",
        payload=b"{}",
    )

    with pytest.raises(RuntimePortfolioSnapshotError):
        load_runtime_portfolio_snapshot(
            base=tmp_path,
            run_id="fixture-runtime-0001",
            account="acct_fixture",
            reference_payloads=references,
        )


def test_canonical_encoder_rejects_non_finite_numbers() -> None:
    with pytest.raises(RuntimePortfolioSnapshotError):
        canonical_json_bytes({"bad": float("nan")})


def test_policy_bearing_source_freshness_cannot_drift_from_owner() -> None:
    fixture = generate_fixture("current_scale")
    kwargs = fixture["builder_kwargs"]
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
    kwargs = generate_fixture("current_scale")["builder_kwargs"]
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
    kwargs = generate_fixture("current_scale")["builder_kwargs"]
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
    receipt = run_profile("current_scale", warmups=0, repetitions=1)
    assert benchmark_exit_code(receipt) == 0
    assert receipt["violations"] == []
    assert receipt["forbidden_history_structural_reference_count"] == 0
    assert receipt["forbidden_history_executable_spy_calls"] == 0
    assert receipt["forbidden_history_reader_calls"] == 0
    assert receipt["production_artifact_read_calls"] == 0
    assert receipt["legacy_comparison_matches"]

    def owner_drift(builder_kwargs: dict) -> None:
        chosen = builder_kwargs["chosen_results"]
        relpath = chosen["owner_snapshots"][0]["relpath"]
        builder_kwargs["reference_payloads"][relpath] = b"{}"

    owner_fault = run_profile(
        "current_scale",
        warmups=0,
        repetitions=1,
        fixture_mutator=owner_drift,
    )
    history_fault = run_profile(
        "current_scale",
        warmups=0,
        repetitions=1,
        executable_probe=lambda: ledger_api.preview_current_decision_projection_oracle(),
    )
    assert benchmark_exit_code(owner_fault) == 1
    assert "fixture_owner_validators_passed" in owner_fault["violations"]
    assert benchmark_exit_code(history_fault) == 1
    assert history_fault["forbidden_history_executable_spy_calls"] == 1
    assert history_fault["production_artifact_read_calls"] == 0
    assert "forbidden_history_executable_spy_calls" in history_fault["violations"]
