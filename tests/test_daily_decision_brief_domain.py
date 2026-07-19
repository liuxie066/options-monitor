from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest


def _action(
    *,
    priority: str = "P1",
    state: str = "active",
    action_type: str = "open_candidate",
    symbol: str = "NVDA",
    contract_symbol: str = "NVDA260821P00100000",
    rank: int = 1,
    annualized_return: float = 0.20,
) -> dict:
    return {
        "priority": priority,
        "state": state,
        "action_type": action_type,
        "strategy_family": "sell_put",
        "account": "lx",
        "symbol": symbol,
        "option_type": "put",
        "side": "short",
        "expiration": "2026-08-21",
        "strike": 100.0,
        "contract_symbol": contract_symbol,
        "title": "Sell Put candidate",
        "reason": "passed canonical filter",
        "metrics": {"rank": rank, "annualized_return": annualized_return},
    }


def _brief(
    *,
    revision: int,
    actionability: str = "live_actionable",
    actions: list[dict] | None = None,
    put_contracts: int = 1,
    call_contracts: int = 2,
) -> dict:
    return {
        "schema_version": "daily_decision_brief.v1",
        "market": "US",
        "market_trading_date": "2026-07-20",
        "account": "lx",
        "revision": revision,
        "run_id": f"run-{revision}",
        "generated_at_utc": "2026-07-20T13:40:00+00:00",
        "data_as_of_utc": "2026-07-20T13:39:30+00:00",
        "valid_until_utc": "2026-07-20T20:00:00+00:00",
        "status": "ready" if actionability != "blocked" else "blocked",
        "actionability": actionability,
        "strategy_summary": "manage risk before new positions",
        "actions": actions or [],
        "positions": [],
        "capacity": {
            "sell_put": {"contracts_available": put_contracts, "cash_free": 20000.0},
            "covered_call": {"contracts_available": call_contracts, "shares_available": 200},
        },
        "candidates": {},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def test_action_identity_ignores_price_rank_and_return_changes() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    first = _action(rank=1, annualized_return=0.20)
    second = _action(rank=3, annualized_return=0.15)
    second["metrics"]["mid"] = 2.5

    assert build_daily_brief_action_id(first) == build_daily_brief_action_id(second)


def test_action_identity_normalizes_case_and_strike_representation() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    first = _action()
    second = deepcopy(first)
    second.update({"account": "LX", "symbol": "nvda", "option_type": "PUT", "strike": "100.000"})

    assert build_daily_brief_action_id(first) == build_daily_brief_action_id(second)


def test_normalize_brief_builds_stable_ids_and_rejects_invalid_contracts() -> None:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief

    normalized = normalize_daily_decision_brief(_brief(revision=0, actions=[_action()]))
    assert normalized["brief_id"].startswith("daily-brief-")
    assert normalized["actions"][0]["action_id"].startswith("action-")
    assert normalized["actions"][0]["strike"] == "100"

    invalid = _brief(revision=0)
    invalid["actionability"] = "trade_now"
    with pytest.raises(ValueError, match="unsupported daily brief actionability"):
        normalize_daily_decision_brief(invalid)


def test_effective_actionability_downgrades_expired_live_brief() -> None:
    from domain.domain.daily_decision_brief import effective_daily_brief_actionability

    brief = _brief(revision=0, actions=[_action()])
    assert effective_daily_brief_actionability(
        brief,
        now_utc=datetime(2026, 7, 20, 19, 59, tzinfo=timezone.utc),
    ) == "live_actionable"
    assert effective_daily_brief_actionability(
        brief,
        now_utc=datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc),
    ) == "planning_only"

    missing_expiry = deepcopy(brief)
    missing_expiry["valid_until_utc"] = ""
    assert effective_daily_brief_actionability(missing_expiry) == "planning_only"


def test_diff_marks_blocked_and_recovered_as_material() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    ready = _brief(revision=0, actions=[_action()])
    blocked = _brief(revision=1, actionability="blocked", actions=[])
    blocked_change = diff_daily_decision_briefs(ready, blocked)
    assert blocked_change["material"] is True
    assert {item["change_type"] for item in blocked_change["changes"]} >= {"blocked", "action_invalidated"}

    recovered = _brief(revision=2, actions=[_action()])
    recovery_change = diff_daily_decision_briefs(blocked, recovered)
    assert recovery_change["material"] is True
    assert "recovered" in {item["change_type"] for item in recovery_change["changes"]}


