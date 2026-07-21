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
    contracts_available: int | None = 1,
    event_risk: dict | None = None,
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
        "metrics": {
            "rank": rank,
            "annualized_return": annualized_return,
            **(
                {"capacity": {"contracts_available": contracts_available}}
                if contracts_available is not None
                else {}
            ),
        },
        **({"event_risk": event_risk} if event_risk is not None else {}),
    }


def _event_risk(state: str, *, date: str | None = None, chain: str = "event-chain-futu") -> dict:
    event = (
        {
            "event_id": "event-q2",
            "event_series_id": "event-series-earnings",
            "event_type": "earnings",
            "event_date": date,
            "occurrence_anchor": "2026|Q2",
            "anchored": True,
        }
        if date
        else None
    )
    return {
        "user_state": state,
        "reason_code": state,
        "reliable": state != "unknown",
        "evidence_chain_id": chain,
        "nearest_event": event,
        "events": [event] if event else [],
        "expiration_relations": (
            {
                "contract": {
                    "expiration": "2026-08-21",
                    "relation": "before_expiration",
                    "days_before_expiration": 16,
                }
            }
            if event
            else {}
        ),
        "in_attention_window": bool(event),
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
    assert {item["change_type"] for item in blocked_change["changes"]} >= {"blocked", "candidate_invalidated"}

    recovered = _brief(revision=2, actions=[_action()])
    recovery_change = diff_daily_decision_briefs(blocked, recovered)
    assert recovery_change["material"] is True
    assert "recovered" in {item["change_type"] for item in recovery_change["changes"]}


def test_diff_marks_p0_add_upgrade_and_high_priority_invalidation() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    empty = _brief(revision=0)
    p0 = _brief(revision=1, actions=[_action(priority="P0")])
    assert "candidate_added" in {item["change_type"] for item in diff_daily_decision_briefs(empty, p0)["changes"]}

    p1 = _brief(revision=0, actions=[_action(priority="P1")])
    upgraded = _brief(revision=1, actions=[_action(priority="P0")])
    assert "candidate_priority_upgraded_to_p0" in {
        item["change_type"] for item in diff_daily_decision_briefs(p1, upgraded)["changes"]
    }

    invalid = _brief(revision=1, actions=[_action(priority="P1", state="invalidated")])
    assert "candidate_invalidated" in {item["change_type"] for item in diff_daily_decision_briefs(p1, invalid)["changes"]}


def test_diff_new_p1_candidate_is_material_but_p2_observe_is_not() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    empty = _brief(revision=0)
    p1 = _brief(revision=1, actions=[_action(priority="P1")])
    p2 = _brief(revision=1, actions=[_action(priority="P2", state="observe")])

    assert diff_daily_decision_briefs(empty, p1)["material"] is True
    assert diff_daily_decision_briefs(empty, p2)["material"] is False


@pytest.mark.parametrize("prior_state", ["blocked", "observe", "invalidated"])
def test_diff_marks_existing_high_priority_action_becoming_active_as_material(prior_state: str) -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    previous = _brief(revision=0, actions=[_action(priority="P0", state=prior_state)])
    current = _brief(revision=1, actions=[_action(priority="P0", state="active")])

    diff = diff_daily_decision_briefs(previous, current)

    assert diff["material"] is True
    assert "candidate_added" in {item["change_type"] for item in diff["changes"]}


def test_diff_marks_existing_p2_action_crossing_into_active_p1_as_material() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    previous = _brief(revision=0, actions=[_action(priority="P2", state="active")])
    current = _brief(revision=1, actions=[_action(priority="P1", state="active")])

    diff = diff_daily_decision_briefs(previous, current)

    assert diff["material"] is True
    assert "candidate_added" in {item["change_type"] for item in diff["changes"]}


def test_diff_capacity_uses_candidate_whole_contracts_not_top_level_cash_noise() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    first = _brief(revision=0, actions=[_action(contracts_available=1)])
    top_level_noise = _brief(
        revision=1,
        actions=[_action(contracts_available=1)],
        put_contracts=9,
        call_contracts=7,
    )
    top_level_noise["capacity"]["sell_put"]["cash_free"] = 20500.0
    assert diff_daily_decision_briefs(first, top_level_noise)["material"] is False

    changed = _brief(revision=2, actions=[_action(contracts_available=2)])
    diff = diff_daily_decision_briefs(first, changed)
    assert diff["material"] is True
    capacity_change = next(
        item for item in diff["changes"] if item["change_type"] == "candidate_capacity_changed"
    )
    assert (capacity_change["before"], capacity_change["after"]) == (1, 2)
    assert capacity_change["action"]["expiration"] == "2026-08-21"
    assert capacity_change["action"]["strike"] == "100"


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

    assert "candidate_priority_downgraded" in {item["change_type"] for item in diff_daily_decision_briefs(p0, p1)["changes"]}
    assert "candidate_priority_downgraded" in {item["change_type"] for item in diff_daily_decision_briefs(p1, p2)["changes"]}


def test_candidate_transition_emits_one_semantic_change_before_capacity() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    previous = _brief(
        revision=0,
        actions=[_action(priority="P1", state="active", contracts_available=2)],
    )
    blocked = _brief(
        revision=1,
        actions=[_action(priority="P0", state="blocked", contracts_available=1)],
    )

    diff = diff_daily_decision_briefs(previous, blocked)

    assert [item["change_type"] for item in diff["changes"]] == ["candidate_invalidated"]


def test_true_close_actions_keep_action_vocabulary() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    close = _action(priority="P0", action_type="close_position")
    added = diff_daily_decision_briefs(_brief(revision=0), _brief(revision=1, actions=[close]))
    assert "p0_added" in {item["change_type"] for item in added["changes"]}

    invalid = deepcopy(close)
    invalid["state"] = "observe"
    removed = diff_daily_decision_briefs(
        _brief(revision=1, actions=[close]),
        _brief(revision=2, actions=[invalid]),
    )
    assert "action_invalidated" in {item["change_type"] for item in removed["changes"]}


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


def test_diff_emits_candidate_bound_event_material_changes() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    previous = _brief(
        revision=0,
        actions=[_action(event_risk=_event_risk("confirmed_none"))],
    )
    current = _brief(
        revision=1,
        actions=[_action(event_risk=_event_risk("confirmed_event", date="2026-08-05"))],
    )
    previous["market_trading_date"] = current["market_trading_date"] = "2026-07-21"

    diff = diff_daily_decision_briefs(previous, current)
    change = next(item for item in diff["changes"] if item["change_type"] == "candidate_event_added")

    assert diff["material"] is True
    assert change["action"]["contract_symbol"] == "NVDA260821P00100000"
    assert change["after_event_risk"]["nearest_event"]["event_date"] == "2026-08-05"


def test_diff_ignores_event_changes_for_never_important_candidate() -> None:
    from domain.domain.daily_decision_brief import diff_daily_decision_briefs

    previous_action = _action(
        priority="P2",
        state="observe",
        event_risk=_event_risk("confirmed_none"),
    )
    current_action = _action(
        priority="P2",
        state="observe",
        event_risk=_event_risk("confirmed_event", date="2026-08-05"),
    )
    previous = _brief(revision=0, actions=[previous_action])
    current = _brief(revision=1, actions=[current_action])
    previous["market_trading_date"] = current["market_trading_date"] = "2026-07-21"

    diff = diff_daily_decision_briefs(previous, current)

    assert diff["material"] is False
    assert not any(item["change_type"].startswith("candidate_event_") for item in diff["changes"])
