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
        "new_capital_basis": 10000, "extension_days": 0,
        "evidence": {"old_ask": 1.0, "new_bid": 2.0},
    }
    if option_type == "call":
        target.update(recommendation="credit_roll" if credit > 0 else "cash_neutral_roll" if credit == 0 else "debit_roll",
            cash_flow_type="credit" if credit > 0 else "neutral" if credit == 0 else "debit",
            strike_increase=5, strike_increase_pct=0.05, assignment_net_improvement=500 + credit,
            assignment_improvement_period_return=0.0595, assignment_improvement_annualized_return=0.7)
    else:
        target.update(strike_drop_pct=0.05)
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


def _assert_roll_explanations_absent(text: str) -> None:
    for explanation in (
        "每股", "含双腿费用", "双腿扣费后", "移仓收益口径", "收益分母",
        "净现金流 ÷", "新仓担保资金", "新到期剩余期限",
    ):
        assert explanation not in text


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
        assert "增量期收益率" in text and "增量年化" in text
        _assert_roll_explanations_absent(text)
        assert "预计锁定收益" not in text


@pytest.mark.parametrize("render", [
    renderer.render_query_brief, renderer.render_full_brief, renderer.render_fixed_report,
    renderer.render_fixed_report_text, renderer.render_fixed_report_card_markdown,
    renderer.render_candidate_alert, renderer.render_candidate_alert_text,
    renderer.render_candidate_alert_card_markdown, renderer.render_delta_brief,
    renderer.render_recovery_brief, renderer.render_daily_brief_lifecycle,
])
@pytest.mark.parametrize("option_type", ["put", "call"])
def test_roll_notifications_omit_explanations_and_preserve_values(
    tmp_path: Path, render, option_type: str,
) -> None:
    rows = [position_row(lot=f"lot-{i}", option_type=option_type, credit=24.3734) for i in range(2)]
    for row in rows:
        row.pop("contract_symbol")
        row["roll_advice"]["position"].pop("contract_symbol")
        target = row["roll_advice"]["candidates"][0]
        target.update(new_capital_basis=7900 if option_type == "put" else 10000,
            incremental_period_return=0.003, incremental_annualized_return=0.039)
        target["evidence"].update(old_ask=1.81234, new_bid=2.11234)
    brief = roll_brief(base=tmp_path, rows=rows)
    if render.__name__.startswith("render_candidate_alert"):
        message = render(brief, daily_brief_roll_notification_identities(brief))
    elif render in (renderer.render_delta_brief, renderer.render_recovery_brief):
        message = render(brief, {})
    elif render is renderer.render_daily_brief_lifecycle:
        message = render({"brief": brief, "delivery_kind": "full"})
    else:
        message = render(brief)

    lines = message.splitlines()
    assert "平仓 ask USD 1.81234；开仓 bid USD 2.11234" in lines
    assert "合计 2 手" in lines
    assert "每手净收取：USD 24.37" in lines
    assert "共 2 笔" not in message and "合计净收取" not in message
    assert message.count("旧仓买回") == 1
    assert "24.3734" not in message
    assert "增量期收益率 0.3%；增量年化 3.9%" in lines
    assert not any(line.startswith("参考｜") for line in lines)
    _assert_roll_explanations_absent(message)
    for removed in ("()", "（）"):
        assert removed not in message


@pytest.mark.parametrize("old_code", [None, "", "   ", "OLD_OPTION_CONTRACT"])
@pytest.mark.parametrize("currency", ["USD", "HKD"])
def test_roll_optional_contract_code_and_total_precision(tmp_path: Path, old_code, currency: str) -> None:
    row = position_row(option_type="put", credit=1234.5678)
    row["contract_symbol"] = old_code
    row["roll_advice"]["position"].update(contract_symbol=old_code, currency=currency)
    brief = roll_brief(base=tmp_path, rows=[row])
    message = renderer.render_fixed_report_card_markdown(brief)
    contract_line = next(line for line in message.splitlines() if "旧仓买回：" in line)
    old_leg = contract_line.split("；新仓卖出：", 1)[0]
    assert "()" not in contract_line and "（）" not in contract_line
    if old_code and old_code.strip():
        assert f"（{old_code}）" in old_leg
    else:
        assert "Put（" not in old_leg
    assert f"（{row['roll_advice']['candidates'][0]['contract']}）" in contract_line
    assert f"每手净收取：{currency} 1234.57" in message
    assert f"平仓 ask {currency} 1；开仓 bid {currency} 2" in message
    _assert_roll_explanations_absent(message)


