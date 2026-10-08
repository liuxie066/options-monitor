from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.config_edit import get_runtime_config_value
from src.application.config_yaml import (
    build_yaml_bot_config_file,
    build_yaml_runtime_config_file,
    explain_yaml_config_key,
    validate_yaml_runtime_config,
)
from src.application.config_yaml_init import _normalize_markets, init_yaml_config
from src.application.config_yaml_symbols import set_yaml_symbol_config
from src.application.config_yaml_holdings import set_yaml_holdings_inclusion
from src.application.runtime_config_readiness import require_runtime_config_readiness
from src.interfaces.cli.setup_ops import add_symbol_policy_arguments, symbol_policies_from_args


def add_config_commands(subparsers: Any) -> None:
    config = subparsers.add_parser("config", help="config operations")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    migrate = config_sub.add_parser("migrate-switches", help="preview retirement of inert switches and preserve legacy notification intent")
    migrate.add_argument("--config-yaml", default=None)
    migrate.add_argument("--runtime-root", default=None)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--confirm", action="store_true")
    migrate.add_argument("--expected-source-sha256", default=None)
    migrate.add_argument("--expected-preview-sha256", default=None)
    init_config = config_sub.add_parser("init", help="generate starter config.yaml and runtime configs")
    init_config.add_argument("--output", default=None, help="config.yaml path; defaults to repo-local config.yaml")
    init_config.add_argument("--runtime-output-dir", default=None, help="directory for generated config.us.json/config.hk.json")
    init_config.add_argument("--market", action="append", choices=("us", "hk", "all"), default=None)
    init_config.add_argument("--futu-acc-id", default=None, help="Futu account id; omitted keeps a placeholder in config.yaml")
    init_config.add_argument("--futu-host", default="127.0.0.1")
    init_config.add_argument("--futu-port", type=int, default=11111)
    init_config.add_argument("--trd-env", choices=("REAL", "SIMULATE"), default="REAL")
    init_config.add_argument("--account-label", "--account", dest="account_label", default=None,
                            help="required user-chosen account label; no default")
    init_config.add_argument("--us-symbol", action="append", dest="us_symbols", default=None,
                             help="required for US; repeat for each monitored symbol")
    init_config.add_argument("--hk-symbol", action="append", dest="hk_symbols", default=None,
                             help="required for HK; repeat for each monitored symbol")
    add_symbol_policy_arguments(init_config)
    init_config.add_argument("--no-build", action="store_true", help="only write config.yaml; do not build runtime JSON")
    init_config.add_argument("--dry-run", action="store_true", help="preview starter YAML without writing files")
    init_config.add_argument("--force", action="store_true")
    validate = config_sub.add_parser("validate", help="validate runtime config")
    validate.add_argument(
        "--source",
        default="runtime",
        metavar="{runtime,yaml}",
        help="validation source; defaults to generated runtime JSON",
    )
    validate.add_argument("--config-yaml", default=None)
    validate.add_argument("--config-key", default=None, choices=("us", "hk"))
    validate.add_argument("--config-path", default=None)
    validate.add_argument(
        "--related-config-path",
        action="append",
        default=None,
        help="additional generated runtime JSON to include in Futu routing audit; repeatable",
    )
    validate.add_argument("--market", default=None, choices=("us", "hk"))
    build = config_sub.add_parser("build", help="build canonical runtime config from config.yaml")
    build.add_argument("--source", default="yaml", metavar="{yaml}", help="authoring source; defaults to yaml")
    build.add_argument("--config-yaml", default=None)
    build.add_argument("--market", required=True, choices=("us", "hk"))
    build.add_argument("--system-config", default=None)
    build.add_argument("--output", default=None)
    build.add_argument("--dry-run", action="store_true")
    build_bot = config_sub.add_parser("build-bot", help="build Bot config from config.yaml")
    build_bot.add_argument("--source", default="yaml", choices=("yaml",))
    build_bot.add_argument("--config-yaml", default=None)
    build_bot.add_argument("--system-config", default=None)
    build_bot.add_argument("--output", default=None)
    build_bot.add_argument("--dry-run", action="store_true")
    explain = config_sub.add_parser("explain", help="explain a config.yaml key")
    explain.add_argument("--source", default="yaml", metavar="{yaml}", help="authoring source; defaults to yaml")
    explain.add_argument("--config-yaml", default=None)
    explain.add_argument("--market", required=True, choices=("us", "hk"))
    explain.add_argument("--key", required=True)
    explain.add_argument("--system-config", default=None)
    get_config = config_sub.add_parser("get", help="read a runtime config value by dot path")
    get_config.add_argument("--config-key", default=None, choices=("us", "hk"))
    get_config.add_argument("--config-path", default=None)
    get_config.add_argument("--key", required=True)
    symbol = config_sub.add_parser("symbol", help="edit config.yaml symbol authoring entries")
    symbol_sub = symbol.add_subparsers(dest="config_symbol_command", required=True)
    symbol_set = symbol_sub.add_parser("set", help="set one symbol strategy override in config.yaml")
    symbol_set.add_argument("--config-yaml", default=None)
    symbol_set.add_argument("--market", required=True, choices=("us", "hk"))
    symbol_set.add_argument("--symbol", required=True)
    symbol_set.add_argument("--covered-call-enabled", type=_parse_bool_value, default=None)
    symbol_set.add_argument("--covered-call-min-strike", type=float, default=None)
    symbol_set.add_argument("--sell-put-enabled", type=_parse_bool_value, default=None)
    symbol_set.add_argument("--sell-put-max-strike", type=float, default=None)
    symbol_set.add_argument("--expected-source-sha256", default=None)
    symbol_set.add_argument("--combo-yield-enabled", type=_parse_bool_value, default=None)
    symbol_set.add_argument("--rebuild-runtime-root", default=None)
    symbol_set.add_argument("--apply", action="store_true")
    symbol_set.add_argument("--no-backup", action="store_true")
    holdings = config_sub.add_parser("holdings", help="manage optional Portfolio Exposure Holdings inclusion")
    holdings_sub = holdings.add_subparsers(dest="config_holdings_command", required=True)
    holdings_set = holdings_sub.add_parser("set", help="preview or apply Holdings inclusion")
    holdings_set.add_argument("--enabled", required=True, type=_parse_bool_value)
    holdings_set.add_argument("--config-yaml", default=None)
    holdings_set.add_argument("--runtime-root", default=None)
    holdings_set.add_argument("--enable-pm", action="store_true", help="enable PM in the same generation as Holdings")
    holdings_set.add_argument("--apply", action="store_true")
    holdings_set.add_argument("--confirm", action="store_true")
    holdings_set.add_argument("--expected-source-sha256", default=None)
    holdings_set.add_argument("--expected-preview-sha256", default=None)


