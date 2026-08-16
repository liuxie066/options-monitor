from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.application.strategy_lab.top1.advance as advance_module
from src.application.strategy_lab.top1.advance import advance_scheduled


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}


def _explode() -> Any:  # pragma: no cover - asserted unreachable
    raise AssertionError("lazy dependency must not be loaded")


def _enabled(*_args: object, **_kwargs: object) -> dict[str, bool]:
    return {"maintainer_available": True, "user_opt_in": True, "effective": True}


def _calendar() -> dict[str, object]:
    return {
        "coverage_start": "2026-08-16",
        "coverage_end": "2026-08-18",
        "trading_dates": ["2026-08-17"],
        "market_calendar_version": "hk-calendar.v1",
        "snapshot_content_sha256": "a" * 64,
    }


def test_disabled_gate_loads_no_schedule_source_readiness_or_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        advance_module,
        "effective_feature_status",
        lambda *_args, **_kwargs: {
            "maintainer_available": False,
            "user_opt_in": True,
            "effective": False,
        },
    )
    monkeypatch.setattr(
        advance_module, "reconcile_disabled_experiments", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(advance_module, "discover_recommendation_points", _explode)

    result = advance_scheduled(
        object(),
        tmp_path / "source",
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_schedule=_explode,
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
            "committed_days": [],
            "open_trading_date": "2026-08-17",
            "consumed_point_ids": [],
            "last_consumed_available_point_id": None,
            "timer_binding": {
                "revision": "top1-advance.v1",
                "advance_cadence_seconds": 60,
            },
            "has_outcome_jobs": True,
        }

    monkeypatch.setattr(advance_module, "effective_feature_status", _enabled)
    monkeypatch.setattr(advance_module, "read_active_experiment_ids", active)
    monkeypatch.setattr(advance_module, "read_advance_context", context)
    monkeypatch.setattr(advance_module, "read_market_calendar_binding", lambda *_args, **_kwargs: _calendar())
    monkeypatch.setattr(advance_module, "discover_recommendation_points", lambda *_args, **_kwargs: [])
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
        tmp_path / "source",
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_schedule=lambda: {},
        load_readiness=lambda: {"validation_runtime_ready": False},
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

    monkeypatch.setattr(advance_module, "effective_feature_status", _enabled)
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
    monkeypatch.setattr(advance_module, "read_market_calendar_binding", lambda *_args, **_kwargs: _calendar())
    monkeypatch.setattr(advance_module, "discover_recommendation_points", lambda *_args, **_kwargs: [])
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
        tmp_path / "source",
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_schedule=lambda: {},
        load_readiness=lambda: {"validation_runtime_ready": True},
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


def test_hidden_window_overlap_blocks_sealing_and_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day_a = {
        "trading_date": "2026-08-17",
        "scheduled_scan_targets_market": ["2026-08-17T01:00:00Z"],
        "expected_recommendation_point_ids": ["a"],
    }
    day_b = {**day_a, "expected_recommendation_point_ids": ["b"]}

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
                "schedule_config_sha256": "b" * 64,
            },
            "committed_days": [day_a if experiment_id == "a" else day_b],
            "open_trading_date": "2026-08-17",
            "consumed_point_ids": [],
            "last_consumed_available_point_id": None,
            "timer_binding": {
                "revision": "top1-advance.v1",
                "advance_cadence_seconds": 60,
            },
            "has_outcome_jobs": False,
        }

    monkeypatch.setattr(advance_module, "effective_feature_status", _enabled)
    monkeypatch.setattr(
        advance_module,
        "read_active_experiment_ids",
        lambda *_args, **_kwargs: ["a", "b"],
    )
    monkeypatch.setattr(advance_module, "read_advance_context", context)
    monkeypatch.setattr(
        advance_module, "discover_recommendation_points", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(advance_module, "seal_committed_day_expectation", _explode)
    monkeypatch.setattr(advance_module, "read_validation_day_source", _explode)
    monkeypatch.setattr(
        advance_module,
        "settle_due_outcomes",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "source",
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_schedule=lambda: {},
        load_readiness=lambda: {"validation_runtime_ready": False},
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-17T01:00:00Z",
        idempotency_key="overlap",
        environ=AVAILABLE,
    )

    assert result["status"] == "partial"
    assert any(
        item.get("status") == "blocked"
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


def test_out_of_coverage_calendar_blocks_sealing_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(advance_module, "effective_feature_status", _enabled)
    monkeypatch.setattr(
        advance_module, "read_active_experiment_ids", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        advance_module,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            **_calendar(),
            "coverage_start": "2026-08-17",
            "coverage_end": "2026-08-18",
        },
    )
    monkeypatch.setattr(
        advance_module, "discover_recommendation_points", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        advance_module,
        "recover_account_terminal_projections",
        lambda *_args, **_kwargs: [],
    )

    result = advance_scheduled(
        object(),
        tmp_path / "source",
        tmp_path / "artifacts",
        market="HK",
        account="lx",
        load_schedule=lambda: {},
        load_readiness=lambda: {"validation_runtime_ready": False},
        load_gateway=_explode,
        advance_revision="top1-advance.v1",
        advance_interval_seconds=60,
        actor="timer",
        occurred_at_utc="2026-08-16T01:00:00Z",
        idempotency_key="coverage",
        environ=AVAILABLE,
    )

    assert result["status"] == "partial"
    assert result["corpus"][0]["reason_code"] == (
        "market_calendar_binding_unavailable"
    )
