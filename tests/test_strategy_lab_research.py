from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.strategy_lab.contracts import strict_json_bytes
from src.application.strategy_lab.receipts import build_research_receipt, publish_receipt
from src.application.strategy_lab.service import (
    StrategyLabServiceError,
    confirm_research,
    confirm_validation,
    execute_research,
    get_experiment_status,
    preview_validation,
    read_receipt,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore


NOW = "2026-08-30T12:00:00Z"


def _context(tmp_path: Path) -> dict[str, object]:
    return {
        "repo_root": tmp_path,
        "store_path": tmp_path / "experiments.sqlite3",
        "artifact_root": tmp_path / "artifacts",
        "config_hk": tmp_path / "config.hk.json",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
        "opend_limiter_root": tmp_path,
        "tick_markets": ("hk",),
        "tick_lock_paths": (tmp_path / "tick.lock",),
    }


def _spec() -> dict[str, object]:
    manifest = [{"path": "owner.py", "sha256": "a" * 64}]
    return {
        "source_commit_sha": "b" * 40,
        "behavior_manifest": manifest,
        "evaluator_behavior_sha256": canonical_sha256(manifest),
        "history_k_authority": {"probe_request": {"opend_binding": {"host": "127.0.0.1", "port": 11111}}},
        "research_window": {
            "selected_trading_dates": [f"2026-08-{day:02d}" for day in range(1, 21)],
            "sessions": [{"trading_date": f"2026-08-{day:02d}", "points": []} for day in range(1, 21)],
        },
    }


def _provider_query() -> dict[str, object]:
    return {
        "provider_source": {
            "provider": "futu_opend",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
            "source_authority_sha256": "d" * 64,
        }
    }


def _create(context: dict[str, object]) -> dict[str, object]:
    spec = _spec()
    store = ExperimentStore(context["store_path"])
    store.initialize()
    return store.create_experiment(
        experiment_id="experiment-1",
        spec=spec,
        spec_sha256=canonical_sha256(spec),
        source_commit_sha=spec["source_commit_sha"],
        behavior_manifest=spec["behavior_manifest"],
        evaluator_behavior_sha256=spec["evaluator_behavior_sha256"],
        confirmation_sha256="c" * 64,
        idempotency_key="confirm-1",
        actor="tester",
        occurred_at_utc=NOW,
    )


def _awaiting_validation(context: dict[str, object]) -> dict[str, object]:
    created = _create(context)
    store = ExperimentStore(context["store_path"])
    complete = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=created["revision"],
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc=NOW,
    )
    leader = {
        "variant_id": "challenger_0.002",
        "near_return_threshold": 0.002,
        "comparison_sha256": "e" * 64,
    }
    return store.attach_research_receipt_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=complete["revision"],
        new_state="awaiting_validation_confirmation",
        receipt_ref="experiments/experiment-1/receipts/research.json",
        receipt_sha256="f" * 64,
        leader=leader,
        actor="tester",
        occurred_at_utc=NOW,
        payload={"status": "leader"},
        idempotency_key="conclude-1",
    )


def _patch_behavior(monkeypatch: pytest.MonkeyPatch, *, matches: bool = True) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(service, "build_evaluator_behavior_manifest", lambda _root: ["current"])
    monkeypatch.setattr(
        service,
        "evaluator_behavior_sha256",
        lambda _manifest: canonical_sha256(_spec()["behavior_manifest"]) if matches else "f" * 64,
    )


