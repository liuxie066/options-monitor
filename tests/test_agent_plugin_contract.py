from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

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
    assert "symbol_resolve" in tool_names
    assert "symbol_config_read" in tool_names
    assert "prepare_close_advice_inputs" in tool_names
    assert "close_advice" in tool_names
    assert "get_close_advice" in tool_names
    assert "monthly_income_report" in tool_names
    assert "option_positions_read" in tool_names
    assert "runtime_status" in tool_names
    assert "runtime_runs" in tool_names
    assert "runtime_logs" in tool_names
    assert "notification_perception_read" in tool_names
    assert "operation_timeline" in tool_names
    assert "assistant_trace" in tool_names
    assert "openclaw_readiness" not in tool_names
    assert "version_update" in tool_names
    assert "candidate_rank_explain" in tool_names
    assert "candidate_filter_explain" in tool_names
    assert "strategy_replay_analyze" not in tool_names
    assert "doctor" not in tool_names
    assert "research" not in tool_names
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
    notification_perception = next(item for item in spec["tools"] if item["name"] == "notification_perception_read")
    assert notification_perception["risk_level"] == "read_only"
    assert notification_perception["requires_confirm"] is False
    assert notification_perception["safe_default_input"] == {"limit": 10}
    assert "run_id" in notification_perception["input_schema"]
    assert "conversation_id" in notification_perception["input_schema"]
    assert "audit_path" not in notification_perception["input_schema"]
    operation_timeline = next(item for item in spec["tools"] if item["name"] == "operation_timeline")
    assert operation_timeline["risk_level"] == "read_only"
    assert operation_timeline["requires_confirm"] is False
    assert operation_timeline["safe_default_input"] == {"limit": 10}
    assert "operation_id" in operation_timeline["input_schema"]
    assert "operation_types" in operation_timeline["input_schema"]
    assert "audit_scan_limit" in operation_timeline["input_schema"]
    assistant_trace = next(item for item in spec["tools"] if item["name"] == "assistant_trace")
    assert assistant_trace["risk_level"] == "read_only"
    assert assistant_trace["requires_confirm"] is False
    assert assistant_trace["safe_default_input"] == {"limit": 10}
    assert "session_id" in assistant_trace["input_schema"]
    assert "command_id" in assistant_trace["input_schema"]
    assert "include_snapshot" in assistant_trace["input_schema"]
    income_report = next(item for item in spec["tools"] if item["name"] == "monthly_income_report")
    assert income_report["risk_level"] == "read_only"
    assert income_report["requires_confirm"] is False
    assert "month" in income_report["input_schema"]
    option_positions_read = next(item for item in spec["tools"] if item["name"] == "option_positions_read")
    assert option_positions_read["risk_level"] == "read_only"
    assert option_positions_read["safe_default_input"]["action"] == "list"
    action_schema = option_positions_read["input_schema"]["action"]
    action_description = action_schema["description"] if isinstance(action_schema, dict) else action_schema
    assert "history" in action_description
    assert "assigned-stock" in action_description
    assert option_positions_read["input_json_schema"]["properties"]["action"]["type"] == ["string", "array"]
    assert "quote_snapshots" in option_positions_read["input_schema"]
    assert "refresh_quotes" in option_positions_read["input_schema"]
    assert "opend_host" in option_positions_read["input_schema"]
    assert "opend_port" in option_positions_read["input_schema"]
    config_validate = next(item for item in spec["tools"] if item["name"] == "config_validate")
    assert config_validate["risk_level"] == "read_only"
    scheduler_status = next(item for item in spec["tools"] if item["name"] == "scheduler_status")
    assert scheduler_status["side_effects"] == []
    symbol_resolve = next(item for item in spec["tools"] if item["name"] == "symbol_resolve")
    assert symbol_resolve["risk_level"] == "read_only"
    assert symbol_resolve["requires_confirm"] is False
    assert "symbol" in symbol_resolve["input_schema"]
    symbol_config_read = next(item for item in spec["tools"] if item["name"] == "symbol_config_read")
    assert symbol_config_read["risk_level"] == "read_only"
    assert symbol_config_read["requires_confirm"] is False
    assert "symbol" in symbol_config_read["input_schema"]
    assert "field" in symbol_config_read["input_schema"]
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
    manage_symbols_set_schema = manage_symbols["input_json_schema"]["properties"]["set"]
    assert manage_symbols_set_schema["propertyNames"]["pattern"] == r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$"
    assert manage_symbols_set_schema["additionalProperties"]["type"] == [
        "string",
        "number",
        "integer",
        "boolean",
        "null",
    ]
    candidate_rank_explain = next(item for item in spec["tools"] if item["name"] == "candidate_rank_explain")
    assert candidate_rank_explain["risk_level"] == "read_only"
    assert candidate_rank_explain["requires_confirm"] is False
    assert candidate_rank_explain["safe_default_input"]["mode"] == "all"
    candidate_filter_explain = next(item for item in spec["tools"] if item["name"] == "candidate_filter_explain")
    assert candidate_filter_explain["risk_level"] == "read_only"
    assert candidate_filter_explain["requires_confirm"] is False
    assert "symbol" in candidate_filter_explain["input_schema"]


