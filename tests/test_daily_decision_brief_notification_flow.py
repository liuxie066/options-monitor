from __future__ import annotations

from dataclasses import replace
import json
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


def _brief(
    *,
    base: Path,
    run_id: str,
    account: str = "lx",
    market: str = "US",
    blocked: bool = False,
    candidate: bool = True,
) -> dict:
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


def _feishu_config() -> dict:
    return {
        "notifications": {
            "provider": "feishu_app",
            "channel": "feishu_app",
            "target": "feishu:bot-user",
            "daily_brief": {"enabled": True},
        },
        "schedule": {"timezone": "America/New_York"},
    }


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
    trigger_kind: str = "scheduled",
    markets_to_run: tuple[str, ...] = ("US",),
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
            "should_run_scan": not delivery_only,
            "now_utc": (
                "2026-07-21T14:00:30Z" if fixed else "2026-07-21T14:30:30Z"
            ),
            "now_market": "2026-07-21T10:10:00-04:00" if delivery_only else target,
            "scheduled_scan_target_market": None if delivery_only else target,
            "scheduled_target_market": None if delivery_only or not fixed else target,
        }
        for account in accounts
    }
    request = mod.TickNotificationRequest(
        base=tmp_path,
        cfg_path=tmp_path / f"config.{markets_to_run[0].lower()}.json",
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
        markets_to_run=markets_to_run,
        scheduler_markets=markets_to_run,
        scheduler_decision={"in_run_window": True, "now_market": scheduler_by_account[accounts[0]]["now_market"]},
        ran_pipeline_accounts=accounts if pipeline_ok and not delivery_only else (),
        account_ids=accounts,
        scheduler_decisions_by_account=scheduler_by_account,
        scheduled_scan_targets_by_account={} if delivery_only else {account: target for account in accounts},
        commit_scan_targets_fn=lambda value: commits.append(dict(value)),
        delivery_only=delivery_only,
        trigger_kind=trigger_kind,
    )
    return SimpleNamespace(request=request, completions=completions, commits=commits)


def _patch_assembler(monkeypatch, *, blocked: bool = False, candidate: bool = True) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda *, base, run_id, account, markets_to_run, **_kwargs: {
            market: _brief(base=base, run_id=run_id, account=account, market=market, blocked=blocked, candidate=candidate)
            for market in markets_to_run
        },
    )


def _patch_sender(
    monkeypatch,
    *,
    result: dict | None = None,
    calls: list[dict] | None = None,
    order: list[str] | None = None,
) -> None:
    import src.application.tick_notification_flow as mod

    def send(**kwargs):
        if order is not None:
            order.append("provider")
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


