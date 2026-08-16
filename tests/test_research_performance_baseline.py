from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.application.research import performance_baseline as module


def _dimensions(**overrides: int) -> module.BaselineDimensions:
    values = {
        "event_count": 12,
        "current_lot_count": 3,
        "account_count": 2,
        "payload_bytes": 256,
    }
    values.update(overrides)
    return module.BaselineDimensions(
        **values,
        dimension_source="test",
        requested=dict(values),
        clamped={},
        metadata={"payload_fields_consumed": 0},
    )


def _small_spec(
    *,
    key: str = "current_scale",
    shape: str = "fixed_open_lots_with_verifications",
    event_count: int = 6,
    lot_count: int = 2,
    account_count: int = 2,
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
        "axis": key.split(".", 1)[0],
        "shape": shape,
        "classification": "test_fixture",
        "axis_status": "evaluable",
        "requested_dimensions": {
            "event_count": event_count,
            "projected_lot_count": projected_lots,
            "account_count": account_count,
            "payload_bytes": 256,
        },
        "effective_dimensions": {
            "event_count": event_count,
            "projected_lot_count": projected_lots,
            "open_lot_count": open_lots,
            "risk_view_count": risk_views,
            "allocation_count": allocations,
            "account_count": account_count,
            "payload_bytes": 256,
        },
    }


def _timing_artifact(
    fixture_manifest: dict[str, Any],
    *,
    wall_p95: int = 100,
    cpu_p95: int = 100,
) -> dict[str, Any]:
    wall_samples = [wall_p95] * 30
    cpu_samples = [cpu_p95] * 30
    payload = {
        "schema_version": module.TIMING_SCHEMA,
        "measurement_mode": "timing_without_profiler",
        "profilers_enabled": False,
        "tracemalloc_enabled": False,
        "warmups": 5,
        "repetitions": 30,
        "run_label": "acceptance_5_warmups_30_repetitions",
        "clock_authority": ["time.perf_counter_ns", "time.process_time_ns"],
        "scenarios": [
            {
                "key": row["key"],
                "fixture_sha256": row["fixture_sha256"],
                "axis_status": row["axis_status"],
                "counts": row["effective_dimensions"],
                "parity": {"exact": True, "mismatched_fingerprints": []},
                "components": {
                    "projector_only": {
                        "wall_time_ns": module._timing_distribution(wall_samples),
                        "cpu_time_ns": module._timing_distribution(cpu_samples),
                    },
                    "existing_full_replay_writer": {
                        "wall_time_ns": module._timing_distribution(wall_samples),
                        "cpu_time_ns": module._timing_distribution(cpu_samples),
                    },
                },
            }
            for row in fixture_manifest["scenarios"]
        ],
    }
    storage = fixture_manifest.get("research_storage_status")
    if isinstance(storage, dict):
        payload["research_storage_status"] = {
            "key": storage["key"],
            "fixture_sha256": storage["fixture_sha256"],
            "setup_included": False,
            "wall_time_ns": module._timing_distribution(wall_samples),
            "cpu_time_ns": module._timing_distribution(cpu_samples),
        }
    else:
        payload["research_storage_status"] = None
    return payload


def _profile_artifact(
    fixture_manifest: dict[str, Any],
    *,
    schema: str,
    mode: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": schema,
        "measurement_mode": mode,
        "timing_threshold_eligible": False,
        "scenarios": [
            {
                "key": row["key"],
                "fixture_sha256": row["fixture_sha256"],
                "parity": {"exact": True, "mismatched_fingerprints": []},
                "components": {
                    "projector_only": {},
                    "existing_full_replay_writer": {},
                },
            }
            for row in fixture_manifest["scenarios"]
        ],
    }
    storage = fixture_manifest.get("research_storage_status")
    if isinstance(storage, dict):
        payload["research_storage_status"] = {
            "key": storage["key"],
            "fixture_sha256": storage["fixture_sha256"],
            "setup_included": False,
            "component": {"python_peak_bytes": 1_024},
        }
    else:
        payload["research_storage_status"] = None
    return payload


