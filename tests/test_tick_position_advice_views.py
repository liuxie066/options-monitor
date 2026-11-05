from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from domain.domain.daily_decision_brief import (
    daily_brief_position_roll, daily_brief_position_roll_identity,
    daily_brief_roll_notification_identities,
)
from src.application import daily_decision_brief_renderer as renderer
from src.application import tick_notification_flow as flow
from src.application.daily_decision_brief_repository import (
    read_daily_decision_brief_delivery_state, read_retryable_daily_decision_brief_delivery,
)
from test_daily_decision_brief_notification_flow import (
    MARKET_DATE, _brief, _config, _patch_sender, _request,
)
from test_daily_decision_brief_service import _account_dir, _assemble, _write_close_report


def position_row(*, lot: str = "old-lot", option_type: str = "call", credit: float = 95) -> dict:
    strike = 100
    target_strike = 105 if option_type == "call" else 95
    letter = "C" if option_type == "call" else "P"
    old_contract = f"NVDA260821{letter}00100000"
    target_contract = f"NVDA260821{letter}{target_strike * 1000:08d}"
    target = {
        "contract": target_contract, "expiration": "2026-08-21", "strike": target_strike,
        "net_credit": credit, "net_debit": max(0, -credit),
        "incremental_period_return": credit / 10000,
        "incremental_annualized_return": credit / 10000 * 365 / 31,
        "new_capital_basis": 10000, "evidence": {"old_ask": 1.0, "new_bid": 2.0},
    }
    if option_type == "call":
        target.update(recommendation="credit_roll" if credit > 0 else "cash_neutral_roll" if credit == 0 else "debit_roll",
            cash_flow_type="credit" if credit > 0 else "neutral" if credit == 0 else "debit",
            strike_increase=5, strike_increase_pct=0.05, assignment_net_improvement=500 + credit,
            assignment_improvement_period_return=0.0595, assignment_improvement_annualized_return=0.7)
    roll = {
        "schema_version": "covered_call_roll.v1" if option_type == "call" else "short_put_roll.v1",
        "status": "success", "outcome": "found", "account": "lx",
        "position": {"lot_id": lot, "symbol": "NVDA", "option_type": option_type,
            "strike": strike, "expiration": "2026-08-21", "contract_symbol": old_contract,
            "contracts_open": 1, "multiplier": 100, "currency": "USD"},
        "stock_coverage": {"status": "covered"} if option_type == "call" else None,
        "scope": {"market": "US"}, "coverage": {"status": "complete"},
        "candidates": [target, {**target, "contract": "SECOND_TARGET_NEVER_DISPLAY"}],
        "reason_codes": [], "evaluated_at_utc": "2026-07-21T14:30:00Z",
        "pricing_basis": "old_ask_new_bid", "actionable_until_utc": "2026-07-21T14:01:00Z",
        "source": {"expires_at": "2026-07-21T14:01:00Z", "snapshot_id": "fixture-frozen"},
    }
    return {"position_lot_id": lot, "symbol": "NVDA", "option_type": option_type,
        "strike": strike, "expiration": "2026-08-21", "contract_symbol": old_contract,
        "recommendation_state": "hold", "evaluation_status": "not_evaluable",
        "quote_status": "unavailable", "reason": "original_premium_missing",
        "notification_eligible": False, "roll_notification_eligible": True,
        "strategy_family": "covered_call" if option_type == "call" else "sell_put",
        "metrics": {}, "roll_advice": roll}


def roll_brief(*, base: Path, run_id: str = "roll", rows: list[dict] | None = None, account: str = "lx") -> dict:
    brief = _brief(base=base, run_id=run_id, account=account, candidate=False)
    brief["positions"] = rows if rows is not None else [position_row()]
    return brief


def patch_roll_assembler(monkeypatch, *, rows=None):
    def assemble(**kw):
        brief = roll_brief(base=kw["base"], run_id=kw["run_id"], rows=rows, account=kw["account"])
        if kw.get("scheduler_decision", {}).get("in_run_window") is False:
            brief["actionability"] = "planning_only"
        return {market: brief for market in kw["markets_to_run"]}
    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    monkeypatch.setattr(flow, "utc_now", lambda: datetime(2026, 7, 21, 14, 0, 30, tzinfo=timezone.utc))


