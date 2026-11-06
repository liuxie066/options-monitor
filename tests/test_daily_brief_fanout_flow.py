from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import src.application.channels.plugins as plugins
import src.application.daily_decision_brief_repository as repo
import src.application.tick_notification_flow as mod
from src.application.notification_delivery_route import notification_reminder_route_key
from src.application.notification_fanout import list_notification_fanouts
from test_daily_decision_brief_notification_flow import (
    _brief, _config, _request, fixed_report_flow, FIXED_TARGET, MARKET_DATE, IDENTITY,
)


def config(routes=("a", "b")):
    cfg = _config()
    cfg["notifications"]["routes"] = [{"id": route, "provider": "feishu_app",
        "target": "ou_" + route, "accounts": ["lx"]} for route in routes]
    return cfg


@pytest.fixture
def flow(monkeypatch, tmp_path):
    clock = {"now": "2026-07-21T14:00:30+00:00", "blocked": False, "candidate": True}
    monkeypatch.setattr(mod, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(repo, "_utc_now_iso", lambda: clock["now"])
    monkeypatch.setattr(mod, "report_system_meta_signal", lambda **_kwargs: "suppressed")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_legacy")
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _: True)
    assembled, calls, outcomes = [], [], {}
    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        assembled.append((run_id, account))
        return {market: {**_brief(base=base, run_id=run_id, account=account, market=market,
            blocked=clock["blocked"], candidate=clock["candidate"]), "generated_at_utc": clock["now"]}
            for market in markets_to_run}
    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", assemble)
    def send(**kwargs):
        calls.append(deepcopy(kwargs))
        route = kwargs["target"].removeprefix("ou_")
        result = outcomes.get(route, {"delivery_confirmed": True})
        return {**result, "attribution_route_key": notification_reminder_route_key(
            config={"notifications": kwargs["notifications"]}, base=tmp_path)}
    monkeypatch.setattr(mod, "select_notification_delivery_adapter", lambda _: SimpleNamespace(
        send_fn=send, normalize_fn=lambda *, send_result: send_result, failure_stage="fixture"))
    return SimpleNamespace(clock=clock, calls=calls, assembled=assembled, outcomes=outcomes, base=tmp_path)


def fanouts(flow):
    return list_notification_fanouts(base=flow.base, account="lx", logical_key_prefix="daily-brief:")


def test_real_entry_mixed_children_continue_only_failed_without_assembly_or_retarget(flow, monkeypatch):
    flow.outcomes.update(b={"explicit_pre_acceptance_failure": True}, c={"command_ok": True})
    first = _request(flow.base, run_id="first", config=config(("a", "b", "c")))
    assert mod.run_tick_notification_flow(first.request) == 1
    assert first.commits == [{"lx": FIXED_TARGET}]
    original = fanouts(flow)[0]
    assert original["status"] == "partial" and original["has_retryable_children"]
    assert original["has_unknown_children"] and not original["explicit_pre_acceptance_failure"]
    assert len(flow.assembled) == 1 and len(flow.calls) == 3
    flow.clock["now"] = "2026-07-21T14:01:00+00:00"
    flow.outcomes["b"] = {"delivery_confirmed": True}
    retry = _request(flow.base, run_id="retry", delivery_only=True, config=config(("a", "b", "c", "d")))
    assert mod.run_tick_notification_flow(retry.request) == 1  # unknown sibling remains visible
    assert len(flow.assembled) == 1 and retry.commits == []
    assert [call["target"] for call in flow.calls] == ["ou_a", "ou_b", "ou_c", "ou_b"]
    assert flow.calls[1] == flow.calls[3]
    after = fanouts(flow)[0]
    assert len(after["children"]) == 3 and not after["has_retryable_children"]
    rows = repo.query_daily_brief_receipts(base=flow.base, account="lx", market="US", query={"run_id": "retry"})
    assert sorted(row["delivery_state"] for row in rows) == ["attempted", "confirmed", "confirmed"]
    assert {row["business_result"]["route_id"] for row in rows} == {"a", "b", "c"}