def _passing_phase_gate_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    distribution = module._timing_distribution([100] * 30)

    def measurement(*, checkpoint_delta: int = 0) -> dict[str, Any]:
        return {
            "wall_time_ns": copy.deepcopy(distribution),
            "cpu_time_ns": copy.deepcopy(distribution),
            "call_count_max": {
                "full_prefix_reader_calls": 0,
                "full_lot_list_calls": 0,
                "candidate_event_ids_requested": 1,
                "candidate_event_ids_unique": 1,
                "candidate_event_id_max_batch": 1,
            },
            "rows_returned_max": {"get_trade_events_by_ids": 0},
            "checkpoint_row_deltas": [checkpoint_delta],
            "checkpoint_state_byte_deltas": [1_000 if checkpoint_delta else 0],
            "checkpoint_after_max": {
                "row_count": 2 if checkpoint_delta else 1,
                "state_bytes": 2_000 if checkpoint_delta else 1_000,
                "one_state_bytes": 1_000,
            },
            "sqlite_growth_bytes": copy.deepcopy(distribution),
        }

    comparable = []
    allocation_comparable = []
    for key in ("single_combo_metadata_close", "atomic_batch_combo_metadata_adjust"):
        fast = measurement()
        comparable.append(
            {
                "key": key,
                "forced_full": measurement(),
                "fast": fast,
                "parity": {"exact": True},
                "improvement": {"wall_fraction": 0.9, "cpu_fraction": 0.9},
            }
        )
        allocation_comparable.append(
            {"key": key, "fast": {"component": {"python_peak_bytes": 1_024}}}
        )
    force_full = measurement()
    timing = {
        "schema_version": "data_storage_projection_phase3a_timing.v1",
        "fixture_sha256": "f" * 64,
        "setup_included": False,
        "comparable_facades": comparable,
        "force_full_facades": [
            {
                "key": "special_combo_identity_membership",
                "reason": "immutable_identity_and_membership_transaction",
                "measurement": force_full,
            }
        ],
        "checkpoint": {
            "no_rotation": measurement(),
            "rotation_100_events": measurement(checkpoint_delta=1),
            "rotation_1_mib": measurement(checkpoint_delta=1),
        },
        "current_reads": {
            "current": measurement(),
            "current_state_10x": measurement(),
        },
        "fingerprint_only": {
            "current": {**measurement(), "rows": 100, "bytes": 1_000},
            "retained_lots_10x": {
                **measurement(),
                "rows": 5_000,
                "bytes": 50_000,
                "guarantee": False,
                "capacity_warning": None,
            },
        },
        "invalidation_lookup": measurement(),
        "loaded_projector_fingerprint_startup": {
            **measurement(),
            "output": {"matches_loaded": True},
        },
    }
    allocation = {
        "schema_version": "data_storage_projection_phase3a_allocation_profile.v1",
        "fixture_sha256": "f" * 64,
        "setup_included": False,
        "comparable_facades": allocation_comparable,
        "checkpoint": {
            "rotation_100_events": {"component": {"python_peak_bytes": 1_024}},
            "rotation_1_mib": {"component": {"python_peak_bytes": 1_024}},
        },
        "current_reads": {
            "current": {"component": {"python_peak_bytes": 1_024}},
            "current_state_10x": {"component": {"python_peak_bytes": 1_024}},
        },
        "fingerprint_only": {"component": {"python_peak_bytes": 1_024}},
        "loaded_projector_fingerprint_startup": {
            "component": {"python_peak_bytes": 1_024}
        },
    }
    return timing, allocation


def _fake_workers(
    *,
    repo_root: Path,
    mode: str,
    worker_spec: dict[str, Any],
) -> dict[str, Any]:
    del repo_root
    scenarios = []
    for spec in worker_spec["scenarios"]:
        events = module._build_synthetic_events(spec, seed=worker_spec["seed"])
        timing_distribution = module._timing_distribution([100] * worker_spec["repetitions"])
        scenarios.append(
            {
                "key": spec["key"],
                "fixture_sha256": module._events_sha256(events),
                "axis_status": spec["axis_status"],
                "parity": {"exact": True, "mismatched_fingerprints": []},
                "components": {
                    "projector_only": {
                        "wall_time_ns": timing_distribution,
                        "cpu_time_ns": timing_distribution,
                    },
                    "existing_full_replay_writer": {
                        "wall_time_ns": timing_distribution,
                        "cpu_time_ns": timing_distribution,
                    },
                },
            }
        )
    schemas = {
        "timing": (module.TIMING_SCHEMA, "timing_without_profiler"),
        "cpu": (module.CPU_PROFILE_SCHEMA, "cprofile_separate_process"),
        "allocation": (module.ALLOCATION_PROFILE_SCHEMA, "tracemalloc_separate_process"),
    }
    schema, measurement_mode = schemas[mode]
    storage_spec = worker_spec.get("research_storage_status")
    storage_status = None
    if isinstance(storage_spec, dict):
        storage_status = {
            "key": storage_spec["key"],
            "fixture_sha256": storage_spec["fixture_sha256"],
            "setup_included": False,
        }
        if mode == "timing":
            storage_status.update(
                wall_time_ns=module._timing_distribution(
                    [100] * worker_spec["repetitions"]
                ),
                cpu_time_ns=module._timing_distribution(
                    [100] * worker_spec["repetitions"]
                ),
            )
        elif mode == "allocation":
            storage_status["component"] = {"python_peak_bytes": 1_024}
        else:
            storage_status["component"] = {}
    return {
        "schema_version": schema,
        "measurement_mode": measurement_mode,
        "profilers_enabled": False if mode == "timing" else None,
        "tracemalloc_enabled": False if mode in {"timing", "cpu"} else None,
        "timing_threshold_eligible": False if mode != "timing" else None,
        "warmups": worker_spec["warmups"] if mode == "timing" else None,
        "repetitions": worker_spec["repetitions"] if mode == "timing" else None,
        "run_label": worker_spec["run_label"],
        "scenarios": scenarios,
        "research_storage_status": storage_status,
    }


def test_history_specs_freeze_output_and_retain_closed_lot_coupling() -> None:
    specs = module._build_scenario_specs(_dimensions(), selected=["history_10x"])

    assert [spec["key"] for spec in specs] == [
        "history_10x.fixed_output",
        "history_10x.retained_closed_lots",
    ]
    fixed, retained = specs
    assert fixed["effective_dimensions"]["event_count"] >= module.MIN_HISTORY_EVENTS
    assert fixed["effective_dimensions"]["projected_lot_count"] == 3
    assert fixed["effective_dimensions"]["open_lot_count"] == 3
    assert fixed["effective_dimensions"]["allocation_count"] == 0
    assert retained["effective_dimensions"]["projected_lot_count"] == 5_000
    assert retained["effective_dimensions"]["open_lot_count"] == 0
    assert retained["effective_dimensions"]["allocation_count"] == 5_000


