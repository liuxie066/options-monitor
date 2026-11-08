"""Upgrade reconciliation with isolated plists and an in-memory launchctl."""
from __future__ import annotations

import json
import os
import plistlib
import pwd
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import service_lifecycle as lifecycle
from src.application import service_drift as drift


class Launchctl:
    def __init__(self):
        self.loaded = {}
        self.disabled = set()
        self.calls = []
        self.fail = None
        self.stuck = False
        self.pid = 100

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == 'ps':
            present = any(row[2] == int(command[2]) for row in self.loaded.values())
            return subprocess.CompletedProcess(command, 0 if present else 1, command[2] if present else '', '')
        assert command[0] == 'launchctl'
        action = command[1]
        name = command[-1].split('/')[-1]
        if action == 'print-disabled':
            return subprocess.CompletedProcess(command, 0, '\n'.join(f'"{n}" => true' for n in self.disabled), '')
        if action == 'print':
            row = self.loaded.get(name)
            if not row:
                return subprocess.CompletedProcess(command, 113, '', 'Could not find service')
            payload, path, pid = row
            text = f'path = {path}\nOM_RUNTIME_ROOT => {payload["EnvironmentVariables"]["OM_RUNTIME_ROOT"]}\nprogram = {payload["ProgramArguments"][0]}\n'
            if pid:
                text += f'pid = {pid}\n'
            return subprocess.CompletedProcess(command, 0, text, '')
        if action == self.fail:
            return subprocess.CompletedProcess(command, 1, '', 'injected failure')
        if action == 'bootout' and not self.stuck:
            self.loaded.pop(name, None)
        elif action == 'bootstrap':
            path = Path(command[-1])
            payload = plistlib.loads(path.read_bytes())
            self.pid += 1
            self.loaded[payload['Label']] = (payload, path, self.pid if payload.get('KeepAlive') else None)
        elif action == 'disable':
            self.disabled.add(name)
        elif action == 'enable':
            self.disabled.discard(name)
        return subprocess.CompletedProcess(command, 0, '', '')


