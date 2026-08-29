from __future__ import annotations

import argparse
import base64
import cProfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pstats
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid
import zlib


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.domain.combo_identity import build_combo_identity_intent
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import (
    attach_settlement_semantics,
    apply_position_projection_migration,
    build_position_projection_migration_inventory,
    build_lifecycle_attempt_audit_envelope,
    compute_projector_implementation_fingerprint,
    compute_lifecycle_attempt_chain_sha256,
    LEDGER_DB_RELATIVE_PATH,
    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
    loaded_projector_implementation_fingerprint,
    open_position_ledger,
    project_trade_event_log as project_stored_trade_events_to_position_lots,
    read_current_position_projection,
    record_combo_trade_open,
    record_manual_position_adjustments,
    record_manual_position_close,
    refresh_position_lot_projection as rebuild_position_lots_from_trade_events,
    trade_event_application_payload,
)
from src.application.research.storage_baseline import collect_storage_runtime_baseline

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on some supported platforms
    resource = None  # type: ignore[assignment]


FIXTURE_SCHEMA = "data_storage_projection_fixture.v1"
TIMING_SCHEMA = "data_storage_projection_timing.v1"
CPU_PROFILE_SCHEMA = "data_storage_projection_cpu_profile.v1"
ALLOCATION_PROFILE_SCHEMA = "data_storage_projection_allocation_profile.v1"
DECISION_SCHEMA = "data_storage_projection_gate_decision.v1"
PHASE_3A_ACCEPTANCE_SCHEMA = "data_storage_projection_phase3a_acceptance.v1"
PHASE_3A_FIXTURE_SCHEMA = "data_storage_projection_phase3a_fixture.v1"
WORKER_SPEC_SCHEMA = "data_storage_projection_worker_spec.v1"
SEED = 20260813
DEFAULT_WARMUPS = 5
DEFAULT_REPETITIONS = 30
DEFAULT_CURRENT_EVENTS = 100
DEFAULT_CURRENT_LOTS = 50
DEFAULT_ACCOUNT_COUNT = 2
DEFAULT_PAYLOAD_BYTES = 768
MIN_HISTORY_EVENTS = 10_000
MAX_HISTORY_EVENTS = 20_000
MAX_CURRENT_EVENTS = 10_000
MAX_CURRENT_LOTS = 500
MAX_CURRENT_STATE_LOTS = 5_000
MAX_ACCOUNTS = 50
MIN_PAYLOAD_BYTES = 256
MAX_PAYLOAD_BYTES = 4_096
WALL_LIMIT_NS = 2_000_000_000
CPU_LIMIT_NS = 1_500_000_000
STORAGE_STATUS_KEY = "research_storage_status.history_10x"
STORAGE_STATUS_PARTITION_COUNT = 10_000
STORAGE_STATUS_WALL_LIMIT_NS = 5_000_000_000
STORAGE_STATUS_ALLOCATION_LIMIT_BYTES = 64 * 1024 * 1024
STORAGE_STATUS_OBSERVED_AT = datetime(2026, 8, 13, tzinfo=timezone.utc)
PHASE_3A_EVENT_COUNT = 10_000
PHASE_3A_LOT_COUNT = 100
PHASE_3A_STATE_10X_LOT_COUNT = 1_000
PHASE_3A_WALL_LIMIT_NS = 500_000_000
PHASE_3A_CPU_LIMIT_NS = 500_000_000
PHASE_3A_READ_CURRENT_LIMIT_NS = 50_000_000
PHASE_3A_READ_STATE_10X_LIMIT_NS = 200_000_000
PHASE_3A_INVALIDATION_LIMIT_NS = 50_000_000
PHASE_3A_FINGERPRINT_STARTUP_LIMIT_NS = 50_000_000
PHASE_3A_ALLOCATION_FLOOR_BYTES = 64 * 1024 * 1024
PHASE_3A_READ_ALLOCATION_LIMIT_BYTES = 16 * 1024 * 1024
PHASE_3A_FINGERPRINT_ALLOCATION_LIMIT_BYTES = 8 * 1024 * 1024
LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA = "data_storage_lifecycle_attempt_benchmark.v1"
LIFECYCLE_ATTEMPT_ARTIFACT_FILENAME = "lifecycle-attempt-audit.json"
LIFECYCLE_ATTEMPT_COUNT = 100_000
LIFECYCLE_RECEIPT_BYTES = 64 * 1024
LIFECYCLE_P99_RECEIPT_BYTES = 55_759
LIFECYCLE_MOVE_COUNT = 1_000
LIFECYCLE_WALL_LIMIT_NS = 25_000_000
LIFECYCLE_BYTES_PER_ATTEMPT_LIMIT = 224
LIFECYCLE_ALLOCATION_FLOOR_BYTES = 8 * 1024 * 1024
LIFECYCLE_MOVE_WAL_FLOOR_BYTES = 1 * 1024 * 1024
LIFECYCLE_MOVE_PEAK_FLOOR_BYTES = 64 * 1024 * 1024
ARTIFACT_FILENAMES = (
    "fixture-manifest.json",
    "timing.json",
    "cpu-profile.json",
    "allocation-profile.json",
    "decision.json",
    "phase-3a-acceptance.json",
)
PUBLIC_SCENARIOS = (
    "all",
    "current_scale",
    "history_10x",
    "current_state_10x",
    "account_fanout",
    "research_storage_status",
    "phase_3a",
)


@dataclass(frozen=True)
class BaselineDimensions:
    event_count: int
    current_lot_count: int
    account_count: int
    payload_bytes: int
    dimension_source: str
    requested: dict[str, int]
    clamped: dict[str, dict[str, int]]
    metadata: dict[str, Any]


