from __future__ import annotations

from copy import deepcopy
import random

import pytest

from src.application.daily_decision_brief_renderer import (
    build_daily_brief_user_view, render_fixed_report_card_markdown,
)
from test_daily_decision_brief_renderer import _brief, _candidate
from test_tick_position_advice_views import position_row


def _roll(index: int, *, annualized: float = 0.2, credit: float = 100, option_type: str = "call") -> dict:
    row = position_row(lot=f"roll-{index}", credit=credit, option_type=option_type)
    row["symbol"] = f"R{index}"
    row["roll_advice"]["position"]["symbol"] = row["symbol"]
    target = row["roll_advice"]["candidates"][0]
    target["incremental_annualized_return"] = annualized
    target["extension_days"] = 0
    return row


def _close(index: int) -> dict:
    return {
        "symbol": f"C{index}", "position_lot_id": f"close-{index}",
        "strategy_family": "sell_put", "expiration": "2026-08-21",
        "strike": 100, "option_type": "put", "quote_status": "priced",
        "evaluation_status": "evaluable", "recommendation_state": "close",
        "notification_eligible": True,
        "metrics": {"remaining_max_annualized_return": index / 1000,
                    "net_capture_ratio": 0.9, "ask": 0.1, "capital_basis": 10000},
    }


def _source(rows: list[dict]) -> dict:
    brief = _brief()
    brief.update(positions=rows, candidates={}, actions=[])
    return brief


def _symbols(view: dict) -> list[str]:
    return [row["title"].split(" · ")[0] for row in view["positions"]]


@pytest.mark.parametrize("with_candidates", [False, True])
def test_thirteen_positions_keep_both_rolls_and_hold_uses_no_card(with_candidates: bool) -> None:
    holds = [{**_close(i), "recommendation_state": "hold"} for i in range(11)]
    rows = holds[:5] + [_roll(1, credit=2040), _roll(2, credit=1523)] + holds[5:]
    brief = _source(rows)
    if with_candidates:
        brief["candidates"] = {"sell_put": [
            _candidate(rank=i + 1, symbol=f"B{i}", option_type="put",
                       expiration="2026-08-21", strike=100, capacity=1)
            for i in range(6)
        ]}
    view = build_daily_brief_user_view(brief)
    assert set(_symbols(view)) == {"R1", "R2"}
    assert len(view["positions"]) + len(view["candidates"]) <= 5
    assert view["position_hold_total"] == 11
    message = render_fixed_report_card_markdown(brief)
    assert "R1" in message and "R2" in message
    assert "普通持有 11 条" in message


def test_close_roll_queues_alternate_and_urgent_deadline_precedes_without_duplicate() -> None:
    rows = [_roll(i, annualized=0.5 - i / 100) for i in range(4)] + [_close(i) for i in range(3)]
    brief = _source(rows)
    assert _symbols(build_daily_brief_user_view(brief)) == ["C0", "R0", "C1", "R1", "C2", "R2", "R3"]
    rows[3]["last_trading_at_utc"] = "2026-07-20T16:00:00Z"
    context = {"next_fixed_report_at_utc": "2026-07-20T18:00:00Z"}
    view = build_daily_brief_user_view(brief, context=context, limits={"max_total_items": 5})
    assert _symbols(view) == ["R3", "C0", "R0", "C1", "R1"]
    assert len(set(_symbols(view))) == 5


def test_roll_rank_uses_sealed_annualized_return_and_is_input_order_independent() -> None:
    rows = [_roll(0, annualized=0.1, credit=2040), _roll(1, annualized=0.8, credit=100)]
    for seed in range(5):
        shuffled = deepcopy(rows)
        random.Random(seed).shuffle(shuffled)
        assert _symbols(build_daily_brief_user_view(_source(shuffled))) == ["R1", "R0"]


