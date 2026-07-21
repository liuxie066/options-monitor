from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


MARKET_DATE = "2026-07-21"
FIXED_TARGET = "2026-07-21T10:00:00-04:00"
HALF_TARGET = "2026-07-21T10:30:00-04:00"
IDENTITY = "candidate:v1:lx:US:NVDA:sell_put"


class _RunLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        self.events.append({"step": step, "status": status, **kwargs})


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.failures: list[tuple[str, str]] = []
        self.successes = 0

    def audit(self, event_type: str, action: str, **kwargs) -> None:
        self.events.append({"event_type": event_type, "action": action, **kwargs})

    def guard_mark_failure(self, error_code: str, stage: str) -> None:
        self.failures.append((error_code, stage))

    def guard_mark_success(self) -> None:
        self.successes += 1


def _brief(*, run_id: str, account: str = "lx", market: str = "US", blocked: bool = False, candidate: bool = True) -> dict:
    actions = []
    candidates = {"sell_put": [], "covered_call": [], "combo_yield": []}
    if candidate:
        action = {
            "priority": "P1",
            "state": "active",
            "action_type": "open_candidate",
            "strategy_family": "sell_put",
            "account": account,
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "expiration": "2026-08-21",
            "strike": 100,
            "contract_symbol": "NVDA260821P00100000",
            "metrics": {"mid": 1.2, "capacity": {"contracts_available": 1}},
        }
        actions.append(action)
        candidates["sell_put"].append({
            "rank": 1,
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "option_type": "put",
            "expiration": "2026-08-21",
            "strike": 100,
            "contract_symbol": "NVDA260821P00100000",
            "metrics": {"mid": 1.2},
            "capacity": {"contracts_available": 1},
        })
    if blocked:
        actions.insert(0, {
            "priority": "P0",
            "state": "blocked",
            "action_type": "data_blocked",
            "strategy_family": "sell_put",
            "account": account,
            "symbol": "NVDA",
            "title": "关键数据阻塞",
            "reason": "pipeline_failed",
            "metrics": {},
        })
    return {
        "market": market,
        "market_trading_date": MARKET_DATE,
        "account": account,
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-21T14:00:00+00:00",
        "data_as_of_utc": "2026-07-21T13:59:00+00:00",
        "valid_until_utc": "2026-07-21T20:00:00+00:00",
        "status": "blocked" if blocked else "ready",
        "actionability": "blocked" if blocked else "live_actionable",
        "strategy_summary": "test",
        "actions": actions,
        "positions": [],
        "capacity": {"sell_put": {"contracts_available": 1}},
        "funds": {
            "cash_total_by_currency": {"USD": 100_000.0},
            "option_opening_available_by_currency": {"USD": 60_000.0},
            "available": True,
            "reason": "ok",
        },
        "candidates": candidates,
        "rejections": {},
        "events": [],
        "data_gaps": ([{"scope": "pipeline", "reason": "pipeline_failed"}] if blocked else []),
        "source_artifacts": [],
    }


def _config(*, enabled: bool = True, quiet: str | None = None) -> dict:
    notifications = {
        "provider": "wechat_clawbot",
        "channel": "wechat_clawbot",
        "target": "wechat:ops",
        "daily_brief": {"enabled": enabled},
    }
    if quiet:
        notifications["quiet_hours_beijing"] = quiet
    return {"notifications": notifications, "schedule": {"timezone": "America/New_York"}}


