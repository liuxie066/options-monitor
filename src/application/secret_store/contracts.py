from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Protocol


class SecretError(RuntimeError):
    """Safe-to-display secret subsystem error.

    Error messages must describe the failed boundary without including command
    output, credential contents, or other secret-derived material.
    """


class SecretBackendUnavailable(SecretError):
    pass


class SecretProvisioningError(SecretError):
    pass


@dataclass(frozen=True)
class SecretStatus:
    logical_name: str
    configured: bool
    backend: str
    source: str

    def public_payload(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "configured": bool(self.configured),
            "backend": self.backend,
            "source": self.source,
        }


class SecretProvider(Protocol):
    @property
    def backend_name(self) -> str: ...

    def get(self, logical_name: str, *, legacy_env_name: str | None = None) -> str | None: ...

    def status(self, logical_name: str, *, legacy_env_name: str | None = None) -> SecretStatus: ...


class SecretProvisioner(Protocol):
    @property
    def backend_name(self) -> str: ...

    def status(self, logical_name: str) -> SecretStatus: ...

    def set(self, logical_name: str, value: str, *, replace: bool) -> None: ...

    def delete(self, logical_name: str) -> bool: ...


class SnapshotSecretProvider:
    """Cache only requested credentials for the lifetime of this provider.

    The wrapper deliberately avoids preloading the registry. Long-running
    services therefore receive a startup snapshot of only the credentials they
    actually request, and rotation takes effect after an explicit restart.
    """

    def __init__(self, provider: SecretProvider):
        self._provider = provider
        self._values: dict[str, str | None] = {}
        self._statuses: dict[str, SecretStatus] = {}
        self._lock = RLock()

    @property
    def backend_name(self) -> str:
        return self._provider.backend_name

    def get(self, logical_name: str, *, legacy_env_name: str | None = None) -> str | None:
        key = logical_name
        with self._lock:
            if key not in self._values:
                self._values[key] = self._provider.get(logical_name, legacy_env_name=legacy_env_name)
            return self._values[key]

    def status(self, logical_name: str, *, legacy_env_name: str | None = None) -> SecretStatus:
        key = logical_name
        with self._lock:
            if key in self._values:
                return SecretStatus(
                    logical_name=logical_name,
                    configured=bool(self._values[key]),
                    backend=self.backend_name,
                    source=self._provider.status(logical_name, legacy_env_name=legacy_env_name).source,
                )
            if key not in self._statuses:
                self._statuses[key] = self._provider.status(logical_name, legacy_env_name=legacy_env_name)
            return self._statuses[key]


__all__ = [
    "SecretBackendUnavailable",
    "SecretError",
    "SecretProvider",
    "SecretProvisioner",
    "SecretProvisioningError",
    "SecretStatus",
    "SnapshotSecretProvider",
]
