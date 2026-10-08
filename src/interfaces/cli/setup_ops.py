from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.setup import run_setup_check
from src.application.config_yaml_init import _normalize_markets, _normalize_symbols, init_yaml_config
from src.application.config_yaml_init import create_starter_config
from src.application.platform_profile import current_platform_profile
from src.application.runtime_paths import read_runtime_root_record, runtime_root_record_path
from src.application.settings import build_effective_env
from src.application.symbol_calibration import canonical_symbol_for_write


def add_symbol_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol-strategy", action="append", metavar="SYMBOL=csp|cc|both",
                        help="strategy for one monitored symbol; repeat for each symbol")
    parser.add_argument("--csp-min-strike", action="append", metavar="SYMBOL=PRICE",
                        help="optional CSP lower strike bound")
    parser.add_argument("--csp-max-strike", action="append", metavar="SYMBOL=PRICE",
                        help="required for each CSP symbol")
    parser.add_argument("--cc-min-strike", action="append", metavar="SYMBOL=PRICE",
                        help="required for each CC symbol")
    parser.add_argument("--cc-max-strike", action="append", metavar="SYMBOL=PRICE",
                        help="optional CC upper strike bound")


def _keyed_symbol_values(values: list[str] | None, *, option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values or []:
        symbol, separator, value = str(raw).partition("=")
        symbol, value = symbol.strip().upper(), value.strip()
        if not separator or not symbol or not value:
            raise AgentToolError(code="INPUT_ERROR", message=f"{option} expects SYMBOL=VALUE")
        symbol = canonical_symbol_for_write(
            symbol, error_factory=lambda message: AgentToolError(code="INPUT_ERROR", message=message),
        )
        if symbol in result:
            raise AgentToolError(code="INPUT_ERROR", message=f"duplicate {option} for {symbol}")
        result[symbol] = value
    return result


def symbol_policies_from_args(
    args: argparse.Namespace,
    *,
    symbols: dict[str, list[str] | None],
    markets: list[str],
    interactive: bool = False,
    input_fn: Callable[[str], str] = input,
) -> dict[str, dict[str, Any]]:
    fields = {
        name: _keyed_symbol_values(getattr(args, name, None), option="--" + name.replace("_", "-"))
        for name in ("symbol_strategy", "csp_min_strike", "csp_max_strike", "cc_min_strike", "cc_max_strike")
    }
    ordered = list(dict.fromkeys(
        symbol for market in markets if symbols[market]
        for symbol in _normalize_symbols(symbols[market], market=market)
    ))
    unknown = set().union(*(set(items) for items in fields.values())) - set(ordered)
    if unknown:
        raise AgentToolError(code="INPUT_ERROR", message=f"symbol policy refers to unselected symbol: {', '.join(sorted(unknown))}")
    policies: dict[str, dict[str, Any]] = {}
    for symbol in ordered:
        policy = {
            name: values[symbol]
            for name, values in fields.items() if symbol in values
        }
        if interactive:
            current = policy.get("symbol_strategy") or "必填"
            entered = input_fn(f"{symbol} 策略 [csp/cc/both] [{current}]: ").strip().lower()
            if entered:
                policy["symbol_strategy"] = entered
            strategy = policy.get("symbol_strategy")
            for side, enabled in (("csp", strategy in {"csp", "both"}), ("cc", strategy in {"cc", "both"})):
                if not enabled:
                    continue
                for bound in (("max", "min") if side == "csp" else ("min", "max")):
                    name = f"{side}_{bound}_strike"
                    current_value = policy.get(name) or ("必填" if (side, bound) in {("csp", "max"), ("cc", "min")} else "可选")
                    entered = input_fn(f"{symbol} {side.upper()} {bound} strike [{current_value}]: ").strip()
                    if entered:
                        policy[name] = entered
        if not policy.get("symbol_strategy"):
            raise AgentToolError(code="INPUT_ERROR", message=f"{symbol} requires --symbol-strategy SYMBOL=csp|cc|both")
        policies[symbol] = {
            "strategy": policy["symbol_strategy"],
            **{name: value for name, value in policy.items() if name != "symbol_strategy"},
        }
    return policies


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
    setup_init.add_argument("--account-label", default=None, help="required user-chosen account label; no default")
    setup_init.add_argument("--futu-acc-id", default=None)
    setup_init.add_argument("--futu-host", default="127.0.0.1")
    setup_init.add_argument("--futu-port", type=int, default=11111)
    setup_init.add_argument("--trd-env", choices=("REAL", "SIMULATE"), default=None)
    setup_init.add_argument("--us-symbol", action="append", dest="us_symbols", default=None,
                            help="monitored US symbol; repeat for multiple symbols")
    setup_init.add_argument("--hk-symbol", action="append", dest="hk_symbols", default=None,
                            help="monitored HK symbol; repeat for multiple symbols")
    add_symbol_policy_arguments(setup_init)
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
    create_config_fn: Callable[..., dict[str, Any]] = create_starter_config,
    user_home: Path | None = None,
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
    futu_host = getattr(args, "futu_host", "127.0.0.1")
    futu_port = getattr(args, "futu_port", 11111)
    trd_env = getattr(args, "trd_env", None)
    symbols = {"us": args.us_symbols, "hk": args.hk_symbols}
    if interactive:
        try:
            selected_default = markets[0] if markets and len(markets) == 1 and markets[0] in {"us", "hk"} else "us"
            raw_market = input_fn(f"首次配置市场 [us/hk] [{selected_default}]: ").strip().lower() or selected_default
            if raw_market not in {"us", "hk"}:
                raise AgentToolError(code="INPUT_ERROR", message="interactive setup selects one market: us or hk")
            markets = [raw_market]
            futu_host = input_fn(f"OpenD 地址 [{futu_host}]: ").strip() or futu_host
            raw_port = input_fn(f"OpenD 端口 [{futu_port}]: ").strip()
            if raw_port:
                try:
                    futu_port = int(raw_port)
                except ValueError as exc:
                    raise AgentToolError(code="INPUT_ERROR", message="OpenD port must be an integer") from exc
            trd_env = input_fn(f"账户环境 REAL（真实）/SIMULATE（测试）[{trd_env or '必填'}]: ").strip().upper() or trd_env
            account_label = input_fn(f"账户标签（自定义，必填）[{account_label or '无默认值'}]: ").strip() or account_label
            if not account_label or not account_label.strip():
                raise AgentToolError(code="INPUT_ERROR", message="账户标签必填，请填写自己的账户标签（--account-label）")
            futu_acc_id = input_fn("富途账户 ID（数字，必填）: ").strip() or futu_acc_id
            if not futu_acc_id:
                raise AgentToolError(code="INPUT_ERROR", message="interactive setup requires a Futu account ID")
            if not trd_env:
                raise AgentToolError(code="INPUT_ERROR", message="interactive setup requires REAL or SIMULATE")
            for market in _normalize_markets(markets):
                current = ", ".join(symbols[market] or []) or "必填"
                entered = input_fn(f"{market.upper()} 监控标的（逗号或空格分隔）[{current}]: ").strip()
                if entered:
                    symbols[market] = entered.replace(",", " ").split()
            selected_markets = _normalize_markets(markets)
            symbol_policies = symbol_policies_from_args(
                args, symbols=symbols, markets=selected_markets, interactive=True, input_fn=input_fn,
            )
        except (EOFError, KeyboardInterrupt) as exc:
            raise AgentToolError(code="INPUT_ERROR", message="setup init cancelled before preview") from exc
    else:
        selected_markets = _normalize_markets(markets)
        symbol_policies = symbol_policies_from_args(args, symbols=symbols, markets=selected_markets)
    output_dir = output_dir.resolve()
    repo_root = repo_base_fn()
    record = runtime_root_record_path(user_home=user_home)
    options = {
        "repo_root": repo_root,
        "output_config_yaml_path": output_dir / "config.yaml",
        "runtime_output_dir": output_dir,
        "bot_output_config_path": output_dir / "resolved" / "config.bot.json",
        "markets": markets,
        "futu_acc_id": futu_acc_id,
        "futu_host": futu_host, "futu_port": futu_port, "trd_env": trd_env or "REAL",
        "account_label": account_label,
        "us_symbols": symbols["us"],
        "hk_symbols": symbols["hk"],
        "symbol_policies": symbol_policies,
    }
    preview = init_config_fn(**options, dry_run=True)
    selected = preview["markets"]
    paths = [preview["config_yaml_path"], preview["bot_config_path"]]
    paths.extend(preview["runtime_config_paths"][market] for market in selected)
    record_exists = record.exists() or record.is_symlink()
    if record_exists and read_runtime_root_record(record, require_config=False) != output_dir:
        raise AgentToolError(code="CONFIG_ERROR", message="runtime root record points to another directory",
                             details={"record_path": str(record)},
                             hint="Inspect the existing runtime-root record before choosing a new directory.")
    if not record_exists:
        paths.append(str(record))
    effective = build_effective_env(repo_root=repo_root, include_local_env_file=True)
    active_root = str(effective.get("OM_RUNTIME_ROOT") or "").strip()
    override = bool(active_root and Path(active_root).expanduser().resolve() != output_dir)
    lines = ["首次配置预览（尚未写入）："]
    lines.extend(f"  {path}" for path in paths)
    lines.append(f"市场：{', '.join(selected)} · 账户标签：{preview['account_label']}")
    lines.append("监控标的：" + "；".join(
        f"{market.upper()} {', '.join(preview['market_symbols'][market])}" for market in selected
    ))
    for market in selected:
        for symbol in preview["market_symbols"][market]:
            policy = preview["symbol_policies"][symbol]
            put, call = policy["sell_put"], policy["covered_call"]
            lines.append(
                f"  {symbol}：CSP={'on' if put['enabled'] else 'off'}"
                f" max_strike={put.get('max_strike', '-')} min_strike={put.get('min_strike', '-')}; "
                f"CC={'on' if call['enabled'] else 'off'}"
                f" min_strike={call.get('min_strike', '-')} max_strike={call.get('max_strike', '-')}"
            )
    lines.append(f"OpenD：{preview['futu_host']}:{preview['futu_port']} · 环境：{preview['trd_env']}（尚未验证登录与账户）")
    if not trd_env:
        lines.append("账户环境尚未确认；当前仅按 REAL 生成预览，写入前必须选择 REAL/SIMULATE。")
    lines.append("通知和 Bot 默认未启用，可在后续步骤单独配置。")
    lines.append(f"运行目录记录：{record}（{'保留已有' if record_exists else '新建'}）")
    if preview.get("futu_account_id_placeholder"):
        lines.append("富途账户 ID 尚未填写，基础配置未就绪；创建后可用 om accounts edit 补齐。")
    else:
        lines.append("富途账户 ID 已填写（值已隐藏）。")
    if not record_exists and (repo_root / "config.yaml").exists() and output_dir != repo_root:
        lines.append("注意：源码目录已有配置；新记录将改变无显式路径命令的默认实例。")
    if override:
        source = effective.source_of("OM_RUNTIME_ROOT")
        lines.append(f"注意：当前 OM_RUNTIME_ROOT 来自 {source.public_value() if source else 'environment'}，仍优先于新记录；请核对或清除该覆盖。")
    preview_text = "\n".join(lines) + "\n"
    if not interactive and args.apply and (not futu_acc_id or not trd_env):
        raise AgentToolError(code="INPUT_ERROR", message="setup init --apply requires --futu-acc-id and --trd-env REAL|SIMULATE",
                             hint="Use --dry-run to preview; advanced config init can create an unfinished placeholder.")
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
    applied = create_config_fn(**options, record_path=record)
    if not applied.get("write_applied") or not all(Path(path).is_file() for path in paths):
        raise AgentToolError(code="CONFIG_ERROR", message="setup init write could not be verified")
    next_lines = ["已写入并回读文件，运行目录已记住。下一步："]
    if override:
        next_lines.append("  当前 OM_RUNTIME_ROOT 仍覆盖该记录；请核对或清除覆盖后再检查。")
    if preview.get("futu_account_id_placeholder"):
        command = (f"om accounts edit --market {selected[0]} --account-label {preview['account_label']}"
                   f" --futu-acc-id ACCOUNT_ID --config-yaml {shlex.quote(applied['config_yaml_path'])}")
        next_lines.extend((
            "  富途账户 ID 仍是占位符，请将 ACCOUNT_ID 换成真实数字：",
            "  先预览：",
            f"    {command}",
            "  确认后写入：",
            f"    {command} --apply --confirm",
        ))
    source_arg = f" --config-yaml {shlex.quote(applied['config_yaml_path'])}" if override else ""
    next_lines.extend(f"  om symbols list --market {market}{source_arg}" for market in selected)
    next_lines.append("  om setup check --format text")
    next_lines.append("高级配置：om config --help；凭证：om secrets status --format text")
    next_steps = "\n".join(next_lines)
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
