from __future__ import annotations

from src.application.ai_decision_advice.projection import (
    project_all_candidates,
    project_one_contract,
)


def _portfolio() -> dict:
    return {
        "symbol_weights": [
            {"symbol": "NVDA", "currency": "USD", "weight": 0.5},
            {"symbol": "AAPL", "currency": "USD", "weight": 0.3},
            {"symbol": "0700.HK", "currency": "HKD", "weight": 0.2},
        ]
    }


def _positions() -> dict:
    return {
        "open_positions": [
            {"symbol": "NVDA", "option_type": "put", "side": "short", "strike": 95, "expiry": "2026-09-18", "contracts": 1},
            {"symbol": "NVDA", "option_type": "call", "side": "long", "strike": 120, "expiry": "2026-09-18", "contracts": 1},
            {"symbol": "AAPL", "option_type": "call", "side": "short", "strike": 210, "expiry": "2026-10-16", "contracts": 1},
        ]
    }


def test_put_projection_facts() -> None:
    out = project_one_contract(
        candidate={"symbol": "NVDA", "strike": 100, "multiplier": 100, "expiry": "2026-09-18"},
        strategy_mode="put",
        portfolio=_portfolio(),
        option_positions=_positions(),
    )
    assert out["direction"] == "assignment_adds_shares"
    assert out["assignment_notional"] == 10000.0
    assert out["contract_shares"] == 100
    assert out["symbol_concentration"]["current_weight"] == 0.5
    assert out["symbol_concentration"]["already_holds_symbol"] is True
    assert out["same_direction_overlay_count"] == 1
    assert out["expiry_overlap_count"] == 2
    assert out["currency_concentration"]["currency"] == "USD"
    assert out["currency_concentration"]["current_weight"] == 0.8


def test_call_projection_facts() -> None:
    out = project_one_contract(
        candidate={"symbol": "AAPL", "strike": 200, "multiplier": 100, "expiry": "2026-10-16"},
        strategy_mode="call",
        portfolio=_portfolio(),
        option_positions=_positions(),
    )
    assert out["direction"] == "call_away_removes_shares"
    assert out["call_away_shares"] == 100
    assert out["same_direction_overlay_count"] == 1
    assert out["assignment_notional"] is None
    assert out["symbol_concentration"]["current_weight"] == 0.3


def test_projection_no_fabricated_after_weight() -> None:
    out = project_one_contract(
        candidate={"symbol": "NVDA", "strike": 100, "multiplier": 100},
        strategy_mode="put",
        portfolio=_portfolio(),
        option_positions={"open_positions": []},
    )
    assert "after_one_contract_weight" not in out["symbol_concentration"]


def test_projection_symbol_not_held() -> None:
    out = project_one_contract(
        candidate={"symbol": "MSFT", "strike": 400, "multiplier": 100},
        strategy_mode="put",
        portfolio=_portfolio(),
        option_positions={"open_positions": []},
    )
    assert out["symbol_concentration"]["current_weight"] == 0.0
    assert out["symbol_concentration"]["already_holds_symbol"] is False


def test_project_all_candidates() -> None:
    candidates = {
        "sell_put": [{"candidate_id": "sp1", "symbol": "NVDA", "strike": 100, "multiplier": 100}],
        "covered_call": [{"candidate_id": "cc1", "symbol": "AAPL", "strike": 200, "multiplier": 100}],
    }
    out = project_all_candidates(
        candidates=candidates,
        portfolio=_portfolio(),
        option_positions=_positions(),
    )
    assert set(out) == {"sp1", "cc1"}
    assert out["sp1"]["strategy_mode"] == "put"
    assert out["cc1"]["strategy_mode"] == "call"
