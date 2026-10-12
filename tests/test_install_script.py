from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Upgrade invocation shared by the release-switch tests, in the order they pass it.
_UPGRADE_ARGS = (
    "--version",
    "v9.9.10",
    "--repo-url",
    "https://example.invalid/options-monitor.git",
)


def test_install_script_is_shell_parseable_and_has_no_service_side_effects() -> None:
    script = ROOT / "scripts" / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)

    text = script.read_text(encoding="utf-8")
    assert "resolved latest release" in text
    assert "Default: latest published GitHub release, never main." in text
    assert "xcode-select --install" in text
    assert "python3.12-venv" in text
    assert "Node >=" not in text
    assert "systemctl enable" not in text
    assert "launchctl bootstrap" not in text
    assert "OM_FEISHU_BOT_APP_SECRET" not in text


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_installer_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fake-bin"
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "clone" ]]; then
  echo "unexpected git command: $*" >&2
  exit 2
fi
dest=""
for arg in "$@"; do
  dest="$arg"
done
mkdir -p "$dest/requirements" "$dest/constraints" "$dest/agent-runtime" "$dest/scripts"
: > "$dest/VERSION"
printf '9.9.%s\n' "${FAKE_RELEASE_PATCH:-9}" > "$dest/VERSION"
: > "$dest/requirements.txt"
: > "$dest/constraints.txt"
: > "$dest/requirements/server.txt"
: > "$dest/constraints/server.txt"
cat > "$dest/om" <<'SH'
#!/usr/bin/env bash
printf 'fake om %s\\n' "$*"
SH
cat > "$dest/om-agent" <<'SH'
#!/usr/bin/env bash
printf 'fake om-agent %s\\n' "$*"
SH
chmod +x "$dest/om" "$dest/om-agent"
if [[ "${FAKE_MISSING_OM_AGENT:-0}" == "1" ]]; then
  rm -f "$dest/om-agent"
fi
pi_version="${FAKE_PI_PACKAGE_VERSION:-0.84.2}"
cat > "$dest/agent-runtime/package.json" <<JSON
{"dependencies":{"@earendil-works/pi-agent-core":"$pi_version","@earendil-works/pi-ai":"$pi_version","@earendil-works/pi-session-backend-sqlite-node":"$pi_version"}}
JSON
cat > "$dest/agent-runtime/package-lock.json" <<JSON
{"lockfileVersion":3,"packages":{"":{"dependencies":{"@earendil-works/pi-agent-core":"$pi_version","@earendil-works/pi-ai":"$pi_version","@earendil-works/pi-session-backend-sqlite-node":"$pi_version"}}}}
JSON
cat > "$dest/scripts/pi_runtime_smoke.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'smoke %s\n' "$*" >> "${FAKE_SMOKE_LOG:?}"
[[ -x "$4" ]]
[[ -d "$2/agent-runtime/node_modules" ]]
if [[ "${FAKE_PI_IMPORT_FAIL:-0}" == "1" ]]; then
  echo "Pi import failed" >&2
  exit 31
fi
if [[ "${FAKE_SMOKE_FAIL:-0}" == "1" ]]; then
  echo "Pi smoke failed" >&2
  exit 32
fi
SH
chmod +x "$dest/scripts/pi_runtime_smoke.sh"
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf '{"tag_name":"v9.9.10"}\\n'
""",
    )
    _write_executable(
        bin_dir / "node",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_NODE_FAIL:-0}" == "1" ]]; then
  exit 33
fi
printf '%s\n' "${FAKE_NODE_VERSION:-v22.19.0}"
""",
    )
    _write_executable(
        bin_dir / "npm",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_NPM_LOG:?}"
if [[ "${1:-}" == "--version" ]]; then
  printf '10.8.2\n'
  exit 0
fi
if [[ "${FAKE_NPM_FAIL:-0}" == "1" ]]; then
  echo "npm install failed" >&2
  exit 34
fi
mkdir -p agent-runtime/node_modules
""",
    )
    fake_python = """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" ]]; then
  if [[ "$2" == *install_target_snapshot* ]]; then
    exec "${FAKE_REAL_PYTHON:?}" "$@"
  fi
  if [[ "$2" == *"sys.version_info"* ]]; then
    printf '3.12.0\n'
  fi
  if [[ "$2" == *"os.replace"* ]]; then
    if [[ "${FAKE_CURRENT_REPLACE_FAIL:-0}" == "1" ]]; then
      exit 35
    fi
    "${FAKE_REAL_PYTHON:?}" -c "$2" "$3" "$4"
  fi
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  venv="$3"
  mkdir -p "$venv/bin"
  cat > "$venv/bin/pip" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat > "$venv/bin/python" <<'SH'
#!/usr/bin/env bash
printf 'python import smoke\n' >> "${FAKE_SMOKE_LOG:?}"
[[ "${FAKE_SMOKE_FAIL:-0}" != "1" ]]
SH
  chmod +x "$venv/bin/pip" "$venv/bin/python"
  exit 0
