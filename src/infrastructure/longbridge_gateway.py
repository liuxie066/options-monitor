"""Read-only Longbridge SDK boundary; importing this module never loads the SDK."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from src.application.settings import build_effective_env


ACCOUNT_IDENTITY_ERROR = "longbridge_account_identity_unverified"


def _credentials(settings: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    prefix = str(settings.get("credentials_env_prefix") or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prefix):
        raise ValueError("longbridge credentials_env_prefix must be an environment name prefix")
    credentials = {}
    for field in ("app_key", "app_secret", "access_token"):
        name = f"{prefix}_{field.upper()}"
        value = environ.get(name)
        if not value or not value.strip():
            raise ValueError(f"longbridge credential environment variable missing: {name}")
        credentials[field] = value
    return credentials


def _fingerprint(settings: Mapping[str, Any], credentials: Mapping[str, str]) -> str:
    if not isinstance(settings.get("account_id"), str) or not settings["account_id"].strip():
        raise ValueError("longbridge account_id must be a nonempty string")
    payload = {"schema_version": "longbridge.credential_account_binding.v1", **credentials,
               **{key: settings[key] for key in ("account_id", "region", "trd_env")}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def credential_binding_fingerprint(
    settings: Mapping[str, Any], *, environ: Mapping[str, str] | None = None,
) -> str:
    """Offline preview only; never prints credentials, imports SDK or contacts API."""
    if settings.get("region") != "ap" or settings.get("trd_env") != "REAL":
        raise ValueError("longbridge binding preview currently requires ap/REAL")
    env = build_effective_env().values if environ is None else environ
    return _fingerprint(settings, _credentials(settings, env))


class LongbridgeGateway:
    """Lazy compatibility facade; offline core imports never load the package."""

    def __new__(cls, settings: Mapping[str, Any], *, client: Any = None,
                context_factory: Callable[..., Any] | None = None,
                environ: Mapping[str, str] | None = None) -> Any:
        from src.application.plugin_runtime import require_plugin

        return require_plugin("broker-longbridge").LongbridgeGateway(
            settings, client=client, context_factory=context_factory, environ=environ,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview a user-confirmed Longbridge credential/account binding offline")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--credentials-env-prefix", required=True)
    parser.add_argument("--region", choices=["ap"], default="ap")
    parser.add_argument("--trd-env", choices=["REAL"], default="REAL")
    args = parser.parse_args()
    try:
        fingerprint = credential_binding_fingerprint(vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"credential_binding_sha256": fingerprint, "account_id": args.account_id,
        "region": args.region, "trd_env": args.trd_env, "broker_echoed": False,
        "binding_method": "user_confirmed_credentials_sha256"}))


if __name__ == "__main__":
    main()
