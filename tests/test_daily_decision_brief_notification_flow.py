from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
    read_authority_resolution,
)
from src.application.position_advice_notification_authority import (
    build_fixed_failure_notification_authority_token,
    build_notification_authority_token,
)


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


def _portfolio_identity_hash(account: str) -> str:
    return canonical_sha256(
        {
            "normalized_portfolio_source": "futu",
            "normalized_account": account,
            "broker_account_id": f"test-{account}",
        }
    )


def _ensure_v1_authority(base: Path, account: str) -> None:
    identity_hash = _portfolio_identity_hash(account)
    resolution = read_authority_resolution(
        base=base,
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity_hash,
    )
    if resolution.resolution_status == "resolved":
        return
    binding = build_identity_binding_evidence(
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity_hash,
        authoring_config_hash="b" * 64,
        market_bindings=[
            {
                "market": "US",
                "generated_config_hash": "c" * 64,
                "source_receipt_hash": "d" * 64,
                "normalized_account": account,
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": identity_hash,
                "source_receipt_fresh": True,
            }
        ],
    )
    apply_authority_change(
        base=base,
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity_hash,
        target_mode="v1",
        expected_policy_hash="absent",
        actor="test",
        requested_at="2026-07-21T13:00:00+00:00",
        confirm=True,
        identity_binding_evidence=binding,
    )


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
    resolution = read_authority_resolution(
        base=base,
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=_portfolio_identity_hash(account),
    )
    assert resolution.resolution_status == "resolved"
    notification_token = build_notification_authority_token(
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=_portfolio_identity_hash(account),
        selected_advice_contract="v1",
        resolved_mode="v1",
        authority_generation=resolution.generation,
        authority_policy_hash=resolution.policy_hash,
        account_run_id=run_id,
    )
    failure_token = build_fixed_failure_notification_authority_token(
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=_portfolio_identity_hash(account),
        selected_advice_contract="v1",
        resolved_mode="v1",
        authority_generation=resolution.generation,
        authority_policy_hash=resolution.policy_hash,
        account_run_id=run_id,
    )
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
        "notification_authority": {
            "selected_advice_contract": "v1",
            "resolved_mode": "v1",
            "authority_generation": resolution.generation,
            "authority_policy_hash": resolution.policy_hash,
            "normal_delivery_allowed": True,
            "fixed_failure_delivery_allowed": True,
            "notification_allowed": True,
            "blocker": None,
            "normal_delivery_token": notification_token,
            "fixed_failure_delivery_token": failure_token,
            "token": notification_token,
            "failure_authority_resolution_status": (
                resolution.resolution_status
            ),
            "failure_authority_resolved_mode": resolution.mode,
            "failure_authority_generation": resolution.generation,
            "failure_authority_policy_hash": resolution.policy_hash,
            "identity_evidence": {
                "status": "available",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": (
                    _portfolio_identity_hash(account)
                ),
            },
        },
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
):
    import src.application.tick_notification_flow as mod
    from src.application.multi_tick.misc import AccountResult

    for account in accounts:
        _ensure_v1_authority(tmp_path, account)
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
    assert "**NVDA｜Sell Put｜08-21 $100 Put（首选）**" in card_markdown
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
    assert "数据异常" in envelope["rendered_message"]
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"] is False


