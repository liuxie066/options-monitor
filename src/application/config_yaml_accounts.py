from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.account_config import (
    ACCOUNT_TYPE_EXTERNAL_HOLDINGS,
    ACCOUNT_TYPE_FUTU,
    ACCOUNT_TYPES,
    normalize_account_label,
    normalize_accounts,
)
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_primitives import normalize_config_market, resolve_config_path
from src.application.config_yaml import default_yaml_config_path, load_yaml_config_file
from src.application.write_contract import attach_write_contract


def mutate_yaml_account_config(
    *,
    repo_root: Path,
    action: str,
    market: str,
    account_label: str,
    account_type: str | None = None,
    config_path: str | Path | None = None,
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
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key not in {"add", "edit", "remove"}:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported account action: {action}")
    market_key = normalize_config_market(market)
    account = _normalize_account_label(account_label)
    config_yaml_path = resolve_config_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    loaded_source_sha = config_source_sha256(config_yaml_path)
    after_doc = deepcopy(load_yaml_config_file(config_yaml_path))

    if action_key == "add":
        summary = _add_account(
            after_doc,
            market=market_key,
            account=account,
            account_type=account_type,
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
        )
    elif action_key == "edit":
        summary = _edit_account(
            after_doc,
            market=market_key,
            account=account,
            account_type=account_type,
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
        )
    else:
        summary = _remove_account(after_doc, market=market_key, account=account)

    runtime_root = (
        Path(rebuild_runtime_root).expanduser().resolve()
        if rebuild_runtime_root is not None and str(rebuild_runtime_root).strip()
        else config_yaml_path.parent
    )
    transaction = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=config_yaml_path,
        config_doc=after_doc,
        runtime_root=runtime_root,
        markets=_markets_in_doc(after_doc),
        include_assistant=True,
        apply=bool(apply),
        backup=bool(backup),
        expected_source_sha256=expected_source_sha256 or loaded_source_sha,
    )
    backup_path = transaction.get("backup_path")
    payload = {
        "ok": True,
        "action": action_key,
        "source_format": "yaml",
        "config_yaml_path": str(config_yaml_path),
        "market": market_key,
        "account_label": account,
        "account_type": summary.get("account_type"),
        "accounts": summary.get("accounts"),
        **{
            key: summary[key]
            for key in (
                "futu_acc_id_masked",
                "holdings_account",
                "removed_account",
                "removed_global_account",
            )
            if key in summary
        },
        "summary": summary,
        "validation": transaction["markets"],
        "rebuild": transaction if apply else None,
        "source_revision": transaction.get("source_revision"),
        "audit_id": transaction.get("audit_id"),
    }
    return attach_write_contract(
        payload,
        dry_run=not bool(apply),
        write_applied=bool(apply),
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {config_yaml_path}" if backup_path else f"edit or restore {config_yaml_path}",
    )


def _add_account(
    config_doc: dict[str, Any],
    *,
    market: str,
    account: str,
    account_type: str | None,
    futu_acc_id: str | None,
    holdings_account: str | None,
    market_label: str | None,
    enabled: bool | None,
    trade_intake_enabled: bool | None,
    futu_host: str | None,
    futu_port: int | None,
    bitable_app_token: str | None,
    bitable_table_id: str | None,
    bitable_view_name: str | None,
) -> dict[str, Any]:
    accounts = _account_defs(config_doc)
    if account in accounts:
        raise AgentToolError(code="INPUT_ERROR", message=f"account already exists: {account}")
    normalized_type = _normalize_account_type(account_type)
    setting = _build_account_setting(
        current=None,
        account=account,
        account_type=normalized_type,
        futu_acc_id=futu_acc_id,
        holdings_account=holdings_account,
        clear_holdings_account=False,
        market_label=market_label or market,
        enabled=enabled,
        trade_intake_enabled=trade_intake_enabled,
        futu_host=futu_host,
        futu_port=futu_port,
        bitable_app_token=bitable_app_token,
        bitable_table_id=bitable_table_id,
        bitable_view_name=bitable_view_name,
    )
    _ensure_unique_futu_account_id(accounts, account=account, setting=setting)
    accounts[account] = setting
    market_accounts = _market_accounts(config_doc, market=market)
    market_accounts.append(account)
    return {
        "action": "add",
        "account_type": normalized_type,
        "accounts": list(market_accounts),
        "changed_paths": [f"accounts.{account}", f"markets.{market}.accounts[]"],
        **_masked_account_details(setting),
    }


