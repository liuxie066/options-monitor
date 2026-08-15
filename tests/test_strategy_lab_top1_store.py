from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from domain.domain.engine import (
    SELL_PUT_RANKING_CONTRACT_VERSION,
    SELL_PUT_RANKING_PROFILES,
)
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.application.opening_candidate_snapshot import OPENING_CANDIDATE_SNAPSHOT_SCHEMA
from src.application.strategy_lab.top1.contracts import (
    ACCEPTED_SET_CONTRACT_VERSION,
    EXPERIMENT_SPEC_SCHEMA_VERSION,
    EXPIRY_OUTCOME_CONTRACT_VERSION,
    RESEARCH_METRIC_CONTRACT_VERSION,
    RESEARCH_SELECTION_CONTRACT_VERSION,
    VALIDATION_FILL_CONTRACT_VERSION,
    VALIDATION_METRIC_CONTRACT_VERSION,
    build_behavior_binding,
    build_research_spec_sha256,
)
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    authorize_research,
    authorize_validation,
    commit_validation_point,
    effective_feature_status,
    lock_challenger,
    prepare_experiment,
    read_public_receipt,
    read_public_status,
    seal_generation,
    seal_validation_partition,
    set_account_opt_in,
    start_research,
    start_validation,
    terminate_experiment,
)
from src.application.strategy_lab.top1.ranking import RANKING_PROJECTION_SCHEMA_VERSION
from src.application.strategy_lab.top1.terminal_projection import (
    publish_exact_text,
    recover_terminal_projection,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
NOW = "2026-08-15T03:00:00Z"


def _behavior_versions(calendar: str = "hk-calendar.v1") -> dict[str, str]:
    return {
        "baseline_version": "sell-put-baseline.v1",
        "opening_snapshot_schema_version": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
        "accepted_set_contract_version": ACCEPTED_SET_CONTRACT_VERSION,
        "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
        "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
        "research_selection_contract_version": RESEARCH_SELECTION_CONTRACT_VERSION,
        "research_metric_contract_version": RESEARCH_METRIC_CONTRACT_VERSION,
        "validation_fill_contract_version": VALIDATION_FILL_CONTRACT_VERSION,
        "validation_metric_contract_version": VALIDATION_METRIC_CONTRACT_VERSION,
        "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "market_calendar_version": calendar,
        "expiry_outcome_contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
    }


def _spec(experiment_id: str, *, validation: bool = False) -> dict[str, Any]:
    profiles = tuple(SELL_PUT_RANKING_PROFILES)
    spec: dict[str, Any] = {
        "schema_version": EXPERIMENT_SPEC_SCHEMA_VERSION,
        "topic_id": f"topic-{experiment_id}",
        "experiment_id": experiment_id,
        "market": "HK",
        "account": "lx",
        "hypothesis": {
            "hypothesis_type": "sell_put_ranking",
            "statement": "Prefer lower cross-symbol concentration earlier.",
            "mechanism": "Move the existing concentration fact ahead of return.",
            "independent_variable": "cross_symbol_concentration_priority",
            "expected_direction": "higher_top1_efficiency_without_higher_concentration",
        },
        "baseline": {
            "version": "sell-put-baseline.v1",
            "opening_snapshot_schema": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
            "accepted_set_contract_version": ACCEPTED_SET_CONTRACT_VERSION,
            "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
            "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
            "behavior_binding_sha256": build_behavior_binding(_behavior_versions()),
        },
        "research_source": {
            "mode": "sealed_historical_dataset",
            "dataset_ref": f"strategy_lab/top1/{experiment_id}/research.json",
            "dataset_sha256": SHA_A,
            "research_cutoff_at": "2026-08-14T16:00:00Z",
            "start_trading_date": "2026-06-19",
            "end_trading_date": "2026-08-14",
        },
        "research_evaluation": {
            "contract_version": RESEARCH_SELECTION_CONTRACT_VERSION,
            "metric_contract_version": RESEARCH_METRIC_CONTRACT_VERSION,
            "fill_assumption": "t0_sell_limit",
            "required_days": 40,
            "window_mode": "fixed_consecutive_trading_days",
            "visibility": "visible_after_research_seal",
        },
        "variants": [
            {"variant_id": "baseline", "patch": {}},
            *[
                {
                    "variant_id": f"level-{index}",
                    "patch": {"ranking_profile": profile},
                }
                for index, profile in enumerate(profiles, start=1)
            ],
        ],
        "frozen_safety": {
            "mode": "inherit_each_point_producer_accepted_set",
            "variant_may_change_acceptance": False,
        },
        "economics_contracts": {
            "fee_schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
            "market_calendar_version": "hk-calendar.v1",
        },
        "expiry_outcome": {
            "contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
            "spot_source": "opend_history_kline",
            "ktype": "K_DAY",
            "autype": "NONE",
            "price_field": "close",
            "due_boundary": "expiration_observation_start_ms",
            "pending_elapsed_hours": 72,
        },
    }
    if validation:
        spec.update(
            {
                "validation_evaluation": {
                    "required_days": 20,
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
    return [(start + timedelta(days=index * step)).isoformat() for index in range(20)]


def _store(tmp_path: Path) -> ExperimentStore:
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    store.migrate(migrated_at_utc=NOW)
    return store


def _enable(
    store: ExperimentStore, root: Path, *, idempotency_key: str = "enable"
) -> None:
    set_account_opt_in(
        store,
        market="HK",
        account="lx",
        enabled=True,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=idempotency_key,
        artifact_root=root,
        environ=AVAILABLE,
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


def _ready_validation(
    store: ExperimentStore,
    root: Path,
    experiment_id: str,
    trading_dates: list[str],
) -> dict[str, Any]:
    _ready_research(store, root, experiment_id)
    research = store.generations(experiment_id)[0]
    locked = lock_challenger(
        store,
        _spec(experiment_id, validation=True),
        system_leader="level-1",
        challenger_variant_id="level-1",
        research_receipt_ref=f"strategy_lab/top1/{experiment_id}/research-receipt.json",
        research_receipt_file_sha256=str(research["terminal_file_sha256"]),
        trading_dates=trading_dates,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key=f"lock-{experiment_id}",
        artifact_root=root,
        environ=AVAILABLE,
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
    assert store.migrate(migrated_at_utc=NOW) == {"status": "ready", "schema_version": 2}
    assert store.migrate(migrated_at_utc=NOW) == {"status": "ready", "schema_version": 2}
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
            "strategy_lab_features",
            "strategy_lab_experiments",
            "strategy_lab_generations",
            "strategy_lab_hidden_commitments",
            "strategy_lab_events",
            "strategy_lab_corpus_days",
            "strategy_lab_corpus_points",
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
    assert ExperimentStore(v0_path).migrate(migrated_at_utc=NOW)["schema_version"] == 2

    v1_path = tmp_path / "v1.sqlite3"
    v1_store = ExperimentStore(v1_path)
    v1_store.migrate(migrated_at_utc=NOW)
    v1_store.set_feature(
        market="HK",
        account="lx",
        enabled=True,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="legacy-feature",
    )
    with sqlite3.connect(v1_path) as connection:
        connection.execute("DROP TABLE strategy_lab_corpus_points")
        connection.execute("DROP TABLE strategy_lab_corpus_days")
        connection.execute("UPDATE strategy_lab_schema SET schema_version = 1")
    assert v1_store.schema_state() == {"status": "migration_required", "schema_version": 1}
    assert v1_store.migrate(migrated_at_utc=NOW) == {
        "status": "ready",
        "schema_version": 2,
    }
    assert v1_store.feature("HK", "lx")["user_opt_in"] == 1

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


def test_default_off_and_maintainer_off_enable_is_no_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    assert effective_feature_status(
        store, market="HK", account="lx", environ=AVAILABLE
    )["effective"] is False
    with pytest.raises(Top1LifecycleError) as exc_info:
        set_account_opt_in(
            store,
            market="HK",
            account="lx",
            enabled=True,
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="enable-off",
            artifact_root=root,
            environ={},
        )
    assert exc_info.value.reason_code == "feature_disabled"
    assert store.feature("HK", "lx") is None


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


def test_separate_authorization_day20_and_hidden_status(tmp_path: Path) -> None:
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
    for index, trading_date in enumerate(dates, start=1):
        kwargs = dict(
            experiment_id="experiment-a",
            point_id=f"point-{index}",
            trading_date=trading_date,
            source_ref=f"points/{index}.json",
            source_file_sha256=SHA_A,
            revision=index,
            manifest_ref=f"hidden/{index}.json",
            manifest_file_sha256=SHA_B,
            frozen_row_sha256=SHA_C,
            actor="runner",
            occurred_at_utc=NOW,
            idempotency_key=f"point-{index}",
            artifact_root=root,
            environ=AVAILABLE,
        )
        if index == 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(commit_validation_point, store, **kwargs),
                    executor.submit(
                        commit_validation_point,
                        store,
                        **{**kwargs, "idempotency_key": "point-1-alias"},
                    ),
                ]
                for future in futures:
                    future.result()
            assert len(
                [
                    event
                    for event in store.events("experiment-a")
                    if event["event_type"] == "validation_point_committed"
                ]
            ) == 1
            with pytest.raises(Top1LifecycleError) as conflict_info:
                commit_validation_point(
                    store,
                    **{
                        **kwargs,
                        "point_id": "different-point",
                        "source_file_sha256": SHA_C,
                        "idempotency_key": "point-1-alias",
                    },
                )
            assert conflict_info.value.reason_code == "experiment_conflict"
        else:
            commit_validation_point(store, **kwargs)
        seal_validation_partition(
            store,
            experiment_id="experiment-a",
            trading_date=trading_date,
            actor="runner",
            occurred_at_utc=NOW,
            idempotency_key=f"partition-{index}",
            artifact_root=root,
            environ=AVAILABLE,
        )
        if index == 1:
            with pytest.raises(Top1LifecycleError) as late_info:
                commit_validation_point(
                    store,
                    experiment_id="experiment-a",
                    point_id="late-day-one",
                    trading_date=trading_date,
                    source_ref="points/late-day-one.json",
                    source_file_sha256=SHA_A,
                    revision=2,
                    manifest_ref="hidden/late-day-one.json",
                    manifest_file_sha256=SHA_B,
                    frozen_row_sha256=SHA_C,
                    actor="runner",
                    occurred_at_utc=NOW,
                    idempotency_key="late-day-one",
                    artifact_root=root,
                    environ=AVAILABLE,
                )
            assert late_info.value.reason_code == "late_write"
        state = store.experiment("experiment-a")
        if index == 19:
            assert state["validation_progress"] == "collecting_decisions"
    state = store.experiment("experiment-a")
    assert state["completed_validation_partitions"] == 20
    assert state["validation_progress"] == "awaiting_outcomes"
    with pytest.raises(Top1LifecycleError) as exc_info:
        commit_validation_point(
            store,
            experiment_id="experiment-a",
            point_id="late-point",
            trading_date=dates[-1],
            source_ref="points/late.json",
            source_file_sha256=SHA_A,
            revision=21,
            manifest_ref="hidden/late.json",
            manifest_file_sha256=SHA_B,
            frozen_row_sha256=SHA_C,
            actor="runner",
            occurred_at_utc=NOW,
            idempotency_key="late-point",
            artifact_root=root,
            environ=AVAILABLE,
        )
    assert exc_info.value.reason_code == "late_write"
    public = json.dumps(read_public_status(store, experiment_id="experiment-a"))
    assert "point-1" not in public
    assert "daily_delta" not in public
    assert read_public_receipt(store, experiment_id="experiment-a") is None


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
        disabled_scope=None,
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
        disabled_scope=None,
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
    research = next(
        row
        for row in store.generations("experiment-overlap")
        if row["generation_kind"] == "research"
    )
    relocked = lock_challenger(
        store,
        _spec("experiment-overlap", validation=True),
        system_leader="level-1",
        challenger_variant_id="level-1",
        research_receipt_ref="strategy_lab/top1/experiment-overlap/research-receipt.json",
        research_receipt_file_sha256=str(research["terminal_file_sha256"]),
        trading_dates=replacement_dates,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="relock-overlap",
        artifact_root=root,
        environ=AVAILABLE,
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


def test_terminal_competition_crash_recovery_and_disable(tmp_path: Path) -> None:
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
            disabled_scope=None,
            actor="human",
            occurred_at_utc=NOW,
            idempotency_key="abort-terminal",
            artifact_root=root,
            publisher=publish_then_crash,
        )
    assert store.experiment("experiment-terminal")["terminal_mode"] == "aborted"
    assert read_public_receipt(store, experiment_id="experiment-terminal") is None
    set_account_opt_in(
        store,
        market="HK",
        account="lx",
        enabled=False,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="disable-after-crash",
        artifact_root=root,
        environ=AVAILABLE,
    )
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
    assert exc_info.value.reason_code in {"feature_disabled", "terminal_conflict"}

    _enable(store, root, idempotency_key="reenable")
    _ready_research(store, root, "experiment-disable")
    set_account_opt_in(
        store,
        market="HK",
        account="lx",
        enabled=False,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="disable",
        artifact_root=root,
        environ=AVAILABLE,
    )
    disabled = read_public_receipt(store, experiment_id="experiment-disable")
    assert disabled is not None
    assert disabled["terminal"]["reason"] == "experimental_feature_disabled"
    assert disabled["terminal"]["disabled_scope"] == "user"
