from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.shadow_replay.common import artifact_content_sha256, render_json_text
from src.application.strategy_lab.top1.contracts import (
    VALIDATION_FILL_CONTRACT_VERSION,
    VALIDATION_METRIC_CONTRACT_VERSION,
    VALIDATION_REQUIRED_DAYS,
    build_research_spec_sha256,
    build_sell_put_top1_research_spec,
    build_validation_spec_sha256,
)
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    authorize_research,
    authorize_validation,
    build_hidden_window_commitment,
    prepare_experiment,
    read_public_receipt,
    read_public_status,
    seal_generation,
    start_research,
    start_validation,
    terminate_experiment,
)
from src.application.strategy_lab.top1.terminal_projection import (
    publish_exact_text,
    recover_terminal_projection,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
    compact_json,
)
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
    top1_hk_schedule_fixture,
)


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
NOW = "2026-08-15T03:00:00Z"


def _spec(experiment_id: str, *, validation: bool = False) -> dict[str, Any]:
    spec = build_sell_put_top1_research_spec(
        topic_id=f"topic-{experiment_id}",
        experiment_id=experiment_id,
        account="user1",
        market_calendar_version="hk-calendar.v1",
        research_source={
            "mode": "sealed_historical_dataset",
            "dataset_ref": f"strategy_lab/top1/{experiment_id}/research.json",
            "dataset_sha256": SHA_A,
            "research_cutoff_at": "2026-08-14T16:00:00Z",
            "start_trading_date": "2026-06-19",
            "end_trading_date": "2026-08-14",
        },
    )
    if validation:
        spec.update(
            {
                "validation_evaluation": {
                    "required_days": VALIDATION_REQUIRED_DAYS,
                    "window_mode": "fixed_future_consecutive_trading_days",
                    "visibility": "hidden_until_final_seal",
                },
                "fill_observation": {
                    "applies_to": "validation_only",
                    "contract_version": VALIDATION_FILL_CONTRACT_VERSION,
                },
                "timer_binding": {
                    "revision": "top1-advance.v1",
                    "producer_catchup_grace_seconds": 30,
                    "producer_run_timeout_upper_bound_seconds": 120,
                    "advance_cadence_seconds": 60,
                    "fill_observation_duration_upper_bound_seconds": 120,
                    "terms_capture_duration_upper_bound_seconds": 120,
                },
                "validation_metrics": {
                    "contract_version": VALIDATION_METRIC_CONTRACT_VERSION,
                    "confidence_level": 0.95,
                    "worst_fraction": 0.20,
                },
            }
        )
    return spec


def _dates(start: date, *, step: int = 1) -> list[str]:
    current = start
    values: list[str] = []
    while len(values) < VALIDATION_REQUIRED_DAYS:
        if current.weekday() < 5:
            values.append(current.isoformat())
            remaining = step
            while remaining:
                current += timedelta(days=1)
                if current.weekday() < 5:
                    remaining -= 1
        else:
            current += timedelta(days=1)
    return values


def _store(tmp_path: Path) -> ExperimentStore:
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    store.migrate(migrated_at_utc=NOW)
    return store


def _enable(
    store: ExperimentStore, root: Path, *, idempotency_key: str = "enable"
) -> None:
    del store, root, idempotency_key