def test_confirm_rebuilds_exact_preview_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    spec = _spec()
    preview = {
        "status": "available",
        "blockers": [],
        "spec": spec,
        "spec_sha256": canonical_sha256(spec),
        "preview_sha256": "d" * 64,
    }
    calls: list[str] = []
    monkeypatch.setattr(
        service,
        "preview_experiment",
        lambda _context, _request, *, occurred_at_utc: calls.append(occurred_at_utc) or preview,
    )
    command = dict(
        confirmed_preview_sha256="d" * 64,
        actor="tester",
        idempotency_key="confirm-1",
        occurred_at_utc=NOW,
    )

    first = confirm_research(context, {"request": "rebuilt"}, **command)
    second = confirm_research(context, {"request": "rebuilt"}, **command)

    assert first == second
    assert first["experiment"]["state"] == "research_running"
    assert calls == [NOW, NOW]


def test_validation_preview_hash_excludes_time_and_confirmation_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    awaiting = _awaiting_validation(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "c" * 40)
    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda **_kwargs: (
            Path(context["config_hk"]),
            {
                "schedule_hk": {
                    "enabled": True,
                    "timezone": "Asia/Hong_Kong",
                    "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
                }
            },
        ),
    )
    receipt = {
        "experiment_id": "experiment-1",
        "spec_sha256": awaiting["spec_sha256"],
        "conclusion": {"status": "leader", "leader": awaiting["leader"]},
    }
    monkeypatch.setattr(service, "read_receipt", lambda *_args, **_kwargs: {"receipt": receipt})

    def plan(*_args: object, requested_start: str, **_kwargs: object) -> dict[str, object]:
        return {
            "experiment_id": "experiment-1",
            "requested_start": requested_start,
            "market_calendar": {
                "sessions": [
                    {
                        "expected_recommendation_point_ids": ["point-1"],
                    }
                ]
            },
        }

    monkeypatch.setattr(service, "build_validation_plan", plan)
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("validation confirmation must not build a gateway"),
    )
    store = ExperimentStore(context["store_path"])
    before = store.get_experiment("experiment-1")

    first = preview_validation(
        context,
        "experiment-1",
        "2026-09-01",
        occurred_at_utc="2026-08-31T00:00:00Z",
    )
    second = preview_validation(
        context,
        "experiment-1",
        "2026-09-01",
        occurred_at_utc="2026-08-31T01:00:00Z",
    )

    assert first["preview_sha256"] == second["preview_sha256"]
    assert first["validation_plan_sha256"] == second["validation_plan_sha256"]
    assert first["occurred_at_utc"] != second["occurred_at_utc"]
    assert store.get_experiment("experiment-1") == before

    command = {
        "confirmed_preview_sha256": first["preview_sha256"],
        "actor": "tester",
        "idempotency_key": "validation-confirm-1",
        "occurred_at_utc": "2026-08-31T00:00:00Z",
    }
    confirmed = confirm_validation(context, "experiment-1", "2026-09-01", **command)
    retried = confirm_validation(
        context,
        "experiment-1",
        "2026-09-01",
        **{**command, "occurred_at_utc": "2026-09-02T00:00:00Z"},
    )

    assert retried == confirmed
    assert confirmed["experiment"]["state"] == "validation_collecting"
    events = store.list_events("experiment-1")
    assert [event["event_type"] for event in events][-2:] == [
        "source_commit_observed",
        "validation_confirmed",
    ]
    assert events[-1]["occurred_at_utc"] == "2026-08-31T00:00:00Z"
    assert events[-1]["confirmation_sha256"] == first["preview_sha256"]

    status = get_experiment_status(context, "experiment-1")
    assert status["progress"]["validation_sessions"] == {"total": 1}
    assert status["next_action"]["action"] == "collect_validation_evidence"

    with pytest.raises(StrategyLabServiceError) as changed_retry:
        confirm_validation(
            context,
            "experiment-1",
            "2026-09-01",
            **{**command, "idempotency_key": "validation-confirm-2"},
        )
    assert changed_retry.value.reason_code == "idempotency_conflict"

    with sqlite3.connect(context["store_path"]) as connection:
        connection.execute(
            "UPDATE experiments SET state = 'waiting_outcome' WHERE experiment_id = ?",
            ("experiment-1",),
        )
    later_retry = confirm_validation(
        context,
        "experiment-1",
        "2026-09-01",
        **{**command, "occurred_at_utc": "2026-09-21T00:00:00Z"},
    )
    assert later_retry["experiment"]["state"] == "waiting_outcome"

    with sqlite3.connect(context["store_path"]) as connection:
        connection.execute(
            "UPDATE experiments SET validation_plan_json = ? WHERE experiment_id = ?",
            (json.dumps({"changed": True}), "experiment-1"),
        )
    drifted = get_experiment_status(context, "experiment-1")
    assert drifted["progress"] is None
    assert drifted["blocker"]["reason_code"] == "validation_plan_invalid"
    assert drifted["next_action"] == {
        "action": "inspect_validation_plan",
        "provider_required": False,
    }


