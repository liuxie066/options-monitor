from __future__ import annotations

import pytest

from src.application.agent_tool_registry import get_tool_definition, pure_read_tool_names
from src.application.bot import tools as bot_tools
from src.application.bot.host import run_contract
from src.application.bot.scene import build_scene_manifest, load_general_scene
from src.application.tool_execution import execute_tool
from tests.bot_pi_test_support import _TEST_MODEL
from tests.test_bot_phase1 import _contract


RETIRED = {"preview_notification", "portfolio_cash_bridge"}


def test_bot_filter_preserves_the_public_registry_and_retained_tools():
    public = set(pure_read_tool_names())
    assert RETIRED <= public
    assert set(bot_tools.available_read_tools()) == public - RETIRED
    stale_descriptions = bot_tools.tool_descriptions(tuple(sorted(public)))
    assert {item["name"] for item in stale_descriptions} == public - RETIRED
    assert bot_tools.is_active_bot_read_tool("not_registered") is False


@pytest.mark.parametrize("name", sorted(RETIRED))
def test_stale_schema_and_allowlist_cannot_execute_retired_tool(monkeypatch, name):
    definition = get_tool_definition(name)
    assert definition is not None and definition.is_pure_read()
    monkeypatch.setattr(bot_tools, "execute_tool", lambda *_a, **_kw: pytest.fail("retired Bot call reached public execution"))
    payload, error = bot_tools.build_tool_payload(name, {}, static_payloads={name: {"period": "mtd"}})
    assert payload is None
    assert error == f"unsupported read-only tool: {name}"
    result = bot_tools.call_read_tool(name, {}, allowed_tools=(name,))
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_ERROR"


@pytest.mark.parametrize("name", sorted(RETIRED | {"tool_directory", "request_control_preview"}))
def test_current_runtime_never_executes_retired_tool(monkeypatch, name):
    from tests.test_bot_python_runtime import answer, call, script
    monkeypatch.setattr(bot_tools, "execute_tool", lambda *a, **k: pytest.fail("retired tool executed"))
    seen=[]
    result=run_contract(_contract(), model_settings=_TEST_MODEL,
                        model_request=script([call(name),answer("工具不可用。")],seen))
    assert result.ok
    assert not (RETIRED | {"tool_directory", "request_control_preview"}) & {t["name"] for t in seen[0]["tools"]}


def test_public_cash_bridge_still_reports_unavailable_without_transport(monkeypatch):
    from src.application.agent_tools import portfolio

    monkeypatch.setattr(portfolio.urllib.request, "urlopen", lambda *_a, **_kw: pytest.fail("cash bridge must not open transport"))
    response = execute_tool("portfolio_cash_bridge", {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]})
    assert response["ok"] is True
    data = response["data"]
    assert data["status"] == "unavailable"
    assert data["accounts"][0]["reason"] == "portfolio_cash_facts_not_onboarded"
    assert data["accounts"][0]["steps"] == []
    assert data["combined"]["reason"] == "portfolio_cash_facts_not_onboarded"