def _restore_v3_feature_table(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE strategy_lab_features(
                market TEXT NOT NULL,
                account TEXT NOT NULL,
                user_opt_in INTEGER NOT NULL CHECK(user_opt_in IN (0, 1)),
                last_actor TEXT NOT NULL,
                last_occurred_at_utc TEXT NOT NULL,
                state_version INTEGER NOT NULL CHECK(state_version >= 1),
                PRIMARY KEY(market, account)
            )
            """
        )


def _ready_research(store: ExperimentStore, root: Path, experiment_id: str) -> None:
    spec = _spec(experiment_id)
    prepared = prepare_experiment(
        store,
        spec,
        provenance={"source_commit_sha": "commit-1", "config_sha256": SHA_B},
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=f"prepare-{experiment_id}",
        artifact_root=root,
        environ=AVAILABLE,
    )
    authorize_research(
        store,
        experiment_id=experiment_id,
        research_spec_sha256=str(prepared["research_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=f"authorize-research-{experiment_id}",
        artifact_root=root,
        environ=AVAILABLE,
    )
    start_research(
        store,
        experiment_id=experiment_id,
        research_spec_sha256=str(prepared["research_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=f"start-research-{experiment_id}",
        artifact_root=root,
        environ=AVAILABLE,
    )
    seal_generation(
        store,
        experiment_id=experiment_id,
        generation_kind="research",
        actor="runner",
        occurred_at_utc=NOW,
        idempotency_key=f"seal-research-{experiment_id}",
        artifact_root=root,
        environ=AVAILABLE,
    )
    recover_terminal_projection(store, root)


def _lock_store_challenger(
    store: ExperimentStore,
    *,
    root: Path,
    experiment_id: str,
    trading_dates: list[str],
    idempotency_key: str,
) -> dict[str, Any]:
    spec = _spec(experiment_id, validation=True)
    research = next(
        item
        for item in store.generations(experiment_id)
        if item["generation_kind"] == "research"
    )
    research_hash = build_research_spec_sha256(spec)
    terminal_hash = str(research["terminal_file_sha256"])
    calendar_binding = seal_market_calendar_fixture(
        root, trading_dates, version="hk-calendar.v1"
    )
    commitment = build_hidden_window_commitment(
        experiment_id=experiment_id,
        account="user1",
        validation_start_trading_date=trading_dates[0],
        market_calendar_binding=calendar_binding,
        schedule=top1_hk_schedule_fixture(),
        challenger_variant_id="concentration-0.002",
        research_spec_sha256=research_hash,
        research_terminal_file_sha256=terminal_hash,
        behavior_binding_sha256=str(spec["baseline"]["behavior_binding_sha256"]),
    )
    commitment_sha = canonical_sha256(commitment)
    commitment_text = render_json_text(commitment)
    return store.lock_challenger(
        experiment_id=experiment_id,
        spec_json=compact_json(spec),
        research_spec_sha256=research_hash,
        validation_spec_sha256=build_validation_spec_sha256(
            spec,
            research_terminal_sha256=terminal_hash,
            challenger_variant_id="concentration-0.002",
            hidden_window_commitment_sha256=commitment_sha,
        ),
        research_leader="concentration-0.002",
        research_receipt_ref=(
            f"strategy_lab/top1/{experiment_id}/research-receipt.json"
        ),
        research_receipt_file_sha256=terminal_hash,
        commitment_json=compact_json(commitment),
        commitment_sha256=commitment_sha,
        commitment_ref=(
            f"strategy_lab/top1/experiments/{experiment_id}/"
            f"hidden_window_commitments/{commitment_sha}.json"
        ),
        commitment_content_sha256=artifact_content_sha256(commitment),
        commitment_file_sha256=hashlib.sha256(
            commitment_text.encode("utf-8")
        ).hexdigest(),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=idempotency_key,
    )


def _ready_validation(
    store: ExperimentStore,
    root: Path,
    experiment_id: str,
    trading_dates: list[str],
) -> dict[str, Any]:
    _ready_research(store, root, experiment_id)
    locked = _lock_store_challenger(
        store,
        root=root,
        experiment_id=experiment_id,
        trading_dates=trading_dates,
        idempotency_key=f"lock-{experiment_id}",
    )
    authorize_validation(
        store,
        experiment_id=experiment_id,
        validation_spec_sha256=str(locked["validation_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=f"authorize-validation-{experiment_id}",
        artifact_root=root,
        environ=AVAILABLE,
    )
    return store.experiment(experiment_id)


def test_schema_migration_is_explicit_private_and_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "lab.sqlite3"
    store = ExperimentStore(path)
    assert store.schema_state() == {"status": "not_initialized", "schema_version": None}
    assert not path.exists()
    assert store.migrate(migrated_at_utc=NOW) == {"status": "ready", "schema_version": 4}
    assert store.migrate(migrated_at_utc=NOW) == {"status": "ready", "schema_version": 4}
    assert stat_mode(path) == 0o600
    assert not Path(f"{path}-wal").exists()
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "strategy_lab_schema",
            "strategy_lab_experiments",
            "strategy_lab_generations",
            "strategy_lab_hidden_commitments",
            "strategy_lab_events",
            "strategy_lab_corpus_days",
            "strategy_lab_corpus_points",
            "strategy_lab_validation_decisions",
            "strategy_lab_validation_days",
            "strategy_lab_fill_observations",
            "strategy_lab_outcome_jobs",
            "strategy_lab_expiry_close_facts",
        }

    v0_path = tmp_path / "v0.sqlite3"
    with sqlite3.connect(v0_path) as connection:
        connection.execute(
            "CREATE TABLE strategy_lab_schema(component TEXT PRIMARY KEY, schema_version INTEGER, migrated_at_utc TEXT)"
        )
        connection.execute(
            "INSERT INTO strategy_lab_schema VALUES (?, 0, ?)",
            ("sell_put_top1_experiment_store", NOW),
        )
    assert ExperimentStore(v0_path).migrate(migrated_at_utc=NOW)["schema_version"] == 4

    v1_path = tmp_path / "v1.sqlite3"
    v1_store = ExperimentStore(v1_path)
    v1_store.migrate(migrated_at_utc=NOW)
    _restore_v3_feature_table(v1_path)
    with sqlite3.connect(v1_path) as connection:
        connection.execute("DROP TABLE strategy_lab_expiry_close_facts")
        connection.execute("DROP TABLE strategy_lab_outcome_jobs")
        connection.execute("DROP TABLE strategy_lab_fill_observations")
        connection.execute("DROP TABLE strategy_lab_validation_days")
        connection.execute("DROP TABLE strategy_lab_validation_decisions")
        connection.execute("DROP TABLE strategy_lab_corpus_points")
        connection.execute("DROP TABLE strategy_lab_corpus_days")
        connection.execute("UPDATE strategy_lab_schema SET schema_version = 1")
    assert v1_store.schema_state() == {"status": "migration_required", "schema_version": 1}
    assert v1_store.migrate(migrated_at_utc=NOW) == {
        "status": "ready",
        "schema_version": 4,
    }

    v2_path = tmp_path / "v2.sqlite3"
    v2_store = ExperimentStore(v2_path)
    v2_store.migrate(migrated_at_utc=NOW)
    prepare_experiment(
        v2_store,
        _spec("v2-preserved"),
        provenance={"source_commit_sha": "commit-v2", "config_sha256": SHA_B},
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="v2-prepare",
        artifact_root=tmp_path / "v2-artifacts",
        environ=AVAILABLE,
    )
    _restore_v3_feature_table(v2_path)
    with sqlite3.connect(v2_path) as connection:
        connection.execute("DROP TABLE strategy_lab_expiry_close_facts")
        connection.execute("DROP TABLE strategy_lab_outcome_jobs")
        connection.execute("DROP TABLE strategy_lab_fill_observations")
        connection.execute("DROP TABLE strategy_lab_validation_days")
        connection.execute("DROP TABLE strategy_lab_validation_decisions")
        connection.execute("UPDATE strategy_lab_schema SET schema_version = 2")
    with pytest.raises(ExperimentStoreError) as exc_info:
        v2_store.migrate(migrated_at_utc=NOW)
    assert exc_info.value.reason_code == "migration_active_experiments"
    assert v2_store.schema_state() == {"status": "migration_required", "schema_version": 2}
    with sqlite3.connect(v2_path) as connection:
        assert connection.execute(
            "SELECT topic_id FROM strategy_lab_experiments WHERE experiment_id = ?",
            ("v2-preserved",),
        ).fetchone() == ("topic-v2-preserved",)
        assert connection.execute(
            "SELECT event_type FROM strategy_lab_events WHERE experiment_id = ?",
            ("v2-preserved",),
        ).fetchone() == ("experiment_prepared",)

    v3_path = tmp_path / "v3-terminal.sqlite3"
    v3_store = ExperimentStore(v3_path)
    v3_store.migrate(migrated_at_utc=NOW)
    v3_root = tmp_path / "v3-artifacts"
    prepare_experiment(
        v3_store,
        _spec("v3-terminal"),
        provenance={"source_commit_sha": "commit-v3", "config_sha256": SHA_B},
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="v3-prepare",
        artifact_root=v3_root,
        environ=AVAILABLE,
    )
    terminate_experiment(
        v3_store,
        experiment_id="v3-terminal",
        reason="human_abandoned",
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="v3-abort",
        artifact_root=v3_root,
    )
    _restore_v3_feature_table(v3_path)
    with sqlite3.connect(v3_path) as connection:
        connection.execute("UPDATE strategy_lab_schema SET schema_version = 3")
    assert v3_store.migrate(migrated_at_utc=NOW) == {
        "status": "ready",
        "schema_version": 4,
    }
    assert read_public_receipt(v3_store, experiment_id="v3-terminal") is not None

    partial = tmp_path / "partial.sqlite3"
    with sqlite3.connect(partial) as connection:
        connection.execute("CREATE TABLE unexpected(value TEXT)")
    with pytest.raises(ExperimentStoreError) as exc_info:
        ExperimentStore(partial).migrate(migrated_at_utc=NOW)
    assert exc_info.value.reason_code == "schema_unsupported"

    missing_index = tmp_path / "missing-index.sqlite3"
    missing_store = ExperimentStore(missing_index)
    missing_store.migrate(migrated_at_utc=NOW)
    with sqlite3.connect(missing_index) as connection:
        connection.execute("DROP INDEX strategy_lab_one_active_validation")
    assert missing_store.schema_state()["status"] == "schema_unsupported"


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_service_off_rejects_lifecycle_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    with pytest.raises(Top1LifecycleError) as exc_info:
        prepare_experiment(
            store,
            _spec("service-off"),
            provenance={"source_commit_sha": "commit-off"},
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="prepare-off",
            artifact_root=root,
            environ={},
        )
    assert exc_info.value.reason_code == "strategy_lab_service_disabled"
    assert store.active_experiments("HK", "user1") == []


def test_exact_publisher_adopts_bytes_and_rejects_conflict_or_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    target = publish_exact_text(root, "safe/result.json", b"{}\n")
    assert publish_exact_text(root, "safe/result.json", b"{}\n") == target
    assert stat_mode(target) == 0o600
    with pytest.raises(ValueError, match="bytes conflict"):
        publish_exact_text(root, "safe/result.json", b"{ }\n")
    link = root / "unsafe"
    link.symlink_to(tmp_path)
    with pytest.raises(OSError, match="symlink"):
        publish_exact_text(root, "unsafe/result.json", b"{}\n")


def test_separate_authorization_starts_evidence_bound_validation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    _enable(store, root)
    dates = _dates(date(2026, 9, 1))
    ready = _ready_validation(store, root, "experiment-a", dates)
    with pytest.raises(Top1LifecycleError) as exc_info:
        start_validation(
            store,
            experiment_id="experiment-a",
            validation_spec_sha256=SHA_C,
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="start-validation-wrong",
            artifact_root=root,
            environ=AVAILABLE,
        )
    assert exc_info.value.reason_code == "authorization_required"

    start_validation(
        store,
        experiment_id="experiment-a",
        validation_spec_sha256=str(ready["validation_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="start-validation-a",
        artifact_root=root,
        environ=AVAILABLE,
    )
    state = store.experiment("experiment-a")
    assert state["completed_validation_partitions"] == 0
    assert state["validation_progress"] == "collecting_decisions"
    assert {row["generation_kind"] for row in store.generations("experiment-a")} == {
        "research",
        "hidden",
        "outcome",
    }
    assert not hasattr(store, "commit_validation_point")
    assert not hasattr(store, "seal_validation_partition")
    public = json.dumps(read_public_status(store, experiment_id="experiment-a"))
    assert "daily_delta" not in public
    assert read_public_receipt(store, experiment_id="experiment-a") is None


def test_public_status_and_receipt_reject_cross_account_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    spec = _spec("experiment-user2")
    spec["account"] = "user2"
    prepare_experiment(
        store,
        spec,
        provenance={"source_commit_sha": "commit-1", "config_sha256": SHA_B},
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="prepare-user2",
        artifact_root=root,
        environ=AVAILABLE,
    )

    with pytest.raises(Top1LifecycleError) as exc_info:
        read_public_status(
            store,
            experiment_id="experiment-user2",
            expected_market="HK",
            expected_account="user1",
        )
    assert exc_info.value.reason_code == "experiment_conflict"

    with pytest.raises(Top1LifecycleError) as exc_info:
        read_public_receipt(
            store,
            experiment_id="experiment-user2",
            expected_market="HK",
            expected_account="user1",
        )
    assert exc_info.value.reason_code == "experiment_conflict"


def test_exact_date_overlap_and_content_addressed_orphan(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    _enable(store, root)
    odd_dates = _dates(date(2026, 9, 1), step=2)
    even_dates = _dates(date(2026, 9, 2), step=2)
    first = _ready_validation(store, root, "experiment-odd", odd_dates)
    start_validation(
        store,
        experiment_id="experiment-odd",
        validation_spec_sha256=str(first["validation_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="start-odd",
        artifact_root=root,
        environ=AVAILABLE,
    )
    second = _ready_validation(store, root, "experiment-even", even_dates)
    with pytest.raises(Top1LifecycleError) as exc_info:
        start_validation(
            store,
            experiment_id="experiment-even",
            validation_spec_sha256=str(second["validation_spec_sha256"]),
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="start-even",
            artifact_root=root,
            environ=AVAILABLE,
        )
    assert exc_info.value.reason_code == "validation_slot_occupied"
    even_ref = str(store.experiment("experiment-even")["proposed_commitment_ref"])
    assert (root / even_ref).is_file()
    terminate_experiment(
        store,
        experiment_id="experiment-odd",
        reason="human_abandoned",
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="abort-odd",
        artifact_root=root,
    )
    start_validation(
        store,
        experiment_id="experiment-even",
        validation_spec_sha256=str(second["validation_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="start-even",
        artifact_root=root,
        environ=AVAILABLE,
    )
    terminate_experiment(
        store,
        experiment_id="experiment-even",
        reason="human_abandoned",
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="abort-even",
        artifact_root=root,
    )

    overlap = [odd_dates[0], *_dates(date(2027, 1, 1))[:19]]
    overlap = sorted(overlap)
    third = _ready_validation(store, root, "experiment-overlap", overlap)
    with pytest.raises(Top1LifecycleError) as exc_info:
        start_validation(
            store,
            experiment_id="experiment-overlap",
            validation_spec_sha256=str(third["validation_spec_sha256"]),
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="start-overlap",
            artifact_root=root,
            environ=AVAILABLE,
        )
    assert exc_info.value.reason_code == "hidden_window_overlap"
    orphan_ref = str(store.experiment("experiment-overlap")["proposed_commitment_ref"])
    assert (root / orphan_ref).is_file()

    replacement_dates = _dates(date(2027, 3, 1))
    relocked = _lock_store_challenger(
        store,
        root=root,
        experiment_id="experiment-overlap",
        trading_dates=replacement_dates,
        idempotency_key="relock-overlap",
    )
    replacement_ref = str(relocked["proposed_commitment_ref"])
    assert not (root / replacement_ref).exists()
    with pytest.raises(Top1LifecycleError) as stale_info:
        start_validation(
            store,
            experiment_id="experiment-overlap",
            validation_spec_sha256=str(third["validation_spec_sha256"]),
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="start-stale-overlap",
            artifact_root=root,
            environ=AVAILABLE,
        )
    assert stale_info.value.reason_code == "authorization_required"
    assert not (root / replacement_ref).exists()
    assert not any(
        event["event_type"] == "validation_started"
        and json.loads(str(event["payload_json"]))["authorized_hash"]
        == third["validation_spec_sha256"]
        for event in store.events("experiment-overlap")
    )
    authorize_validation(
        store,
        experiment_id="experiment-overlap",
        validation_spec_sha256=str(relocked["validation_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="reauthorize-overlap",
        artifact_root=root,
        environ=AVAILABLE,
    )
    start_validation(
        store,
        experiment_id="experiment-overlap",
        validation_spec_sha256=str(relocked["validation_spec_sha256"]),
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="start-replacement",
        artifact_root=root,
        environ=AVAILABLE,
    )
    assert store.commitment_dates("experiment-overlap") == replacement_dates
    assert str(store.experiment("experiment-overlap")["proposed_commitment_ref"]) != orphan_ref


def test_terminal_competition_crash_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    _enable(store, root)
    _ready_research(store, root, "experiment-terminal")
    research_before = store.generations("experiment-terminal")[0]

    crashed = False

    def publish_then_crash(
        artifact_root: str | Path, relative_ref: str, content: bytes
    ) -> Path:
        nonlocal crashed
        path = publish_exact_text(artifact_root, relative_ref, content)
        if not crashed:
            crashed = True
            raise RuntimeError("crash after publish before CAS")
        return path

    with pytest.raises(RuntimeError):
        terminate_experiment(
            store,
            experiment_id="experiment-terminal",
            reason="human_abandoned",
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="abort-terminal",
            artifact_root=root,
            publisher=publish_then_crash,
        )
    assert store.experiment("experiment-terminal")["terminal_mode"] == "aborted"
    assert read_public_receipt(store, experiment_id="experiment-terminal") is None
    recover_terminal_projection(store, root, experiment_id="experiment-terminal")
    receipt = read_public_receipt(store, experiment_id="experiment-terminal")
    assert receipt is not None
    assert receipt["terminal"]["mode"] == "aborted"
    research_after = store.generations("experiment-terminal")[0]
    assert research_after["terminal_mode"] == "completed"
    assert research_after["terminal_file_sha256"] == research_before["terminal_file_sha256"]
    with pytest.raises(Top1LifecycleError) as exc_info:
        seal_generation(
            store,
            experiment_id="experiment-terminal",
            generation_kind="research",
            actor="runner",
            occurred_at_utc=NOW,
            idempotency_key="late-seal",
            artifact_root=root,
            environ=AVAILABLE,
        )
    assert exc_info.value.reason_code == "terminal_conflict"