def test_agent_registry_manifest_and_tool_objects_stay_in_sync() -> None:
    from src.application.tool_execution import build_tool_manifest as build_spec
    from src.application.agent_tools.base import AgentTool
    from src.application.agent_tool_registry import AGENT_TOOL_DEFINITIONS, get_tool_definition, tool_names

    spec = build_spec()
    manifest_names = [str(x.get("name")) for x in spec.get("tools", [])]
    registry_names = list(tool_names())
    active_definitions = [definition for definition in AGENT_TOOL_DEFINITIONS if definition.enabled]
    migrated_names = {definition.name for definition in active_definitions}
    pure_read_names = {definition.name for definition in active_definitions if definition.is_pure_read()}

    assert manifest_names == registry_names
    assert pure_read_names <= migrated_names
    assert {
        "healthcheck",
        "config_validate",
        "scheduler_status",
        "symbol_resolve",
        "version_update",
        "manage_symbols",
        "scan_opportunities",
        "query_cash_headroom",
        "get_portfolio_context",
        "prepare_close_advice_inputs",
        "close_advice",
        "get_close_advice",
        "candidate_rank_explain",
        "candidate_filter_explain",
        "monthly_income_report",
        "option_positions_read",
        "close_advice_read",
        "preview_notification",
        "runtime_status",
        "version_check",
        "runtime_runs",
        "runtime_logs",
        "notification_perception_read",
        "operation_timeline",
        "assistant_trace",
    } <= migrated_names
    for name in migrated_names:
        definition = get_tool_definition(name)
        assert isinstance(definition, AgentTool)
        assert callable(definition.call)
    assert '"user1"' not in json.dumps([x.get("examples") for x in spec.get("tools", [])], ensure_ascii=False)


