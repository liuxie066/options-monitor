from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml import build_yaml_runtime_config_file, validate_yaml_runtime_config
from src.application.layered_config import MARKETS
from src.application.write_contract import attach_write_contract
from src.infrastructure.io_utils import atomic_write_text


DEFAULT_US_SYMBOLS = ("NVDA", "FUTU", "GOOGL")
DEFAULT_HK_SYMBOLS = ("0700.HK", "9992.HK")
DEFAULT_FUTU_ACCOUNT_ID = "REPLACE_WITH_FUTU_ACCOUNT_ID"


class _IndentedYamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _resolve_path(raw: str | Path | None, *, default: Path) -> Path:
    if raw is None or not str(raw).strip():
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def _normalize_markets(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    values = [str(item or "").strip().lower() for item in (raw or ["all"])]
    out: list[str] = []
    for item in values:
        if item == "all":
            for market in MARKETS:
                if market not in out:
                    out.append(market)
            continue
        if item not in MARKETS:
            raise AgentToolError(code="INPUT_ERROR", message="market must be us, hk, or all")
        if item not in out:
            out.append(item)
    return out or list(MARKETS)


def _normalize_account_label(raw: str | None) -> str:
    account = str(raw or "lx").strip().lower()
    if not account:
        raise AgentToolError(code="INPUT_ERROR", message="account label must be non-empty")
    return account


def _normalize_futu_account_id(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_FUTU_ACCOUNT_ID
    if not text.isdigit():
        raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id must be digits only")
    return text


def _normalize_symbols(raw: list[str] | tuple[str, ...] | None, *, defaults: tuple[str, ...]) -> list[str]:
    values = [str(item or "").strip().upper() for item in (raw or []) if str(item or "").strip()]
    out = values or list(defaults)
    seen: set[str] = set()
    deduped: list[str] = []
    for symbol in out:
        if symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(symbol)
    return deduped


def _dump_yaml(payload: dict[str, Any]) -> str:
    text = yaml.dump(
        payload,
        Dumper=_IndentedYamlDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        indent=2,
        width=100,
    )
    return text.rstrip() + "\n"


def _starter_yaml_payload(
    *,
    account_label: str,
    futu_account_id: str,
    external_holdings_account: str | None,
    us_symbols: list[str],
    hk_symbols: list[str],
) -> dict[str, Any]:
    accounts: dict[str, Any] = {
        account_label: {
            "type": "futu",
            "futu_account_id": futu_account_id,
        }
    }
    us_accounts = [account_label]
    external_account = str(external_holdings_account or "").strip().lower()
    if external_account:
        accounts[external_account] = {
            "type": "external_holdings",
            "holdings_account": external_account,
        }
        us_accounts.append(external_account)

    us_market: dict[str, Any] = {
        "accounts": us_accounts,
        "symbols": us_symbols,
    }
    if "FUTU" in us_symbols:
        us_market["overrides"] = {
            "FUTU": {
                "sell_put": {
                    "dte": [20, 45],
                    "strike": [55, 85],
                }
            }
        }

    return {
        "accounts": accounts,
        "markets": {
            "us": us_market,
            "hk": {
                "accounts": [account_label],
                "symbols": hk_symbols,
            },
        },
        "agent": {
            "runtime": {
                "enabled": False,
                "context_window_messages": 8,
            },
            "llm": {
                "enabled": False,
                "provider": "",
                "model": "",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        },
        "inbound": {
            "feishu_ws": {
                "ack_reaction": "THUMBSUP",
            }
        },
    }


def _build_commands(*, config_path: Path, outputs: dict[str, Path], markets: list[str]) -> list[str]:
    commands: list[str] = []
    for market in markets:
        command = [
            "./om",
            "config",
            "build",
            "--source",
            "yaml",
            "--market",
            market,
            "--config-yaml",
            str(config_path),
            "--output",
            str(outputs[market]),
        ]
        commands.append(" ".join(shlex.quote(part) for part in command))
    return commands


def init_yaml_config(
    *,
    repo_root: Path,
    output_config_yaml_path: str | Path | None = None,
    runtime_output_dir: str | Path | None = None,
    markets: list[str] | tuple[str, ...] | None = None,
    futu_acc_id: str | None = None,
    account_label: str | None = None,
    external_holdings_account: str | None = "sy",
    us_symbols: list[str] | tuple[str, ...] | None = None,
    hk_symbols: list[str] | tuple[str, ...] | None = None,
    build: bool = True,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    selected_markets = _normalize_markets(list(markets) if markets is not None else None)
    account = _normalize_account_label(account_label)
    futu_id = _normalize_futu_account_id(futu_acc_id)
    us_symbol_values = _normalize_symbols(us_symbols, defaults=DEFAULT_US_SYMBOLS)
    hk_symbol_values = _normalize_symbols(hk_symbols, defaults=DEFAULT_HK_SYMBOLS)
    output_path = _resolve_path(output_config_yaml_path, default=repo_root / "config.yaml")
    output_dir = _resolve_path(runtime_output_dir, default=output_path.parent)
    runtime_outputs = {
        market: (output_dir / f"config.{market}.json").resolve()
        for market in selected_markets
    }

    if not force:
        existing = [output_path, *(runtime_outputs.values() if build else [])]
        conflicts = [str(path) for path in existing if path.exists()]
        if conflicts:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="starter config target already exists",
                details={"conflicts": conflicts},
                hint="Pass --force to overwrite generated starter files, or choose a different --output/--runtime-output-dir.",
            )

    yaml_payload = _starter_yaml_payload(
        account_label=account,
        futu_account_id=futu_id,
        external_holdings_account=external_holdings_account,
        us_symbols=us_symbol_values,
        hk_symbols=hk_symbol_values,
    )
    yaml_text = _dump_yaml(yaml_payload)
    validation: dict[str, Any] = {}
    build_results: dict[str, Any] = {}

    if dry_run:
        validation = {
            market: {
                "ok": True,
                "source_format": "yaml",
                "planned_config_yaml_path": str(output_path),
                "planned_output_config_path": str(runtime_outputs[market]),
            }
            for market in selected_markets
        }
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_path, yaml_text, encoding="utf-8")
        for market in selected_markets:
            validation[market] = validate_yaml_runtime_config(
                repo_root=repo_root,
                market=market,
                config_path=output_path,
            )
            if build:
                build_results[market] = build_yaml_runtime_config_file(
                    repo_root=repo_root,
                    market=market,
                    config_path=output_path,
                    output_config_path=runtime_outputs[market],
                    dry_run=False,
                )

    data = {
        "ok": True,
        "source_format": "yaml",
        "config_yaml_path": str(output_path),
        "markets": selected_markets,
        "account_label": account,
        "futu_account_id_placeholder": futu_id == DEFAULT_FUTU_ACCOUNT_ID,
        "runtime_output_dir": str(output_dir),
        "runtime_config_paths": {market: str(path) for market, path in runtime_outputs.items()},
        "validation": validation,
        "build": build_results,
        "build_enabled": bool(build),
        "yaml": yaml_text,
        "next_steps": [
            *(f"./om config validate --source yaml --market {market} --config-yaml {shlex.quote(str(output_path))}" for market in selected_markets),
            *(_build_commands(config_path=output_path, outputs=runtime_outputs, markets=selected_markets) if build else []),
        ],
    }
    return attach_write_contract(
        data,
        dry_run=bool(dry_run),
        write_applied=not bool(dry_run),
        rollback_hint=f"delete {output_path} and generated config.<market>.json files under {output_dir}",
    )


__all__ = ["init_yaml_config"]