@pytest.fixture
def installed(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    repo = tmp_path / 'current'
    repo.mkdir()
    root = tmp_path / 'home' / 'Library' / 'LaunchAgents'
    root.mkdir(parents=True)
    real = pwd.getpwuid(os.geteuid())
    user = SimpleNamespace(pw_uid=real.pw_uid, pw_gid=real.pw_gid, pw_name=real.pw_name, pw_dir=str(root.parents[1]))
    monkeypatch.setattr(lifecycle, '_identity', lambda *args: user)
    monkeypatch.setattr(lifecycle.platform, 'system', lambda: 'Darwin')
    manager = Launchctl()
    files = []
    for label, keep_alive in [('com.options-monitor.tick.us', False), ('com.options-monitor.trade-intake', True)]:
        payload = {'Label': label, 'ProgramArguments': [str(repo / 'om'), 'tick'],
                   'WorkingDirectory': str(repo), 'KeepAlive': keep_alive,
                   'EnvironmentVariables': {'OM_RUNTIME_ROOT': str(runtime)}}
        content = plistlib.dumps(payload).decode()
        path = root / (label + '.plist')
        path.write_text(content)
        manager.loaded[label] = (payload, path, 10 if keep_alive else None)
        files.append({'kind': 'launchd_plist', 'relative_path': 'launchd/' + path.name,
                      'install_path': str(path), 'content': content})
    profile = {'service_provider': 'launchd', 'repo_root': str(repo), 'runtime_root': str(runtime),
               'deploy_home': user.pw_dir, 'services': [{'name': item['Label']} for item, _, _ in manager.loaded.values()]}
    content = json.dumps(profile) + '\n'
    (runtime / 'service.profile.json').write_text(content)
    files.append({'kind': 'service_profile', 'relative_path': 'service.profile.json', 'content': content})
    bundle = {'files': files}
    monkeypatch.setattr(drift, '_expected_bundle_from_profile', lambda *args, **kwargs: bundle)
    monkeypatch.setattr(lifecycle, '_expected_bundle_from_profile', lambda *args, **kwargs: bundle)
    return dict(profile=profile, repo_root=repo, runtime_root=runtime, run_cmd=manager), manager, root, bundle


def test_launchd_reconcile_reloads_even_identical_definitions_and_preserves_paused(installed):
    args, manager, root, _ = installed
    paused = 'com.options-monitor.tick.us'
    manager.loaded.pop(paused)
    manager.disabled.add(paused)
    before = lifecycle.launchd_upgrade_state(**args)
    result = lifecycle.reconcile_launchd_services(**args, confirm=True)
    assert result['ok'] is True
    assert paused not in manager.loaded and paused in manager.disabled
    daemon = 'com.options-monitor.trade-intake'
    assert manager.loaded[daemon][2] != 10
    assert result['after'][daemon]['program'] == str(args['repo_root'] / 'om')
    assert result['before'] == before['states']
    actions = [call[1] for call in manager.calls]
    assert 'bootout' in actions and 'bootstrap' in actions
    assert actions.index('bootout') < actions.index('bootstrap')


@pytest.mark.parametrize('mode', ['unknown', 'foreign-runtime', 'foreign-path', 'foreign-program'])
def test_launchd_preflight_fails_before_mutation(installed, mode):
    args, manager, root, _ = installed
    original = args['run_cmd']
    def runner(command, **kwargs):
        result = original(command, **kwargs)
        if command[1] == 'print':
            if mode == 'unknown':
                return subprocess.CompletedProcess(command, 1, '', 'permission denied')
            text = result.stdout.replace(str(args['runtime_root']), '/foreign') if mode == 'foreign-runtime' else result.stdout
            if mode == 'foreign-path':
                text = text.replace(str(root), '/foreign')
            if mode == 'foreign-program':
                text = text.replace(str(args['repo_root'] / 'om'), '/foreign/om')
            return subprocess.CompletedProcess(command, result.returncode, text, result.stderr)
        return result
    args['run_cmd'] = runner
    with pytest.raises(lifecycle.AgentToolError):
        lifecycle.launchd_upgrade_state(**args)
    assert not any(c[1] in {'bootout', 'bootstrap', 'disable', 'enable'} for c in manager.calls)


@pytest.mark.parametrize('failure', ['bootout', 'bootstrap', 'stuck'])
def test_launchd_failed_or_ambiguous_reload_never_succeeds(installed, failure):
    args, manager, _, _ = installed
    manager.fail = failure
    manager.stuck = failure == 'stuck'
    result = lifecycle.reconcile_launchd_services(**args, confirm=True)
    assert result['ok'] is False and result['errors']
    if failure in {'bootout', 'stuck'}:
        assert not any(c[1] == 'bootstrap' for c in manager.calls)


def test_launchd_preview_never_mutates(installed):
    args, manager, _, _ = installed
    result = lifecycle.reconcile_launchd_services(**args, confirm=False)
    assert result['ok'] is True
    assert not any(c[1] in {'bootout', 'bootstrap'} for c in manager.calls)


def _bundle(root, version):
    import hashlib
    from src.application import runtime_bundle
    (root / 'runtime/python/bin').mkdir(parents=True)
    python = root / runtime_bundle.PYTHON
    python.write_text('#!/bin/sh\nexit 0\n')
    python.chmod(0o755)
    (root / 'VERSION').write_text(version + '\n')
    for name in ('om', 'om-agent'):
        (root / name).write_text('#!/bin/sh\nexit 0\n')
    (root / 'src/application/bot').mkdir(parents=True)
    (root / 'src/application/bot/runtime.py').write_text('# isolated runtime fixture\n')
    manifest = {'schema_version': 1, 'kind': 'options-monitor-runtime', 'version': version,
                'tag': 'v' + version, 'commit': 'a' * 40, 'repository': runtime_bundle.REPOSITORY,
                'platform': runtime_bundle.current_platform(),
                'minimum_system': runtime_bundle.MINIMUM_SYSTEMS[runtime_bundle.current_platform()],
                'python': {'executable': runtime_bundle.PYTHON, 'version': '3.12.12',
                           'source_url': 'https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz', 'sha256': 'b' * 64},
                'dependencies': {'pytest': '8.0.0'},
                'files': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in root.rglob('*') if path.is_file()}}
    (root / 'om.runtime.json').write_text(json.dumps(manifest))