def test_agent_tool_output_contracts_advertise_canonical_renderers() -> None:
    from src.application.agent_tool_registry import get_tool_definition
    from src.application.tool_execution import build_tool_manifest as build_spec

    spec = build_spec()
    tools = {str(item.get("name")): item for item in spec.get("tools", [])}

    assert tools["monthly_income_report"]["output_contract"] == {
        "schema_version": "monthly_income_report.output",
        "payload_dependent": True,
    }
    assert tools["option_positions_read"]["output_contract"] == {
        "schema_version": "option_positions_read.output",
        "payload_dependent": True,
    }
    assert tools["runtime_status"]["output_contract"]["canonical_renderer"] == "runtime_status"
    assert tools["healthcheck"]["output_contract"]["canonical_renderer"] == "healthcheck"
    assert tools["runtime_runs"]["output_contract"]["canonical_renderer"] == "runtime_runs"
    assert tools["runtime_logs"]["output_contract"]["canonical_renderer"] == "runtime_logs"
    assert tools["notification_perception_read"]["output_contract"]["canonical_renderer"] == "notification_perception"
    assert tools["assistant_trace"]["output_contract"]["canonical_renderer"] == "assistant_trace"
    assistant_trace_fields = tools["assistant_trace"]["output_contract"]["fact_fields"]
    assert "traces[].capability_selection.selected_tools[]" in assistant_trace_fields
    assert "traces[].compact_trace.selected_capability" in assistant_trace_fields
    assert "traces[].compact_trace.model_turns.tool_call_count" in assistant_trace_fields
    assert "traces[].compact_trace.tool_observations[].tool_name" in assistant_trace_fields
    assert "traces[].compact_trace.evidence_gaps[].suggested_tool" in assistant_trace_fields
    assert "traces[].compact_trace.stop_reason" in assistant_trace_fields
    assert "traces[].compact_trace.answer_route" in assistant_trace_fields
    assert "traces[].progress.next_action" in assistant_trace_fields
    assert "traces[].progress.blocked_by[].tool_name" in assistant_trace_fields
    assert "traces[].answer.clarification_request.questions[].slot" in assistant_trace_fields
    assert tools["config_validate"]["output_contract"]["canonical_renderer"] == "config_validate"
    assert tools["symbol_resolve"]["output_contract"]["canonical_renderer"] == "symbol_resolve"
    assert tools["symbol_resolve"]["output_contract"]["result_shape"] == "scalar"
    assert "canonical_symbol" in tools["symbol_resolve"]["output_contract"]["fact_fields"]
    assert tools["symbol_config_read"]["output_contract"]["canonical_renderer"] == "symbol_config"
    assert "strategies" in tools["symbol_config_read"]["output_contract"]["model_preview_fields"]
    assert "strategies" in tools["symbol_config_read"]["evidence_contract"]["model_preview_fields"]
    assert tools["query_cash_headroom"]["risk_level"] == "read_only"
    assert tools["query_cash_headroom"]["side_effects"] == []
    assert tools["query_cash_headroom"]["output_contract"]["canonical_renderer"] == "cash_headroom"
    assert tools["close_advice_read"]["output_contract"]["canonical_renderer"] == "position_exit_analysis"
    assert tools["analysis_catalog"]["output_contract"]["canonical_renderer"] == "analysis_catalog"
    assert tools["analysis_catalog"]["output_contract"]["answer_surface"] == "internal"
    assert "view_names[]" in tools["analysis_catalog"]["output_contract"]["fact_fields"]
    assert "investigation_recipes[].name" not in tools["analysis_catalog"]["output_contract"]["fact_fields"]

    positions = get_tool_definition("option_positions_read")
    assert positions is not None
    positions_contract = positions.resolve_output_contract({"action": "list"})
    assert positions_contract["canonical_renderer"] == "position_rows"
    assert positions_contract["stable_order"] == "expiration_asc_missing_last"
    assert "rows[].contracts_open" in positions_contract["fact_fields"]
    assigned_stock_contract = positions.resolve_output_contract({"action": "assigned-stock"})
    assert assigned_stock_contract["canonical_renderer"] == "assigned_stock_lifecycle"
    assert "rows[].assignment_lifecycle_pnl" in assigned_stock_contract["fact_fields"]
    assert "rows[].quote_status" in assigned_stock_contract["freshness_fields"]
    assert "quote_refresh.missing_symbols" in assigned_stock_contract["missing_data_fields"]
    list_wrapped_assigned_stock_contract = positions.resolve_output_contract({"action": ["assigned-stock"]})
    assert list_wrapped_assigned_stock_contract["canonical_renderer"] == "assigned_stock_lifecycle"
    assert positions.resolve_answer_policy({"action": ["assigned-stock"]}) == "facts_then_analysis"

    income = get_tool_definition("monthly_income_report")
    assert income is not None
    detail_contract = income.resolve_output_contract({"include_rows": True})
    assert detail_contract["canonical_renderer"] == "monthly_income"
    assert detail_contract["guard_profile"] == "income_rows"
    assert "cashflow_rows[].contracts" in detail_contract["fact_fields"]


def test_agent_tool_manifest_exposes_p1_annotations_and_evidence_contract() -> None:
    from src.application.tool_execution import build_tool_manifest as build_spec

    spec = build_spec()
    tools = {str(item.get("name")): item for item in spec.get("tools", [])}

    runtime_status = tools["runtime_status"]
    assert runtime_status["annotations"] == {
        "read_only": True,
        "destructive": False,
        "idempotent": True,
        "open_world": False,
    }
    assert runtime_status["input_schema_version"] == "om-tool-input-v1"
    assert runtime_status["output_schema"] == {}
    assert runtime_status["evidence_contract"]["source_label"] == "OM 本地 runtime_status"
    assert runtime_status["evidence_contract"]["canonical_renderer"] == "runtime_status"
    assert "output_contract" in runtime_status["verifiers"]
    assert "numeric" in runtime_status["verifiers"]

    monthly_income = tools["monthly_income_report"]
    assert monthly_income["evidence_contract"] == {
        "schema_version": "monthly_income_report.output",
        "payload_dependent": True,
    }
    assert monthly_income["verifiers"] == ["schema", "output_contract"]
    symbol_resolve = tools["symbol_resolve"]
    assert symbol_resolve["evidence_contract"]["result_shape"] == "scalar"
    assert "canonical_symbol" in symbol_resolve["evidence_contract"]["fact_fields"]


