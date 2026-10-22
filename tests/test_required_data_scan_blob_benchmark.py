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
        "tests/fixtures/required_data_scan_blob_benchmark_metadata.v2.json"
    )
    assert descriptor["baseline"]["kind"] == "read_only_filesystem_metadata"
    assert descriptor["baseline"]["production_payload_copied"] is False
    assert descriptor["baseline"]["raw_account_identifier_copied"] is False
    assert descriptor["baseline"]["paired_payload_count"] == 1926
    assert descriptor["fixture"]["row_count"] == 254
    assert descriptor["fixture"]["expected"] == fixture["observed"]
    assert len(fixture["raw"]) + len(fixture["csv"]) == 227_564


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


def test_schema_growth_preserves_historical_baseline_and_raw_payload() -> None:
    historical_bytes = (benchmark.REPO_ROOT / "tests/fixtures/required_data_scan_blob_benchmark_metadata.v1.json").read_bytes()
    historical = json.loads(historical_bytes)
    assert hashlib.sha256(historical_bytes).hexdigest() == "e6d4c4033a4695bfc3f38298d34be0257d90042e3a1a84907dc9812f9ced1485"
    current = benchmark.fixture_descriptor()
    assert current["baseline"] == historical["baseline"]
    assert current["baseline"]["legacy_pair_uncompressed_bytes"]["p99"] == 226_760
    growth = current["fixture"]["schema_growth"]
    assert growth["shape_is_new_observed_p99"] is False
    assert growth["row_delimiter_bytes"] == 254 * 3
    assert growth["header_bytes"] == sum(len(c) + 1 for c in growth["added_columns"])
    assert growth["total_bytes"] == growth["row_delimiter_bytes"] + growth["header_bytes"] == 804
    fixture = benchmark.generate_fixture()
    assert fixture["observed"]["raw_json_sha256"] == historical["fixture"]["expected"]["raw_json_sha256"]
    assert fixture["observed"]["raw_json_bytes"] == historical["fixture"]["expected"]["raw_json_bytes"]
    assert fixture["observed"]["required_data_csv_bytes"] - historical["fixture"]["expected"]["required_data_csv_bytes"] == 804
    assert len(json.loads(fixture["raw"])["fixture_padding"]) == 419
    assert len(benchmark.FIXTURE_COLUMNS) == 63
    assert benchmark.FIXTURE_COLUMNS == benchmark.REQUIRED_DATA_COLUMNS


def test_unreviewed_schema_growth_fails_before_measurement(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "REQUIRED_DATA_COLUMNS", [*benchmark.REQUIRED_DATA_COLUMNS, "unreviewed"])
    with pytest.raises(RuntimeError, match="schema changed"):
        benchmark.generate_fixture()


def test_current_schema_populated_delta_evidence_uses_real_bundle(tmp_path) -> None:
    """Correctness sample; not a new resource baseline or p99 observation."""
    import io
    import math

    import pandas as pd

    fixture = benchmark.generate_fixture()
    provider = json.loads(fixture["raw"])
    for index, row in enumerate(provider["rows"]):
        row.update(
            delta=-math.nextafter(.35, math.inf) if index % 2 == 0 else None,
            delta_source="opend_market_snapshot.option_delta",
            delta_unit="fraction",
            delta_reason_code=None if index % 2 == 0 else "delta_missing",
        )
    frame = pd.DataFrame(provider["rows"]).reindex(columns=benchmark.REQUIRED_DATA_COLUMNS)
    frame.loc[frame["multiplier"].isna(), "multiplier"] = 100
    current = {"raw": benchmark._raw_bytes(provider), "csv": frame.to_csv(index=False).encode()}
    decoded = pd.read_csv(io.BytesIO(current["csv"]), float_precision="round_trip")
    assert abs(decoded.iloc[0]["delta"]) > .35
    assert decoded.iloc[0]["delta_source"] == "opend_market_snapshot.option_delta"
    assert decoded.iloc[0]["delta_unit"] == "fraction"
    assert decoded.iloc[1]["delta_reason_code"] == "delta_missing"
    result = benchmark._canonical_bundle(tmp_path / "current", current)
    assert result["mismatch_count"] == 0
    assert result["removed_files"] == 2
    assert result["failed_files"] == 0
