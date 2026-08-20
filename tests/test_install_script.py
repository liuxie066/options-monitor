from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_install_script_is_shell_parseable_and_has_no_service_side_effects() -> None:
    script = ROOT / "scripts" / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)

    text = script.read_text(encoding="utf-8")
    assert "resolved latest release" in text
    assert "Default: latest published GitHub release, never main." in text
    assert "xcode-select --install" in text
    assert "python3.12-venv" in text
    assert "Node >= 22.19.0" in text
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
cat > "$dest/agent-runtime/package.json" <<'JSON'
{"dependencies":{"@earendil-works/pi-agent-core":"0.84.2","@earendil-works/pi-ai":"0.84.2","@earendil-works/pi-session-backend-sqlite-node":"0.84.2"}}
JSON
cat > "$dest/agent-runtime/package-lock.json" <<'JSON'
{"lockfileVersion":3,"packages":{"":{"dependencies":{"@earendil-works/pi-agent-core":"0.84.2","@earendil-works/pi-ai":"0.84.2","@earendil-works/pi-session-backend-sqlite-node":"0.84.2"}}}}
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
exit 0
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
    assert f'exec "{prefix}/current/om" "$@"' in om.read_text(encoding="utf-8")
    assert f'exec "{prefix}/current/om-agent" "$@"' in om_agent.read_text(encoding="utf-8")
    assert subprocess.check_output([str(om), "doctor"], text=True).strip() == "fake om doctor"
    assert subprocess.check_output([str(om_agent), "spec"], text=True).strip() == "fake om-agent spec"
    assert "Warning:" in result.stdout


