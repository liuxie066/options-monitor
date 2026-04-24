from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_ACCOUNTS = ("user1",)
ACCOUNT_TYPE_FUTU = "futu"
ACCOUNT_TYPE_EXTERNAL_HOLDINGS = "external_holdings"
ACCOUNT_TYPES = (ACCOUNT_TYPE_FUTU, ACCOUNT_TYPE_EXTERNAL_HOLDINGS)


def normalize_accounts(raw: Any, *, fallback: tuple[str, ...] = DEFAULT_ACCOUNTS) -> list[str]:
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = []

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        acct = str(item or "").strip().lower()
        if not acct or acct in seen:
            continue
        seen.add(acct)
        out.append(acct)

    if out:
        return out
    return list(fallback)


def accounts_from_config(config: dict[str, Any] | None, *, fallback: tuple[str, ...] = DEFAULT_ACCOUNTS) -> list[str]:
    cfg = config if isinstance(config, dict) else {}
    return normalize_accounts(cfg.get("accounts"), fallback=fallback)


def account_settings_from_config(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    cfg = config if isinstance(config, dict) else {}
    raw = cfg.get("account_settings")
    if not isinstance(raw, dict):
        return {}

    known = set(accounts_from_config(cfg))
    out: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in raw.items():
        account = str(raw_key or "").strip().lower()
        if not account or account not in known or not isinstance(raw_value, dict):
            continue
        item = dict(raw_value)
        acct_type = str(item.get("type") or "").strip().lower()
        if acct_type not in ACCOUNT_TYPES:
            acct_type = ACCOUNT_TYPE_FUTU
        normalized: dict[str, Any] = {"type": acct_type}
        holdings_account = str(item.get("holdings_account") or "").strip()
        if holdings_account:
            normalized["holdings_account"] = holdings_account
        out[account] = normalized
    return out


def resolve_account_type(config: dict[str, Any] | None, *, account: str | None) -> str:
    cfg = config if isinstance(config, dict) else {}
    account_key = str(account or "").strip().lower()
    if not account_key:
        return ACCOUNT_TYPE_FUTU

    settings = account_settings_from_config(cfg)
    item = settings.get(account_key)
    if isinstance(item, dict):
        acct_type = str(item.get("type") or "").strip().lower()
        if acct_type in ACCOUNT_TYPES:
            return acct_type

    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    mapping = portfolio_cfg.get("source_by_account") if isinstance(portfolio_cfg, dict) else None
    if isinstance(mapping, dict):
        value = str(mapping.get(account_key) or "").strip().lower()
        if value == "holdings":
            return ACCOUNT_TYPE_EXTERNAL_HOLDINGS
    return ACCOUNT_TYPE_FUTU


def resolve_holdings_account(config: dict[str, Any] | None, *, account: str | None) -> str | None:
    account_key = str(account or "").strip().lower()
    if not account_key:
        return None
    explicit = resolve_configured_holdings_account(config, account=account_key)
    if explicit:
        return explicit
    return account_key


def resolve_configured_holdings_account(config: dict[str, Any] | None, *, account: str | None) -> str | None:
    account_key = str(account or "").strip().lower()
    if not account_key:
        return None
    settings = account_settings_from_config(config)
    item = settings.get(account_key) if isinstance(settings, dict) else None
    if isinstance(item, dict):
        value = str(item.get("holdings_account") or "").strip()
        if value:
            return value
    return None


def has_holdings_fallback(config: dict[str, Any] | None, *, account: str | None) -> bool:
    return bool(str(resolve_configured_holdings_account(config, account=account) or "").strip())


def resolve_portfolio_source(config: dict[str, Any] | None, *, account: str | None) -> str:
    cfg = config if isinstance(config, dict) else {}
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    account_key = str(account or "").strip().lower()

    if account_key:
        acct_type = resolve_account_type(cfg, account=account_key)
        if acct_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS:
            return "holdings"
        mapping = portfolio_cfg.get("source_by_account") if isinstance(portfolio_cfg, dict) else None
        if isinstance(mapping, dict):
            value = mapping.get(account_key)
            if value is not None and str(value).strip():
                return str(value).strip()

    value = portfolio_cfg.get("source") if isinstance(portfolio_cfg, dict) else None
    if value is not None and str(value).strip():
        return str(value).strip()
    return "auto"


def cash_footer_accounts_from_config(
    config: dict[str, Any] | None,
    *,
    fallback: tuple[str, ...] = DEFAULT_ACCOUNTS,
) -> list[str]:
    cfg = config if isinstance(config, dict) else {}
    notif_cfg = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    explicit = notif_cfg.get("cash_footer_accounts") if isinstance(notif_cfg, dict) else None
    if explicit is not None:
        return normalize_accounts(explicit, fallback=fallback)
    return accounts_from_config(cfg, fallback=fallback)


def accounts_from_config_path(path: str | Path, *, fallback: tuple[str, ...] = DEFAULT_ACCOUNTS) -> list[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return accounts_from_config(data, fallback=fallback)
