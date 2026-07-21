from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def _action(
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    priority: str = "P1",
    state: str = "active",
    mid: float = 1.0,
    contracts_available: int | None = 1,
    event_risk: dict | None = None,
) -> dict:
    return {
        "priority": priority,
        "state": state,
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "account": account,
        "symbol": symbol,
        "option_type": "put",
        "side": "short",
        "expiration": "2026-05-15",
        "strike": 100,
        "contract_symbol": f"{symbol}260515P00100000",
        "title": f"评估 {symbol} Sell Put",
        "reason": "收益/风险通过筛选",
        "metrics": {
            "mid": mid,
            **(
                {"capacity": {"contracts_available": contracts_available}}
                if contracts_available is not None
                else {}
            ),
        },
        **({"event_risk": event_risk} if event_risk is not None else {}),
    }


def _scenario_event_risk(state: str, *, fetched_at: str = "") -> dict:
    event = (
        {
            "event_id": "event-q2",
            "event_series_id": "event-series-earnings",
            "event_type": "earnings",
            "event_date": "2026-08-05",
            "occurrence_anchor": "2026|Q2",
            "anchored": True,
        }
        if state == "confirmed_event"
        else None
    )
    return {
        "user_state": state,
        "reason_code": state,
        "reliable": state != "unknown",
        "evidence_chain_id": "event-chain-futu",
        "nearest_event": event,
        "events": [event] if event else [],
        "expiration_relations": (
            {
                "contract": {
                    "expiration": "2026-08-21",
                    "relation": "before_expiration",
                    "days_before_expiration": 16,
                }
            }
            if event
            else {}
        ),
        "in_attention_window": bool(event),
        **({"fetched_at": fetched_at} if fetched_at else {}),
    }


