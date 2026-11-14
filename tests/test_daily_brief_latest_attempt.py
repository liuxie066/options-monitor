from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from test_daily_decision_brief_agent_tool import _brief
from src.application.agent_tools.daily_brief import read_daily_brief_view
from src.application.daily_decision_brief_repository import (
    persist_daily_decision_brief_failure,
    persist_daily_decision_brief_success,
)
from src.infrastructure.decision_history_sqlite import history_path


NOW = datetime(2026, 7, 19, 15, 0, tzinfo=timezone.utc)


def _failure(base: Path, *, account: str = "lx", run_id: str = "failed-latest") -> dict:
    brief = _brief(run_id=run_id)
    brief.update(
        account=account,
        generated_at_utc="2026-07-19T14:40:00+00:00",
        status="blocked",
        actionability="blocked",
        data_gaps=[{"scope": "source", "reason": "quote_unavailable"}],
    )
    return persist_daily_decision_brief_failure(base=base, brief=brief)


def _read(base: Path, **kwargs) -> dict:
    return read_daily_brief_view(base=base, account="lx", market="US", now_utc=NOW, **kwargs)


def test_latest_preserves_success_and_exposes_newer_failure_without_writing(tmp_path):
    saved = persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="last-good"))
    failed = _failure(tmp_path)
    database = history_path(tmp_path)
    original = database.read_bytes()

    result = _read(tmp_path)

    assert result["available"] is True
    assert result["brief"] == saved["brief"]
    assert result["effective_actionability"] == "live_actionable"
    attempt = result["latest_attempt"]
    assert attempt["available"] is True
    assert attempt["reason"] == "scan_failed"
    assert attempt["run_id"] == failed["run_id"]
    assert attempt["revision"] == failed["revision"]
    assert attempt["market_trading_date"] == "2026-07-19"
    assert attempt["status"] == "blocked"
    assert attempt["reason_codes"] == ["quote_unavailable"]
    assert "最近一次扫描失败" in result["rendered_markdown"]
    assert "最近成功" in result["rendered_markdown"]
    assert "今日最新" not in result["rendered_markdown"]
    assert database.read_bytes() == original


def test_failure_without_success_remains_unavailable_and_reports_attempt(tmp_path):
    failed = _failure(tmp_path)

    result = _read(tmp_path)

    assert result["available"] is False
    assert result["brief"] is None
    assert result["latest_attempt"]["run_id"] == failed["run_id"]
    assert result["latest_attempt"]["reason"] == "scan_failed"
    assert "最近一次扫描失败" in result["rendered_markdown"]


def test_recovery_updates_latest_attempt_and_exact_history_is_unchanged(tmp_path):
    first = persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="first-good"))
    _failure(tmp_path)
    recovered = _brief(run_id="recovered")
    recovered["generated_at_utc"] = "2026-07-19T14:50:00+00:00"
    saved = persist_daily_decision_brief_success(base=tmp_path, brief=recovered)

    latest = _read(tmp_path)
    exact = _read(tmp_path, market_trading_date="2026-07-19", revision=first["current_revision"])

    assert latest["brief"] == saved["brief"]
    assert latest["latest_attempt"]["run_id"] == "recovered"
    assert latest["latest_attempt"]["reason"] == "ok"
    assert "最近一次扫描失败" not in latest["rendered_markdown"]
    assert exact["brief"] == first["brief"]
    assert "latest_attempt" not in exact
    assert "最近一次扫描失败" not in exact["rendered_markdown"]


def test_latest_attempt_does_not_borrow_another_account(tmp_path):
    saved = persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="lx-good"))
    _failure(tmp_path, account="sy", run_id="sy-failed")

    result = _read(tmp_path)

    assert result["brief"] == saved["brief"]
    assert result["latest_attempt"]["account"] == "lx"
    assert result["latest_attempt"]["run_id"] == "lx-good"
    assert result["latest_attempt"]["reason"] == "ok"


def test_position_and_notification_preview_preserve_latest_attempt(tmp_path):
    from src.application.agent_tools.notifications_impl import preview_notification_tool
    from src.application.agent_tools.position_advice_read_impl import read_position_advice_view

    persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="last-good"))
    _failure(tmp_path)
    result = _read(tmp_path)

    advice = read_position_advice_view(result, now_utc=NOW)
    preview, warnings, _meta = preview_notification_tool(
        {}, query_daily_brief=lambda payload: (result, [], {}),
    )

    assert advice["scopes"][0]["latest_attempt"] == result["latest_attempt"]
    assert preview["latest_attempt"] == result["latest_attempt"]
    assert preview["delivery_evidence"] is False
    assert "最近一次扫描失败" in preview["notification_text"]
    assert warnings == []


