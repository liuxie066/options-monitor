from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.application.secret_store import (
    FEISHU_BOT_APP_SECRET,
    LLM_DEEPSEEK_API_KEY,
    SecretBackendUnavailable,
    SnapshotSecretProvider,
    credential_specs,
)
from src.infrastructure.secret_store.environment import EnvSecretProvider
from src.infrastructure.secret_store.factory import build_secret_provider
from src.infrastructure.secret_store.macos_keychain import MacOSKeychain
from src.infrastructure.secret_store.memory import InMemorySecretProvider
from src.infrastructure.secret_store.systemd_credentials import (
    SystemdCredentialProvider,
    SystemdCredentialProvisioner,
)


def test_registry_has_unique_fixed_names_and_credential_ids() -> None:
    specs = credential_specs()
    assert len(specs) == 8
    assert len({item.logical_name for item in specs}) == len(specs)
    assert len({item.systemd_credential_id for item in specs}) == len(specs)
    assert all(item.systemd_credential_id.startswith("om-") for item in specs)


def test_env_provider_is_explicit_and_deprecated() -> None:
    provider = EnvSecretProvider({"DEEPSEEK_API_KEY": "test-value"})
    assert provider.get(LLM_DEEPSEEK_API_KEY) == "test-value"
    assert provider.status(LLM_DEEPSEEK_API_KEY).source == "environment_compatibility"


def test_auto_backend_fails_closed_on_linux_without_systemd_context() -> None:
    with pytest.raises(SecretBackendUnavailable, match="no secure Linux runtime credential context"):
        build_secret_provider(environ={}, platform_name="linux")


def test_auto_backend_selects_keychain_on_macos() -> None:
    provider = build_secret_provider(environ={}, platform_name="darwin")
    assert isinstance(provider, MacOSKeychain)


def test_systemd_provider_reads_only_fixed_regular_credential(tmp_path: Path) -> None:
    provider = SystemdCredentialProvider(tmp_path)
    target = tmp_path / "om-llm-deepseek-api-key"
    target.write_text("test-value\n", encoding="utf-8")

    assert provider.get(LLM_DEEPSEEK_API_KEY) == "test-value"
    assert provider.status(LLM_DEEPSEEK_API_KEY).configured is True

    target.unlink()
    target.symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_text("must-not-read", encoding="utf-8")
    assert provider.get(LLM_DEEPSEEK_API_KEY) is None


def test_snapshot_provider_does_not_hot_reload() -> None:
    memory = InMemorySecretProvider({LLM_DEEPSEEK_API_KEY: "first"})
    provider = SnapshotSecretProvider(memory)

    assert provider.get(LLM_DEEPSEEK_API_KEY) == "first"
    memory.set(LLM_DEEPSEEK_API_KEY, "second", replace=True)
    assert provider.get(LLM_DEEPSEEK_API_KEY) == "first"


def test_snapshot_provider_identity_ignores_legacy_alias() -> None:
    memory = InMemorySecretProvider({LLM_DEEPSEEK_API_KEY: "first"})
    provider = SnapshotSecretProvider(memory)

    assert (
        provider.get(LLM_DEEPSEEK_API_KEY, legacy_env_name="ALIAS_A")
        == "first"
    )
    memory.set(LLM_DEEPSEEK_API_KEY, "second", replace=True)
    assert (
        provider.get(LLM_DEEPSEEK_API_KEY, legacy_env_name="ALIAS_B")
        == "first"
    )


def test_keychain_write_never_places_secret_in_argv() -> None:
    observed: dict[str, object] = {}

    def fake_run(args, **kwargs):
        observed["args"] = list(args)
        observed["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    keychain = MacOSKeychain(run_command=fake_run)
    keychain.set(FEISHU_BOT_APP_SECRET, "test-secret-value", replace=True)

    assert "test-secret-value" not in observed["args"]
    assert observed["args"][-1] == "-w"
    assert observed["input"] == "test-secret-value\n"


def test_systemd_provisioner_stages_only_encrypted_output(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def fake_run(args, **kwargs):
        observed["args"] = list(args)
        observed["input"] = kwargs.get("input")
        Path(args[-1]).write_text("encrypted-fixture", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    provisioner = SystemdCredentialProvisioner(
        store_root=tmp_path,
        run_command=fake_run,
        geteuid=lambda: 0,
    )
    provisioner.set(LLM_DEEPSEEK_API_KEY, "test-secret-value", replace=True)

    target = tmp_path / "om-llm-deepseek-api-key"
    assert target.read_text(encoding="utf-8") == "encrypted-fixture"
    assert "test-secret-value" not in json.dumps(observed["args"])
    assert observed["input"] == "test-secret-value\n"


def test_systemd_provisioner_requires_separate_root_authority(tmp_path: Path) -> None:
    provisioner = SystemdCredentialProvisioner(store_root=tmp_path, geteuid=lambda: 501)
    with pytest.raises(Exception, match="root-authorized"):
        provisioner.set(LLM_DEEPSEEK_API_KEY, "test-value", replace=False)