def test_roll_return_note_only_appears_with_visible_roll(tmp_path: Path) -> None:
    brief = roll_brief(base=tmp_path, rows=[{**position_row(), "roll_advice": None}])
    for render in (renderer.render_query_brief, renderer.render_fixed_report_text,
                   renderer.render_fixed_report_card_markdown):
        assert "移仓收益口径" not in render(brief)


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
    text = renderer.render_fixed_report_text(brief)
    assert "CC 向上移仓" not in text
    assert "缺口摘要：1 条暂无法评估" in text
    assert "旧仓买回" not in text

    row.update(evaluation_status="evaluable", quote_status="priced")
    text = renderer.render_fixed_report_text(brief)
    assert "普通持有 1 条" in text
    assert "暂无法评估" not in text
    assert "CC 向上移仓" not in text


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


def test_grouped_roll_alert_acknowledges_all_lots_and_does_not_repeat(monkeypatch, tmp_path: Path) -> None:
    rows = [position_row(lot=f"grouped-{index}") for index in range(3)]
    patch_roll_assembler(monkeypatch, rows=rows)
    calls = []
    _patch_sender(monkeypatch, calls=calls)
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="grouped", fixed=False).request) == 0
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["state"]["days"][MARKET_DATE]
    assert set(state["alerted_candidates"]) == daily_brief_roll_notification_identities(
        roll_brief(base=tmp_path, rows=rows))
    assert not state["pending_candidates"]
    assert len(calls) == 1
    assert calls[0]["message"].count("旧仓买回") == 1
    assert "合计 3 手" in calls[0]["message"]
    assert "每手净收取：USD 95.00" in calls[0]["message"]
    assert "共 3 笔" not in calls[0]["message"] and "合计净收取" not in calls[0]["message"]
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="grouped-again", fixed=False).request) == 0
    assert len(calls) == 1


def test_display_budget_acknowledges_only_shown_roll_positions(monkeypatch, tmp_path: Path) -> None:
    rows = [position_row(lot=f"lot-{n}") for n in range(7)]
    for index, row in enumerate(rows):
        # Distinct actions still consume distinct slots after lot grouping.
        row["roll_advice"]["candidates"][0]["contract"] = f"ROLL_TARGET_{index}"
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
    with pytest.raises(ValueError, match="render_unavailable"):
        flow._prepare_daily_brief_notification(_request(tmp_path, run_id="truncated", fixed=False).request)
    state = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    day = state["state"]["days"][MARKET_DATE]
    assert not day["alerted_candidates"]
    assert len(day["pending_candidates"]) == 1


def _many_candidate_roll_brief(*, base, run_id, account="lx") -> dict:
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity
    from test_daily_decision_brief_renderer import _candidate

    rows = [position_row(lot=f"lot-{i}") for i in range(20)]
    for i, row in enumerate(rows):
        row["roll_advice"]["candidates"][0]["contract"] = f"ROLL_TARGET_{i}"
    brief = roll_brief(base=base, run_id=run_id, account=account, rows=rows)
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