def _request(
    tmp_path: Path,
    *,
    run_id: str,
    fixed: bool = True,
    no_send: bool = False,
    pipeline_ok: bool = True,
    delivery_only: bool = False,
    config: dict | None = None,
    accounts: tuple[str, ...] = ("lx",),
):
    import src.application.tick_notification_flow as mod
    from src.application.multi_tick.misc import AccountResult

    target = FIXED_TARGET if fixed else HALF_TARGET
    results = [] if delivery_only else [AccountResult(account, pipeline_ok, fixed, "ok" if pipeline_ok else "pipeline failed", "") for account in accounts]
    completions: list[dict] = []
    commits: list[dict[str, str]] = []
    scheduler_by_account = {
        account: {
            "in_run_window": True,
            "now_market": "2026-07-21T10:10:00-04:00" if delivery_only else target,
            "scheduled_scan_target_market": None if delivery_only else target,
            "scheduled_target_market": None if delivery_only or not fixed else target,
        }
        for account in accounts
    }
    request = mod.TickNotificationRequest(
        base=tmp_path,
        cfg_path=tmp_path / "config.us.json",
        state_path=tmp_path / "scheduler_state.json",
        scheduler_schedule_key="schedule",
        base_cfg=config or _config(),
        run_id=run_id,
        runlog=_RunLog(),
        results=results,
        tick_metrics={},
        no_send=no_send,
        bj_tz=ZoneInfo("Asia/Shanghai"),
        audit_helper=_Audit(),
        vpy=Path("python3"),
        complete_tick_idempotency_fn=lambda **kwargs: completions.append(dict(kwargs)),
        markets_to_run=("US",),
        scheduler_markets=("US",),
        scheduler_decision={"in_run_window": True, "now_market": scheduler_by_account[accounts[0]]["now_market"]},
        ran_pipeline_accounts=accounts if pipeline_ok and not delivery_only else (),
        account_ids=accounts,
        scheduler_decisions_by_account=scheduler_by_account,
        scheduled_scan_targets_by_account={} if delivery_only else {account: target for account in accounts},
        commit_scan_targets_fn=lambda value: commits.append(dict(value)),
        delivery_only=delivery_only,
    )
    return SimpleNamespace(request=request, completions=completions, commits=commits)


def _patch_assembler(monkeypatch, *, blocked: bool = False, candidate: bool = True) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda *, run_id, account, markets_to_run, **_kwargs: {
            market: _brief(run_id=run_id, account=account, market=market, blocked=blocked, candidate=candidate)
            for market in markets_to_run
        },
    )


def _patch_sender(monkeypatch, *, result: dict | None = None, calls: list[dict] | None = None) -> None:
    import src.application.tick_notification_flow as mod

    def send(**kwargs):
        if calls is not None:
            calls.append(dict(kwargs))
        return result or {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
            "returncode": 0,
            "message_id": "msg-1",
            "idempotency_key": kwargs["idempotency_key"],
        }

    monkeypatch.setattr(
        mod,
        "select_notification_delivery_adapter",
        lambda _provider: SimpleNamespace(
            send_fn=send,
            normalize_fn=lambda *, send_result: send_result,
            failure_stage="wechat_clawbot_message_send",
        ),
    )
    monkeypatch.setattr(mod, "finalize_multi_tick_run", lambda **kwargs: 1 if kwargs.get("notify_failures") else 0)


def test_daily_brief_default_off_preserves_legacy_preparation(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    called = {"legacy": 0}
    monkeypatch.setattr(mod, "_prepare_daily_brief_notification", lambda _request: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        mod,
        "prepare_multi_account_notification",
        lambda **_kwargs: called.update(legacy=called["legacy"] + 1) or SimpleNamespace(
            prepared_messages=SimpleNamespace(messages_by_account={}, threshold_met=False, used_heartbeat=False, heartbeat_accounts=()),
            notify_candidates=[],
            results_count=0,
        ),
    )
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **_kwargs: 0)
    bundle = _request(tmp_path, run_id="disabled", config=_config(enabled=False))
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert called["legacy"] == 1
    assert bundle.commits == [{"lx": FIXED_TARGET}]


@pytest.mark.parametrize(
    ("trigger_kind", "target"),
    (("manual", HALF_TARGET), ("force", None)),
)
def test_non_scheduled_scan_updates_current_without_delivery_side_effects(
    monkeypatch,
    tmp_path: Path,
    trigger_kind: str,
    target: str | None,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
        read_latest_daily_decision_brief,
    )

    _patch_assembler(monkeypatch)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(tmp_path, run_id=f"{trigger_kind}-snapshot", fixed=False)
    scheduler = dict(bundle.request.scheduler_decisions_by_account["lx"])
    scheduler["scheduled_scan_target_market"] = target
    scheduler["scheduled_target_market"] = None
    bundle.request = replace(
        bundle.request,
        trigger_kind=trigger_kind,
        scheduler_decisions_by_account={"lx": scheduler},
        scheduled_scan_targets_by_account={"lx": target},
    )

    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"] is True
    assert read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["available"] is False
    assert calls == []
    assert bundle.commits == [{"lx": None}]


