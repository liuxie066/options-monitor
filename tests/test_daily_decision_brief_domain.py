from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys

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
        "title": "CSP candidate",
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


def _combo_action(
    *,
    symbol: str = "NVDA",
    pair_id: str | None = "pair-nvda-100-110",
    put_contract: str = "NVDA260821P00100000",
    call_contract: str = "NVDA260821C00110000",
    strategy_group_id: str = "",
    state: str = "active",
) -> dict:
    action = {
        "priority": "P1",
        "state": state,
        "action_type": "open_combo_yield",
        "strategy_family": "combo_yield",
        "account": "lx",
        "symbol": symbol,
        "option_type": "",
        "side": "",
        "expiration": "2026-08-21",
        "strike": 100,
        "contract_symbol": put_contract,
        "strategy_group_id": strategy_group_id,
        "leg_role": "pair",
        "metrics": {
            "put_contract_symbol": put_contract,
            "call_contract_symbol": call_contract,
            "capacity": {"contracts_available": 1},
        },
    }
    if pair_id is not None:
        action["candidate_pair_id"] = pair_id
    return action


def _legacy_combo_action_id(action: dict) -> str:
    identity = {
        "action_type": str(action.get("action_type") or "").strip().lower(),
        "strategy_family": str(action.get("strategy_family") or "").strip().lower(),
        "account": str(action.get("account") or "").strip().lower(),
        "symbol": str(action.get("symbol") or "").strip().upper(),
        "option_type": str(action.get("option_type") or "").strip().lower(),
        "side": str(action.get("side") or "").strip().lower(),
        "expiration": str(action.get("expiration") or "").strip(),
        "strike": str(action.get("strike") or "").strip(),
        "contract_symbol": str(action.get("contract_symbol") or "").strip().upper(),
        "position_lot_id": str(action.get("position_lot_id") or "").strip(),
        "strategy_group_id": str(action.get("strategy_group_id") or "").strip(),
        "leg_role": str(action.get("leg_role") or "").strip().lower(),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return "action-" + digest[:24]


def _combo_candidate_item(
    *,
    symbol: str = "NVDA",
    pair_id: str | None = "pair-nvda-100-110",
    put_contract: str = "NVDA260821P00100000",
    call_contract: str = "NVDA260821C00110000",
    strategy_group_id: str = "",
) -> dict:
    representative = {
        "symbol": symbol,
        "strategy_family": "combo_yield",
        "structure_mode": "same_expiry_pair",
        "put_contract_symbol": put_contract,
        "call_contract_symbol": call_contract,
        "put_expiration": "2026-08-21",
        "call_expiration": "2026-08-21",
        "put_strike": 100,
        "call_strike": 110,
        "currency": "USD",
        "multiplier": 100,
        "strategy_group_id": strategy_group_id,
        "capacity": {"contracts_available": 1},
    }
    if pair_id is not None:
        representative["candidate_pair_id"] = pair_id
    return {
        "identity": f"candidate:v1:lx:US:{symbol}:combo_yield",
        "symbol": symbol,
        "strategy_family": "combo_yield",
        "representative": representative,
        "contract_count": 1,
    }


def _combo_brief(
    *,
    revision: int,
    actions: list[dict],
    candidate_items: list[dict] | None = None,
    data_gaps: list[dict] | None = None,
) -> dict:
    brief = _brief(revision=revision, actions=actions)
    brief["candidate_index"] = list(candidate_items or [])
    brief["candidates"] = {"combo_yield": []}
    brief["data_gaps"] = list(data_gaps or [])
    return brief


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


def test_wheel_candidate_identity_is_scoped_to_stock_lot() -> None:
    from domain.domain.daily_decision_brief import (
        build_daily_brief_candidate_identity,
        normalize_daily_decision_brief,
    )

    assert build_daily_brief_candidate_identity(
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="wheel",
        position_lot_id="stock-lot-1",
    ) == "candidate:v1:lx:US:NVDA:wheel:stock-lot-1"
    with pytest.raises(ValueError, match="position_lot_id"):
        build_daily_brief_candidate_identity(
            account="lx",
            market="US",
            symbol="NVDA",
            strategy_family="wheel",
        )
    assert build_daily_brief_candidate_identity(
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="wheel",
        wheel_branch_id="put-branch-1",
    ) == "candidate:v1:lx:US:NVDA:wheel:put-branch-1"

    action = _action()
    action.update(
        {
            "strategy_family": "wheel",
            "option_type": "call",
            "contract_symbol": "NVDA260821C00110000",
            "position_lot_id": "stock-lot-1",
        }
    )
    candidate = normalize_daily_decision_brief(
        _brief(revision=0, actions=[action])
    )["candidate_index"][0]
    assert candidate["identity"] == "candidate:v1:lx:US:NVDA:wheel:stock-lot-1"
    assert candidate["representative"]["position_lot_id"] == "stock-lot-1"

    from domain.domain.wheel import deterministic_wheel_branch_id

    put_branch_id = deterministic_wheel_branch_id("lx", "assignment-1", "put")
    assert ":" not in put_branch_id
    assert build_daily_brief_candidate_identity(
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="wheel",
        wheel_branch_id=put_branch_id,
    ).endswith(f":{put_branch_id}")


def test_legacy_wheel_call_branch_alias_preserves_action_identity() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    legacy = _action()
    legacy.update(
        {
            "strategy_family": "wheel",
            "option_type": "call",
            "contract_symbol": "NVDA260821C00110000",
            "position_lot_id": "stock-lot-1",
        }
    )
    aliased = {**legacy, "wheel_branch_id": "stock-lot-1"}
    put = {**legacy, "wheel_branch_id": "wheel-put-1"}

    assert build_daily_brief_action_id(aliased) == build_daily_brief_action_id(legacy)
    assert build_daily_brief_action_id(put) != build_daily_brief_action_id(legacy)


def test_action_identity_normalizes_case_and_strike_representation() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    first = _action()
    second = deepcopy(first)
    second.update({"account": "LX", "symbol": "nvda", "option_type": "PUT", "strike": "100.000"})

    assert build_daily_brief_action_id(first) == build_daily_brief_action_id(second)


def test_combo_action_identity_adds_only_pair_and_preserves_ordinary_frozen_ids() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_action_id

    first = _combo_action()
    second = _combo_action(pair_id="pair-nvda-100-111", call_contract="NVDA260821C00111000")
    noisy = deepcopy(first)
    noisy["metrics"].update({"rank": 9, "mid": 2.75})
    noisy["run_id"] = "another-run"

    assert build_daily_brief_action_id(first) != build_daily_brief_action_id(second)
    assert build_daily_brief_action_id(first) == build_daily_brief_action_id(noisy)
    with pytest.raises(ValueError, match="candidate_pair_id"):
        build_daily_brief_action_id(_combo_action(pair_id=None))

    ordinary = {
        "csp": _action(),
        "covered_call": {
            **_action(),
            "strategy_family": "covered_call",
            "option_type": "call",
            "strike": 110,
            "contract_symbol": "NVDA260821C00110000",
        },
        "close": {
            **_action(priority="P0", action_type="close_position"),
            "position_lot_id": "lot-baseline-put-1",
        },
    }
    assert {
        name: build_daily_brief_action_id(action)
        for name, action in ordinary.items()
    } == {
        "csp": "action-549581c47d9d4b532d021d4e",
        "covered_call": "action-49fe4dec6252b50a32f3f628",
        "close": "action-a8d565d8ea34ecbc4107248c",
    }


def test_strict_and_persisted_combo_action_id_validation_are_separate() -> None:
    from domain.domain.daily_decision_brief import (
        build_daily_brief_action_id,
        normalize_daily_decision_brief,
        normalize_persisted_daily_decision_brief,
    )

    current_action = _combo_action(strategy_group_id="real-group-1")
    current_action["action_id"] = build_daily_brief_action_id(current_action)
    current = _combo_brief(
        revision=0,
        actions=[current_action],
        candidate_items=[_combo_candidate_item(strategy_group_id="real-group-1")],
    )
    assert normalize_daily_decision_brief(current)["actions"][0]["action_id"] == current_action["action_id"]

    legacy_action = _combo_action(pair_id=None, strategy_group_id="pair-nvda-100-110")
    legacy_action["action_id"] = _legacy_combo_action_id(legacy_action)
    legacy = _combo_brief(
        revision=0,
        actions=[legacy_action],
        candidate_items=[
            _combo_candidate_item(
                pair_id="pair-nvda-100-110",
                strategy_group_id="pair-nvda-100-110",
            )
        ],
    )
    with pytest.raises(ValueError, match="candidate_pair_id"):
        normalize_daily_decision_brief(legacy)
    persisted = normalize_persisted_daily_decision_brief(legacy)
    assert persisted["actions"][0]["action_id"] == legacy_action["action_id"]
    assert "candidate_pair_id" not in persisted["actions"][0]

    tampered = deepcopy(legacy)
    tampered["actions"][0]["contract_symbol"] = "NVDA260821P00101000"
    with pytest.raises(ValueError, match="supported algorithm"):
        normalize_persisted_daily_decision_brief(tampered)


def test_persisted_combo_representative_accepts_old_group_shape_read_only() -> None:
    from domain.domain.daily_decision_brief import (
        normalize_daily_decision_brief,
        normalize_persisted_daily_decision_brief,
    )

    legacy_action = _combo_action(pair_id=None, strategy_group_id="old-group")
    legacy_action["action_id"] = _legacy_combo_action_id(legacy_action)
    legacy = _combo_brief(
        revision=0,
        actions=[legacy_action],
        candidate_items=[_combo_candidate_item(pair_id=None, strategy_group_id="old-group")],
    )
    normalized = normalize_persisted_daily_decision_brief(legacy)
    representative = normalized["candidate_index"][0]["representative"]
    assert representative["strategy_group_id"] == "old-group"
    assert "candidate_pair_id" not in representative
    with pytest.raises(ValueError, match="candidate_pair_id"):
        normalize_daily_decision_brief(legacy)


@pytest.mark.parametrize("raw_pair", [None, "  pair-nvda-100-110  "])
def test_persisted_legacy_combo_preserves_raw_pair_representation_for_digest(
    raw_pair: str | None,
) -> None:
    from domain.domain.daily_decision_brief import (
        daily_brief_compatible_digests,
        normalize_persisted_daily_decision_brief,
    )

    action = _combo_action(pair_id=None, strategy_group_id="pair-nvda-100-110")
    action["candidate_pair_id"] = raw_pair
    action["action_id"] = _legacy_combo_action_id(action)
    raw = _combo_brief(
        revision=0,
        actions=[action],
        candidate_items=[
            _combo_candidate_item(strategy_group_id="pair-nvda-100-110")
        ],
    )

    compatible_before = daily_brief_compatible_digests(raw)
    normalized = normalize_persisted_daily_decision_brief(raw)

    assert normalized["actions"][0]["candidate_pair_id"] == raw_pair
    assert daily_brief_compatible_digests(normalized) == compatible_before


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


def test_candidate_evidence_hold_lifecycle_preserves_identity_without_false_invalidation() -> None:
    from domain.domain.daily_decision_brief import (
        diff_daily_decision_briefs,
        normalize_daily_decision_brief,
        reconcile_daily_decision_brief_evidence,
    )

    active = normalize_daily_decision_brief(
        _brief(revision=0, actions=[_action()])
    )
    unavailable_source = _brief(revision=1)
    unavailable_source["status"] = "degraded"
    unavailable_source["data_gaps"] = [
        {
            "scope": "strategy",
            "market": "US",
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "reason": "empty_chain",
        }
    ]
    held = reconcile_daily_decision_brief_evidence(
        active,
        unavailable_source,
    )
    held_action = held["actions"][0]

    assert held_action["action_id"] == active["actions"][0]["action_id"]
    assert held_action["state"] == "observe"
    assert held_action["evidence_state"] == "unavailable"
    first_diff = diff_daily_decision_briefs(active, held)
    assert [item["change_type"] for item in first_diff["changes"]] == [
        "candidate_evidence_unavailable"
    ]

    held_again = reconcile_daily_decision_brief_evidence(
        held,
        {**unavailable_source, "revision": 2, "run_id": "run-2"},
    )
    assert diff_daily_decision_briefs(held, held_again)["changes"] == []

    recovered = normalize_daily_decision_brief(
        _brief(revision=3, actions=[_action()])
    )
    assert [
        item["change_type"]
        for item in diff_daily_decision_briefs(held_again, recovered)["changes"]
    ] == ["candidate_evidence_recovered"]

    absent = normalize_daily_decision_brief(_brief(revision=3))
    assert [
        item["change_type"]
        for item in diff_daily_decision_briefs(held_again, absent)["changes"]
    ] == ["candidate_invalidated"]


def test_legacy_combo_hold_aligns_to_current_pair_and_recovers_with_real_ids() -> None:
    from domain.domain.daily_decision_brief import (
        build_daily_brief_action_id,
        diff_daily_decision_briefs,
        normalize_persisted_daily_decision_brief,
    )

    legacy = _combo_action(pair_id=None, strategy_group_id="pair-nvda-100-110", state="observe")
    legacy.update(
        {
            "action_id": _legacy_combo_action_id(legacy),
            "evidence_state": "unavailable",
            "evidence_gap_key": "US:NVDA:combo_yield:snapshot_unavailable",
            "evidence_reason": "snapshot_unavailable",
        }
    )
    current = _combo_action()
    current["action_id"] = build_daily_brief_action_id(current)
    previous_brief = _combo_brief(
        revision=0,
        actions=[legacy],
        candidate_items=[_combo_candidate_item(strategy_group_id="pair-nvda-100-110")],
    )
    current_brief = _combo_brief(
        revision=1,
        actions=[current],
        candidate_items=[_combo_candidate_item()],
    )

    normalized_previous = normalize_persisted_daily_decision_brief(previous_brief)
    changes = diff_daily_decision_briefs(normalized_previous, current_brief)["changes"]
    assert [item["change_type"] for item in changes] == ["candidate_evidence_recovered"]
    assert changes[0]["before_action_id"] == legacy["action_id"]
    assert changes[0]["after_action_id"] == current["action_id"]
    assert changes[0]["action"]["action_id"] == current["action_id"]


def test_reconcile_combo_alignment_holds_only_the_still_missing_pair() -> None:
    from domain.domain.daily_decision_brief import (
        build_daily_brief_action_id,
        reconcile_daily_decision_brief_evidence,
    )

    nvda_legacy = _combo_action(pair_id=None, strategy_group_id="fake-group-nvda")
    nvda_legacy["action_id"] = _legacy_combo_action_id(nvda_legacy)
    amd_legacy = _combo_action(
        symbol="AMD",
        pair_id=None,
        put_contract="AMD260821P00100000",
        call_contract="AMD260821C00110000",
        strategy_group_id="fake-group-amd",
    )
    amd_legacy["action_id"] = _legacy_combo_action_id(amd_legacy)
    previous = _combo_brief(
        revision=0,
        actions=[nvda_legacy, amd_legacy],
        candidate_items=[
            _combo_candidate_item(strategy_group_id="fake-group-nvda"),
            _combo_candidate_item(
                symbol="AMD",
                pair_id="pair-amd-100-110",
                put_contract="AMD260821P00100000",
                call_contract="AMD260821C00110000",
                strategy_group_id="fake-group-amd",
            ),
        ],
    )
    nvda_current = _combo_action(strategy_group_id="real-trade-group")
    nvda_current["action_id"] = build_daily_brief_action_id(nvda_current)
    current = _combo_brief(
        revision=1,
        actions=[nvda_current],
        candidate_items=[_combo_candidate_item(strategy_group_id="real-trade-group")],
        data_gaps=[
            {
                "market": "US",
                "symbol": "AMD",
                "strategy_family": "combo_yield",
                "reason": "snapshot_unavailable",
            },
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "combo_yield",
                "reason": "other_pair_partial_gap",
            },
        ],
    )

    reconciled = reconcile_daily_decision_brief_evidence(previous, current)

    assert [item["symbol"] for item in reconciled["actions"]] == ["NVDA", "AMD"]
    held = reconciled["actions"][1]
    assert held["action_id"] == amd_legacy["action_id"]
    assert held["state"] == "observe"
    assert held["evidence_reason"] == "snapshot_unavailable"
    assert held["strategy_group_id"] == "fake-group-amd"