fi
cat >/dev/null || true
exit 0
"""
    _write_executable(bin_dir / "python3", fake_python)
    _write_executable(bin_dir / "python3.12", fake_python)
    return bin_dir


def _installer_env(tmp_path: Path, *, path_prefix: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    path_parts = [str(_fake_installer_tools(tmp_path))]
    if path_prefix:
        path_parts.append(path_prefix)
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(path_parts)
    env["FAKE_NPM_LOG"] = str(tmp_path / "npm.log")
    env["FAKE_SMOKE_LOG"] = str(tmp_path / "smoke.log")
    env["FAKE_REAL_PYTHON"] = sys.executable
    return env


def _run_installer(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = ROOT / "scripts" / "install.sh"
    cmd = [
        "bash",
        str(script),
        "--version",
        "v9.9.9",
        "--prefix",
        str(tmp_path / "apps" / "options-monitor"),
        "--repo-url",
        "https://example.invalid/options-monitor.git",
        *args,
    ]
    return subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        env=env or _installer_env(tmp_path),
    )


def _run_installer_without_version(
    tmp_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = ROOT / "scripts" / "install.sh"
    return subprocess.run(
        [
            "bash",
            str(script),
            "--prefix",
            str(tmp_path / "apps" / "options-monitor"),
            *args,
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env or _installer_env(tmp_path),
    )


def _installed_baseline(tmp_path: Path) -> tuple[dict[str, str], subprocess.CompletedProcess[str]]:
    """Install the v9.9.9 baseline release the release-switch tests start from."""
    env = _installer_env(tmp_path)
    return env, _run_installer(tmp_path, env=env)


def test_install_script_resolves_latest_release_by_default(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    result = _run_installer_without_version(tmp_path, env=env)

    assert result.returncode == 0, result.stderr + result.stdout
    prefix = tmp_path / "apps" / "options-monitor"
    assert (prefix / "releases" / "v9.9.10").exists()
    assert os.readlink(prefix / "current") == str(prefix / "releases" / "v9.9.10")
    assert "[install] resolved latest release: v9.9.10" in result.stdout


def test_install_script_creates_user_cli_wrappers_by_default(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    result = _run_installer(tmp_path, env=env)

    assert result.returncode == 0, result.stderr + result.stdout
    home = tmp_path / "home"
    prefix = tmp_path / "apps" / "options-monitor"
    om = home / ".local" / "bin" / "om"
    om_agent = home / ".local" / "bin" / "om-agent"

    assert om.exists()
    assert om_agent.exists()
    assert "options-monitor managed wrapper" in om.read_text(encoding="utf-8")
    assert f"exec '{prefix}/current/om'" in om.read_text(encoding="utf-8")
    assert f"exec '{prefix}/current/om-agent'" in om_agent.read_text(encoding="utf-8")
    assert subprocess.check_output([str(om), "doctor"], text=True).strip() == "fake om doctor"
    assert subprocess.check_output([str(om_agent), "spec"], text=True).strip() == "fake om-agent spec"
    assert "Warning:" in result.stdout


def test_install_script_reinstall_current_release_is_idempotent(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    second = _run_installer(tmp_path, env=env)

    assert first.returncode == 0, first.stderr + first.stdout
    assert second.returncode == 0, second.stderr + second.stdout
    assert "[install] options-monitor v9.9.9 is already installed" in second.stdout
    assert not (tmp_path / "npm.log").exists()


def test_install_script_rejects_force_for_active_release_before_mutation(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    active = prefix / "releases" / "v9.9.9"
    marker = active / "preserve-me"
    marker.write_text("active\n", encoding="utf-8")
    before = (prefix / "current").resolve()
    active_cli = (active / "om").read_bytes()
    active_python = (active / ".venv" / "bin" / "python").read_bytes()
    wrapper = tmp_path / "home" / ".local" / "bin" / "om"
    wrapper_text = wrapper.read_text(encoding="utf-8")

    forced = _run_installer(tmp_path, "--force", env=env)

    assert first.returncode == 0, first.stderr + first.stdout
    assert forced.returncode != 0
    assert "refusing --force for the active current release" in forced.stderr
    assert (prefix / "current").resolve() == before
    assert marker.read_text(encoding="utf-8") == "active\n"
    assert (active / "VERSION").read_text(encoding="utf-8") == "9.9.9\n"
    assert (active / "om").read_bytes() == active_cli
    assert (active / ".venv" / "bin" / "python").read_bytes() == active_python
    assert wrapper.read_text(encoding="utf-8") == wrapper_text


def test_install_script_force_replaces_inactive_target_only_after_smoke(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    inactive = prefix / "releases" / "v9.9.9"
    inactive.mkdir(parents=True)
    marker = inactive / "preserve-until-ready"
    marker.write_text("old\n", encoding="utf-8")
    env["FAKE_SMOKE_FAIL"] = "1"

    failed = _run_installer(tmp_path, "--force", env=env)

    assert failed.returncode != 0
    assert marker.read_text(encoding="utf-8") == "old\n"
    assert not (prefix / "current").exists()

    env.pop("FAKE_SMOKE_FAIL")
    succeeded = _run_installer(tmp_path, "--force", env=env)
    assert succeeded.returncode == 0, succeeded.stderr + succeeded.stdout
    assert not marker.exists()
    assert (prefix / "current").resolve() == inactive.resolve()


def test_install_script_python_import_failures_preserve_active_release(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()

    for flag in ("FAKE_SMOKE_FAIL",):
        env["FAKE_RELEASE_PATCH"] = "10"
        env[flag] = "1"
        result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
        assert result.returncode != 0
        assert (prefix / "current").resolve() == active
        assert not (prefix / "releases" / "v9.9.10").exists()
        assert not list((prefix / "releases").glob(".v9.9.10.tmp.*"))
        env.pop(flag)

    assert first.returncode == 0, first.stderr + first.stdout


def test_install_script_cli_validation_failure_preserves_current_and_wrappers(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    current = prefix / "current"
    wrappers = [tmp_path / "home" / ".local" / "bin" / name for name in ("om", "om-agent")]
    previous_target = os.readlink(current)
    previous_wrappers = [(path.read_bytes(), path.stat().st_mode) for path in wrappers]
    env.update({"FAKE_RELEASE_PATCH": "10", "FAKE_MISSING_OM_AGENT": "1"})

    failed = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)

    assert first.returncode == 0, first.stderr + first.stdout
    assert failed.returncode != 0
    assert "target is not executable" in failed.stderr
    assert os.readlink(current) == previous_target
    assert [(path.read_bytes(), path.stat().st_mode) for path in wrappers] == previous_wrappers
    assert not (prefix / "releases" / "v9.9.10").exists()


def test_install_script_current_publish_failure_restores_wrappers(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    current = prefix / "current"
    wrappers = [tmp_path / "home" / ".local" / "bin" / name for name in ("om", "om-agent")]
    previous_target = os.readlink(current)
    previous_wrappers = [(path.read_bytes(), path.stat().st_mode) for path in wrappers]
    env.update({"FAKE_RELEASE_PATCH": "10", "FAKE_CURRENT_REPLACE_FAIL": "1"})

    failed = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)

    assert first.returncode == 0, first.stderr + first.stdout
    assert failed.returncode != 0
    assert "failed to atomically switch current release" in failed.stderr
    assert os.readlink(current) == previous_target
    assert [(path.read_bytes(), path.stat().st_mode) for path in wrappers] == previous_wrappers
    assert not (prefix / "releases" / "v9.9.10").exists()


def test_install_script_current_switch_has_no_missing_link_window(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    current = prefix / "current"
    previous_target = os.readlink(current)
    env["FAKE_RELEASE_PATCH"] = "10"
    ready = Event()
    stop = Event()
    missing_reads: list[BaseException] = []

    def _observe() -> None:
        ready.set()
        while not stop.is_set():
            try:
                os.readlink(current)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    missing_reads.append(exc)

    observer = Thread(target=_observe)
    observer.start()
    ready.wait(timeout=1)
    try:
        upgraded = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    finally:
        stop.set()
        observer.join(timeout=1)

    assert first.returncode == 0, first.stderr + first.stdout
    assert upgraded.returncode == 0, upgraded.stderr + upgraded.stdout
    assert previous_target != os.readlink(current)
    assert missing_reads == []


def test_install_script_accepts_old_node(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()
    env["FAKE_NODE_VERSION"] = "v22.18.9"
    env["FAKE_RELEASE_PATCH"] = "10"

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)

    assert first.returncode == 0, first.stderr
    assert result.returncode == 0, result.stderr
    assert (prefix / "current").resolve() != active
    assert not (tmp_path / "npm.log").exists()


@pytest.mark.parametrize(
    ("missing_tool", "expected"),
    (("node", "node was not found on PATH"), ("npm", "npm is required")),
)
def test_install_script_accepts_missing_node_or_npm(
    tmp_path: Path,
    missing_tool: str,
    expected: str,
) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    (fake_bin / missing_tool).unlink()
    env["PATH"] = os.pathsep.join((str(fake_bin), "/usr/bin", "/bin"))
    env["FAKE_RELEASE_PATCH"] = "10"

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)

    assert first.returncode == 0, first.stderr
    assert result.returncode == 0, result.stderr
    assert (prefix / "current").resolve() != active
    assert not (tmp_path / "npm.log").exists()




def test_install_script_no_install_cli_skips_wrappers(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, "--no-install-cli")

    assert result.returncode == 0, result.stderr + result.stdout
    assert not (tmp_path / "home" / ".local" / "bin" / "om").exists()
    assert f"'{tmp_path}/apps/options-monitor/current/om' setup init" in result.stdout


def test_first_pi_transition_stages_private_target_without_changing_production(
    tmp_path: Path,
) -> None:
    production = tmp_path / "production"
    old_release = production / "releases" / "3.5.1"
    old_runtime = old_release / "agent-runtime"
    old_runtime.mkdir(parents=True)
    (old_release / "VERSION").write_text("3.5.1\n", encoding="utf-8")
    (old_runtime / "package.json").write_text(
        '{"dependencies":{"@earendil-works/pi-agent-core":"0.84.2"}}\n',
        encoding="utf-8",
    )
    production_link = production / "current"
    production_link.symlink_to(old_release, target_is_directory=True)
    production_runtime = tmp_path / "production-runtime"
    production_runtime.mkdir()
    runtime_marker = production_runtime / "service.profile.json"
    runtime_marker.write_text('{"activation":"stopped"}\n', encoding="utf-8")
    before_link = production_link.resolve()
    before_runtime = runtime_marker.read_bytes()

    stage = tmp_path / "private-upgrade-control"
    env = _installer_env(tmp_path)
    env["FAKE_PI_PACKAGE_VERSION"] = "0.85.1"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install.sh"),
            "--version",
            "v9.9.9",
            "--prefix",
            str(stage),
            "--repo-url",
            "https://example.invalid/options-monitor.git",
            "--no-install-cli",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert production_link.resolve() == before_link
    assert runtime_marker.read_bytes() == before_runtime
    assert json.loads((old_runtime / "package.json").read_text(encoding="utf-8"))[
        "dependencies"
    ]["@earendil-works/pi-agent-core"] == "0.84.2"
    target_control = stage / "releases" / "v9.9.9"
    assert (stage / "current").resolve() == target_control.resolve()
    assert json.loads(
        (target_control / "agent-runtime" / "package.json").read_text(
            encoding="utf-8"
        )
    )["dependencies"]["@earendil-works/pi-agent-core"] == "0.85.1"
    assert not (tmp_path / "home" / ".local" / "bin" / "om").exists()
    assert not (tmp_path / "home" / ".local" / "bin" / "om-agent").exists()


def test_install_script_refuses_existing_non_om_wrapper_before_installing(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    existing = tmp_path / "home" / ".local" / "bin" / "om"
    _write_executable(existing, "#!/usr/bin/env bash\necho other\n")

    result = _run_installer(tmp_path, env=env)

    assert result.returncode != 0
    assert "refusing to overwrite existing non-options-monitor command" in result.stderr
    assert not (tmp_path / "apps" / "options-monitor" / "current").exists()


def test_install_script_updates_existing_managed_wrapper(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    existing = tmp_path / "home" / ".local" / "bin" / "om"
    _write_executable(
        existing,
        "#!/usr/bin/env bash\n# options-monitor managed wrapper\nexec /old/om \"$@\"\n",
    )

    result = _run_installer(tmp_path, env=env)

    assert result.returncode == 0, result.stderr + result.stdout
    text = existing.read_text(encoding="utf-8")
    assert "/old/om" not in text
    assert f'{tmp_path / "apps" / "options-monitor"}/current/om' in text


def test_install_script_force_overwrites_existing_non_om_wrapper(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    existing = tmp_path / "home" / ".local" / "bin" / "om"
    _write_executable(existing, "#!/usr/bin/env bash\necho other\n")

    result = _run_installer(tmp_path, "--force-cli-wrapper", env=env)

    assert result.returncode == 0, result.stderr + result.stdout
    text = existing.read_text(encoding="utf-8")
    assert "options-monitor managed wrapper" in text
    assert "echo other" not in text


def test_install_script_custom_bin_dir_uses_path_without_warning(tmp_path: Path) -> None:
    bin_dir = tmp_path / "custom-bin"
    env = _installer_env(tmp_path, path_prefix=str(bin_dir))

    result = _run_installer(tmp_path, "--bin-dir", str(bin_dir), env=env)

    assert result.returncode == 0, result.stderr + result.stdout
    assert (bin_dir / "om").exists()
    assert (bin_dir / "om-agent").exists()
    assert "Warning:" not in result.stdout
    assert f"'{bin_dir}/om' setup init" in result.stdout


def test_install_script_rejects_incompatible_explicit_python(tmp_path: Path) -> None:
    old_python = _write_executable(
        tmp_path / "python3.11",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" ]]; then
  printf '3.11.9\n'
  exit 42
fi
exit 0
""",
    )

    result = _run_installer(tmp_path, "--python", str(old_python))

    assert result.returncode != 0
    assert "Python >= 3.12 is required" in result.stderr
    assert f"executable={old_python}" in result.stderr
    assert "observed=3.11.9" in result.stderr
    assert not (tmp_path / "apps" / "options-monitor" / "current").exists()


