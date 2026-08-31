from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import utc_timestamp
from src.application.strategy_lab.contracts import (
    EXPERIMENT_TRANSITIONS,
    EXPERIMENT_STATES,
    OBSERVATION_KINDS,
    OBSERVATION_STATUSES,
    TERMINAL_STATES,
    strict_json_bytes,
)
from src.infrastructure.private_storage import (
    connect_private_sqlite,
    private_path,
    secure_sqlite_artifacts,
)


_SCHEMA_STATEMENTS = (
    """CREATE TABLE experiments (
    experiment_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN (
        'research_running', 'research_complete',
        'awaiting_validation_confirmation', 'validation_collecting',
        'waiting_outcome', 'completed'
    )),
    spec_json TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL CHECK (length(spec_sha256) = 64),
    source_commit_sha TEXT NOT NULL CHECK (length(source_commit_sha) = 40),
    behavior_manifest_json TEXT NOT NULL,
    evaluator_behavior_sha256 TEXT NOT NULL CHECK (length(evaluator_behavior_sha256) = 64),
    leader_json TEXT,
    research_receipt_ref TEXT,
    research_receipt_sha256 TEXT,
    validation_plan_json TEXT,
    validation_plan_sha256 TEXT,
    final_receipt_ref TEXT,
    final_receipt_sha256 TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK (
        (research_receipt_ref IS NULL AND research_receipt_sha256 IS NULL)
        OR (
            research_receipt_ref IS NOT NULL
            AND research_receipt_sha256 IS NOT NULL
            AND length(research_receipt_sha256) = 64
        )
    ),
    CHECK (
        (validation_plan_json IS NULL AND validation_plan_sha256 IS NULL)
        OR (
            validation_plan_json IS NOT NULL
            AND validation_plan_sha256 IS NOT NULL
            AND length(validation_plan_sha256) = 64
        )
    ),
    CHECK (
        (final_receipt_ref IS NULL AND final_receipt_sha256 IS NULL)
        OR (
            final_receipt_ref IS NOT NULL
            AND final_receipt_sha256 IS NOT NULL
            AND length(final_receipt_sha256) = 64
        )
    )
)""",
    """CREATE TABLE experiment_events (
    experiment_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    confirmation_sha256 TEXT,
    idempotency_key TEXT,
    payload_json TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    PRIMARY KEY (experiment_id, sequence),
    UNIQUE (idempotency_key),
    UNIQUE (experiment_id, confirmation_sha256),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE
)""",
    """CREATE TABLE experiment_observations (
    experiment_id TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    recommendation_point_id TEXT,
    arm_id TEXT,
    observation_slot_utc TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifact_ref TEXT,
    artifact_sha256 TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (experiment_id, observation_key),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    CHECK (
        (artifact_ref IS NULL AND artifact_sha256 IS NULL)
        OR (
            artifact_ref IS NOT NULL
            AND artifact_sha256 IS NOT NULL
            AND length(artifact_sha256) = 64
        )
    )
)""",
)
_SCHEMA = ";\n".join(_SCHEMA_STATEMENTS) + ";\n"
_TABLES = {"experiments", "experiment_events", "experiment_observations"}
_HEX = frozenset("0123456789abcdef")


class ExperimentStoreError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise ExperimentStoreError(reason_code, message)


def compact_json(payload: object) -> str:
    return strict_json_bytes(payload).decode("utf-8")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("experiment_input_invalid", f"{label} must be canonical text")
    return value


def _sha(value: object, label: str, *, length: int = 64) -> str:
    text = _text(value, label)
    if len(text) != length or set(text) - _HEX:
        _fail("experiment_input_invalid", f"{label} is invalid")
    return text


