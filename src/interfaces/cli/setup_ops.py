from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.setup import run_setup_check
from src.application.config_yaml_init import init_yaml_config
from src.application.platform_profile import current_platform_profile


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
    setup_check.add_argument("--format", choices=("json", "text"), default="json")
    setup_init = setup_sub.add_parser("init", help="preview and confirm first-run configuration")
    setup_init.add_argument("--output-dir", default=None, help="config and runtime output directory")
    setup_init.add_argument("--market", action="append", choices=("us", "hk", "all"), default=None)
    setup_init.add_argument("--account-label", default=None)
    setup_init.add_argument("--futu-acc-id", default=None)
    setup_init.add_argument("--external-holdings-account", default=None)
    mode = setup_init.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only; never write")
    mode.add_argument("--apply", action="store_true", help="write after preview without a terminal prompt")


def _default_setup_dir() -> Path:
    profile = current_platform_profile()
    if profile.platform == "macos":
        return profile.default_runtime_root
    return Path.home() / ".local" / "share" / "options-monitor"


def run_setup_init(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    init_config_fn: Callable[..., dict[str, Any]] = init_yaml_config,
    input_fn: Callable[[str], str] = input,
    input_is_tty: Callable[[], bool] = lambda: bool(sys.stdin.isatty()),
) -> tuple[str, bool]:
    interactive = input_is_tty() and not (args.dry_run or args.apply)
    if not interactive and not (args.dry_run or args.apply):
        raise AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message="setup init requires an interactive terminal, --dry-run, or --apply",
        )
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else _default_setup_dir()
    markets = args.market
    account_label = args.account_label
    futu_acc_id = args.futu_acc_id
    if interactive:
        try:
            raw_dir = input_fn(f"配置目录 [{output_dir}]: ").strip()
            if raw_dir:
                output_dir = Path(raw_dir).expanduser()
            raw_markets = input_fn("市场 [us,hk]（也可填 us 或 hk）: ").strip().lower()
            if raw_markets:
                markets = [item.strip() for item in raw_markets.split(",")]
            account_label = input_fn(f"账户标签 [{account_label or 'lx'}]: ").strip() or account_label
            futu_acc_id = input_fn("富途账户 ID（数字，回车保留占位符）: ").strip() or futu_acc_id
        except (EOFError, KeyboardInterrupt) as exc:
            raise AgentToolError(code="INPUT_ERROR", message="setup init cancelled before preview") from exc
    output_dir = output_dir.resolve()
    options = {
        "repo_root": repo_base_fn(),
        "output_config_yaml_path": output_dir / "config.yaml",
        "runtime_output_dir": output_dir,
        "assistant_output_config_path": output_dir / "resolved" / "config.assistant.json",
        "markets": markets,
        "futu_acc_id": futu_acc_id,
        "account_label": account_label,
        "external_holdings_account": args.external_holdings_account,
    }
    preview = init_config_fn(**options, dry_run=True)
    selected = preview["markets"]
    paths = [preview["config_yaml_path"], preview["assistant_config_path"]]
    paths.extend(preview["runtime_config_paths"][market] for market in selected)
    lines = ["首次配置预览（尚未写入）："]
    lines.extend(f"  {path}" for path in paths)
    lines.append(f"市场：{', '.join(selected)} · 账户标签：{preview['account_label']}")
    if preview.get("futu_account_id_placeholder"):
        lines.append("富途账户 ID 尚未填写；创建后须在 config.yaml 中替换占位符。")
    preview_text = "\n".join(lines) + "\n"
    if args.dry_run:
        return preview_text + "仅预览，未写入。\n", False
    if interactive:
        sys.stdout.write(preview_text)
        sys.stdout.flush()
        try:
            approved = input_fn("确认写入上述文件？输入 yes 继续: ").strip().lower() == "yes"
        except (EOFError, KeyboardInterrupt):
            approved = False
        if not approved:
            return "已取消，未写入。\n", False
    applied = init_config_fn(**options, dry_run=False)
    if not applied.get("write_applied") or not all(Path(path).is_file() for path in paths):
        raise AgentToolError(code="CONFIG_ERROR", message="setup init write could not be verified")
    quoted_dir = shlex.quote(str(output_dir))
    next_steps = "\n".join(
        (
            "已写入并回读文件。下一步：",
            f"  export OM_RUNTIME_ROOT={quoted_dir}",
            f"  $EDITOR {shlex.quote(applied['config_yaml_path'])}",
            "  编辑后校验并重建快照：",
            *(f"    {command}" for command in applied["next_steps"]),
            "  om setup check " + " ".join(f"--market {market}" for market in selected) + " --format text",
            "高级配置：om config --help；凭证：om secrets status --format text",
        )
    )
    return ("" if interactive else preview_text) + next_steps + "\n", True


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
