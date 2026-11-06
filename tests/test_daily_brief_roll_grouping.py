from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from domain.domain.daily_decision_brief import daily_brief_roll_notification_identities
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
    assert "共 3 笔 · 合计净收取 USD 300.38 · 合计 6 手" in card["details"]
    assert view["position_total"] == 3
    assert view["position_actionable_total"] == 1
    assert brief == original


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
        assert "共 3 笔 · 合计净收取 HKD 1,926.00 · 合计 3 手" in cards[1]["details"]
        assert "共 2 笔 · 合计净收取 HKD 952.71 · 合计 2 手" in cards[2]["details"]
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
