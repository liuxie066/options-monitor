"""F5/F6: recovery policy follows committed intent, source and transport identity."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import json
import socket
from time import monotonic
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import src.application.channels.plugins as channel_plugins
import src.application.daily_decision_brief_repository as repo
import src.application.tick_notification_flow as notification
from src.application.notification_delivery_route import notification_reminder_route_key
from src.application.notification_fanout import list_notification_fanouts
from src.application.scan_scheduler import daily_brief_recovery_cutoff, decide
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, history_path
import test_daily_decision_brief_notification_flow as shared_fixtures
from test_daily_decision_brief_notification_flow import (
    FIXED_TARGET, IDENTITY, MARKET_DATE, _brief, _config, _patch_assembler, _patch_sender, _request, _run_flow,
)
from test_daily_brief_fanout_flow import config as fanout_config
from test_wechat_clawbot_channel import _wechat_state_dir, _write_wechat_bindings


ORIGINAL_CUTOFF = "2026-07-21T15:00:00+00:00"
SOURCE_TIME = "2026-07-21T14:30:30+00:00"


def _run(request):
    return _run_flow(request)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("recovery policy tests forbid real network/provider access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_legacy")
    monkeypatch.setattr(channel_plugins, "channel_plugin_installed", lambda _: True)


@pytest.fixture(params=["legacy", "fanout"])
def policy(request, monkeypatch, tmp_path):
    clock = {"now": "2026-07-21T14:00:30+00:00", "blocked": True, "candidate": True}
    calls, assembled = [], []
    monkeypatch.setattr(notification, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(repo, "_utc_now_iso", lambda: clock["now"])
    monkeypatch.setattr(notification, "report_system_meta_signal", lambda **_kw: "suppressed")

    def timed_brief(**kwargs):
        assembled.append((kwargs["run_id"], kwargs["account"]))
        return {**_brief(**kwargs), "generated_at_utc": clock["now"], "data_as_of_utc": clock["now"]}

    # The shared assembler patcher owns the production patch location as Brief
    # preparation moves. Only its synthetic Brief data is specialized here.
    monkeypatch.setattr(shared_fixtures, "_brief", timed_brief)
    _patch_sender(monkeypatch, calls=calls)
    route = request.param
    config = fanout_config() if route == "fanout" else _config()
    if route == "legacy":
        state_dir = _wechat_state_dir(tmp_path)
        _write_wechat_bindings(state_dir, {
            "ops": {"to_user_id": "fixture-ops", "context_token": "fixture-context"},
            "replacement": {"to_user_id": "fixture-replacement", "context_token": "fixture-context"},
        })
        config["notifications"]["wechat_clawbot_state_dir"] = str(state_dir)
    return SimpleNamespace(
        base=tmp_path, route=route, clock=clock, calls=calls, assembled=assembled, patch=monkeypatch,
        config=config,
        recipients=2 if route == "fanout" else 1,
    )


def _tick(policy, run_id, *, fixed=False, delivery_only=False, config=None, no_send=False, timezone=None):
    _patch_assembler(policy.patch, blocked=policy.clock["blocked"], candidate=policy.clock["candidate"])
    bundle = _request(policy.base, run_id=run_id, fixed=fixed, delivery_only=delivery_only,
                      pipeline_ok=not policy.clock["blocked"], no_send=no_send,
                      config=deepcopy(config if config is not None else policy.config))
    request = bundle.request
    if timezone is not None:
        request = replace(request, bj_tz=ZoneInfo(timezone))
    return _run(request)


def _history(policy, run_id):
    return DecisionHistoryStore(history_path(policy.base)).get(account="lx", market="US", run_id=run_id)


def _recoveries(policy):
    if policy.route == "fanout":
        return [child for parent in list_notification_fanouts(
            base=policy.base, account="lx", logical_key_prefix="daily-brief:")
            if ":recovery:" in parent["logical_key"] for child in parent["children"]]
    state = repo.read_daily_decision_brief_delivery_state(base=policy.base, account="lx", market="US")
    return list(state["state"]["days"].get(MARKET_DATE, {}).get("recovery_reports", {}).values())


def _intents(policy, row):
    intent = row["input"].get("notification_recovery_intent")
    if not intent:
        return []
    return intent.get("fanout_recoveries", []) if policy.route == "fanout" else [intent]


def _changed_schedule(policy, change):
    config = deepcopy(policy.config)
    if change == "earlier":
        config["schedule"]["run_points"] = {"hourly_minute": 45}
        expected = "2026-07-21T14:45:00+00:00"
    elif change == "later":
        config["schedule"]["run_points"] = {"start_plus_min": 120}
        expected = "2026-07-21T15:30:00+00:00"
    else:
        expected = ORIGINAL_CUTOFF
    # Verify this input actually changes the cutoff while keeping the same
    # business run window; a closed-window result cannot prove F6.
    assert daily_brief_recovery_cutoff(
        schedule_cfg=config["schedule"], scheduled_target_market=FIXED_TARGET,
    ) == expected
    return config


def _seed_failure(policy):
    assert _tick(policy, "fixed-failure", fixed=True) == 1
    assert len(policy.calls) == policy.recipients
    if policy.route == "fanout":
        parents = list_notification_fanouts(base=policy.base, account="lx", logical_key_prefix="daily-brief:")
        assert all(parent["delivery_confirmed"] for parent in parents)
    else:
        state = repo.read_daily_decision_brief_delivery_state(base=policy.base, account="lx", market="US")["state"]
        assert state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]["status"] == "confirmed"
    policy.clock.update(now=SOURCE_TIME, blocked=False)


def _commit_without_binding(policy, monkeypatch):
    def crash(*_args, **_kwargs):
        raise SystemExit("recovery-policy crash after history append, before binding")
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_persist_json", crash)
        with pytest.raises(SystemExit, match="after history append"):
            _tick(policy, "committed-source")
    row = deepcopy(_history(policy, "committed-source"))
    assert row is not None and row["successful"] == 1
    intents = _intents(policy, row)
    assert len(intents) == policy.recipients
    assert all(item["cutoff_utc"] == ORIGINAL_CUTOFF for item in intents)
    assert not _recoveries(policy)
    if policy.route == "legacy":
        assert repo.read_daily_decision_brief_fixed_recovery(
            base=policy.base, account="lx", market="US", market_trading_date=MARKET_DATE,
        )["available"] is False
    return row


def _assert_frozen_source(policy, row):
    records = _recoveries(policy)
    assert len(records) == policy.recipients
    contexts = []
    for intent in _intents(policy, row):
        context = deepcopy(intent["render_context"])
        if policy.route == "fanout":
            # Fanout rendering derives the restored batch label from the frozen
            # failure target and the committed source's generation time.
            context.update(
                scheduled_target_market=intent["scheduled_target_market"],
                recovery_original_target=intent["scheduled_target_market"],
                recovery_scan_at_utc=row["payload"]["generated_at_utc"],
            )
        contexts.append(context)
    for record in records:
        meta = record["spec"]["metadata"] if policy.route == "fanout" else record
        assert meta["cutoff_utc"] == ORIGINAL_CUTOFF
        assert meta["revision"] == row["revision"]
        assert meta["source_digest"] == repo.daily_brief_digest(row["payload"])
        # Envelope normalization adds derived display fields. Every key frozen
        # in the committed rendering input must still retain its original value.
        assert any(all(meta["render_context"].get(key) == value for key, value in context.items())
                   for context in contexts), {"actual": meta["render_context"], "frozen": contexts}
        assert meta["render_context"]["user_timezone"] == "Asia/Shanghai"
        assert meta["candidate_identities"] == [IDENTITY]
        if policy.route == "fanout":
            assert meta["generated_at_utc"] == SOURCE_TIME
            assert meta["render_context"]["attribution_route_key"] == record["spec"]["recipient"]["identity_key"]
            assert record["route_id"] in {"a", "b"}
    assert _history(policy, "committed-source") == row


def _frozen_transport(policy):
    if policy.route == "fanout":
        return {row["child_id"]: deepcopy(row["spec"]) for row in _recoveries(policy)}
    return {row["delivery_key"]: {key: deepcopy(row[key]) for key in (
        "revision", "source_digest", "cutoff_utc", "rendered_message", "render_context",
    )} for row in _recoveries(policy)}


def _sent_payloads(calls):
    result = {}
    for call in calls:
        key = call["idempotency_key"]
        payload = {
            "channel": call["channel"], "target": call["target"], "message": call["message"],
            "transport_envelope": deepcopy(call.get("transport_envelope")),
        }
        if key in result:
            assert result[key] == payload
        result[key] = payload
    return result


def test_disabled_success_does_not_create_intent_or_enabled_replay_retrofit(policy):
    _seed_failure(policy)
    # A qualified zero-candidate success isolates recovery from ordinary newly
    # detected candidate alerts when the same run is replayed with delivery on.
    policy.clock["candidate"] = False
    disabled = deepcopy(policy.config)
    disabled["notifications"]["enabled"] = False
    assert _tick(policy, "disabled-success", config=disabled) == 0
    row = deepcopy(_history(policy, "disabled-success"))
    assert row is not None and row["successful"] == 1
    assert "notification_recovery_intent" not in row["input"]
    assert not _recoveries(policy)
    before_calls = len(policy.calls)
    assert _tick(policy, "disabled-success") == 0
    assert _history(policy, "disabled-success") == row
    assert not _recoveries(policy)
    policy.clock["now"] = "2026-07-21T14:40:30+00:00"
    assert _tick(policy, "enabled-delivery-only", delivery_only=True) == 0
    assert not _recoveries(policy) and len(policy.calls) == before_calls


def test_preexisting_intent_is_preserved_while_disabled_and_replayed(policy, monkeypatch):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    disabled = deepcopy(policy.config)
    disabled["notifications"]["enabled"] = False
    assert _tick(policy, "committed-source", config=disabled) == 0
    assert _history(policy, "committed-source") == row
    assert len(policy.calls) == policy.recipients
    policy.clock["now"] = "2026-07-21T14:40:30+00:00"
    assert _tick(policy, "reenabled-recovery", delivery_only=True) == 0
    _assert_frozen_source(policy, row)
    assert len(policy.calls) == 2 * policy.recipients


@pytest.mark.parametrize("change", ["same", "earlier", "later"])
def test_unbound_history_recovers_original_cutoff_source_and_render(policy, monkeypatch, change):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    later = {**_brief(base=policy.base, run_id="newer-manual", candidate=False),
             "generated_at_utc": "2026-07-21T14:40:00+00:00"}
    repo.persist_daily_decision_brief_success(base=policy.base, brief=later)
    assembled = len(policy.assembled)
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    config = _changed_schedule(policy, change)
    assert _tick(policy, "recover-unbound", delivery_only=True, config=config, timezone="America/Los_Angeles") == 0
    assert len(policy.assembled) == assembled
    _assert_frozen_source(policy, row)
    assert len(policy.calls) == 2 * policy.recipients
    assert all("22:30 扫描" in call["message"] for call in policy.calls[-policy.recipients:])


@pytest.mark.parametrize("now", ["2026-07-21T15:00:00+00:00", "2026-07-21T15:10:00+00:00"])
def test_unbound_expired_intent_cannot_be_extended_by_current_schedule(policy, monkeypatch, now):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    policy.clock["now"] = now
    config = _changed_schedule(policy, "later")
    _tick(policy, "expired-unbound", delivery_only=True, config=config)
    assert len(policy.calls) == policy.recipients
    assert _history(policy, "committed-source") == row
    for record in _recoveries(policy):
        if policy.route == "legacy":
            assert record["status"] == "expired_unconfirmed"
        else:
            assert record["delivery_confirmed"] is False
            assert record["status"] != "pending"


@pytest.mark.parametrize("change", ["earlier", "later"])
@pytest.mark.parametrize("expired", [False, True])
def test_existing_pending_uses_frozen_deadline(policy, monkeypatch, change, expired):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    _patch_sender(monkeypatch, calls=policy.calls, result={
        "ok": False, "command_ok": False, "delivery_confirmed": False,
        "explicit_pre_acceptance_failure": True, "returncode": 1,
    })
    _tick(policy, "bind-pending", delivery_only=True)
    _assert_frozen_source(policy, row)
    frozen = _frozen_transport(policy)
    attempts = len(policy.calls)
    assert attempts > policy.recipients
    # The legacy transport may retry an explicit pre-acceptance failure within
    # the tick. Every attempt must still use one frozen payload per recipient.
    sent_payloads = _sent_payloads(policy.calls[policy.recipients:])
    assert len(sent_payloads) == policy.recipients
    _patch_sender(monkeypatch, calls=policy.calls)
    policy.clock["now"] = "2026-07-21T15:00:00+00:00" if expired else "2026-07-21T14:50:00+00:00"
    _tick(policy, "retry-pending", delivery_only=True, config=_changed_schedule(policy, change))
    assert _frozen_transport(policy) == frozen
    assert len(policy.calls) == attempts + (0 if expired else policy.recipients)
    if not expired:
        assert _sent_payloads(policy.calls[-policy.recipients:]) == sent_payloads
        assert all(record["delivery_confirmed"] if policy.route == "fanout" else record["status"] == "confirmed"
                   for record in _recoveries(policy))
    elif policy.route == "legacy":
        assert all(record["status"] == "expired_unconfirmed" for record in _recoveries(policy))


@pytest.mark.parametrize("gate", ["disabled", "no_send", "quiet", "retarget"])
def test_existing_frozen_task_survives_current_delivery_gates(policy, monkeypatch, gate):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    _patch_sender(monkeypatch, calls=policy.calls, result={
        "ok": False, "delivery_confirmed": False, "explicit_pre_acceptance_failure": True,
    })
    _tick(policy, "bind-before-gate", delivery_only=True)
    _assert_frozen_source(policy, row)
    frozen = _frozen_transport(policy)
    attempts = len(policy.calls)
    assert attempts > policy.recipients
    # The legacy transport may retry an explicit pre-acceptance failure within
    # the tick. Every attempt must still use one frozen payload per recipient.
    sent_payloads = _sent_payloads(policy.calls[policy.recipients:])
    assert len(sent_payloads) == policy.recipients
    _patch_sender(monkeypatch, calls=policy.calls)
    config = deepcopy(policy.config)
    if gate == "disabled":
        config["notifications"]["enabled"] = False
    elif gate == "quiet":
        config["notifications"]["quiet_hours_beijing"] = {"start": "00:00", "end": "23:59"}
    elif gate == "retarget":
        if policy.route == "fanout":
            config["notifications"]["routes"] = fanout_config(("replacement",))["notifications"]["routes"]
        else:
            config["notifications"]["target"] = "wechat:replacement"
            frozen_key = _recoveries(policy)[0]["render_context"].get("attribution_route_key")
            before_key = notification_reminder_route_key(config=policy.config, base=policy.base)
            after_key = notification_reminder_route_key(config=config, base=policy.base)
            proof = {"frozen_key": frozen_key, "before_key": before_key, "after_key": after_key}
            print(json.dumps({"canonical_route_proof": proof}, sort_keys=True))
            assert frozen_key and frozen_key == before_key, proof
            assert after_key and after_key != before_key, proof
    policy.clock["now"] = "2026-07-21T14:40:00+00:00"
    _tick(policy, "current-gate", delivery_only=True, config=config, no_send=gate == "no_send")
    assert len(policy.calls) == attempts, [
        {key: call[key] for key in ("channel", "target", "idempotency_key")}
        for call in policy.calls[attempts:]
    ]
    assert _frozen_transport(policy) == frozen
    assert _history(policy, "committed-source") == row
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    _tick(policy, "after-current-gate", delivery_only=True)
    assert len(policy.calls) == attempts + policy.recipients
    assert _sent_payloads(policy.calls[-policy.recipients:]) == sent_payloads
    assert _frozen_transport(policy) == frozen


def test_ambiguous_keeps_exact_identity_under_original_retry_rule(policy, monkeypatch):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    _patch_sender(monkeypatch, calls=policy.calls, result={
        "ok": False, "command_ok": True, "delivery_confirmed": False,
        "ambiguous_send": True, "error_code": "SEND_UNCONFIRMED", "returncode": 1,
    })
    _tick(policy, "unknown-result", delivery_only=True)
    _assert_frozen_source(policy, row)
    frozen = _frozen_transport(policy)
    before = len(policy.calls)
    assert before == 2 * policy.recipients
    records = _recoveries(policy)
    assert all(record["status"] == ("unknown" if policy.route == "fanout" else "ambiguous") for record in records)
    policy.clock["now"] = "2026-07-21T15:10:00+00:00"
    _patch_sender(monkeypatch, calls=policy.calls)
    _tick(policy, "after-cutoff-unknown", delivery_only=True, config=_changed_schedule(policy, "earlier"))
    assert _frozen_transport(policy) == frozen
    if policy.route == "fanout":
        assert len(policy.calls) == before
        assert all(record["status"] == "unknown" for record in _recoveries(policy))
    else:
        assert len(policy.calls) == before + 1
        assert _recoveries(policy)[0]["status"] == "confirmed"
        assert policy.calls[-1]["message"] == policy.calls[-2]["message"]
        assert policy.calls[-1]["idempotency_key"] == policy.calls[-2]["idempotency_key"]


def test_confirmed_recovery_absorbs_duplicate_replays_after_schedule_change(policy, monkeypatch):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    assert _tick(policy, "confirm-once", delivery_only=True) == 0
    _assert_frozen_source(policy, row)
    frozen = _frozen_transport(policy)
    attempts = len(policy.calls)
    assert attempts == 2 * policy.recipients
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    assert _tick(policy, "confirmed-replay", delivery_only=True, config=_changed_schedule(policy, "earlier")) == 0
    assert len(policy.calls) == attempts
    assert _frozen_transport(policy) == frozen
    assert _history(policy, "committed-source") == row


@pytest.mark.parametrize("parent_seconds", [600.0, 0.25, None])
def test_pending_recovery_actual_sender_uses_earlier_frozen_budget(policy, monkeypatch, parent_seconds):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    policy.clock["now"] = "2026-07-21T14:59:59+00:00"
    bundle = _request(policy.base, run_id="bounded-recovery", fixed=False,
                      delivery_only=True, config=policy.config)
    parent = None if parent_seconds is None else monotonic() + parent_seconds
    before = len(policy.calls)
    assert _run_flow(replace(bundle.request, deadline=parent)) == 0
    sent = policy.calls[before:]
    assert len(sent) == policy.recipients
    for call in sent:
        assert 0 < call["deadline"] - monotonic() <= (parent_seconds or 1.0) + 0.01
        assert call["deadline"] - monotonic() <= 1.01
        if parent is not None:
            assert call["deadline"] <= parent
    _assert_frozen_source(policy, row)


def _closed_window(policy):
    from src.application.scan_scheduler import decide

    config = deepcopy(policy.config)
    config["schedule"]["run_window"] = {"start": "09:30", "end": "10:40"}
    decision = asdict(decide(config["schedule"], {}, datetime.fromisoformat(policy.clock["now"]), account="lx"))
    assert decision["in_run_window"] is False
    assert decision["should_run_scan"] is False
    return config, decision


@pytest.mark.parametrize("bound", [False, True], ids=["durable_unbound", "pending"])
def test_real_scheduler_closed_window_preserves_original_recovery(policy, monkeypatch, bound):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    if bound:
        bundle = _request(policy.base, run_id="bind-existing", fixed=False, delivery_only=True, config=policy.config)
        if policy.route == "legacy":
            notification._daily_brief_delivery_read_scope(bundle.request, "lx", "US", MARKET_DATE)
        else:
            notification._prepare_daily_brief_fanout_recoveries(
                bundle.request, "lx", "US", MARKET_DATE, bundle.request.scheduler_decision,
                notification.resolve_daily_brief_render_limits(notification._daily_brief_limits(policy.config)),
            )
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    config, decision = _closed_window(policy)
    before, assembled = len(policy.calls), len(policy.assembled)
    bundle = _request(policy.base, run_id="closed-window-recovery", fixed=False,
                      delivery_only=True, config=config)
    assert _run_flow(replace(bundle.request, scheduler_decision=decision,
                            scheduler_decisions_by_account={"lx": decision})) == 0
    assert len(policy.calls) - before == policy.recipients
    assert len(policy.assembled) == assembled
    _assert_frozen_source(policy, row)


@pytest.mark.parametrize("obligation", ["valid", "absent", "expired"])
def test_real_root_admits_only_existing_eligible_recovery_outside_window(policy, monkeypatch, obligation):
    from domain.domain.engine import SchedulerDecisionView
    from src.application import multi_account_tick as root
    from src.application.tick_guard_flow import TickGuardOutcome
    from src.application.tick_scheduler_context import TickSchedulerContext, TickSchedulerOutcome
    import test_multi_account_tick as root_fixtures

    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch) if obligation != "absent" else None
    policy.clock["now"] = "2026-07-21T15:00:00+00:00" if obligation == "expired" else "2026-07-21T14:50:00+00:00"
    config, decision = _closed_window(policy)
    config.update(
        accounts=["lx"], symbols=[{"symbol": "NVDA", "broker": "US"}], portfolio={},
        _generated={"schema_version": "1.0", "generator": "options-monitor", "source_format": "yaml", "market": "us"},
    )
    path = policy.base / "config.us.json"
    path.write_text(json.dumps(config))
    trace = []

    class Log(shared_fixtures._RunLog):
        def __init__(self, _base):
            super().__init__()
            self.run_id = "root-frozen-recovery"

    def guard(request):
        return TickGuardOutcome(True, 0, request.base_cfg, request.accounts,
                                request.default_account, ZoneInfo("Asia/Shanghai"))

    root_fixtures._patch_runtime_bootstrap(monkeypatch, root, policy.base, Log, guard)
    root_fixtures._patch_config_bootstrap(monkeypatch, root)
    monkeypatch.setattr(root, "build_account_portfolio_source_plan", lambda *_a, **_kw: {})
    monkeypatch.setattr(root, "build_tick_scheduler_context", lambda _request: TickSchedulerOutcome(
        True, 0, TickSchedulerContext(
            markets_to_run=["US"], scheduler_markets=["US"], state_path=policy.base / "scheduler_state.json",
            scheduler_schedule_key="schedule", scheduler_ms=1, scheduler_decision=decision,
            scheduler_view=SchedulerDecisionView.from_payload(decision), notify_decision_by_account={},
            scan_decision_by_account={"lx": {"should_run": False, "reason": "outside_window", "scheduler_decision": decision}},
            should_run_global=False, reason_global="outside_window",
        ), []))

    def no_scan(*_a, **_kw):
        raise AssertionError("recovery admission must not start a scan or assemble new sources")

    monkeypatch.setenv("OM_TRIGGER_SOURCE", "cron")
    monkeypatch.setattr(root, "prepare_tick_run_workspace", no_scan)
    monkeypatch.setattr(root, "run_tick_account_execution", no_scan)
    monkeypatch.setattr("src.application.tick_brief_preparation.assemble_daily_decision_briefs", no_scan)
    real_notification = root._run_notification_with_post_delivery_fallback

    def notify(**kwargs):
        trace.append(kwargs["notification_request"])
        return real_notification(**kwargs)

    monkeypatch.setattr(root, "_run_notification_with_post_delivery_fallback", notify)
    before = len(policy.calls)
    assert root.main(["--config", str(path), "--accounts", "lx"]) == 0
    expected = int(obligation == "valid")
    assert len(trace) == expected
    assert len(policy.calls) - before == expected * policy.recipients
    if row is not None:
        assert _history(policy, "committed-source") == row
    if expected:
        _assert_frozen_source(policy, row)


def test_recovery_admission_is_read_only_and_rejects_corrupt_storage(policy, monkeypatch):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    config, decision = _closed_window(policy)
    before = {str(path.relative_to(policy.base)): path.read_bytes() for path in policy.base.rglob("*") if path.is_file()}
    assert notification.has_daily_brief_recovery_to_resume(
        base=policy.base, config=config, account="lx", market="US", scheduler=decision,
    ) is True
    after = {str(path.relative_to(policy.base)): path.read_bytes() for path in policy.base.rglob("*") if path.is_file()}
    assert after == before
    assert _history(policy, "committed-source") == row
    assert _recoveries(policy) == []
    if policy.route == "legacy":
        path = repo._delivery_path(policy.base, "lx", "US")
        error = repo.DailyDecisionBriefStateError
    else:
        path = next((policy.base / "output_shared/state/notification_fanout").rglob("nf_*.json"))
        error = ValueError
    path.write_text("{}")
    with pytest.raises(error):
        notification.has_daily_brief_recovery_to_resume(
            base=policy.base, config=config, account="lx", market="US", scheduler=decision,
        )


@pytest.mark.parametrize("gate", ["disabled", "no_send", "quiet", "retarget", "plugin"])
def test_closed_window_recovery_keeps_current_delivery_pauses(policy, monkeypatch, gate):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    config, decision = _closed_window(policy)
    original = deepcopy(config)
    if gate == "disabled":
        config["notifications"]["enabled"] = False
    elif gate == "quiet":
        config["notifications"]["quiet_hours_beijing"] = {"start": "00:00", "end": "23:59"}
    elif gate == "retarget":
        if policy.route == "legacy":
            config["notifications"]["target"] = "wechat:replacement"
        else:
            for route in config["notifications"]["routes"]:
                route["target"] += "_replacement"
    before = len(policy.calls)
    with monkeypatch.context() as paused:
        if gate == "plugin":
            from src.application.notification_delivery_adapter import select_notification_delivery_adapter

            paused.setattr(channel_plugins, "channel_plugin_installed", lambda _provider: False)
            paused.setattr(notification, "select_notification_delivery_adapter", select_notification_delivery_adapter)
        bundle = _request(policy.base, run_id="paused-window-recovery", fixed=False, delivery_only=True,
                          config=config, no_send=gate == "no_send")
        _run_flow(replace(bundle.request, scheduler_decision=decision, scheduler_decisions_by_account={"lx": decision}))
    assert len(policy.calls) == before
    assert _history(policy, "committed-source") == row
    bundle = _request(policy.base, run_id="resume-window-recovery", fixed=False, delivery_only=True, config=original)
    assert _run_flow(replace(bundle.request, scheduler_decision=decision, scheduler_decisions_by_account={"lx": decision})) == 0
    assert len(policy.calls) - before == policy.recipients
    _assert_frozen_source(policy, row)


@pytest.mark.parametrize("policy", ["legacy"], indirect=True)
@pytest.mark.parametrize("parent_seconds", [600.0, 0.25, None])
def test_legacy_continuation_uses_original_recovery_sender_budget(policy, monkeypatch, parent_seconds):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    prepared = shared_fixtures._prepare_flow(
        _request(policy.base, run_id="freeze-legacy-recovery", fixed=False, delivery_only=True, config=policy.config).request,
    )
    original = prepared.lifecycles_by_account["lx"]["envelope"]
    frozen = _frozen_transport(policy)
    config = deepcopy(policy.config)
    config["notifications"]["routes"] = [
        {"id": "legacy", "provider": "wechat_clawbot", "target": "wechat:ops", "accounts": ["lx"]},
    ]
    policy.clock["now"] = "2026-07-21T14:59:59+00:00"
    bundle = _request(policy.base, run_id="continue-original-owner", fixed=False, delivery_only=True, config=config)
    parent = None if parent_seconds is None else monotonic() + parent_seconds
    before = len(policy.calls)
    assert _run_flow(replace(bundle.request, deadline=parent)) == 0
    sent = policy.calls[before:]
    assert len(sent) == 1
    assert 0 < sent[0]["deadline"] - monotonic() <= 1.01
    if parent is not None:
        assert sent[0]["deadline"] <= parent
    assert sent[0]["idempotency_key"] == notification.build_notification_transport_key(original["delivery_key"])
    assert _frozen_transport(policy) == frozen
    _assert_frozen_source(policy, row)


@pytest.mark.parametrize("policy", ["legacy"], indirect=True)
def test_ambiguous_recovery_outside_window_keeps_exact_retry_without_pending_cutoff(policy, monkeypatch):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    _patch_sender(monkeypatch, calls=policy.calls, result={
        "ok": False, "command_ok": False, "delivery_confirmed": False, "returncode": 1,
        "ambiguous_send": True, "error_code": "FEISHU_SEND_UNKNOWN",
    })
    _tick(policy, "ambiguous-before-window-change", delivery_only=True)
    assert _recoveries(policy)[0]["status"] == "ambiguous"
    frozen = _frozen_transport(policy)
    exact = policy.calls[-1]
    policy.clock["now"] = "2026-07-21T15:10:00+00:00"
    config, decision = _closed_window(policy)
    _patch_sender(monkeypatch, calls=policy.calls)
    parent = monotonic() + 600
    bundle = _request(policy.base, run_id="exact-retry-after-original-cutoff", fixed=False, delivery_only=True, config=config)
    before = len(policy.calls)
    assert _run_flow(replace(bundle.request, deadline=parent, scheduler_decision=decision,
                            scheduler_decisions_by_account={"lx": decision})) == 0
    assert len(policy.calls) == before + 1
    assert policy.calls[-1]["deadline"] == parent
    assert policy.calls[-1]["idempotency_key"] == exact["idempotency_key"]
    assert policy.calls[-1]["message"] == exact["message"]
    assert _recoveries(policy)[0]["status"] == "confirmed"
    assert _frozen_transport(policy) == frozen
    _assert_frozen_source(policy, row)


def test_local_recovery_expiry_does_not_cancel_healthy_account_or_recipient(policy, monkeypatch):
    from src.application import scheduled_notification
    from src.infrastructure import fetch_control

    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    shared_fixtures._prepare_flow(_request(policy.base, run_id="freeze-local-recovery", fixed=False,
                                 delivery_only=True, config=policy.config).request)
    frozen = _frozen_transport(policy)
    if policy.route == "legacy":
        recovery_keys = {notification.build_notification_transport_key(item["delivery_key"]) for item in _recoveries(policy)}
    else:
        recovery_keys = {child["idempotency_key"] for child in _recoveries(policy)}
    config = deepcopy(policy.config)
    for route in config["notifications"].get("routes", []):
        route["accounts"] = ["lx", "sy"]
    policy.clock["now"] = "2026-07-21T14:40:00+00:00"
    shared_fixtures._prepare_flow(_request(policy.base, run_id="healthy-sibling", fixed=False,
                                 config=config, accounts=("sy",)).request)
    policy.clock["now"] = "2026-07-21T14:59:59+00:00"
    clock = {"mono": monotonic()}
    waits = []
    monkeypatch.setattr(notification, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(fetch_control.time, "monotonic", lambda: clock["mono"])

    def advance(seconds):
        clock["mono"] += seconds
        policy.clock["now"] = (datetime.fromisoformat(policy.clock["now"]) + timedelta(seconds=seconds)).isoformat()

    def wait(seconds, **_controls):
        waits.append(seconds)
        advance(seconds)

    monkeypatch.setattr(notification, "wait_fetch_control", wait)
    monkeypatch.setattr(scheduled_notification, "_retry_delay_for_attempt", lambda *_a: 5.0)
    adapter = notification.select_notification_delivery_adapter("wechat_clawbot" if policy.route == "legacy" else "feishu_app")

    def send(**kwargs):
        result = adapter.send_fn(**kwargs)
        if kwargs["idempotency_key"] in recovery_keys:
            if policy.route == "legacy":
                return {"ok": False, "command_ok": False, "delivery_confirmed": False,
                        "returncode": 1, "error_code": "SEND_FAILED"}
            advance(1.0)
            fetch_control.check_fetch_control(kwargs["deadline"])
            raise AssertionError("expired native send must stop")
        return result

    monkeypatch.setattr(notification, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=send, normalize_fn=adapter.normalize_fn, failure_stage=adapter.failure_stage,
    ))
    before = len(policy.calls)
    bundle = _request(policy.base, run_id="isolate-local-cutoff", fixed=False, delivery_only=True,
                      config=config, accounts=("lx", "sy"))
    _run_flow(replace(bundle.request, deadline=clock["mono"] + 600))
    observed = policy.calls[before:]
    assert len([call for call in observed if call["idempotency_key"] in recovery_keys]) == 1
    assert len([call for call in observed if call["idempotency_key"] not in recovery_keys]) == policy.recipients
    if policy.route == "legacy":
        assert waits == [1.0]
    else:
        assert waits == []
    assert "sy" in bundle.request.tick_metrics["sent_accounts"]
    if policy.route == "fanout":
        healthy = list_notification_fanouts(base=policy.base, account="sy", logical_key_prefix="daily-brief:")
        assert healthy and all(child["delivery_confirmed"] for parent in healthy for child in parent["children"])
    assert _frozen_transport(policy) == frozen
    assert _history(policy, "committed-source") == row


def test_bound_pending_recovery_never_starts_at_or_after_frozen_cutoff(policy, monkeypatch):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    shared_fixtures._prepare_flow(_request(policy.base, run_id="freeze-boundary", fixed=False,
                                 delivery_only=True, config=policy.config).request)
    frozen = _frozen_transport(policy)
    assert frozen
    before = len(policy.calls)
    for suffix, now in (("at", ORIGINAL_CUTOFF), ("after", "2026-07-21T15:00:00.001+00:00")):
        policy.clock["now"] = now
        _tick(policy, "bounded-expiry-" + suffix, delivery_only=True, config=_changed_schedule(policy, "later"))
        assert len(policy.calls) == before
    assert _frozen_transport(policy) == frozen
    assert _history(policy, "committed-source") == row


@pytest.mark.parametrize("policy", ["legacy"], indirect=True)
@pytest.mark.parametrize("bound", [False, True], ids=["durable_unbound", "rendered_bound"])
@pytest.mark.parametrize("in_run_window", [False, True], ids=["closed_window", "open_window"])
def test_recovery_rebuild_preserves_window_selection_with_competing_fixed_report(
    policy, monkeypatch, bound, in_run_window,
):
    _seed_failure(policy)
    row = _commit_without_binding(policy, monkeypatch)
    if bound:
        shared_fixtures._prepare_flow(_request(
            policy.base, run_id="bind-competing-recovery", fixed=False,
            delivery_only=True, config=policy.config,
        ).request)
    target = "2026-07-21T10:30:00-04:00"
    ordinary = repo.prepare_daily_decision_brief_delivery(
        base=policy.base, account="lx", market="US", market_trading_date=MARKET_DATE,
        run_id=row["payload"]["run_id"], delivery_kind="fixed_report",
        source_kind="successful_brief", source_digest=repo.daily_brief_digest(row["payload"]),
        revision=row["revision"], rendered_message="Independent pending fixed report",
        scheduled_target_market=target, prepared_at_utc="2026-07-21T14:30:31+00:00",
    )["envelope"]
    policy.clock["now"] = "2026-07-21T14:50:00+00:00"
    config, decision = _closed_window(policy)
    if in_run_window:
        config = deepcopy(policy.config)
        decision = asdict(decide(
            config["schedule"], {}, datetime.fromisoformat(policy.clock["now"]), account="lx",
        ))
    assert decision["in_run_window"] is in_run_window
    assert notification.has_daily_brief_recovery_to_resume(
        base=policy.base, config=config, account="lx", market="US", scheduler=decision,
    )
    request = _request(
        policy.base, run_id="competing-recovery-selection", fixed=False,
        delivery_only=True, config=config,
    ).request
    sent_before, assembled_before = len(policy.calls), len(policy.assembled)
    assert _run_flow(replace(
        request, scheduler_decision=decision, scheduler_decisions_by_account={"lx": decision},
    )) == 0
    sent = policy.calls[sent_before:]
    day = repo.read_daily_decision_brief_delivery_state(
        base=policy.base, account="lx", market="US",
    )["state"]["days"][MARKET_DATE]
    recoveries = list(day.get("recovery_reports", {}).values())
    assert len(sent) == 1
    if in_run_window:
        assert sent[0]["idempotency_key"] == notification.build_notification_transport_key(ordinary["delivery_key"])
        assert day["fixed_reports"][target]["status"] == "confirmed"
        assert len(recoveries) == 1
        assert all(item["status"] == "pending" for item in recoveries)
    else:
        assert day["fixed_reports"][target]["status"] == "pending"
        assert len(recoveries) == 1 and recoveries[0]["status"] == "confirmed"
        assert sent[0]["idempotency_key"] == notification.build_notification_transport_key(recoveries[0]["delivery_key"])
    assert len(policy.assembled) == assembled_before
    _assert_frozen_source(policy, row)
