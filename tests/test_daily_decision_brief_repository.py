from __future__ import annotations

from pathlib import Path


def _action() -> dict:
    return {
        "priority": "P1",
        "state": "active",
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "expiration": "2026-08-21",
        "strike": 100,
        "contract_symbol": "NVDA260821P00100000",
        "metrics": {"mid": 1.0, "capacity": {"contracts_available": 1}},
    }


def _brief(*, run_id: str, actions: list[dict] | None = None) -> dict:
    return {
        "market": "US",
        "market_trading_date": "2026-07-17",
        "account": "lx",
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-17T13:40:00+00:00",
        "data_as_of_utc": "2026-07-17T13:39:00+00:00",
        "valid_until_utc": "2026-07-17T20:00:00+00:00",
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": "test",
        "actions": list(actions or []),
        "positions": [],
        "capacity": {},
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def test_success_persistence_reconciles_evidence_hold_inside_current_lock(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success

    first = persist_daily_decision_brief_success(
        base=tmp_path,
        brief=_brief(run_id="run-a", actions=[_action()]),
    )
    degraded = _brief(run_id="run-b")
    degraded["status"] = "degraded"
    degraded["data_gaps"] = [
        {
            "scope": "strategy",
            "market": "US",
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "reason": "required_data_snapshot_unavailable",
        }
    ]

    second = persist_daily_decision_brief_success(base=tmp_path, brief=degraded)

    assert second["current_revision"] == 1
    assert second["brief"]["actions"][0]["action_id"] == first["brief"]["actions"][0]["action_id"]
    assert second["brief"]["actions"][0]["evidence_state"] == "unavailable"
    assert second["current_candidate_identities"] == []
    assert second["newly_detected_candidate_identities"] == []
