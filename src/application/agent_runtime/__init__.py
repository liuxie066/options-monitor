from __future__ import annotations

from src.application.agent_runtime.command_catalog import command_catalog_payload, command_specs
from src.application.agent_runtime.command_parser import parse_agent_command
from src.application.agent_runtime.llm_intent_schema import LLM_INTENT_SCHEMA_VERSION, llm_intent_json_schema, llm_intent_schema
from src.application.agent_runtime.settings import AgentRuntimeSettings, LlmTranslatorSettings

__all__ = [
    "AgentRuntimeSettings",
    "handle_agent_message",
    "LLM_INTENT_SCHEMA_VERSION",
    "LlmTranslatorSettings",
    "command_catalog_payload",
    "command_specs",
    "llm_intent_json_schema",
    "llm_intent_schema",
    "parse_agent_command",
]


def __getattr__(name: str):
    if name == "handle_agent_message":
        from src.application.agent_runtime.runtime import handle_agent_message

        return handle_agent_message
    raise AttributeError(name)
