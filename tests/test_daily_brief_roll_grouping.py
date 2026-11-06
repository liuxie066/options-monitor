from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from domain.domain.daily_decision_brief import daily_brief_roll_notification_identities
from src.application import tick_notification_flow
from src.application.channels.feishu_notification_renderer import render_feishu_notification_card
from src.application.daily_decision_brief_renderer import (
    build_daily_brief_user_view,
    render_candidate_alert_card_markdown,
    render_candidate_alert_text,
    render_fixed_report_card_markdown,
    render_fixed_report_text,
    select_rendered_position_roll_identities,
)


def _roll_row(
    lot: str,
    *,
    option_type: str = "put",
    symbol: str = "TEST",
    expiration: str = "2026-08-21",
    strike: float = 100,
    target_contract: str = "TEST260918P00095000",
    contracts: int = 1,
    credit: float = 100.125,
    expires: str = "2026-07-21T15:00:00Z",
) -> dict:
    return {
        "position_lot_id": lot,
        "symbol": symbol,
        "strategy_family": "sell_put" if option_type == "put" else "covered_call",
        "option_type": option_type,
        "expiration": expiration,
        "strike": strike,
        "recommendation_state": "hold",
        "notification_eligible": False,
        "roll_notification_eligible": True,
        "roll_advice": {
            "status": "success",
            "outcome": "found",
            "actionable_until_utc": expires,
            "position": {
                "lot_id": lot,
                "symbol": symbol,
                "option_type": option_type,
                "expiration": expiration,
                "strike": strike,
                "contracts_open": contracts,
                "currency": "USD",
            },
            "candidates": [{
                "contract": target_contract,
                "expiration": "2026-09-18",
                "strike": 95 if option_type == "put" else 105,
                "contracts": contracts,
                "net_credit": credit,
                "per_contract": {"net_credit": credit / contracts},
                "recommendation": "credit_roll",
                "cash_flow_type": "credit",
                "evidence": {"old_ask": 1, "new_bid": 2},
            }],
        },
    }


def _brief(rows: list[dict]) -> dict:
    return {
        "account": "fixture",
        "market": "US",
        "generated_at_utc": "2026-07-21T14:30:00Z",
        "actionability": "live_actionable",
        "positions": rows,
    }


def _view(rows: list[dict], *, limit: int = 5, **kwargs) -> dict:
    return build_daily_brief_user_view(
        _brief(rows), limits={"max_actions_per_priority": limit}, **kwargs,
    )


@pytest.mark.parametrize("option_type", ["put", "call"])
def test_same_roll_groups_lots_sizes_and_unrounded_credit_without_mutating_brief(option_type: str) -> None:
    rows = [
        _roll_row("first", option_type=option_type, contracts=2),
        _roll_row("second", option_type=option_type, contracts=3),
        _roll_row("third", option_type=option_type),
    ]
    brief = _brief(rows)
    original = deepcopy(brief)
    view = build_daily_brief_user_view(brief)

    assert len(view["positions"]) == 1
    card = view["positions"][0]
    assert card["is_roll"] is True
    assert card["lot_count"] == 3
    assert card["contracts"] == 6
    assert card["net_credit"] == pytest.approx(300.375)
    assert "合计 6 手" in card["details"]
    assert "每手净收取：USD 50.06" in card["details"]
    assert all("共 3 笔" not in detail and "合计净收取" not in detail for detail in card["details"])
    assert view["position_total"] == 3
    assert view["position_actionable_total"] == 1
    assert brief == original


@pytest.mark.parametrize("option_type", ["put", "call"])
def test_roll_group_displays_total_contracts_with_one_contract_returns(option_type: str) -> None:
    rows = [
        _roll_row("first", option_type=option_type, contracts=2, credit=1008.204),
        _roll_row("second", option_type=option_type, credit=487),
        _roll_row("third", option_type=option_type, credit=487),
    ]
    for row in rows:
        row["roll_advice"]["position"]["currency"] = "HKD"
        row["roll_advice"]["candidates"][0].update(
            incremental_period_return=0.01292569,
            incremental_annualized_return=0.05684190,
            per_contract={"net_credit": 487, "incremental_period_return": 487 / 39000,
                "incremental_annualized_return": 487 / 39000 * 365 / 83},
        )
    brief = _brief(rows)
    original = deepcopy(brief)

    identities = daily_brief_roll_notification_identities(brief)
    for render in (render_fixed_report_text, render_fixed_report_card_markdown,
                   render_candidate_alert_text, render_candidate_alert_card_markdown):
        candidate_alert = render in (render_candidate_alert_text, render_candidate_alert_card_markdown)
        message = render(brief, identities) if candidate_alert else render(brief)
        assert message.count("旧仓买回") == 1
        assert "合计 4 手" in message
        assert "每手净收取：HKD 487.00" in message
        assert "增量期收益率 1.2%；增量年化 5.5%" in message
        assert "共 3 笔" not in message
        assert "合计净收取" not in message
        assert "1008.20" not in message and "1,982.20" not in message
        assert select_rendered_position_roll_identities(
            brief, delivery_kind="candidate_alert" if candidate_alert else "fixed_report",
            candidate_identities=identities, rendered_message=message,
        ) == identities
    assert brief == original