def test_roll_and_close_share_one_mutually_exclusive_card_with_both_metrics() -> None:
    row = _roll(0)
    row.update(recommendation_state="close", notification_eligible=True,
               evaluation_status="evaluable", quote_status="priced",
               metrics={"remaining_max_annualized_return": 0.005, "net_capture_ratio": 0.9, "ask": 0.1, "capital_basis": 10000})
    view = build_daily_brief_user_view(_source([row]))
    assert len(view["positions"]) == 1
    message = render_fixed_report_card_markdown(_source([row]))
    assert "互斥" in message
    assert "90.0%" in message
    assert "20.0%" in message


def test_account_cap_names_the_sixth_omitted_action_and_legacy_key_is_ignored() -> None:
    brief = _source([_close(i) for i in range(6)])
    view = build_daily_brief_user_view(brief, limits={"max_items_per_account": 1, "max_actions_per_priority": 1})
    assert _symbols(view) == ["C0", "C1", "C2", "C3", "C4"]
    assert any("C5" in line and "1" in line for line in view["selection_omissions"])
    message = render_fixed_report_card_markdown(brief)
    assert "C5" in message and "1 条" in message


def test_candidates_round_robin_by_sealed_rank_and_do_not_promote_diff_changes() -> None:
    brief = _source([])
    families = ("sell_put", "covered_call", "combo_yield", "wheel")
    brief["candidates"] = {
        family: [
            _candidate(rank=rank, symbol=f"{family}{rank}", option_type="put",
                       expiration="2026-08-21", strike=100, capacity=1)
            for rank in (2, 1)
        ] for family in families
    }
    # Combo keeps its actual sealed two-leg shape.
    combo = _brief()["candidates"]["combo_yield"][0]
    brief["candidates"]["combo_yield"] = [{**combo, "rank": rank} for rank in (2, 1)]
    view = build_daily_brief_user_view(brief, diff={"changes": []})
    assert [(row["family"], row["rank"]) for row in view["candidates"]] == [
        (family, 1) for family in families
    ] + [(family, 2) for family in families]


def test_zero_account_cap_still_respects_whole_brief_budget() -> None:
    view = build_daily_brief_user_view(_source([_close(i) for i in range(50)]),
                                       limits={"max_close_items": 0})
    assert len(view["positions"]) == 40


@pytest.mark.parametrize("unknown_time", [None, "2026-07-20", "2026-07-20T01:00:00", "invalid"])
def test_unknown_reminder_time_sorts_after_confirmed_time_without_forced_close(unknown_time) -> None:
    rows = [{**_close(i), "recommendation_state": "hold", "decision_basis": "near_expiry_far_otm_hold"} for i in range(2)]
    rows[0]["last_trading_at_utc"] = unknown_time
    rows[1]["last_trading_at_utc"] = "2026-07-21T20:00:00Z"
    brief = _source(rows)
    view = build_daily_brief_user_view(brief)
    assert _symbols(view) == ["C1", "C0"]
    text = render_fixed_report_card_markdown(brief)
    assert "时间待核实" in text
    assert "必须平仓" not in text


def test_wheel_opening_and_status_cards_share_account_and_whole_brief_caps() -> None:
    brief = _source([_close(i) for i in range(4)])
    brief["wheel_batches"] = [
        {"wheel_branch_id": f"wheel-{i}", "symbol": f"W{i}", "direction": "call",
         "shares_remaining": 100, "recommended_contracts": 1, "status": "ready",
         "expiration": "2026-08-21", "strike": 100, "sell_limit": 1.2}
        for i in range(3)
    ]
    view = build_daily_brief_user_view(brief)
    assert len(view["positions"]) == 4
    assert len(view["wheel_batches"]) == 3
    assert not any("Wheel 另有" in line for line in view["selection_omissions"])
    bounded = build_daily_brief_user_view(brief, limits={"max_total_items": 5})
    assert len(bounded["wheel_batches"]) == 1
    assert any("Wheel 另有 2" in line for line in bounded["selection_omissions"])
    brief["positions"] = []
    for row in brief["wheel_batches"]:
        row["recommended_contracts"] = 0
    view = build_daily_brief_user_view(brief, limits={"max_total_items": 2})
    assert len(view["wheel_batches"]) == 2


