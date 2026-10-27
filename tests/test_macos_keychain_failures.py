from __future__ import annotations

import subprocess

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.secret_store import LLM_DEEPSEEK_API_KEY, SecretBackendUnavailable, SecretProvisioningError
from src.infrastructure.secret_store.macos_keychain import MacOSKeychain
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.secret_ops import run_store_command


@pytest.mark.parametrize('method', ['get', 'status', 'delete'])
@pytest.mark.parametrize('returncode', [0, 44])
def test_keychain_success_and_native_item_not_found(method: str, returncode: int) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout='fixture-value\n', stderr='')

    result = getattr(MacOSKeychain(run_command=run), method)(LLM_DEEPSEEK_API_KEY)
    if method == 'get':
        assert result == ('fixture-value' if returncode == 0 else None)
    elif method == 'status':
        assert result.configured is (returncode == 0)
        assert result.source == 'macos_keychain'
    else:
        assert result is (returncode == 0)
    assert len(calls) == 1
    assert calls[0][1]['stderr'] == subprocess.DEVNULL
    if method == 'status':
        assert calls[0][1]['stdout'] == subprocess.DEVNULL
        assert '-w' not in calls[0][0]


@pytest.mark.parametrize('method', ['get', 'status', 'delete'])
@pytest.mark.parametrize('returncode', [1, 2, 36, 50, 51, -9, -15, 300])
def test_keychain_other_native_failures_are_not_absence(method: str, returncode: int) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout='private-value', stderr='private-diagnostic')

    kind = SecretProvisioningError if method == 'delete' else SecretBackendUnavailable
    with pytest.raises(kind) as caught:
        getattr(MacOSKeychain(run_command=run), method)(LLM_DEEPSEEK_API_KEY)
    assert 'private-value' not in str(caught.value)
    assert 'private-diagnostic' not in str(caught.value)
    assert len(calls) == 1  # Never reinterpret a failed delete with another query.


@pytest.mark.parametrize('method', ['get', 'status', 'delete'])
def test_keychain_launch_failure_remains_redacted(method: str) -> None:
    def run(*args, **kwargs):
        raise OSError('private-native-detail')

    kind = SecretProvisioningError if method == 'delete' else SecretBackendUnavailable
    with pytest.raises(kind) as caught:
        getattr(MacOSKeychain(run_command=run), method)(LLM_DEEPSEEK_API_KEY)
    assert 'private-native-detail' not in str(caught.value)


@pytest.mark.parametrize('action', ['status', 'delete'])
@pytest.mark.parametrize('returncode', [0, 44, 36, 51])
def test_keychain_public_cli_keeps_backend_failure_distinct(action: str, returncode: int) -> None:
    keychain = MacOSKeychain(run_command=lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, returncode, stdout='private-value', stderr='private-diagnostic',
    ))
    argv = ['secrets', action, LLM_DEEPSEEK_API_KEY, '--backend', 'keychain']
    if action == 'delete':
        argv.append('--confirm')
    args = parse_args(argv)
    factories = dict(provider_factory=lambda **kwargs: keychain, provisioner_factory=lambda **kwargs: keychain)
    if returncode not in (0, 44):
        with pytest.raises(AgentToolError) as caught:
            run_store_command(args, **factories)
        assert caught.value.code == 'CONFIG_ERROR'
        assert 'private-value' not in str(caught.value)
        assert 'private-diagnostic' not in str(caught.value)
    else:
        payload = run_store_command(args, **factories)
        if action == 'status':
            assert payload['summary']['configured_count'] == (1 if returncode == 0 else 0)
        else:
            assert payload['changed'] is (returncode == 0)
        assert 'private-value' not in repr(payload)
