from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.secret_store import (
    SecretError,
    SecretProvider,
    SecretProvisioner,
    credential_specs,
    require_credential_spec,
)
from src.infrastructure.secret_store.factory import (
    SUPPORTED_SECRET_BACKENDS,
    build_secret_provider,
    build_secret_provisioner,
)
from src.infrastructure.secret_store.systemd_credentials import DEFAULT_ENCRYPTED_STORE


def _stdin_is_tty() -> bool:
    return bool(sys.stdin.isatty())


def add_secret_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser("secrets", help="inspect and provision logical credentials without printing values")
    commands = parser.add_subparsers(dest="store_action", required=True)

    status = commands.add_parser("status", help="show redacted credential readiness")
    status.add_argument("logical_names", nargs="*")
    status.add_argument("--backend", choices=SUPPORTED_SECRET_BACKENDS, default=None)
    status.add_argument("--store-root", default=str(DEFAULT_ENCRYPTED_STORE))

    for name in ("set", "rotate"):
        command = commands.add_parser(name, help=f"{name} one credential using a hidden terminal prompt")
        command.add_argument("logical_name")
        command.add_argument("--backend", choices=("auto", "keychain", "systemd"), default=None)
        command.add_argument("--store-root", default=str(DEFAULT_ENCRYPTED_STORE))

    delete = commands.add_parser("delete", help="delete one credential without displaying it")
    delete.add_argument("logical_name")
    delete.add_argument("--backend", choices=("auto", "keychain", "systemd"), default=None)
    delete.add_argument("--store-root", default=str(DEFAULT_ENCRYPTED_STORE))
    delete.add_argument("--confirm", action="store_true")


def run_store_command(
    args: argparse.Namespace,
    *,
    provider_factory: Callable[..., SecretProvider] = build_secret_provider,
    provisioner_factory: Callable[..., SecretProvisioner] = build_secret_provisioner,
    prompt_fn: Callable[[str], str] = getpass.getpass,
    input_is_tty: Callable[[], bool] = _stdin_is_tty,
) -> dict[str, Any]:
    try:
        if args.store_action == "status":
            return _status_payload(
                args,
                provider_factory=provider_factory,
                provisioner_factory=provisioner_factory,
            )

        spec = _require_cli_spec(args.logical_name)
        provisioner = provisioner_factory(
            backend=args.backend,
            store_root=Path(args.store_root),
        )
        if args.store_action in {"set", "rotate"}:
            if not input_is_tty():
                raise AgentToolError(
                    code="INPUT_ERROR",
                    message="credential input requires an interactive terminal",
                )
            value = prompt_fn(f"Enter {spec.logical_name}: ")
            confirmation = prompt_fn(f"Confirm {spec.logical_name}: ")
            if value != confirmation:
                raise AgentToolError(code="INPUT_ERROR", message="credential confirmation does not match")
            provisioner.set(spec.logical_name, value, replace=args.store_action == "rotate")
            return _mutation_payload(
                action=args.store_action,
                logical_name=spec.logical_name,
                backend=provisioner.backend_name,
                changed=True,
            )

        if args.store_action == "delete":
            if not bool(args.confirm):
                raise AgentToolError(
                    code="CONFIRMATION_REQUIRED",
                    message="credential deletion requires --confirm",
                )
            changed = provisioner.delete(spec.logical_name)
            return _mutation_payload(
                action="delete",
                logical_name=spec.logical_name,
                backend=provisioner.backend_name,
                changed=changed,
            )
    except AgentToolError:
        raise
    except (SecretError, ValueError) as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported secrets command: {args.store_action}")


def _status_payload(
    args: argparse.Namespace,
    *,
    provider_factory: Callable[..., SecretProvider],
    provisioner_factory: Callable[..., SecretProvisioner],
) -> dict[str, Any]:
    specs = tuple(_require_cli_spec(name) for name in args.logical_names) if args.logical_names else credential_specs()
    explicit_backend = str(args.backend or "").strip().lower()
    if explicit_backend in {"keychain", "systemd"}:
        reader: SecretProvider | SecretProvisioner = provisioner_factory(
            backend=explicit_backend,
            store_root=Path(args.store_root),
        )
    else:
        reader = provider_factory(backend=args.backend)
    items = [reader.status(spec.logical_name).public_payload() for spec in specs]
    return {
        "summary": {
            "backend": reader.backend_name,
            "credential_count": len(items),
            "configured_count": sum(1 for item in items if item["configured"]),
            "values_exposed": False,
            "warnings": (
                ["OM_SECRET_BACKEND=env is a deprecated compatibility backend"]
                if reader.backend_name == "env"
                else []
            ),
        },
        "credentials": items,
    }


def _mutation_payload(*, action: str, logical_name: str, backend: str, changed: bool) -> dict[str, Any]:
    spec = require_credential_spec(logical_name)
    return {
        "action": action,
        "logical_name": logical_name,
        "backend": backend,
        "changed": bool(changed),
        "value_exposed": False,
        "restart_performed": False,
        "restart_required": bool(changed),
        "affected_services": list(spec.affected_services),
        "next_step": "restart and verify only the affected services through the separately authorized service workflow",
    }


def _require_cli_spec(logical_name: str):
    try:
        return require_credential_spec(logical_name)
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=str(exc),
            details={"available_logical_names": [spec.logical_name for spec in credential_specs()]},
        ) from exc


__all__ = ["add_secret_commands", "run_store_command"]
