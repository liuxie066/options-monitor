from __future__ import annotations

import errno
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.application.secret_store.contracts import (
    SecretBackendUnavailable,
    SecretProvisioningError,
    SecretStatus,
)
from src.application.secret_store.registry import require_credential_spec


DEFAULT_ENCRYPTED_STORE = Path("/etc/credstore.encrypted")
SYSTEMD_CREDS_COMMAND = "/usr/bin/systemd-creds"
MAX_SECRET_BYTES = 64 * 1024


class SystemdCredentialProvider:
    backend_name = "systemd"

    def __init__(self, credential_directory: str | Path):
        raw = str(credential_directory or "").strip()
        if not raw:
            raise SecretBackendUnavailable(
                "systemd credential backend requires CREDENTIALS_DIRECTORY; run inside a unit with LoadCredentialEncrypted"
            )
        directory = Path(raw)
        if not directory.is_absolute():
            raise SecretBackendUnavailable("CREDENTIALS_DIRECTORY must be an absolute path")
        self._directory = directory

    def _path(self, logical_name: str) -> Path:
        spec = require_credential_spec(logical_name)
        return self._directory / spec.systemd_credential_id

    def get(self, logical_name: str, *, legacy_env_name: str | None = None) -> str | None:
        del legacy_env_name
        path = self._path(logical_name)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                return None
            raise SecretBackendUnavailable(
                f"systemd credential is unreadable: {logical_name}"
            ) from exc
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                return None
            if file_stat.st_size > MAX_SECRET_BYTES:
                raise SecretBackendUnavailable(f"systemd credential is unexpectedly large: {logical_name}")
            with os.fdopen(fd, "rb", closefd=True) as credential_file:
                fd = -1
                raw = credential_file.read(MAX_SECRET_BYTES + 1)
        except SecretBackendUnavailable:
            raise
        except OSError as exc:
            raise SecretBackendUnavailable(f"systemd credential is unreadable: {logical_name}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > MAX_SECRET_BYTES:
            raise SecretBackendUnavailable(f"systemd credential is unexpectedly large: {logical_name}")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise SecretBackendUnavailable(
                f"systemd credential contains unsupported data: {logical_name}"
            ) from exc
        if "\x00" in value:
            raise SecretBackendUnavailable(f"systemd credential contains unsupported data: {logical_name}")
        return value or None

    def status(self, logical_name: str, *, legacy_env_name: str | None = None) -> SecretStatus:
        del legacy_env_name
        path = self._path(logical_name)
        configured = False
        try:
            file_stat = path.lstat()
            configured = stat.S_ISREG(file_stat.st_mode) and 0 < file_stat.st_size <= MAX_SECRET_BYTES
        except OSError:
            configured = False
        return SecretStatus(
            logical_name=logical_name,
            configured=configured,
            backend=self.backend_name,
            source="systemd_credential",
        )


class SystemdCredentialProvisioner:
    backend_name = "systemd"

    def __init__(
        self,
        *,
        store_root: str | Path = DEFAULT_ENCRYPTED_STORE,
        run_command: Callable[..., Any] = subprocess.run,
        geteuid: Callable[[], int] = os.geteuid,
    ):
        root = Path(store_root)
        if not root.is_absolute():
            raise SecretBackendUnavailable("systemd encrypted credential store must be an absolute path")
        self._store_root = root
        self._run_command = run_command
        self._geteuid = geteuid

    def _path(self, logical_name: str) -> Path:
        spec = require_credential_spec(logical_name)
        return self._store_root / spec.systemd_credential_id

    def status(self, logical_name: str) -> SecretStatus:
        path = self._path(logical_name)
        configured = False
        try:
            file_stat = path.lstat()
            configured = stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0
        except OSError:
            configured = False
        return SecretStatus(
            logical_name=logical_name,
            configured=configured,
            backend=self.backend_name,
            source="systemd_encrypted_store",
        )

    def set(self, logical_name: str, value: str, *, replace: bool) -> None:
        self._require_root()
        normalized = _validate_secret_value(value)
        target = self._path(logical_name)
        if target.is_symlink():
            raise SecretProvisioningError(
                f"refusing to replace symlinked credential path: {logical_name}"
            )
        if target.exists() and not replace:
            raise SecretProvisioningError(f"credential already exists: {logical_name}; use rotate")
        try:
            self._store_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._store_root.is_symlink() or not self._store_root.is_dir():
                raise SecretProvisioningError("systemd encrypted credential store must be a real directory")
            os.chmod(self._store_root, 0o700)
            fd, raw_stage = tempfile.mkstemp(prefix=f".{target.name}.", dir=self._store_root)
        except SecretProvisioningError:
            raise
        except OSError as exc:
            raise SecretProvisioningError("systemd encrypted credential store is unavailable") from exc
        os.close(fd)
        stage = Path(raw_stage)
        stage.unlink()
        try:
            try:
                result = self._run_command(
                    [
                        SYSTEMD_CREDS_COMMAND,
                        "encrypt",
                        f"--name={target.name}",
                        "-",
                        str(stage),
                    ],
                    input=normalized + "\n",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                raise SecretProvisioningError(
                    f"systemd credential encryption failed for {logical_name}"
                ) from exc
            if (
                int(result.returncode) != 0
                or stage.is_symlink()
                or not stage.is_file()
            ):
                raise SecretProvisioningError(f"systemd credential encryption failed for {logical_name}")
            os.chmod(stage, 0o600)
            os.replace(stage, target)
        finally:
            try:
                stage.unlink()
            except FileNotFoundError:
                pass

    def delete(self, logical_name: str) -> bool:
        self._require_root()
        target = self._path(logical_name)
        if target.is_symlink():
            raise SecretProvisioningError(f"refusing to delete symlinked credential path: {logical_name}")
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecretProvisioningError(f"systemd credential delete failed for {logical_name}") from exc
        return True

    def _require_root(self) -> None:
        if int(self._geteuid()) != 0:
            raise SecretProvisioningError(
                "systemd credential provisioning requires a separate root-authorized invocation, for example sudo ./om secrets set ..."
            )


def _validate_secret_value(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SecretProvisioningError("credential value must not be empty")
    if "\x00" in normalized:
        raise SecretProvisioningError("credential value contains an unsupported NUL byte")
    if len(normalized.encode("utf-8")) > MAX_SECRET_BYTES:
        raise SecretProvisioningError("credential value is too large")
    return normalized


__all__ = [
    "DEFAULT_ENCRYPTED_STORE",
    "MAX_SECRET_BYTES",
    "SYSTEMD_CREDS_COMMAND",
    "SystemdCredentialProvider",
    "SystemdCredentialProvisioner",
]
