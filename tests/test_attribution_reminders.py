from copy import deepcopy
from dataclasses import replace

import pytest

from src.application.daily_decision_brief_renderer import (
    build_attribution_reminder_context, render_fixed_report,
    render_fixed_report_card_markdown, render_query_brief,
)
from src.application.daily_decision_brief_repository import read_confirmed_attribution_render_context
from test_daily_decision_brief_repository_v2 import _brief


@pytest.fixture(autouse=True)
def _synthetic_wechat_bindings(tmp_path):
    import json

    from src.application.channels.wechat_clawbot.state import resolve_wechat_clawbot_state_dir

    for label in ("default", "other-sender"):
        state_dir = resolve_wechat_clawbot_state_dir(base=tmp_path, label=label)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "bindings.json").write_text(json.dumps({"bindings": {
            name: {"to_user_id": f"fixture-recipient-{name}", "context_token": "fixture-context"}
            for name in ("ops", "other-recipient", "retry-recipient")
        }}), encoding="utf-8")


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


@pytest.mark.parametrize("outcome", ["confirmed", "failed", "ambiguous", "no_send", "confirm_failure"])
def test_real_tick_only_confirmed_advances_reminder_baseline(tmp_path, monkeypatch, outcome):
    import src.application.tick_notification_flow as flow
    import test_daily_decision_brief_notification_flow as fixture

    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        return {market: {**fixture._brief(base=base, run_id=run_id, account=account,
                                       market=market, candidate=False),
                         "attribution_pending": [_pending()]} for market in markets_to_run}
    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    original_confirm = flow.confirm_daily_decision_brief_delivery_v2
    if outcome == "confirm_failure":
        def fail_confirm(**kwargs):
            raise OSError("synthetic receipt persistence failure")
        monkeypatch.setattr(flow, "confirm_daily_decision_brief_delivery_v2", fail_confirm)
    result = None if outcome in {"confirmed", "confirm_failure"} else {
        "ok": False, "command_ok": False, "delivery_confirmed": False,
        "returncode": 1, "error_code": "TIMEOUT" if outcome == "ambiguous" else "SEND_FAILED",
        "ambiguous_send": outcome == "ambiguous",
    }
    fixture._patch_sender(monkeypatch, result=result)
    first = fixture._request(tmp_path, run_id="first", no_send=outcome == "no_send")
    assert flow.run_tick_notification_flow(first.request) == (1 if outcome in {"failed", "ambiguous", "confirm_failure"} else 0)
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
    elif outcome in {"failed", "ambiguous", "confirm_failure"}:
        from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery
        pending = read_retryable_daily_decision_brief_delivery(
            base=tmp_path, account="lx", market="US", market_trading_date=fixture.MARKET_DATE)["envelope"]
        assert pending["status"] == ("ambiguous" if outcome in {"ambiguous", "confirm_failure"} else "pending")
        monkeypatch.setattr(flow, "confirm_daily_decision_brief_delivery_v2", original_confirm)
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