@pytest.fixture
def upgrade_instance(installed, monkeypatch, tmp_path):
    from src.application import service_upgrade as upgrade
    from src.application import runtime_bundle
    args, manager, root, bundle = installed
    current = args['repo_root']
    old = tmp_path / 'releases/1.0.0'
    old.parent.mkdir()
    current.rename(old)
    current.symlink_to(old)
    _bundle(old, '1.0.0')
    new = old.parent / '1.0.1'
    new.mkdir()
    _bundle(new, '1.0.1')
    monkeypatch.setattr(runtime_bundle, 'resolve_release', lambda tag='latest', **kwargs: {'tag_name': 'v1.0.1', 'draft': False, 'prerelease': False})
    monkeypatch.setattr(runtime_bundle, 'validate_runtime', lambda root: {'ok': True, 'fixture': str(root)})
    monkeypatch.setattr(upgrade, '_prepare_runtime_configs_for_release', lambda **kwargs: {'targets': []})
    monkeypatch.setattr(upgrade, '_commit_prepared_runtime_configs', lambda **kwargs: {})
    monkeypatch.setattr(upgrade, '_validate_committed_runtime_configs', lambda **kwargs: [])
    base_files = list(bundle['files'])
    def renderer(*a, **k):
        rendered = []
        for item in base_files:
            item = dict(item)
            if item['kind'] == 'launchd_plist':
                data = plistlib.loads(item['content'].encode())
                if data['Label'] != 'com.options-monitor.upgrade':
                    data['EnvironmentVariables']['OM_RELEASE'] = current.resolve().name
                item['content'] = plistlib.dumps(data).decode()
            rendered.append(item)
        return {'files': rendered}
    monkeypatch.setattr(lifecycle, '_expected_bundle_from_profile', renderer)
    monkeypatch.setattr(drift, '_expected_bundle_from_profile', renderer)
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        assert command[0] != 'git'
        assert not any(value in command for value in ['pip', 'venv', 'brew'])
        if command[0] in {'launchctl', 'ps'}:
            return manager(command, **kwargs)
        if any('launchd_upgrade_state' in part for part in command):
            try:
                lifecycle.launchd_upgrade_state(**args)
            except lifecycle.AgentToolError as exc:
                return subprocess.CompletedProcess(command, 1, '', str(exc))
            return subprocess.CompletedProcess(command, 0, '', '')
        if len(command) > 2 and command[1:3] == ['service', 'drift']:
            result = drift.service_drift(repo_root=current, runtime_root=args['runtime_root'], confirm=True, run_cmd=manager)
            return subprocess.CompletedProcess(command, 0, json.dumps({'ok': True, 'tool_name': 'service.drift', 'data': result}), '')
        if len(command) == 4 and command[:3] == [str(old / runtime_bundle.PYTHON), '-I', '-c']:
            return subprocess.CompletedProcess(command, 0, '[false, false, false]', '')
        if '-I' in command:
            request = json.loads(Path(command[-1]).read_text())
            result = drift.service_drift(repo_root=current, runtime_root=args['runtime_root'],
                profile=request['profile'], confirm=True, run_cmd=manager,
                preserved_activation_states=request['preserved_activation_states'])
            health_profile = {**request['profile'], '_launchd_expected_states': request['preserved_activation_states']}
            health = upgrade._post_upgrade_service_health(profile=health_profile, repo_root=current, run_cmd=manager, operations=[])
            response = {'ok': result['summary']['ok'] and health['ok'], 'config_validate': [],
                        'service_reconcile': result, 'restarted_services': [], 'service_health': health,
                        'operations': [], 'errors': [], 'remediation': []}
            return subprocess.CompletedProcess(command, 0, json.dumps(response), '')
        assert '/runtime/python/bin/python3' in command[0] or command[0] == './om-agent'
        return subprocess.CompletedProcess(command, 0, '{}', '')
    return args, manager, old, new, runner, calls


