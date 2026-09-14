"""Project capability integration through the existing gateway and Bot boundary."""
import json
import time

import pytest

from src.application.agent_tool_registry import pure_read_toolsets
from src.application.agent_tools import project
from src.application.bot import tools
from src.application.tool_execution import execute_tool


def test_project_context_discovery_and_no_config(tmp_path, monkeypatch, example_config_path):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(example_config_path.parent))
    response = execute_tool("project_context", {"config_key": "us"})
    assert response["ok"]
    scope = response["data"]["scope"]
    assert scope["config_status"] == "current"
    assert scope["accounts"] == json.loads(example_config_path.read_text())["accounts"]
    assert scope["market"] == "us"
    assert str(example_config_path.parent) not in json.dumps(response["data"])
    assert {"project_files", "project_context"} <= set(pure_read_toolsets()["project"])
    missing = execute_tool("project_context", {"config_path": str(tmp_path / "missing.json")})
    assert missing["ok"]
    assert missing["data"]["scope"]["config_status"] == "config_missing"
    assert missing["data"]["scope"]["accounts"] == []


def test_stale_configuration_is_not_account_authority(tmp_path, example_config_path):
    raw = json.loads(example_config_path.read_text())
    next(item for item in raw["_generated"]["sources"] if item.get("loaded"))["sha256"] = "0" * 64
    stale = tmp_path / "stale/config.us.json"
    stale.parent.mkdir()
    stale.write_text(json.dumps(raw))
    response = execute_tool("project_context", {"config_path": str(stale)})
    assert response["data"]["scope"]["config_status"] == "config_stale"
    run = execute_tool("project_files", {"resource": "run", "config_path": str(stale)})
    assert not run["ok"]
    assert run["error"]["code"] == "CONFIG_ERROR"


@pytest.mark.parametrize("tool", ["candidate_filter_explain", "candidate_rank_explain", "option_performance_report", "project_files"])
def test_common_account_scope_before_execution(tool, example_config_path):
    config = json.loads(example_config_path.read_text())
    account = config["accounts"][0]
    payload, error = tools.build_tool_payload(tool, {"account": account}, fixed_input={"config_path": str(example_config_path)})
    assert error is None
    assert payload["account"] == account
    rejected, error = tools.build_tool_payload(tool, {"account": "not-configured"}, fixed_input={"config_path": str(example_config_path)})
    assert rejected is None and "configured scope" in error
    missing, error = tools.build_tool_payload(tool, {"account": account}, fixed_input={"config_path": str(example_config_path.parent / "missing.json")})
    assert missing is None and "配置" in error


def test_hash_restoration_is_restricted_to_typed_project_hash_locations():
    raw_hash = "1234567890123456" * 4
    original = {"tool_name": "project_files", "ok": True,
                "source": {"revision": "token=DO_NOT_EXPOSE", "content_hash": raw_hash + "/private/secret"},
                "value": {"arbitrary": {"revision": raw_hash}, "text": raw_hash},
                "result_contract": {"pagination": {"mode": "none"}}}
    safe = tools.redact_model_observation(original)
    assert "DO_NOT_EXPOSE" not in str(safe)
    assert "/private/secret" not in str(safe)
    assert raw_hash not in str(safe)
    for changes in ({"tool_name": "receipt_read"}, {"ok": False}):
        safe = tools.redact_model_observation({**original, **changes, "source": {"revision": raw_hash}})
        assert safe["source"]["revision"] != raw_hash


@pytest.mark.parametrize("source", ["argument", "repo_default", "env:OM_RUNTIME_ROOT"])
def test_only_closed_context_source_enum_is_preserved(source):
    raw = {"ok": True, "tool_name": "project_context", "source": {"runtime_root_source": source},
        "value": {"source": {"runtime_root_source": source}, "text": "env:OM_RUNTIME_ROOT"}}
    projected = tools.redact_model_observation(raw)
    assert projected["source"]["runtime_root_source"] == source
    assert projected["value"]["source"]["runtime_root_source"] == source
    assert projected["value"]["text"] == "env:***REDACTED_ID***"


@pytest.mark.parametrize("source", ["env:OM_RUNTIME_ROOT_EXTRA", "env:om_private_id", "/private/runtime token=secret-value"])
def test_unknown_context_source_remains_redacted(source):
    from src.application.research.redaction import redact_value
    raw = {"ok": True, "tool_name": "project_context", "source": {"runtime_root_source": source},
        "value": {"source": {"runtime_root_source": source}}}
    assert tools.redact_model_observation(raw) == redact_value(raw)
    assert tools.redact_model_observation(raw)["source"]["runtime_root_source"] != source
    for mutation in ({"ok": False}, {"tool_name": "project_files"}):
        wrong_owner = {**raw, **mutation, "source": {"runtime_root_source": "env:OM_RUNTIME_ROOT"}}
        assert tools.redact_model_observation(wrong_owner) == redact_value(wrong_owner)


@pytest.mark.parametrize("action", ["list", "search"])
@pytest.mark.parametrize("controls", [{"max_lines": 40}, {"start_line": 2}])
def test_line_controls_error_is_actionable_through_gateway_and_host(tmp_path, monkeypatch, action, controls):

    (tmp_path / "README.md").write_text("intro\nneedle implementation rule\n")
    monkeypatch.setattr(project, "repo_base", lambda: tmp_path)
    payload = {"action": action, **controls}
    if action == "search":
        payload["query"] = "needle"
    response = execute_tool("project_files", payload)
    assert response["ok"] is False
    assert response["error"]["code"] == "INPUT_ERROR"
    assert response["error"]["message"] == "line_controls_require_read"
    hint = "start_line/max_lines 仅用于 action=read；list/search 请移除这两个参数后重试。"
    assert response["error"]["hint"] == hint
    definition = next(tool for tool in project.TOOLS if tool.name == "project_files")
    for key in controls:
        assert "action=read only" in tools._bot_input_schema(definition)["properties"][key]["description"]

    from tests.test_bot_python_runtime import MODEL, answer, call, contract, run_contract, script
    captured = []
    result = run_contract(contract("读取项目参考资料"), model_settings=MODEL,
        model_request=script([call("project_files", payload), answer(hint)], captured))
    observed = json.loads(captured[-1]["messages"][-1]["content"])
    assert not observed["ok"] and observed["error"]["message"] == "line_controls_require_read"
    assert result.ok and hint in result.user_response
