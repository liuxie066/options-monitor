from __future__ import annotations

from dataclasses import replace

import pytest

import src.application.daily_decision_brief_repository as repo
import src.application.tick_notification_flow as mod
from test_daily_decision_brief_notification_flow import (
    FIXED_TARGET,
    MARKET_DATE,
    IDENTITY,
    _brief,
    _patch_sender,
    _request,
)


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    clock = {"now": "2026-07-21T14:00:30+00:00"}
    blocked = {"value": True}
    calls = []
    monkeypatch.setattr(mod, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(repo, "_utc_now_iso", lambda: clock["now"])

    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        return {
            market: {
                **_brief(
                    base=base,
                    run_id=run_id,
                    account=account,
                    market=market,
                    blocked=blocked["value"],
                    candidate=blocked.get("candidate", True),
                ),
                "generated_at_utc": clock["now"],
                "data_as_of_utc": clock["now"],
            }
            for market in markets_to_run
        }

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", assemble)
    _patch_sender(monkeypatch, calls=calls)
    failed = _request(tmp_path, run_id="failed", pipeline_ok=False)
    assert mod.run_tick_notification_flow(failed.request) == 1
    assert len(calls) == 1
    original = _day(tmp_path)["fixed_reports"][FIXED_TARGET].copy()
    assert original["status"] == "confirmed"
    blocked["value"] = False
    clock["now"] = "2026-07-21T14:30:30+00:00"
    return clock, blocked, calls, original


def _day(base):
    return repo.read_daily_decision_brief_delivery_state(base=base, account="lx", market="US")["state"]["days"][
        MARKET_DATE
    ]


def _recovery(base):
    return _day(base)["recovery_reports"][FIXED_TARGET]


def test_recovery_sends_full_report_once_and_preserves_failed_receipt(recovery, tmp_path):
    clock, _, calls, original = recovery
    restored = _request(tmp_path, run_id="restored", fixed=False)
    assert mod.run_tick_notification_flow(restored.request) == 0
    supplement = _recovery(tmp_path)
    assert supplement["status"] == "confirmed"
    assert supplement["delivery_kind"] == "fixed_recovery_report"
    assert supplement["delivery_key"].startswith("option-report-recovery:US:")
    assert "22:00 批次恢复补报 · 22:30 扫描" in calls[-1]["message"]
    assert "## 持仓" in calls[-1]["message"] and "## 资金" in calls[-1]["message"]
    assert _day(tmp_path)["fixed_reports"][FIXED_TARGET] == original
    assert IDENTITY in _day(tmp_path)["alerted_candidates"]
    clock["now"] = "2026-07-21T14:40:30+00:00"
    assert mod.run_tick_notification_flow(_request(tmp_path, run_id="again", fixed=False).request) == 0
    assert len(calls) == 2


def test_continued_failure_no_send_and_manual_do_not_create_recovery(recovery, tmp_path):
    _, blocked, calls, _ = recovery
    blocked["value"] = True
    mod.run_tick_notification_flow(_request(tmp_path, run_id="still-failed", fixed=False, pipeline_ok=False).request)
    assert len(calls) == 1
    blocked["value"] = False
    mod.run_tick_notification_flow(_request(tmp_path, run_id="preview", fixed=False, no_send=True).request)
    mod.run_tick_notification_flow(_request(tmp_path, run_id="manual", fixed=False, trigger_kind="manual").request)
    assert not _day(tmp_path).get("recovery_reports")
    assert len(calls) == 1


def test_render_crash_rebuilds_bound_revision_without_scan(recovery, monkeypatch, tmp_path):
    clock, _, calls, _ = recovery
    renderer = mod.render_fixed_report
    monkeypatch.setattr(mod, "render_fixed_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render crash")))
    restored = _request(tmp_path, run_id="bound", fixed=False)
    with pytest.raises(RuntimeError, match="render crash"):
        mod.run_tick_notification_flow(restored.request)
    assert restored.commits
    bound = _recovery(tmp_path).copy()
    clock["now"] = "2026-07-21T14:40:30+00:00"
    newer = {**_brief(base=tmp_path, run_id="newer", candidate=False), "generated_at_utc": clock["now"]}
    persisted = repo.persist_daily_decision_brief_success(base=tmp_path, brief=newer)
    assert persisted["current_revision"] > bound["revision"]
    monkeypatch.setattr(mod, "render_fixed_report", renderer)
    forbidden = []

    def forbid(**kwargs):
        forbidden.append(True)
        raise AssertionError("delivery-only must not scan")

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", forbid)
    assert mod.run_tick_notification_flow(_request(tmp_path, run_id="retry-bound", delivery_only=True).request) == 0
    assert not forbidden
    assert _recovery(tmp_path)["revision"] == bound["revision"]
    assert "22:30 扫描" in calls[-1]["message"]