def test_bundle_upgrade_launchd_reloads_target_without_git_venv_or_pip(upgrade_instance):
    from src.application import service_upgrade as upgrade
    args, manager, old, new, runner, calls = upgrade_instance
    paused = 'com.options-monitor.tick.us'
    manager.loaded.pop(paused)
    manager.disabled.add(paused)
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, target_version='1.0.1', confirm=True, run_cmd=runner)
    assert result['ok'] is True, result
    assert args['repo_root'].resolve() == new
    assert result['runtime_prepare']['installer'] == 'runtime_bundle'
    assert paused not in manager.loaded and paused in manager.disabled
    assert manager.loaded['com.options-monitor.trade-intake'][0]['EnvironmentVariables']['OM_RELEASE'] == '1.0.1'
    assert result['service_health']['ok'] is True
    assert 'com.options-monitor.trade-intake' in result['restarted_services']
    assert any('launchd_upgrade_state' in part for call in calls for part in call)


@pytest.mark.parametrize('unresolved', [False, True])
def test_bundle_launchd_failure_compensates_or_reports_unresolved(upgrade_instance, unresolved):
    from src.application import service_upgrade as upgrade
    args, manager, old, new, runner, calls = upgrade_instance
    original = manager.__call__
    failed = False
    def failing(command, **kwargs):
        nonlocal failed
        if command[:2] == ['launchctl', 'bootstrap'] and (not failed or unresolved):
            failed = True
            return subprocess.CompletedProcess(command, 1, '', 'injected bootstrap failure')
        return original(command, **kwargs)
    # runner uses this instance as callable; patch its class only in this test.
    from unittest.mock import patch
    with patch.object(Launchctl, '__call__', lambda self, command, **kw: failing(command, **kw)):
        result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
            releases_root=old.parent, target_version='1.0.1', confirm=True, run_cmd=runner)
    assert result['ok'] is False
    assert args['repo_root'].resolve() == old
    assert result['rolled_back'] is (not unresolved), result
    assert result['compensation']['ok'] is (not unresolved)
    if not unresolved:
        assert manager.loaded['com.options-monitor.trade-intake'][0]['EnvironmentVariables']['OM_RELEASE'] == '1.0.0'
    else:
        assert result['compensation']['errors']


def test_loaded_launchd_cannot_upgrade_with_restart_disabled(upgrade_instance):
    from src.application import service_upgrade as upgrade
    args, manager, old, new, runner, _ = upgrade_instance
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, target_version='1.0.1', confirm=True, restart_services=False, run_cmd=runner)
    assert result['ok'] is False and result['status'] == 'service_restart_required', result
    assert args['repo_root'].resolve() == old
    assert not any(command[1] == 'bootout' for command in manager.calls)


def test_target_without_launchd_capability_fails_before_switch(upgrade_instance):
    from src.application import service_upgrade as upgrade
    args, manager, old, new, runner, _ = upgrade_instance
    def unsupported(command, **kwargs):
        if any('launchd_upgrade_state' in part for part in command):
            return subprocess.CompletedProcess(command, 1, '', 'ImportError: no launchd lifecycle')
        return runner(command, **kwargs)
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, target_version='1.0.1', confirm=True, run_cmd=unsupported)
    assert result['ok'] is False
    assert args['repo_root'].resolve() == old
    assert not any(command[1] == 'bootout' for command in manager.calls)


