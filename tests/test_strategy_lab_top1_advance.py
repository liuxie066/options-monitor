from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.application.strategy_lab.top1.advance as advance_module
from src.application.strategy_lab.top1.advance import advance_scheduled


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}


def _explode() -> Any:  # pragma: no cover - asserted unreachable
    raise AssertionError("lazy dependency must not be loaded")


def _readiness(ready: bool) -> dict[str, object]:
    return {
        "validation_runtime_ready": ready,
        "facts": {"corpus": {"schema_version": "corpus_health_receipt.v2"}},
    }


def test_disabled_gate_loads_no_schedule_source_readiness_or_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=_explode,
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="disabled",
        environ={},
    )

    assert result["status"] == "disabled"
    assert result["schema_version"] == "sell_put_top1_advance_result.v2"
    assert result["service_available"] is False


def test_collecting_order_due_conclusion_and_peer_failure_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    context_calls = 0

    def active(*_args: object, **_kwargs: object) -> list[str]:
        return ["bad", "good"]

    def context(*_args: object, experiment_id: str, **_kwargs: object) -> dict[str, object]:
        nonlocal context_calls
        if experiment_id == "bad":
            raise ValueError("bad experiment")
        context_calls += 1
        progress = (
            "collecting_decisions"
            if context_calls == 1
            else "awaiting_outcomes"
            if context_calls == 2
            else "ready_to_conclude"
        )
        return {
            "experiment_id": "good",
            "phase": "validation",
            "validation_progress": progress,
            "terminal_mode": None,
            "behavior_binding_drift": False,
            "commitment": {
                "market_calendar_version": "hk-calendar.v1",
                "market_calendar_snapshot_content_sha256": "a" * 64,
                "schedule_config_sha256": "b" * 64,
            },
            "committed_days": [{"trading_date": "2026-08-16"}],
            "open_trading_date": "2026-08-17",
            "consumed_point_ids": [],
            "last_consumed_available_point_id": None,
            "timer_binding": {
                "revision": "top1-advance.v1",
                "advance_cadence_seconds": 60,
            },
            "has_outcome_jobs": True,
        }

    monkeypatch.setattr(advance_module, "read_active_experiment_ids", active)
    monkeypatch.setattr(advance_module, "read_advance_context", context)
    monkeypatch.setattr(
        advance_module,
        "read_validation_day_source",
        lambda *_args, **_kwargs: {
            "status": "available",
            "expectation": {"expected_recommendation_point_ids": ["p1", "p2"]},
        },
    )
    monkeypatch.setattr(
        advance_module,
        "read_validation_point_source",
        lambda *_args, **_kwargs: {"status": "available"},
    )

    def consume(*_args: object, recommendation_point_id: str, **_kwargs: object) -> dict[str, str]:
        calls.append(f"consume:{recommendation_point_id}")
        return {"status": "committed"}

    def observe(*_args: object, observed_recommendation_point_id: str, **_kwargs: object) -> dict[str, str]:
        calls.append(f"observe:{observed_recommendation_point_id}")
        return {"status": "committed"}

    monkeypatch.setattr(advance_module, "consume_validation_point", consume)
    monkeypatch.setattr(advance_module, "observe_active_contracts", observe)
    monkeypatch.setattr(
        advance_module,
        "settle_due_outcomes",
        lambda *_args, **_kwargs: calls.append("settle") or {"status": "ok"},
    )
    monkeypatch.setattr(
        advance_module,
        "conclude_validation",
        lambda *_args, **_kwargs: calls.append("conclude") or {"status": "pass"},
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=lambda: _readiness(False),
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="advance",
        environ=AVAILABLE,
    )

    assert calls == ["consume:p1", "observe:p1", "consume:p2", "observe:p2", "settle", "conclude"]
    assert result["status"] == "partial"
    assert result["corpus"] == []
    by_id = {item["experiment_id"]: item for item in result["experiments"]}
    assert by_id["bad"]["status"] == "failed"
    assert by_id["good"]["status"] == "ok"