def _parse_bool_value(raw: str) -> bool:
    value = str(raw or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected boolean: true/false")


def _normalize_config_source(
    args: argparse.Namespace,
    *,
    allowed: tuple[str, ...],
) -> str:
    source = str(getattr(args, "source", "") or "").strip().lower()
    if source in allowed:
        return source
    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"--source must be one of: {', '.join(allowed)}",
        details={
            "source": source or None,
            "allowed": list(allowed),
        },
        hint="Create config.yaml, then use `om config build --source yaml`.",
    )


def _reject_runtime_validate_flags_for_yaml_source(args: argparse.Namespace) -> None:
    runtime_flags = []
    if str(getattr(args, "config_key", "") or "").strip():
        runtime_flags.append("--config-key")
    if str(getattr(args, "config_path", "") or "").strip():
        runtime_flags.append("--config-path")
    if list(getattr(args, "related_config_path", None) or []):
        runtime_flags.append("--related-config-path")
    if runtime_flags:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="runtime config flags cannot be used with --source yaml",
            details={"flags": runtime_flags},
            hint="Use `om config validate --source yaml --market <market> --config-yaml <path>` for authoring, or `om config validate --config-path <runtime-json> --market <market>` for generated runtime config.",
        )


def _reject_yaml_validate_flags_for_runtime_source(args: argparse.Namespace) -> None:
    if str(getattr(args, "config_yaml", "") or "").strip():
        raise AgentToolError(
            code="INPUT_ERROR",
            message="--config-yaml requires --source yaml",
            details={"flags": ["--config-yaml"]},
            hint="Use `om config validate --source yaml --market <market> --config-yaml <path>` for authoring, or pass generated JSON via --config-path.",
        )


