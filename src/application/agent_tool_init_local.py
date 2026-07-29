from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.account_config import ACCOUNT_TYPE_FUTU, normalize_accounts
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_config
from src.application.layered_config import build_layered_runtime_config_from_user_config
from src.application.runtime_config_freshness import GENERATED_KEY, build_inline_generated_metadata


DEFAULT_SYMBOLS = {
    "us": "NVDA",
    "hk": "0700.HK",
}


def _example_data_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "configs" / "examples" / "portfolio.runtime.example.json").resolve()


def default_runtime_config_path(*, repo_root: Path, market: str) -> Path:
    return (repo_root / f"config.{market}.json").resolve()


def default_data_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "portfolio.runtime.json").resolve()


def _normalize_market(value: str) -> str:
    market = str(value or "").strip().lower()
    if market not in {"us", "hk"}:
        raise AgentToolError(code="INPUT_ERROR", message="market must be us or hk")
    return market


def _normalize_account_label(value: str | None) -> str:
    accounts = normalize_accounts([value or "user1"], fallback=("user1",))
    return accounts[0]


def _normalize_futu_acc_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id is required")
    if not raw.isdigit():
        raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id must be digits only")
    return raw


def _normalize_symbols(value: list[str] | tuple[str, ...] | None, *, market: str) -> list[str]:
    items = [str(x or "").strip().upper() for x in (value or []) if str(x or "").strip()]
    if items:
        return items
    return [DEFAULT_SYMBOLS[market]]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse template JSON: {path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"template must be a JSON object: {path.name}")
    return payload


def _validate_runtime_config_or_raise(cfg: dict[str, Any]) -> None:
    try:
        validate_config(deepcopy(cfg))
    except SystemExit as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc


def _relative_or_absolute(*, base_dir: Path, target: Path) -> str:
    try:
        return str(target.relative_to(base_dir))
    except ValueError:
        return str(target)