@pytest.mark.parametrize("candidate", [False, True], ids=["valid-empty", "still-valid-actions"])
def test_real_adapter_fanout_sends_identical_fixed_batches_once_per_route(fixed_report_flow, candidate):
    flow = fixed_report_flow
    flow.clock["candidate"] = candidate
    cfg = config()
    assert mod.run_tick_notification_flow(flow.request(run_id="fanout-fixed-first", config=cfg).request) == 0
    first = list_notification_fanouts(base=flow.base, account="lx", logical_key_prefix="daily-brief:")[0]
    assert first["status"] == "confirmed"
    assert {child["route_id"] for child in first["children"]} == {"a", "b"}
    assert {call["open_id"] for call in flow.calls} == {"ou_a", "ou_b"}
    assert len(flow.calls) == 2
    frozen_first = deepcopy(first)
    assert mod.run_tick_notification_flow(flow.request(run_id="fanout-fixed-first-replay", config=cfg).request) == 0
    assert len(flow.calls) == 2

    next_target = "2026-07-21T11:00:00-04:00"
    flow.clock["now"] = "2026-07-21T15:00:30+00:00"
    assert mod.run_tick_notification_flow(
        flow.request(run_id="fanout-fixed-second", target=next_target, config=cfg).request,
    ) == 0
    rows = list_notification_fanouts(base=flow.base, account="lx", logical_key_prefix="daily-brief:")
    assert len(rows) == 2 and all(row["status"] == "confirmed" for row in rows)
    assert next(row for row in rows if row["fanout_id"] == first["fanout_id"]) == frozen_first
    second = next(row for row in rows if row["fanout_id"] != first["fanout_id"])
    assert second["logical_key"].endswith(next_target)
    assert {child["route_id"] for child in second["children"]} == {"a", "b"}
    assert all(child["spec"]["metadata"]["delivery_kind"] == "fixed_report" for child in second["children"])
    assert len(flow.calls) == 4
    assert len({call["uuid"] for call in flow.calls}) == 4
    assert mod.run_tick_notification_flow(
        flow.request(run_id="fanout-fixed-second-replay", target=next_target, config=cfg).request,
    ) == 0
    assert len(flow.calls) == 4
    day = repo.read_daily_decision_brief_delivery_state(
        base=flow.base, account="lx", market="US",
    )["state"]["days"][MARKET_DATE]
    assert day["fixed_reports"] == {} and day["candidate_delivery"] is None


def test_new_fixed_fanout_preserves_prior_route_failure_for_exact_retry(flow):
    flow.clock["now"] = "2026-07-21T13:30:30+00:00"
    flow.outcomes["b"] = {"explicit_pre_acceptance_failure": True}
    assert mod.run_tick_notification_flow(
        _request(flow.base, run_id="candidate-partial-before-fixed", fixed=False, config=config()).request,
    ) == 1
    old = next(item for item in fanouts(flow) if ":candidate:" in item["logical_key"])
    assert old["status"] == "partial"
    frozen = {child["route_id"]: deepcopy(child["spec"]) for child in old["children"]}
    flow.clock["now"] = "2026-07-21T14:00:30+00:00"
    flow.outcomes.clear()
    assert mod.run_tick_notification_flow(
        _request(flow.base, run_id="fixed-after-partial-candidate", config=config()).request,
    ) == 0
    rows = fanouts(flow)
    fixed = next(item for item in rows if ":fixed:" in item["logical_key"])
    assert fixed["status"] == "confirmed"
    assert all(child["spec"]["metadata"]["delivery_kind"] == "fixed_report" for child in fixed["children"])
    continued = next(item for item in rows if item["fanout_id"] == old["fanout_id"])
    assert continued["status"] == "confirmed"
    assert {child["route_id"]: child["spec"] for child in continued["children"]} == frozen
    assert [call["target"] for call in flow.calls].count("ou_a") == 2
    assert [call["target"] for call in flow.calls].count("ou_b") == 3
    day = repo.read_daily_decision_brief_delivery_state(
        base=flow.base, account="lx", market="US",
    )["state"]["days"][MARKET_DATE]
    assert day["fixed_reports"] == {} and day["candidate_delivery"] is None


