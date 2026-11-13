"""Controller acceptance must reject unqualified completed benchmark work."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import benchmark_tick_execution as benchmark


def _run_coordinator(tmp_path, monkeypatch, *, scenario="complete", invalid_index=None,
                     worker_return=0, emit_records=True, timeout=False):
    inputs, output = tmp_path / "inputs", tmp_path / "output"
    inputs.mkdir()

    def run_worker(command, **_kwargs):
        if timeout:
            benchmark.write_json(output / f"{scenario}-0" / "sample.json", {
                "scenario": scenario, "index": 0, "status": "started",
                "condition": "process_cold"})
            raise subprocess.TimeoutExpired(command, 1)
        if emit_records:
            for index in range(4):
                benchmark.write_json(output / f"{scenario}-{index}" / "sample.json", {
                    "scenario": scenario, "index": index, "status": "completed",
                    "condition": "process_cold" if index == 0 else "process_warm",
                    "wall_seconds": float(index + 1),
                    "valid_expected_outcome": index != invalid_index,
                    "execution": {"rc": 1 if scenario == "localized_discovery_failure" else 0},
                })
        return SimpleNamespace(returncode=worker_return, stdout="", stderr="")

    monkeypatch.setattr(benchmark, "subprocess", SimpleNamespace(
        run=run_worker, check_output=subprocess.check_output,
        TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(sys, "argv", ["benchmark_tick_execution.py",
        "--source-root", str(Path(benchmark.__file__).resolve().parents[1]),
        "--inputs", str(inputs), "--output-dir", str(output),
        "--scenario", scenario, "--samples", "4"])
    rc = benchmark.main()
    return rc, json.loads((output / "results.json").read_text())


@pytest.mark.parametrize("scenario", ["complete", "localized_discovery_failure"])
def test_benchmark_accepts_all_qualified_outcomes_including_expected_business_failure(
        tmp_path, monkeypatch, scenario):
    rc, result = _run_coordinator(tmp_path, monkeypatch, scenario=scenario)
    assert result["statistics"][scenario]["warm_p50_seconds"] == 3.0
    assert result["acceptance_valid"] is True
    assert result["denominator"]["planned"] == result["denominator"]["valid_expected_outcomes"] == 4
    assert len(result["samples"]) == 4
    assert rc == 0


def test_benchmark_rejects_completed_but_unqualified_work_without_dropping_samples(tmp_path, monkeypatch):
    rc, result = _run_coordinator(tmp_path, monkeypatch, invalid_index=1)
    assert result["statistics"]["complete"]["warm_p50_seconds"] is None
    assert result["statistics"]["complete"]["warm_count"] == 3
    assert result["statistics"]["complete"]["valid_warm_count"] == 2
    assert result["acceptance_valid"] is False
    assert result["denominator"]["planned"] == result["denominator"]["records"] == 4
    assert result["denominator"]["valid_expected_outcomes"] == 3
    assert len(result["samples"]) == 4
    assert rc == 1


@pytest.mark.parametrize("emit_records", [True, False])
def test_benchmark_worker_failure_invalidates_acceptance_even_with_qualified_records(
        tmp_path, monkeypatch, emit_records):
    rc, result = _run_coordinator(tmp_path, monkeypatch, worker_return=2, emit_records=emit_records)
    assert result["statistics"]["complete"]["warm_p50_seconds"] is None
    assert result["acceptance_valid"] is False
    assert result["workers"][0]["returncode"] == 2
    assert result["denominator"]["planned"] == result["denominator"]["records"] == 4
    assert result["denominator"]["valid_expected_outcomes"] == (4 if emit_records else 0)
    assert len(result["samples"]) == 4
    assert rc == 1


def test_benchmark_timeout_retains_started_and_not_started_records(tmp_path, monkeypatch):
    rc, result = _run_coordinator(tmp_path, monkeypatch, timeout=True)
    assert result["statistics"]["complete"]["warm_p50_seconds"] is None
    assert result["acceptance_valid"] is False
    assert result["denominator"]["planned"] == result["denominator"]["records"] == 4
    assert result["denominator"]["worker_timeouts"] == 1
    assert result["denominator"]["measurement_timeouts"] == 1
    assert result["denominator"]["not_started"] == 3
    assert len(result["samples"]) == 4
    assert rc == 1
