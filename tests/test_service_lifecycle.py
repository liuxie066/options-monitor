from __future__ import annotations

import json
import os
import plistlib
import pwd
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml_init import init_yaml_config
from src.application.service_lifecycle import service_lifecycle

REPO = Path(__file__).resolve().parents[1]


class Manager:
    """Bounded fake manager; every mutation stays in memory under tmp_path."""
    def __init__(self, root: Path, target: str):
        self.root, self.target = root, target
        self.calls: list[list[str]] = []
        self.states: dict[str, tuple[str, object]] = {}
        self.fail: str | None = None
        self.timeout = False
        self.keep_idle = False

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        assert command[0] in {'systemctl', 'launchctl'}
        assert kwargs['timeout'] in {3, 20}
        if command[1] == 'show':
            name = command[2]
            active, enabled = self.states.get(name, ('inactive', 'disabled'))
            path = self.root / name
            loaded = 'loaded' if path.exists() else 'not-found'
            environment = ' '.join(line.split('=', 1)[1] for line in path.read_text().splitlines() if line.startswith('Environment=')) if path.exists() else ''
            return subprocess.CompletedProcess(command, 0, f'LoadState={loaded}\nActiveState={active}\nSubState=waiting\nUnitFileState={enabled}\nFragmentPath={path}\nEnvironment={environment}\n', '')
        if command[1] == 'print-disabled':
            text = '\n'.join(f'"{name}" => true' for name, (_, enabled) in self.states.items() if not enabled)
            return subprocess.CompletedProcess(command, 0, text, '')
        if command[1] == 'print':
            name = command[-1].split('/')[-1]
            active, _ = self.states.get(name, ('absent', True))
            path = self.root / (name + '.plist')
            runtime = plistlib.loads(path.read_bytes())['EnvironmentVariables']['OM_RUNTIME_ROOT'] if path.exists() else ''
            return subprocess.CompletedProcess(command, 113 if active == 'absent' else 0,
                                               ('pid = 12' if active == 'active' else 'state = waiting') + f'\npath = {path}\nOM_RUNTIME_ROOT => {runtime}', '')
        if self.fail and self.fail == command[1]:
            if self.timeout:
                raise subprocess.TimeoutExpired(command, 20)
            return subprocess.CompletedProcess(command, 1, '', 'injected manager failure')
        if command[0] == 'systemctl' and command[1] != 'daemon-reload':
            name = command[-1]
            if command[1] == 'enable':
                self.states[name] = ('active', 'enabled')
            elif command[1] == 'disable':
                self.states[name] = ('inactive', 'disabled')
            elif command[1] == 'stop':
                self.states[name] = ('inactive', self.states.get(name, ('active', 'static'))[1])
        elif command[0] == 'launchctl':
            name = command[-1].split('/')[-1]
            if command[1] == 'bootstrap':
                payload = plistlib.loads(Path(command[-1]).read_bytes())
                name = payload['Label']
                active = 'active' if payload.get('KeepAlive') and not self.keep_idle else 'inactive'
                self.states[name] = (active, True)
            elif command[1] in {'disable', 'enable'}:
                self.states[name] = (self.states.get(name, ('absent', True))[0], command[1] == 'enable')
            elif command[1] == 'bootout':
                self.states[name] = ('absent', self.states.get(name, ('active', True))[1])
            elif command[1] == 'kickstart':
                self.states[name] = ('active', self.states[name][1])
        return subprocess.CompletedProcess(command, 0, '', '')


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.delenv('OM_ENV_FILE', raising=False)
    monkeypatch.delenv('OM_RUNTIME_ROOT', raising=False)
    runtime = tmp_path / 'runtime'
    init_yaml_config(repo_root=REPO, output_config_yaml_path=runtime / 'config.yaml', runtime_output_dir=runtime,
                     markets=['us'], account_label='test', futu_acc_id='123456', us_symbols=['AAPL'],
                     symbol_policies={'AAPL': {'strategy': 'csp', 'csp_max_strike': 100}})
    return runtime


