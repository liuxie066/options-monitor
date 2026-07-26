from __future__ import annotations

from typing import Any

from src.application.agent_tools.diagnostics import _runtime_status_tool


def read_runtime_status(_tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Read the existing runtime-status application facade without the tool registry."""

    try:
        data, warnings, meta = _runtime_status_tool(payload)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "data": data,
        "warnings": warnings,
        "meta": meta,
    }


__all__ = ["read_runtime_status"]
