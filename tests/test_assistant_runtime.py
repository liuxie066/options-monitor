from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.runtime import handle_assistant_turn
from src.application.assistant.settings import AssistantSettings, CopilotSettings
from src.application.copilot.contracts import AppResult


def _request(tmp_path: Path, text: str, *, message_id: str = "m_runtime") -> AssistantRequest:
    return AssistantRequest(
        text=text,
        sender_id="u_runtime",
        channel="test",
        conversation_id="c_runtime",
        message_id=message_id,
        audit_db=str(tmp_path / "assistant_audit.db"),
        config_key="us",
        assistant_config_path=str(tmp_path / "config.assistant.json"),
    )


def test_portfolio_toolset_is_disabled_by_default_and_requires_all_gates() -> None:
    assert AssistantSettings.from_runtime_config({}).enabled_copilot_toolsets == frozenset()
    assert AssistantSettings.from_runtime_config(
        {"assistant": {"copilot": {"enabled": True, "toolsets": {"portfolio": True}}}}
    ).enabled_copilot_toolsets == frozenset({"portfolio"})
    assert AssistantSettings.from_runtime_config(
        {"assistant": {"enabled": False, "copilot": {"enabled": True, "toolsets": {"portfolio": True}}}}
    ).enabled_copilot_toolsets == frozenset()
    assert AssistantSettings.from_runtime_config(
        {"assistant": {"copilot": {"enabled": False, "toolsets": {"portfolio": True}}}}
    ).enabled_copilot_toolsets == frozenset()


def test_freeform_turn_goes_directly_to_copilot(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant import inbound_service

    captured: list[dict[str, Any]] = []

    def fake_copilot(**kwargs: Any) -> AppResult:
        captured.append(dict(kwargs))
        return AppResult(status="answered", user_response="7 月收益主要来自权利金。")

    monkeypatch.setattr(inbound_service, "run_channel_request", fake_copilot)
    result = handle_assistant_turn(
        _request(tmp_path, "7月收益"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True)),
    )

    assert result.ok is True
    assert result.response_text == "7 月收益主要来自权利金。"
    assert result.trace["route"] == "copilot"
    assert result.meta["assistant"]["route"] == "copilot"
    assert captured[0]["conversation_id"] == "c_runtime"


def test_followup_text_is_not_reparsed_as_a_business_intent(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant import inbound_service

    captured: list[str] = []

    def fake_copilot(**kwargs: Any) -> AppResult:
        captured.append(str(kwargs["user_message"]))
        return AppResult(status="answered", user_response="结论是收益集中于两个标的。")

    monkeypatch.setattr(inbound_service, "run_channel_request", fake_copilot)
    result = handle_assistant_turn(
        _request(tmp_path, "结论呢", message_id="m_followup"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True)),
    )

    assert captured == ["结论呢"]
    assert result.response_text == "结论是收益集中于两个标的。"


def test_slash_command_keeps_deterministic_control_path(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, dict(payload)))
        return build_response(tool_name=name, ok=True, data={"status": "ok"})

    result = handle_assistant_turn(
        _request(tmp_path, "/status", message_id="m_status"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(copilot=CopilotSettings(enabled=True)),
    )

    assert result.ok is True
    assert result.trace["route"] != "copilot"
    assert calls


def test_duplicate_freeform_message_reuses_audited_response(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant import inbound_service

    calls = 0

    def fake_copilot(**_kwargs: Any) -> AppResult:
        nonlocal calls
        calls += 1
        return AppResult(status="answered", user_response="第一次回答。")

    monkeypatch.setattr(inbound_service, "run_channel_request", fake_copilot)
    request = _request(tmp_path, "最近有哪些风险？", message_id="m_duplicate")
    settings = AssistantSettings(copilot=CopilotSettings(enabled=True))
    first = handle_assistant_turn(request, allowed_senders="u_runtime", settings=settings)
    second = handle_assistant_turn(request, allowed_senders="u_runtime", settings=settings)

    assert calls == 1
    assert first.response_text == second.response_text
    assert second.meta["idempotent_replay"] is True