def test_old_multi_contract_roll_does_not_mislabel_total_as_per_contract_cash() -> None:
    row = _roll_row("legacy", contracts=2, credit=1008.204)
    target = row["roll_advice"]["candidates"][0]
    target.pop("per_contract")
    target.update(incremental_period_return=0.01292569, incremental_annualized_return=0.05684190)

    for render in (render_fixed_report_text, render_fixed_report_card_markdown):
        message = render(_brief([row]))
        assert "合计 2 手" in message
        assert "每手净收取：暂无法评估" in message
        assert "1008.20" not in message and "504.10" not in message
        assert "增量期收益率" not in message and "增量年化" not in message


@pytest.mark.parametrize("changed", [
    {"option_type": "call"},
    {"symbol": "OTHER"},
    {"expiration": "2026-07-17"},
    {"strike": 101},
    {"target_contract": "OTHER_TARGET"},
])
def test_roll_group_preserves_each_old_position_and_target_boundary(changed: dict) -> None:
    view = _view([_roll_row("first"), _roll_row("second", **changed)])
    assert len(view["positions"]) == 2
    assert all(card["lot_count"] == 1 for card in view["positions"])


def test_expired_roll_member_and_hold_rows_do_not_consume_slots_or_inflate_totals() -> None:
    hold = {"symbol": "OBSERVE", "recommendation_state": "hold", "notification_eligible": False}
    rows = [
        hold,
        _roll_row("expired", expires="2026-07-21T14:29:59Z"),
        _roll_row("first", contracts=2),
        _roll_row("second", contracts=3),
    ]
    card, = _view(rows, limit=1)["positions"]
    assert card["lot_count"] == 2
    assert card["contracts"] == 5
    assert card["net_credit"] == pytest.approx(200.25)
    assert len(_view(rows, context={"query_time_utc": "2026-07-21T15:00:01Z"})["positions"]) == 0


def test_section_limit_applies_after_roll_grouping_and_preserves_first_action_order() -> None:
    rows = [
        _roll_row("first"),
        _roll_row("second"),
        _roll_row("third", target_contract="SECOND_TARGET"),
        _roll_row("fourth", target_contract="THIRD_TARGET"),
    ]
    view = _view(rows, limit=2)
    assert len(view["positions"]) == 2
    assert view["positions"][0]["lot_count"] == 2
    assert "SECOND_TARGET" in " ".join(view["positions"][1]["details"])
    assert "THIRD_TARGET" not in str(view["positions"])
    assert view["position_actionable_total"] == 3


def test_changed_member_prioritizes_its_complete_roll_group_without_extra_slots() -> None:
    rows = [
        _roll_row("first"),
        _roll_row("second"),
        _roll_row("changed-first", target_contract="CHANGED_TARGET"),
        _roll_row("changed-second", target_contract="CHANGED_TARGET"),
    ]
    diff = {"changes": [{"action": {
        "action_type": "close_position", "position_lot_id": "changed-second",
    }}]}
    view = _view(rows, limit=1, diff=diff)
    card, = view["positions"]
    assert "CHANGED_TARGET" in " ".join(card["details"])
    assert card["lot_count"] == 2


def test_non_roll_close_cards_stay_separate_and_keep_their_fields() -> None:
    close = {
        "symbol": "CLOSE",
        "strategy_family": "sell_put",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 50,
        "recommendation_state": "close",
        "evaluation_status": "evaluable",
        "quote_status": "priced",
        "metrics": {"ask": 0.5, "estimated_pnl_if_close_net": 25},
    }
    view = _view([_roll_row("first"), _roll_row("second"), close, deepcopy(close)])
    assert len(view["positions"]) == 3
    first, second = view["positions"][1:]
    assert first == second
    assert first["is_roll"] is False
    assert first["status"] == "建议平仓"
    assert first["close_ask"] == "$0.50"
    assert first["realized_if_close"] == "+$25.00"


