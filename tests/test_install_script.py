from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
if [[ -n "${FAKE_TRANSACTION_TRACE:-}" ]]; then
  printf 'clone\\n' >> "$FAKE_TRANSACTION_TRACE"
fi
if [[ "${FAKE_CLONE_STATUS:-0}" != "0" ]]; then exit "$FAKE_CLONE_STATUS"; fi
dest=""
for arg in "$@"; do
  dest="$arg"
done
mkdir -p "$dest/requirements" "$dest/constraints" "$dest/src/application/bot" "$dest/scripts"
: > "$dest/src/__init__.py"
: > "$dest/src/application/__init__.py"
: > "$dest/src/application/bot/__init__.py"
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
printf '# Isolated Python host fixture\n' > "$dest/src/application/bot/host.py"
if [[ "${FAKE_RUNTIME_MISSING:-0}" != "1" ]]; then
  printf '# Isolated Python runtime fixture\n' > "$dest/src/application/bot/runtime.py"
fi
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf '{"tag_name":"v9.9.10"}\\n'
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
    if [[ -n "${FAKE_SIGNAL_BEFORE_CURRENT:-}" ]]; then
      kill -s "$FAKE_SIGNAL_BEFORE_CURRENT" "$PPID"
      exit 0
    fi
    "${FAKE_REAL_PYTHON:?}" -c "$2" "$3" "$4"
    if [[ -n "${FAKE_SIGNAL_AFTER_CURRENT:-}" ]]; then
      kill -s "$FAKE_SIGNAL_AFTER_CURRENT" "$PPID"
    fi
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
[[ "${FAKE_SMOKE_FAIL:-0}" != "1" ]] || exit 32
exec "${FAKE_REAL_PYTHON:?}" "$@"
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


def _transaction_fault_tools(tmp_path: Path, env: dict[str, str]) -> None:
    """Intercept filesystem commands only within the temporary installer fixture."""
    tools = tmp_path / "fake-bin"
    env["FAKE_TRANSACTION_TRACE"] = str(tmp_path / "transaction.log")
    env["FAKE_WRAPPER_PUBLISHED"] = str(tmp_path / "wrapper-published")
    for command in ("mv", "cp", "rm", "readlink"):
        real = shutil.which(command)
        assert real
        env[f"FAKE_REAL_{command.upper()}"] = real
    _write_executable(tools / "mv", """#!/usr/bin/env bash
set -euo pipefail
src="${@: -2:1}"
dst="${@: -1}"
case "$src" in
  */.om.backup.*|*/.om-agent.backup.*)
    printf 'restore %s\\n' "$dst" >> "$FAKE_TRANSACTION_TRACE"
    if [[ "${FAKE_RESTORE_FAIL:-}" == "${dst##*/}" ]]; then exit 47; fi
    ;;
esac
name=""
case "$src" in
  */.om.tmp.*) name=om ;;
  */.om-agent.tmp.*) name=om-agent ;;
esac
if [[ -n "$name" && "${FAKE_PUBLISH_FAIL:-}" == "$name" ]]; then exit 46; fi
"$FAKE_REAL_MV" "$@"
if [[ -n "$name" ]]; then
  printf 'published %s\\n' "$name" >> "$FAKE_TRANSACTION_TRACE"
  : > "$FAKE_WRAPPER_PUBLISHED"
  if [[ "${FAKE_SIGNAL_WRAPPER:-}" == "$name" ]]; then
    kill -s "${FAKE_EXIT_SIGNAL:-TERM}" "$PPID"
  fi
fi
""")
    _write_executable(tools / "cp", """#!/usr/bin/env bash
set -euo pipefail
dst="${@: -1}"
printf 'backup %s\\n' "$dst" >> "$FAKE_TRANSACTION_TRACE"
"$FAKE_REAL_CP" "$@"
case "$dst" in
  */.om.backup.*) name=om ;;
  */.om-agent.backup.*) name=om-agent ;;
  *) name="" ;;
esac
if [[ -n "$name" && "${FAKE_BACKUP_FAIL:-}" == "$name" ]]; then exit 41; fi
""")
    _write_executable(tools / "rm", """#!/usr/bin/env bash
set -euo pipefail
printf 'remove %s\\n' "$*" >> "$FAKE_TRANSACTION_TRACE"
for path in "$@"; do
  if [[ "${FAKE_CLEANUP_FAIL:-0}" == "1" && "$path" == */.om.backup.* ]]; then exit 49; fi
  if [[ "${FAKE_REMOVE_FAIL:-}" == om && "$path" == */bin/om ]]; then exit 48; fi
done
exec "$FAKE_REAL_RM" "$@"
""")
    _write_executable(tools / "readlink", """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_INITIAL_READLINK_FAIL:-0}" == "1" ]]; then exit 51; fi
if [[ -f "$FAKE_WRAPPER_PUBLISHED" ]]; then
  if [[ "${FAKE_CLEANUP_READLINK_FAIL:-0}" == "1" ]]; then exit 52; fi
  if [[ "${FAKE_CLEANUP_READLINK_OTHER:-0}" == "1" ]]; then
    printf '/unexpected-current\\n'
    exit 0
  fi
fi
exec "$FAKE_REAL_READLINK" "$@"
""")


