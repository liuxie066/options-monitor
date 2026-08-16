#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import tracemalloc
from typing import Any, Callable, Iterator, Mapping, Sequence
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.domain.combo_identity import build_combo_identity
import src.application.quality.service as quality_service_module
from src.application.ledger.api import (
    build_current_decision_projection,
    capture_current_decision_projection_fence,
    current_decision_projection_row,
    empty_assigned_stock_fact,
    encode_current_decision_projection,
    finalize_current_decision_projection,
    read_current_decision_projection,
    run_position_projection_in_transaction,
)
from src.application.quality.service import OMQualityService
from src.application.research import performance_baseline as baseline
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import (
    QualityControlStateRepository,
)
from src.infrastructure.quality.opend_position_adapter import OpenDOptionSnapshot


SCHEMA = "data_storage_current_decision_aggregate_benchmark.v1"
REFERENCE_HOST = "327f740925923dfe92919e74ae630d072bacc6259298e8e0a57b8060ca056aec"
SEED = 20260813
WARMUPS = 5
REPETITIONS = 30
HISTORY_BASE_ATTEMPTS = 10_000
HISTORY_10X_ATTEMPTS = 100_000
HISTORY_BASE_EVENTS = 1_000
HISTORY_10X_EVENTS = 10_000
HISTORY_BASE_IDENTITIES = 100
HISTORY_10X_IDENTITIES = 1_000
CURRENT_STATE_EVENTS = 10_000
CURRENT_STATE_LOTS = 1_000
FANOUT_ACCOUNTS = 10
FANOUT_WALL_LIMIT_NS = 250_000_000
FANOUT_CPU_LIMIT_NS = 200_000_000
FANOUT_ALLOCATION_LIMIT_BYTES = 64 * 1024 * 1024
HISTORY_DELTA_FLOOR_NS = 25_000_000
HISTORY_READ_WALL_LIMIT_NS = 200_000_000
HISTORY_READ_ALLOCATION_LIMIT_BYTES = 32 * 1024 * 1024
CURRENT_STATE_READ_WALL_LIMIT_NS = 500_000_000
CURRENT_STATE_PAYLOAD_LIMIT_BYTES = 1_048_576
CURRENT_STATE_ALLOCATION_LIMIT_BYTES = 64 * 1024 * 1024
QUALITY_WALL_LIMIT_NS = 10_000_000_000
QUALITY_CPU_OVERHEAD_RATIO = 0.25
FORBIDDEN_METHODS = (
    "list_trade_events",
    "list_position_lots",
    "list_assigned_stock_events",
    "list_assigned_stock_events_for_account",
    "list_trade_lifecycle_cases",
    "list_trade_lifecycle_evidence",
    "list_trade_lifecycle_attempt_audits",
    "list_trade_lifecycle_source_consumptions",
    "list_trade_lifecycle_allocations",
    "read_lifecycle_account_rows",
)
FORBIDDEN_SQL_TABLES = (
    "trade_events",
    "trade_lifecycle_attempt_audits",
    "trade_lifecycle_evidence",
    "trade_lifecycle_allocations",
    "trade_lifecycle_source_consumptions",
    "assigned_stock_events",
)
FORBIDDEN_QUALITY_CALLS = {
    *FORBIDDEN_METHODS,
    "preview_current_decision_projection_oracle",
    "resolve_lifecycle_account_rows",
    "project_stored_trade_events_to_position_lots",
}


def _spec(
    *, key: str, events: int, lots: int, accounts: int
) -> dict[str, Any]:
    return baseline._scenario_spec(  # noqa: SLF001 - reuse the frozen fixture owner
        key=key,
        axis=key,
        event_count=events,
        lot_count=lots,
        account_count=accounts,
        payload_bytes=baseline.MIN_PAYLOAD_BYTES,
        shape="fixed_open_lots_with_verifications",
        axis_status="evaluable",
        classification="current_decision_slice2_gate",
    )


def _ensure_generation(repo: Any, accounts: Sequence[str]) -> None:
    with repo._connect() as conn:  # noqa: SLF001 - synthetic fixture owner
        conn.executemany(
            """
            INSERT INTO current_decision_input_generations (
              account, generation, case_generation, evidence_generation,
              allocation_generation, source_consumption_generation,
              timing_generation, combo_identity_generation,
              assigned_stock_generation, updated_at_ms
            ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, 1)
            ON CONFLICT(account) DO NOTHING
            """,
            [(account,) for account in accounts],
        )


def _bootstrap(repo: Any, accounts: Sequence[str]) -> None:
    _ensure_generation(repo, accounts)
    for account in accounts:
        payload = build_current_decision_projection(
            repo,
            account=account,
            updated_at_ms=1_900_000_000_000,
            assigned_stock_after=empty_assigned_stock_fact(account),
            all_quality_case_facts=[],
        )
        repo.upsert_current_decision_projection(
            current_decision_projection_row(payload)
        )


