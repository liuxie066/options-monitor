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


@pytest.mark.parametrize(
    "fault_sample",
    [None, "sample-0000", "sample-0005", "allocation", "sample-0034"],
    ids=["normal", "warmup-eio", "first-timed-eio", "allocation-eio", "last-timed-eio"],
)
def test_formal_main_checks_every_sample_cleanup(monkeypatch, tmp_path, fault_sample) -> None:
    """Real 5+30+allocation correctness run; makes no timing performance claim."""
    import errno
    import os
    from pathlib import Path

    from src.application import required_data_snapshot as snapshot

    original_bundle = benchmark._canonical_bundle
    original_bound_fsync = snapshot.fsync_required_data_bound_file
    original_fsync = os.fsync
    samples = []
    cleanup_sample = None
    injected = 0

    def _observe_bundle(root, fixture):
        result = original_bundle(root, fixture)
        samples.append({"sample": root.name, **result})
        return result

    def _bound_fsync(**kwargs):
        nonlocal cleanup_sample
        cleanup_sample = next(
            (part for part in Path(kwargs["root"]).parts if part == fault_sample),
            None,
        )
        try:
            return original_bound_fsync(**kwargs)
        finally:
            cleanup_sample = None

    def _fsync(fd):
        nonlocal injected
        if fault_sample is not None and cleanup_sample == fault_sample and injected == 0:
            injected += 1
            raise OSError(errno.EIO, "synthetic cleanup durability fsync EIO")
        return original_fsync(fd)

    monkeypatch.setattr(benchmark, "_canonical_bundle", _observe_bundle)
    monkeypatch.setattr(snapshot, "fsync_required_data_bound_file", _bound_fsync)
    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(benchmark, "_git_sha", lambda: "0" * 40)
    monkeypatch.setattr(benchmark.platform, "platform", lambda: "isolated correctness test")
    output = tmp_path / "receipt.json"
    code = benchmark.main(["--profile", "canonical", "--output", str(output)])
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert receipt["run_label"] == "acceptance_5_warmups_30_repetitions"
    assert [sample["sample"] for sample in samples] == [
        *(f"sample-{index:04d}" for index in range(35)), "allocation",
    ]
    assert len(receipt["timing"]["canonical_wall"]["samples"]) == 30
    assert len(receipt["timing"]["canonical_cpu"]["samples"]) == 30
    assert receipt["timing"]["tracemalloc_enabled"] is True
    bad = [sample for sample in samples if sample["mismatch_count"] or sample["failed_files"]]
    assert injected == int(fault_sample is not None)
    assert [sample["sample"] for sample in bad] == ([] if fault_sample is None else [fault_sample])
    for sample in samples:
        limit = sum(sample[key] for key in (
            "compressed_blob_bytes", "manifest_bytes", "compact_receipt_bytes", "cache_metadata_bytes",
        ))
        if sample["sample"] == fault_sample:
            assert sample["failed_files"] == 2
            assert sample["removed_files"] == 0
            assert sample["retained_bytes"] - limit == 227_564
            assert sample["mismatch_count"] == 3
            assert set(sample["mismatch_samples"]) == {
                "canonical_cleanup", "canonical_raw_shadow", "canonical_csv_shadow",
            }
        else:
            assert sample["failed_files"] == 0
            assert sample["removed_files"] == 2
            assert sample["removed_bytes"] == 227_564
            assert sample["mismatch_count"] == 0
            assert sample["mismatch_samples"] == []
            assert sample["retained_bytes"] == limit

    diagnostic = json.dumps({
        "fault_sample": fault_sample, "injected": injected, "bad_samples": bad,
        "main_exit": code, "receipt_space": receipt["space"], "violations": receipt["violations"],
    }, sort_keys=True)
    if fault_sample is None:
        assert code == 0, diagnostic
        assert receipt["violations"] == [], diagnostic
    else:
        assert code == 1, diagnostic
        assert "bounded_shadow_comparison" in receipt["violations"], diagnostic
        assert "canonical_persisted_bytes" in receipt["violations"], diagnostic

    checks = receipt["sample_checks"]
    assert checks["counts"] == {"warmup": 5, "timing": 30, "allocation": 1}
    assert checks["failed_count"] == int(fault_sample is not None)
    if fault_sample is None:
        assert checks["failure_samples"] == []
        assert checks["violations"] == []
    else:
        expected_phase, expected_index = {
            "sample-0000": ("warmup", 0),
            "sample-0005": ("timing", 0),
            "sample-0034": ("timing", 29),
            "allocation": ("allocation", 0),
        }[fault_sample]
        failure, = checks["failure_samples"]
        assert (failure["phase"], failure["index"]) == (expected_phase, expected_index)
        assert set(failure["violations"]) == {"bounded_shadow_comparison", "canonical_persisted_bytes"}
        assert failure["space"]["failed_files"] == 2
        assert failure["space"]["retained_bytes"] - failure["space"]["canonical_persisted_limit_bytes"] == 227_564