def test_status_missing_store_is_read_only(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with pytest.raises(StrategyLabServiceError) as raised:
        get_experiment_status(context, "experiment-1")
    assert raised.value.reason_code == "experiment_store_not_found"
    assert not Path(context["store_path"]).exists()


def test_status_reports_deterministic_progress_and_next_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service
    import src.infrastructure.strategy_lab.experiment_store as store_module

    context = _context(tmp_path)
    _create(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(
        service,
        "load_research_projection",
        lambda _spec: {
            "history_k_queries": [{}, {}],
            "expiry_close_queries": [{}],
            "arms": [
                {
                    "research_fill_key": "fill-1",
                    "expiry_close_query_sha256": "a" * 64,
                },
                {
                    "research_fill_key": "fill-2",
                    "expiry_close_query_sha256": "a" * 64,
                },
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "next_missing_research_evidence",
        lambda *_args: {
            "action": "collect_history_k",
            "observation_key": "history_k_query:" + "b" * 64,
        },
    )
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("status must not construct a provider gateway"),
    )
    monkeypatch.setattr(
        store_module,
        "connect_private_sqlite",
        lambda *_args, **_kwargs: pytest.fail("status must not use the write connector"),
    )
    monkeypatch.setattr(
        store_module,
        "secure_sqlite_artifacts",
        lambda *_args, **_kwargs: pytest.fail("status must not repair Store files"),
    )

    result = get_experiment_status(context, "experiment-1")

    assert result["progress"] == {
        "history_k_queries": {"completed": 0, "total": 2},
        "research_fills": {"completed": 0, "total": 2},
        "expiry_close_queries": {"completed": 0, "total_required": 0},
        "single_results": {"completed": 0, "total": 2},
    }
    assert result["blocker"] is None
    assert result["next_action"] == {
        "action": "collect_history_k",
        "observation_key": "history_k_query:" + "b" * 64,
        "provider_required": True,
        "provider_admission_checked": False,
    }


def test_status_blocks_changed_evaluator_without_provider_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    before = _create(context)
    _patch_behavior(monkeypatch, matches=False)
    monkeypatch.setattr(
        service,
        "load_research_projection",
        lambda _spec: pytest.fail("changed evaluator must not interpret the frozen spec"),
    )
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("status must not construct a provider gateway"),
    )

    result = get_experiment_status(context, "experiment-1")

    assert result["experiment"] == service._experiment_view(before)
    assert result["observation_count"] == 0
    assert result["progress"] is None
    assert result["blocker"]["reason_code"] == "evaluator_behavior_mismatch"
    assert result["next_action"] == {
        "action": "restore_evaluator_behavior",
        "provider_required": False,
    }
    assert ExperimentStore(context["store_path"]).get_experiment("experiment-1") == before


def test_behavior_mismatch_precedes_provider_and_state_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    before = _create(context)
    _patch_behavior(monkeypatch, matches=False)
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("behavior mismatch must precede gateway construction"),
    )

    with pytest.raises(StrategyLabServiceError) as raised:
        execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)

    assert raised.value.reason_code == "evaluator_behavior_mismatch"
    assert ExperimentStore(context["store_path"]).get_experiment("experiment-1") == before


