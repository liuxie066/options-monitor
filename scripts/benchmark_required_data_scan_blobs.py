#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

from src.application.close_advice_quote_cache import publish_quote_cache_metadata
from src.application.opend_symbol_outputs import (
    REQUIRED_DATA_COLUMNS,
    publish_required_data_quote_snapshot,
)
from src.application.required_data_blobs import (
    build_required_data_scan_blob_payload,
    canonical_scan_blob_bytes,
)
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_plan_id,
)
from src.application.required_data_snapshot import (
    resolve_frozen_required_data,
    retire_required_data_snapshot_shadows,
    seal_required_data_snapshot,
)


FIXTURE_SCHEMA = "required_data_scan_blob_benchmark_fixture.v1"
RECEIPT_SCHEMA = "required_data_scan_blob_benchmark.v2"
FIXTURE_PATH = REPO_ROOT / "tests/fixtures/required_data_scan_blob_benchmark_metadata.v1.json"
FIXTURE_CONTRACT_SHA256 = "e6d4c4033a4695bfc3f38298d34be0257d90042e3a1a84907dc9812f9ced1485"
EXPECTED_FIXTURE = {
    "raw_json_sha256": "0520d76e029e6f7447d835cdb0fe77e8cd9f1b554006750c195bd7126d2a58ea",
    "required_data_csv_sha256": "05685c16d0cb17fc1bf6359d51c01be81e96526f38920873afca266f5e709404",
    "canonical_blob_sha256": "df7e782498fec5853515a4fd354fcfae6f13bb775cc80b64d522d5c64a5a6be3",
    "raw_json_bytes": 187_345,
    "required_data_csv_bytes": 39_415,
    "canonical_blob_bytes": 146_955,
}
SEED = 20260816
ROW_COUNT = 254
TARGET_LEGACY_PAIR_BYTES = 226_760
WARMUPS = 5
REPETITIONS = 30
PROFILES = ("canonical",)
OBSERVED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
EXPIRATION = "2026-12-18"
HOST = "127.0.0.1"
PORT = 11111


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
    entropy_class = index % 3
    if entropy_class == 0:
        vendor_detail = "A" * 140
    elif entropy_class == 1:
        vendor_detail = (f"fixture-{index:04d}|" * 20)[:140]
    else:
        vendor_detail = _deterministic_text(f"row-{index}", 140)
    return {
        "symbol": "FIXTURE",
        "market": "US",
        "option_type": "put",
        "expiration": EXPIRATION,
        "dte": 124,
        "contract_symbol": f"FIXTURE261218P{index:08d}",
        "strike": 80 + index / 10,
        "spot": 110.0,
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
    codes = [str(row["contract_symbol"]) for row in rows]
    provider = {
        "symbol": "FIXTURE",
        "underlier_code": "US.FIXTURE",
        "expiration_count": 1,
        "expirations": [EXPIRATION],
        "meta": {
            "status": "ok",
            "source": "opend",
            "host": HOST,
            "port": PORT,
            "trading_date": "2026-08-16",
            "source_outcome": "success_rows",
            "source_observed_at": OBSERVED_AT.isoformat(),
            "completed_at_utc": OBSERVED_AT.isoformat(),
            "snapshot_complete": True,
            "snapshot_requested_codes": ROW_COUNT,
            "snapshot_returned_codes": ROW_COUNT,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": codes,
            "snapshot_returned_code_set": codes,
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
        },
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


def _fetch_plan() -> dict[str, Any]:
    strikes = [float(80 + index / 10) for index in range(ROW_COUNT)]
    side_plan = {
        "option_type": "put",
        "min_dte": 100,
        "max_dte": 140,
        "explicit_expirations": [EXPIRATION],
        "strike_window": {
            "min_strike": strikes[0],
            "max_strike": strikes[-1],
            "source": "fixture",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": strikes[0],
            "base_max_strike": strikes[-1],
        },
        "planning_reason": "fixture",
        "source_fields": ["benchmark"],
        "spot_reference": 110.0,
        "min_strike": strikes[0],
        "max_strike": strikes[-1],
        "expiration_count": 1,
        "required_exact_strikes_by_expiration": {EXPIRATION: strikes},
    }
    return {
        "symbol": "FIXTURE",
        "spot_reference": 110.0,
        "side_plans": [side_plan],
        "merged_requests": [
            {
                "symbol": "FIXTURE",
                "limit_expirations": 8,
                "host": HOST,
                "port": PORT,
                "option_types": ["put"],
                "explicit_expirations": [EXPIRATION],
                "min_dte": 100,
                "max_dte": 140,
                "side_strike_windows": {
                    "put": {
                        "min_strike": strikes[0],
                        "max_strike": strikes[-1],
                    }
                },
                "include_realized_volatility": False,
                "trading_date": "2026-08-16",
                "side_plans": [side_plan],
                "planning_reason": "fixture",
            }
        ],
        "require_realized_volatility": False,
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_rows",
            "reason_code": None,
            "expirations": [EXPIRATION],
            "observed_at_utc": OBSERVED_AT.isoformat(),
            "completed_at_utc": OBSERVED_AT.isoformat(),
            "request_identity": {
                "symbol": "FIXTURE",
                "underlier": "US.FIXTURE",
                "source": "opend",
                "host": HOST,
                "port": PORT,
                "trading_date": "2026-08-16",
            },
            "error": None,
        },
        "projection_outcome": "success_rows",
        "projected_expirations": [EXPIRATION],
    }


def _plan_summary(fetch_plan: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    plan_item = {
        "symbol": "FIXTURE",
        "source": "opend",
        "fetch_binding": dict(contract["fetch_binding"]),
        "fetch_plan": fetch_plan,
        "expected_fetch_contract": contract,
        "projection_outcome": "success_rows",
        "discovery_status": "complete",
    }
    return {
        "schema_version": "1.0",
        "errors": 0,
        "global_required_data_plan": {
            "plan_id": required_data_plan_id([plan_item]),
            "symbols": [plan_item],
            "symbols_count": 1,
            "discovery_complete": True,
        },
        "symbols": [],
        "results": [],
    }


def _canonical_bundle(
    root: Path,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    root.mkdir()
    root = root.resolve()
    run_id = "fixture"
    run_dir = root / "output_runs" / run_id
    required_root = run_dir / "required_data"
    raw_path = required_root / "raw" / "FIXTURE_required_data.json"
    csv_path = required_root / "parsed" / "FIXTURE_required_data.csv"
    raw_path.parent.mkdir(parents=True)
    csv_path.parent.mkdir(parents=True)
    raw_path.write_bytes(fixture["raw"])
    csv_path.write_bytes(fixture["csv"])
    publish_quote_cache_metadata(
        csv_path=csv_path,
        symbol="FIXTURE",
        source="opend",
        source_run_id=run_id,
        observed_at=OBSERVED_AT,
    )
    fetch_plan = _fetch_plan()
    contract = build_required_data_expected_fetch_contract(
        symbol="FIXTURE",
        fetch_plan=fetch_plan,
        source="opend",
        host=HOST,
        port=PORT,
    )
    receipt_path, receipt = publish_required_data_quote_snapshot(
        runtime_root=root,
        producer_root=required_root,
        producer_run_id=run_id,
        symbol="FIXTURE",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy={"source": "opend", "host": HOST, "port": PORT},
        expected_fetch_contract=contract,
        source_observed_at=OBSERVED_AT,
        completed_at=OBSERVED_AT,
        now=OBSERVED_AT,
    )
    manifest_path = run_dir / "state" / "required_data_snapshot_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=required_root,
        run_id=run_id,
        prefetch_summary=_plan_summary(fetch_plan, contract),
        sealed_at=OBSERVED_AT,
    )
    manifest_bytes = manifest_path.read_bytes()
    cleanup = {
        "removed_files": 0,
        "removed_bytes": 0,
        "absent_files": 0,
        "failed_files": 0,
    }
    cleanup = retire_required_data_snapshot_shadows(
        manifest_path=manifest_path,
        required_data_root=required_root,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
    )
    resolved = resolve_frozen_required_data(
        manifest_path=manifest_path,
        expected_run_id=run_id,
        symbol="FIXTURE",
        required_data_root=required_root,
        now=OBSERVED_AT,
    )
    ref = manifest["symbols"]["FIXTURE"]["scan_blob_ref"]
    mismatches: list[str] = []
    if resolved["read_source"] != "canonical_blob":
        mismatches.append("canonical_read_source")
    if cleanup != {
        "removed_files": 2,
        "removed_bytes": len(fixture["raw"]) + len(fixture["csv"]),
        "absent_files": 0,
        "failed_files": 0,
    }:
        mismatches.append("canonical_cleanup")
    if raw_path.exists():
        mismatches.append("canonical_raw_shadow")
    if csv_path.exists():
        mismatches.append("canonical_csv_shadow")
    payload_path = required_root / str(receipt["payload_relpath"])
    compact_receipt_bytes = receipt_path.stat().st_size + payload_path.stat().st_size
    cache_metadata_bytes = sum(path.stat().st_size for path in required_root.glob("parsed/*_required_data.meta.json"))
    retained = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    return {
        "compressed_blob_bytes": ref["compressed_size_bytes"],
        "manifest_bytes": len(manifest_bytes),
        "compact_receipt_bytes": compact_receipt_bytes,
        "cache_metadata_bytes": cache_metadata_bytes,
        **cleanup,
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
    measure_allocation: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], int, dict[str, Any]]:
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
        peak = 0
        if measure_allocation:
            allocation_root = base / "allocation"
            tracemalloc.start()
            action(allocation_root)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
    return _distribution(wall), _distribution(cpu), peak, last


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
    action = lambda root: _canonical_bundle(root, fixture)
    wall, cpu, peak, evidence = _measure(
        action,
        warmups=warmups,
        repetitions=repetitions,
        measure_allocation=formal,
    )
    allocation_limit = max(
        32 * 1024 * 1024,
        2 * fixture["observed"]["canonical_blob_bytes"],
    )
    canonical_limit = (
        evidence["compressed_blob_bytes"]
        + evidence["manifest_bytes"]
        + evidence["compact_receipt_bytes"]
        + evidence["cache_metadata_bytes"]
    )
    if evidence["mismatch_count"] or len(evidence["mismatch_samples"]) > 10:
        violations.append("bounded_shadow_comparison")
    if peak > allocation_limit:
        violations.append("python_peak_allocation_bytes")
    if evidence["retained_bytes"] > canonical_limit:
        violations.append("canonical_persisted_bytes")
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
            "profilers_enabled": False,
            "tracemalloc_enabled": formal,
        },
        "python_peak_allocation_bytes": peak,
        "python_peak_allocation_limit_bytes": allocation_limit,
        "space": {
            **evidence,
            "canonical_persisted_limit_bytes": canonical_limit,
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
