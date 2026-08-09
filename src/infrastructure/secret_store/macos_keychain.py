from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from src.application.secret_store.contracts import (
    SecretBackendUnavailable,
    SecretProvisioningError,
    SecretStatus,
)
from src.application.secret_store.registry import require_credential_spec


KEYCHAIN_SERVICE = "options-monitor"
SECURITY_COMMAND = "/usr/bin/security"
MAX_KEYCHAIN_SECRET_BYTES = 64 * 1024


class MacOSKeychain:
    backend_name = "keychain"

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        run_command: Callable[..., Any] = subprocess.run,
    ):
        self._service = str(service)
        self._run_command = run_command

    def _args(self, action: str, logical_name: str) -> list[str]:
        require_credential_spec(logical_name)
        return [SECURITY_COMMAND, action, "-a", logical_name, "-s", self._service]

    def get(self, logical_name: str, *, legacy_env_name: str | None = None) -> str | None:
        del legacy_env_name
        try:
            result = self._run_command(
                [*self._args("find-generic-password", logical_name), "-w"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SecretBackendUnavailable("macOS Keychain is unavailable") from exc
        if int(result.returncode) != 0:
            return None
        value = str(result.stdout or "").strip()
        if len(value.encode("utf-8")) > MAX_KEYCHAIN_SECRET_BYTES:
            raise SecretBackendUnavailable("macOS Keychain credential is unexpectedly large")
        if "\x00" in value:
            raise SecretBackendUnavailable("macOS Keychain credential contains unsupported data")
        return value or None

    def status(self, logical_name: str, *, legacy_env_name: str | None = None) -> SecretStatus:
        del legacy_env_name
        try:
            result = self._run_command(
                self._args("find-generic-password", logical_name),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SecretBackendUnavailable("macOS Keychain is unavailable") from exc
        return SecretStatus(
            logical_name=logical_name,
            configured=int(result.returncode) == 0,
            backend=self.backend_name,
            source="macos_keychain",
        )

    def set(self, logical_name: str, value: str, *, replace: bool) -> None:
        normalized = _validate_secret_value(value)
        if not replace and self.status(logical_name).configured:
            raise SecretProvisioningError(f"credential already exists: {logical_name}; use rotate")
        args = [*self._args("add-generic-password", logical_name), "-U", "-w"]
        try:
            result = self._run_command(
                args,
                input=normalized + "\n",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SecretProvisioningError("macOS Keychain update failed") from exc
        if int(result.returncode) != 0:
            raise SecretProvisioningError(f"macOS Keychain update failed for {logical_name}")

    def delete(self, logical_name: str) -> bool:
        try:
            result = self._run_command(
                self._args("delete-generic-password", logical_name),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SecretProvisioningError("macOS Keychain delete failed") from exc
        if int(result.returncode) == 0:
            return True
        if not self.status(logical_name).configured:
            return False
        raise SecretProvisioningError(f"macOS Keychain delete failed for {logical_name}")


def _validate_secret_value(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SecretProvisioningError("credential value must not be empty")
    if "\x00" in normalized:
        raise SecretProvisioningError("credential value contains an unsupported NUL byte")
    if len(normalized.encode("utf-8")) > MAX_KEYCHAIN_SECRET_BYTES:
        raise SecretProvisioningError("credential value is too large")
    return normalized


__all__ = [
    "KEYCHAIN_SERVICE",
    "MAX_KEYCHAIN_SECRET_BYTES",
    "MacOSKeychain",
    "SECURITY_COMMAND",
]