@pytest.mark.parametrize("render,delivery_kind", [
    (render_fixed_report_text, "fixed_report"),
    (render_fixed_report_card_markdown, "fixed_report"),
    (render_candidate_alert_text, "candidate_alert"),
    (render_candidate_alert_card_markdown, "candidate_alert"),
])
def test_visible_group_acknowledges_every_member_lot_and_respects_truncation(render, delivery_kind: str) -> None:
    brief = _brief([
        _roll_row("first"), _roll_row("second"),
        _roll_row("hidden", target_contract="HIDDEN_TARGET"),
    ])
    limits = {"max_actions_per_priority": 1}
    identities = sorted(daily_brief_roll_notification_identities(brief))
    args = (brief, identities) if delivery_kind == "candidate_alert" else (brief,)
    message = render(*args, limits=limits)
    expected = daily_brief_roll_notification_identities({**brief, "positions": brief["positions"][:2]})
    kwargs = {
        "delivery_kind": delivery_kind,
        "candidate_identities": identities,
        "limits": limits,
    }
    assert select_rendered_position_roll_identities(brief, rendered_message=message, **kwargs) == expected
    assert select_rendered_position_roll_identities(brief, rendered_message=None, **kwargs) == expected
    assert select_rendered_position_roll_identities(brief, rendered_message="持仓内容已截断", **kwargs) == set()


@pytest.mark.parametrize("grouped", [False, True])
def test_roll_cards_preserve_close_advice_for_each_close_member(grouped: bool) -> None:
    first = _roll_row("hold" if grouped else "close")
    close = _roll_row("close") if grouped else first
    close.update(
        recommendation_state="close", notification_eligible=True,
        quote_status="priced", evaluation_status="evaluable",
        metrics={"ask": 0.5, "estimated_pnl_if_close_net": 25, "net_capture_ratio": 0.8},
    )
    rows = [first, close] if grouped else [close]
    if grouped:
        other_close = deepcopy(close)
        other_close["position_lot_id"] = "another-close"
        other_close["roll_advice"]["position"]["lot_id"] = "another-close"
        other_close["metrics"]["estimated_pnl_if_close_net"] = 40
        rows.append(other_close)
    brief = _brief(rows)
    card, = build_daily_brief_user_view(brief)["positions"]
    assert card["status"] == "CSP 向下移仓 · 同时建议平仓"
    for render in (render_fixed_report_text, render_fixed_report_card_markdown):
        message = render(brief)
        assert "同时建议平仓" in message
        assert "买回参考价 $0.50（ask）" in message
        assert "预计锁定收益 $25.00" in message
        assert "净兑现比例 80.0%" in message
        if grouped:
            assert "预计锁定收益 $40.00" in message
            assert message.count("旧仓买回") == 1


@pytest.mark.parametrize("render", [render_fixed_report_text, render_fixed_report_card_markdown])
def test_truncated_group_cannot_borrow_identical_totals_from_another_old_position(render) -> None:
    brief = _brief([
        _roll_row("a1", contracts=1, credit=80),
        _roll_row("a2", contracts=3, credit=240),
        _roll_row("b1", expiration="2026-07-17", contracts=2, credit=160),
        _roll_row("b2", expiration="2026-07-17", contracts=2, credit=160),
    ])
    cards = build_daily_brief_user_view(brief)["positions"]
    assert len(cards) == 2
    assert cards[0]["details"][1:] == cards[1]["details"][1:]
    message = render(brief)
    first_line = cards[1]["details"][0]
    truncated = message[:message.index(first_line) + len(first_line)]
    expected = daily_brief_roll_notification_identities({**brief, "positions": brief["positions"][:2]})
    assert select_rendered_position_roll_identities(
        brief, delivery_kind="fixed_report", rendered_message=truncated,
    ) == expected


