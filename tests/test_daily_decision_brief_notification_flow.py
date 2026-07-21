from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


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


def _brief(*, run_id: str, account: str = "lx", market: str = "US", blocked: bool = False) -> dict:
    action = {
        "priority": "P0" if blocked else "P1",
        "state": "blocked" if blocked else "active",
        "action_type": "data_blocked" if blocked else "open_candidate",
        "strategy_family": "sell_put",
        "account": account,
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "expiration": "2026-08-21",
        "strike": 100,
        "contract_symbol": "NVDA260821P00100000",
        "title": "关键数据阻塞" if blocked else "评估 Sell Put",
        "reason": "pipeline_failed" if blocked else "收益/风险通过筛选",
        "metrics": {"mid": 1.2},
    }
    return {
        "market": market,
        "market_trading_date": "2026-07-19",
        "account": account,
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-19T13:40:00+00:00",
        "data_as_of_utc": "2026-07-19T13:39:00+00:00",
        "valid_until_utc": "2026-07-19T20:00:00+00:00",
        "status": "blocked" if blocked else "ready",
        "actionability": "blocked" if blocked else "live_actionable",
        "strategy_summary": "test",
        "actions": [action],
        "positions": [],
        "capacity": {"sell_put": {"contracts_available": 1}},
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": ([{"scope": "pipeline", "reason": "pipeline_failed"}] if blocked else []),
        "source_artifacts": [],
    }


def _config(*, enabled: bool = True) -> dict:
    return {
        "notifications": {
            "provider": "wechat_clawbot",
            "channel": "wechat_clawbot",
            "target": "wechat:ops",
            "daily_brief": {"enabled": enabled},
        }
    }


def _request(
    tmp_path: Path,
    *,
    run_id: str,
    results: list[object] | None = None,
    ran_pipeline_accounts: tuple[str, ...] | None = None,
    markets: tuple[str, ...] = ("US",),
    no_send: bool = False,
    config: dict | None = None,
) -> SimpleNamespace:
    import src.application.tick_notification_flow as mod

    completions: list[dict] = []
    request = mod.TickNotificationRequest(
        base=tmp_path,
        cfg_path=tmp_path / "config.us.json",
        state_path=tmp_path / "scheduler_state.json",
        scheduler_schedule_key="us",
        base_cfg=config or _config(),
        run_id=run_id,
        runlog=_RunLog(),
        results=results or [{"account": "lx"}],
        tick_metrics={},
        no_send=no_send,
        bj_tz=ZoneInfo("Asia/Shanghai"),
        audit_helper=_Audit(),
        vpy=Path("python3"),
        complete_tick_idempotency_fn=lambda **kwargs: completions.append(dict(kwargs)),
        markets_to_run=markets,
        scheduler_markets=markets,
        scheduler_decision={"in_run_window": True},
        ran_pipeline_accounts=(
            ran_pipeline_accounts
            if ran_pipeline_accounts is not None
            else tuple(str(item["account"]) for item in (results or [{"account": "lx"}]))
        ),
    )
    return SimpleNamespace(request=request, completions=completions)


def _patch_assembler(monkeypatch) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda *, run_id, account, markets_to_run, **_kwargs: {
            market: _brief(run_id=run_id, account=account, market=market)
            for market in markets_to_run
        },
    )


def test_daily_brief_default_off_preserves_legacy_preparation(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    called = {"legacy": 0}
    monkeypatch.setattr(
        mod,
        "_prepare_daily_brief_notification",
        lambda _request: (_ for _ in ()).throw(AssertionError("daily brief must remain disabled")),
    )

    def _legacy(**_kwargs):
        called["legacy"] += 1
        return SimpleNamespace(
            prepared_messages=SimpleNamespace(
                messages_by_account={},
                threshold_met=False,
                used_heartbeat=False,
                heartbeat_accounts=(),
            ),
            notify_candidates=[],
            results_count=0,
        )

    monkeypatch.setattr(mod, "prepare_multi_account_notification", _legacy)
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **_kwargs: 0)
    bundle = _request(tmp_path, run_id="run-disabled", config=_config(enabled=False))

    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert called == {"legacy": 1}
    assert bundle.completions == [{"status": "completed", "message": "no_account_notification"}]