def test_current_binding_rotation_and_route_removal_pause_without_attempt(flow, monkeypatch):
    flow.outcomes.update(a={"explicit_pre_acceptance_failure": True}, b={"explicit_pre_acceptance_failure": True})
    mod.run_tick_notification_flow(_request(flow.base, run_id="first", config=config()).request)
    flow.clock["now"] = "2026-07-21T14:01:00+00:00"
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "rotated-app")
    mod.run_tick_notification_flow(_request(flow.base, run_id="rotated", delivery_only=True, config=config(("a",))).request)
    assert len(flow.calls) == 2
    assert [child["attempt_count"] for child in fanouts(flow)[0]["children"]] == [1, 1]


@pytest.mark.parametrize("mode", ["no_send", "manual", "disabled", "empty"])
def test_non_delivery_scans_keep_history_and_never_create_obligations(flow, mode):
    cfg = config(()) if mode == "empty" else config()
    if mode == "disabled":
        cfg["notifications"]["enabled"] = False
    req = _request(flow.base, run_id=mode, config=cfg, no_send=mode == "no_send",
        trigger_kind="manual" if mode == "manual" else "scheduled")
    assert mod.run_tick_notification_flow(req.request) == 0
    assert not flow.calls and not fanouts(flow)
    assert repo.read_latest_daily_decision_brief(base=flow.base, account="lx", market="US")["available"]


def test_quiet_hours_keep_frozen_pending_without_attempt(flow):
    cfg = config()
    cfg["notifications"]["quiet_hours_beijing"] = {"start": "00:00", "end": "23:59"}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="quiet", config=cfg).request) == 0
    assert not flow.calls
    assert [child["attempt_count"] for child in fanouts(flow)[0]["children"]] == [0, 0]


def test_missing_plugin_is_configuration_failure_retaining_pending(flow, monkeypatch):
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _: False)
    req = _request(flow.base, run_id="missing", config=config())
    assert mod.run_tick_notification_flow(req.request) == 1
    assert not flow.calls and fanouts(flow)[0]["has_retryable_children"]
    assert req.request.tick_metrics["notification_config_failures"][0]["error_code"] == "CHANNEL_PLUGIN_NOT_INSTALLED"


def test_candidate_reminders_use_independent_child_receipts(flow):
    flow.outcomes["b"] = {"command_ok": True}
    mod.run_tick_notification_flow(_request(flow.base, run_id="fixed", config=config()).request)
    flow.clock["now"] = "2026-07-21T14:30:30+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="same-candidates", fixed=False, config=config()).request)
    assert len(flow.calls) == 2  # confirmed and unknown both retain their original obligation
    children = repo.read_daily_brief_fanout_children(base=flow.base, account="lx", market="US")
    assert repo.read_confirmed_fanout_attribution_context(base=flow.base, account="lx", market="US",
        identity_key=children[0]["identity_key"])
    assert not repo.read_confirmed_fanout_attribution_context(base=flow.base, account="lx", market="US",
        identity_key=children[1]["identity_key"])


