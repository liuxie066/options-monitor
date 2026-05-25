from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_agent_spec_uses_symbols_public_name() -> None:
    from src.application.tool_execution import build_tool_manifest as build_spec

    spec = build_spec()
    tool_names = [str(x.get("name")) for x in spec.get("tools", [])]

    assert "manage_symbols" in tool_names
    assert "manage_watchlist" not in tool_names
    assert "version_check" in tool_names
    assert "config_validate" in tool_names
    assert "scheduler_status" in tool_names
    assert "prepare_close_advice_inputs" in tool_names
    assert "close_advice" in tool_names
    assert "get_close_advice" in tool_names
    assert "monthly_income_report" in tool_names
    assert "option_positions_read" in tool_names
    assert "runtime_status" in tool_names
    assert "runtime_runs" in tool_names
    assert "runtime_logs" in tool_names
    assert "openclaw_readiness" in tool_names
    assert "version_update" in tool_names
    assert "candidate_rank_explain" in tool_names
    assert "candidate_filter_explain" in tool_names
    assert "strategy_replay_analyze" in tool_names
    assert "doctor" not in tool_names
    assert "research" in tool_names
    assert "strategy_lab" not in tool_names
    assert "strategy_lab_dataset_collect" not in tool_names
    assert "strategy_lab_experiment" not in tool_names
    assert "strategy_lab_current" not in tool_names
    assert "strategy_analyze" not in tool_names
    assert "strategy_current" not in tool_names
    assert spec["schema_version"] == "1.0"
    assert spec["recommended_flow"] == ["healthcheck", "scan_opportunities", "get_close_advice"]
    get_close_advice = next(item for item in spec["tools"] if item["name"] == "get_close_advice")
    assert "requires" in get_close_advice
    assert "capabilities" in get_close_advice
    runtime_status = next(item for item in spec["tools"] if item["name"] == "runtime_status")
    assert runtime_status["risk_level"] == "read_only"
    assert runtime_status["requires_confirm"] is False
    assert "run_id" in runtime_status["input_schema"]
    assert "run_dir" in runtime_status["input_schema"]
    runtime_runs = next(item for item in spec["tools"] if item["name"] == "runtime_runs")
    assert runtime_runs["risk_level"] == "read_only"
    assert runtime_runs["requires_confirm"] is False
    assert runtime_runs["safe_default_input"] == {"limit": 10}
    assert "run_id" in runtime_runs["input_schema"]
    assert "run_dir" in runtime_runs["input_schema"]
    assert "limit" in runtime_runs["input_schema"]
    runtime_logs = next(item for item in spec["tools"] if item["name"] == "runtime_logs")
    assert runtime_logs["risk_level"] == "read_only"
    assert runtime_logs["requires_confirm"] is False
    assert runtime_logs["safe_default_input"] == {"kind": "all", "lines": 50}
    assert "kind" in runtime_logs["input_schema"]
    assert "lines" in runtime_logs["input_schema"]
    assert "log_file" in runtime_logs["input_schema"]
    assert "file" not in runtime_logs["input_schema"]
    research = next(item for item in spec["tools"] if item["name"] == "research")
    assert "runs_root" in research["input_schema"]
    assert "run_id" in research["input_schema"]
    assert "report_dir" in research["input_schema"]
    income_report = next(item for item in spec["tools"] if item["name"] == "monthly_income_report")
    assert income_report["risk_level"] == "read_only"
    assert income_report["requires_confirm"] is False
    assert "month" in income_report["input_schema"]
    option_positions_read = next(item for item in spec["tools"] if item["name"] == "option_positions_read")
    assert option_positions_read["risk_level"] == "read_only"
    assert option_positions_read["safe_default_input"]["action"] == "list"
    assert "history" in option_positions_read["input_schema"]["action"]
    config_validate = next(item for item in spec["tools"] if item["name"] == "config_validate")
    assert config_validate["risk_level"] == "read_only"
    scheduler_status = next(item for item in spec["tools"] if item["name"] == "scheduler_status")
    assert scheduler_status["side_effects"] == []
    version_check = next(item for item in spec["tools"] if item["name"] == "version_check")
    assert version_check["safe_default_input"]["remote_name"] == "origin"
    version_update = next(item for item in spec["tools"] if item["name"] == "version_update")
    assert version_update["risk_level"] == "local_write"
    assert version_update["requires_confirm"] is True
    assert version_update["safe_default_input"] == {"bump": "patch", "apply": False}
    assert "target_version" in version_update["input_schema"]
    assert "version" not in version_update["input_schema"]
    manage_symbols = next(item for item in spec["tools"] if item["name"] == "manage_symbols")
    assert manage_symbols["risk_level"] == "local_write"
    assert manage_symbols["requires_confirm"] is True
    assert manage_symbols["safe_default_input"]["action"] == "list"
    candidate_rank_explain = next(item for item in spec["tools"] if item["name"] == "candidate_rank_explain")
    assert candidate_rank_explain["risk_level"] == "read_only"
    assert candidate_rank_explain["requires_confirm"] is False
    assert candidate_rank_explain["safe_default_input"]["mode"] == "all"
    candidate_filter_explain = next(item for item in spec["tools"] if item["name"] == "candidate_filter_explain")
    assert candidate_filter_explain["risk_level"] == "read_only"
    assert candidate_filter_explain["requires_confirm"] is False
    assert "symbol" in candidate_filter_explain["input_schema"]
    strategy_replay = next(item for item in spec["tools"] if item["name"] == "strategy_replay_analyze")
    assert strategy_replay["risk_level"] == "read_only"
    assert strategy_replay["requires_confirm"] is False
    assert strategy_replay["safe_default_input"]["min_sample"] == 5
    assert "strategy_replay_diagnostic" in strategy_replay["capabilities"]
    research = next(item for item in spec["tools"] if item["name"] == "research")
    assert research["read_only"] is False
    assert research["risk_level"] == "local_write"
    assert research["requires_confirm"] is True
    assert research["safe_default_input"] == {
        "scope": "full",
        "config_key": "us",
        "output": "handoff",
        "write_outputs": False,
    }
    assert "scope" in research["input_schema"]
    assert "include_healthcheck" in research["input_schema"]
    assert "data_config" in research["input_schema"]
    assert "strategy_replay_paths" in research["input_schema"]
    assert "ai_config" not in research["input_schema"]
    assert "healthcheck_snapshot" in research["capabilities"]