def test_scheduled_daily_brief_ignores_deprecated_enabled_switch(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    enabled = mod._prepare_daily_brief_notification(
        _request(tmp_path / "enabled", run_id="enabled", config=_config(enabled=True)).request
    )
    disabled = mod._prepare_daily_brief_notification(
        _request(tmp_path / "disabled", run_id="disabled", config=_config(enabled=False)).request
    )

    assert enabled.lifecycles_by_account["lx"]["envelope"]["delivery_kind"] == "fixed_report"
    assert disabled.lifecycles_by_account["lx"]["envelope"]["delivery_kind"] == "fixed_report"
    assert disabled.prepared_messages.threshold_met is True


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
    assert bundle.commits == []


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
    assert "transport_envelope" not in calls[0]
    assert bundle.commits == [{"lx": FIXED_TARGET}]


def test_feishu_fixed_scan_persists_and_sends_exact_card_transport(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
    )

    _patch_assembler(monkeypatch)
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_test")
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(
        tmp_path,
        run_id="fixed-feishu-card",
        config=_feishu_config(),
    )

    assert mod.run_tick_notification_flow(bundle.request) == 0
    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    envelope = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    transport = envelope["rendered_transport"]

    assert envelope["status"] == "confirmed"
    assert envelope["rendered_transport_sha256"]
    assert transport["schema_version"] == "feishu-proactive-notification.v1"
    assert transport["render_mode"] == "card_markdown_v2"
    assert transport["text"] == envelope["rendered_message"]
    assert transport["render_meta"]["markdown_table_detected"] is False
    assert calls[0]["transport_envelope"] == transport
    card_markdown = transport["transport"]["content"]["body"]["elements"][0]["content"]
    assert "| 优先 | 合约 | 权利金 / 净收入 | 年化 | 风险 / 容量 |" not in card_markdown
    assert "**NVDA｜Sell Put｜08-21 $100 Put（策略排序 1）**" in card_markdown
    assert "指标｜权利金 $1.20" in card_markdown
    assert "现金总额｜$100,000.00" in card_markdown
    assert "可用于期权开仓｜$60,000.00" in card_markdown
    assert "| 项目 | 数值 |" not in card_markdown


def test_confirmed_delivery_records_degraded_evidence_when_tick_metrics_write_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    _patch_sender(monkeypatch)
    monkeypatch.setattr(
        mod.state_repo,
        "write_tick_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("metrics unavailable")
        ),
    )
    bundle = _request(tmp_path, run_id="metrics-degraded")

    assert mod.run_tick_notification_flow(bundle.request) == 0
    degraded = [
        event
        for event in bundle.request.runlog.events
        if event.get("step") == "finalize"
        and event.get("status") == "degraded"
    ]
    assert degraded
    assert degraded[-1]["data"]["action"] == "write_tick_metrics"
    assert degraded[-1]["data"]["notification_delivery_confirmed"] is True
    assert any(
        event.get("action") == "write_tick_metrics"
        and event.get("status") == "error"
        for event in bundle.request.audit_helper.events
    )


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
    assert "现金总额｜$100,000.00" in envelope["rendered_message"]
    assert "## 持仓" not in envelope["rendered_message"]


def test_pipeline_failure_fixed_sends_explicit_failure_without_advancing_current(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_latest_daily_decision_brief

    _patch_assembler(monkeypatch, blocked=True)
    bundle = _request(tmp_path, run_id="failed", pipeline_ok=False)
    prep = mod._prepare_daily_brief_notification(bundle.request)
    envelope = prep.lifecycles_by_account["lx"]["envelope"]
    assert envelope["delivery_kind"] == "fixed_failure"
    assert "数据异常 · 22:00 批次失败" in envelope["rendered_message"]
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"] is False


def test_pending_fixed_failure_without_provider_attempt_is_preserved(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch, blocked=True)
    failed = _request(
        tmp_path,
        run_id="failed-before-provider",
        pipeline_ok=False,
    )
    failed_prep = mod._prepare_daily_brief_notification(failed.request)
    failed_envelope = failed_prep.lifecycles_by_account["lx"]["envelope"]
    assert failed_envelope["delivery_kind"] == "fixed_failure"

    _patch_assembler(monkeypatch)
    recovered = _request(tmp_path, run_id="recovered-before-provider")
    recovered_prep = mod._prepare_daily_brief_notification(
        recovered.request
    )
    recovered_envelope = recovered_prep.lifecycles_by_account["lx"][
        "envelope"
    ]
    audit = recovered.request.tick_metrics["daily_brief"]["prepared"][0]

    assert recovered_envelope["delivery_kind"] == "fixed_failure"
    assert "notification_authority_token" not in recovered_envelope["render_context"]
    assert audit["pending_delivery_status"] == "existing_pending_preserved"
    assert audit["selected_delivery_kind"] == "fixed_failure"


def test_pending_fixed_failure_after_definite_failure_is_retried_unchanged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
    )

    _patch_assembler(monkeypatch, blocked=True)
    failed_calls: list[dict] = []
    _patch_sender(
        monkeypatch,
        calls=failed_calls,
        result={
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 1,
            "error_code": "SEND_FAILED",
        },
    )
    failed = _request(
        tmp_path,
        run_id="failed-provider-attempt",
        pipeline_ok=False,
    )
    assert mod.run_tick_notification_flow(failed.request) == 1

    _patch_assembler(monkeypatch)
    recovered_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=recovered_calls)
    recovered = _request(tmp_path, run_id="recovered-provider-attempt")
    assert mod.run_tick_notification_flow(recovered.request) == 0

    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    envelope = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    audit = recovered.request.tick_metrics["daily_brief"]["prepared"][0]
    assert envelope["delivery_kind"] == "fixed_failure"
    assert envelope["status"] == "confirmed"
    assert len(failed_calls) >= 1
    assert all("数据异常" in call["message"] for call in failed_calls)
    assert len(recovered_calls) == 1
    assert "数据异常" in recovered_calls[0]["message"]
    assert audit["pending_delivery_status"] == "existing_pending_preserved"


