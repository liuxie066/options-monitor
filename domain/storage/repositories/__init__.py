from __future__ import annotations

from importlib import import_module


_MODULE_EXPORTS = {
    "report_repo",
    "run_repo",
    "state_repo",
}

__all__ = sorted(_MODULE_EXPORTS)


def __getattr__(name: str):
    if name not in _MODULE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return import_module(f".{name}", __name__)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
