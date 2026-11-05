from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.config_defaults import output_runs_keep_days
from src.application.output_runs_retention import configured_keep_days, prune_output_runs
from src.application.runtime_paths import resolve_runtime_root


def add_maintenance_commands(subparsers: Any) -> None:
    maintenance = subparsers.add_parser("maintenance", help="preview or apply runtime maintenance")
    commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    prune = commands.add_parser("prune-runs", help="retain recent output_runs directories (preview by default)")
    prune.add_argument("--runtime-root", default=None)
    prune.add_argument("--config-key", choices=("us", "hk"), default=None)
    prune.add_argument("--config-path", default=None)
    prune.add_argument("--keep-days", type=int, default=None)
    mode = prune.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (the default)")
    mode.add_argument("--confirm", action="store_true", help="delete eligible expired run directories")


def _configured_days(root: Path, args: Any) -> int:
    if args.keep_days is not None:
        return output_runs_keep_days({"runtime": {"output_runs_keep_days": args.keep_days}})
    if args.config_path:
        paths = [Path(args.config_path).expanduser().resolve()]
    elif args.config_key:
        paths = [root / f"config.{args.config_key}.json"]
    else:
        return configured_keep_days(root)
    days: list[int] = []
    for path in paths:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AgentToolError(code="CONFIG_ERROR", message=f"cannot read runtime config: {path}") from exc
        if not isinstance(config, dict):
            raise ValueError(f"runtime config must be an object: {path}")
        days.append(output_runs_keep_days(config))
    # Both markets share output_runs: preserve the longer retention when configured differently.
    return max(days) if days else output_runs_keep_days({})


def handle_maintenance_command(args: Any, *, repo_root: Path) -> int:
    root = resolve_runtime_root(repo_root=repo_root, runtime_root=args.runtime_root).runtime_root
    try:
        keep_days = _configured_days(root, args)
        result = prune_output_runs(runtime_root=root, keep_days=keep_days, confirm=args.confirm, trigger="manual")
    except (ValueError, OSError) as exc:
        raise AgentToolError(code="MAINTENANCE_ERROR", message=str(exc)) from exc
    print(json.dumps(build_response(tool_name="maintenance.prune_runs", ok=result["ok"], data=result),
                     ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2