def test_confirmed_baseline_crosses_dates_but_not_accounts_with_real_repository(tmp_path):
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_success, prepare_daily_decision_brief_delivery,
        confirm_daily_decision_brief_delivery_v2,
    )
    from src.application.notification_delivery_adapter import build_notification_transport_key

    brief = _reminder_brief([_pending()])
    saved = persist_daily_decision_brief_success(base=tmp_path, brief=brief)
    from src.application.notification_delivery_route import notification_reminder_route_key
    import test_daily_decision_brief_notification_flow as fixture

    route_key = notification_reminder_route_key(config=fixture._config(), base=tmp_path)
    assert route_key
    context = {**build_attribution_reminder_context(saved["brief"]), "attribution_route_key": route_key}
    envelope = prepare_daily_decision_brief_delivery(
        base=tmp_path, account="lx", market="US", market_trading_date="2026-07-21",
        run_id=brief["run_id"], delivery_kind="fixed_report", source_kind="successful_brief",
        revision=saved["current_revision"], source_digest=saved["current_brief_digest"],
        scheduled_target_market="2026-07-21T10:00:00-04:00",
        rendered_message=render_fixed_report(saved["brief"], context=context), render_context=context,
    )["envelope"]
    confirm_daily_decision_brief_delivery_v2(
        base=tmp_path, account="lx", market="US", market_trading_date="2026-07-21",
        delivery_key=envelope["delivery_key"], source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
        confirmed_at_utc="2026-07-21T14:01:00Z", attribution_route_key=route_key,
    )
    next_day = {**brief, "market_trading_date": "2026-07-22"}
    previous = read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")
    # A fresh process reads only the confirmed repository receipt, not this test's memory.
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    restarted = subprocess.run([
        sys.executable, "-c",
        "import json,sys; from pathlib import Path; "
        "from src.application.daily_decision_brief_repository import read_confirmed_attribution_render_context; "
        "print(json.dumps(read_confirmed_attribution_render_context(base=Path(sys.argv[1]),account='lx',market='US')))",
        str(tmp_path),
    ], cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=True)
    assert json.loads(restarted.stdout) == previous
    assert "1 笔未变化" in render_fixed_report(next_day, context=build_attribution_reminder_context(
        next_day, previous_context=previous))
    sy = {**next_day, "account": "sy"}
    assert "TCOM0 2026" in render_fixed_report(
        sy, context=build_attribution_reminder_context(sy, previous_context=previous))


def test_candidate_alert_uses_same_attribution_summary(tmp_path, monkeypatch):
    import src.application.tick_notification_flow as flow
    import test_daily_decision_brief_notification_flow as fixture
    from src.application.daily_decision_brief_renderer import (
        render_candidate_alert, render_candidate_alert_card_markdown,
    )
    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        return {market: {**fixture._brief(base=base, run_id=run_id, account=account, market=market),
                         "attribution_pending": [_pending()]} for market in markets_to_run}
    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    request = fixture._request(tmp_path, run_id="candidate", fixed=False).request
    prepared = flow._prepare_daily_brief_notification(request)
    envelope = prepared.lifecycles_by_account["lx"]["envelope"]
    assert envelope["delivery_kind"] == "candidate_alert"
    assert "TCOM0 2026" in envelope["rendered_message"]
    saved = flow.read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"]
    repeated = build_attribution_reminder_context(saved, previous_context=envelope["render_context"])
    for render in (render_candidate_alert, render_candidate_alert_card_markdown):
        message = render(saved, envelope["candidate_identities"], context=repeated)
        assert "1 笔未变化" in message and "TCOM0 2026" not in message


def test_normal_prepare_reads_late_confirmation_fresh_after_scan_writes(tmp_path, monkeypatch):
    import src.application.tick_notification_flow as flow
    import test_daily_decision_brief_notification_flow as fixture
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery_v2
    from src.application.notification_delivery_adapter import build_notification_transport_key

    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        return {market: {**fixture._brief(base=base, run_id=run_id, account=account,
                                       market=market, candidate=False),
                         "attribution_pending": [_pending()]} for market in markets_to_run}

    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    seed = flow._prepare_daily_brief_notification(
        fixture._request(tmp_path, run_id="seed").request).lifecycles_by_account["lx"]["envelope"]
    assert seed["status"] == "pending"
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}

    def assemble_with_late_receipt(**kwargs):
        confirm_daily_decision_brief_delivery_v2(
            base=tmp_path, account="lx", market="US", market_trading_date=fixture.MARKET_DATE,
            delivery_key=seed["delivery_key"], source_digest=seed["source_digest"],
            message_sha256=seed["message_sha256"],
            transport_idempotency_key=build_notification_transport_key(seed["delivery_key"]),
            confirmed_at_utc="2026-07-21T14:59:30Z",
            attribution_route_key=seed["render_context"]["attribution_route_key"])
        return assemble(**kwargs)

    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble_with_late_receipt)
    request = fixture._request(tmp_path, run_id="next").request
    scheduler = deepcopy(request.scheduler_decisions_by_account)
    scheduler["lx"]["scheduled_target_market"] = "2026-07-21T11:00:00-04:00"
    scheduler["lx"]["scheduled_scan_target_market"] = "2026-07-21T11:00:00-04:00"
    request = replace(request, scheduler_decisions_by_account=scheduler)
    envelope = flow._prepare_daily_brief_notification(request).lifecycles_by_account["lx"]["envelope"]
    assert envelope["render_context"]["attribution_reminders"]["detail_rows"] == []
    assert "1 笔未变化" in envelope["rendered_message"]
    assert "TCOM0 2026" not in envelope["rendered_message"]


