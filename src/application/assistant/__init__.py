from __future__ import annotations

from src.application.assistant.capability_catalog import (
    AssistantCapabilitySpec,
    AssistantCommandSpec,
    capability_catalog_text,
    capability_catalog_payload,
    capability_specs,
    command_catalog_payload,
    command_specs,
    operation_specs,
    operation_target_intents,
)
from src.application.assistant.contracts import AssistantRequest, PerceptionResult, ToolCall
from src.application.assistant.perception import PerceptionEngine
from src.application.assistant.perception_trace import AssistantDecision, PerceptionCandidate, PerceptionTrace
from src.application.assistant.settings import AssistantSettings, AssistantLlmSettings

__all__ = [
    "AssistantCapabilitySpec",
    "AssistantCommandSpec",
    "AssistantDecision",
    "AssistantRequest",
    "AssistantSettings",
    "AssistantLlmSettings",
    "PerceptionCandidate",
    "PerceptionEngine",
    "PerceptionResult",
    "PerceptionTrace",
    "ToolCall",
    "capability_catalog_text",
    "capability_catalog_payload",
    "capability_specs",
    "command_catalog_payload",
    "command_specs",
    "operation_specs",
    "operation_target_intents",
    "handle_assistant_message",
]


def __getattr__(name: str):
    if name == "handle_assistant_message":
        from src.application.assistant.runtime import handle_assistant_message

        return handle_assistant_message
    raise AttributeError(name)