def test_agent_registry_collects_domain_tool_modules() -> None:
    from src.application.agent_tool_registry import AGENT_TOOL_DEFINITIONS, AGENT_TOOL_MODULES

    expected = tuple(
        definition
        for module in AGENT_TOOL_MODULES
        for definition in getattr(module, "TOOLS")
    )

    assert AGENT_TOOL_DEFINITIONS == expected
    assert {module.__name__.rsplit(".", 1)[-1] for module in AGENT_TOOL_MODULES} >= {
        "candidate",
        "close_advice",
        "config",
        "diagnostics",
        "materialization",
        "notifications",
        "positions",
        "runtime",
    }


def test_pure_read_allowlist_is_derived_from_registry_metadata() -> None:
    from src.application.agent_tool_registry import AGENT_TOOL_DEFINITIONS, pure_read_tool_names
    from src.application.tool_allowlist import PURE_READ_TOOLS

    expected = frozenset(definition.name for definition in AGENT_TOOL_DEFINITIONS if definition.is_pure_read())

    assert PURE_READ_TOOLS == expected
    assert pure_read_tool_names() == expected
    assert "runtime_status" in PURE_READ_TOOLS
    assert "version_check" in PURE_READ_TOOLS
    assert "runtime_runs" in PURE_READ_TOOLS
    assert "runtime_logs" in PURE_READ_TOOLS
    assert "notification_perception_read" in PURE_READ_TOOLS
    assert "symbol_resolve" in PURE_READ_TOOLS
    assert "symbol_config_read" in PURE_READ_TOOLS
    assert "query_cash_headroom" in PURE_READ_TOOLS
    assert "candidate_filter_explain" in PURE_READ_TOOLS
    assert "operation_timeline" in PURE_READ_TOOLS
    assert "assistant_trace" in PURE_READ_TOOLS
    assert "scan_opportunities" not in PURE_READ_TOOLS
    assert "manage_symbols" not in PURE_READ_TOOLS
    assert "research" not in PURE_READ_TOOLS


def test_write_request_policy_is_tool_permission_driven() -> None:
    from src.application.agent_tool_registry import get_tool_definition
    from src.application.agent_tools.permissions import tool_write_requested

    version_update = get_tool_definition("version_update")
    manage_symbols = get_tool_definition("manage_symbols")
    runtime_status = get_tool_definition("runtime_status")

    assert version_update is not None
    assert version_update.write_request_predicate is not None
    assert not tool_write_requested(version_update, {"bump": "patch"})
    assert not tool_write_requested(version_update, {"bump": "patch", "apply": False})
    assert tool_write_requested(version_update, {"bump": "patch", "apply": True})

    assert manage_symbols is not None
    assert manage_symbols.write_request_predicate is not None
    assert not tool_write_requested(manage_symbols, {"action": "list"})
    assert not tool_write_requested(manage_symbols, {"action": "edit", "dry_run": True})
    assert tool_write_requested(manage_symbols, {"action": "edit", "dry_run": False})

    assert runtime_status is not None
    assert not tool_write_requested(runtime_status, {"dry_run": False})