def _patch_pending_tick(monkeypatch):
    import src.application.tick_notification_flow as flow
    import test_daily_decision_brief_notification_flow as fixture

    def assemble(*, base, run_id, account, markets_to_run, **kwargs):
        return {market: {**fixture._brief(base=base, run_id=run_id, account=account,
                                       market=market, candidate=False),
                         "attribution_pending": [_pending()]} for market in markets_to_run}

    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    return flow, fixture


def _next_fixed_request(fixture, tmp_path, *, run_id, config, hour=11):
    request = fixture._request(tmp_path, run_id=run_id, config=config).request
    scheduler = deepcopy(request.scheduler_decisions_by_account)
    target = f"2026-07-21T{hour:02d}:00:00-04:00"
    scheduler["lx"]["scheduled_target_market"] = target
    scheduler["lx"]["scheduled_scan_target_market"] = target
    return replace(request, scheduler_decisions_by_account=scheduler)


def _synthetic_feishu_identity(monkeypatch, *, sender="fixture-app-a", recipient="fixture-user-a"):
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", sender)
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", recipient)
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", recipient)


@pytest.mark.parametrize("change", ["target", "wechat_sender_label", "provider", "feishu_sender", "feishu_recipient"])
def test_effective_route_change_reexpands_confirmed_pending_details(tmp_path, monkeypatch, change):
    flow, fixture = _patch_pending_tick(monkeypatch)
    _synthetic_feishu_identity(monkeypatch)
    config = fixture._feishu_config() if change.startswith("feishu_") else fixture._config()
    calls = []
    fixture._patch_sender(monkeypatch, calls=calls)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="route-a", config=config).request) == 0
    assert "TCOM0 2026" in calls[0]["message"]
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")
    changed = deepcopy(config)
    if change == "target":
        changed["notifications"]["target"] = "wechat:other-recipient"
    elif change == "wechat_sender_label":
        changed["notifications"]["wechat_clawbot_label"] = "other-sender"
    elif change == "provider":
        changed = fixture._feishu_config()
    elif change == "feishu_sender":
        _synthetic_feishu_identity(monkeypatch, sender="fixture-app-b")
    else:
        _synthetic_feishu_identity(monkeypatch, recipient="fixture-user-b")
    assert flow.run_tick_notification_flow(_next_fixed_request(
        fixture, tmp_path, run_id="route-b", config=changed)) == 0
    assert len(calls) == 2
    assert "TCOM0 2026" in calls[1]["message"]
    assert "1 笔未变化" not in calls[1]["message"]
    # Only a confirmed delivery at B may suppress its next original scheduled detail.
    prepared = flow._prepare_daily_brief_notification(_next_fixed_request(
        fixture, tmp_path, run_id="route-b-next", config=changed, hour=12))
    assert "1 笔未变化" in prepared.lifecycles_by_account["lx"]["envelope"]["rendered_message"]
    # Use the latest confirmed owner; returning to A conservatively expands instead
    # of searching arbitrary historical receipts for a matching route.
    _synthetic_feishu_identity(monkeypatch)
    back = flow._rebuild_daily_brief_delivery(
        request=_next_fixed_request(fixture, tmp_path, run_id="back-a", config=config, hour=13),
        account="lx", market="US", market_date=fixture.MARKET_DATE,
        scheduler={"scheduled_target_market": "2026-07-21T13:00:00-04:00"}, daily_limits={},
    )["envelope"]
    assert "TCOM0 2026" in back["rendered_message"]