def test_pending_fixed_failure_without_provider_attempt_upgrades_to_fixed_report(
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

    assert recovered_envelope["delivery_kind"] == "fixed_report"
    assert recovered_envelope["render_context"][
        "notification_authority_token"
    ]["account_run_id"] == "recovered-before-provider"
    assert audit["pending_failure_upgrade_status"] == "clear"
    assert audit["pending_failure_upgrade_applied"] is True
    assert audit["selected_delivery_kind"] == "fixed_report"


def test_pending_fixed_failure_after_definite_failure_upgrades_to_fixed_report(
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
    assert envelope["delivery_kind"] == "fixed_report"
    assert envelope["status"] == "confirmed"
    assert len(failed_calls) >= 1
    assert all("数据异常" in call["message"] for call in failed_calls)
    assert len(recovered_calls) == 1
    assert "数据异常" not in recovered_calls[0]["message"]
    assert audit["pending_failure_upgrade_status"] == "clear"
    assert audit["pending_failure_upgrade_applied"] is True


def test_provider_accepted_unconfirmed_failure_is_not_upgraded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )
    from src.application.position_advice_notification_authority import (
        execute_notification_with_authority,
    )

    _patch_assembler(monkeypatch, blocked=True)
    failed = _request(
        tmp_path,
        run_id="accepted-before-confirm",
        pipeline_ok=False,
    )
    failed_prep = mod._prepare_daily_brief_notification(failed.request)
    failed_envelope = failed_prep.lifecycles_by_account["lx"]["envelope"]
    failed_token = failed_envelope["render_context"][
        "notification_authority_token"
    ]
    provider_calls: list[str] = []
    accepted = execute_notification_with_authority(
        base=tmp_path,
        token=failed_token,
        channel="wechat_clawbot",
        delivery_identity={
            "account": "lx",
            "market": "US",
            "market_trading_date": MARKET_DATE,
            "delivery_key": failed_envelope["delivery_key"],
            "source_digest": failed_envelope["source_digest"],
            "message_sha256": failed_envelope["message_sha256"],
            "transport_idempotency_key": (
                build_notification_transport_key(
                    failed_envelope["delivery_key"]
                )
            ),
            "delivery_kind": "fixed_failure",
        },
        send=lambda: provider_calls.append("sent") or {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
        },
    )
    assert accepted["authority_receipt_status"] == "accepted"
    assert provider_calls == ["sent"]

    _patch_assembler(monkeypatch)
    recovered = _request(tmp_path, run_id="accepted-recovery")
    recovered_prep = mod._prepare_daily_brief_notification(
        recovered.request
    )
    selected = recovered_prep.lifecycles_by_account["lx"]["envelope"]
    audit = recovered.request.tick_metrics["daily_brief"]["prepared"][0]

    assert selected["delivery_kind"] == "fixed_failure"
    assert selected["render_context"]["notification_authority_token"][
        "account_run_id"
    ] == "accepted-before-confirm"
    assert audit["pending_failure_upgrade_status"] == "accepted"
    assert audit["pending_failure_upgrade_applied"] is False
    assert audit["selected_delivery_kind"] == "fixed_failure"


def test_ambiguous_fixed_failure_is_not_upgraded_or_resent(
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
    assert mod.run_tick_notification_flow(recovered.request) == 1

    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    envelope = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    audit = recovered.request.tick_metrics["daily_brief"]["prepared"][0]
    assert len(ambiguous_calls) >= 1
    assert recovered_calls == []
    assert envelope["delivery_kind"] == "fixed_failure"
    assert envelope["status"] == "ambiguous"
    assert envelope["render_context"]["notification_authority_token"][
        "account_run_id"
    ] == "ambiguous-provider-attempt"
    assert audit["pending_failure_upgrade_status"] == "not_applicable"
    assert audit["pending_failure_upgrade_applied"] is False
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
            if account == "sy":
                authority = dict(brief["notification_authority"])
                authority.update(
                    {
                        "selected_advice_contract": None,
                        "normal_delivery_allowed": False,
                        "notification_allowed": False,
                        "blocker": (
                            "position_advice_source_summary_missing"
                        ),
                        "normal_delivery_token": None,
                        "token": None,
                        "authority_identity_source": (
                            "current_run_portfolio_receipt"
                        ),
                        "identity_snapshot_id": "b" * 64,
                        "identity_receipt_hash": "c" * 64,
                    }
                )
                brief["notification_authority"] = authority
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
    assert prepared["lx"]["normal_report_reliable"] is True
    assert prepared["sy"]["decision"] == "fixed_failure"
    assert prepared["sy"]["normal_report_reliable"] is False
    assert prepared["sy"]["fixed_failure_delivery_allowed"] is True
    assert prepared["sy"]["authority_identity_source"] == (
        "current_run_portfolio_receipt"
    )


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


def test_pipeline_failure_nonfixed_is_quiet_but_commits_after_failure_artifact(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch, blocked=True)
    bundle = _request(tmp_path, run_id="failed-half", fixed=False, pipeline_ok=False)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert bundle.commits == [{"lx": HALF_TARGET}]
    assert not bundle.request.tick_metrics["daily_brief"]["prepared"][0]["delivery_key"]


def test_commit_failure_prevents_provider_call_and_keeps_envelope(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
        read_retryable_daily_decision_brief_delivery,
    )

    _patch_assembler(monkeypatch)
    calls: list[dict] = []
    _patch_sender(monkeypatch, calls=calls)
    bundle = _request(tmp_path, run_id="commit-fail")
    bundle.request = replace(bundle.request, commit_scan_targets_fn=lambda _targets: (_ for _ in ()).throw(OSError("state write failed")))
    with pytest.raises(OSError, match="state write failed"):
        mod.run_tick_notification_flow(bundle.request)
    assert calls == []
    pending = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )["envelope"]
    assert pending

    retry_calls: list[dict] = []
    _patch_sender(monkeypatch, calls=retry_calls)
    retry = _request(tmp_path, run_id="commit-recovery")
    assert mod.run_tick_notification_flow(retry.request) == 0
    assert retry.commits == [{"lx": FIXED_TARGET}]
    assert retry_calls[0]["message"] == pending["rendered_message"]
    delivery = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    confirmed = delivery["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert confirmed["delivery_key"] == pending["delivery_key"]
    assert confirmed["message_sha256"] == pending["message_sha256"]
    assert confirmed["status"] == "confirmed"


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


def test_scheduled_renderer_uses_batch_time_without_leaking_revision(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    _patch_assembler(monkeypatch)
    prep = mod._prepare_daily_brief_notification(_request(tmp_path, run_id="render").request)
    message = prep.prepared_messages.messages_by_account["lx"]
    assert "10:00 批次" in message
    assert "状态｜10:00 批次" in message
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
