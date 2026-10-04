"""Bind ordinary settings to one CLI invocation, including nested menu calls."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator

from src.application.runtime_paths import read_runtime_root_record, runtime_root_record_path, runtime_root_scope
from src.application.settings import bootstrap_process_env


def _option(argv: list[str], *names: str) -> str | None:
    value = None
    for index, word in enumerate(argv):
        for name in names:
            if word.startswith(name + "="):
                value = word.split("=", 1)[1]
            elif word == name and index + 1 < len(argv):
                value = argv[index + 1]
    return value


@contextmanager
def command_environment(
    argv: list[str], *, repo_root: Path, discover_local: bool = False,
    user_home: Path | None = None,
) -> Iterator[None]:
    # Menus and help must not retain settings while dispatching another command.
    if (not argv or argv[:2] == ["setup", "init"] or argv[0] == "help"
            or any(arg in {"--help", "-h"} for arg in argv)):
        yield
        return
    before = dict(os.environ)
    env_file = _option(argv, "--env-file") or before.get("OM_ENV_FILE")
    root = _option(argv, "--runtime-root", "--rebuild-runtime-root")
    source = "argument"
    config = _option(argv, "--config-yaml", "--config-path", "--config")
    if not root and before.get("OM_RUNTIME_ROOT"):
        root = before["OM_RUNTIME_ROOT"]
        source = "env:OM_RUNTIME_ROOT"
    if not root and config:
        root = str(Path(config).expanduser().resolve().parent)
    allow_discovery = discover_local and "--no-local-env-file" not in argv
    if not root and allow_discovery:
        record = runtime_root_record_path(user_home=user_home)
        if record.exists() or record.is_symlink():
            root = str(read_runtime_root_record(record))
            source = "user_record"
    if not env_file and root and "--no-local-env-file" not in argv:
        candidate = Path(root).expanduser() / "options-monitor.env"
        if candidate.is_file():
            env_file = str(candidate)
    try:
        try:
            if env_file or allow_discovery:
                bootstrap_process_env(
                    repo_root=repo_root, env_file=env_file,
                    include_local_env_file=allow_discovery and not root,
                )
            # The selected instance takes precedence over an env-file's runtime pointer.
            if root:
                os.environ["OM_RUNTIME_ROOT"] = str(Path(root).expanduser().resolve())
        finally:
            # Bootstrap can fail after setting some keys; those also belong to us.
            changed = {key for key in before.keys() | os.environ.keys() if before.get(key) != os.environ.get(key)}
        with runtime_root_scope(root, source=source):
            yield
    finally:
        # Restore only our bootstrap keys; unrelated command state is not ours.
        for key in changed:
            if key in before:
                os.environ[key] = before[key]
            else:
                os.environ.pop(key, None)