def test_migrated_agent_tool_executes_through_agent_tool_object(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    runs_root = tmp_path / "output_runs"
    runs_root.mkdir()

    runs = run_tool("runtime_runs", {"runs_root": str(runs_root), "limit": 1})
    assert runs["ok"] is True
    assert runs["data"]["schema_version"] == "runtime_runs.v1"
    assert runs["data"]["summary"]["limit"] == 1
    assert runs["meta"]["runs_root"] == ".../output_runs"

    logs = run_tool("runtime_logs", {"runs_root": str(runs_root), "kind": "all", "lines": 1})
    assert logs["ok"] is True
    assert logs["data"]["schema_version"] == "runtime_logs.v1"
    assert logs["data"]["summary"]["lines"] == 1


def test_write_gate_uses_tool_write_policy(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace

    import src.application.tool_execution as tool_execution

    calls: list[dict[str, Any]] = []

    def _update_local_version(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "mode": "dry_run" if not kwargs.get("apply") else "applied",
            "current_version": "1.0.0",
            "target_version": "1.0.1",
            "would_change": True,
            "changed": False,
            "version_path": tmp_path / "VERSION",
        }

    ctx = tool_execution.build_default_agent_tool_context()
    monkeypatch.setattr(
        tool_execution,
        "build_default_agent_tool_context",
        lambda: replace(ctx, repo_base=lambda: tmp_path, update_local_version=_update_local_version),
    )

    monkeypatch.delenv("OM_AGENT_ENABLE_WRITE_TOOLS", raising=False)

    preview = tool_execution.execute_tool("version_update", {"bump": "patch", "apply": False})
    assert preview["ok"] is True
    assert len(calls) == 1
    assert calls[0]["apply"] is False

    blocked_apply = tool_execution.execute_tool("version_update", {"bump": "patch", "apply": True})
    assert blocked_apply["ok"] is False
    assert blocked_apply["error"]["code"] == "PERMISSION_DENIED"
    assert len(calls) == 1

    blocked_write = tool_execution.execute_tool("manage_symbols", {"config_key": "us", "action": "edit", "dry_run": False})
    assert blocked_write["ok"] is False
    assert blocked_write["error"]["code"] == "PERMISSION_DENIED"


def test_agent_manifest_safe_defaults_do_not_select_market_config() -> None:
    from src.application.tool_execution import build_tool_manifest as build_spec

    spec = build_spec()

    for tool in spec.get("tools", []):
        schema = tool.get("input_schema") if isinstance(tool, dict) else {}
        safe_default = tool.get("safe_default_input") if isinstance(tool, dict) else {}
        if isinstance(schema, dict) and "config_key" in schema:
            assert isinstance(safe_default, dict)
            assert "config_key" not in safe_default
            assert "config_path" not in safe_default


def test_agent_run_unknown_tool_returns_structured_error() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("does_not_exist", {})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert out["schema_version"] == "1.0"


def test_agent_tool_execution_rejects_nested_symbol_set_before_handler() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool(
        "manage_symbols",
        {
            "config_key": "hk",
            "action": "edit",
            "symbol": "0883.HK",
            "set": {"sell_put": {"max_strike": 18}},
            "dry_run": True,
        },
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert out["error"]["details"]["schema_errors"][0]["path"] == "set.sell_put"


def test_agent_tool_execution_rejects_non_dot_symbol_set_key_before_handler() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool(
        "manage_symbols",
        {
            "config_key": "hk",
            "action": "edit",
            "symbol": "0883.HK",
            "set": {"sell_put": 18},
            "dry_run": True,
        },
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert out["error"]["details"]["schema_errors"][0]["path"] == "set.sell_put"
    assert "property name matching" in out["error"]["details"]["schema_errors"][0]["expected"]


def test_removed_strategy_replay_tool_returns_unknown_tool(monkeypatch) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    monkeypatch.delenv("OM_AGENT_ENABLE_WRITE_TOOLS", raising=False)

    out = run_tool("strategy_replay_analyze", {"rows": []})
    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert "unknown tool" in out["error"]["message"]


def test_research_is_not_an_agent_tool(monkeypatch) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    monkeypatch.delenv("OM_AGENT_ENABLE_WRITE_TOOLS", raising=False)

    out = run_tool("research", {"scope": "full", "write_outputs": False})
    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert "unknown tool" in out["error"]["message"]


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
    assert any(str(x.get("name")) == "operation_timeline" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "candidate_rank_explain" for x in payload.get("tools", []))
    assert not any(str(x.get("name")) == "doctor" for x in payload.get("tools", []))
    assert not any(str(x.get("name")) == "research" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "candidate_filter_explain" for x in payload.get("tools", []))
    assert not any(str(x.get("name")) == "strategy_replay_analyze" for x in payload.get("tools", []))
    assert "init_command" not in payload["launcher"]
    assert payload["launcher"]["add_account_command"][0:2] == ["./om-agent", "add-account"]
    assert payload["launcher"]["edit_account_command"][0:2] == ["./om-agent", "edit-account"]
    assert payload["launcher"]["remove_account_command"][0:2] == ["./om-agent", "remove-account"]
    assert "--dry-run" in payload["launcher"]["add_account_command"]
    assert payload["config"]["service_profile_name"] == "service.profile.json"
    assert "openclaw_profile_names" not in payload["config"]
