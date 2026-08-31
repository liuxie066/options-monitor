from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import stat

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.strategy_lab.contracts import (
    EVALUATOR_OWNER_PATHS,
    StrategyLabContractError,
    build_evaluator_behavior_manifest,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)
import src.infrastructure.strategy_lab.experiment_store as store_module


NOW = "2026-08-30T01:00:00Z"


def test_behavior_manifest_owns_provider_admission_and_pagination() -> None:
    assert "src/application/strategy_lab/readiness.py" in EVALUATOR_OWNER_PATHS


def test_behavior_manifest_fails_closed_when_slice2_owner_is_absent(tmp_path: Path) -> None:
    with pytest.raises(StrategyLabContractError) as raised:
        build_evaluator_behavior_manifest(tmp_path)
    assert raised.value.reason_code == "evaluator_owner_unavailable"


def _create(
    store: ExperimentStore, identity: str, key: str, *, actor: str = "tester"
) -> dict[str, object]:
    spec = {
        "recipe_id": "sell_put_option_position_concentration",
        "id": identity,
        "near_return_threshold": 0.002,
        "research_sessions": 20,
    }
    manifest = [{"path": "owner.py", "sha256": "a" * 64}]
    return store.create_experiment(
        experiment_id=identity,
        spec=spec,
        spec_sha256=canonical_sha256(spec),
        source_commit_sha="b" * 40,
        behavior_manifest=manifest,
        evaluator_behavior_sha256=canonical_sha256(manifest),
        confirmation_sha256="c" * 64,
        idempotency_key=key,
        actor=actor,
        occurred_at_utc=NOW,
    )


