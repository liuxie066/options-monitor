from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.application.ledger import repository_core
from src.application.ledger.repository import SQLiteOptionPositionsRepository


def _repo(tmp_path: Path) -> SQLiteOptionPositionsRepository:
    return SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")


def _case(
    case_id: str,
    *,
    account: str,
    lot_id: str | None = None,
) -> dict[str, object]:
    target_lot_id = lot_id or f"lot-{case_id}"
    return {
        "case_id": case_id,
        "case_key": f"key-{case_id}",
        "account": account,
        "symbol": "NVDA",
        "status": "waiting_settlement_evidence",
        "target_lot_ids": [target_lot_id],
        "target_contracts_by_lot": {target_lot_id: 2},
    }


def _generation(
    repo: SQLiteOptionPositionsRepository,
    account: str,
) -> dict[str, object] | None:
    return repo.read_current_decision_storage_state(account)["generation"]


def _legacy_db(path: Path) -> str:
    event_json = json.dumps(
        {
            "account": "lx",
            "stock_event_id": "legacy-stock",
            "trade_time_ms": 100,
        },
        sort_keys=True,
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE assigned_stock_events (
              stock_event_id TEXT PRIMARY KEY,
              event_json TEXT NOT NULL,
              trade_time_ms INTEGER NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE trade_lifecycle_evidence (
              evidence_id TEXT PRIMARY KEY,
              case_id TEXT,
              source_type TEXT NOT NULL,
              source_event_id TEXT,
              evidence_type TEXT NOT NULL,
              account TEXT,
              symbol TEXT,
              raw_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE trade_lifecycle_evidence_revisions (
              case_id TEXT PRIMARY KEY,
              revision INTEGER NOT NULL CHECK(revision >= 0)
            );
            CREATE TRIGGER trg_trade_lifecycle_evidence_revision_insert
            AFTER INSERT ON trade_lifecycle_evidence
            WHEN NEW.case_id IS NOT NULL AND NEW.case_id != ''
            BEGIN
              INSERT INTO trade_lifecycle_evidence_revisions(case_id, revision)
              VALUES (NEW.case_id, 1)
              ON CONFLICT(case_id) DO UPDATE SET revision = revision + 1;
            END;
            """
        )
        conn.execute(
            """
            INSERT INTO assigned_stock_events (
              stock_event_id, event_json, trade_time_ms,
              created_at_ms, updated_at_ms
            ) VALUES ('legacy-stock', ?, 100, 100, 100)
            """,
            (event_json,),
        )
        conn.execute(
            """
            INSERT INTO trade_lifecycle_evidence_revisions(case_id, revision)
            VALUES ('legacy-case', 7)
            """
        )
    return event_json


def test_phase3b_schema_upgrade_is_additive_idempotent_and_does_not_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    event_json = _legacy_db(db_path)
    startup_sql: list[str] = []
    connect = repository_core.connect_private_sqlite

    def _traced_connect(path: Path) -> sqlite3.Connection:
        conn = connect(path)
        conn.set_trace_callback(startup_sql.append)
        return conn

    monkeypatch.setattr(repository_core, "connect_private_sqlite", _traced_connect)

    repo = SQLiteOptionPositionsRepository(db_path)
    initialization_sql = tuple(startup_sql)
    # The trade-event pagination schema owns a separate, explicit one-time
    # backfill. This contract remains scoped to the phase-3b current-decision
    # tables and the other historical owners below.
    history_tables = (
        "position_lots",
        "assigned_stock_events",
        "trade_lifecycle_cases",
        "trade_lifecycle_evidence",
        "trade_lifecycle_allocations",
        "trade_lifecycle_source_consumptions",
        "trade_lifecycle_timing_policies",
        "strategy_group_identities",
    )
    history_reads = [
        statement.lower()
        for statement in initialization_sql
        if statement.lstrip().lower().startswith("select ")
        and any(f" from {table}" in statement.lower() for table in history_tables)
    ]
    assert history_reads
    assert all("select 1 from" in statement and "limit 1" in statement for statement in history_reads)
    with repo._connect() as conn:  # noqa: SLF001 - schema contract
        stored = conn.execute(
            """
            SELECT account, event_json
            FROM assigned_stock_events
            WHERE stock_event_id = 'legacy-stock'
            """
        ).fetchone()
        revision = conn.execute(
            """
            SELECT revision, evidence_count
            FROM trade_lifecycle_evidence_revisions
            WHERE case_id = 'legacy-case'
            """
        ).fetchone()
        assert stored["account"] is None
        assert stored["event_json"] == event_json
        assert dict(revision) == {"revision": 7, "evidence_count": None}
        assert conn.execute("SELECT count(*) FROM current_decision_input_generations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM current_decision_projections").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trade_lifecycle_case_targets").fetchone()[0] == 0
        assert (
            conn.execute(
                """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_assigned_stock_events_account_time'
            """
            ).fetchone()
            is None
        )
        trigger_sql = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'trg_trade_lifecycle_evidence_revision_insert'
            """
        ).fetchone()[0]
        assert "evidence_count" in trigger_sql
        schema_cookie = conn.execute("PRAGMA schema_version").fetchone()[0]

    SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # noqa: SLF001 - schema contract
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_cookie


def test_case_targets_generations_assigned_stock_and_evidence_counts_are_exact(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    case = _case("case-move", account="lx", lot_id="lot-1")
    assert repo.upsert_trade_lifecycle_case(case)
    assert _generation(repo, "lx")["case_generation"] == 1  # type: ignore[index]
    assert repo.list_trade_lifecycle_case_targets_for_lots(account="lx", target_lot_ids=["lot-1"]) == [
        {
            "case_id": "case-move",
            "account": "lx",
            "target_lot_id": "lot-1",
            "target_contracts": 2,
        }
    ]

    assert not repo.upsert_trade_lifecycle_case(case)
    assert _generation(repo, "lx")["case_generation"] == 1  # type: ignore[index]

    moved_case = {**case, "account": "sy"}
    assert repo.upsert_trade_lifecycle_case(moved_case)
    assert _generation(repo, "lx")["case_generation"] == 2  # type: ignore[index]
    assert _generation(repo, "sy")["case_generation"] == 1  # type: ignore[index]
    assert not repo.list_trade_lifecycle_case_targets_for_lots(account="lx", target_lot_ids=["lot-1"])
    assert (
        repo.list_trade_lifecycle_case_targets_for_lots(account="sy", target_lot_ids=["lot-1"])[0]["target_contracts"]
        == 2
    )

    assert repo.insert_trade_lifecycle_case_once(_case("case-once", account="lx", lot_id="lot-once"))
    assert (
        repo.list_trade_lifecycle_case_targets_for_lots(account="lx", target_lot_ids=["lot-once"])[0]["case_id"]
        == "case-once"
    )

    assert repo.upsert_trade_lifecycle_case(_case("case-lx", account="lx"))
    assert repo.upsert_trade_lifecycle_case(_case("case-sy", account="sy"))
    evidence = {
        "evidence_id": "evidence-move",
        "case_id": "case-lx",
        "source_type": "test",
        "evidence_type": "settlement_observation",
        "account": "lx",
        "symbol": "NVDA",
    }
    lx_before = int(_generation(repo, "lx")["evidence_generation"])  # type: ignore[index]
    sy_before = int(_generation(repo, "sy")["evidence_generation"])  # type: ignore[index]
    assert repo.insert_trade_lifecycle_evidence_once(evidence)
    assert not repo.insert_trade_lifecycle_evidence_once(evidence)
    with repo._connect() as conn:  # noqa: SLF001 - trigger contract
        assert dict(
            conn.execute(
                """
                SELECT revision, evidence_count
                FROM trade_lifecycle_evidence_revisions
                WHERE case_id = 'case-lx'
                """
            ).fetchone()
        ) == {"revision": 1, "evidence_count": 1}
        conn.execute(
            """
            UPDATE trade_lifecycle_evidence
            SET case_id = 'case-sy'
            WHERE evidence_id = 'evidence-move'
            """
        )
        counts = {
            row["case_id"]: (row["revision"], row["evidence_count"])
            for row in conn.execute(
                """
                SELECT case_id, revision, evidence_count
                FROM trade_lifecycle_evidence_revisions
                WHERE case_id IN ('case-lx', 'case-sy')
                """
            )
        }
        assert counts == {"case-lx": (2, 0), "case-sy": (1, 1)}
        conn.execute("DELETE FROM trade_lifecycle_evidence WHERE evidence_id = 'evidence-move'")
        assert dict(
            conn.execute(
                """
                SELECT revision, evidence_count
                FROM trade_lifecycle_evidence_revisions
                WHERE case_id = 'case-sy'
                """
            ).fetchone()
        ) == {"revision": 2, "evidence_count": 0}
    assert int(_generation(repo, "lx")["evidence_generation"]) == lx_before + 2  # type: ignore[index]
    assert int(_generation(repo, "sy")["evidence_generation"]) == sy_before + 2  # type: ignore[index]

    stock_event = {
        "stock_event_id": "stock-1",
        "account": "lx",
        "event_type": "assignment",
        "trade_time_ms": 100,
    }
    assigned_before = int(_generation(repo, "lx")["assigned_stock_generation"])  # type: ignore[index]
    assert repo.upsert_assigned_stock_event(stock_event)
    assert not repo.upsert_assigned_stock_event(stock_event)
    assert repo.list_assigned_stock_events_for_account("lx") == [stock_event]
    assert int(_generation(repo, "lx")["assigned_stock_generation"]) == assigned_before + 1  # type: ignore[index]
    with repo._connect() as conn:  # noqa: SLF001 - trigger contract
        moved_json = json.dumps({**stock_event, "account": "sy"}, sort_keys=True)
        conn.execute(
            """
            UPDATE assigned_stock_events
            SET account = 'sy', event_json = ?
            WHERE stock_event_id = 'stock-1'
            """,
            (moved_json,),
        )
    assert int(_generation(repo, "lx")["assigned_stock_generation"]) == assigned_before + 2  # type: ignore[index]
    assert _generation(repo, "sy")["assigned_stock_generation"] == 1  # type: ignore[index]


def test_phase3b_storage_guards_fail_closed_without_partial_generation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    before = dict(_generation(repo, "lx") or {})

    with repo._connect() as conn:  # noqa: SLF001 - schema guard contract
        generation_sql = """
            INSERT INTO current_decision_input_generations (
              account, generation, case_generation, evidence_generation,
              allocation_generation, source_consumption_generation,
              timing_generation, combo_identity_generation,
              assigned_stock_generation, updated_at_ms
            ) VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 1)
        """
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(generation_sql, ("UPPER", 1))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(generation_sql, ("bad-type", 0.5))

        projection_values = (
            "lx",
            "current_decision_projection.v1",
            "0" * 64,
            0,
            0,
            "0" * 64,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            "0" * 64,
            "A" * 64,
            "{}",
            1,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO current_decision_projections VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                projection_values,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="decision fact is incomplete",
        ):
            conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET decision_fact_json = ?
                WHERE case_id = 'case-a'
                """,
                (json.dumps({"schema_version": "lifecycle_case_decision_fact.v1"}),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="conflicts with JSON"):
            conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET raw_json = '{}'
                WHERE case_id = 'case-a'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="account is required"):
            conn.execute(
                """
                INSERT INTO assigned_stock_events (
                  stock_event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms
                ) VALUES ('bad-stock', NULL, '{}', 1, 1, 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="conflicts with JSON"):
            conn.execute(
                """
                INSERT INTO assigned_stock_events (
                  stock_event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms
                ) VALUES ('missing-json-account', 'lx', '{}', 1, 1, 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="target account mismatch"):
            conn.execute(
                """
                INSERT INTO trade_lifecycle_case_targets (
                  case_id, account, target_lot_id, target_contracts
                ) VALUES ('case-a', 'sy', 'bad-lot', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="account is missing"):
            conn.execute(
                """
                INSERT INTO trade_lifecycle_evidence (
                  evidence_id, case_id, source_type, evidence_type,
                  raw_json, created_at_ms
                ) VALUES ('orphan', 'missing-case', 'test', 'test', '{}', 1)
                """
            )

    assert _generation(repo, "lx") == before
    with pytest.raises(ValueError, match="lowercase"):
        repo.upsert_assigned_stock_event({"stock_event_id": "bad", "account": "LX", "trade_time_ms": 1})
    with pytest.raises(ValueError, match="lowercase"):
        repo.upsert_trade_lifecycle_case(_case("bad-case", account="LX"))


def test_phase3b_trigger_allowlist_exclusions_and_index_plans(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert repo.upsert_trade_lifecycle_case(_case("case-a", account="lx"))
    assert repo.upsert_assigned_stock_event({"stock_event_id": "stock-a", "account": "lx", "trade_time_ms": 1})
    before = dict(_generation(repo, "lx") or {})
    assert repo.insert_trade_lifecycle_migration_receipt_once(
        {
            "target_key": "excluded",
            "migration_schema": "test.v1",
            "manifest_hash": "a" * 64,
            "row_hash": "b" * 64,
        }
    )
    assert _generation(repo, "lx") == before

    labels = {
        "lifecycle_case": "trade_lifecycle_cases",
        "lifecycle_evidence": "trade_lifecycle_evidence",
        "lifecycle_allocation": "trade_lifecycle_allocations",
        "lifecycle_source_consumption": "trade_lifecycle_source_consumptions",
        "lifecycle_timing": "trade_lifecycle_timing_policies",
        "combo_identity": "strategy_group_identities",
        "assigned_stock": "assigned_stock_events",
    }
    expected_generation_triggers = {
        f"trg_current_decision_{label}_{operation}": table
        for label, table in labels.items()
        for operation in ("insert", "update", "delete")
    }
    with repo._connect() as conn:  # noqa: SLF001 - plan/trigger proof
        rows = conn.execute(
            """
            SELECT name, tbl_name, sql
            FROM sqlite_master
            WHERE type = 'trigger' AND name LIKE 'trg_current_decision_%'
            """
        ).fetchall()
        by_name = {row["name"]: row for row in rows}
        assert {
            name: by_name[name]["tbl_name"] for name in expected_generation_triggers
        } == expected_generation_triggers
        assert {
            row["name"]
            for row in rows
            if "current_decision_input_generations" in str(row["sql"] or "")
        } == set(expected_generation_triggers)
        trigger_sql = "\n".join(str(row["sql"] or "").lower() for row in rows)
        assert "json_each" not in trigger_sql
        assert "insert into current_decision_projections" not in trigger_sql
        assert "update current_decision_projections" not in trigger_sql
        assert "delete from current_decision_projections" not in trigger_sql
        for name in expected_generation_triggers:
            body = str(by_name[name]["sql"] or "").lower().split("begin", 1)[1]
            assert " from trade_events" not in body
            assert " from position_lots" not in body
            assert " from trade_lifecycle_evidence " not in body
            assert " from trade_lifecycle_allocations" not in body
            assert " from trade_lifecycle_source_consumptions" not in body

        plans = {
            "assigned": conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT event_json FROM assigned_stock_events
                WHERE account = ?
                ORDER BY trade_time_ms, stock_event_id
                """,
                ("lx",),
            ).fetchall(),
            "target": conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT case_id FROM trade_lifecycle_case_targets
                WHERE account = ? AND target_lot_id IN (?)
                ORDER BY target_lot_id, case_id
                """,
                ("lx", "lot-case-a"),
            ).fetchall(),
            "case": conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT case_id FROM trade_lifecycle_cases
                WHERE account = ? AND status IN (
                  'pending', 'waiting_settlement_evidence', 'needs_review',
                  'partially_resolved', 'conflict'
                )
                ORDER BY status, updated_at_ms DESC, case_id DESC
                """,
                ("lx",),
            ).fetchall(),
            "generation": conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM current_decision_input_generations
                WHERE account = ?
                """,
                ("lx",),
            ).fetchall(),
            "projection": conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM current_decision_projections
                WHERE account = ?
                """,
                ("lx",),
            ).fetchall(),
        }
    plan_text = {name: " ".join(str(row[3]) for row in rows) for name, rows in plans.items()}
    assert "idx_assigned_stock_events_account_time" in plan_text["assigned"]
    assert "idx_trade_lifecycle_case_targets_account_lot" in plan_text["target"]
    assert "idx_trade_lifecycle_cases_account_status" in plan_text["case"]
    assert "PRIMARY KEY" in plan_text["generation"]
    assert "PRIMARY KEY" in plan_text["projection"]
