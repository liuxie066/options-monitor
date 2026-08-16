from __future__ import annotations

import hashlib
import json

from scripts import benchmark_shadow_replay_generations as benchmark


def test_generation_benchmark_fixture_and_short_gate() -> None:
    canonical = (
        json.dumps(
            benchmark.fixture_descriptor(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    receipt = benchmark.run_benchmark(warmups=0, repetitions=1)

    assert hashlib.sha256(canonical).hexdigest() == benchmark.FIXTURE_CONTRACT_SHA256
    assert receipt["fixture_contract_sha256"] == benchmark.FIXTURE_CONTRACT_SHA256
    assert receipt["resolution"]["generation_id"].startswith("generation:")
    assert receipt["resolution"]["partition_reference_count"] == 10_000
    assert receipt["resolution"]["manifest_read_count"] == 33
    assert receipt["resolution"]["delta_manifest_read_count"] == 32
    assert receipt["resolution"]["partition_payload_read_count"] == 0
    assert all(receipt["checks"].values())
    assert receipt["status"] == "pass"
    assert receipt["violations"] == []
    assert benchmark.benchmark_exit_code(receipt) == 0


def test_generation_benchmark_limit_controls_exit(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "WALL_LIMIT_NS", 0)

    receipt = benchmark.run_benchmark(warmups=0, repetitions=1)

    assert "resolution_wall_p95" in receipt["violations"]
    assert receipt["status"] == "fail"
    assert benchmark.benchmark_exit_code(receipt) == 1
