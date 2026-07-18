from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_fake_python(path: Path, *, version: str, log: Path | None = None) -> Path:
    status = 0 if tuple(int(part) for part in version.split(".")) >= (3, 12, 0) else 42
    log_line = f"printf '%s\\n' \"$*\" >> {shlex_quote(str(log))}\n" if log else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == \"-c\" ]]; then\n"
        f"  printf '%s\\n' {shlex_quote(version)}\n"
        f"  exit {status}\n"
        "fi\n"
        f"{log_line}"
        "printf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _copy_launcher_repo(tmp_path: Path, launcher: str = "om") -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / launcher, repo / launcher)
    shutil.copy2(ROOT / "scripts" / "python_runtime.sh", repo / "scripts" / "python_runtime.sh")
    (repo / launcher).chmod(0o755)
    return repo


def _runtime_env(fake_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("OM_PYTHON", None)
    env.pop("PYTHON", None)
    env["PATH"] = os.pathsep.join([str(fake_bin), "/usr/bin", "/bin"])
    return env


def test_om_python_override_explicitly_bypasses_incompatible_repo_venv(tmp_path: Path) -> None:
    repo = _copy_launcher_repo(tmp_path)
    _write_fake_python(repo / ".venv" / "bin" / "python", version="3.11.9")
    log = tmp_path / "override.log"
    override = _write_fake_python(tmp_path / "override-python", version="3.12.4", log=log)
    env = _runtime_env(tmp_path / "empty-bin")
    env["OM_PYTHON"] = str(override)

    result = subprocess.run(
        ["bash", str(repo / "om"), "config", "validate"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "-m src.interfaces.cli.main config validate" in log.read_text(encoding="utf-8")


def test_incompatible_repo_venv_blocks_python_and_path_fallback(tmp_path: Path) -> None:
    repo = _copy_launcher_repo(tmp_path)
    old = _write_fake_python(repo / ".venv" / "bin" / "python", version="3.11.9")
    fallback_log = tmp_path / "fallback.log"
    fallback = _write_fake_python(tmp_path / "fake-bin" / "python3.12", version="3.12.4", log=fallback_log)
    env = _runtime_env(fallback.parent)
    env["PYTHON"] = str(fallback)

    result = subprocess.run(
        ["bash", str(repo / "om"), "--help"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "Python >= 3.12 is required" in result.stderr
    assert str(old) in result.stderr
    assert "observed=3.11.9" in result.stderr
    assert not fallback_log.exists()


def test_missing_repo_venv_prefers_python312_and_forwards_agent_argv(tmp_path: Path) -> None:
    repo = _copy_launcher_repo(tmp_path, launcher="om-agent")
    log = tmp_path / "python312.log"
    python312 = _write_fake_python(tmp_path / "fake-bin" / "python3.12", version="3.12.2", log=log)
    env = _runtime_env(python312.parent)

    result = subprocess.run(
        ["bash", str(repo / "om-agent"), "spec"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "-m src.interfaces.agent.cli spec" in log.read_text(encoding="utf-8")


def test_old_python3_is_only_a_diagnostic_final_candidate(tmp_path: Path) -> None:
    repo = _copy_launcher_repo(tmp_path)
    old = _write_fake_python(tmp_path / "fake-bin" / "python3", version="3.9.6")
    env = _runtime_env(old.parent)

    result = subprocess.run(
        ["bash", str(repo / "om"), "--help"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "candidate=PATH python3" in result.stderr
    assert "observed=3.9.6" in result.stderr
    assert str(old) in result.stderr


def test_bootstrap_selector_rejects_interpreter_inside_target_venv(tmp_path: Path) -> None:
    target = tmp_path / "repo" / ".venv"
    target_python = _write_fake_python(target / "bin" / "python", version="3.12.3")
    env = _runtime_env(tmp_path / "empty-bin")
    env["PYTHON"] = str(target_python)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && om_select_bootstrap_python "$2"',
            "bootstrap-test",
            str(ROOT / "scripts" / "python_runtime.sh"),
            str(target),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "bootstrap interpreter must be outside" in result.stderr
    assert str(target) in result.stderr


def test_bootstrap_selector_rejects_interpreter_through_symlinked_target_venv(tmp_path: Path) -> None:
    shared = tmp_path / "cache" / "shared-venv"
    _write_fake_python(shared / "bin" / "python", version="3.12.3")
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".venv"
    target.symlink_to(shared, target_is_directory=True)
    env = _runtime_env(tmp_path / "empty-bin")
    env["PYTHON"] = str(target / "bin" / "python")

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && om_select_bootstrap_python "$2"',
            "bootstrap-test",
            str(ROOT / "scripts" / "python_runtime.sh"),
            str(target),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "bootstrap interpreter must be outside" in result.stderr
    assert f"target_venv={shared}" in result.stderr


def test_bootstrap_selector_ignores_existing_target_venv(tmp_path: Path) -> None:
    target = tmp_path / "repo" / ".venv"
    _write_fake_python(target / "bin" / "python", version="3.11.9")
    external = _write_fake_python(tmp_path / "fake-bin" / "python3.12", version="3.12.3")
    env = _runtime_env(external.parent)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && om_select_bootstrap_python "$2"',
            "bootstrap-test",
            str(ROOT / "scripts" / "python_runtime.sh"),
            str(target),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(external)


def test_src_and_domain_guards_are_python39_parseable_and_fail_fast() -> None:
    for package in ("src", "domain"):
        init_path = ROOT / package / "__init__.py"
        ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path), feature_version=(3, 9))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.version_info = (3, 9, 18); import {package}",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        assert "options-monitor requires Python >= 3.12" in result.stderr
        assert "observed=3.9.18" in result.stderr