def test_execute_consumes_at_most_one_provider_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    _create(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "c" * 40)
    monkeypatch.setattr(service, "_provider_guard", lambda *_args: None)
    monkeypatch.setattr(service, "load_runtime_config", lambda **_kwargs: (Path("config"), {}))
    monkeypatch.setattr(
        service,
        "resolve_opend_fetch_limits",
        lambda _config: SimpleNamespace(history_kline=SimpleNamespace(window_sec=30, max_calls=20)),
    )

    @contextmanager
    def lock(*_args: object, **_kwargs: object):
        yield

    monkeypatch.setattr(service, "exclusive_private_file_lock", lock)
    gateway = SimpleNamespace(close=lambda: None)
    gateway_calls: list[int] = []
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: gateway_calls.append(1) or gateway,
    )
    monkeypatch.setattr(
        service,
        "collect_research_fill_evidence",
        lambda *_args, **_kwargs: {"status": "available", "bars": []},
    )

    def publish(*_args: object, **_kwargs: object) -> dict[str, object]:
        with sqlite3.connect(context["store_path"]) as connection:
            payload = connection.execute(
                "SELECT payload_json FROM experiment_events WHERE event_type = 'source_commit_observed'"
            ).fetchone()
        assert payload is None
        return {}

    monkeypatch.setattr(service, "publish_evidence_artifact", publish)
    collect = {
        "action": "collect_history_k",
        "query_sha256": "a" * 64,
        "observation_key": "history_k_query:" + "a" * 64,
        "kind": "history_k_query",
        "artifact_kind": "history_k",
        "query": _provider_query(),
        "lock_path": str(tmp_path / "query.lock"),
    }
    bind = {
        **collect,
        "action": "bind_artifact",
        "artifact": {
            "payload": {"status": "available", "bars": []},
            "artifact_ref": "evidence/history.json",
            "artifact_sha256": "e" * 64,
            "artifact": {"producer_source_commit_sha": "c" * 40},
        },
    }
    second = {**collect, "query_sha256": "f" * 64, "observation_key": "history_k_query:" + "f" * 64}
    actions = iter([collect, collect, bind, second])
    monkeypatch.setattr(
        service,
        "next_missing_research_evidence",
        lambda *_args, **_kwargs: next(actions),
    )

    result = execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)

    assert result["status"] == "progress"
    assert result["provider_logical_units"] == 1
    assert gateway_calls == [1]
    assert len(ExperimentStore(context["store_path"]).list_observations("experiment-1")) == 1
    with sqlite3.connect(context["store_path"]) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM experiment_events WHERE event_type = 'source_commit_observed'"
        ).fetchone()
    assert payload is not None
    assert json.loads(payload[0])["payload"] == {"source_commit_sha": "c" * 40}


def test_execute_timeout_closes_gateway_without_publishing_or_store_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    _create(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(service, "_provider_guard", lambda *_args: None)
    monkeypatch.setattr(service, "load_runtime_config", lambda **_kwargs: (Path("config"), {}))
    monkeypatch.setattr(
        service,
        "resolve_opend_fetch_limits",
        lambda _config: SimpleNamespace(history_kline=SimpleNamespace(window_sec=30, max_calls=20)),
    )
    monkeypatch.setattr(service, "INTERRUPTIBLE_OPEND_UNIT_TIMEOUT_SECONDS", 0.05)
    gateway = SimpleNamespace(closed=False)

    def close() -> None:
        gateway.closed = True

    gateway.close = close
    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: gateway)
    monkeypatch.setattr(
        service,
        "collect_research_fill_evidence",
        lambda *_args, **_kwargs: time.sleep(1.0),
    )
    monkeypatch.setattr(
        service,
        "publish_evidence_artifact",
        lambda *_args, **_kwargs: pytest.fail("timed-out evidence must not be published"),
    )
    action = {
        "action": "collect_history_k",
        "query_sha256": "a" * 64,
        "observation_key": "history_k_query:" + "a" * 64,
        "kind": "history_k_query",
        "artifact_kind": "history_k",
        "query": _provider_query(),
        "lock_path": str(tmp_path / "query.lock"),
    }
    monkeypatch.setattr(service, "next_missing_research_evidence", lambda *_args: action)

    result = execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)

    assert result["reason_code"] == "opend_low_priority_timeout"
    assert gateway.closed is True
    store = ExperimentStore(context["store_path"])
    assert store.list_observations("experiment-1") == []
    assert store.get_experiment("experiment-1")["revision"] == 0