def context(instance, tmp_path, target):
    root = tmp_path / 'units'
    manager = Manager(root, target)
    return dict(repo_root=REPO, runtime_root=instance, config_yaml=instance / 'config.yaml',
                env_file=instance / 'options-monitor.env', target=target, unit_root=root,
                system='Linux' if target == 'systemd' else 'Darwin', run_cmd=manager,
                credential_store_root=tmp_path / 'credentials'), manager


def apply(action, ctx):
    preview = service_lifecycle(action, **ctx)
    return service_lifecycle(action, **ctx, confirm=True, expected_preview_sha256=preview['preview_sha256'])


@pytest.mark.parametrize('target', ['systemd', 'launchd'])
def test_install_preview_cancel_and_apply_without_start(instance, tmp_path, target):
    ctx, manager = context(instance, tmp_path, target)
    preview = service_lifecycle('install', **ctx)
    assert not ctx['unit_root'].exists()
    assert not (instance / 'options-monitor.env').exists()
    assert not (instance / 'service.profile.json').exists()
    assert not any(call[1] not in {'show', 'print', 'print-disabled'} for call in manager.calls)
    assert '交易采集' in str(preview['side_effects'])
    assert not preview['required_credentials']
    cancelled = service_lifecycle('install', **ctx, confirm=True, expected_preview_sha256=preview['preview_sha256'], cancelled=lambda: True)
    assert cancelled['status'] == 'cancelled' and not cancelled['changed']
    assert not ctx['unit_root'].exists()
    result = apply('install', ctx)
    assert result['ok'] and result['status'] == 'installed'
    assert (instance / 'options-monitor.env').stat().st_mode & 0o777 == 0o600
    assert (instance / 'service.profile.json').stat().st_mode & 0o777 == 0o600
    assert (instance / 'config.yaml').stat().st_uid == os.getuid()
    mutations = [call for call in manager.calls if call[1] not in {'show', 'print', 'print-disabled'}]
    assert mutations == ([['systemctl', 'daemon-reload']] if target == 'systemd' else [])
    profile = json.loads((instance / 'service.profile.json').read_text())
    assert profile['env_file'] == str(instance / 'options-monitor.env')
    assert profile['accounts'] == ['test']
    assert profile['markets'] == ['us']
    assert not (instance / 'option_positions.db').exists()


@pytest.mark.parametrize('target', ['systemd', 'launchd'])
def test_install_start_and_stop_share_profile_and_idempotent_state(instance, tmp_path, target):
    ctx, manager = context(instance, tmp_path, target)
    apply('install', ctx)
    manager.calls.clear()
    result = apply('start', ctx)
    assert result['ok'], result
    assert result['status'] == 'started'
    starts = [call for call in manager.calls if call[1] in {'enable', 'bootstrap', 'kickstart'}]
    assert starts
    if target == 'systemd':
        assert ['systemctl', 'enable', '--now', 'options-monitor-trade-intake.service'] in starts
        assert not any(call[-1] == 'options-monitor-tick-us.service' for call in starts)
    manager.calls.clear()
    assert apply('start', ctx)['ok']
    assert not any(call[1] in {'enable', 'bootstrap', 'kickstart'} for call in manager.calls)
    # A broken/absent authoring file must not prevent safely stopping our installed definitions.
    (instance / 'config.yaml').unlink()
    result = apply('stop', ctx)
    assert result['ok'], result
    assert result['status'] == 'stopped'
    assert (instance / 'service.profile.json').exists()