def test_fixture_hash_is_repeatable_and_changes_with_seed() -> None:
    spec = _small_spec()

    first = module._build_synthetic_events(spec, seed=1)
    second = module._build_synthetic_events(spec, seed=1)
    third = module._build_synthetic_events(spec, seed=2)

    assert first == second
    assert module._events_sha256(first) == module._events_sha256(second)
    assert module._events_sha256(first) != module._events_sha256(third)
    assert {row["raw_payload"]["entropy_class"] for row in first} == {
        "low",
        "median",
        "high",
    }
    metrics = module._event_payload_metrics(first)
    assert (
        metrics["entropy_classes"]["high"]["compression_ratio"]["p50"]
        > metrics["entropy_classes"]["median"]["compression_ratio"]["p50"]
        > metrics["entropy_classes"]["low"]["compression_ratio"]["p50"]
    )


def test_fixed_output_history_growth_does_not_change_projected_output() -> None:
    small = _small_spec(event_count=6, lot_count=2)
    large = _small_spec(event_count=60, lot_count=2)

    small_projection = module.project_stored_trade_events_to_position_lots(
        module._build_synthetic_events(small, seed=module.SEED)
    )
    large_projection = module.project_stored_trade_events_to_position_lots(
        module._build_synthetic_events(large, seed=module.SEED)
    )
    small_output = module._projection_output(small_projection, event_count=6)
    large_output = module._projection_output(large_projection, event_count=60)

    assert small_output["lot_fingerprint"] == large_output["lot_fingerprint"]
    assert small_output["counts"]["projected_lot_count"] == 2
    assert large_output["counts"]["projected_lot_count"] == 2
    assert small_output["counts"]["risk_view_count"] == 2
    assert large_output["counts"]["risk_view_count"] == 2


def test_retained_closed_lot_fixture_projects_one_allocation_per_pair() -> None:
    spec = _small_spec(
        key="history_10x.retained_closed_lots",
        shape="open_close_pairs",
        event_count=10,
        lot_count=5,
    )
    events = module._build_synthetic_events(spec, seed=module.SEED)
    projection = module.project_stored_trade_events_to_position_lots(events)
    output = module._projection_output(projection, event_count=len(events))

    assert output["counts"] == {
        "event_count": 10,
        "projected_lot_count": 5,
        "open_lot_count": 0,
        "risk_view_count": 0,
        "allocation_count": 5,
        "diagnostic_count": 0,
    }


def test_real_writer_and_projector_have_exact_canonical_parity_and_byte_accounting() -> None:
    spec = _small_spec(event_count=8, lot_count=3)
    events = module._build_synthetic_events(spec, seed=module.SEED)

    projector = module._timed_projector(events, warmups=0, repetitions=1)
    writer = module._timed_writer(events, warmups=0, repetitions=1)

    assert module._projection_parity(projector["output"], writer["output"])["exact"] is True
    assert writer["sql"]["publication_behavior"] == "global_delete_then_insert"
    assert writer["sql"]["trade_event_rows_read_per_replay"] == 8
    assert writer["sql"]["position_lot_rows_inserted_per_replay"] == 3
    for stage in (
        "before_replay",
        "peak_observed_after_repetition",
        "after_replay_before_checkpoint",
        "steady_state_after_wal_checkpoint_truncate",
    ):
        assert set(writer["sqlite_bytes"][stage]) == {
            "db_bytes",
            "wal_bytes",
            "shm_bytes",
            "total_bytes",
        }
        assert writer["sqlite_bytes"][stage]["total_bytes"] >= 0


def test_timing_cpu_and_allocation_modes_are_contractually_separate() -> None:
    worker_spec = {
        "schema_version": module.WORKER_SPEC_SCHEMA,
        "seed": module.SEED,
        "warmups": 0,
        "repetitions": 1,
        "run_label": "non_acceptance_smoke",
        "scenarios": [_small_spec(event_count=3, lot_count=1)],
    }

    timing = module._worker_payload(mode="timing", worker_spec=worker_spec)
    cpu = module._worker_payload(mode="cpu", worker_spec=worker_spec)
    allocation = module._worker_payload(mode="allocation", worker_spec=worker_spec)

    assert timing["profilers_enabled"] is False
    assert timing["tracemalloc_enabled"] is False
    assert cpu["measurement_mode"] == "cprofile_separate_process"
    assert cpu["timing_threshold_eligible"] is False
    assert allocation["measurement_mode"] == "tracemalloc_separate_process"
    assert allocation["timing_threshold_eligible"] is False


def test_storage_status_fixture_identity_is_repeatable() -> None:
    first = module._storage_status_spec(seed=module.SEED)
    second = module._storage_status_spec(seed=module.SEED)
    third = module._storage_status_spec(seed=module.SEED + 1)

    assert first == second
    assert first["fixture_sha256"] != third["fixture_sha256"]
    assert first["effective_dimensions"] == {
        "partition_count": 10_000,
        "manifest_count": 1,
        "runtime_file_count": 10_001,
    }