def test_ambiguous_fixed_failure_retries_same_frozen_delivery_idempotently(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
    )

    _patch_assembler(monkeypatch, blocked=True)
    ambiguous_calls: list[dict] = []
    _patch_sender(
        monkeypatch,
        calls=ambiguous_calls,
        result={
            "ok": False,
            "command_ok": True,
            "delivery_confirmed": False,
            "returncode": 1,
            "error_code": "SEND_UNCONFIRMED",
            "ambiguous_send": True,
        },
    )
    failed = _request(
        tmp_path,
        run_id="ambiguous-provider-attempt",
        pipeline_ok=False,
    )
    assert mod.run_tick_notification_flow(failed.request) == 1

    _patch_assembler(monkeypatch)
    recovered_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=recovered_calls)
    recovered = _request(tmp_path, run_id="ambiguous-recovery")
    assert mod.run_tick_notification_flow(recovered.request) == 0

    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    envelope = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    audit = recovered.request.tick_metrics["daily_brief"]["prepared"][0]
    assert len(ambiguous_calls) >= 1
    assert len(recovered_calls) == 1
    assert recovered_calls[0]["idempotency_key"] == ambiguous_calls[-1]["idempotency_key"]
    assert envelope["delivery_kind"] == "fixed_failure"
    assert envelope["status"] == "confirmed"
    assert "notification_authority_token" not in envelope["render_context"]
    assert audit["pending_delivery_status"] == "existing_pending_preserved"
    assert audit["selected_delivery_kind"] == "fixed_failure"


def test_lx_normal_and_sy_failure_are_prepared_and_sent_independently(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
    )

    def assemble(
        *,
        base: Path,
        run_id: str,
        account: str,
        markets_to_run: list[str],
        **_kwargs,
    ) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for market in markets_to_run:
            brief = _brief(
                base=base,
                run_id=run_id,
                account=account,
                market=market,
                blocked=account == "sy",
            )
            out[market] = brief
        return out

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", assemble)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(
        tmp_path,
        run_id="mixed-authority",
        accounts=("lx", "sy"),
    )

    assert mod.run_tick_notification_flow(bundle.request) == 0

    lx_state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    sy_state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="sy",
        market="US",
    )["state"]
    lx_envelope = lx_state["days"][MARKET_DATE]["fixed_reports"][
        FIXED_TARGET
    ]
    sy_envelope = sy_state["days"][MARKET_DATE]["fixed_reports"][
        FIXED_TARGET
    ]

    assert lx_envelope["delivery_kind"] == "fixed_report"
    assert lx_envelope["status"] == "confirmed"
    assert sy_envelope["delivery_kind"] == "fixed_failure"
    assert sy_envelope["status"] == "confirmed"
    assert sy_envelope["rendered_transport"] is None
    assert sy_envelope["candidate_identities"] == []
    assert "数据异常" in sy_envelope["rendered_message"]
    assert len(calls) == 2
    prepared = {
        item["account"]: item
        for item in bundle.request.tick_metrics["daily_brief"]["prepared"]
    }
    assert prepared["lx"]["decision"] == "fixed_report"
    assert prepared["lx"]["pipeline_reliable"] is True
    assert prepared["sy"]["decision"] == "fixed_failure"
    assert prepared["sy"]["pipeline_reliable"] is False


def test_fixed_report_without_candidates_still_contains_positions_and_funds(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch, candidate=False)
    bundle = _request(tmp_path, run_id="fixed-empty")
    prep = mod._prepare_daily_brief_notification(bundle.request)
    message = prep.lifecycles_by_account["lx"]["envelope"]["rendered_message"]

    assert "本轮暂无符合条件的候选" in message
    assert "## 持仓" in message
    assert "## 资金" in message
    assert "现金总额｜$100,000.00" in message