def test_agent_registry_manifest_and_handlers_stay_in_sync() -> None:
    from src.application.tool_execution import build_tool_manifest as build_spec
    from src.application.agent_tool_handlers import TOOL_HANDLERS
    from src.application.agent_tool_registry import tool_names

    spec = build_spec()
    manifest_names = [str(x.get("name")) for x in spec.get("tools", [])]
    registry_names = list(tool_names())

    assert manifest_names == registry_names
    assert sorted(TOOL_HANDLERS) == sorted(registry_names)
    assert '"user1"' not in json.dumps([x.get("examples") for x in spec.get("tools", [])], ensure_ascii=False)


def test_agent_run_unknown_tool_returns_structured_error() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("does_not_exist", {})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert out["schema_version"] == "1.0"


def test_strategy_lab_tools_are_removed() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("strategy_lab", {"write_outputs": False})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert "unknown tool" in out["error"]["message"]
    out = run_tool("strategy_lab_dataset_collect", {"dry_run": True})
    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"


def test_removed_strategy_tools_return_unknown_tool(monkeypatch) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    monkeypatch.delenv("OM_AGENT_ENABLE_WRITE_TOOLS", raising=False)

    out = run_tool("strategy_analyze", {"config_key": "us", "strategy_type": "sell_put", "confirm": True})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert "unknown tool" in out["error"]["message"]
    out = run_tool("strategy_current", {})
    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"


def test_agent_cli_run_loads_explicit_env_file(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.agent.cli as agent_cli

    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_agent\n", encoding="utf-8")
    bootstrap_calls: list[dict] = []
    calls: list[tuple[str, dict]] = []

    def _bootstrap_process_env(**kwargs):
        bootstrap_calls.append(kwargs)

    def _execute_tool(name: str, payload: dict) -> dict:
        calls.append((name, payload))
        return {"schema_version": "1.0", "tool_name": name, "ok": True, "data": {"status": "ok"}, "warnings": [], "error": None, "meta": {}}

    monkeypatch.setattr(agent_cli, "bootstrap_process_env", _bootstrap_process_env)
    monkeypatch.setattr(agent_cli, "execute_tool", _execute_tool)

    rc = agent_cli.main(["run", "--tool", "healthcheck", "--env-file", str(env_file)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "healthcheck"
    assert calls == [("healthcheck", {})]
    assert bootstrap_calls == [{
        "repo_root": agent_cli.repo_base(),
        "env_file": str(env_file),
        "include_local_env_file": True,
    }]


def test_agent_cli_spec_prints_json_manifest() -> None:
    import subprocess

    p = subprocess.run(
        [str((BASE / "om-agent").resolve()), "spec"],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(p.stdout)
    assert payload["name"] == "options-monitor-local-tools"
    assert any(str(x.get("name")) == "query_cash_headroom" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "monthly_income_report" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "option_positions_read" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "config_validate" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "runtime_runs" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "runtime_logs" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "candidate_rank_explain" for x in payload.get("tools", []))
    assert not any(str(x.get("name")) == "doctor" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "research" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "candidate_filter_explain" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "strategy_replay_analyze" for x in payload.get("tools", []))
    assert "init_command" not in payload["launcher"]
    assert payload["launcher"]["add_account_command"][0:2] == ["./om-agent", "add-account"]
    assert payload["launcher"]["edit_account_command"][0:2] == ["./om-agent", "edit-account"]
    assert payload["launcher"]["remove_account_command"][0:2] == ["./om-agent", "remove-account"]
    assert "--dry-run" in payload["launcher"]["add_account_command"]
    assert payload["config"]["openclaw_profile_names"] == ["openclaw.profile.json", ".openclaw-profile.json"]