def test_install_script_prefers_python312_when_no_override(tmp_path: Path) -> None:
    text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert 'command -v python3.12' in text
    assert 'PYTHON_BIN="python3.12"' in text
    assert 'sys.version_info >= (3, 12)' in text
    assert "os.replace" in text
    assert "ln -sfn" not in text


def test_install_script_real_venv_uses_final_paths(tmp_path: Path) -> None:
    case = tmp_path / "中文 安装"
    env = _installer_env(case)
    git = Path(env["PATH"].split(os.pathsep, 1)[0]) / "git"
    git.write_text(git.read_text() + "\nmkdir -p \"$dest/src/application/bot\"\n"
                   "touch \"$dest/src/__init__.py\" \"$dest/src/application/__init__.py\" "
                   "\"$dest/src/application/bot/__init__.py\" \"$dest/src/application/bot/host.py\" "
                   "\"$dest/src/application/bot/runtime.py\"\n", encoding="utf-8")
    env.update({"PIP_NO_INDEX": "1", "PIP_CONFIG_FILE": os.devnull})
    installed = _run_installer(case, "--python", sys.executable, env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    venv = (case / "apps/options-monitor/current/.venv").resolve()
    pip = subprocess.run([str(venv / "bin/pip"), "--version"], env=env, capture_output=True, text=True)
    assert pip.returncode == 0, pip.stderr
    activated = subprocess.check_output(
        ["bash", "-c", '. "$1"; printf "%s\\n" "$VIRTUAL_ENV"; command -v python',
         "probe", str(venv / "bin/activate")], env=env, text=True,
    ).splitlines()
    assert activated == [str(venv), str(venv / "bin/python")]


def test_install_script_publish_failure_allows_same_command_retry(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    assert first.returncode == 0, first.stderr
    env.update({"FAKE_RELEASE_PATCH": "10", "FAKE_CURRENT_REPLACE_FAIL": "1"})
    failed = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert failed.returncode != 0
    env.pop("FAKE_CURRENT_REPLACE_FAIL")
    retried = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert retried.returncode == 0, retried.stdout + retried.stderr


def _pause_wrapper_publish(env: dict[str, str], case: Path) -> Path:
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    marker = case / "publish-paused"
    env["FAKE_PUBLISH_MARKER"] = str(marker)
    _write_executable(fake_bin / "mv", """#!/usr/bin/env bash
set -eu
case "$*" in
  *'.om-agent.tmp.'*)
    touch "$FAKE_PUBLISH_MARKER"
    while :; do sleep .05; done
    ;;
esac
exec /bin/mv "$@"
""")
    return marker


def _interrupt_installer(case: Path, env: dict[str, str], marker: Path, *args: str,
                         interrupt: int = signal.SIGTERM) -> subprocess.CompletedProcess[str]:
    argv = ["bash", str(ROOT / "scripts/install.sh"), "--version", "v9.9.10",
            "--prefix", str(case / "apps/options-monitor"), "--repo-url",
            "https://example.invalid/options-monitor.git", *args]
    process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    deadline = time.monotonic() + 10
    try:
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), "installer never reached the injected boundary"
        os.killpg(process.pid, interrupt)
        out, err = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_install_script_sigterm_during_wrapper_publish_rolls_back_and_retries(
    tmp_path: Path, upgrade: bool, interrupt: int,
) -> None:
    env = _installer_env(tmp_path)
    prefix = tmp_path / "apps/options-monitor"
    wrappers = [tmp_path / "home/.local/bin" / name for name in ("om", "om-agent")]
    if upgrade:
        first = _run_installer(tmp_path, env=env)
        assert first.returncode == 0, first.stderr
        for wrapper in wrappers:
            wrapper.write_text(wrapper.read_text() + "# previous-wrapper\n", encoding="utf-8")
    previous = [(p.read_bytes(), p.stat().st_mode) if p.exists() else None for p in wrappers]
    env["FAKE_RELEASE_PATCH"] = "10"
    marker = _pause_wrapper_publish(env, tmp_path)
    interrupted = _interrupt_installer(tmp_path, env, marker, interrupt=interrupt)
    assert interrupted.returncode != 0
    assert [(p.read_bytes(), p.stat().st_mode) if p.exists() else None for p in wrappers] == previous
    assert not (prefix / "releases/v9.9.10").exists()
    assert (prefix / "current").exists() == upgrade
    if upgrade:
        assert (prefix / "current").resolve() == (prefix / "releases/v9.9.9").resolve()
    (Path(env["PATH"].split(os.pathsep, 1)[0]) / "mv").unlink()
    retry = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert retry.returncode == 0, retry.stdout + retry.stderr


def test_install_script_setup_opt_in_without_tty_only_prints_followup(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, "--setup")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "首次配置未执行" in result.stdout
    assert "setup init" in result.stdout
    assert "fake om setup init" not in result.stdout


@pytest.mark.parametrize("stage", ["clone", "venv", "pip", "facade", "first-wrapper", "second-wrapper"])
@pytest.mark.parametrize("upgrade", [False, True])
def test_install_failure_boundaries_restore_and_retry(tmp_path: Path, stage: str, upgrade: bool) -> None:
    env = _installer_env(tmp_path)
    if upgrade:
        assert _run_installer(tmp_path, env=env).returncode == 0
    prefix = tmp_path / "apps/options-monitor"
    wrappers = [tmp_path / "home/.local/bin" / name for name in ("om", "om-agent")]
    before = [p.read_bytes() if p.exists() else None for p in wrappers]
    env.update(FAKE_RELEASE_PATCH="10", FAKE_FAIL_STAGE=stage)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    for name in ("git", "python3.12", "python3"):
        path = fake_bin / name
        text = path.read_text()
        if name == "git":
            text = text.replace('dest=""', '[[ "${FAKE_FAIL_STAGE:-}" != clone ]] || exit 36\ndest=""', 1)
            text = text.replace("printf 'fake om-agent", '[[ "${FAKE_FAIL_STAGE:-}" != facade ]] || exit 36\nprintf \'fake om-agent', 1)
        else:
            text = text.replace('  venv="$3"', '  [[ "${FAKE_FAIL_STAGE:-}" != venv ]] || exit 36\n  venv="$3"', 1)
            text = text.replace("#!/usr/bin/env bash\nexit 0\nSH", '#!/usr/bin/env bash\n[[ "${FAKE_FAIL_STAGE:-}" != pip ]] || exit 36\nexit 0\nSH', 1)
        path.write_text(text)
    _write_executable(fake_bin / "mv", """#!/usr/bin/env bash
case "$*" in
  *'.om.tmp.'*) [[ "${FAKE_FAIL_STAGE:-}" != first-wrapper ]] || exit 36 ;;
  *'.om-agent.tmp.'*) [[ "${FAKE_FAIL_STAGE:-}" != second-wrapper ]] || exit 36 ;;
esac
exec /bin/mv "$@"
""")
    failed = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert failed.returncode != 0, failed.stdout
    assert [p.read_bytes() if p.exists() else None for p in wrappers] == before
    assert not (prefix / "releases/v9.9.10").exists()
    assert not (prefix / ".install.lock").exists()
    assert (prefix / "current").exists() == upgrade
    env.pop("FAKE_FAIL_STAGE")
    retry = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert retry.returncode == 0, retry.stderr


@pytest.mark.parametrize("after_replace", [False, True])
def test_install_sigterm_at_atomic_commit_obeys_actual_current(tmp_path: Path, after_replace: bool) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    marker = tmp_path / "commit-paused"
    env.update(FAKE_RELEASE_PATCH="10", FAKE_COMMIT_MARKER=str(marker))
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    for name in ("python3", "python3.12"):
        path = fake_bin / name
        text = path.read_text()
        pause = 'touch "$FAKE_COMMIT_MARKER"; while :; do sleep .05; done'
        replace = '"${FAKE_REAL_PYTHON:?}" -c "$2" "$3" "$4"'
        text = text.replace(replace, f"{replace}\n    {pause}" if after_replace else f"{pause}\n    {replace}")
        path.write_text(text)
    interrupted = _interrupt_installer(tmp_path, env, marker)
    assert interrupted.returncode != 0
    prefix = tmp_path / "apps/options-monitor"
    expected = "v9.9.10" if after_replace else "v9.9.9"
    assert (prefix / "current").resolve() == prefix / "releases" / expected
    assert (prefix / "releases/v9.9.10").exists() == after_replace
    assert not (prefix / ".install.lock").exists()


def test_install_sigterm_during_active_reinstall_restores_both_wrappers(tmp_path: Path) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    wrappers = [tmp_path / "home/.local/bin" / name for name in ("om", "om-agent")]
    for path in wrappers:
        path.write_text(path.read_text() + "# prior\n")
    before = [p.read_bytes() for p in wrappers]
    marker = _pause_wrapper_publish(env, tmp_path)
    interrupted = _interrupt_installer(tmp_path, env, marker, "--version", "v9.9.9")
    assert interrupted.returncode != 0
    assert [p.read_bytes() for p in wrappers] == before
    assert (tmp_path / "apps/options-monitor/current").is_dir()


def _run_terminal(argv: list[str], env: dict[str, str], reply: bytes = b"stop\n") -> tuple[int, str]:
    master, slave = pty.openpty()
    process = subprocess.Popen(argv, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if b"SETUP_INPUT:" in output and reply:
                    os.write(master, reply)
                    reply = b""
            elif process.poll() is not None:
                break
        return process.wait(timeout=2), output.decode("utf-8")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)


def test_documented_install_command_keeps_terminal_and_setup_cancel_keeps_install(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    git = fake_bin / "git"
    git.write_text(git.read_text().replace("printf 'fake om %s", """if [[ "$*" == 'setup init' ]]; then
  [[ -t 0 && -t 1 ]] || exit 37
  printf 'SETUP_INPUT:'
  read -r answer
  printf 'SETUP_CANCELLED:%s\\n' "$answer"
  exit 130
fi
printf 'fake om %s""", 1))
    env["FAKE_INSTALLER_SOURCE"] = str(ROOT / "scripts/install.sh")
    _write_executable(fake_bin / "curl", """#!/usr/bin/env bash
case "$*" in
  *raw.githubusercontent.com*) exec /bin/cp "$FAKE_INSTALLER_SOURCE" "$4" ;;
  *) printf '{"tag_name":"v9.9.9"}\\n' ;;
esac
""")
    env["TMPDIR"] = str(tmp_path)
    readme = (ROOT / "README.md").read_text()
    command = readme.split("## 安装", 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]
    status, output = _run_terminal(["bash", "-c", command], env)
    assert status == 130, output
    assert "SETUP_CANCELLED:stop" in output
    prefix = tmp_path / "home/apps/options-monitor"
    assert (prefix / "current").is_dir()
    assert not (prefix / ".install.lock").exists()
    assert not list(prefix.rglob("config.yaml"))


def test_install_wrapper_path_with_shell_characters_is_literal(tmp_path: Path) -> None:
    case = tmp_path / "中文 '安装 $HOME `false`"
    result = _run_installer(case)
    assert result.returncode == 0, result.stderr
    wrapper = case / "home/.local/bin/om"
    assert subprocess.check_output([str(wrapper), "doctor"], text=True).strip() == "fake om doctor"


def test_install_changed_wrapper_preserves_recovery_evidence(tmp_path: Path) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    env["FAKE_RELEASE_PATCH"] = "10"
    marker = _pause_wrapper_publish(env, tmp_path)
    mv = Path(env["PATH"].split(os.pathsep, 1)[0]) / "mv"
    wrapper = tmp_path / "home/.local/bin/om"
    mv.write_text(mv.read_text().replace('touch "$FAKE_PUBLISH_MARKER"',
                                        f"printf 'external edit\\n' > '{wrapper}'\n    touch \"$FAKE_PUBLISH_MARKER\""))
    interrupted = _interrupt_installer(tmp_path, env, marker)
    assert interrupted.returncode != 0
    assert "recovery incomplete" in interrupted.stderr
    assert wrapper.read_text() == "external edit\n"
    prefix = tmp_path / "apps/options-monitor"
    assert (prefix / "current").resolve() == prefix / "releases/v9.9.9"
    assert (prefix / ".install.lock/owner").is_file()
    transactions = list((prefix / "releases").glob(".v9.9.10.txn.*"))
    assert len(transactions) == 1
    assert (transactions[0] / "om.old").is_file()
    assert (transactions[0] / "om-agent.old").is_file()


def test_install_symlink_wrappers_are_restored_without_modifying_their_target(tmp_path: Path) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    wrappers = [tmp_path / "home/.local/bin" / name for name in ("om", "om-agent")]
    targets = []
    for path in wrappers:
        target = tmp_path / f"original-{path.name}"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        targets.append(target)
    env.update(FAKE_RELEASE_PATCH="10", FAKE_CURRENT_REPLACE_FAIL="1")
    failed = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert failed.returncode != 0
    assert [p.resolve() for p in wrappers] == targets
    assert all(p.is_symlink() for p in wrappers)
    assert not (tmp_path / "apps/options-monitor/.install.lock").exists()


@pytest.mark.parametrize("stage", ["clone", "venv", "backup"])
def test_install_sigterm_during_preparation_can_retry(tmp_path: Path, stage: str) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    env["FAKE_RELEASE_PATCH"] = "10"
    marker = tmp_path / "prepare-paused"
    env["FAKE_PREPARE_MARKER"] = str(marker)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    pause = 'touch "$FAKE_PREPARE_MARKER"; while :; do sleep .05; done'
    if stage == "clone":
        script = fake_bin / "git"
        script.write_text(script.read_text().replace('dest=""', pause + '\ndest=""', 1))
    elif stage == "venv":
        script = fake_bin / "python3.12"
        script.write_text(script.read_text().replace('  venv="$3"', pause + '\n  venv="$3"', 1))
    else:
        script = _write_executable(fake_bin / "cp", f"""#!/usr/bin/env bash
case "$*" in *'/om-agent.old'*) {pause} ;; esac
exec /bin/cp "$@"
""")
    original = (tmp_path / "home/.local/bin/om").read_bytes()
    interrupted = _interrupt_installer(tmp_path, env, marker)
    assert interrupted.returncode != 0
    prefix = tmp_path / "apps/options-monitor"
    assert (prefix / "current").resolve() == prefix / "releases/v9.9.9"
    assert (tmp_path / "home/.local/bin/om").read_bytes() == original
    assert not (prefix / "releases/v9.9.10").exists()
    assert not (prefix / ".install.lock").exists()
    # Recreate only the fixture transport/tools, then retry exactly the same command.
    _fake_installer_tools(tmp_path)
    if stage == "backup":
        script.unlink()
    retry = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert retry.returncode == 0, retry.stderr


def test_install_unknown_lock_is_preserved(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    lock = tmp_path / "apps/options-monitor/.install.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text("unverified-owner\n")
    failed = _run_installer(tmp_path, env=env)
    assert failed.returncode != 0
    assert "unverified lock" in failed.stderr
    assert (lock / "owner").read_text() == "unverified-owner\n"
    assert not list((lock.parent / "releases").iterdir())


def test_install_restore_failure_keeps_both_backups_and_old_current(tmp_path: Path) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    env["FAKE_RELEASE_PATCH"] = "10"
    marker = _pause_wrapper_publish(env, tmp_path)
    mv = Path(env["PATH"].split(os.pathsep, 1)[0]) / "mv"
    mv.write_text(mv.read_text().replace('case "$*" in', 'case "$*" in\n  *\'.om.restore.\'*) exit 38 ;;', 1))
    interrupted = _interrupt_installer(tmp_path, env, marker)
    assert interrupted.returncode != 0
    assert "recovery incomplete" in interrupted.stderr
    prefix = tmp_path / "apps/options-monitor"
    transaction = Path((prefix / ".install.lock/owner").read_text().strip())
    assert all((transaction / f"{name}.old").is_file() for name in ("om", "om-agent"))
    assert (prefix / "current").resolve() == prefix / "releases/v9.9.9"
    assert (prefix / "releases/v9.9.10/.om-install-owner").is_file()


@pytest.mark.parametrize("foreign_change", ["added", "modified", "root-symlink"])
@pytest.mark.parametrize("inactive_force", [False, True])
def test_install_foreign_release_content_preserved_on_cancel(
    tmp_path: Path, foreign_change: str, inactive_force: bool,
) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    prefix = tmp_path / "apps/options-monitor"
    target = prefix / "releases/v9.9.10"
    wrappers = [tmp_path / "home/.local/bin" / n for n in ("om", "om-agent")]
    before = [p.read_bytes() for p in wrappers]
    if inactive_force:
        target.mkdir()
        (target / "inactive-user-file").write_text("old inactive contents\n")
    env["FAKE_RELEASE_PATCH"] = "10"
    marker = tmp_path / "current-paused"
    env["FAKE_COMMIT_MARKER"] = str(marker)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    for name in ("python3", "python3.12"):
        python = fake_bin / name
        python.write_text(python.read_text().replace(
            '"${FAKE_REAL_PYTHON:?}" -c "$2" "$3" "$4"',
            'touch "$FAKE_COMMIT_MARKER"; while :; do sleep .05; done',
        ))
    process_args = ["bash", str(ROOT / "scripts/install.sh"), *_UPGRADE_ARGS,
                    "--prefix", str(prefix), *(["--force"] if inactive_force else [])]
    process = subprocess.Popen(process_args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    deadline = time.monotonic() + 10
    try:
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        owner_before = (target / ".om-install-owner").read_bytes()
        path = target / ("user-added.txt" if foreign_change == "added" else "VERSION")
        path.write_text("external data must remain\n")
        if foreign_change == "root-symlink":
            displaced = tmp_path / "displaced-release"
            target.rename(displaced)
            target.symlink_to(displaced, target_is_directory=True)
        os.killpg(process.pid, signal.SIGTERM)
        out, err = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    assert process.returncode != 0, out
    assert path.read_text() == "external data must remain\n"
    assert (target / ".om-install-owner").read_bytes() == owner_before
    assert "release content changed" in err
    assert "recovery incomplete" in err
    assert (prefix / "current").resolve() == prefix / "releases/v9.9.9"
    assert [p.read_bytes() for p in wrappers] == before
    transaction = Path((prefix / ".install.lock/owner").read_text().strip())
    assert all((transaction / f"{n}.old").is_file() for n in ("om", "om-agent"))
    if inactive_force:
        assert (transaction / "previous-release/inactive-user-file").read_text() == "old inactive contents\n"
    blocked = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert blocked.returncode != 0
    assert "unverified lock" in blocked.stderr
    assert path.read_text() == "external data must remain\n"


def test_install_preparation_failure_retains_contents_and_retries(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    for name in ("python3", "python3.12"):
        path = fake_bin / name
        path.write_text(path.read_text().replace('  venv="$3"', '''
  venv="$3"
  if [[ "${FAKE_PREPARE_FAIL:-}" == 1 ]]; then
    mkdir -p "$venv"
    printf 'external early contents\\n' > "$venv/../user-added.txt"
    exit 39
  fi
''', 1))
    env["FAKE_PREPARE_FAIL"] = "1"
    failed = _run_installer(tmp_path, env=env)
    assert failed.returncode != 0
    assert "failed release retained" in failed.stderr
    prefix = tmp_path / "apps/options-monitor"
    retained = list((prefix / "releases").glob(".v9.9.9.txn.*/failed-release/user-added.txt"))
    assert len(retained) == 1
    assert retained[0].read_text() == "external early contents\n"
    assert not (prefix / "releases/v9.9.9").exists()
    assert not (prefix / ".install.lock").exists()
    env.pop("FAKE_PREPARE_FAIL")
    retry = _run_installer(tmp_path, env=env)
    assert retry.returncode == 0, retry.stderr
    assert retained[0].read_text() == "external early contents\n"


def test_install_snapshot_handles_symlink_and_fifo_without_reading_them(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    git = fake_bin / "git"
    git.write_text(git.read_text() + '\nln -s missing-outside "$dest/foreign-link"\nmkfifo "$dest/foreign-pipe"\n')
    env["FAKE_CURRENT_REPLACE_FAIL"] = "1"
    failed = _run_installer(tmp_path, env=env)
    assert failed.returncode != 0
    prefix = tmp_path / "apps/options-monitor"
    retained = list((prefix / "releases").glob(".v9.9.9.txn.*/failed-release"))
    assert len(retained) == 1
    assert (retained[0] / "foreign-link").is_symlink()
    assert os.readlink(retained[0] / "foreign-link") == "missing-outside"
    assert (retained[0] / "foreign-pipe").exists()
    assert not (prefix / ".install.lock").exists()


def test_install_unverifiable_snapshot_preserves_target_and_lock(tmp_path: Path) -> None:
    env, installed = _installed_baseline(tmp_path)
    assert installed.returncode == 0
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    for name in ("python3", "python3.12"):
        path = fake_bin / name
        path.write_text(path.read_text().replace(
            'exec "${FAKE_REAL_PYTHON:?}" "$@"',
            '[[ "${4:-}" != matches ]] || exit 40\n    exec "${FAKE_REAL_PYTHON:?}" "$@"',
        ))
    env.update(FAKE_RELEASE_PATCH="10", FAKE_CURRENT_REPLACE_FAIL="1")
    failed = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert failed.returncode != 0
    assert "could not be verified" in failed.stderr
    prefix = tmp_path / "apps/options-monitor"
    assert (prefix / "releases/v9.9.10").is_dir()
    assert (prefix / ".install.lock/owner").is_file()
    assert (prefix / "current").resolve() == prefix / "releases/v9.9.9"