def init_local_config(
    *,
    repo_root: Path,
    market: str,
    futu_acc_id: str,
    account_label: str = "user1",
    symbols: list[str] | tuple[str, ...] | None = None,
    config_path: str | Path | None = None,
    data_config_path: str | Path | None = None,
    holdings_account: str | None = None,
    opend_host: str = "127.0.0.1",
    opend_port: int = 11111,
    force: bool = False,
) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    raw_account_label = str(account_label or "").strip()
    normalized_account = _normalize_account_label(account_label)
    normalized_acc_id = _normalize_futu_acc_id(futu_acc_id)
    normalized_symbols = _normalize_symbols(symbols, market=normalized_market)
    used_defaults: list[str] = []
    warnings: list[str] = []
    if not raw_account_label:
        used_defaults.append("account_label")
    if not list(symbols or []):
        used_defaults.append("symbols")
    if data_config_path is None:
        used_defaults.append("data_config")

    data_template_path = _example_data_config_path(repo_root=repo_root)
    data_cfg = _read_json_object(data_template_path)

    target_config_path = (
        Path(config_path).expanduser().resolve()
        if config_path
        else default_runtime_config_path(repo_root=repo_root, market=normalized_market)
    )
    target_data_config_path = (
        Path(data_config_path).expanduser().resolve()
        if data_config_path
        else default_data_config_path(repo_root=repo_root)
    )

    if target_config_path.exists() and not force:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"config already exists: {target_config_path.name}",
            hint="Pass --force to overwrite it.",
        )
    reuse_existing_data_config = target_data_config_path.exists() and not force
    if reuse_existing_data_config:
        _read_json_object(target_data_config_path)

    opend_host_value = str(opend_host).strip() or "127.0.0.1"
    opend_port_value = int(opend_port)
    data_config_ref = _relative_or_absolute(
        base_dir=target_config_path.parent,
        target=target_data_config_path,
    )
    account_setting: dict[str, Any] = {
        "type": ACCOUNT_TYPE_FUTU,
        "futu": {
            "account_id": normalized_acc_id,
            "host": opend_host_value,
            "port": opend_port_value,
        },
    }
    if str(holdings_account or "").strip():
        account_setting["holdings_account"] = str(holdings_account).strip()

    user_cfg: dict[str, Any] = {
        "account_settings": {
            normalized_account: account_setting,
        },
        "portfolio": {
            "account": normalized_account,
            "data_config": data_config_ref,
            "futu": {
                "host": opend_host_value,
                "port": opend_port_value,
            },
        },
        "trade_intake": {
            "mode": "dry-run",
        },
        "symbols": [
            {
                "symbol": symbol,
                "fetch": {
                    "host": opend_host_value,
                    "port": opend_port_value,
                },
            }
            for symbol in normalized_symbols
        ],
    }
    runtime_cfg, _meta = build_layered_runtime_config_from_user_config(
        repo_root=repo_root,
        market=normalized_market,
        user_config=user_cfg,
        user_config_ref="init_local_config",
    )
    runtime_cfg[GENERATED_KEY] = build_inline_generated_metadata(
        repo_root=repo_root,
        market=normalized_market,
        system_config_path=(repo_root / "configs" / "system.json").resolve(),
        user_config=user_cfg,
        user_config_ref="init_local_config",
    )

    _validate_runtime_config_or_raise(runtime_cfg)
    target_config_path.parent.mkdir(parents=True, exist_ok=True)
    target_data_config_path.parent.mkdir(parents=True, exist_ok=True)
    target_config_path.write_text(
        json.dumps(runtime_cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not reuse_existing_data_config or force:
        target_data_config_path.write_text(
            json.dumps(data_cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if used_defaults:
        warnings.append(
            "Starter defaults applied: "
            + ", ".join(used_defaults)
            + ". Review account label, symbols, and local data_config before long-term use."
        )

    return {
        "market": normalized_market,
        "account_label": normalized_account,
        "futu_acc_id_masked": f"...{normalized_acc_id[-4:]}",
        **(
            {"holdings_account": str(holdings_account).strip()}
            if str(holdings_account or "").strip()
            else {}
        ),
        "symbols": normalized_symbols,
        "config_path": str(target_config_path),
        "data_config_path": str(target_data_config_path),
        "data_config_reused": reuse_existing_data_config,
        "used_defaults": used_defaults,
        "warnings": warnings,
        "opend": {
            "host": opend_host_value,
            "port": opend_port_value,
        },
        "next_steps": [
            f"./om-agent run --tool healthcheck --input-json '{{\"config_path\":\"{target_config_path}\"}}'",
            f"./om-agent run --tool scan_opportunities --input-json '{{\"config_path\":\"{target_config_path}\"}}'",
            f"./om-agent run --tool get_close_advice --input-json '{{\"config_path\":\"{target_config_path}\"}}'",
        ],
        "recommended_flow": ["healthcheck", "scan_opportunities", "get_close_advice"],
    }


def add_account_to_local_config(
    *,
    repo_root: Path,
    market: str,
    account_label: str,
    account_type: str,
    config_path: str | Path | None = None,
    futu_acc_id: str | None = None,
    holdings_account: str | None = None,
    market_label: str | None = None,
    enabled: bool | None = None,
    trade_intake_enabled: bool | None = None,
    futu_host: str | None = None,
    futu_port: int | None = None,
    bitable_app_token: str | None = None,
    bitable_table_id: str | None = None,
    bitable_view_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    from src.application.config_yaml_accounts import mutate_yaml_account_config

    return mutate_yaml_account_config(
        repo_root=repo_root,
        action="add",
        market=market,
        account_label=account_label,
        account_type=account_type,
        config_path=config_path,
        futu_acc_id=futu_acc_id,
        holdings_account=holdings_account,
        market_label=market_label,
        enabled=enabled,
        trade_intake_enabled=trade_intake_enabled,
        futu_host=futu_host,
        futu_port=futu_port,
        bitable_app_token=bitable_app_token,
        bitable_table_id=bitable_table_id,
        bitable_view_name=bitable_view_name,
        apply=not bool(dry_run),
    )


def edit_account_in_local_config(
    *,
    repo_root: Path,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
    account_type: str | None = None,
    futu_acc_id: str | None = None,
    holdings_account: str | None = None,
    clear_holdings_account: bool = False,
    market_label: str | None = None,
    enabled: bool | None = None,
    trade_intake_enabled: bool | None = None,
    futu_host: str | None = None,
    futu_port: int | None = None,
    bitable_app_token: str | None = None,
    bitable_table_id: str | None = None,
    bitable_view_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    from src.application.config_yaml_accounts import mutate_yaml_account_config

    return mutate_yaml_account_config(
        repo_root=repo_root,
        action="edit",
        market=market,
        account_label=account_label,
        account_type=account_type,
        config_path=config_path,
        futu_acc_id=futu_acc_id,
        holdings_account=holdings_account,
        clear_holdings_account=clear_holdings_account,
        market_label=market_label,
        enabled=enabled,
        trade_intake_enabled=trade_intake_enabled,
        futu_host=futu_host,
        futu_port=futu_port,
        bitable_app_token=bitable_app_token,
        bitable_table_id=bitable_table_id,
        bitable_view_name=bitable_view_name,
        apply=not bool(dry_run),
    )


def remove_account_from_local_config(
    *,
    repo_root: Path,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    from src.application.config_yaml_accounts import mutate_yaml_account_config

    return mutate_yaml_account_config(
        repo_root=repo_root,
        action="remove",
        market=market,
        account_label=account_label,
        config_path=config_path,
        apply=not bool(dry_run),
    )