def _budget_combo_roll_brief(*, base: Path, run_id: str) -> dict:
    from domain.domain.combo_candidate_evidence import build_combo_candidate_occurrence
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity
    from test_daily_decision_brief_renderer import _brief as renderer_brief

    brief = _many_candidate_roll_brief(base=base, run_id=run_id)
    for family in ("sell_put", "covered_call"):
        brief["candidates"][family] = brief["candidates"][family][:12]
    brief["candidate_index"] = [
        item for item in brief["candidate_index"] if int(item["symbol"][3:]) < 12
    ]
    combos = []
    for i in range(3):
        symbol = f"COMBO{i}"
        candidate = deepcopy(renderer_brief()["candidates"]["combo_yield"][0])
        candidate.update(
            symbol=symbol, candidate_pair_id=f"combo-pair-{i}", strategy_group_id=f"combo-pair-{i}",
            structure_mode="same_expiry_pair", currency="USD", multiplier=100,
            capacity={"contracts_available": 1},
            put_contract_symbol=f"{symbol}260821P00300000",
            call_contract_symbol=f"{symbol}260821C00400000", call_expiration="2026-08-21",
        )
        candidate.update(build_combo_candidate_occurrence(
            candidate, account="lx", market="US", run_id=run_id,
            generated_at_utc=brief["generated_at_utc"],
        ))
        combos.append(candidate)
        brief["candidate_index"].append({
            "identity": build_daily_brief_candidate_identity(
                account="lx", market="US", symbol=symbol, strategy_family="combo_yield",
            ),
            "symbol": symbol, "strategy_family": "combo_yield", "representative": candidate,
            "contract_count": 1,
        })
    brief["candidates"]["combo_yield"] = combos
    return brief


def test_combo_selector_uses_effective_advice_time_for_shared_budget(tmp_path: Path) -> None:
    brief = _budget_combo_roll_brief(base=tmp_path, run_id="effective-time")
    for row in brief["positions"]:
        row["roll_advice"]["actionable_until_utc"] = "2026-07-21T14:01:00Z"
    limits = {"max_items_per_account": 0, "max_candidates_per_strategy": 20}
    context = {"advice_time_utc": "2026-07-21T14:02:00Z"}

    view = renderer.build_daily_brief_user_view(brief, limits=limits, context=context)
    selected = renderer.select_rendered_combo_candidate_rows(
        brief, delivery_kind="fixed_report", limits=limits, context=context,
    )

    assert not view["positions"]
    assert [row["symbol"] for row in selected] == ["COMBO0", "COMBO1", "COMBO2"]
    assert [row["symbol"] for row in view["candidates"] if row["family"] == "combo_yield"] == [
        row["symbol"] for row in selected
    ]