def test_single_market_confirmed_send_advances_pointer_with_compact_transport_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery
    from src.application.notification_delivery_adapter import build_notification_transport_key

    _patch_assembler(monkeypatch)
    sent: list[dict] = []
    marked: list[list[str]] = []

    def _send(**kwargs):
        sent.append(dict(kwargs))
        return {
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
            send_fn=_send,
            normalize_fn=lambda *, send_result: send_result,
            failure_stage="wechat_clawbot_message_send",
        ),
    )
    monkeypatch.setattr(mod, "mark_accounts_notified", lambda **kwargs: marked.append(list(kwargs["accounts"])))
    monkeypatch.setattr(mod, "finalize_multi_tick_run", lambda **_kwargs: 0)
    bundle = _request(tmp_path, run_id="run-full")

    assert mod.run_tick_notification_flow(bundle.request) == 0

    pointer = read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")
    assert pointer["available"] is True
    logical_key = pointer["pointer"]["delivery_key"]
    assert sent[0]["idempotency_key"] == build_notification_transport_key(logical_key)
    assert len(sent[0]["idempotency_key"]) == 35
    assert marked == [["lx"]]
    assert bundle.request.tick_metrics["notify_summary"]["send_confirmed_count"] == 1


def test_no_send_persists_full_artifact_without_advancing_pointer(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery,
        read_latest_daily_decision_brief,
    )

    _patch_assembler(monkeypatch)
    monkeypatch.setattr(
        mod,
        "select_notification_delivery_adapter",
        lambda _provider: (_ for _ in ()).throw(AssertionError("provider must not be selected in no-send")),
    )
    monkeypatch.setattr(
        mod,
        "mark_accounts_notified",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("scheduler pointer must not be marked")),
    )
    monkeypatch.setattr(mod, "finalize_multi_tick_run", lambda **_kwargs: 0)
    bundle = _request(tmp_path, run_id="run-no-send", no_send=True)

    assert mod.run_tick_notification_flow(bundle.request) == 0
    latest = read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")
    assert latest["available"] is True
    assert read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")["available"] is False
    prepared = bundle.request.tick_metrics["daily_brief"]["prepared"][0]
    assert prepared["brief_id"] == latest["brief"]["brief_id"]
    assert prepared["message_chars"] > 0
    assert len(prepared["message_sha256"]) == 64