@pytest.mark.parametrize("conflict", ["pair_binding", "missing_legs", "ambiguous"])
def test_combo_cross_algorithm_alignment_never_guesses(conflict: str) -> None:
    from domain.domain.daily_decision_brief import (
        build_daily_brief_action_id,
        diff_daily_decision_briefs,
    )

    legacy = _combo_action(pair_id=None, strategy_group_id="legacy-group")
    legacy["action_id"] = _legacy_combo_action_id(legacy)
    previous_actions = [legacy]
    current = _combo_action()
    current["action_id"] = build_daily_brief_action_id(current)
    current_items = [_combo_candidate_item()]
    if conflict == "pair_binding":
        current["candidate_pair_id"] = "pair-conflicting"
        current["action_id"] = build_daily_brief_action_id(current)
        current_items = [_combo_candidate_item(pair_id="pair-conflicting")]
    elif conflict == "missing_legs":
        legacy["metrics"].pop("call_contract_symbol")
        legacy["action_id"] = _legacy_combo_action_id(legacy)
    else:
        duplicate = deepcopy(legacy)
        duplicate["strategy_group_id"] = "another-legacy-group"
        duplicate["action_id"] = _legacy_combo_action_id(duplicate)
        previous_actions.append(duplicate)

    previous = _combo_brief(
        revision=0,
        actions=previous_actions,
        candidate_items=[_combo_candidate_item(strategy_group_id="legacy-group")],
    )
    current_brief = _combo_brief(
        revision=1,
        actions=[current],
        candidate_items=current_items,
    )
    change_types = {
        item["change_type"]
        for item in diff_daily_decision_briefs(previous, current_brief)["changes"]
    }
    assert change_types == {"candidate_added", "candidate_invalidated"}