def test_verified_csv_roll_enters_positions_independent_of_close(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = position_row()
    row["roll_advice"]["actionable_until_utc"] = "2026-07-17T14:01:00Z"
    _write_close_report(account_dir, [{**row, "account": "lx", "roll_advice": json.dumps(row["roll_advice"])}])
    brief = _assemble(tmp_path)
    saved = brief["positions"][0]
    assert saved["roll_advice"] == row["roll_advice"]
    assert saved["notification_eligible"] is False
    assert daily_brief_position_roll(saved)[0] == "roll_up"
    assert daily_brief_roll_notification_identities(brief)

    # Changing CSV without re-publishing the manifest cannot introduce a target.
    csv = account_dir / "close_advice.csv"
    csv.write_text(csv.read_text().replace("SECOND_TARGET_NEVER_DISPLAY", "TAMPERED"))
    invalid = _assemble(tmp_path)
    assert invalid["positions"] == []
    assert not daily_brief_roll_notification_identities(invalid)


@pytest.mark.parametrize("roll_raw", ["{broken", "[]", "null"])
def test_invalid_verified_roll_does_not_destroy_close_row(tmp_path: Path, roll_raw: str) -> None:
    account_dir = _account_dir(tmp_path)
    row = position_row()
    _write_close_report(account_dir, [{**row, "account": "lx", "roll_advice": roll_raw}])
    brief = _assemble(tmp_path)
    assert len(brief["positions"]) == 1
    assert "roll_advice" not in brief["positions"][0]
    if roll_raw != "null":  # pandas treats a literal CSV null as a missing optional cell.
        assert any(gap["reason"] == "roll_advice_invalid" for gap in brief["data_gaps"])


@pytest.mark.parametrize("option_type,label,delta", [("call", "CC 向上移仓", "+5"), ("put", "CSP 向下移仓", "-5")])
def test_roll_concise_text_card_parity_and_first_target(tmp_path: Path, option_type, label, delta) -> None:
    row = position_row(option_type=option_type)
    brief = roll_brief(base=tmp_path, rows=[row])
    identity = next(iter(daily_brief_roll_notification_identities(brief)))
    outputs = [renderer.render_candidate_alert(brief, [identity]),
        renderer.render_candidate_alert_text(brief, [identity]),
        "\n".join(renderer.render_candidate_alert_card_blocks(brief, [identity])),
        renderer.render_fixed_report_text(brief), renderer.render_query_brief(brief)]
    for text in outputs:
        assert "持仓" in text and label in text
        assert row["contract_symbol"] in text
        assert row["roll_advice"]["candidates"][0]["contract"] in text
        assert "SECOND_TARGET_NEVER_DISPLAY" not in text
        assert "平仓 ask USD 1" in text and "开仓 bid USD 2" in text
        assert f"USD {delta}" in text
        assert "净收取" in text and "95" in text
        assert "增量期收益率" in text and "净现金流 ÷" in text
        assert "预计锁定收益" not in text


def test_debit_cc_is_not_revived_as_an_actionable_roll(tmp_path: Path) -> None:
    row = position_row(credit=-95)
    brief = roll_brief(base=tmp_path, rows=[row])
    assert daily_brief_position_roll(row) is None
    assert not daily_brief_roll_notification_identities(brief)
    for text in (renderer.render_fixed_report_text(brief), renderer.render_query_brief(brief)):
        assert "CC 向上移仓" not in text
        assert "净收取" not in text
        assert row["roll_advice"]["candidates"][0]["contract"] not in text


@pytest.mark.parametrize("option_type", ["call", "put"])
def test_zero_cash_rolls_suppressed_and_positive_same_expiration_eligible(tmp_path: Path, option_type: str) -> None:
    row = position_row(credit=0, option_type=option_type)
    assert daily_brief_position_roll(row) is None
    text = renderer.render_fixed_report_text(roll_brief(base=tmp_path, rows=[row]))
    assert "净收取：USD 0" not in text
    assert daily_brief_position_roll(position_row(option_type=option_type)) is not None


def test_plain_hold_and_missing_disabled_roll_stay_suppressed(tmp_path: Path) -> None:
    row = position_row()
    row["roll_advice"] = {"status": "disabled", "outcome": "unable", "candidates": []}
    brief = roll_brief(base=tmp_path, rows=[row])
    assert not daily_brief_roll_notification_identities(brief)
    assert "CC 向上移仓" not in renderer.render_fixed_report_text(brief)


def test_expired_roll_is_not_actionable_in_query_or_new_fixed_report(tmp_path: Path) -> None:
    brief = roll_brief(base=tmp_path)
    later = {"query_time_utc": "2026-07-21T14:02:00Z"}
    assert "CC 向上移仓" not in renderer.render_query_brief(brief, context=later)
    assert "CC 向上移仓" not in renderer.render_fixed_report_text(
        brief, context={"advice_time_utc": "2026-07-21T14:02:00Z"})


def test_identity_isolated_by_lot_account_market_direction(tmp_path: Path) -> None:
    row = position_row()
    identity = daily_brief_position_roll_identity(row, account="lx", market="US")
    assert identity == daily_brief_position_roll_identity(deepcopy(row), account="LX", market="us")
    variants = [daily_brief_position_roll_identity(position_row(lot="other"), account="lx", market="US"),
        daily_brief_position_roll_identity(row, account="other", market="US"),
        daily_brief_position_roll_identity(row, account="lx", market="HK"),
        daily_brief_position_roll_identity(position_row(option_type="put"), account="lx", market="US")]
    assert identity not in variants and len(set(variants)) == 4
    changed_target = deepcopy(row)
    changed_target["roll_advice"]["candidates"][0]["contract"] = "DIFFERENT_BEST"
    assert daily_brief_position_roll_identity(changed_target, account="lx", market="US") == identity


def test_roll_only_scheduled_alert_dedup_and_new_old_lot(monkeypatch, tmp_path: Path) -> None:
    rows = [position_row()]
    patch_roll_assembler(monkeypatch, rows=rows)
    calls = []
    _patch_sender(monkeypatch, calls=calls)
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="first", fixed=False).request) == 0
    assert len(calls) == 1 and "持仓" in calls[0]["message"] and "CC 向上移仓" in calls[0]["message"]
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="again", fixed=False).request) == 0
    assert len(calls) == 1
    rows.append(position_row(lot="other-lot"))
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="new-lot", fixed=False).request) == 0
    assert len(calls) == 2
    assert "other-lot" not in calls[1]["message"]
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert len(state["state"]["days"][MARKET_DATE]["alerted_candidates"]) == 2