def test_recovery_only_confirmed_failure_route_and_zero_candidates_is_valid(flow):
    flow.clock["blocked"] = True
    flow.outcomes["b"] = {"command_ok": True}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="failed", config=config(), pipeline_ok=False).request) == 1
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False, candidate=False)
    req = _request(flow.base, run_id="recovered", fixed=False, config=config())
    mod.run_tick_notification_flow(req.request)
    recovery = [item for item in fanouts(flow) if ":recovery:" in item["logical_key"]]
    assert len(recovery) == 1 and recovery[0]["delivery_confirmed"]
    child = recovery[0]["children"][0]
    assert child["route_id"] == "a" and child["spec"]["metadata"]["candidate_identities"] == []
    assert len(flow.calls) == 3 and "恢复" in flow.calls[-1]["message"]
    flow.clock["now"] = "2026-07-21T14:31:30+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="again", delivery_only=True, config=config()).request)
    assert len(flow.calls) == 3


def test_fanout_is_durable_before_scheduler_commit_and_recovered_after_parent_crash(flow):
    req = _request(flow.base, run_id="crashed", config=config())
    def crash(_targets):
        assert len(fanouts(flow)) == 1
        raise SystemExit("commit process lost")
    with pytest.raises(SystemExit):
        mod.run_tick_notification_flow(replace(req.request, commit_scan_targets_fn=crash))
    assert not flow.calls
    retry = _request(flow.base, run_id="delivery", delivery_only=True, config=config(("a", "b", "c")))
    assert mod.run_tick_notification_flow(retry.request) == 0
    assert len(flow.calls) == 2 and len(flow.assembled) == 1


def test_account_route_selection_does_not_leak_other_account_brief(flow):
    cfg = config(("a",))
    cfg["notifications"]["routes"].append({"id": "sy", "provider": "feishu_app", "target": "ou_sy", "accounts": ["sy"]})
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="accounts", accounts=("lx", "sy"), config=cfg).request) == 0
    assert len(flow.calls) == 2
    assert "· lx" in flow.calls[0]["message"] and "· sy" in flow.calls[1]["message"]


def test_recovery_render_crash_continues_earliest_committed_source_without_assembly(flow, monkeypatch):
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", config=config(), pipeline_ok=False).request)
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False)
    original = mod._daily_brief_fanout_spec
    def crash(*args, **kwargs):
        if kwargs["kind"] == "fixed_recovery_report":
            raise SystemExit("render process lost")
        return original(*args, **kwargs)
    monkeypatch.setattr(mod, "_daily_brief_fanout_spec", crash)
    with pytest.raises(SystemExit):
        mod.run_tick_notification_flow(_request(flow.base, run_id="first-ready", fixed=False, config=config()).request)
    assert len(flow.calls) == 2 and len(flow.assembled) == 2
    later = {**_brief(base=flow.base, run_id="later-ready", candidate=False),
        "generated_at_utc": "2026-07-21T14:40:00+00:00"}
    repo.persist_daily_decision_brief_success(base=flow.base, brief=later)
    flow.clock["now"] = "2026-07-21T14:41:00+00:00"
    monkeypatch.setattr(mod, "_daily_brief_fanout_spec", original)
    req = _request(flow.base, run_id="resume", delivery_only=True, config=config(("a", "b", "c")))
    assert mod.run_tick_notification_flow(req.request) == 0
    assert len(flow.assembled) == 2 and len(flow.calls) == 4
    recoveries = [child for item in fanouts(flow) if ":recovery:" in item["logical_key"] for child in item["children"]]
    assert {child["spec"]["metadata"]["generated_at_utc"] for child in recoveries} == {"2026-07-21T14:30:30+00:00"}
    assert all(child["spec"]["metadata"]["candidate_identities"] == [IDENTITY] for child in recoveries)
    assert {child["route_id"] for child in recoveries} == {"a", "b"}