def _timestamp(value: object, label: str) -> str:
    try:
        result = utc_timestamp(value, label)
    except ValueError as exc:
        raise ExperimentStoreError("experiment_input_invalid", str(exc)) from exc
    if result != value:
        _fail("experiment_input_invalid", f"{label} must be canonical UTC")
    return result


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _experiment(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["spec"] = _decode(item.pop("spec_json"))
    item["behavior_manifest"] = _decode(item.pop("behavior_manifest_json"))
    item["leader"] = _decode(item.pop("leader_json"))
    item["validation_plan"] = _decode(item.pop("validation_plan_json"))
    return item


def _observation(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = _decode(item.pop("payload_json"))
    return item


def _event(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = _decode(item.pop("payload_json"))
    return item


def _schema_signature(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [
        (str(row[0]), str(row[1]), str(row[2]), " ".join(str(row[3] or "").split()))
        for row in rows
    ]


def _expected_schema_signature() -> list[tuple[str, str, str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _schema_signature(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_SIGNATURE = _expected_schema_signature()


class ExperimentStore:
    """Concrete three-table write authority for Strategy Lab."""

    def __init__(self, path: str | Path) -> None:
        self.path = private_path(path)

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect_private_sqlite(self.path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()
            secure_sqlite_artifacts(self.path)

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        descriptor = -1
        connection: sqlite3.Connection | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                _fail(
                    "experiment_store_incompatible",
                    "experiment Store must be a private regular file",
                )
            os.close(descriptor)
            descriptor = -1
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        except ExperimentStoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ExperimentStoreError(
                "experiment_store_incompatible", "experiment Store cannot be read"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if _schema_signature(connection) != _EXPECTED_SCHEMA_SIGNATURE:
            _fail("experiment_store_incompatible", "experiment Store schema is incompatible")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            _fail("experiment_store_incompatible", "experiment Store foreign keys are invalid")

    def initialize(self) -> dict[str, object]:
        try:
            with self._write_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    if not tables:
                        for statement in _SCHEMA_STATEMENTS:
                            connection.execute(statement)
                    elif tables != _TABLES:
                        _fail(
                            "experiment_store_incompatible",
                            "experiment Store must be new or contain exactly three tables",
                        )
                    self._require_schema(connection)
                    if connection.in_transaction:
                        connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
        except ExperimentStoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ExperimentStoreError(
                "experiment_store_incompatible", "experiment Store cannot be initialized"
            ) from exc
        return {"status": "ready"}

    def create_experiment(
        self,
        *,
        experiment_id: object,
        spec: object,
        spec_sha256: object,
        source_commit_sha: object,
        behavior_manifest: object,
        evaluator_behavior_sha256: object,
        confirmation_sha256: object,
        idempotency_key: object,
        actor: object,
        occurred_at_utc: object,
    ) -> dict[str, Any]:
        experiment = _text(experiment_id, "experiment_id")
        spec_hash = _sha(spec_sha256, "spec_sha256")
        if canonical_sha256(spec) != spec_hash:
            _fail("experiment_input_invalid", "spec_sha256 does not match spec")
        source_hash = _sha(source_commit_sha, "source_commit_sha", length=40)
        behavior_hash = _sha(evaluator_behavior_sha256, "evaluator_behavior_sha256")
        if canonical_sha256(behavior_manifest) != behavior_hash:
            _fail("experiment_input_invalid", "behavior hash does not match manifest")
        confirmation = _sha(confirmation_sha256, "confirmation_sha256")
        key = _text(idempotency_key, "idempotency_key")
        actor_text = _text(actor, "actor")
        occurred = _timestamp(occurred_at_utc, "occurred_at_utc")
        event_payload = {
            "experiment_id": experiment,
            "spec_sha256": spec_hash,
            "source_commit_sha": source_hash,
            "evaluator_behavior_sha256": behavior_hash,
            "confirmation_sha256": confirmation,
            "actor": actor_text,
        }
        payload_json = compact_json(event_payload)
        try:
            with self._write_connection() as connection:
                self._require_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                prior = connection.execute(
                    "SELECT experiment_id, payload_json FROM experiment_events "
                    "WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if prior is not None:
                    if prior["experiment_id"] != experiment or prior["payload_json"] != payload_json:
                        _fail("idempotency_conflict", "idempotency key changed")
                    row = connection.execute(
                        "SELECT * FROM experiments WHERE experiment_id = ?", (experiment,)
                    ).fetchone()
                    connection.commit()
                    result = _experiment(row)
                    assert result is not None
                    return result
                if connection.execute(
                    "SELECT 1 FROM experiments WHERE state <> 'completed' LIMIT 1"
                ).fetchone() is not None:
                    _fail(
                        "active_experiment_exists",
                        "a non-terminal Strategy Lab experiment already exists",
                    )
                connection.execute(
                    "INSERT INTO experiments("
                    "experiment_id, state, spec_json, spec_sha256, source_commit_sha, "
                    "behavior_manifest_json, evaluator_behavior_sha256, revision, "
                    "created_at_utc, updated_at_utc"
                    ") VALUES (?, 'research_running', ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        experiment,
                        compact_json(spec),
                        spec_hash,
                        source_hash,
                        compact_json(behavior_manifest),
                        behavior_hash,
                        occurred,
                        occurred,
                    ),
                )
                connection.execute(
                    "INSERT INTO experiment_events("
                    "experiment_id, sequence, event_type, actor, confirmation_sha256, "
                    "idempotency_key, payload_json, occurred_at_utc"
                    ") VALUES (?, 1, 'research_confirmed', ?, ?, ?, ?, ?)",
                    (experiment, actor_text, confirmation, key, payload_json, occurred),
                )
                connection.commit()
        except ExperimentStoreError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ExperimentStoreError(
                "experiment_store_conflict", "experiment creation conflicts with stored state"
            ) from exc
        result = self.get_experiment(experiment)
        assert result is not None
        return result

    def get_active_experiment(self) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM experiments WHERE state <> 'completed' "
                "ORDER BY created_at_utc, experiment_id LIMIT 2"
            ).fetchall()
        if len(rows) > 1:
            _fail("experiment_store_incompatible", "multiple active experiments exist")
        return _experiment(rows[0]) if rows else None

    def get_experiment(self, experiment_id: object) -> dict[str, Any] | None:
        identity = _text(experiment_id, "experiment_id")
        with self._read_connection() as connection:
            self._require_schema(connection)
            row = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (identity,)
            ).fetchone()
        return _experiment(row)

    def list_events(self, experiment_id: object) -> list[dict[str, Any]]:
        identity = _text(experiment_id, "experiment_id")
        with self._read_connection() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM experiment_events WHERE experiment_id = ? ORDER BY sequence",
                (identity,),
            ).fetchall()
        return [_event(row) for row in rows]

    def append_event_and_transition(
        self,
        experiment_id: object,
        *,
        expected_state: object,
        expected_revision: object,
        new_state: object,
        event_type: object,
        actor: object,
        payload: object,
        occurred_at_utc: object,
        confirmation_sha256: object | None = None,
        idempotency_key: object | None = None,
    ) -> dict[str, Any]:
        identity = _text(experiment_id, "experiment_id")
        old_state = _text(expected_state, "expected_state")
        state = _text(new_state, "new_state")
        if old_state not in EXPERIMENT_STATES or state not in EXPERIMENT_STATES:
            _fail("experiment_input_invalid", "experiment state is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            _fail("experiment_input_invalid", "expected_revision is invalid")
        event = _text(event_type, "event_type")
        actor_text = _text(actor, "actor")
        occurred = _timestamp(occurred_at_utc, "occurred_at_utc")
        confirmation = (
            None if confirmation_sha256 is None else _sha(confirmation_sha256, "confirmation_sha256")
        )
        key = None if idempotency_key is None else _text(idempotency_key, "idempotency_key")
        command_payload = {
            "expected_state": old_state,
            "expected_revision": expected_revision,
            "new_state": state,
            "event_type": event,
            "actor": actor_text,
            "confirmation_sha256": confirmation,
            "payload": payload,
        }
        payload_json = compact_json(command_payload)
        with self._write_connection() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            if key is not None:
                prior = connection.execute(
                    "SELECT experiment_id, event_type, actor, confirmation_sha256, payload_json "
                    "FROM experiment_events "
                    "WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if prior is not None:
                    same_payload = prior["payload_json"] == payload_json
                    retry_ignores_actor_revision = event in {
                        "source_commit_observed",
                        "research_materialized",
                    }
                    if retry_ignores_actor_revision:
                        prior_payload = json.loads(prior["payload_json"])
                        prior_payload.pop("expected_revision", None)
                        prior_payload.pop("actor", None)
                        retry_payload = json.loads(payload_json)
                        retry_payload.pop("expected_revision", None)
                        retry_payload.pop("actor", None)
                        same_payload = prior_payload == retry_payload
                    if (
                        prior["experiment_id"] != identity
                        or prior["event_type"] != event
                        or (not retry_ignores_actor_revision and prior["actor"] != actor_text)
                        or prior["confirmation_sha256"] != confirmation
                        or not same_payload
                    ):
                        _fail("idempotency_conflict", "idempotency key changed")
                    connection.commit()
                    result = _experiment(
                        connection.execute(
                            "SELECT * FROM experiments WHERE experiment_id = ?", (identity,)
                        ).fetchone()
                    )
                    assert result is not None
                    return result
            allowed_transition = (
                (old_state, state) in EXPERIMENT_TRANSITIONS
                and (old_state, state, event)
                == ("research_running", "research_complete", "research_materialized")
            )
            allowed_source_observation = (
                old_state == state
                and state not in TERMINAL_STATES
                and event == "source_commit_observed"
            )
            if not (allowed_transition or allowed_source_observation):
                _fail("experiment_transition_invalid", "experiment transition is invalid")
            if allowed_source_observation:
                if not isinstance(payload, dict) or set(payload) != {"source_commit_sha"}:
                    _fail("experiment_input_invalid", "source commit observation is invalid")
                source_commit = _sha(
                    payload["source_commit_sha"], "source_commit_sha", length=40
                )
                if key != f"source_commit_observed:{identity}:{source_commit}":
                    _fail(
                        "experiment_input_invalid",
                        "source commit observation idempotency key is invalid",
                    )
            current = connection.execute(
                "SELECT state, revision FROM experiments WHERE experiment_id = ?", (identity,)
            ).fetchone()
            if current is None:
                _fail("experiment_not_found", "experiment does not exist")
            if current["state"] != old_state or current["revision"] != expected_revision:
                _fail("experiment_revision_conflict", "experiment state or revision changed")
            if confirmation is not None and connection.execute(
                "SELECT 1 FROM experiment_events "
                "WHERE experiment_id = ? AND confirmation_sha256 = ? LIMIT 1",
                (identity, confirmation),
            ).fetchone() is not None:
                _fail("confirmation_conflict", "confirmation was already used")
            if state not in TERMINAL_STATES and connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id <> ? AND state <> 'completed' LIMIT 1",
                (identity,),
            ).fetchone() is not None:
                _fail("active_experiment_exists", "a non-terminal Strategy Lab experiment already exists")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM experiment_events "
                    "WHERE experiment_id = ?",
                    (identity,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE experiments SET state = ?, revision = revision + 1, "
                "updated_at_utc = ? WHERE experiment_id = ?",
                (state, occurred, identity),
            )
            connection.execute(
                "INSERT INTO experiment_events("
                "experiment_id, sequence, event_type, actor, confirmation_sha256, "
                "idempotency_key, payload_json, occurred_at_utc"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, sequence, event, actor_text, confirmation, key, payload_json, occurred),
            )
            connection.commit()
        result = self.get_experiment(identity)
        assert result is not None
        return result

    def confirm_validation(
        self,
        experiment_id: object,
        *,
        expected_revision: object,
        validation_plan: object,
        validation_plan_sha256: object,
        preview_sha256: object,
        actor: object,
        idempotency_key: object,
        occurred_at_utc: object,
    ) -> dict[str, Any]:
        identity = _text(experiment_id, "experiment_id")
        if type(expected_revision) is not int or expected_revision < 0:
            _fail("experiment_input_invalid", "expected_revision is invalid")
        plan_hash = _sha(validation_plan_sha256, "validation_plan_sha256")
        if canonical_sha256(validation_plan) != plan_hash:
            _fail("experiment_input_invalid", "validation plan hash does not match")
        confirmation = _sha(preview_sha256, "preview_sha256")
        actor_text = _text(actor, "actor")
        key = _text(idempotency_key, "idempotency_key")
        occurred = _timestamp(occurred_at_utc, "occurred_at_utc")
        command_payload = {
            "expected_state": "awaiting_validation_confirmation",
            "expected_revision": expected_revision,
            "new_state": "validation_collecting",
            "event_type": "validation_confirmed",
            "actor": actor_text,
            "preview_sha256": confirmation,
            "validation_plan_sha256": plan_hash,
        }
        payload_json = compact_json(command_payload)
        plan_json = compact_json(validation_plan)
        with self._write_connection() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT experiment_id, event_type, actor, confirmation_sha256, payload_json "
                "FROM experiment_events WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["experiment_id"] != identity
                    or prior["event_type"] != "validation_confirmed"
                    or prior["actor"] != actor_text
                    or prior["confirmation_sha256"] != confirmation
                    or prior["payload_json"] != payload_json
                ):
                    _fail("idempotency_conflict", "idempotency key changed")
                result = _experiment(
                    connection.execute(
                        "SELECT * FROM experiments WHERE experiment_id = ?", (identity,)
                    ).fetchone()
                )
                connection.commit()
                assert result is not None
                return result
            row = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (identity,)
            ).fetchone()
            if row is None:
                _fail("experiment_not_found", "experiment does not exist")
            if row["state"] != "awaiting_validation_confirmation" or row["revision"] != expected_revision:
                _fail("experiment_revision_conflict", "experiment state or revision changed")
            if row["validation_plan_json"] is not None:
                _fail("validation_confirmation_mismatch", "validation plan is already bound")
            if connection.execute(
                "SELECT 1 FROM experiment_events "
                "WHERE experiment_id = ? AND confirmation_sha256 = ? LIMIT 1",
                (identity, confirmation),
            ).fetchone() is not None:
                _fail("confirmation_conflict", "confirmation was already used")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM experiment_events "
                    "WHERE experiment_id = ?",
                    (identity,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE experiments SET state = 'validation_collecting', "
                "validation_plan_json = ?, validation_plan_sha256 = ?, "
                "revision = revision + 1, updated_at_utc = ? WHERE experiment_id = ?",
                (plan_json, plan_hash, occurred, identity),
            )
            connection.execute(
                "INSERT INTO experiment_events("
                "experiment_id, sequence, event_type, actor, confirmation_sha256, "
                "idempotency_key, payload_json, occurred_at_utc"
                ") VALUES (?, ?, 'validation_confirmed', ?, ?, ?, ?, ?)",
                (
                    identity,
                    sequence,
                    actor_text,
                    confirmation,
                    key,
                    payload_json,
                    occurred,
                ),
            )
            connection.commit()
        result = self.get_experiment(identity)
        assert result is not None
        return result

    def put_observation(
        self,
        experiment_id: object,
        *,
        observation_key: object,
        kind: object,
        status: object,
        payload: object,
        created_at_utc: object,
        recommendation_point_id: object | None = None,
        arm_id: object | None = None,
        artifact_ref: object | None = None,
        artifact_sha256: object | None = None,
    ) -> dict[str, Any]:
        identity = _text(experiment_id, "experiment_id")
        key = _text(observation_key, "observation_key")
        kind_text = _text(kind, "kind")
        status_text = _text(status, "status")
        if kind_text not in OBSERVATION_KINDS or status_text not in OBSERVATION_STATUSES:
            _fail("experiment_input_invalid", "observation kind or status is invalid")
        occurred = _timestamp(created_at_utc, "created_at_utc")
        point_id = None if recommendation_point_id is None else _text(
            recommendation_point_id, "recommendation_point_id"
        )
        arm = None if arm_id is None else _text(arm_id, "arm_id")
        ref = None if artifact_ref is None else _text(artifact_ref, "artifact_ref")
        artifact_hash = None if artifact_sha256 is None else _sha(
            artifact_sha256, "artifact_sha256"
        )
        if (ref is None) != (artifact_hash is None):
            _fail("experiment_input_invalid", "artifact ref/hash must be paired")
        canonical = (
            point_id,
            arm,
            kind_text,
            status_text,
            compact_json(payload),
            ref,
            artifact_hash,
        )
        with self._write_connection() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            experiment = connection.execute(
                "SELECT state, research_receipt_ref FROM experiments WHERE experiment_id = ?",
                (identity,),
            ).fetchone()
            if experiment is None:
                _fail("experiment_not_found", "experiment does not exist")
            if experiment["state"] != "research_running" or experiment["research_receipt_ref"] is not None:
                _fail("observation_write_closed", "experiment observations are frozen")
            existing = connection.execute(
                "SELECT * FROM experiment_observations "
                "WHERE experiment_id = ? AND observation_key = ?",
                (identity, key),
            ).fetchone()
            if existing is not None:
                stored = tuple(
                    existing[field]
                    for field in (
                        "recommendation_point_id",
                        "arm_id",
                        "kind",
                        "status",
                        "payload_json",
                        "artifact_ref",
                        "artifact_sha256",
                    )
                )
                if stored != canonical:
                    _fail(
                        "observation_immutable_conflict",
                        "observation key is already bound to different content",
                    )
                connection.commit()
                return _observation(existing)
            connection.execute(
                "INSERT INTO experiment_observations("
                "experiment_id, observation_key, recommendation_point_id, arm_id, "
                "observation_slot_utc, kind, status, payload_json, artifact_ref, artifact_sha256, "
                "created_at_utc, updated_at_utc"
                ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (identity, key, *canonical, occurred, occurred),
            )
            row = connection.execute(
                "SELECT * FROM experiment_observations "
                "WHERE experiment_id = ? AND observation_key = ?",
                (identity, key),
            ).fetchone()
            connection.commit()
        assert row is not None
        return _observation(row)

    def list_observations(
        self, experiment_id: object, *, kind: object | None = None
    ) -> list[dict[str, Any]]:
        identity = _text(experiment_id, "experiment_id")
        kind_text = None if kind is None else _text(kind, "kind")
        with self._read_connection() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM experiment_observations WHERE experiment_id = ? "
                + ("AND kind = ? " if kind_text is not None else "")
                + "ORDER BY observation_key",
                (identity, kind_text) if kind_text is not None else (identity,),
            ).fetchall()
        return [_observation(row) for row in rows]

    def attach_research_receipt_and_transition(
        self,
        experiment_id: object,
        *,
        expected_state: object,
        expected_revision: object,
        new_state: object,
        receipt_ref: object,
        receipt_sha256: object,
        leader: object | None,
        actor: object,
        occurred_at_utc: object,
        payload: object,
        idempotency_key: object,
    ) -> dict[str, Any]:
        identity = _text(experiment_id, "experiment_id")
        old_state = _text(expected_state, "expected_state")
        state = _text(new_state, "new_state")
        if old_state not in EXPERIMENT_STATES or state not in EXPERIMENT_STATES:
            _fail("experiment_input_invalid", "experiment state is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            _fail("experiment_input_invalid", "expected_revision is invalid")
        ref = _text(receipt_ref, "receipt_ref")
        receipt_hash = _sha(receipt_sha256, "receipt_sha256")
        actor_text = _text(actor, "actor")
        occurred = _timestamp(occurred_at_utc, "occurred_at_utc")
        key = _text(idempotency_key, "idempotency_key")
        command_payload = {
            "expected_state": old_state,
            "expected_revision": expected_revision,
            "new_state": state,
            "event_type": "research_concluded",
            "actor": actor_text,
            "receipt_ref": ref,
            "receipt_sha256": receipt_hash,
            "leader": leader,
            "payload": payload,
        }
        payload_json = compact_json(command_payload)
        leader_json = None if leader is None else compact_json(leader)
        with self._write_connection() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT experiment_id, event_type, actor, payload_json FROM experiment_events "
                "WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if prior is not None:
                prior_payload = json.loads(prior["payload_json"])
                prior_payload.pop("actor", None)
                retry_payload = json.loads(payload_json)
                retry_payload.pop("actor", None)
                if (
                    prior["experiment_id"] != identity
                    or prior["event_type"] != "research_concluded"
                    or prior_payload != retry_payload
                ):
                    _fail("idempotency_conflict", "idempotency key changed")
                result = _experiment(
                    connection.execute(
                        "SELECT * FROM experiments WHERE experiment_id = ?", (identity,)
                    ).fetchone()
                )
                connection.commit()
                assert result is not None
                return result
            if (old_state, state) not in {
                ("research_complete", "awaiting_validation_confirmation"),
                ("research_complete", "completed"),
            }:
                _fail("experiment_transition_invalid", "experiment transition is invalid")
            if (leader is not None) != (state == "awaiting_validation_confirmation"):
                _fail(
                    "receipt_leader_state_conflict",
                    "research leader does not match the receipt conclusion state",
                )
            row = connection.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?", (identity,)
            ).fetchone()
            if row is None:
                _fail("experiment_not_found", "experiment does not exist")
            if row["research_receipt_ref"] is not None:
                if (
                    row["research_receipt_ref"] != ref
                    or row["research_receipt_sha256"] != receipt_hash
                    or row["leader_json"] != leader_json
                ):
                    _fail("receipt_immutable_conflict", "research receipt is already bound")
                connection.commit()
                result = _experiment(row)
                assert result is not None
                return result
            if row["state"] != old_state or row["revision"] != expected_revision:
                _fail("experiment_revision_conflict", "experiment state or revision changed")
            if state not in TERMINAL_STATES and connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id <> ? AND state <> 'completed' LIMIT 1",
                (identity,),
            ).fetchone() is not None:
                _fail("active_experiment_exists", "a non-terminal Strategy Lab experiment already exists")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM experiment_events "
                    "WHERE experiment_id = ?",
                    (identity,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE experiments SET state = ?, leader_json = ?, "
                "research_receipt_ref = ?, research_receipt_sha256 = ?, "
                "revision = revision + 1, updated_at_utc = ? WHERE experiment_id = ?",
                (state, leader_json, ref, receipt_hash, occurred, identity),
            )
            connection.execute(
                "INSERT INTO experiment_events("
                "experiment_id, sequence, event_type, actor, idempotency_key, "
                "payload_json, occurred_at_utc"
                ") VALUES (?, ?, 'research_concluded', ?, ?, ?, ?)",
                (identity, sequence, actor_text, key, payload_json, occurred),
            )
            connection.commit()
        result = self.get_experiment(identity)
        assert result is not None
        return result


__all__ = ["ExperimentStore", "ExperimentStoreError", "compact_json"]