def test_aggregate_and_preview_keep_attempts_in_their_market_scopes(tmp_path):
    from src.application.agent_tools.notifications_impl import preview_notification_tool
    from src.application.config_yaml_init import init_yaml_config

    init_yaml_config(
        account_label="lx", futu_acc_id="123456", repo_root=Path(__file__).resolve().parents[1],
        output_config_yaml_path=tmp_path / "config.yaml", runtime_output_dir=tmp_path,
        bot_output_config_path=tmp_path / "resolved/config.bot.json", markets=["us", "hk"],
        us_symbols=["AAPL"], hk_symbols=["0700.HK"],
    )
    for market in ("HK", "US"):
        brief = {**_brief(run_id=f"good-{market}"), "market": market}
        persist_daily_decision_brief_success(base=tmp_path, brief=brief)
    _failure(tmp_path)

    view = read_daily_brief_view(base=tmp_path, now_utc=NOW)
    sections = {section["query"]["market"]: section for section in view["sections"]}
    preview, _, _ = preview_notification_tool({}, query_daily_brief=lambda payload: (view, [], {}))
    preview_sections = {section["query"]["market"]: section for section in preview["sections"]}

    assert sections["US"]["latest_attempt"]["reason"] == "scan_failed"
    assert "最近一次扫描失败" in sections["US"]["rendered_markdown"]
    assert sections["HK"]["latest_attempt"]["reason"] == "ok"
    assert "最近一次扫描失败" not in sections["HK"]["rendered_markdown"]
    assert "最近一次扫描失败" in view["rendered_markdown"]
    assert preview_sections["US"]["latest_attempt"] == sections["US"]["latest_attempt"]
    assert preview_sections["HK"]["latest_attempt"] == sections["HK"]["latest_attempt"]


def test_corrupt_attempt_is_visible_while_successful_content_remains_readable(tmp_path):
    from src.infrastructure.decision_history_sqlite import DecisionHistoryStore

    saved = persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="last-good"))
    _failure(tmp_path)
    with DecisionHistoryStore(history_path(tmp_path)).connect(write=True) as connection:
        connection.execute("DROP TRIGGER decisions_no_update")
        connection.execute("UPDATE decisions SET payload_hash='corrupted' WHERE run_id='failed-latest'")

    result = _read(tmp_path)

    assert result["available"] is True
    assert result["brief"] == saved["brief"]
    assert result["latest_attempt"] == {
        "available": False, "account": "lx", "market": "US", "reason": "state_invalid",
    }
    assert "最近一次扫描状态无法核验" in result["rendered_markdown"]
    assert "今日最新" not in result["rendered_markdown"]
    assert str(tmp_path) not in str(result)


def test_public_paged_tool_preserves_failed_attempt_without_success(tmp_path, monkeypatch):
    from src.application.agent_tools import daily_brief as tool

    _failure(tmp_path)
    monkeypatch.setattr(tool, "repo_base", lambda: tmp_path)
    result, _, _ = tool.DAILY_DECISION_BRIEF_READ_TOOL.call({
        "account": "lx", "market": "US", "section": "rendered_markdown",
    })

    assert result["available"] is False
    assert result["latest_attempt"]["reason"] == "scan_failed"
    assert result["latest_attempt"]["run_id"] == "failed-latest"


def test_paged_tool_rejects_continuation_after_attempt_changes(tmp_path, monkeypatch):
    import pytest
    from src.application import query_cursor
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools import daily_brief as tool

    monkeypatch.setattr(query_cursor, "_key", lambda: "isolated-test-cursor-key")
    brief = _brief(run_id="last-good")
    brief["strategy_summary"] = "retained report details " * 4000
    persist_daily_decision_brief_success(base=tmp_path, brief=brief)
    monkeypatch.setattr(tool, "repo_base", lambda: tmp_path)
    selection = {"account": "lx", "market": "US", "section": "brief"}
    first, _, _ = tool.DAILY_DECISION_BRIEF_READ_TOOL.call(selection)
    assert first["next_cursor"]
    _failure(tmp_path)

    with pytest.raises(AgentToolError, match="source_changed"):
        tool.DAILY_DECISION_BRIEF_READ_TOOL.call({**selection, "cursor": first["next_cursor"]})
