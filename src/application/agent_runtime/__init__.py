from __future__ import annotations

from src.application.assistant.commands import capability_catalog_payload, capability_specs, command_catalog_payload, command_specs
from src.application.assistant.command_parser import parse_agent_command, parse_assistant_command
from src.application.assistant.llm_intent_schema import LLM_INTENT_SCHEMA_VERSION, llm_intent_json_schema, llm_intent_schema
from src.application.assistant.settings import AgentRuntimeSettings, AssistantSettings, LlmTranslatorSettings

__all__ = [
    "AgentRuntimeSettings",
    "AssistantSettings",
    "handle_agent_message",
    "LLM_INTENT_SCHEMA_VERSION",
    "LlmTranslatorSettings",
    "capability_catalog_payload",
    "capability_specs",
    "command_catalog_payload",
    "command_specs",
    "llm_intent_json_schema",
    "llm_intent_schema",
    "parse_agent_command",
    "parse_assistant_command",
]


def __getattr__(name: str):
    if name == "handle_agent_message":
        from src.application.assistant.runtime import handle_agent_message

        return handle_agent_message
    raise AttributeError(name)
