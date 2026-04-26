from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.account_config import ACCOUNT_TYPE_EXTERNAL_HOLDINGS, ACCOUNT_TYPE_FUTU, normalize_accounts
from scripts.agent_plugin.contracts import AgentToolError
from scripts.validate_config import validate_config


DEFAULT_SYMBOLS = {
    "us": "NVDA",
    "hk": "0700.HK",
}


def _example_runtime_config_path(*, repo_root: Path, market: str) -> Path:
    return (repo_root / "configs" / "examples" / f"config.example.{market}.json").resolve()


def _example_data_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "configs" / "examples" / "portfolio.sqlite.example.json").resolve()


def default_runtime_config_path(*, repo_root: Path, market: str) -> Path:
    return (repo_root / f"config.{market}.json").resolve()


def default_data_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "secrets" / "portfolio.sqlite.json").resolve()


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


def _normalize_account_type(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw not in {ACCOUNT_TYPE_FUTU, ACCOUNT_TYPE_EXTERNAL_HOLDINGS}:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="account_type must be futu or external_holdings",
        )
    return raw


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


def _build_symbols(template_cfg: dict[str, Any], *, market: str, symbols: list[str], host: str, port: int) -> list[dict[str, Any]]:
    template_symbols = template_cfg.get("symbols")
    if not isinstance(template_symbols, list) or not template_symbols:
        raise AgentToolError(code="CONFIG_ERROR", message="runtime config template must contain at least one symbol")
    template_symbol = template_symbols[0]
    if not isinstance(template_symbol, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="runtime config template symbol must be an object")

    market_name = market.upper()
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        row = deepcopy(template_symbol)
        row["symbol"] = symbol
        row.pop("market", None)
        row["broker"] = market_name
        fetch = row.get("fetch")
        if not isinstance(fetch, dict):
            fetch = {}
        fetch["source"] = "futu"
        fetch["host"] = str(host)
        fetch["port"] = int(port)
        row["fetch"] = fetch
        rows.append(row)
    return rows


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
    normalized_account = _normalize_account_label(account_label)
    normalized_acc_id = _normalize_futu_acc_id(futu_acc_id)
    normalized_symbols = _normalize_symbols(symbols, market=normalized_market)

    runtime_template_path = _example_runtime_config_path(repo_root=repo_root, market=normalized_market)
    data_template_path = _example_data_config_path(repo_root=repo_root)
    runtime_cfg = _read_json_object(runtime_template_path)
    data_cfg = _read_json_object(data_template_path)

    target_config_path = Path(config_path).expanduser().resolve() if config_path else default_runtime_config_path(repo_root=repo_root, market=normalized_market)
    target_data_config_path = Path(data_config_path).expanduser().resolve() if data_config_path else default_data_config_path(repo_root=repo_root)

    if target_config_path.exists() and not force:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"config already exists: {target_config_path.name}",
            hint="Pass --force to overwrite it.",
        )
    reuse_existing_data_config = target_data_config_path.exists() and not force
    if reuse_existing_data_config:
        _read_json_object(target_data_config_path)

    runtime_cfg["accounts"] = [normalized_account]
    runtime_cfg["account_settings"] = {
        normalized_account: {
            "type": ACCOUNT_TYPE_FUTU,
            **({"holdings_account": str(holdings_account).strip()} if str(holdings_account or "").strip() else {}),
        }
    }
    portfolio = runtime_cfg.get("portfolio")
    if not isinstance(portfolio, dict):
        portfolio = {}
    portfolio["account"] = normalized_account
    portfolio["broker"] = "富途"
    portfolio["source"] = "futu"
    portfolio["source_by_account"] = {normalized_account: "futu"}
    portfolio["data_config"] = _relative_or_absolute(
        base_dir=target_config_path.parent,
        target=target_data_config_path,
    )
    runtime_cfg["portfolio"] = portfolio

    trade_intake = runtime_cfg.get("trade_intake")
    if not isinstance(trade_intake, dict):
        trade_intake = {}
    trade_intake["enabled"] = True
    account_mapping = trade_intake.get("account_mapping")
    if not isinstance(account_mapping, dict):
        account_mapping = {}
    account_mapping["futu"] = {normalized_acc_id: normalized_account}
    trade_intake["account_mapping"] = account_mapping
    runtime_cfg["trade_intake"] = trade_intake
    runtime_cfg["symbols"] = _build_symbols(
        runtime_cfg,
        market=normalized_market,
        symbols=normalized_symbols,
        host=str(opend_host).strip() or "127.0.0.1",
        port=int(opend_port),
    )

    _validate_runtime_config_or_raise(runtime_cfg)
    target_config_path.parent.mkdir(parents=True, exist_ok=True)
    target_data_config_path.parent.mkdir(parents=True, exist_ok=True)
    target_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not reuse_existing_data_config or force:
        target_data_config_path.write_text(json.dumps(data_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "market": normalized_market,
        "account_label": normalized_account,
        "futu_acc_id_masked": f"...{normalized_acc_id[-4:]}",
        **({"holdings_account": str(holdings_account).strip()} if str(holdings_account or "").strip() else {}),
        "symbols": normalized_symbols,
        "config_path": str(target_config_path),
        "data_config_path": str(target_data_config_path),
        "data_config_reused": reuse_existing_data_config,
        "opend": {
            "host": str(opend_host).strip() or "127.0.0.1",
            "port": int(opend_port),
        },
        "next_steps": [
            f"./om-agent run --tool healthcheck --input-json '{{\"config_path\":\"{target_config_path}\"}}'",
            f"./om-agent run --tool scan_opportunities --input-json '{{\"config_path\":\"{target_config_path}\"}}'",
            f"./om-agent run --tool get_close_advice --input-json '{{\"config_path\":\"{target_config_path}\"}}'",
            "./run_webui.sh",
        ],
        "recommended_flow": ["healthcheck", "scan_opportunities", "get_close_advice"],
    }


def _load_runtime_config_for_update(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"runtime config not found: {path.name}",
        )
    return _read_json_object(path)


def _target_runtime_config_path(*, repo_root: Path, market: str, config_path: str | Path | None) -> Path:
    normalized_market = _normalize_market(market)
    return Path(config_path).expanduser().resolve() if config_path else default_runtime_config_path(repo_root=repo_root, market=normalized_market)


def _require_existing_account(runtime_cfg: dict[str, Any], *, account_label: str) -> tuple[list[str], str]:
    accounts = normalize_accounts(runtime_cfg.get("accounts"), fallback=())
    normalized_account = _normalize_account_label(account_label)
    if normalized_account not in accounts:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"account not found: {normalized_account}",
        )
    return accounts, normalized_account


