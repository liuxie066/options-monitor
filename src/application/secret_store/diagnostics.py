from __future__ import annotations

import os
from collections.abc import Mapping

from src.application.secret_store.contracts import SecretError, SecretProvider
from src.application.secret_store.registry import CredentialSpec, credential_specs


def inspect_secret_credentials(
    *,
    environ: Mapping[str, str] | None = None,
    provider: SecretProvider | None = None,
    specs: tuple[CredentialSpec, ...] | None = None,
) -> dict[str, object]:
    selected_specs = specs or credential_specs()
    try:
        if provider is None:
            from src.infrastructure.secret_store.factory import (
                SECRET_BACKEND_ENV,
                build_secret_provider,
            )

            # Callers may pass an effective/configured environment that intentionally
            # omits process-only controls.  Keep the backend selector process-wide so
            # an explicit compatibility choice still applies without copying it into
            # every diagnostic mapping.  This never enables env fallback implicitly:
            # OM_SECRET_BACKEND=env must still be set explicitly somewhere.
            selected_backend = None
            if environ is not None:
                selected_backend = str(
                    environ.get(SECRET_BACKEND_ENV)
                    or os.environ.get(SECRET_BACKEND_ENV)
                    or ""
                ).strip() or None
            provider = build_secret_provider(
                backend=selected_backend,
                environ=environ,
            )
        items = [provider.status(spec.logical_name).public_payload() for spec in selected_specs]
    except (SecretError, ValueError) as exc:
        return {
            "summary": {
                "ok": False,
                "backend": None,
                "credential_count": len(selected_specs),
                "configured_count": 0,
                "values_exposed": False,
                "error": str(exc),
            },
            "credentials": [],
        }
    return {
        "summary": {
            "ok": True,
            "backend": provider.backend_name,
            "credential_count": len(items),
            "configured_count": sum(1 for item in items if item["configured"]),
            "values_exposed": False,
            "warnings": (
                ["OM_SECRET_BACKEND=env is a deprecated compatibility backend"]
                if provider.backend_name == "env"
                else []
            ),
        },
        "credentials": items,
    }


__all__ = ["inspect_secret_credentials"]