def test_wheel_cohort_is_one_budget_item_before_account_and_total_caps() -> None:
    from test_daily_decision_brief_renderer import _hk_assigned_wheel_brief

    brief = _hk_assigned_wheel_brief()
    view = build_daily_brief_user_view(brief, delivery_kind="fixed_report")
    assert len(view["wheel_batches"]) == 4
    assert not any("Wheel 另有" in note for note in view["selection_omissions"])
    template = brief["wheel_batches"][0]
    brief["wheel_batches"] = [
        {**deepcopy(template), "wheel_branch_id": f"cohort-{i}"} for i in range(42)
    ] + [brief["wheel_batches"][-1]]
    view = build_daily_brief_user_view(brief, delivery_kind="fixed_report", limits={"max_items_per_account": 0})
    assert len(view["wheel_batches"]) == 2
    assert not any("Wheel 另有" in note for note in view["selection_omissions"])
    assert any("42 批" in row["title"] for row in view["wheel_batches"])


def test_sealed_rank_two_does_not_skip_another_strategys_rank_two() -> None:
    brief = _source([_close(i) for i in range(3)])
    brief["candidates"] = {
        family: [_candidate(rank=rank, symbol=f"{family}{rank}", option_type="put",
                            expiration="2026-08-21", strike=100,
                            capacity=0 if family == "covered_call" and rank == 1 else 1)
                 for rank in (1, 2)]
        for family in ("sell_put", "covered_call")
    }
    # Sealed rank=1 precedes rank=2; same-rank strategy order stays CSP then CC.
    # A zero-capacity CC1 is informational and does not promote CC2 to rank=1.
    assert [(row["family"], row["rank"]) for row in build_daily_brief_user_view(brief)["candidates"]] == [
        ("sell_put", 1), ("sell_put", 2), ("covered_call", 2), ("covered_call", 1),
    ]


def test_next_fixed_report_excludes_candidate_checks_and_send_cutoff() -> None:
    from datetime import datetime, timezone
    from src.application.scan_scheduler import next_daily_brief_fixed_report

    schedule = {"timezone": "Asia/Hong_Kong", "run_window": {"start": "09:30", "end": "16:00"},
                "run_points": {"hourly_minute": 0},
                "gates": [{"type": "before", "timezone": "Asia/Hong_Kong", "time": "15:55"}]}
    assert next_daily_brief_fixed_report(
        schedule_cfg=schedule, now_utc=datetime(2026, 11, 6, 6, 10, tzinfo=timezone.utc),
    ) == "2026-11-06T07:00:00+00:00"
    assert next_daily_brief_fixed_report(
        schedule_cfg=schedule, now_utc=datetime(2026, 11, 6, 7, 50, tzinfo=timezone.utc),
    ) == "2026-11-09T02:00:00+00:00"


def test_independent_close_up_down_sections_keep_opening_candidates() -> None:
    down = [_roll(i, option_type="put") for i in range(7)]
    rows = [*down, *[_close(i) for i in range(2)], _roll(20)]
    brief = _source(rows)
    brief["candidates"] = {"sell_put": [
        _candidate(rank=i + 1, symbol=f"B{i}", option_type="put",
                   expiration="2026-08-21", strike=100, capacity=1)
        for i in range(3)
    ]}
    view = build_daily_brief_user_view(brief)
    assert len(view["positions"]) == 8
    assert len(view["candidates"]) == 3
    assert sum("向下移仓" in row["status"] for row in view["positions"]) == 5
    assert sum("向上移仓" in row["status"] for row in view["positions"]) == 1
    assert any("下移" in note and "2 条" in note for note in view["selection_omissions"])
    text = render_fixed_report_card_markdown(brief)
    assert all(label in text for label in ("平仓", "上移", "下移"))