@pytest.mark.parametrize("change", ["unrelated_config", "feishu_authored_target", "feishu_secret_rotation"])
def test_same_effective_route_config_change_keeps_confirmed_baseline(tmp_path, monkeypatch, change):
    flow, fixture = _patch_pending_tick(monkeypatch)
    _synthetic_feishu_identity(monkeypatch)
    config = fixture._feishu_config()
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="same-route", config=config).request) == 0
    changed = deepcopy(config)
    if change == "unrelated_config":
        changed["scan"] = {"fixture_setting": "changed"}
        changed["notifications"]["daily_brief"] = {"max_sell_put_rows": 1}
    elif change == "feishu_authored_target":
        # Feishu's effective recipient is canonical bot user_open_id, not this authoring hint.
        changed["notifications"]["target"] = "feishu:unused-hint-b"
    else:
        monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-rotated-secret")
    envelope = flow._prepare_daily_brief_notification(_next_fixed_request(
        fixture, tmp_path, run_id="same-route-next", config=changed)).lifecycles_by_account["lx"]["envelope"]
    assert "1 笔未变化" in envelope["rendered_message"]
    assert "TCOM0 2026" not in envelope["rendered_message"]


@pytest.mark.parametrize("outcome", ["failed", "ambiguous"])
def test_new_route_unconfirmed_delivery_does_not_advance_seen_baseline(tmp_path, monkeypatch, outcome):
    flow, fixture = _patch_pending_tick(monkeypatch)
    config = fixture._config()
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="confirmed-a", config=config).request) == 0
    baseline = read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")
    changed = deepcopy(config)
    changed["notifications"]["target"] = "wechat:other-recipient"
    calls = []
    fixture._patch_sender(monkeypatch, calls=calls, result={
        "ok": False, "command_ok": False, "delivery_confirmed": False, "returncode": 1,
        "error_code": "SEND_UNCONFIRMED" if outcome == "ambiguous" else "SEND_FAILED",
        "ambiguous_send": outcome == "ambiguous",
    })
    assert flow.run_tick_notification_flow(_next_fixed_request(
        fixture, tmp_path, run_id="unconfirmed-b", config=changed)) == 1
    assert "TCOM0 2026" in calls[0]["message"]
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == baseline
    # A read-only render/rebuild still has no proof B saw these details.
    rebuilt = flow._rebuild_daily_brief_delivery(
        request=_next_fixed_request(fixture, tmp_path, run_id="next-b", config=changed, hour=12),
        account="lx", market="US", market_date=fixture.MARKET_DATE,
        scheduler={"scheduled_target_market": "2026-07-21T12:00:00-04:00"}, daily_limits={},
    )["envelope"]
    assert "TCOM0 2026" in rebuilt["rendered_message"]
    assert "1 笔未变化" not in rebuilt["rendered_message"]


