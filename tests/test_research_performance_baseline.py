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
    return {
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


def _profile_artifact(
    fixture_manifest: dict[str, Any],
    *,
    schema: str,
    mode: str,
) -> dict[str, Any]:
    return {
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
        timing_distribution = module._timing_distribution(
            [100] * worker_spec["repetitions"]
        )
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
    assert matching["components"]["lot_diff_publication"]["status"] == "not_implemented"
    assert matching["phase_3a_combined"] == {
        "status": "not_ready",
        "reason": "lot_diff_publication_not_implemented",
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
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["p95"] = (
        module.CPU_LIMIT_NS + 1
    )
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["samples"] = [
        module.CPU_LIMIT_NS + 1
    ] * 30
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["median"] = (
        module.CPU_LIMIT_NS + 1
    )
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["min"] = (
        module.CPU_LIMIT_NS + 1
    )
    timing["scenarios"][1]["components"]["existing_full_replay_writer"]["cpu_time_ns"]["max"] = (
        module.CPU_LIMIT_NS + 1
    )

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
                "runtime_storage": {"account_count": 10**6, "largest_files": ["private"]},
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
                    "roots": [
                        {"root": "output_accounts", "status": "complete", "file_count": 3, "size_bytes": 100}
                    ],
                    "largest_files": ["must not be retained"],
                },
            }
        ),
        encoding="utf-8",
    )

    dimensions = module._load_baseline_dimensions(baseline, repo_root=tmp_path)

    assert dimensions.account_count == 1
    assert dimensions.metadata["payload_fields_consumed"] == 0
    assert "must not be retained" not in json.dumps(dimensions.metadata)


def test_parent_publishes_all_five_artifacts_atomically_and_labels_smoke(
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
    assert {path.name for path in output.iterdir()} == set(module.ARTIFACT_FILENAMES)
    decision = json.loads((output / "decision.json").read_text(encoding="utf-8"))
    assert decision["components"]["lot_diff_publication"]["status"] == "not_implemented"
    assert decision["phase_3a_combined"]["status"] == "not_ready"


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
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"].pop(
                "wall_time_ns"
            ),
            "timing distribution is invalid",
        ),
        (
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"][
                "wall_time_ns"
            ]["samples"].pop(),
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
            lambda timing: timing["scenarios"][0]["components"]["existing_full_replay_writer"][
                "wall_time_ns"
            ]["samples"].__setitem__(0, -1),
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