def test_stale_preview_rejected_before_writes(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    preview = service_lifecycle('install', **ctx)
    (instance / 'options-monitor.env').write_text('OM_SETTING=value\n')
    with pytest.raises(AgentToolError, match='STALE_PREVIEW'):
        service_lifecycle('install', **ctx, confirm=True, expected_preview_sha256=preview['preview_sha256'])
    assert not any(call[1] not in {'show', 'print', 'print-disabled'} for call in manager.calls)
    assert not ctx['unit_root'].exists()


def test_cross_instance_unit_blocks_takeover(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    path = ctx['unit_root'] / 'options-monitor-tick-us.service'
    path.write_text(path.read_text().replace(str(instance), str(tmp_path / 'other-runtime')))
    manager.calls.clear()
    with pytest.raises(AgentToolError, match='SERVICE_INSTANCE_CONFLICT'):
        service_lifecycle('install', **ctx)
    assert not any(call[1] not in {'show', 'print', 'print-disabled'} for call in manager.calls)


def test_root_requires_explicit_nonroot_deploy_identity(instance, tmp_path):
    ctx, _ = context(instance, tmp_path, 'systemd')
    with pytest.raises(AgentToolError, match='SERVICE_IDENTITY_REQUIRED'):
        service_lifecycle('install', **ctx, euid=0)
    name = pwd.getpwuid(os.getuid()).pw_name
    result = apply('install', {**ctx, 'euid': 0, 'deploy_user': name})
    assert result['scope']['deploy_user'] == name
    assert (instance / 'config.yaml').stat().st_uid == os.getuid()


def test_failed_reload_can_retry_identical_files_without_start(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    manager.fail = 'daemon-reload'
    result = apply('install', ctx)
    assert result['status'] == 'failed' and result['changed']
    assert all(op.get('readback', 'matched') == 'matched' for op in result['operations'])
    manager.fail = None
    manager.calls.clear()
    retry = apply('install', ctx)
    assert retry['ok'] and retry['changed'] and not retry['files_changed']
    assert [call for call in manager.calls if call[1] != 'show'] == [['systemctl', 'daemon-reload']]


def test_manager_timeout_is_unknown_not_success_or_retried(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    manager.fail, manager.timeout = 'enable', True
    result = apply('start', ctx)
    assert result['status'] == 'unknown' and not result['ok']
    assert len(result['operations']) == 1
    assert result['operations'][0]['outcome'] == 'unknown'
    assert result['after']


def test_loaded_launchd_daemon_without_pid_is_not_ready(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'launchd')
    apply('install', ctx)
    manager.keep_idle = True
    result = apply('start', ctx)
    assert result['status'] == 'unverified' and not result['ok']
    assert any(row['state'] == 'loaded-not-running' for row in result['after'].values())


def test_mocked_platform_never_defaults_to_real_etc(instance):
    with pytest.raises(AgentToolError, match='isolated unit root'):
        service_lifecycle('install', repo_root=REPO, runtime_root=instance, system='Linux', run_cmd=lambda *a, **k: None)


def test_cli_facade_passes_explicit_scope_and_confirmation(tmp_path):
    from src.interfaces.cli.main import parse_args
    from src.interfaces.cli.service_ops import handle_service_update_command
    args = parse_args(['service', 'install', '--runtime-root', str(tmp_path), '--config-yaml', str(tmp_path / 'config.yaml'),
                       '--deploy-user', 'alice', '--confirm', '--expected-preview-sha256', 'abc', '--include-feishu-ws', '--channel-market', 'us'])
    seen = {}
    def lifecycle(action, **kwargs):
        seen.update(action=action, **kwargs)
        return {'ok': True, 'status': 'installed'}
    result = handle_service_update_command(args, service_lifecycle_fn=lifecycle, repo_base_fn=lambda: REPO)
    assert result['ok']
    assert seen['action'] == 'install' and seen['confirm'] is True
    assert seen['expected_preview_sha256'] == 'abc'
    assert seen['include_feishu_ws'] and seen['channel_market'] == 'us'
    assert seen['runtime_root'] == tmp_path


def rewrite_notifications(instance, enabled):
    from src.application.config_yaml import build_yaml_runtime_config_file
    path = instance / 'config.yaml'
    document = yaml.safe_load(path.read_text())
    document['notifications'] = {'enabled': enabled, 'provider': 'feishu_app'}
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    build_yaml_runtime_config_file(repo_root=REPO, market='us', config_path=path,
                                   output_config_path=instance / 'config.us.json')


def test_feature_bindings_retire_disabled_dropins_with_backup(instance, tmp_path):
    ctx, _ = context(instance, tmp_path, 'systemd')
    rewrite_notifications(instance, True)
    install = apply('install', ctx)
    assert 'feishu.bot.app_secret' in install['required_credentials']
    dropin = ctx['unit_root'] / 'options-monitor-tick-us.service.d/zzzz-secret-credentials.conf'
    old_content = dropin.read_bytes()
    with pytest.raises(AgentToolError, match='SERVICE_CREDENTIALS_MISSING'):
        apply('start', ctx)
    rewrite_notifications(instance, False)
    preview = service_lifecycle('install', **ctx)
    assert not preview['required_credentials']
    assert any(item.get('remove') == str(dropin) for item in preview['planned_operations'])
    result = apply('install', ctx)
    assert result['ok'] and not dropin.exists()
    operation = next(item for item in result['operations'] if item.get('path') == str(dropin))
    assert Path(operation['backup_path']).read_bytes() == old_content
    assert operation['readback'] == 'matched'
    assert apply('start', ctx)['ok']


def test_activation_source_and_env_overrides_rejected(instance, tmp_path):
    ctx, _ = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    for key, path in [('env_file', tmp_path / 'elsewhere.env'), ('config_yaml', tmp_path / 'elsewhere.yaml')]:
        with pytest.raises(AgentToolError, match='SERVICE_INSTANCE_CONFLICT'):
            service_lifecycle('start', **{**ctx, key: path})


def test_cancellation_after_one_write_reports_partial_progress(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    preview = service_lifecycle('install', **ctx)
    checks = iter([False, False, True])
    result = service_lifecycle('install', **ctx, confirm=True, expected_preview_sha256=preview['preview_sha256'],
                               cancelled=lambda: next(checks, True))
    assert result['status'] == 'cancelled' and result['changed']
    assert len(result['operations']) == 1 and result['operations'][0]['readback'] == 'matched'
    assert not (instance / 'service.profile.json').exists()
    assert not any(call[1] == 'daemon-reload' for call in manager.calls)
    assert apply('install', ctx)['ok']


def test_file_failure_keeps_backup_and_reports_completed_operations(instance, tmp_path, monkeypatch):
    import src.application.service_lifecycle as module
    ctx, _ = context(instance, tmp_path, 'systemd')
    original = module._atomic_file
    def fail_unit(path, *args, **kwargs):
        if str(path).endswith('.service'):
            raise OSError('injected disk failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(module, '_atomic_file', fail_unit)
    result = apply('install', ctx)
    assert result['status'] == 'failed' and result['changed']
    assert result['operations'][0]['readback'] == 'matched'
    assert result['operations'][-1]['readback'] == 'unverified'
    assert not (instance / 'service.profile.json').exists()


def test_symlinked_unit_is_not_replaced(instance, tmp_path):
    ctx, _ = context(instance, tmp_path, 'systemd')
    ctx['unit_root'].mkdir()
    victim = tmp_path / 'untouched'
    victim.write_text('preserve')
    (ctx['unit_root'] / 'options-monitor-tick-us.service').symlink_to(victim)
    with pytest.raises(AgentToolError, match='SERVICE_PATH_UNSAFE'):
        service_lifecycle('install', **ctx)
    assert victim.read_text() == 'preserve'


def test_start_rejects_stale_runtime_and_changed_confirmation(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    preview = service_lifecycle('start', **ctx)
    manager.states['options-monitor-tick-us.timer'] = ('active', 'enabled')
    with pytest.raises(AgentToolError, match='STALE_PREVIEW'):
        service_lifecycle('start', **ctx, confirm=True, expected_preview_sha256=preview['preview_sha256'])
    path = instance / 'config.yaml'
    document = yaml.safe_load(path.read_text())
    document['markets']['us']['overrides']['AAPL']['sell_put']['max_strike'] = 101
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(AgentToolError, match='SERVICE_CONFIG_STALE'):
        service_lifecycle('start', **ctx)


def test_loaded_foreign_definition_blocks_install_even_when_local_file_missing(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'launchd')
    def foreign(command, **kwargs):
        if command[1] == 'print':
            return subprocess.CompletedProcess(command, 0, 'pid = 12\npath = /elsewhere/foreign.plist', '')
        return manager(command, **kwargs)
    with pytest.raises(AgentToolError, match='SERVICE_INSTANCE_CONFLICT'):
        service_lifecycle('install', **{**ctx, 'run_cmd': foreign})
    assert not ctx['unit_root'].exists()


def test_feature_aware_renderer_keeps_legacy_default_and_profile_policy(instance, tmp_path, monkeypatch):
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import _expected_bundle_from_profile
    options = dict(target='systemd', repo_root=REPO, runtime_root=instance, config_yaml=instance / 'config.yaml',
                   markets=['us'], config_paths={'us': instance / 'config.us.json'}, include_secret_credentials=True,
                   secret_credential_store_root=tmp_path / 'credentials', deploy_user=pwd.getpwuid(os.getuid()).pw_name,
                   use_default_deploy_user=False)
    def profile(bundle):
        return json.loads(next(item['content'] for item in bundle['files'] if item['kind'] == 'service_profile'))
    legacy = profile(render_service_bundle(**options))
    assert legacy['secret_credentials']['service_credentials']
    assert 'binding_policy' not in legacy['secret_credentials']
    current = profile(render_service_bundle(**options, feature_aware_credentials=True))
    assert current['secret_credentials']['binding_policy'] == 'enabled-consumers-v1'
    assert not current['secret_credentials']['service_credentials']
    rebuilt = profile(_expected_bundle_from_profile(current, provider='systemd', repo_root=REPO, runtime_root=instance))
    assert rebuilt['secret_credentials'] == current['secret_credentials']
    # Shell gates are not systemd's env; only selected ordinary env is authoritative.
    monkeypatch.setenv('OM_INBOUND_OPERATIONS_ENABLED', 'true')
    current = profile(render_service_bundle(**options, feature_aware_credentials=True, include_feishu_ws=True))
    assert current['secret_credentials']['service_credentials']['options-monitor-feishu-ws.service'] == ['feishu.bot.app_secret']


def test_feature_binding_keeps_selected_enabled_llm_and_explicit_operations_gate(instance, tmp_path):
    from src.application.service_deploy import render_service_bundle
    path = instance / 'config.yaml'
    document = yaml.safe_load(path.read_text())
    document['bot']['enabled'] = True
    document['bot']['enabled'] = True
    path.write_text(yaml.safe_dump(document))
    env = instance / 'options-monitor.env'
    env.write_text('OM_INBOUND_OPERATIONS_ENABLED=true\n')
    result = render_service_bundle(target='systemd', repo_root=REPO, runtime_root=instance, config_yaml=path,
                                   markets=['us'], include_feishu_ws=True, include_secret_credentials=True,
                                   feature_aware_credentials=True, env_file=env,
                                   secret_credential_store_root=tmp_path / 'credentials')
    profile = json.loads(next(item['content'] for item in result['files'] if item['kind'] == 'service_profile'))
    credentials = profile['secret_credentials']['service_credentials']
    assert set(credentials['options-monitor-feishu-ws.service']) == {'feishu.bot.app_secret', 'inbound.operation_hmac_key', 'llm.deepseek.api_key'}
    assert all('feishu.holdings.app_secret' not in names for names in credentials.values())


@pytest.mark.parametrize('target', ['systemd', 'launchd'])
@pytest.mark.parametrize('evidence', ['missing', 'foreign', 'long-valid'])
def test_loaded_runtime_scope_requires_untruncated_evidence(instance, tmp_path, target, evidence):
    ctx, manager = context(instance, tmp_path, target)
    apply('install', ctx)
    if target == 'launchd':
        apply('start', ctx)
    def probe(command, **kwargs):
        result = manager(command, **kwargs)
        if command[1] in {'show', 'print'} and '.timer' not in command[2]:
            text = result.stdout
            if evidence == 'missing':
                text = '\n'.join(line for line in text.splitlines() if not line.startswith(('Environment=', 'OM_RUNTIME_ROOT')))
            elif evidence == 'foreign':
                if target == 'systemd':
                    text = text.replace('OM_RUNTIME_ROOT=' + str(instance), 'OM_RUNTIME_ROOT=/some/other/instance')
                else:
                    text = text.replace('OM_RUNTIME_ROOT => ' + str(instance), 'OM_RUNTIME_ROOT => /some/other/instance')
            text = 'description = ' + ('x' * 3000) + '\n' + text
            return subprocess.CompletedProcess(command, result.returncode, text, result.stderr)
        return result
    if evidence == 'long-valid':
        result = service_lifecycle('start', **{**ctx, 'run_cmd': probe})
        assert result['ok']
        assert 'x' * 3000 not in json.dumps(result)
    else:
        with pytest.raises(AgentToolError, match='SERVICE_INSTANCE_UNVERIFIED' if evidence == 'missing' else 'SERVICE_INSTANCE_CONFLICT'):
            service_lifecycle('start', **{**ctx, 'run_cmd': probe})


def test_preview_hides_existing_env_values_and_live_probe_environment(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    sentinel = 'test-only-secret-must-not-be-printed'
    env = instance / 'options-monitor.env'
    env.write_text('DEEPSEEK_API_KEY=' + sentinel + '\n')
    apply('install', ctx)
    unit = ctx['unit_root'] / 'options-monitor-tick-us.service'
    unit.write_text(unit.read_text().replace('[Service]', '[Service]\nEnvironment="DEEPSEEK_API_KEY=' + sentinel + '"'))
    result = service_lifecycle('install', **ctx)
    assert sentinel not in json.dumps(result)
    assert '<redacted>' in str(result['files'])
    assert env.read_text() == 'DEEPSEEK_API_KEY=' + sentinel + '\n'


def test_inbound_install_requires_current_selected_bot_snapshot(instance, tmp_path):
    from src.application.config_yaml import build_yaml_bot_config_file
    ctx, manager = context(instance, tmp_path, 'systemd')
    ctx['include_feishu_ws'] = True
    with pytest.raises(AgentToolError, match='SERVICE_CONFIG_MISSING'):
        service_lifecycle('install', **ctx)
    assert not manager.calls and not ctx['unit_root'].exists()
    bot_path = instance / 'resolved/config.bot.json'
    build_yaml_bot_config_file(repo_root=REPO, config_path=instance / 'config.yaml', output_config_path=bot_path)
    preview = service_lifecycle('install', **ctx)
    assert any(row['path'] == str(bot_path) for row in preview['input_files'])
    data = json.loads(bot_path.read_text())
    data['bot']['enabled'] = True
    bot_path.write_text(json.dumps(data))
    with pytest.raises(AgentToolError, match='SERVICE_CONFIG_STALE'):
        service_lifecycle('install', **ctx)


def test_interrupt_during_activation_preserves_unknown_operation_and_reads_back(instance, tmp_path):
    ctx, manager = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    def interrupt(command, **kwargs):
        if command[1] == 'enable':
            manager(command, **kwargs)  # The external effect preceded Ctrl-C.
            raise KeyboardInterrupt()
        return manager(command, **kwargs)
    result = apply('start', {**ctx, 'run_cmd': interrupt})
    assert result['status'] == 'unknown' and not result['ok']
    assert result['changed']
    assert result['operations'][0]['error'] == 'KeyboardInterrupt'
    assert any(row['state'] == 'running' for row in result['after'].values())


def test_unknown_manager_blocks_install_before_any_managed_write(instance, tmp_path):
    ctx, _ = context(instance, tmp_path, 'systemd')
    def unavailable(command, **kwargs):
        raise OSError('manager unavailable')
    with pytest.raises(AgentToolError, match='SERVICE_STATE_UNKNOWN'):
        service_lifecycle('install', **{**ctx, 'run_cmd': unavailable})
    assert not ctx['unit_root'].exists()
    assert not (instance / 'options-monitor.env').exists()


@pytest.mark.parametrize('target', ['systemd', 'launchd'])
def test_two_runtime_installs_serialize_shared_unit_names(instance, tmp_path, target, monkeypatch):
    import src.application.service_lifecycle as module
    first, manager = context(instance, tmp_path, target)
    other = tmp_path / 'other-runtime'
    init_yaml_config(repo_root=REPO, output_config_yaml_path=other / 'config.yaml', runtime_output_dir=other,
                     markets=['us'], account_label='test', futu_acc_id='123456', us_symbols=['AAPL'],
                     symbol_policies={'AAPL': {'strategy': 'csp', 'csp_max_strike': 100}})
    second = {**first, 'runtime_root': other, 'config_yaml': other / 'config.yaml',
              'env_file': other / 'options-monitor.env'}
    preview_a = service_lifecycle('install', **first)
    preview_b = service_lifecycle('install', **second)
    assert not first['unit_root'].exists(), 'preview must not create the shared lock directory'
    real_mkdir = module._mkdir
    rejected = []
    def interleave(path, **kwargs):
        # A has acquired the shared lock and rechecked the absent unit files;
        # B tries the same fixed names from its independently confirmed preview.
        if path == instance / 'logs' and not rejected:
            with pytest.raises(AgentToolError) as failure:
                service_lifecycle('install', **second, confirm=True,
                                  expected_preview_sha256=preview_b['preview_sha256'])
            rejected.append(failure.value.code)
        return real_mkdir(path, **kwargs)
    monkeypatch.setattr(module, '_mkdir', interleave)
    result = service_lifecycle('install', **first, confirm=True,
                               expected_preview_sha256=preview_a['preview_sha256'])
    assert result['ok'] and rejected == ['SERVICE_BUSY']
    assert not (other / 'service.profile.json').exists()
    assert not (other / 'options-monitor.env').exists()
    with pytest.raises(AgentToolError, match='SERVICE_INSTANCE_CONFLICT'):
        service_lifecycle('install', **second)
    assert json.loads((instance / 'service.profile.json').read_text())['runtime_root'] == str(instance)


def test_systemd_explicit_env_selection_reaches_cli_bootstrap(instance, monkeypatch):
    from src.application.service_deploy import render_service_bundle
    from src.application.settings.effective import parse_env_file
    from src.interfaces.cli.command_environment import command_environment
    selected = instance / 'selected.env'
    selected.write_text('OM_FEISHU_BOT_USER_OPEN_ID=ou_selected\nPORTFOLIO_SERVICE_URL=http://127.0.0.1:8766\n')
    (instance / 'options-monitor.env').write_text('OM_FEISHU_BOT_USER_OPEN_ID=ou_default\nPORTFOLIO_SERVICE_URL=http://127.0.0.1:8765\n')
    bundle = render_service_bundle(target='systemd', repo_root=REPO, runtime_root=instance,
                                   config_yaml=instance / 'config.yaml', markets=['us'],
                                   config_paths={'us': instance / 'config.us.json'}, env_file=selected)
    unit = next(row['content'] for row in bundle['files'] if row['relative_path'] == 'systemd/options-monitor-tick-us.service')
    process_env = {}
    argv = []
    for line in unit.splitlines():
        if line.startswith('Environment='):
            process_env.update(item.split('=', 1) for item in shlex.split(line.split('=', 1)[1]))
        elif line.startswith('EnvironmentFile='):
            process_env.update(parse_env_file(Path(shlex.split(line.split('=', 1)[1])[0]).read_text()))
        elif line.startswith('ExecStart='):
            argv = shlex.split(line.split('=', 1)[1])[1:]
    assert process_env['OM_ENV_FILE'] == str(selected)
    for key, value in process_env.items():
        monkeypatch.setenv(key, value)
    with command_environment(argv, repo_root=REPO):
        assert os.environ['OM_ENV_FILE'] == str(selected)
        assert os.environ['OM_FEISHU_BOT_USER_OPEN_ID'] == 'ou_selected'
        assert os.environ['PORTFOLIO_SERVICE_URL'] == 'http://127.0.0.1:8766'


def test_shared_lock_rechecks_manager_state_before_activation(instance, tmp_path, monkeypatch):
    import src.application.service_lifecycle as module
    ctx, manager = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    preview = service_lifecycle('start', **ctx)
    manager.calls.clear()
    real_flock = module.fcntl.flock
    def changed_when_locked(descriptor, operation):
        result = real_flock(descriptor, operation)
        manager.states['options-monitor-tick-us.timer'] = ('active', 'enabled')
        return result
    monkeypatch.setattr(module.fcntl, 'flock', changed_when_locked)
    with pytest.raises(AgentToolError, match='service manager state changed'):
        service_lifecycle('start', **ctx, confirm=True, expected_preview_sha256=preview['preview_sha256'])
    assert not any(call[1] != 'show' for call in manager.calls)


def test_systemd_definition_directories_are_public_but_runtime_remains_private(instance, tmp_path):
    ctx, _ = context(instance, tmp_path, 'systemd')
    rewrite_notifications(instance, True)
    previous_umask = os.umask(0o077)
    try:
        result = apply('install', ctx)
    finally:
        os.umask(previous_umask)
    assert result['ok']
    dropin = ctx['unit_root'] / 'options-monitor-tick-us.service.d/zzzz-secret-credentials.conf'
    assert ctx['unit_root'].stat().st_mode & 0o777 == 0o755
    assert dropin.parent.stat().st_mode & 0o777 == 0o755
    assert dropin.stat().st_mode & 0o777 == 0o644
    assert (instance / 'logs').stat().st_mode & 0o777 == 0o700
    assert (instance / 'options-monitor.env').stat().st_mode & 0o777 == 0o600
    assert (instance / 'service.profile.json').stat().st_mode & 0o777 == 0o600
    assert not ctx['credential_store_root'].exists()


def test_protected_service_directory_returns_actionable_error_before_any_change(instance, tmp_path, monkeypatch):
    ctx, manager = context(instance, tmp_path, 'systemd')
    rewrite_notifications(instance, True)
    apply('install', ctx)
    manager.calls.clear()
    protected = ctx['unit_root'] / 'options-monitor-tick-us.service.d/zzzz-secret-credentials.conf'
    before = protected.read_bytes()
    real_is_symlink = Path.is_symlink
    def denied(path):
        if path == protected:
            raise PermissionError('fixture simulates root-only directory traversal')
        return real_is_symlink(path)
    monkeypatch.setattr(Path, 'is_symlink', denied)
    with pytest.raises(AgentToolError) as failure:
        service_lifecycle('start', **ctx)
    assert failure.value.code == 'SERVICE_PERMISSION_REQUIRED'
    assert failure.value.details['path'] == str(protected)
    assert '--deploy-user' in failure.value.message
    assert not manager.calls
    assert protected.read_bytes() == before
