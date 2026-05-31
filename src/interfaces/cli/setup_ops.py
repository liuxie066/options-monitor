from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.setup import run_setup_check


def add_setup_commands(subparsers: Any) -> None:
    multiplier_cache = subparsers.add_parser("multiplier-cache", help="inspect or seed the shared multiplier cache")
    multiplier_cache_sub = multiplier_cache.add_subparsers(dest="multiplier_cache_command", required=True)
    multiplier_seed = multiplier_cache_sub.add_parser("seed", help="seed a symbol multiplier into runtime cache; dry-run by default")
    multiplier_seed.add_argument("--symbol", required=True)
    multiplier_seed.add_argument("--multiplier", type=int, required=True)
    multiplier_seed.add_argument("--source", default="manual_seed")
    multiplier_seed.add_argument("--runtime-root", default=None)
    multiplier_seed.add_argument("--config-path", default=None)
    multiplier_seed.add_argument("--cache", default=None)
    multiplier_seed.add_argument("--apply", action="store_true")

    setup = subparsers.add_parser("setup", help="install-time checks and first-run setup helpers")
    setup_sub = setup.add_subparsers(dest="setup_command", required=True)
    setup_check = setup_sub.add_parser("check", help="run read-only first-run setup diagnostics")
    setup_check.add_argument("--market", action="append", choices=("us", "hk", "all"), default=None)
    setup_check.add_argument("--env-file", default=None)
    setup_check.add_argument("--no-local-env-file", action="store_true")


def handle_setup_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    run_setup_check_fn: Callable[..., dict[str, Any]] = run_setup_check,
) -> dict[str, Any]:
    if args.command == "setup" and args.setup_command == "check":
        data = run_setup_check_fn(
            repo_root=repo_base_fn(),
            markets=args.market,
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
        )
        return build_response(
            tool_name="setup.check",
            ok=bool(data.get("summary", {}).get("ok", True)),
            data=data,
        )

    if args.command == "multiplier-cache" and args.multiplier_cache_command == "seed":
        from src.application.multiplier_cache import seed_multiplier_cache

        data = seed_multiplier_cache(
            repo_base=repo_base_fn(),
            symbol=args.symbol,
            multiplier=args.multiplier,
            source=args.source,
            runtime_root=args.runtime_root,
            config_path=args.config_path,
            cache_path=args.cache,
            confirm=bool(args.apply),
        )
        return build_response(tool_name="multiplier_cache.seed", ok=bool(data.get("ok")), data=data)

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported setup command: {args.command}")
