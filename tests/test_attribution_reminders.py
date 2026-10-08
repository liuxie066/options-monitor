from copy import deepcopy
from dataclasses import replace

import pytest

from src.application.daily_decision_brief_renderer import (
    build_attribution_reminder_context, render_fixed_report,
    render_fixed_report_card_markdown, render_query_brief,
)
from src.application.daily_decision_brief_repository import read_confirmed_attribution_render_context
from test_daily_decision_brief_repository_v2 import _brief


def _pending(index=0, **overrides):
    return {"execution_key": f"execution:{index}", "symbol": f"TCOM{index}",
            "expiration": "2026-10-16", "strike": 35, "option_type": "put",
            "status": "pending", "reason_codes": ["attribution_evidence_incomplete"],
            "rules_enabled": False, "selected_candidate_id": None, **overrides}


def _reminder_brief(rows):
    return {**_brief(run_id="reminder"), "attribution_pending": rows}


def test_seen_only_after_display_and_changed_or_reappeared_rows_expand():
    brief = _reminder_brief([_pending(i) for i in range(7)])
    first = build_attribution_reminder_context(brief)
    assert len(first["attribution_reminders"]["seen_rows"]) == 5
    assert "另有 2 笔" in render_fixed_report(brief, context=first)
    second = build_attribution_reminder_context(brief, previous_context=first)
    for render in (render_fixed_report, render_fixed_report_card_markdown):
        text = render(brief, context=second)
        assert "5 笔未变化" in text and "TCOM0 2026" not in text
        assert "TCOM5 2026" in text and "TCOM6 2026" in text
    third = build_attribution_reminder_context(brief, previous_context=second)
    assert "7 笔未变化" in render_fixed_report(brief, context=third)
    assert "TCOM0 2026" in render_query_brief(brief, context=third)
    changed = deepcopy(brief)
    changed["attribution_pending"][0]["status"] = "conflict"
    fourth = build_attribution_reminder_context(changed, previous_context=third)
    assert "TCOM0 2026" in render_fixed_report(changed, context=fourth)
    removed = {**brief, "attribution_pending": brief["attribution_pending"][1:]}
    removal = build_attribution_reminder_context(removed, previous_context=third)
    assert "TCOM0 2026" in render_fixed_report(
        brief, context=build_attribution_reminder_context(brief, previous_context=removal))


def test_reason_order_stable_and_unknown_does_not_mark_resolved():
    row = _pending(reason_codes=["attribution_evidence_incomplete", "other_reason"])
    brief = _reminder_brief([row])
    previous = build_attribution_reminder_context(brief)
    row["reason_codes"].reverse()
    current = build_attribution_reminder_context(brief, previous_context=previous)
    assert current["attribution_reminders"]["detail_rows"] == []
    unknown = {**brief, "attribution_pending": [], "attribution_read_error": "ReadError"}
    error = build_attribution_reminder_context(unknown, previous_context=current)
    assert "待办情况未知" in render_fixed_report(unknown, context=error)
    restored = build_attribution_reminder_context(brief, previous_context=error)
    assert restored["attribution_reminders"]["detail_rows"] == []


def test_malformed_or_wrong_scope_context_does_not_suppress_and_missing_key_expands():
    brief = _reminder_brief([_pending()])
    previous = build_attribution_reminder_context(brief)
    for override in ({"account": "sy"}, {"market": "HK"}, {"schema_version": "future"},
                     {"seen_rows": [{}]}, {"detail_rows": "invalid"},
                     {"seen_rows": [{**previous["attribution_reminders"]["seen_rows"][0],
                                     "reason_codes": [None]}]},
                     {"seen_rows": [{**previous["attribution_reminders"]["seen_rows"][0],
                                     "execution_key": {}}]}):
        bad = deepcopy(previous)
        bad["attribution_reminders"].update(override)
        assert "TCOM0 2026" in render_fixed_report(
            brief, context=build_attribution_reminder_context(brief, previous_context=bad))
    missing = _reminder_brief([_pending(execution_key=None)])
    first = build_attribution_reminder_context(missing)
    second = build_attribution_reminder_context(missing, previous_context=first)
    assert "TCOM0 2026" in render_fixed_report(missing, context=second)


