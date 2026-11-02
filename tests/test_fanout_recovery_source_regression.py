from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import socket

import pytest

from test_daily_brief_fanout_flow import flow, config, fanouts, mod, _request
import src.application.daily_decision_brief_repository as repo
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, history_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("real network forbidden in fanout recovery regressions")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def history(flow, run_id):
    return DecisionHistoryStore(history_path(flow.base)).get(account="lx", market="US", run_id=run_id)


def freeze_brief_generation(monkeypatch, generated_at):
    assemble = mod.assemble_daily_decision_briefs

    def frozen(**kwargs):
        return {market: {**brief, "generated_at_utc": generated_at}
                for market, brief in assemble(**kwargs).items()}

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", frozen)


def recoveries(flow):
    return [item for item in fanouts(flow) if ":recovery:" in item["logical_key"]]


@pytest.mark.parametrize("mode", ["manual", "no_send", "disabled"])
def test_delivery_only_never_promotes_non_delivery_history(flow, mode):
    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:10:00+00:00", blocked=False)
    excluded_cfg = config(("a",))
    if mode == "disabled":
        excluded_cfg["notifications"]["enabled"] = False
    excluded = _request(flow.base, run_id="excluded", fixed=False, config=excluded_cfg,
        trigger_kind="manual" if mode == "manual" else "scheduled", no_send=mode == "no_send")
    mod.run_tick_notification_flow(excluded.request)
    flow.clock["now"] = "2026-07-21T14:11:00+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="resume", delivery_only=True, config=cfg).request)
    assert len(flow.calls) == 1
    assert not recoveries(flow)
    flow.clock["now"] = "2026-07-21T14:20:00+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="eligible", fixed=False, config=cfg).request)
    recovery = recoveries(flow)
    assert len(recovery) == 1 and recovery[0]["delivery_confirmed"]
    assert recovery[0]["children"][0]["spec"]["metadata"]["generated_at_utc"] == flow.clock["now"]


def test_recovery_rebuild_preserves_committed_render_inputs(flow, monkeypatch):
    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:10:00+00:00", blocked=False, candidate=False)
    original = mod._daily_brief_fanout_spec
    def crash(*args, **kwargs):
        if kwargs["kind"] == "fixed_recovery_report":
            raise SystemExit("after source commit")
        return original(*args, **kwargs)
    monkeypatch.setattr(mod, "_daily_brief_fanout_spec", crash)
    with pytest.raises(SystemExit):
        mod.run_tick_notification_flow(_request(flow.base, run_id="eligible", fixed=False, config=cfg).request)
    flow.clock["now"] = "2026-07-21T14:11:00+00:00"
    monkeypatch.setattr(mod, "_daily_brief_fanout_spec", original)
    resumed = _request(flow.base, run_id="resume", delivery_only=True, config=cfg)
    from zoneinfo import ZoneInfo
    mod.run_tick_notification_flow(replace(resumed.request, bj_tz=ZoneInfo("America/Los_Angeles")))
    context = recoveries(flow)[0]["children"][0]["spec"]["metadata"]["render_context"]
    assert context["user_timezone"] == "Asia/Shanghai"
    assert len(flow.assembled) == 2


def test_recovery_survives_history_commit_before_json_export(flow, monkeypatch):
    import src.application.daily_decision_brief_repository as repo

    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:10:00+00:00", blocked=False)
    original = repo._persist_json
    def crash(*args):
        raise SystemExit("history committed, exports not published")
    monkeypatch.setattr(repo, "_persist_json", crash)
    with pytest.raises(SystemExit):
        mod.run_tick_notification_flow(_request(flow.base, run_id="eligible", fixed=False, config=cfg).request)
    monkeypatch.setattr(repo, "_persist_json", original)
    flow.clock["now"] = "2026-07-21T14:11:00+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="resume", delivery_only=True, config=cfg).request)
    assert recoveries(flow)[0]["delivery_confirmed"]
    assert len(flow.assembled) == 2


def test_recovery_without_committed_intent_never_infers_eligibility(flow):
    import src.application.daily_decision_brief_repository as repo
    from test_daily_decision_brief_notification_flow import _brief

    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    repo.persist_daily_decision_brief_success(base=flow.base,
        brief={**_brief(base=flow.base, run_id="legacy-history"), "generated_at_utc": "2026-07-21T14:10:00+00:00"})
    flow.clock["now"] = "2026-07-21T14:11:00+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="resume", delivery_only=True, config=cfg).request)
    assert not recoveries(flow) and len(flow.calls) == 1


def test_confirmed_recovery_same_run_replay_reuses_committed_intent(flow):
    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False)
    bundle = _request(flow.base, run_id="eligible", fixed=False, config=cfg)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    original = history(flow, "eligible")
    committed = original["input"]["notification_recovery_intent"]["fanout_recoveries"][0]
    assert committed["previous_attribution_context"] == {}
    child = recoveries(flow)[0]["children"][0]
    assert child["delivery_confirmed"]
    assert repo.read_confirmed_fanout_attribution_context(base=flow.base, account="lx", market="US",
        identity_key=child["identity_key"])
    envelopes, calls = deepcopy(fanouts(flow)), deepcopy(flow.calls)
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert history(flow, "eligible") == original
    assert fanouts(flow) == envelopes and flow.calls == calls and len(calls) == 2
    assert bundle.commits == [{"lx": "2026-07-21T10:30:00-04:00"}] * 2
    assert DecisionHistoryStore(history_path(flow.base)).revisions(
        account="lx", market="US", market_date="2026-07-21") == [0, 1]