def test_recovery_retry_never_sends_after_original_window(flow):
    flow.clock["blocked"] = True
    mod.run_tick_notification_flow(_request(flow.base, run_id="failed", config=config(), pipeline_ok=False).request)
    flow.clock.update(now="2026-07-21T14:30:30+00:00", blocked=False, candidate=False)
    flow.outcomes.update(a={"explicit_pre_acceptance_failure": True}, b={"explicit_pre_acceptance_failure": True})
    mod.run_tick_notification_flow(_request(flow.base, run_id="ready", fixed=False, config=config()).request)
    recoveries = [item for item in fanouts(flow) if ":recovery:" in item["logical_key"]]
    assert len(recoveries) == 2
    flow.clock["now"] = "2026-07-22T00:00:00+00:00"
    mod.run_tick_notification_flow(_request(flow.base, run_id="expired", delivery_only=True, config=config()).request)
    assert len(flow.calls) == 4
    assert all(item["children"][0]["attempt_count"] == 1 for item in fanouts(flow) if ":recovery:" in item["logical_key"])


def test_run_receipt_fails_closed_if_frozen_child_content_changes(flow):
    import hashlib
    import json
    mod.run_tick_notification_flow(_request(flow.base, run_id="frozen", config=config()).request)
    path = next(flow.base.rglob("nf_*.json"))
    state = json.loads(path.read_text())
    for spec in [state["plan"]["deliveries"][0], state["children"][0]["spec"]]:
        spec["message"] = "replacement body"
    state["plan_sha256"] = hashlib.sha256(json.dumps(state["plan"], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="run_content_mismatch"):
        repo.query_daily_brief_receipts(base=flow.base, account="lx", market="US", query={"run_id": "frozen"})


@pytest.mark.parametrize("provider,channel", [("feishu_app", "feishu"), ("wechat_clawbot", "wechat_clawbot")])
@pytest.mark.parametrize("flags", [{}, {"enabled": False, "receive_enabled": True, "notify_enabled": True},
    {"enabled": True, "receive_enabled": True, "notify_enabled": False}])
def test_explicit_channels_without_routes_disabled_scan_never_resolves_credentials(flow, monkeypatch, provider, channel, flags):
    import src.application.notification_delivery_route as routes
    cfg = _config()
    cfg["notifications"].update(provider=provider, channel=provider)
    cfg["channels"] = {channel: flags}
    def forbidden(**kwargs):
        pytest.fail("disabled channel resolved sender credentials")
    monkeypatch.setattr(routes, "build_notification_channel_registry", forbidden)
    monkeypatch.setattr(plugins, "channel_plugin_installed", forbidden)
    req = _request(flow.base, run_id="disabled-single", config=cfg)
    assert mod.run_tick_notification_flow(req.request) == 0
    assert req.commits == [{"lx": FIXED_TARGET}]
    assert not flow.calls and not fanouts(flow)
    assert repo.read_latest_daily_decision_brief(base=flow.base, account="lx", market="US")["available"]


def test_single_route_explicit_channel_disable_pauses_frozen_retry_without_credentials(flow, monkeypatch):
    import src.application.notification_delivery_route as routes
    cfg = _config()
    cfg["notifications"].update(provider="feishu_app", channel="feishu_app")
    cfg["channels"] = {"feishu": {"enabled": True, "receive_enabled": False, "notify_enabled": True}}
    flow.outcomes["legacy"] = {"explicit_pre_acceptance_failure": True}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="single", config=cfg).request) == 1
    assert len(flow.calls) == 1 and fanouts(flow)[0]["children"][0]["route_id"] == "legacy"
    flow.clock["now"] = "2026-07-21T14:01:00+00:00"
    cfg["channels"]["feishu"]["enabled"] = False
    def forbidden(**kwargs):
        pytest.fail("disabled continuation resolved sender credentials")
    monkeypatch.setattr(routes, "build_notification_channel_registry", forbidden)
    mod.run_tick_notification_flow(_request(flow.base, run_id="paused-single", delivery_only=True, config=cfg).request)
    assert len(flow.calls) == 1 and fanouts(flow)[0]["children"][0]["attempt_count"] == 1