def test_candidate_identity_is_canonical_stable_and_contract_independent() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity

    first = build_daily_brief_candidate_identity(
        account="LX", market="hk", symbol="POP", strategy_family="sell_put"
    )
    second = build_daily_brief_candidate_identity(
        account="lx", market="HK", symbol="9992.HK", strategy_family="sell_put"
    )

    assert first == second == "candidate:v1:lx:HK:9992.HK:sell_put"
    assert build_daily_brief_candidate_identity(
        account="lx", market="US", symbol="NVDA", strategy_family="sell_call"
    ) == "candidate:v1:lx:US:NVDA:covered_call"


def test_candidate_identity_is_stable_across_python_hash_seeds() -> None:
    code = (
        "from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity as f;"
        "print(f(account='lx', market='HK', symbol='9992.HK', strategy_family='sell_put'))"
    )
    outputs = {
        subprocess.check_output(
            [sys.executable, "-c", code],
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).strip()
        for seed in ("1", "987654")
    }

    assert outputs == {"candidate:v1:lx:HK:9992.HK:sell_put"}


def test_candidate_identity_rejects_cross_market_or_unknown_family() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity

    with pytest.raises(ValueError, match="does not belong"):
        build_daily_brief_candidate_identity(
            account="lx", market="HK", symbol="NVDA", strategy_family="sell_put"
        )
    with pytest.raises(ValueError, match="unsupported candidate strategy"):
        build_daily_brief_candidate_identity(
            account="lx", market="US", symbol="NVDA", strategy_family="close_advice"
        )


