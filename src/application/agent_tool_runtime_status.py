from __future__ import annotations

from typing import Any

from src.application.agent_tools import runtime_status_impl as _impl

_relative_path = _impl._relative_path
service_status_from_profile = _impl.service_status_from_profile


def runtime_status_tool(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    _impl.service_status_from_profile = service_status_from_profile
    return _impl.runtime_status_tool(*args, **kwargs)

__all__ = [
    "_relative_path",
    "runtime_status_tool",
    "service_status_from_profile",
]