def _transaction_fixture(tmp_path: Path, layout: str) -> tuple[dict[str, str], list[str]]:
    env = _installer_env(tmp_path)
    extra = []
    if layout == "upgrade":
        baseline = _run_installer(tmp_path, env=env)
        assert baseline.returncode == 0, baseline.stderr + baseline.stdout
    elif layout != "absent":
        old = tmp_path / "old-prefix" / "current"
        for name, mode in (("om", 0o751), ("om-agent", 0o705)):
            target = _write_executable(old / name, f"#!/usr/bin/env bash\nprintf 'old {name}\\n'\n")
            wrapper = tmp_path / "home/.local/bin" / name
            if layout == "symlink":
                wrapper.parent.mkdir(parents=True, exist_ok=True)
                wrapper.symlink_to(target)
            else:
                marker = f"# options-monitor managed wrapper\n# target-prefix: {old.parent}\n" if layout == "managed" else ""
                _write_executable(wrapper, f'#!/usr/bin/env bash\n{marker}exec "{target}" "$@"\n')
                wrapper.chmod(mode)
        if layout in {"unmanaged", "symlink"}:
            extra.append("--force-cli-wrapper")
    _transaction_fault_tools(tmp_path, env)
    env["FAKE_RELEASE_PATCH"] = "10"
    return env, extra


def _wrapper_state(tmp_path: Path) -> dict[str, tuple | None]:
    result = {}
    for name in ("om", "om-agent"):
        path = tmp_path / "home/.local/bin" / name
        if path.is_symlink():
            result[name] = ("link", os.readlink(path), path.lstat().st_mode)
        elif path.exists():
            result[name] = ("file", path.read_bytes(), path.stat().st_mode)
        else:
            result[name] = None
    return result


def _assert_transaction_clean(tmp_path: Path) -> None:
    assert not list((tmp_path / "home/.local/bin").glob(".*.backup.*"))
    assert not list((tmp_path / "home/.local/bin").glob(".*.tmp.*"))
    assert not list((tmp_path / "apps/options-monitor/releases").glob(".*.tmp.*"))
    assert not list((tmp_path / "apps/options-monitor").glob("current.tmp.*"))


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
    assert (prefix / "releases" / "v9.9.10").is_dir()


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


