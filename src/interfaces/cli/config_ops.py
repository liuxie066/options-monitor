from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from domain.domain.config_contract import ensure_runtime_schedule_matches_market
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.config_edit import get_runtime_config_value
from src.application.config_validator import validate_config
from src.application.config_yaml import (
    build_yaml_assistant_config_file,
    build_yaml_runtime_config_file,
    explain_yaml_config_key,
    validate_yaml_runtime_config,
)
from src.application.config_yaml_init import init_yaml_config
from src.application.config_yaml_migration import preview_config_yaml_migration
from src.application.config_yaml_symbols import set_yaml_symbol_config
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    ensure_runtime_config_freshness,
    infer_runtime_config_market,
)


def add_config_commands(subparsers: Any) -> None:
    config = subparsers.add_parser("config", help="config operations")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    init_config = config_sub.add_parser("init", help="generate starter config.yaml and runtime configs")
    init_config.add_argument("--output", default=None, help="config.yaml path; defaults to repo-local config.yaml")
    init_config.add_argument("--runtime-output-dir", default=None, help="directory for generated config.us.json/config.hk.json")
    init_config.add_argument("--market", action="append", choices=("us", "hk", "all"), default=None)
    init_config.add_argument("--futu-acc-id", default=None, help="Futu account id; omitted keeps a placeholder in config.yaml")
    init_config.add_argument("--account-label", "--account", dest="account_label", default="lx")
    init_config.add_argument("--external-holdings-account", default="sy")
    init_config.add_argument("--no-external-holdings", action="store_true")
    init_config.add_argument("--us-symbol", action="append", dest="us_symbols", default=None)
    init_config.add_argument("--hk-symbol", action="append", dest="hk_symbols", default=None)
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
    validate.add_argument("--market", default=None, choices=("us", "hk"))
    build = config_sub.add_parser("build", help="build canonical runtime config from config.yaml")
    build.add_argument("--source", default="yaml", metavar="{yaml}", help="authoring source; defaults to yaml")
    build.add_argument("--config-yaml", default=None)
    build.add_argument("--market", required=True, choices=("us", "hk"))
    build.add_argument("--system-config", default=None)
    build.add_argument("--output", default=None)
    build.add_argument("--dry-run", action="store_true")
    build_assistant = config_sub.add_parser("build-assistant", help="build assistant config from config.yaml")
    build_assistant.add_argument("--source", default="yaml", choices=("yaml",))
    build_assistant.add_argument("--config-yaml", default=None)
    build_assistant.add_argument("--system-config", default=None)
    build_assistant.add_argument("--output", default=None)
    build_assistant.add_argument("--dry-run", action="store_true")
    explain = config_sub.add_parser("explain", help="explain a config.yaml key")
    explain.add_argument("--source", default="yaml", metavar="{yaml}", help="authoring source; defaults to yaml")
    explain.add_argument("--config-yaml", default=None)
    explain.add_argument("--market", required=True, choices=("us", "hk"))
    explain.add_argument("--key", required=True)
    explain.add_argument("--system-config", default=None)
    migrate_yaml = config_sub.add_parser("migrate-yaml", help="preview migration from layered JSON user config to config.yaml")
    migrate_yaml.add_argument("--common-user-config", default=None)
    migrate_yaml.add_argument("--no-common-user-config", action="store_true")
    migrate_yaml.add_argument("--us-user-config", default=None)
    migrate_yaml.add_argument("--hk-user-config", default=None)
    migrate_yaml.add_argument("--us-accounts", nargs="+", default=None)
    migrate_yaml.add_argument("--hk-accounts", nargs="+", default=None)
    migrate_yaml.add_argument("--output", default=None)
    migrate_yaml.add_argument("--apply", action="store_true", help="write config.yaml; omitted means dry-run preview")
    migrate_yaml.add_argument("--no-backup", action="store_true", help="do not write a .bak timestamp copy before applying")
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
    symbol_set.add_argument("--rebuild-runtime-root", default=None)
    symbol_set.add_argument("--apply", action="store_true")
    symbol_set.add_argument("--no-backup", action="store_true")


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
        hint="Use `om config migrate-yaml` for old JSON configs, then use `om config build --source yaml`.",
    )


