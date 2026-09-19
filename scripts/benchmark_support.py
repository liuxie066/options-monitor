"""Shared deterministic instrumentation for the ledger storage benchmarks.

This module owns the fixture and measurement primitives that more than one
benchmark script depends on: synthetic trade-event construction, distribution
statistics, schema constants, host identity, SQLite sizing, and the temporary
SQLite ledger fixtures (position-projection and lifecycle-attempt) used to build
deterministic synthetic stores.

`scripts/benchmark_data_storage_projection.py` and
`scripts/benchmark_current_decision_projection_slice2.py` both import from here
so the synthetic fixture definition stays single-sourced. This module is
offline-only instrumentation: it never opens a runtime ledger, never applies a
migration to a runtime store, and never enables checkpoint mode.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from typing import Any, Iterator, Mapping, Sequence
import uuid
import zlib


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import (
    apply_position_projection_migration,
    attach_settlement_semantics,
    build_lifecycle_attempt_audit_envelope,
    build_position_projection_migration_inventory,
    compute_lifecycle_attempt_chain_sha256,
    open_position_ledger,
    trade_event_application_payload,
)


FIXTURE_SCHEMA = "data_storage_projection_fixture.v1"
MAX_HISTORY_EVENTS = 20_000
MAX_CURRENT_STATE_LOTS = 5_000
MAX_ACCOUNTS = 50
MIN_PAYLOAD_BYTES = 256
MAX_PAYLOAD_BYTES = 4_096
LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA = "data_storage_lifecycle_attempt_benchmark.v1"
LIFECYCLE_RECEIPT_BYTES = 64 * 1024


def _bounded_nonnegative_int(value: Any, *, name: str, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 0 or result > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return result


def _bounded_positive_int(value: Any, *, name: str, maximum: int) -> int:
    result = _bounded_nonnegative_int(value, name=name, maximum=maximum)
    if result == 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _scenario_spec(
    *,
    key: str,
    axis: str,
    event_count: int,
    lot_count: int,
    account_count: int,
    payload_bytes: int,
    shape: str,
    axis_status: str,
    classification: str,
    requested_event_count: int | None = None,
    requested_lot_count: int | None = None,
    requested_account_count: int | None = None,
) -> dict[str, Any]:
    if shape == "open_close_pairs":
        projected_lots = event_count // 2
        open_lots = 0
        risk_views = 0
        allocations = event_count // 2
    else:
        projected_lots = lot_count
        open_lots = lot_count
        risk_views = lot_count
        allocations = 0
    return {
        "key": key,
        "axis": axis,
        "shape": shape,
        "classification": classification,
        "axis_status": axis_status,
        "requested_dimensions": {
            "event_count": int(requested_event_count if requested_event_count is not None else event_count),
            "projected_lot_count": int(requested_lot_count if requested_lot_count is not None else projected_lots),
            "account_count": int(requested_account_count if requested_account_count is not None else account_count),
            "payload_bytes": int(payload_bytes),
        },
        "effective_dimensions": {
            "event_count": int(event_count),
            "projected_lot_count": int(projected_lots),
            "open_lot_count": int(open_lots),
            "risk_view_count": int(risk_views),
            "allocation_count": int(allocations),
            "account_count": int(account_count),
            "payload_bytes": int(payload_bytes),
        },
    }


def _build_synthetic_events(spec: Mapping[str, Any], *, seed: int) -> list[dict[str, Any]]:
    dims = spec.get("effective_dimensions")
    if not isinstance(dims, Mapping):
        raise ValueError("scenario effective_dimensions are missing")
    event_count = _bounded_positive_int(dims.get("event_count"), name="event_count", maximum=MAX_HISTORY_EVENTS)
    lot_count = _bounded_nonnegative_int(
        dims.get("projected_lot_count"),
        name="projected_lot_count",
        maximum=MAX_CURRENT_STATE_LOTS * 2,
    )
    account_count = _bounded_positive_int(dims.get("account_count"), name="account_count", maximum=MAX_ACCOUNTS)
    payload_bytes = _bounded_positive_int(dims.get("payload_bytes"), name="payload_bytes", maximum=MAX_PAYLOAD_BYTES)
    key = str(spec.get("key") or "").strip()
    shape = str(spec.get("shape") or "").strip()
    if not key or shape not in {"fixed_open_lots_with_verifications", "open_close_pairs"}:
        raise ValueError("scenario key or shape is invalid")
    events: list[dict[str, Any]] = []
    if shape == "open_close_pairs":
        pair_count = event_count // 2
        for pair_index in range(pair_count):
            open_event = _synthetic_event(
                scenario_key=key,
                sequence=len(events),
                lot_index=pair_index,
                account_index=pair_index % account_count,
                event_type="open",
                target_lot_id=None,
                payload_bytes=payload_bytes,
                seed=seed,
            )
            events.append(open_event)
            events.append(
                _synthetic_event(
                    scenario_key=key,
                    sequence=len(events),
                    lot_index=pair_index,
                    account_index=pair_index % account_count,
                    event_type="close",
                    target_lot_id=str(open_event["lot_id"]),
                    payload_bytes=payload_bytes,
                    seed=seed,
                )
            )
        if len(events) < event_count:
            events.append(
                _synthetic_event(
                    scenario_key=key,
                    sequence=len(events),
                    lot_index=0,
                    account_index=0,
                    event_type="verification",
                    target_lot_id=None,
                    payload_bytes=payload_bytes,
                    seed=seed,
                )
            )
    else:
        if lot_count > event_count:
            raise ValueError("fixed-output fixture cannot have more lots than events")
        for lot_index in range(lot_count):
            events.append(
                _synthetic_event(
                    scenario_key=key,
                    sequence=len(events),
                    lot_index=lot_index,
                    account_index=lot_index % account_count,
                    event_type="open",
                    target_lot_id=None,
                    payload_bytes=payload_bytes,
                    seed=seed,
                )
            )
        while len(events) < event_count:
            sequence = len(events)
            events.append(
                _synthetic_event(
                    scenario_key=key,
                    sequence=sequence,
                    lot_index=sequence % max(1, lot_count),
                    account_index=sequence % account_count,
                    event_type="verification",
                    target_lot_id=None,
                    payload_bytes=payload_bytes,
                    seed=seed,
                )
            )
    if len(events) != event_count:
        raise AssertionError("synthetic fixture cardinality mismatch")
    return events


def _synthetic_event(
    *,
    scenario_key: str,
    sequence: int,
    lot_index: int,
    account_index: int,
    event_type: str,
    target_lot_id: str | None,
    payload_bytes: int,
    seed: int,
) -> dict[str, Any]:
    slug = scenario_key.replace(".", "-").replace("_", "-")
    event_id = f"bench-{slug}-{sequence:06d}-{event_type}"
    lot_id = f"lot-{slug}-{lot_index:06d}"
    entropy_class = ("low", "median", "high")[sequence % 3]
    phase_3a_call = scenario_key.startswith("phase_3a.") and lot_index % 2 == 1
    raw_payload = {
        "benchmark_schema": FIXTURE_SCHEMA,
        "fixture_seed": int(seed),
        "entropy_class": entropy_class,
        "synthetic_filler": _deterministic_filler(
            seed=seed,
            scenario_key=scenario_key,
            sequence=sequence,
            entropy_class=entropy_class,
            size=payload_bytes,
        ),
        "source_type": "synthetic_benchmark",
        "side": (
            "sell"
            if event_type == "close" and phase_3a_call
            else "buy"
            if event_type == "close"
            else "buy"
            if phase_3a_call
            else "sell"
        ),
    }
    if (
        scenario_key == "phase_3a.runtime"
        and event_type == "open"
        and lot_index == 0
    ):
        raw_payload.update(
            strategy="combo_yield",
            leg_role="funding_put",
            strategy_group_id="bench-special-combo",
            strategy_snapshot={"schema_version": "benchmark_strategy_snapshot.v1"},
        )
    if event_type == "close":
        raw_payload["close_type"] = "buy_to_close"
    contract_key = ContractKey.from_values(
        broker="futu",
        account=f"bench{account_index:02d}",
        underlying_symbol="NVDA",
        option_type="call" if phase_3a_call else "put",
        position_side="long" if phase_3a_call else "short",
        strike=(20.0 if phase_3a_call else 10.0) + (lot_index * 0.01),
        expiration_ymd="2028-12-15",
    )
    event = TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=1_800_000_000_000 + sequence,
        contract_key=contract_key,
        contracts=1 if event_type in {"open", "close"} else 0,
        price=2.0 if event_type == "open" else 0.5 if event_type == "close" else 0.0,
        currency="USD",
        source="synthetic_benchmark",
        multiplier=100.0,
        fees=0.0,
        target_lot_id=target_lot_id,
        lot_id=lot_id if event_type == "open" else None,
        raw_payload=raw_payload,
    )
    return trade_event_application_payload(event.to_dict())


def _deterministic_filler(
    *,
    seed: int,
    scenario_key: str,
    sequence: int,
    entropy_class: str,
    size: int,
) -> str:
    target = max(1, int(size))
    if entropy_class == "low":
        return "L" * target
    token = hashlib.sha256(f"{seed}:{scenario_key}:{sequence}".encode("utf-8")).digest()
    if entropy_class == "median":
        alphabet = base64.b32encode(token).decode("ascii").rstrip("=")
        chunk = f"{alphabet[:8]}:{sequence % 97:02d}|"
    else:
        chunks: list[str] = []
        produced = 0
        block = 0
        while produced < target:
            digest = hashlib.sha256(token + block.to_bytes(4, "big")).digest()
            encoded = base64.b85encode(digest).decode("ascii")
            chunks.append(encoded)
            produced += len(encoded)
            block += 1
        return "".join(chunks)[:target]
    repeats = math.ceil(target / len(chunk))
    return (chunk * repeats)[:target]


def _event_payload_metrics(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    sizes: list[int] = []
    ratios: list[float] = []
    class_rows: dict[str, list[tuple[int, float]]] = {"low": [], "median": [], "high": []}
    compressed_total = 0
    for event in events:
        encoded = _canonical_json_bytes(event)
        compressed = zlib.compress(encoded, level=6)
        size = len(encoded)
        ratio = len(compressed) / max(1, size)
        entropy = str((event.get("raw_payload") or {}).get("entropy_class") or "unknown")
        sizes.append(size)
        ratios.append(ratio)
        compressed_total += len(compressed)
        class_rows.setdefault(entropy, []).append((size, ratio))
    return {
        "uncompressed_bytes": _distribution(sizes),
        "compressed_bytes_total_individual_rows": compressed_total,
        "compression_ratio": _float_distribution(ratios),
        "entropy_classes": {
            name: {
                "row_count": len(rows),
                "uncompressed_bytes": _distribution([row[0] for row in rows]),
                "compression_ratio": _float_distribution([row[1] for row in rows]),
            }
            for name, rows in sorted(class_rows.items())
            if rows
        },
    }


def _distribution(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {"count": 0, "total": 0, "min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0}
    ordered = sorted(int(value) for value in values)
    return {
        "count": len(ordered),
        "total": sum(ordered),
        "min": ordered[0],
        "p50": _nearest_rank(ordered, 0.50),
        "p95": _nearest_rank(ordered, 0.95),
        "p99": _nearest_rank(ordered, 0.99),
        "max": ordered[-1],
    }


def _float_distribution(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 6),
        "p50": round(float(_nearest_rank(ordered, 0.50)), 6),
        "p95": round(float(_nearest_rank(ordered, 0.95)), 6),
        "p99": round(float(_nearest_rank(ordered, 0.99)), 6),
        "max": round(ordered[-1], 6),
    }


def _nearest_rank(ordered: Sequence[Any], percentile: float) -> Any:
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _events_sha256(events: Sequence[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, event in enumerate(events):
        if index:
            digest.update(b",")
        digest.update(_canonical_json_bytes(event))
    digest.update(b"]")
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _host_profile() -> dict[str, Any]:
    cpu_model, hardware_model = _hardware_identity()
    fields = {
        "schema_version": "data_storage_projection_host_profile.v1",
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "hardware_model": hardware_model,
        "physical_memory_bytes": _physical_memory_bytes(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "logical_cpu_count": int(os.cpu_count() or 0),
    }
    return {**fields, "fingerprint": _sha256_json(fields)}


def _hardware_identity() -> tuple[str, str]:
    system = platform.system()
    if system == "Darwin":
        cpu_model = _command_value(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"])
        hardware_model = _command_value(["/usr/sbin/sysctl", "-n", "hw.model"])
        if not cpu_model or not hardware_model:
            details = _darwin_hardware_details()
            cpu_model = cpu_model or details.get("chip_type")
            hardware_model = hardware_model or details.get("machine_model")
        return (
            cpu_model or platform.processor() or "unknown",
            hardware_model or platform.machine() or "unknown",
        )
    if system == "Linux":
        cpu_model = None
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    value = line.split(":", 1)[1].strip()
                    if value:
                        cpu_model = value
                        break
        except (OSError, UnicodeError):
            pass
        hardware_model = _bounded_text_file(Path("/sys/devices/virtual/dmi/id/product_name"))
        return (
            cpu_model or platform.processor() or "unknown",
            hardware_model or platform.machine() or "unknown",
        )
    return (
        platform.processor() or "unknown",
        platform.machine() or "unknown",
    )


def _command_value(command: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _darwin_hardware_details() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["/usr/sbin/system_profiler", "SPHardwareDataType", "-json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    rows = payload.get("SPHardwareDataType") if isinstance(payload, Mapping) else None
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else {}
    return {
        key: str(row.get(key) or "").strip()
        for key in ("chip_type", "machine_model")
        if str(row.get(key) or "").strip()
    }


def _bounded_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > 4_096:
            return None
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _physical_memory_bytes() -> int | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    total = page_size * page_count
    return total if total > 0 else None


def _git_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _insert_phase_3a_events(repo: Any, events: Sequence[dict[str, Any]]) -> None:
    conn = repo._connect()
    try:
        for event in events:
            repo.upsert_trade_event(event, conn=conn)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


@contextmanager
def _temporary_phase_3a_base(
    spec: Mapping[str, Any],
    *,
    seed: int,
) -> Iterator[dict[str, Any]]:
    events = _build_synthetic_events(spec, seed=seed)
    with tempfile.TemporaryDirectory(prefix="om-phase3a-base-") as temp_name:
        root = Path(temp_name)
        data_config = root / "data.json"
        data_config.write_text("{}\n", encoding="utf-8")
        repo = open_position_ledger(data_config)
        _insert_phase_3a_events(repo, events)
        inventory = build_position_projection_migration_inventory(repo.db_path)
        apply_result = apply_position_projection_migration(repo.db_path, inventory)
        conn = repo._connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        db_path = Path(repo.db_path)
        yield {
            "repo": repo,
            "db_path": db_path,
            "fixture_sha256": _events_sha256(events),
            "sqlite_sha256": _file_sha256(db_path),
            "spec": dict(spec),
            "apply": apply_result,
        }


def _phase_3a_tail_events(*, count: int, payload_bytes: int = 256) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    key = ContractKey.from_values(
        broker="futu",
        account="bench00",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=10,
        expiration_ymd="2028-12-15",
    )
    for index in range(int(count)):
        event = TradeEvent(
            event_id=f"bench-phase3a-tail-{payload_bytes}-{index:06d}",
            event_type="verification",
            event_time_ms=1_850_000_000_000 + index,
            contract_key=key,
            contracts=0,
            price=0,
            currency="USD",
            source="synthetic_benchmark",
            multiplier=100,
            raw_payload={
                "source_type": "synthetic_benchmark",
                "synthetic_filler": "R" * max(1, int(payload_bytes)),
            },
        )
        events.append(trade_event_application_payload(event.to_dict()))
    return events


def _timing_distribution(samples: Sequence[int]) -> dict[str, Any]:
    ordered = sorted(int(value) for value in samples)
    if not ordered:
        raise ValueError("timing samples are empty")
    return {
        "unit": "ns",
        "sample_count": len(ordered),
        "median": int(statistics.median(ordered)),
        "p95": int(_nearest_rank(ordered, 0.95)),
        "min": ordered[0],
        "max": ordered[-1],
        "samples": list(samples),
    }


def _sqlite_sizes(db_path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for label, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm")):
        path = Path(str(db_path) + suffix)
        try:
            result[f"{label}_bytes"] = int(path.stat().st_size)
        except FileNotFoundError:
            result[f"{label}_bytes"] = 0
    result["total_bytes"] = sum(result.values())
    return result


def _lifecycle_benchmark_invocation(index: int) -> bytes:
    return uuid.UUID(int=int(index), version=4).bytes


def _lifecycle_benchmark_observation(
    *,
    target_bytes: int,
    nonce: int,
    seed: int,
) -> dict[str, Any]:
    target = _bounded_positive_int(
        target_bytes,
        name="lifecycle receipt bytes",
        maximum=4 * 1024 * 1024,
    )

    def materialize(padding: str) -> dict[str, Any]:
        return attach_settlement_semantics(
            {
                "schema_version": "broker_settlement_observation.v2",
                "case_id": "benchmark-case",
                "account": "lx",
                "futu_account_id": "1001",
                "market": "US",
                "contract_identity": {
                    "symbol": "NVDA",
                    "option_contract_code": "US.NVDA280121P100000",
                    "option_type": "put",
                    "position_side": "short",
                    "strike": "100.00",
                    "expiration_ymd": "2028-01-21",
                    "multiplier": 100,
                },
                "target_contracts_by_lot": {"benchmark-lot": 1},
                "frozen_preterminal_remaining_by_lot": {"benchmark-lot": 0},
                "anchor_option_deal_key": "futu:lx:1001:benchmark-deal",
                "anchor_execution_time_ms": 1_900_000_000_000,
                "observed_at_ms": 1_900_000_001_000,
                "settlement_deadline_ms": 1_900_000_000_500,
                "required_sources": ["anchor_option_close"],
                "source_receipts": {
                    "anchor_option_close": {
                        "status": "complete",
                        "coverage_complete": True,
                        "pagination_complete": True,
                        "rows": [],
                    }
                },
                "stock_settlement_candidates": [],
                "broker_option_position_absent": True,
                "projection_matches_frozen_remaining": True,
                "reservation_exclusive": True,
                "competing_effective_consumption": False,
                "stock_settlement_present": False,
                "normal_order_present": False,
                "complete": True,
                "incomplete_reason_codes": [],
                "benchmark_receipt_nonce": f"{int(nonce):08d}",
                "benchmark_receipt_padding": padding,
            },
            evidence_kind="expire_close",
        )

    observation = materialize("")
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id="benchmark-case",
        invocation_id=_lifecycle_benchmark_invocation(1),
        attempted_at_ms=1_900_000_001_000,
        outcome_kind="observed_complete",
        observation=observation,
    )
    padding_bytes = target - int(envelope.receipt_uncompressed_bytes or 0)
    if padding_bytes < 0:
        raise ValueError("lifecycle receipt target is smaller than the canonical fixture")
    padding = _deterministic_filler(
        seed=seed,
        scenario_key="lifecycle-attempt-receipt",
        sequence=0,
        entropy_class="high",
        size=max(1, padding_bytes),
    )[:padding_bytes]
    for _attempt in range(4):
        observation = materialize(padding)
        envelope = build_lifecycle_attempt_audit_envelope(
            case_id="benchmark-case",
            invocation_id=_lifecycle_benchmark_invocation(1),
            attempted_at_ms=1_900_000_001_000,
            outcome_kind="observed_complete",
            observation=observation,
        )
        delta = target - int(envelope.receipt_uncompressed_bytes or 0)
        if delta == 0:
            return observation
        if len(padding) + delta < 0:
            break
        if delta > 0:
            padding += _deterministic_filler(
                seed=seed + len(padding),
                scenario_key="lifecycle-attempt-receipt-tail",
                sequence=0,
                entropy_class="high",
                size=delta,
            )[:delta]
        else:
            padding = padding[:delta]
    raise RuntimeError("failed to build exact-size lifecycle receipt fixture")


def _checkpoint_lifecycle_repo(repo: Any) -> dict[str, int]:
    conn = repo._connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return _sqlite_sizes(Path(repo.db_path))


def _append_lifecycle_benchmark_envelope(
    repo: Any,
    *,
    envelope: Any,
    first_evidence_id: str | None = "benchmark-evidence",
    trace: list[str] | None = None,
) -> dict[str, Any]:
    conn = repo._connect()
    try:
        if trace is not None:
            conn.set_trace_callback(trace.append)
        conn.execute("BEGIN IMMEDIATE")
        result = repo.append_trade_lifecycle_attempt_audit_in_transaction(
            attempt_audit=envelope,
            first_evidence_id=first_evidence_id,
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    cleanup_hash = result.pop("_cleanup_receipt_sha256", None)
    if cleanup_hash is not None:
        cleanup = repo._connect()
        try:
            if trace is not None:
                cleanup.set_trace_callback(trace.append)
            cleanup.execute("BEGIN IMMEDIATE")
            repo.delete_unreferenced_trade_lifecycle_receipt_blob(
                cleanup_hash,
                conn=cleanup,
            )
            cleanup.commit()
        except Exception:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()
    return result


@contextmanager
def _temporary_lifecycle_attempt_fixture(
    *,
    prior_attempts: int,
    receipt_bytes: int,
    seed: int,
) -> Iterator[dict[str, Any]]:
    attempt_count = _bounded_positive_int(
        prior_attempts,
        name="lifecycle prior attempts",
        maximum=1_000_000,
    )
    observation = _lifecycle_benchmark_observation(
        target_bytes=receipt_bytes,
        nonce=0,
        seed=seed,
    )
    with tempfile.TemporaryDirectory(prefix="om-lifecycle-attempt-") as temp_name:
        root = Path(temp_name)
        data_config = root / "data.json"
        data_config.write_text("{}\n", encoding="utf-8")
        repo = open_position_ledger(data_config)
        repo.upsert_trade_lifecycle_case(
            {
                "case_id": "benchmark-case",
                "case_key": "benchmark-case",
                "account": "lx",
                "symbol": "NVDA",
                "status": "waiting_settlement_evidence",
            }
        )
        repo.insert_trade_lifecycle_evidence_once(
            {
                "evidence_id": "benchmark-evidence",
                "case_id": "benchmark-case",
                "source_type": "broker_settlement_observation",
                "evidence_type": "expire_close",
                "account": "lx",
                "symbol": "NVDA",
                "semantic_schema": observation["semantic_schema"],
                "semantic_fingerprint": observation["semantic_fingerprint"],
                "semantic_projection": observation["semantic_projection"],
                "observation": observation,
            }
        )
        conn = repo._connect()
        try:
            evidence_created_at_ms = int(
                conn.execute(
                    "SELECT created_at_ms FROM trade_lifecycle_evidence "
                    "WHERE evidence_id = 'benchmark-evidence'"
                ).fetchone()[0]
            )
        finally:
            conn.close()
        repo.upsert_trade_lifecycle_settlement_admission_head(
            case_id="benchmark-case",
            semantic_schema=str(observation["semantic_schema"]),
            semantic_fingerprint=str(observation["semantic_fingerprint"]),
            evidence_id="benchmark-evidence",
            evidence_created_at_ms=evidence_created_at_ms,
            updated_at_ms=1_900_000_001_000,
        )
        baseline_sqlite = _checkpoint_lifecycle_repo(repo)
        first = build_lifecycle_attempt_audit_envelope(
            case_id="benchmark-case",
            invocation_id=_lifecycle_benchmark_invocation(1),
            attempted_at_ms=1_900_000_001_000,
            outcome_kind="observed_complete",
            observation=observation,
        )
        first_result = _append_lifecycle_benchmark_envelope(
            repo,
            envelope=first,
        )
        chain = bytes.fromhex(str(first_result["audit_chain_sha256"]))
        if attempt_count > 1:
            head = repo.get_trade_lifecycle_attempt_audit_head(
                case_id="benchmark-case"
            )
            if head is None:
                raise RuntimeError("lifecycle benchmark head was not created")
            audit_case_key = int(head["audit_case_key"])
            conn = repo._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows: list[tuple[Any, ...]] = []
                for ordinal in range(2, attempt_count + 1):
                    invocation = _lifecycle_benchmark_invocation(ordinal)
                    attempted_at_ms = 1_900_000_001_000 + ordinal - 1
                    chain = compute_lifecycle_attempt_chain_sha256(
                        previous_chain_sha256=chain,
                        case_id="benchmark-case",
                        ordinal=ordinal,
                        invocation_id=invocation,
                        attempted_at_ms=attempted_at_ms,
                        outcome_code=first.outcome_code,
                        semantic_fingerprint=first.semantic_fingerprint,
                        receipt_sha256=first.receipt_sha256,
                        diagnostic_sha256=None,
                    )
                    rows.append(
                        (
                            audit_case_key,
                            ordinal,
                            invocation,
                            attempted_at_ms,
                            first.outcome_code,
                            first.semantic_fingerprint,
                            first.receipt_sha256,
                            1,
                        )
                    )
                    if len(rows) == 5_000:
                        conn.executemany(
                            "INSERT INTO trade_lifecycle_attempt_audits "
                            "(audit_case_key,ordinal,invocation_id,attempted_at_ms,"
                            "outcome_code,semantic_fingerprint,receipt_sha256,span_ordinal) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            rows,
                        )
                        rows.clear()
                if rows:
                    conn.executemany(
                        "INSERT INTO trade_lifecycle_attempt_audits "
                        "(audit_case_key,ordinal,invocation_id,attempted_at_ms,"
                        "outcome_code,semantic_fingerprint,receipt_sha256,span_ordinal) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        rows,
                    )
                conn.execute(
                    "UPDATE trade_lifecycle_observation_spans "
                    "SET last_success_ordinal=?,last_success_at_ms=?,"
                    "successful_observation_count=? "
                    "WHERE audit_case_key=? AND span_ordinal=1",
                    (
                        attempt_count,
                        1_900_000_001_000 + attempt_count - 1,
                        attempt_count,
                        audit_case_key,
                    ),
                )
                conn.execute(
                    "UPDATE trade_lifecycle_attempt_audit_heads "
                    "SET last_ordinal=?,chain_sha256=?,last_invocation_id=?,updated_at_ms=? "
                    "WHERE audit_case_key=?",
                    (
                        attempt_count,
                        chain,
                        _lifecycle_benchmark_invocation(attempt_count),
                        1_900_000_001_000 + attempt_count - 1,
                        audit_case_key,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        fixture_sqlite = _checkpoint_lifecycle_repo(repo)
        keeper = repo._connect()
        try:
            yield {
                "root": root,
                "repo": repo,
                "db_path": Path(repo.db_path),
                "observation": observation,
                "baseline_sqlite": baseline_sqlite,
                "fixture_sqlite": fixture_sqlite,
                "fixture_sha256": _sha256_json(
                    {
                        "schema_version": LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA,
                        "seed": seed,
                        "prior_attempts": attempt_count,
                        "receipt_bytes": receipt_bytes,
                        "chain_sha256": chain.hex(),
                    }
                ),
            }
        finally:
            keeper.close()