@pytest.mark.parametrize('materialization', ['cached', 'downloaded'])
@pytest.mark.parametrize('damage', ['none', 'tag', 'version', 'repository', 'checksum', 'qualification', 'qualification_error'])
def test_bundle_upgrade_without_maintainer_script_preserves_validation(
    upgrade_instance, monkeypatch, tmp_path, materialization, damage,
):
    import shutil
    import sys
    import tarfile
    from src.application import runtime_bundle, service_upgrade as upgrade

    args, manager, old, new, runner, _ = upgrade_instance
    assert not (new / 'scripts/release_check.py').exists()
    manifest_path = new / runtime_bundle.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    if damage == 'tag':
        manifest.update(tag='v1.0.2', version='1.0.2')
        (new / 'VERSION').write_text('1.0.2\n')
        manifest['files']['VERSION'] = runtime_bundle.sha256(new / 'VERSION')
    elif damage == 'version':
        (new / 'VERSION').write_text('1.0.2\n')
        manifest['files']['VERSION'] = runtime_bundle.sha256(new / 'VERSION')
    elif damage == 'repository':
        manifest['repository'] = 'other/repository'
    elif damage == 'checksum':
        (new / 'om').write_text('tampered entrypoint\n')
    manifest_path.write_text(json.dumps(manifest))

    if materialization == 'downloaded':
        archive = tmp_path / 'target.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for path in new.rglob('*'):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(new))
        shutil.rmtree(new)

        def download(release, destination, **kwargs):
            assert release['tag_name'] == 'v1.0.1'
            assert kwargs['platform_name'] == runtime_bundle.current_platform()
            shutil.copyfile(archive, destination)
            return destination

        monkeypatch.setattr(runtime_bundle, 'download_bundle', download)

    qualified = []
    def qualify(root):
        qualified.append(Path(root))
        runtime_bundle.load_manifest(root, expected_platform=runtime_bundle.current_platform(), verify_files=True)
        if damage == 'qualification_error':
            raise runtime_bundle.RuntimeBundleError('runtime self-check failed')
        return {'ok': damage != 'qualification'}

    monkeypatch.setattr(runtime_bundle, 'validate_runtime', qualify)
    def checked_runner(command, **kwargs):
        # Exercise CPython's actual missing-file failure instead of a successful command stub.
        if command[1:2] == ['scripts/release_check.py']:
            return subprocess.run([sys.executable, *command[1:]], **kwargs)
        return runner(command, **kwargs)

    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, target_version='1.0.1', confirm=True, run_cmd=checked_runner)
    assert result['ok'] is (damage == 'none'), json.dumps(result.get('operations', [])[-1:])
    if damage == 'none':
        assert args['repo_root'].resolve() == new
        assert qualified[-1] == new
        assert result['runtime_prepare']['installer'] == 'runtime_bundle'
        assert manager.loaded['com.options-monitor.trade-intake'][0]['EnvironmentVariables']['OM_RELEASE'] == '1.0.1'
    else:
        assert args['repo_root'].resolve() == old
        assert result['symlink_switched'] is False
        assert not any(command[1] == 'bootout' for command in manager.calls)
        assert bool(qualified) is damage.startswith('qualification')


def test_latest_bundle_check_never_invokes_git(upgrade_instance):
    from src.application import service_upgrade as upgrade
    args, _, old, _, runner, calls = upgrade_instance
    result = upgrade.service_upgrade_check(repo_root=args['repo_root'], runtime_root=args['runtime_root'], run_cmd=runner)
    assert result['ok'] is True and result['latest_version'] == '1.0.1', result
    assert calls == []


def test_bundle_materialization_uses_verified_shared_archive_api(upgrade_instance, monkeypatch, tmp_path):
    import shutil
    import tarfile
    from src.application import service_upgrade as upgrade, runtime_bundle
    args, _, old, new, runner, calls = upgrade_instance
    archive = tmp_path / 'release.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in new.rglob('*'):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(new))
    shutil.rmtree(new)
    def download(release, destination, **kwargs):
        assert release['tag_name'] == 'v1.0.1'
        assert kwargs['platform_name'] == runtime_bundle.current_platform()
        shutil.copyfile(archive, destination)
        return destination
    monkeypatch.setattr(runtime_bundle, 'download_bundle', download)
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, target_version='1.0.1', confirm=True, run_cmd=runner)
    assert result['ok'] is True, result
    assert result['release_materialize']['method'] == 'runtime_bundle'
    assert new.exists() and args['repo_root'].resolve() == new
    assert not list(old.parent.glob('.om-upgrade-bundle-*'))


