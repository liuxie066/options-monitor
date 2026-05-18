from __future__ import annotations

from pathlib import Path


def test_resolve_runtime_root_prefers_argument(tmp_path: Path) -> None:
    from src.application.runtime_paths import resolve_runtime_root

    repo = tmp_path / "repo"
    arg = tmp_path / "runtime-arg"
    env = {"OM_RUNTIME_ROOT": str(tmp_path / "runtime-env")}

    resolved = resolve_runtime_root(repo_root=repo, runtime_root=arg, environ=env)

    assert resolved.runtime_root == arg.resolve()
    assert resolved.source == "argument"


def test_resolve_runtime_root_uses_env_then_repo_default(tmp_path: Path) -> None:
    from src.application.runtime_paths import resolve_runtime_root

    repo = tmp_path / "repo"
    env_runtime = tmp_path / "runtime-env"

    from_env = resolve_runtime_root(repo_root=repo, environ={"OM_RUNTIME_ROOT": str(env_runtime)})
    defaulted = resolve_runtime_root(repo_root=repo, environ={})

    assert from_env.runtime_root == env_runtime.resolve()
    assert from_env.source == "env:OM_RUNTIME_ROOT"
    assert defaulted.runtime_root == repo.resolve()
    assert defaulted.source == "repo_default"
