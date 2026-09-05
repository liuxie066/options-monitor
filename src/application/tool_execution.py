from __future__ import annotations

from typing import Any

from src.application.agent_tools.permissions import write_gate_error
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import (
    build_agent_spec,
    get_tool_definition,
)


def build_tool_manifest() -> dict[str, Any]:
    return build_agent_spec()


def execute_tool(
    tool_name: str,
    payload: dict[str, Any] | None = None,
    *,
    raise_unexpected: bool = False,
) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    definition = get_tool_definition(name)
    if definition is None:
        err = AgentToolError(
            code="INPUT_ERROR",
            message=f"unknown tool: {tool_name}",
            hint="Call `om-agent spec` to inspect supported tools.",
        )
        return build_response(tool_name=str(tool_name or ""), ok=False, error=build_error_payload(err))

    payload_dict = dict(payload or {})
    gate_error = write_gate_error(definition, payload_dict)
    if gate_error is not None:
        return build_response(tool_name=name, ok=False, error=build_error_payload(gate_error))

    try:
        data, warnings, meta = definition.call(payload_dict)
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
    except SystemExit as exc:
        err = AgentToolError(
            code="CONFIG_ERROR",
            message=str(exc) or "tool configuration rejected",
        )
        return build_response(
            tool_name=name,
            ok=False,
            error=build_error_payload(err),
        )
    except Exception as exc:
        if raise_unexpected:
            raise
        err = AgentToolError(
            code="INTERNAL_ERROR",
            message=f"{type(exc).__name__}: {exc}",
        )
        return build_response(
            tool_name=name,
            ok=False,
            error=build_error_payload(err),
        )