def test_research_storage_status_can_run_without_projection_scenarios(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        module,
        "_host_profile",
        lambda: {
            "schema_version": "data_storage_projection_host_profile.v1",
            "fingerprint": "a" * 64,
        },
    )
    output = tmp_path / "storage-only"

    result = module.run_data_storage_projection_benchmark(
        repo_root=Path.cwd(),
        output_dir=output,
        scenario="research_storage_status",
        warmups=1,
        repetitions=2,
        worker_runner=_fake_workers,
    )

    assert result["scenario_count"] == 0
    assert result["research_storage_status"]["status"] == "not_evaluable"
    fixture = json.loads((output / "fixture-manifest.json").read_text(encoding="utf-8"))
    assert fixture["scenarios"] == []
    assert fixture["research_storage_status"]["key"] == module.STORAGE_STATUS_KEY


def test_storage_status_workers_exclude_setup_and_preserve_payload_free_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = module._storage_status_spec(seed=module.SEED)
    result = {
        "status": "partial_data",
        "runtime_storage": {"file_count": 10_001},
        "research_storage": {
            "manifest_count": 1,
            "declared_reference_count": 10_000,
            "protected_reference_failures": [],
        },
        "safety": {
            "payload_content_reads": 0,
            "mutation_operations": 0,
            "no_follow_traversal": True,
        },
    }
    setup_calls = 0
    collect_calls = 0

    @module.contextmanager
    def fake_fixture(_spec: dict[str, Any]):
        nonlocal setup_calls
        setup_calls += 1
        yield {"fixture_sha256": spec["fixture_sha256"]}

    def fake_collect(_context: dict[str, Any]) -> dict[str, Any]:
        nonlocal collect_calls
        collect_calls += 1
        return result

    monkeypatch.setattr(module, "_temporary_storage_status_fixture", fake_fixture)
    monkeypatch.setattr(module, "_collect_synthetic_storage_status", fake_collect)

    timing = module._measure_storage_status_timing(spec, warmups=1, repetitions=2)
    allocation = module._measure_storage_status_allocation(spec)

    assert setup_calls == 2
    assert collect_calls == 4
    assert timing["setup_included"] is False
    assert timing["output"]["payload_content_reads"] == 0
    assert timing["output"]["mutation_operations"] == 0
    assert allocation["setup_included"] is False


def test_storage_status_gate_checks_space_on_any_host_and_time_on_reference_host() -> None:
    timing = {
        "wall_time_ns": module._timing_distribution([100] * 30),
    }
    allocation = {"component": {"python_peak_bytes": 1_024}}

    mismatch = module._storage_status_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=False,
        comparison_reason="host_profile_fingerprint_mismatch",
    )
    too_large = module._storage_status_gate(
        timing=timing,
        allocation={
            "component": {
                "python_peak_bytes": module.STORAGE_STATUS_ALLOCATION_LIMIT_BYTES + 1,
            }
        },
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=False,
        comparison_reason="host_profile_fingerprint_mismatch",
    )
    passing = module._storage_status_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="exact_host_profile_fingerprint_match",
    )

    assert mismatch[:2] == ("not_comparable", "host_profile_fingerprint_mismatch")
    assert too_large[:2] == ("fail", "frozen_allocation_limit_exceeded")
    assert passing[:2] == ("pass", "within_frozen_limits")


def test_storage_status_gate_is_not_comparable_when_allocation_measurement_is_missing() -> None:
    timing = {
        "wall_time_ns": module._timing_distribution([100] * 30),
    }

    result = module._storage_status_gate(
        timing=timing,
        allocation=None,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="exact_host_profile_fingerprint_match",
    )

    assert result[:2] == ("not_evaluable", "scenario_not_selected")


def test_reference_host_gate_requires_exact_fingerprint_and_both_history_subcases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = {
        "schema_version": "data_storage_projection_host_profile.v1",
        "fingerprint": "a" * 64,
    }
    specs = module._build_scenario_specs(_dimensions(), selected=["history_10x"])
    manifest = module._build_fixture_manifest(
        repo_root=Path.cwd(),
        dimensions=_dimensions(),
        specs=specs,
        seed=module.SEED,
        host=host,
        run_label="acceptance_5_warmups_30_repetitions",
    )
    timing = _timing_artifact(manifest)

    absent = module._build_gate_decision(
        timing=timing,
        fixture_manifest=manifest,
        current_host=host,
        reference_host_fingerprint=None,
    )
    mismatch = module._build_gate_decision(
        timing=timing,
        fixture_manifest=manifest,
        current_host=host,
        reference_host_fingerprint="b" * 64,
    )
    matching = module._build_gate_decision(
        timing=timing,
        fixture_manifest=manifest,
        current_host=host,
        reference_host_fingerprint="a" * 64,
    )

    assert absent["components"]["existing_full_replay_writer"]["status"] == "not_comparable"
    assert mismatch["components"]["existing_full_replay_writer"]["status"] == "not_comparable"
    assert matching["components"]["existing_full_replay_writer"]["status"] == "pass"
    assert matching["components"]["projector_only"]["status"] == "diagnostic_only"
    assert matching["components"]["lot_diff_publication"]["status"] == "not_evaluable"
    assert matching["phase_3a_combined"] == {
        "status": "not_ready",
        "reason": "scenario_not_selected",
    }


