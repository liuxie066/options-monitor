from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.application.agent_tools.daily_brief import read_daily_brief_view
from src.application.agent_tools.position_advice_read_impl import read_position_advice_view
from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success
from test_tick_position_advice_views import position_row, roll_brief


NOW = datetime(2026, 7, 21, 14, 0, 30, tzinfo=timezone.utc)


def read_view(base: Path, *, action=None, now=NOW, account="lx") -> dict:
    daily = read_daily_brief_view(base=base, account=account, market="US", now_utc=now)
    return read_position_advice_view(daily, action=action, now_utc=now)


def test_unified_reader_matches_saved_roll_and_keeps_all_targets(tmp_path: Path) -> None:
    row = position_row()
    lifecycle = persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    output = read_view(tmp_path, action="roll_up")
    assert output["returned_count"] == output["actionable_count"] == 1
    roll = output["rows"][0]
    assert roll["roll_advice"] == lifecycle["brief"]["positions"][0]["roll_advice"]
    assert len(roll["roll_advice"]["candidates"]) == 2
    assert roll["run_id"] == lifecycle["brief"]["run_id"]
    assert roll["revision"] == lifecycle["brief"]["revision"]
    assert read_view(tmp_path, action="roll_down")["rows"] == []
    all_rows = read_view(tmp_path)
    assert {item["action"] for item in all_rows["rows"]} == {"close", "roll_up"}
    assert next(item for item in all_rows["rows"] if item["action"] == "close")["actionable"] is False


def test_close_and_roll_down_actions_share_same_persisted_position(tmp_path: Path) -> None:
    row = position_row(option_type="put")
    row.update(recommendation_state="close", notification_eligible=True, close_actionable=True,
        evaluation_status="priced")
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    output = read_view(tmp_path)
    assert output["actionable_count"] == 2
    assert {item["action"] for item in output["rows"]} == {"close", "roll_down"}
    assert read_view(tmp_path, action="close")["rows"][0]["close_advice"]["metrics"] == row["metrics"]


@pytest.mark.parametrize("limit", [1, 5])
def test_close_actionability_uses_all_qualified_decisions_before_notification_limit(tmp_path: Path, limit) -> None:
    from test_daily_decision_brief_service import (
        _assemble, _config, _write_close_report, _write_labeled_put_candidates,
    )

    rows = [{
        "account": "lx", "position_lot_id": f"lot-{symbol.lower()}", "symbol": symbol,
        "option_type": "put", "expiration": "2026-08-21", "strike": strike,
        "policy_version": "remaining_yield_capture.v3", "decision_evidence_status": "complete",
        "evaluation_status": "priced", "quote_status": "priced", "recommendation_state": "close",
        "assessment_mode": "current", "net_capture_ratio": .9, "capital_basis": strike * 100,
        "remaining_max_annualized_return": .05, "all_in_close_cost": 10,
    } for symbol, strike in (("FIRST", 100), ("SECOND", 101), ("THIRD", 102))]
    rows.append({**rows[0], "symbol": "UNQUALIFIED", "position_lot_id": "bad-lot",
        "decision_evidence_status": "not_evaluable"})
    account_dir = _write_labeled_put_candidates(tmp_path, header_only=True)
    _write_close_report(account_dir, rows)
    config = _config()
    config["close_advice"] = {"enabled": True, "max_items_per_account": limit}
    brief = _assemble(tmp_path, config=config)
    persist_daily_decision_brief_success(base=tmp_path, brief=brief)
    now = datetime(2026, 7, 17, 14, 0, 30, tzinfo=timezone.utc)
    output = read_view(tmp_path, action="close", now=now)

    assert output["returned_count"] == 4
    assert output["actionable_count"] == 3
    assert {row["symbol"] for row in output["rows"] if row["actionable"]} == {"FIRST", "SECOND", "THIRD"}
    assert sum(row["close_advice"]["notification_eligible"] for row in output["rows"]) == min(limit, 3)
    assert next(row for row in output["rows"] if row["symbol"] == "UNQUALIFIED")["actionable"] is False


def test_historical_close_without_decision_qualification_is_not_upgraded(tmp_path: Path) -> None:
    row = position_row()
    row.update(recommendation_state="close", notification_eligible=True, evaluation_status="priced")
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    assert read_view(tmp_path, action="close")["actionable_count"] == 0


@pytest.mark.parametrize("expiry", ["2026-07-21T13:59:59Z", None])
def test_live_brief_with_expired_or_missing_roll_freshness_is_not_actionable(tmp_path: Path, expiry) -> None:
    row = position_row()
    if expiry is None:
        row["roll_advice"].pop("actionable_until_utc")
    else:
        row["roll_advice"]["actionable_until_utc"] = expiry
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    result = read_view(tmp_path, action="roll_up")["rows"][0]
    assert result["effective_actionability"] == "live_actionable"
    assert result["actionable"] is False
    assert "roll_advice_stale_or_freshness_missing" in result["reason_codes"]
    assert result["roll_advice"] == row["roll_advice"]


