from __future__ import annotations

import hashlib
import json

import pytest

from scripts import benchmark_required_data_scan_blobs as benchmark


def test_checked_in_metadata_and_fixture_are_independently_pinned() -> None:
    raw = benchmark.FIXTURE_PATH.read_bytes()
    descriptor = json.loads(raw)
    canonical = (
        json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    fixture = benchmark.generate_fixture()

    assert raw == canonical
    assert hashlib.sha256(canonical).hexdigest() == benchmark.FIXTURE_CONTRACT_SHA256
    assert benchmark.FIXTURE_PATH == benchmark.REPO_ROOT / (
        "tests/fixtures/required_data_scan_blob_benchmark_metadata.v1.json"
    )
    assert descriptor["baseline"]["kind"] == "read_only_filesystem_metadata"
    assert descriptor["baseline"]["production_payload_copied"] is False
    assert descriptor["baseline"]["raw_account_identifier_copied"] is False
    assert descriptor["baseline"]["paired_payload_count"] == 1926
    assert descriptor["fixture"]["row_count"] == 254
    assert descriptor["fixture"]["expected"] == fixture["observed"]
    assert len(fixture["raw"]) + len(fixture["csv"]) == 226_760


def test_short_profile_enforces_resource_and_exit_gates(monkeypatch) -> None:
    original = benchmark._canonical_bundle
    calls = 0

    def _counted(root, fixture):
        nonlocal calls
        calls += 1
        return original(root, fixture)

    monkeypatch.setattr(benchmark, "_canonical_bundle", _counted)
    receipt = benchmark.run_profile("canonical", warmups=0, repetitions=1)

    assert calls == 1
    assert receipt["run_label"] == "non_acceptance_smoke"
    assert receipt["space"]["mismatch_count"] == 0
    assert receipt["space"]["mismatch_samples"] == []
    assert receipt["python_peak_allocation_bytes"] <= receipt["python_peak_allocation_limit_bytes"]
    assert receipt["violations"] == []
    assert receipt["space"]["removed_files"] == 2
    assert receipt["space"]["failed_files"] == 0
    assert receipt["timing"]["tracemalloc_enabled"] is False
    assert benchmark.benchmark_exit_code(receipt) == 0
    receipt["violations"] = ["injected_fault"]
    assert benchmark.benchmark_exit_code(receipt) == 1


def test_canonical_space_gate_drives_nonzero_exit(monkeypatch) -> None:
    original = benchmark._canonical_bundle

    def _fault(root, fixture):
        result = original(root, fixture)
        result["retained_bytes"] = 10**9
        return result

    monkeypatch.setattr(benchmark, "_canonical_bundle", _fault)
    receipt = benchmark.run_profile("canonical", warmups=0, repetitions=1)

    assert "canonical_persisted_bytes" in receipt["violations"]
    assert benchmark.benchmark_exit_code(receipt) == 1


def test_dual_output_profile_is_retired() -> None:
    assert benchmark.PROFILES == ("canonical",)
    with pytest.raises(ValueError, match="profile must be one of: canonical"):
        benchmark.run_profile("dual_output", warmups=0, repetitions=1)


def test_descriptor_drift_is_a_preflight_violation(monkeypatch, tmp_path) -> None:
    drifted = tmp_path / "descriptor.json"
    drifted.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(benchmark, "FIXTURE_PATH", drifted)

    _digest, violations = benchmark._descriptor_status()

    assert "fixture_descriptor_drift" in violations
    assert "fixture_descriptor_sha256" in violations


def test_descriptor_missing_is_a_preflight_violation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(benchmark, "FIXTURE_PATH", tmp_path / "missing.json")

    digest, violations = benchmark._descriptor_status()

    assert digest is None
    assert violations == ["fixture_descriptor_missing"]