def test_matching_host_never_turns_smoke_measurements_into_a_pass() -> None:
    host = {"fingerprint": "a" * 64}
    specs = module._build_scenario_specs(_dimensions(), selected=["history_10x"])
    manifest = module._build_fixture_manifest(
        repo_root=Path.cwd(),
        dimensions=_dimensions(),
        specs=specs,
        seed=module.SEED,
        host=host,
        run_label="non_acceptance_smoke",
    )
    timing = _timing_artifact(manifest)
    timing["run_label"] = "non_acceptance_smoke"
    timing["warmups"] = 1
    timing["repetitions"] = 2

    decision = module._build_gate_decision(
        timing=timing,
        fixture_manifest=manifest,
        current_host=host,
        reference_host_fingerprint="a" * 64,
    )

    assert decision["components"]["existing_full_replay_writer"]["status"] == "not_evaluable"
    assert {row["reason"] for row in decision["components"]["existing_full_replay_writer"]["subcases"]} == {
        "non_acceptance_smoke"
    }


def test_reference_host_gate_fails_when_either_history_subcase_exceeds_limit() -> None:
    host = {"fingerprint": "a" * 64}
    specs = module._build_scenario_specs(_dimensions(), selected=["history_10x"])
    manifest = module._build_fixture_manifest(
        repo_root=Path.cwd(),
        dimensions=_dimensions(),
        specs=specs,
        seed=module.SEED,
        host=host,
        run_label="acceptance_5_warmups_30_repetitions",
    )
    timing = _timing_artifact(manifest)
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["p95"] = module.CPU_LIMIT_NS + 1
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["samples"] = [
        module.CPU_LIMIT_NS + 1
    ] * 30
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["median"] = (
        module.CPU_LIMIT_NS + 1
    )
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["min"] = module.CPU_LIMIT_NS + 1
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["max"] = module.CPU_LIMIT_NS + 1

    decision = module._build_gate_decision(
        timing=timing,
        fixture_manifest=manifest,
        current_host=host,
        reference_host_fingerprint="a" * 64,
    )

    assert decision["components"]["existing_full_replay_writer"]["status"] == "fail"


def test_hostile_baseline_metadata_is_clamped_and_payload_paths_are_discarded(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "storage_runtime_baseline.v1",
                "identity": {
                    "runtime_root": "/do/not/retain",
                    "ledger_sqlite": "/do/not/open.sqlite3",
                },
                "sqlite": {
                    "status": "complete",
                    "tables": [
                        {
                            "table": "trade_events",
                            "row_count": 10**12,
                            "json_bytes": 10**18,
                            "payload": {"secret": "must not be consumed"},
                        },
                        {"table": "position_lots", "row_count": 10**9, "fields_json": "ignored"},
                    ],
                },
                "runtime_storage": {
                    "account_count": 10**6,
                    "account_count_status": "complete",
                    "largest_files": ["private"],
                },
            }
        ),
        encoding="utf-8",
    )

    dimensions = module._load_baseline_dimensions(baseline, repo_root=tmp_path)
    specs = module._build_scenario_specs(dimensions, selected=["history_10x"])

    assert dimensions.event_count == module.MAX_CURRENT_EVENTS
    assert dimensions.current_lot_count == module.MAX_CURRENT_LOTS
    assert dimensions.account_count == module.MAX_ACCOUNTS
    assert dimensions.payload_bytes == module.MAX_PAYLOAD_BYTES
    assert dimensions.metadata["payload_fields_consumed"] == 0
    assert dimensions.metadata["paths_retained"] == 0
    assert specs[0]["effective_dimensions"]["event_count"] == module.MAX_HISTORY_EVENTS
    assert specs[0]["axis_status"] == "not_evaluable_clamped_below_requested_10x"
    assert "/do/not/retain" not in json.dumps(dimensions.metadata)


def test_baseline_account_dimension_uses_payload_free_aggregate_only(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "storage_runtime_baseline.v1",
                "sqlite": {
                    "status": "complete",
                    "tables": [
                        {"table": "trade_events", "row_count": 20, "json_bytes": 20_000},
                        {"table": "position_lots", "row_count": 4, "json_bytes": 4_000},
                    ],
                },
                "runtime_storage": {
                    "account_count": 3,
                    "account_count_status": "complete",
                    "roots": [{"root": "output_accounts", "status": "complete", "file_count": 3, "size_bytes": 100}],
                    "largest_files": ["must not be retained"],
                },
            }
        ),
        encoding="utf-8",
    )

    dimensions = module._load_baseline_dimensions(baseline, repo_root=tmp_path)

    fanout = module._build_scenario_specs(dimensions, selected=["account_fanout"])[0]

    assert dimensions.account_count == 3
    assert dimensions.metadata["account_dimension_source"] == ("runtime_storage.output_accounts_immediate_directories")
    assert fanout["effective_dimensions"]["account_count"] == 15
    assert fanout["axis_status"] == "evaluable"
    assert dimensions.metadata["payload_fields_consumed"] == 0
    assert "must not be retained" not in json.dumps(dimensions.metadata)


def test_legacy_baseline_without_exact_account_count_is_fail_closed_for_fanout(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "storage_runtime_baseline.v1",
                "sqlite": {"status": "complete", "tables": []},
                "runtime_storage": {"roots": [{"root": "output_accounts", "status": "complete", "file_count": 12}]},
            }
        ),
        encoding="utf-8",
    )

    dimensions = module._load_baseline_dimensions(baseline, repo_root=tmp_path)
    fanout = module._build_scenario_specs(dimensions, selected=["account_fanout"])[0]

    assert dimensions.account_count == module.DEFAULT_ACCOUNT_COUNT
    assert dimensions.metadata["account_dimension_source"] == ("safe_default_account_count_unavailable")
    assert fanout["axis_status"] == "not_evaluable_baseline_account_count_unavailable"