def test_combo_exposure_readback_uses_only_confirmed_fanout_child(tmp_path):
    from domain.domain.combo_candidate_evidence import combo_exposure_render_context, derive_combo_candidate_exposures
    from src.application.notification_fanout import FanoutRetryPolicy, prepare_notification_fanout, dispatch_notification_fanout
    from test_combo_candidate_evidence import _candidate_row, _occurrence, _brief as combo_brief
    row = _candidate_row()
    row.update(_occurrence(row))
    source = repo.persist_daily_decision_brief_success(base=tmp_path, brief=combo_brief(row, run_id="run-1"))
    metadata = {"market": "US", "market_trading_date": "2026-07-17", "delivery_kind": "fixed_report",
        "revision": source["current_revision"], "source_digest": source["current_brief_digest"],
        "render_context": combo_exposure_render_context(derive_combo_candidate_exposures(source["brief"]))}
    snapshot = prepare_notification_fanout(base=tmp_path, account="lx", logical_key="daily-brief:combo-fixture",
        deliveries=[{"recipient": {"route_id": route, "provider": "feishu_app", "channel": "feishu_app",
            "target": route, "identity_key": route, "transport_options": {}},
            "message": "frozen combo", "metadata": metadata} for route in ("a", "b")],
        retry_policy=FanoutRetryPolicy(2, (1000,)))
    def send(*, spec, **_kwargs):
        return {"delivery_confirmed": spec["recipient"]["route_id"] == "a",
            "attribution_route_key": spec["recipient"]["identity_key"]}
    snapshot = dispatch_notification_fanout(base=tmp_path, fanout_id=snapshot["fanout_id"], send_fn=send)
    result = repo.read_combo_candidate_exposures(base=tmp_path, account="lx", market="US", market_trading_date="2026-07-17")
    assert result["complete"] and result["exposures"][0]["delivery_confirmed"]
    assert result["exposures"][0]["confirmed_delivery_keys"] == [snapshot["children"][0]["child_id"]]


def test_legacy_confirmed_fixed_report_is_not_backfilled_when_routes_are_added(flow):
    cfg = _config()
    cfg["notifications"].update(provider="feishu_app", channel="feishu_app")
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="legacy", config=cfg).request) == 0
    assert len(flow.calls) == 1
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="new-routes", config=config()).request) == 0
    assert len(flow.calls) == 1 and not fanouts(flow)


def test_per_route_display_evidence_keeps_unshown_candidates_eligible(flow, monkeypatch):
    actual_fits = mod.feishu_notification_blocks_fit
    def smaller_b_budget(blocks, text, *, open_id, **kwargs):
        return actual_fits(blocks, text, open_id=open_id, **kwargs) and (
            open_id != "ou_b" or not any(getattr(block, "tier", None) == "candidate" for block in blocks))
    monkeypatch.setattr(mod, "feishu_notification_blocks_fit", smaller_b_budget)
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="different-budgets", config=config()).request) == 0
    first = {child["route_id"]: deepcopy(child["spec"]) for child in fanouts(flow)[0]["children"]}
    assert first["a"]["metadata"]["display_evidence"] == [{"identity": IDENTITY, "state": "expanded"}]
    assert first["b"]["metadata"]["display_evidence"] == [{"identity": IDENTITY, "state": "not_shown"}]
    assert "正文大小限制" in first["b"]["message"]
    monkeypatch.setattr(mod, "feishu_notification_blocks_fit", actual_fits)
    flow.clock["now"] = "2026-07-21T14:30:30+00:00"
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="unshown-next-scan", fixed=False, config=config()).request) == 0
    assert [call["notifications"]["target"] for call in flow.calls] == ["ou_a", "ou_b", "ou_b"]
    candidates = [item for item in fanouts(flow) if ":candidate:" in item["logical_key"]]
    assert len(candidates) == 1 and candidates[0]["children"][0]["route_id"] == "b"
    assert candidates[0]["children"][0]["spec"]["metadata"]["display_evidence"] == [
        {"identity": IDENTITY, "state": "expanded"}]