def _edit_account(
    config_doc: dict[str, Any],
    *,
    market: str,
    account: str,
    account_type: str | None,
    futu_acc_id: str | None,
    holdings_account: str | None,
    clear_holdings_account: bool,
    market_label: str | None,
    enabled: bool | None,
    trade_intake_enabled: bool | None,
    futu_host: str | None,
    futu_port: int | None,
    bitable_app_token: str | None,
    bitable_table_id: str | None,
    bitable_view_name: str | None,
) -> dict[str, Any]:
    accounts = _account_defs(config_doc)
    current = accounts.get(account)
    if not isinstance(current, dict):
        raise AgentToolError(code="INPUT_ERROR", message=f"account not found: {account}")
    market_accounts = _market_accounts(config_doc, market=market)
    if account not in market_accounts:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"account {account} is not enabled for market {market}",
        )
    current_type = _normalize_account_type(current.get("type"))
    normalized_type = _normalize_account_type(account_type) if account_type is not None else current_type
    setting = _build_account_setting(
        current=current,
        account=account,
        account_type=normalized_type,
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
    )
    _ensure_unique_futu_account_id(accounts, account=account, setting=setting)
    accounts[account] = setting
    return {
        "action": "edit",
        "account_type": normalized_type,
        "accounts": list(market_accounts),
        "changed_paths": [f"accounts.{account}"],
        **_masked_account_details(setting),
    }


def _remove_account(config_doc: dict[str, Any], *, market: str, account: str) -> dict[str, Any]:
    accounts = _account_defs(config_doc)
    if account not in accounts:
        raise AgentToolError(code="INPUT_ERROR", message=f"account not found: {account}")
    market_accounts = _market_accounts(config_doc, market=market)
    if account not in market_accounts:
        raise AgentToolError(code="INPUT_ERROR", message=f"account {account} is not enabled for market {market}")
    remaining = [item for item in market_accounts if item != account]
    if not remaining:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"cannot remove the last account from market {market}",
            hint=f"Keep at least one account in markets.{market}.accounts.",
        )
    market_accounts[:] = remaining
    _remove_account_from_market_overrides(config_doc, market=market, account=account)
    still_referenced = any(account in _market_accounts(config_doc, market=item) for item in _markets_in_doc(config_doc))
    removed_global = not still_referenced
    if removed_global:
        accounts.pop(account, None)
        _remove_explicit_account_references(config_doc, account=account)
    return {
        "action": "remove",
        "account_type": None,
        "removed_account": account,
        "removed_global_account": removed_global,
        "accounts": remaining,
        "changed_paths": [
            f"markets.{market}.accounts",
            *([f"accounts.{account}"] if removed_global else []),
        ],
    }


def _build_account_setting(
    *,
    current: dict[str, Any] | None,
    account: str,
    account_type: str,
    futu_acc_id: str | None,
    holdings_account: str | None,
    clear_holdings_account: bool,
    market_label: str | None,
    enabled: bool | None,
    trade_intake_enabled: bool | None,
    futu_host: str | None,
    futu_port: int | None,
    bitable_app_token: str | None,
    bitable_table_id: str | None,
    bitable_view_name: str | None,
) -> dict[str, Any]:
    existing = deepcopy(current) if isinstance(current, dict) else {}
    setting: dict[str, Any] = {"type": account_type}
    for key in ("enabled", "trade_intake_enabled", "market"):
        if key in existing:
            setting[key] = existing[key]
    if market_label is not None:
        normalized_market = normalize_config_market(market_label)
        setting["market"] = normalized_market
    if enabled is not None:
        setting["enabled"] = bool(enabled)
    elif current is None:
        setting["enabled"] = True
    if trade_intake_enabled is not None:
        setting["trade_intake_enabled"] = bool(trade_intake_enabled)
    elif current is None or str(existing.get("type") or "").strip().lower() != account_type:
        setting["trade_intake_enabled"] = account_type == ACCOUNT_TYPE_FUTU

    if account_type == ACCOUNT_TYPE_FUTU:
        raw_futu = existing.get("futu")
        futu = deepcopy(raw_futu) if isinstance(raw_futu, dict) else {}
        shorthand_id = str(existing.get("futu_account_id") or "").strip()
        if shorthand_id and not str(futu.get("account_id") or "").strip():
            futu["account_id"] = shorthand_id
        if futu_acc_id is not None:
            futu["account_id"] = _normalize_futu_acc_id(futu_acc_id)
        elif not str(futu.get("account_id") or "").strip():
            raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id is required for a Futu account")
        if futu_host is not None:
            host = str(futu_host).strip()
            if host:
                futu["host"] = host
            else:
                futu.pop("host", None)
        if futu_port is not None:
            futu["port"] = int(futu_port)
        setting["futu"] = futu
        if clear_holdings_account:
            holdings_value = ""
        elif holdings_account is not None:
            holdings_value = str(holdings_account).strip()
        else:
            holdings_value = str(existing.get("holdings_account") or "").strip()
        if holdings_value:
            setting["holdings_account"] = holdings_value
        return setting

    if clear_holdings_account:
        holdings_value = ""
    elif holdings_account is not None:
        holdings_value = str(holdings_account).strip()
    else:
        holdings_value = str(existing.get("holdings_account") or "").strip()
    setting["holdings_account"] = holdings_value or account
    raw_bitable = existing.get("bitable")
    bitable = deepcopy(raw_bitable) if isinstance(raw_bitable, dict) else {}
    for key, value in {
        "app_token": bitable_app_token,
        "table_id": bitable_table_id,
        "view_name": bitable_view_name,
    }.items():
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            bitable[key] = normalized
        else:
            bitable.pop(key, None)
    if bitable:
        setting["bitable"] = bitable
    return setting