def test_empty_observed_account_root_is_not_treated_as_configured_cardinality(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "storage_runtime_baseline.v1",
                "sqlite": {"status": "complete", "tables": []},
                "runtime_storage": {
                    "account_count": 0,
                    "account_count_status": "complete",
                },
            }
        ),
        encoding="utf-8",
    )

    dimensions = module._load_baseline_dimensions(baseline, repo_root=tmp_path)
    fanout = module._build_scenario_specs(dimensions, selected=["account_fanout"])[0]

    assert dimensions.account_count == module.DEFAULT_ACCOUNT_COUNT
    assert dimensions.metadata["account_dimension_source"] == ("safe_default_account_count_unavailable")
    assert fanout["axis_status"] == "not_evaluable_baseline_account_count_unavailable"


@pytest.mark.parametrize("invalid_count", [True, 3.5, "3", -1])
def test_malformed_account_count_is_not_coerced_into_an_exact_dimension(
    tmp_path: Path,
    invalid_count: object,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": "storage_runtime_baseline.v1",
                "sqlite": {"status": "complete", "tables": []},
                "runtime_storage": {
                    "account_count": invalid_count,
                    "account_count_status": "complete",
                },
            }
        ),
        encoding="utf-8",
    )

    dimensions = module._load_baseline_dimensions(baseline, repo_root=tmp_path)

    assert dimensions.account_count == module.DEFAULT_ACCOUNT_COUNT
    assert dimensions.metadata["account_dimension_source"] == (
        "safe_default_account_count_unavailable"
    )


def test_darwin_hardware_identity_collects_cpu_and_machine_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_command_value(command: list[str]) -> str | None:
        calls.append(command[-1])
        return {
            "machdep.cpu.brand_string": "arm",
            "hw.model": "Mac16,10",
        }[command[-1]]

    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module, "_command_value", fake_command_value)

    assert module._hardware_identity() == ("arm", "Mac16,10")
    assert calls == ["machdep.cpu.brand_string", "hw.model"]


def test_darwin_hardware_identity_falls_back_to_bounded_system_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module, "_command_value", lambda _command: None)
    monkeypatch.setattr(
        module,
        "_darwin_hardware_details",
        lambda: {"chip_type": "Apple M4", "machine_model": "Mac16,10"},
    )

    assert module._hardware_identity() == ("Apple M4", "Mac16,10")


def test_host_fingerprint_binds_cpu_and_hardware_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_hardware_identity", lambda: ("Apple M4", "Mac16,10"))
    first = module._host_profile()
    monkeypatch.setattr(module, "_hardware_identity", lambda: ("Apple M4", "Mac16,11"))
    second = module._host_profile()

    assert first["cpu_model"] == "Apple M4"
    assert first["hardware_model"] == "Mac16,10"
    assert first["fingerprint"] != second["fingerprint"]


def test_parent_publishes_all_six_artifacts_atomically_and_labels_smoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        module,
        "_host_profile",
        lambda: {"schema_version": "data_storage_projection_host_profile.v1", "fingerprint": "a" * 64},
    )
    output = tmp_path / "benchmark-output"

    result = module.run_data_storage_projection_benchmark(
        repo_root=Path.cwd(),
        output_dir=output,
        scenario="current_scale",
        warmups=1,
        repetitions=2,
        worker_runner=_fake_workers,
    )

    assert result["run_label"] == "non_acceptance_smoke"
    assert len(module.ARTIFACT_FILENAMES) == 6
    assert {path.name for path in output.iterdir()} == set(module.ARTIFACT_FILENAMES)
    decision = json.loads((output / "decision.json").read_text(encoding="utf-8"))
    assert decision["components"]["lot_diff_publication"]["status"] == "not_evaluable"
    assert decision["phase_3a_combined"]["status"] == "not_ready"
    acceptance = json.loads(
        (output / "phase-3a-acceptance.json").read_text(encoding="utf-8")
    )
    assert acceptance["status"] == "fail"
    assert acceptance["readiness"] == "not_ready"