def _resolve_futu_mapping(runtime_cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    trade_intake = runtime_cfg.get("trade_intake")
    if not isinstance(trade_intake, dict):
        trade_intake = {}
    account_mapping = trade_intake.get("account_mapping")
    if not isinstance(account_mapping, dict):
        account_mapping = {}
    futu_mapping = account_mapping.get("futu")
    if not isinstance(futu_mapping, dict):
        futu_mapping = {}
    return trade_intake, account_mapping, futu_mapping


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
) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    normalized_account = _normalize_account_label(account_label)
    normalized_type = _normalize_account_type(account_type)
    target_config_path = _target_runtime_config_path(repo_root=repo_root, market=normalized_market, config_path=config_path)
    runtime_cfg = _load_runtime_config_for_update(target_config_path)

    accounts = normalize_accounts(runtime_cfg.get("accounts"), fallback=())
    if normalized_account in accounts:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"account already exists: {normalized_account}",
        )
    runtime_cfg["accounts"] = [*accounts, normalized_account]

    account_settings = runtime_cfg.get("account_settings")
    if not isinstance(account_settings, dict):
        account_settings = {}
    setting: dict[str, Any] = {"type": normalized_type}
    normalized_market_label = str(market_label or normalized_market).strip().lower()
    if normalized_market_label in {"us", "hk"}:
        setting["market"] = normalized_market_label
    setting["enabled"] = True if enabled is None else bool(enabled)
    setting["trade_intake_enabled"] = (
        (normalized_type == ACCOUNT_TYPE_FUTU) if trade_intake_enabled is None else bool(trade_intake_enabled)
    )
    holdings_value = str(holdings_account or "").strip()
    if normalized_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS:
        holdings_value = holdings_value or normalized_account
        if not holdings_value:
            raise AgentToolError(
                code="INPUT_ERROR",
                message="holdings_account must be a non-empty string for external_holdings",
            )
    if holdings_value:
        setting["holdings_account"] = holdings_value
    if normalized_type == ACCOUNT_TYPE_FUTU:
        futu_cfg: dict[str, Any] = {}
        host = str(futu_host or "").strip()
        if host:
            futu_cfg["host"] = host
        if futu_port not in (None, ""):
            futu_cfg["port"] = int(futu_port)
        if futu_acc_id is not None and str(futu_acc_id).strip():
            futu_cfg["account_id"] = str(futu_acc_id).strip()
        if futu_cfg:
            setting["futu"] = futu_cfg
    else:
        bitable_cfg: dict[str, Any] = {}
        for key, value in {
            "app_token": bitable_app_token,
            "table_id": bitable_table_id,
            "view_name": bitable_view_name,
        }.items():
            raw = str(value or "").strip()
            if raw:
                bitable_cfg[key] = raw
        if bitable_cfg:
            setting["bitable"] = bitable_cfg
    account_settings[normalized_account] = setting
    runtime_cfg["account_settings"] = account_settings

    portfolio = runtime_cfg.get("portfolio")
    if not isinstance(portfolio, dict):
        portfolio = {}
    source_by_account = portfolio.get("source_by_account")
    if not isinstance(source_by_account, dict):
        source_by_account = {}
    source_by_account[normalized_account] = ("futu" if normalized_type == ACCOUNT_TYPE_FUTU else "holdings")
    portfolio["source_by_account"] = source_by_account
    runtime_cfg["portfolio"] = portfolio

    trade_intake, account_mapping, futu_mapping = _resolve_futu_mapping(runtime_cfg)

    if normalized_type == ACCOUNT_TYPE_FUTU:
        normalized_acc_id = _normalize_futu_acc_id(futu_acc_id)
        if normalized_acc_id in futu_mapping:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"futu acc_id already exists: ...{normalized_acc_id[-4:]}",
            )
        futu_mapping[normalized_acc_id] = normalized_account
        account_mapping["futu"] = futu_mapping
        trade_intake["enabled"] = True
    trade_intake["account_mapping"] = account_mapping
    runtime_cfg["trade_intake"] = trade_intake

    _validate_runtime_config_or_raise(runtime_cfg)
    target_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result: dict[str, Any] = {
        "market": normalized_market,
        "account_label": normalized_account,
        "account_type": normalized_type,
        "config_path": str(target_config_path),
        "accounts": runtime_cfg["accounts"],
    }
    if holdings_value:
        result["holdings_account"] = holdings_value
    if normalized_type == ACCOUNT_TYPE_FUTU:
        result["futu_acc_id_masked"] = f"...{str(futu_acc_id or '')[-4:]}"
    return result


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
) -> dict[str, Any]:
    target_config_path = _target_runtime_config_path(repo_root=repo_root, market=market, config_path=config_path)
    runtime_cfg = _load_runtime_config_for_update(target_config_path)
    accounts, normalized_account = _require_existing_account(runtime_cfg, account_label=account_label)

    account_settings = runtime_cfg.get("account_settings")
    if not isinstance(account_settings, dict):
        account_settings = {}
    current_setting = account_settings.get(normalized_account)
    if not isinstance(current_setting, dict):
        current_setting = {"type": ACCOUNT_TYPE_FUTU}
    current_type = _normalize_account_type(current_setting.get("type"))
    new_type = _normalize_account_type(account_type) if account_type is not None else current_type

    trade_intake, account_mapping, futu_mapping = _resolve_futu_mapping(runtime_cfg)
    existing_acc_ids = [key for key, value in futu_mapping.items() if str(value or "").strip().lower() == normalized_account]

    setting: dict[str, Any] = {"type": new_type}
    current_market = str(current_setting.get("market") or "").strip().lower()
    normalized_market_label = str(market_label or current_market or _normalize_market(market)).strip().lower()
    if normalized_market_label in {"us", "hk"}:
        setting["market"] = normalized_market_label
    if enabled is None:
        if "enabled" in current_setting:
            setting["enabled"] = bool(current_setting.get("enabled"))
    else:
        setting["enabled"] = bool(enabled)
    if trade_intake_enabled is None:
        if "trade_intake_enabled" in current_setting:
            setting["trade_intake_enabled"] = bool(current_setting.get("trade_intake_enabled"))
        else:
            setting["trade_intake_enabled"] = new_type == ACCOUNT_TYPE_FUTU
    else:
        setting["trade_intake_enabled"] = bool(trade_intake_enabled)
    if clear_holdings_account:
        pass
    elif holdings_account is not None:
        holdings_value = str(holdings_account).strip()
        if holdings_value:
            setting["holdings_account"] = holdings_value
    elif str(current_setting.get("holdings_account") or "").strip():
        setting["holdings_account"] = str(current_setting.get("holdings_account")).strip()

    if new_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS and not str(setting.get("holdings_account") or "").strip():
        setting["holdings_account"] = normalized_account

    if new_type == ACCOUNT_TYPE_FUTU:
        futu_cfg = current_setting.get("futu") if isinstance(current_setting.get("futu"), dict) else {}
        merged_futu: dict[str, Any] = dict(futu_cfg)
        if futu_host is not None:
            host = str(futu_host).strip()
            if host:
                merged_futu["host"] = host
            else:
                merged_futu.pop("host", None)
        if futu_port is not None:
            merged_futu["port"] = int(futu_port)
        if futu_acc_id is not None and str(futu_acc_id).strip():
            merged_futu["account_id"] = str(futu_acc_id).strip()
        if merged_futu:
            setting["futu"] = merged_futu
    else:
        bitable_cfg = current_setting.get("bitable") if isinstance(current_setting.get("bitable"), dict) else {}
        merged_bitable: dict[str, Any] = dict(bitable_cfg)
        for key, value in {
            "app_token": bitable_app_token,
            "table_id": bitable_table_id,
            "view_name": bitable_view_name,
        }.items():
            if value is None:
                continue
            raw = str(value).strip()
            if raw:
                merged_bitable[key] = raw
            else:
                merged_bitable.pop(key, None)
        if merged_bitable:
            setting["bitable"] = merged_bitable

    for acc_id in existing_acc_ids:
        futu_mapping.pop(acc_id, None)

    normalized_acc_id: str | None = None
    if new_type == ACCOUNT_TYPE_FUTU:
        raw_acc_id = futu_acc_id
        if raw_acc_id is None:
            if len(existing_acc_ids) == 1:
                raw_acc_id = existing_acc_ids[0]
            elif len(existing_acc_ids) > 1:
                raise AgentToolError(
                    code="INPUT_ERROR",
                    message=f"account has multiple futu acc_ids; pass --futu-acc-id explicitly for {normalized_account}",
                )
        normalized_acc_id = _normalize_futu_acc_id(raw_acc_id)
        existing_owner = futu_mapping.get(normalized_acc_id)
        if existing_owner is not None and str(existing_owner).strip().lower() != normalized_account:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"futu acc_id already exists: ...{normalized_acc_id[-4:]}",
            )
        futu_mapping[normalized_acc_id] = normalized_account
        trade_intake["enabled"] = True

    account_mapping["futu"] = futu_mapping
    trade_intake["account_mapping"] = account_mapping
    runtime_cfg["trade_intake"] = trade_intake

    account_settings[normalized_account] = setting
    runtime_cfg["account_settings"] = account_settings

    portfolio = runtime_cfg.get("portfolio")
    if not isinstance(portfolio, dict):
        portfolio = {}
    source_by_account = portfolio.get("source_by_account")
    if not isinstance(source_by_account, dict):
        source_by_account = {}
    source_by_account[normalized_account] = ("futu" if new_type == ACCOUNT_TYPE_FUTU else "holdings")
    portfolio["source_by_account"] = source_by_account
    if str(portfolio.get("account") or "").strip().lower() not in accounts:
        portfolio["account"] = normalized_account
    runtime_cfg["portfolio"] = portfolio

    _validate_runtime_config_or_raise(runtime_cfg)
    target_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result: dict[str, Any] = {
        "market": _normalize_market(market),
        "account_label": normalized_account,
        "account_type": new_type,
        "config_path": str(target_config_path),
        "accounts": accounts,
    }
    if str(setting.get("holdings_account") or "").strip():
        result["holdings_account"] = str(setting["holdings_account"])
    if normalized_acc_id is not None:
        result["futu_acc_id_masked"] = f"...{normalized_acc_id[-4:]}"
    return result


