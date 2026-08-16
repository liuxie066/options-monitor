#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application.shadow_replay import generations
from src.application.shadow_replay.common import (
    DATASET_FILES,
    refresh_dataset_manifest,
    write_jsonl,
)


RECEIPT_SCHEMA = "shadow_replay_generation_benchmark.v1"
FIXTURE_SCHEMA = "shadow_replay_generation_benchmark_fixture.v1"
FIXTURE_CONTRACT_SHA256 = "d37d1cec77413db4d0ef0f03e0caf4337a1e7ffe929721a0aa558b2a30147c5d"
PARTITION_COUNT = 10_000
DELTA_DEPTH = generations.MAX_DELTA_DEPTH
WARMUPS = 5
REPETITIONS = 30
WALL_LIMIT_NS = 2_000_000_000
ALLOCATION_LIMIT_BYTES = 64 * 1024 * 1024
SOURCE_PATHS = (
    "src/application/shadow_replay/generations.py",
    "src/application/shadow_replay/common.py",
    "scripts/benchmark_shadow_replay_generations.py",
)


def fixture_descriptor() -> dict[str, Any]:
    return {
        "schema_version": FIXTURE_SCHEMA,
        "partition_reference_count": PARTITION_COUNT,
        "delta_depth": DELTA_DEPTH,
        "files": ["candidate_snapshots.jsonl"],
        "replacement_partition_count_per_delta": 1,
        "partition_payload_files_created": False,
        "measurement": {
            "fixture_construction_inside_timing": False,
            "warmups": WARMUPS,
            "repetitions": REPETITIONS,
            "allocation_profile_separate": True,
        },
    }


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _partition_ref(index: int, *, version: int = 0) -> dict[str, Any]:
    compressed = _sha(f"compressed:{index}:{version}")
    return generations.validate_partition_ref(
        {
            "schema_version": generations.PARTITION_REF_SCHEMA_VERSION,
            "artifact_class": "immutable_shared_partition",
            "relpath": f"partitions/sha256/{compressed[:2]}/{compressed}.jsonl.gz",
            "sha256": compressed,
            "size_bytes": 1,
            "content_sha256": _sha(f"content:{index}:{version}"),
            "content_size_bytes": 1,
            "file_name": "candidate_snapshots.jsonl",
            "file_schema": "shadow_replay_candidate_snapshot.v1",
            "row_count": 1,
            "scope": {
                "schema_version": "shadow_replay_candidate_snapshot.v1",
                "market": "us",
                "date": "2026-08-17",
                "account": f"fixture-{index:05d}",
            },
        }
    )


def _summary(refs: list[dict[str, Any]]) -> dict[str, Any]:
    file_summary = {
        "sha256": generations._canonical_sha256([ref["content_sha256"] for ref in refs]),
        "bytes": len(refs),
        "row_count": len(refs),
    }
    return generations._logical_summary(
        {"candidate_snapshots.jsonl": refs},
        {"candidate_snapshots.jsonl": file_summary},
    )


