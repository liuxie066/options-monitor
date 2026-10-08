from __future__ import annotations

import shlex
import tempfile
import hashlib
import os
from pathlib import Path
from typing import Any

from src.application.account_config import normalize_account_label, parse_lossless_integer
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import MARKETS, dump_yaml as _dump_yaml, normalize_trd_env
from src.application.config_primitives import resolve_config_path as _resolve_path
from src.application.config_yaml import build_yaml_bot_config_file, build_yaml_runtime_config_file, validate_yaml_runtime_config
from src.application.config_yaml_symbols import symbol_strategy_override
from src.application.config_authoring_transaction import _prepare_generation
from src.application.runtime_paths import read_runtime_root_record
from src.application.symbol_calibration import require_calibrated_symbol
from src.application.write_contract import attach_write_contract
from src.infrastructure.io_utils import atomic_write_text


DEFAULT_FUTU_ACCOUNT_ID = "REPLACE_WITH_FUTU_ACCOUNT_ID"


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
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise AgentToolError(code="INPUT_ERROR", message="account label is required; pass --account-label")
    try:
        return normalize_account_label(raw)
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"account label is invalid: {exc}",
        ) from exc


def _normalize_futu_account_id(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_FUTU_ACCOUNT_ID
    if not text.isdigit():
        raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id must be digits only")
    return text


def _normalize_symbols(raw: list[str] | tuple[str, ...] | None, *, market: str) -> list[str]:
    values = [str(item or "").strip().upper() for item in (raw or []) if str(item or "").strip()]
    if not values:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"{market} symbols are required",
            hint=f"Pass at least one --{market}-symbol or enter monitored symbols during setup init.",
        )
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_symbol in values:
        calibrated = require_calibrated_symbol(
            raw_symbol, error_factory=lambda message: AgentToolError(code="INPUT_ERROR", message=message),
        )
        if str(calibrated.market).lower() != market:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol belongs to {calibrated.market}, not {market}")
        symbol = str(calibrated.canonical_symbol)
        if symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(symbol)
    return deduped


def _normalize_symbol_policies(
    raw: dict[str, dict[str, Any]] | None,
    *,
    symbols: list[str],
) -> dict[str, dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AgentToolError(code="INPUT_ERROR", message="symbol policies must be keyed by symbol")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_symbol, fields in raw.items():
        symbol = str(require_calibrated_symbol(
            raw_symbol, error_factory=lambda message: AgentToolError(code="INPUT_ERROR", message=message),
        ).canonical_symbol)
        if symbol in normalized:
            raise AgentToolError(code="INPUT_ERROR", message=f"duplicate symbol policy: {symbol}")
        if not isinstance(fields, dict):
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol policy must be an object: {symbol}")
        unknown = set(fields) - {"strategy", "csp_min_strike", "csp_max_strike", "cc_min_strike", "cc_max_strike"}
        if unknown:
            raise AgentToolError(code="INPUT_ERROR", message=f"unknown symbol policy fields for {symbol}: {', '.join(sorted(unknown))}")
        normalized[symbol] = symbol_strategy_override(
            strategy=fields.get("strategy"),
            csp_min_strike=fields.get("csp_min_strike"),
            csp_max_strike=fields.get("csp_max_strike"),
            cc_min_strike=fields.get("cc_min_strike"),
            cc_max_strike=fields.get("cc_max_strike"),
        )
    missing = set(symbols) - set(normalized)
    extra = set(normalized) - set(symbols)
    if missing or extra:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"symbol policies mismatch: missing={', '.join(sorted(missing)) or '-'}; unknown={', '.join(sorted(extra)) or '-'}",
        )
    return normalized


