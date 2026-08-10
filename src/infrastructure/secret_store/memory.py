from __future__ import annotations

from collections.abc import Mapping

from src.application.secret_store.contracts import SecretStatus
from src.application.secret_store.registry import require_credential_spec


class InMemorySecretProvider:
    """Non-persistent provider/provisioner for tests."""

    backend_name = "memory"

    def __init__(self, values: Mapping[str, str] | None = None):
        self._values = {str(key): str(value) for key, value in (values or {}).items()}

    def get(self, logical_name: str, *, legacy_env_name: str | None = None) -> str | None:
        del legacy_env_name
        require_credential_spec(logical_name)
        value = str(self._values.get(logical_name) or "").strip()
        return value or None

    def status(self, logical_name: str, *, legacy_env_name: str | None = None) -> SecretStatus:
        del legacy_env_name
        return SecretStatus(
            logical_name=logical_name,
            configured=bool(self.get(logical_name)),
            backend=self.backend_name,
            source="in_memory_test",
        )

    def set(self, logical_name: str, value: str, *, replace: bool = True) -> None:
        require_credential_spec(logical_name)
        if logical_name in self._values and not replace:
            raise ValueError(f"credential already exists: {logical_name}")
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("credential value must not be empty")
        self._values[logical_name] = normalized

    def delete(self, logical_name: str) -> bool:
        require_credential_spec(logical_name)
        return self._values.pop(logical_name, None) is not None


__all__ = ["InMemorySecretProvider"]
