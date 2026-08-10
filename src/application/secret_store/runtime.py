from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from threading import RLock
from typing import Iterator, Mapping

from src.application.secret_store.contracts import SecretProvider, SecretStatus, SnapshotSecretProvider


_provider_override: ContextVar[SecretProvider | None] = ContextVar("options_monitor_secret_provider", default=None)
_default_provider: SecretProvider | None = None
_default_lock = RLock()


def default_secret_provider() -> SecretProvider:
    override = _provider_override.get()
    if override is not None:
        return override
    global _default_provider
    with _default_lock:
        if _default_provider is None:
            from src.infrastructure.secret_store.factory import build_secret_provider

            _default_provider = SnapshotSecretProvider(build_secret_provider())
        return _default_provider


def reset_default_secret_provider() -> None:
    """Reset process-local provider state; intended for deterministic tests."""

    global _default_provider
    with _default_lock:
        _default_provider = None


@contextmanager
def use_secret_provider(provider: SecretProvider) -> Iterator[SecretProvider]:
    token = _provider_override.set(SnapshotSecretProvider(provider))
    try:
        yield _provider_override.get()  # type: ignore[misc]
    finally:
        _provider_override.reset(token)


def _provider_for(
    *,
    provider: SecretProvider | None,
    environ: Mapping[str, str] | None,
) -> SecretProvider:
    if provider is not None:
        return provider
    override = _provider_override.get()
    if override is not None:
        return override
    if environ is not None:
        from src.infrastructure.secret_store.factory import SECRET_BACKEND_ENV, build_secret_provider

        selected_backend = str(environ.get(SECRET_BACKEND_ENV) or os.environ.get(SECRET_BACKEND_ENV) or "auto")
        return SnapshotSecretProvider(
            build_secret_provider(
                backend=selected_backend,
                environ=environ,
            )
        )
    return default_secret_provider()


def resolve_secret(
    logical_name: str,
    *,
    provider: SecretProvider | None = None,
    environ: Mapping[str, str] | None = None,
    legacy_env_name: str | None = None,
) -> str | None:
    value = _provider_for(provider=provider, environ=environ).get(
        logical_name,
        legacy_env_name=legacy_env_name,
    )
    normalized = str(value or "").strip()
    return normalized or None


def resolve_secret_status(
    logical_name: str,
    *,
    provider: SecretProvider | None = None,
    environ: Mapping[str, str] | None = None,
    legacy_env_name: str | None = None,
) -> SecretStatus:
    return _provider_for(provider=provider, environ=environ).status(
        logical_name,
        legacy_env_name=legacy_env_name,
    )


__all__ = [
    "default_secret_provider",
    "reset_default_secret_provider",
    "resolve_secret",
    "resolve_secret_status",
    "use_secret_provider",
]