def test_pipeline_failure_nonfixed_is_quiet_and_keeps_scan_retryable(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch, blocked=True)
    bundle = _request(tmp_path, run_id="failed-half", fixed=False, pipeline_ok=False)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert bundle.commits == []
    assert not bundle.request.tick_metrics["daily_brief"]["prepared"][0]["delivery_key"]


def test_commit_failure_prevents_provider_call_and_leaves_brief_recoverable(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
        read_latest_daily_decision_brief,
        read_retryable_daily_decision_brief_delivery,
    )

    _patch_assembler(monkeypatch)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    observer_calls: list[str] = []
    monkeypatch.setattr(mod, "source_commit_sha", lambda _root: "c" * 40)
    monkeypatch.setattr(
        mod,
        "capture_scheduled_recommendation_point",
        lambda *_args, **_kwargs: observer_calls.append("observer"),
    )
    bundle = _request(tmp_path, run_id="commit-fail")
    bundle.request = replace(bundle.request, commit_scan_targets_fn=lambda _targets: (_ for _ in ()).throw(OSError("state write failed")))
    with pytest.raises(OSError, match="state write failed"):
        mod.run_tick_notification_flow(bundle.request)
    assert calls == []
    assert observer_calls == []
    pending = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )["envelope"]
    assert pending is None
    assert read_latest_daily_decision_brief(
        base=tmp_path,
        account="lx",
        market="US",
    )["available"] is True

    retry_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=retry_calls)
    retry = _request(tmp_path, run_id="commit-recovery")
    assert mod.run_tick_notification_flow(retry.request) == 0
    assert retry.commits == [{"lx": FIXED_TARGET}]
    delivery = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    confirmed = delivery["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert retry_calls[0]["message"] == confirmed["rendered_message"]
    assert confirmed["status"] == "confirmed"


@pytest.mark.parametrize("observer_fails", (False, True))
def test_recommendation_point_observer_runs_after_provider_and_is_best_effort(
    monkeypatch,
    tmp_path: Path,
    observer_fails: bool,
) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    order: list[str] = []
    _patch_sender(monkeypatch, order=order)
    monkeypatch.setattr(mod, "source_commit_sha", lambda _root: "c" * 40)

    def capture(*_args, **_kwargs):
        order.append("observer")
        if observer_fails:
            raise mod.RecommendationPointError(
                "official_point_unavailable",
                "injected observer failure",
            )
        return "published", {"recommendation_point_id": "p" * 64}

    monkeypatch.setattr(mod, "capture_scheduled_recommendation_point", capture)
    bundle = _request(
        tmp_path,
        run_id=f"observer-{observer_fails}",
        markets_to_run=("HK",),
    )
    complete_tick = bundle.request.complete_tick_idempotency_fn

    def complete(**kwargs):
        order.append("complete")
        complete_tick(**kwargs)

    bundle.request = replace(
        bundle.request,
        commit_scan_targets_fn=lambda _targets: order.append("commit"),
        post_delivery_sidecars_fn=lambda: order.append("runtime_snapshot"),
        complete_tick_idempotency_fn=complete,
    )

    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert order == [
        "commit",
        "provider",
        "observer",
        "runtime_snapshot",
        "complete",
    ]
    actions = [event["action"] for event in bundle.request.audit_helper.events]
    assert (
        "recommendation_point_gap"
        if observer_fails
        else "recommendation_point_captured"
    ) in actions
    latency_stages = [
        event.get("data", {}).get("stage")
        for event in bundle.request.runlog.events
        if event.get("step") == "tick_latency"
    ]
    assert latency_stages == [
        "scheduler_target_commit",
        "daily_brief_prepare",
        "provider_delivery",
        "recommendation_point_observer",
    ]


def test_post_delivery_sidecar_failure_is_degraded_before_tick_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    _patch_sender(monkeypatch)
    order: list[str] = []
    bundle = _request(tmp_path, run_id="sidecar-degraded")
    complete_tick = bundle.request.complete_tick_idempotency_fn

    def fail_sidecar() -> None:
        order.append("sidecar")
        raise OSError("snapshot unavailable")

    def complete(**kwargs) -> None:
        order.append("complete")
        complete_tick(**kwargs)

    bundle.request = replace(
        bundle.request,
        post_delivery_sidecars_fn=fail_sidecar,
        complete_tick_idempotency_fn=complete,
    )

    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert order == ["sidecar", "complete"]
    degraded = [
        event
        for event in bundle.request.audit_helper.events
        if event["action"] == "post_delivery_sidecars_failed"
    ]
    assert degraded[0]["status"] == "degraded"
    assert degraded[0]["extra"] == {"exception_type": "OSError"}


@pytest.mark.parametrize(
        "case",
        (
            "manual",
        "force",
        "delivery_only",
        "not_run",
        "target_missing",
        "target_mismatch",
        "source_unavailable",
    ),
)
def test_recommendation_point_observer_excludes_ineligible_paths(
    monkeypatch,
    tmp_path: Path,
    case: str,
) -> None:
    import src.application.tick_notification_flow as mod

    bundle = _request(
        tmp_path,
        run_id=f"observer-excluded-{case}",
        delivery_only=case == "delivery_only",
        trigger_kind=case if case in {"manual", "force"} else "scheduled",
        markets_to_run=("HK",),
    )
    if case == "not_run":
        bundle.request = replace(bundle.request, ran_pipeline_accounts=())
    if case == "target_mismatch":
        bundle.request = replace(
            bundle.request,
            scheduled_scan_targets_by_account={"lx": HALF_TARGET},
        )
    if case == "target_missing":
        bundle.request = replace(
            bundle.request,
            scheduled_scan_targets_by_account={},
        )
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "source_commit_sha",
        lambda _root: None if case == "source_unavailable" else "c" * 40,
    )
    monkeypatch.setattr(
        mod,
        "capture_scheduled_recommendation_point",
        lambda *_args, **_kwargs: calls.append("capture"),
    )

    mod._observe_recommendation_points_best_effort(bundle.request)

    assert calls == []