def test_behavior_drift_terminates_without_loading_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_calls = 0
    terminated: list[str] = []

    def active(*_args: object, **_kwargs: object) -> list[str]:
        nonlocal active_calls
        active_calls += 1
        return ["drift"] if active_calls == 1 else []

    monkeypatch.setattr(advance_module, "read_active_experiment_ids", active)
    monkeypatch.setattr(
        advance_module,
        "read_advance_context",
        lambda *_args, **_kwargs: {
            "experiment_id": "drift",
            "phase": "validation",
            "validation_progress": "collecting_decisions",
            "terminal_mode": None,
            "behavior_binding_drift": True,
            "committed_days": [],
        },
    )
    monkeypatch.setattr(
        advance_module,
        "terminate_experiment",
        lambda *_args, experiment_id, **_kwargs: terminated.append(experiment_id),
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=lambda: _readiness(True),
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="drift",
        environ=AVAILABLE,
    )

    assert result["status"] == "ok"
    assert terminated == ["drift"]


@pytest.mark.parametrize("conflict_kind", ["day", "schedule"])
def test_hidden_window_overlap_blocks_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict_kind: str
) -> None:
    expected_gateway = object()
    day_a = {
        "trading_date": "2026-08-17",
        "scheduled_scan_targets_market": ["2026-08-17T01:00:00Z"],
        "expected_recommendation_point_ids": ["a"],
    }
    day_b = (
        {**day_a, "expected_recommendation_point_ids": ["b"]}
        if conflict_kind == "day"
        else dict(day_a)
    )

    def context(
        *_args: object, experiment_id: str, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "experiment_id": experiment_id,
            "phase": "validation",
            "validation_progress": "collecting_decisions",
            "terminal_mode": None,
            "behavior_binding_drift": False,
            "commitment": {
                "market_calendar_version": "hk-calendar.v1",
                "market_calendar_snapshot_content_sha256": "a" * 64,
                "schedule_config_sha256": (
                    "c" * 64
                    if conflict_kind == "schedule" and experiment_id == "b"
                    else "b" * 64
                ),
            },
            "committed_days": [day_a if experiment_id == "a" else day_b],
            "open_trading_date": "2026-08-17",
            "consumed_point_ids": [],
            "last_consumed_available_point_id": None,
            "timer_binding": {
                "revision": "top1-advance.v1",
                "advance_cadence_seconds": 60,
            },
            "has_outcome_jobs": True,
        }

    def settle(*_args: object, gateway: object, **_kwargs: object) -> dict[str, str]:
        assert gateway is expected_gateway
        return {"status": "ok"}

    monkeypatch.setattr(
        advance_module,
        "read_active_experiment_ids",
        lambda *_args, **_kwargs: ["a", "b"],
    )
    monkeypatch.setattr(advance_module, "read_advance_context", context)
    monkeypatch.setattr(advance_module, "read_validation_day_source", _explode)
    monkeypatch.setattr(
        advance_module,
        "settle_due_outcomes",
        settle,
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=lambda: _readiness(True),
        load_gateway=lambda: expected_gateway,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-17T01:00:00Z",
        idempotency_key="overlap",
        environ=AVAILABLE,
    )

    assert result["status"] == "partial"
    assert any(
        item.get("status") == "conflict"
        and item.get("reason_code") == "hidden_window_overlap"
        for item in result["corpus"]
    )
    assert all(
        item["steps"] == [
            {
                "operation": "collect_validation_day",
                "status": "blocked",
                "reason_code": "hidden_window_overlap",
                "trading_date": "2026-08-17",
            },
            {"operation": "settle_due_outcomes", "status": "ok"},
        ]
        for item in result["experiments"]
    )