def _write_manifest(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    identity = generations._canonical_sha256(body)
    manifest = {**body, "generation_id": f"generation:{identity}"}
    payload = generations._canonical_bytes(manifest)
    relpath = f"generations/{identity}.manifest.json"
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return generations.validate_generation_ref(
        {
            "schema_version": generations.GENERATION_REF_SCHEMA_VERSION,
            "generation_id": manifest["generation_id"],
            "relpath": relpath,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "resolved_generation_sha256": body["resolved_generation_sha256"],
            "depth": body["depth"],
        }
    )


def build_fixture(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    refs = [_partition_ref(index) for index in range(PARTITION_COUNT)]
    logical = _summary(refs)
    files = {"candidate_snapshots.jsonl": refs}
    projection = {
        "schema_version": "shadow_replay_dataset.v1",
        "dataset_id": "generation-benchmark",
    }

    def body(*, depth: int, parent: dict[str, Any] | None, changes: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": generations.GENERATION_SCHEMA_VERSION,
            "kind": "base" if depth == 0 else "delta",
            "depth": depth,
            "parent": parent,
            "generation_store_root_relpath": "..",
            "dataset": dict(projection),
            "provenance": {
                "legacy_revision": depth + 1,
                "manifest_projection": dict(projection),
            },
            "added_partition_sha256": [ref["sha256"] for change in changes for ref in change["added"]],
            "removed_partition_sha256": [digest for change in changes for digest in change["removed_sha256"]],
            "files": files if depth == 0 else {},
            "changes": changes,
            "logical_summary": logical,
            "resolved_generation_sha256": generations._resolved_sha256(files, logical),
        }

    ref = _write_manifest(root, body(depth=0, parent=None, changes=[]))
    for depth in range(1, DELTA_DEPTH + 1):
        old = refs[-1]
        replacement = _partition_ref(PARTITION_COUNT - 1, version=depth)
        refs[-1] = replacement
        logical = _summary(refs)
        files = {"candidate_snapshots.jsonl": refs}
        change = {
            "file_name": "candidate_snapshots.jsonl",
            "prefix_count": PARTITION_COUNT - 1,
            "removed_sha256": [old["sha256"]],
            "added": [replacement],
            "delete_file": False,
        }
        ref = _write_manifest(root, body(depth=depth, parent=ref, changes=[change]))
    return ref


def _distribution(samples: list[int]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "unit": "ns",
        "median": int(statistics.median(samples)),
        "p95": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "samples": samples,
    }


def _measure(
    action: Callable[[], dict[str, Any]],
    *,
    warmups: int,
    repetitions: int,
) -> tuple[dict[str, Any], dict[str, Any], int, dict[str, Any]]:
    wall: list[int] = []
    cpu: list[int] = []
    result: dict[str, Any] = {}
    for index in range(warmups + repetitions):
        started_wall = time.perf_counter_ns()
        started_cpu = time.process_time_ns()
        result = action()
        elapsed_cpu = time.process_time_ns() - started_cpu
        elapsed_wall = time.perf_counter_ns() - started_wall
        if index >= warmups:
            wall.append(elapsed_wall)
            cpu.append(elapsed_cpu)
    tracemalloc.start()
    action()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return _distribution(wall), _distribution(cpu), peak, result


def _append_growth(root: Path) -> dict[str, Any]:
    dataset = root / "append"
    dataset.mkdir()
    rows = [
        {
            "schema_version": "shadow_replay_candidate_snapshot.v1",
            "market": "US",
            "account": "lx",
            "decision_at_utc": "2026-08-17T00:00:00Z",
            "contract_symbol": f"FIXTURE{index:05d}",
        }
        for index in range(258)
    ]
    for name in DATASET_FILES:
        write_jsonl(dataset / name, rows[:257] if name == DATASET_FILES[0] else [])
    first = refresh_dataset_manifest(dataset)["generation"]
    before = _generation_store_bytes(dataset)
    write_jsonl(dataset / DATASET_FILES[0], rows)
    second = refresh_dataset_manifest(dataset)["generation"]
    after = _generation_store_bytes(dataset)
    delta = json.loads((dataset / second["relpath"]).read_text(encoding="utf-8"))
    resolved = generations.resolve_dataset_generation(dataset, second)
    added_bytes = sum(ref["size_bytes"] for change in delta["changes"] for ref in change["added"])
    return {
        "physical_growth_bytes": after - before,
        "new_unique_compressed_partition_bytes": added_bytes,
        "growth_limit_bytes": added_bytes + 64 * 1024,
        "parent_prefix_reused": resolved["files"][DATASET_FILES[0]][0]
        == generations.resolve_dataset_generation(dataset, first)["files"][DATASET_FILES[0]][0],
        "resolved_generation_sha256": resolved["resolved_generation_sha256"],
    }


def _generation_store_bytes(dataset: Path) -> int:
    return sum(
        path.stat().st_size
        for directory in (dataset / "partitions", dataset / "generations")
        if directory.exists()
        for path in directory.rglob("*")
        if path.is_file()
    )


def _source_sha256() -> str:
    digest = hashlib.sha256()
    for relpath in SOURCE_PATHS:
        payload = (REPO_ROOT / relpath).read_bytes()
        digest.update(relpath.encode("utf-8") + b"\0" + payload + b"\0")
    return digest.hexdigest()


def run_benchmark(*, warmups: int, repetitions: int) -> dict[str, Any]:
    if warmups < 0 or repetitions < 1:
        raise ValueError("warmups must be non-negative and repetitions must be positive")
    descriptor_sha = generations._canonical_sha256(fixture_descriptor())
    violations = [] if descriptor_sha == FIXTURE_CONTRACT_SHA256 else ["fixture_contract_sha256"]
    with tempfile.TemporaryDirectory(prefix="shadow-generation-benchmark-") as raw:
        root = Path(raw) / "dataset"
        ref = build_fixture(root)
        wall, cpu, peak, resolved = _measure(
            lambda: generations.resolve_dataset_generation(root, ref),
            warmups=warmups,
            repetitions=repetitions,
        )
        growth = _append_growth(Path(raw))
    checks = {
        "partition_reference_count": sum(len(refs) for refs in resolved["files"].values()) == PARTITION_COUNT,
        "delta_manifest_read_count": resolved["delta_manifest_read_count"] == DELTA_DEPTH,
        "manifest_read_count": resolved["manifest_read_count"] == DELTA_DEPTH + 1,
        "partition_payload_read_count": resolved["partition_payload_read_count"] == 0,
        "generation_id": resolved["generation_id"] == ref["generation_id"],
        "append_growth": growth["physical_growth_bytes"] <= growth["growth_limit_bytes"],
        "append_parent_prefix_reused": growth["parent_prefix_reused"],
    }
    violations.extend(name for name, passed in checks.items() if not passed)
    if wall["p95"] > WALL_LIMIT_NS:
        violations.append("resolution_wall_p95")
    if peak > ALLOCATION_LIMIT_BYTES:
        violations.append("resolution_peak_allocation")
    formal = warmups == WARMUPS and repetitions == REPETITIONS
    return {
        "schema_version": RECEIPT_SCHEMA,
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_label": "acceptance_5_warmups_30_repetitions" if formal else "non_acceptance_smoke",
        "fixture_contract_sha256": descriptor_sha,
        "source_sha256": _source_sha256(),
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "fixture": fixture_descriptor(),
        "timing": {"wall": wall, "cpu": cpu},
        "python_peak_allocation_bytes": peak,
        "python_peak_allocation_limit_bytes": ALLOCATION_LIMIT_BYTES,
        "wall_p95_limit_ns": WALL_LIMIT_NS,
        "resolution": {
            "generation_id": resolved["generation_id"],
            "partition_reference_count": sum(len(refs) for refs in resolved["files"].values()),
            "manifest_read_count": resolved["manifest_read_count"],
            "delta_manifest_read_count": resolved["delta_manifest_read_count"],
            "partition_payload_read_count": resolved["partition_payload_read_count"],
        },
        "append_growth": growth,
        "checks": checks,
        "violations": sorted(set(violations)),
        "status": "pass" if not violations else "fail",
    }


def benchmark_exit_code(receipt: dict[str, Any]) -> int:
    return 0 if receipt["status"] == "pass" and not receipt["violations"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark immutable Shadow Replay generation resolution.")
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = run_benchmark(warmups=args.warmups, repetitions=args.repetitions)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return benchmark_exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