def test_sample_failure_details_are_bounded_but_all_violations_are_retained(monkeypatch, tmp_path) -> None:
    healthy = benchmark._canonical_bundle(tmp_path / "reference", benchmark.generate_fixture())
    observed = []

    def _fault(root, fixture):
        observed.append(root.name)
        result = dict(healthy)
        if root.name == "allocation":
            result["retained_bytes"] += 1
        else:
            result["mismatch_count"] = 1
            result["mismatch_samples"] = ["synthetic_sample_mismatch"]
        return result

    monkeypatch.setattr(benchmark, "_canonical_bundle", _fault)
    receipt = benchmark.run_profile("canonical", warmups=5, repetitions=30)

    assert observed == [*(f"sample-{index:04d}" for index in range(35)), "allocation"]
    checks = receipt["sample_checks"]
    assert checks["counts"] == {"warmup": 5, "timing": 30, "allocation": 1}
    assert checks["failed_count"] == 36
    assert [(row["phase"], row["index"]) for row in checks["failure_samples"]] == [
        *(("warmup", index) for index in range(5)),
        *(("timing", index) for index in range(5)),
    ]
    assert all(row["violations"] == ["bounded_shadow_comparison"] for row in checks["failure_samples"])
    assert set(checks["violations"]) == {"bounded_shadow_comparison", "canonical_persisted_bytes"}
    assert receipt["violations"] == checks["violations"]
    assert receipt["space"]["retained_bytes"] == healthy["retained_bytes"]
    assert benchmark.benchmark_exit_code(receipt) == 1


def test_sample_validation_is_outside_measured_timing() -> None:
    clock = {"wall": 0, "cpu": 0}

    class Evidence(dict):
        def __getitem__(self, key):
            clock["wall"] += 10_000
            clock["cpu"] += 10_000
            return super().__getitem__(key)

    def action(root):
        clock["wall"] += 10
        clock["cpu"] += 3
        return Evidence(
            compressed_blob_bytes=1, manifest_bytes=2, compact_receipt_bytes=3,
            cache_metadata_bytes=4, retained_bytes=10, mismatch_count=0, mismatch_samples=[],
        )

    from unittest.mock import patch

    with patch.object(benchmark.time, "perf_counter_ns", lambda: clock["wall"]), \
         patch.object(benchmark.time, "process_time_ns", lambda: clock["cpu"]):
        wall, cpu, peak, _, checks = benchmark._measure(
            action, warmups=1, repetitions=3, measure_allocation=False,
        )

    assert wall["samples"] == [10, 10, 10]
    assert cpu["samples"] == [3, 3, 3]
    assert peak == 0
    assert checks["counts"] == {"warmup": 1, "timing": 3, "allocation": 0}
    assert checks["violations"] == []


def test_allocation_cancellation_stops_profiler_and_does_not_publish_receipt(monkeypatch, tmp_path) -> None:
    healthy = benchmark._canonical_bundle(tmp_path / "reference", benchmark.generate_fixture())
    profiler_calls = []

    def action(root, fixture):
        if root.name == "allocation":
            raise KeyboardInterrupt
        return dict(healthy)

    monkeypatch.setattr(benchmark, "_canonical_bundle", action)
    monkeypatch.setattr(benchmark.tracemalloc, "start", lambda: profiler_calls.append("start"))
    monkeypatch.setattr(benchmark.tracemalloc, "stop", lambda: profiler_calls.append("stop"))
    output = tmp_path / "receipt.json"
    with pytest.raises(KeyboardInterrupt):
        benchmark.main(["--profile", "canonical", "--output", str(output)])

    assert profiler_calls == ["start", "stop"]
    assert not output.exists()
