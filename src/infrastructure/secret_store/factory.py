from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Mapping

from src.application.secret_store.contracts import (
    SecretBackendUnavailable,
    SecretProvider,
    SecretProvisioner,
)
from src.infrastructure.secret_store.environment import EnvSecretProvider
from src.infrastructure.secret_store.macos_keychain import MacOSKeychain
from src.infrastructure.secret_store.systemd_credentials import (
    DEFAULT_ENCRYPTED_STORE,
    SystemdCredentialProvider,
    SystemdCredentialProvisioner,
)


SECRET_BACKEND_ENV = "OM_SECRET_BACKEND"
CREDENTIALS_DIRECTORY_ENV = "CREDENTIALS_DIRECTORY"
SUPPORTED_SECRET_BACKENDS = ("auto", "env", "keychain", "systemd")


def _backend_name(backend: str | None, environ: Mapping[str, str]) -> str:
    name = str(backend if backend is not None else environ.get(SECRET_BACKEND_ENV) or "auto").strip().lower()
    if name not in SUPPORTED_SECRET_BACKENDS:
        raise SecretBackendUnavailable(
            f"{SECRET_BACKEND_ENV} must be one of: {', '.join(SUPPORTED_SECRET_BACKENDS)}"
        )
    return name


def _platform_name(value: str | None) -> str:
    return str(value or platform.system()).strip().lower()


def build_secret_provider(
    *,
    backend: str | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    credential_directory: str | Path | None = None,
) -> SecretProvider:
    env = environ if environ is not None else os.environ
    selected = _backend_name(backend, env)
    os_name = _platform_name(platform_name)
    if selected == "env":
        return EnvSecretProvider(env)
    if selected == "auto":
        if os_name == "darwin":
            selected = "keychain"
        elif os_name == "linux" and str(credential_directory or env.get(CREDENTIALS_DIRECTORY_ENV) or "").strip():
            selected = "systemd"
        elif os_name == "linux":
            raise SecretBackendUnavailable(
                "no secure Linux runtime credential context is available; configure an explicit per-unit credential delivery mode "
                "or explicitly select OM_SECRET_BACKEND=env for temporary compatibility"
            )
        else:
            raise SecretBackendUnavailable(f"no secure automatic secret backend is available for platform: {os_name}")
    if selected == "keychain":
        if os_name != "darwin":
            raise SecretBackendUnavailable("macOS Keychain backend is available only on macOS")
        return MacOSKeychain()
    if selected == "systemd":
        if os_name != "linux":
            raise SecretBackendUnavailable("systemd credential backend is available only on Linux")
        directory = credential_directory or env.get(CREDENTIALS_DIRECTORY_ENV)
        return SystemdCredentialProvider(str(directory or ""))
    raise AssertionError(f"unhandled secret backend: {selected}")


def build_secret_provisioner(
    *,
    backend: str | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    store_root: str | Path = DEFAULT_ENCRYPTED_STORE,
) -> SecretProvisioner:
    env = environ if environ is not None else os.environ
    selected = _backend_name(backend, env)
    os_name = _platform_name(platform_name)
    if selected == "auto":
        if os_name == "darwin":
            selected = "keychain"
        elif os_name == "linux":
            selected = "systemd"
        else:
            raise SecretBackendUnavailable(f"no secure provisioning backend is available for platform: {os_name}")
    if selected == "env":
        raise SecretBackendUnavailable(
            "the env compatibility backend is read-only; provision environment variables outside options-monitor"
        )
    if selected == "keychain":
        if os_name != "darwin":
            raise SecretBackendUnavailable("macOS Keychain provisioning is available only on macOS")
        return MacOSKeychain()
    if selected == "systemd":
        if os_name != "linux":
            raise SecretBackendUnavailable("systemd credential provisioning is available only on Linux")
        return SystemdCredentialProvisioner(store_root=store_root)
    raise AssertionError(f"unhandled secret backend: {selected}")


__all__ = [
    "CREDENTIALS_DIRECTORY_ENV",
    "SECRET_BACKEND_ENV",
    "SUPPORTED_SECRET_BACKENDS",
    "build_secret_provider",
    "build_secret_provisioner",
]