def test_store_preserves_json_number_types(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    created = _create(store, "experiment-1", "confirm-1")
    stored = store.put_observation(
        "experiment-1",
        observation_key="single_result:1",
        kind="single_result",
        status="available",
        payload={"annualized_return": 0.12, "holding_calendar_days": 20},
        created_at_utc=NOW,
    )

    assert isinstance(created["spec"]["near_return_threshold"], float)
    assert isinstance(created["spec"]["research_sessions"], int)
    assert isinstance(stored["payload"]["annualized_return"], float)
    assert isinstance(stored["payload"]["holding_calendar_days"], int)


def test_store_reads_are_strictly_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    store.path.chmod(0o400)
    before = store.path.stat()
    monkeypatch.setattr(
        store_module,
        "connect_private_sqlite",
        lambda *_args, **_kwargs: pytest.fail("read path must not use the write connector"),
    )
    monkeypatch.setattr(
        store_module,
        "secure_sqlite_artifacts",
        lambda *_args, **_kwargs: pytest.fail("read path must not repair permissions"),
    )

    assert store.get_experiment("experiment-1")["experiment_id"] == "experiment-1"
    assert store.get_active_experiment()["experiment_id"] == "experiment-1"
    assert store.list_events("experiment-1")[0]["event_type"] == "research_confirmed"
    assert store.list_observations("experiment-1") == []
    after = store.path.stat()

    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o400
    assert after.st_mtime_ns == before.st_mtime_ns


def test_store_read_rejects_insecure_permissions_without_repair(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    store.path.chmod(0o644)

    with pytest.raises(ExperimentStoreError) as raised:
        store.get_experiment("experiment-1")

    assert raised.value.reason_code == "experiment_store_incompatible"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o644


def test_store_initializes_only_exact_three_table_schema(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    assert store.initialize() == {"status": "ready"}
    assert store.initialize() == {"status": "ready"}

    incompatible = ExperimentStore(tmp_path / "old.sqlite3")
    import sqlite3

    connection = sqlite3.connect(incompatible.path)
    connection.execute("CREATE TABLE legacy(id INTEGER)")
    connection.close()
    with pytest.raises(ExperimentStoreError) as raised:
        incompatible.initialize()
    assert raised.value.reason_code == "experiment_store_incompatible"


def test_store_concurrent_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "experiments.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: ExperimentStore(path).initialize(), range(2)))
    assert results == [{"status": "ready"}, {"status": "ready"}]


def test_store_initialization_rolls_back_partial_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "experiments.sqlite3"
    monkeypatch.setattr(
        store_module,
        "_SCHEMA_STATEMENTS",
        (store_module._SCHEMA_STATEMENTS[0], "CREATE TABLE broken("),
    )
    with pytest.raises(ExperimentStoreError):
        ExperimentStore(path).initialize()

    import sqlite3

    connection = sqlite3.connect(path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    assert tables == []


@pytest.mark.parametrize(
    "drift_sql",
    [
        "ALTER TABLE experiments ADD COLUMN unexpected TEXT",
        "CREATE INDEX unexpected_index ON experiments(state)",
    ],
)
def test_store_rejects_schema_column_or_index_drift(tmp_path: Path, drift_sql: str) -> None:
    import sqlite3

    path = tmp_path / "experiments.sqlite3"
    ExperimentStore(path).initialize()
    connection = sqlite3.connect(path)
    connection.execute(drift_sql)
    connection.commit()
    connection.close()
    with pytest.raises(ExperimentStoreError) as raised:
        ExperimentStore(path).initialize()
    assert raised.value.reason_code == "experiment_store_incompatible"


@pytest.mark.parametrize("drift_kind", ["constraint", "foreign_key"])
def test_store_rejects_constraint_or_foreign_key_drift(
    tmp_path: Path, drift_kind: str
) -> None:
    import sqlite3

    statements = list(store_module._SCHEMA_STATEMENTS)
    if drift_kind == "constraint":
        statements[1] = statements[1].replace("sequence > 0", "sequence >= 0")
    else:
        statements[2] = statements[2].replace(
            ",\n    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE",
            "",
        )
    path = tmp_path / f"{drift_kind}.sqlite3"
    connection = sqlite3.connect(path)
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()
    with pytest.raises(ExperimentStoreError) as raised:
        ExperimentStore(path).initialize()
    assert raised.value.reason_code == "experiment_store_incompatible"


def test_store_enforces_global_single_active_and_idempotency(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    first = _create(store, "experiment-1", "confirm-1")
    assert first["state"] == "research_running"
    assert _create(store, "experiment-1", "confirm-1") == first

    with pytest.raises(ExperimentStoreError) as raised:
        _create(store, "experiment-2", "confirm-2")
    assert raised.value.reason_code == "active_experiment_exists"


def test_create_idempotency_binds_actor(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    first = _create(store, "experiment-1", "confirm-1", actor="alice")
    assert _create(store, "experiment-1", "confirm-1", actor="alice") == first
    with pytest.raises(ExperimentStoreError) as raised:
        _create(store, "experiment-1", "confirm-1", actor="bob")
    assert raised.value.reason_code == "idempotency_conflict"


def test_store_concurrent_create_has_one_active_experiment(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: _capture_create(store, *pair),
                [("experiment-1", "confirm-1"), ("experiment-2", "confirm-2")],
            )
        )
    assert sorted(item[0] for item in outcomes) == ["active_experiment_exists", "ok"]
    assert store.get_active_experiment() is not None


def _capture_create(store: ExperimentStore, identity: str, key: str) -> tuple[str, object]:
    try:
        return "ok", _create(store, identity, key)
    except ExperimentStoreError as exc:
        return exc.reason_code, str(exc)


def test_observation_is_write_once_and_transition_uses_revision(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    observation = store.put_observation(
        "experiment-1",
        observation_key="history-k:1",
        kind="history_k_query",
        status="available",
        payload={"rows": 1},
        artifact_ref="experiments/experiment-1/history-k.json",
        artifact_sha256="d" * 64,
        created_at_utc=NOW,
    )
    assert store.put_observation(
        "experiment-1",
        observation_key="history-k:1",
        kind="history_k_query",
        status="available",
        payload={"rows": 1},
        artifact_ref="experiments/experiment-1/history-k.json",
        artifact_sha256="d" * 64,
        created_at_utc=NOW,
    ) == observation
    with pytest.raises(ExperimentStoreError) as raised:
        store.put_observation(
            "experiment-1",
            observation_key="history-k:1",
            kind="history_k_query",
            status="available",
            payload={"rows": 2},
            artifact_ref="experiments/experiment-1/history-k.json",
            artifact_sha256="d" * 64,
            created_at_utc=NOW,
        )
    assert raised.value.reason_code == "observation_immutable_conflict"

    transitioned = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    assert transitioned["revision"] == 1
    with pytest.raises(ExperimentStoreError) as raised:
        store.append_event_and_transition(
            "experiment-1",
            expected_state="research_running",
            expected_revision=0,
            new_state="research_complete",
            event_type="research_materialized",
            actor="tester",
            payload={},
            occurred_at_utc=NOW,
        )
    assert raised.value.reason_code == "experiment_revision_conflict"


def test_transition_map_and_observation_freeze_are_closed(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    with pytest.raises(ExperimentStoreError) as raised:
        store.append_event_and_transition(
            "experiment-1",
            expected_state="research_running",
            expected_revision=0,
            new_state="completed",
            event_type="skip",
            actor="tester",
            payload={},
            occurred_at_utc=NOW,
        )
    assert raised.value.reason_code == "experiment_transition_invalid"

    store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    with pytest.raises(ExperimentStoreError) as raised:
        store.put_observation(
            "experiment-1",
            observation_key="late",
            kind="single_result",
            status="available",
            payload={},
            created_at_utc=NOW,
        )
    assert raised.value.reason_code == "observation_write_closed"


def test_generic_transition_reserves_validation_lifecycle_for_dedicated_apis(
    tmp_path: Path,
) -> None:
    store = ExperimentStore(tmp_path / "reserved-validation.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    completed = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    awaiting = store.attach_research_receipt_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=completed["revision"],
        new_state="awaiting_validation_confirmation",
        receipt_ref="experiments/experiment-1/receipts/research.json",
        receipt_sha256="e" * 64,
        leader={"variant_id": "challenger_0.002"},
        actor="tester",
        occurred_at_utc=NOW,
        payload={"status": "leader"},
        idempotency_key="conclude-1",
    )

    def assert_generic_rejected(old_state: str, new_state: str, event_type: str) -> None:
        before = store.get_experiment("experiment-1")
        events_before = store.list_events("experiment-1")
        assert before is not None
        with pytest.raises(ExperimentStoreError) as raised:
            store.append_event_and_transition(
                "experiment-1",
                expected_state=old_state,
                expected_revision=before["revision"],
                new_state=new_state,
                event_type=event_type,
                actor="tester",
                payload={},
                occurred_at_utc=NOW,
            )
        assert raised.value.reason_code == "experiment_transition_invalid"
        assert store.get_experiment("experiment-1") == before
        assert store.list_events("experiment-1") == events_before

    assert_generic_rejected(
        "awaiting_validation_confirmation",
        "validation_collecting",
        "validation_confirmed",
    )

    plan = {"experiment_id": "experiment-1", "selected_trading_dates": ["2026-09-01"]}
    command = {
        "expected_revision": awaiting["revision"],
        "validation_plan": plan,
        "validation_plan_sha256": canonical_sha256(plan),
        "preview_sha256": "f" * 64,
        "actor": "tester",
        "idempotency_key": "validation-confirm-1",
        "occurred_at_utc": "2026-08-30T02:00:00Z",
    }
    confirmed = store.confirm_validation("experiment-1", **command)
    assert store.confirm_validation(
        "experiment-1",
        **{**command, "occurred_at_utc": "2026-08-30T02:00:01Z"},
    ) == confirmed
    assert confirmed["state"] == "validation_collecting"
    assert confirmed["validation_plan"] == plan

    assert_generic_rejected(
        "validation_collecting", "waiting_outcome", "validation_materialized"
    )

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE experiments SET state = 'waiting_outcome' WHERE experiment_id = ?",
            ("experiment-1",),
        )
    assert_generic_rejected("waiting_outcome", "completed", "validation_concluded")


def test_observation_artifact_pair_is_strict_in_api_and_sql(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "observation-pairs.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")

    assert store.put_observation(
        "experiment-1",
        observation_key="api-null",
        kind="history_k_query",
        status="available",
        payload={},
        created_at_utc=NOW,
    )["artifact_ref"] is None
    assert store.put_observation(
        "experiment-1",
        observation_key="api-full",
        kind="history_k_query",
        status="available",
        payload={},
        artifact_ref="experiments/experiment-1/full.json",
        artifact_sha256="d" * 64,
        created_at_utc=NOW,
    )["artifact_sha256"] == "d" * 64
    for key, ref, digest in (
        ("api-ref-only", "experiments/experiment-1/ref-only.json", None),
        ("api-hash-only", None, "e" * 64),
    ):
        with pytest.raises(ExperimentStoreError) as raised:
            store.put_observation(
                "experiment-1",
                observation_key=key,
                kind="history_k_query",
                status="available",
                payload={},
                artifact_ref=ref,
                artifact_sha256=digest,
                created_at_utc=NOW,
            )
        assert raised.value.reason_code == "experiment_input_invalid"

    statement = (
        "INSERT INTO experiment_observations("
        "experiment_id, observation_key, kind, status, payload_json, "
        "artifact_ref, artifact_sha256, created_at_utc, updated_at_utc"
        ") VALUES (?, ?, 'history_k_query', 'available', '{}', ?, ?, ?, ?)"
    )
    with sqlite3.connect(store.path, isolation_level=None) as connection:
        connection.execute(
            statement, ("experiment-1", "raw-null", None, None, NOW, NOW)
        )
        connection.execute(
            statement,
            (
                "experiment-1",
                "raw-full",
                "experiments/experiment-1/raw-full.json",
                "f" * 64,
                NOW,
                NOW,
            ),
        )
        for key, ref, digest in (
            ("raw-ref-only", "experiments/experiment-1/raw-ref-only.json", None),
            ("raw-hash-only", None, "a" * 64),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    statement, ("experiment-1", key, ref, digest, NOW, NOW)
                )
        stored = {
            row[0]
            for row in connection.execute(
                "SELECT observation_key FROM experiment_observations "
                "WHERE observation_key LIKE 'raw-%'"
            )
        }
    assert stored == {"raw-null", "raw-full"}


def test_transition_and_observation_race_never_writes_after_completion(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")

    def transition() -> str:
        store.append_event_and_transition(
            "experiment-1",
            expected_state="research_running",
            expected_revision=0,
            new_state="research_complete",
            event_type="research_materialized",
            actor="tester",
            payload={},
            occurred_at_utc=NOW,
        )
        return "transitioned"

    def observe() -> str:
        try:
            store.put_observation(
                "experiment-1",
                observation_key="racing",
                kind="single_result",
                status="available",
                payload={},
                created_at_utc=NOW,
            )
            return "observed"
        except ExperimentStoreError as exc:
            return exc.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(transition), pool.submit(observe)]
        results = [future.result() for future in outcomes]
    assert results[0] == "transitioned"
    assert results[1] in {"observed", "observation_write_closed"}
    assert store.get_experiment("experiment-1")["state"] == "research_complete"
    with pytest.raises(ExperimentStoreError) as raised:
        store.put_observation(
            "experiment-1",
            observation_key="after",
            kind="single_result",
            status="available",
            payload={},
            created_at_utc=NOW,
        )
    assert raised.value.reason_code == "observation_write_closed"


def test_transition_idempotency_binds_complete_command(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    command = dict(
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={"count": 1},
        occurred_at_utc=NOW,
        idempotency_key="research_materialized:experiment-1",
    )
    first = store.append_event_and_transition("experiment-1", **command)
    assert store.append_event_and_transition(
        "experiment-1",
        **{
            **command,
            "actor": "other",
            "expected_revision": 99,
            "occurred_at_utc": "2026-08-30T01:00:01Z",
        },
    ) == first
    mutations = [
        {"new_state": "completed"},
        {"event_type": "changed"},
        {"payload": {"count": 2}},
        {"confirmation_sha256": "e" * 64},
    ]
    for mutation in mutations:
        with pytest.raises(ExperimentStoreError) as raised:
            store.append_event_and_transition("experiment-1", **{**command, **mutation})
        assert raised.value.reason_code == "idempotency_conflict"


def test_transition_rejects_reused_confirmation_without_partial_write(
    tmp_path: Path,
) -> None:
    import sqlite3

    path = tmp_path / "experiments.sqlite3"
    store = ExperimentStore(path)
    store.initialize()
    before = _create(store, "experiment-1", "confirm-1")
    with pytest.raises(ExperimentStoreError) as raised:
        store.append_event_and_transition(
            "experiment-1",
            expected_state="research_running",
            expected_revision=0,
            new_state="research_complete",
            event_type="research_materialized",
            actor="tester",
            payload={},
            occurred_at_utc=NOW,
            confirmation_sha256="c" * 64,
            idempotency_key="advance-1",
        )
    assert raised.value.reason_code == "confirmation_conflict"
    assert store.get_experiment("experiment-1") == before
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM experiment_events").fetchone()[0] == 1
    connection.close()


@pytest.mark.parametrize(
    ("new_state", "leader"),
    [
        ("awaiting_validation_confirmation", None),
        ("completed", {"arm_id": "threshold-0.002"}),
    ],
)
def test_research_receipt_requires_leader_to_match_conclusion_state(
    tmp_path: Path, new_state: str, leader: object | None
) -> None:
    import sqlite3

    path = tmp_path / f"{new_state}.sqlite3"
    store = ExperimentStore(path)
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    before = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    with pytest.raises(ExperimentStoreError) as raised:
        store.attach_research_receipt_and_transition(
            "experiment-1",
            expected_state="research_complete",
            expected_revision=1,
            new_state=new_state,
            receipt_ref="experiments/experiment-1/receipts/research.json",
            receipt_sha256="e" * 64,
            leader=leader,
            actor="tester",
            occurred_at_utc=NOW,
            payload={},
            idempotency_key="conclude-1",
        )
    assert raised.value.reason_code == "receipt_leader_state_conflict"
    assert store.get_experiment("experiment-1") == before
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM experiment_events").fetchone()[0] == 2
    connection.close()


def test_research_receipt_binding_is_atomic_and_immutable(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    completed = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    receipt_command = dict(
        expected_state="research_complete",
        expected_revision=completed["revision"],
        new_state="awaiting_validation_confirmation",
        receipt_ref="experiments/experiment-1/receipts/research.json",
        receipt_sha256="e" * 64,
        leader={"arm_id": "threshold-0.002"},
        actor="tester",
        occurred_at_utc=NOW,
        payload={"status": "leader"},
        idempotency_key="conclude-1",
    )
    attached = store.attach_research_receipt_and_transition(
        "experiment-1", **receipt_command
    )
    assert attached["revision"] == 2
    assert attached["research_receipt_sha256"] == "e" * 64
    assert store.attach_research_receipt_and_transition(
        "experiment-1",
        **{**receipt_command, "occurred_at_utc": "2026-08-30T01:00:01Z"},
    ) == attached
    assert store.attach_research_receipt_and_transition(
        "experiment-1",
        **{**receipt_command, "idempotency_key": "conclude-recovered"},
    ) == attached
    assert store.attach_research_receipt_and_transition(
        "experiment-1", **{**receipt_command, "actor": "other"}
    ) == attached
    for mutation in (
        {"payload": {"status": "changed"}},
        {"expected_revision": 99},
        {"new_state": "completed"},
        {"leader": {"arm_id": "threshold-0.004"}},
    ):
        with pytest.raises(ExperimentStoreError) as raised:
            store.attach_research_receipt_and_transition(
                "experiment-1", **{**receipt_command, **mutation}
            )
        assert raised.value.reason_code == "idempotency_conflict"
    with pytest.raises(ExperimentStoreError) as raised:
        store.attach_research_receipt_and_transition(
            "experiment-1",
            expected_state="research_complete",
            expected_revision=1,
            new_state="awaiting_validation_confirmation",
            receipt_ref="experiments/experiment-1/receipts/research.json",
            receipt_sha256="f" * 64,
            leader={"arm_id": "threshold-0.002"},
            actor="tester",
            occurred_at_utc=NOW,
            payload={"status": "leader"},
            idempotency_key="conclude-2",
        )
    assert raised.value.reason_code == "receipt_immutable_conflict"

    with pytest.raises(ExperimentStoreError) as raised:
        store.append_event_and_transition(
            "experiment-1",
            expected_state="awaiting_validation_confirmation",
            expected_revision=2,
            new_state="research_running",
            event_type="reopen",
            actor="tester",
            payload={},
            occurred_at_utc=NOW,
        )
    assert raised.value.reason_code == "experiment_transition_invalid"


def test_source_commit_observation_is_same_state_idempotent_event(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "source.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    command = dict(
        expected_state="research_running",
        expected_revision=0,
        new_state="research_running",
        event_type="source_commit_observed",
        actor="tester",
        payload={"source_commit_sha": "f" * 40},
        occurred_at_utc=NOW,
        idempotency_key=f"source_commit_observed:experiment-1:{'f' * 40}",
    )
    observed = store.append_event_and_transition("experiment-1", **command)
    assert observed["state"] == "research_running"
    assert observed["revision"] == 1
    assert store.append_event_and_transition("experiment-1", **command) == observed
    assert store.append_event_and_transition(
        "experiment-1", **{**command, "expected_revision": observed["revision"]}
    ) == observed
    assert store.append_event_and_transition(
        "experiment-1",
        **{**command, "expected_revision": observed["revision"], "actor": "other"},
    ) == observed
    with pytest.raises(ExperimentStoreError) as raised:
        store.append_event_and_transition(
            "experiment-1",
            **{**command, "payload": {"source_commit_sha": "e" * 40}},
        )
    assert raised.value.reason_code == "idempotency_conflict"

    completed = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=1,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    audited = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=completed["revision"],
        new_state="research_complete",
        event_type="source_commit_observed",
        actor="tester",
        payload={"source_commit_sha": "d" * 40},
        occurred_at_utc=NOW,
        idempotency_key=f"source_commit_observed:experiment-1:{'d' * 40}",
    )
    assert audited["state"] == "research_complete"


def test_validation_confirmation_is_atomic_idempotent_and_records_actual_time(
    tmp_path: Path,
) -> None:
    store = ExperimentStore(tmp_path / "validation.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    completed = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    awaiting = store.attach_research_receipt_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=completed["revision"],
        new_state="awaiting_validation_confirmation",
        receipt_ref="experiments/experiment-1/receipts/research.json",
        receipt_sha256="e" * 64,
        leader={"variant_id": "challenger_0.002"},
        actor="tester",
        occurred_at_utc=NOW,
        payload={"status": "leader"},
        idempotency_key="conclude-1",
    )
    plan = {"experiment_id": "experiment-1", "selected_trading_dates": ["2026-09-01"]}
    command = {
        "expected_revision": awaiting["revision"],
        "validation_plan": plan,
        "validation_plan_sha256": canonical_sha256(plan),
        "preview_sha256": "f" * 64,
        "actor": "tester",
        "idempotency_key": "validation-confirm-1",
        "occurred_at_utc": "2026-08-30T02:00:00Z",
    }

    confirmed = store.confirm_validation("experiment-1", **command)
    retried = store.confirm_validation(
        "experiment-1",
        **{**command, "occurred_at_utc": "2026-08-30T02:00:01Z"},
    )

    assert retried == confirmed
    assert confirmed["state"] == "validation_collecting"
    assert confirmed["validation_plan"] == plan
    assert confirmed["validation_plan_sha256"] == canonical_sha256(plan)
    event = store.list_events("experiment-1")[-1]
    assert event["event_type"] == "validation_confirmed"
    assert event["confirmation_sha256"] == "f" * 64
    assert event["occurred_at_utc"] == "2026-08-30T02:00:00Z"
    assert "occurred_at_utc" not in event["payload"]

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE experiments SET validation_plan_sha256 = NULL "
                "WHERE experiment_id = 'experiment-1'"
            )

    with pytest.raises(ExperimentStoreError) as raised:
        store.confirm_validation(
            "experiment-1",
            **{
                **command,
                "validation_plan": {**plan, "changed": True},
                "validation_plan_sha256": canonical_sha256({**plan, "changed": True}),
            },
        )
    assert raised.value.reason_code == "idempotency_conflict"


def test_completed_is_absorbing_even_when_another_experiment_is_active(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "completed.sqlite3")
    store.initialize()
    _create(store, "experiment-1", "confirm-1")
    completed = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    store.attach_research_receipt_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=completed["revision"],
        new_state="completed",
        receipt_ref="experiments/experiment-1/receipts/research.json",
        receipt_sha256="e" * 64,
        leader=None,
        actor="tester",
        occurred_at_utc=NOW,
        payload={"status": "no_leader"},
        idempotency_key="conclude-1",
    )
    _create(store, "experiment-2", "confirm-2")
    with pytest.raises(ExperimentStoreError) as raised:
        store.append_event_and_transition(
            "experiment-1",
            expected_state="completed",
            expected_revision=2,
            new_state="research_running",
            event_type="reopen",
            actor="tester",
            payload={},
            occurred_at_utc=NOW,
        )
    assert raised.value.reason_code == "experiment_transition_invalid"
