from __future__ import annotations

import json

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.runtime_status_impl import read_onboarding_status
from src.interfaces.cli import home
from test_cli_tick_result import _brief


def _document():
    return {"markets": {"us": {"accounts": ["lx"]}}, "accounts": {"lx": {"futu": {}}}}


def _metrics(root, run, *, time, reason=None):
    path = root / "output_runs" / run / "accounts" / "lx" / "state" / "account_metrics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": run, "account": "lx", "markets_to_run": ["US"],
                               "as_of_utc": time, "ran_scan": True, "ran_pipeline": False,
                               "reason": reason}))


def test_latest_failed_attempt_and_old_readable_brief_are_distinct(tmp_path, capsys):
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_failure, persist_daily_decision_brief_success,
    )
    persist_daily_decision_brief_success(base=tmp_path, brief=_brief("20260721_140000_old"))
    persist_daily_decision_brief_failure(base=tmp_path, brief=_brief("20260721_150000_failed", status="blocked", actionability="blocked"))
    _metrics(tmp_path, "20260721_150000_failed", time="2026-07-21T15:00:00+00:00")
    result = read_onboarding_status(base=tmp_path, document=_document())
    scope = result["scopes"][0]
    assert scope["attempt"]["status"] == "failed"
    assert scope["attempt"]["run_id"] == "20260721_150000_failed"
    assert scope["brief_run_id"] == "20260721_140000_old" and scope["brief_time"] == "2026-07-21T13:59:00+00:00"
    assert result["automatic_running"] == "unknown"
    home._print_home_status(result)
    output = capsys.readouterr().out
    assert "最近扫描：失败" in output and "较早结果，非最近尝试" in output
    assert "2026-07-21T15:00:00+00:00" in output


def test_home_status_without_runs_does_not_claim_automatic_running(tmp_path, capsys):
    (tmp_path / "service.profile.json").write_text('{"enabled": true}')
    state = read_onboarding_status(base=tmp_path, document=_document())
    assert state["scopes"][0]["attempt"]["status"] == "not_found"
    assert not state["scopes"][0]["brief_available"]
    assert state["scopes"][0]["brief_reason"] == "state_invalid"
    assert state["scopes"][0]["brief_error"] == "history_missing"
    assert state["automatic_running"] == "unknown"
    home._print_home_status(state)
    output = capsys.readouterr().out
    assert "暂无可读结果" in output and "state_invalid" not in output
    assert sorted(path.name for path in tmp_path.iterdir()) == ["service.profile.json"]


def test_missing_history_after_attempt_keeps_unavailable_reason_and_repair_hint(tmp_path, capsys):
    _metrics(tmp_path, "20260721_150000_attempt", time="2026-07-21T15:00:00+00:00")
    state = read_onboarding_status(base=tmp_path, document=_document())
    assert state["scopes"][0]["brief_reason"] == "state_invalid"
    assert state["scopes"][0]["brief_error"] == "history_missing"
    assert state["scopes"][0]["attempt"]["status"] != "completed"
    home._print_home_status(state)
    output = capsys.readouterr().out
    assert "历史来源不存在" in output and "已有扫描证据" in output
    assert "其他管理中排查" in output


@pytest.mark.parametrize("document", [
    {}, {"markets": {"us": {}, "hk": "broken"}},
    {"markets": {"us": {"accounts": []}}},
    {"markets": {"us": {"accounts": ["../bad"]}}},
    {"markets": {"us": {"accounts": ["lx"]}}, "accounts": {}},
])
def test_malformed_authoring_is_not_silently_empty(tmp_path, document):
    with pytest.raises(AgentToolError, match="CONFIG_ERROR"):
        read_onboarding_status(base=tmp_path, document=document)


def test_configured_home_read_and_actions_remain_in_selected_instance(tmp_path, monkeypatch, capsys):
    import os
    from src.application.runtime_paths import resolve_runtime_root
    from src.interfaces.cli import journeys
    state = read_onboarding_status(base=tmp_path, document=_document())
    monkeypatch.setattr(home, "_home_state", lambda: (tmp_path / "config.yaml", state))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "other"))
    calls = []
    def run(argv):
        calls.append((argv, resolve_runtime_root(repo_root=tmp_path).runtime_root))
        return 0
    def scan(run, source, **kwargs):
        assert source == tmp_path / "config.yaml"
        return run(["fixture-scan"])
    monkeypatch.setattr(journeys, "manual_scan", scan)
    answers = iter(("1", "2", "0"))
    assert home.interactive_home(run, input_fn=lambda _: next(answers)) == 0
    assert calls == [(["daily-brief", "latest"], tmp_path), (["fixture-scan"], tmp_path)]
    assert os.environ["OM_RUNTIME_ROOT"] == str(tmp_path / "other")
    output = capsys.readouterr().out
    assert "尚未扫描" in output and "修改关注标的与策略边界" in output


def test_home_broken_config_reports_repair_without_reinitializing(tmp_path, monkeypatch, capsys):
    def fail():
        raise AgentToolError(code="CONFIG_ERROR", message="broken source")
    monkeypatch.setattr(home, "_home_state", fail)
    assert home.interactive_home(lambda _: pytest.fail("must not dispatch writes")) == 2
    assert "不会重建已有配置" in capsys.readouterr().out