def test_scheduled_scan_missing_exact_account_target_fails_before_prepare_or_send(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must fail before prepare")),
    )
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(tmp_path, run_id="missing-target")
    bundle.request = replace(bundle.request, scheduled_scan_targets_by_account={})

    with pytest.raises(RuntimeError, match="scheduled scan target missing for accounts: lx"):
        mod.run_tick_notification_flow(bundle.request)
    assert calls == []
    assert bundle.commits == []
    assert bundle.request.audit_helper.failures == [("SCHEDULED_SCAN_TARGET_MISSING", "validate_scan_targets")]


def test_fixed_scan_persists_commits_then_sends_full_and_confirms(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery_state
    from src.application.notification_delivery_adapter import build_notification_transport_key

    _patch_assembler(monkeypatch)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(tmp_path, run_id="fixed")
    assert mod.run_tick_notification_flow(bundle.request) == 0
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["state"]
    envelope = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert envelope["status"] == "confirmed"
    assert set(state["days"][MARKET_DATE]["alerted_candidates"]) == {IDENTITY}
    assert calls[0]["idempotency_key"] == build_notification_transport_key(envelope["delivery_key"])
    assert bundle.commits == [{"lx": FIXED_TARGET}]


@pytest.mark.parametrize(
    ("fixed", "candidate", "expected_pending"),
    (
        (False, False, 0),
        (True, False, 0),
        (False, True, 1),
        (True, True, 1),
    ),
)
def test_no_send_four_way_matrix_updates_snapshot_without_publishing_envelope(
    monkeypatch,
    tmp_path: Path,
    fixed: bool,
    candidate: bool,
    expected_pending: int,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_latest_daily_decision_brief, read_retryable_daily_decision_brief_delivery

    _patch_assembler(monkeypatch, candidate=candidate)
    bundle = _request(tmp_path, run_id=f"no-send-{fixed}-{candidate}", fixed=fixed, no_send=True)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"] is True
    retry = read_retryable_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)
    assert retry["envelope"] is None
    day = retry["state"]["days"][MARKET_DATE]
    assert len(day["pending_candidates"]) == expected_pending
    assert day["fixed_reports"] == {}
    assert day["candidate_delivery"] is None
    assert day["alerted_candidates"] == {}
    assert bundle.commits == [{"lx": FIXED_TARGET if fixed else HALF_TARGET}]


def test_quiet_hours_keeps_durable_fixed_envelope(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    monkeypatch.setattr(mod, "evaluate_dnd_quiet_hours", lambda **_kwargs: {"is_quiet": True, "quiet_window": "00:00-23:59", "parse_error": None})
    bundle = _request(tmp_path, run_id="quiet")
    assert mod.run_tick_notification_flow(bundle.request) == 0
    retry = read_retryable_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)
    assert retry["reason"] == "pending_fixed"
    assert bundle.commits == [{"lx": FIXED_TARGET}]


def test_nonfixed_new_candidate_prepares_candidate_alert(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    bundle = _request(tmp_path, run_id="candidate", fixed=False)
    prep = mod._prepare_daily_brief_notification(bundle.request)
    envelope = prep.lifecycles_by_account["lx"]["envelope"]
    assert envelope["delivery_kind"] == "candidate_alert"
    assert envelope["candidate_identities"] == [IDENTITY]
    assert "新增候选 · 10:30 发现" in envelope["rendered_message"]
    assert "现金总额：$100,000.00" in envelope["rendered_message"]
    assert "## 持仓" not in envelope["rendered_message"]


def test_pipeline_failure_fixed_sends_explicit_failure_without_advancing_current(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_latest_daily_decision_brief

    _patch_assembler(monkeypatch, blocked=True)
    bundle = _request(tmp_path, run_id="failed", pipeline_ok=False)
    prep = mod._prepare_daily_brief_notification(bundle.request)
    envelope = prep.lifecycles_by_account["lx"]["envelope"]
    assert envelope["delivery_kind"] == "fixed_failure"
    assert "数据异常" in envelope["rendered_message"]
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"] is False


def test_fixed_report_without_candidates_still_contains_positions_and_funds(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch, candidate=False)
    bundle = _request(tmp_path, run_id="fixed-empty")
    prep = mod._prepare_daily_brief_notification(bundle.request)
    message = prep.lifecycles_by_account["lx"]["envelope"]["rendered_message"]

    assert "本轮暂无符合条件的候选" in message
    assert "## 持仓" in message
    assert "## 资金" in message
    assert "现金总额：$100,000.00" in message


def test_pipeline_failure_nonfixed_is_quiet_but_commits_after_failure_artifact(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch, blocked=True)
    bundle = _request(tmp_path, run_id="failed-half", fixed=False, pipeline_ok=False)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert bundle.commits == [{"lx": HALF_TARGET}]
    assert not bundle.request.tick_metrics["daily_brief"]["prepared"][0]["delivery_key"]


def test_commit_failure_prevents_provider_call_and_keeps_envelope(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(tmp_path, run_id="commit-fail")
    bundle.request = replace(bundle.request, commit_scan_targets_fn=lambda _targets: (_ for _ in ()).throw(OSError("state write failed")))
    with pytest.raises(OSError, match="state write failed"):
        mod.run_tick_notification_flow(bundle.request)
    assert calls == []
    assert read_retryable_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)["envelope"]


def test_provider_definite_failure_stays_pending_for_exact_delivery_only_retry(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls, result={"ok": False, "command_ok": False, "delivery_confirmed": False, "returncode": 1, "error_code": "SEND_FAILED"})
    first = _request(tmp_path, run_id="send-fail")
    assert mod.run_tick_notification_flow(first.request) == 1
    retry_before = read_retryable_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)["envelope"]

    retry_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=retry_calls)
    second = _request(tmp_path, run_id="delivery-only", delivery_only=True)
    assert mod.run_tick_notification_flow(second.request) == 0
    assert retry_calls[0]["message"] == retry_before["rendered_message"]
    assert retry_calls[0]["idempotency_key"] == calls[0]["idempotency_key"]


def test_delivery_only_without_envelope_is_read_only_and_skips_assembler(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not assemble")))
    bundle = _request(tmp_path, run_id="delivery-only-empty", delivery_only=True)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert bundle.completions == [{"status": "skipped", "message": "no_retryable_delivery"}]
    assert not (tmp_path / "output_runs").exists()


def test_multi_market_scan_persists_snapshots_but_does_not_dispatch(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    bundle = _request(tmp_path, run_id="multi")
    bundle.request = replace(bundle.request, markets_to_run=("US", "HK"), scheduler_markets=("US", "HK"))
    prep = mod._prepare_daily_brief_notification(bundle.request)
    assert prep.multi_market_delivery_skipped is True
    assert prep.prepared_messages.messages_by_account == {}


def test_scheduled_renderer_uses_batch_time_without_leaking_revision(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    prep = mod._prepare_daily_brief_notification(_request(tmp_path, run_id="render").request)
    message = prep.prepared_messages.messages_by_account["lx"]
    assert "10:00 批次" in message
    assert "数据截至：美东 09:59 / 北京 21:59" in message
    assert "revision" not in message.lower()


def test_later_nonfixed_scan_preserves_existing_pending_candidate_envelope(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    first = mod._prepare_daily_brief_notification(_request(tmp_path, run_id="candidate-1", fixed=False).request)
    first_envelope = first.lifecycles_by_account["lx"]["envelope"]
    second = mod._prepare_daily_brief_notification(_request(tmp_path, run_id="candidate-2", fixed=False).request)
    second_envelope = second.lifecycles_by_account["lx"]["envelope"]
    assert second_envelope["delivery_key"] == first_envelope["delivery_key"]
    assert second_envelope["message_sha256"] == first_envelope["message_sha256"]
    assert second_envelope["revision"] == first_envelope["revision"]
