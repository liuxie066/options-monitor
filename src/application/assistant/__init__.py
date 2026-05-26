from __future__ import annotations

from src.application.assistant.commands import (
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
from src.application.assistant.contracts import AssistantIntent, AssistantRequest, AssistantToolCall
from src.application.assistant.intent_arbitration import AssistantDecision, IntentArbitration, IntentCandidate
from src.application.assistant.intent_arbitrator import IntentArbitrator
from src.application.assistant.settings import AssistantSettings, LlmTranslatorSettings

__all__ = [
    "AssistantIntent",
    "AssistantCapabilitySpec",
    "AssistantCommandSpec",
    "AssistantDecision",
    "IntentArbitrator",
    "IntentArbitration",
    "IntentCandidate",
    "AssistantRequest",
    "AssistantSettings",
    "AssistantToolCall",
    "LlmTranslatorSettings",
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