@pytest.mark.parametrize("render_crash", [True, False])
def test_next_fixed_report_replaces_expired_recovery(recovery, monkeypatch, tmp_path, render_crash):
    clock, _, calls, _ = recovery
    renderer = mod.render_fixed_report
    if render_crash:
        monkeypatch.setattr(
            mod, "render_fixed_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render crash"))
        )
        with pytest.raises(RuntimeError):
            mod.run_tick_notification_flow(_request(tmp_path, run_id="bind", fixed=False).request)
        monkeypatch.setattr(mod, "render_fixed_report", renderer)
    else:
        _patch_sender(
            monkeypatch,
            result={
                "ok": False,
                "command_ok": False,
                "delivery_confirmed": False,
                "error_code": "REJECTED",
                "returncode": 1,
            },
        )
        mod.run_tick_notification_flow(_request(tmp_path, run_id="definite-failure", fixed=False).request)
        assert _recovery(tmp_path)["status"] == "pending"
    clock["now"] = "2026-07-21T15:00:00+00:00"
    _patch_sender(monkeypatch, calls=calls)
    request = _request(tmp_path, run_id="next-fixed").request
    decision = {
        **request.scheduler_decisions_by_account["lx"],
        "now_market": "2026-07-21T11:00:00-04:00",
        "scheduled_target_market": "2026-07-21T11:00:00-04:00",
        "scheduled_scan_target_market": "2026-07-21T11:00:00-04:00",
    }
    request = replace(
        request,
        scheduler_decisions_by_account={"lx": decision},
        scheduled_scan_targets_by_account={"lx": decision["scheduled_scan_target_market"]},
    )
    assert mod.run_tick_notification_flow(request) == 0
    assert _recovery(tmp_path)["status"] == "expired_unconfirmed"
    assert "状态｜23:00 批次" in calls[-1]["message"]
    assert "恢复补报" not in calls[-1]["message"]
    assert len(calls) == 2


def test_unknown_outcome_keeps_exact_message_after_next_fixed_point(recovery, monkeypatch, tmp_path):
    clock, _, calls, _ = recovery
    _patch_sender(
        monkeypatch,
        result={
            "ok": False,
            "command_ok": True,
            "delivery_confirmed": False,
            "ambiguous_send": True,
            "error_code": "SEND_UNCONFIRMED",
            "returncode": 1,
        },
    )
    mod.run_tick_notification_flow(_request(tmp_path, run_id="ambiguous", fixed=False).request)
    frozen = _recovery(tmp_path).copy()
    assert frozen["status"] == "ambiguous"
    clock["now"] = "2026-07-21T15:00:00+00:00"
    _patch_sender(monkeypatch, calls=calls)
    mod.run_tick_notification_flow(_request(tmp_path, run_id="retry-unknown", delivery_only=True).request)
    assert calls[-1]["message"] == frozen["rendered_message"]
    assert _recovery(tmp_path)["delivery_key"] == frozen["delivery_key"]
    assert _recovery(tmp_path)["status"] == "confirmed"


def test_cutoff_is_rechecked_immediately_before_provider(recovery, monkeypatch, tmp_path):
    clock, _, calls, _ = recovery
    actual_select = mod.select_notification_delivery_adapter

    def select(provider):
        adapter = actual_select(provider)
        clock["now"] = "2026-07-21T15:00:00+00:00"
        return adapter

    monkeypatch.setattr(mod, "select_notification_delivery_adapter", select)
    assert mod.run_tick_notification_flow(_request(tmp_path, run_id="crossed-cutoff", fixed=False).request) == 1
    assert len(calls) == 1
    assert _recovery(tmp_path)["status"] == "expired_unconfirmed"