def test_install_script_rejects_unsupported_runtime_before_publishing(tmp_path: Path) -> None:
    env, first = _installed_baseline(tmp_path)
    prefix = tmp_path / "apps" / "options-monitor"
    active = (prefix / "current").resolve()
    wrappers = [tmp_path / "home/.local/bin" / name for name in ("om", "om-agent")]
    before = [path.read_bytes() for path in wrappers]
    env.update({"FAKE_RELEASE_PATCH": "10", "FAKE_RUNTIME_MISSING": "1"})
    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, env=env)
    assert first.returncode == 0, first.stderr
    assert result.returncode != 0
    assert "src.application.bot.runtime" in result.stderr
    assert (prefix / "current").resolve() == active
    assert not (prefix / "releases/v9.9.10").exists()
    assert [path.read_bytes() for path in wrappers] == before





def test_install_script_no_install_cli_skips_wrappers(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, "--no-install-cli")

    assert result.returncode == 0, result.stderr + result.stdout
    assert not (tmp_path / "home" / ".local" / "bin" / "om").exists()
    assert "cd " in result.stdout
    assert "./om setup check" in result.stdout


def test_python_install_stages_private_target_without_changing_production(
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
    assert (target_control / "src/application/bot/runtime.py").is_file()
    assert not (target_control / "agent-runtime").exists()
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


@pytest.mark.parametrize("layout", ["absent", "managed", "unmanaged", "symlink", "upgrade"])
@pytest.mark.parametrize("point", ["om", "om-agent", "before-current"])
@pytest.mark.parametrize("signal_name,exit_code", [("INT", 130), ("TERM", 143)])
def test_install_transaction_signal_before_current_restores_original_entries(
    tmp_path: Path, layout: str, point: str, signal_name: str, exit_code: int,
) -> None:
    env, extra = _transaction_fixture(tmp_path, layout)
    prefix = tmp_path / "apps/options-monitor"
    current = prefix / "current"
    before_current = os.readlink(current) if current.is_symlink() else None
    before = _wrapper_state(tmp_path)
    if point == "before-current":
        env["FAKE_SIGNAL_BEFORE_CURRENT"] = signal_name
    else:
        env.update(FAKE_SIGNAL_WRAPPER=point, FAKE_EXIT_SIGNAL=signal_name)

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == exit_code, result.stderr + result.stdout
    assert _wrapper_state(tmp_path) == before
    assert (os.readlink(current) if current.is_symlink() else None) == before_current
    assert (prefix / "releases/v9.9.10").is_dir()
    _assert_transaction_clean(tmp_path)


@pytest.mark.parametrize("layout", ["absent", "managed", "unmanaged", "symlink"])
@pytest.mark.parametrize("no_cli", [False, True])
@pytest.mark.parametrize("refusal", ["exact-target", "unreadable"])
def test_install_transaction_initial_current_refusal_has_no_publication_effects(
    tmp_path: Path, layout: str, no_cli: bool, refusal: str,
) -> None:
    env, extra = _transaction_fixture(tmp_path, layout)
    prefix = tmp_path / "apps/options-monitor"
    prefix.mkdir(parents=True, exist_ok=True)
    target = prefix / "releases/v9.9.10"
    current = prefix / "current"
    initial = str(target) if refusal == "exact-target" else str(prefix / "other-missing-release")
    current.symlink_to(initial)
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    backups = [bin_dir / f".{name}.backup.previous" for name in ("om", "om-agent")]
    for path in backups:
        path.write_bytes(b"existing unique recovery input\n")
    before_backups = {path: path.read_bytes() for path in backups}
    before = _wrapper_state(tmp_path)
    if no_cli:
        extra.append("--no-install-cli")
    if refusal == "unreadable":
        env["FAKE_INITIAL_READLINK_FAIL"] = "1"

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode != 0
    expected = "initial current already points to target" if refusal == "exact-target" else "cannot read initial current symlink"
    assert expected in result.stderr
    assert str(current) in result.stderr
    if refusal == "exact-target":
        assert str(target) in result.stderr
    assert os.readlink(current) == initial
    assert _wrapper_state(tmp_path) == before
    assert {path: path.read_bytes() for path in backups} == before_backups
    assert not target.exists()
    assert not (tmp_path / "transaction.log").exists()
    assert not list(bin_dir.glob(".*.tmp.*"))
    assert set(bin_dir.glob(".*.backup.*")) == set(backups)
    assert not list((prefix / "releases").glob(".*.tmp.*"))
    assert not list(prefix.glob("current.tmp.*"))


@pytest.mark.parametrize("alias_kind", ["relative", "dot", "prefix-alias"])
def test_install_transaction_initial_alias_text_is_not_committed(
    tmp_path: Path, alias_kind: str,
) -> None:
    env, extra = _transaction_fixture(tmp_path, "managed")
    prefix = tmp_path / "apps/options-monitor"
    prefix.mkdir(parents=True)
    if alias_kind == "relative":
        initial = "releases/v9.9.10"
    elif alias_kind == "dot":
        initial = str(prefix) + "/releases/./v9.9.10"
    else:
        alias = tmp_path / "prefix-alias"
        alias.symlink_to(prefix, target_is_directory=True)
        initial = str(alias / "releases/v9.9.10")
    current = prefix / "current"
    current.symlink_to(initial)
    before = _wrapper_state(tmp_path)
    env["FAKE_SIGNAL_WRAPPER"] = "om"

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == 143, result.stderr + result.stdout
    assert "initial current already points to target" not in result.stderr
    assert os.readlink(current) == initial
    assert _wrapper_state(tmp_path) == before
    assert (prefix / "releases/v9.9.10").is_dir()
    _assert_transaction_clean(tmp_path)


@pytest.mark.parametrize("layout", ["absent", "managed", "unmanaged", "symlink", "upgrade"])
@pytest.mark.parametrize("no_cli", [False, True])
def test_install_transaction_term_after_actual_current_replace_keeps_committed_entries(
    tmp_path: Path, layout: str, no_cli: bool,
) -> None:
    env, extra = _transaction_fixture(tmp_path, layout)
    before = _wrapper_state(tmp_path)
    env["FAKE_SIGNAL_AFTER_CURRENT"] = "TERM"
    if no_cli:
        extra.append("--no-install-cli")

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == 143, result.stderr + result.stdout
    prefix = tmp_path / "apps/options-monitor"
    assert os.readlink(prefix / "current") == str(prefix / "releases/v9.9.10")
    if no_cli:
        assert _wrapper_state(tmp_path) == before
    else:
        for name in ("om", "om-agent"):
            wrapper = tmp_path / "home/.local/bin" / name
            assert not wrapper.is_symlink()
            assert f'{prefix}/current/{name}' in wrapper.read_text(encoding="utf-8")
            assert subprocess.check_output([str(wrapper), "probe"], text=True).strip() == f"fake {name} probe"
    _assert_transaction_clean(tmp_path)


@pytest.mark.parametrize("point", ["om", "om-agent", "current"])
def test_install_transaction_explicit_publish_failure_restores_both_wrappers(
    tmp_path: Path, point: str,
) -> None:
    env, extra = _transaction_fixture(tmp_path, "upgrade")
    prefix = tmp_path / "apps/options-monitor"
    before_current = os.readlink(prefix / "current")
    before = _wrapper_state(tmp_path)
    env["FAKE_CURRENT_REPLACE_FAIL" if point == "current" else "FAKE_PUBLISH_FAIL"] = "1" if point == "current" else point

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode != 0
    assert "failed to" in result.stderr
    assert _wrapper_state(tmp_path) == before
    assert os.readlink(prefix / "current") == before_current
    assert (prefix / "releases/v9.9.10").is_dir()
    trace = (tmp_path / "transaction.log").read_text()
    for name in ("om", "om-agent"):
        assert f"restore {tmp_path / 'home/.local/bin' / name}\n" in trace
    _assert_transaction_clean(tmp_path)


@pytest.mark.parametrize("name", ["om", "om-agent"])
def test_install_transaction_backup_failure_never_deletes_unarmed_originals(
    tmp_path: Path, name: str,
) -> None:
    env, extra = _transaction_fixture(tmp_path, "managed")
    before = _wrapper_state(tmp_path)
    env["FAKE_BACKUP_FAIL"] = name

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode != 0
    assert "failed to back up CLI wrapper" in result.stderr
    assert _wrapper_state(tmp_path) == before
    prefix = tmp_path / "apps/options-monitor"
    assert not (prefix / "current").is_symlink()
    assert not (prefix / "releases/v9.9.10").exists()
    trace = (tmp_path / "transaction.log").read_text()
    assert f"restore {tmp_path / 'home/.local/bin' / name}\n" not in trace
    if name == "om-agent":
        assert f"restore {tmp_path / 'home/.local/bin/om'}\n" in trace
    _assert_transaction_clean(tmp_path)


@pytest.mark.parametrize("layout", ["managed", "symlink"])
def test_install_transaction_failed_first_restore_retains_only_backup_and_restores_second(
    tmp_path: Path, layout: str,
) -> None:
    env, extra = _transaction_fixture(tmp_path, layout)
    before = _wrapper_state(tmp_path)
    env.update(FAKE_SIGNAL_WRAPPER="om-agent", FAKE_RESTORE_FAIL="om")

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == 143, result.stderr + result.stdout
    assert _wrapper_state(tmp_path)["om-agent"] == before["om-agent"]
    bin_dir = tmp_path / "home/.local/bin"
    backups = list(bin_dir.glob(".om.backup.*"))
    assert len(backups) == 1 and str(backups[0]) in result.stderr
    assert str(bin_dir / "om") in result.stderr
    if layout == "symlink":
        assert backups[0].is_symlink() and os.readlink(backups[0]) == before["om"][1]
    else:
        assert backups[0].read_bytes() == before["om"][1]
        assert backups[0].stat().st_mode == before["om"][2]
    assert not list(bin_dir.glob(".om-agent.backup.*"))
    assert not list(bin_dir.glob(".*.tmp.*"))
    trace = (tmp_path / "transaction.log").read_text()
    assert f"restore {bin_dir / 'om'}\n" in trace and f"restore {bin_dir / 'om-agent'}\n" in trace


def test_install_transaction_absent_wrapper_removal_failure_still_handles_second(tmp_path: Path) -> None:
    env, extra = _transaction_fixture(tmp_path, "absent")
    env.update(FAKE_SIGNAL_WRAPPER="om-agent", FAKE_REMOVE_FAIL="om")

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == 143
    assert (tmp_path / "home/.local/bin/om").exists()
    assert not (tmp_path / "home/.local/bin/om-agent").exists()
    assert "failed to restore CLI wrapper" in result.stderr


def test_install_transaction_successful_commit_cleanup_failure_is_not_success(tmp_path: Path) -> None:
    env, extra = _transaction_fixture(tmp_path, "managed")
    before = _wrapper_state(tmp_path)
    env["FAKE_CLEANUP_FAIL"] = "1"

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode != 0
    prefix = tmp_path / "apps/options-monitor"
    assert os.readlink(prefix / "current") == str(prefix / "releases/v9.9.10")
    bin_dir = tmp_path / "home/.local/bin"
    backups = list(bin_dir.glob(".om.backup.*"))
    assert len(backups) == 1 and backups[0].read_bytes() == before["om"][1]
    assert "failed to remove CLI backup" in result.stderr and str(backups[0]) in result.stderr
    assert not list(bin_dir.glob(".om-agent.backup.*"))
    for name in ("om", "om-agent"):
        assert f'{prefix}/current/{name}' in (bin_dir / name).read_text()


@pytest.mark.parametrize("readback", ["FAKE_CLEANUP_READLINK_FAIL", "FAKE_CLEANUP_READLINK_OTHER"])
def test_install_transaction_indeterminate_current_preserves_both_backups(
    tmp_path: Path, readback: str,
) -> None:
    env, extra = _transaction_fixture(tmp_path, "upgrade")
    before = _wrapper_state(tmp_path)
    current = tmp_path / "apps/options-monitor/current"
    before_current = os.readlink(current)
    env.update(FAKE_SIGNAL_WRAPPER="om-agent", **{readback: "1"})

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == 143
    assert "cannot determine current commit state" in result.stderr
    assert os.readlink(current) == before_current
    bin_dir = tmp_path / "home/.local/bin"
    for name in ("om", "om-agent"):
        backups = list(bin_dir.glob(f".{name}.backup.*"))
        assert len(backups) == 1 and backups[0].read_bytes() == before[name][1]
        assert str(backups[0]) in result.stderr and str(bin_dir / name) in result.stderr
    assert "restore " not in (tmp_path / "transaction.log").read_text()
    assert not list(bin_dir.glob(".*.tmp.*"))


def test_install_transaction_preserves_original_child_failure_status(tmp_path: Path) -> None:
    env, extra = _transaction_fixture(tmp_path, "managed")
    before = _wrapper_state(tmp_path)
    env["FAKE_CLONE_STATUS"] = "37"

    result = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)

    assert result.returncode == 37
    assert _wrapper_state(tmp_path) == before
    _assert_transaction_clean(tmp_path)


