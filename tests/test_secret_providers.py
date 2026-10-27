from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from src.application.secret_store import (
    FEISHU_BOT_APP_SECRET,
    LLM_DEEPSEEK_API_KEY,
    SecretBackendUnavailable,
    SecretProvisioningError,
    SnapshotSecretProvider,
    credential_spec,
    credential_specs,
    legacy_secret_env_names,
)
from src.infrastructure.secret_store.environment import EnvSecretProvider
from src.infrastructure.secret_store.factory import build_secret_provider
from src.infrastructure.secret_store.macos_keychain import MacOSKeychain
from src.infrastructure.secret_store.macos_keychain import _run_security_password_prompt
from src.infrastructure.secret_store.memory import InMemorySecretProvider
from src.infrastructure.secret_store.systemd_credentials import (
    SystemdCredentialProvider,
    SystemdCredentialProvisioner,
)


def test_registry_has_unique_fixed_names_and_credential_ids() -> None:
    specs = credential_specs()
    assert len(specs) == 7
    assert len({item.logical_name for item in specs}) == len(specs)
    assert len({item.systemd_credential_id for item in specs}) == len(specs)
    assert all(item.systemd_credential_id.startswith("om-") for item in specs)
    assert credential_spec("quality.read_token") is None
    assert "OM_QUALITY_READ_TOKEN" in legacy_secret_env_names()
    assert credential_spec("bot.cursor_hmac_key") is None
    assert "OM_COPILOT_CURSOR_HMAC_KEY" in legacy_secret_env_names()

    import src.application.secret_store as secret_store

    assert not hasattr(secret_store, "BOT_CURSOR_HMAC_KEY")
    assert not hasattr(secret_store, "QUALITY_READ_TOKEN")


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

    def fake_run(args, value):
        observed["args"] = list(args)
        observed["value"] = value
        return 0

    keychain = MacOSKeychain(run_secret_command=fake_run)
    keychain.set(FEISHU_BOT_APP_SECRET, "test-secret-value", replace=True)

    assert "test-secret-value" not in observed["args"]
    assert observed["args"][-1] == "-w"
    assert observed["value"] == "test-secret-value"


def test_keychain_write_answers_both_native_password_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-reach-child")
    prompt_helper = tmp_path / "keychain-prompt-helper"
    prompt_helper.write_text(
        """#!/usr/bin/env python3
import getpass
import os

first = getpass.getpass("password data for new item: ")
second = getpass.getpass("retype password for new item: ")
secret_env_absent = "DEEPSEEK_API_KEY" not in os.environ
raise SystemExit(0 if first == second == "test-secret-value" and secret_env_absent else 9)
""",
        encoding="utf-8",
    )
    prompt_helper.chmod(0o700)

    assert (
        _run_security_password_prompt(
            [str(prompt_helper)],
            "test-secret-value",
            timeout_seconds=3,
        )
        == 0
    )


def test_keychain_write_rejects_multiline_values() -> None:
    keychain = MacOSKeychain(run_secret_command=lambda _args, _value: 0)
    with pytest.raises(SecretProvisioningError, match="line break"):
        keychain.set(FEISHU_BOT_APP_SECRET, "first\nsecond", replace=True)


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


@pytest.mark.parametrize("reader_kind", ["provider", "provisioner"])
@pytest.mark.parametrize("error_number", [None, errno.ENOENT, errno.EACCES, errno.EPERM, errno.EIO, errno.ENOTDIR, errno.ELOOP])
def test_systemd_status_distinguishes_missing_from_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader_kind: str, error_number: int | None,
) -> None:
    target = tmp_path / "om-llm-deepseek-api-key"
    original_lstat = Path.lstat

    def fail_lstat(path, *args, **kwargs):
        if path == target:
            if error_number is None:
                raise FileNotFoundError("synthetic-secret-and-private-path")
            raise OSError(error_number, "synthetic-secret-and-private-path")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    reader = (SystemdCredentialProvider(tmp_path) if reader_kind == "provider"
              else SystemdCredentialProvisioner(store_root=tmp_path))
    if error_number in {None, errno.ENOENT}:
        status = reader.status(LLM_DEEPSEEK_API_KEY)
        assert status.configured is False
        assert status.source == ("systemd_credential" if reader_kind == "provider" else "systemd_encrypted_store")
    else:
        with pytest.raises(SecretBackendUnavailable) as caught:
            reader.status(LLM_DEEPSEEK_API_KEY)
        assert LLM_DEEPSEEK_API_KEY in str(caught.value)
        assert "synthetic-secret-and-private-path" not in str(caught.value)
        assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("reader_kind", ["provider", "provisioner"])
@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "symlink", "valid", "limit", "oversize"])
def test_systemd_status_preserves_metadata_policy_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader_kind: str, kind: str,
) -> None:
    from src.infrastructure.secret_store.systemd_credentials import MAX_SECRET_BYTES

    target = tmp_path / "om-llm-deepseek-api-key"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(tmp_path / "absent-outside")
    elif kind != "missing":
        size = {"empty": 0, "valid": 1, "limit": MAX_SECRET_BYTES, "oversize": MAX_SECRET_BYTES + 1}[kind]
        target.write_bytes(b"x" * size)

    def forbidden(*args, **kwargs):
        pytest.fail("status must not read/decrypt a credential")

    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == target:
            forbidden()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    reader = (SystemdCredentialProvider(tmp_path) if reader_kind == "provider"
              else SystemdCredentialProvisioner(store_root=tmp_path, run_command=forbidden))
    if reader_kind == "provider":
        monkeypatch.setattr(reader, "get", forbidden)
    status = reader.status(LLM_DEEPSEEK_API_KEY)
    expected = kind in {"valid", "limit"} or (kind == "oversize" and reader_kind == "provisioner")
    assert status.configured is expected
    assert status.backend == "systemd"


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.EACCES, errno.EIO])
def test_systemd_status_direct_diagnostics_preserves_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int,
) -> None:
    from src.application.secret_store.diagnostics import inspect_secret_credentials

    target = tmp_path / "om-llm-deepseek-api-key"
    original_lstat = Path.lstat

    def fail_lstat(path, *args, **kwargs):
        if path == target:
            raise OSError(error_number, "synthetic-private-credential")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    spec = credential_spec(LLM_DEEPSEEK_API_KEY)
    assert spec is not None
    result = inspect_secret_credentials(provider=SystemdCredentialProvider(tmp_path), specs=(spec,), environ={})
    assert result["summary"]["ok"] is (error_number == errno.ENOENT)
    assert result["summary"]["values_exposed"] is False
    assert result["summary"]["configured_count"] == 0
    if error_number != errno.ENOENT:
        assert result["credentials"] == []
    assert "synthetic-private-credential" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