@pytest.mark.parametrize("outcome", ["failed", "ambiguous"])
def test_route_change_exact_retry_keeps_frozen_payload_and_current_sender_contract(tmp_path, monkeypatch, outcome):
    from src.application.daily_decision_brief_repository import read_retryable_daily_decision_brief_delivery
    from src.application.notification_delivery_adapter import build_notification_transport_key

    flow, fixture = _patch_pending_tick(monkeypatch)
    config = fixture._config()
    fixture._patch_sender(monkeypatch, result={
        "ok": False, "command_ok": False, "delivery_confirmed": False, "returncode": 1,
        "error_code": "SEND_UNCONFIRMED" if outcome == "ambiguous" else "SEND_FAILED",
        "ambiguous_send": outcome == "ambiguous",
    })
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="pending-a", config=config).request) == 1
    pending = read_retryable_daily_decision_brief_delivery(
        base=tmp_path, account="lx", market="US", market_trading_date=fixture.MARKET_DATE)["envelope"]
    changed = deepcopy(config)
    changed["notifications"]["target"] = "wechat:retry-recipient"
    calls = []
    fixture._patch_sender(monkeypatch, calls=calls)
    assert flow.run_tick_notification_flow(fixture._request(
        tmp_path, run_id="retry-b", delivery_only=True, config=changed).request) == 0
    assert len(calls) == 1
    assert calls[0]["target"] == changed["notifications"]["target"]
    assert calls[0]["notifications"] == changed["notifications"]
    assert calls[0]["message"] == pending["rendered_message"]
    assert calls[0]["idempotency_key"] == build_notification_transport_key(pending["delivery_key"])
    # The exact envelope is not silently re-rendered against the new config.
    confirmed = flow.read_daily_decision_brief_delivery_state(
        base=tmp_path, account="lx", market="US")["state"]["days"][fixture.MARKET_DATE]["fixed_reports"][fixture.FIXED_TARGET]
    for field in ("delivery_key", "source_digest", "message_sha256", "rendered_message", "render_context"):
        assert confirmed[field] == pending[field]
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}
    # A never received the pending attempt; B received an A-frozen context. Neither
    # route can use this receipt to suppress the next newly rendered detail.
    for hour, next_config in enumerate((config, changed), start=11):
        rebuilt = flow._rebuild_daily_brief_delivery(
            request=_next_fixed_request(fixture, tmp_path, run_id="after-cross-route-retry", config=next_config, hour=hour),
            account="lx", market="US", market_date=fixture.MARKET_DATE,
            scheduler={"scheduled_target_market": f"2026-07-21T{hour}:00:00-04:00"}, daily_limits={},
        )["envelope"]
        assert "TCOM0 2026" in rebuilt["rendered_message"]
        assert "1 笔未变化" not in rebuilt["rendered_message"]


@pytest.mark.parametrize("change", ["binding_recipient", "state_directory"])
def test_wechat_resolved_binding_change_reexpands_details(tmp_path, monkeypatch, change):
    import json

    from src.application.channels.wechat_clawbot.state import resolve_wechat_clawbot_state_dir

    flow, fixture = _patch_pending_tick(monkeypatch)
    config = fixture._config()
    state_dir = resolve_wechat_clawbot_state_dir(base=tmp_path, label="default", notifications=config["notifications"])
    state_dir.mkdir(parents=True, exist_ok=True)
    binding = {"bindings": {"ops": {"to_user_id": "fixture-recipient-a", "context_token": "fixture-context-a"}}}
    (state_dir / "bindings.json").write_text(json.dumps(binding), encoding="utf-8")
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="binding-a", config=config).request) == 0
    changed = deepcopy(config)
    if change == "binding_recipient":
        binding["bindings"]["ops"]["to_user_id"] = "fixture-recipient-b"
        (state_dir / "bindings.json").write_text(json.dumps(binding), encoding="utf-8")
    else:
        changed_dir = tmp_path / "other-sender-state"
        changed_dir.mkdir()
        (changed_dir / "bindings.json").write_text(json.dumps(binding), encoding="utf-8")
        changed["notifications"]["wechat_clawbot_state_dir"] = str(changed_dir)
    prepared = flow._prepare_daily_brief_notification(_next_fixed_request(
        fixture, tmp_path, run_id="binding-b", config=changed))
    message = prepared.lifecycles_by_account["lx"]["envelope"]["rendered_message"]
    assert "TCOM0 2026" in message
    assert "1 笔未变化" not in message


@pytest.mark.parametrize("unknown", ["missing_wechat_binding", "missing_feishu_sender"])
def test_unavailable_effective_route_never_suppresses_pending_detail(tmp_path, monkeypatch, unknown):
    flow, fixture = _patch_pending_tick(monkeypatch)
    _synthetic_feishu_identity(monkeypatch)
    config = fixture._config() if unknown == "missing_wechat_binding" else fixture._feishu_config()
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="known", config=config).request) == 0
    changed = deepcopy(config)
    if unknown == "missing_wechat_binding":
        changed["notifications"]["target"] = "wechat:binding-does-not-exist"
    else:
        monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "")
    envelope = flow._prepare_daily_brief_notification(_next_fixed_request(
        fixture, tmp_path, run_id="unknown-route", config=changed)).lifecycles_by_account["lx"]["envelope"]
    assert "TCOM0 2026" in envelope["rendered_message"]
    assert "1 笔未变化" not in envelope["rendered_message"]