def test_failed_child_retry_keeps_original_display_evidence(flow, monkeypatch):
    flow.outcomes["b"] = {"explicit_pre_acceptance_failure": True}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="freeze-evidence", config=config()).request) == 1
    initial = deepcopy(fanouts(flow)[0]["children"])
    assert all(child["spec"]["metadata"]["display_evidence"] == [
        {"identity": IDENTITY, "state": "expanded"}] for child in initial)
    def forbidden(*_args, **_kwargs):
        pytest.fail("failed child retry must reuse its frozen display evidence")
    monkeypatch.setattr(mod, "select_daily_brief_notification", forbidden)
    flow.clock["now"] = "2026-07-21T14:01:00+00:00"
    flow.outcomes["b"] = {"delivery_confirmed": True}
    cfg = config(("a", "b", "new"))
    cfg["notifications"]["daily_brief"] = {"max_actions": 1}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="retry-evidence", delivery_only=True, config=cfg).request) == 0
    retried = fanouts(flow)[0]["children"]
    assert [child["spec"] for child in retried] == [child["spec"] for child in initial]
    assert [call["notifications"]["target"] for call in flow.calls] == ["ou_a", "ou_b", "ou_b"]


def _legacy_pending(flow):
    cfg = _config()
    cfg["notifications"].update(provider="feishu_app", channel="feishu_app")
    flow.outcomes["legacy"] = {"command_ok": False, "delivery_confirmed": False,
        "explicit_pre_acceptance_failure": True, "http_status": 400}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="legacy-pending", config=cfg).request) == 1
    envelope = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert envelope["status"] == "pending" and len(flow.calls) == 2
    return cfg, deepcopy(envelope)


def _continuation_config():
    cfg = config(("new",))
    cfg["notifications"]["routes"].insert(0, {"id": "original", "provider": "feishu_app",
        "target": "ou_legacy", "accounts": ["lx"]})
    return cfg


@pytest.mark.parametrize("use_routes", [True, False])
def test_legacy_pending_continues_exact_envelope_key_and_budget_only_to_original_actual_target(flow, use_routes):
    from src.application.notification_delivery_adapter import build_notification_transport_key
    _cfg, envelope = _legacy_pending(flow)
    continuation_cfg = _continuation_config() if use_routes else {
        **_cfg, "channels": {"feishu": {"enabled": True, "notify_enabled": True}}}
    original_call = deepcopy(flow.calls[-1])
    flow.clock["now"] = "2026-07-21T14:01:00+00:00"
    req = _request(flow.base, run_id="migrated-rejected", delivery_only=True, config=continuation_cfg)
    assert mod.run_tick_notification_flow(req.request) == 1
    assert len(flow.calls) == 4  # The existing per-invocation two-attempt budget.
    for call in flow.calls[2:]:
        for key in ("target", "channel", "message", "transport_envelope", "idempotency_key"):
            assert call[key] == original_call[key]
        assert call["idempotency_key"] == build_notification_transport_key(envelope["delivery_key"])
    flow.outcomes["legacy"] = {"delivery_confirmed": True}
    req = _request(flow.base, run_id="migrated-confirmed", delivery_only=True, config=continuation_cfg)
    assert mod.run_tick_notification_flow(req.request) == 0
    assert len(flow.calls) == 5 and len(flow.assembled) == 1 and not req.commits
    state = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]
    after = state["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert after["status"] == "confirmed"
    for key in ("delivery_key", "source_digest", "revision", "rendered_message", "rendered_transport", "message_sha256"):
        assert after[key] == envelope[key]
    assert not fanouts(flow)
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="again", delivery_only=True, config=continuation_cfg).request) == 0
    assert len(flow.calls) == 5


