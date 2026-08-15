from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote

from src.infrastructure.private_storage import (
    connect_private_sqlite,
    private_path,
    secure_sqlite_artifacts,
)


SCHEMA_COMPONENT = "sell_put_top1_experiment_store"
SCHEMA_VERSION = 2

_V1_REQUIRED_TABLES = {
    "strategy_lab_schema",
    "strategy_lab_features",
    "strategy_lab_experiments",
    "strategy_lab_generations",
    "strategy_lab_hidden_commitments",
    "strategy_lab_events",
}
_REQUIRED_TABLES = {
    *_V1_REQUIRED_TABLES,
    "strategy_lab_corpus_days",
    "strategy_lab_corpus_points",
}
_REQUIRED_INDEXES = {
    "strategy_lab_one_active_validation",
    "strategy_lab_hidden_date_unique",
    "strategy_lab_event_subject_unique",
    "strategy_lab_event_idempotency_unique",
}
_V1_EXPECTED_FOREIGN_KEYS = {
    "strategy_lab_experiments": {
        ("receipt_request_event_id", "strategy_lab_events", "event_id"),
        ("receipt_published_event_id", "strategy_lab_events", "event_id"),
    },
    "strategy_lab_generations": {
        ("experiment_id", "strategy_lab_experiments", "experiment_id"),
        ("terminal_request_event_id", "strategy_lab_events", "event_id"),
        ("terminal_published_event_id", "strategy_lab_events", "event_id"),
    },
    "strategy_lab_hidden_commitments": {
        ("experiment_id", "strategy_lab_experiments", "experiment_id"),
    },
    "strategy_lab_events": set(),
}
_EXPECTED_FOREIGN_KEYS = {
    **_V1_EXPECTED_FOREIGN_KEYS,
    "strategy_lab_corpus_days": set(),
    "strategy_lab_corpus_points": {
        ("market", "strategy_lab_corpus_days", "market"),
        ("account", "strategy_lab_corpus_days", "account"),
        ("trading_date", "strategy_lab_corpus_days", "trading_date"),
    },
}