def _reject_runtime_validate_flags_for_yaml_source(args: argparse.Namespace) -> None:
    runtime_flags = []
    if str(getattr(args, "config_key", "") or "").strip():
        runtime_flags.append("--config-key")
    if str(getattr(args, "config_path", "") or "").strip():
        runtime_flags.append("--config-path")
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
    validate_config(dict(cfg))
    inferred_market = infer_runtime_config_market(
        explicit_market=market,
        config_key=config_key,
        config_path=path,
        config=cfg,
    )
    freshness = None
    schedule_contract = None
    if inferred_market:
        try:
            schedule_contract = ensure_runtime_schedule_matches_market(
                cfg,
                config_path=path,
                market_config=inferred_market,
            )
        except SystemExit as exc:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=str(exc),
                details={"config_path": str(path), "market": str(inferred_market)},
            ) from exc
        try:
            freshness = ensure_runtime_config_freshness(
                cfg,
                repo_root=repo_base_fn(),
                market=inferred_market,
                runtime_config_path=path,
            )
        except RuntimeConfigFreshnessError as exc:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=str(exc),
                details=exc.result,
            ) from exc
    return {
        "ok": True,
        "config_path": str(path),
        "config_key": str(config_key or "").strip().lower() or None,
        "market": inferred_market,
        "source_format": (
            (cfg.get("_generated") or {}).get("source_format")
            if isinstance(cfg.get("_generated"), dict)
            else None
        ),
        "schedule_contract": schedule_contract,
        "freshness": freshness,
    }


def handle_config_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    validate_runtime_config_fn: Callable[..., dict[str, Any]] = _validate_runtime_config,
    validate_yaml_runtime_config_fn: Callable[..., dict[str, Any]] = validate_yaml_runtime_config,
    build_yaml_runtime_config_file_fn: Callable[..., dict[str, Any]] = build_yaml_runtime_config_file,
    build_yaml_assistant_config_file_fn: Callable[..., dict[str, Any]] = build_yaml_assistant_config_file,
    explain_yaml_config_key_fn: Callable[..., dict[str, Any]] = explain_yaml_config_key,
    preview_config_yaml_migration_fn: Callable[..., dict[str, Any]] = preview_config_yaml_migration,
    init_yaml_config_fn: Callable[..., dict[str, Any]] = init_yaml_config,
    get_runtime_config_value_fn: Callable[..., dict[str, Any]] = get_runtime_config_value,
    set_yaml_symbol_config_fn: Callable[..., dict[str, Any]] = set_yaml_symbol_config,
) -> dict[str, Any]:
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
        return validate_runtime_config_fn(
            config_key=args.config_key,
            config_path=args.config_path,
            market=args.market,
        )

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

    if args.config_command == "build-assistant":
        return build_yaml_assistant_config_file_fn(
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

    if args.config_command == "migrate-yaml":
        return preview_config_yaml_migration_fn(
            repo_root=repo_base_fn(),
            common_user_config_path=args.common_user_config,
            include_common_user_config=not bool(args.no_common_user_config),
            us_user_config_path=args.us_user_config,
            hk_user_config_path=args.hk_user_config,
            us_accounts=args.us_accounts,
            hk_accounts=args.hk_accounts,
            output_config_yaml_path=args.output,
            apply=bool(args.apply),
            backup=not bool(args.no_backup),
        )

    if args.config_command == "init":
        return init_yaml_config_fn(
            repo_root=repo_base_fn(),
            output_config_yaml_path=args.output,
            runtime_output_dir=args.runtime_output_dir,
            markets=args.market,
            futu_acc_id=args.futu_acc_id,
            account_label=args.account_label,
            external_holdings_account=None if bool(args.no_external_holdings) else args.external_holdings_account,
            us_symbols=args.us_symbols,
            hk_symbols=args.hk_symbols,
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
                rebuild_runtime_root=args.rebuild_runtime_root,
                apply=bool(args.apply),
                backup=not bool(args.no_backup),
            )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported config command: {args.config_command}")