def _brief(
    *,
    run_id: str,
    account: str = "lx",
    revision: int = 0,
    actionability: str = "live_actionable",
    status: str = "ready",
    actions: list[dict] | None = None,
    contracts: int = 1,
    available_cash: float = 10_000.0,
    valid_until: str = "2026-04-01T20:00:00+00:00",
) -> dict:
    return {
        "market": "US",
        "market_trading_date": "2026-04-01",
        "account": account,
        "revision": revision,
        "run_id": run_id,
        "generated_at_utc": "2026-04-01T13:40:00+00:00",
        "data_as_of_utc": "2026-04-01T13:39:00+00:00",
        "valid_until_utc": valid_until,
        "status": status,
        "actionability": actionability,
        "strategy_summary": "scenario",
        "actions": list(actions or []),
        "positions": [],
        "capacity": {
            "sell_put": {
                "contracts_available": contracts,
                "available_cash": available_cash,
            },
            "covered_call": {"contracts_available": 0},
        },
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def _confirm(base: Path, lifecycle: dict) -> None:
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery

    brief = lifecycle["brief"]
    confirm_daily_decision_brief_delivery(
        base=base,
        market=brief["market"],
        market_trading_date=brief["market_trading_date"],
        account=brief["account"],
        revision=brief["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
        confirmed_at_utc=f"{brief['market_trading_date']}T13:41:00+00:00",
    )


def _schedule(*, full_day_break: bool = False) -> dict:
    return {
        "enabled": True,
        "timezone": "America/New_York",
        "cron_interval_min": 10,
        "run_window": {
            "start": "09:30",
            "end": "16:00",
            "breaks": ([{"start": "09:30", "end": "16:00"}] if full_day_break else []),
        },
        "run_points": {
            "start_plus_min": 10,
            "hourly_minute": 0,
            "end_minus_min": 10,
        },
        "beijing_timezone": "Asia/Shanghai",
    }


def _empty_scheduler_state() -> dict:
    return {
        "last_run_utc_by_account": {},
        "last_notify_utc": None,
        "last_notify_utc_by_account": {},
    }


def test_0940_first_success_is_full_for_lx_and_sy(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief
    from src.application.scan_scheduler import decide

    now_utc = datetime(2026, 4, 1, 13, 40, tzinfo=timezone.utc)
    decisions = {
        account: decide(_schedule(), _empty_scheduler_state(), now_utc, account=account, schedule_key="schedule_us")
        for account in ("lx", "sy")
    }
    lifecycles = {
        account: prepare_daily_decision_brief(
            base=tmp_path,
            brief=_brief(run_id=f"run-{account}", account=account, actions=[_action(account=account)]),
        )
        for account in ("lx", "sy")
    }

    assert all(item.should_run_scan and item.is_notify_window_open for item in decisions.values())
    assert all(item.now_market.endswith("09:40:00-04:00") for item in decisions.values())
    assert {account: item["delivery_kind"] for account, item in lifecycles.items()} == {"lx": "full", "sy": "full"}
    assert {account: item["current_revision"] for account, item in lifecycles.items()} == {"lx": 0, "sy": 0}


def test_unchanged_same_day_is_silent_after_confirmed_full(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    first = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-full", actions=[_action()]),
    )
    _confirm(tmp_path, first)
    unchanged = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-unchanged", actions=[_action(mid=9.9)]),
    )

    assert unchanged["last_delivered_revision"] == 0
    assert unchanged["delivery_kind"] == "none"
    assert unchanged["diff"]["material"] is False


def test_new_and_upgraded_p0_are_material() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    empty = _brief(run_id="run-empty", revision=0)
    new_p0 = _brief(run_id="run-new", revision=1, actions=[_action(priority="P0")])
    prior_p1 = _brief(run_id="run-prior", revision=0, actions=[_action(priority="P1")])
    upgraded_p0 = _brief(run_id="run-upgraded", revision=1, actions=[_action(priority="P0")])

    new_diff = diff_daily_decision_briefs(empty, new_p0)
    upgraded_diff = diff_daily_decision_briefs(prior_p1, upgraded_p0)

    assert new_diff["material"] is True
    assert "candidate_added" in {item["change_type"] for item in new_diff["changes"]}
    assert upgraded_diff["material"] is True
    assert "candidate_priority_upgraded_to_p0" in {item["change_type"] for item in upgraded_diff["changes"]}


def test_main_action_invalidation_is_material() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    active = _brief(run_id="run-active", revision=0, actions=[_action(priority="P1")])
    invalid = _brief(
        run_id="run-invalid",
        revision=1,
        actions=[_action(priority="P1", state="invalidated")],
    )

    diff = diff_daily_decision_briefs(active, invalid)

    assert diff["material"] is True
    assert "candidate_invalidated" in {item["change_type"] for item in diff["changes"]}


def test_stable_high_priority_action_recovery_is_material(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    blocked = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-action-blocked", actions=[_action(priority="P0", state="blocked")]),
    )
    _confirm(tmp_path, blocked)

    recovered = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-action-active", actions=[_action(priority="P0", state="active")]),
    )

    assert recovered["delivery_kind"] == "delta"
    assert recovered["diff"]["material"] is True
    assert "candidate_added" in {item["change_type"] for item in recovered["diff"]["changes"]}


def test_blocked_to_recovery_is_material() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    blocked = _brief(
        run_id="run-blocked",
        revision=0,
        actionability="blocked",
        status="blocked",
    )
    recovered = _brief(
        run_id="run-recovered",
        revision=1,
        actionability="live_actionable",
        status="ready",
        actions=[_action(priority="P1")],
    )

    diff = diff_daily_decision_briefs(blocked, recovered)

    assert diff["material"] is True
    assert "recovered" in {item["change_type"] for item in diff["changes"]}


def test_capacity_changes_only_on_whole_contract_boundary() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    baseline = _brief(
        run_id="run-0",
        revision=0,
        actions=[_action(contracts_available=1)],
        contracts=1,
        available_cash=10_000.0,
    )
    cash_noise = _brief(
        run_id="run-1",
        revision=1,
        actions=[_action(contracts_available=1)],
        contracts=1,
        available_cash=10_499.0,
    )
    whole_contract = _brief(
        run_id="run-2",
        revision=1,
        actions=[_action(contracts_available=2)],
        contracts=2,
        available_cash=20_000.0,
    )

    noise_diff = diff_daily_decision_briefs(baseline, cash_noise)
    contract_diff = diff_daily_decision_briefs(baseline, whole_contract)

    assert noise_diff["material"] is False
    assert contract_diff["material"] is True
    assert "candidate_capacity_changed" in {item["change_type"] for item in contract_diff["changes"]}


def test_failed_delta_is_retained_against_last_delivered_revision(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    first = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-full", actions=[_action(priority="P1")]),
    )
    _confirm(tmp_path, first)

    failed_delta = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-failed-delta", actions=[_action(priority="P0")]),
    )
    retry_latest = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-retry", actions=[_action(priority="P0", mid=2.5)]),
    )

    assert failed_delta["delivery_kind"] == "delta"
    assert failed_delta["last_delivered_revision"] == 0
    assert retry_latest["current_revision"] == 2
    assert retry_latest["last_delivered_revision"] == 0
    assert retry_latest["diff"]["from_revision"] == 0
    assert retry_latest["delivery_kind"] == "delta"
    assert "candidate_priority_upgraded_to_p0" in {item["change_type"] for item in retry_latest["diff"]["changes"]}


