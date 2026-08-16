#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Callable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application.opend_symbol_outputs import REQUIRED_DATA_COLUMNS
from src.application.required_data_blobs import (
    build_required_data_scan_blob_payload,
    canonical_scan_blob_bytes,
    load_required_data_scan_blob,
    publish_required_data_scan_blob,
    required_data_shadow_base64_matches,
    required_data_shadow_file_matches,
)


FIXTURE_SCHEMA = "required_data_scan_blob_benchmark_fixture.v1"
RECEIPT_SCHEMA = "required_data_scan_blob_benchmark.v1"
FIXTURE_PATH = REPO_ROOT / "tests/fixtures/required_data_scan_blob_benchmark_metadata.v1.json"
FIXTURE_CONTRACT_SHA256 = "3bb750138808d18867a73ea57587aad2ea4e548ed18c1f23dc0954d113835da4"
EXPECTED_FIXTURE = {
    "raw_json_sha256": "54a6c0af5174a00956abc822dafca03647fab963cae1ba765b358eb53b0fb64e",
    "required_data_csv_sha256": "32cb7dda9bbbee24a7c52cf0264e6ec4c3db6041c811586975dc4f7790504087",
    "canonical_blob_sha256": "5c32970edd645a88a8d045dddf92b5ad0dd73a2a8a1dd66610c8c20594703784",
    "raw_json_bytes": 188_488,
    "required_data_csv_bytes": 38_272,
    "canonical_blob_bytes": 153_813,
}
SEED = 20260816
ROW_COUNT = 254
TARGET_LEGACY_PAIR_BYTES = 226_760
WARMUPS = 5
REPETITIONS = 30
PROFILES = ("canonical", "dual_output")


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def fixture_descriptor() -> dict[str, Any]:
    return {
        "schema_version": FIXTURE_SCHEMA,
        "seed": SEED,
        "baseline": {
            "kind": "read_only_filesystem_metadata",
            "observed_at_utc": "2026-08-16T00:00:00Z",
            "paired_payload_count": 1926,
            "production_payload_copied": False,
            "raw_account_identifier_copied": False,
            "legacy_pair_uncompressed_bytes": {
                "min": 1348,
                "median": 53928,
                "p99": TARGET_LEGACY_PAIR_BYTES,
                "max": 1807549,
                "mean": 70731,
            },
            "csv_row_count": {
                "min": 0,
                "median": 58,
                "p99": ROW_COUNT,
                "max": 738,
                "mean": 73,
            },
            "selection": "independent_nearest_rank_p99",
        },
        "fixture": {
            "symbol": "FIXTURE",
            "market": "US",
            "row_count": ROW_COUNT,
            "legacy_pair_uncompressed_bytes": TARGET_LEGACY_PAIR_BYTES,
            "entropy_class_row_counts": {
                "low": 85,
                "median": 85,
                "high": 84,
            },
            "expected": dict(EXPECTED_FIXTURE),
        },
        "measurement_metadata": {
            "python_version": "recorded_at_execution",
            "sqlite_version": "recorded_at_execution",
            "platform": "recorded_at_execution",
            "source_git_sha": "recorded_at_execution",
            "cold_warm_mode": "fresh_temp_root_per_sample",
            "timing_instrumentation": "perf_counter_ns_and_process_time_ns",
            "allocation_instrumentation": "separate_tracemalloc_sample",
            "warmups": WARMUPS,
            "repetitions": REPETITIONS,
        },
    }