def test_tick_blocker_constructs_no_gateway_or_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    _create(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "c" * 40)
    monkeypatch.setattr(service, "_provider_guard", lambda *_args: "tick_busy")
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("Tick blocker must precede gateway construction"),
    )
    action = {
        "action": "collect_history_k",
        "query_sha256": "a" * 64,
        "observation_key": "history_k_query:" + "a" * 64,
        "kind": "history_k_query",
        "artifact_kind": "history_k",
        "query": _provider_query(),
        "lock_path": str(tmp_path / "query.lock"),
    }
    monkeypatch.setattr(service, "next_missing_research_evidence", lambda *_args: action)

    result = execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)

    assert result["reason_code"] == "tick_busy"
    assert ExperimentStore(context["store_path"]).list_observations("experiment-1") == []
    assert ExperimentStore(context["store_path"]).get_experiment("experiment-1")["revision"] == 0


def test_provider_binding_drift_blocks_before_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    _create(context)
    context["opend_binding"] = {"host": "127.0.0.1", "port": 22222}
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(service, "_provider_guard", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("provider drift must precede gateway construction"),
    )
    action = {
        "action": "collect_history_k",
        "query_sha256": "a" * 64,
        "observation_key": "history_k_query:" + "a" * 64,
        "kind": "history_k_query",
        "artifact_kind": "history_k",
        "query": _provider_query(),
        "lock_path": str(tmp_path / "query.lock"),
    }
    monkeypatch.setattr(service, "next_missing_research_evidence", lambda *_args: action)

    result = execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)

    assert result["reason_code"] == "research_provider_binding_mismatch"
    assert ExperimentStore(context["store_path"]).list_observations("experiment-1") == []


