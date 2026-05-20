from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.application.settings import build_effective_env


@dataclass(frozen=True)
class RuntimeRootResolution:
    runtime_root: Path
    source: str


def resolve_runtime_root(
    *,
    repo_root: str | Path,
    runtime_root: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> RuntimeRootResolution:
    """Resolve the canonical runtime root for stateful artifacts.

    The repo root remains the code/config execution root. The runtime root owns
    generated state such as output_runs, output_shared, output_accounts, locks,
    logs, and the option-position SQLite store.
    """
    if runtime_root is not None and str(runtime_root).strip():
        return RuntimeRootResolution(Path(runtime_root).expanduser().resolve(), "argument")

    env = build_effective_env(environ=environ).values
    env_root = str(env.get("OM_RUNTIME_ROOT") or "").strip()
    if env_root:
        return RuntimeRootResolution(Path(env_root).expanduser().resolve(), "env:OM_RUNTIME_ROOT")

    return RuntimeRootResolution(Path(repo_root).expanduser().resolve(), "repo_default")


__all__ = ["RuntimeRootResolution", "resolve_runtime_root"]
