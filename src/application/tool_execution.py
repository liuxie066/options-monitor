from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import build_agent_spec, get_tool_definition


def build_tool_manifest() -> dict[str, Any]:
    return build_agent_spec()


def execute_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    definition = get_tool_definition(name)
    if definition is None:
        err = AgentToolError(
            code="INPUT_ERROR",
            message=f"unknown tool: {tool_name}",
            hint="Call `om-agent spec` to inspect supported tools.",
        )
        return build_response(tool_name=str(tool_name or ""), ok=False, error=build_error_payload(err))

    from src.application.agent_tool_handlers import TOOL_HANDLERS

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        err = AgentToolError(
            code="INTERNAL_ERROR",
            message=f"registered tool has no handler: {name}",
        )
        return build_response(tool_name=name, ok=False, error=build_error_payload(err))

    try:
        data, warnings, meta = handler(dict(payload or {}))
        return build_response(
            tool_name=name,
            ok=True,
            data=data,
            warnings=warnings,
            meta=meta,
        )
    except AgentToolError as err:
        return build_response(
            tool_name=name,
            ok=False,
            error=build_error_payload(err),
        )
    except Exception as exc:
        err = AgentToolError(
            code="INTERNAL_ERROR",
            message=f"{type(exc).__name__}: {exc}",
        )
        return build_response(
            tool_name=name,
            ok=False,
            error=build_error_payload(err),
        )