def test_stale_read_preserves_result_but_never_actionable(tmp_path: Path) -> None:
    row = position_row()
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    output = read_view(tmp_path, now=datetime(2026, 7, 22, tzinfo=timezone.utc))
    assert output["actionable_count"] == 0
    assert all(item["effective_actionability"] == "planning_only" for item in output["rows"])
    roll = next(item for item in output["rows"] if item["action"] == "roll_up")
    assert roll["roll_advice"] == row["roll_advice"]
    assert roll["freshness"]["effective_actionability"] == "planning_only"


@pytest.mark.parametrize("status,outcome", [("disabled", "unable"), ("unavailable", "unable"),
    ("success", "none"), ("success", "not_applicable")])
def test_unavailable_disabled_and_success_none_remain_distinct(tmp_path: Path, status, outcome) -> None:
    row = position_row()
    row["roll_advice"].update(status=status, outcome=outcome, candidates=[])
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    result = read_view(tmp_path, action="roll_up")["rows"][0]
    assert (result["status"], result["outcome"]) == (status, outcome)
    assert result["actionable"] is False


def test_missing_roll_column_is_unavailable_not_successful_empty(tmp_path: Path) -> None:
    row = position_row()
    row.pop("roll_advice")
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    result = read_view(tmp_path, action="roll_up")["rows"][0]
    assert result["roll_advice"] is None
    assert result["status"] == "unavailable" and result["outcome"] is None
    assert result["reason_codes"] == ["roll_advice_missing"]


def test_missing_brief_and_valid_empty_positions_are_distinct(tmp_path: Path) -> None:
    missing = read_view(tmp_path)
    assert missing["rows"] == []
    assert missing["scopes"][0]["status"] == "state_invalid"  # canonical reader: history_missing
    assert missing["status"] != "available"
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[]))
    empty = read_view(tmp_path)
    assert empty["rows"] == [] and empty["scopes"][0]["status"] == "available"


@pytest.mark.parametrize("credit", [-5, 0])
def test_historical_debit_or_neutral_is_preserved_only_as_nonactionable_result(tmp_path: Path, credit) -> None:
    row = position_row(credit=credit)
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    output = read_view(tmp_path, action="roll_up")
    result = output["rows"][0]
    assert result["actionable"] is False
    assert result["status"] == "unavailable"
    assert result["roll_advice"] == row["roll_advice"]
    assert "roll_advice_not_eligible" in result["reason_codes"]


def test_account_filter_keeps_old_lots_isolated(tmp_path: Path) -> None:
    for account in ("lx", "other"):
        row = position_row(lot=account + "-lot")
        row["roll_advice"]["account"] = account
        persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row], account=account))
    output = read_view(tmp_path, account="other", action="roll_up")
    assert len(output["rows"]) == 1
    assert output["rows"][0]["account"] == "other"
    assert output["rows"][0]["position_lot_id"] == "other-lot"


def test_registered_tool_reuses_daily_brief_facade_without_quotes_or_writes(monkeypatch, tmp_path: Path) -> None:
    from src.application.agent_tool_registry import AGENT_TOOL_REGISTRY
    from src.application.agent_tools import daily_brief

    row = position_row()
    persist_daily_decision_brief_success(base=tmp_path, brief=roll_brief(base=tmp_path, rows=[row]))
    view = read_daily_brief_view(base=tmp_path, account="lx", market="US", now_utc=NOW)
    seen = []
    def fake_reader(payload):
        seen.append(payload)
        return deepcopy(view), [], {"source": "persisted"}
    monkeypatch.setattr(daily_brief, "query_daily_brief_tool_view", fake_reader)
    tool = AGENT_TOOL_REGISTRY["position_advice_read"]
    assert tool.is_pure_read() and tool.side_effects == ()
    tool.validate_input({"account": "lx", "market": "US", "action": "roll_up"})
    with pytest.raises(Exception, match="action"):
        tool.validate_input({"action": "debit_roll"})
    output, warnings, metadata = tool.handler({"account": "lx", "market": "US", "action": "roll_up"})
    assert seen == [{"account": "lx", "market": "US"}]
    assert output == read_position_advice_view(view, action="roll_up")
    assert warnings == [] and metadata == {"source": "persisted"}
    for old_tool in ("close_advice_read", "position_advice_evaluate", "short_put_roll_candidates", "covered_call_roll_candidates"):
        assert old_tool in AGENT_TOOL_REGISTRY