def test_install_script_reinstall_current_release_is_idempotent(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    second = _run_installer(tmp_path, env=env)

    assert first.returncode == 0, first.stderr + first.stdout
    assert second.returncode == 0, second.stderr + second.stdout
    assert "[install] options-monitor v9.9.9 is already installed" in second.stdout
    assert (tmp_path / "npm.log").read_text(encoding="utf-8").splitlines() == [
        "ci --omit=dev --ignore-scripts --prefix agent-runtime"
    ]


def test_install_script_rejects_force_for_active_release_before_mutation(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    prefix = tmp_path / "apps" / "options-monitor"
    active = prefix / "releases" / "v9.9.9"
    marker = active / "agent-runtime" / "node_modules" / "preserve-me"
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


def test_install_script_pi_failures_preserve_active_release(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()

    for flag in ("FAKE_NPM_FAIL", "FAKE_PI_IMPORT_FAIL", "FAKE_SMOKE_FAIL"):
        env["FAKE_RELEASE_PATCH"] = "10"
        env[flag] = "1"
        result = _run_installer_without_version(
            tmp_path,
            "--version",
            "v9.9.10",
            "--repo-url",
            "https://example.invalid/options-monitor.git",
            env=env,
        )
        assert result.returncode != 0
        assert (prefix / "current").resolve() == active
        assert not (prefix / "releases" / "v9.9.10").exists()
        assert not list((prefix / "releases").glob(".v9.9.10.tmp.*"))
        env.pop(flag)

    assert first.returncode == 0, first.stderr + first.stdout


def test_install_script_cli_validation_failure_preserves_current_and_wrappers(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    prefix = tmp_path / "apps" / "options-monitor"
    current = prefix / "current"
    wrappers = [tmp_path / "home" / ".local" / "bin" / name for name in ("om", "om-agent")]
    previous_target = os.readlink(current)
    previous_wrappers = [(path.read_bytes(), path.stat().st_mode) for path in wrappers]
    env.update({"FAKE_RELEASE_PATCH": "10", "FAKE_MISSING_OM_AGENT": "1"})

    failed = _run_installer_without_version(
        tmp_path,
        "--version",
        "v9.9.10",
        "--repo-url",
        "https://example.invalid/options-monitor.git",
        env=env,
    )

    assert first.returncode == 0, first.stderr + first.stdout
    assert failed.returncode != 0
    assert "target is not executable" in failed.stderr
    assert os.readlink(current) == previous_target
    assert [(path.read_bytes(), path.stat().st_mode) for path in wrappers] == previous_wrappers
    assert not (prefix / "releases" / "v9.9.10").exists()


def test_install_script_current_publish_failure_restores_wrappers(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    prefix = tmp_path / "apps" / "options-monitor"
    current = prefix / "current"
    wrappers = [tmp_path / "home" / ".local" / "bin" / name for name in ("om", "om-agent")]
    previous_target = os.readlink(current)
    previous_wrappers = [(path.read_bytes(), path.stat().st_mode) for path in wrappers]
    env.update({"FAKE_RELEASE_PATCH": "10", "FAKE_CURRENT_REPLACE_FAIL": "1"})

    failed = _run_installer_without_version(
        tmp_path,
        "--version",
        "v9.9.10",
        "--repo-url",
        "https://example.invalid/options-monitor.git",
        env=env,
    )

    assert first.returncode == 0, first.stderr + first.stdout
    assert failed.returncode != 0
    assert "failed to atomically switch current release" in failed.stderr
    assert os.readlink(current) == previous_target
    assert [(path.read_bytes(), path.stat().st_mode) for path in wrappers] == previous_wrappers
    assert (prefix / "releases" / "v9.9.10").is_dir()


def test_install_script_current_switch_has_no_missing_link_window(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
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
        upgraded = _run_installer_without_version(
            tmp_path,
            "--version",
            "v9.9.10",
            "--repo-url",
            "https://example.invalid/options-monitor.git",
            env=env,
        )
    finally:
        stop.set()
        observer.join(timeout=1)

    assert first.returncode == 0, first.stderr + first.stdout
    assert upgraded.returncode == 0, upgraded.stderr + upgraded.stdout
    assert previous_target != os.readlink(current)
    assert missing_reads == []


def test_install_script_rejects_old_node_without_changing_current(tmp_path: Path) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()
    env["FAKE_NODE_VERSION"] = "v22.18.9"
    env["FAKE_RELEASE_PATCH"] = "10"

    result = _run_installer_without_version(
        tmp_path,
        "--version",
        "v9.9.10",
        "--repo-url",
        "https://example.invalid/options-monitor.git",
        env=env,
    )

    assert first.returncode == 0, first.stderr + first.stdout
    assert result.returncode != 0
    assert "Node >= 22.19.0 is required" in result.stderr
    assert (prefix / "current").resolve() == active
    assert not (prefix / "releases" / "v9.9.10").exists()


@pytest.mark.parametrize(
    ("missing_tool", "expected"),
    (("node", "node was not found on PATH"), ("npm", "npm is required")),
)
def test_install_script_rejects_missing_node_or_npm_without_changing_current(
    tmp_path: Path,
    missing_tool: str,
    expected: str,
) -> None:
    env = _installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    (fake_bin / missing_tool).unlink()
    env["PATH"] = os.pathsep.join((str(fake_bin), "/usr/bin", "/bin"))
    env["FAKE_RELEASE_PATCH"] = "10"

    result = _run_installer_without_version(
        tmp_path,
        "--version",
        "v9.9.10",
        "--repo-url",
        "https://example.invalid/options-monitor.git",
        env=env,
    )

    assert first.returncode == 0, first.stderr + first.stdout
    assert result.returncode != 0
    assert expected in result.stderr
    assert (prefix / "current").resolve() == active
    assert not (prefix / "releases" / "v9.9.10").exists()


def test_pi_runtime_smoke_is_hermetic_and_leaves_no_session_state(tmp_path: Path) -> None:
    inherited_runtime = tmp_path / "inherited-runtime"
    inherited_runtime.mkdir()
    inherited_session = inherited_runtime / "pi_sessions.sqlite3"
    inherited_env = tmp_path / "inherited.env"
    inherited_env.write_text(f"OM_PI_SESSION_DB={inherited_session}\n", encoding="utf-8")
    smoke_tmp = tmp_path / "smoke-tmp"
    smoke_tmp.mkdir()
    python_injection = tmp_path / "python-injection"
    python_injection.mkdir()
    injection_sentinel = tmp_path / "sitecustomize-ran"
    (python_injection / "sitecustomize.py").write_text(
        "import os; open(os.environ['OM_TEST_SITE_SENTINEL'], 'w').write('ran')\n",
        encoding="utf-8",
    )
    before = set(smoke_tmp.iterdir())
    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(smoke_tmp),
            "OM_RUNTIME_ROOT": str(inherited_runtime),
            "OM_ENV_FILE": str(inherited_env),
            "OM_PI_SESSION_DB": str(inherited_session),
            "OM_PI_MODEL_API_KEY": "must-not-be-used",
            "OM_LLM_API_KEY": "must-not-be-used",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "PYTHONPATH": str(python_injection),
            "PYTHONHOME": str(tmp_path / "invalid-python-home"),
            "OM_TEST_SITE_SENTINEL": str(injection_sentinel),
        }
    )

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "pi_runtime_smoke.sh"),
            "--root",
            str(ROOT),
            "--python",
            sys.executable,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "[pi-runtime] smoke passed" in result.stdout
    assert not inherited_session.exists()
    assert not injection_sentinel.exists()
    assert set(smoke_tmp.iterdir()) == before


def test_install_script_no_install_cli_skips_wrappers(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, "--no-install-cli")

    assert result.returncode == 0, result.stderr + result.stdout
    assert not (tmp_path / "home" / ".local" / "bin" / "om").exists()
    assert "cd " in result.stdout
    assert "./om setup check" in result.stdout


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
    assert "  om setup check" in result.stdout


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
