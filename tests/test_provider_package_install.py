"""Installer transaction boundaries; official asset validation lives in distribution tests."""
from pathlib import Path
import shutil

import pytest

from src.application import plugin_runtime
from tests.test_install_script import ROOT, _installer_env, _run_installer


def provider_installer(tmp_path):
    env = _installer_env(tmp_path)
    fixture = tmp_path / "provider-source"
    for name in ("src/__init__.py", "src/application/__init__.py", "src/application/plugin_runtime.py"):
        path = fixture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    (fixture / "src/application/plugin_distribution.py").write_text('''
import hashlib,json,os
from pathlib import Path
from src.application.plugin_runtime import PLUGINS,plugin_store

def install_plugin(plugin_id, *, repo_root, confirm, python_executable):
    root = repo_root.resolve()
    assert root.name == 'v9.9.9', root
    current = root.parent.parent / 'current'
    assert not current.exists() or current.resolve() != root, 'published before market installed'
    with open(os.environ['PROVIDER_TRACE'], 'a') as log:
        log.write('market-final-path:'+str(root)+'\\n')
    if os.environ.get('PROVIDER_FAIL') == '1':
        raise ValueError('official market asset unavailable')
    assert plugin_id == 'market-opend' and confirm
    directory = plugin_store(root) / plugin_id
    module = 'om_market_opend/__init__.py'
    path = directory / 'site' / module
    path.parent.mkdir(parents=True)
    path.write_text('class OpenDMarketDataProvider: pass\\n')
    version = (root/'VERSION').read_text().strip()
    receipt = dict(schema_version=1,plugin_id=plugin_id,repo_root=str(root),om_version=version,version=version,
        distribution=PLUGINS[plugin_id]['distribution'],module='om_market_opend',
        wheel_sha256='b'*64,release_tag='v'+version,commit='a'*40,
        installed_at_utc='2026-11-02T00:00:00+00:00',files={module:hashlib.sha256(path.read_bytes()).hexdigest()})
    (directory/'receipt.json').write_text(json.dumps(receipt))
''')
    for key in ("broker-futu", "broker-longbridge"):
        item = plugin_runtime.PLUGINS[key]
        module = fixture / item["module"]
        module.mkdir()
        (module / "__init__.py").write_text('''
def resolve_portfolio_provider(*args): pass
def build_ready_broker_gateway(*args): pass
class LongbridgeGateway: pass
''')
    sdk = fixture / "longbridge"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("")
    (sdk / "openapi.py").write_text("class Config: pass\nclass TradeContext: pass\n")
    info = fixture / "longbridge-5.3.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: longbridge\nVersion: 5.3.0\n")
    (fixture / "requirements").mkdir()
    (fixture / "requirements/longbridge.txt").write_text("longbridge==5.3.0\n")
    git = tmp_path / "fake-bin/git"
    git.write_text(git.read_text() + '\ncp -R "$PROVIDER_FIXTURE/." "$dest/"\n')
    # Record the existing shell's pip selection without installing anything.
    for name in ("python3", "python3.12"):
        path = tmp_path / "fake-bin" / name
        content = path.read_text().replace('#!/usr/bin/env bash\nif [[ "$*"', '#!/usr/bin/env bash\nprintf "pip:%s\\n" "$*" >> "$PROVIDER_TRACE"\nif [[ "$*"')
        path.write_text(content)
    env.update(PROVIDER_FIXTURE=str(fixture), PROVIDER_TRACE=str(tmp_path / "providers.log"))
    return env


@pytest.mark.parametrize("brokers", [False, True])
def test_source_default_market_installs_at_final_path_before_publish(tmp_path, brokers):
    env = provider_installer(tmp_path)
    flags = ("--with-broker-futu", "--with-broker-longbridge") if brokers else ()
    result = _run_installer(tmp_path, *flags, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = tmp_path / "apps/options-monitor"
    release = prefix / "releases/v9.9.9"
    receipt = plugin_runtime.read_managed_plugin(release, "market-opend")
    assert receipt["repo_root"] == str(release.resolve())
    assert (prefix / "current").resolve() == release
    trace = Path(env["PROVIDER_TRACE"]).read_text().splitlines()
    assert trace[-1] == "market-final-path:" + str(release.resolve())
    if brokers:
        sdk = next(i for i, row in enumerate(trace) if "requirements/longbridge.txt" in row)
        lb = next(i for i, row in enumerate(trace) if "./plugins/broker-longbridge" in row)
        assert sdk < lb < len(trace) - 1
        assert any("./plugins/broker-futu" in row for row in trace)
    else:
        assert not any("broker-" in row or "longbridge.txt" in row for row in trace)
        assert plugin_runtime.read_managed_plugin(release, "broker-futu") is None
        assert plugin_runtime.read_managed_plugin(release, "broker-longbridge") is None


def test_default_market_asset_failure_preserves_previous_current_and_wrapper(tmp_path):
    env = provider_installer(tmp_path)
    prefix = tmp_path / "apps/options-monitor"
    previous = prefix / "releases/v9.9.8"
    previous.mkdir(parents=True)
    (prefix / "current").symlink_to(previous)
    wrapper = Path(env["HOME"]) / ".local/bin/om"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# options-monitor managed wrapper\nold wrapper\n")
    env["PROVIDER_FAIL"] = "1"
    result = _run_installer(tmp_path, env=env)
    assert result.returncode != 0
    assert "official market asset unavailable" in result.stderr
    assert (prefix / "current").resolve() == previous
    assert wrapper.read_text() == "# options-monitor managed wrapper\nold wrapper\n"
    assert plugin_runtime.read_managed_plugin(prefix / "releases/v9.9.9", "market-opend") is None