def test_same_run_after_commit_export_interrupt_and_cutoff_keeps_intent(flow, monkeypatch):
    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False, candidate=False)
    bundle = _request(flow.base, run_id="eligible", fixed=False, config=cfg)
    envelopes, calls = deepcopy(fanouts(flow)), deepcopy(flow.calls)

    def crash(*args):
        raise SystemExit("history committed before export")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "_persist_json", crash)
        with pytest.raises(SystemExit, match="history committed before export"):
            mod.run_tick_notification_flow(bundle.request)
    original = history(flow, "eligible")
    assert original["input"]["notification_recovery_intent"]["fanout_recoveries"]
    assert not bundle.commits and not recoveries(flow)
    cutoff = original["input"]["notification_recovery_intent"]["fanout_recoveries"][0]["cutoff_utc"]
    flow.clock["now"] = (datetime.fromisoformat(cutoff) + timedelta(minutes=1)).isoformat()
    freeze_brief_generation(monkeypatch, original["input"]["generated_at_utc"])
    assert mod.run_tick_notification_flow(bundle.request) == 0
    assert history(flow, "eligible") == original
    assert fanouts(flow) == envelopes and flow.calls == calls
    assert bundle.commits == [{"lx": "2026-07-21T10:30:00-04:00"}]
    assert repo.read_latest_daily_decision_brief(base=flow.base, account="lx", market="US")["brief"] == original["payload"]
    assert DecisionHistoryStore(history_path(flow.base)).revisions(
        account="lx", market="US", market_date="2026-07-21") == [0, 1]


@pytest.mark.parametrize("mode", ["manual", "no_send", "disabled"])
def test_same_run_initial_absence_of_intent_is_frozen(flow, mode):
    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False, candidate=False)
    excluded_cfg = deepcopy(cfg)
    if mode == "disabled":
        excluded_cfg["notifications"]["enabled"] = False
    excluded = _request(flow.base, run_id="same-run", fixed=False, config=excluded_cfg,
        trigger_kind="manual" if mode == "manual" else "scheduled", no_send=mode == "no_send")
    assert mod.run_tick_notification_flow(excluded.request) == 0
    original = history(flow, "same-run")
    assert "notification_recovery_intent" not in original["input"]
    envelopes, calls = deepcopy(fanouts(flow)), deepcopy(flow.calls)
    replay = _request(flow.base, run_id="same-run", fixed=False, config=cfg)
    assert mod.run_tick_notification_flow(replay.request) == 0
    assert history(flow, "same-run") == original
    assert not recoveries(flow) and fanouts(flow) == envelopes and flow.calls == calls
    assert replay.commits == [{"lx": "2026-07-21T10:30:00-04:00"}]
    assert DecisionHistoryStore(history_path(flow.base)).revisions(
        account="lx", market="US", market_date="2026-07-21") == [0, 1]


@pytest.mark.parametrize("changed_brief", [False, True])
def test_existing_run_validates_real_input_before_observing_notification_state(flow, monkeypatch, changed_brief):
    cfg = config(("a",))
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", pipeline_ok=False, config=cfg).request)
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False)
    bundle = _request(flow.base, run_id="eligible", fixed=False, config=cfg)
    mod.run_tick_notification_flow(bundle.request)
    original, envelopes, calls = history(flow, "eligible"), deepcopy(fanouts(flow)), deepcopy(flow.calls)

    def forbidden(*args, **kwargs):
        pytest.fail("an existing run must not observe dynamic recovery inputs")

    monkeypatch.setattr(mod, "_daily_brief_fanout_recovery_intent", forbidden)
    if changed_brief:
        flow.clock["candidate"] = False
        with pytest.raises(repo.DailyDecisionBriefStateError, match="history_run_conflict"):
            mod.run_tick_notification_flow(bundle.request)
    else:
        assert mod.run_tick_notification_flow(bundle.request) == 0
    assert history(flow, "eligible") == original
    assert fanouts(flow) == envelopes and flow.calls == calls


@pytest.mark.parametrize("successful_first", [False, True])
def test_same_run_changed_success_qualification_remains_conflict(flow, successful_first):
    from test_daily_decision_brief_notification_flow import _brief

    brief = _brief(base=flow.base, run_id="qualification")
    success = repo.persist_daily_decision_brief_success
    failure = repo.persist_daily_decision_brief_failure
    first, replay = (success, failure) if successful_first else (failure, success)
    first(base=flow.base, brief=brief)
    original = history(flow, "qualification")
    with pytest.raises(repo.DailyDecisionBriefStateError, match="history_run_conflict"):
        replay(base=flow.base, brief=brief)
    assert history(flow, "qualification") == original
    assert DecisionHistoryStore(history_path(flow.base)).revisions(
        account="lx", market="US", market_date="2026-07-21") == [0]
    assert not flow.calls and not fanouts(flow)