@pytest.mark.parametrize("explicit_routes", [False, True])
@pytest.mark.parametrize("remove_rolls", ["expired", "transport"])
def test_final_combo_metadata_uses_shared_budget_after_roll_removal(
    monkeypatch, tmp_path: Path, explicit_routes: bool, remove_rolls: str,
) -> None:
    from test_daily_decision_brief_notification_flow import _feishu_config

    captured = []
    def assemble(**kw):
        brief = _budget_combo_roll_brief(base=kw["base"], run_id=kw["run_id"])
        if remove_rolls == "expired":
            for row in brief["positions"]:
                row["roll_advice"]["actionable_until_utc"] = "2026-07-21T14:00:01Z"
        captured.append(brief)
        return {"US": brief}

    monkeypatch.setattr(flow, "assemble_daily_decision_briefs", assemble)
    monkeypatch.setattr(flow, "utc_now", lambda: datetime(2026, 7, 21, 14, 0, 30, tzinfo=timezone.utc))
    if remove_rolls == "transport":
        actual_fits = flow.feishu_notification_blocks_fit
        def without_roll_details(blocks, text, **kwargs):
            return actual_fits(blocks, text, **kwargs) and not any(
                any(identity.startswith("position-roll:") for identity in getattr(block, "identities", ()))
                for block in blocks
            )
        monkeypatch.setattr(flow, "feishu_notification_blocks_fit", without_roll_details)
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_fixture")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    config = _feishu_config()
    config["notifications"]["daily_brief"] = {
        "max_items_per_account": 0, "max_candidates_per_strategy": 20,
    }
    if explicit_routes:
        config["notifications"]["routes"] = [{
            "id": "primary", "provider": "feishu_app", "channel": "feishu_app",
            "target": "ou_fixture", "accounts": ["lx"],
        }]
    request = _request(tmp_path, run_id="combo-budget", config=config).request

    preparation = flow._prepare_daily_brief_notification(request)

    if explicit_routes:
        from src.application.daily_decision_brief_repository import read_daily_brief_fanout_children
        child = read_daily_brief_fanout_children(
            base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE,
        )[0]
        metadata = child["spec"]["metadata"]
        message = child["spec"]["message"]
    else:
        envelope = preparation.lifecycles_by_account["lx"]["envelope"]
        metadata = envelope
        message = envelope["rendered_message"]
    render_context = metadata["render_context"]
    states = {item["identity"]: item["state"] for item in metadata["display_evidence"]}
    original_view = renderer.build_daily_brief_user_view(captured[0],
        limits=config["notifications"]["daily_brief"], context=render_context)
    expected = {
        item["identity"] for item in captured[0]["candidate_index"]
        if item["strategy_family"] == "combo_yield" and any(
            row["symbol"] == item["symbol"] and row["family"] == "combo_yield"
            for row in original_view["candidates"])
    }
    assert len([item for item in captured[0]["candidate_index"]
                if item["strategy_family"] == "combo_yield"]) == 3
    # Rank rotation keeps all three Combo rows in the shared 42-item plan;
    # stripping roll cards from transport never causes a second selection.
    assert len(expected) == 3
    assert set(render_context["rendered_combo_candidate_identities"]) == expected
    assert len(render_context["candidate_occurrence_ids"]) == len(expected)
    assert len(render_context["candidate_exposure_ids"]) == len(expected)
    expanded = {identity for identity in expected if states[identity] == "expanded"}
    actual_expanded = {
        item["identity"] for item in captured[0]["candidate_index"]
        if item["strategy_family"] == "combo_yield" and states[item["identity"]] == "expanded"
    }
    assert expanded == actual_expanded
    # The event summary can retain a symbol after its complete action card is
    # removed. Only a complete card title proves expanded delivery.
    assert all((f"{item['symbol']}｜组合增强（策略排序" in message) == (item["identity"] in expanded)
               for item in captured[0]["candidate_index"] if item["strategy_family"] == "combo_yield")
    assert not any(state == "expanded" for identity, state in states.items()
                   if identity.startswith("position-roll:"))


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

    # Exercise length truncation without refilling candidate slots when hidden rolls are removed.
    monkeypatch.setattr(renderer, "_MAX_MESSAGE_CHARS", 6000)
    limits = {"max_roll_up_items": 0, "max_candidates_per_strategy": 10}
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
    monkeypatch.setattr(renderer, "_MAX_MESSAGE_CHARS", 2_000)
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
        acknowledged = {row["identity"] for row in child["spec"]["metadata"]["display_evidence"]
                        if row["state"] == "expanded"} & all_rolls
    else:
        day = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["state"]["days"][MARKET_DATE]
        envelope = next(iter(day["fixed_reports"].values()))["rendered_transport"]
        acknowledged = set(day["alerted_candidates"]) & all_rolls
    primary, fallback = feishu_notification_visible_markdown(envelope)
    for body in (primary, fallback, envelope["text"]):
        _assert_roll_explanations_absent(body)
        assert not any(line.startswith("参考｜") for line in body.splitlines())
    primary_rolls, fallback_rolls = [renderer.select_rendered_position_roll_identities(
        brief, delivery_kind="fixed_report", limits=limits, rendered_message=body)
        for body in (primary, fallback)]
    assert 0 < len(acknowledged) < len(all_rolls)
    assert acknowledged == primary_rolls == fallback_rolls

    # A larger later budget can deliver the previously omitted actions.
    monkeypatch.setattr(renderer, "_MAX_MESSAGE_CHARS", 12_000)
    assert flow.run_tick_notification_flow(_request(tmp_path, run_id="followup", fixed=False, config=config).request) == 0
    if explicit_routes:
        children = read_daily_brief_fanout_children(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE)
        assert len(children) == 2 and all(child["delivery_confirmed"] for child in children)
        delivered = [{row["identity"] for row in child["spec"]["metadata"]["display_evidence"]
                      if row["state"] == "expanded"} & all_rolls for child in children]
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
        "notification_eligible": True, "evaluation_status": "priced", "quote_status": "priced",
        "metrics": {"capital_basis": 10000, "remaining_max_annualized_return": 0.05,
                    "net_capture_ratio": 0.95}}])
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