def _validate_runtime_config(
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    market: str | None = None,
    repo_base_fn: Callable[[], Path] = repo_base,
) -> dict[str, Any]:
    path, cfg = load_runtime_config(
        config_key=config_key,
        config_path=config_path,
        expected_market=market,
    )
    readiness = require_runtime_config_readiness(
        dict(cfg),
        repo_root=repo_base_fn(),
        runtime_config_path=path,
        explicit_market=market,
        config_key=config_key,
    )
    return {
        "ok": True,
        "config_path": str(path),
        "config_key": str(config_key or "").strip().lower() or None,
        "market": readiness.get("market"),
        "source_format": (
            (cfg.get("_generated") or {}).get("source_format")
            if isinstance(cfg.get("_generated"), dict)
            else None
        ),
        "schedule_contract": readiness.get("schedule"),
        "freshness": readiness.get("freshness"),
        "readiness": readiness,
    }


def handle_config_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    validate_runtime_config_fn: Callable[..., dict[str, Any]] = _validate_runtime_config,
    validate_yaml_runtime_config_fn: Callable[..., dict[str, Any]] = validate_yaml_runtime_config,
    build_yaml_runtime_config_file_fn: Callable[..., dict[str, Any]] = build_yaml_runtime_config_file,
    build_yaml_bot_config_file_fn: Callable[..., dict[str, Any]] = build_yaml_bot_config_file,
    explain_yaml_config_key_fn: Callable[..., dict[str, Any]] = explain_yaml_config_key,
    init_yaml_config_fn: Callable[..., dict[str, Any]] = init_yaml_config,
    get_runtime_config_value_fn: Callable[..., dict[str, Any]] = get_runtime_config_value,
    set_yaml_symbol_config_fn: Callable[..., dict[str, Any]] = set_yaml_symbol_config,
) -> dict[str, Any]:
    if args.config_command == "migrate-switches":
        from src.application.config_switch_migration import migrate_yaml_switches
        return migrate_yaml_switches(repo_root=repo_base_fn(), config_path=args.config_yaml,
                                    runtime_root=args.runtime_root, apply=args.apply, confirm=args.confirm,
                                    expected_source_sha256=args.expected_source_sha256,
                                    expected_preview_sha256=args.expected_preview_sha256)
    if args.config_command == "validate":
        source = _normalize_config_source(args, allowed=("runtime", "yaml"))
        if source == "yaml":
            _reject_runtime_validate_flags_for_yaml_source(args)
            if not args.market:
                raise AgentToolError(code="INPUT_ERROR", message="--market is required when --source yaml")
            return validate_yaml_runtime_config_fn(
                repo_root=repo_base_fn(),
                market=args.market,
                config_path=args.config_yaml,
            )
        _reject_yaml_validate_flags_for_runtime_source(args)
        result = validate_runtime_config_fn(
            config_key=args.config_key,
            config_path=args.config_path,
            market=args.market,
        )
        related_paths = list(getattr(args, "related_config_path", None) or [])
        if not related_paths:
            return result
        primary_path, primary_cfg = load_runtime_config(
            config_key=args.config_key,
            config_path=args.config_path,
            expected_market=args.market,
        )
        resolved_paths = [primary_path.resolve()]
        loaded: list[tuple[str | None, Path, dict[str, Any]]] = [
            (str(args.config_key or "").strip().lower() or None, primary_path, primary_cfg)
        ]
        markets = {str(result.get("market") or "").strip().upper()}
        for raw_path in related_paths:
            path, cfg = load_runtime_config(config_path=raw_path)
            resolved = path.resolve()
            if resolved in resolved_paths:
                raise AgentToolError(code="INPUT_ERROR", message="runtime config paths must be unique")
            readiness = require_runtime_config_readiness(
                dict(cfg), repo_root=repo_base_fn(), runtime_config_path=path
            )
            related_market = str(readiness.get("market") or "").strip().upper()
            if related_market in markets:
                raise AgentToolError(code="INPUT_ERROR", message="runtime config markets must be unique")
            markets.add(related_market)
            resolved_paths.append(resolved)
            loaded.append((related_market.lower() or None, path, cfg))
        from src.application.futu_routing_audit import build_futu_routing_audit

        audit = build_futu_routing_audit(loaded)
        result["futu_routing_audit"] = audit
        result["ok"] = bool(result.get("ok", True) and audit["ok"])
        return result

    if args.config_command == "build":
        _normalize_config_source(args, allowed=("yaml",))
        return build_yaml_runtime_config_file_fn(
            repo_root=repo_base_fn(),
            market=args.market,
            config_path=args.config_yaml,
            system_config_path=args.system_config,
            output_config_path=args.output,
            dry_run=bool(args.dry_run),
        )

    if args.config_command == "build-bot":
        return build_yaml_bot_config_file_fn(
            repo_root=repo_base_fn(),
            config_path=args.config_yaml,
            system_config_path=args.system_config,
            output_config_path=args.output,
            dry_run=bool(args.dry_run),
        )

    if args.config_command == "explain":
        _normalize_config_source(args, allowed=("yaml",))
        return explain_yaml_config_key_fn(
            repo_root=repo_base_fn(),
            market=args.market,
            key=args.key,
            config_path=args.config_yaml,
            system_config_path=args.system_config,
        )

    if args.config_command == "init":
        return init_yaml_config_fn(
            repo_root=repo_base_fn(),
            output_config_yaml_path=args.output,
            runtime_output_dir=args.runtime_output_dir,
            markets=args.market,
            futu_acc_id=args.futu_acc_id,
            futu_host=args.futu_host, futu_port=args.futu_port, trd_env=args.trd_env,
            account_label=args.account_label,
            us_symbols=args.us_symbols,
            hk_symbols=args.hk_symbols,
            symbol_policies=symbol_policies_from_args(
                args,
                symbols={"us": args.us_symbols, "hk": args.hk_symbols},
                markets=_normalize_markets(args.market),
            ),
            build=not bool(args.no_build),
            dry_run=bool(args.dry_run),
            force=bool(args.force),
        )

    if args.config_command == "get":
        return build_response(
            tool_name="config.get",
            ok=True,
            data=get_runtime_config_value_fn(
                config_key=args.config_key,
                config_path=args.config_path,
                key=args.key,
                repo_root=repo_base_fn(),
            ),
        )

    if args.config_command == "symbol":
        if args.config_symbol_command == "set":
            return set_yaml_symbol_config_fn(
                repo_root=repo_base_fn(),
                market=args.market,
                symbol=args.symbol,
                config_path=args.config_yaml,
                covered_call_enabled=args.covered_call_enabled,
                covered_call_min_strike=args.covered_call_min_strike,
                sell_put_enabled=args.sell_put_enabled,
                sell_put_max_strike=getattr(args, "sell_put_max_strike", None),
                expected_source_sha256=getattr(args, "expected_source_sha256", None),
                combo_yield_enabled=args.combo_yield_enabled,
                rebuild_runtime_root=args.rebuild_runtime_root,
                apply=bool(args.apply),
                backup=not bool(args.no_backup),
            )

    if args.config_command == "holdings" and args.config_holdings_command == "set":
        return set_yaml_holdings_inclusion(
            repo_root=repo_base_fn(),
            enabled=args.enabled,
            enable_pm=args.enable_pm,
            config_path=args.config_yaml,
            runtime_root=args.runtime_root,
            apply=bool(args.apply),
            confirm=bool(args.confirm),
            expected_source_sha256=args.expected_source_sha256,
            expected_preview_sha256=args.expected_preview_sha256,
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported config command: {args.config_command}")