def test_roll_retry_reuses_exact_payload_and_does_not_ack_failed_send(monkeypatch, tmp_path: Path) -> None:
    patch_roll_assembler(monkeypatch)
    calls = []
    _patch_sender(monkeypatch, calls=calls, result={"ok": False, "command_ok": False,
        "delivery_confirmed": False, "returncode": 1, "error_code": "SEND_FAILED"})
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="failed", fixed=False).request) == 1
    pending = read_retryable_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)["envelope"]
    assert pending["delivery_kind"] == "candidate_alert"
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert not state["state"]["days"][MARKET_DATE]["alerted_candidates"]
    retries = []
    _patch_sender(monkeypatch, calls=retries)
    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", lambda **kw: pytest.fail("retry must not scan"))
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="retry", delivery_only=True).request) == 0
    assert retries[0]["message"] == pending["rendered_message"]
    assert retries[0]["idempotency_key"] == calls[0]["idempotency_key"]


def test_display_budget_acknowledges_only_shown_roll_positions(monkeypatch, tmp_path: Path) -> None:
    rows = [position_row(lot=f"lot-{n}") for n in range(7)]
    patch_roll_assembler(monkeypatch, rows=rows)
    calls = []
    _patch_sender(monkeypatch, calls=calls)
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="many", fixed=False).request) == 0
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["state"]["days"][MARKET_DATE]
    assert len(state["alerted_candidates"]) == 5
    assert len(state["pending_candidates"]) == 2
    expected = sorted(daily_brief_roll_notification_identities(roll_brief(base=tmp_path, rows=rows)))[:5]
    assert set(state["alerted_candidates"]) == set(expected)
    assert calls[0]["message"].count("旧仓买回") == 5
    assert all(row["position_lot_id"] not in calls[0]["message"] for row in rows)