def test_concurrent_execute_uses_one_provider_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    _create(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(service, "_provider_guard", lambda *_args: None)
    monkeypatch.setattr(service, "load_runtime_config", lambda **_kwargs: (Path("config"), {}))
    monkeypatch.setattr(
        service,
        "resolve_opend_fetch_limits",
        lambda _config: SimpleNamespace(history_kline=SimpleNamespace(window_sec=30, max_calls=20)),
    )
    gateway = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: gateway)
    published = threading.Event()
    concurrent_results: list[dict[str, object]] = []
    concurrent_errors: list[BaseException] = []
    provider_calls = 0

    def collect(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1

        def run_concurrent() -> None:
            try:
                concurrent_results.append(
                    execute_research(context, "experiment-1", actor="second", occurred_at_utc=NOW)
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                concurrent_errors.append(exc)

        thread = threading.Thread(target=run_concurrent)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        return {"status": "available", "bars": []}

    monkeypatch.setattr(service, "collect_research_fill_evidence", collect)
    monkeypatch.setattr(
        service,
        "publish_evidence_artifact",
        lambda *_args, **_kwargs: published.set() or {},
    )
    collect_action = {
        "action": "collect_history_k",
        "query_sha256": "a" * 64,
        "observation_key": "history_k_query:" + "a" * 64,
        "kind": "history_k_query",
        "artifact_kind": "history_k",
        "query": _provider_query(),
        "lock_path": str(tmp_path / "query.lock"),
    }
    bind_action = {
        **collect_action,
        "action": "bind_artifact",
        "artifact": {
            "payload": {"status": "available", "bars": []},
            "artifact_ref": "evidence/history.json",
            "artifact_sha256": "e" * 64,
            "artifact": {"producer_source_commit_sha": "b" * 40},
        },
    }
    next_query = {
        **collect_action,
        "query_sha256": "f" * 64,
        "observation_key": "history_k_query:" + "f" * 64,
    }

    def next_action(_spec: object, observations: list[dict[str, object]], _root: object) -> dict[str, object]:
        if observations:
            return next_query
        return bind_action if published.is_set() else collect_action

    monkeypatch.setattr(service, "next_missing_research_evidence", next_action)

    first_result = execute_research(context, "experiment-1", actor="first", occurred_at_utc=NOW)

    assert concurrent_errors == []
    assert concurrent_results[0]["reason_code"] == "research_evidence_busy"
    assert first_result["provider_logical_units"] == 1
    assert provider_calls == 1


def test_concurrent_complete_execute_converges_on_one_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    _create(context)
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(service, "_comparisons", lambda *_args: [])
    barrier = threading.Barrier(2)

    def complete(*_args: object) -> dict[str, object]:
        barrier.wait(timeout=2)
        return {"action": "complete", "projection": {}}

    monkeypatch.setattr(service, "next_missing_research_evidence", complete)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda actor: execute_research(context, "experiment-1", actor=actor, occurred_at_utc=NOW),
                ("first", "second"),
            )
        )

    assert [result["status"] for result in results] == ["complete", "complete"]
    refs = {result["experiment"]["research_receipt_ref"] for result in results}
    hashes = {result["experiment"]["research_receipt_sha256"] for result in results}
    assert len(refs) == len(hashes) == 1
    store = ExperimentStore(context["store_path"])
    assert store.get_experiment("experiment-1")["state"] == "completed"
    with sqlite3.connect(context["store_path"]) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM experiment_events WHERE event_type = 'research_materialized'"
            ).fetchone()[0]
            == 1
        )


def test_research_complete_publishes_and_attaches_leader_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service
    import src.infrastructure.strategy_lab.experiment_store as store_module

    context = _context(tmp_path)
    created = _create(context)
    store = ExperimentStore(context["store_path"])
    complete = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=created["revision"],
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={"observation_count": 0},
        occurred_at_utc=NOW,
    )
    _patch_behavior(monkeypatch)
    comparison = {
        "status": "complete",
        "reason_code": None,
        "variant_id": "challenger_0.002",
        "near_return_threshold": 0.002,
        "expected_point_count": 20,
        "effective_point_count": 20,
        "top1_change_count": 10,
        "daily_aggregates": [],
        "mean_daily_annualized_return_delta": 0.01,
        "mean_daily_pnl_delta_cny": 1.0,
        "passed": True,
    }
    monkeypatch.setattr(service, "_comparisons", lambda *_args: [comparison])

    result = execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)

    assert result["status"] == "complete"
    assert result["experiment"]["state"] == "awaiting_validation_confirmation"
    assert result["experiment"]["leader"]["variant_id"] == "challenger_0.002"
    assert result["receipt_sha256"] == result["experiment"]["research_receipt_sha256"]
    monkeypatch.setattr(
        store_module,
        "connect_private_sqlite",
        lambda *_args, **_kwargs: pytest.fail("receipt must not use the write connector"),
    )
    monkeypatch.setattr(
        store_module,
        "secure_sqlite_artifacts",
        lambda *_args, **_kwargs: pytest.fail("receipt must not repair Store files"),
    )
    assert read_receipt(context, "experiment-1")["receipt"]["concluded_at_utc"] == complete["updated_at_utc"]

    attached = ExperimentStore(context["store_path"]).get_experiment("experiment-1")
    assert attached is not None
    before = dict(attached)
    receipt_path = Path(context["artifact_root"]) / attached["research_receipt_ref"]
    encoded = receipt_path.read_bytes()
    receipt_path.unlink()
    with pytest.raises(StrategyLabServiceError) as missing:
        read_receipt(context, "experiment-1")
    assert missing.value.reason_code == "receipt_artifact_invalid"

    receipt_path.write_bytes(encoded)
    changed = json.loads(encoded)
    changed["provisional"] = False
    receipt_path.write_bytes(strict_json_bytes(changed))
    with pytest.raises(StrategyLabServiceError) as replaced:
        read_receipt(context, "experiment-1")
    assert replaced.value.reason_code == "receipt_immutable_conflict"
    assert ExperimentStore(context["store_path"]).get_experiment("experiment-1") == before


