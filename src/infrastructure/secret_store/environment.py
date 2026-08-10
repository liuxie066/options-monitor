from __future__ import annotations

from typing import Mapping

from src.application.secret_store.contracts import SecretStatus
from src.application.secret_store.registry import require_credential_spec


class EnvSecretProvider:
    """Explicit compatibility backend for migration, CI, and legacy tests."""

    backend_name = "env"

    def __init__(self, environ: Mapping[str, str]):
        self._environ = environ

    def _env_name(self, logical_name: str, legacy_env_name: str | None) -> str:
        spec = require_credential_spec(logical_name)
        override = str(legacy_env_name or "").strip()
        return override or spec.legacy_env_names[0]

    def get(self, logical_name: str, *, legacy_env_name: str | None = None) -> str | None:
        env_name = self._env_name(logical_name, legacy_env_name)
        value = str(self._environ.get(env_name) or "").strip()
        return value or None

    def status(self, logical_name: str, *, legacy_env_name: str | None = None) -> SecretStatus:
        configured = bool(self.get(logical_name, legacy_env_name=legacy_env_name))
        return SecretStatus(
            logical_name=logical_name,
            configured=configured,
            backend=self.backend_name,
            source="environment_compatibility",
        )


__all__ = ["EnvSecretProvider"]
