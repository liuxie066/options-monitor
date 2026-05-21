from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_install_script_is_shell_parseable_and_has_no_service_side_effects() -> None:
    script = ROOT / "scripts" / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)

    text = script.read_text(encoding="utf-8")
    assert "--version is required" in text
    assert "xcode-select --install" in text
    assert "python3-venv" in text
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
mkdir -p "$dest/requirements" "$dest/constraints"
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
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  venv="$3"
  mkdir -p "$venv/bin"
  cat > "$venv/bin/pip" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$venv/bin/pip"
  exit 0
fi
cat >/dev/null || true
exit 0
""",
    )
    return bin_dir


def _installer_env(tmp_path: Path, *, path_prefix: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    path_parts = [str(_fake_installer_tools(tmp_path))]
    if path_prefix:
        path_parts.append(path_prefix)
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def _run_installer(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = ROOT / "scripts" / "install.sh"
    return subprocess.run(
        [
            "bash",
            str(script),
            "--version",
            "v9.9.9",
            "--prefix",
            str(tmp_path / "apps" / "options-monitor"),
            "--repo-url",
            "https://example.invalid/options-monitor.git",
            *args,
        ],
        check=False,
        text=True,
        capture_output=True,
        env=env or _installer_env(tmp_path),
    )


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
