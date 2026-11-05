"""Shared dependency cache retention and source package reuse, entirely offline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from src.application import service_cleanup as cleanup, service_upgrade as upgrade
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import upgrade_keep_venvs
from src.application.config_yaml import build_yaml_runtime_config_file, resolve_yaml_runtime_config
from src.application.plugin_runtime import read_source_plugin
from tests.test_config_yaml import REPO_ROOT, _minimal_yaml
from tests.test_pm_package_upgrade import OfflineRuntime, _upgrade, transition


def _venv(root: Path, number: int) -> Path:
    path = root / 'venvs' / f'{number:016x}'
    path.mkdir(parents=True)
    (path / 'payload').write_bytes(b'x' * number)
    os.utime(path, ns=(number * 1_000_000_000, number * 1_000_000_000))
    return path


@pytest.mark.parametrize('keep', [0, 1, 2, 9])
def test_cache_retains_current_plus_recent_noncurrent(tmp_path, keep):
    cache = tmp_path / '_cache'
    paths = [_venv(cache, number) for number in range(1, 7)]
    result = cleanup.cleanup_shared_venvs(cache_root=cache, current_venv=paths[0], keep_other_venvs=keep)
    kept = {paths[0], *list(reversed(paths[1:]))[:keep]}
    assert result['ok'], result
    assert {path for path in paths if path.exists()} == kept
    assert set(result['deleted_paths']) == {str(path) for path in paths if path not in kept}
    assert result['freed_bytes'] == sum(number for number, path in enumerate(paths, 1) if path not in kept)


def test_cache_preserves_rollback_reference_and_never_leaves_venvs(tmp_path):
    cache = tmp_path / '_cache'
    paths = [_venv(cache, number) for number in range(1, 6)]
    sentinels = [cache / 'git', tmp_path / 'releases', tmp_path / 'output_shared',
                 tmp_path / 'ledger', tmp_path / 'config', cache / 'venvs/not-a-hash',
                 cache / 'venvs/.1234567890abcdef.tmp.123']
    for path in sentinels:
        path.mkdir(parents=True, exist_ok=True)
        (path / 'sentinel').write_text('keep')
    (cache / 'venvs/aaaaaaaaaaaaaaaa').symlink_to(sentinels[0], target_is_directory=True)
    (cache / 'venvs/bbbbbbbbbbbbbbbb').write_text('not a directory')
    (paths[1] / 'outside').symlink_to(sentinels[0], target_is_directory=True)
    result = cleanup.cleanup_shared_venvs(cache_root=cache, current_venv=paths[0], keep_other_venvs=0,
                                         protected_venvs=(paths[2],))
    assert result['ok'], result
    assert paths[0].is_dir() and paths[2].is_dir()
    assert set(result['deleted_paths']) == {str(paths[i]) for i in (1, 3, 4)}
    assert all((path / 'sentinel').read_text() == 'keep' for path in sentinels)
    assert (cache / 'venvs/aaaaaaaaaaaaaaaa').is_symlink()
    assert (cache / 'venvs/bbbbbbbbbbbbbbbb').read_text() == 'not a directory'


def test_symlink_root_and_invalid_current_disable_deletion(tmp_path):
    cache, external = tmp_path / '_cache', tmp_path / 'external'
    current = _venv(external, 1)
    old = _venv(external, 2)
    cache.mkdir()
    (cache / 'venvs').symlink_to(external / 'venvs', target_is_directory=True)
    result = cleanup.cleanup_shared_venvs(cache_root=cache, current_venv=current, keep_other_venvs=0)
    assert not result['ok'] and result['deleted_paths'] == []
    assert current.exists() and old.exists()
    result = cleanup.cleanup_shared_venvs(cache_root=external, current_venv=tmp_path, keep_other_venvs=0)
    assert not result['ok'] and current.exists() and old.exists()


def test_failed_cache_deletion_is_audited_with_actual_freed_bytes(tmp_path, monkeypatch):
    cache = tmp_path / '_cache'
    current, bad, good = [_venv(cache, n) for n in (1, 2, 3)]
    original = cleanup._delete_path

    def remove(path):
        if path == bad:
            raise OSError('fixture permission failure')
        original(path)

    monkeypatch.setattr(cleanup, '_delete_path', remove)
    result = cleanup.cleanup_shared_venvs(cache_root=cache, current_venv=current, keep_other_venvs=0)
    assert not result['ok'] and result['status'] == 'partial_failure'
    assert result['deleted_paths'] == [str(good)] and result['freed_bytes'] == 3
    assert current.exists() and bad.exists()
    failed = next(item for item in result['operations'] if not item['ok'])
    assert failed['path'] == str(bad) and 'fixture permission failure' in failed['error']


@pytest.mark.parametrize('value', [0, 2, 7])
def test_retention_yaml_projection(tmp_path, value):
    document = yaml.safe_load(_minimal_yaml())
    document['runtime'] = {'upgrade_keep_venvs': value}
    source = tmp_path / 'config.yaml'
    source.write_text(yaml.safe_dump(document))
    config, _ = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market='us', config_path=source)
    assert config['runtime']['upgrade_keep_venvs'] == value
    assert upgrade_keep_venvs(config) == value
    assert upgrade_keep_venvs({}) == 2


@pytest.mark.parametrize('value', [-1, True, False, 1.5, 2.0, '2', None, [], {}])
def test_invalid_retention_cannot_overwrite_snapshot(tmp_path, value):
    document = yaml.safe_load(_minimal_yaml())
    document['runtime'] = {'upgrade_keep_venvs': value}
    source = tmp_path / 'config.yaml'
    source.write_text(yaml.safe_dump(document))
    target = tmp_path / 'config.us.json'
    target.write_text('sentinel')
    with pytest.raises(AgentToolError, match='runtime.upgrade_keep_venvs'):
        build_yaml_runtime_config_file(repo_root=REPO_ROOT, market='us', config_path=source,
                                       output_config_path=target)
    assert target.read_text() == 'sentinel'


def test_retention_is_global_even_when_building_another_market(tmp_path):
    document = yaml.safe_load(_minimal_yaml())
    document['markets']['hk']['runtime'] = {'upgrade_keep_venvs': 0}
    source = tmp_path / 'config.yaml'
    source.write_text(yaml.safe_dump(document))
    with pytest.raises(AgentToolError, match='must be configured globally'):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market='us', config_path=source)


def test_unchanged_plugin_reuses_verified_files_changed_plugin_installs_only_it(transition, monkeypatch):
    previous, target, current, _ = transition
    monkeypatch.setenv('OM_UPGRADE_INSTALLER', 'pip')
    for release in (previous, target):
        shutil.copytree(REPO_ROOT / 'plugins/wechat-channel', release / 'plugins/wechat-channel',
                        ignore=shutil.ignore_patterns('build', '*.egg-info', '__pycache__'))
    runner = OfflineRuntime(previous, installed=True)
    cache = current.parent / 'cache'
    original = upgrade._ensure_release_runtime(target_dir=previous, cache_root=cache, include_pm=True,
                                               include_wechat=True, run_cmd=runner, operations=[])
    runner.commands.clear()
    reused = upgrade._ensure_release_runtime(target_dir=target, previous_dir=previous, cache_root=cache,
                                             include_pm=True, include_wechat=True, run_cmd=runner, operations=[])
    assert reused['venv_reused'] and reused['dependency_hash'] == original['dependency_hash']
    assert all(item['status'] == 'reused' for item in reused['source_plugins'])
    assert not any('install' in command for command in runner.commands)
    old_files = read_source_plugin(previous, 'pm')['files']
    source = target / 'plugins/portfolio-management/src/om_pm_plugin/__init__.py'
    source.write_text('VALUE = 2\n')
    runner.commands.clear()
    changed = upgrade._ensure_release_runtime(target_dir=target, previous_dir=previous, cache_root=cache,
                                              include_pm=True, include_wechat=True, run_cmd=runner, operations=[])
    assert changed['venv_reused'] and changed['dependency_hash'] == original['dependency_hash']
    assert {item['plugin']: item['installed'] for item in changed['source_plugins']} == {'pm': True, 'wechat': False}
    installs = [command for command in runner.commands if 'install' in command]
    assert len(installs) == 1 and './plugins/portfolio-management' in installs[0]
    assert '--no-deps' in installs[0] and '--target' in installs[0]
    assert read_source_plugin(previous, 'pm')['files'] == old_files
    installed_source = target / '.om-source-plugins/pm/site/om_pm_plugin/__init__.py'
    installed_source.write_text('VALUE = 999\n')
    runner.commands.clear()
    upgrade._ensure_release_runtime(target_dir=target, previous_dir=previous, cache_root=cache,
                                    include_pm=True, include_wechat=True, run_cmd=runner, operations=[])
    assert len([command for command in runner.commands if 'install' in command]) == 1
    assert installed_source.read_text() == 'VALUE = 2\n'
    # Real isolated imports prove one shared interpreter loads each release's
    # own package and metadata, including an old process after the update.
    for release in (previous, target):
        (release / 'src/application').mkdir(parents=True, exist_ok=True)
        for relative in ('src/__init__.py', 'src/application/__init__.py', 'src/application/plugin_runtime.py'):
            shutil.copyfile(REPO_ROOT / relative, release / relative)
    script = (
        'import importlib.metadata as m,json,sys; sys.path.insert(0,sys.argv[1]); '
        'import src,om_pm_plugin; '
        'print(json.dumps([getattr(om_pm_plugin,"VALUE",None),m.version("om-pm-plugin"),om_pm_plugin.__file__]))'
    )
    interpreter = str(Path(os.sys.executable))
    before = subprocess.run([interpreter, '-I', '-c', script, str(previous)],
                            capture_output=True, text=True, check=True)
    after = subprocess.run([interpreter, '-I', '-c', script, str(target)],
                           capture_output=True, text=True, check=True)
    assert json.loads(before.stdout) == [None, '0.1.0', str(previous / '.om-source-plugins/pm/site/om_pm_plugin/__init__.py')]
    assert json.loads(after.stdout) == [2, '0.1.0', str(installed_source)]


def test_dependency_change_rebuilds_existing_cache(transition, monkeypatch):
    previous, target, current, _ = transition
    monkeypatch.setenv('OM_UPGRADE_INSTALLER', 'pip')
    runner = OfflineRuntime(previous, installed=True)
    original = upgrade._ensure_release_runtime(target_dir=previous, cache_root=current.parent / 'cache',
                                               include_pm=True, run_cmd=runner, operations=[])
    project = target / 'plugins/portfolio-management/pyproject.toml'
    project.write_text(project.read_text() + 'dependencies=["example-dependency==2"]\n')
    runner.commands.clear()
    changed = upgrade._ensure_release_runtime(target_dir=target, previous_dir=previous,
                                              cache_root=current.parent / 'cache', include_pm=True,
                                              run_cmd=runner, operations=[])
    assert not changed['venv_reused'] and changed['dependency_hash'] != original['dependency_hash']
    assert any('example-dependency==2' in command for command in runner.commands)


def test_plugin_uv_auto_fallback_retries_only_plugin(transition, monkeypatch):
    previous, target, current, _ = transition
    monkeypatch.setenv('OM_UPGRADE_INSTALLER', 'auto')
    runner = OfflineRuntime(previous, installed=True)

    def run(command, **kwargs):
        if command[0] == 'uv' and '--target' in command:
            runner.commands.append(command)
            return subprocess.CompletedProcess(command, 1, '', 'fixture uv plugin build failure')
        return runner(command, **kwargs)

    result = upgrade._ensure_release_runtime(target_dir=target, cache_root=current.parent / 'cache',
                                             include_pm=True, run_cmd=run, operations=[])
    assert result['installer'] == 'uv'
    assert result['source_plugins'][0]['fallback'] == 'pip'
    assert len([cmd for cmd in runner.commands if cmd[:2] == ['uv', 'venv']]) == 1
    assert len([cmd for cmd in runner.commands if '-r' in cmd and 'requirements.txt' in cmd]) == 1


def test_upgrade_facade_reuses_cache_and_keeps_plugin_selection_on_next_upgrade(transition, monkeypatch):
    previous, target, current, _ = transition
    monkeypatch.setenv('OM_UPGRADE_INSTALLER', 'pip')
    runner = OfflineRuntime(previous, installed=True)
    original = upgrade._ensure_release_runtime(target_dir=previous, cache_root=current.parent / 'cache',
                                               include_pm=True, run_cmd=runner, operations=[])
    (target / 'plugins/portfolio-management/src/om_pm_plugin/__init__.py').write_text('VALUE=2\n')
    # Metadata in the shared dependency interpreter no longer owns PM. The
    # release-specific receipt is the selection carried to the next upgrade.
    runner.installed = False
    runner.probe_output = '[false,false,false]'
    runner.commands.clear()
    result = _upgrade(transition, runner)
    assert result['ok'], result
    assert result['runtime_prepare']['venv_reused']
    assert result['runtime_prepare']['dependency_hash'] == original['dependency_hash']
    assert len([cmd for cmd in runner.commands if 'install' in cmd]) == 1
    assert result['runtime_prepare']['source_plugins'][0]['installed']
    assert result['post_upgrade_venv_cleanup']['ok']
    assert result['symlink_switched'] and current.resolve() == target
    probe = upgrade._installed_optional_packages(previous_dir=target,
        run_cmd=lambda command, **_: subprocess.CompletedProcess(command, 0, '[false,false,false]', ''), operations=[])
    assert probe == (True, False, False)


def test_cleanup_failure_keeps_success_receipt(transition, monkeypatch):
    previous, target, current, _ = transition
    monkeypatch.setenv('OM_UPGRADE_INSTALLER', 'pip')

    def failure(**kwargs):
        raise OSError('fixture cleanup unavailable')

    monkeypatch.setattr(cleanup, 'cleanup_shared_venvs', failure)
    result = _upgrade(transition, OfflineRuntime(previous, installed=False))
    assert result['ok'] and result['status'] == 'upgraded' and current.resolve() == target
    audit = json.loads((current.parent / 'runtime/upgrade_status.json').read_text())
    assert not audit['post_upgrade_venv_cleanup']['ok']
    assert 'fixture cleanup unavailable' in audit['post_upgrade_venv_cleanup']['error']


def test_cleanup_reads_effective_global_policy_and_protects_retained_releases(tmp_path):
    cache, runtime, releases = tmp_path / '_cache', tmp_path / 'runtime', tmp_path / 'releases'
    current, rollback, obsolete = [_venv(cache, n) for n in (1, 2, 3)]
    runtime.mkdir()
    config = runtime / 'config.us.json'
    config.write_text(json.dumps({'runtime': {'upgrade_keep_venvs': 0}}))
    (runtime / 'service.profile.json').write_text(json.dumps({'config_paths': {'us': str(config)}}))
    target = releases / '1.0.1'
    target.mkdir(parents=True)
    (target / '.venv').symlink_to(current)
    old_release = releases / '1.0.0'
    old_release.mkdir()
    (old_release / '.venv').symlink_to(rollback)
    result = upgrade._cleanup_upgrade_venvs(cache_root=cache, releases_root=releases,
                                           target_dir=target, runtime_root=runtime)
    assert result['ok'] and result['keep_other_venvs'] == 0
    assert current.exists() and rollback.exists() and not obsolete.exists()