def test_non_live_brief_cannot_expose_alertable_candidate_index() -> None:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief

    planning = _brief(
        revision=0,
        actionability="planning_only",
        actions=[_action()],
    )
    assert normalize_daily_decision_brief(planning)["candidate_index"] == []

    planning["candidate_index"] = [
        {
            "identity": "candidate:v1:lx:US:NVDA:sell_put",
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "representative": {
                "contract_symbol": "NVDA260821P00100000",
                "expiration": "2026-08-21",
                "strike": 100,
                "capacity": {"contracts_available": 1},
            },
            "contract_count": 1,
        }
    ]
    with pytest.raises(ValueError, match="only valid for live_actionable"):
        normalize_daily_decision_brief(planning)


def test_explicit_candidate_index_rejects_ineligible_representative() -> None:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief

    payload = _brief(revision=0)
    payload["candidate_index"] = [
        {
            "identity": "candidate:v1:lx:US:NVDA:sell_put",
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "representative": {},
            "contract_count": 1,
        }
    ]
    with pytest.raises(ValueError, match="capacity must be at least one"):
        normalize_daily_decision_brief(payload)

    payload["candidate_index"][0]["representative"] = {
        "contract_symbol": "NVDA260821P00100000",
        "expiration": "2026-08-21",
        "strike": 100,
        "capacity": {"contracts_available": 0},
    }
    with pytest.raises(ValueError, match="capacity must be at least one"):
        normalize_daily_decision_brief(payload)


