from __future__ import annotations

from src.application.assistant.commands import AssistantCommandSpec, command_catalog_payload, command_specs
from src.application.assistant.contracts import AssistantIntent, AssistantRequest, AssistantToolCall
from src.application.assistant.settings import AssistantSettings, LlmTranslatorSettings

__all__ = [
    "AssistantIntent",
    "AssistantCommandSpec",
    "AssistantRequest",
    "AssistantSettings",
    "AssistantToolCall",
    "LlmTranslatorSettings",
    "command_catalog_payload",
    "command_specs",
    "handle_assistant_message",
]


def __getattr__(name: str):
    if name == "handle_assistant_message":
        from src.application.assistant.runtime import handle_assistant_message

        return handle_assistant_message
    raise AttributeError(name)