def test_truncated_message_does_not_ack_cut_off_roll(monkeypatch, tmp_path: Path) -> None:
    patch_roll_assembler(monkeypatch)
    monkeypatch.setattr(renderer, "_MAX_MESSAGE_CHARS", 200)
    prep = flow._prepare_daily_brief_notification(_request(tmp_path, run_id="truncated", fixed=False).request)
    assert not prep.lifecycles_by_account.get("lx", {}).get("envelope")
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    day = state["state"]["days"][MARKET_DATE]
    assert not day["alerted_candidates"]
    assert len(day["pending_candidates"]) == 1


def _many_candidate_roll_brief(*, base, run_id, account="lx") -> dict:
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity
    from test_daily_decision_brief_renderer import _candidate

    brief = roll_brief(base=base, run_id=run_id, account=account,
        rows=[position_row(lot=f"lot-{i}") for i in range(20)])
    index = []
    for family, option_type in (("sell_put", "put"), ("covered_call", "call")):
        candidates = []
        for i in range(20):
            symbol = f"ABC{i}"
            candidate = _candidate(rank=i + 1, symbol=symbol, option_type=option_type,
                expiration="2026-08-21", strike=100 + i, capacity=2)
            candidate["contract_symbol"] = f"{symbol}260821{option_type[0].upper()}{(100 + i) * 1000:08d}"
            candidate["strategy_family"] = family
            candidates.append(candidate)
            index.append({"identity": build_daily_brief_candidate_identity(account=account, market="US",
                symbol=symbol, strategy_family=family), "symbol": symbol, "strategy_family": family,
                "representative": candidate, "contract_count": 1})
        brief["candidates"][family] = candidates
    brief["candidate_index"] = index
    return brief


