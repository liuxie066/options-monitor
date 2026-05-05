from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_agent_spec_uses_symbols_public_name() -> None:
    from scripts.agent_plugin.main import build_spec

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
    assert "openclaw_readiness" in tool_names
    assert spec["schema_version"] == "1.0"
    assert spec["recommended_flow"] == ["healthcheck", "scan_opportunities", "get_close_advice"]
    get_close_advice = next(item for item in spec["tools"] if item["name"] == "get_close_advice")
    assert "requires" in get_close_advice
    assert "capabilities" in get_close_advice
    runtime_status = next(item for item in spec["tools"] if item["name"] == "runtime_status")
    assert runtime_status["risk_level"] == "read_only"
    assert runtime_status["requires_confirm"] is False
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
    manage_symbols = next(item for item in spec["tools"] if item["name"] == "manage_symbols")
    assert manage_symbols["risk_level"] == "local_write"
    assert manage_symbols["requires_confirm"] is True
    assert manage_symbols["safe_default_input"]["action"] == "list"


def test_agent_run_unknown_tool_returns_structured_error() -> None:
    from scripts.agent_plugin.main import run_tool

    out = run_tool("does_not_exist", {})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert out["schema_version"] == "1.0"


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
    assert "init_command" not in payload["launcher"]
    assert payload["launcher"]["add_account_command"][0:2] == ["./om-agent", "add-account"]
    assert payload["launcher"]["edit_account_command"][0:2] == ["./om-agent", "edit-account"]
    assert payload["launcher"]["remove_account_command"][0:2] == ["./om-agent", "remove-account"]