def test_quiet_hours_does_not_advance_pointer_or_select_provider(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    monkeypatch.setattr(
        mod,
        "evaluate_dnd_quiet_hours",
        lambda **_kwargs: {"is_quiet": True, "quiet_window": "00:00-23:59", "parse_error": None},
    )
    monkeypatch.setattr(
        mod,
        "select_notification_delivery_adapter",
        lambda _provider: (_ for _ in ()).throw(AssertionError("provider must not be selected in quiet hours")),
    )
    bundle = _request(tmp_path, run_id="run-quiet")

    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")["available"] is False
    assert bundle.completions == [{"status": "skipped", "message": "quiet_hours"}]


def test_no_material_daily_brief_finishes_before_delivery_route_resolution(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    first = _request(tmp_path, run_id="run-first").request
    first_prep = mod._prepare_daily_brief_notification(first)
    lifecycle = first_prep.lifecycles_by_account["lx"]
    brief = lifecycle["brief"]
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=brief["market_trading_date"],
        account="lx",
        revision=brief["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
        confirmed_at_utc="2026-07-19T13:41:00+00:00",
    )

    monkeypatch.setattr(
        mod,
        "resolve_notification_delivery_route",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no-material must not resolve credentials/route")),
    )
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **_kwargs: 0)
    second = _request(tmp_path, run_id="run-same")

    assert mod.run_tick_notification_flow(second.request) == 0
    prepared = second.request.tick_metrics["daily_brief"]["prepared"][0]
    assert prepared["delivery_kind"] == "none"
    assert prepared["message_sha256"] is None
    assert prepared["message_chars"] is None
    assert prepared["render_limits"] == {
        "max_actions_per_priority": 5,
        "max_candidates_per_strategy": 3,
        "max_rejection_reasons": 5,
    }
    assert second.completions == [{"status": "completed", "message": "no_account_notification"}]


def test_multi_market_persists_partitioned_artifacts_but_suppresses_outbound(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from domain.storage.repositories import run_repo
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    bundle = _request(tmp_path, run_id="run-us-hk", markets=("US", "HK"))
    preparation = mod._prepare_daily_brief_notification(bundle.request)

    assert preparation.multi_market_delivery_skipped is True
    assert preparation.prepared_messages.messages_by_account == {}
    account_dir = run_repo.get_run_account_state_dir(tmp_path, "run-us-hk", "lx")
    assert (account_dir / "daily_decision_brief.US.json").exists()
    assert (account_dir / "daily_decision_brief.HK.json").exists()
    assert read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")["available"] is False
    assert read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="HK")["available"] is False


def test_prepare_and_confirmation_are_account_isolated(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    bundle = _request(
        tmp_path,
        run_id="run-two-accounts",
        results=[{"account": "lx"}, {"account": "sy"}],
    )
    preparation = mod._prepare_daily_brief_notification(bundle.request)

    assert set(preparation.prepared_messages.messages_by_account) == {"lx", "sy"}
    assert set(preparation.lifecycles_by_account) == {"lx", "sy"}
    assert preparation.delivery_keys_by_account["lx"] != preparation.delivery_keys_by_account["sy"]


def test_noop_account_does_not_prepare_or_send_daily_brief(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.multi_tick.misc import AccountResult

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no-op account must not be assembled")),
    )
    monkeypatch.setattr(
        mod,
        "resolve_notification_delivery_route",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no-op account must not resolve delivery")),
    )
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **_kwargs: 0)
    bundle = _request(
        tmp_path,
        run_id="run-noop",
        results=[AccountResult("lx", False, False, "already processed", "")],
        ran_pipeline_accounts=(),
    )

    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert bundle.request.tick_metrics["daily_brief"]["prepared"] == []
    assert bundle.completions == [{"status": "completed", "message": "no_account_notification"}]


def test_explicit_notification_denial_skips_even_completed_scan(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("denied account must not be assembled")),
    )
    bundle = _request(
        tmp_path,
        run_id="run-notify-denied",
        results=[{"account": "lx", "ran_scan": True, "should_notify": False}],
    )

    preparation = mod._prepare_daily_brief_notification(bundle.request)

    assert preparation.prepared_messages.messages_by_account == {}
    assert preparation.lifecycles_by_account == {}
    assert bundle.request.tick_metrics["daily_brief"]["prepared"] == []