class ExperimentStoreError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def compact_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class ExperimentStore:
    """Single SQLite write authority for the experimental Top1 lifecycle."""

    def __init__(self, path: str | Path) -> None:
        self.path = private_path(path)

    def schema_state(self) -> dict[str, object]:
        if not self.path.exists():
            return {"status": "not_initialized", "schema_version": None}
        if self.path.is_symlink() or not self.path.is_file():
            return {"status": "schema_unsupported", "schema_version": None}
        if self.path.stat().st_size == 0:
            return {"status": "empty", "schema_version": None}
        try:
            connection = self._readonly_connection()
            try:
                tables = self._tables(connection)
                if not tables:
                    return {"status": "empty", "schema_version": None}
                if "strategy_lab_schema" not in tables:
                    return {"status": "schema_unsupported", "schema_version": None}
                metadata = connection.execute(
                    "SELECT schema_version FROM strategy_lab_schema WHERE component = ?",
                    (SCHEMA_COMPONENT,),
                ).fetchone()
                if metadata is None:
                    return {"status": "schema_unsupported", "schema_version": None}
                version = int(metadata[0])
                if version == 0 and tables == {"strategy_lab_schema"}:
                    return {"status": "migration_required", "schema_version": 0}
                if version == 1:
                    self._validate_v1(connection, deep=True)
                    return {"status": "migration_required", "schema_version": 1}
                if version != SCHEMA_VERSION:
                    return {"status": "schema_unsupported", "schema_version": version}
                self._validate_v2(connection, deep=True)
                return {"status": "ready", "schema_version": version}
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError, ValueError, ExperimentStoreError):
            return {"status": "schema_unsupported", "schema_version": None}

    def migrate(self, *, migrated_at_utc: str) -> dict[str, object]:
        connection = connect_private_sqlite(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection)
            connection.execute("BEGIN IMMEDIATE")
            tables = self._tables(connection)
            if not tables:
                self._create_v1(connection)
                self._create_v2(connection)
                connection.execute(
                    "INSERT INTO strategy_lab_schema(component, schema_version, migrated_at_utc) "
                    "VALUES (?, ?, ?)",
                    (SCHEMA_COMPONENT, SCHEMA_VERSION, migrated_at_utc),
                )
            elif tables == {"strategy_lab_schema"}:
                metadata = connection.execute(
                    "SELECT schema_version FROM strategy_lab_schema WHERE component = ?",
                    (SCHEMA_COMPONENT,),
                ).fetchone()
                if metadata is None or int(metadata[0]) != 0:
                    raise ExperimentStoreError(
                        "schema_unsupported", "version-0 metadata is invalid"
                    )
                connection.execute("DROP TABLE strategy_lab_schema")
                self._create_v1(connection)
                self._create_v2(connection)
                connection.execute(
                    "INSERT INTO strategy_lab_schema(component, schema_version, migrated_at_utc) "
                    "VALUES (?, ?, ?)",
                    (SCHEMA_COMPONENT, SCHEMA_VERSION, migrated_at_utc),
                )
            else:
                metadata = (
                    connection.execute(
                        "SELECT schema_version FROM strategy_lab_schema WHERE component = ?",
                        (SCHEMA_COMPONENT,),
                    ).fetchone()
                    if "strategy_lab_schema" in tables
                    else None
                )
                if metadata is None:
                    raise ExperimentStoreError(
                        "schema_unsupported", "existing schema cannot be migrated"
                    )
                version = int(metadata[0])
                if version == 1:
                    self._validate_v1(connection, deep=True)
                    self._create_v2(connection)
                    connection.execute(
                        "UPDATE strategy_lab_schema "
                        "SET schema_version = ?, migrated_at_utc = ? "
                        "WHERE component = ?",
                        (SCHEMA_VERSION, migrated_at_utc, SCHEMA_COMPONENT),
                    )
                elif version == SCHEMA_VERSION:
                    self._validate_v2(connection, deep=True)
                else:
                    raise ExperimentStoreError(
                        "schema_unsupported", "existing schema cannot be migrated"
                    )
            connection.commit()
            self._validate_v2(connection, deep=True)
        except (sqlite3.DatabaseError, ValueError) as exc:
            connection.rollback()
            raise ExperimentStoreError("schema_unsupported", "SQLite schema is invalid") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            secure_sqlite_artifacts(self.path)
        return {"status": "ready", "schema_version": SCHEMA_VERSION}

    def feature(self, market: str, account: str) -> dict[str, Any] | None:
        with self._read() as connection:
            return _row(
                connection.execute(
                    "SELECT * FROM strategy_lab_features WHERE market = ? AND account = ?",
                    (market, account),
                ).fetchone()
            )

    def corpus_day(
        self, market: str, account: str, trading_date: str
    ) -> dict[str, Any] | None:
        with self._read() as connection:
            return _row(
                connection.execute(
                    """
                    SELECT * FROM strategy_lab_corpus_days
                    WHERE market = ? AND account = ? AND trading_date = ?
                    """,
                    (market, account, trading_date),
                ).fetchone()
            )

    def corpus_days(self, market: str, account: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM strategy_lab_corpus_days
                    WHERE market = ? AND account = ?
                    ORDER BY trading_date
                    """,
                    (market, account),
                ).fetchall()
            ]

    def record_corpus_day(
        self,
        *,
        market: str,
        account: str,
        trading_date: str,
        expectation_ref: str,
        expectation_content_sha256: str,
        expectation_file_sha256: str,
        market_calendar_version: str,
        market_calendar_sha256: str,
        schedule_config_sha256: str,
        expected_point_count: int,
        first_target_at_utc: str | None,
        sealed_at_utc: str,
        sealed_before_first_target: bool,
        completeness_reason: str | None,
        conflict_observed: bool = False,
    ) -> dict[str, Any]:
        values = {
            "market": market,
            "account": account,
            "trading_date": trading_date,
            "expectation_ref": expectation_ref,
            "expectation_content_sha256": expectation_content_sha256,
            "expectation_file_sha256": expectation_file_sha256,
            "market_calendar_version": market_calendar_version,
            "market_calendar_sha256": market_calendar_sha256,
            "schedule_config_sha256": schedule_config_sha256,
            "expected_point_count": expected_point_count,
            "first_target_at_utc": first_target_at_utc,
            "sealed_at_utc": sealed_at_utc,
            "sealed_before_first_target": int(sealed_before_first_target),
            "completeness_reason": (
                "research_corpus_conflict"
                if conflict_observed
                else completeness_reason
            ),
        }
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM strategy_lab_corpus_days
                WHERE market = ? AND account = ? AND trading_date = ?
                """,
                (market, account, trading_date),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO strategy_lab_corpus_days(
                        market, account, trading_date,
                        expectation_ref, expectation_content_sha256,
                        expectation_file_sha256, market_calendar_version,
                        market_calendar_sha256, schedule_config_sha256,
                        expected_point_count, first_target_at_utc, sealed_at_utc,
                        sealed_before_first_target, completeness_reason,
                        conflict_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*values.values(), "conflict" if conflict_observed else "clean"),
                )
                status = "conflict" if conflict_observed else "inserted"
            else:
                exact = all(existing[key] == value for key, value in values.items())
                if conflict_observed or not exact or existing["conflict_status"] == "conflict":
                    connection.execute(
                        """
                        UPDATE strategy_lab_corpus_days
                        SET conflict_status = 'conflict'
                        WHERE market = ? AND account = ? AND trading_date = ?
                        """,
                        (market, account, trading_date),
                    )
                    status = "conflict"
                else:
                    status = "idempotent"
            row = connection.execute(
                """
                SELECT * FROM strategy_lab_corpus_days
                WHERE market = ? AND account = ? AND trading_date = ?
                """,
                (market, account, trading_date),
            ).fetchone()
            return {"status": status, "row": dict(row or {})}

    def corpus_point(
        self, market: str, account: str, recommendation_point_id: str
    ) -> dict[str, Any] | None:
        with self._read() as connection:
            return _row(
                connection.execute(
                    """
                    SELECT * FROM strategy_lab_corpus_points
                    WHERE market = ? AND account = ? AND recommendation_point_id = ?
                    """,
                    (market, account, recommendation_point_id),
                ).fetchone()
            )

    def corpus_points(
        self,
        market: str,
        account: str,
        *,
        trading_date: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM strategy_lab_corpus_points "
            "WHERE market = ? AND account = ?"
        )
        params: tuple[object, ...] = (market, account)
        if trading_date is not None:
            query += " AND trading_date = ?"
            params += (trading_date,)
        query += " ORDER BY trading_date, recommendation_point_id"
        with self._read() as connection:
            return [
                dict(row)
                for row in connection.execute(query, params).fetchall()
            ]

    def record_corpus_point(
        self,
        *,
        market: str,
        account: str,
        recommendation_point_id: str,
        trading_date: str,
        source_run_id: str,
        source_point_ref: str,
        source_point_content_sha256: str,
        opening_snapshot_ref: str,
        opening_snapshot_sha256: str,
        ranking_projection_schema_version: str | None,
        projection_ref: str | None,
        projection_content_sha256: str | None,
        projection_file_sha256: str | None,
        captured_at_utc: str,
        capture_status: str,
        reason_code: str | None,
        conflict_observed: bool = False,
    ) -> dict[str, Any]:
        if conflict_observed:
            capture_status = "not_evaluable"
            reason_code = "research_corpus_conflict"
            ranking_projection_schema_version = None
            projection_ref = None
            projection_content_sha256 = None
            projection_file_sha256 = None
        values = {
            "market": market,
            "account": account,
            "recommendation_point_id": recommendation_point_id,
            "trading_date": trading_date,
            "source_run_id": source_run_id,
            "source_point_ref": source_point_ref,
            "source_point_content_sha256": source_point_content_sha256,
            "opening_snapshot_ref": opening_snapshot_ref,
            "opening_snapshot_sha256": opening_snapshot_sha256,
            "ranking_projection_schema_version": ranking_projection_schema_version,
            "projection_ref": projection_ref,
            "projection_content_sha256": projection_content_sha256,
            "projection_file_sha256": projection_file_sha256,
            "captured_at_utc": captured_at_utc,
            "capture_status": capture_status,
            "reason_code": reason_code,
        }
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM strategy_lab_corpus_points
                WHERE market = ? AND account = ? AND recommendation_point_id = ?
                """,
                (market, account, recommendation_point_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO strategy_lab_corpus_points(
                        market, account, recommendation_point_id, trading_date,
                        source_run_id, source_point_ref,
                        source_point_content_sha256, opening_snapshot_ref,
                        opening_snapshot_sha256,
                        ranking_projection_schema_version, projection_ref,
                        projection_content_sha256, projection_file_sha256,
                        captured_at_utc, capture_status, reason_code,
                        conflict_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*values.values(), "conflict" if conflict_observed else "clean"),
                )
                status = "conflict" if conflict_observed else "inserted"
            else:
                exact = all(
                    existing[key] == value
                    for key, value in values.items()
                    if key != "captured_at_utc"
                )
                if conflict_observed or not exact or existing["conflict_status"] == "conflict":
                    connection.execute(
                        """
                        UPDATE strategy_lab_corpus_points
                        SET conflict_status = 'conflict'
                        WHERE market = ? AND account = ? AND recommendation_point_id = ?
                        """,
                        (market, account, recommendation_point_id),
                    )
                    status = "conflict"
                else:
                    status = "idempotent"
            row = connection.execute(
                """
                SELECT * FROM strategy_lab_corpus_points
                WHERE market = ? AND account = ? AND recommendation_point_id = ?
                """,
                (market, account, recommendation_point_id),
            ).fetchone()
            return {"status": status, "row": dict(row or {})}

    def set_feature(
        self,
        *,
        market: str,
        account: str,
        enabled: bool,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {"market": market, "account": account, "user_opt_in": enabled}
        with self._write() as connection:
            existing_command = self._command_event(
                connection, f"feature:{market}:{account}", idempotency_key
            )
            if existing_command is not None:
                self._assert_event_replay(
                    existing_command, request, actor, occurred_at_utc
                )
                return dict(
                    connection.execute(
                        "SELECT * FROM strategy_lab_features WHERE market = ? AND account = ?",
                        (market, account),
                    ).fetchone()
                    or {}
                )
            current = connection.execute(
                "SELECT * FROM strategy_lab_features WHERE market = ? AND account = ?",
                (market, account),
            ).fetchone()
            version = int(current["state_version"]) if current is not None else 0
            changed = current is None or bool(current["user_opt_in"]) != enabled
            next_version = version + int(changed)
            subject = f"feature:{market}:{account}:state:{next_version}:value:{int(enabled)}"
            _, inserted = self._claim_event(
                connection,
                event_type="feature_intent_set",
                subject_key=subject,
                command_scope=f"feature:{market}:{account}",
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
            )
            if inserted and changed:
                connection.execute(
                    """
                    INSERT INTO strategy_lab_features(
                        market, account, user_opt_in, last_actor, last_occurred_at_utc,
                        state_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, account) DO UPDATE SET
                        user_opt_in = excluded.user_opt_in,
                        last_actor = excluded.last_actor,
                        last_occurred_at_utc = excluded.last_occurred_at_utc,
                        state_version = excluded.state_version
                    """,
                    (
                        market,
                        account,
                        int(enabled),
                        actor,
                        occurred_at_utc,
                        next_version,
                    ),
                )
            return dict(
                connection.execute(
                    "SELECT * FROM strategy_lab_features WHERE market = ? AND account = ?",
                    (market, account),
                ).fetchone()
                or {}
            )

    def prepare_experiment(
        self,
        *,
        experiment_id: str,
        topic_id: str,
        market: str,
        account: str,
        spec_json: str,
        research_spec_sha256: str,
        provenance_json: str,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "spec_sha256": _sha256_text(spec_json),
            "research_spec_sha256": research_spec_sha256,
            "provenance_sha256": _sha256_text(provenance_json),
        }
        scope = f"experiment:{experiment_id}:prepare"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_experiment(connection, experiment_id)
            current = connection.execute(
                "SELECT * FROM strategy_lab_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if current is not None and (
                current["phase"] != "draft" or current["terminal_mode"] is not None
            ):
                raise ExperimentStoreError(
                    "invalid_transition", "experiment is no longer editable"
                )
            if current is not None and (
                current["market"] != market
                or current["account"] != account
                or current["topic_id"] != topic_id
            ):
                raise ExperimentStoreError(
                    "experiment_conflict", "experiment identity changed"
                )
            subject = f"experiment:{experiment_id}:prepare:{request['spec_sha256']}"
            _, inserted = self._claim_event(
                connection,
                event_type="experiment_prepared",
                subject_key=subject,
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
            )
            if inserted:
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO strategy_lab_experiments(
                            experiment_id, topic_id, market, account, strategy_family,
                            spec_json, research_spec_sha256, source_provenance_json,
                            phase, research_authorization_status,
                            validation_authorization_status,
                            completed_validation_partitions, created_at_utc,
                            updated_at_utc, state_version
                        ) VALUES (?, ?, ?, ?, 'sell_put', ?, ?, ?, 'draft',
                                  'unconfirmed', 'unconfirmed', 0, ?, ?, 1)
                        """,
                        (
                            experiment_id,
                            topic_id,
                            market,
                            account,
                            spec_json,
                            research_spec_sha256,
                            provenance_json,
                            occurred_at_utc,
                            occurred_at_utc,
                        ),
                    )
                elif (
                    current["spec_json"] != spec_json
                    or current["source_provenance_json"] != provenance_json
                ):
                    connection.execute(
                        """
                        UPDATE strategy_lab_experiments SET
                            spec_json = ?, research_spec_sha256 = ?,
                            source_provenance_json = ?,
                            validation_spec_sha256 = NULL,
                            research_authorization_status = 'unconfirmed',
                            research_authorized_hash = NULL,
                            research_authorized_actor = NULL,
                            research_authorized_at_utc = NULL,
                            validation_authorization_status = 'unconfirmed',
                            validation_authorized_hash = NULL,
                            validation_authorized_actor = NULL,
                            validation_authorized_at_utc = NULL,
                            updated_at_utc = ?, state_version = state_version + 1
                        WHERE experiment_id = ?
                        """,
                        (
                            spec_json,
                            research_spec_sha256,
                            provenance_json,
                            occurred_at_utc,
                            experiment_id,
                        ),
                    )
            return self._required_experiment(connection, experiment_id)

    def authorize(
        self,
        *,
        experiment_id: str,
        stage: str,
        authorized_hash: str,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if stage not in {"research", "validation"}:
            raise ValueError("stage must be research or validation")
        request = {"experiment_id": experiment_id, "authorized_hash": authorized_hash}
        scope = f"experiment:{experiment_id}:authorize:{stage}"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_experiment(connection, experiment_id)
            current = self._required_experiment(connection, experiment_id)
            expected = current[f"{stage}_spec_sha256"]
            if expected is None or expected != authorized_hash:
                raise ExperimentStoreError(
                    "authorization_hash_mismatch", "authorization hash is stale"
                )
            if current["terminal_mode"] is not None:
                raise ExperimentStoreError("invalid_transition", "experiment is terminal")
            if stage == "research" and current["phase"] != "draft":
                raise ExperimentStoreError(
                    "invalid_transition", "research authorization is closed"
                )
            if stage == "validation" and not (
                current["phase"] == "research"
                and current["research_progress"] == "challenger_locked"
            ):
                raise ExperimentStoreError(
                    "invalid_transition", "challenger is not locked"
                )
            if (
                current[f"{stage}_authorization_status"] == "confirmed"
                and current[f"{stage}_authorized_hash"] == authorized_hash
            ):
                return current
            subject = (
                f"experiment:{experiment_id}:authorize:{stage}:"
                f"version:{current['state_version']}:hash:{authorized_hash}"
            )
            _, inserted = self._claim_event(
                connection,
                event_type=f"{stage}_authorized",
                subject_key=subject,
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
            )
            if inserted:
                connection.execute(
                    f"""
                    UPDATE strategy_lab_experiments SET
                        {stage}_authorization_status = 'confirmed',
                        {stage}_authorized_hash = ?,
                        {stage}_authorized_actor = ?,
                        {stage}_authorized_at_utc = ?,
                        updated_at_utc = ?, state_version = state_version + 1
                    WHERE experiment_id = ?
                    """,
                    (
                        authorized_hash,
                        actor,
                        occurred_at_utc,
                        occurred_at_utc,
                        experiment_id,
                    ),
                )
            return self._required_experiment(connection, experiment_id)

    def start_research(
        self,
        *,
        experiment_id: str,
        authorized_hash: str,
        dataset_ref: str,
        dataset_file_sha256: str,
        frozen_row_sha256: str,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "authorized_hash": authorized_hash,
            "dataset_ref": dataset_ref,
            "dataset_file_sha256": dataset_file_sha256,
            "frozen_row_sha256": frozen_row_sha256,
        }
        scope = f"experiment:{experiment_id}:start-research"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_experiment(connection, experiment_id)
            current = self._required_experiment(connection, experiment_id)
            _, inserted = self._claim_event(
                connection,
                event_type="research_started",
                subject_key=f"experiment:{experiment_id}:research",
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
                generation_kind="research",
            )
            if not inserted:
                return current
            if current["terminal_mode"] is not None or current["phase"] != "draft":
                raise ExperimentStoreError(
                    "invalid_transition", "research cannot start from current state"
                )
            if (
                current["research_authorization_status"] != "confirmed"
                or current["research_authorized_hash"] != authorized_hash
                or current["research_spec_sha256"] != authorized_hash
            ):
                raise ExperimentStoreError(
                    "authorization_required", "current research hash is not confirmed"
                )
            connection.execute(
                """
                INSERT INTO strategy_lab_generations(
                    experiment_id, generation_kind, state, revision,
                    last_revision_ref, last_revision_file_sha256,
                    frozen_row_content_sha256, created_at_utc, updated_at_utc
                ) VALUES (?, 'research', 'open', 0, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    dataset_ref,
                    dataset_file_sha256,
                    frozen_row_sha256,
                    occurred_at_utc,
                    occurred_at_utc,
                ),
            )
            connection.execute(
                """
                UPDATE strategy_lab_experiments SET
                    phase = 'research', research_progress = 'building_dataset',
                    updated_at_utc = ?, state_version = state_version + 1
                WHERE experiment_id = ?
                """,
                (occurred_at_utc, experiment_id),
            )
            return self._required_experiment(connection, experiment_id)

    def record_generation_revision(
        self,
        *,
        experiment_id: str,
        generation_kind: str,
        revision: int,
        revision_ref: str,
        revision_file_sha256: str,
        frozen_row_sha256: str,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "generation_kind": generation_kind,
            "revision": revision,
            "revision_ref": revision_ref,
            "revision_file_sha256": revision_file_sha256,
            "frozen_row_sha256": frozen_row_sha256,
        }
        scope = f"generation:{experiment_id}:{generation_kind}:revision"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_generation(connection, experiment_id, generation_kind)
            experiment = self._required_experiment(connection, experiment_id)
            generation = self._required_generation(
                connection, experiment_id, generation_kind
            )
            _, inserted = self._claim_event(
                connection,
                event_type="generation_revision_recorded",
                subject_key=f"generation:{experiment_id}:{generation_kind}:revision:{revision}",
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
                generation_kind=generation_kind,
            )
            if not inserted:
                return generation
            if experiment["terminal_mode"] is not None:
                raise ExperimentStoreError("late_write", "experiment is terminal")
            if generation["terminal_request_event_id"] is not None:
                raise ExperimentStoreError("late_write", "generation is terminal")
            if revision != int(generation["revision"]) + 1:
                raise ExperimentStoreError(
                    "generation_conflict", "generation revision is not next"
                )
            connection.execute(
                """
                UPDATE strategy_lab_generations SET
                    revision = ?, last_revision_ref = ?,
                    last_revision_file_sha256 = ?, frozen_row_content_sha256 = ?,
                    updated_at_utc = ?
                WHERE experiment_id = ? AND generation_kind = ?
                """,
                (
                    revision,
                    revision_ref,
                    revision_file_sha256,
                    frozen_row_sha256,
                    occurred_at_utc,
                    experiment_id,
                    generation_kind,
                ),
            )
            return self._required_generation(connection, experiment_id, generation_kind)

    def lock_challenger(
        self,
        *,
        experiment_id: str,
        spec_json: str,
        research_spec_sha256: str,
        validation_spec_sha256: str,
        research_leader: str,
        research_receipt_ref: str,
        research_receipt_file_sha256: str,
        commitment_json: str,
        commitment_sha256: str,
        commitment_ref: str,
        commitment_content_sha256: str,
        commitment_file_sha256: str,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "spec_sha256": _sha256_text(spec_json),
            "research_spec_sha256": research_spec_sha256,
            "validation_spec_sha256": validation_spec_sha256,
            "research_leader": research_leader,
            "research_receipt_ref": research_receipt_ref,
            "research_receipt_file_sha256": research_receipt_file_sha256,
            "commitment_sha256": commitment_sha256,
            "commitment_ref": commitment_ref,
            "commitment_content_sha256": commitment_content_sha256,
            "commitment_file_sha256": commitment_file_sha256,
        }
        scope = f"experiment:{experiment_id}:lock-challenger"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_experiment(connection, experiment_id)
            current = self._required_experiment(connection, experiment_id)
            research = self._required_generation(connection, experiment_id, "research")
            if current["terminal_mode"] is not None or current["phase"] != "research":
                raise ExperimentStoreError(
                    "invalid_transition", "challenger cannot be locked"
                )
            if current["research_spec_sha256"] != research_spec_sha256:
                raise ExperimentStoreError(
                    "experiment_conflict", "research hash changed"
                )
            if not (
                research["terminal_mode"] == "completed"
                and research["terminal_published_event_id"] is not None
            ):
                raise ExperimentStoreError(
                    "invalid_transition", "research terminal is not published"
                )
            if current["research_leader"] not in {None, research_leader}:
                raise ExperimentStoreError(
                    "experiment_conflict", "research leader is already immutable"
                )
            if current["research_receipt_ref"] not in {None, research_receipt_ref}:
                raise ExperimentStoreError(
                    "experiment_conflict", "research receipt ref changed"
                )
            if current["research_receipt_file_sha256"] not in {
                None,
                research_receipt_file_sha256,
            }:
                raise ExperimentStoreError(
                    "experiment_conflict", "research receipt hash changed"
                )
            subject = (
                f"experiment:{experiment_id}:challenger:{research_leader}:"
                f"validation:{validation_spec_sha256}"
            )
            _, inserted = self._claim_event(
                connection,
                event_type="challenger_locked",
                subject_key=subject,
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
            )
            if inserted:
                connection.execute(
                    """
                    UPDATE strategy_lab_experiments SET
                        spec_json = ?, validation_spec_sha256 = ?,
                        research_leader = ?, research_receipt_ref = ?,
                        research_receipt_file_sha256 = ?,
                        proposed_commitment_json = ?, proposed_commitment_sha256 = ?,
                        proposed_commitment_ref = ?,
                        proposed_commitment_content_sha256 = ?,
                        proposed_commitment_file_sha256 = ?,
                        research_progress = 'challenger_locked',
                        validation_authorization_status = 'unconfirmed',
                        validation_authorized_hash = NULL,
                        validation_authorized_actor = NULL,
                        validation_authorized_at_utc = NULL,
                        updated_at_utc = ?, state_version = state_version + 1
                    WHERE experiment_id = ?
                    """,
                    (
                        spec_json,
                        validation_spec_sha256,
                        research_leader,
                        research_receipt_ref,
                        research_receipt_file_sha256,
                        commitment_json,
                        commitment_sha256,
                        commitment_ref,
                        commitment_content_sha256,
                        commitment_file_sha256,
                        occurred_at_utc,
                        experiment_id,
                    ),
                )
            return self._required_experiment(connection, experiment_id)

    def start_validation(
        self,
        *,
        experiment_id: str,
        authorized_hash: str,
        commitment_sha256: str,
        commitment_dates: Sequence[str],
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "authorized_hash": authorized_hash,
            "commitment_sha256": commitment_sha256,
            "trading_dates": list(commitment_dates),
        }
        scope = f"experiment:{experiment_id}:start-validation"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_experiment(connection, experiment_id)
            current = self._required_experiment(connection, experiment_id)
            _, inserted = self._claim_event(
                connection,
                event_type="validation_started",
                subject_key=f"experiment:{experiment_id}:validation:{authorized_hash}",
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
                generation_kind="hidden",
            )
            if not inserted:
                return current
            if current["terminal_mode"] is not None or not (
                current["phase"] == "research"
                and current["research_progress"] == "challenger_locked"
            ):
                raise ExperimentStoreError(
                    "invalid_transition", "validation cannot start"
                )
            if (
                current["validation_authorization_status"] != "confirmed"
                or current["validation_authorized_hash"] != authorized_hash
                or current["validation_spec_sha256"] != authorized_hash
            ):
                raise ExperimentStoreError(
                    "authorization_required", "current validation hash is not confirmed"
                )
            if current["proposed_commitment_sha256"] != commitment_sha256:
                raise ExperimentStoreError(
                    "authorization_hash_mismatch", "commitment changed after authorization"
                )
            occupied = connection.execute(
                """
                SELECT trading_date FROM strategy_lab_hidden_commitments
                WHERE market = ? AND account = ? AND strategy_family = 'sell_put'
                  AND trading_date IN ({}) LIMIT 1
                """.format(",".join("?" for _ in commitment_dates)),
                (current["market"], current["account"], *commitment_dates),
            ).fetchone()
            if occupied is not None:
                raise ExperimentStoreError(
                    "hidden_window_overlap", "hidden trading date is already consumed"
                )
            active = connection.execute(
                """
                SELECT experiment_id FROM strategy_lab_experiments
                WHERE market = ? AND account = ? AND strategy_family = 'sell_put'
                  AND terminal_mode IS NULL AND phase = 'validation'
                  AND validation_progress = 'collecting_decisions'
                LIMIT 1
                """,
                (current["market"], current["account"]),
            ).fetchone()
            if active is not None:
                raise ExperimentStoreError(
                    "validation_slot_occupied", "validation collection slot is occupied"
                )
            connection.executemany(
                """
                INSERT INTO strategy_lab_hidden_commitments(
                    experiment_id, market, account, strategy_family,
                    commitment_sha256, trading_date
                ) VALUES (?, ?, ?, 'sell_put', ?, ?)
                """,
                [
                    (
                        experiment_id,
                        current["market"],
                        current["account"],
                        commitment_sha256,
                        trading_date,
                    )
                    for trading_date in commitment_dates
                ],
            )
            connection.execute(
                """
                INSERT INTO strategy_lab_generations(
                    experiment_id, generation_kind, state, revision,
                    last_revision_ref, last_revision_file_sha256,
                    frozen_row_content_sha256, created_at_utc, updated_at_utc
                ) VALUES (?, 'hidden', 'open', 0, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    current["proposed_commitment_ref"],
                    current["proposed_commitment_file_sha256"],
                    current["proposed_commitment_content_sha256"],
                    occurred_at_utc,
                    occurred_at_utc,
                ),
            )
            connection.execute(
                """
                UPDATE strategy_lab_experiments SET
                    phase = 'validation', validation_progress = 'collecting_decisions',
                    completed_validation_partitions = 0,
                    updated_at_utc = ?, state_version = state_version + 1
                WHERE experiment_id = ?
                """,
                (occurred_at_utc, experiment_id),
            )
            return self._required_experiment(connection, experiment_id)

    def commit_validation_point(
        self,
        *,
        experiment_id: str,
        point_id: str,
        trading_date: str,
        source_ref: str,
        source_file_sha256: str,
        revision: int,
        manifest_ref: str,
        manifest_file_sha256: str,
        frozen_row_sha256: str,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "point_id": point_id,
            "trading_date": trading_date,
            "source_ref": source_ref,
            "source_file_sha256": source_file_sha256,
            "revision": revision,
            "manifest_ref": manifest_ref,
            "manifest_file_sha256": manifest_file_sha256,
            "frozen_row_sha256": frozen_row_sha256,
        }
        scope = f"experiment:{experiment_id}:validation-point"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_generation(connection, experiment_id, "hidden")
            experiment = self._required_experiment(connection, experiment_id)
            generation = self._required_generation(connection, experiment_id, "hidden")
            _, inserted = self._claim_event(
                connection,
                event_type="validation_point_committed",
                subject_key=f"experiment:{experiment_id}:point:{point_id}",
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
                generation_kind="hidden",
            )
            if not inserted:
                return generation
            if experiment["terminal_mode"] is not None or not (
                experiment["phase"] == "validation"
                and experiment["validation_progress"] == "collecting_decisions"
                and generation["terminal_request_event_id"] is None
            ):
                raise ExperimentStoreError("late_write", "validation intake is closed")
            committed_date = connection.execute(
                """
                SELECT 1 FROM strategy_lab_hidden_commitments
                WHERE experiment_id = ? AND trading_date = ?
                """,
                (experiment_id, trading_date),
            ).fetchone()
            if committed_date is None:
                raise ExperimentStoreError(
                    "experiment_conflict", "point date is outside commitment"
                )
            next_date = connection.execute(
                """
                SELECT trading_date FROM strategy_lab_hidden_commitments
                WHERE experiment_id = ? ORDER BY trading_date LIMIT 1 OFFSET ?
                """,
                (experiment_id, int(experiment["completed_validation_partitions"])),
            ).fetchone()
            if next_date is None or str(next_date[0]) != trading_date:
                raise ExperimentStoreError(
                    "late_write", "point date partition is not open"
                )
            if revision != int(generation["revision"]) + 1:
                raise ExperimentStoreError(
                    "generation_conflict", "hidden revision is not next"
                )
            connection.execute(
                """
                UPDATE strategy_lab_generations SET
                    revision = ?, last_revision_ref = ?,
                    last_revision_file_sha256 = ?, frozen_row_content_sha256 = ?,
                    updated_at_utc = ?
                WHERE experiment_id = ? AND generation_kind = 'hidden'
                """,
                (
                    revision,
                    manifest_ref,
                    manifest_file_sha256,
                    frozen_row_sha256,
                    occurred_at_utc,
                    experiment_id,
                ),
            )
            return self._required_generation(connection, experiment_id, "hidden")

    def seal_validation_partition(
        self,
        *,
        experiment_id: str,
        trading_date: str,
        terminal_request: Mapping[str, object] | None,
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = {
            "experiment_id": experiment_id,
            "trading_date": trading_date,
            "terminal_request_sha256": (
                _sha256_text(compact_json(terminal_request))
                if terminal_request is not None
                else None
            ),
        }
        scope = f"experiment:{experiment_id}:validation-partition"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(replay, request, actor, occurred_at_utc)
                return self._required_experiment(connection, experiment_id)
            experiment = self._required_experiment(connection, experiment_id)
            generation = self._required_generation(connection, experiment_id, "hidden")
            _, inserted = self._claim_event(
                connection,
                event_type="validation_partition_sealed",
                subject_key=f"experiment:{experiment_id}:partition:{trading_date}",
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request,
                experiment_id=experiment_id,
                generation_kind="hidden",
            )
            if not inserted:
                return experiment
            if experiment["terminal_mode"] is not None or not (
                experiment["phase"] == "validation"
                and experiment["validation_progress"] == "collecting_decisions"
                and generation["terminal_request_event_id"] is None
            ):
                raise ExperimentStoreError("late_write", "validation intake is closed")
            dates = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT trading_date FROM strategy_lab_hidden_commitments
                    WHERE experiment_id = ? ORDER BY trading_date
                    """,
                    (experiment_id,),
                ).fetchall()
            ]
            completed = int(experiment["completed_validation_partitions"])
            if completed >= len(dates) or dates[completed] != trading_date:
                raise ExperimentStoreError(
                    "experiment_conflict", "partition is not the next commitment date"
                )
            point_rows = connection.execute(
                """
                SELECT payload_json FROM strategy_lab_events
                WHERE experiment_id = ? AND event_type = 'validation_point_committed'
                """,
                (experiment_id,),
            ).fetchall()
            if not any(
                json.loads(str(row[0])).get("trading_date") == trading_date
                for row in point_rows
            ):
                raise ExperimentStoreError(
                    "experiment_conflict", "partition has no committed point"
                )
            is_final = completed + 1 == 20
            if is_final != (terminal_request is not None):
                raise ExperimentStoreError(
                    "experiment_conflict", "terminal request is required only on day 20"
                )
            if terminal_request is not None:
                self._request_generation_terminal(
                    connection,
                    experiment_id=experiment_id,
                    generation_kind="hidden",
                    request=terminal_request,
                    actor=actor,
                    occurred_at_utc=occurred_at_utc,
                    idempotency_key=f"{idempotency_key}:hidden-terminal",
                )
            connection.execute(
                """
                UPDATE strategy_lab_experiments SET
                    completed_validation_partitions = ?,
                    validation_progress = CASE WHEN ? THEN 'awaiting_outcomes'
                                               ELSE validation_progress END,
                    updated_at_utc = ?, state_version = state_version + 1
                WHERE experiment_id = ?
                """,
                (completed + 1, int(is_final), occurred_at_utc, experiment_id),
            )
            return self._required_experiment(connection, experiment_id)

    def request_generation_terminal(
        self,
        *,
        experiment_id: str,
        generation_kind: str,
        request: Mapping[str, object],
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._write() as connection:
            self._request_generation_terminal(
                connection,
                experiment_id=experiment_id,
                generation_kind=generation_kind,
                request=request,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=idempotency_key,
            )
            return self._required_generation(connection, experiment_id, generation_kind)

    def terminate(
        self,
        *,
        experiment_id: str,
        expected_state_version: int,
        reason: str,
        disabled_scope: str | None,
        terminated_at_partition: int | None,
        generation_requests: Sequence[Mapping[str, object]],
        receipt_request: Mapping[str, object],
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request_payload = {
            "experiment_id": experiment_id,
            "reason": reason,
            "disabled_scope": disabled_scope,
            "terminated_at_partition": terminated_at_partition,
            "generation_request_sha256": [
                _sha256_text(compact_json(item)) for item in generation_requests
            ],
            "receipt_request_sha256": _sha256_text(compact_json(receipt_request)),
        }
        scope = f"experiment:{experiment_id}:terminate"
        with self._write() as connection:
            replay = self._command_event(connection, scope, idempotency_key)
            if replay is not None:
                self._assert_event_replay(
                    replay, request_payload, actor, occurred_at_utc
                )
                return self._required_experiment(connection, experiment_id)
            experiment = self._required_experiment(connection, experiment_id)
            if int(experiment["state_version"]) != expected_state_version:
                raise ExperimentStoreError("stale_snapshot", "experiment changed")
            if experiment["terminal_mode"] is not None:
                raise ExperimentStoreError(
                    "terminal_conflict", "experiment already has terminal intent"
                )
            self._claim_event(
                connection,
                event_type="experiment_termination_committed",
                subject_key=f"experiment:{experiment_id}:terminal",
                command_scope=scope,
                idempotency_key=idempotency_key,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload=request_payload,
                experiment_id=experiment_id,
            )
            connection.execute(
                """
                UPDATE strategy_lab_experiments SET
                    terminal_mode = 'aborted', terminal_reason = ?,
                    disabled_scope = ?, terminal_at_utc = ?,
                    terminated_at_partition = ?,
                    final_outcome_status = 'insufficient_evidence',
                    updated_at_utc = ?, state_version = state_version + 1
                WHERE experiment_id = ?
                """,
                (
                    reason,
                    disabled_scope,
                    occurred_at_utc,
                    terminated_at_partition,
                    occurred_at_utc,
                    experiment_id,
                ),
            )
            for item in generation_requests:
                self._request_generation_terminal(
                    connection,
                    experiment_id=experiment_id,
                    generation_kind=str(item["generation_kind"]),
                    request=item,
                    actor=actor,
                    occurred_at_utc=occurred_at_utc,
                    idempotency_key=f"{idempotency_key}:{item['generation_kind']}-terminal",
                    allow_experiment_terminal=True,
                )
            self._request_receipt(
                connection,
                experiment_id=experiment_id,
                request=receipt_request,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=f"{idempotency_key}:receipt",
            )
            return self._required_experiment(connection, experiment_id)

    def active_experiments(self, market: str, account: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM strategy_lab_experiments
                    WHERE market = ? AND account = ? AND terminal_mode IS NULL
                      AND phase != 'concluded'
                    ORDER BY created_at_utc, experiment_id
                    """,
                    (market, account),
                ).fetchall()
            ]

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        with self._read() as connection:
            return self._required_experiment(connection, experiment_id)

    def generations(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM strategy_lab_generations
                    WHERE experiment_id = ? ORDER BY generation_kind
                    """,
                    (experiment_id,),
                ).fetchall()
            ]

    def commitment_dates(self, experiment_id: str) -> list[str]:
        with self._read() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT trading_date FROM strategy_lab_hidden_commitments
                    WHERE experiment_id = ? ORDER BY trading_date
                    """,
                    (experiment_id,),
                ).fetchall()
            ]

    def pending_projections(
        self, *, experiment_id: str | None = None
    ) -> list[dict[str, Any]]:
        experiment_filter = "" if experiment_id is None else "AND e.experiment_id = ?"
        parameters: tuple[object, ...] = () if experiment_id is None else (experiment_id,)
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT e.* FROM strategy_lab_events e
                LEFT JOIN strategy_lab_generations g
                  ON e.event_id = g.terminal_request_event_id
                LEFT JOIN strategy_lab_experiments x
                  ON e.event_id = x.receipt_request_event_id
                WHERE e.event_type IN (
                    'generation_terminal_requested', 'experiment_receipt_requested'
                ) AND (
                    (g.terminal_request_event_id IS NOT NULL
                     AND g.terminal_published_event_id IS NULL)
                    OR
                    (x.receipt_request_event_id IS NOT NULL
                     AND x.receipt_published_event_id IS NULL)
                )
                {experiment_filter}
                ORDER BY e.rowid
                """,
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_projection_published(
        self,
        *,
        request_event_id: str,
        actor: str,
        occurred_at_utc: str,
    ) -> None:
        with self._write() as connection:
            request_event = connection.execute(
                "SELECT * FROM strategy_lab_events WHERE event_id = ?",
                (request_event_id,),
            ).fetchone()
            if request_event is None or request_event["event_type"] not in {
                "generation_terminal_requested",
                "experiment_receipt_requested",
            }:
                raise ExperimentStoreError(
                    "projection_conflict", "projection request is missing"
                )
            request = json.loads(str(request_event["payload_json"]))
            subject = f"projection:{request_event_id}:published"
            published_event_id, _ = self._claim_event(
                connection,
                event_type="terminal_projection_published",
                subject_key=subject,
                command_scope=f"projection:{request_event_id}",
                idempotency_key=request_event_id,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                payload={
                    "request_event_id": request_event_id,
                    "ref": request["ref"],
                    "content_sha256": request["content_sha256"],
                    "file_sha256": request["file_sha256"],
                },
                experiment_id=request_event["experiment_id"],
                generation_kind=request_event["generation_kind"],
            )
            if request_event["event_type"] == "generation_terminal_requested":
                generation = self._required_generation(
                    connection,
                    str(request_event["experiment_id"]),
                    str(request_event["generation_kind"]),
                )
                if generation["terminal_request_event_id"] != request_event_id:
                    raise ExperimentStoreError(
                        "projection_conflict", "generation request binding changed"
                    )
                if generation["terminal_published_event_id"] is None:
                    connection.execute(
                        """
                        UPDATE strategy_lab_generations SET
                            terminal_published_event_id = ?, terminal_published_at_utc = ?,
                            updated_at_utc = ?
                        WHERE experiment_id = ? AND generation_kind = ?
                        """,
                        (
                            published_event_id,
                            occurred_at_utc,
                            occurred_at_utc,
                            request_event["experiment_id"],
                            request_event["generation_kind"],
                        ),
                    )
                    if (
                        request_event["generation_kind"] == "research"
                        and generation["terminal_mode"] == "completed"
                    ):
                        connection.execute(
                            """
                            UPDATE strategy_lab_experiments SET
                                research_progress = 'ready_to_compare',
                                updated_at_utc = ?, state_version = state_version + 1
                            WHERE experiment_id = ? AND terminal_mode IS NULL
                            """,
                            (occurred_at_utc, request_event["experiment_id"]),
                        )
            else:
                experiment = self._required_experiment(
                    connection, str(request_event["experiment_id"])
                )
                if experiment["receipt_request_event_id"] != request_event_id:
                    raise ExperimentStoreError(
                        "projection_conflict", "receipt request binding changed"
                    )
                if experiment["receipt_published_event_id"] is None:
                    connection.execute(
                        """
                        UPDATE strategy_lab_experiments SET
                            receipt_published_event_id = ?,
                            receipt_published_at_utc = ?, updated_at_utc = ?,
                            state_version = state_version + 1
                        WHERE experiment_id = ?
                        """,
                        (
                            published_event_id,
                            occurred_at_utc,
                            occurred_at_utc,
                            request_event["experiment_id"],
                        ),
                    )
            self._maybe_conclude(connection, str(request_event["experiment_id"]), occurred_at_utc)

    def receipt_text(self, experiment_id: str) -> str | None:
        with self._read() as connection:
            experiment = self._required_experiment(connection, experiment_id)
            if not (
                experiment["phase"] == "concluded"
                and experiment["receipt_published_event_id"] is not None
                and experiment["receipt_request_event_id"] is not None
            ):
                return None
            event = connection.execute(
                "SELECT payload_json FROM strategy_lab_events WHERE event_id = ?",
                (experiment["receipt_request_event_id"],),
            ).fetchone()
            if event is None:
                raise ExperimentStoreError(
                    "projection_conflict", "receipt event is missing"
                )
            return str(json.loads(str(event[0]))["text"])

    def events(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM strategy_lab_events WHERE experiment_id = ? ORDER BY rowid",
                    (experiment_id,),
                ).fetchall()
            ]

    def _request_generation_terminal(
        self,
        connection: sqlite3.Connection,
        *,
        experiment_id: str,
        generation_kind: str,
        request: Mapping[str, object],
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
        allow_experiment_terminal: bool = False,
    ) -> str:
        experiment = self._required_experiment(connection, experiment_id)
        generation = self._required_generation(connection, experiment_id, generation_kind)
        if experiment["terminal_mode"] is not None and not allow_experiment_terminal:
            raise ExperimentStoreError("terminal_conflict", "experiment is terminal")
        existing_request_id = generation["terminal_request_event_id"]
        if existing_request_id is not None:
            event = connection.execute(
                "SELECT * FROM strategy_lab_events WHERE event_id = ?",
                (existing_request_id,),
            ).fetchone()
            if event is None or str(event["payload_json"]) != compact_json(request):
                raise ExperimentStoreError(
                    "terminal_conflict", "generation terminal already requested"
                )
            return str(existing_request_id)
        request_revision = request["revision"]
        if isinstance(request_revision, bool) or not isinstance(request_revision, int):
            raise ExperimentStoreError("stale_snapshot", "generation revision is invalid")
        if request_revision != int(generation["revision"]):
            raise ExperimentStoreError("stale_snapshot", "generation revision changed")
        if request["last_revision_ref"] != generation["last_revision_ref"]:
            raise ExperimentStoreError("stale_snapshot", "generation ref changed")
        if request["last_revision_file_sha256"] != generation["last_revision_file_sha256"]:
            raise ExperimentStoreError("stale_snapshot", "generation file hash changed")
        if request["frozen_row_content_sha256"] != generation["frozen_row_content_sha256"]:
            raise ExperimentStoreError("stale_snapshot", "generation content hash changed")
        event_id, _ = self._claim_event(
            connection,
            event_type="generation_terminal_requested",
            subject_key=f"generation:{experiment_id}:{generation_kind}:terminal",
            command_scope=f"generation:{experiment_id}:{generation_kind}:terminal",
            idempotency_key=idempotency_key,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            payload=request,
            experiment_id=experiment_id,
            generation_kind=generation_kind,
        )
        connection.execute(
            """
            UPDATE strategy_lab_generations SET
                state = 'terminal', terminal_request_event_id = ?,
                terminal_mode = ?, terminal_ref = ?,
                terminal_content_sha256 = ?, terminal_file_sha256 = ?,
                terminal_requested_at_utc = ?, updated_at_utc = ?
            WHERE experiment_id = ? AND generation_kind = ?
              AND terminal_request_event_id IS NULL
            """,
            (
                event_id,
                request["terminal_mode"],
                request["ref"],
                request["content_sha256"],
                request["file_sha256"],
                occurred_at_utc,
                occurred_at_utc,
                experiment_id,
                generation_kind,
            ),
        )
        return event_id

    def _request_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        experiment_id: str,
        request: Mapping[str, object],
        actor: str,
        occurred_at_utc: str,
        idempotency_key: str,
    ) -> str:
        experiment = self._required_experiment(connection, experiment_id)
        if experiment["receipt_request_event_id"] is not None:
            event = connection.execute(
                "SELECT payload_json FROM strategy_lab_events WHERE event_id = ?",
                (experiment["receipt_request_event_id"],),
            ).fetchone()
            if event is None or str(event[0]) != compact_json(request):
                raise ExperimentStoreError(
                    "terminal_conflict", "receipt already requested"
                )
            return str(experiment["receipt_request_event_id"])
        event_id, _ = self._claim_event(
            connection,
            event_type="experiment_receipt_requested",
            subject_key=f"experiment:{experiment_id}:receipt",
            command_scope=f"experiment:{experiment_id}:receipt",
            idempotency_key=idempotency_key,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            payload=request,
            experiment_id=experiment_id,
        )
        connection.execute(
            """
            UPDATE strategy_lab_experiments SET
                receipt_request_event_id = ?, receipt_ref = ?,
                receipt_content_sha256 = ?, receipt_file_sha256 = ?,
                updated_at_utc = ?, state_version = state_version + 1
            WHERE experiment_id = ? AND receipt_request_event_id IS NULL
            """,
            (
                event_id,
                request["ref"],
                request["content_sha256"],
                request["file_sha256"],
                occurred_at_utc,
                experiment_id,
            ),
        )
        return event_id

    def _maybe_conclude(
        self, connection: sqlite3.Connection, experiment_id: str, occurred_at_utc: str
    ) -> None:
        experiment = self._required_experiment(connection, experiment_id)
        if experiment["terminal_mode"] is None or experiment["receipt_published_event_id"] is None:
            return
        pending = connection.execute(
            """
            SELECT 1 FROM strategy_lab_generations
            WHERE experiment_id = ? AND terminal_published_event_id IS NULL
            LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
        if pending is None and experiment["phase"] != "concluded":
            connection.execute(
                """
                UPDATE strategy_lab_experiments SET
                    phase = 'concluded', updated_at_utc = ?, state_version = state_version + 1
                WHERE experiment_id = ?
                """,
                (occurred_at_utc, experiment_id),
            )

    def _claim_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        subject_key: str,
        command_scope: str,
        idempotency_key: str,
        actor: str,
        occurred_at_utc: str,
        payload: object,
        experiment_id: str | None = None,
        generation_kind: str | None = None,
    ) -> tuple[str, bool]:
        payload_json = compact_json(payload)
        replay = self._command_event(connection, command_scope, idempotency_key)
        if replay is not None:
            self._assert_event_replay(replay, payload, actor, occurred_at_utc)
            if replay["event_type"] != event_type or replay["subject_key"] != subject_key:
                raise ExperimentStoreError(
                    "idempotency_conflict", "idempotency key changed command subject"
                )
            return str(replay["event_id"]), False
        natural = connection.execute(
            """
            SELECT * FROM strategy_lab_events
            WHERE event_type = ? AND subject_key = ?
            """,
            (event_type, subject_key),
        ).fetchone()
        if natural is not None:
            if str(natural["payload_json"]) != payload_json:
                raise ExperimentStoreError(
                    "natural_fact_conflict", "natural fact has different bytes"
                )
            target_event_id = str(natural["event_id"])
            alias_key_sha256 = _sha256_text(
                f"{command_scope}\0{idempotency_key}"
            )
            alias_subject = (
                f"event:{target_event_id}:command:{alias_key_sha256}"
            )
            alias_event_id = _sha256_text(
                f"command_idempotency_bound\0{alias_subject}"
            )
            connection.execute(
                """
                INSERT INTO strategy_lab_events(
                    event_id, experiment_id, generation_kind, event_type, subject_key,
                    command_scope, idempotency_key, actor, occurred_at_utc,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, 'command_idempotency_bound', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alias_event_id,
                    natural["experiment_id"],
                    natural["generation_kind"],
                    alias_subject,
                    command_scope,
                    idempotency_key,
                    actor,
                    occurred_at_utc,
                    payload_json,
                    _sha256_text(payload_json),
                ),
            )
            return target_event_id, False
        event_id = _sha256_text(f"{event_type}\0{subject_key}")
        connection.execute(
            """
            INSERT INTO strategy_lab_events(
                event_id, experiment_id, generation_kind, event_type, subject_key,
                command_scope, idempotency_key, actor, occurred_at_utc,
                payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                experiment_id,
                generation_kind,
                event_type,
                subject_key,
                command_scope,
                idempotency_key,
                actor,
                occurred_at_utc,
                payload_json,
                _sha256_text(payload_json),
            ),
        )
        return event_id, True

    @staticmethod
    def _assert_event_replay(
        event: sqlite3.Row,
        payload: object,
        actor: str,
        occurred_at_utc: str,
    ) -> None:
        if (
            str(event["payload_json"]) != compact_json(payload)
            or event["actor"] != actor
            or event["occurred_at_utc"] != occurred_at_utc
        ):
            raise ExperimentStoreError(
                "idempotency_conflict", "idempotency key changed request bytes"
            )

    @staticmethod
    def _command_event(
        connection: sqlite3.Connection, command_scope: str, idempotency_key: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM strategy_lab_events
            WHERE command_scope = ? AND idempotency_key = ?
            """,
            (command_scope, idempotency_key),
        ).fetchone()

    @staticmethod
    def _required_experiment(
        connection: sqlite3.Connection, experiment_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM strategy_lab_experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise ExperimentStoreError("experiment_not_found", "experiment does not exist")
        return dict(row)

    @staticmethod
    def _required_generation(
        connection: sqlite3.Connection, experiment_id: str, generation_kind: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT * FROM strategy_lab_generations
            WHERE experiment_id = ? AND generation_kind = ?
            """,
            (experiment_id, generation_kind),
        ).fetchone()
        if row is None:
            raise ExperimentStoreError("generation_not_found", "generation does not exist")
        return dict(row)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_existing()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_existing()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ExperimentStoreError("constraint_conflict", "SQLite constraint failed") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            secure_sqlite_artifacts(self.path)

    def _open_existing(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise ExperimentStoreError("schema_unsupported", "store is not initialized")
        connection = connect_private_sqlite(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        self._configure(connection)
        self._validate_v2(connection)
        return connection

    def _readonly_connection(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        self._configure(connection)
        return connection

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    @staticmethod
    def _indexes(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    def _validate_v1(
        self, connection: sqlite3.Connection, *, deep: bool = False
    ) -> None:
        self._validate_schema(
            connection,
            expected_version=1,
            required_tables=_V1_REQUIRED_TABLES,
            expected_foreign_keys=_V1_EXPECTED_FOREIGN_KEYS,
            deep=deep,
        )

    def _validate_v2(
        self, connection: sqlite3.Connection, *, deep: bool = False
    ) -> None:
        self._validate_schema(
            connection,
            expected_version=SCHEMA_VERSION,
            required_tables=_REQUIRED_TABLES,
            expected_foreign_keys=_EXPECTED_FOREIGN_KEYS,
            deep=deep,
        )

    def _validate_schema(
        self,
        connection: sqlite3.Connection,
        *,
        expected_version: int,
        required_tables: set[str],
        expected_foreign_keys: Mapping[str, set[tuple[str, str, str]]],
        deep: bool,
    ) -> None:
        tables = self._tables(connection)
        if not required_tables.issubset(tables):
            raise ExperimentStoreError("schema_unsupported", "required table is missing")
        metadata = connection.execute(
            "SELECT schema_version FROM strategy_lab_schema WHERE component = ?",
            (SCHEMA_COMPONENT,),
        ).fetchone()
        if metadata is None or int(metadata[0]) != expected_version:
            raise ExperimentStoreError("schema_unsupported", "schema version is unsupported")
        if not _REQUIRED_INDEXES.issubset(self._indexes(connection)):
            raise ExperimentStoreError("schema_unsupported", "required index is missing")
        for table, expected in expected_foreign_keys.items():
            observed = {
                (str(row[3]), str(row[2]), str(row[4]))
                for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            }
            if observed != expected:
                raise ExperimentStoreError(
                    "schema_unsupported", f"foreign key layout is invalid for {table}"
                )
        if deep:
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise ExperimentStoreError("schema_unsupported", "integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ExperimentStoreError("schema_unsupported", "foreign key check failed")

    @staticmethod
    def _create_v1(connection: sqlite3.Connection) -> None:
        schema_sql = """
            CREATE TABLE strategy_lab_schema(
                component TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                migrated_at_utc TEXT NOT NULL
            );
            """
        ddl = schema_sql + """
            CREATE TABLE strategy_lab_features(
                market TEXT NOT NULL,
                account TEXT NOT NULL,
                user_opt_in INTEGER NOT NULL CHECK(user_opt_in IN (0, 1)),
                last_actor TEXT NOT NULL,
                last_occurred_at_utc TEXT NOT NULL,
                state_version INTEGER NOT NULL CHECK(state_version >= 1),
                PRIMARY KEY(market, account)
            );

            CREATE TABLE strategy_lab_experiments(
                experiment_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                market TEXT NOT NULL CHECK(market = 'HK'),
                account TEXT NOT NULL,
                strategy_family TEXT NOT NULL CHECK(strategy_family = 'sell_put'),
                spec_json TEXT NOT NULL,
                research_spec_sha256 TEXT NOT NULL,
                validation_spec_sha256 TEXT,
                source_provenance_json TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('draft','research','validation','concluded')),
                research_progress TEXT CHECK(research_progress IS NULL OR research_progress IN (
                    'building_dataset','ready_to_compare','challenger_locked'
                )),
                validation_progress TEXT CHECK(validation_progress IS NULL OR validation_progress IN (
                    'collecting_decisions','awaiting_outcomes','ready_to_conclude'
                )),
                blocked_reason TEXT,
                completed_validation_partitions INTEGER NOT NULL DEFAULT 0
                    CHECK(completed_validation_partitions BETWEEN 0 AND 20),
                research_authorization_status TEXT NOT NULL
                    CHECK(research_authorization_status IN ('unconfirmed','confirmed')),
                research_authorized_hash TEXT,
                research_authorized_actor TEXT,
                research_authorized_at_utc TEXT,
                validation_authorization_status TEXT NOT NULL
                    CHECK(validation_authorization_status IN ('unconfirmed','confirmed')),
                validation_authorized_hash TEXT,
                validation_authorized_actor TEXT,
                validation_authorized_at_utc TEXT,
                research_leader TEXT,
                research_receipt_ref TEXT,
                research_receipt_file_sha256 TEXT,
                proposed_commitment_json TEXT,
                proposed_commitment_sha256 TEXT,
                proposed_commitment_ref TEXT,
                proposed_commitment_content_sha256 TEXT,
                proposed_commitment_file_sha256 TEXT,
                terminal_mode TEXT CHECK(terminal_mode IS NULL OR terminal_mode = 'aborted'),
                terminal_reason TEXT,
                disabled_scope TEXT CHECK(disabled_scope IS NULL OR disabled_scope IN ('user','maintainer')),
                terminal_at_utc TEXT,
                terminated_at_partition INTEGER CHECK(
                    terminated_at_partition IS NULL OR terminated_at_partition BETWEEN 0 AND 20
                ),
                final_outcome_status TEXT CHECK(
                    final_outcome_status IS NULL OR final_outcome_status = 'insufficient_evidence'
                ),
                receipt_request_event_id TEXT,
                receipt_published_event_id TEXT,
                receipt_ref TEXT,
                receipt_content_sha256 TEXT,
                receipt_file_sha256 TEXT,
                receipt_published_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                state_version INTEGER NOT NULL CHECK(state_version >= 1),
                FOREIGN KEY(receipt_request_event_id) REFERENCES strategy_lab_events(event_id),
                FOREIGN KEY(receipt_published_event_id) REFERENCES strategy_lab_events(event_id)
            );

            CREATE TABLE strategy_lab_generations(
                experiment_id TEXT NOT NULL,
                generation_kind TEXT NOT NULL CHECK(generation_kind IN ('research','hidden','outcome')),
                state TEXT NOT NULL CHECK(state IN ('open','terminal')),
                revision INTEGER NOT NULL CHECK(revision >= 0),
                last_revision_ref TEXT NOT NULL,
                last_revision_file_sha256 TEXT NOT NULL,
                frozen_row_content_sha256 TEXT NOT NULL,
                terminal_request_event_id TEXT,
                terminal_published_event_id TEXT,
                terminal_mode TEXT CHECK(terminal_mode IS NULL OR terminal_mode IN ('completed','aborted')),
                terminal_ref TEXT,
                terminal_content_sha256 TEXT,
                terminal_file_sha256 TEXT,
                terminal_requested_at_utc TEXT,
                terminal_published_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                PRIMARY KEY(experiment_id, generation_kind),
                FOREIGN KEY(experiment_id) REFERENCES strategy_lab_experiments(experiment_id),
                FOREIGN KEY(terminal_request_event_id) REFERENCES strategy_lab_events(event_id),
                FOREIGN KEY(terminal_published_event_id) REFERENCES strategy_lab_events(event_id)
            );

            CREATE TABLE strategy_lab_hidden_commitments(
                experiment_id TEXT NOT NULL,
                market TEXT NOT NULL,
                account TEXT NOT NULL,
                strategy_family TEXT NOT NULL,
                commitment_sha256 TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                PRIMARY KEY(experiment_id, trading_date),
                FOREIGN KEY(experiment_id) REFERENCES strategy_lab_experiments(experiment_id)
            );

            CREATE TABLE strategy_lab_events(
                event_id TEXT PRIMARY KEY,
                experiment_id TEXT,
                generation_kind TEXT,
                event_type TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                command_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK(length(payload_json) <= 8192),
                payload_sha256 TEXT NOT NULL
            );

            CREATE UNIQUE INDEX strategy_lab_one_active_validation
            ON strategy_lab_experiments(market, account, strategy_family)
            WHERE terminal_mode IS NULL
              AND phase = 'validation'
              AND validation_progress = 'collecting_decisions';

            CREATE UNIQUE INDEX strategy_lab_hidden_date_unique
            ON strategy_lab_hidden_commitments(market, account, strategy_family, trading_date);

            CREATE UNIQUE INDEX strategy_lab_event_subject_unique
            ON strategy_lab_events(event_type, subject_key);

            CREATE UNIQUE INDEX strategy_lab_event_idempotency_unique
            ON strategy_lab_events(command_scope, idempotency_key);
            """
        for statement in ddl.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _create_v2(connection: sqlite3.Connection) -> None:
        ddl = """
            CREATE TABLE strategy_lab_corpus_days(
                market TEXT NOT NULL CHECK(market = 'HK'),
                account TEXT NOT NULL CHECK(account = lower(account)),
                trading_date TEXT NOT NULL CHECK(length(trading_date) = 10),
                expectation_ref TEXT NOT NULL,
                expectation_content_sha256 TEXT NOT NULL CHECK(length(expectation_content_sha256) = 64),
                expectation_file_sha256 TEXT NOT NULL CHECK(length(expectation_file_sha256) = 64),
                market_calendar_version TEXT NOT NULL,
                market_calendar_sha256 TEXT NOT NULL CHECK(length(market_calendar_sha256) = 64),
                schedule_config_sha256 TEXT NOT NULL CHECK(length(schedule_config_sha256) = 64),
                expected_point_count INTEGER NOT NULL CHECK(expected_point_count >= 0),
                first_target_at_utc TEXT,
                sealed_at_utc TEXT NOT NULL,
                sealed_before_first_target INTEGER NOT NULL
                    CHECK(sealed_before_first_target IN (0, 1)),
                completeness_reason TEXT CHECK(completeness_reason IS NULL OR completeness_reason IN (
                    'corpus_day_expectation_late',
                    'corpus_day_expectation_empty',
                    'research_corpus_conflict'
                )),
                conflict_status TEXT NOT NULL CHECK(conflict_status IN ('clean','conflict')),
                PRIMARY KEY(market, account, trading_date)
            );

            CREATE TABLE strategy_lab_corpus_points(
                market TEXT NOT NULL CHECK(market = 'HK'),
                account TEXT NOT NULL CHECK(account = lower(account)),
                recommendation_point_id TEXT NOT NULL CHECK(length(recommendation_point_id) = 64),
                trading_date TEXT NOT NULL CHECK(length(trading_date) = 10),
                source_run_id TEXT NOT NULL,
                source_point_ref TEXT NOT NULL,
                source_point_content_sha256 TEXT NOT NULL CHECK(length(source_point_content_sha256) = 64),
                opening_snapshot_ref TEXT NOT NULL,
                opening_snapshot_sha256 TEXT NOT NULL CHECK(length(opening_snapshot_sha256) = 64),
                ranking_projection_schema_version TEXT,
                projection_ref TEXT,
                projection_content_sha256 TEXT,
                projection_file_sha256 TEXT,
                captured_at_utc TEXT NOT NULL,
                capture_status TEXT NOT NULL CHECK(capture_status IN ('captured','not_evaluable')),
                reason_code TEXT,
                conflict_status TEXT NOT NULL CHECK(conflict_status IN ('clean','conflict')),
                PRIMARY KEY(market, account, recommendation_point_id),
                FOREIGN KEY(market, account, trading_date)
                    REFERENCES strategy_lab_corpus_days(market, account, trading_date),
                CHECK(
                    (
                        capture_status = 'captured'
                        AND reason_code IS NULL
                        AND ranking_projection_schema_version IS NOT NULL
                        AND projection_ref IS NOT NULL
                        AND projection_content_sha256 IS NOT NULL
                        AND length(projection_content_sha256) = 64
                        AND projection_file_sha256 IS NOT NULL
                        AND length(projection_file_sha256) = 64
                    )
                    OR
                    (
                        capture_status = 'not_evaluable'
                        AND reason_code IS NOT NULL
                        AND ranking_projection_schema_version IS NULL
                        AND projection_ref IS NULL
                        AND projection_content_sha256 IS NULL
                        AND projection_file_sha256 IS NULL
                    )
                )
            );
            """
        for statement in ddl.split(";"):
            if statement.strip():
                connection.execute(statement)


__all__ = [
    "ExperimentStore",
    "ExperimentStoreError",
    "SCHEMA_COMPONENT",
    "SCHEMA_VERSION",
    "compact_json",
]
