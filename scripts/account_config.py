from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.futu_portfolio_context import resolve_trade_intake_futu_account_ids


DEFAULT_ACCOUNTS = ("user1",)
ACCOUNT_TYPE_FUTU = "futu"
ACCOUNT_TYPE_EXTERNAL_HOLDINGS = "external_holdings"
ACCOUNT_TYPES = (ACCOUNT_TYPE_FUTU, ACCOUNT_TYPE_EXTERNAL_HOLDINGS)


@dataclass(frozen=True)
class AccountPortfolioSourcePlan:
    account: str | None
    account_type: str
    requested_source: str
    primary_source: str
    fallback_source: str | None
    holdings_account: str | None
    configured_holdings_account: str | None


@dataclass(frozen=True)
class AccountConfigView:
    account: str
    account_type: str
    futu_acc_ids: list[str]
    holdings_account: str | None
    portfolio_source_plan: AccountPortfolioSourcePlan
    fallback_enabled: bool


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
        market = str(item.get("market") or "").strip().lower()
        if market in {"us", "hk"}:
            normalized["market"] = market
        if "enabled" in item:
            normalized["enabled"] = bool(item.get("enabled"))
        if "trade_intake_enabled" in item:
            normalized["trade_intake_enabled"] = bool(item.get("trade_intake_enabled"))
        holdings_account = str(item.get("holdings_account") or "").strip()
        if holdings_account:
            normalized["holdings_account"] = holdings_account
        futu_cfg = item.get("futu")
        if isinstance(futu_cfg, dict):
            futu_out: dict[str, Any] = {}
            host = str(futu_cfg.get("host") or "").strip()
            if host:
                futu_out["host"] = host
            port = futu_cfg.get("port")
            if port not in (None, ""):
                try:
                    futu_out["port"] = int(port)
                except Exception:
                    pass
            account_id = str(futu_cfg.get("account_id") or "").strip()
            if account_id:
                futu_out["account_id"] = account_id
            if futu_out:
                normalized["futu"] = futu_out
        bitable_cfg = item.get("bitable")
        if isinstance(bitable_cfg, dict):
            bitable_out: dict[str, Any] = {}
            for key in ("app_token", "table_id", "view_name"):
                value = str(bitable_cfg.get(key) or "").strip()
                if value:
                    bitable_out[key] = value
            if bitable_out:
                normalized["bitable"] = bitable_out
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


def normalize_portfolio_source(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("", "auto"):
        return "auto"
    if raw in ("futu", "opend"):
        return "futu"
    return "holdings"


def build_account_portfolio_source_plan(
    config: dict[str, Any] | None,
    *,
    account: str | None,
    portfolio_source: str | None = None,
) -> AccountPortfolioSourcePlan:
    account_key = str(account or "").strip().lower() or None
    cfg = config if isinstance(config, dict) else {}
    account_type = resolve_account_type(cfg, account=account_key)
    requested_source = normalize_portfolio_source(
        portfolio_source if portfolio_source is not None else resolve_portfolio_source(cfg, account=account_key)
    )
    configured_holdings_account = resolve_configured_holdings_account(cfg, account=account_key)
    holdings_account = resolve_holdings_account(cfg, account=account_key)

    if account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS:
        primary_source = "holdings"
        fallback_source = None
    elif requested_source == "futu":
        primary_source = "futu"
        fallback_source = ("holdings" if configured_holdings_account else None)
    elif requested_source == "auto":
        primary_source = "futu"
        fallback_source = "holdings"
    else:
        primary_source = requested_source
        fallback_source = None

    return AccountPortfolioSourcePlan(
        account=account_key,
        account_type=account_type,
        requested_source=requested_source,
        primary_source=primary_source,
        fallback_source=fallback_source,
        holdings_account=holdings_account,
        configured_holdings_account=configured_holdings_account,
    )


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


def build_account_config_view(config: dict[str, Any] | None, *, account: str) -> AccountConfigView:
    account_key = str(account or "").strip().lower()
    cfg = config if isinstance(config, dict) else {}
    source_plan = build_account_portfolio_source_plan(cfg, account=account_key)
    futu_acc_ids = resolve_trade_intake_futu_account_ids(cfg, account=account_key)
    fallback_enabled = bool(source_plan.fallback_source) or source_plan.account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS
    return AccountConfigView(
        account=account_key,
        account_type=source_plan.account_type,
        futu_acc_ids=futu_acc_ids,
        holdings_account=source_plan.holdings_account,
        portfolio_source_plan=source_plan,
        fallback_enabled=fallback_enabled,
    )


def list_account_config_views(
    config: dict[str, Any] | None,
    *,
    fallback: tuple[str, ...] = DEFAULT_ACCOUNTS,
) -> list[AccountConfigView]:
    cfg = config if isinstance(config, dict) else {}
    return [
        build_account_config_view(cfg, account=account)
        for account in accounts_from_config(cfg, fallback=fallback)
    ]


def accounts_from_config_path(path: str | Path, *, fallback: tuple[str, ...] = DEFAULT_ACCOUNTS) -> list[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return accounts_from_config(data, fallback=fallback)