def test_diff_marks_p0_add_upgrade_and_high_priority_invalidation() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    empty = _brief(revision=0)
    p0 = _brief(revision=1, actions=[_action(priority="P0")])
    assert "p0_added" in {item["change_type"] for item in diff_daily_decision_briefs(empty, p0)["changes"]}

    p1 = _brief(revision=0, actions=[_action(priority="P1")])
    upgraded = _brief(revision=1, actions=[_action(priority="P0")])
    assert "priority_upgraded_to_p0" in {
        item["change_type"] for item in diff_daily_decision_briefs(p1, upgraded)["changes"]
    }

    invalid = _brief(revision=1, actions=[_action(priority="P1", state="invalidated")])
    assert "action_invalidated" in {item["change_type"] for item in diff_daily_decision_briefs(p1, invalid)["changes"]}


def test_diff_new_p1_candidate_is_material_but_p2_observe_is_not() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    empty = _brief(revision=0)
    p1 = _brief(revision=1, actions=[_action(priority="P1")])
    p2 = _brief(revision=1, actions=[_action(priority="P2", state="observe")])

    assert diff_daily_decision_briefs(empty, p1)["material"] is True
    assert diff_daily_decision_briefs(empty, p2)["material"] is False


def test_diff_capacity_uses_whole_contracts_not_cash_noise() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    first = _brief(revision=0, put_contracts=1, call_contracts=2)
    cash_only = _brief(revision=1, put_contracts=1, call_contracts=2)
    cash_only["capacity"]["sell_put"]["cash_free"] = 20500.0
    assert diff_daily_decision_briefs(first, cash_only)["material"] is False

    changed = _brief(revision=2, put_contracts=2, call_contracts=1)
    diff = diff_daily_decision_briefs(first, changed)
    assert diff["material"] is True
    assert [item["capacity_kind"] for item in diff["changes"] if item["change_type"] == "capacity_changed"] == [
        "sell_put",
        "covered_call",
    ]


def test_diff_ignores_same_action_price_rank_and_return_changes() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    first = _brief(revision=0, actions=[_action(rank=1, annualized_return=0.20)])
    second = _brief(revision=1, actions=[_action(rank=5, annualized_return=0.11)])
    second["actions"][0]["metrics"]["mid"] = 1.25

    diff = diff_daily_decision_briefs(first, second)
    assert diff["material"] is False
    assert diff["changes"] == []


def test_diff_rejects_cross_market_or_cross_account_comparison() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    first = _brief(revision=0)
    second = _brief(revision=1)
    second["market"] = "HK"
    with pytest.raises(ValueError, match="identity mismatch"):
        diff_daily_decision_briefs(first, second)


def test_normalize_rejects_duplicate_stable_action_identity() -> None:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief

    duplicate = _brief(revision=0, actions=[_action(), _action(rank=2, annualized_return=0.15)])
    with pytest.raises(ValueError, match="duplicate daily brief action_id"):
        normalize_daily_decision_brief(duplicate)


def test_diff_marks_high_priority_downgrade_as_material() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    p0 = _brief(revision=0, actions=[_action(priority="P0")])
    p1 = _brief(revision=1, actions=[_action(priority="P1")])
    p2 = _brief(revision=2, actions=[_action(priority="P2")])

    assert "priority_downgraded" in {item["change_type"] for item in diff_daily_decision_briefs(p0, p1)["changes"]}
    assert "priority_downgraded" in {item["change_type"] for item in diff_daily_decision_briefs(p1, p2)["changes"]}


def test_material_diff_digest_ignores_title_and_reason_copy_changes() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    previous = _brief(revision=0, actions=[_action(priority="P1")])
    first_invalid = _action(priority="P1", state="invalidated")
    first_invalid.update({"title": "候选失效", "reason": "价格越过阈值"})
    second_invalid = deepcopy(first_invalid)
    second_invalid.update({"title": "主行动已失效", "reason": "最新价格不再满足条件"})

    first = _brief(revision=1, actions=[first_invalid])
    second = _brief(revision=2, actions=[second_invalid])
    assert diff_daily_decision_briefs(previous, first)["material_diff_digest"] == diff_daily_decision_briefs(
        previous, second
    )["material_diff_digest"]


def test_daily_brief_digest_handles_non_finite_nested_values_deterministically() -> None:
    from domain.domain.daily_decision_brief import daily_brief_digest

    with_nan = _brief(revision=0)
    with_nan["capacity"]["sell_put"]["cash_free"] = float("nan")
    with_none = _brief(revision=0)
    with_none["capacity"]["sell_put"]["cash_free"] = None

    assert daily_brief_digest(with_nan) == daily_brief_digest(with_none)