def _account_defs(config_doc: dict[str, Any]) -> dict[str, Any]:
    accounts = config_doc.get("accounts")
    if not isinstance(accounts, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="config.yaml accounts must be an object")
    return accounts


def _market_accounts(config_doc: dict[str, Any], *, market: str) -> list[str]:
    markets = config_doc.get("markets")
    if not isinstance(markets, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="config.yaml markets must be an object")
    market_doc = markets.get(market)
    if not isinstance(market_doc, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"config.yaml missing markets.{market}")
    raw_accounts = market_doc.get("accounts")
    if not isinstance(raw_accounts, list):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.accounts must be a list")
    normalized = normalize_accounts(raw_accounts, fallback=())
    raw_accounts[:] = normalized
    return raw_accounts


def _normalize_account_label(value: str | None) -> str:
    try:
        return normalize_account_label(value)
    except ValueError as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"account_label is invalid: {exc}",
        ) from exc


def _normalize_account_type(value: Any) -> str:
    account_type = str(value or "").strip().lower()
    if account_type not in ACCOUNT_TYPES:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"account_type must be one of: {', '.join(ACCOUNT_TYPES)}",
        )
    return account_type


def _normalize_futu_acc_id(value: Any) -> str:
    account_id = str(value or "").strip()
    if not account_id:
        raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id is required")
    if not account_id.isdigit():
        raise AgentToolError(code="INPUT_ERROR", message="futu_acc_id must be digits only")
    return account_id


def _futu_account_id(setting: Any) -> str:
    if not isinstance(setting, dict):
        return ""
    shorthand = str(setting.get("futu_account_id") or "").strip()
    futu = setting.get("futu")
    nested = str(futu.get("account_id") or "").strip() if isinstance(futu, dict) else ""
    return nested or shorthand


def _ensure_unique_futu_account_id(accounts: dict[str, Any], *, account: str, setting: dict[str, Any]) -> None:
    account_id = _futu_account_id(setting)
    if not account_id:
        return
    for other_account, other_setting in accounts.items():
        if str(other_account).strip().lower() == account:
            continue
        if _futu_account_id(other_setting) == account_id:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"futu acc_id already exists: ...{account_id[-4:]}",
            )


def _masked_account_details(setting: dict[str, Any]) -> dict[str, Any]:
    account_id = _futu_account_id(setting)
    holdings = str(setting.get("holdings_account") or "").strip()
    return {
        **({"futu_acc_id_masked": f"...{account_id[-4:]}"} if account_id else {}),
        **({"holdings_account": holdings} if holdings else {}),
    }


def _remove_account_from_market_overrides(config_doc: dict[str, Any], *, market: str, account: str) -> None:
    markets = config_doc.get("markets")
    market_doc = markets.get(market) if isinstance(markets, dict) else None
    overrides = market_doc.get("overrides") if isinstance(market_doc, dict) else None
    if not isinstance(overrides, dict):
        return
    for raw_override in overrides.values():
        if not isinstance(raw_override, dict) or not isinstance(raw_override.get("accounts"), list):
            continue
        raw_override["accounts"] = [
            item
            for item in normalize_accounts(raw_override.get("accounts"), fallback=())
            if item != account
        ]


def _remove_explicit_account_references(config_doc: dict[str, Any], *, account: str) -> None:
    portfolio = config_doc.get("portfolio")
    if isinstance(portfolio, dict):
        source_by_account = portfolio.get("source_by_account")
        if isinstance(source_by_account, dict):
            source_by_account.pop(account, None)
        if str(portfolio.get("account") or "").strip().lower() == account:
            portfolio.pop("account", None)
    notifications = config_doc.get("notifications")
    if isinstance(notifications, dict) and isinstance(notifications.get("cash_footer_accounts"), list):
        notifications["cash_footer_accounts"] = [
            item
            for item in normalize_accounts(notifications.get("cash_footer_accounts"), fallback=())
            if item != account
        ]


def _markets_in_doc(config_doc: dict[str, Any]) -> list[str]:
    markets = config_doc.get("markets")
    if not isinstance(markets, dict):
        return []
    return [market for market in ("us", "hk") if isinstance(markets.get(market), dict)]


__all__ = ["mutate_yaml_account_config"]