@pytest.mark.parametrize("partial_output", ["primary", "fallback", "message"])
def test_final_transport_only_acknowledges_complete_grouped_cards(monkeypatch, partial_output: str) -> None:
    brief = _brief([
        _roll_row("a1", contracts=1, credit=80),
        _roll_row("a2", contracts=3, credit=240),
        _roll_row("b1", expiration="2026-07-17", contracts=2, credit=160),
        _roll_row("b2", expiration="2026-07-17", contracts=2, credit=160),
    ])
    monkeypatch.setattr(tick_notification_flow, "utc_now", lambda: datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc))
    text = render_fixed_report_text(brief)
    markdown = render_fixed_report_card_markdown(brief)
    first_line = build_daily_brief_user_view(brief)["positions"][1]["details"][0]
    truncate = lambda body: body[:body.index(first_line) + len(first_line)]
    transport = render_feishu_notification_card(
        markdown=truncate(markdown) if partial_output == "primary" else markdown,
        fallback_text=truncate(text) if partial_output == "fallback" else text,
        fallback_open_id="ou_fixture",
    )
    identities = sorted(daily_brief_roll_notification_identities(brief))
    expected = daily_brief_roll_notification_identities({**brief, "positions": brief["positions"][:2]})
    assert set(tick_notification_flow._daily_brief_transport_roll_identities(
        brief, identities, delivery_kind="fixed_report", diff={}, limits={},
        rendered_message=truncate(text) if partial_output == "message" else text,
        rendered_transport=transport,
    )) == expected


@pytest.mark.parametrize("account,expected_groups", [
    ("lx", [
        ("0700.HK", "2026-11-27", 410, "HK.TCH270128P400000", 1, 1, 488.0),
        ("9992.HK", "2026-11-27", 140, "HK.POP270128P137500", 3, 3, 1926.0),
        ("0700.HK", "2026-11-27", 420, "HK.TCH270128P410000", 2, 2, 952.708),
    ]),
    ("sy", [
        ("9992.HK", "2026-12-30", 140, "HK.POP270128P137500", 2, 2, 88.0),
        ("9992.HK", "2026-11-27", 140, "HK.POP270128P137500", 1, 1, 642.0),
        ("0700.HK", "2026-11-27", 420, "HK.TCH270128P410000", 2, 7, 3487.222),
    ]),
])
def test_historical_hk_briefs_render_three_distinct_roll_actions(account: str, expected_groups: list) -> None:
    fixture = Path(__file__).parent / "fixtures" / "daily_brief_roll_grouping" / f"{account}.json"
    brief = json.loads(fixture.read_text())
    original = deepcopy(brief)
    limits = {"max_actions_per_priority": 5, "max_candidates_per_strategy": 3}
    view = build_daily_brief_user_view(brief, limits=limits)
    cards = [card for card in view["positions"] if card["is_roll"]]
    assert len(cards) == 3
    by_lot = {row["position_lot_id"]: row for row in brief["positions"]}
    seen_actions = set()
    target_old_positions: dict[str, set] = {}
    for card, expected in zip(cards, expected_groups):
        symbol, expiration, strike, contract, lots, contracts, credit = expected
        assert card["lot_count"] == lots
        assert card["contracts"] == contracts
        assert card["net_credit"] == pytest.approx(credit)
        assert contract in card["details"][0]
        # Every displayed member matches the complete old-position/target key.
        for identity in card["roll_identities"]:
            members = [row for row in by_lot.values() if identity in daily_brief_roll_notification_identities(
                {**brief, "positions": [row]},
            )]
            member, = members
            assert (member["symbol"], member["expiration"], member["strike"]) == (symbol, expiration, strike)
            assert member["roll_advice"]["candidates"][0]["contract"] == contract
        action_key = (symbol, expiration, strike, contract)
        assert action_key not in seen_actions
        seen_actions.add(action_key)
        old_positions = target_old_positions.setdefault(contract, set())
        assert (symbol, expiration, strike) not in old_positions
        old_positions.add((symbol, expiration, strike))
    if account == "lx":
        assert len(target_old_positions) == 3
        assert "合计 3 手" in cards[1]["details"]
        assert "每手净收取：HKD 642.00" in cards[1]["details"]
        assert "合计 2 手" in cards[2]["details"]
        assert "每手净收取：HKD 476.35" in cards[2]["details"]
    else:
        assert target_old_positions["HK.POP270128P137500"] == {
            ("9992.HK", "2026-11-27", 140), ("9992.HK", "2026-12-30", 140),
        }
    for render in (render_fixed_report_text, render_fixed_report_card_markdown):
        message = render(brief, limits=limits)
        assert message.count("旧仓买回") == 3
        assert select_rendered_position_roll_identities(
            brief, delivery_kind="fixed_report", limits=limits, rendered_message=message,
        ) == daily_brief_roll_notification_identities(brief)
    assert brief == original