def test_lifecycle_attempt_benchmark_reuses_harness_and_publishes_one_artifact(
    tmp_path: Path,
) -> None:
    output = tmp_path / "lifecycle-attempt-benchmark"

    result = module.run_lifecycle_attempt_audit_benchmark(
        repo_root=Path.cwd(),
        output_dir=output,
        warmups=0,
        repetitions=1,
        prior_attempts=20,
        receipt_bytes=8 * 1024,
        p99_receipt_bytes=9 * 1024,
        moves=2,
        reference_host_fingerprint="f" * 64,
    )

    assert result["status"] == "not_evaluable"
    assert {path.name for path in output.iterdir()} == {
        module.LIFECYCLE_ATTEMPT_ARTIFACT_FILENAME
    }
    artifact = json.loads(
        (output / module.LIFECYCLE_ATTEMPT_ARTIFACT_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert artifact["schema_version"] == module.LIFECYCLE_ATTEMPT_BENCHMARK_SCHEMA
    assert artifact["dimensions"]["prior_attempts"] == 20
    assert [item["receipt"]["uncompressed_bytes"] for item in artifact["classes"]] == [
        8 * 1024,
        9 * 1024,
    ]
    for item in artifact["classes"]:
        assert item["checks"]["exact_replay_zero_rows"] is True
        assert item["checks"]["exact_replay_zero_physical_bytes"] is True
        assert item["checks"]["shadow_verifier"] is True
        assert item["checks"]["forbidden_work_zero"] is True
        assert item["hot_write_probe"]["attempt_history_scan_count"] == 0
        assert item["sealing"]["non_durable_append"]["fsync_count"] == 0
        assert item["checks"]["concurrent_append_integrity"] is True
    assert artifact["runtime_sealing"]["status"] == "pass"
    assert artifact["runtime_sealing"]["one_touched"]["seal_count"] == 1
    assert artifact["runtime_sealing"]["zero_touched"]["seal_count"] == 0
    assert not any(artifact["forbidden_work"].values())


def test_lifecycle_forbidden_work_detects_real_attempt_history_reader() -> None:
    with module._temporary_lifecycle_attempt_fixture(
        prior_attempts=20,
        receipt_bytes=8 * 1024,
        seed=module.SEED,
    ) as fixture:
        repo = fixture["repo"]
        trace: list[str] = []
        conn = repo._connect()
        try:
            conn.set_trace_callback(trace.append)
            rows = repo.list_trade_lifecycle_attempt_audits(
                case_id="benchmark-case",
                conn=conn,
            )
        finally:
            conn.close()

    assert len(rows) == 20
    assert module._lifecycle_forbidden_work(trace)["attempt_history_scan"] == 1


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "attempt_history_scan",
        "evidence_history_scan",
        "full_replay",
        "global_blob_sweep",
        "decision_projection_write",
        "per_attempt_checkpoint",
    ],
)
def test_lifecycle_attempt_benchmark_fails_for_observed_forbidden_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_key: str,
) -> None:
    fingerprint = "a" * 64
    forbidden = {
        "attempt_history_scan": 0,
        "evidence_history_scan": 0,
        "full_replay": 0,
        "global_blob_sweep": 0,
        "decision_projection_write": 0,
        "per_attempt_checkpoint": 0,
    }
    forbidden[forbidden_key] = 1
    monkeypatch.setattr(
        module,
        "_host_profile",
        lambda: {"fingerprint": fingerprint},
    )
    monkeypatch.setattr(
        module,
        "_measure_lifecycle_receipt_class",
        lambda **_kwargs: {
            "status": "pass",
            "checks": {"forbidden_work_zero": False},
            "forbidden_work": dict(forbidden),
        },
    )
    monkeypatch.setattr(
        module,
        "_measure_lifecycle_runtime_sealing",
        lambda: {
            "status": "pass",
            "checks": {"forbidden_work_zero": True},
            "forbidden_work": {key: 0 for key in forbidden},
        },
    )

    result = module.run_lifecycle_attempt_audit_benchmark(
        repo_root=Path.cwd(),
        output_dir=tmp_path / forbidden_key,
        reference_host_fingerprint=fingerprint,
    )

    assert result["status"] == "fail"
    artifact = json.loads(Path(result["artifact"]).read_text(encoding="utf-8"))
    assert artifact["forbidden_work"][forbidden_key] == 2


def test_phase_3a_gate_passes_only_complete_reference_evidence() -> None:
    timing, allocation = _passing_phase_gate_inputs()

    result = module._phase_3a_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="reference_host_match",
    )

    assert result["lot_diff_publication"]["status"] == "pass"
    assert result["checkpoint_tail"]["status"] == "pass"
    assert result["combined"] == {
        "status": "ready",
        "reason": "all_phase_3a_gates_pass",
    }
    assert result["evidence"]["retained_lots_10x_guarantee"] is False


def test_phase_3a_gate_does_not_hide_non_checkpoint_failure() -> None:
    timing, allocation = _passing_phase_gate_inputs()
    timing["comparable_facades"][0]["fast"]["wall_time_ns"] = (
        module._timing_distribution([module.PHASE_3A_WALL_LIMIT_NS + 1] * 30)
    )

    result = module._phase_3a_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="reference_host_match",
    )

    assert result["lot_diff_publication"]["status"] == "fail"
    assert result["checkpoint_tail"]["status"] == "pass"
    assert result["combined"]["status"] == "not_ready"


def test_phase_3a_candidate_gate_counts_unique_ids_not_repeat_calls() -> None:
    timing, allocation = _passing_phase_gate_inputs()
    calls = timing["comparable_facades"][0]["fast"]["call_count_max"]
    calls["candidate_event_ids_requested"] = 4

    repeated_same_id = module._phase_3a_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="reference_host_match",
    )
    assert repeated_same_id["lot_diff_publication"]["status"] == "pass"

    calls["candidate_event_ids_unique"] = 2
    unbounded = module._phase_3a_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="reference_host_match",
    )
    assert "single_combo_metadata_close:candidate_id_lookup_unbounded" in unbounded[
        "lot_diff_publication"
    ]["failures"]


def test_phase_3a_gate_rejects_checkpoint_write_between_rotations() -> None:
    timing, allocation = _passing_phase_gate_inputs()
    timing["checkpoint"]["no_rotation"]["checkpoint_row_deltas"] = [1]

    result = module._phase_3a_gate(
        timing=timing,
        allocation=allocation,
        run_label="acceptance_5_warmups_30_repetitions",
        comparable=True,
        comparison_reason="reference_host_match",
    )

    assert result["lot_diff_publication"]["status"] == "pass"
    assert result["checkpoint_tail"]["status"] == "fail"
    assert "checkpoint:no_rotation_row_write" in result["checkpoint_tail"]["failures"]