def test_corrupt_existing_bundle_fails_without_switch(upgrade_instance):
    from src.application import service_upgrade as upgrade
    args, _, old, new, runner, _ = upgrade_instance
    (new / 'om').write_text('tampered bundle entrypoint')
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, target_version='1.0.1', confirm=True, run_cmd=runner)
    assert result['ok'] is False
    assert args['repo_root'].resolve() == old


def test_bundle_launchd_rollback_reconciles_old_target(upgrade_instance):
    from src.application import service_upgrade as upgrade
    args, manager, old, new, runner, _ = upgrade_instance
    args['repo_root'].unlink()
    args['repo_root'].symlink_to(new)
    result = upgrade.service_rollback(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, to_version='1.0.0', confirm=True, run_cmd=runner)
    assert result['ok'] is True, result
    assert args['repo_root'].resolve() == old
    assert manager.loaded['com.options-monitor.trade-intake'][0]['EnvironmentVariables']['OM_RELEASE'] == '1.0.0'


def test_launchd_old_pid_must_exit_before_target_starts(installed, monkeypatch):
    args, manager, _, _ = installed
    original = manager.__call__
    monkeypatch.setattr(lifecycle.time, 'sleep', lambda _: None)
    def runner(command, **kwargs):
        if command[0] == 'ps':
            return subprocess.CompletedProcess(command, 0, '10', '')
        return original(command, **kwargs)
    args['run_cmd'] = runner
    result = lifecycle.reconcile_launchd_services(**args, confirm=True)
    assert result['ok'] is False
    assert any('process exit' in error for error in result['errors'])
    assert not any(command[1] == 'bootstrap' for command in manager.calls)


def test_launchd_retired_worker_cannot_survive_reconciliation(installed):
    args, manager, root, bundle = installed
    retired = 'com.options-monitor.tick.us'
    bundle['files'] = [item for item in bundle['files'] if not item.get('install_path', '').endswith(retired + '.plist')]
    desired_profile = {**args['profile'], 'services': [{'name': 'com.options-monitor.trade-intake'}]}
    bundle['files'][-1]['content'] = json.dumps(desired_profile)
    result = lifecycle.reconcile_launchd_services(**args, confirm=True)
    assert result['ok'] is True
    assert retired not in manager.loaded
    assert not (root / (retired + '.plist')).exists()


def _install_upgrade_controller(installed):
    args, manager, root, bundle = installed
    label = 'com.options-monitor.upgrade'
    item = bundle['files'][0]
    payload = plistlib.loads(item['content'].encode())
    payload['Label'] = label
    payload['KeepAlive'] = False
    payload['ProgramArguments'] = [str(args['repo_root'] / 'om'), 'update', 'apply',
        '--repo-root', str(args['repo_root']), '--runtime-root', str(args['runtime_root']),
        '--auto', '--confirm', '--preserve-activation-state']
    payload['StartCalendarInterval'] = {'Hour': 5, 'Minute': 0}
    path = root / (label + '.plist')
    path.write_bytes(plistlib.dumps(payload))
    bundle['files'].insert(0, {'kind': 'launchd_plist', 'relative_path': 'launchd/' + path.name,
        'install_path': str(path), 'content': path.read_text()})
    manager.loaded[label] = (payload, path, 123)
    args['profile']['services'].append({'name': label})
    content = json.dumps(args['profile']) + '\n'
    (args['runtime_root'] / 'service.profile.json').write_text(content)
    bundle['files'][-1]['content'] = content
    return path


@pytest.fixture
def scheduled_upgrade(installed, request):
    _install_upgrade_controller(installed)
    return request.getfixturevalue('upgrade_instance')