def test_mixed_accounts_prepare_only_accounts_not_explicitly_denied(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    assembled: list[str] = []

    def _assemble(*, run_id, account, markets_to_run, **_kwargs):
        assembled.append(account)
        return {
            market: _brief(run_id=run_id, account=account, market=market)
            for market in markets_to_run
        }

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", _assemble)
    bundle = _request(
        tmp_path,
        run_id="run-mixed",
        results=[
            {"account": "lx", "ran_scan": False, "should_notify": False},
            {"account": "sy", "ran_scan": True, "should_notify": True},
        ],
        ran_pipeline_accounts=("sy",),
    )

    preparation = mod._prepare_daily_brief_notification(bundle.request)

    assert assembled == ["sy"]
    assert set(preparation.prepared_messages.messages_by_account) == {"sy"}
    assert set(preparation.lifecycles_by_account) == {"sy"}


def test_pipeline_failure_with_notification_allowed_still_prepares_blocked_brief(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.multi_tick.misc import AccountResult

    def _assemble(*, run_id, account, markets_to_run, pipeline_succeeded, **_kwargs):
        assert pipeline_succeeded is False
        return {
            market: _brief(run_id=run_id, account=account, market=market, blocked=True)
            for market in markets_to_run
        }

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", _assemble)
    bundle = _request(
        tmp_path,
        run_id="run-pipeline-failed",
        results=[
            AccountResult("lx", True, True, "pipeline failed", "")
        ],
        ran_pipeline_accounts=(),
    )

    preparation = mod._prepare_daily_brief_notification(bundle.request)

    assert set(preparation.prepared_messages.messages_by_account) == {"lx"}
    assert preparation.lifecycles_by_account["lx"]["brief"]["status"] == "blocked"
    assert "数据异常" in preparation.prepared_messages.messages_by_account["lx"]


def test_provider_success_but_local_confirmation_failure_is_not_marked_sent(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.notification_delivery_adapter import build_notification_transport_key
    from src.application.scheduled_notification import PerAccountSendExecution

    _patch_assembler(monkeypatch)
    bundle = _request(tmp_path, run_id="run-confirm-fail")
    preparation = mod._prepare_daily_brief_notification(bundle.request)
    logical_key = preparation.delivery_keys_by_account["lx"]
    execution = PerAccountSendExecution(
        sent_accounts=["lx"],
        notify_failures=[],
        attempted_accounts=["lx"],
        send_results=[
            {
                "ok": True,
                "account": "lx",
                "attempts": 1,
                "delivery_confirmed": True,
                "command_ok": True,
                "idempotency_key": build_notification_transport_key(logical_key),
            }
        ],
    )
    monkeypatch.setattr(
        mod,
        "confirm_daily_decision_brief_delivery",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("pointer write failed")),
    )

    sent_accounts, failures = mod._confirm_daily_brief_execution(
        request=bundle.request,
        preparation=preparation,
        execution=execution,
    )

    assert sent_accounts == []
    assert failures[0]["error_code"] == "DAILY_BRIEF_CONFIRM_FAILED"
    assert failures[0]["duplicate_risk"] is True
    assert bundle.request.audit_helper.failures == [
        ("DAILY_BRIEF_CONFIRM_FAILED", "confirm_daily_decision_brief_delivery")
    ]


def test_prepared_audit_hashes_exact_message_with_resolved_limits(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    config = _config()
    config["notifications"]["daily_brief"].update(
        {
            "max_actions_per_priority": "2",
            "max_candidates_per_strategy": 0,
            "max_rejection_reasons": 99,
        }
    )
    bundle = _request(tmp_path, run_id="run-audit", no_send=True, config=config)

    preparation = mod._prepare_daily_brief_notification(bundle.request)
    message = preparation.prepared_messages.messages_by_account["lx"]
    prepared = bundle.request.tick_metrics["daily_brief"]["prepared"][0]

    assert prepared["brief_id"] == preparation.lifecycles_by_account["lx"]["brief"]["brief_id"]
    assert prepared["message_sha256"] == hashlib.sha256(message.encode("utf-8")).hexdigest()
    assert prepared["message_chars"] == len(message)
    assert prepared["render_limits"] == {
        "max_actions_per_priority": 2,
        "max_candidates_per_strategy": 1,
        "max_rejection_reasons": 20,
    }
    audit_event = next(
        item
        for item in bundle.request.audit_helper.events
        if item["event_type"] == "daily_brief" and item["action"] == "prepared"
    )
    assert audit_event["extra"]["message_sha256"] == prepared["message_sha256"]
    assert "message" not in audit_event["extra"]


def test_scheduled_notification_renders_batch_and_localized_data_time(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace

    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    bundle = _request(
        tmp_path,
        run_id="run-scheduled-context",
        config={
            **_config(),
            "schedule": {"timezone": "America/New_York"},
        },
    )
    bundle.request = replace(
        bundle.request,
        scheduler_decision={
            "in_run_window": True,
            "scheduled_target_market": "2026-07-19T10:00:00-04:00",
        },
        trigger_kind="scheduled",
    )

    prepared = mod._prepare_daily_brief_notification(bundle.request)
    message = prepared.prepared_messages.messages_by_account["lx"]

    assert "状态｜今日首次 · 10:00 批次" in message
    assert "数据｜美东 09:39 / 北京 21:39" in message
    assert "2026-07-19T" not in message
    lifecycle = prepared.lifecycles_by_account["lx"]
    assert "scheduled_target_market" not in lifecycle["brief"]
    assert "scheduled_target_market" not in lifecycle["diff"]
    assert "10:00:00-04:00" not in lifecycle["delivery_key"]


def test_force_notification_says_manual_and_ignores_scheduler_batch(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace

    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    bundle = _request(tmp_path, run_id="run-force-context")
    bundle.request = replace(
        bundle.request,
        scheduler_decision={
            "in_run_window": True,
            "scheduled_target_market": "2026-07-19T10:00:00-04:00",
        },
        trigger_kind="force",
    )

    prepared = mod._prepare_daily_brief_notification(bundle.request)
    message = prepared.prepared_messages.messages_by_account["lx"]

    assert "状态｜手动触发" in message
    assert "10:00 批次" not in message
