"""Offline installer fixtures exercise the actual shell option and release cwd."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests.test_install_script import _installer_env, _run_installer


def _advice_installer_env(tmp_path: Path):
    env = _installer_env(tmp_path)
    env["FAKE_DEV_INSTALL_LOG"] = str(tmp_path / "pm-install.log")
    env["FAKE_ADVICE_INSTALL_LOG"] = str(tmp_path / "advice-install.log")
    tools = tmp_path / "fake-bin"
    git = tools / "git"
    git.write_text(git.read_text() + '''
mkdir -p "$dest/plugins/position-advice"
printf 'target advice package\\n' > "$dest/plugins/position-advice/package-fixture"
printf './plugins/position-advice\\n' >> "$dest/requirements/dev.txt"
''')
    for name in ("python3", "python3.12"):
        python = tools / name
        text = python.read_text()
        needle = 'if [[ "$*" == *"/requirements/dev.txt"* ]]; then\n'
        text = text.replace(needle, '''if [[ "$*" == *"./plugins/position-advice"* || "$*" == *"/requirements/dev.txt"* ]]; then
  [[ "${FAKE_ADVICE_INSTALL_FAIL:-0}" != "1" ]] || exit 44
  cat ./plugins/position-advice/package-fixture > "${FAKE_ADVICE_INSTALL_LOG:?}" || exit 34
fi
''' + needle)
        python.write_text(text)
    return env


def test_advice_install_requires_source_before_bundle_download(tmp_path):
    env = _advice_installer_env(tmp_path)
    prefix = tmp_path / "apps/options-monitor"
    script = Path(__file__).resolve().parents[1] / "scripts/install.sh"
    result = subprocess.run(
        ["bash", str(script), "--version", "v9.9.9", "--prefix", str(prefix),
         "--with-position-advice"], env=env, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert "--with-position-advice requires explicit --source mode" in result.stderr
    assert not prefix.exists()
    assert not Path(env["FAKE_ADVICE_INSTALL_LOG"]).exists()


@pytest.mark.parametrize("args,installed", [
    ([], False), (["--with-position-advice"], True), (["--with-dev"], True),
    (["--with-dev", "--with-position-advice"], True),
])
def test_optional_advice_install_is_explicit_and_relative_to_release(monkeypatch, tmp_path, args, installed):
    caller = tmp_path / "caller"
    decoy = caller / "plugins/position-advice"
    decoy.mkdir(parents=True)
    (decoy / "package-fixture").write_text("wrong package from caller\n")
    monkeypatch.chdir(caller)
    env = _advice_installer_env(tmp_path)
    result = _run_installer(tmp_path, *args, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    log = tmp_path / "advice-install.log"
    assert log.exists() is installed
    if installed:
        assert log.read_text() == "target advice package\n"
    release = tmp_path / "apps/options-monitor/current"
    assert not (release / "config.yaml").exists()
    assert not (release / "config.us.json").exists()


def test_advice_install_failure_preserves_current_release_and_wrappers(tmp_path):
    env = _advice_installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    current = tmp_path / "apps/options-monitor/current"
    before = current.resolve()
    wrappers = tmp_path / "home/.local/bin"
    originals = {name: (wrappers / name).read_bytes() for name in ("om", "om-agent")}
    env["FAKE_RELEASE_PATCH"] = "10"
    env["FAKE_ADVICE_INSTALL_FAIL"] = "1"
    result = _run_installer(tmp_path, "--version", "v9.9.10", "--with-position-advice", env=env)
    assert result.returncode == 44, result.stdout + result.stderr
    assert current.resolve() == before
    assert {name: (wrappers / name).read_bytes() for name in originals} == originals
    assert not (current.parent / "releases/v9.9.10").exists()
