from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

import pytest

from src.application.agent_tool_contracts import AgentToolError


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


def test_resolve_runtime_root_uses_safe_user_record_after_env(tmp_path: Path) -> None:
    from src.application.runtime_paths import resolve_runtime_root, runtime_root_record_path

    root = tmp_path / "runtime"
    root.mkdir()
    (root / "config.yaml").write_text("accounts: {}\n", encoding="utf-8")
    record = runtime_root_record_path(user_home=tmp_path)
    record.parent.mkdir(parents=True)
    record.write_text(str(root) + "\n", encoding="utf-8")
    record.chmod(0o600)

    found = resolve_runtime_root(repo_root=tmp_path / "repo", environ={}, user_home=tmp_path)
    overridden = resolve_runtime_root(
        repo_root=tmp_path / "repo",
        environ={"OM_RUNTIME_ROOT": str(tmp_path / "service")},
        user_home=tmp_path,
    )
    assert (found.runtime_root, found.source) == (root, "user_record")
    assert (overridden.runtime_root, overridden.source) == (tmp_path / "service", "env:OM_RUNTIME_ROOT")


def test_resolve_runtime_root_fails_closed_on_invalid_record(tmp_path: Path) -> None:
    from src.application.runtime_paths import resolve_runtime_root, runtime_root_record_path

    record = runtime_root_record_path(user_home=tmp_path)
    record.parent.mkdir(parents=True)
    record.write_text(str(tmp_path / "missing") + "\n", encoding="utf-8")
    record.chmod(0o600)
    with pytest.raises(AgentToolError, match="invalid runtime root record"):
        resolve_runtime_root(repo_root=tmp_path / "repo", environ={}, user_home=tmp_path)

    record.unlink()
    record.symlink_to(tmp_path / "other")
    with pytest.raises(AgentToolError, match="invalid runtime root record"):
        resolve_runtime_root(repo_root=tmp_path / "repo", environ={}, user_home=tmp_path)


def test_new_process_uses_record_for_config_and_trusted_run_scope(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "config.yaml").write_text("accounts: {}\n", encoding="utf-8")
    from src.application.runtime_paths import runtime_root_record_path

    record = runtime_root_record_path(user_home=tmp_path)
    record.parent.mkdir(parents=True)
    record.write_text(str(runtime) + "\n", encoding="utf-8")
    record.chmod(0o600)
    env = dict(os.environ)
    env.pop("OM_RUNTIME_ROOT", None)
    env.pop("OM_ENV_FILE", None)
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "from src.application.runtime_paths import resolve_runtime_root; "
         "from src.application.agent_tool_config import resolve_runtime_config_path; "
         "from src.application.agent_tools.project_runs import _scope; "
         "r=resolve_runtime_root(repo_root='.', ); "
         "assert r.source == 'user_record'; "
         "assert _scope(r, ['lx'], 'lx', 'us') == ['lx']; "
         "print(resolve_runtime_config_path(config_key='us'))"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(runtime / "config.us.json")