@pytest.mark.parametrize("market", ("HK", "US"))
def test_recommendation_point_observer_scopes_base_corpus_to_lx(
    monkeypatch,
    tmp_path: Path,
    market: str,
) -> None:
    import src.application.tick_notification_flow as mod

    bundle = _request(
        tmp_path,
        run_id="observer-account-isolation",
        accounts=("lx", "sy"),
        markets_to_run=(market,),
    )
    bundle.request = replace(
        bundle.request,
        ran_pipeline_accounts=("lx", "lx", "sy"),
    )
    calls: list[str] = []
    monkeypatch.setattr(mod, "source_commit_sha", lambda _root: "c" * 40)

    def capture(_base, _run_id, account, _decision, **_kwargs):
        calls.append(account)
        raise mod.RecommendationPointError(
            "official_point_unavailable",
            "injected account failure",
        )

    monkeypatch.setattr(mod, "capture_scheduled_recommendation_point", capture)

    mod._observe_recommendation_points_best_effort(bundle.request)

    assert calls == ["lx"]
    point_events = [
        event
        for event in bundle.request.audit_helper.events
        if event["action"].startswith("recommendation_point_")
    ]
    assert [event["extra"]["account"] for event in point_events] == ["lx"]
    assert [event["status"] for event in point_events] == ["degraded"]


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