def test_reminder_route_identity_does_not_resolve_sender_secret_during_prepare(tmp_path, monkeypatch):
    import src.application.secret_resolver as secrets

    flow, fixture = _patch_pending_tick(monkeypatch)
    _synthetic_feishu_identity(monkeypatch)
    config = fixture._feishu_config()
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="secret-seed", config=config).request) == 0

    def forbidden_secret_resolution(*args, **kwargs):
        raise AssertionError("reminder identity must only read non-secret sender metadata")

    monkeypatch.setattr(secrets, "_secret", forbidden_secret_resolution)
    envelope = flow._prepare_daily_brief_notification(_next_fixed_request(
        fixture, tmp_path, run_id="metadata-only", config=config)).lifecycles_by_account["lx"]["envelope"]
    assert "1 笔未变化" in envelope["rendered_message"]


def test_repeat_confirmation_cannot_replace_actual_reminder_route(tmp_path, monkeypatch):
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery_v2
    from src.application.notification_delivery_adapter import build_notification_transport_key

    flow, fixture = _patch_pending_tick(monkeypatch)
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="confirmed-route").request) == 0
    envelope = flow.read_daily_decision_brief_delivery_state(
        base=tmp_path, account="lx", market="US")["state"]["days"][fixture.MARKET_DATE]["fixed_reports"][fixture.FIXED_TARGET]
    original_route = envelope["confirmed_attribution_route_key"]
    assert original_route == envelope["render_context"]["attribution_route_key"]
    result = confirm_daily_decision_brief_delivery_v2(
        base=tmp_path, account="lx", market="US", market_trading_date=fixture.MARKET_DATE,
        delivery_key=envelope["delivery_key"], source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
        attribution_route_key="b" * 64, confirmed_at_utc="2026-07-21T15:00:00Z",
    )
    assert result["reason"] == "already_confirmed"
    assert result["envelope"] == envelope
    assert read_confirmed_attribution_render_context(
        base=tmp_path, account="lx", market="US") == envelope["render_context"]


@pytest.mark.parametrize("actual_route", [None, "b" * 64])
def test_unknown_or_different_actual_route_receipt_never_becomes_seen_baseline(tmp_path, monkeypatch, actual_route):
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery_v2
    from src.application.notification_delivery_adapter import build_notification_transport_key

    flow, fixture = _patch_pending_tick(monkeypatch)
    envelope = flow._prepare_daily_brief_notification(
        fixture._request(tmp_path, run_id="unproven-route").request).lifecycles_by_account["lx"]["envelope"]
    assert envelope["render_context"]["attribution_route_key"]
    confirm_daily_decision_brief_delivery_v2(
        base=tmp_path, account="lx", market="US", market_trading_date=fixture.MARKET_DATE,
        delivery_key=envelope["delivery_key"], source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
        attribution_route_key=actual_route, confirmed_at_utc="2026-07-21T14:01:00Z",
    )
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}
    next_envelope = flow._prepare_daily_brief_notification(_next_fixed_request(
        fixture, tmp_path, run_id="no-route-proof", config=fixture._config())).lifecycles_by_account["lx"]["envelope"]
    assert "TCOM0 2026" in next_envelope["rendered_message"]
    assert "1 笔未变化" not in next_envelope["rendered_message"]