def test_old_brief_without_additive_fields_remains_readable() -> None:
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief

    normalized = normalize_daily_decision_brief(_brief(revision=0, actions=[_action()]))

    assert normalized["funds"] == {
        "as_of_utc": "",
        "cash_total_by_currency": {},
        "option_opening_available_by_currency": {},
        "available": False,
        "reason": "not_recorded",
    }
    assert normalized["candidate_index"][0]["identity"] == "candidate:v1:lx:US:NVDA:sell_put"
    assert normalized["candidate_index"][0]["contract_count"] == 1


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


def test_daily_brief_digest_compatibility_preserves_exact_retired_overlay_shape() -> None:
    from domain.domain.daily_decision_brief import (
        daily_brief_compatible_digests,
        normalize_daily_decision_brief,
    )

    historical = _brief(revision=0)
    historical["ai_decision_advice"] = {"status": "completed", "opaque": {"value": 1}}
    historical["ai_decision_advice_evidence_index"] = {"symbols": []}
    compatible = daily_brief_compatible_digests(historical)
    assert len(compatible) == 2

    materialized = normalize_daily_decision_brief(historical)
    assert "ai_decision_advice" not in materialized
    assert "ai_decision_advice_evidence_index" not in materialized
    assert daily_brief_compatible_digests(materialized) == (compatible[0],)

    tampered = deepcopy(historical)
    tampered["strategy_summary"] = "different strategy facts"
    assert compatible[1] not in daily_brief_compatible_digests(tampered)

    tampered_overlay = deepcopy(historical)
    tampered_overlay["ai_decision_advice"]["opaque"]["value"] = 2
    assert compatible[1] not in daily_brief_compatible_digests(tampered_overlay)


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


