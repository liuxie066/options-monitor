"""Human CLI for the authoritative YAML monitored-symbol list."""

from __future__ import annotations

import argparse
import json
from typing import Any

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import normalize_config_market
from src.application.config_yaml import load_yaml_config_file, resolve_yaml_config_path, resolve_yaml_runtime_config
from src.application.config_yaml_symbols import mutate_yaml_symbol_config, symbol_strategy_override
from src.application.symbol_calibration import calibrate_symbol


def parse_value(raw: str) -> Any:
    text = raw.strip()
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=f"invalid JSON value for --set: {text}") from exc
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.lower() in {"null", "none"}:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="om symbols", description="Manage monitored symbols in config.yaml")
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("--market", choices=("us", "hk"),
                       help="optional for recognizable symbols or a single configured market")
    scope.add_argument("--config-yaml", "--config", dest="config_yaml", default=None,
                       help="authoritative config.yaml; defaults to the active runtime root")
    scope.add_argument("--format", choices=("text", "json"), default="text")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", parents=[scope], help="list this market's monitored symbols")
    for name in ("add", "rm", "edit"):
        command = commands.add_parser(name, parents=[scope], help=f"preview or apply a YAML symbol {name}")
        command.add_argument("symbol")
        if name == "add":
            command.add_argument("--strategy", choices=("csp", "cc", "both"),
                                 help="required: enable CSP, CC, or both")
            command.add_argument("--csp-min-strike", type=float, help="optional CSP lower strike bound")
            command.add_argument("--csp-max-strike", type=float, help="required when CSP is enabled")
            command.add_argument("--cc-min-strike", type=float, help="required when CC is enabled")
            command.add_argument("--cc-max-strike", type=float, help="optional CC upper strike bound")
        command.add_argument("--expected-source-sha256", default=None, help="source revision from the preview")
        command.add_argument("--rebuild-runtime-root", default=None,
                             help="generated snapshot directory; defaults to the YAML directory")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="preview only (the default)")
        mode.add_argument("--apply", action="store_true", help="publish YAML and generated snapshots")
        if name == "edit":
            command.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                                 help="set one YAML strategy override; repeat as needed")
    return parser


def _edit_values(values: list[str]) -> dict[str, Any]:
    if not values:
        raise AgentToolError(code="INPUT_ERROR", message="edit requires at least one --set PATH=VALUE")
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item or not item.split("=", 1)[0].strip():
            raise AgentToolError(code="INPUT_ERROR", message=f"invalid --set: {item}; expected PATH=VALUE")
        path, value = item.split("=", 1)
        result[path.strip()] = parse_value(value)
    return result


def _add_values(args: argparse.Namespace) -> dict[str, Any]:
    override = symbol_strategy_override(
        strategy=args.strategy,
        csp_min_strike=args.csp_min_strike,
        csp_max_strike=args.csp_max_strike,
        cc_min_strike=args.cc_min_strike,
        cc_max_strike=args.cc_max_strike,
    )
    return {f"{side}.{key}": value for side, fields in override.items() for key, value in fields.items()}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        add_values = _add_values(args) if args.command == "add" else None
        source = resolve_yaml_config_path(args.config_yaml, repo_root=repo_base())
        if source.suffix.lower() not in {".yaml", ".yml"}:
            raise AgentToolError(code="INPUT_ERROR", message="symbols requires a config.yaml authoring source, not generated JSON")
        document = load_yaml_config_file(source)
        markets = document.get("markets")
        configured = [name for name in ("us", "hk") if isinstance(markets, dict) and name in markets]
        if args.command == "list":
            if not configured:
                raise AgentToolError(code="CONFIG_ERROR", message="config.yaml has no configured markets")
            if not args.market and len(configured) > 1:
                raise AgentToolError(code="INPUT_ERROR", message="list requires --market when multiple markets are configured")
            market = normalize_config_market(args.market or configured[0])
            market_doc = markets.get(market) if isinstance(markets, dict) else None
            if not isinstance(market_doc, dict) or not isinstance(market_doc.get("symbols"), list):
                raise AgentToolError(code="CONFIG_ERROR", message=f"config.yaml missing markets.{market}.symbols")
            runtime, _ = resolve_yaml_runtime_config(repo_root=repo_base(), market=market, config_path=source)
            payload = {
                "market": market, "config_yaml_path": str(source),
                "symbols": market_doc["symbols"], "effective": runtime["symbols"],
            }
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"{market.upper()} 监控标的（{source}）")
                for entry in payload["effective"]:
                    put = bool((entry.get("sell_put") or {}).get("enabled"))
                    call = bool((entry.get("sell_call") or {}).get("enabled"))
                    print(f"  {entry['symbol']}: CSP={'on' if put else 'off'} CC={'on' if call else 'off'}")
            return 0

        calibration = calibrate_symbol(args.symbol, config=document)
        if calibration.status != "ok" or not calibration.market:
            raise AgentToolError(code="INPUT_ERROR", message=calibration.message)
        inferred_market = calibration.market.lower()
        market = normalize_config_market(args.market or inferred_market)
        if market != inferred_market:
            raise AgentToolError(code="INPUT_ERROR", message=f"{args.symbol} belongs to {inferred_market}, not {market}")
        action = {"rm": "remove"}.get(args.command, args.command)
        mutation: dict[str, Any] = {"action": action, "symbol": args.symbol}
        if action == "add":
            mutation["inherit_defaults"] = True
            mutation["set"] = add_values
        if action == "edit":
            mutation["set"] = _edit_values(args.set)
        result = mutate_yaml_symbol_config(
            repo_root=repo_base(), market=market, payload=mutation,
            config_path=source, rebuild_runtime_root=args.rebuild_runtime_root,
            apply=bool(args.apply), expected_source_sha256=args.expected_source_sha256,
        )
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            summary = result["summary"]
            state = "已写入 YAML 并重建快照" if result["write_applied"] else "预览，未写入"
            print(f"{state}：{summary['action']} {summary['canonical_symbol']} · {source}")
            print("共享账户：" + ", ".join(summary["affected_accounts"]))
            if action != "remove":
                entry = summary["after_effective"]
                for label, key in (("CSP", "sell_put"), ("CC", "sell_call")):
                    setting = entry[key]
                    bounds = " ".join(
                        f"{bound}_strike={setting[bound + '_strike']}"
                        for bound in ("min", "max") if bound + "_strike" in setting
                    )
                    print(f"  {label}: {'on' if setting['enabled'] else 'off'}{(' · ' + bounds) if bounds else ''}")
            if not result["write_applied"]:
                print("源版本：" + result["source_revision"]["before_sha256"])
                print("确认后追加 --apply --expected-source-sha256 " + result["source_revision"]["before_sha256"])
            elif result.get("backup_path"):
                print(f"配置备份：{result['backup_path']}")
        return 0
    except AgentToolError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