def test_reason_groups_are_truthful_and_card_matches_text():
    brief = _reminder_brief([
        _pending(0), _pending(1, reason_codes=["multiple_strategy_candidates"]),
        _pending(2, reason_codes=["awaiting_ledger_commit"], rules_enabled=True,
                 selected_candidate_id="wheel:x"), _pending(3, status="conflict"),
    ])
    for render in (render_fixed_report, render_fixed_report_card_markdown):
        text = render(brief, context=build_attribution_reminder_context(brief))
        for label in ("归属证据核对", "归属待确认", "归属处理中", "归属冲突"):
            assert f"{label}｜1 笔" in text
        assert "历史" not in text and "execution:" not in text


@pytest.mark.parametrize("outcome", ["confirmed", "failed", "ambiguous", "no_send"])
def test_real_tick_only_confirmed_advances_reminder_baseline(tmp_path, monkeypatch, outcome):
    import src.application.tick_notification_flow as flow
    import test_daily_decision_brief_notification_flow as fixture

    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        return {market: {**fixture._brief(base=base, run_id=run_id, account=account,
                                       market=market, candidate=False),
                         "attribution_pending": [_pending()]} for market in markets_to_run}
    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    result = None if outcome == "confirmed" else {
        "ok": False, "command_ok": False, "delivery_confirmed": False,
        "returncode": 1, "error_code": "TIMEOUT" if outcome == "ambiguous" else "SEND_FAILED",
        "delivery_ambiguous": outcome == "ambiguous",
    }
    fixture._patch_sender(monkeypatch, result=result)
    first = fixture._request(tmp_path, run_id="first", no_send=outcome == "no_send")
    assert flow.run_tick_notification_flow(first.request) == (1 if outcome in {"failed", "ambiguous"} else 0)
    baseline = read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")
    assert bool(baseline) == (outcome == "confirmed")
    assert read_confirmed_attribution_render_context(base=tmp_path, account="sy", market="US") == {}
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="HK") == {}
    if outcome == "confirmed":
        next_request = fixture._request(tmp_path, run_id="second").request
        scheduler = deepcopy(next_request.scheduler_decisions_by_account)
        scheduler["lx"]["scheduled_target_market"] = "2026-07-21T11:00:00-04:00"
        scheduler["lx"]["scheduled_scan_target_market"] = "2026-07-21T11:00:00-04:00"
        next_request = replace(next_request, scheduler_decisions_by_account=scheduler)
        prepared = flow._prepare_daily_brief_notification(next_request)
        envelope = prepared.lifecycles_by_account["lx"]["envelope"]
        assert "1 笔未变化" in envelope["rendered_message"]
        assert "TCOM0 2026" not in envelope["rendered_message"]
        rebuilt = flow._rebuild_daily_brief_delivery(
            request=next_request, account="lx", market="US", market_date=fixture.MARKET_DATE,
            scheduler={**scheduler["lx"], "scheduled_target_market": "2026-07-21T12:00:00-04:00"},
            daily_limits={},
        )["envelope"]
        assert "1 笔未变化" in rebuilt["rendered_message"]
        assert "TCOM0 2026" not in rebuilt["rendered_message"]
    elif outcome in {"failed", "ambiguous"}:
        from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery
        pending = read_retryable_daily_decision_brief_delivery(
            base=tmp_path, account="lx", market="US", market_trading_date=fixture.MARKET_DATE)["envelope"]
        fixture._patch_sender(monkeypatch)
        retry = fixture._request(tmp_path, run_id="retry", delivery_only=True)
        assert flow.run_tick_notification_flow(retry.request) == 0
        assert read_confirmed_attribution_render_context(
            base=tmp_path, account="lx", market="US") == pending["render_context"]


def test_latest_confirmed_success_not_latest_supported_context(monkeypatch, tmp_path):
    import src.application.daily_decision_brief_repository as repo
    first = {"status": "confirmed", "source_kind": "successful_brief",
             "confirmed_at_utc": "2026-07-21T14:00:00Z", "delivery_key": "first",
             "render_context": {"attribution_reminders": {"marker": "older"}}}
    latest = {**first, "confirmed_at_utc": "2026-07-22T14:00:00Z",
              "delivery_key": "latest", "render_context": {}}
    failure = {**first, "source_kind": "scan_failure", "delivery_key": "failure",
               "confirmed_at_utc": "2026-07-23T14:00:00Z"}
    monkeypatch.setattr(repo, "read_daily_decision_brief_delivery_state", lambda **_: {
        "state": {"days": {"2026-07-21": {"fixed_reports": {"first": first}},
                           "2026-07-22": {"candidate_delivery_history": [latest],
                                          "fixed_reports": {"failure": failure}}}}})
    assert repo.read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}