def _descriptor_status() -> tuple[str | None, list[str]]:
    try:
        raw = FIXTURE_PATH.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None, ["fixture_descriptor_missing"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ["fixture_descriptor_unreadable"]
    canonical = _canonical_bytes(value)
    digest = hashlib.sha256(canonical).hexdigest()
    violations = []
    if raw != canonical or value != fixture_descriptor():
        violations.append("fixture_descriptor_drift")
    if digest != FIXTURE_CONTRACT_SHA256:
        violations.append("fixture_descriptor_sha256")
    return digest, violations


def _deterministic_text(label: str, length: int) -> str:
    blocks = (length + 63) // 64
    return "".join(hashlib.sha256(f"{SEED}:{label}:{sequence}".encode()).hexdigest() for sequence in range(blocks))[
        :length
    ]


def _fixture_row(index: int) -> dict[str, Any]:
    option_type = "put" if index % 2 == 0 else "call"
    entropy_class = index % 3
    if entropy_class == 0:
        vendor_detail = "A" * 200
    elif entropy_class == 1:
        vendor_detail = (f"fixture-{index:04d}|" * 20)[:200]
    else:
        vendor_detail = _deterministic_text(f"row-{index}", 200)
    return {
        "symbol": "FIXTURE",
        "market": "US",
        "option_type": option_type,
        "expiration": "2026-12-18",
        "dte": 124,
        "contract_symbol": f"FIXTURE261218{option_type[0].upper()}{index:08d}",
        "strike": 80 + index / 10,
        "bid": 1.1,
        "ask": 1.2,
        "last_price": 1.15,
        "mid": 1.15,
        "volume": index,
        "open_interest": 100 + index,
        "currency": "USD",
        "multiplier": None if index % 17 == 0 else 100,
        "opening_contract_status": "ready",
        "opening_contract_reason_codes": "[]",
        "vendor_detail": vendor_detail,
    }


def _raw_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def generate_fixture(*, verify: bool = True) -> dict[str, Any]:
    rows = [_fixture_row(index) for index in range(ROW_COUNT)]
    provider = {
        "symbol": "FIXTURE",
        "meta": {"status": "ok", "source_outcome": "success_rows"},
        "rows": rows,
        "fixture_padding": "",
    }
    frame = pd.DataFrame(rows)
    for column in REQUIRED_DATA_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[REQUIRED_DATA_COLUMNS]
    frame.loc[frame["multiplier"].isna(), "multiplier"] = 100
    output = io.StringIO()
    frame.to_csv(output, index=False)
    csv_bytes = output.getvalue().encode("utf-8")
    raw = _raw_bytes(provider)
    padding = TARGET_LEGACY_PAIR_BYTES - len(raw) - len(csv_bytes)
    if padding < 0:
        raise RuntimeError("scan blob fixture exceeds baseline p99 size")
    provider["fixture_padding"] = _deterministic_text("padding", padding)
    raw = _raw_bytes(provider)
    if len(raw) + len(csv_bytes) != TARGET_LEGACY_PAIR_BYTES:
        raise RuntimeError("scan blob fixture p99 size is not exact")
    payload = build_required_data_scan_blob_payload(
        symbol="FIXTURE",
        market="US",
        raw_json_bytes=raw,
        required_data_csv_bytes=csv_bytes,
        columns=REQUIRED_DATA_COLUMNS,
    )
    canonical = canonical_scan_blob_bytes(payload)
    observed = {
        "raw_json_sha256": hashlib.sha256(raw).hexdigest(),
        "required_data_csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "canonical_blob_sha256": hashlib.sha256(canonical).hexdigest(),
        "raw_json_bytes": len(raw),
        "required_data_csv_bytes": len(csv_bytes),
        "canonical_blob_bytes": len(canonical),
    }
    if verify and observed != EXPECTED_FIXTURE:
        raise RuntimeError("scan blob fixture does not match checked-in metadata")
    return {
        "raw": raw,
        "csv": csv_bytes,
        "canonical": canonical,
        "observed": observed,
    }


def _legacy_bundle(root: Path, fixture: dict[str, Any]) -> dict[str, int]:
    # Hold both profiles to the same semantic validation boundary.  Comparing
    # the fail-closed CAS path with unchecked file/base64 I/O would measure two
    # different contracts rather than the storage transition.
    build_required_data_scan_blob_payload(
        symbol="FIXTURE",
        market="US",
        raw_json_bytes=fixture["raw"],
        required_data_csv_bytes=fixture["csv"],
        columns=REQUIRED_DATA_COLUMNS,
    )
    raw_path = root / "required_data/raw/FIXTURE_required_data.json"
    csv_path = root / "required_data/parsed/FIXTURE_required_data.csv"
    raw_path.parent.mkdir(parents=True)
    csv_path.parent.mkdir(parents=True)
    raw_path.write_bytes(fixture["raw"])
    csv_path.write_bytes(fixture["csv"])
    bundle = {
        "raw_json_base64": base64.b64encode(fixture["raw"]).decode("ascii"),
        "required_data_csv_base64": base64.b64encode(fixture["csv"]).decode("ascii"),
    }
    receipt = _canonical_bytes(bundle)
    receipt_path = root / "state/required_data_bundle.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt)
    loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
    loaded_raw = base64.b64decode(loaded["raw_json_base64"], validate=True)
    loaded_csv = base64.b64decode(loaded["required_data_csv_base64"], validate=True)
    if loaded_raw != raw_path.read_bytes() or loaded_csv != csv_path.read_bytes():
        raise RuntimeError("legacy required-data roundtrip mismatch")
    build_required_data_scan_blob_payload(
        symbol="FIXTURE",
        market="US",
        raw_json_bytes=loaded_raw,
        required_data_csv_bytes=loaded_csv,
        columns=REQUIRED_DATA_COLUMNS,
    )
    return {
        "legacy_files_bytes": len(fixture["raw"]) + len(fixture["csv"]),
        "legacy_receipt_bytes": len(receipt),
    }


def _canonical_bundle(
    root: Path,
    fixture: dict[str, Any],
    *,
    dual_output: bool,
) -> dict[str, Any]:
    root.mkdir()
    legacy = (
        _legacy_bundle(root, fixture)
        if dual_output
        else {
            "legacy_files_bytes": 0,
            "legacy_receipt_bytes": 0,
        }
    )
    ref = publish_required_data_scan_blob(
        runtime_root=root,
        symbol="FIXTURE",
        market="US",
        raw_json_bytes=fixture["raw"],
        required_data_csv_bytes=fixture["csv"],
        columns=REQUIRED_DATA_COLUMNS,
    )
    manifest = _canonical_bytes(
        {
            "schema_version": "required_data_scan_blob_benchmark_manifest.v1",
            "scan_blob_refs": [ref],
        }
    )
    manifest_path = root / "output_runs/fixture/state/required_data_snapshot_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(manifest)
    loaded_ref = json.loads(manifest_path.read_text(encoding="utf-8"))["scan_blob_refs"][0]
    loaded = load_required_data_scan_blob(runtime_root=root, blob_ref=loaded_ref)
    mismatches: list[str] = []
    if loaded["raw_json_bytes"] != fixture["raw"]:
        mismatches.append("canonical_raw")
    if loaded["required_data_csv_bytes"] != fixture["csv"]:
        mismatches.append("canonical_csv")
    if dual_output:
        raw_path = root / "required_data/raw/FIXTURE_required_data.json"
        csv_path = root / "required_data/parsed/FIXTURE_required_data.csv"
        receipt = json.loads((root / "state/required_data_bundle.json").read_text(encoding="utf-8"))
        checks = (
            (
                "legacy_raw_file",
                required_data_shadow_file_matches(raw_path, loaded["raw_json_bytes"]),
            ),
            (
                "legacy_csv_file",
                required_data_shadow_file_matches(csv_path, loaded["required_data_csv_bytes"]),
            ),
            (
                "legacy_raw_inline",
                required_data_shadow_base64_matches(receipt["raw_json_base64"], loaded["raw_json_bytes"]),
            ),
            (
                "legacy_csv_inline",
                required_data_shadow_base64_matches(
                    receipt["required_data_csv_base64"],
                    loaded["required_data_csv_bytes"],
                ),
            ),
        )
        mismatches.extend(name for name, matched in checks if not matched)
    retained = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    return {
        **legacy,
        "compressed_blob_bytes": ref["compressed_size_bytes"],
        "manifest_bytes": len(manifest),
        "retained_bytes": retained,
        "mismatch_samples": mismatches[:10],
        "mismatch_count": len(mismatches),
    }


def _distribution(samples: list[int]) -> dict[str, Any]:
    ordered = sorted(samples)
    p95 = ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]
    return {
        "unit": "ns",
        "samples": samples,
        "median": int(statistics.median(samples)),
        "p95": p95,
    }


