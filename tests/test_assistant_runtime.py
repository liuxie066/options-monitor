from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import build_response
from src.application.assistant.perception import NATURAL_LANGUAGE_REBUILDING_CODE
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.runtime import handle_assistant_turn
from src.application.assistant.settings import AssistantSettings


def _request(tmp_path: Path, text: str, *, config_key: str = "us") -> AssistantRequest:
    return AssistantRequest(
        text=text,
        sender_id="u_runtime",
        channel="test",
        conversation_id="c_runtime",
        message_id="m_runtime",
        audit_db=str(tmp_path / "assistant_audit.db"),
        config_key=config_key,
    )


def test_assistant_turn_disables_free_form_natural_language(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(tmp_path, "分析6月的期权操作有没有不合理，需要优化的地方"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is False
    assert result.render_route == "error"
    assert result.error is not None
    assert result.error["code"] == NATURAL_LANGUAGE_REBUILDING_CODE
    assert result.trace["route"] == "natural_language_rebuilding"
    assert "自由问答正在重建中" in result.response_text


def test_assistant_turn_keeps_slash_help_available(tmp_path: Path) -> None:
    result = handle_assistant_turn(
        _request(tmp_path, "/help"),
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert result.render_route == "command"
    assert "/help" in result.response_text


def test_assistant_turn_keeps_explicit_read_command_available(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "status": "ok",
                "summary": "runtime ok",
            },
        )

    result = handle_assistant_turn(
        _request(tmp_path, "/status"),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert result.ok is True
    assert calls == [("runtime_status", {"config_key": "us"})]


def test_free_form_trade_message_does_not_preview_or_write(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(payload)))
        return build_response(tool_name=tool_name, ok=True, data={})

    result = handle_assistant_turn(
        _request(
            tmp_path,
            "sy 成交提醒: 【成交提醒】成功卖出2张$腾讯 260605 440.00 沽$，成交价格：0.86，此笔订单委托已全部成交",
            config_key="hk",
        ),
        execute_tool_fn=execute_tool,
        allowed_senders="u_runtime",
        settings=AssistantSettings(),
        now_fn=lambda: date(2026, 7, 6),
    )

    assert calls == []
    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == NATURAL_LANGUAGE_REBUILDING_CODE