def _move_attempt_history_to_account(repo: Any, account: str) -> None:
    with repo._connect() as conn:  # noqa: SLF001 - synthetic fixture owner
        case_row = conn.execute(
            "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id='benchmark-case'"
        ).fetchone()
        evidence_row = conn.execute(
            "SELECT raw_json FROM trade_lifecycle_evidence "
            "WHERE evidence_id='benchmark-evidence'"
        ).fetchone()
        if case_row is None or evidence_row is None:
            raise RuntimeError("lifecycle benchmark fixture is incomplete")
        case_payload = json.loads(str(case_row["raw_json"]))
        evidence_payload = json.loads(str(evidence_row["raw_json"]))
        case_payload.update(account=account, status="superseded")
        evidence_payload["account"] = account
        conn.execute(
            "UPDATE trade_lifecycle_cases SET account=?,status='superseded',"
            "raw_json=? WHERE case_id='benchmark-case'",
            (account, json.dumps(case_payload, ensure_ascii=False, sort_keys=True)),
        )
        conn.execute(
            "UPDATE trade_lifecycle_evidence SET account=?,raw_json=? "
            "WHERE evidence_id='benchmark-evidence'",
            (
                account,
                json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True),
            ),
        )


def _seed_combo_identity_history(repo: Any, account: str, count: int) -> None:
    with repo._connect() as conn:  # noqa: SLF001 - synthetic fixture owner
        event_ids = [
            str(row["event_id"])
            for row in conn.execute(
                "SELECT event_id FROM trade_events ORDER BY event_id LIMIT 2"
            )
        ]
        if len(event_ids) != 2:
            raise RuntimeError("combo identity history fixture needs two events")
        for index in range(count):
            repo.insert_strategy_group_identity(
                build_combo_identity(
                    {
                        "group_id": f"benchmark-unused-{index:04d}",
                        "strategy": "combo_yield",
                        "account": account,
                        "symbol": "NVDA",
                        "funding_put_record_id": f"unused-put-{index:04d}",
                        "funding_put_open_event_id": event_ids[0],
                        "funding_put_contract_key": {"id": f"put-{index:04d}"},
                        "participation_call_record_id": f"unused-call-{index:04d}",
                        "participation_call_open_event_id": event_ids[1],
                        "participation_call_contract_key": {
                            "id": f"call-{index:04d}"
                        },
                        "original_contracts": 1,
                    }
                ),
                conn=conn,
            )


@contextmanager
def _history_fixture(
    *, attempts: int, events: int, identities: int
) -> Iterator[dict[str, Any]]:
    account = "bench00"
    with baseline._temporary_lifecycle_attempt_fixture(  # noqa: SLF001
        prior_attempts=attempts,
        receipt_bytes=baseline.LIFECYCLE_RECEIPT_BYTES,
        seed=SEED,
    ) as context:
        repo = context["repo"]
        _move_attempt_history_to_account(repo, account)
        spec = _spec(
            key=f"current_decision.history.{events}",
            events=events,
            lots=1,
            accounts=1,
        )
        synthetic_events = baseline._build_synthetic_events(  # noqa: SLF001
            spec, seed=SEED
        )
        baseline._insert_phase_3a_events(repo, synthetic_events)  # noqa: SLF001
        inventory = baseline.build_position_projection_migration_inventory(
            repo.db_path
        )
        baseline.apply_position_projection_migration(repo.db_path, inventory)
        _seed_combo_identity_history(repo, account, identities)
        _bootstrap(repo, (account,))
        yield {
            "repo": repo,
            "account": account,
            "attempt_count": attempts,
            "event_count": events,
            "identity_count": identities,
            "fixture_sha256": hashlib.sha256(
                (
                    str(context["fixture_sha256"])
                    + baseline._events_sha256(synthetic_events)  # noqa: SLF001
                    + str(identities)
                ).encode("ascii")
            ).hexdigest(),
        }