@pytest.mark.parametrize("malformed", ["invalid", "g" * 64, {"route": "fixture"}])
def test_malformed_actual_route_receipt_is_rejected_without_rewriting_state(tmp_path, monkeypatch, malformed):
    import json

    flow, fixture = _patch_pending_tick(monkeypatch)
    fixture._patch_sender(monkeypatch)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="valid-receipt").request) == 0
    result = flow.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    path = result["path"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["days"][fixture.MARKET_DATE]["fixed_reports"][fixture.FIXED_TARGET]["confirmed_attribution_route_key"] = malformed
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    invalid = flow.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert invalid["available"] is False
    assert invalid["reason"] == "state_invalid"
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize("state_layout", ["default", "relative"])
@pytest.mark.parametrize("runtime_decoy", [False, True])
def test_dual_root_reminders_bind_actual_sender_root_and_keep_receipts_in_runtime(
    tmp_path, monkeypatch, state_layout, runtime_decoy,
):
    import json

    from src.application.channels.wechat_clawbot.state import resolve_wechat_clawbot_state_dir
    from src.application.notification_delivery_route import notification_reminder_route_key

    flow, fixture = _patch_pending_tick(monkeypatch)
    runtime_base = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    config = fixture._config()
    if state_layout == "relative":
        config["notifications"]["wechat_clawbot_state_dir"] = "sender-state"

    def write_binding(base, recipient):
        path = resolve_wechat_clawbot_state_dir(
            base=base, label="default", notifications=config["notifications"],
        ) / "bindings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"bindings": {"ops": {
            "to_user_id": recipient, "context_token": "fixture-dual-root-context",
        }}}), encoding="utf-8")
        return path

    sender_binding_path = write_binding(repo_root, "fixture-actual-recipient-a")
    if runtime_decoy:
        write_binding(runtime_base, "fixture-wrong-runtime-recipient")
    sender_key = notification_reminder_route_key(config=config, base=repo_root)
    runtime_key = notification_reminder_route_key(config=config, base=runtime_base)
    assert sender_key and sender_key != runtime_key
    assert bool(runtime_key) == runtime_decoy
    calls = []
    fixture._patch_sender(monkeypatch, calls=calls)
    first = replace(fixture._request(runtime_base, run_id="dual-root-a", config=config).request,
                    repo_root=repo_root)
    assert flow.run_tick_notification_flow(first) == 0
    assert calls[0]["base"] == repo_root.resolve()
    baseline = read_confirmed_attribution_render_context(base=runtime_base, account="lx", market="US")
    assert baseline["attribution_route_key"] == sender_key
    assert read_confirmed_attribution_render_context(base=repo_root, account="lx", market="US") == {}
    assert flow.read_latest_daily_decision_brief(base=repo_root, account="lx", market="US")["available"] is False

    unchanged = replace(_next_fixed_request(
        fixture, runtime_base, run_id="dual-root-unchanged", config=config), repo_root=repo_root)
    assert flow.run_tick_notification_flow(unchanged) == 0
    assert "1 笔未变化" in calls[1]["message"]
    assert "TCOM0 2026" not in calls[1]["message"]
    assert calls[1]["base"] == repo_root.resolve()

    write_binding(repo_root, "fixture-actual-recipient-b")
    rebound = replace(_next_fixed_request(
        fixture, runtime_base, run_id="dual-root-rebound", config=config, hour=12), repo_root=repo_root)
    assert flow.run_tick_notification_flow(rebound) == 0
    assert "TCOM0 2026" in calls[2]["message"]
    assert "1 笔未变化" not in calls[2]["message"]
    rebound_baseline = read_confirmed_attribution_render_context(base=runtime_base, account="lx", market="US")
    assert rebound_baseline["attribution_route_key"] == notification_reminder_route_key(config=config, base=repo_root)
    assert rebound_baseline["attribution_route_key"] != sender_key

    # An unavailable actual sender binding cannot fall back to a bound runtime alias.
    sender_binding_path.write_text(json.dumps({"bindings": {}}), encoding="utf-8")
    missing = replace(_next_fixed_request(
        fixture, runtime_base, run_id="dual-root-missing", config=config, hour=13), repo_root=repo_root)
    envelope = flow._prepare_daily_brief_notification(missing).lifecycles_by_account["lx"]["envelope"]
    assert envelope["render_context"]["attribution_route_key"] is None
    assert "TCOM0 2026" in envelope["rendered_message"]