@pytest.mark.parametrize("retry_status", ("pending", "ambiguous"))
def test_delivery_only_blocks_retired_ai_payload_without_mutating_retry_state(
    monkeypatch,
    tmp_path: Path,
    retry_status: str,
) -> None:
    import src.application.tick_notification_flow as mod
    from domain.domain.daily_decision_brief import daily_brief_compatible_digests
    from src.application.daily_decision_brief_repository import (
        read_retryable_daily_decision_brief_delivery,
    )

    _patch_assembler(monkeypatch)
    first_calls: list[dict] = []
    _patch_sender(
        monkeypatch,
        calls=first_calls,
        result={
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 1,
            "error_code": "SEND_FAILED",
        },
    )
    first = _request(tmp_path, run_id="retired-ai-seed")
    assert mod.run_tick_notification_flow(first.request) == 1
    retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    envelope = retry["envelope"]
    assert envelope["status"] == "pending"

    revision_path = (
        tmp_path
        / "output_accounts"
        / "lx"
        / "state"
        / f"daily_decision_brief.US.{MARKET_DATE}.r{envelope['revision']:04d}.json"
    )
    historical = json.loads(revision_path.read_text(encoding="utf-8"))
    historical["ai_decision_advice"] = {"status": "completed"}
    historical["ai_decision_advice_evidence_index"] = {"symbols": []}
    revision_path.write_text(json.dumps(historical), encoding="utf-8")
    legacy_digest = daily_brief_compatible_digests(historical)[-1]

    delivery_path = retry["path"]
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    delivery["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET][
        "source_digest"
    ] = legacy_digest
    delivery["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET][
        "status"
    ] = retry_status
    delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
    state_before = delivery_path.read_bytes()

    retry_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=retry_calls)
    second = _request(tmp_path, run_id="retired-ai-delivery-only", delivery_only=True)

    assert mod.run_tick_notification_flow(second.request) == 2
    assert retry_calls == []
    assert delivery_path.read_bytes() == state_before
    audit = second.request.tick_metrics["daily_brief"]["prepared"][0]
    assert audit["delivery_key"] == envelope["delivery_key"]
    assert audit["error_code"] == "legacy_ai_payload_retired"
    assert audit["blocked"] is True
    assert second.request.audit_helper.failures == [
        ("legacy_ai_payload_retired", "daily_brief_retry_guard")
    ]
    assert second.completions == [
        {
            "status": "unsupported_failed",
            "message": "legacy_ai_payload_retired",
            "ok": False,
            "error_code": "legacy_ai_payload_retired",
        }
    ]

    post_scan_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=post_scan_calls)
    post_scan = _request(tmp_path, run_id="retired-ai-post-scan")
    assert mod.run_tick_notification_flow(post_scan.request) == 2
    assert post_scan_calls == []
    assert delivery_path.read_bytes() == state_before
    current_path = (
        tmp_path
        / "output_accounts"
        / "lx"
        / "state"
        / "daily_decision_brief.US.current.json"
    )
    assert json.loads(current_path.read_text(encoding="utf-8"))["run_id"] == (
        "retired-ai-post-scan"
    )
    post_scan_audit = post_scan.request.tick_metrics["daily_brief"]["prepared"][0]
    assert post_scan_audit["delivery_key"] == envelope["delivery_key"]
    assert post_scan_audit["error_code"] == "legacy_ai_payload_retired"


def test_retired_ai_blocker_does_not_suppress_clean_account_delivery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from domain.domain.daily_decision_brief import daily_brief_compatible_digests
    from src.application.daily_decision_brief_repository import (
        read_retryable_daily_decision_brief_delivery,
    )

    _patch_assembler(monkeypatch)
    _patch_sender(
        monkeypatch,
        result={
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 1,
            "error_code": "SEND_FAILED",
        },
    )
    seed = _request(
        tmp_path,
        run_id="retired-ai-mixed-seed",
        accounts=("lx", "sy"),
    )
    assert mod.run_tick_notification_flow(seed.request) == 1

    lx_retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    lx_envelope = lx_retry["envelope"]
    revision_path = (
        tmp_path
        / "output_accounts"
        / "lx"
        / "state"
        / f"daily_decision_brief.US.{MARKET_DATE}.r{lx_envelope['revision']:04d}.json"
    )
    historical = json.loads(revision_path.read_text(encoding="utf-8"))
    historical["ai_decision_advice"] = {"status": "completed"}
    historical["ai_decision_advice_evidence_index"] = {"symbols": []}
    revision_path.write_text(json.dumps(historical), encoding="utf-8")
    delivery = json.loads(lx_retry["path"].read_text(encoding="utf-8"))
    delivery["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET][
        "source_digest"
    ] = daily_brief_compatible_digests(historical)[-1]
    lx_retry["path"].write_text(json.dumps(delivery), encoding="utf-8")
    lx_state_before = lx_retry["path"].read_bytes()

    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    retry = _request(
        tmp_path,
        run_id="retired-ai-mixed-retry",
        accounts=("lx", "sy"),
        delivery_only=True,
    )

    assert mod.run_tick_notification_flow(retry.request) == 1
    assert len(calls) == 1
    assert "sy" in calls[0]["message"]
    assert "lx" not in calls[0]["message"]
    assert lx_retry["path"].read_bytes() == lx_state_before
    assert retry.completions == [
        {
            "status": "unsupported_failed",
            "message": "legacy_ai_payload_retired",
            "ok": False,
            "error_code": "legacy_ai_payload_retired",
        }
    ]
    assert retry.request.audit_helper.failures == [
        ("legacy_ai_payload_retired", "daily_brief_retry_guard")
    ]