def test_extra_down_suggestions_do_not_displace_other_sections() -> None:
    def brief(down_count: int) -> dict:
        result = _source([*[_close(i) for i in range(8)], *[_roll(i) for i in range(7)],
                          *[_roll(i + 20, option_type="put") for i in range(down_count)]])
        result["candidates"] = {"sell_put": [
            _candidate(rank=i + 1, symbol=f"B{i}", option_type="put",
                       expiration="2026-08-21", strike=100, capacity=1) for i in range(3)
        ]}
        return result
    first = build_daily_brief_user_view(brief(6))
    crowded = build_daily_brief_user_view(brief(26))
    assert len(first["positions"]) + len(first["candidates"]) == 18
    for section in ("close", "roll_up", "roll_down"):
        assert sum(row["section"] == section for row in first["positions"]) == 5
    assert first["positions"] == crowded["positions"]
    assert first["candidates"] == crowded["candidates"]
    assert any("下移" in note and "21 条" in note for note in crowded["selection_omissions"])


def test_admitted_mutually_exclusive_alternatives_use_one_physical_total_slot() -> None:
    row = _roll(0)
    row.update(recommendation_state="close", notification_eligible=True,
               evaluation_status="evaluable", quote_status="priced",
               metrics=_close(0)["metrics"])
    view = build_daily_brief_user_view(_source([row, _close(1), _roll(2)]), limits={
        "max_close_items": 1, "max_roll_up_items": 1, "max_total_items": 1,
    })
    assert len(view["positions"]) == 1
    assert view["positions"][0]["has_close_alternative"]
    assert view["positions"][0]["is_roll"]
    assert len(view["positions"][0]["roll_identities"]) == 1


def test_section_quota_does_not_leak_unselected_close_or_roll_alternative() -> None:
    row = _roll(0)
    row.update(recommendation_state="close", notification_eligible=True,
               evaluation_status="evaluable", quote_status="priced",
               metrics={"remaining_max_annualized_return": 0.02, "net_capture_ratio": 0.9,
                        "ask": 0.1, "capital_basis": 10000})
    brief = _source([_close(0), row])
    view = build_daily_brief_user_view(brief, limits={"max_close_items": 1})
    roll = next(item for item in view["positions"] if item.get("is_roll"))
    assert "互斥" not in "\n".join(roll["details"])
    assert "同时建议平仓" not in roll["status"]
    assert len(view["positions"]) == 2
    other_roll = _roll(1, annualized=0.8)
    view = build_daily_brief_user_view(_source([row, other_roll]), limits={"max_roll_up_items": 1})
    close = next(item for item in view["positions"] if item["title"].startswith("R0"))
    assert not close.get("is_roll")
    assert not close.get("roll_identities")
    assert "互斥" not in "\n".join(close["details"])


def _urgent_roll_and_close_remainder_brief() -> dict:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief
    from src.application.daily_decision_brief_service import _position_view

    urgent = position_row(lot="a-urgent-event-roll")
    urgent.update(currency="USD", recommendation_state="close", evaluation_status="evaluable",
        quote_status="priced", event_at_utc="2026-07-20T14:20:00Z", **_close(0)["metrics"])
    remainder = {**deepcopy(urgent), "position_lot_id": "b-no-time-close"}
    remainder.pop("roll_advice")
    remainder.pop("event_at_utc")
    brief = _source([_position_view(row, notification_eligible=True, close_actionable=True)
                     for row in (urgent, remainder)])
    brief["source_artifacts"] = []
    return normalize_daily_decision_brief(brief)