def test_expired_unrendered_binding_cannot_resurrect_as_candidate_delivery(recovery, monkeypatch, tmp_path):
    clock, _, calls, _ = recovery
    renderer = mod.render_fixed_report
    monkeypatch.setattr(mod, "render_fixed_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render crash")))
    with pytest.raises(RuntimeError):
        mod.run_tick_notification_flow(_request(tmp_path, run_id="unrendered", fixed=False).request)
    monkeypatch.setattr(mod, "render_fixed_report", renderer)
    clock["now"] = "2026-07-21T15:00:00+00:00"
    mod.run_tick_notification_flow(_request(tmp_path, run_id="late-delivery-only", delivery_only=True).request)
    assert len(calls) == 1
    assert _recovery(tmp_path)["status"] == "expired_unconfirmed"


def test_recovery_uses_actual_feishu_adapter_and_frozen_card(recovery, monkeypatch, tmp_path):
    from src.application import notification_delivery_adapter as adapter
    from test_daily_decision_brief_notification_flow import _feishu_config

    clock, _, _, _ = recovery
    for name, value in (
        ("OM_FEISHU_BOT_APP_ID", "cli_test"),
        ("OM_FEISHU_BOT_APP_SECRET", "fixture-secret"),
        ("OM_FEISHU_BOT_USER_OPEN_ID", "ou_fixture"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(mod, "select_notification_delivery_adapter", adapter.select_notification_delivery_adapter)
    captured, fallback = [], []

    def send_message(**kwargs):
        captured.append(kwargs)
        assert _recovery(tmp_path)["status"] == "ambiguous"
        return {"code": 0, "msg": "success", "data": {"message_id": "msg-recovery"}}

    def forbid_fallback(**kwargs):
        fallback.append(kwargs)
        raise AssertionError("unexpected fallback")

    monkeypatch.setattr(adapter, "send_message", send_message)
    monkeypatch.setattr(adapter, "send_post_message", forbid_fallback)
    assert (
        mod.run_tick_notification_flow(
            _request(tmp_path, run_id="real-adapter", fixed=False, config=_feishu_config()).request
        )
        == 0
    )
    envelope = _recovery(tmp_path)
    assert len(captured) == 1 and not fallback
    assert captured[0]["open_id"] == "ou_fixture"
    assert captured[0]["msg_type"] == "interactive"
    assert captured[0]["content"] == envelope["rendered_transport"]["transport"]["content"]
    assert captured[0]["uuid"] == adapter.build_notification_transport_key(envelope["delivery_key"])
    assert envelope["status"] == "confirmed"


def test_reliable_empty_result_still_sends_full_recovery(recovery, tmp_path):
    _, flags, calls, _ = recovery
    flags["candidate"] = False
    assert mod.run_tick_notification_flow(_request(tmp_path, run_id="empty-recovery", fixed=False).request) == 0
    assert len(calls) == 2
    assert "恢复补报" in calls[-1]["message"]
    assert "## 持仓" in calls[-1]["message"] and "## 资金" in calls[-1]["message"]
    assert _recovery(tmp_path)["candidate_identities"] == []
    assert _recovery(tmp_path)["status"] == "confirmed"


def test_provider_process_exit_leaves_frozen_unknown_attempt(recovery, monkeypatch, tmp_path):
    from types import SimpleNamespace

    clock, _, calls, _ = recovery

    def exit_during_provider(**kwargs):
        assert _recovery(tmp_path)["status"] == "ambiguous"
        raise SystemExit("simulated process exit")

    monkeypatch.setattr(
        mod,
        "select_notification_delivery_adapter",
        lambda _provider: SimpleNamespace(
            send_fn=exit_during_provider,
            normalize_fn=lambda **kwargs: kwargs["send_result"],
            failure_stage="fixture-provider",
        ),
    )
    with pytest.raises(SystemExit, match="simulated process exit"):
        mod.run_tick_notification_flow(_request(tmp_path, run_id="process-exit", fixed=False).request)
    frozen = _recovery(tmp_path).copy()
    assert frozen["status"] == "ambiguous"
    clock["now"] = "2026-07-21T15:00:00+00:00"
    _patch_sender(monkeypatch, calls=calls)
    mod.run_tick_notification_flow(_request(tmp_path, run_id="process-resume", delivery_only=True).request)
    assert calls[-1]["message"] == frozen["rendered_message"]
    assert _recovery(tmp_path)["status"] == "confirmed"


@pytest.mark.parametrize("crash_boundary", ["sqlite_commit", "persist_return"])
@pytest.mark.parametrize("delivery_only", [True, False])
def test_first_committed_recovery_source_survives_before_binding(
    recovery,
    monkeypatch,
    tmp_path,
    crash_boundary,
    delivery_only,
):
    clock, _, calls, original = recovery
    persist, write = mod.persist_daily_decision_brief_success, repo._persist_json

    def exit_after_persist(**kwargs):
        persist(**kwargs)
        raise SystemExit("source committed")

    def exit_before_export(path, payload):
        raise SystemExit("source committed")

    if crash_boundary == "sqlite_commit":
        monkeypatch.setattr(repo, "_persist_json", exit_before_export)
    else:
        monkeypatch.setattr(mod, "persist_daily_decision_brief_success", exit_after_persist)
    bundle = _request(tmp_path, run_id="first-source", fixed=False)
    with pytest.raises(SystemExit, match="source committed"):
        mod.run_tick_notification_flow(bundle.request)
    assert not bundle.commits
    assert not _day(tmp_path).get("recovery_reports")
    source = repo.read_daily_decision_brief_recovery_source(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        scheduled_target_market=FIXED_TARGET,
        original_failure_key=original["delivery_key"],
        original_failure_digest=original["source_digest"],
    )
    assert source["available"]
    assert "notification_recovery_intent" not in source["brief"]
    monkeypatch.setattr(mod, "persist_daily_decision_brief_success", persist)
    monkeypatch.setattr(repo, "_persist_json", write)
    clock["now"] = "2026-07-21T14:35:30+00:00"
    # A newer manual snapshot cannot become the recovery source.
    manual = {**_brief(base=tmp_path, run_id="manual-newer", candidate=False), "generated_at_utc": clock["now"]}
    repo.persist_daily_decision_brief_success(base=tmp_path, brief=manual)
    clock["now"] = "2026-07-21T14:40:30+00:00"
    if delivery_only:
        forbidden = []

        def forbid(**kwargs):
            forbidden.append(True)
            raise AssertionError("delivery-only must not scan")

        monkeypatch.setattr(mod, "assemble_daily_decision_briefs", forbid)
    resumed = _request(tmp_path, run_id="resume-source", fixed=False, delivery_only=delivery_only)
    assert mod.run_tick_notification_flow(resumed.request) == 0
    envelope = _recovery(tmp_path)
    assert envelope["revision"] == source["current_revision"]
    assert envelope["source_digest"] == source["current_brief_digest"]
    assert envelope["source_run_id"] == "first-source"
    assert envelope["render_limits"] == source["recovery_intent"]["render_limits"]
    assert "22:30 扫描" in calls[-1]["message"]
    assert "22:40 扫描" not in calls[-1]["message"]
    if delivery_only:
        assert not forbidden and not resumed.commits
    else:
        assert resumed.commits
    assert len(calls) == 2


def test_unmaterialized_source_intent_expires_before_delivery(recovery, monkeypatch, tmp_path):
    clock, _, calls, _ = recovery
    persist = mod.persist_daily_decision_brief_success

    def exit_after_persist(**kwargs):
        persist(**kwargs)
        raise SystemExit("before binding")

    monkeypatch.setattr(mod, "persist_daily_decision_brief_success", exit_after_persist)
    with pytest.raises(SystemExit):
        mod.run_tick_notification_flow(_request(tmp_path, run_id="late-source", fixed=False).request)
    monkeypatch.setattr(mod, "persist_daily_decision_brief_success", persist)
    clock["now"] = "2026-07-21T15:00:00+00:00"
    assert mod.run_tick_notification_flow(_request(tmp_path, run_id="late-resume", delivery_only=True).request) == 0
    assert len(calls) == 1
    assert _recovery(tmp_path)["status"] == "expired_unconfirmed"
