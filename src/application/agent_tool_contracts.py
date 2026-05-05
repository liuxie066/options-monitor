from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"


def mask_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    name = Path(path).name
    return f".../{name}" if name else "..."


@dataclass(frozen=True)
class AgentToolError(Exception):
    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def build_error_payload(err: AgentToolError) -> dict[str, Any]:
    payload = {
        "code": str(err.code),
        "message": str(err.message),
    }
    if err.hint:
        payload["hint"] = str(err.hint)
    if isinstance(err.details, dict) and err.details:
        payload["details"] = dict(err.details)
    return payload


def build_response(
    *,
    tool_name: str,
    ok: bool,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_name": str(tool_name),
        "ok": bool(ok),
        "data": dict(data or {}),
        "warnings": [str(x) for x in (warnings or []) if str(x).strip()],
        "error": dict(error or {}) if error else None,
        "meta": dict(meta or {}),
    }