@pytest.mark.parametrize("total", [1, 2])
def test_actual_card_deadline_keeps_urgent_roll_before_unknown_close_remainder(total: int) -> None:
    view = build_daily_brief_user_view(_urgent_roll_and_close_remainder_brief(),
        limits={"max_total_items": total}, context={"next_fixed_report_at_utc": "2026-07-20T15:00:00Z"})
    ordered = sorted(view["positions"], key=lambda row: row["selection_rank"])
    assert [row["section"] for row in ordered] == ["roll_up", "close"][:total]
    assert sum(row["lot_count"] for row in ordered) == total


def _cross_target_close_brief() -> dict:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief

    ups = []
    for index in range(3):
        row = position_row(lot=f"shared-old-{index}")
        row.update(currency="USD", notification_eligible=True, recommendation_state="close",
            evaluation_status="evaluable", quote_status="priced", metrics=_close(0)["metrics"])
        target = row["roll_advice"]["candidates"][0]
        target.update(contract=f"NVDA260821C{(105 + index) * 1000:08d}", strike=105 + index,
            strike_increase_pct=(5 + index) / 100, incremental_annualized_return=0.3 - index / 100)
        ups.append(row)
    down = position_row(lot="independent-down", option_type="put")
    down["currency"] = "USD"
    brief = _source([*ups, down, {**_close(10), "currency": "USD"}])
    brief["source_artifacts"] = []
    return normalize_daily_decision_brief(brief)


@pytest.mark.parametrize("total", [2, 3])
def test_close_group_with_multiple_roll_targets_keeps_section_round_robin(total: int) -> None:
    view = build_daily_brief_user_view(_cross_target_close_brief(), limits={"max_total_items": total})
    ordered = sorted(view["positions"], key=lambda row: row["selection_rank"])
    assert [row["section"] for row in ordered] == ["roll_up", "roll_down", "close"][:total]
    assert sum(row["section"] == "roll_up" for row in ordered) == 1


def test_total_forty_is_shared_after_independent_sections_and_can_be_lowered() -> None:
    brief = _source([_close(i) for i in range(50)])
    brief["candidates"] = {"sell_put": [
        _candidate(rank=i + 1, symbol=f"B{i}", option_type="put",
                   expiration="2026-08-21", strike=100, capacity=1)
        for i in range(10)
    ]}
    limits = {"max_close_items": 0, "max_candidates_per_strategy": 10}
    view = build_daily_brief_user_view(brief, limits=limits)
    assert len(view["positions"]) + len(view["candidates"]) == 40
    view = build_daily_brief_user_view(brief, limits={**limits, "max_total_items": 7})
    assert len(view["positions"]) == 7
    assert view["candidates"] == []


def test_missing_required_roll_economics_fails_eligibility_instead_of_zero_default() -> None:
    row = _roll(0)
    row["roll_advice"]["candidates"][0].pop("incremental_annualized_return")
    view = build_daily_brief_user_view(_source([row]))
    assert view["positions"] == []


def test_close_missing_required_economics_is_not_actionable_and_optional_roll_key_sorts_last() -> None:
    close = _close(0)
    close["metrics"].pop("remaining_max_annualized_return")
    assert build_daily_brief_user_view(_source([close]))["positions"] == []
    first, second = _roll(0), _roll(1)
    first["roll_advice"]["candidates"][0].pop("strike_increase_pct")
    assert _symbols(build_daily_brief_user_view(_source([first, second]))) == ["R1", "R0"]


def test_zero_capacity_candidate_is_after_actionable_opening_and_market_caps_are_independent() -> None:
    brief = _source([])
    brief["candidates"] = {"sell_put": [
        _candidate(rank=1, symbol="NO_CAPACITY", option_type="put", expiration="2026-08-21", strike=100, capacity=0),
        _candidate(rank=2, symbol="ACTIONABLE", option_type="put", expiration="2026-08-21", strike=100, capacity=1),
    ]}
    for market in ("US", "HK"):
        brief["market"] = market
        assert [row["symbol"] for row in build_daily_brief_user_view(
            brief, limits={"max_total_items": 1},
        )["candidates"]] == ["ACTIONABLE"]
