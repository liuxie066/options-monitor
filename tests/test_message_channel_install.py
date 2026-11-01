"""Source-install flags use target packages and never enable message channels."""
from pathlib import Path
import subprocess

import pytest

from tests.test_install_script import _installer_env, _run_installer


ROOT = Path(__file__).resolve().parents[1]


def _message_installer_env(tmp_path):
    env = _installer_env(tmp_path)
    env["FAKE_DEV_INSTALL_LOG"] = str(tmp_path / "pm-install.log")
    env["FAKE_MESSAGE_INSTALL_LOG"] = str(tmp_path / "message-install.log")
    env["FAKE_MESSAGE_VALIDATE_LOG"] = str(tmp_path / "message-validate.log")
    git = tmp_path / "fake-bin/git"
    git.write_text(git.read_text() + '''
for package in feishu-channel wechat-channel; do
  mkdir -p "$dest/plugins/$package"
  printf 'target %s\\n' "$package" > "$dest/plugins/$package/package-fixture"
  printf '[project]\\nversion="0.1.0"\\n' > "$dest/plugins/$package/pyproject.toml"
done
''')
    for name in ("python3", "python3.12"):
        python = tmp_path / "fake-bin" / name
        text = python.read_text().replace(
            'if [[ "$*" == *"/requirements/dev.txt"* ]]; then\n', '''
for package in feishu-channel wechat-channel; do
  if [[ "$*" == *"./plugins/$package"* || "$*" == *"/requirements/dev.txt"* ]]; then
    [[ "${FAKE_MESSAGE_INSTALL_FAIL:-0}" != "1" ]] || exit 45
    cat "./plugins/$package/package-fixture" >> "${FAKE_MESSAGE_INSTALL_LOG:?}" || exit 34
  fi
done
if [[ "$*" == *"/requirements/dev.txt"* ]]; then
''')
        text = text.replace('exec "${FAKE_REAL_PYTHON:?}" "$@"', '''
if [[ "$*" == *"_validate_message_runtime"* ]]; then
  printf '%s %s\\n' "$3" "$4" >> "${FAKE_MESSAGE_VALIDATE_LOG:?}"
  [[ "${FAKE_MESSAGE_VALIDATE_FAIL:-0}" != "1" ]] || exit 46
  exit 0
fi
exec "${FAKE_REAL_PYTHON:?}" "$@"
''')
        python.write_text(text)
    return env


@pytest.mark.parametrize("flag", ["--with-feishu", "--with-wechat"])
def test_message_flags_reject_bundle_before_download(tmp_path, flag):
    env = _message_installer_env(tmp_path)
    prefix = tmp_path / "apps/options-monitor"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install.sh"), "--version", "v9.9.9", "--prefix", str(prefix), flag],
        env=env, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert f"{flag} requires explicit --source mode" in result.stderr
    assert not prefix.exists()


@pytest.mark.parametrize("flags,selected", [
    ([], []), (["--with-server"], []),
    (["--with-feishu"], ["feishu-channel"]),
    (["--with-wechat"], ["wechat-channel"]),
    (["--with-feishu", "--with-wechat"], ["feishu-channel", "wechat-channel"]),
    (["--with-dev"], ["feishu-channel", "wechat-channel"]),
    (["--with-dev", "--with-feishu", "--with-wechat"], ["feishu-channel", "wechat-channel"]),
])
def test_source_message_selection_is_explicit_and_uses_release_cwd(monkeypatch, tmp_path, flags, selected):
    caller = tmp_path / "caller/plugins/feishu-channel"
    caller.mkdir(parents=True)
    (caller / "package-fixture").write_text("wrong caller package\n")
    monkeypatch.chdir(caller.parent.parent)
    env = _message_installer_env(tmp_path)
    result = _run_installer(tmp_path, *flags, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    log = Path(env["FAKE_MESSAGE_INSTALL_LOG"])
    assert (log.read_text().splitlines() if log.exists() else []) == [f"target {p}" for p in selected]
    validation = Path(env["FAKE_MESSAGE_VALIDATE_LOG"])
    assert validation.exists() is bool(selected)
    if selected:
        assert validation.read_text().strip() == f"{int('feishu-channel' in selected)} {int('wechat-channel' in selected)}"
    release = tmp_path / "apps/options-monitor/current"
    assert not any((release / name).exists() for name in ("config.yaml", "config.us.json", "config.hk.json"))


@pytest.mark.parametrize("failure,expected", [("FAKE_MESSAGE_INSTALL_FAIL", 45), ("FAKE_MESSAGE_VALIDATE_FAIL", 46)])
def test_failed_message_install_preserves_current_and_wrappers(tmp_path, failure, expected):
    env = _message_installer_env(tmp_path)
    first = _run_installer(tmp_path, env=env)
    assert first.returncode == 0, first.stderr
    current = tmp_path / "apps/options-monitor/current"
    before = current.resolve()
    wrappers = tmp_path / "home/.local/bin"
    originals = {name: (wrappers / name).read_bytes() for name in ("om", "om-agent")}
    env.update(FAKE_RELEASE_PATCH="10")
    env[failure] = "1"
    result = _run_installer(tmp_path, "--version", "v9.9.10", "--with-feishu", env=env)
    assert result.returncode == expected, result.stderr
    assert current.resolve() == before
    assert {name: (wrappers / name).read_bytes() for name in originals} == originals
    assert not (current.parent / "releases/v9.9.10").exists()


def test_message_dependencies_are_optional_in_server_and_both_in_dev():
    server = (ROOT / "requirements/server.txt").read_text()
    runtime = (ROOT / "requirements/runtime.txt").read_text()
    assert "lark-oapi" not in server + runtime
    dev = (ROOT / "requirements/dev.txt").read_text()
    assert "./plugins/feishu-channel" in dev and "./plugins/wechat-channel" in dev
    assert '"lark-oapi"' in (ROOT / "plugins/feishu-channel/pyproject.toml").read_text()