def test_scheduled_bundle_upgrade_retains_only_unchanged_controller(scheduled_upgrade):
    from src.application import service_upgrade as upgrade
    args, manager, old, new, runner, calls = scheduled_upgrade
    label = 'com.options-monitor.upgrade'
    payload, path, pid = manager.loaded[label]
    before = path.stat(), path.read_bytes()
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, confirm=True, auto=True, preserve_activation_state=True, run_cmd=runner)
    assert result['ok'] is True, result
    assert args['repo_root'].resolve() == new
    assert manager.loaded[label] == (payload, path, pid)
    assert (path.stat(), path.read_bytes()) == before
    assert label not in result['restarted_services']
    assert 'com.options-monitor.trade-intake' in result['restarted_services']
    assert manager.loaded['com.options-monitor.trade-intake'][0]['EnvironmentVariables']['OM_RELEASE'] == '1.0.1'
    assert not any(label in c[-1] for c in manager.calls if c[1] in {'bootout', 'bootstrap', 'enable', 'disable'})
    assert any('launchd_upgrade_state' in part for call in calls for part in call)
    assert result['service_health']['ok'] is True


@pytest.mark.parametrize('change', ['arguments', 'environment', 'calendar', 'retired', 'unstable'])
def test_scheduled_target_controller_changes_rejected_before_switch(scheduled_upgrade, monkeypatch, change):
    from src.application import service_upgrade as upgrade
    args, manager, old, _, runner, _ = scheduled_upgrade
    probed = False
    renderer = lifecycle._expected_bundle_from_profile
    def changed(*a, **kw):
        bundle = renderer(*a, **kw)
        for item in list(bundle['files']):
            if not item.get('relative_path', '').endswith('com.options-monitor.upgrade.plist'):
                continue
            data = plistlib.loads(item['content'].encode())
            if change == 'retired':
                bundle['files'].remove(item)
                continue
            if change == 'arguments':
                data['ProgramArguments'].append('--allow-major')
            elif change == 'environment':
                data['EnvironmentVariables']['NEW_SETTING'] = 'changed'
            elif change == 'calendar':
                data['StartCalendarInterval']['Hour'] = 6
            else:
                data['ProgramArguments'][0] = str(old / 'om')
                path = manager.loaded[data['Label']][1]
                path.write_bytes(plistlib.dumps(data))
                manager.loaded[data['Label']] = (data, path, 123)
            item['content'] = plistlib.dumps(data).decode()
        return bundle
    def target_runner(command, **kw):
        nonlocal probed
        if any('launchd_upgrade_state' in part for part in command):
            probed = True
            assert args['repo_root'].resolve() == old
            with monkeypatch.context() as context:
                context.setattr(lifecycle, '_expected_bundle_from_profile', changed)
                return runner(command, **kw)
        return runner(command, **kw)
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, confirm=True, auto=True, preserve_activation_state=True, run_cmd=target_runner)
    assert result['ok'] is False, result
    assert probed
    assert args['repo_root'].resolve() == old
    assert not any(c[1] in {'bootout', 'bootstrap', 'enable', 'disable'} for c in manager.calls)


@pytest.mark.parametrize('unresolved', [False, True])
def test_scheduled_upgrade_failure_compensation_reads_retained_controller(scheduled_upgrade, monkeypatch, unresolved):
    from src.application import service_upgrade as upgrade
    args, manager, old, _, runner, _ = scheduled_upgrade
    label = 'com.options-monitor.upgrade'
    before = manager.loaded[label]
    original = Launchctl.__call__
    failed = False
    def failing(self, command, **kw):
        nonlocal failed
        if command[:2] == ['launchctl', 'bootstrap'] and not failed:
            failed = True
            if unresolved:
                payload, path, pid = self.loaded[label]
                payload = {**payload, 'StartCalendarInterval': {'Hour': 7}}
                path.write_bytes(plistlib.dumps(payload))
                self.loaded[label] = (payload, path, pid)
            return subprocess.CompletedProcess(command, 1, '', 'injected target bootstrap failure')
        return original(self, command, **kw)
    monkeypatch.setattr(Launchctl, '__call__', failing)
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, confirm=True, auto=True, preserve_activation_state=True, run_cmd=runner)
    assert failed
    assert result['ok'] is False and args['repo_root'].resolve() == old
    assert result['compensation']['ok'] is (not unresolved), result
    assert result['rolled_back'] is (not unresolved)
    assert not any(label in c[-1] for c in manager.calls if c[1] in {'bootout', 'bootstrap', 'enable', 'disable'})
    if unresolved:
        assert result['compensation']['errors']
    else:
        assert manager.loaded[label] == before
        assert result['compensation']['service_health']['ok'] is True
        assert manager.loaded['com.options-monitor.trade-intake'][0]['EnvironmentVariables']['OM_RELEASE'] == '1.0.0'


