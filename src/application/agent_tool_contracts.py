from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
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


# `frozen=True` is meant to freeze the *payload* fields, and it must not extend to the
# slots `BaseException` owns. Python assigns those from the interpreter side:
# `contextlib._GeneratorContextManager.__exit__` does `exc.__traceback__ = traceback`, and
# `BaseException.add_note` sets `__notes__` the same way. Left frozen, either one raises
# `FrozenInstanceError` *while handling the real error*, so the agent tool call reports
# "cannot assign to field '__traceback__'" instead of the code/message that failed.
#
# The assignment has to happen after the decorator runs: `dataclasses._process_class`
# rejects a class-body `__setattr__` under `frozen=True` with
# "TypeError: Cannot overwrite attribute __setattr__ in class AgentToolError".
_EXCEPTION_SLOTS = frozenset(
    {"args", "__traceback__", "__cause__", "__context__", "__suppress_context__", "__notes__"}
)


def _set_exception_slot_or_raise(self: AgentToolError, name: str, value: Any) -> None:
    if name in _EXCEPTION_SLOTS:
        BaseException.__setattr__(self, name, value)
        return
    raise FrozenInstanceError(f"cannot assign to field {name!r}")


AgentToolError.__setattr__ = _set_exception_slot_or_raise


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