def test_phase_3a_worker_validation_requires_exact_5_30_samples() -> None:
    timing, allocation = _passing_phase_gate_inputs()
    cpu = {
        "schema_version": "data_storage_projection_phase3a_cpu_profile.v1",
        "fixture_sha256": "f" * 64,
        "setup_included": False,
    }
    fixture = {"phase_3a": {"fixture_sha256": "f" * 64}}

    module._validate_phase_3a_worker_artifacts(
        fixture_manifest=fixture,
        timing={"phase_3a": timing},
        cpu_profile={"phase_3a": cpu},
        allocation_profile={"phase_3a": allocation},
        repetitions=30,
    )

    timing["fingerprint_only"]["current"]["wall_time_ns"] = (
        module._timing_distribution([100] * 29)
    )
    with pytest.raises(RuntimeError, match="sample count"):
        module._validate_phase_3a_worker_artifacts(
            fixture_manifest=fixture,
            timing={"phase_3a": timing},
            cpu_profile={"phase_3a": cpu},
            allocation_profile={"phase_3a": allocation},
            repetitions=30,
        )


def test_phase_3a_pair_uses_independent_identical_database_copies() -> None:
    spec = _small_spec(
        key="phase_3a.runtime",
        event_count=20,
        lot_count=4,
        account_count=1,
    )
    with module._temporary_phase_3a_base(spec, seed=module.SEED) as base:
        result = module._phase_3a_pair(
            base,
            operation="single_combo_metadata_close",
            warmups=0,
            repetitions=1,
        )

    assert result["fixture_reset"] == "independent_copies_same_base_sqlite"
    assert result["forced_full"]["base_sqlite_sha256"] == result["fast"][
        "base_sqlite_sha256"
    ]
    assert result["parity"]["exact"] is True


def test_parent_failure_leaves_absent_output_unmodified(tmp_path: Path) -> None:
    output = tmp_path / "benchmark-output"

    def fail_worker(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("synthetic worker failure")

    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        module.run_data_storage_projection_benchmark(
            repo_root=Path.cwd(),
            output_dir=output,
            scenario="current_scale",
            warmups=0,
            repetitions=1,
            worker_runner=fail_worker,
        )

    assert not output.exists()


def test_parent_refuses_nonempty_or_symlink_output(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing.txt").write_text("keep", encoding="utf-8")
    symlink = tmp_path / "linked-output"
    symlink.symlink_to(nonempty, target_is_directory=True)

    with pytest.raises(ValueError, match="empty"):
        module._resolve_output_dir(nonempty, repo_root=tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        module._resolve_output_dir(symlink, repo_root=tmp_path)
    assert (nonempty / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_worker_artifact_validation_rejects_fixture_identity_drift() -> None:
    specs = [_small_spec()]
    host = {"fingerprint": "a" * 64}
    manifest = module._build_fixture_manifest(
        repo_root=Path.cwd(),
        dimensions=_dimensions(),
        specs=specs,
        seed=module.SEED,
        host=host,
        run_label="non_acceptance_smoke",
    )
    timing = _timing_artifact(manifest)
    cpu = _profile_artifact(
        manifest,
        schema=module.CPU_PROFILE_SCHEMA,
        mode="cprofile_separate_process",
    )
    allocation = _profile_artifact(
        manifest,
        schema=module.ALLOCATION_PROFILE_SCHEMA,
        mode="tracemalloc_separate_process",
    )
    cpu = copy.deepcopy(cpu)
    cpu["scenarios"][0]["fixture_sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="fixture identity mismatch"):
        module._validate_worker_artifacts(
            fixture_manifest=manifest,
            timing=timing,
            cpu_profile=cpu,
            allocation_profile=allocation,
            expected_warmups=5,
            expected_repetitions=30,
            expected_run_label="acceptance_5_warmups_30_repetitions",
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"].pop("wall_time_ns"),
            "timing distribution is invalid",
        ),
        (
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"]["wall_time_ns"][
                "samples"
            ].pop(),
            "timing sample count is invalid",
        ),
        (
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"].update(
                wall_time_ns={
                    **timing["scenarios"][0]["components"]["existing_full_replay_writer"]["wall_time_ns"],
                    "p95": 0,
                }
            ),
            "timing summary is inconsistent",
        ),
        (
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"]["wall_time_ns"][
                "samples"
            ].__setitem__(0, -1),
            "timing sample value is invalid",
        ),
    ],
)
def test_worker_artifact_validation_fails_closed_on_invalid_timing(
    mutate: Any,
    message: str,
) -> None:
    specs = [_small_spec()]
    manifest = module._build_fixture_manifest(
        repo_root=Path.cwd(),
        dimensions=_dimensions(),
        specs=specs,
        seed=module.SEED,
        host={"fingerprint": "a" * 64},
        run_label="acceptance_5_warmups_30_repetitions",
    )
    timing = _timing_artifact(manifest)
    cpu = _profile_artifact(
        manifest,
        schema=module.CPU_PROFILE_SCHEMA,
        mode="cprofile_separate_process",
    )
    allocation = _profile_artifact(
        manifest,
        schema=module.ALLOCATION_PROFILE_SCHEMA,
        mode="tracemalloc_separate_process",
    )
    mutate(timing)

    with pytest.raises(RuntimeError, match=message):
        module._validate_worker_artifacts(
            fixture_manifest=manifest,
            timing=timing,
            cpu_profile=cpu,
            allocation_profile=allocation,
            expected_warmups=5,
            expected_repetitions=30,
            expected_run_label="acceptance_5_warmups_30_repetitions",
        )