@pytest.mark.parametrize('change', ['definition', 'loaded-program'])
def test_retained_controller_target_readback_rejects_stale_state(installed, monkeypatch, change):
    _install_upgrade_controller(installed)
    args, manager, _, _ = installed
    label = 'com.options-monitor.upgrade'
    original = Launchctl.__call__
    def changed(self, command, **kw):
        result = original(self, command, **kw)
        if command[:2] == ['launchctl', 'bootstrap']:
            payload, path, pid = self.loaded[label]
            payload = dict(payload)
            if change == 'definition':
                payload['StartCalendarInterval'] = {'Hour': 7}
                path.write_bytes(plistlib.dumps(payload))
            else:
                payload['ProgramArguments'] = ['/stale/om']
            self.loaded[label] = (payload, path, pid)
        return result
    monkeypatch.setattr(Launchctl, '__call__', changed)
    result = lifecycle.reconcile_launchd_services(**args, confirm=True)
    assert result['ok'] is False
    assert any('readback mismatch' in error for error in result['errors'])
    assert label not in result['restarted_services']


def test_scheduled_controller_finishing_before_health_still_requires_target_definition(scheduled_upgrade):
    from src.application import service_upgrade as upgrade
    args, manager, old, _, runner, _ = scheduled_upgrade
    label = 'com.options-monitor.upgrade'
    def finished(command, **kw):
        result = runner(command, **kw)
        if len(command) > 2 and command[1:3] == ['service', 'drift']:
            payload, path, _ = manager.loaded[label]
            payload = {**payload, 'StartCalendarInterval': {'Hour': 7}}
            path.write_bytes(plistlib.dumps(payload))
            manager.loaded[label] = (payload, path, None)
        return result
    result = upgrade.service_upgrade(repo_root=args['repo_root'], runtime_root=args['runtime_root'],
        releases_root=old.parent, confirm=True, auto=True, preserve_activation_state=True, run_cmd=finished)
    assert result['ok'] is False and result['rolled_back'] is True, result
    assert result['failure_status'] == 'upgraded_service_health_failed'
    assert result['compensation']['ok'] is True
    assert result['compensation']['service_health']['ok'] is True
    assert manager.loaded[label][0]['StartCalendarInterval'] == {'Hour': 5, 'Minute': 0}
    assert args['repo_root'].resolve() == old


@pytest.mark.parametrize('channel', ['feishu-ws', 'wechat-clawbot'])
def test_launchd_channel_health_reports_actual_label_and_native_remediation(monkeypatch, tmp_path, channel):
    from src.application import service_upgrade as upgrade
    label = 'com.options-monitor.' + channel
    definition = tmp_path / (label + '.plist')
    definition.write_text('isolated expected definition')
    monkeypatch.setattr(lifecycle, 'launchd_upgrade_state', lambda **kwargs: {
        'units': [{'name': label, 'path': str(definition), 'content': definition.read_text()}],
        'states': {label: {'state': 'running', 'enabled': True}}})
    profile = {'service_provider': 'launchd', 'runtime_root': str(tmp_path), 'repo_root': str(tmp_path)}
    result = upgrade._post_upgrade_service_health(profile=profile, repo_root=tmp_path,
        run_cmd=lambda command, **kwargs: subprocess.CompletedProcess(command, 1, '', 'check failed'), operations=[])
    assert result['ok'] is False
    assert result['failed_checks'][0]['service'] == label
    assert not any('systemctl' in command or 'sudo' in command for command in result['remediation'])
