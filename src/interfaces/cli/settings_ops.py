from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.settings import (
    diagnose_effective_settings,
    explain_effective_setting,
    inspect_effective_settings,
)


def add_settings_commands(subparsers: Any) -> None:
    settings = subparsers.add_parser("settings", help="inspect effective environment-backed settings")
    settings_sub = settings.add_subparsers(dest="settings_command", required=True)
    settings_inspect = settings_sub.add_parser("inspect", help="show redacted effective settings and sources")
    settings_inspect.add_argument("--env-file", default=None)
    settings_inspect.add_argument("--no-local-env-file", action="store_true")
    settings_doctor = settings_sub.add_parser("doctor", help="diagnose env-file, Feishu Bot, and write-gate settings")
    settings_doctor.add_argument("--env-file", default=None)
    settings_doctor.add_argument("--no-local-env-file", action="store_true")
    settings_explain = settings_sub.add_parser("explain", help="explain one effective setting source")
    settings_explain.add_argument("--key", required=True)
    settings_explain.add_argument("--env-file", default=None)
    settings_explain.add_argument("--no-local-env-file", action="store_true")


def handle_settings_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    inspect_effective_settings_fn: Callable[..., dict[str, Any]] = inspect_effective_settings,
    diagnose_effective_settings_fn: Callable[..., dict[str, Any]] = diagnose_effective_settings,
    explain_effective_setting_fn: Callable[..., dict[str, Any]] = explain_effective_setting,
) -> dict[str, Any]:
    if args.settings_command == "inspect":
        return build_response(
            tool_name="settings.inspect",
            ok=True,
            data=inspect_effective_settings_fn(
                repo_root=repo_base_fn(),
                env_file=args.env_file,
                include_local_env_file=not bool(args.no_local_env_file),
            ),
        )

    if args.settings_command == "doctor":
        data = diagnose_effective_settings_fn(
            repo_root=repo_base_fn(),
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
        )
        return build_response(
            tool_name="settings.doctor",
            ok=bool(data.get("summary", {}).get("ok", True)),
            data=data,
        )

    if args.settings_command == "explain":
        try:
            data = explain_effective_setting_fn(
                args.key,
                repo_root=repo_base_fn(),
                env_file=args.env_file,
                include_local_env_file=not bool(args.no_local_env_file),
            )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        return build_response(tool_name="settings.explain", ok=True, data=data)

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported settings command: {args.settings_command}")
