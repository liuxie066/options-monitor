from __future__ import annotations

import pytest

import src.application.daily_decision_brief_repository as repo
import src.application.tick_notification_flow as mod
from src.application.notification_delivery_adapter import build_notification_transport_key
from test_daily_decision_brief_notification_flow import _request
from test_daily_decision_brief_recovery_flow import _day, _recovery, recovery
from test_daily_decision_brief_recovery_repository import SCOPE, identity
from test_daily_decision_brief_repository_v2 import _legacy_combo_brief, _prepare


def test_confirmed_recovery_is_read_as_delivered_combo_exposure(recovery, monkeypatch, tmp_path):
    clock, _, calls, original = recovery

    def assemble(*, run_id, markets_to_run, **kwargs):
        brief = _legacy_combo_brief(run_id=run_id)
        brief["actions"][0].pop("action_id")
        brief["actions"][0]["candidate_pair_id"] = "pair-nvda-100-110"
        brief["actions"][0]["strategy_group_id"] = ""
        brief["candidate_index"][0]["representative"]["strategy_group_id"] = ""
        brief["generated_at_utc"] = clock["now"]
        return {market: brief for market in markets_to_run}

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", assemble)
    prepared = mod._prepare_daily_brief_notification(
        _request(tmp_path, run_id="combo-recovery", fixed=False).request,
    )
    envelope = prepared.lifecycles_by_account["lx"]["envelope"]
    assert envelope["delivery_kind"] == "fixed_recovery_report"
    assert envelope["status"] == "pending"
    before = repo.read_combo_candidate_exposures(base=tmp_path, **SCOPE)
    assert before["complete"] is True
    assert len(before["exposures"]) == 1
    exposure = before["exposures"][0]
    assert exposure["candidate_exposure_id"] in envelope["render_context"]["candidate_exposure_ids"]
    assert exposure["candidate_occurrence_id"] in envelope["render_context"]["candidate_occurrence_ids"]
    assert exposure["delivery_confirmed"] is False
    assert exposure["confirmed_delivery_keys"] == []

    def forbid_scan(**kwargs):
        raise AssertionError("delivery-only must not scan")

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", forbid_scan)
    assert (
        mod.run_tick_notification_flow(
            _request(tmp_path, run_id="send-combo-recovery", delivery_only=True).request,
        )
        == 0
    )
    assert len(calls) == 2
    assert calls[-1]["message"] == envelope["rendered_message"]
    assert _recovery(tmp_path)["status"] == "confirmed"
    assert _day(tmp_path)["fixed_reports"][original["scheduled_target_market"]] == original
    after = repo.read_combo_candidate_exposures(base=tmp_path, **SCOPE)
    assert after["reason"] == "ok" and after["complete"] is True
    assert after["exposures"] == [
        {**exposure, "delivery_confirmed": True, "confirmed_delivery_keys": [envelope["delivery_key"]]},
    ]


@pytest.mark.parametrize("ambiguous", [True, False], ids=["ambiguous", "pending"])
def test_delivery_only_prioritizes_unknown_candidate_over_unrendered_supplement(
    recovery,
    monkeypatch,
    tmp_path,
    ambiguous,
):
    clock, _, calls, _ = recovery
    renderer = mod.render_fixed_report

    def crash_render(*args, **kwargs):
        raise RuntimeError("render crash")

    monkeypatch.setattr(mod, "render_fixed_report", crash_render)
    with pytest.raises(RuntimeError, match="render crash"):
        mod.run_tick_notification_flow(_request(tmp_path, run_id="bound", fixed=False).request)
    bound = _recovery(tmp_path)
    assert not bound.get("rendered_message")
    candidate = _prepare(
        tmp_path,
        run_id="candidate-before-rebuild",
        delivery_kind="candidate_alert",
        source_kind="successful_brief",
        revision=bound["revision"],
        source_digest=bound["source_digest"],
        candidate_identities=bound["candidate_identities"],
        rendered_message="# 原候选消息\n保留这份精确正文。",
        prepared_at_utc=clock["now"],
    )["envelope"]
    if ambiguous:
        repo.record_daily_decision_brief_delivery_attempt(
            **identity(tmp_path, candidate),
            ambiguous=True,
            attempted_at_utc=clock["now"],
        )
    frozen = _day(tmp_path)["candidate_delivery"]
    retry = repo.read_retryable_daily_decision_brief_delivery(base=tmp_path, **SCOPE, now_utc=clock["now"])
    assert retry["envelope"] == frozen
    assert retry["reason"] == ("ambiguous" if ambiguous else "pending_candidates")

    monkeypatch.setattr(mod, "render_fixed_report", renderer)

    def forbid_scan(**kwargs):
        raise AssertionError("delivery-only must not scan")

    monkeypatch.setattr(mod, "assemble_daily_decision_briefs", forbid_scan)
    clock["now"] = "2026-07-21T14:40:30+00:00"
    request = _request(tmp_path, run_id="retry-priority", delivery_only=True)
    assert mod.run_tick_notification_flow(request.request) == 0
    assert not request.commits
    assert len(calls) == 2
    if ambiguous:
        assert calls[-1]["idempotency_key"] == build_notification_transport_key(frozen["delivery_key"])
        assert calls[-1]["message"] == frozen["rendered_message"]
        assert _day(tmp_path)["candidate_delivery"]["status"] == "confirmed"
        assert _recovery(tmp_path) == bound
    else:
        supplement = _recovery(tmp_path)
        assert supplement["status"] == "confirmed"
        assert calls[-1]["idempotency_key"] == build_notification_transport_key(supplement["delivery_key"])
        assert calls[-1]["message"] == supplement["rendered_message"]
        assert "恢复补报" in calls[-1]["message"]
        assert _day(tmp_path)["candidate_delivery"] == frozen
