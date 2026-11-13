"""Benchmark-only request wiring for the real Tick preparation/composition owners.

This supports archived and current source without importing the notification-flow
unit-test facade. Synthetic assembly/transport below belong only to the preexisting
isolated delivery microbenchmark, never preparation or whole-Tick measurements.
"""
from __future__ import annotations

from dataclasses import fields
from functools import partial
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.application import multi_account_tick as tick_owner
from src.application import tick_notification_flow as flow
from src.application.multi_tick.misc import AccountResult


class BenchmarkRunlog:
    def __init__(self):
        self.events = []

    def safe_event(self, step, status, **kwargs):
        self.events.append({"step": step, "status": status, **kwargs})


class BenchmarkAudit:
    def __init__(self):
        self.events, self.failures, self.successes = [], [], 0

    def audit(self, event_type, action, **kwargs):
        self.events.append({"event_type": event_type, "action": action, **kwargs})

    def guard_mark_failure(self, error_code, stage):
        self.failures.append((error_code, stage))

    def guard_mark_success(self):
        self.successes += 1


def notification_bundle(base, *, run_id, config=None, accounts=("lx",), markets=("US",)):
    """Construct the selected source's production request; callbacks stay with root."""
    target = "2026-07-21T10:00:00-04:00"
    scheduler = {account: {"in_run_window": True, "should_run_scan": True,
        "now_utc": "2026-07-21T14:00:30Z", "now_market": target,
        "scheduled_scan_target_market": target, "scheduled_target_market": target}
        for account in accounts}
    completions, commits = [], []
    complete = lambda **kwargs: completions.append(dict(kwargs))
    commit = lambda values: commits.append(dict(values))
    kwargs = dict(base=base, cfg_path=base / f"config.{markets[0].lower()}.json",
        state_path=base / "scheduler_state.json", scheduler_schedule_key="schedule",
        base_cfg=config if config is not None else {
            "notifications": {"enabled": True, "provider": "wechat_clawbot",
                "channel": "wechat_clawbot", "target": "wechat:ops", "daily_brief": {}},
            "schedule": {"timezone": "America/New_York"}},
        run_id=run_id, runlog=BenchmarkRunlog(),
        results=[AccountResult(account, True, True, "ok", "") for account in accounts],
        tick_metrics={}, no_send=False, bj_tz=ZoneInfo("Asia/Shanghai"),
        audit_helper=BenchmarkAudit(), vpy=Path(sys.executable), markets_to_run=markets,
        scheduler_markets=markets,
        scheduler_decision={"in_run_window": True, "now_market": target},
        ran_pipeline_accounts=accounts, account_ids=accounts,
        scheduler_decisions_by_account=scheduler,
        scheduled_scan_targets_by_account={account: target for account in accounts},
        trigger_kind="scheduled")
    legacy = "complete_tick_idempotency_fn" in {item.name for item in fields(flow.TickNotificationRequest)}
    if legacy:
        kwargs.update(complete_tick_idempotency_fn=complete, commit_scan_targets_fn=commit)
    return SimpleNamespace(request=flow.TickNotificationRequest(**kwargs), completions=completions,
        commits=commits, complete=complete, commit=commit, legacy_request=legacy)


def prepare_notification(request):
    """Execute real source preparation then obligation freezing, without delivery."""
    if not hasattr(tick_owner, "prepare_tick_briefs"):
        preparation = flow._prepare_daily_brief_notification(request)
        return preparation, preparation.scan_outcomes_by_account
    sources = tick_owner.prepare_tick_briefs(
        base=request.base, run_id=request.run_id, config=request.base_cfg,
        markets=request.markets_to_run or request.scheduler_markets,
        results=request.results, ran_pipeline_accounts=request.ran_pipeline_accounts,
        scheduled_scan_targets_by_account=request.scheduled_scan_targets_by_account,
        scheduler_decisions_by_account=request.scheduler_decisions_by_account,
        scheduler_decision=request.scheduler_decision, runlog=request.runlog,
        recovery_intent_factory=partial(tick_owner._daily_brief_recovery_intent_for_preparation, request),
        deadline=request.deadline, cancelled=request.cancelled)
    preparation = flow._prepare_daily_brief_notification(request, brief_preparation=sources)
    return preparation, sources.scan_outcomes_by_account


def run_notification(bundle):
    """Use the real root composition, which owns finalization in current source."""
    kwargs = {"notification_request": bundle.request, "post_delivery_sidecars_fn": lambda: None}
    if not bundle.legacy_request:
        kwargs.update(complete_tick_idempotency_fn=bundle.complete, commit_scan_targets_fn=bundle.commit)
    return tick_owner._run_notification_with_post_delivery_fallback(**kwargs)


def patch_delivery_microbenchmark(monkeypatch, sends):
    """Retain the old delivery-only benchmark's declared synthetic boundaries."""
    from test_daily_decision_brief_repository_v2 import _action, _brief
    from src.application.notification_delivery_route import notification_reminder_route_key

    def assemble(*, base, run_id, account, markets_to_run, **_kwargs):
        result = {}
        for market in markets_to_run:
            action = _action()
            action.update(account=account, metrics={"mid": 1.2, "capacity": {"contracts_available": 1}})
            brief = _brief(run_id=run_id, actions=[action], account=account, market=market,
                           market_date="2026-07-21")
            brief.update(capacity={"sell_put": {"contracts_available": 1}},
                funds={"cash_total_by_currency": {"USD": 100_000.0},
                    "option_opening_available_by_currency": {"USD": 60_000.0},
                    "available": True, "reason": "ok"},
                candidates={"sell_put": [{"rank": 1, "symbol": "NVDA", "strategy_family": "sell_put",
                    "option_type": "put", "expiration": "2026-08-21", "strike": 100,
                    "contract_symbol": "NVDA260821P00100000", "metrics": {"mid": 1.2},
                    "capacity": {"contracts_available": 1}}], "covered_call": [], "combo_yield": []})
            result[market] = brief
        return result

    def send(**kwargs):
        sends.append(dict(kwargs))
        return {"ok": True, "command_ok": True, "delivery_confirmed": True,
            "returncode": 0, "message_id": "msg-1", "idempotency_key": kwargs["idempotency_key"],
            "attribution_route_key": notification_reminder_route_key(
                config={"notifications": kwargs.get("notifications") or {}}, base=kwargs["base"])}

    owner = (sys.modules[tick_owner.prepare_tick_briefs.__module__]
             if hasattr(tick_owner, "prepare_tick_briefs") else flow)
    monkeypatch.setattr(owner, "assemble_daily_decision_briefs", assemble)
    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=send, normalize_fn=lambda *, send_result: send_result,
        failure_stage="wechat_clawbot_message_send"))
