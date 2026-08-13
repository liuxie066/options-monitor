from __future__ import annotations

import argparse
import base64
import cProfile
from contextlib import contextmanager
from dataclasses import dataclass
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
import zlib

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.event_codec import (
    encode_trade_event_for_storage,
    trade_event_application_payload,
)
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import rebuild_position_lots_from_trade_events

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on some supported platforms
    resource = None  # type: ignore[assignment]


FIXTURE_SCHEMA = "data_storage_projection_fixture.v1"
TIMING_SCHEMA = "data_storage_projection_timing.v1"
CPU_PROFILE_SCHEMA = "data_storage_projection_cpu_profile.v1"
ALLOCATION_PROFILE_SCHEMA = "data_storage_projection_allocation_profile.v1"
DECISION_SCHEMA = "data_storage_projection_gate_decision.v1"
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
ARTIFACT_FILENAMES = (
    "fixture-manifest.json",
    "timing.json",
    "cpu-profile.json",
    "allocation-profile.json",
    "decision.json",
)
PUBLIC_SCENARIOS = (
    "all",
    "current_scale",
    "history_10x",
    "current_state_10x",
    "account_fanout",
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
    scenario_specs = _build_scenario_specs(dimensions, selected=selected)
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
    )
    worker_spec = {
        "schema_version": WORKER_SPEC_SCHEMA,
        "seed": fixture_seed,
        "warmups": warmup_count,
        "repetitions": repetition_count,
        "run_label": run_label,
        "scenarios": scenario_specs,
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
        },
    )
    return {
        "status": "complete",
        "output_dir": str(target),
        "run_label": run_label,
        "scenario_count": len(scenario_specs),
        "fixture_set_sha256": fixture_manifest["fixture_set_sha256"],
        "host_fingerprint": host["fingerprint"],
        "existing_full_replay_writer": decision["components"]["existing_full_replay_writer"],
        "phase_3a_combined": decision["phase_3a_combined"],
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


def _build_fixture_manifest(
    *,
    repo_root: Path,
    dimensions: BaselineDimensions,
    specs: Sequence[dict[str, Any]],
    seed: int,
    host: dict[str, Any],
    run_label: str,
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
    return {
        "schema_version": FIXTURE_SCHEMA,
        "fixture_seed": int(seed),
        "fixture_set_sha256": _sha256_json(fixture_hashes),
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
        "side": "buy" if event_type == "close" else "sell",
    }
    if event_type == "close":
        raw_payload["close_type"] = "buy_to_close"
    contract_key = ContractKey.from_values(
        broker="futu",
        account=f"bench{account_index:02d}",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=10.0 + (lot_index * 0.01),
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
    # The real writer reads canonical storage rows through the compatibility
    # adapter before projection. Feed the projector-only component the same
    # public stored-event shape so parity tests compare the measured components,
    # not an incidental pre-adapter representation.
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
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("worker spec scenarios are missing")
    seed = _bounded_nonnegative_int(worker_spec.get("seed"), name="seed", maximum=2**31 - 1)
    warmups = _bounded_nonnegative_int(worker_spec.get("warmups"), name="warmups", maximum=100)
    repetitions = _bounded_positive_int(worker_spec.get("repetitions"), name="repetitions", maximum=1_000)
    label = str(worker_spec.get("run_label") or "")
    if mode == "timing":
        rows = [
            _measure_timing_scenario(spec, seed=seed, warmups=warmups, repetitions=repetitions)
            for spec in raw_scenarios
            if isinstance(spec, dict)
        ]
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
        }
    if mode == "cpu":
        rows = [_measure_cpu_scenario(spec, seed=seed) for spec in raw_scenarios if isinstance(spec, dict)]
        return {
            "schema_version": CPU_PROFILE_SCHEMA,
            "measurement_mode": "cprofile_separate_process",
            "timing_threshold_eligible": False,
            "tracemalloc_enabled": False,
            "scenarios": rows,
        }
    if mode == "allocation":
        rows = [_measure_allocation_scenario(spec, seed=seed) for spec in raw_scenarios if isinstance(spec, dict)]
        return {
            "schema_version": ALLOCATION_PROFILE_SCHEMA,
            "measurement_mode": "tracemalloc_separate_process",
            "timing_threshold_eligible": False,
            "cprofile_enabled": False,
            "scenarios": rows,
        }
    raise ValueError(f"unsupported worker mode: {mode}")


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
        db_path = Path(temp_name) / "synthetic-option-positions.sqlite3"
        repo = SQLiteOptionPositionsRepository(db_path)
        keeper = repo._connect()
        try:
            now_ms = 1_800_000_000_000
            rows = []
            for event in events:
                encoded = encode_trade_event_for_storage(event)
                rows.append(
                    (
                        encoded.event_id,
                        encoded.event_json,
                        encoded.event_time_ms,
                        now_ms,
                        now_ms,
                    )
                )
            keeper.executemany(
                "INSERT INTO trade_events "
                "(event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
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
            "required_subcases": list(history_keys),
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
            "lot_diff_publication": {
                "status": "not_implemented",
                "reason": "phase_3a_diff_publication_is_out_of_scope_for_phase_1",
            },
        },
        "phase_3a_combined": {
            "status": "not_ready",
            "reason": "lot_diff_publication_not_implemented",
        },
        "automatic_actions": [],
    }


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
    parser.add_argument("--scenario", choices=PUBLIC_SCENARIOS, default="all")
    parser.add_argument("--output-dir", help="Required absent or empty local output directory")
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--reference-host-fingerprint",
        help="Exact host-profile SHA-256 required before absolute timing decisions are allowed",
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
        repo_root = Path(__file__).resolve().parents[3]
        result = run_data_storage_projection_benchmark(
            repo_root=repo_root,
            output_dir=args.output_dir,
            baseline=args.baseline,
            scenario=args.scenario,
            warmups=args.warmups,
            repetitions=args.repetitions,
            seed=args.seed,
            reference_host_fingerprint=args.reference_host_fingerprint,
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
    "TIMING_SCHEMA",
    "build_parser",
    "main",
    "run_data_storage_projection_benchmark",
]
