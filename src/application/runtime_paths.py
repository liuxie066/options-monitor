from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import stat
from typing import Iterator

from src.application.agent_tool_contracts import AgentToolError
from src.application.settings import build_effective_env


_command_runtime_root: ContextVar[RuntimeRootResolution | None] = ContextVar("command_runtime_root", default=None)


@contextmanager
def runtime_root_scope(runtime_root: str | Path | None, *, source: str = "argument") -> Iterator[None]:
    """Bind nested consumers to the instance explicitly selected by the caller."""
    if runtime_root is None:
        yield
        return
    token = _command_runtime_root.set(RuntimeRootResolution(Path(runtime_root).expanduser().resolve(), source))
    try:
        yield
    finally:
        _command_runtime_root.reset(token)


@dataclass(frozen=True)
class RuntimeRootResolution:
    runtime_root: Path
    source: str


def resolve_runtime_root(
    *,
    repo_root: str | Path,
    runtime_root: str | Path | None = None,
    environ: dict[str, str] | None = None,
    user_home: str | Path | None = None,
) -> RuntimeRootResolution:
    """Resolve the canonical runtime root for stateful artifacts.

    The repo root remains the code/config execution root. The runtime root owns
    generated state such as output_runs, output_shared, output_accounts, locks,
    logs, and the option-position SQLite store.
    """
    if runtime_root is not None and str(runtime_root).strip():
        return RuntimeRootResolution(Path(runtime_root).expanduser().resolve(), "argument")

    scoped_root = _command_runtime_root.get()
    if scoped_root is not None:
        return scoped_root

    env = build_effective_env(environ=environ).values
    env_root = str(env.get("OM_RUNTIME_ROOT") or "").strip()
    if env_root:
        return RuntimeRootResolution(Path(env_root).expanduser().resolve(), "env:OM_RUNTIME_ROOT")

    # Explicit test/service environments do not inherit the operator's home record.
    if user_home is not None or environ is None:
        record = runtime_root_record_path(user_home=user_home)
        if record.exists() or record.is_symlink():
            return RuntimeRootResolution(read_runtime_root_record(record), "user_record")

    return RuntimeRootResolution(Path(repo_root).expanduser().resolve(), "repo_default")


def runtime_root_record_path(*, user_home: str | Path | None = None) -> Path:
    return Path(user_home if user_home is not None else Path.home()).expanduser() / ".config" / "options-monitor" / "runtime-root"


def read_runtime_root_record(record: Path, *, require_config: bool = True) -> Path:
    try:
        descriptor = os.open(record, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("record must be an owner-controlled regular file")
            contents = os.read(descriptor, 4097)
            if info.st_size > 4096 or len(contents) > 4096:
                raise ValueError("record is too large")
            lines = contents.decode("utf-8").splitlines()
        finally:
            os.close(descriptor)
        if len(lines) != 1 or not lines[0].strip() or lines[0] != lines[0].strip():
            raise ValueError("record must contain one absolute directory")
        root = Path(lines[0])
        if not root.is_absolute() or (require_config and (not root.is_dir() or not (root / "config.yaml").is_file())):
            raise ValueError("record target must contain config.yaml")
        return root.resolve()
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"invalid runtime root record: {record}",
            hint="Inspect the user runtime-root record and its config.yaml; fix or remove the record explicitly.",
            details={"reason": str(exc)},
        ) from exc


__all__ = ["RuntimeRootResolution", "resolve_runtime_root", "runtime_root_scope", "runtime_root_record_path", "read_runtime_root_record"]