@pytest.mark.parametrize("explicit_routes", [False, True])
@pytest.mark.parametrize("fallback_used", [False, True])
def test_final_card_and_fallback_ack_only_the_same_visible_rolls(
    monkeypatch, tmp_path: Path, explicit_routes, fallback_used,
) -> None:
    from types import SimpleNamespace
    from src.application.channels import plugins
    from src.application.channels.feishu_notification_renderer import feishu_notification_visible_markdown
    from src.application.daily_decision_brief_repository import read_daily_brief_fanout_children
    from src.application.notification_delivery_route import notification_reminder_route_key
    from test_daily_decision_brief_notification_flow import _feishu_config

    limits = {"max_actions_per_priority": 20, "max_candidates_per_strategy": 20}
    def assemble(**kw):
        return {market: _many_candidate_roll_brief(base=kw["base"], run_id=kw["run_id"], account=kw["account"])
            for market in kw["markets_to_run"]}

    calls = []
    def send(**kw):
        calls.append(deepcopy(kw))
        return {"delivery_confirmed": True, "fallback_used": fallback_used,
            "attribution_route_key": notification_reminder_route_key(
                config={"notifications": kw["notifications"]}, base=tmp_path)}

    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    monkeypatch.setattr(flow, "utc_now", lambda: datetime(2026, 7, 21, 14, 0, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _provider: True)
    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=send, normalize_fn=lambda *, send_result: send_result, failure_stage="fixture"))
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_fixture")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    config = _feishu_config()
    config["notifications"]["daily_brief"] = limits
    if explicit_routes:
        config["notifications"]["routes"] = [{"id": "a", "provider": "feishu_app",
            "target": "ou_fixture", "accounts": ["lx"]}]
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="first", config=config).request) == 0
    brief = _many_candidate_roll_brief(base=tmp_path, run_id="first")
    all_rolls = daily_brief_roll_notification_identities(brief)
    if explicit_routes:
        child = read_daily_brief_fanout_children(base=tmp_path, account="lx", market="US",
            market_trading_date=MARKET_DATE)[0]
        assert child["delivery_confirmed"] is True
        envelope = child["spec"]["transport_envelope"]
        acknowledged = set(child["spec"]["metadata"]["candidate_identities"]) & all_rolls
    else:
        day = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["state"]["days"][MARKET_DATE]
        envelope = next(iter(day["fixed_reports"].values()))["rendered_transport"]
        acknowledged = set(day["alerted_candidates"]) & all_rolls
    primary, fallback = feishu_notification_visible_markdown(envelope)
    primary_rolls, fallback_rolls = [renderer.select_rendered_position_roll_identities(
        brief, delivery_kind="fixed_report", limits=limits, rendered_message=body)
        for body in (primary, fallback)]
    assert 0 < len(acknowledged) < len(all_rolls)
    assert acknowledged == primary_rolls == fallback_rolls

    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="followup", fixed=False, config=config).request) == 0
    if explicit_routes:
        children = read_daily_brief_fanout_children(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)
        assert len(children) == 2 and all(child["delivery_confirmed"] for child in children)
        delivered = [set(child["spec"]["metadata"]["candidate_identities"]) & all_rolls for child in children]
        assert delivered[0].isdisjoint(delivered[1])
        assert set.union(*delivered) == all_rolls
    else:
        day = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["state"]["days"][MARKET_DATE]
        followup = set(day["candidate_delivery"]["candidate_identities"]) & all_rolls
        assert followup == all_rolls - acknowledged
        assert set(day["alerted_candidates"]) & all_rolls == all_rolls
    assert len(calls) == 2


def test_suppressing_unshown_roll_preserves_close_in_same_position(tmp_path: Path) -> None:
    brief = roll_brief(base=tmp_path, rows=[{**position_row(), "recommendation_state": "close",
        "notification_eligible": True, "evaluation_status": "priced", "quote_status": "priced"}])
    render_brief = flow._daily_brief_roll_render_source(brief, [])
    message = renderer.render_fixed_report_text(render_brief, diff={}, limits={}, context={})
    identities = flow._daily_brief_transport_roll_identities(
        render_brief, [], delivery_kind="fixed_report", diff={}, limits={},
        rendered_message=message, rendered_transport=None)
    assert "建议平仓" in message
    assert "向上移仓" not in message
    assert identities == []
    assert daily_brief_position_roll(brief["positions"][0]) is not None


@pytest.mark.parametrize("gate", ["quiet", "disabled", "missing_target", "closed_window"])
def test_roll_alert_preserves_notification_gates(monkeypatch, tmp_path: Path, gate: str) -> None:
    from dataclasses import replace
    patch_roll_assembler(monkeypatch)
    calls = []
    _patch_sender(monkeypatch, calls=calls)
    config = _config()
    if gate == "disabled":
        config["notifications"]["enabled"] = False
    elif gate == "missing_target":
        config["notifications"]["target"] = ""
    elif gate == "quiet":
        monkeypatch.setattr(flow, "evaluate_dnd_quiet_hours", lambda **kw: {"is_quiet": True, "parse_error": None})
    bundle = _request(tmp_path, run_id=gate, fixed=False, config=config)
    if gate == "closed_window":
        scheduler = dict(bundle.request.scheduler_decisions_by_account["lx"], in_run_window=False)
        bundle.request = replace(bundle.request, scheduler_decisions_by_account={"lx": scheduler})
    if gate == "missing_target":
        with pytest.raises(SystemExit, match="notifications.target is required"):
            flow.run_tick_notification_flow(bundle.request)
    else:
        flow.run_tick_notification_flow(bundle.request)
    assert calls == []
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    if state.get("available"):
        assert not state["state"]["days"].get(MARKET_DATE, {}).get("alerted_candidates")