def test_post_close_read_is_effectively_planning_only(tmp_path: Path) -> None:
    from src.application.agent_tools.daily_brief import read_daily_brief_view
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(
            run_id="run-close",
            actions=[_action(priority="P1")],
            valid_until="2026-04-01T20:00:00+00:00",
        ),
    )
    view = read_daily_brief_view(
        base=tmp_path,
        account="lx",
        market="US",
        now_utc=datetime(2026, 4, 1, 21, 0, tzinfo=timezone.utc),
    )

    assert view["brief"]["actionability"] == "live_actionable"
    assert view["effective_actionability"] == "planning_only"
    assert view["freshness"]["effective_actionability"] == "planning_only"
    assert "当前已不在可执行时段，仅供规划参考。" in view["rendered_markdown"]


def test_all_day_no_run_does_not_create_fake_live_brief(tmp_path: Path) -> None:
    from src.application.agent_tools.daily_brief import read_daily_brief_view
    from src.application.scan_scheduler import decide

    schedule = _schedule(full_day_break=True)
    state = _empty_scheduler_state()
    decisions = [
        decide(schedule, state, datetime(2026, 4, 1, hour, minute, tzinfo=timezone.utc), account="lx")
        for hour, minute in ((13, 40), (14, 0), (19, 50))
    ]
    view = read_daily_brief_view(base=tmp_path, account="lx", market="US")

    assert all(item.should_run_scan is False for item in decisions)
    assert all(item.is_notify_window_open is False for item in decisions)
    assert view["available"] is False
    assert view["effective_actionability"] == "unavailable"
    assert not (tmp_path / "output_accounts" / "lx" / "state").exists()


def test_old_runtime_without_artifacts_is_explicitly_unavailable(tmp_path: Path) -> None:
    from src.application.agent_tools.daily_brief import read_daily_brief_view

    view = read_daily_brief_view(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-03-31",
        revision=0,
    )

    assert view["available"] is False
    assert view["reason"] == "not_found"
    assert view["query"]["mode"] == "revision"
    assert view["brief"] is None
    assert view["source"]["state_path"].endswith("daily_decision_brief.US.2026-03-31.r0000.json")
    assert "不可用" in view["rendered_markdown"]


class _RunLog:
    def safe_event(self, *_args, **_kwargs) -> None:
        return None


class _Audit:
    def audit(self, *_args, **_kwargs) -> None:
        return None

    def guard_mark_failure(self, *_args, **_kwargs) -> None:
        return None

    def guard_mark_success(self) -> None:
        return None


