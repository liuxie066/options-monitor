from __future__ import annotations

from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.llm_trace import skipped_llm_trace
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.permission_response import parse_permission_response
from src.application.assistant.perception_trace import (
    PerceptionTrace,
    accepted_candidate,
    build_perception_trace,
    error_candidate,
    skipped_candidate,
)
from src.application.assistant.settings import AssistantSettings

NATURAL_LANGUAGE_REBUILDING_CODE = "NATURAL_LANGUAGE_REBUILDING"
NATURAL_LANGUAGE_REBUILDING_MESSAGE = "自由问答正在重建中，当前只处理明确指令和待确认操作。"
NATURAL_LANGUAGE_REBUILDING_HINT = "发送 /help 查看当前可用指令；自然语言分析不会再降级到旧自由问答链路或普通 LLM 回复。"


class PerceptionEngine:
    def __init__(
        self,
        *,
        request: AssistantRequest,
        audit_store: InboundAuditStore,
        settings: AssistantSettings,
    ) -> None:
        self._request = request
        self._audit_store = audit_store
        self._settings = settings
        self.route = self._initial_route(request.text)
        skipped_reason = "command" if self.route == "command" else "not_needed"
        self.llm_trace = skipped_llm_trace(settings.llm, reason=skipped_reason)
        self.trace: PerceptionTrace | None = None

    def perceive(self, text: str, parser_now_fn: Callable[[], date] | None) -> PerceptionResult:
        try:
            command_perception = parse_assistant_command(text, now_fn=parser_now_fn)
        except AgentToolError as err:
            self.trace = build_perception_trace(
                decision="command_error",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    error_candidate("command", err),
                    skipped_candidate("permission_response", "command_error"),
                    skipped_candidate("natural_language", "command_error"),
                ],
            )
            raise
        if command_perception is not None:
            self.route = "command"
            self.trace = build_perception_trace(
                decision="command_selected",
                selected_source="command",
                selected_perception=command_perception,
                candidates=[
                    accepted_candidate("command", command_perception),
                    skipped_candidate("permission_response", "command_selected"),
                    skipped_candidate("natural_language", "command_selected"),
                ],
            )
            return command_perception

        operation_store = InboundOperationStore(self._audit_store.path)
        try:
            permission_perception = parse_permission_response(
                text,
                request=self._request,
                store=operation_store,
            )
        except AgentToolError as err:
            self.route = "permission_response"
            self.trace = build_perception_trace(
                decision="permission_response_error",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    skipped_candidate("command", "not_command"),
                    error_candidate("permission_response", err),
                    skipped_candidate("natural_language", "permission_response_error"),
                ],
            )
            raise
        if permission_perception is not None:
            self.route = "permission_response"
            self.llm_trace = skipped_llm_trace(self._settings.llm, reason="permission_response")
            self.trace = build_perception_trace(
                decision="permission_response_selected",
                selected_source="permission_response",
                selected_perception=permission_perception,
                candidates=[
                    skipped_candidate("command", "not_command"),
                    accepted_candidate("permission_response", permission_perception),
                    skipped_candidate("natural_language", "permission_response_selected"),
                ],
            )
            return permission_perception

        del parser_now_fn
        err = natural_language_rebuilding_error()
        self.route = "natural_language_rebuilding"
        self.llm_trace = skipped_llm_trace(self._settings.llm, reason="natural_language_rebuilding")
        self.trace = build_perception_trace(
            decision="natural_language_rebuilding",
            selected_source=None,
            selected_perception=None,
            candidates=[
                skipped_candidate("command", "not_command"),
                skipped_candidate("permission_response", "not_permission_response"),
                error_candidate("natural_language", err),
            ],
        )
        raise err

    def _initial_route(self, text: str) -> str:
        if looks_like_command(text):
            return "command"
        return "natural_language_rebuilding"


def looks_like_command(text: str) -> bool:
    return str(text or "").lstrip().startswith("/")


def natural_language_rebuilding_error() -> AgentToolError:
    return AgentToolError(
        code=NATURAL_LANGUAGE_REBUILDING_CODE,
        message=NATURAL_LANGUAGE_REBUILDING_MESSAGE,
        hint=NATURAL_LANGUAGE_REBUILDING_HINT,
        details={"supported_input": ["slash_command", "permission_response"]},
    )


__all__ = [
    "NATURAL_LANGUAGE_REBUILDING_CODE",
    "PerceptionEngine",
    "looks_like_command",
    "natural_language_rebuilding_error",
]