def test_daily_brief_notification_decision_matrix() -> None:
    from domain.domain.daily_decision_brief import decide_daily_brief_notification

    cases = [
        ({"ran_scan": False, "pipeline_reliable": False, "fixed_due": False, "pending_candidate_identities": [], "retryable_envelope_kind": "fixed_report"}, "retry_exact"),
        ({"ran_scan": False, "pipeline_reliable": False, "fixed_due": False, "pending_candidate_identities": []}, "none"),
        ({"ran_scan": True, "pipeline_reliable": False, "fixed_due": True, "pending_candidate_identities": []}, "fixed_failure"),
        ({"ran_scan": True, "pipeline_reliable": False, "fixed_due": False, "pending_candidate_identities": ["candidate:v1:lx:US:NVDA:sell_put"]}, "none"),
        ({"ran_scan": True, "pipeline_reliable": True, "fixed_due": True, "pending_candidate_identities": ["candidate:v1:lx:US:NVDA:sell_put"]}, "fixed_report"),
        ({"ran_scan": True, "pipeline_reliable": True, "fixed_due": False, "pending_candidate_identities": ["candidate:v1:lx:US:NVDA:sell_put"]}, "candidate_alert"),
        ({"ran_scan": True, "pipeline_reliable": True, "fixed_due": False, "pending_candidate_identities": []}, "none"),
    ]

    for inputs, expected in cases:
        assert decide_daily_brief_notification(**inputs)["action"] == expected