def test_feishu_delivery_only_retry_reuses_frozen_card_transport(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_retryable_daily_decision_brief_delivery,
    )

    _patch_assembler(monkeypatch)
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_test")
    first_calls: list[dict] = []
    _patch_sender(
        monkeypatch,
        calls=first_calls,
        result={
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 1,
            "error_code": "SEND_FAILED",
        },
    )
    first = _request(
        tmp_path,
        run_id="feishu-card-send-fail",
        config=_feishu_config(),
    )
    assert mod.run_tick_notification_flow(first.request) == 1
    retry_before = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )["envelope"]

    retry_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=retry_calls)
    second = _request(
        tmp_path,
        run_id="feishu-card-delivery-only",
        delivery_only=True,
        config=_feishu_config(),
    )
    assert mod.run_tick_notification_flow(second.request) == 0

    assert retry_calls[0]["message"] == retry_before["rendered_message"]
    assert retry_calls[0]["transport_envelope"] == retry_before["rendered_transport"]
    assert retry_calls[0]["idempotency_key"] == first_calls[0]["idempotency_key"]


def test_delivery_only_no_send_keeps_pending_envelope_without_claiming_send(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery

    _patch_assembler(monkeypatch)
    seed = _request(tmp_path, run_id="seed-pending")
    mod._prepare_daily_brief_notification(seed.request)

    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    retry = _request(tmp_path, run_id="delivery-only-no-send", delivery_only=True, no_send=True)

    assert mod.run_tick_notification_flow(retry.request) == 0
    assert calls == []
    assert retry.completions == [{"status": "skipped", "message": "delivery_only_no_send"}]
    pending = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert pending["reason"] == "pending_fixed"
    assert pending["envelope"]["status"] == "pending"


def test_delivery_only_without_envelope_is_read_only_and_skips_assembler(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not assemble")))
    bundle = _request(tmp_path, run_id="delivery-only-empty", delivery_only=True)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert bundle.completions == [{"status": "skipped", "message": "no_retryable_delivery"}]
    assert not (tmp_path / "output_runs").exists()


def test_delivery_only_rebuilds_missing_envelope_from_committed_brief(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_success,
        read_daily_decision_brief_fixed_recovery,
        read_daily_decision_brief_delivery_state,
        read_latest_daily_decision_brief,
        read_retryable_daily_decision_brief_delivery,
    )
    from src.application.daily_decision_brief_renderer import (
        render_fixed_report as actual_render_fixed_report,
    )

    _patch_assembler(monkeypatch)
    first = _request(tmp_path, run_id="render-crash")
    monkeypatch.setattr(
        mod,
        "render_fixed_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )
    with pytest.raises(RuntimeError, match="render failed"):
        mod.run_tick_notification_flow(first.request)
    assert first.commits == [{"lx": FIXED_TARGET}]
    assert read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )["envelope"] is None
    recovery = read_daily_decision_brief_fixed_recovery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert recovery["available"] is True
    assert recovery["recovery"]["revision"] == 0
    original_digest = recovery["brief_digest"]

    advanced = persist_daily_decision_brief_success(
        base=tmp_path,
        brief=_brief(
            base=tmp_path,
            run_id="later-scan",
            candidate=False,
        ),
    )
    assert advanced["current_revision"] == 1
    assert advanced["current_brief_digest"] != original_digest
    assert read_latest_daily_decision_brief(
        base=tmp_path,
        account="lx",
        market="US",
    )["brief"]["revision"] == 1

    monkeypatch.setattr(mod, "render_fixed_report", actual_render_fixed_report)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    retry = _request(tmp_path, run_id="delivery-recovery", delivery_only=True)

    assert mod.run_tick_notification_flow(retry.request) == 0
    assert len(calls) == 1
    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    envelope = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert envelope["status"] == "confirmed"
    assert envelope["revision"] == 0
    assert envelope["source_digest"] == original_digest
    assert read_daily_decision_brief_fixed_recovery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )["available"] is False


def test_multi_market_scan_fails_before_snapshot_or_outbound(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_latest_daily_decision_brief

    monkeypatch.setattr(
        mod,
        "assemble_daily_decision_briefs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("multi-market must fail before assemble")),
    )
    bundle = _request(tmp_path, run_id="multi")
    bundle.request = replace(bundle.request, markets_to_run=("US", "HK"), scheduler_markets=("US", "HK"))
    prep = mod._prepare_daily_brief_notification(bundle.request)
    assert prep.multi_market_delivery_unsupported is True
    assert prep.prepared_messages.messages_by_account == {}
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"] is False
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="HK")["available"] is False


def test_scheduled_renderer_uses_beijing_batch_time_without_leaking_revision(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    prep = mod._prepare_daily_brief_notification(_request(tmp_path, run_id="render").request)
    message = prep.prepared_messages.messages_by_account["lx"]
    assert "22:00 批次" in message
    assert "状态｜22:00 批次" in message
    assert "数据｜美东 09:59 / 北京 21:59" in message
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


def test_later_half_hour_sends_new_candidate_after_prior_candidate_confirmation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import copy

    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery_state

    def assemble(*, base, run_id, account, markets_to_run, **_kwargs):
        brief = _brief(base=base, run_id=run_id, account=account)
        if run_id == "candidate-2":
            action = copy.deepcopy(brief["actions"][-1])
            action.update(
                {
                    "symbol": "AMD",
                    "contract_symbol": "AMD260821P00100000",
                }
            )
            candidate = copy.deepcopy(brief["candidates"]["sell_put"][-1])
            candidate.update(
                {
                    "symbol": "AMD",
                    "contract_symbol": "AMD260821P00100000",
                }
            )
            brief["actions"].append(action)
            brief["candidates"]["sell_put"].append(candidate)
        return {market: {**brief, "market": market} for market in markets_to_run}

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", assemble)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)

    first = _request(tmp_path, run_id="candidate-1", fixed=False)
    second = _request(tmp_path, run_id="candidate-2", fixed=False)
    assert mod.run_tick_notification_flow(first.request) == 0
    assert mod.run_tick_notification_flow(second.request) == 0

    assert len(calls) == 2
    assert "NVDA" in calls[0]["message"]
    assert "AMD" in calls[1]["message"]
    assert "NVDA" not in calls[1]["message"]
    day = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]["days"][MARKET_DATE]
    assert set(day["alerted_candidates"]) == {
        IDENTITY,
        "candidate:v1:lx:US:AMD:sell_put",
    }
    assert day["pending_candidates"] == {}


def test_multi_market_flow_records_terminal_failure_and_nonzero_exit(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    bundle = _request(tmp_path, run_id="multi-terminal")
    bundle.request = replace(
        bundle.request,
        markets_to_run=("US", "HK"),
        scheduler_markets=("US", "HK"),
    )
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **kwargs: int(kwargs.get("return_code") or 0))

    assert mod.run_tick_notification_flow(bundle.request) == 2
    assert bundle.completions == [{
        "status": "unsupported_failed",
        "message": "daily_brief_multi_market_delivery_unsupported",
        "ok": False,
        "error_code": "daily_brief_multi_market_delivery_unsupported",
    }]