def _measure(
    action: Callable[[Path], dict[str, Any]],
    *,
    warmups: int,
    repetitions: int,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    wall: list[int] = []
    cpu: list[int] = []
    last: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="scan-blob-benchmark-") as raw_tmp:
        base = Path(raw_tmp)
        for index in range(warmups + repetitions):
            started_wall = time.perf_counter_ns()
            started_cpu = time.process_time_ns()
            result = action(base / f"sample-{index:04d}")
            elapsed_wall = time.perf_counter_ns() - started_wall
            elapsed_cpu = time.process_time_ns() - started_cpu
            if index >= warmups:
                wall.append(elapsed_wall)
                cpu.append(elapsed_cpu)
                last = result
        allocation_root = base / "allocation"
        tracemalloc.start()
        action(allocation_root)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return _distribution(wall), _distribution(cpu), peak


def _git_sha() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def run_profile(profile: str, *, warmups: int, repetitions: int) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(PROFILES)}")
    descriptor_sha, violations = _descriptor_status()
    try:
        fixture = generate_fixture()
    except RuntimeError as exc:
        raise RuntimeError("scan blob fixture preflight failed") from exc
    formal = warmups == WARMUPS and repetitions == REPETITIONS
    action = lambda root: _canonical_bundle(
        root,
        fixture,
        dual_output=profile == "dual_output",
    )
    wall, cpu, peak = _measure(action, warmups=warmups, repetitions=repetitions)
    with tempfile.TemporaryDirectory(prefix="scan-blob-evidence-") as raw_tmp:
        evidence = action(Path(raw_tmp) / "runtime")
    legacy_wall = None
    if profile == "canonical":
        measured, _legacy_cpu, _legacy_peak = _measure(
            lambda root: root.mkdir() or _legacy_bundle(root, fixture),
            warmups=warmups,
            repetitions=repetitions,
        )
        legacy_wall = measured
    allocation_limit = max(
        32 * 1024 * 1024 if profile == "canonical" else 48 * 1024 * 1024,
        int(fixture["observed"]["canonical_blob_bytes"] * (2 if profile == "canonical" else 2.5)),
    )
    canonical_limit = evidence["compressed_blob_bytes"] + evidence["manifest_bytes"]
    legacy_representation = evidence["legacy_files_bytes"] + evidence["legacy_receipt_bytes"]
    transition_limit = 2 * max(evidence["compressed_blob_bytes"], legacy_representation) + evidence["manifest_bytes"]
    peak_transition = evidence["retained_bytes"] + evidence["compressed_blob_bytes"]
    if evidence["mismatch_count"] or len(evidence["mismatch_samples"]) > 10:
        violations.append("bounded_shadow_comparison")
    if peak > allocation_limit:
        violations.append("python_peak_allocation_bytes")
    if profile == "canonical" and evidence["retained_bytes"] > canonical_limit:
        violations.append("canonical_persisted_bytes")
    if profile == "dual_output" and peak_transition > transition_limit:
        violations.append("dual_output_transition_bytes")
    if formal and legacy_wall is not None and wall["p95"] > legacy_wall["p95"] * 1.10:
        violations.append("canonical_vs_legacy_p95_wall")
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "run_label": ("acceptance_5_warmups_30_repetitions" if formal else "non_acceptance_smoke"),
        "fixture_contract_sha256": descriptor_sha,
        "fixture": fixture["observed"],
        "environment": {
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "source_git_sha": _git_sha(),
        },
        "timing": {
            "canonical_wall": wall,
            "canonical_cpu": cpu,
            "legacy_wall": legacy_wall,
            "profilers_enabled": False,
            "tracemalloc_enabled": False,
        },
        "python_peak_allocation_bytes": peak,
        "python_peak_allocation_limit_bytes": allocation_limit,
        "space": {
            **evidence,
            "canonical_persisted_limit_bytes": canonical_limit,
            "dual_output_peak_temp_plus_retained_bytes": peak_transition,
            "dual_output_two_representation_limit_bytes": transition_limit,
        },
        "violations": sorted(set(violations)),
    }
    return receipt


def benchmark_exit_code(receipt: dict[str, Any]) -> int:
    return 1 if receipt["violations"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark deterministic p99 required-data CAS payloads.")
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("warmups must be non-negative and repetitions must be positive")
    try:
        receipt = run_profile(
            args.profile,
            warmups=args.warmups,
            repetitions=args.repetitions,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return benchmark_exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