def test_install_transaction_interrupted_release_keeps_ordinary_and_force_retry_contract(tmp_path: Path) -> None:
    env, extra = _transaction_fixture(tmp_path, "upgrade")
    before = _wrapper_state(tmp_path)
    current = tmp_path / "apps/options-monitor/current"
    before_current = os.readlink(current)
    env["FAKE_SIGNAL_WRAPPER"] = "om"
    interrupted = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)
    assert interrupted.returncode == 143
    assert _wrapper_state(tmp_path) == before and os.readlink(current) == before_current
    env.pop("FAKE_SIGNAL_WRAPPER")

    ordinary = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, env=env)
    assert ordinary.returncode != 0 and "not the active current release" in ordinary.stderr
    assert _wrapper_state(tmp_path) == before and os.readlink(current) == before_current
    forced = _run_installer_without_version(tmp_path, *_UPGRADE_ARGS, *extra, "--force", env=env)
    assert forced.returncode == 0, forced.stderr + forced.stdout
    assert os.readlink(current) == str(current.parent / "releases/v9.9.10")
    _assert_transaction_clean(tmp_path)


@pytest.mark.parametrize("alias_kind", ["relative", "prefix-alias"])
def test_install_transaction_guard_preserves_already_installed_and_active_force_paths(
    tmp_path: Path, alias_kind: str,
) -> None:
    env, baseline = _installed_baseline(tmp_path)
    assert baseline.returncode == 0, baseline.stderr + baseline.stdout
    prefix = tmp_path / "apps/options-monitor"
    current = prefix / "current"
    current.unlink()
    if alias_kind == "relative":
        initial = "releases/v9.9.9"
    else:
        alias = tmp_path / "prefix-alias"
        alias.symlink_to(prefix, target_is_directory=True)
        initial = str(alias / "releases/v9.9.9")
    current.symlink_to(initial)
    _transaction_fault_tools(tmp_path, env)
    before = _wrapper_state(tmp_path)

    installed = _run_installer(tmp_path, env=env)
    assert installed.returncode == 0 and "already installed" in installed.stdout
    assert os.readlink(current) == initial and _wrapper_state(tmp_path) == before
    forced = _run_installer(tmp_path, "--force", env=env)
    assert forced.returncode != 0 and "refusing --force for the active current release" in forced.stderr
    assert os.readlink(current) == initial and _wrapper_state(tmp_path) == before
    assert not (tmp_path / "transaction.log").exists()