@pytest.mark.parametrize(
    ("readiness_result", "expected_status"),
    [(True, "ok"), (False, "ok"), (None, "partial")],
)
def test_idle_advance_only_fails_when_readiness_cannot_be_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readiness_result: bool | None,
    expected_status: str,
) -> None:
    monkeypatch.setattr(
        advance_module, "read_active_experiment_ids", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=(
            (lambda: _readiness(readiness_result))
            if readiness_result is not None
            else _explode
        ),
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="readiness-error",
        environ=AVAILABLE,
    )

    assert result["status"] == expected_status
    if readiness_result is None:
        assert result["readiness"]["reason_code"] == "advance_failed"
    else:
        assert result["readiness"]["validation_runtime_ready"] is readiness_result


def test_validation_provider_need_makes_unready_advance_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = {
        "experiment_id": "active",
        "phase": "validation",
        "validation_progress": "awaiting_outcomes",
        "terminal_mode": None,
        "behavior_binding_drift": False,
        "committed_days": [],
        "timer_binding": {
            "revision": "top1-advance.v1",
            "advance_cadence_seconds": 60,
        },
        "has_outcome_jobs": True,
    }
    monkeypatch.setattr(
        advance_module,
        "read_active_experiment_ids",
        lambda *_args, **_kwargs: ["active"],
    )
    monkeypatch.setattr(
        advance_module,
        "read_advance_context",
        lambda *_args, **_kwargs: context,
    )

    def settle(*_args: object, gateway: object, **_kwargs: object) -> dict[str, str]:
        assert gateway is None
        return {"status": "pending"}

    monkeypatch.setattr(
        advance_module,
        "settle_due_outcomes",
        settle,
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=lambda: _readiness(False),
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="validation-unready",
        environ=AVAILABLE,
    )

    assert result["status"] == "partial"
    assert result["readiness"]["validation_runtime_ready"] is False


def test_timer_binding_mismatch_is_partial_without_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = {
        "experiment_id": "mismatch",
        "phase": "validation",
        "validation_progress": "awaiting_outcomes",
        "terminal_mode": None,
        "behavior_binding_drift": False,
        "committed_days": [],
        "timer_binding": {
            "revision": "top1-advance.v1",
            "advance_cadence_seconds": 61,
        },
        "has_outcome_jobs": True,
    }
    monkeypatch.setattr(
        advance_module, "read_active_experiment_ids", lambda *_args, **_kwargs: ["mismatch"]
    )
    monkeypatch.setattr(
        advance_module, "read_advance_context", lambda *_args, **_kwargs: context
    )
    monkeypatch.setattr(
        advance_module,
        "settle_due_outcomes",
        lambda *_args, **_kwargs: {"status": "pending"},
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=lambda: _readiness(True),
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="timer-mismatch",
        environ=AVAILABLE,
    )

    assert result["status"] == "partial"
    assert any(
        step.get("reason_code") == "timer_binding_mismatch"
        for step in result["experiments"][0]["steps"]
    )


def test_advance_uses_readiness_as_the_only_health_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness_calls = 0
    monkeypatch.setattr(
        advance_module,
        "read_active_experiment_ids",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    def readiness() -> dict[str, object]:
        nonlocal readiness_calls
        readiness_calls += 1
        return _readiness(False)

    result = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=readiness,
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=300,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="single-health-fact",
        environ=AVAILABLE,
    )
    assert result["status"] == "ok"
    assert result["schema_version"] == "sell_put_top1_advance_result.v2"
    assert "corpus_health" not in result
    assert result["readiness"]["facts"]["corpus"] is not None
    assert readiness_calls == 1

    unavailable = advance_scheduled(
        object(),
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_readiness=lambda: {
            "validation_runtime_ready": False,
            "facts": {"corpus": None},
        },
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=300,
        actor="timer",
        occurred_at_utc="2026-08-16T01:05:00Z",
        idempotency_key="health-unavailable",
        environ=AVAILABLE,
    )
    assert unavailable["status"] == "partial"
    assert "corpus_health" not in unavailable