def test_public_completion_preserves_numbers_and_selects_real_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    context = _context(tmp_path)
    created = _create(context)
    store = ExperimentStore(context["store_path"])
    expected_points = []
    for day in range(1, 21):
        trading_day = f"2026-08-{day:02d}"
        point_id = f"point-{day:02d}"
        expected_points.append({"recommendation_point_id": point_id, "trading_day": trading_day})
        for arm, variant, threshold, annualized, pnl in (
            ("baseline", None, None, 0.10, 100.0),
            ("challenger", "challenger_0.002", 0.002, 0.12, 101.0),
        ):
            arm_id = variant or "baseline"
            store.put_observation(
                "experiment-1",
                observation_key=f"single_result:{point_id}:{arm_id}",
                recommendation_point_id=point_id,
                arm_id=arm_id,
                kind="single_result",
                status="available",
                payload={
                    "recommendation_point_id": point_id,
                    "trading_day": trading_day,
                    "arm": arm,
                    "variant_id": variant,
                    "near_return_threshold": threshold,
                    "candidate_ref": f"{arm_id}-{point_id}",
                    "fill_status": "simulated_fill",
                    "outcome_status": "available",
                    "safety_status": "pass",
                    "annualized_return": annualized,
                    "economic_pnl_cny": pnl,
                },
                created_at_utc=NOW,
            )
    complete = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=created["revision"],
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={"observation_count": 40},
        occurred_at_utc=NOW,
    )
    _patch_behavior(monkeypatch)
    monkeypatch.setattr(
        service,
        "load_research_projection",
        lambda _spec: {"expected_points": expected_points},
    )

    result = execute_research(context, "experiment-1", actor="tester", occurred_at_utc=NOW)
    receipt = read_receipt(context, "experiment-1")["receipt"]

    assert complete["state"] == "research_complete"
    assert result["experiment"]["state"] == "awaiting_validation_confirmation"
    assert result["conclusion"]["status"] == "leader"
    assert result["conclusion"]["leader"]["variant_id"] == "challenger_0.002"
    assert isinstance(receipt["comparisons"][0]["near_return_threshold"], float)
    assert isinstance(receipt["comparisons"][0]["expected_point_count"], int)


def test_orphan_receipt_is_not_public_before_store_attach(tmp_path: Path) -> None:
    context = _context(tmp_path)
    created = _create(context)
    store = ExperimentStore(context["store_path"])
    complete = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=created["revision"],
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={"observation_count": 0},
        occurred_at_utc=NOW,
    )
    receipt = build_research_receipt(
        complete,
        [],
        [],
        {
            "status": "insufficient_evidence",
            "reason_code": "variant_comparison_invalid",
            "leader": None,
            "passing_variant_ids": [],
        },
        complete["updated_at_utc"],
    )
    publish_receipt(context["artifact_root"], "experiment-1", "research", receipt)

    with pytest.raises(StrategyLabServiceError) as raised:
        read_receipt(context, "experiment-1")

    assert raised.value.reason_code == "receipt_not_found"
    assert store.get_experiment("experiment-1") == complete
