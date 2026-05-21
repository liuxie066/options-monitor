from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import AgentToolDefinition, build_agent_spec, get_tool_definition, write_tools_enabled_from_env


def build_tool_manifest() -> dict[str, Any]:
    return build_agent_spec()


def _tool_write_requested(definition: AgentToolDefinition, payload: dict[str, Any]) -> bool:
    name = definition.name
    if name == "ai_cofunder":
        return _truthy(payload.get("write_outputs"))
    if definition.read_only:
        return False
    if name == "version_update":
        return bool(payload.get("apply", False))
    if name == "manage_symbols":
        action = str(payload.get("action") or "list").strip().lower()
        return action != "list" and not bool(payload.get("dry_run", False))
    if bool(payload.get("dry_run", False)):
        return False
    return bool(definition.side_effects or definition.requires_confirm or definition.risk_level != "read_only")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _write_gate_error(definition: AgentToolDefinition, payload: dict[str, Any]) -> AgentToolError | None:
    if not _tool_write_requested(definition, payload):
        return None
    if not write_tools_enabled_from_env():
        return AgentToolError(
            code="PERMISSION_DENIED",
            message=f"{definition.name} write mode is disabled",
            hint="Set OM_AGENT_ENABLE_WRITE_TOOLS=true and pass confirm=true for non-dry-run writes.",
        )
    if definition.requires_confirm and not bool(payload.get("confirm", False) or payload.get("yes", False)):
        return AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message=f"confirm=true is required for {definition.name} non-dry-run writes",
            hint="Run the tool in dry-run/preview mode first, then retry with confirm=true or yes=true only when the write is intended.",
        )
    return None


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

    payload_dict = dict(payload or {})
    gate_error = _write_gate_error(definition, payload_dict)
    if gate_error is not None:
        return build_response(tool_name=name, ok=False, error=build_error_payload(gate_error))

    from src.application.agent_tool_handlers import TOOL_HANDLERS

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        err = AgentToolError(
            code="INTERNAL_ERROR",
            message=f"registered tool has no handler: {name}",
        )
        return build_response(tool_name=name, ok=False, error=build_error_payload(err))

    try:
        data, warnings, meta = handler(payload_dict)
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