def remove_account_from_local_config(
    *,
    repo_root: Path,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    target_config_path = _target_runtime_config_path(repo_root=repo_root, market=market, config_path=config_path)
    runtime_cfg = _load_runtime_config_for_update(target_config_path)
    accounts, normalized_account = _require_existing_account(runtime_cfg, account_label=account_label)

    remaining_accounts = [item for item in accounts if item != normalized_account]
    if not remaining_accounts:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="cannot remove the last account",
            hint="Keep at least one account in the runtime config.",
        )
    runtime_cfg["accounts"] = remaining_accounts

    account_settings = runtime_cfg.get("account_settings")
    if isinstance(account_settings, dict):
        account_settings.pop(normalized_account, None)
        runtime_cfg["account_settings"] = account_settings

    portfolio = runtime_cfg.get("portfolio")
    if not isinstance(portfolio, dict):
        portfolio = {}
    source_by_account = portfolio.get("source_by_account")
    if isinstance(source_by_account, dict):
        source_by_account.pop(normalized_account, None)
        portfolio["source_by_account"] = source_by_account
    if str(portfolio.get("account") or "").strip().lower() == normalized_account:
        portfolio["account"] = remaining_accounts[0]
    runtime_cfg["portfolio"] = portfolio

    trade_intake, account_mapping, futu_mapping = _resolve_futu_mapping(runtime_cfg)
    stale_keys = [key for key, value in futu_mapping.items() if str(value or "").strip().lower() == normalized_account]
    for key in stale_keys:
        futu_mapping.pop(key, None)
    account_mapping["futu"] = futu_mapping
    trade_intake["account_mapping"] = account_mapping
    runtime_cfg["trade_intake"] = trade_intake

    _validate_runtime_config_or_raise(runtime_cfg)
    target_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "market": _normalize_market(market),
        "removed_account": normalized_account,
        "config_path": str(target_config_path),
        "accounts": remaining_accounts,
        "portfolio_account": str((runtime_cfg.get("portfolio") or {}).get("account") or ""),
    }
