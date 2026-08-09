from __future__ import annotations

import errno
import os
import pty
import select
import signal
import subprocess
import time
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
KEYCHAIN_PASSWORD_PROMPT = b"password data for new item:"
KEYCHAIN_PASSWORD_CONFIRM_PROMPT = b"retype password for new item:"
KEYCHAIN_PROMPT_TIMEOUT_SECONDS = 15.0
MAX_KEYCHAIN_PROMPT_BYTES = 4 * 1024


def _write_all(fd: int, data: bytearray) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("failed to write credential to macOS Keychain prompt")
        offset += written


def _run_security_password_prompt(
    args: list[str],
    value: str,
    *,
    timeout_seconds: float = KEYCHAIN_PROMPT_TIMEOUT_SECONDS,
) -> int:
    """Run ``security ... -w`` behind a PTY without putting the value in argv.

    ``security add-generic-password -w`` reads from a controlling terminal. A
    normal subprocess pipe is accepted with exit code 0 but stores an empty
    password, so provisioning must wait for the real prompt before writing.
    Child output is intentionally discarded and never enters an exception.
    """

    secret_input = bytearray(value.encode("utf-8"))
    secret_input.append(ord("\n"))
    pid = -1
    master_fd = -1
    child_status: int | None = None
    expected_prompts = (KEYCHAIN_PASSWORD_PROMPT, KEYCHAIN_PASSWORD_CONFIRM_PROMPT)
    sent_count = 0
    prompt_window = bytearray()
    try:
        pid, master_fd = pty.fork()
        if pid == 0:
            child_env = {
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            }
            try:
                os.execve(args[0], args, child_env)
            except BaseException:
                os._exit(127)

        deadline = time.monotonic() + float(timeout_seconds)
        while child_status is None:
            waited_pid, wait_status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                child_status = wait_status
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SecretProvisioningError("macOS Keychain password prompt timed out")
            readable, _, _ = select.select([master_fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(master_fd, 1024)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                _, child_status = os.waitpid(pid, 0)
                break
            if not chunk:
                _, child_status = os.waitpid(pid, 0)
                break
            if sent_count < len(expected_prompts):
                prompt_window.extend(chunk.lower())
                if len(prompt_window) > MAX_KEYCHAIN_PROMPT_BYTES:
                    del prompt_window[:-MAX_KEYCHAIN_PROMPT_BYTES]
                if expected_prompts[sent_count] in prompt_window:
                    _write_all(master_fd, secret_input)
                    sent_count += 1
                    prompt_window.clear()

        return_code = os.waitstatus_to_exitcode(child_status)
        if sent_count != len(expected_prompts) or return_code != 0:
            raise SecretProvisioningError("macOS Keychain update failed")
        return return_code
    finally:
        for index in range(len(secret_input)):
            secret_input[index] = 0
        prompt_window.clear()
        if master_fd >= 0:
            os.close(master_fd)
        if pid > 0 and child_status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


class MacOSKeychain:
    backend_name = "keychain"

    def __init__(
        self,
        *,
        service: str = KEYCHAIN_SERVICE,
        run_command: Callable[..., Any] = subprocess.run,
        run_secret_command: Callable[[list[str], str], int] = _run_security_password_prompt,
    ):
        self._service = str(service)
        self._run_command = run_command
        self._run_secret_command = run_secret_command

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
            return_code = self._run_secret_command(args, normalized)
        except SecretProvisioningError:
            raise
        except OSError as exc:
            raise SecretProvisioningError("macOS Keychain update failed") from exc
        if int(return_code) != 0:
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
    if "\r" in normalized or "\n" in normalized:
        raise SecretProvisioningError("credential value contains an unsupported line break")
    if len(normalized.encode("utf-8")) > MAX_KEYCHAIN_SECRET_BYTES:
        raise SecretProvisioningError("credential value is too large")
    return normalized


__all__ = [
    "KEYCHAIN_SERVICE",
    "KEYCHAIN_PASSWORD_CONFIRM_PROMPT",
    "KEYCHAIN_PASSWORD_PROMPT",
    "KEYCHAIN_PROMPT_TIMEOUT_SECONDS",
    "MAX_KEYCHAIN_SECRET_BYTES",
    "MacOSKeychain",
    "SECURITY_COMMAND",
]