@contextmanager
def _phase_3a_fixture(spec: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    with baseline._temporary_phase_3a_base(spec, seed=SEED) as context:  # noqa: SLF001
        accounts = tuple(
            f"bench{index:02d}"
            for index in range(int(spec["effective_dimensions"]["account_count"]))
        )
        _bootstrap(context["repo"], accounts)
        yield {**context, "accounts": accounts}


def _distribution(samples: Sequence[int]) -> dict[str, int]:
    return baseline._distribution(samples)  # noqa: SLF001 - shared gate format


def _measure(operation: Callable[[], Any], clock: Callable[[], int]) -> dict[str, int]:
    for _ in range(WARMUPS):
        start = clock()
        operation()
        _ = clock() - start
    samples = []
    for _ in range(REPETITIONS):
        start = clock()
        operation()
        samples.append(clock() - start)
    return _distribution(samples)


def _allocation(operation: Callable[[], Any]) -> int:
    tracemalloc.start()
    try:
        operation()
        _current, peak = tracemalloc.get_traced_memory()
        return int(peak)
    finally:
        tracemalloc.stop()


def _forbidden_call_count(operation: Callable[[], Any]) -> int:
    count = 0

    def profile(frame: Any, event: str, _arg: Any) -> None:
        nonlocal count
        if event == "call" and frame.f_code.co_name in FORBIDDEN_QUALITY_CALLS:
            count += 1

    sys.setprofile(profile)
    try:
        operation()
    finally:
        sys.setprofile(None)
    return count


class _StaticOpenD:
    def fetch(
        self,
        *,
        account: str,
        market: str,
        **_kwargs: Any,
    ) -> OpenDOptionSnapshot:
        return OpenDOptionSnapshot(
            account=account,
            market=market,
            environment="REAL",
            account_fingerprint="sha256:" + ("b" * 64),
            observed_at_utc="2030-03-17T00:00:00Z",
            snapshot_id=f"benchmark-{account}-{market}",
            complete=True,
            refresh_cache=True,
            rows=[],
            trading_days=[],
        )


def _quality_measurement(repo: Any, account: str) -> dict[str, Any]:
    now_ms = 1_900_000_000_000
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    root = Path(repo.db_path).parent
    config_path = root / "quality-benchmark.us.json"
    config_path.write_text("{}\n", encoding="utf-8")
    config = {
        "accounts": [account],
        "account_settings": {
            account: {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "1",
                    "trd_env": "REAL",
                },
            }
        },
    }
    runtime = {
        "config": {"config_key": "us"},
        "summary": {"ok": True},
        "ledger_store": {"sqlite_path": str(repo.db_path)},
        "trade_intake": {"holdings_sync": {"enabled": False}, "sources": []},
        "service_profile": {"loaded": True},
    }

    def service(label: str) -> OMQualityService:
        return OMQualityService(
            artifact_repository=QualityArtifactRepository(
                root / f"quality-{label}.json"
            ),
            control_repository=QualityControlStateRepository(
                root / f"quality-{label}-control.json"
            ),
            opend_adapter=_StaticOpenD(),
            runtime_status_fn=lambda *_args: {"ok": True, "data": runtime},
            now_fn=lambda: now,
            instance_id=f"benchmark-{label}",
            ledger_probe_path=repo.db_path,
        )

    baseline_service = service("legacy")
    aggregate_service = service("aggregate")
    original_reader = quality_service_module.read_current_decision_projection
    original_summary = quality_service_module.build_lifecycle_quality_migration_summary

    def refresh(active: OMQualityService) -> dict[str, Any]:
        return active.refresh(config_keys=["us"], deep=False)

    common = (
        patch.object(
            quality_service_module,
            "load_runtime_config",
            lambda **_kwargs: (config_path, config),
        ),
        patch.object(
            quality_service_module,
            "infer_runtime_config_market",
            lambda **_kwargs: "US",
        ),
        patch.object(quality_service_module, "repo_base", lambda: root),
        patch.object(
            quality_service_module,
            "quality_consumer_telemetry_snapshot",
            lambda: {
                "coverage_status": "observed",
                "entries": [
                    {
                        "consumer": "benchmark",
                        "legacy_rows_requested": True,
                    }
                ],
            },
        ),
    )
    for active in common:
        active.start()
    try:
        with patch.object(
            quality_service_module,
            "read_current_decision_projection",
            return_value={"status": "absent"},
        ):
            legacy = lambda: refresh(baseline_service)  # noqa: E731
            legacy_wall = _measure(legacy, time.perf_counter_ns)
            legacy_cpu = _measure(legacy, time.process_time_ns)

        with patch.object(
            quality_service_module,
            "read_current_decision_projection",
            original_reader,
        ):
            aggregate = lambda: refresh(aggregate_service)  # noqa: E731
            payload = aggregate()
            comparisons = payload["extensions"]["current_decision_migration"][
                "comparisons"
            ]
            if [item["status"] for item in comparisons] != ["matched"]:
                raise RuntimeError("quality benchmark fixture is not parity-matched")
            aggregate_wall = _measure(aggregate, time.perf_counter_ns)
            aggregate_cpu = _measure(aggregate, time.process_time_ns)
            aggregate_allocation = _allocation(aggregate)

        read_probes: list[dict[str, Any]] = []
        summary_forbidden_calls: list[int] = []

        def observed_reader(active_repo: Any, **kwargs: Any) -> dict[str, Any]:
            with _instrument(active_repo, trace_new_connections=True) as counters:
                result = original_reader(active_repo, **kwargs)
            read_probes.append(_finish_counters(counters))
            return result

        def observed_summary(*args: Any, **kwargs: Any) -> Any:
            result: list[Any] = []
            summary_forbidden_calls.append(
                _forbidden_call_count(
                    lambda: result.append(original_summary(*args, **kwargs))
                )
            )
            return result[0]

        with (
            patch.object(
                quality_service_module,
                "read_current_decision_projection",
                observed_reader,
            ),
            patch.object(
                quality_service_module,
                "build_lifecycle_quality_migration_summary",
                observed_summary,
            ),
        ):
            refresh(service("probe"))
    finally:
        for active in reversed(common):
            active.stop()

    read_probe = read_probes[0]
    return {
        "lot_count": CURRENT_STATE_LOTS,
        "legacy_wall_time_ns": legacy_wall,
        "legacy_cpu_time_ns": legacy_cpu,
        "aggregate_wall_time_ns": aggregate_wall,
        "aggregate_cpu_time_ns": aggregate_cpu,
        "aggregate_python_peak_bytes": aggregate_allocation,
        "new_path_probe": read_probe,
        "new_path_forbidden_call_count": (
            int(read_probe["forbidden_method_count"])
            + int(read_probe["forbidden_sql_count"])
            + int(read_probe["combo_identity_select_count"])
            + sum(summary_forbidden_calls)
        ),
    }


