from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _FakeRunlog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        event = {"step": step, "status": status}
        event.update(kwargs)
        self.events.append(event)


def test_resolve_market_run_reports_trading_day_block_without_exiting() -> None:
    from src.application.multi_tick_scheduler import resolve_market_run

    runlog = _FakeRunlog()

    out = resolve_market_run(
        now_utc=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
        base_cfg={"symbols": [{"market": "US"}]},
        market_config="auto",
        force_mode=False,
        runlog=runlog,
        safe_data_fn=lambda data: data,
        domain_select_markets_to_run=lambda *_args: [],
        domain_markets_for_trading_day_guard=lambda *_args: ["US"],
        decide_trading_day_guard=lambda **_kwargs: {
            "guard_results": [{"market": "US", "is_trading_day": False}],
            "markets_to_run": [],
            "should_skip": True,
            "skip_message": "non-trading day: US",
        },
        reduce_trading_day_guard=lambda **_kwargs: {},
        check_trading_day_for_market=lambda market: (False, market),
    )

    assert out.trading_day_blocked is True
    assert out.markets_to_run == []
    assert out.scheduler_markets == ["US"]
    assert out.skip_message == "non-trading day: US"
    assert any(evt["step"] == "trading_day_guard" and evt["status"] == "skip" for evt in runlog.events)


def test_run_scheduler_flow_uses_account_scan_decisions() -> None:
    from domain.domain.engine import AccountSchedulerDecisionView, resolve_multi_tick_engine_entrypoint
    from domain.domain import SnapshotDTO
    from src.application.multi_tick_scheduler import run_scheduler_flow

    payloads = {
        None: {
            "should_run_scan": False,
            "is_notify_window_open": True,
            "reason": "global_not_due",
        },
        "lx": {
            "should_run_scan": False,
            "is_notify_window_open": True,
            "reason": "lx_not_due",
        },
        "sy": {
            "should_run_scan": True,
            "is_notify_window_open": True,
            "reason": "sy_due",
        },
    }
    calls: list[str | None] = []
    audit_events: list[dict[str, Any]] = []

    def fake_scheduler_cli(**kwargs):
        account = kwargs.get("account")
        calls.append(account)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payloads[account]), stderr="")

    out = run_scheduler_flow(
        vpy=Path("/repo/.venv/bin/python"),
        base=Path("/repo"),
        cfg_path=Path("/repo/config.us.json"),
        base_cfg={},
        state_path=Path("/repo/output_shared/state/scheduler_state.json"),
        scheduler_schedule_key="schedule",
        accounts=["lx", "sy"],
        force_mode=False,
        smoke=False,
        snapshot_cls=SnapshotDTO,
        engine_entrypoint=resolve_multi_tick_engine_entrypoint,
        account_view_cls=AccountSchedulerDecisionView,
        run_scan_scheduler_cli=fake_scheduler_cli,
        build_failure_audit_fields=lambda **_kwargs: {},
        audit_fn=lambda event_type, action, **kwargs: audit_events.append({"event_type": event_type, "action": action, **kwargs}),
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs)),
    )

    assert calls == [None, "lx", "sy"]
    assert out.should_run_global is False
    assert out.reason_global == "global_not_due"
    assert out.scan_decision_by_account["lx"]["should_run"] is False
    assert out.scan_decision_by_account["lx"]["reason"] == "lx_not_due"
    assert out.scan_decision_by_account["sy"]["should_run"] is True
    assert out.scan_decision_by_account["sy"]["reason"] == "sy_due"
    assert out.notify_decision_by_account["sy"].is_notify_window_open is True
    assert [event["action"] for event in audit_events if event["action"] == "scan_scheduler_account"] == [
        "scan_scheduler_account",
        "scan_scheduler_account",
    ]