def run_data_storage_projection_benchmark(
    *,
    repo_root: str | Path,
    output_dir: str | Path,
    baseline: str | Path | None = None,
    scenario: str = "all",
    warmups: int = DEFAULT_WARMUPS,
    repetitions: int = DEFAULT_REPETITIONS,
    seed: int = SEED,
    reference_host_fingerprint: str | None = None,
    shadow_manifest: str | Path | Mapping[str, Any] | None = None,
    worker_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build deterministic fixtures and publish a complete local evidence set.

    The benchmark consumes only aggregate metadata from an optional Slice 1
    report. Every SQLite file opened by the measured writer is created below a
    private temporary directory owned by the worker process.
    """

    base = Path(repo_root).expanduser().resolve(strict=True)
    selected = _selected_scenarios(scenario)
    warmup_count = _bounded_nonnegative_int(warmups, name="warmups", maximum=100)
    repetition_count = _bounded_positive_int(repetitions, name="repetitions", maximum=1_000)
    fixture_seed = _bounded_nonnegative_int(seed, name="seed", maximum=2**31 - 1)
    reference_fingerprint = _validated_reference_fingerprint(reference_host_fingerprint)
    dimensions = _load_baseline_dimensions(baseline, repo_root=base)
    projection_scenarios = tuple(
        item for item in selected if item != "research_storage_status"
    )
    scenario_specs = _build_scenario_specs(dimensions, selected=projection_scenarios)
    storage_status_spec = (
        _storage_status_spec(seed=fixture_seed)
        if "history_10x" in selected or "research_storage_status" in selected
        else None
    )
    phase_3a_spec = (
        _phase_3a_fixture_spec(seed=fixture_seed)
        if "phase_3a" in selected
        else None
    )
    if not scenario_specs and storage_status_spec is None and phase_3a_spec is None:
        raise ValueError("benchmark selection produced no scenarios")
    host = _host_profile()
    run_label = (
        "acceptance_5_warmups_30_repetitions"
        if warmup_count == DEFAULT_WARMUPS and repetition_count == DEFAULT_REPETITIONS
        else "non_acceptance_smoke"
    )
    fixture_manifest = _build_fixture_manifest(
        repo_root=base,
        dimensions=dimensions,
        specs=scenario_specs,
        seed=fixture_seed,
        host=host,
        run_label=run_label,
        storage_status_spec=storage_status_spec,
        phase_3a_spec=phase_3a_spec,
    )
    worker_spec = {
        "schema_version": WORKER_SPEC_SCHEMA,
        "seed": fixture_seed,
        "warmups": warmup_count,
        "repetitions": repetition_count,
        "run_label": run_label,
        "scenarios": scenario_specs,
        "research_storage_status": storage_status_spec,
        "phase_3a": phase_3a_spec,
    }
    run_worker = worker_runner or _run_worker_process
    timing = run_worker(repo_root=base, mode="timing", worker_spec=worker_spec)
    cpu_profile = run_worker(repo_root=base, mode="cpu", worker_spec=worker_spec)
    allocation_profile = run_worker(repo_root=base, mode="allocation", worker_spec=worker_spec)
    _validate_worker_artifacts(
        fixture_manifest=fixture_manifest,
        timing=timing,
        cpu_profile=cpu_profile,
        allocation_profile=allocation_profile,
        expected_warmups=warmup_count,
        expected_repetitions=repetition_count,
        expected_run_label=run_label,
    )
    decision = _build_gate_decision(
        timing=timing,
        fixture_manifest=fixture_manifest,
        current_host=host,
        reference_host_fingerprint=reference_fingerprint,
        allocation_profile=allocation_profile,
    )
    acceptance = _build_phase_3a_acceptance(
        decision=decision,
        shadow_manifest=_load_shadow_manifest(shadow_manifest, repo_root=base),
    )
    target = _publish_artifact_set(
        output_dir=output_dir,
        repo_root=base,
        artifacts={
            "fixture-manifest.json": fixture_manifest,
            "timing.json": timing,
            "cpu-profile.json": cpu_profile,
            "allocation-profile.json": allocation_profile,
            "decision.json": decision,
            "phase-3a-acceptance.json": acceptance,
        },
    )
    return {
        "status": "complete",
        "output_dir": str(target),
        "run_label": run_label,
        "scenario_count": len(scenario_specs) + int(phase_3a_spec is not None),
        "fixture_set_sha256": fixture_manifest["fixture_set_sha256"],
        "host_fingerprint": host["fingerprint"],
        "existing_full_replay_writer": decision["components"]["existing_full_replay_writer"],
        "research_storage_status": decision["components"]["research_storage_status"],
        "phase_3a_combined": decision["phase_3a_combined"],
        "phase_3a_acceptance": {
            "status": acceptance["status"],
            "readiness": acceptance["readiness"],
        },
    }


def _selected_scenarios(value: str) -> tuple[str, ...]:
    normalized = str(value or "").strip().lower()
    if normalized not in PUBLIC_SCENARIOS:
        raise ValueError(f"scenario must be one of: {', '.join(PUBLIC_SCENARIOS)}")
    if normalized == "all":
        return PUBLIC_SCENARIOS[1:]
    return (normalized,)


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


def _validated_reference_fingerprint(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError("reference host fingerprint must be a 64-character lowercase SHA-256")
    return normalized


def _load_baseline_dimensions(
    value: str | Path | None,
    *,
    repo_root: Path,
) -> BaselineDimensions:
    if value is None:
        return BaselineDimensions(
            event_count=DEFAULT_CURRENT_EVENTS,
            current_lot_count=DEFAULT_CURRENT_LOTS,
            account_count=DEFAULT_ACCOUNT_COUNT,
            payload_bytes=DEFAULT_PAYLOAD_BYTES,
            dimension_source="defaults",
            requested={
                "event_count": DEFAULT_CURRENT_EVENTS,
                "current_lot_count": DEFAULT_CURRENT_LOTS,
                "account_count": DEFAULT_ACCOUNT_COUNT,
                "payload_bytes": DEFAULT_PAYLOAD_BYTES,
            },
            clamped={},
            metadata={
                "baseline_schema": None,
                "payload_fields_consumed": 0,
                "account_dimension_source": "safe_defaults_no_baseline",
            },
        )

    raw = Path(value).expanduser()
    path = (raw if raw.is_absolute() else repo_root / raw).resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise ValueError("baseline must be a regular JSON file")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("baseline exceeds the 32 MiB metadata input limit")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"baseline is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "storage_runtime_baseline.v1":
        raise ValueError("baseline schema must be storage_runtime_baseline.v1")
    sqlite_payload = payload.get("sqlite")
    tables = sqlite_payload.get("tables") if isinstance(sqlite_payload, dict) else None
    table_rows = {
        str(item.get("table") or ""): item
        for item in (tables if isinstance(tables, list) else [])
        if isinstance(item, dict)
    }
    event_row = table_rows.get("trade_events", {})
    lot_row = table_rows.get("position_lots", {})
    requested_events = _metadata_int(event_row.get("row_count"), DEFAULT_CURRENT_EVENTS)
    requested_lots = _metadata_int(lot_row.get("row_count"), DEFAULT_CURRENT_LOTS)
    requested_accounts, account_dimension_source = _baseline_account_count(payload)
    event_json_bytes = _metadata_int(event_row.get("json_bytes"), 0)
    requested_payload_bytes = (
        max(MIN_PAYLOAD_BYTES, math.ceil(event_json_bytes / requested_events))
        if requested_events > 0 and event_json_bytes > 0
        else DEFAULT_PAYLOAD_BYTES
    )
    requested = {
        "event_count": requested_events,
        "current_lot_count": requested_lots,
        "account_count": requested_accounts,
        "payload_bytes": requested_payload_bytes,
    }
    effective = {
        "event_count": _clamp(max(1, requested_events), 1, MAX_CURRENT_EVENTS),
        "current_lot_count": _clamp(max(1, requested_lots), 1, MAX_CURRENT_LOTS),
        "account_count": _clamp(max(1, requested_accounts), 1, MAX_ACCOUNTS),
        "payload_bytes": _clamp(requested_payload_bytes, MIN_PAYLOAD_BYTES, MAX_PAYLOAD_BYTES),
    }
    if effective["event_count"] < effective["current_lot_count"]:
        effective["event_count"] = effective["current_lot_count"]
    clamped = {
        key: {"requested": int(requested[key]), "effective": int(effective[key])}
        for key in requested
        if int(requested[key]) != int(effective[key])
    }
    return BaselineDimensions(
        event_count=effective["event_count"],
        current_lot_count=effective["current_lot_count"],
        account_count=effective["account_count"],
        payload_bytes=effective["payload_bytes"],
        dimension_source="storage_runtime_baseline.v1_metadata",
        requested=requested,
        clamped=clamped,
        metadata={
            "baseline_schema": "storage_runtime_baseline.v1",
            "sqlite_status": str(sqlite_payload.get("status") or "unknown")
            if isinstance(sqlite_payload, dict)
            else "missing",
            "table_metadata_consumed": ["trade_events", "position_lots"],
            "account_dimension_source": account_dimension_source,
            "payload_fields_consumed": 0,
            "paths_retained": 0,
        },
    )


def _baseline_account_count(payload: Mapping[str, Any]) -> tuple[int, str]:
    runtime_storage = payload.get("runtime_storage")
    if isinstance(runtime_storage, Mapping):
        status = str(runtime_storage.get("account_count_status") or "")
        explicit = runtime_storage.get("account_count")
        if status == "complete" and isinstance(explicit, int) and not isinstance(explicit, bool):
            if explicit > 0:
                return explicit, "runtime_storage.output_accounts_immediate_directories"
    return DEFAULT_ACCOUNT_COUNT, "safe_default_account_count_unavailable"


def _metadata_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return int(fallback)
    return parsed if parsed >= 0 else int(fallback)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _build_scenario_specs(
    dimensions: BaselineDimensions,
    *,
    selected: Sequence[str],
) -> list[dict[str, Any]]:
    current_events = max(dimensions.event_count, dimensions.current_lot_count)
    current_lots = dimensions.current_lot_count
    current_accounts = dimensions.account_count
    history_requested = max(MIN_HISTORY_EVENTS, dimensions.requested["event_count"] * 10)
    history_events = _clamp(
        max(MIN_HISTORY_EVENTS, current_events * 10),
        MIN_HISTORY_EVENTS,
        MAX_HISTORY_EVENTS,
    )
    history_axis_status = (
        "evaluable"
        if history_events >= history_requested and history_events >= MIN_HISTORY_EVENTS
        else "not_evaluable_clamped_below_requested_10x"
    )
    state_requested_lots = max(10, dimensions.requested["current_lot_count"] * 10)
    state_lots = _clamp(max(10, current_lots * 10), 10, MAX_CURRENT_STATE_LOTS)
    state_ratio = max(1.0, current_events / max(1, current_lots))
    state_events_requested = max(state_lots, math.ceil(state_lots * state_ratio))
    state_events = _clamp(state_events_requested, state_lots, MAX_HISTORY_EVENTS)
    state_axis_status = (
        "evaluable"
        if state_lots >= state_requested_lots and state_events >= state_events_requested
        else "not_evaluable_clamped_below_requested_10x"
    )
    fanout_requested_accounts = max(10, dimensions.requested["account_count"] * 5)
    fanout_accounts = _clamp(max(10, current_accounts * 5), 10, MAX_ACCOUNTS)
    per_account_lots = max(1, math.ceil(current_lots / max(1, current_accounts)))
    fanout_lots = min(MAX_CURRENT_STATE_LOTS, fanout_accounts * per_account_lots)
    fanout_events = min(
        MAX_HISTORY_EVENTS,
        max(fanout_lots, math.ceil(fanout_lots * state_ratio)),
    )
    fanout_axis_status = (
        "not_evaluable_baseline_account_count_unavailable"
        if dimensions.metadata.get("account_dimension_source") == "safe_default_account_count_unavailable"
        else "evaluable"
        if fanout_accounts >= fanout_requested_accounts
        else "not_evaluable_clamped_below_requested_5x"
    )
    specs: list[dict[str, Any]] = []
    if "current_scale" in selected:
        specs.append(
            _scenario_spec(
                key="current_scale",
                axis="current_scale",
                event_count=current_events,
                lot_count=current_lots,
                account_count=current_accounts,
                payload_bytes=dimensions.payload_bytes,
                shape="fixed_open_lots_with_verifications",
                axis_status="evaluable",
                classification="synthetic_current_scale",
            )
        )
    if "history_10x" in selected:
        specs.extend(
            [
                _scenario_spec(
                    key="history_10x.fixed_output",
                    axis="history_10x",
                    event_count=history_events,
                    lot_count=current_lots,
                    account_count=current_accounts,
                    payload_bytes=dimensions.payload_bytes,
                    shape="fixed_open_lots_with_verifications",
                    axis_status=history_axis_status,
                    classification="complexity_isolation_not_production_event_mix",
                    requested_event_count=history_requested,
                ),
                _scenario_spec(
                    key="history_10x.retained_closed_lots",
                    axis="history_10x",
                    event_count=history_events,
                    lot_count=history_events // 2,
                    account_count=current_accounts,
                    payload_bytes=dimensions.payload_bytes,
                    shape="open_close_pairs",
                    axis_status=history_axis_status,
                    classification="current_retained_closed_lot_coupling",
                    requested_event_count=history_requested,
                ),
            ]
        )
    if "current_state_10x" in selected:
        specs.append(
            _scenario_spec(
                key="current_state_10x",
                axis="current_state_10x",
                event_count=state_events,
                lot_count=state_lots,
                account_count=current_accounts,
                payload_bytes=dimensions.payload_bytes,
                shape="fixed_open_lots_with_verifications",
                axis_status=state_axis_status,
                classification="current_state_growth_axis",
                requested_event_count=state_events_requested,
                requested_lot_count=state_requested_lots,
            )
        )
    if "account_fanout" in selected:
        specs.append(
            _scenario_spec(
                key="account_fanout",
                axis="account_fanout",
                event_count=fanout_events,
                lot_count=fanout_lots,
                account_count=fanout_accounts,
                payload_bytes=dimensions.payload_bytes,
                shape="fixed_open_lots_with_verifications",
                axis_status=fanout_axis_status,
                classification="account_fanout_growth_axis",
                requested_event_count=fanout_events,
                requested_lot_count=fanout_lots,
                requested_account_count=fanout_requested_accounts,
            )
        )
    return specs


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


def _storage_status_spec(*, seed: int) -> dict[str, Any]:
    fixture_identity = {
        "schema_version": "research_storage_status_history_10x_fixture.v1",
        "seed": int(seed),
        "partition_count": STORAGE_STATUS_PARTITION_COUNT,
        "manifest_count": 1,
        "bytes_per_partition": 1,
        "manifest_shape": "integrity.files",
    }
    return {
        "key": STORAGE_STATUS_KEY,
        "axis": "history_10x",
        "classification": "synthetic_manifest_declared_partitions",
        "axis_status": "evaluable",
        "effective_dimensions": {
            "partition_count": STORAGE_STATUS_PARTITION_COUNT,
            "manifest_count": 1,
            "runtime_file_count": STORAGE_STATUS_PARTITION_COUNT + 1,
        },
        "fixture_sha256": _sha256_json(fixture_identity),
        "fixture_identity": fixture_identity,
    }


def _phase_3a_fixture_spec(*, seed: int) -> dict[str, Any]:
    reference = _scenario_spec(
        key="phase_3a.runtime",
        axis="phase_3a",
        event_count=PHASE_3A_EVENT_COUNT,
        lot_count=PHASE_3A_LOT_COUNT,
        account_count=1,
        payload_bytes=MIN_PAYLOAD_BYTES,
        shape="fixed_open_lots_with_verifications",
        axis_status="evaluable",
        classification="synthetic_checkpoint_tail_reference",
    )
    current_state_10x = _scenario_spec(
        key="phase_3a.current_state_10x",
        axis="phase_3a",
        event_count=PHASE_3A_EVENT_COUNT,
        lot_count=PHASE_3A_STATE_10X_LOT_COUNT,
        account_count=1,
        payload_bytes=MIN_PAYLOAD_BYTES,
        shape="fixed_open_lots_with_verifications",
        axis_status="evaluable",
        classification="synthetic_current_state_10x",
    )
    retained_lots_10x = _scenario_spec(
        key="phase_3a.retained_lots_10x",
        axis="phase_3a",
        event_count=PHASE_3A_EVENT_COUNT,
        lot_count=PHASE_3A_EVENT_COUNT // 2,
        account_count=1,
        payload_bytes=MIN_PAYLOAD_BYTES,
        shape="open_close_pairs",
        axis_status="diagnostic_only",
        classification="synthetic_retained_closed_lots_capacity",
    )
    references = [reference, current_state_10x, retained_lots_10x]
    identities = []
    for item in references:
        events = _build_synthetic_events(item, seed=seed)
        identities.append(
            {
                "key": item["key"],
                "fixture_sha256": _events_sha256(events),
                "effective_dimensions": item["effective_dimensions"],
            }
        )
    identity = {
        "schema_version": PHASE_3A_FIXTURE_SCHEMA,
        "seed": int(seed),
        "references": identities,
        "comparable_facades": [
            "single_combo_metadata_close",
            "atomic_batch_combo_metadata_adjust",
        ],
        "force_full_facades": ["special_combo_identity_membership"],
        "rotation_boundaries": ["100_events", "1_mib"],
    }
    return {
        "schema_version": PHASE_3A_FIXTURE_SCHEMA,
        "fixture_sha256": _sha256_json(identity),
        "fixture_identity": identity,
        "reference": reference,
        "current_state_10x": current_state_10x,
        "retained_lots_10x": retained_lots_10x,
    }


def _build_fixture_manifest(
    *,
    repo_root: Path,
    dimensions: BaselineDimensions,
    specs: Sequence[dict[str, Any]],
    seed: int,
    host: dict[str, Any],
    run_label: str,
    storage_status_spec: Mapping[str, Any] | None = None,
    phase_3a_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    fixture_hashes: list[str] = []
    for spec in specs:
        events = _build_synthetic_events(spec, seed=seed)
        metrics = _event_payload_metrics(events)
        fixture_hash = _events_sha256(events)
        fixture_hashes.append(fixture_hash)
        scenarios.append(
            {
                **spec,
                "fixture_sha256": fixture_hash,
                "payload_distribution": metrics,
                "synthetic_only": True,
            }
        )
    fixture_set_items: list[Any] = list(fixture_hashes)
    if storage_status_spec is not None:
        fixture_set_items.append(str(storage_status_spec.get("fixture_sha256") or ""))
    if phase_3a_spec is not None:
        fixture_set_items.append(str(phase_3a_spec.get("fixture_sha256") or ""))
    return {
        "schema_version": FIXTURE_SCHEMA,
        "fixture_seed": int(seed),
        "fixture_set_sha256": _sha256_json(fixture_set_items),
        "dimension_source": dimensions.dimension_source,
        "baseline_dimensions": {
            "requested": dimensions.requested,
            "effective": {
                "event_count": dimensions.event_count,
                "current_lot_count": dimensions.current_lot_count,
                "account_count": dimensions.account_count,
                "payload_bytes": dimensions.payload_bytes,
            },
            "clamped": dimensions.clamped,
            "metadata": dimensions.metadata,
        },
        "identity": {
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "git_sha": _git_sha(repo_root),
            "host_profile": host,
            "run_label": run_label,
            "process_condition": {
                "worker_start": "fresh_process_per_measurement_mode",
                "timing_repetitions": "warm_after_fixture_setup",
                "os_page_cache": "not_flushed",
            },
        },
        "safety": {
            "synthetic_trade_events_only": True,
            "production_sqlite_connections": 0,
            "temporary_sqlite_only": True,
            "runtime_mutations": 0,
        },
        "scenarios": scenarios,
        "research_storage_status": dict(storage_status_spec) if storage_status_spec is not None else None,
        "phase_3a": dict(phase_3a_spec) if phase_3a_spec is not None else None,
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


def _run_worker_process(
    *,
    repo_root: Path,
    mode: str,
    worker_spec: dict[str, Any],
) -> dict[str, Any]:
    if mode not in {"timing", "cpu", "allocation"}:
        raise ValueError(f"unsupported worker mode: {mode}")
    script = repo_root / "scripts/benchmark_data_storage_projection.py"
    if not script.is_file():
        raise ValueError("benchmark worker script is missing")
    with tempfile.TemporaryDirectory(prefix="om-projection-worker-") as temp_name:
        temp_root = Path(temp_name)
        spec_path = temp_root / "worker-spec.json"
        spec_path.write_bytes(_canonical_json_bytes(worker_spec) + b"\n")
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(temp_root / "pycache")
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--_worker-mode",
                mode,
                "--_worker-spec",
                str(spec_path),
            ],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{mode} worker failed: {detail[-4_000:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{mode} worker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{mode} worker returned a non-object payload")
    return payload


def _worker_payload(*, mode: str, worker_spec: Mapping[str, Any]) -> dict[str, Any]:
    if worker_spec.get("schema_version") != WORKER_SPEC_SCHEMA:
        raise ValueError("worker spec schema is invalid")
    raw_scenarios = worker_spec.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise ValueError("worker spec scenarios are missing")
    seed = _bounded_nonnegative_int(worker_spec.get("seed"), name="seed", maximum=2**31 - 1)
    warmups = _bounded_nonnegative_int(worker_spec.get("warmups"), name="warmups", maximum=100)
    repetitions = _bounded_positive_int(worker_spec.get("repetitions"), name="repetitions", maximum=1_000)
    label = str(worker_spec.get("run_label") or "")
    storage_status_spec = worker_spec.get("research_storage_status")
    if storage_status_spec is not None and not isinstance(storage_status_spec, Mapping):
        raise ValueError("research storage status worker spec is invalid")
    phase_3a_spec = worker_spec.get("phase_3a")
    if phase_3a_spec is not None and not isinstance(phase_3a_spec, Mapping):
        raise ValueError("Phase 3A worker spec is invalid")
    if not raw_scenarios and storage_status_spec is None and phase_3a_spec is None:
        raise ValueError("worker spec has no measurable scenarios")
    if mode == "timing":
        rows = [
            _measure_timing_scenario(spec, seed=seed, warmups=warmups, repetitions=repetitions)
            for spec in raw_scenarios
            if isinstance(spec, dict)
        ]
        storage_status = (
            _measure_storage_status_timing(
                storage_status_spec,
                warmups=warmups,
                repetitions=repetitions,
            )
            if isinstance(storage_status_spec, Mapping)
            else None
        )
        phase_3a = (
            _measure_phase_3a_timing(
                phase_3a_spec,
                seed=seed,
                warmups=warmups,
                repetitions=repetitions,
            )
            if isinstance(phase_3a_spec, Mapping)
            else None
        )
        return {
            "schema_version": TIMING_SCHEMA,
            "measurement_mode": "timing_without_profiler",
            "profilers_enabled": False,
            "tracemalloc_enabled": False,
            "warmups": warmups,
            "repetitions": repetitions,
            "run_label": label,
            "clock_authority": ["time.perf_counter_ns", "time.process_time_ns"],
            "scenarios": rows,
            "research_storage_status": storage_status,
            "phase_3a": phase_3a,
        }
    if mode == "cpu":
        rows = [_measure_cpu_scenario(spec, seed=seed) for spec in raw_scenarios if isinstance(spec, dict)]
        storage_status = (
            _measure_storage_status_cpu(storage_status_spec)
            if isinstance(storage_status_spec, Mapping)
            else None
        )
        phase_3a = (
            _measure_phase_3a_cpu(phase_3a_spec, seed=seed)
            if isinstance(phase_3a_spec, Mapping)
            else None
        )
        return {
            "schema_version": CPU_PROFILE_SCHEMA,
            "measurement_mode": "cprofile_separate_process",
            "timing_threshold_eligible": False,
            "tracemalloc_enabled": False,
            "scenarios": rows,
            "research_storage_status": storage_status,
            "phase_3a": phase_3a,
        }
    if mode == "allocation":
        rows = [_measure_allocation_scenario(spec, seed=seed) for spec in raw_scenarios if isinstance(spec, dict)]
        storage_status = (
            _measure_storage_status_allocation(storage_status_spec)
            if isinstance(storage_status_spec, Mapping)
            else None
        )
        phase_3a = (
            _measure_phase_3a_allocation(phase_3a_spec, seed=seed)
            if isinstance(phase_3a_spec, Mapping)
            else None
        )
        return {
            "schema_version": ALLOCATION_PROFILE_SCHEMA,
            "measurement_mode": "tracemalloc_separate_process",
            "timing_threshold_eligible": False,
            "cprofile_enabled": False,
            "scenarios": rows,
            "research_storage_status": storage_status,
            "phase_3a": phase_3a,
        }
    raise ValueError(f"unsupported worker mode: {mode}")


@contextmanager
def _temporary_storage_status_fixture(spec: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    dimensions = spec.get("effective_dimensions")
    if not isinstance(dimensions, Mapping):
        raise ValueError("research storage status dimensions are missing")
    partition_count = _bounded_positive_int(
        dimensions.get("partition_count"),
        name="partition_count",
        maximum=STORAGE_STATUS_PARTITION_COUNT,
    )
    if partition_count != STORAGE_STATUS_PARTITION_COUNT:
        raise ValueError("research storage status partition count must match the frozen fixture")
    with tempfile.TemporaryDirectory(prefix="om-storage-status-history10x-") as temp_name:
        runtime_root = Path(temp_name)
        history_root = runtime_root / "output_shared" / "research" / "history_10x"
        history_root.mkdir(parents=True)
        digest = hashlib.sha256(b"x").hexdigest()
        files: dict[str, dict[str, Any]] = {}
        for index in range(partition_count):
            name = f"partition-{index:05d}.bin"
            (history_root / name).write_bytes(b"x")
            files[name] = {"sha256": digest, "bytes": 1}
        manifest = {
            "schema_version": "research.history_10x.v1",
            "integrity": {"files": files},
        }
        manifest_path = history_root / "manifest.json"
        manifest_path.write_bytes(_canonical_json_bytes(manifest) + b"\n")
        actual_identity = _sha256_json(
            {
                "schema_version": "research_storage_status_history_10x_fixture.v1",
                "seed": int((spec.get("fixture_identity") or {}).get("seed") or 0),
                "partition_count": partition_count,
                "manifest_count": 1,
                "bytes_per_partition": 1,
                "manifest_shape": "integrity.files",
            }
        )
        expected_identity = str(spec.get("fixture_sha256") or "")
        if actual_identity != expected_identity:
            raise RuntimeError("research storage status fixture identity mismatch")
        yield {
            "repo_root": Path.cwd(),
            "runtime_root": runtime_root,
            "fixture_sha256": actual_identity,
        }


def _collect_synthetic_storage_status(context: Mapping[str, Any]) -> dict[str, Any]:
    return collect_storage_runtime_baseline(
        repo_root=context["repo_root"],
        runtime_root=context["runtime_root"],
        now_fn=lambda: STORAGE_STATUS_OBSERVED_AT,
    )


def _storage_status_output(result: Mapping[str, Any]) -> dict[str, Any]:
    runtime = result.get("runtime_storage")
    research = result.get("research_storage")
    safety = result.get("safety")
    if not isinstance(runtime, Mapping) or not isinstance(research, Mapping) or not isinstance(safety, Mapping):
        raise RuntimeError("research storage status output is incomplete")
    output = {
        "status": result.get("status"),
        "runtime_file_count": runtime.get("file_count"),
        "manifest_count": research.get("manifest_count"),
        "declared_reference_count": research.get("declared_reference_count"),
        "protected_reference_failure_count": len(research.get("protected_reference_failures") or []),
        "payload_content_reads": safety.get("payload_content_reads"),
        "mutation_operations": safety.get("mutation_operations"),
        "no_follow_traversal": safety.get("no_follow_traversal"),
    }
    expected = {
        "runtime_file_count": STORAGE_STATUS_PARTITION_COUNT + 1,
        "manifest_count": 1,
        "declared_reference_count": STORAGE_STATUS_PARTITION_COUNT,
        "protected_reference_failure_count": 0,
        "payload_content_reads": 0,
        "mutation_operations": 0,
        "no_follow_traversal": True,
    }
    mismatches = {
        key: {"expected": value, "actual": output.get(key)}
        for key, value in expected.items()
        if output.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"research storage status output mismatch: {mismatches}")
    return output


def _measure_storage_status_timing(
    spec: Mapping[str, Any],
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    with _temporary_storage_status_fixture(spec) as context:
        result: Mapping[str, Any] | None = None
        for _ in range(warmups):
            result = _collect_synthetic_storage_status(context)
        wall_samples: list[int] = []
        cpu_samples: list[int] = []
        for _ in range(repetitions):
            wall_start = time.perf_counter_ns()
            cpu_start = time.process_time_ns()
            result = _collect_synthetic_storage_status(context)
            cpu_samples.append(time.process_time_ns() - cpu_start)
            wall_samples.append(time.perf_counter_ns() - wall_start)
        if result is None:
            result = _collect_synthetic_storage_status(context)
        return {
            "key": STORAGE_STATUS_KEY,
            "fixture_sha256": str(context["fixture_sha256"]),
            "axis_status": str(spec.get("axis_status") or "unknown"),
            "setup_included": False,
            "measurement_scope": "payload_free_storage_status_collection",
            "wall_time_ns": _timing_distribution(wall_samples),
            "cpu_time_ns": _timing_distribution(cpu_samples),
            "output": _storage_status_output(result),
        }


def _measure_storage_status_cpu(spec: Mapping[str, Any]) -> dict[str, Any]:
    with _temporary_storage_status_fixture(spec) as context:
        profile, output = _profile_call(
            lambda: _collect_synthetic_storage_status(context),
            output_fn=_storage_status_output,
        )
        return {
            "key": STORAGE_STATUS_KEY,
            "fixture_sha256": str(context["fixture_sha256"]),
            "setup_included": False,
            "component": profile,
            "output": output,
        }


def _measure_storage_status_allocation(spec: Mapping[str, Any]) -> dict[str, Any]:
    with _temporary_storage_status_fixture(spec) as context:
        allocation, output = _allocation_call(
            lambda: _collect_synthetic_storage_status(context),
            output_fn=_storage_status_output,
        )
        return {
            "key": STORAGE_STATUS_KEY,
            "fixture_sha256": str(context["fixture_sha256"]),
            "setup_included": False,
            "component": allocation,
            "output": output,
        }


def _phase_3a_record_id(spec: Mapping[str, Any], index: int) -> str:
    slug = str(spec.get("key") or "").replace(".", "-").replace("_", "-")
    return f"lot-{slug}-{int(index):06d}"


def _phase_3a_open_event_id(spec: Mapping[str, Any], index: int) -> str:
    slug = str(spec.get("key") or "").replace(".", "-").replace("_", "-")
    return f"bench-{slug}-{int(index):06d}-open"


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


@contextmanager
def _temporary_phase_3a_clone(
    base: Mapping[str, Any],
    *,
    checkpoint_mode: str,
    tail_events: Sequence[dict[str, Any]] = (),
) -> Iterator[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="om-phase3a-run-") as temp_name:
        root = Path(temp_name)
        data_config = root / "data.json"
        data_config.write_text("{}\n", encoding="utf-8")
        db_path = root / LEDGER_DB_RELATIVE_PATH
        db_path.parent.mkdir(parents=True)
        shutil.copy2(Path(base["db_path"]), db_path)
        repo = open_position_ledger(data_config)
        repo.set_position_projection_checkpoint_mode(checkpoint_mode)
        if tail_events:
            _insert_phase_3a_events(repo, tail_events)
        yield {
            "repo": repo,
            "db_path": db_path,
            "fixture_sha256": str(base["fixture_sha256"]),
            "sqlite_sha256": str(base["sqlite_sha256"]),
        }


@contextmanager
def _instrument_phase_3a_repo(repo: Any) -> Iterator[dict[str, Any]]:
    counters: dict[str, Any] = {
        "method_calls": {},
        "rows_returned": {},
        "full_prefix_reader_calls": 0,
        "full_lot_list_calls": 0,
        "candidate_event_ids_requested": 0,
        "candidate_event_id_max_batch": 0,
        "candidate_event_id_request_counts": {},
        "sql_statements": {"total": 0, "select": 0, "insert": 0, "update": 0, "delete": 0},
    }
    originals: dict[str, Any] = {}
    original_connect = repo._connect

    def traced_connect() -> sqlite3.Connection:
        conn = original_connect()

        def trace(statement: str) -> None:
            normalized = str(statement or "").lstrip().lower()
            counters["sql_statements"]["total"] += 1
            for operation in ("select", "insert", "update", "delete"):
                if normalized.startswith(operation):
                    counters["sql_statements"][operation] += 1
                    break

        conn.set_trace_callback(trace)
        return conn

    repo._connect = traced_connect
    method_names = (
        "list_position_projection_event_rows",
        "list_trade_events",
        "list_position_lots",
        "get_trade_events_by_ids",
        "list_active_position_lots",
        "get_position_lots_by_ids",
        "position_projection_account_snapshot",
    )

    def wrapper(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def measured(*args: Any, **kwargs: Any) -> Any:
            counters["method_calls"][name] = counters["method_calls"].get(name, 0) + 1
            if name == "list_position_projection_event_rows" and kwargs.get("after") is None:
                counters["full_prefix_reader_calls"] += 1
            if name in {"list_trade_events", "list_position_lots"}:
                if name == "list_position_lots":
                    counters["full_lot_list_calls"] += 1
                else:
                    counters["full_prefix_reader_calls"] += 1
            if name == "get_trade_events_by_ids":
                requested = tuple(args[0] if args else kwargs.get("event_ids", ()) or ())
                counters["candidate_event_ids_requested"] += len(requested)
                counters["candidate_event_id_max_batch"] = max(
                    int(counters["candidate_event_id_max_batch"]),
                    len(requested),
                )
                request_counts = counters["candidate_event_id_request_counts"]
                for event_id in requested:
                    key = str(event_id)
                    request_counts[key] = int(request_counts.get(key, 0)) + 1
            result = original(*args, **kwargs)
            row_count = (
                len(result)
                if isinstance(result, (list, tuple))
                else int(getattr(result, "lot_count", 0) or 0)
            )
            counters["rows_returned"][name] = counters["rows_returned"].get(name, 0) + row_count
            return result

        return measured

    try:
        for name in method_names:
            original = getattr(repo, name, None)
            if callable(original):
                originals[name] = original
                setattr(repo, name, wrapper(name, original))
        yield counters
    finally:
        repo._connect = original_connect
        for name, original in originals.items():
            setattr(repo, name, original)


def _phase_3a_checkpoint_stats(repo: Any) -> dict[str, int]:
    rows = repo.list_position_projection_checkpoints()
    return {
        "row_count": len(rows),
        "state_bytes": sum(int(row.get("state_bytes") or 0) for row in rows),
        "max_state_bytes": max((int(row.get("state_bytes") or 0) for row in rows), default=0),
    }


def _phase_3a_lot_fingerprint(db_path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    rows = 0
    payload_bytes = 0
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT record_id,fields_json FROM position_lots ORDER BY record_id"
        )
        for record_id, fields_json in cursor:
            payload = _canonical_json_bytes([str(record_id), str(fields_json)])
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            rows += 1
            payload_bytes += len(payload)
    return {"sha256": digest.hexdigest(), "rows": rows, "bytes": payload_bytes}


def _phase_3a_operation(repo: Any, *, key: str, spec: Mapping[str, Any]) -> Any:
    if key == "single_combo_metadata_close":
        return record_manual_position_close(
            repo,
            record_id=_phase_3a_record_id(spec, 0),
            contracts_to_close=1,
            close_price=0.5,
            close_reason="phase_3a_benchmark",
            as_of_ms=1_900_000_000_000,
        )
    if key == "atomic_batch_combo_metadata_adjust":
        group_id = "bench-ordinary-combo"
        return record_manual_position_adjustments(
            repo,
            [
                {
                    "record_id": _phase_3a_record_id(spec, index),
                    "strategy": "combo_yield",
                    "leg_role": role,
                    "strategy_group_id": group_id,
                    "strategy_snapshot": {
                        "schema_version": "benchmark_strategy_snapshot.v1"
                    },
                    "as_of_ms": 1_900_000_000_100 + index,
                }
                for index, role in ((2, "funding_put"), (3, "participation_call"))
            ],
        )
    if key == "special_combo_identity_membership":
        put_key = ContractKey.from_values(
            broker="futu",
            account="bench00",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=10,
            expiration_ymd="2028-12-15",
        )
        call_key = ContractKey.from_values(
            broker="futu",
            account="bench00",
            underlying_symbol="NVDA",
            option_type="call",
            position_side="long",
            strike=25,
            expiration_ymd="2028-12-15",
        )
        event = TradeEvent(
            event_id="bench-phase3a-special-call-open",
            event_type="open",
            event_time_ms=1_900_000_000_500,
            contract_key=call_key,
            contracts=1,
            price=0.5,
            currency="USD",
            source="synthetic_benchmark",
            multiplier=100,
            lot_id="lot-phase3a-special-call",
            raw_payload={
                "strategy": "combo_yield",
                "leg_role": "participation_call",
                "strategy_group_id": "bench-special-combo",
            },
        )
        intent = build_combo_identity_intent(
            first_leg={
                "strategy_group_id": "bench-special-combo",
                "strategy": "combo_yield",
                "leg_role": "funding_put",
                "account": "bench00",
                "symbol": "NVDA",
                "contracts": 1,
                "open_event_id": _phase_3a_open_event_id(spec, 0),
                "record_id": _phase_3a_record_id(spec, 0),
                "contract_key": put_key.to_dict(),
            },
            second_leg={
                "strategy_group_id": "bench-special-combo",
                "strategy": "combo_yield",
                "leg_role": "participation_call",
                "account": "bench00",
                "symbol": "NVDA",
                "contracts": 1,
                "open_event_id": event.event_id,
                "record_id": str(event.lot_id),
                "contract_key": call_key.to_dict(),
            },
        )
        return record_combo_trade_open(repo, event=event, combo_identity_intent=intent)
    raise ValueError(f"unsupported Phase 3A operation: {key}")


def _measure_phase_3a_once(
    base: Mapping[str, Any],
    *,
    operation: str,
    checkpoint_mode: str,
    tail_events: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    with _temporary_phase_3a_clone(
        base,
        checkpoint_mode=checkpoint_mode,
        tail_events=tail_events,
    ) as context:
        repo = context["repo"]
        before_checkpoint = _phase_3a_checkpoint_stats(repo)
        before_sizes = _sqlite_sizes(context["db_path"])
        with _instrument_phase_3a_repo(repo) as counters:
            wall_start = time.perf_counter_ns()
            cpu_start = time.process_time_ns()
            _phase_3a_operation(repo, key=operation, spec=base["spec"])
            cpu_ns = time.process_time_ns() - cpu_start
            wall_ns = time.perf_counter_ns() - wall_start
        after_checkpoint = _phase_3a_checkpoint_stats(repo)
        after_sizes = _sqlite_sizes(context["db_path"])
        output = _phase_3a_lot_fingerprint(context["db_path"])
        conn = repo._connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        steady_sizes = _sqlite_sizes(context["db_path"])
        return {
            "fixture_sha256": context["fixture_sha256"],
            "base_sqlite_sha256": context["sqlite_sha256"],
            "checkpoint_mode": checkpoint_mode,
            "wall_ns": wall_ns,
            "cpu_ns": cpu_ns,
            "counters": counters,
            "checkpoint": {
                "before": before_checkpoint,
                "after": after_checkpoint,
                "row_delta": after_checkpoint["row_count"] - before_checkpoint["row_count"],
                "state_bytes_delta": after_checkpoint["state_bytes"] - before_checkpoint["state_bytes"],
            },
            "sqlite_bytes": {
                "before": before_sizes,
                "after_before_checkpoint": after_sizes,
                "steady_after_truncate": steady_sizes,
            },
            "output": output,
        }


def _phase_3a_sample_summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise ValueError("Phase 3A samples are empty")
    wall = [int(item["wall_ns"]) for item in samples]
    cpu = [int(item["cpu_ns"]) for item in samples]
    fingerprints = sorted({str((item.get("output") or {}).get("sha256") or "") for item in samples})
    counter_keys = (
        "full_prefix_reader_calls",
        "full_lot_list_calls",
        "candidate_event_ids_requested",
        "candidate_event_id_max_batch",
    )
    counter_max = {
        key: max(int((item.get("counters") or {}).get(key) or 0) for item in samples)
        for key in counter_keys
    }
    candidate_event_ids_unique_max = max(
        len((item.get("counters") or {}).get("candidate_event_id_request_counts", {}))
        for item in samples
    )
    method_names = sorted(
        {
            name
            for item in samples
            for name in (item.get("counters") or {}).get("method_calls", {})
        }
    )
    method_call_max = {
        name: max(
            int((item.get("counters") or {}).get("method_calls", {}).get(name, 0))
            for item in samples
        )
        for name in method_names
    }
    row_names = sorted(
        {
            name
            for item in samples
            for name in (item.get("counters") or {}).get("rows_returned", {})
        }
    )
    row_max = {
        name: max(
            int((item.get("counters") or {}).get("rows_returned", {}).get(name, 0))
            for item in samples
        )
        for name in row_names
    }
    sql_names = ("total", "select", "insert", "update", "delete")
    sql_max = {
        name: max(
            int((item.get("counters") or {}).get("sql_statements", {}).get(name, 0))
            for item in samples
        )
        for name in sql_names
    }
    sqlite_growth = [
        int(item["sqlite_bytes"]["after_before_checkpoint"]["total_bytes"])
        - int(item["sqlite_bytes"]["before"]["total_bytes"])
        for item in samples
    ]
    return {
        "wall_time_ns": _timing_distribution(wall),
        "cpu_time_ns": _timing_distribution(cpu),
        "output_fingerprints": fingerprints,
        "output_fingerprint_stable": len(fingerprints) == 1 and bool(fingerprints[0]),
        "fixture_sha256": str(samples[0].get("fixture_sha256") or ""),
        "base_sqlite_sha256": str(samples[0].get("base_sqlite_sha256") or ""),
        "checkpoint_mode": str(samples[0].get("checkpoint_mode") or ""),
        "call_count_max": {
            **counter_max,
            "candidate_event_ids_unique": candidate_event_ids_unique_max,
            "methods": method_call_max,
        },
        "rows_returned_max": row_max,
        "sql_statement_max": sql_max,
        "checkpoint_row_deltas": sorted(
            {int((item.get("checkpoint") or {}).get("row_delta") or 0) for item in samples}
        ),
        "checkpoint_state_byte_deltas": sorted(
            {int((item.get("checkpoint") or {}).get("state_bytes_delta") or 0) for item in samples}
        ),
        "checkpoint_after_max": {
            "row_count": max(int(item["checkpoint"]["after"]["row_count"]) for item in samples),
            "state_bytes": max(int(item["checkpoint"]["after"]["state_bytes"]) for item in samples),
            "one_state_bytes": max(int(item["checkpoint"]["after"]["max_state_bytes"]) for item in samples),
        },
        "sqlite_growth_bytes": _timing_distribution(sqlite_growth),
        "sqlite_peak_total_bytes": max(
            int(item["sqlite_bytes"]["after_before_checkpoint"]["total_bytes"])
            for item in samples
        ),
        "sqlite_steady_total_bytes": max(
            int(item["sqlite_bytes"]["steady_after_truncate"]["total_bytes"])
            for item in samples
        ),
    }


def _measure_phase_3a_distribution(
    base: Mapping[str, Any],
    *,
    operation: str,
    checkpoint_mode: str,
    warmups: int,
    repetitions: int,
    tail_events: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    for _ in range(warmups):
        _measure_phase_3a_once(
            base,
            operation=operation,
            checkpoint_mode=checkpoint_mode,
            tail_events=tail_events,
        )
    samples = [
        _measure_phase_3a_once(
            base,
            operation=operation,
            checkpoint_mode=checkpoint_mode,
            tail_events=tail_events,
        )
        for _ in range(repetitions)
    ]
    return _phase_3a_sample_summary(samples)


def _phase_3a_pair(
    base: Mapping[str, Any],
    *,
    operation: str,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    forced = _measure_phase_3a_distribution(
        base,
        operation=operation,
        checkpoint_mode="disabled",
        warmups=warmups,
        repetitions=repetitions,
    )
    fast = _measure_phase_3a_distribution(
        base,
        operation=operation,
        checkpoint_mode="enabled",
        warmups=warmups,
        repetitions=repetitions,
    )
    forced_wall = int(forced["wall_time_ns"]["p95"])
    forced_cpu = int(forced["cpu_time_ns"]["p95"])
    fast_wall = int(fast["wall_time_ns"]["p95"])
    fast_cpu = int(fast["cpu_time_ns"]["p95"])
    parity = bool(
        forced["output_fingerprint_stable"]
        and fast["output_fingerprint_stable"]
        and forced["output_fingerprints"] == fast["output_fingerprints"]
        and forced["fixture_sha256"] == fast["fixture_sha256"]
        and forced["base_sqlite_sha256"] == fast["base_sqlite_sha256"]
    )
    return {
        "key": operation,
        "fixture_reset": "independent_copies_same_base_sqlite",
        "forced_full": forced,
        "fast": fast,
        "parity": {"exact": parity},
        "improvement": {
            "wall_fraction": round(1.0 - fast_wall / max(1, forced_wall), 6),
            "cpu_fraction": round(1.0 - fast_cpu / max(1, forced_cpu), 6),
        },
    }


def _timed_phase_3a_read(
    fn: Callable[[], Any],
    *,
    warmups: int,
    repetitions: int,
    output_fn: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    result: Any = None
    for _ in range(warmups):
        result = fn()
    wall_samples: list[int] = []
    cpu_samples: list[int] = []
    for _ in range(repetitions):
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        result = fn()
        cpu_samples.append(time.process_time_ns() - cpu_start)
        wall_samples.append(time.perf_counter_ns() - wall_start)
    if result is None:
        result = fn()
    return {
        "wall_time_ns": _timing_distribution(wall_samples),
        "cpu_time_ns": _timing_distribution(cpu_samples),
        "output": output_fn(result),
    }


def _phase_3a_current_read(
    base: Mapping[str, Any],
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    return _timed_phase_3a_read(
        lambda: read_current_position_projection(base["repo"], account="bench00"),
        warmups=warmups,
        repetitions=repetitions,
        output_fn=lambda result: {
            "status": result.get("status"),
            "lot_count": result.get("lot_count"),
            "projection_fingerprint": result.get("projection_fingerprint"),
        },
    )


def _phase_3a_fingerprint_only(
    base: Mapping[str, Any],
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    measured = _timed_phase_3a_read(
        lambda: base["repo"].position_projection_account_snapshot("bench00"),
        warmups=warmups,
        repetitions=repetitions,
        output_fn=lambda result: {
            "lot_count": int(result.lot_count),
            "fingerprint": str(result.fingerprint),
        },
    )
    with sqlite3.connect(base["db_path"]) as conn:
        row = conn.execute(
            "SELECT count(*),coalesce(sum(length(fields_json)),0) "
            "FROM position_lots WHERE account='bench00'"
        ).fetchone()
    measured["rows"] = int(row[0] or 0)
    measured["bytes"] = int(row[1] or 0)
    return measured


def _phase_3a_invalidation_lookup(
    base: Mapping[str, Any],
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    backdated = _phase_3a_tail_events(count=1)[0]
    backdated["event_id"] = "bench-phase3a-backdated-invalidation"
    backdated["event_time_ms"] = 1_700_000_000_000
    with _temporary_phase_3a_clone(
        base,
        checkpoint_mode="enabled",
        tail_events=(backdated,),
    ) as context:
        return _timed_phase_3a_read(
            lambda: context["repo"].read_newest_trusted_position_projection_checkpoint(),
            warmups=warmups,
            repetitions=repetitions,
            output_fn=lambda result: {"trusted_checkpoint_found": result is not None},
        )


def _phase_3a_loaded_fingerprint_timing(
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    measured = _timed_phase_3a_read(
        compute_projector_implementation_fingerprint,
        warmups=warmups,
        repetitions=repetitions,
        output_fn=lambda result: {
            "fingerprint": str(result),
            "matches_loaded": str(result) == loaded_projector_implementation_fingerprint(),
        },
    )
    measured["ledger_history_reads"] = 0
    return measured


def _phase_3a_index_migration(base: Mapping[str, Any]) -> dict[str, Any]:
    with _temporary_phase_3a_clone(base, checkpoint_mode="disabled") as context:
        conn = context["repo"]._connect()
        try:
            for index in (
                "idx_trade_events_account_time",
                "idx_position_lots_account_expiration",
                "idx_position_lots_account_record",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {index}")
            conn.commit()
        finally:
            conn.close()
        inventory = build_position_projection_migration_inventory(context["db_path"])
        result = apply_position_projection_migration(context["db_path"], inventory)
        return {
            "indexes_created": result["indexes_created"],
            "index_timing": result["index_timing"],
            "total_timing": result["timing"],
            "sqlite_bytes": result["sqlite_bytes"],
        }


def _measure_phase_3a_timing(
    spec: Mapping[str, Any],
    *,
    seed: int,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    reference_spec = spec.get("reference")
    state_spec = spec.get("current_state_10x")
    retained_spec = spec.get("retained_lots_10x")
    if not all(isinstance(item, Mapping) for item in (reference_spec, state_spec, retained_spec)):
        raise ValueError("Phase 3A fixture specifications are incomplete")
    with _temporary_phase_3a_base(reference_spec, seed=seed) as reference:
        comparable = [
            _phase_3a_pair(
                reference,
                operation=operation,
                warmups=warmups,
                repetitions=repetitions,
            )
            for operation in (
                "single_combo_metadata_close",
                "atomic_batch_combo_metadata_adjust",
            )
        ]
        no_rotation = comparable[0]["fast"]
        rotation_100 = _measure_phase_3a_distribution(
            reference,
            operation="single_combo_metadata_close",
            checkpoint_mode="enabled",
            warmups=warmups,
            repetitions=repetitions,
            tail_events=_phase_3a_tail_events(count=99),
        )
        rotation_1_mib = _measure_phase_3a_distribution(
            reference,
            operation="single_combo_metadata_close",
            checkpoint_mode="enabled",
            warmups=warmups,
            repetitions=repetitions,
            tail_events=_phase_3a_tail_events(count=1, payload_bytes=1_048_576),
        )
        force_full = _measure_phase_3a_distribution(
            reference,
            operation="special_combo_identity_membership",
            checkpoint_mode="enabled",
            warmups=warmups,
            repetitions=repetitions,
        )
        current_read = _phase_3a_current_read(
            reference,
            warmups=warmups,
            repetitions=repetitions,
        )
        fingerprint_current = _phase_3a_fingerprint_only(
            reference,
            warmups=warmups,
            repetitions=repetitions,
        )
        invalidation = _phase_3a_invalidation_lookup(
            reference,
            warmups=warmups,
            repetitions=repetitions,
        )
        index_migration = _phase_3a_index_migration(reference)
    with _temporary_phase_3a_base(state_spec, seed=seed) as state_10x:
        state_read = _phase_3a_current_read(
            state_10x,
            warmups=warmups,
            repetitions=repetitions,
        )
    with _temporary_phase_3a_base(retained_spec, seed=seed) as retained_10x:
        retained = _phase_3a_fingerprint_only(
            retained_10x,
            warmups=warmups,
            repetitions=repetitions,
        )
    return {
        "schema_version": "data_storage_projection_phase3a_timing.v1",
        "fixture_sha256": str(spec.get("fixture_sha256") or ""),
        "setup_included": False,
        "comparable_facades": comparable,
        "checkpoint": {
            "no_rotation": no_rotation,
            "rotation_100_events": rotation_100,
            "rotation_1_mib": rotation_1_mib,
        },
        "force_full_facades": [
            {
                "key": "special_combo_identity_membership",
                "reason": "immutable_identity_and_membership_transaction",
                "measurement": force_full,
            }
        ],
        "current_reads": {
            "current": current_read,
            "current_state_10x": state_read,
        },
        "fingerprint_only": {
            "current": fingerprint_current,
            "retained_lots_10x": {
                **retained,
                "guarantee": False,
                "capacity_warning": (
                    "diagnostic exceeds current 500 ms wall/CPU boundary"
                    if (
                        int(retained["wall_time_ns"]["p95"]) > PHASE_3A_WALL_LIMIT_NS
                        or int(retained["cpu_time_ns"]["p95"]) > PHASE_3A_CPU_LIMIT_NS
                    )
                    else None
                ),
            },
        },
        "invalidation_lookup": invalidation,
        "loaded_projector_fingerprint_startup": _phase_3a_loaded_fingerprint_timing(
            warmups=warmups,
            repetitions=repetitions,
        ),
        "index_migration": index_migration,
    }


def _phase_3a_profile_operation(
    base: Mapping[str, Any],
    *,
    operation: str,
    checkpoint_mode: str,
    allocation: bool,
    tail_events: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    with _temporary_phase_3a_clone(
        base,
        checkpoint_mode=checkpoint_mode,
        tail_events=tail_events,
    ) as context:
        repo = context["repo"]
        before = _phase_3a_checkpoint_stats(repo)
        with _instrument_phase_3a_repo(repo) as counters:
            runner = _allocation_call if allocation else _profile_call
            profile, _output = runner(
                lambda: _phase_3a_operation(repo, key=operation, spec=base["spec"]),
                output_fn=lambda _result: {"completed": True},
            )
        after = _phase_3a_checkpoint_stats(repo)
        return {
            "key": operation,
            "checkpoint_mode": checkpoint_mode,
            "component": profile,
            "call_counts": counters,
            "checkpoint": {
                "row_delta": after["row_count"] - before["row_count"],
                "state_bytes_delta": after["state_bytes"] - before["state_bytes"],
                "after": after,
            },
            "output": _phase_3a_lot_fingerprint(context["db_path"]),
        }


def _measure_phase_3a_cpu(spec: Mapping[str, Any], *, seed: int) -> dict[str, Any]:
    reference_spec = spec.get("reference")
    if not isinstance(reference_spec, Mapping):
        raise ValueError("Phase 3A reference fixture is missing")
    with _temporary_phase_3a_base(reference_spec, seed=seed) as reference:
        comparable = [
            {
                "key": operation,
                "forced_full": _phase_3a_profile_operation(
                    reference,
                    operation=operation,
                    checkpoint_mode="disabled",
                    allocation=False,
                ),
                "fast": _phase_3a_profile_operation(
                    reference,
                    operation=operation,
                    checkpoint_mode="enabled",
                    allocation=False,
                ),
            }
            for operation in (
                "single_combo_metadata_close",
                "atomic_batch_combo_metadata_adjust",
            )
        ]
        force_full = _phase_3a_profile_operation(
            reference,
            operation="special_combo_identity_membership",
            checkpoint_mode="enabled",
            allocation=False,
        )
    return {
        "schema_version": "data_storage_projection_phase3a_cpu_profile.v1",
        "fixture_sha256": str(spec.get("fixture_sha256") or ""),
        "setup_included": False,
        "comparable_facades": comparable,
        "force_full_facades": [
            {
                "key": "special_combo_identity_membership",
                "reason": "immutable_identity_and_membership_transaction",
                "measurement": force_full,
            }
        ],
    }


def _measure_phase_3a_allocation(spec: Mapping[str, Any], *, seed: int) -> dict[str, Any]:
    reference_spec = spec.get("reference")
    state_spec = spec.get("current_state_10x")
    if not isinstance(reference_spec, Mapping) or not isinstance(state_spec, Mapping):
        raise ValueError("Phase 3A allocation fixtures are missing")
    with _temporary_phase_3a_base(reference_spec, seed=seed) as reference:
        comparable = [
            {
                "key": operation,
                "fast": _phase_3a_profile_operation(
                    reference,
                    operation=operation,
                    checkpoint_mode="enabled",
                    allocation=True,
                ),
            }
            for operation in (
                "single_combo_metadata_close",
                "atomic_batch_combo_metadata_adjust",
            )
        ]
        rotations = {
            "rotation_100_events": _phase_3a_profile_operation(
                reference,
                operation="single_combo_metadata_close",
                checkpoint_mode="enabled",
                allocation=True,
                tail_events=_phase_3a_tail_events(count=99),
            ),
            "rotation_1_mib": _phase_3a_profile_operation(
                reference,
                operation="single_combo_metadata_close",
                checkpoint_mode="enabled",
                allocation=True,
                tail_events=_phase_3a_tail_events(count=1, payload_bytes=1_048_576),
            ),
        }
        current_read, current_output = _allocation_call(
            lambda: read_current_position_projection(reference["repo"], account="bench00"),
            output_fn=lambda result: {
                "status": result.get("status"),
                "lot_count": result.get("lot_count"),
            },
        )
        fingerprint, fingerprint_output = _allocation_call(
            lambda: reference["repo"].position_projection_account_snapshot("bench00"),
            output_fn=lambda result: {
                "lot_count": result.lot_count,
                "fingerprint": result.fingerprint,
            },
        )
    with _temporary_phase_3a_base(state_spec, seed=seed) as state_10x:
        state_read, state_output = _allocation_call(
            lambda: read_current_position_projection(state_10x["repo"], account="bench00"),
            output_fn=lambda result: {
                "status": result.get("status"),
                "lot_count": result.get("lot_count"),
            },
        )
    startup, startup_output = _allocation_call(
        compute_projector_implementation_fingerprint,
        output_fn=lambda result: {
            "fingerprint": str(result),
            "matches_loaded": str(result) == loaded_projector_implementation_fingerprint(),
        },
    )
    return {
        "schema_version": "data_storage_projection_phase3a_allocation_profile.v1",
        "fixture_sha256": str(spec.get("fixture_sha256") or ""),
        "setup_included": False,
        "comparable_facades": comparable,
        "checkpoint": rotations,
        "current_reads": {
            "current": {"component": current_read, "output": current_output},
            "current_state_10x": {"component": state_read, "output": state_output},
        },
        "fingerprint_only": {"component": fingerprint, "output": fingerprint_output},
        "loaded_projector_fingerprint_startup": {
            "component": startup,
            "output": startup_output,
        },
    }


def _measure_timing_scenario(
    spec: Mapping[str, Any],
    *,
    seed: int,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    events = _build_synthetic_events(spec, seed=seed)
    fixture_hash = _events_sha256(events)
    projector = _timed_projector(events, warmups=warmups, repetitions=repetitions)
    writer = _timed_writer(events, warmups=warmups, repetitions=repetitions)
    parity = _projection_parity(projector["output"], writer["output"])
    _assert_expected_counts(spec, projector["output"])
    if not parity["exact"]:
        raise RuntimeError(f"writer/projector output mismatch for {spec.get('key')}: {parity}")
    return {
        "key": str(spec.get("key") or ""),
        "fixture_sha256": fixture_hash,
        "axis_status": str(spec.get("axis_status") or "unknown"),
        "counts": _output_counts(projector["output"]),
        "parity": parity,
        "components": {
            "projector_only": projector,
            "existing_full_replay_writer": writer,
        },
    }


def _timed_projector(
    events: list[dict[str, Any]],
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    projection: Any = None
    for _ in range(warmups):
        projection = project_stored_trade_events_to_position_lots(events)
    wall_samples: list[int] = []
    cpu_samples: list[int] = []
    for _ in range(repetitions):
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        projection = project_stored_trade_events_to_position_lots(events)
        cpu_samples.append(time.process_time_ns() - cpu_start)
        wall_samples.append(time.perf_counter_ns() - wall_start)
    if projection is None:
        projection = project_stored_trade_events_to_position_lots(events)
    return {
        "measurement_scope": "canonical_codec_projection_no_sqlite",
        "wall_time_ns": _timing_distribution(wall_samples),
        "cpu_time_ns": _timing_distribution(cpu_samples),
        "output": _projection_output(projection, event_count=len(events)),
        "sql": {
            "application_statement_count_per_replay": 0,
            "rows_read_per_replay": 0,
            "rows_written_per_replay": 0,
        },
    }


def _timed_writer(
    events: list[dict[str, Any]],
    *,
    warmups: int,
    repetitions: int,
) -> dict[str, Any]:
    with _temporary_writer(events) as context:
        result: Any = None
        for _ in range(warmups):
            result = rebuild_position_lots_from_trade_events(context["repo"])
        wall_samples: list[int] = []
        cpu_samples: list[int] = []
        peak_sqlite = dict(context["before"])
        for _ in range(repetitions):
            wall_start = time.perf_counter_ns()
            cpu_start = time.process_time_ns()
            result = rebuild_position_lots_from_trade_events(context["repo"])
            cpu_samples.append(time.process_time_ns() - cpu_start)
            wall_samples.append(time.perf_counter_ns() - wall_start)
            peak_sqlite = _max_sqlite_sizes(peak_sqlite, _sqlite_sizes(context["db_path"]))
        if result is None:
            result = rebuild_position_lots_from_trade_events(context["repo"])
        after_replay = _sqlite_sizes(context["db_path"])
        rows = context["repo"].list_position_lots(conn=context["keeper"])
        output = _writer_output(result, rows=rows, event_count=len(events))
        context["keeper"].execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after_checkpoint = _sqlite_sizes(context["db_path"])
        lot_count = int(output["counts"]["projected_lot_count"])
        return {
            "measurement_scope": "temporary_sqlite_load_decode_publishability_global_replace",
            "wall_time_ns": _timing_distribution(wall_samples),
            "cpu_time_ns": _timing_distribution(cpu_samples),
            "output": output,
            "sqlite_bytes": {
                "before_replay": context["before"],
                "peak_observed_after_repetition": peak_sqlite,
                "after_replay_before_checkpoint": after_replay,
                "steady_state_after_wal_checkpoint_truncate": after_checkpoint,
            },
            "sql": {
                "count_basis": "known_current_writer_operations",
                "application_statement_count_per_replay": 2 + lot_count,
                "select_statements_per_replay": 1,
                "delete_statements_per_replay": 1,
                "insert_statements_per_replay": lot_count,
                "trade_event_rows_read_per_replay": len(events),
                "position_lot_rows_inserted_per_replay": lot_count,
                "publication_behavior": "global_delete_then_insert",
            },
        }


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


@contextmanager
def _temporary_writer(events: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="om-synthetic-ledger-") as temp_name:
        data_config = Path(temp_name) / "data.json"
        data_config.write_text("{}\n", encoding="utf-8")
        repo = open_position_ledger(data_config)
        db_path = Path(repo.db_path)
        keeper = repo._connect()
        try:
            for event in events:
                repo.upsert_trade_event(event, conn=keeper)
            keeper.commit()
            keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = _sqlite_sizes(db_path)
            yield {"repo": repo, "keeper": keeper, "db_path": db_path, "before": before}
        finally:
            keeper.close()


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


def _max_sqlite_sizes(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    return {key: max(int(left.get(key, 0)), int(right.get(key, 0))) for key in set(left) | set(right)}


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


def _lifecycle_sidecar_counts(repo: Any) -> dict[str, int]:
    conn = repo._connect()
    try:
        return {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in (
                "trade_lifecycle_attempt_audit_heads",
                "trade_lifecycle_attempt_audits",
                "trade_lifecycle_observation_spans",
                "trade_lifecycle_receipt_blobs",
            )
        }
    finally:
        conn.close()


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


def _persist_lifecycle_benchmark_observation(
    repo: Any,
    *,
    observation: Mapping[str, Any],
    invocation_index: int,
    attempted_at_ms: int,
    trace: list[str] | None = None,
) -> dict[str, Any]:
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id="benchmark-case",
        invocation_id=_lifecycle_benchmark_invocation(invocation_index),
        attempted_at_ms=attempted_at_ms,
        outcome_kind="observed_complete",
        observation=observation,
    )
    result = _append_lifecycle_benchmark_envelope(
        repo,
        envelope=envelope,
        trace=trace,
    )
    return {
        **result,
        "invocation_id": envelope.invocation_id.hex(),
        "attempted_at_ms": envelope.attempted_at_ms,
        "receipt_sha256": envelope.receipt_sha256.hex(),
        "receipt_uncompressed_bytes": int(envelope.receipt_uncompressed_bytes or 0),
        "receipt_compressed_bytes": int(envelope.receipt_compressed_bytes or 0),
    }


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


def _explain_lifecycle_hot_lookups(repo: Any) -> dict[str, list[str]]:
    conn = repo._connect()
    try:
        head = repo.get_trade_lifecycle_attempt_audit_head(
            case_id="benchmark-case",
            conn=conn,
        )
        if head is None:
            raise RuntimeError("lifecycle benchmark head is missing")
        audit_case_key = int(head["audit_case_key"])
        invocation = head["last_invocation_id"]

        def explain(sql: str, params: Sequence[Any]) -> list[str]:
            return [
                str(row["detail"])
                for row in conn.execute("EXPLAIN QUERY PLAN " + sql, tuple(params))
            ]

        return {
            "head": explain(
                "SELECT audit_case_key,last_ordinal,chain_sha256,current_span_ordinal,"
                "last_invocation_id FROM trade_lifecycle_attempt_audit_heads WHERE case_id=?",
                ("benchmark-case",),
            ),
            "invocation": explain(
                "SELECT ordinal FROM trade_lifecycle_attempt_audits "
                "WHERE audit_case_key=? AND invocation_id=?",
                (audit_case_key, invocation),
            ),
            "span": explain(
                "SELECT last_receipt_sha256 FROM trade_lifecycle_observation_spans "
                "WHERE audit_case_key=? AND span_ordinal=?",
                (audit_case_key, 1),
            ),
            "orphan_reference": explain(
                "SELECT 1 FROM trade_lifecycle_observation_spans "
                "WHERE last_receipt_sha256=? LIMIT 1",
                (bytes(32),),
            ),
            "account_heads": explain(
                "SELECT audit_head.audit_case_key,audit_head.case_id "
                "FROM trade_lifecycle_cases AS lifecycle_case "
                "JOIN trade_lifecycle_attempt_audit_heads AS audit_head "
                "ON audit_head.case_id=lifecycle_case.case_id "
                "WHERE lifecycle_case.account=? ORDER BY lifecycle_case.case_id ASC",
                ("lx",),
            ),
        }
    finally:
        conn.close()


def _measure_lifecycle_sealing(repo: Any, *, root: Path) -> dict[str, Any]:
    from src.application.ledger.api import build_lifecycle_attempt_run_seal
    import src.application.trades.state as trade_state

    account_heads = repo.list_trade_lifecycle_attempt_audit_heads_for_account(
        account="lx"
    )
    if len(account_heads) != 1:
        raise RuntimeError("lifecycle benchmark seal head is missing")
    head = account_heads[0]
    audit_path = root / "benchmark-audit.jsonl"
    original_flock = trade_state.fcntl.flock
    original_fsync = trade_state.os.fsync
    lock_wait_samples: list[int] = []
    fsync_calls = 0

    def measured_flock(fd: int, operation: int) -> Any:
        started = time.perf_counter_ns()
        result = original_flock(fd, operation)
        if operation & trade_state.fcntl.LOCK_EX:
            lock_wait_samples.append(time.perf_counter_ns() - started)
        return result

    def counted_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(fd)

    trade_state.fcntl.flock = measured_flock
    trade_state.os.fsync = counted_fsync
    try:
        touched = build_lifecycle_attempt_run_seal(
            account="lx",
            source_id="benchmark-source",
            completed_at_ms=1_900_000_200_000,
            seal_scope="touched_heads",
            reason="ordinary_due",
            heads=[head],
        )
        started = time.perf_counter_ns()
        trade_state.append_trade_intake_audit(
            audit_path,
            touched,
            durable=True,
        )
        touched_wall = time.perf_counter_ns() - started
        touched_fsync = fsync_calls
        started = time.perf_counter_ns()
        checkpoint = trade_state.append_lifecycle_attempt_checkpoint_seal(
            audit_path,
            repo=repo,
            account="lx",
            source_id="benchmark-source",
            completed_at_ms=1_900_000_201_000,
            reason="process_startup",
        )
        checkpoint_wall = time.perf_counter_ns() - started
        checkpoint_fsync = fsync_calls - touched_fsync
        before_non_durable_fsync = fsync_calls
        trade_state.append_trade_intake_audit(
            audit_path,
            {"schema_version": "benchmark_non_durable.v1"},
            durable=False,
        )
        non_durable_fsync = fsync_calls - before_non_durable_fsync
    finally:
        trade_state.fcntl.flock = original_flock
        trade_state.os.fsync = original_fsync
    stress_path = root / "benchmark-concurrent-audit.jsonl"
    worker = (
        "import sys;"
        "from src.application.trades.state import append_trade_intake_audit as append;"
        "[append(sys.argv[1],{'writer':sys.argv[2],'index':i},durable=sys.argv[3]=='1') "
        "for i in range(int(sys.argv[4]))]"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(stress_path), name, durable, count],
            cwd=REPO_ROOT,
        )
        for name, durable, count in (("ordinary", "0", "50"), ("durable", "1", "10"))
    ]
    try:
        for process in processes:
            process.wait(timeout=30)
            if process.returncode != 0:
                raise RuntimeError("lifecycle concurrent append worker failed")
    except Exception:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        raise
    raw_lines = stress_path.read_bytes().splitlines()
    stress_rows = [json.loads(line) for line in raw_lines]
    writer_counts = {
        name: sum(row.get("writer") == name for row in stress_rows)
        for name in ("ordinary", "durable")
    }
    return {
        "touched_heads": {
            "head_count": int(touched["head_count"]),
            "wall_time_ns": touched_wall,
            "flush_fsync_count": touched_fsync,
            "sqlite_attempt_rows_read": 0,
        },
        "account_checkpoint": {
            "head_count": int(checkpoint["head_count"]),
            "wall_time_ns": checkpoint_wall,
            "flush_fsync_count": checkpoint_fsync,
            "sqlite_attempt_rows_read": 0,
        },
        "non_durable_append": {"fsync_count": non_durable_fsync},
        "concurrent_append_stress": {
            "process_count": len(processes),
            "line_count": len(stress_rows),
            "writer_counts": writer_counts,
            "all_lines_complete_json": stress_path.read_bytes().endswith(b"\n")
            and len(stress_rows) == 60
            and writer_counts == {"ordinary": 50, "durable": 10},
        },
        "exclusive_flock_wait_ns": _timing_distribution(lock_wait_samples),
    }


def _lifecycle_forbidden_work(trace: Sequence[str]) -> dict[str, int]:
    statements = [" ".join(statement.upper().split()) for statement in trace]
    mutations = tuple(
        statement
        for statement in statements
        if statement.startswith(("INSERT", "UPDATE", "DELETE"))
    )
    return {
        "attempt_history_scan": sum(
            "FROM TRADE_LIFECYCLE_ATTEMPT_AUDITS" in statement
            and "INVOCATION_ID" not in statement.partition(" WHERE ")[2]
            for statement in statements
        ),
        "evidence_history_scan": sum(
            "FROM TRADE_LIFECYCLE_EVIDENCE" in statement
            for statement in statements
        ),
        "full_replay": sum(
            "FROM TRADE_EVENTS" in statement for statement in statements
        ),
        "global_blob_sweep": sum(
            "DELETE FROM TRADE_LIFECYCLE_RECEIPT_BLOBS" in statement
            and "WHERE RECEIPT_SHA256" not in statement
            for statement in statements
        ),
        "decision_projection_write": sum(
            "POSITION_PROJECTION_" in statement for statement in mutations
        ),
        "per_attempt_checkpoint": sum(
            "WAL_CHECKPOINT" in statement for statement in statements
        ),
    }


def _measure_lifecycle_runtime_sealing() -> dict[str, Any]:
    from src.application.trades.inbox import (
        finish_settlement_attempt_provider_invocation,
        mark_settlement_attempt_provider_started,
        reserve_settlement_attempt_invocation,
        upsert_settlement_attempt_state,
    )
    from src.application.trades.lifecycle_runtime import (
        reconcile_due_lifecycle_cases_for_source,
    )
    from src.application.trades.settlement_attempts import (
        SettlementAttemptOutcome,
        SettlementCapabilitySnapshot,
        SettlementCollectorContract,
        case_scope_fingerprint,
        prepare_provider_required_state,
    )

    class NoCallCollector:
        contract = SettlementCollectorContract(required_capability_keys=())
        capability = SettlementCapabilitySnapshot(
            contract_version=contract.contract_version,
            gateway_adapter_version="benchmark",
            provider_sdk_version="benchmark",
            capability_fingerprint="benchmark-capability",
            capabilities={},
        )

        def __init__(self) -> None:
            self.calls = 0

        def collect_outcome(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("lifecycle seal benchmark called provider")

    with tempfile.TemporaryDirectory(prefix="om-lifecycle-runtime-seal-") as temp_name:
        root = Path(temp_name)
        config_path = root / "data.json"
        config_path.write_text("{}\n", encoding="utf-8")
        repo = open_position_ledger(config_path)
        case_id = "benchmark-runtime-case"
        repo.upsert_trade_lifecycle_case(
            {
                "schema_version": "lifecycle_case.v2",
                "case_id": case_id,
                "case_key": case_id,
                "account": "lx",
                "broker": "futu",
                "symbol": "NVDA",
                "status": "waiting_settlement_evidence",
                "futu_account_id": "1001",
                "contract_key": "benchmark-contract",
                "target_contracts_by_lot": {"benchmark-lot": 1},
                "observation_start_ms": 1,
                "pending_until_ms": 2,
                "derived_summary": {"reason_state": "cause_pending"},
            }
        )
        candidate = repo.list_trade_lifecycle_due_candidates(account="lx")[0]
        collector = NoCallCollector()
        source_id = "benchmark-source"
        inbox_path = root / "inbox.sqlite3"
        control_now_ms = 200_000
        state = prepare_provider_required_state(
            None,
            source_id=source_id,
            account="lx",
            case_id=case_id,
            case_scope_fingerprint_value=case_scope_fingerprint(candidate),
            provider_input_scope_fingerprint_value="benchmark-provider-scope",
            contract_version=collector.contract.contract_version,
            capability_fingerprint=collector.capability.capability_fingerprint,
            now_ms=1,
        )
        upsert_settlement_attempt_state(inbox_path, state=state)
        reserved = reserve_settlement_attempt_invocation(
            inbox_path,
            source_id=source_id,
            account="lx",
            case_id=case_id,
            case_scope_fingerprint=str(state["case_scope_fingerprint"]),
            claim_id="benchmark-claim",
            now_ms=1,
            lease_ms=1,
        )
        if reserved is None:
            raise RuntimeError("lifecycle runtime seal benchmark reservation failed")
        started = mark_settlement_attempt_provider_started(
            inbox_path,
            source_id=source_id,
            account="lx",
            case_id=case_id,
            claim_id="benchmark-claim",
            invocation_id=str(reserved["invocation_id"]),
            attempted_at_ms=2,
        )
        outcome = SettlementAttemptOutcome(
            kind="unknown_error",
            source_id=source_id,
            account="lx",
            case_id=case_id,
            contract_version=collector.contract.contract_version,
            capability_fingerprint=collector.capability.capability_fingerprint,
            reason_code="benchmark_provider_failed",
            error_class="BenchmarkProviderError",
        )
        envelope = build_lifecycle_attempt_audit_envelope(
            case_id=case_id,
            invocation_id=str(started["invocation_id"]),
            attempted_at_ms=2,
            outcome_kind="unknown_error",
            reason_code=outcome.reason_code,
            error_class=outcome.error_class,
        )
        finish_settlement_attempt_provider_invocation(
            inbox_path,
            source_id=source_id,
            account="lx",
            case_id=case_id,
            claim_id="benchmark-claim",
            invocation_id=str(started["invocation_id"]),
            outcome=outcome,
            outcome_code=envelope.outcome_code,
            semantic_fingerprint=envelope.semantic_fingerprint,
            receipt_sha256=envelope.receipt_sha256,
            diagnostic_sha256=envelope.diagnostic_sha256,
            control_now_ms=3,
        )
        _append_lifecycle_benchmark_envelope(
            repo,
            envelope=envelope,
            first_evidence_id=None,
        )

        forbidden_work = {
            "attempt_history_scan": 0,
            "evidence_history_scan": 0,
            "full_replay": 0,
            "global_blob_sweep": 0,
            "decision_projection_write": 0,
            "per_attempt_checkpoint": 0,
        }

        def forbid(key: str) -> Callable[..., Any]:
            def blocked(*_args: Any, **_kwargs: Any) -> Any:
                forbidden_work[key] += 1
                raise AssertionError(f"lifecycle runtime seal benchmark called {key}")

            return blocked

        repo.list_trade_events = forbid("full_replay")
        repo.list_trade_lifecycle_evidence = forbid("evidence_history_scan")
        repo.list_trade_lifecycle_attempt_audits = forbid("attempt_history_scan")
        repo.publish_full_position_projection_heads = forbid(
            "decision_projection_write"
        )
        source = {
            "id": source_id,
            "account": "lx",
            "futu_account_ids": ["1001"],
            "inbox_path": inbox_path,
            "settlement_observation": {"enabled": True},
        }
        first_seals: list[dict[str, Any]] = []
        first = reconcile_due_lifecycle_cases_for_source(
            repo,
            source=source,
            now_ms=control_now_ms,
            apply_changes=True,
            settlement_collector=collector,
            settlement_control_now_ms_fn=lambda: control_now_ms,
            seal_sink=first_seals.append,
        )
        second_seals: list[dict[str, Any]] = []
        second = reconcile_due_lifecycle_cases_for_source(
            repo,
            source=source,
            now_ms=control_now_ms,
            apply_changes=True,
            settlement_collector=collector,
            settlement_control_now_ms_fn=lambda: control_now_ms,
            seal_sink=second_seals.append,
        )
        checks = {
            "one_touched_one_seal": first.get("seal_status") == "sealed"
            and len(first_seals) == 1
            and first_seals[0].get("head_count") == 1,
            "zero_touched_no_seal": second.get("seal_status") == "not_required"
            and not second_seals,
            "provider_not_called": collector.calls == 0
            and int(first.get("provider_attempt_count") or 0) == 0
            and int(second.get("provider_attempt_count") or 0) == 0,
            "forbidden_work_zero": not any(forbidden_work.values()),
        }
        return {
            "checks": checks,
            "forbidden_work": forbidden_work,
            "one_touched": {
                "seal_status": first.get("seal_status"),
                "seal_count": len(first_seals),
            },
            "zero_touched": {
                "seal_status": second.get("seal_status"),
                "seal_count": len(second_seals),
            },
            "provider_call_count": collector.calls,
            "status": "pass" if all(checks.values()) else "fail",
        }


def _measure_lifecycle_receipt_class(
    *,
    prior_attempts: int,
    receipt_bytes: int,
    warmups: int,
    repetitions: int,
    moves: int,
    seed: int,
) -> dict[str, Any]:
    with _temporary_lifecycle_attempt_fixture(
        prior_attempts=prior_attempts,
        receipt_bytes=receipt_bytes,
        seed=seed,
    ) as fixture:
        repo = fixture["repo"]
        observation = fixture["observation"]
        compact_delta = (
            int(fixture["fixture_sqlite"]["total_bytes"])
            - int(fixture["baseline_sqlite"]["total_bytes"])
        )
        wall_samples: list[int] = []
        cpu_samples: list[int] = []
        next_invocation = int(prior_attempts) + 1
        for index in range(warmups + repetitions):
            invocation_index = next_invocation
            next_invocation += 1
            started_wall = time.perf_counter_ns()
            started_cpu = time.process_time_ns()
            _persist_lifecycle_benchmark_observation(
                repo,
                observation=observation,
                invocation_index=invocation_index,
                attempted_at_ms=1_900_100_000_000 + invocation_index,
            )
            cpu_elapsed = time.process_time_ns() - started_cpu
            wall_elapsed = time.perf_counter_ns() - started_wall
            if index >= warmups:
                wall_samples.append(wall_elapsed)
                cpu_samples.append(cpu_elapsed)

        allocation_invocation = next_invocation
        next_invocation += 1
        allocation, allocation_output = _allocation_call(
            lambda: _persist_lifecycle_benchmark_observation(
                repo,
                observation=observation,
                invocation_index=allocation_invocation,
                attempted_at_ms=1_900_100_000_000 + allocation_invocation,
            ),
            output_fn=lambda result: dict(result),
        )
        before_replay_counts = _lifecycle_sidecar_counts(repo)
        before_replay_bytes = _checkpoint_lifecycle_repo(repo)
        replay = _persist_lifecycle_benchmark_observation(
            repo,
            observation=observation,
            invocation_index=allocation_invocation,
            attempted_at_ms=1_900_100_000_000 + allocation_invocation,
        )
        after_replay_counts = _lifecycle_sidecar_counts(repo)
        after_replay_bytes = _checkpoint_lifecycle_repo(repo)

        trace: list[str] = []
        probe_invocation = next_invocation
        next_invocation += 1
        probe = _persist_lifecycle_benchmark_observation(
            repo,
            observation=observation,
            invocation_index=probe_invocation,
            attempted_at_ms=1_900_100_000_000 + probe_invocation,
            trace=trace,
        )
        move_start = _checkpoint_lifecycle_repo(repo)
        peak = dict(move_start)
        maximum_wal_growth = 0
        move_wall_samples: list[int] = []
        for move_index in range(1, moves + 1):
            moving_observation = _lifecycle_benchmark_observation(
                target_bytes=receipt_bytes,
                nonce=move_index,
                seed=seed,
            )
            before = _sqlite_sizes(Path(repo.db_path))
            invocation_index = next_invocation
            next_invocation += 1
            started = time.perf_counter_ns()
            _persist_lifecycle_benchmark_observation(
                repo,
                observation=moving_observation,
                invocation_index=invocation_index,
                attempted_at_ms=1_900_200_000_000 + invocation_index,
            )
            move_wall_samples.append(time.perf_counter_ns() - started)
            after = _sqlite_sizes(Path(repo.db_path))
            maximum_wal_growth = max(
                maximum_wal_growth,
                max(0, int(after["wal_bytes"]) - int(before["wal_bytes"])),
            )
            peak = _max_sqlite_sizes(peak, after)
        before_move_checkpoint = _sqlite_sizes(Path(repo.db_path))
        after_move_checkpoint = _checkpoint_lifecycle_repo(repo)
        conn = repo._connect()
        try:
            span = conn.execute(
                "SELECT COUNT(*) AS span_count,"
                "SUM(last_receipt_sha256 IS NOT NULL) AS last_reference_count,"
                "MIN(length(first_evidence_receipt_sha256)) AS commitment_min,"
                "MAX(length(first_evidence_receipt_sha256)) AS commitment_max "
                "FROM trade_lifecycle_observation_spans"
            ).fetchone()
            blob_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM trade_lifecycle_receipt_blobs"
                ).fetchone()[0]
            )
        finally:
            conn.close()
        verified = repo.verify_trade_lifecycle_attempt_audit_case(
            case_id="benchmark-case"
        )
        query_plans = _explain_lifecycle_hot_lookups(repo)
        sealing = _measure_lifecycle_sealing(repo, root=fixture["root"])
        receipt_uncompressed = int(allocation_output["receipt_uncompressed_bytes"])
        allocation_limit = max(
            LIFECYCLE_ALLOCATION_FLOOR_BYTES,
            3 * receipt_uncompressed,
        )
        wal_limit = max(
            LIFECYCLE_MOVE_WAL_FLOOR_BYTES,
            4 * receipt_uncompressed,
        )
        peak_limit = max(
            LIFECYCLE_MOVE_PEAK_FLOOR_BYTES,
            8 * receipt_uncompressed,
        )
        peak_growth = max(
            0,
            int(peak["total_bytes"]) - int(move_start["total_bytes"]),
        )
        indexed = all(
            any("SEARCH" in detail.upper() for detail in details)
            and not any("SCAN TRADE_LIFECYCLE_ATTEMPT_AUDITS" in detail.upper() for detail in details)
            for details in query_plans.values()
        )
        forbidden_work = _lifecycle_forbidden_work(trace)
        checks = {
            "duplicate_wall_p95": _timing_distribution(wall_samples)["p95"]
            <= LIFECYCLE_WALL_LIMIT_NS,
            "compact_bytes_per_attempt": compact_delta / prior_attempts
            <= LIFECYCLE_BYTES_PER_ATTEMPT_LIMIT,
            "python_peak_allocation": int(allocation["python_peak_bytes"])
            <= allocation_limit,
            "exact_replay_zero_rows": before_replay_counts == after_replay_counts,
            "exact_replay_zero_physical_bytes": before_replay_bytes == after_replay_bytes,
            "one_optional_last_receipt": int(span["span_count"] or 0) == 1
            and blob_count <= 1
            and int(span["last_reference_count"] or 0) <= 1,
            "fixed_first_receipt_commitment": int(span["commitment_min"] or 0) == 32
            and int(span["commitment_max"] or 0) == 32,
            "moving_receipt_wal": maximum_wal_growth <= wal_limit,
            "moving_receipt_peak": peak_growth <= peak_limit,
            "checkpoint_returns_to_steady_wal": int(after_move_checkpoint["wal_bytes"]) == 0,
            "indexed_hot_lookups": indexed,
            "shadow_verifier": verified["status"] == "valid",
            "one_fsync_per_seal": sealing["touched_heads"]["flush_fsync_count"] == 1
            and sealing["account_checkpoint"]["flush_fsync_count"] == 1,
            "non_durable_no_fsync": sealing["non_durable_append"]["fsync_count"] == 0,
            "concurrent_append_integrity": sealing["concurrent_append_stress"][
                "all_lines_complete_json"
            ],
            "forbidden_work_zero": not any(forbidden_work.values()),
        }
        return {
            "fixture_sha256": fixture["fixture_sha256"],
            "receipt": {
                "uncompressed_bytes": receipt_uncompressed,
                "compressed_bytes": int(allocation_output["receipt_compressed_bytes"]),
                "compression_ratio": round(
                    int(allocation_output["receipt_compressed_bytes"])
                    / receipt_uncompressed,
                    6,
                ),
                "compression_class": "deterministic_high_entropy_urlsafe_base85",
            },
            "timing": {
                "cold_state": "fresh_temporary_db_fixture_setup_excluded",
                "warm_state": "warm_os_page_cache_not_flushed",
                "duplicate_observation_wall_time_ns": _timing_distribution(wall_samples),
                "duplicate_observation_cpu_time_ns": _timing_distribution(cpu_samples),
                "moving_receipt_wall_time_ns": _timing_distribution(move_wall_samples),
            },
            "allocation": allocation,
            "space": {
                "baseline_before_sidecar": fixture["baseline_sqlite"],
                "fixture_after_prior_attempts": fixture["fixture_sqlite"],
                "incremental_compact_bytes": compact_delta,
                "incremental_compact_bytes_per_attempt": round(
                    compact_delta / prior_attempts,
                    6,
                ),
                "before_exact_replay": before_replay_bytes,
                "after_exact_replay": after_replay_bytes,
                "move_start_checkpoint": move_start,
                "move_peak_observed": peak,
                "move_before_final_checkpoint": before_move_checkpoint,
                "move_after_final_checkpoint": after_move_checkpoint,
                "maximum_single_move_wal_growth_bytes": maximum_wal_growth,
                "peak_growth_bytes_including_compact_rows": peak_growth,
            },
            "rows": {
                "before_exact_replay": before_replay_counts,
                "after_exact_replay": after_replay_counts,
                "span_count": int(span["span_count"] or 0),
                "last_receipt_reference_count": int(span["last_reference_count"] or 0),
                "receipt_blob_count": blob_count,
                "first_receipt_commitment_min_bytes": int(span["commitment_min"] or 0),
                "first_receipt_commitment_max_bytes": int(span["commitment_max"] or 0),
            },
            "exact_replay": replay,
            "hot_write_probe": {
                "result": probe,
                "sql_statement_count": len(trace),
                "select_statement_count": sum(
                    statement.lstrip().upper().startswith("SELECT")
                    for statement in trace
                ),
                "mutation_statement_count": sum(
                    statement.lstrip().upper().startswith(
                        ("INSERT", "UPDATE", "DELETE")
                    )
                    for statement in trace
                ),
                "attempt_history_scan_count": forbidden_work[
                    "attempt_history_scan"
                ],
                "evidence_history_scan_count": forbidden_work[
                    "evidence_history_scan"
                ],
                "checkpoint_statement_count": forbidden_work[
                    "per_attempt_checkpoint"
                ],
                "envelope_build_count": 1,
                "compression_count": 1,
            },
            "forbidden_work": forbidden_work,
            "query_plans": query_plans,
            "sealing": sealing,
            "shadow_verifier": verified,
            "limits": {
                "wall_p95_ns": LIFECYCLE_WALL_LIMIT_NS,
                "compact_bytes_per_attempt": LIFECYCLE_BYTES_PER_ATTEMPT_LIMIT,
                "python_peak_allocation_bytes": allocation_limit,
                "single_move_wal_growth_bytes": wal_limit,
                "moving_peak_growth_bytes": peak_limit,
            },
            "checks": checks,
            "status": "pass" if all(checks.values()) else "fail",
        }


def run_lifecycle_attempt_audit_benchmark(
    *,
    repo_root: str | Path,
    output_dir: str | Path,
    warmups: int = DEFAULT_WARMUPS,
    repetitions: int = DEFAULT_REPETITIONS,
    seed: int = SEED,
    prior_attempts: int = LIFECYCLE_ATTEMPT_COUNT,
    receipt_bytes: int = LIFECYCLE_RECEIPT_BYTES,
    p99_receipt_bytes: int = LIFECYCLE_P99_RECEIPT_BYTES,
    moves: int = LIFECYCLE_MOVE_COUNT,
    reference_host_fingerprint: str | None = None,
) -> dict[str, Any]:
    base = Path(repo_root).expanduser().resolve(strict=True)
    warmup_count = _bounded_nonnegative_int(warmups, name="warmups", maximum=100)
    repetition_count = _bounded_positive_int(
        repetitions,
        name="repetitions",
        maximum=1_000,
    )
    fixture_seed = _bounded_nonnegative_int(seed, name="seed", maximum=2**31 - 1)
    attempt_count = _bounded_positive_int(
        prior_attempts,
        name="lifecycle prior attempts",
        maximum=1_000_000,
    )
    move_count = _bounded_positive_int(moves, name="lifecycle moves", maximum=10_000)
    host = _host_profile()
    reference = _validated_reference_fingerprint(reference_host_fingerprint)
    comparable = reference is not None and reference == host["fingerprint"]
    classes = []
    seen_sizes: set[int] = set()
    for label, size in (
        ("fixed_64_kib", receipt_bytes),
        ("p99_receipt_class", p99_receipt_bytes),
    ):
        size_value = _bounded_positive_int(
            size,
            name=f"{label} receipt bytes",
            maximum=4 * 1024 * 1024,
        )
        if size_value in seen_sizes:
            continue
        seen_sizes.add(size_value)
        classes.append(
            {
                "key": label,
                **_measure_lifecycle_receipt_class(
                    prior_attempts=attempt_count,
                    receipt_bytes=size_value,
                    warmups=warmup_count,
                    repetitions=repetition_count,
                    moves=move_count,
                    seed=fixture_seed,
                ),
            }
        )
    runtime_sealing = _measure_lifecycle_runtime_sealing()
    forbidden_keys = tuple(runtime_sealing["forbidden_work"])
    forbidden_work = {
        key: int(runtime_sealing["forbidden_work"].get(key) or 0)
        + sum(
            int(item.get("forbidden_work", {}).get(key) or 0)
            for item in classes
        )
        for key in forbidden_keys
    }
    acceptance_dimensions = (
        attempt_count == LIFECYCLE_ATTEMPT_COUNT
        and int(receipt_bytes) == LIFECYCLE_RECEIPT_BYTES
        and int(p99_receipt_bytes) == LIFECYCLE_P99_RECEIPT_BYTES
        and move_count == LIFECYCLE_MOVE_COUNT
        and warmup_count == DEFAULT_WARMUPS
        and repetition_count == DEFAULT_REPETITIONS
    )
    resource_pass = (
        all(item["status"] == "pass" for item in classes)
        and runtime_sealing["status"] == "pass"
        and not any(forbidden_work.values())
    )
    status = (
        "pass"
        if acceptance_dimensions and comparable and resource_pass
        else "fail"
        if acceptance_dimensions and comparable
        else "not_evaluable"
    )
    artifact = {
        "schema_version": LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(base),
        "fixture_seed": fixture_seed,
        "run_label": (
            "acceptance_5_warmups_30_repetitions"
            if acceptance_dimensions
            else "non_acceptance_smoke"
        ),
        "host": host,
        "reference_host": {
            "fingerprint": reference,
            "comparable": comparable,
        },
        "dimensions": {
            "prior_attempts": attempt_count,
            "warmups": warmup_count,
            "repetitions": repetition_count,
            "moving_receipt_observations": move_count,
            "receipt_classes_bytes": sorted(seen_sizes),
        },
        "baseline_p99_receipt_class": {
            "bytes": int(p99_receipt_bytes),
            "status": (
                "measured_read_only"
                if int(p99_receipt_bytes) == LIFECYCLE_P99_RECEIPT_BYTES
                else "non_acceptance_override"
            ),
            "source": (
                "liuxie-incus production ledger 2026-08-15"
                if int(p99_receipt_bytes) == LIFECYCLE_P99_RECEIPT_BYTES
                else "caller_override"
            ),
            "method": "nearest_rank_p99_canonical_observation_json_bytes",
            "eligible_row_count": (
                2_250
                if int(p99_receipt_bytes) == LIFECYCLE_P99_RECEIPT_BYTES
                else None
            ),
            "invalid_row_count": (
                0
                if int(p99_receipt_bytes) == LIFECYCLE_P99_RECEIPT_BYTES
                else None
            ),
        },
        "classes": classes,
        "runtime_sealing": runtime_sealing,
        "forbidden_work": forbidden_work,
        "status": status,
        "status_reason": (
            "all_lifecycle_attempt_gates_pass"
            if status == "pass"
            else "lifecycle_attempt_gate_failed"
            if status == "fail"
            else "smoke_or_reference_host_not_comparable"
        ),
        "automatic_actions": [],
    }
    target = _publish_lifecycle_attempt_artifact(
        output_dir=output_dir,
        repo_root=base,
        artifact=artifact,
    )
    return {
        "status": status,
        "output_dir": str(target),
        "artifact": str(target / LIFECYCLE_ATTEMPT_ARTIFACT_FILENAME),
        "class_count": len(classes),
    }


def _projection_output(projection: Any, *, event_count: int) -> dict[str, Any]:
    lots = [lot.to_dict() for lot in projection.lots]
    diagnostics = [item.to_dict() for item in projection.diagnostics]
    ledger_projection = projection.ledger_projection
    return _canonical_output(
        events=event_count,
        lots=lots,
        diagnostics=diagnostics,
        risk_view_count=len(ledger_projection.views),
        allocation_count=len(ledger_projection.allocations),
    )


def _writer_output(result: Any, *, rows: Sequence[dict[str, Any]], event_count: int) -> dict[str, Any]:
    payload = result.to_dict() if callable(getattr(result, "to_dict", None)) else dict(result)
    return _canonical_output(
        events=event_count,
        lots=list(rows),
        diagnostics=list(payload.get("projection_diagnostics") or []),
        risk_view_count=None,
        allocation_count=None,
    )


def _canonical_output(
    *,
    events: int,
    lots: Sequence[dict[str, Any]],
    diagnostics: Sequence[dict[str, Any]],
    risk_view_count: int | None,
    allocation_count: int | None,
) -> dict[str, Any]:
    canonical_lots = sorted(
        [
            {
                "record_id": str(item.get("record_id") or ""),
                "fields": dict(item.get("fields") or {}),
            }
            for item in lots
        ],
        key=lambda item: item["record_id"],
    )
    canonical_diagnostics = [dict(item) for item in diagnostics]
    open_lots = sum(1 for item in canonical_lots if int((item["fields"].get("contracts_open") or 0)) > 0)
    lot_fingerprint = _sha256_json(canonical_lots)
    diagnostic_fingerprint = _sha256_json(canonical_diagnostics)
    return {
        "lot_fingerprint": lot_fingerprint,
        "diagnostic_fingerprint": diagnostic_fingerprint,
        "combined_fingerprint": _sha256_json({"lots": canonical_lots, "diagnostics": canonical_diagnostics}),
        "counts": {
            "event_count": int(events),
            "projected_lot_count": len(canonical_lots),
            "open_lot_count": open_lots,
            "risk_view_count": risk_view_count,
            "allocation_count": allocation_count,
            "diagnostic_count": len(canonical_diagnostics),
        },
    }


def _projection_parity(projector: Mapping[str, Any], writer: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("lot_fingerprint", "diagnostic_fingerprint", "combined_fingerprint")
    mismatches = [field for field in fields if projector.get(field) != writer.get(field)]
    return {
        "exact": not mismatches,
        "mismatched_fingerprints": mismatches,
        "writer_risk_and_allocation_counts": "bound_to_same_canonical_projection_not_exposed_by_writer_result",
    }


def _output_counts(output: Mapping[str, Any]) -> dict[str, Any]:
    return dict(output.get("counts") or {})


def _assert_expected_counts(spec: Mapping[str, Any], output: Mapping[str, Any]) -> None:
    expected = spec.get("effective_dimensions")
    counts = output.get("counts")
    if not isinstance(expected, Mapping) or not isinstance(counts, Mapping):
        raise RuntimeError("fixture counts are missing")
    names = (
        "event_count",
        "projected_lot_count",
        "open_lot_count",
        "risk_view_count",
        "allocation_count",
    )
    mismatches = {
        name: {"expected": expected.get(name), "actual": counts.get(name)}
        for name in names
        if expected.get(name) != counts.get(name)
    }
    if mismatches:
        raise RuntimeError(f"fixture output cardinality mismatch: {mismatches}")


def _measure_cpu_scenario(spec: Mapping[str, Any], *, seed: int) -> dict[str, Any]:
    events = _build_synthetic_events(spec, seed=seed)
    projector_profile, projector_output = _profile_call(
        lambda: project_stored_trade_events_to_position_lots(events),
        output_fn=lambda result: _projection_output(result, event_count=len(events)),
    )
    with _temporary_writer(events) as context:
        writer_profile, writer_output = _profile_call(
            lambda: rebuild_position_lots_from_trade_events(context["repo"]),
            output_fn=lambda result: _writer_output(
                result,
                rows=context["repo"].list_position_lots(conn=context["keeper"]),
                event_count=len(events),
            ),
        )
    parity = _projection_parity(projector_output, writer_output)
    if not parity["exact"]:
        raise RuntimeError(f"CPU profile parity failed for {spec.get('key')}")
    return {
        "key": str(spec.get("key") or ""),
        "fixture_sha256": _events_sha256(events),
        "parity": parity,
        "components": {
            "projector_only": projector_profile,
            "existing_full_replay_writer": writer_profile,
        },
    }


def _profile_call(
    fn: Callable[[], Any],
    *,
    output_fn: Callable[[Any], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    profiler = cProfile.Profile()
    profiler.enable()
    result = fn()
    profiler.disable()
    output = output_fn(result)
    stats = pstats.Stats(profiler)
    rows = []
    for (filename, line, function), values in sorted(
        stats.stats.items(),
        key=lambda item: (-float(item[1][3]), str(item[0])),
    )[:30]:
        primitive_calls, total_calls, total_time, cumulative_time, _callers = values
        rows.append(
            {
                "function": function,
                "location": _profile_location(filename, line),
                "primitive_calls": int(primitive_calls),
                "total_calls": int(total_calls),
                "self_seconds": round(float(total_time), 9),
                "cumulative_seconds": round(float(cumulative_time), 9),
            }
        )
    return {
        "profiled_invocations": 1,
        "total_calls": int(stats.total_calls),
        "primitive_calls": int(stats.prim_calls),
        "total_seconds": round(float(stats.total_tt), 9),
        "top_cumulative_functions": rows,
    }, output


def _profile_location(filename: str, line: int) -> str:
    path = Path(filename)
    parts = path.parts
    for marker in ("domain", "src", "scripts"):
        if marker in parts:
            index = parts.index(marker)
            return f"{'/'.join(parts[index:])}:{line}"
    return f"{path.name}:{line}"


def _measure_allocation_scenario(spec: Mapping[str, Any], *, seed: int) -> dict[str, Any]:
    events = _build_synthetic_events(spec, seed=seed)
    projector_allocation, projector_output = _allocation_call(
        lambda: project_stored_trade_events_to_position_lots(events),
        output_fn=lambda result: _projection_output(result, event_count=len(events)),
    )
    with _temporary_writer(events) as context:
        writer_allocation, writer_output = _allocation_call(
            lambda: rebuild_position_lots_from_trade_events(context["repo"]),
            output_fn=lambda result: _writer_output(
                result,
                rows=context["repo"].list_position_lots(conn=context["keeper"]),
                event_count=len(events),
            ),
        )
    parity = _projection_parity(projector_output, writer_output)
    if not parity["exact"]:
        raise RuntimeError(f"allocation profile parity failed for {spec.get('key')}")
    return {
        "key": str(spec.get("key") or ""),
        "fixture_sha256": _events_sha256(events),
        "parity": parity,
        "components": {
            "projector_only": projector_allocation,
            "existing_full_replay_writer": writer_allocation,
        },
    }


def _allocation_call(
    fn: Callable[[], Any],
    *,
    output_fn: Callable[[Any], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    rss_before = _peak_rss_bytes()
    tracemalloc.start()
    try:
        result = fn()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    output = output_fn(result)
    top_rows = []
    for stat in snapshot.statistics("lineno")[:30]:
        frame = stat.traceback[0]
        top_rows.append(
            {
                "location": _profile_location(frame.filename, frame.lineno),
                "size_bytes": int(stat.size),
                "allocation_count": int(stat.count),
            }
        )
    return {
        "profiled_invocations": 1,
        "python_current_bytes": int(current_bytes),
        "python_peak_bytes": int(peak_bytes),
        "peak_rss_before_bytes": rss_before,
        "peak_rss_after_bytes": _peak_rss_bytes(),
        "peak_rss_scope": "process_high_water_mark",
        "top_allocation_sites": top_rows,
    }, output


def _peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _validate_worker_artifacts(
    *,
    fixture_manifest: Mapping[str, Any],
    timing: Mapping[str, Any],
    cpu_profile: Mapping[str, Any],
    allocation_profile: Mapping[str, Any],
    expected_warmups: int,
    expected_repetitions: int,
    expected_run_label: str,
) -> None:
    expected_schemas = (
        (timing, TIMING_SCHEMA, "timing_without_profiler"),
        (cpu_profile, CPU_PROFILE_SCHEMA, "cprofile_separate_process"),
        (allocation_profile, ALLOCATION_PROFILE_SCHEMA, "tracemalloc_separate_process"),
    )
    for artifact, schema, mode in expected_schemas:
        if artifact.get("schema_version") != schema or artifact.get("measurement_mode") != mode:
            raise RuntimeError(f"worker artifact contract failed for {schema}")
    if (
        timing.get("profilers_enabled") is not False
        or timing.get("tracemalloc_enabled") is not False
        or timing.get("warmups") != expected_warmups
        or timing.get("repetitions") != expected_repetitions
        or timing.get("run_label") != expected_run_label
    ):
        raise RuntimeError("timing worker measurement contract is invalid")
    expected = {
        str(item.get("key")): str(item.get("fixture_sha256"))
        for item in fixture_manifest.get("scenarios", [])
        if isinstance(item, Mapping)
    }
    for artifact in (timing, cpu_profile, allocation_profile):
        rows = artifact.get("scenarios")
        actual = {
            str(item.get("key")): str(item.get("fixture_sha256"))
            for item in (rows if isinstance(rows, list) else [])
            if isinstance(item, Mapping)
        }
        if actual != expected:
            raise RuntimeError(f"worker fixture identity mismatch for {artifact.get('schema_version')}")
        for item in rows if isinstance(rows, list) else []:
            parity = item.get("parity") if isinstance(item, Mapping) else None
            if not isinstance(parity, Mapping) or parity.get("exact") is not True:
                raise RuntimeError(f"worker parity contract failed for {artifact.get('schema_version')}")
        expected_storage = fixture_manifest.get("research_storage_status")
        actual_storage = artifact.get("research_storage_status")
        if expected_storage is None:
            if actual_storage is not None:
                raise RuntimeError("unexpected research storage status worker artifact")
        else:
            if not isinstance(expected_storage, Mapping) or not isinstance(actual_storage, Mapping):
                raise RuntimeError("research storage status worker artifact is missing")
            if (
                actual_storage.get("key") != expected_storage.get("key")
                or actual_storage.get("fixture_sha256") != expected_storage.get("fixture_sha256")
                or actual_storage.get("setup_included") is not False
            ):
                raise RuntimeError("research storage status worker identity mismatch")
    _validate_phase_3a_worker_artifacts(
        fixture_manifest=fixture_manifest,
        timing=timing,
        cpu_profile=cpu_profile,
        allocation_profile=allocation_profile,
        repetitions=expected_repetitions,
    )
    for item in timing.get("scenarios", []):
        components = item.get("components") if isinstance(item, Mapping) else None
        if not isinstance(components, Mapping):
            raise RuntimeError("timing scenario components are missing")
        for component in ("projector_only", "existing_full_replay_writer"):
            payload = components.get(component)
            if not isinstance(payload, Mapping):
                raise RuntimeError(f"timing component is missing: {component}")
            _validate_timing_distribution(
                payload.get("wall_time_ns"),
                repetitions=expected_repetitions,
                label=f"{component}.wall_time_ns",
            )
            _validate_timing_distribution(
                payload.get("cpu_time_ns"),
                repetitions=expected_repetitions,
                label=f"{component}.cpu_time_ns",
            )
    storage_timing = timing.get("research_storage_status")
    if isinstance(storage_timing, Mapping):
        _validate_timing_distribution(
            storage_timing.get("wall_time_ns"),
            repetitions=expected_repetitions,
            label="research_storage_status.wall_time_ns",
        )
        _validate_timing_distribution(
            storage_timing.get("cpu_time_ns"),
            repetitions=expected_repetitions,
            label="research_storage_status.cpu_time_ns",
        )


def _validate_phase_3a_worker_artifacts(
    *,
    fixture_manifest: Mapping[str, Any],
    timing: Mapping[str, Any],
    cpu_profile: Mapping[str, Any],
    allocation_profile: Mapping[str, Any],
    repetitions: int,
) -> None:
    expected = fixture_manifest.get("phase_3a")
    artifacts = (
        (timing, "data_storage_projection_phase3a_timing.v1"),
        (cpu_profile, "data_storage_projection_phase3a_cpu_profile.v1"),
        (allocation_profile, "data_storage_projection_phase3a_allocation_profile.v1"),
    )
    if expected is None:
        if any(artifact.get("phase_3a") is not None for artifact, _schema in artifacts):
            raise RuntimeError("unexpected Phase 3A worker artifact")
        return
    if not isinstance(expected, Mapping):
        raise RuntimeError("Phase 3A fixture manifest is invalid")
    expected_hash = str(expected.get("fixture_sha256") or "")
    for artifact, schema in artifacts:
        payload = artifact.get("phase_3a")
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != schema
            or payload.get("fixture_sha256") != expected_hash
            or payload.get("setup_included") is not False
        ):
            raise RuntimeError(f"Phase 3A worker artifact contract failed: {schema}")
    phase_timing = timing["phase_3a"]
    comparable = phase_timing.get("comparable_facades")
    if not isinstance(comparable, list) or {row.get("key") for row in comparable} != {
        "single_combo_metadata_close",
        "atomic_batch_combo_metadata_adjust",
    }:
        raise RuntimeError("Phase 3A comparable facade set is incomplete")
    for row in comparable:
        for mode in ("forced_full", "fast"):
            payload = row.get(mode)
            if not isinstance(payload, Mapping):
                raise RuntimeError(f"Phase 3A timing mode is missing: {mode}")
            _validate_timing_distribution(
                payload.get("wall_time_ns"),
                repetitions=repetitions,
                label=f"phase_3a.{row.get('key')}.{mode}.wall_time_ns",
            )
            _validate_timing_distribution(
                payload.get("cpu_time_ns"),
                repetitions=repetitions,
                label=f"phase_3a.{row.get('key')}.{mode}.cpu_time_ns",
            )
    force_full = phase_timing.get("force_full_facades")
    if not isinstance(force_full, list) or {row.get("key") for row in force_full} != {
        "special_combo_identity_membership"
    }:
        raise RuntimeError("Phase 3A force-full facade set is incomplete")
    for row in force_full:
        payload = row.get("measurement")
        if not isinstance(payload, Mapping) or not row.get("reason"):
            raise RuntimeError("Phase 3A force-full measurement is incomplete")
        for clock in ("wall_time_ns", "cpu_time_ns"):
            _validate_timing_distribution(
                payload.get(clock),
                repetitions=repetitions,
                label=f"phase_3a.{row.get('key')}.{clock}",
            )
    checkpoint = phase_timing.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise RuntimeError("Phase 3A checkpoint timing is missing")
    for key in ("no_rotation", "rotation_100_events", "rotation_1_mib"):
        payload = checkpoint.get(key)
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Phase 3A checkpoint timing is missing: {key}")
        for clock in ("wall_time_ns", "cpu_time_ns"):
            _validate_timing_distribution(
                payload.get(clock),
                repetitions=repetitions,
                label=f"phase_3a.checkpoint.{key}.{clock}",
            )
    for key, payload in dict(phase_timing.get("current_reads") or {}).items():
        for clock in ("wall_time_ns", "cpu_time_ns"):
            _validate_timing_distribution(
                payload.get(clock) if isinstance(payload, Mapping) else None,
                repetitions=repetitions,
                label=f"phase_3a.current_reads.{key}.{clock}",
            )
    fingerprints = phase_timing.get("fingerprint_only")
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != {
        "current",
        "retained_lots_10x",
    }:
        raise RuntimeError("Phase 3A fingerprint timing set is incomplete")
    for key, payload in fingerprints.items():
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Phase 3A fingerprint timing is missing: {key}")
        for clock in ("wall_time_ns", "cpu_time_ns"):
            _validate_timing_distribution(
                payload.get(clock),
                repetitions=repetitions,
                label=f"phase_3a.fingerprint_only.{key}.{clock}",
            )
    for key in ("invalidation_lookup", "loaded_projector_fingerprint_startup"):
        payload = phase_timing.get(key)
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Phase 3A timing is missing: {key}")
        for clock in ("wall_time_ns", "cpu_time_ns"):
            _validate_timing_distribution(
                payload.get(clock),
                repetitions=repetitions,
                label=f"phase_3a.{key}.{clock}",
            )


def _validate_timing_distribution(value: Any, *, repetitions: int, label: str) -> None:
    if not isinstance(value, Mapping) or value.get("unit") != "ns":
        raise RuntimeError(f"timing distribution is invalid: {label}")
    samples = value.get("samples")
    if not isinstance(samples, list) or len(samples) != repetitions:
        raise RuntimeError(f"timing sample count is invalid: {label}")
    if value.get("sample_count") != repetitions:
        raise RuntimeError(f"timing sample metadata is invalid: {label}")
    normalized: list[int] = []
    for sample in samples:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise RuntimeError(f"timing sample value is invalid: {label}")
        normalized.append(sample)
    expected = _timing_distribution(normalized)
    for field in ("median", "p95", "min", "max"):
        if value.get(field) != expected[field]:
            raise RuntimeError(f"timing summary is inconsistent: {label}.{field}")


def _build_gate_decision(
    *,
    timing: Mapping[str, Any],
    fixture_manifest: Mapping[str, Any],
    current_host: Mapping[str, Any],
    reference_host_fingerprint: str | None,
    allocation_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current_fingerprint = str(current_host.get("fingerprint") or "")
    comparable = bool(
        reference_host_fingerprint and current_fingerprint and reference_host_fingerprint == current_fingerprint
    )
    if reference_host_fingerprint is None:
        comparison_reason = "reference_host_fingerprint_not_supplied"
    elif comparable:
        comparison_reason = "exact_host_profile_fingerprint_match"
    else:
        comparison_reason = "host_profile_fingerprint_mismatch"
    timing_rows = {str(row.get("key")): row for row in timing.get("scenarios", []) if isinstance(row, Mapping)}
    manifest_rows = {
        str(row.get("key")): row for row in fixture_manifest.get("scenarios", []) if isinstance(row, Mapping)
    }
    history_keys = (
        "history_10x.fixed_output",
        "history_10x.retained_closed_lots",
    )
    subcases: list[dict[str, Any]] = []
    for key in history_keys:
        timing_row = timing_rows.get(key)
        manifest_row = manifest_rows.get(key)
        if timing_row is None or manifest_row is None:
            subcases.append({"key": key, "status": "not_evaluable", "reason": "scenario_not_selected"})
            continue
        writer = (timing_row.get("components") or {}).get("existing_full_replay_writer")
        axis_status = str(manifest_row.get("axis_status") or "unknown")
        run_label = str(timing.get("run_label") or "")
        if run_label != "acceptance_5_warmups_30_repetitions":
            status = "not_evaluable"
            reason = "non_acceptance_smoke"
        elif axis_status != "evaluable":
            status = "not_evaluable"
            reason = axis_status
        elif not comparable:
            status = "not_comparable"
            reason = comparison_reason
        else:
            wall_p95 = int(writer["wall_time_ns"]["p95"])
            cpu_p95 = int(writer["cpu_time_ns"]["p95"])
            status = "pass" if wall_p95 <= WALL_LIMIT_NS and cpu_p95 <= CPU_LIMIT_NS else "fail"
            reason = "within_frozen_limits" if status == "pass" else "frozen_limit_exceeded"
        reported_wall_p95 = (
            int(writer["wall_time_ns"]["p95"])
            if isinstance(writer, Mapping)
            and isinstance(writer.get("wall_time_ns"), Mapping)
            and isinstance(writer["wall_time_ns"].get("p95"), int)
            else None
        )
        reported_cpu_p95 = (
            int(writer["cpu_time_ns"]["p95"])
            if isinstance(writer, Mapping)
            and isinstance(writer.get("cpu_time_ns"), Mapping)
            and isinstance(writer["cpu_time_ns"].get("p95"), int)
            else None
        )
        subcases.append(
            {
                "key": key,
                "status": status,
                "reason": reason,
                "wall_p95_ns": reported_wall_p95,
                "cpu_p95_ns": reported_cpu_p95,
            }
        )
    subcase_statuses = {row["status"] for row in subcases}
    if subcase_statuses == {"pass"}:
        writer_status = "pass"
    elif "fail" in subcase_statuses:
        writer_status = "fail"
    elif "not_evaluable" in subcase_statuses:
        writer_status = "not_evaluable"
    else:
        writer_status = "not_comparable"
    storage_timing = timing.get("research_storage_status")
    storage_allocation = (
        allocation_profile.get("research_storage_status")
        if isinstance(allocation_profile, Mapping)
        else None
    )
    storage_status, storage_reason, storage_wall_p95, storage_peak_allocation = _storage_status_gate(
        timing=storage_timing,
        allocation=storage_allocation,
        run_label=str(timing.get("run_label") or ""),
        comparable=comparable,
        comparison_reason=comparison_reason,
    )
    phase_3a = _phase_3a_gate(
        timing=timing.get("phase_3a"),
        allocation=(
            allocation_profile.get("phase_3a")
            if isinstance(allocation_profile, Mapping)
            else None
        ),
        run_label=str(timing.get("run_label") or ""),
        comparable=comparable,
        comparison_reason=comparison_reason,
    )
    return {
        "schema_version": DECISION_SCHEMA,
        "reference_host": {
            "current_profile": dict(current_host),
            "current_fingerprint": current_fingerprint,
            "expected_fingerprint": reference_host_fingerprint,
            "comparable": comparable,
            "reason": comparison_reason,
        },
        "thresholds": {
            "history_10x_writer_wall_p95_ns": WALL_LIMIT_NS,
            "history_10x_writer_cpu_p95_ns": CPU_LIMIT_NS,
            "research_storage_status_wall_p95_ns": STORAGE_STATUS_WALL_LIMIT_NS,
            "research_storage_status_python_peak_bytes": STORAGE_STATUS_ALLOCATION_LIMIT_BYTES,
            "required_subcases": list(history_keys),
            "phase_3a_wall_p95_ns": PHASE_3A_WALL_LIMIT_NS,
            "phase_3a_cpu_p95_ns": PHASE_3A_CPU_LIMIT_NS,
            "phase_3a_minimum_improvement_fraction": 0.5,
            "phase_3a_checkpoint_k": 3,
        },
        "components": {
            "projector_only": {
                "status": "diagnostic_only",
                "reason": "projector_only_cannot_satisfy_writer_gate",
            },
            "existing_full_replay_writer": {
                "status": writer_status,
                "subcases": subcases,
            },
            "research_storage_status": {
                "status": storage_status,
                "reason": storage_reason,
                "wall_p95_ns": storage_wall_p95,
                "python_peak_bytes": storage_peak_allocation,
                "fixture": STORAGE_STATUS_KEY,
            },
            "lot_diff_publication": phase_3a["lot_diff_publication"],
            "checkpoint_tail": phase_3a["checkpoint_tail"],
        },
        "phase_3a_combined": phase_3a["combined"],
        "phase_3a_evidence": phase_3a["evidence"],
        "automatic_actions": [],
    }


def _phase_3a_gate(
    *,
    timing: Any,
    allocation: Any,
    run_label: str,
    comparable: bool,
    comparison_reason: str,
) -> dict[str, Any]:
    unavailable_reason = None
    if not isinstance(timing, Mapping) or not isinstance(allocation, Mapping):
        unavailable_reason = "scenario_not_selected"
    elif run_label != "acceptance_5_warmups_30_repetitions":
        unavailable_reason = "non_acceptance_smoke"
    elif not comparable:
        unavailable_reason = comparison_reason
    if unavailable_reason is not None:
        status = "not_comparable" if "host" in unavailable_reason else "not_evaluable"
        component = {"status": status, "reason": unavailable_reason, "failures": []}
        return {
            "lot_diff_publication": dict(component),
            "checkpoint_tail": dict(component),
            "combined": {"status": "not_ready", "reason": unavailable_reason},
            "evidence": {
                "resource_failures": [],
                "parity_failures": [],
                "retained_lots_10x_guarantee": False,
            },
        }

    resource_failures: list[str] = []
    parity_failures: list[str] = []
    comparable_rows = timing.get("comparable_facades")
    allocation_rows = {
        str(row.get("key") or ""): row
        for row in allocation.get("comparable_facades", [])
        if isinstance(row, Mapping)
    }
    if not isinstance(comparable_rows, list):
        comparable_rows = []
    for row in comparable_rows:
        key = str(row.get("key") or "")
        fast = row.get("fast") if isinstance(row, Mapping) else None
        improvement = row.get("improvement") if isinstance(row, Mapping) else None
        if not isinstance(fast, Mapping) or not isinstance(improvement, Mapping):
            resource_failures.append(f"{key}:measurement_missing")
            continue
        if not bool((row.get("parity") or {}).get("exact")):
            parity_failures.append(f"{key}:forced_fast_output_mismatch")
        if int(fast["wall_time_ns"]["p95"]) > PHASE_3A_WALL_LIMIT_NS:
            resource_failures.append(f"{key}:wall_p95_exceeded")
        if int(fast["cpu_time_ns"]["p95"]) > PHASE_3A_CPU_LIMIT_NS:
            resource_failures.append(f"{key}:cpu_p95_exceeded")
        if float(improvement.get("wall_fraction") or 0.0) < 0.5:
            resource_failures.append(f"{key}:wall_improvement_below_50_percent")
        if float(improvement.get("cpu_fraction") or 0.0) < 0.5:
            resource_failures.append(f"{key}:cpu_improvement_below_50_percent")
        calls = fast.get("call_count_max") or {}
        if int(calls.get("full_prefix_reader_calls") or 0) != 0:
            resource_failures.append(f"{key}:full_prefix_read")
        if int(calls.get("full_lot_list_calls") or 0) != 0:
            resource_failures.append(f"{key}:full_lot_list_read")
        expected_ids = 2 if key.startswith("atomic_batch") else 1
        required_candidate_metrics = {
            "candidate_event_ids_unique",
            "candidate_event_id_max_batch",
        }
        if not required_candidate_metrics.issubset(calls):
            resource_failures.append(f"{key}:candidate_id_measurement_missing")
        elif (
            int(calls.get("candidate_event_ids_unique") or 0) > expected_ids
            or int(calls.get("candidate_event_id_max_batch") or 0) > expected_ids
            or int((fast.get("rows_returned_max") or {}).get("get_trade_events_by_ids") or 0)
            > expected_ids
        ):
            resource_failures.append(f"{key}:candidate_id_lookup_unbounded")
        if fast.get("checkpoint_row_deltas") != [0]:
            resource_failures.append(f"{key}:unexpected_checkpoint_row_write")
        if fast.get("checkpoint_state_byte_deltas") != [0]:
            resource_failures.append(f"{key}:unexpected_checkpoint_payload_write")
        allocation_row = allocation_rows.get(key)
        component = (
            ((allocation_row or {}).get("fast") or {}).get("component")
            if isinstance(allocation_row, Mapping)
            else None
        )
        peak = int((component or {}).get("python_peak_bytes") or -1)
        one_state = int((fast.get("checkpoint_after_max") or {}).get("one_state_bytes") or 0)
        if peak < 0 or peak > max(PHASE_3A_ALLOCATION_FLOOR_BYTES, 2 * one_state):
            resource_failures.append(f"{key}:allocation_limit_exceeded")

    required_keys = {
        "single_combo_metadata_close",
        "atomic_batch_combo_metadata_adjust",
    }
    if {str(row.get("key") or "") for row in comparable_rows} != required_keys:
        resource_failures.append("comparable_facade_set_incomplete")

    checkpoint = timing.get("checkpoint") or {}
    allocation_checkpoint = allocation.get("checkpoint") or {}
    no_rotation = checkpoint.get("no_rotation")
    if not isinstance(no_rotation, Mapping):
        resource_failures.append("checkpoint:no_rotation_missing")
    else:
        if no_rotation.get("checkpoint_row_deltas") != [0]:
            resource_failures.append("checkpoint:no_rotation_row_write")
        if no_rotation.get("checkpoint_state_byte_deltas") != [0]:
            resource_failures.append("checkpoint:no_rotation_payload_write")
    for key in ("rotation_100_events", "rotation_1_mib"):
        measured = checkpoint.get(key)
        allocated = allocation_checkpoint.get(key)
        if not isinstance(measured, Mapping) or not isinstance(allocated, Mapping):
            resource_failures.append(f"checkpoint:{key}:measurement_missing")
            continue
        if int(measured["wall_time_ns"]["p95"]) > PHASE_3A_WALL_LIMIT_NS:
            resource_failures.append(f"checkpoint:{key}:wall_p95_exceeded")
        if int(measured["cpu_time_ns"]["p95"]) > PHASE_3A_CPU_LIMIT_NS:
            resource_failures.append(f"checkpoint:{key}:cpu_p95_exceeded")
        if measured.get("checkpoint_row_deltas") != [1]:
            resource_failures.append(f"checkpoint:{key}:rotation_row_count")
        after = measured.get("checkpoint_after_max") or {}
        if int(after.get("row_count") or 0) > 3:
            resource_failures.append(f"checkpoint:{key}:k_exceeded")
        one_state = int(after.get("one_state_bytes") or 0)
        state_bytes = int(after.get("state_bytes") or 0)
        if one_state <= 0 or state_bytes > int(one_state * 3 * 1.1):
            resource_failures.append(f"checkpoint:{key}:steady_space_exceeded")
        growth = int((measured.get("sqlite_growth_bytes") or {}).get("p95") or 0)
        if growth > max(PHASE_3A_ALLOCATION_FLOOR_BYTES, 2 * one_state):
            resource_failures.append(f"checkpoint:{key}:wal_growth_exceeded")
        peak = int(((allocated.get("component") or {}).get("python_peak_bytes") or -1))
        if peak < 0 or peak > max(PHASE_3A_ALLOCATION_FLOOR_BYTES, 2 * one_state):
            resource_failures.append(f"checkpoint:{key}:allocation_limit_exceeded")

    current_reads = timing.get("current_reads") or {}
    allocation_reads = allocation.get("current_reads") or {}
    for key, limit in (
        ("current", PHASE_3A_READ_CURRENT_LIMIT_NS),
        ("current_state_10x", PHASE_3A_READ_STATE_10X_LIMIT_NS),
    ):
        measured = current_reads.get(key)
        allocated = allocation_reads.get(key)
        if not isinstance(measured, Mapping) or int(measured["wall_time_ns"]["p95"]) > limit:
            resource_failures.append(f"current_read:{key}:wall_p95_exceeded")
        if key == "current":
            peak = int((((allocated or {}).get("component") or {}).get("python_peak_bytes") or -1))
            if peak < 0 or peak > PHASE_3A_READ_ALLOCATION_LIMIT_BYTES:
                resource_failures.append("current_read:current:allocation_limit_exceeded")

    invalidation = timing.get("invalidation_lookup")
    if (
        not isinstance(invalidation, Mapping)
        or int(invalidation["wall_time_ns"]["p95"]) > PHASE_3A_INVALIDATION_LIMIT_NS
    ):
        resource_failures.append("invalidation_lookup:wall_p95_exceeded")
    startup = timing.get("loaded_projector_fingerprint_startup")
    startup_allocation = allocation.get("loaded_projector_fingerprint_startup")
    if not isinstance(startup, Mapping):
        resource_failures.append("projector_fingerprint_startup:missing")
    else:
        if int(startup["wall_time_ns"]["p95"]) > PHASE_3A_FINGERPRINT_STARTUP_LIMIT_NS:
            resource_failures.append("projector_fingerprint_startup:wall_p95_exceeded")
        if int(startup["cpu_time_ns"]["p95"]) > PHASE_3A_FINGERPRINT_STARTUP_LIMIT_NS:
            resource_failures.append("projector_fingerprint_startup:cpu_p95_exceeded")
        if not bool((startup.get("output") or {}).get("matches_loaded")):
            parity_failures.append("projector_fingerprint_startup:loaded_mismatch")
    startup_peak = int(
        (((startup_allocation or {}).get("component") or {}).get("python_peak_bytes") or -1)
    )
    if startup_peak < 0 or startup_peak > PHASE_3A_FINGERPRINT_ALLOCATION_LIMIT_BYTES:
        resource_failures.append("projector_fingerprint_startup:allocation_limit_exceeded")

    fingerprint = ((timing.get("fingerprint_only") or {}).get("current") or {})
    if not isinstance(fingerprint, Mapping):
        resource_failures.append("fingerprint_current:missing")
    else:
        if int(fingerprint["wall_time_ns"]["p95"]) > PHASE_3A_WALL_LIMIT_NS:
            resource_failures.append("fingerprint_current:wall_p95_exceeded")
        if int(fingerprint["cpu_time_ns"]["p95"]) > PHASE_3A_CPU_LIMIT_NS:
            resource_failures.append("fingerprint_current:cpu_p95_exceeded")
    fingerprint_peak = int(
        (((allocation.get("fingerprint_only") or {}).get("component") or {}).get("python_peak_bytes") or -1)
    )
    fingerprint_bytes = int(fingerprint.get("bytes") or 0) if isinstance(fingerprint, Mapping) else 0
    if fingerprint_peak < 0 or fingerprint_peak > max(
        PHASE_3A_ALLOCATION_FLOOR_BYTES,
        2 * fingerprint_bytes,
    ):
        resource_failures.append("fingerprint_current:allocation_limit_exceeded")

    retained = ((timing.get("fingerprint_only") or {}).get("retained_lots_10x") or {})
    failures = sorted(set(resource_failures + parity_failures))
    status = "pass" if not failures else "fail"
    lot_diff_failures = [item for item in failures if not item.startswith("checkpoint:")]
    checkpoint_failures = [item for item in failures if item.startswith("checkpoint:")]
    return {
        "lot_diff_publication": {
            "status": "pass" if not lot_diff_failures else "fail",
            "reason": "within_frozen_limits" if not lot_diff_failures else "lot_diff_gate_failed",
            "failures": lot_diff_failures,
        },
        "checkpoint_tail": {
            "status": "pass" if not checkpoint_failures else "fail",
            "reason": "within_frozen_limits" if not checkpoint_failures else "checkpoint_gate_failed",
            "failures": checkpoint_failures,
        },
        "combined": {
            "status": "ready" if status == "pass" else "not_ready",
            "reason": "all_phase_3a_gates_pass" if status == "pass" else "phase_3a_gate_failed",
        },
        "evidence": {
            "resource_failures": sorted(set(resource_failures)),
            "parity_failures": sorted(set(parity_failures)),
            "retained_lots_10x_guarantee": False,
            "retained_lots_10x": dict(retained),
        },
    }


def _storage_status_gate(
    *,
    timing: Any,
    allocation: Any,
    run_label: str,
    comparable: bool,
    comparison_reason: str,
) -> tuple[str, str, int | None, int | None]:
    if not isinstance(timing, Mapping) or not isinstance(allocation, Mapping):
        return "not_evaluable", "scenario_not_selected", None, None
    wall = timing.get("wall_time_ns")
    component = allocation.get("component")
    wall_p95 = (
        int(wall.get("p95"))
        if isinstance(wall, Mapping) and isinstance(wall.get("p95"), int)
        else None
    )
    peak = (
        int(component.get("python_peak_bytes"))
        if isinstance(component, Mapping) and isinstance(component.get("python_peak_bytes"), int)
        else None
    )
    if run_label != "acceptance_5_warmups_30_repetitions":
        return "not_evaluable", "non_acceptance_smoke", wall_p95, peak
    if wall_p95 is None or peak is None:
        return "not_evaluable", "measurement_missing", wall_p95, peak
    if peak > STORAGE_STATUS_ALLOCATION_LIMIT_BYTES:
        return "fail", "frozen_allocation_limit_exceeded", wall_p95, peak
    if not comparable:
        return "not_comparable", comparison_reason, wall_p95, peak
    if wall_p95 > STORAGE_STATUS_WALL_LIMIT_NS:
        return "fail", "frozen_wall_limit_exceeded", wall_p95, peak
    return "pass", "within_frozen_limits", wall_p95, peak


def _load_shadow_manifest(
    value: str | Path | Mapping[str, Any] | None,
    *,
    repo_root: Path,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        raw = Path(value).expanduser()
        path = raw if raw.is_absolute() else repo_root / raw
        path = path.resolve(strict=True)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("shadow manifest must be a bounded regular JSON file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"shadow manifest is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("shadow manifest must be a JSON object")
    if payload.get("schema_version") != "position_projection_migration_verify.v1":
        raise ValueError("shadow manifest schema is invalid")
    supplied = str(payload.get("manifest_hash") or "")
    unsigned = {key: item for key, item in payload.items() if key != "manifest_hash"}
    if len(supplied) != 64 or supplied != _sha256_json(unsigned):
        raise ValueError("shadow manifest hash mismatch")
    return payload


def _seal_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(payload)
    unsigned.pop("manifest_hash", None)
    return {**unsigned, "manifest_hash": _sha256_json(unsigned)}


def _build_phase_3a_acceptance(
    *,
    decision: Mapping[str, Any],
    shadow_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    phase = decision.get("phase_3a_combined")
    evidence = decision.get("phase_3a_evidence")
    resource_failures = list(
        (evidence.get("resource_failures") if isinstance(evidence, Mapping) else [])
        or []
    )
    parity_failures = list(
        (evidence.get("parity_failures") if isinstance(evidence, Mapping) else [])
        or []
    )
    reasons: list[str] = []
    benchmark_ready = isinstance(phase, Mapping) and phase.get("status") == "ready"
    if not benchmark_ready:
        reasons.append(str((phase or {}).get("reason") or "benchmark_not_ready"))
        resource_failures.append("benchmark_not_ready")
    binding = None
    shadow_ready = bool(
        isinstance(shadow_manifest, Mapping)
        and shadow_manifest.get("status") == "pass"
        and shadow_manifest.get("readiness") == "ready"
        and shadow_manifest.get("mode") == "shadow"
        and not shadow_manifest.get("resource_failures")
        and not shadow_manifest.get("parity_failures")
        and isinstance(shadow_manifest.get("store_binding"), Mapping)
    )
    if shadow_ready:
        binding = dict(shadow_manifest["store_binding"])
    else:
        reasons.append("passing_shadow_manifest_required")
        resource_failures.append("shadow_manifest_not_ready")
    resource_failures = sorted(set(str(item) for item in resource_failures))
    parity_failures = sorted(set(str(item) for item in parity_failures))
    ready = benchmark_ready and shadow_ready and not resource_failures and not parity_failures
    return _seal_manifest(
        {
            "schema_version": PHASE_3A_ACCEPTANCE_SCHEMA,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "pass" if ready else "fail",
            "readiness": "ready" if ready else "not_ready",
            "reasons": sorted(set(reasons)),
            "store_binding": binding,
            "reference_host": dict(decision.get("reference_host") or {}),
            "components": {
                "lot_diff_publication": dict(
                    (decision.get("components") or {}).get("lot_diff_publication") or {}
                ),
                "checkpoint_tail": dict(
                    (decision.get("components") or {}).get("checkpoint_tail") or {}
                ),
                "combined": dict(phase or {}),
            },
            "resource_failures": resource_failures,
            "parity_failures": parity_failures,
            "retained_lots_10x_guarantee": False,
            "retained_lots_10x": dict(
                (evidence.get("retained_lots_10x") if isinstance(evidence, Mapping) else {})
                or {}
            ),
            "automatic_actions": [],
        }
    )


def _resolve_output_dir(value: str | Path, *, repo_root: Path) -> tuple[Path, bool]:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else repo_root / raw
    if candidate.is_symlink():
        raise ValueError("output directory must not be a symlink")
    parent = candidate.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("output directory parent must be a real directory")
    target = parent / candidate.name
    existed = target.exists()
    if existed:
        if target.is_symlink() or not target.is_dir():
            raise ValueError("output path must be an absent or empty directory")
        if any(target.iterdir()):
            raise ValueError("output directory must be empty")
    return target, existed


def _publish_artifact_set(
    *,
    output_dir: str | Path,
    repo_root: Path,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> Path:
    if set(artifacts) != set(ARTIFACT_FILENAMES):
        raise ValueError("artifact set is incomplete")
    target, existed = _resolve_output_dir(output_dir, repo_root=repo_root)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    published = False
    try:
        for filename in ARTIFACT_FILENAMES:
            path = stage / filename
            with path.open("wb") as handle:
                handle.write(_canonical_json_bytes(artifacts[filename]) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        _validate_published_files(stage)
        directory_fd = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if existed:
            if any(target.iterdir()):
                raise ValueError("output directory became non-empty before publish")
            target.rmdir()
        os.replace(stage, target)
        published = True
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return target
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def _publish_lifecycle_attempt_artifact(
    *,
    output_dir: str | Path,
    repo_root: Path,
    artifact: Mapping[str, Any],
) -> Path:
    target, existed = _resolve_output_dir(output_dir, repo_root=repo_root)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    published = False
    try:
        path = stage / LIFECYCLE_ATTEMPT_ARTIFACT_FILENAME
        with path.open("wb") as handle:
            handle.write(_canonical_json_bytes(artifact) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA
        ):
            raise RuntimeError("lifecycle attempt benchmark artifact is invalid")
        directory_fd = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if existed:
            if any(target.iterdir()):
                raise ValueError("output directory became non-empty before publish")
            target.rmdir()
        os.replace(stage, target)
        published = True
        return target
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def _validate_published_files(directory: Path) -> None:
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != set(ARTIFACT_FILENAMES):
        raise RuntimeError("staged artifact set is incomplete")
    expected_schemas = {
        "fixture-manifest.json": FIXTURE_SCHEMA,
        "timing.json": TIMING_SCHEMA,
        "cpu-profile.json": CPU_PROFILE_SCHEMA,
        "allocation-profile.json": ALLOCATION_PROFILE_SCHEMA,
        "decision.json": DECISION_SCHEMA,
        "phase-3a-acceptance.json": PHASE_3A_ACCEPTANCE_SCHEMA,
    }
    for filename, schema in expected_schemas.items():
        payload = json.loads((directory / filename).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != schema:
            raise RuntimeError(f"staged artifact schema is invalid: {filename}")


def _worker_main(*, mode: str, spec_path: str | Path) -> int:
    path = Path(spec_path).expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("worker spec must be a bounded regular JSON file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("worker spec must be a JSON object")
    result = _worker_payload(mode=mode, worker_spec=payload)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark canonical option-position projection on deterministic synthetic data.",
    )
    parser.add_argument("--baseline", help="Optional storage_runtime_baseline.v1 metadata report")
    parser.add_argument(
        "--scenario",
        choices=(*PUBLIC_SCENARIOS, "lifecycle_attempt_audit"),
        default="all",
    )
    parser.add_argument("--output-dir", help="Required absent or empty local output directory")
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--lifecycle-prior-attempts",
        type=int,
        default=LIFECYCLE_ATTEMPT_COUNT,
    )
    parser.add_argument(
        "--lifecycle-receipt-bytes",
        type=int,
        default=LIFECYCLE_RECEIPT_BYTES,
    )
    parser.add_argument(
        "--lifecycle-p99-receipt-bytes",
        type=int,
        default=LIFECYCLE_P99_RECEIPT_BYTES,
    )
    parser.add_argument(
        "--lifecycle-moves",
        type=int,
        default=LIFECYCLE_MOVE_COUNT,
    )
    parser.add_argument(
        "--reference-host-fingerprint",
        help="Exact host-profile SHA-256 required before absolute timing decisions are allowed",
    )
    parser.add_argument(
        "--shadow-manifest",
        help="Passing read-only projection-migration shadow manifest used only to bind acceptance",
    )
    parser.add_argument("--_worker-mode", choices=("timing", "cpu", "allocation"), help=argparse.SUPPRESS)
    parser.add_argument("--_worker-spec", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args._worker_mode:
            if not args._worker_spec:
                parser.error("--_worker-spec is required in worker mode")
            return _worker_main(mode=args._worker_mode, spec_path=args._worker_spec)
        if not args.output_dir:
            parser.error("--output-dir is required")
        repo_root = REPO_ROOT
        if args.scenario == "lifecycle_attempt_audit":
            result = run_lifecycle_attempt_audit_benchmark(
                repo_root=repo_root,
                output_dir=args.output_dir,
                warmups=args.warmups,
                repetitions=args.repetitions,
                seed=args.seed,
                prior_attempts=args.lifecycle_prior_attempts,
                receipt_bytes=args.lifecycle_receipt_bytes,
                p99_receipt_bytes=args.lifecycle_p99_receipt_bytes,
                moves=args.lifecycle_moves,
                reference_host_fingerprint=args.reference_host_fingerprint,
            )
        else:
            result = run_data_storage_projection_benchmark(
                repo_root=repo_root,
                output_dir=args.output_dir,
                baseline=args.baseline,
                scenario=args.scenario,
                warmups=args.warmups,
                repetitions=args.repetitions,
                seed=args.seed,
                reference_host_fingerprint=args.reference_host_fingerprint,
                shadow_manifest=args.shadow_manifest,
            )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


__all__ = [
    "ALLOCATION_PROFILE_SCHEMA",
    "CPU_PROFILE_SCHEMA",
    "DECISION_SCHEMA",
    "FIXTURE_SCHEMA",
    "LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA",
    "PHASE_3A_ACCEPTANCE_SCHEMA",
    "TIMING_SCHEMA",
    "build_parser",
    "main",
    "run_data_storage_projection_benchmark",
    "run_lifecycle_attempt_audit_benchmark",
]


if __name__ == "__main__":
    raise SystemExit(main())
