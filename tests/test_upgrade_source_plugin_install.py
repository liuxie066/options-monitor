"""Exercise real local pip/uv installation into release-specific plugin sites."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import venv

import pytest

from src.application import plugin_distribution, service_upgrade as upgrade
from src.application.plugin_runtime import PLUGINS, read_source_plugin, source_plugin_store


ROOT = Path(__file__).resolve().parents[1]


def _release(path: Path, value: int) -> None:
    source = path / 'plugins/portfolio-management/src/om_pm_plugin'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text(f'VALUE={value}\n')
    (source.parent.parent / 'pyproject.toml').write_text(
        '[build-system]\nrequires=["setuptools>=77"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="om-pm-plugin"\nversion="0.1.0"\n'
        '[tool.setuptools.packages.find]\nwhere=["src"]\n'
    )
    (path / 'VERSION').write_text('1.0.0\n')
    for relative in ('src/__init__.py', 'src/application/__init__.py', 'src/application/plugin_runtime.py'):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)


@pytest.mark.parametrize('installer', ['pip', 'uv'])
def test_real_plugin_install_reuse_change_and_release_isolation(tmp_path, installer):
    if not shutil.which('uv'):
        pytest.skip('uv is required to seed the disposable offline build environment')
    previous, target = tmp_path / 'previous', tmp_path / 'target'
    _release(previous, 1)
    _release(target, 1)
    environment = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / 'bin/python'
    env = {**os.environ, 'PIP_NO_INDEX': '1', 'UV_OFFLINE': '1', 'PIP_NO_BUILD_ISOLATION': '1',
           'UV_NO_BUILD_ISOLATION': '1'}
    # Use the local uv cache solely to seed the disposable build environment.
    # No project runtime or global package installation is modified.
    build = subprocess.run(['uv', 'pip', 'install', '-p', str(python), '--offline', 'setuptools', 'wheel'],
                           capture_output=True, text=True)
    if build.returncode:
        pytest.skip('offline setuptools/wheel unavailable: ' + build.stderr[-300:])
    calls = []

    def run(command, **kwargs):
        if 'install' in command and '--target' in command and command[0] != 'uv':
            command = [*command, '--no-build-isolation']
        calls.append(command)
        return subprocess.run(command, **kwargs)

    arguments = dict(selected=('pm',), venv_python=python, installer=installer, env=env,
                     run_cmd=run, operations=[])
    first = upgrade._prepare_source_plugins(target_dir=previous, previous_dir=None, **arguments)
    assert first[0]['installed']
    calls.clear()
    second = upgrade._prepare_source_plugins(target_dir=target, previous_dir=previous, **arguments)
    assert second[0]['status'] == 'reused' and not calls
    source = target / 'plugins/portfolio-management/src/om_pm_plugin/__init__.py'
    source.write_text('VALUE=2\n')
    third = upgrade._prepare_source_plugins(target_dir=target, previous_dir=previous, **arguments)
    assert third[0]['installed'] and len(calls) == 1
    assert read_source_plugin(target, 'pm')['version'] == '0.1.0'
    script = ('import sys,json,importlib.metadata as m; sys.path.insert(0,sys.argv[1]); '
              'import src,om_pm_plugin; print(json.dumps([om_pm_plugin.VALUE,m.version("om-pm-plugin")]))')
    for release, expected in ((previous, 1), (target, 2)):
        result = subprocess.run([str(python), '-I', '-c', script, str(release)], capture_output=True,
                                text=True, check=True)
        assert json.loads(result.stdout) == [expected, '0.1.0']
    assert not any(dist.metadata['Name'] == PLUGINS['pm']['distribution']
                   for dist in importlib.metadata.distributions(
                       path=[str(next((environment / 'lib').glob('python*/site-packages')))]))
    assert (source_plugin_store(previous) / 'pm').is_dir()
    # Repeat a source-only change at the same path and package version. Build
    # caches must not return the wheel produced for the previous source bytes.
    source.write_text('VALUE=3\n')
    upgrade._prepare_source_plugins(target_dir=target, previous_dir=previous, **arguments)
    result = subprocess.run([str(python), '-I', '-c', script, str(target)], capture_output=True,
                            text=True, check=True)
    assert json.loads(result.stdout) == [3, '0.1.0']


@pytest.mark.parametrize('digest', [None, 3, [], {}])
def test_invalid_source_receipt_does_not_block_core_commands(tmp_path, digest):
    from tests.test_pm_package_upgrade import OfflineRuntime

    release = tmp_path / 'release'
    _release(release, 1)
    upgrade._prepare_source_plugins(target_dir=release, previous_dir=None, selected=('pm',),
                                    venv_python=Path(sys.executable), installer='pip', env={},
                                    run_cmd=OfflineRuntime(tmp_path / 'old', installed=False), operations=[])
    path = source_plugin_store(release) / 'pm/receipt.json'
    receipt = json.loads(path.read_text())
    receipt['source_sha256'] = digest
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='identity mismatch'):
        read_source_plugin(release, 'pm')
    code = ('import sys; sys.path.insert(0,sys.argv[1]); import src; '
            'from src.application.plugin_runtime import plugin_installed; '
            'assert not plugin_installed("pm"); print("core available")')
    result = subprocess.run([sys.executable, '-I', '-c', code, str(release)],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'core available'


def test_source_plugin_management_can_list_and_remove_selection(tmp_path, monkeypatch):
    """Use the real management facade on a verified source-installed fixture."""
    from tests.test_pm_package_upgrade import OfflineRuntime

    release = tmp_path / 'release'
    _release(release, 1)
    runner = OfflineRuntime(tmp_path / 'old', installed=False)
    upgrade._prepare_source_plugins(target_dir=release, previous_dir=None, selected=('pm',),
                                    venv_python=Path(sys.executable), installer='pip', env={},
                                    run_cmd=runner, operations=[])
    monkeypatch.setattr(plugin_distribution, '_environment_version', lambda key: None)
    listed = plugin_distribution.list_plugins(repo_root=release)
    row = next(item for item in listed['plugins'] if item['id'] == 'pm')
    assert row['installed'] and row['compatible'] and row['source'] == 'release_source'
    with pytest.raises(ValueError, match='source installation'):
        plugin_distribution.install_plugin('pm', repo_root=release, confirm=True,
                                            python_executable=Path(sys.executable))
    preview = plugin_distribution.uninstall_plugin('pm', repo_root=release)
    assert preview['status'] == 'preview' and read_source_plugin(release, 'pm')
    installed = source_plugin_store(release) / 'pm/site/om_pm_plugin/__init__.py'
    installed.write_text('raise RuntimeError("damaged plugin")\n')
    with pytest.raises(ValueError, match='inventory mismatch'):
        read_source_plugin(release, 'pm')
    assert plugin_distribution.uninstall_plugin('pm', repo_root=release)['status'] == 'preview'
    removed = plugin_distribution.uninstall_plugin('pm', repo_root=release, confirm=True)
    assert removed['changed'] and read_source_plugin(release, 'pm') is None
    assert not next(item for item in plugin_distribution.list_plugins(repo_root=release)['plugins']
                    if item['id'] == 'pm')['installed']
