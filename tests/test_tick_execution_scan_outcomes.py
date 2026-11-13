"""F3: real account outcomes survive root orchestration and notification finalization."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from domain.storage.repositories import state_repo
from src.application import account_run
from src.application import multi_account_tick as root_tick
from src.application import tick_account_execution as execution
from src.application import tick_notification_flow as notification
from src.application.multi_tick.misc import AccountResult
from src.application.tick_guard_flow import TickGuardOutcome
from src.application.tick_scheduler_context import TickSchedulerContext, TickSchedulerOutcome
from src.application.tick_run_workspace import AccountRunConfigError
from src.infrastructure.io_utils import atomic_write_json

import test_daily_decision_brief_notification_flow as briefs
import test_multi_account_tick as root_fixtures
import test_multi_tick_scheduler_application as scheduler_fixtures
import test_tick_account_execution_barrier as barrier


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    def no_network(*_args, **_kwargs):
        raise AssertionError("F3 verification forbids network/provider access")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_ENV_FILE", str(tmp_path / "absent.env"))


def _root_case(
    monkeypatch, tmp_path, *, trigger="scheduled", failure=None, no_send=False,
    force_positive_qualification=False, fanout=False, cancel_after_persist=None,
):
    trace = SimpleNamespace(
        account_requests=[], pipeline_accounts=[], brief_accounts=[], sends=[],
        executions=[], notifications=[], completions=[], guard_successes=[], delivery_accounts=[],
    )
    account_fixture = barrier._request(
        tmp_path, accounts=["lx", "sy"], workers=2, force=trigger == "force", trigger_kind=trigger,
    )
    config = tmp_path / "config.us.json"
    root_fixtures._write_market_config(config, "us", account_fixture.base_cfg["symbols"])
    payload = json.loads(config.read_text())
    payload.update(accounts=["lx", "sy"], runtime={"portfolio_timeout_sec": 1}, **briefs._config())
    if fanout:
        payload["notifications"]["routes"] = [
            {"id": route, "provider": "feishu_app", "target": "ou_" + route, "accounts": ["lx", "sy"]}
            for route in ("a", "b")
        ]
        monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
        monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_legacy")
        monkeypatch.setattr("src.application.channels.plugins.channel_plugin_installed", lambda _: True)
    config.write_text(json.dumps(payload))

    class Log(barrier._RunLog):
        def __init__(self, _base):
            super().__init__()
            self.run_id = "run-1"

    def guard(request):
        return TickGuardOutcome(
            should_continue=True, return_code=0, base_cfg=request.base_cfg,
            accounts=request.accounts, default_account=request.default_account,
            bj_tz=ZoneInfo("Asia/Shanghai"),
        )

    root_fixtures._patch_runtime_bootstrap(monkeypatch, root_tick, tmp_path, Log, guard)
    root_fixtures._patch_config_bootstrap(monkeypatch, root_tick)
    monkeypatch.setattr(root_tick, "build_account_portfolio_source_plan", lambda *_a, **_kw: {})
    def guard_success(*args, **kwargs):
        trace.guard_successes.append((args, kwargs))
        return {"closed": False}

    monkeypatch.setattr(root_tick, "record_project_success", guard_success)
    monkeypatch.setattr(root_tick, "record_project_failure", lambda *_a, **_kw: {"opened": False})
    monkeypatch.setenv("OM_TRIGGER_SOURCE", "cron" if trigger == "scheduled" else "manual")

    def scheduler_payload(**kwargs):
        account = kwargs["account"]
        if account == "lx" and failure == "scheduler":
            raise ValueError("F3 synthetic account scheduler exception")
        skip = account == "lx" and failure == "skip"
        target = briefs.FIXED_TARGET if trigger == "scheduled" and not skip else None
        return {
            "should_run_scan": not skip, "is_notify_window_open": not skip, "in_run_window": not skip,
            "reason": "already_processed" if skip else "due",
            "now_utc": "2026-07-21T14:00:30Z", "now_market": briefs.FIXED_TARGET,
            "scheduled_scan_target_market": target, "scheduled_target_market": target,
        }

    def scheduler(request):
        outcome = scheduler_fixtures._run_scheduler_flow(
            run_scan_scheduler_cli=lambda **_kw: SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "should_run_scan": True, "is_notify_window_open": True, "reason": "due",
            })),
            audit_fn=lambda *_a, **_kw: None, build_scheduler_decision_payload_fn=scheduler_payload,
            accounts=("lx", "sy"), force_mode=trigger == "force",
        )
        trace.scheduler = outcome
        return TickSchedulerOutcome(True, 0, TickSchedulerContext(
            markets_to_run=["US"], scheduler_markets=["US"], state_path=tmp_path / "scheduler_state.json",
            scheduler_schedule_key="schedule", scheduler_ms=1,
            scheduler_decision=outcome.scheduler_decision, scheduler_view=outcome.scheduler_view,
            notify_decision_by_account=outcome.notify_decision_by_account,
            scan_decision_by_account=outcome.scan_decision_by_account,
            should_run_global=outcome.should_run_global, reason_global=outcome.reason_global,
        ), [])

    monkeypatch.setattr(root_tick, "build_tick_scheduler_context", scheduler)
    real_build = execution.build_account_runtime_config

    def build(**kwargs):
        if kwargs["account"] == "lx" and failure == "config":
            raise AccountRunConfigError("ACCOUNT_CONFIG_BUILD_FAILED", "F3 synthetic account config failure")
        return real_build(**kwargs)

    def prepare(**kwargs):
        manifests = barrier._fake_prepare(**kwargs)
        if failure == "prepared":
            authority = kwargs["account_config_authorities"]["lx"]
            path = Path(manifests["lx"]["manifest_path"])
            manifest = {
                "schema_version": "prepared_portfolio_context.v1", "run_id": kwargs["run_id"],
                "account": "lx", "status": "unavailable",
                "account_config_sha256": authority.account_config_sha256,
                "reason": "portfolio_context_unavailable",
            }
            atomic_write_json(path, manifest)
            manifests["lx"] = {**manifest, "manifest_path": str(path),
                               "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return manifests

    from src.application import prepared_portfolio_context
    monkeypatch.setattr(prepared_portfolio_context, "resolve_broker_portfolio_provider", lambda *_a, **_kw: SimpleNamespace())
    monkeypatch.setattr(execution, "build_account_runtime_config", build)
    monkeypatch.setattr(execution, "prepare_portfolio_contexts", prepare)
    monkeypatch.setattr(execution, "prepare_option_positions_contexts", barrier._fake_prepare_options)
    monkeypatch.setattr(execution, "_build_close_advice_barrier_plan", lambda **kw: (kw["candidate_config"], None))
    monkeypatch.setattr(execution, "prefetch_required_data", lambda **_kw: {
        "global_required_data_plan": {"plan_id": "a" * 64, "symbols": []}, "symbols": [], "results": {},
    })
    monkeypatch.setattr(execution, "seal_required_data_snapshot", barrier._seal_stub())
    monkeypatch.setattr(execution, "seal_run_fx_snapshot", lambda **_kw: ({}, "f" * 64))
    monkeypatch.setattr(account_run, "load_prepared_option_positions_context", lambda **_kw: {"positions": []})

    def pipeline(**kwargs):
        account = kwargs["account_config_account"]
        trace.pipeline_accounts.append(account)
        return SimpleNamespace(
            returncode=1 if account == "lx" and failure == "pipeline" else 0,
            stdout="", stderr="F3 synthetic pipeline failure" if failure == "pipeline" else "",
        )

    monkeypatch.setattr(account_run, "run_pipeline_script", pipeline)
    real_account = execution.run_one_account

    def run_account(*, request, **kwargs):
        trace.account_requests.append(request)
        if request.acct == "lx" and failure == "execution":
            raise RuntimeError("F3 synthetic account execution exception")
        return real_account(request=request, **kwargs)

    monkeypatch.setattr(execution, "run_one_account", run_account)
    real_execution = execution.run_tick_account_execution

    def run_execution(request):
        # Run the real root-created request and return its original results/targets/map.
        trace.execution_request = request
        outcome = real_execution(request)
        trace.executions.append(outcome)
        return outcome

    monkeypatch.setattr(root_tick, "run_tick_account_execution", run_execution)

    def assemble(*, base, run_id, account, markets_to_run, **_kwargs):
        trace.brief_accounts.append(account)
        return {market: briefs._brief(
            base=base, run_id=run_id, account=account, market=market,
            blocked=account == "lx" and failure not in {None, "skip"},
        ) for market in markets_to_run}

    monkeypatch.setattr("src.application.tick_brief_preparation.assemble_daily_decision_briefs", assemble)
    cancellation = {"requested": False}
    if cancel_after_persist:
        from src.application import tick_brief_preparation

        real_persist = tick_brief_preparation.persist_daily_decision_brief_success

        def persist(**kwargs):
            result = real_persist(**kwargs)
            if kwargs["brief"]["account"] == cancel_after_persist:
                cancellation["requested"] = True
            return result

        monkeypatch.setattr(tick_brief_preparation, "persist_daily_decision_brief_success", persist)
    real_qualify = root_tick.prepare_tick_briefs

    def qualify(**kwargs):
        if cancel_after_persist:
            kwargs["cancelled"] = lambda: cancellation["requested"]
        outcome = real_qualify(**kwargs)
        trace.produced_qualification_outcomes = dict(outcome.scan_outcomes_by_account)
        if force_positive_qualification:
            # Fault-inject only the qualification product, preserving the real
            # failed execution, root request, results, targets and tick metrics.
            outcome.scan_outcomes_by_account["lx"] = True
        trace.qualification_outcomes = dict(outcome.scan_outcomes_by_account)
        return outcome

    monkeypatch.setattr(root_tick, "prepare_tick_briefs", qualify)
    monkeypatch.setattr(notification, "utc_now", lambda: "2026-07-21T14:00:30+00:00")
    monkeypatch.setattr(notification, "report_system_meta_signal", lambda **_kw: "disabled")
    briefs._patch_sender(monkeypatch, calls=trace.sends)
    real_delivery = notification.execute_per_account_delivery

    def deliver(**kwargs):
        outcome = real_delivery(**kwargs)
        trace.delivery_accounts.extend(outcome.sent_accounts)
        return outcome

    monkeypatch.setattr(notification, "execute_per_account_delivery", deliver)
    real_notification = notification.run_tick_notification_flow

    def notify(request):
        trace.root_scan_outcomes = deepcopy(request.tick_metrics.get("scan_outcomes"))
        trace.notifications.append(request)
        return real_notification(request)

    monkeypatch.setattr(root_tick, "run_tick_notification_flow", notify)
    real_complete = root_tick._complete_tick_idempotency

    def complete(**kwargs):
        trace.completions.append(deepcopy(kwargs))
        return real_complete(**kwargs)

    monkeypatch.setattr(root_tick, "_complete_tick_idempotency", complete)
    args = ["--config", str(config), "--accounts", "lx", "sy"]
    if trigger == "force":
        args.append("--force")
    if no_send:
        args.append("--no-send")
    if cancel_after_persist:
        with pytest.raises(TimeoutError, match="cancelled") as raised:
            root_tick.main(args)
        trace.error = raised.value
        trace.rc = None
        assert len(trace.executions) == len(trace.completions) == 1
        assert trace.notifications == []
    else:
        trace.rc = root_tick.main(args)
        assert len(trace.executions) == len(trace.notifications) == len(trace.completions) == 1
    trace.execution = trace.executions[0]
    trace.notification = trace.notifications[0] if trace.notifications else None
    trace.terminal = state_repo.read_idempotency_record(
        tmp_path, scope="tick_execution", key=trace.completions[0]["key"],
    )
    return trace


@pytest.mark.parametrize("cancel_after,failure,expected", [
    ("lx", None, {"lx": True, "sy": False}),
    ("sy", None, {"lx": True, "sy": True}),
    ("sy", "pipeline", {"lx": False, "sy": True}),
])
@pytest.mark.parametrize("no_send", [False, True])
def test_preparation_cancellation_preserves_durable_account_outcomes(
    monkeypatch, tmp_path, cancel_after, failure, expected, no_send,
):
    trace = _root_case(
        monkeypatch, tmp_path, failure=failure, no_send=no_send,
        cancel_after_persist=cancel_after,
    )
    assert trace.execution.scan_outcomes == {"lx": failure != "pipeline", "sy": True}
    source = tmp_path / "output_runs/run-1/accounts" / cancel_after / "state/daily_decision_brief.US.json"
    durable = json.loads(source.read_text())
    assert durable["account"] == cancel_after and durable["run_id"] == "run-1"
    assert durable["status"] == "ready" and durable["actionability"] == "live_actionable"
    assert trace.terminal["ok"] is False
    assert trace.completions[0]["error_code"] == "DAILY_BRIEF_PREPARE_FAILED"
    assert {row["account"]: row["scan_ok"] for row in trace.terminal["account_outcomes"]} == expected
    assert all(not row["notify_failed"] and not row["failure_notification_confirmed"]
               for row in trace.terminal["account_outcomes"])
    assert trace.sends == [] and trace.guard_successes == []


@pytest.mark.parametrize("trigger,failure", [
    ("scheduled", "scheduler"),
    ("scheduled", "config"), ("scheduled", "prepared"), ("scheduled", "execution"),
    ("force", "config"), ("force", "prepared"), ("force", "execution"),
    ("scheduled", "pipeline"), ("force", "pipeline"),
])
@pytest.mark.parametrize("no_send", [False, True])
def test_failed_account_survives_real_account_root_and_finalizer(monkeypatch, tmp_path, trigger, failure, no_send):
    trace = _root_case(monkeypatch, tmp_path, trigger=trigger, failure=failure, no_send=no_send)
    assert trace.execution.scan_outcomes == {"lx": False, "sy": True}
    assert trace.root_scan_outcomes == {"lx": False, "sy": True}
    assert trace.notification.tick_metrics["scan_outcomes"] == {"lx": False, "sy": True}
    assert trace.rc == 1
    assert {result.account for result in trace.execution.results} == {"lx", "sy"}
    assert "sy" in trace.pipeline_accounts and "sy" in trace.brief_accounts
    assert trace.terminal["ok"] is False and trace.terminal["status"] == "failed"
    assert {row["account"]: row["scan_ok"] for row in trace.terminal["account_outcomes"]} == {"lx": False, "sy": True}
    assert trace.guard_successes == []
    if trigger == "force" or failure == "scheduler":
        assert trace.execution.scheduled_scan_targets_by_account.get("lx") is None
        assert "lx" not in trace.delivery_accounts
    if (failure in {"config", "prepared", "execution"} and trigger == "force") or failure == "scheduler":
        assert "lx" not in trace.brief_accounts
        assert "lx" not in trace.delivery_accounts
    if failure == "scheduler":
        assert trace.scheduler.scan_decision_by_account["lx"]["reason"] == "scheduler_unavailable"
        assert trace.scheduler.scan_decision_by_account["lx"]["error"]
    if no_send:
        assert trace.sends == []
    elif trigger == "scheduled":
        assert "sy" in trace.delivery_accounts


@pytest.mark.parametrize("trigger", ["scheduled", "force"])
@pytest.mark.parametrize("no_send", [False, True])
def test_healthy_scans_stay_successful_and_skip_stays_unknown(monkeypatch, tmp_path, trigger, no_send):
    failure = "skip" if trigger == "scheduled" else None
    trace = _root_case(monkeypatch, tmp_path, trigger=trigger, failure=failure, no_send=no_send)
    expected = {"lx": None if trigger == "scheduled" else True, "sy": True}
    assert trace.execution.scan_outcomes == expected
    assert trace.root_scan_outcomes == expected
    assert trace.notification.tick_metrics["scan_outcomes"] == expected
    assert trace.rc == 0 and trace.terminal["ok"] is True
    assert len(trace.guard_successes) == 1
    assert {row["account"]: row["scan_ok"] for row in trace.terminal["account_outcomes"]} == expected
    if trigger == "scheduled":
        skipped = next(result for result in trace.execution.results if result.account == "lx")
        assert skipped.ran_scan is False
        assert "lx" not in trace.pipeline_accounts and "lx" not in trace.brief_accounts
    if no_send:
        assert trace.sends == []


def test_confirmed_failure_delivery_does_not_make_scan_successful(monkeypatch, tmp_path):
    trace = _root_case(monkeypatch, tmp_path, failure="pipeline")
    state = briefs.read_daily_decision_brief_delivery_state(
        base=tmp_path, account="lx", market="US",
    )["state"]
    envelope = state["days"][briefs.MARKET_DATE]["fixed_reports"][briefs.FIXED_TARGET]
    assert envelope["delivery_kind"] == "fixed_failure"
    assert envelope["status"] == "confirmed"
    assert "lx" in trace.delivery_accounts
    assert "lx" in trace.notification.tick_metrics["scan_failure_notified_accounts"]
    persisted = next(row for row in trace.terminal["account_outcomes"] if row["account"] == "lx")
    assert persisted["failure_notification_confirmed"] is True
    assert persisted["scan_ok"] is False
    assert trace.notification.tick_metrics["scan_outcomes"]["lx"] is False
    assert trace.rc == 1 and trace.terminal["ok"] is False


def test_confirmed_fanout_failure_delivery_reaches_real_tick_receipt(monkeypatch, tmp_path):
    from src.application.notification_fanout import list_notification_fanouts

    trace = _root_case(monkeypatch, tmp_path, failure="pipeline", fanout=True)
    plans = list_notification_fanouts(base=tmp_path, account="lx", logical_key_prefix="daily-brief:")
    assert len(plans) == 1
    assert plans[0]["delivery_confirmed"] is True
    children = plans[0]["children"]
    assert len(children) == 2
    assert all(child["delivery_confirmed"] is True for child in children)
    assert all(child["spec"]["metadata"]["delivery_kind"] == "fixed_failure" for child in children)
    assert len(trace.sends) == 4
    for child in children:
        assert sum(call["message"] == child["spec"]["message"]
                   and call["idempotency_key"] == child["idempotency_key"] for call in trace.sends) == 1
    assert trace.notification.tick_metrics["scan_outcomes"] == {"lx": False, "sy": True}
    assert trace.notification.tick_metrics["scan_failure_notified_accounts"] == ["lx"]
    persisted = next(row for row in trace.terminal["account_outcomes"] if row["account"] == "lx")
    assert persisted["failure_notification_confirmed"] is True
    assert persisted["scan_ok"] is False
    assert trace.rc == 1 and trace.terminal["ok"] is False


@pytest.mark.parametrize("no_send", [False, True])
def test_positive_qualification_cannot_erase_real_execution_failure(monkeypatch, tmp_path, no_send):
    # Real preparation already rejects failed pipelines. Inject a positive
    # qualification at that boundary to challenge the real notification merge.
    trace = _root_case(
        monkeypatch, tmp_path, failure="pipeline", no_send=no_send, force_positive_qualification=True,
    )
    assert trace.produced_qualification_outcomes == {"lx": False, "sy": True}
    assert trace.qualification_outcomes == {"lx": True, "sy": True}
    assert trace.root_scan_outcomes == {"lx": False, "sy": True}
    assert trace.notification.tick_metrics["scan_outcomes"] == {"lx": False, "sy": True}
    persisted = next(row for row in trace.terminal["account_outcomes"] if row["account"] == "lx")
    assert persisted["scan_ok"] is False
    assert trace.rc == 1 and trace.terminal["ok"] is False
    assert trace.guard_successes == []


def test_account_result_keeps_original_five_field_contract():
    assert [item.name for item in fields(AccountResult)] == [
        "account", "ran_scan", "should_notify", "decision_reason", "notification_text",
    ]
    result = AccountResult("lx", False, False, "scheduler_skipped", "")
    assert result.account == "lx" and result.decision_reason == "scheduler_skipped"