def _sql_forbidden(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return any(
        re.search(rf"\b(?:from|join)\s+{re.escape(table)}\b", normalized)
        for table in FORBIDDEN_SQL_TABLES
    )


def _projection_payload_select(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return (
        normalized.startswith("select")
        and "from current_decision_projections" in normalized
        and (
            "payload_json" in normalized
            or re.match(r"select\s+(?:\w+\.)?\*", normalized) is not None
        )
    )


def _combo_identity_select(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return (
        normalized.startswith("select")
        and "from strategy_group_identities" in normalized
    )


def _combo_identity_history_select(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return _combo_identity_select(statement) and re.search(
        r"\bwhere\s+account\s*=", normalized
    ) is not None


def _finish_counters(counters: dict[str, Any]) -> dict[str, Any]:
    statements = counters.pop("sql")
    counters["forbidden_sql_count"] = sum(_sql_forbidden(row) for row in statements)
    counters["projection_payload_select_count"] = sum(
        _projection_payload_select(row) for row in statements
    )
    counters["combo_identity_select_count"] = sum(
        _combo_identity_select(row) for row in statements
    )
    counters["combo_identity_history_select_count"] = sum(
        _combo_identity_history_select(row) for row in statements
    )
    return counters


@contextmanager
def _instrument(
    repo: Any,
    *,
    conn: Any | None = None,
    trace_new_connections: bool = False,
) -> Iterator[dict[str, Any]]:
    counters: dict[str, Any] = {
        "current_state_read_count": 0,
        "fence_read_count": 0,
        "projection_dml_count": 0,
        "forbidden_method_count": 0,
        "sql": [],
    }
    originals: dict[str, Any] = {}
    original_connect = repo._connect  # noqa: SLF001 - benchmark instrumentation

    def trace(statement: str) -> None:
        counters["sql"].append(statement)

    def connect():
        active = original_connect()
        active.set_trace_callback(trace)
        return active

    def counted(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if name == "read_current_decision_projection_inputs":
                counters["current_state_read_count"] += 1
            elif name == "read_current_decision_projection_fence_inputs":
                counters["fence_read_count"] += 1
            else:
                counters["projection_dml_count"] += 1
            return original(*args, **kwargs)

        return wrapper

    def forbidden(name: str) -> Callable[..., Any]:
        def wrapper(*_args: Any, **_kwargs: Any) -> Any:
            counters["forbidden_method_count"] += 1
            raise AssertionError(f"forbidden lifetime reader called: {name}")

        return wrapper

    try:
        for name in (
            "read_current_decision_projection_inputs",
            "read_current_decision_projection_fence_inputs",
            "upsert_current_decision_projection",
        ):
            original = getattr(repo, name, None)
            if not callable(original):
                continue
            originals[name] = original
            setattr(repo, name, counted(name, original))
        for name in FORBIDDEN_METHODS:
            original = getattr(repo, name, None)
            if callable(original):
                originals[name] = original
                setattr(repo, name, forbidden(name))
        if trace_new_connections:
            repo._connect = connect  # noqa: SLF001 - benchmark instrumentation
        if conn is not None:
            conn.set_trace_callback(trace)
        yield counters
    finally:
        if conn is not None:
            conn.set_trace_callback(None)
        repo._connect = original_connect  # noqa: SLF001 - benchmark instrumentation
        for name, original in originals.items():
            setattr(repo, name, original)


def _local_publication(repo: Any, account: str) -> dict[str, Any]:
    conn = repo._connect()  # noqa: SLF001 - caller-owned benchmark transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        fence = capture_current_decision_projection_fence(
            repo, accounts=(account,), conn=conn
        )
        conn.execute(
            "UPDATE current_decision_input_generations "
            "SET generation=generation+1,case_generation=case_generation+1,"
            "updated_at_ms=updated_at_ms+1 WHERE account=?",
            (account,),
        )
        return finalize_current_decision_projection(
            repo,
            fence=fence,
            updated_at_ms=1_900_000_000_001,
            conn=conn,
        )
    finally:
        conn.rollback()
        conn.close()


def _publication_probe(
    repo: Any,
    accounts: Sequence[str],
    *,
    global_change: bool,
) -> dict[str, Any]:
    before = {
        account: repo.read_current_decision_storage_state(account)["projection"]
        for account in accounts
    }
    conn = repo._connect()  # noqa: SLF001 - caller-owned benchmark transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        with _instrument(repo, conn=conn) as begin_counters:
            fence = capture_current_decision_projection_fence(
                repo, accounts=accounts, conn=conn
            )
        _finish_counters(begin_counters)
        if global_change:
            run_position_projection_in_transaction(
                repo,
                baseline._phase_3a_tail_events(count=1),  # noqa: SLF001
                conn=conn,
                mode="forced_full",
            )
        else:
            conn.execute(
                "UPDATE current_decision_input_generations "
                "SET generation=generation+1,case_generation=case_generation+1,"
                "updated_at_ms=updated_at_ms+1 WHERE account=?",
                (accounts[0],),
            )
        with _instrument(repo, conn=conn) as finish_counters:
            result = finalize_current_decision_projection(
                repo,
                fence=fence,
                updated_at_ms=1_900_000_000_001,
                conn=conn,
            )
        after = {
            account: repo.read_current_decision_storage_state(account, conn=conn)[
                "projection"
            ]
            for account in accounts
        }
        changed = sum(
            before[account]["payload_sha256"] != after[account]["payload_sha256"]
            for account in accounts
        )
        _finish_counters(finish_counters)
        counters = {
            key: int(begin_counters[key]) + int(finish_counters[key])
            for key in begin_counters
        }
        return {
            "result": result,
            "counters": counters,
            "canonical_payloads_changed": changed,
            "source_bindings_current": all(
                after[account]["built_position_source_generation"]
                == conn.execute(
                    "SELECT source_generation FROM position_projection_source_state "
                    "WHERE singleton_id=1"
                ).fetchone()[0]
                for account in accounts
            ),
        }
    finally:
        conn.rollback()
        conn.close()


def _fanout_publication(repo: Any, accounts: Sequence[str]) -> Any:
    conn = repo._connect()  # noqa: SLF001 - caller-owned benchmark transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        fence = capture_current_decision_projection_fence(
            repo, accounts=accounts, conn=conn
        )
        run_position_projection_in_transaction(
            repo,
            baseline._phase_3a_tail_events(count=1),  # noqa: SLF001
            conn=conn,
            mode="forced_full",
        )
        return finalize_current_decision_projection(
            repo,
            fence=fence,
            updated_at_ms=1_900_000_000_001,
            conn=conn,
        )
    finally:
        conn.rollback()
        conn.close()


def _duplicate_publication_probe(
    repo: Any, accounts: Sequence[str]
) -> dict[str, Any]:
    checkpoint = repo._connect()  # noqa: SLF001 - physical zero-write proof
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    before = baseline._sqlite_sizes(Path(repo.db_path))  # noqa: SLF001
    conn = repo._connect()  # noqa: SLF001 - caller-owned benchmark transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        with _instrument(repo, conn=conn) as counters:
            fence = capture_current_decision_projection_fence(
                repo, accounts=accounts, conn=conn
            )
            result = finalize_current_decision_projection(
                repo,
                fence=fence,
                updated_at_ms=1_900_000_000_001,
                conn=conn,
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "result": result,
        "counters": _finish_counters(counters),
        "physical_bytes_before": before,
        "physical_bytes_after": baseline._sqlite_sizes(  # noqa: SLF001
            Path(repo.db_path)
        ),
    }


def _read_probe(repo: Any, account: str) -> dict[str, Any]:
    with _instrument(repo, trace_new_connections=True) as counters:
        result = read_current_decision_projection(
            repo, account=account, now_ms=1_900_000_000_100
        )
    _finish_counters(counters)
    return {
        "result": {
            "status": result["status"],
            "lot_count": int(result.get("lot_count") or 0),
            "reason": result["reason"],
        },
        "counters": counters,
    }


def _schema_probe(repo: Any, account: str) -> dict[str, Any]:
    queries = {
        "position_lots": (
            "SELECT record_id FROM position_lots WHERE account=? ORDER BY record_id",
            (account,),
            "idx_position_lots_account_record",
        ),
        "assigned_stock": (
            "SELECT event_json FROM assigned_stock_events WHERE account=? "
            "ORDER BY trade_time_ms,stock_event_id",
            (account,),
            "idx_assigned_stock_events_account_time",
        ),
        "lifecycle_targets": (
            "SELECT case_id FROM trade_lifecycle_case_targets "
            "WHERE account=? AND target_lot_id IN (?) "
            "ORDER BY target_lot_id,case_id",
            (account, "missing-lot"),
            "idx_trade_lifecycle_case_targets_account_lot",
        ),
        "lifecycle_cases": (
            "SELECT case_id FROM trade_lifecycle_cases WHERE account=? "
            "AND status IN ('pending','waiting_settlement_evidence',"
            "'needs_review','partially_resolved','conflict') "
            "ORDER BY status,updated_at_ms DESC,case_id DESC",
            (account,),
            "idx_trade_lifecycle_cases_account_status",
        ),
        "combo_identities": (
            "SELECT group_id FROM strategy_group_identities WHERE account=? "
            "ORDER BY symbol,group_id",
            (account,),
            "idx_strategy_group_identities_account",
        ),
    }
    with repo._connect() as conn:  # noqa: SLF001 - formal SQL-plan evidence
        plans = {
            name: " ".join(
                str(row[3])
                for row in conn.execute(
                    f"EXPLAIN QUERY PLAN {query}",
                    params,
                )
            )
            for name, (query, params, _index) in queries.items()
        }
        triggers = [
            (str(row["name"]), str(row["sql"] or "").lower())
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_current_decision_%' ORDER BY name"
            )
        ]
    forbidden_trigger_bodies = [
        name
        for name, sql in triggers
        if "json_each" in sql
        or "insert into current_decision_projections" in sql
        or "update current_decision_projections" in sql
        or "delete from current_decision_projections" in sql
        or any(f" from {table}" in sql for table in FORBIDDEN_SQL_TABLES)
    ]
    return {
        "plans": plans,
        "required_indexes": {
            name: index for name, (_query, _params, index) in queries.items()
        },
        "all_required_indexes_used": all(
            index in plans[name]
            for name, (_query, _params, index) in queries.items()
        ),
        "trigger_count": len(triggers),
        "forbidden_trigger_bodies": forbidden_trigger_bodies,
    }


def _timed_read(repo: Any, account: str) -> dict[str, Any]:
    operation = lambda: read_current_decision_projection(  # noqa: E731
        repo, account=account, now_ms=1_900_000_000_100
    )
    wall = _measure(operation, time.perf_counter_ns)
    cpu = _measure(operation, time.process_time_ns)
    result = operation()
    if result["status"] != "trusted":
        raise RuntimeError(f"trusted read failed: {result['reason']}")
    return {
        "wall_time_ns": wall,
        "cpu_time_ns": cpu,
        "python_peak_bytes": _allocation(operation),
        "lot_count": int(result["lot_count"]),
        "payload_bytes": len(encode_current_decision_projection(result["payload"])[0].encode()),
    }


def _history_measurement(context: Mapping[str, Any]) -> dict[str, Any]:
    repo, account = context["repo"], str(context["account"])
    operation = lambda: _local_publication(repo, account)  # noqa: E731
    probe = _publication_probe(repo, (account,), global_change=False)
    return {
        "attempt_count": int(context["attempt_count"]),
        "event_count": int(context["event_count"]),
        "identity_count": int(context["identity_count"]),
        "fixture_sha256": str(context["fixture_sha256"]),
        "wall_time_ns": _measure(operation, time.perf_counter_ns),
        "cpu_time_ns": _measure(operation, time.process_time_ns),
        "probe": probe,
        "duplicate_probe": _duplicate_publication_probe(repo, (account,)),
        "trusted_read": _timed_read(repo, account),
        "read_probe": _read_probe(repo, account),
    }


def _source_sha256() -> str:
    digest = hashlib.sha256()
    for relative in (
        "domain/domain/assigned_stock.py",
        "domain/domain/decision_state_fingerprint.py",
        "domain/domain/option_lifecycle.py",
        "src/application/agent_tools/quality.py",
        "src/application/ledger/api.py",
        "src/application/ledger/commands.py",
        "src/application/ledger/combo_reconciliation.py",
        "src/application/ledger/current_decision_projection.py",
        "src/application/ledger/decision_snapshot.py",
        "src/application/ledger/lifecycle_overlay.py",
        "src/application/ledger/manual_trades.py",
        "src/application/ledger/read_only_evidence.py",
        "src/application/ledger/repository.py",
        "src/application/ledger/sqlite_row_codec.py",
        "src/application/ledger/writer.py",
        "src/application/performance/adapters.py",
        "src/application/pipeline_context.py",
        "src/application/positions/workflows.py",
        "src/application/prepared_option_positions_context.py",
        "src/application/quality/gate.py",
        "src/application/quality/lifecycle_checks.py",
        "src/application/quality/service.py",
        "src/application/trades/lifecycle.py",
        "src/application/trades/lifecycle_timing.py",
        "src/interfaces/cli/option_positions.py",
        "scripts/benchmark_current_decision_projection_slice2.py",
    ):
        data = (REPO_ROOT / relative).read_bytes()
        digest.update(relative.encode())
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def run() -> dict[str, Any]:
    host = baseline._host_profile()  # noqa: SLF001 - shared reference identity
    with _history_fixture(
        attempts=HISTORY_BASE_ATTEMPTS,
        events=HISTORY_BASE_EVENTS,
        identities=HISTORY_BASE_IDENTITIES,
    ) as small:
        baseline_history = _history_measurement(small)
    with _history_fixture(
        attempts=HISTORY_10X_ATTEMPTS,
        events=HISTORY_10X_EVENTS,
        identities=HISTORY_10X_IDENTITIES,
    ) as large:
        history_10x = _history_measurement(large)

    state_spec = _spec(
        key="current_decision.current_state_10x",
        events=CURRENT_STATE_EVENTS,
        lots=CURRENT_STATE_LOTS,
        accounts=1,
    )
    with _phase_3a_fixture(state_spec) as state:
        current_state_read = _timed_read(state["repo"], state["accounts"][0])
        current_state_probe = _read_probe(state["repo"], state["accounts"][0])
        schema_probe = _schema_probe(state["repo"], state["accounts"][0])
        quality = _quality_measurement(state["repo"], state["accounts"][0])
        current_state_fixture_sha256 = str(state["fixture_sha256"])

    fanout_spec = _spec(
        key="current_decision.account_fanout",
        events=FANOUT_ACCOUNTS,
        lots=FANOUT_ACCOUNTS,
        accounts=FANOUT_ACCOUNTS,
    )
    with _phase_3a_fixture(fanout_spec) as fanout:
        repo, accounts = fanout["repo"], fanout["accounts"]
        operation = lambda: _fanout_publication(repo, accounts)  # noqa: E731
        fanout_result = {
            "account_count": len(accounts),
            "fixture_sha256": str(fanout["fixture_sha256"]),
            "wall_time_ns": _measure(operation, time.perf_counter_ns),
            "cpu_time_ns": _measure(operation, time.process_time_ns),
            "python_peak_bytes": _allocation(operation),
            "probe": _publication_probe(
                repo, accounts, global_change=True
            ),
        }
    baseline_wall = int(baseline_history["wall_time_ns"]["p95"])
    baseline_cpu = int(baseline_history["cpu_time_ns"]["p95"])
    history_wall = int(history_10x["wall_time_ns"]["p95"])
    history_cpu = int(history_10x["cpu_time_ns"]["p95"])
    wall_budget = max(HISTORY_DELTA_FLOOR_NS, int(baseline_wall * 0.15))
    cpu_budget = max(HISTORY_DELTA_FLOOR_NS, int(baseline_cpu * 0.15))
    quality_cpu_baseline = int(quality["legacy_cpu_time_ns"]["p95"])
    quality_cpu_budget = int(quality_cpu_baseline * QUALITY_CPU_OVERHEAD_RATIO)
    quality_cpu_overhead = max(
        0,
        int(quality["aggregate_cpu_time_ns"]["p95"]) - quality_cpu_baseline,
    )

    def zero_probe(
        probe: Mapping[str, Any],
        *,
        reads: int,
        dml: int,
        fence_reads: int = 0,
        payload_selects: int | None = None,
        identity_selects: int | None = None,
        identity_history_selects: int | None = None,
    ) -> bool:
        counters = probe["counters"]
        return all(
            (
                counters["current_state_read_count"] == reads,
                counters["fence_read_count"] == fence_reads,
                counters["projection_dml_count"] == dml,
                counters["forbidden_method_count"] == 0,
                counters["forbidden_sql_count"] == 0,
                payload_selects is None
                or counters["projection_payload_select_count"] == payload_selects,
                identity_selects is None
                or counters["combo_identity_select_count"] == identity_selects,
                identity_history_selects is None
                or counters["combo_identity_history_select_count"]
                == identity_history_selects,
            )
        )

    checks = {
        "reference_host_comparable": host["fingerprint"] == REFERENCE_HOST,
        "history_dimensions_frozen": (
            history_10x["attempt_count"] == HISTORY_10X_ATTEMPTS
            and history_10x["event_count"] == HISTORY_10X_EVENTS
            and history_10x["identity_count"] == HISTORY_10X_IDENTITIES
            and baseline_history["identity_count"] == HISTORY_BASE_IDENTITIES
        ),
        "history_publication_wall_delta": history_wall - baseline_wall <= wall_budget,
        "history_publication_cpu_delta": history_cpu - baseline_cpu <= cpu_budget,
        "history_publication_one_read_one_dml": zero_probe(
            history_10x["probe"],
            reads=1,
            dml=1,
            fence_reads=2,
            payload_selects=1,
            identity_selects=0,
            identity_history_selects=0,
        ),
        "duplicate_fence_metadata_only_zero_write": (
            zero_probe(
                history_10x["duplicate_probe"],
                reads=0,
                dml=0,
                fence_reads=2,
                payload_selects=0,
            )
            and set(
                history_10x["duplicate_probe"]["result"]["statuses"].values()
            )
            == {"not_required"}
            and history_10x["duplicate_probe"]["physical_bytes_before"]
            == history_10x["duplicate_probe"]["physical_bytes_after"]
        ),
        "history_read_wall": (
            history_10x["trusted_read"]["wall_time_ns"]["p95"]
            <= HISTORY_READ_WALL_LIMIT_NS
        ),
        "history_read_allocation": (
            history_10x["trusted_read"]["python_peak_bytes"]
            <= HISTORY_READ_ALLOCATION_LIMIT_BYTES
        ),
        "history_read_forbidden_work_zero": zero_probe(
            history_10x["read_probe"], reads=1, dml=0, identity_selects=0
        ),
        "current_state_dimensions_frozen": (
            current_state_read["lot_count"] == CURRENT_STATE_LOTS
        ),
        "current_state_read_wall": (
            current_state_read["wall_time_ns"]["p95"]
            <= CURRENT_STATE_READ_WALL_LIMIT_NS
        ),
        "current_state_payload_size": (
            current_state_read["payload_bytes"] < CURRENT_STATE_PAYLOAD_LIMIT_BYTES
        ),
        "current_state_read_allocation": (
            current_state_read["python_peak_bytes"]
            <= CURRENT_STATE_ALLOCATION_LIMIT_BYTES
        ),
        "current_state_read_forbidden_work_zero": zero_probe(
            current_state_probe, reads=1, dml=0, identity_selects=0
        ),
        "current_state_query_plans_indexed": schema_probe[
            "all_required_indexes_used"
        ],
        "current_state_trigger_bodies_bounded": (
            schema_probe["trigger_count"] > 0
            and not schema_probe["forbidden_trigger_bodies"]
        ),
        "fanout_dimensions_frozen": fanout_result["account_count"] >= FANOUT_ACCOUNTS,
        "fanout_wall": fanout_result["wall_time_ns"]["p95"] <= FANOUT_WALL_LIMIT_NS,
        "fanout_cpu": fanout_result["cpu_time_ns"]["p95"] <= FANOUT_CPU_LIMIT_NS,
        "fanout_allocation": (
            fanout_result["python_peak_bytes"] <= FANOUT_ALLOCATION_LIMIT_BYTES
        ),
        "fanout_one_read_one_dml_per_account": zero_probe(
            fanout_result["probe"],
            reads=FANOUT_ACCOUNTS,
            dml=FANOUT_ACCOUNTS,
            fence_reads=2,
            payload_selects=FANOUT_ACCOUNTS,
            identity_history_selects=0,
        ),
        "global_change_updates_every_payload": (
            fanout_result["probe"]["canonical_payloads_changed"] == FANOUT_ACCOUNTS
            and fanout_result["probe"]["source_bindings_current"]
        ),
        "ordinary_quality_wall": (
            quality["aggregate_wall_time_ns"]["p95"] < QUALITY_WALL_LIMIT_NS
        ),
        "ordinary_quality_cpu_overhead": (
            quality_cpu_overhead <= quality_cpu_budget
        ),
        "ordinary_quality_forbidden_work_zero": (
            quality["new_path_forbidden_call_count"] == 0
        ),
    }
    return {
        "schema_version": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": baseline._git_sha(REPO_ROOT),  # noqa: SLF001
        "source_sha256": _source_sha256(),
        "fixture_seed": SEED,
        "host": host,
        "reference_host_fingerprint": REFERENCE_HOST,
        "run_label": "acceptance_5_warmups_30_repetitions",
        "measurement_contract": {
            "warmups": WARMUPS,
            "repetitions": REPETITIONS,
            "wall_and_cpu_separate": True,
            "tracemalloc_separate": True,
            "setup_included": False,
            "warm_state": "warm_os_page_cache_not_flushed",
        },
        "limits": {
            "history_publication_delta_wall_ns": wall_budget,
            "history_publication_delta_cpu_ns": cpu_budget,
            "history_read_wall_ns": HISTORY_READ_WALL_LIMIT_NS,
            "history_read_allocation_bytes": HISTORY_READ_ALLOCATION_LIMIT_BYTES,
            "current_state_read_wall_ns": CURRENT_STATE_READ_WALL_LIMIT_NS,
            "current_state_payload_bytes": CURRENT_STATE_PAYLOAD_LIMIT_BYTES,
            "current_state_allocation_bytes": CURRENT_STATE_ALLOCATION_LIMIT_BYTES,
            "fanout_wall_ns": FANOUT_WALL_LIMIT_NS,
            "fanout_cpu_ns": FANOUT_CPU_LIMIT_NS,
            "fanout_allocation_bytes": FANOUT_ALLOCATION_LIMIT_BYTES,
            "ordinary_quality_wall_ns": QUALITY_WALL_LIMIT_NS,
            "ordinary_quality_cpu_overhead_ns": quality_cpu_budget,
            "ordinary_quality_cpu_overhead_ratio": QUALITY_CPU_OVERHEAD_RATIO,
        },
        "history_baseline": baseline_history,
        "history_10x": history_10x,
        "current_state_10x": {
            "fixture_sha256": current_state_fixture_sha256,
            "read": current_state_read,
            "probe": current_state_probe,
            "schema_probe": schema_probe,
        },
        "account_fanout": fanout_result,
        "ordinary_quality": {
            **quality,
            "cpu_overhead_ns": quality_cpu_overhead,
        },
        "checks": checks,
        "status": "pass" if all(checks.values()) else "fail",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Phase 3B aggregate current-decision projection gate."
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = run()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    print(json.dumps({"status": artifact["status"], "output": str(output)}))
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