def _starter_yaml_payload(
    *,
    selected_markets: list[str],
    account_label: str,
    futu_account_id: str,
    us_symbols: list[str],
    hk_symbols: list[str],
    symbol_overrides: dict[str, dict[str, Any]] | None,
    futu_host: str,
    futu_port: int,
    trd_env: str,
) -> dict[str, Any]:
    accounts: dict[str, Any] = {
        account_label: {
            "type": "futu",
            "futu_account_id": futu_account_id,
            "futu": {"host": futu_host, "port": futu_port, "trd_env": trd_env},
        }
    }
    markets: dict[str, Any] = {}
    if "us" in selected_markets:
        us_market: dict[str, Any] = {"accounts": [account_label], "symbols": us_symbols}
        if symbol_overrides is not None:
            us_market["overrides"] = {symbol: symbol_overrides[symbol] for symbol in us_symbols}
        markets["us"] = us_market
    if "hk" in selected_markets:
        hk_market: dict[str, Any] = {"accounts": [account_label], "symbols": hk_symbols}
        if symbol_overrides is not None:
            hk_market["overrides"] = {symbol: symbol_overrides[symbol] for symbol in hk_symbols}
        markets["hk"] = hk_market

    return {
        "accounts": accounts,
        "markets": markets,
        "symbol_defaults": {"fetch": {"host": futu_host, "port": futu_port}},
        "notifications": {"enabled": False},
        "bot": {'enabled': False, 'context_window_messages': 8, 'active_model': 'deepseek-default', 'models': {'deepseek-default': {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-v4-pro', 'api_key_env': 'DEEPSEEK_API_KEY', 'confidence_min': 0.75, 'timeout_seconds': 90, 'context_window_tokens': 1000000}}},
        "inbound": {
            "feishu_ws": {
                "ack_reaction": "THUMBSUP",
            }
        },
    }


def _build_commands(*, config_path: Path, outputs: dict[str, Path], markets: list[str]) -> list[str]:
    commands: list[str] = []
    bot_output = outputs.get("bot")
    if bot_output is not None:
        command = [
            "om",
            "config",
            "build-bot",
            "--source",
            "yaml",
            "--config-yaml",
            str(config_path),
            "--output",
            str(bot_output),
        ]
        commands.append(" ".join(shlex.quote(part) for part in command))
    for market in markets:
        command = [
            "om",
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
    bot_output_config_path: str | Path | None = None,
    markets: list[str] | tuple[str, ...] | None = None,
    futu_acc_id: str | None = None,
    futu_host: str = "127.0.0.1",
    futu_port: int = 11111,
    trd_env: str = "REAL",
    account_label: str | None = None,
    us_symbols: list[str] | tuple[str, ...] | None = None,
    hk_symbols: list[str] | tuple[str, ...] | None = None,
    symbol_policies: dict[str, dict[str, Any]] | None = None,
    build: bool = True,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    selected_markets = _normalize_markets(list(markets) if markets is not None else None)
    account = _normalize_account_label(account_label)
    futu_id = _normalize_futu_account_id(futu_acc_id)
    environment = normalize_trd_env(trd_env)
    host = str(futu_host).strip()
    if not host or any(char.isspace() for char in host):
        raise AgentToolError(code="INPUT_ERROR", message="futu_host must be a non-empty host without whitespace")
    parsed_port = parse_lossless_integer(futu_port)
    if parsed_port is None or not 1 <= parsed_port <= 65535:
        raise AgentToolError(code="INPUT_ERROR", message="futu_port must be between 1 and 65535")
    us_symbol_values = _normalize_symbols(us_symbols, market="us") if "us" in selected_markets else []
    hk_symbol_values = _normalize_symbols(hk_symbols, market="hk") if "hk" in selected_markets else []
    symbol_overrides = _normalize_symbol_policies(symbol_policies, symbols=us_symbol_values + hk_symbol_values)
    output_path = _resolve_path(output_config_yaml_path, default=repo_root / "config.yaml")
    output_dir = _resolve_path(runtime_output_dir, default=output_path.parent)
    runtime_outputs = {
        market: (output_dir / f"config.{market}.json").resolve()
        for market in selected_markets
    }
    bot_output = _resolve_path(
        bot_output_config_path,
        default=output_dir / "config.bot.json",
    )
    all_outputs = {"bot": bot_output, **runtime_outputs}

    if not force:
        existing = [output_path, *(all_outputs.values() if build else [])]
        conflicts = [str(path) for path in existing if path.exists()]
        if conflicts:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="starter config target already exists",
                details={"conflicts": conflicts},
                hint="Pass --force to overwrite generated starter files, or choose a different --output/--runtime-output-dir.",
            )

    yaml_payload = _starter_yaml_payload(
        selected_markets=selected_markets,
        account_label=account,
        futu_account_id=futu_id,
        futu_host=host,
        futu_port=parsed_port,
        trd_env=environment,
        us_symbols=us_symbol_values,
        hk_symbols=hk_symbol_values,
        symbol_overrides=symbol_overrides,
    )
    yaml_text = _dump_yaml(yaml_payload)
    validation: dict[str, Any] = {}
    build_results: dict[str, Any] = {}

    # Exercise the same parser, market resolver and generated-config builders
    # against a disposable candidate before the canonical file can be replaced.
    with tempfile.TemporaryDirectory(prefix="options-monitor-config-init-") as temp_name:
        staged_yaml = Path(temp_name) / "config.yaml"
        atomic_write_text(staged_yaml, yaml_text, encoding="utf-8")
        for market in selected_markets:
            validate_yaml_runtime_config(
                repo_root=repo_root,
                market=market,
                config_path=staged_yaml,
            )
            if build:
                build_yaml_runtime_config_file(
                    repo_root=repo_root,
                    market=market,
                    config_path=staged_yaml,
                    output_config_path=Path(temp_name) / f"config.{market}.json",
                    dry_run=True,
                )
        if build:
            build_yaml_bot_config_file(
                repo_root=repo_root,
                config_path=staged_yaml,
                output_config_path=Path(temp_name) / "config.bot.json",
                dry_run=True,
            )

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
            if build and "bot" not in build_results:
                build_results["bot"] = build_yaml_bot_config_file(
                    repo_root=repo_root,
                    config_path=output_path,
                    output_config_path=bot_output,
                    dry_run=False,
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
        "market_symbols": {market: us_symbol_values if market == "us" else hk_symbol_values for market in selected_markets},
        "symbol_policies": symbol_overrides,
        "account_label": account,
        "futu_host": host, "futu_port": parsed_port, "trd_env": environment,
        "ready": False,
        "futu_account_id_placeholder": futu_id == DEFAULT_FUTU_ACCOUNT_ID,
        "runtime_output_dir": str(output_dir),
        "runtime_config_paths": {market: str(path) for market, path in runtime_outputs.items()},
        "bot_config_path": str(bot_output),
        "validation": validation,
        "build": build_results,
        "build_enabled": bool(build),
        "yaml": yaml_text,
        "next_steps": [
            *(f"om config validate --source yaml --market {market} --config-yaml {shlex.quote(str(output_path))}" for market in selected_markets),
            *(_build_commands(config_path=output_path, outputs=all_outputs, markets=selected_markets) if build else []),
        ],
    }
    return attach_write_contract(
        data,
        dry_run=bool(dry_run),
        write_applied=not bool(dry_run),
        rollback_hint=f"delete {output_path} and generated config.<market>.json files under {output_dir}",
    )


def create_starter_config(*, record_path: Path, **options: Any) -> dict[str, Any]:
    """Publish a new setup generation without replacing any existing target."""
    preview = init_yaml_config(**options, dry_run=True)
    source = Path(preview["config_yaml_path"])
    runtime = Path(preview["runtime_output_dir"])
    targets = [source, Path(preview["bot_config_path"]),
               *(Path(preview["runtime_config_paths"][m]) for m in preview["markets"])]
    record_exists = record_path.exists() or record_path.is_symlink()
    if record_exists and read_runtime_root_record(record_path, require_config=False) != runtime:
        raise AgentToolError(code="CONFIG_ERROR", message="runtime root record points to another directory",
                             details={"record_path": str(record_path)},
                             hint="Inspect the existing runtime-root record before choosing a new directory.")
    conflicts = [str(path) for path in targets if path.exists() or path.is_symlink()]
    if conflicts:
        raise AgentToolError(code="CONFIG_ERROR", message="starter config target already exists",
                             details={"conflicts": conflicts}, hint="Inspect these files or choose another output directory.")

    source_bytes = preview["yaml"].encode("utf-8")
    prepared = _prepare_generation(
        repo_root=Path(options["repo_root"]), source_path=source, source_bytes=source_bytes,
        runtime_root=runtime, markets=preview["markets"], include_bot=True,
    )
    payloads = [(Path(item["path"]), item["payload"]) for item in prepared["target_payloads"]]
    payloads.append((source, source_bytes))
    if not record_exists:
        payloads.append((record_path, (str(runtime) + "\n").encode("utf-8")))

    created: list[tuple[Path, int, int, str]] = []
    try:
        for path, payload in payloads:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".om-init-", dir=path.parent, delete=False) as staged:
                staged_path = Path(staged.name)
                try:
                    os.fchmod(staged.fileno(), 0o600)
                    staged.write(payload)
                    staged.flush()
                    os.fsync(staged.fileno())
                except BaseException:
                    staged_path.unlink(missing_ok=True)
                    raise
            try:
                identity = staged_path.stat()
                os.link(staged_path, path)
                created.append((path, identity.st_dev, identity.st_ino, hashlib.sha256(payload).hexdigest()))
            finally:
                staged_path.unlink(missing_ok=True)
        for path, payload in payloads:
            if path.read_bytes() != payload:
                raise OSError(f"published file changed: {path}")
        if read_runtime_root_record(record_path) != runtime:
            raise OSError(f"runtime root record changed: {record_path}")
        from src.application.agent_tool_config import load_runtime_config
        from src.application.runtime_config_readiness import evaluate_runtime_config_readiness
        for market in preview["markets"]:
            path, config = load_runtime_config(config_key=market, config_path=runtime / f"config.{market}.json")
            readiness = evaluate_runtime_config_readiness(config, repo_root=Path(options["repo_root"]),
                                                          runtime_config_path=path, explicit_market=market, config_key=market)
            if not readiness["freshness"]["ok"] or not readiness["identity"]["ok"]:
                raise OSError(f"published market config is stale or invalid: {path}")
    except BaseException as exc:
        preserved: list[str] = []
        for path, device, inode, digest in reversed(created):
            try:
                identity = path.lstat()
                if (identity.st_dev, identity.st_ino) == (device, inode) and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    path.unlink()
                else:
                    preserved.append(str(path))
            except OSError:
                preserved.append(str(path))
        raise AgentToolError(
            code="CONFIG_WRITE_FAILED", message="failed to create starter config",
            details={"error": f"{type(exc).__name__}: {exc}", "preserved": preserved},
            hint="Inspect preserved files before retrying; setup init never overwrites existing targets.",
        ) from exc
    return {**preview, "dry_run": False, "write_applied": True, "rollback_hint": None,
            "runtime_root_record_path": str(record_path)}


__all__ = ["init_yaml_config", "create_starter_config"]