def test_manual_trigger_finishes_without_preparing_or_sending_ordinary_notification(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    completions: list[dict] = []
    finalizations: list[dict] = []
    config = {
        "notifications": {
            "provider": "wechat_clawbot",
            "channel": "wechat_clawbot",
            "target": "wechat:ops",
            "daily_brief": {"enabled": False},
        }
    }
    request = mod.TickNotificationRequest(
        base=tmp_path,
        cfg_path=tmp_path / "config.us.json",
        state_path=tmp_path / "scheduler_state.json",
        scheduler_schedule_key="us",
        base_cfg=config,
        run_id="run-manual",
        runlog=_RunLog(),
        results=[{"account": "lx", "notification_text": "# malicious legacy renderer"}],
        tick_metrics={},
        no_send=False,
        bj_tz=ZoneInfo("Asia/Shanghai"),
        audit_helper=_Audit(),
        vpy=Path("python3"),
        complete_tick_idempotency_fn=lambda **kwargs: completions.append(dict(kwargs)),
        markets_to_run=("US",),
        scheduler_markets=("US",),
        scheduler_decision={"in_run_window": True},
        ran_pipeline_accounts=("lx",),
        trigger_kind="manual",
    )
    monkeypatch.setattr(
        mod,
        "_prepare_daily_brief_notification",
        lambda _request: (_ for _ in ()).throw(AssertionError("manual path must not prepare Daily Brief")),
    )
    monkeypatch.setattr(
        mod,
        "resolve_notification_delivery_route",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("manual path must not resolve delivery")),
    )

    def _finalize(**kwargs):
        finalizations.append(dict(kwargs))
        return 0

    monkeypatch.setattr(mod, "finalize_no_account_notification", _finalize)

    assert mod.run_tick_notification_flow(request) == 0
    assert finalizations[0]["reason"] == "non_scheduled_ordinary_notification_disabled"
    assert completions == [
        {"status": "completed", "message": "non_scheduled_ordinary_notification_disabled"}
    ]


def test_event_change_reuses_confirmed_pointer_and_freshness_only_is_silent(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    baseline_action = _action(event_risk=_scenario_event_risk("confirmed_none"))
    baseline_action.update(
        {
            "expiration": "2026-08-21",
            "contract_symbol": "NVDA260821P00100000",
        }
    )
    baseline = _brief(run_id="run-event-none", actions=[baseline_action])
    baseline.update(
        {
            "market_trading_date": "2026-07-21",
            "generated_at_utc": "2026-07-21T13:40:00+00:00",
            "data_as_of_utc": "2026-07-21T13:39:00+00:00",
            "valid_until_utc": "2026-07-21T20:00:00+00:00",
        }
    )
    first = prepare_daily_decision_brief(base=tmp_path, brief=baseline)
    _confirm(tmp_path, first)

    event_action = _action(event_risk=_scenario_event_risk("confirmed_event"))
    event_action.update(
        {
            "expiration": "2026-08-21",
            "contract_symbol": "NVDA260821P00100000",
        }
    )
    changed = {**baseline, "run_id": "run-event-added", "actions": [event_action]}
    second = prepare_daily_decision_brief(base=tmp_path, brief=changed)

    assert second["delivery_kind"] == "delta"
    assert second["last_delivered_revision"] == 0
    assert "candidate_event_added" in {item["change_type"] for item in second["diff"]["changes"]}
    _confirm(tmp_path, second)

    fresh_action = _action(
        event_risk=_scenario_event_risk(
            "confirmed_event",
            fetched_at="2026-07-21T15:00:00+00:00",
        )
    )
    fresh_action.update(
        {
            "expiration": "2026-08-21",
            "contract_symbol": "NVDA260821P00100000",
        }
    )
    freshness_only = {
        **baseline,
        "run_id": "run-event-freshness",
        "generated_at_utc": "2026-07-21T15:00:00+00:00",
        "data_as_of_utc": "2026-07-21T14:59:00+00:00",
        "actions": [fresh_action],
    }
    third = prepare_daily_decision_brief(base=tmp_path, brief=freshness_only)

    assert third["delivery_kind"] == "none"
    assert third["last_delivered_revision"] == second["current_revision"]
    assert third["diff"]["material"] is False