@pytest.mark.parametrize("change", ["disabled", "removed", "rotated", "no_send"])
def test_legacy_pending_current_controls_never_consume_original_obligation(flow, monkeypatch, change):
    _cfg, before = _legacy_pending(flow)
    cfg = _continuation_config()
    if change == "disabled":
        cfg["channels"] = {"feishu": {"enabled": False, "notify_enabled": True}}
    elif change == "removed":
        cfg["notifications"]["routes"].pop(0)
    elif change == "rotated":
        monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "another-app")
    if change in {"disabled", "no_send"}:
        def forbidden(_provider):
            pytest.fail("paused legacy continuation inspected optional package")
        monkeypatch.setattr(plugins, "channel_plugin_installed", forbidden)
    req = _request(flow.base, run_id="paused-legacy", delivery_only=True,
        config=cfg, no_send=change == "no_send")
    mod.run_tick_notification_flow(req.request)
    after = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert after == before and len(flow.calls) == 2 and len(flow.assembled) == 1


def test_legacy_ambiguous_sibling_is_not_resent_or_allowed_to_hide_pending(flow):
    cfg = _config()
    cfg["notifications"].update(provider="feishu_app", channel="feishu_app")
    flow.outcomes["legacy"] = {"command_ok": True, "ambiguous_send": True}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="legacy-unknown", config=cfg).request) == 1
    old = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert old["status"] == "ambiguous"
    pending = repo.prepare_daily_decision_brief_delivery(base=flow.base, account="lx", market="US",
        market_trading_date=MARKET_DATE, run_id="second-legacy", delivery_kind="fixed_report",
        source_kind="successful_brief", source_digest=old["source_digest"], revision=old["revision"],
        candidate_identities=old["candidate_identities"], scheduled_target_market="2026-07-21T10:05:00-04:00",
        rendered_message="Exact second legacy body", render_context=old["render_context"])["envelope"]
    flow.outcomes["legacy"] = {"delivery_confirmed": True}
    assert mod.run_tick_notification_flow(_request(flow.base, run_id="continue-sibling", delivery_only=True, config=_continuation_config()).request) == 0
    assert len(flow.calls) == 2 and flow.calls[-1]["message"] == pending["rendered_message"]
    day = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]["days"][MARKET_DATE]
    assert day["fixed_reports"][FIXED_TARGET] == old
    assert day["fixed_reports"]["2026-07-21T10:05:00-04:00"]["status"] == "confirmed"


def test_ordinary_scan_continues_legacy_pending_without_backfilling_added_target(flow):
    _cfg, envelope = _legacy_pending(flow)
    flow.clock["now"] = "2026-07-21T14:30:30+00:00"
    flow.outcomes["legacy"] = {"delivery_confirmed": True}
    req = _request(flow.base, run_id="ordinary-migration", fixed=False, config=_continuation_config())
    assert mod.run_tick_notification_flow(req.request) == 0
    assert len(flow.assembled) == 2 and len(flow.calls) == 3
    assert flow.calls[-1]["message"] == envelope["rendered_message"]
    assert flow.calls[-1]["target"] == "ou_legacy" and not fanouts(flow)
    after = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert after["status"] == "confirmed" and after["revision"] == envelope["revision"]


def test_legacy_pending_attempt_that_becomes_unknown_is_not_retried(flow):
    _legacy_pending(flow)
    flow.outcomes["legacy"] = {"command_ok": False, "delivery_confirmed": False, "ambiguous_send": True}
    req = _request(flow.base, run_id="migrated-unknown", delivery_only=True, config=_continuation_config())
    assert mod.run_tick_notification_flow(req.request) == 1
    assert len(flow.calls) == 3
    envelope = repo.read_daily_decision_brief_delivery_state(base=flow.base, account="lx", market="US")["state"]["days"][MARKET_DATE]["fixed_reports"][FIXED_TARGET]
    assert envelope["status"] == "ambiguous"
    mod.run_tick_notification_flow(_request(flow.base, run_id="no-unknown-replay", delivery_only=True, config=_continuation_config()).request)
    assert len(flow.calls) == 3
