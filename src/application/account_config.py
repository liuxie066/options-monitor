from __future__ import annotations

import json
from numbers import Integral
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


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
    holdings_account: str | None
    configured_holdings_account: str | None


@dataclass(frozen=True)
class AccountConfigView:
    account: str
    account_type: str
    futu_acc_ids: list[str]
    holdings_account: str | None
    portfolio_source_plan: AccountPortfolioSourcePlan
    runtime_plan: AccountRuntimePlan


@dataclass(frozen=True)
class AccountRuntimePlan:
    account: str
    account_type: str
    portfolio_source: str
    trade_source: str
    trade_intake_enabled: bool
    holdings_account: str | None
    futu_account_id: str | None = None
    futu_host: str | None = None
    futu_port: int | None = None
    futu_telnet_port: int | None = None
    futu_opend_root: str | None = None
    futu_trd_env: str | None = None


@dataclass(frozen=True)
class ResolvedAccountBrokerBindingMember:
    config_key: str | None
    market: str
    authority_source: str
    account_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedAccountBrokerBindingSet:
    account: str
    status: str
    host: str | None
    port: int | None
    trd_env: str | None
    required_account_ids: tuple[str, ...]
    members: tuple[ResolvedAccountBrokerBindingMember, ...]
    compatibility_warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


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


def parse_lossless_integer(value: Any) -> int | None:
    """Return an integer only when the configured value has exact integer semantics."""

    if isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if str(parsed) == value else None


def _int_or_none(value: Any) -> int | None:
    return parse_lossless_integer(value)


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
            for key in ("port", "telnet_port"):
                port = futu_cfg.get(key)
                if port in (None, ""):
                    continue
                parsed_port = parse_lossless_integer(port)
                if parsed_port is not None:
                    futu_out[key] = parsed_port
            account_id = str(futu_cfg.get("account_id") or "").strip()
            if account_id:
                futu_out["account_id"] = account_id
            opend_root = str(futu_cfg.get("opend_root") or "").strip()
            if opend_root:
                futu_out["opend_root"] = opend_root
            trd_env = str(futu_cfg.get("trd_env") or "").strip()
            if trd_env:
                futu_out["trd_env"] = trd_env
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


def resolve_account_futu_settings(config: Mapping[str, Any] | Any, *, account: str | None) -> dict[str, Any]:
    account_key = str(account or "").strip().lower()
    if not account_key:
        return {}
    settings = account_settings_from_config(dict(config) if isinstance(config, Mapping) else {})
    item = settings.get(account_key) if isinstance(settings, dict) else None
    futu_cfg = item.get("futu") if isinstance(item, dict) else None
    if isinstance(futu_cfg, dict):
        return dict(futu_cfg)

    if not isinstance(config, Mapping):
        return {}
    raw_settings = config.get("account_settings")
    if not isinstance(raw_settings, Mapping):
        return {}
    raw_item = raw_settings.get(account_key)
    if not isinstance(raw_item, Mapping):
        return {}
    raw_futu = raw_item.get("futu")
    return dict(raw_futu) if isinstance(raw_futu, Mapping) else {}


def resolve_futu_account_ids(config: Mapping[str, Any] | Any, *, account: str | None) -> list[str]:
    futu_cfg = resolve_account_futu_settings(config, account=account)
    account_id = str(futu_cfg.get("account_id") or "").strip()
    mapped_ids = _resolve_trade_intake_mapping_futu_account_ids(config, account=account)
    if account_id:
        return [account_id]
    cfg = dict(config) if isinstance(config, Mapping) else {}
    futu_accounts = [
        value
        for value in accounts_from_config(cfg)
        if resolve_account_type(cfg, account=value) == ACCOUNT_TYPE_FUTU
    ]
    if len(futu_accounts) != 1:
        return []
    return mapped_ids


def resolve_account_broker_binding_sets(
    configs: list[tuple[str | None, Mapping[str, Any]]],
) -> dict[str, ResolvedAccountBrokerBindingSet]:
    """Resolve fail-closed broker authority across selected runtime configs."""

    from src.application.futu_quote_routing import resolve_futu_quote_route, runtime_config_market

    contributions: dict[str, list[dict[str, Any]]] = {}
    for config_key, raw_config in configs:
        cfg = dict(raw_config) if isinstance(raw_config, Mapping) else {}
        futu_accounts = [
            account
            for account in accounts_from_config(cfg)
            if resolve_account_type(cfg, account=account) == ACCOUNT_TYPE_FUTU
        ]
        quote_route = resolve_futu_quote_route(cfg, config_key=config_key)
        market = runtime_config_market(cfg)
        for account in futu_accounts:
            explicit = resolve_account_futu_settings(cfg, account=account)
            complete_explicit = all(
                explicit.get(key) not in (None, "")
                for key in ("host", "port", "account_id", "trd_env")
            )
            warnings: list[str] = []
            errors: list[str] = []
            settings = dict(explicit)
            authority = "account_settings"
            if not complete_explicit:
                if len(futu_accounts) != 1:
                    errors.append("multi-Futu runtime requires a complete account_settings broker binding")
                else:
                    authority = "legacy_single_futu_projection"
                    portfolio = cfg.get("portfolio")
                    legacy = portfolio.get("futu") if isinstance(portfolio, Mapping) else None
                    if isinstance(legacy, Mapping):
                        for key, value in legacy.items():
                            if settings.get(key) in (None, "") and value not in (None, ""):
                                settings[str(key)] = value
                    if settings.get("host") in (None, "") and quote_route.ok:
                        settings["host"] = quote_route.host
                    if settings.get("port") in (None, "") and quote_route.ok:
                        settings["port"] = quote_route.port
                    if settings.get("trd_env") in (None, ""):
                        settings["trd_env"] = "REAL"
                    warnings.append(
                        f"{account} uses legacy sole-Futu broker projection; configure account_settings.{account}.futu explicitly"
                    )
            ids = resolve_futu_account_ids(cfg, account=account)
            if settings.get("account_id") not in (None, ""):
                explicit_id = str(settings["account_id"]).strip()
                if explicit_id and explicit_id not in ids:
                    ids.insert(0, explicit_id)
            normalized_ids: list[str] = []
            for raw_id in ids:
                parsed = parse_lossless_integer(raw_id)
                if parsed is None:
                    errors.append("broker account id is not losslessly comparable")
                    continue
                normalized_ids.append(str(parsed))
            host = str(settings.get("host") or "").strip().lower()
            port = parse_lossless_integer(settings.get("port"))
            trd_env = str(settings.get("trd_env") or "").strip().upper()
            if not host or port is None or not 1 <= port <= 65535 or not trd_env or not normalized_ids:
                errors.append("broker binding is incomplete")
            contributions.setdefault(account, []).append(
                {
                    "config_key": str(config_key or "").strip().lower() or None,
                    "market": market,
                    "authority": authority,
                    "host": host or None,
                    "port": port,
                    "trd_env": trd_env or None,
                    "ids": tuple(dict.fromkeys(normalized_ids)),
                    "warnings": tuple(warnings),
                    "errors": tuple(errors),
                }
            )

    result: dict[str, ResolvedAccountBrokerBindingSet] = {}
    for account, rows in contributions.items():
        endpoints = {(row["host"], row["port"], row["trd_env"]) for row in rows if not row["errors"]}
        errors = [error for row in rows for error in row["errors"]]
        if len(endpoints) > 1:
            errors.append("broker endpoint or trd_env differs across runtime configs")
        endpoint = next(iter(endpoints)) if len(endpoints) == 1 else (None, None, None)
        members = tuple(
            ResolvedAccountBrokerBindingMember(
                config_key=row["config_key"],
                market=row["market"],
                authority_source=row["authority"],
                account_ids=row["ids"],
            )
            for row in rows
        )
        result[account] = ResolvedAccountBrokerBindingSet(
            account=account,
            status="ok" if not errors and len(endpoints) == 1 else "conflict",
            host=endpoint[0],
            port=endpoint[1],
            trd_env=endpoint[2],
            required_account_ids=tuple(sorted({value for row in rows for value in row["ids"]}, key=int)),
            members=members,
            compatibility_warnings=tuple(dict.fromkeys(warning for row in rows for warning in row["warnings"])),
            errors=tuple(dict.fromkeys(errors)),
        )

    ready_endpoints: dict[tuple[str, int], list[str]] = {}
    for account, binding in result.items():
        if binding.ok and binding.host is not None and binding.port is not None:
            ready_endpoints.setdefault((binding.host, binding.port), []).append(account)
    for endpoint, accounts in ready_endpoints.items():
        if len(accounts) < 2:
            continue
        for account in accounts:
            current = result[account]
            result[account] = ResolvedAccountBrokerBindingSet(
                account=current.account,
                status="conflict",
                host=None,
                port=None,
                trd_env=current.trd_env,
                required_account_ids=current.required_account_ids,
                members=current.members,
                compatibility_warnings=current.compatibility_warnings,
                errors=current.errors + ("multiple logical Futu accounts share one broker endpoint",),
            )
    return result


def resolve_account_trade_intake_enabled(config: Mapping[str, Any] | Any, *, account: str | None) -> bool:
    account_key = str(account or "").strip().lower()
    if not account_key:
        return False
    if resolve_account_type(dict(config) if isinstance(config, Mapping) else {}, account=account_key) != ACCOUNT_TYPE_FUTU:
        return False

    settings = account_settings_from_config(dict(config) if isinstance(config, Mapping) else {})
    item = settings.get(account_key) if isinstance(settings, dict) else None
    if isinstance(item, dict) and "trade_intake_enabled" in item:
        return bool(item.get("trade_intake_enabled"))

    if isinstance(config, Mapping):
        raw_settings = config.get("account_settings")
        raw_item = raw_settings.get(account_key) if isinstance(raw_settings, Mapping) else None
        if isinstance(raw_item, Mapping) and isinstance(raw_item.get("trade_intake_enabled"), bool):
            return bool(raw_item.get("trade_intake_enabled"))
    return True


def build_account_runtime_plan(config: dict[str, Any] | None, *, account: str) -> AccountRuntimePlan:
    account_key = str(account or "").strip().lower()
    cfg = config if isinstance(config, dict) else {}
    source_plan = build_account_portfolio_source_plan(cfg, account=account_key)
    futu_cfg = resolve_account_futu_settings(cfg, account=account_key)
    account_type = source_plan.account_type
    trade_source = "api" if account_type == ACCOUNT_TYPE_FUTU else "manual"
    portfolio_source = "holdings" if source_plan.primary_source == ACCOUNT_TYPE_EXTERNAL_HOLDINGS else source_plan.primary_source

    return AccountRuntimePlan(
        account=account_key,
        account_type=account_type,
        portfolio_source=portfolio_source,
        trade_source=trade_source,
        trade_intake_enabled=resolve_account_trade_intake_enabled(cfg, account=account_key),
        holdings_account=source_plan.holdings_account,
        futu_account_id=str(futu_cfg.get("account_id") or "").strip() or None,
        futu_host=str(futu_cfg.get("host") or "").strip() or None,
        futu_port=_int_or_none(futu_cfg.get("port")),
        futu_telnet_port=_int_or_none(futu_cfg.get("telnet_port")),
        futu_opend_root=str(futu_cfg.get("opend_root") or "").strip() or None,
        futu_trd_env=str(futu_cfg.get("trd_env") or "").strip() or None,
    )


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
        primary_source = ACCOUNT_TYPE_EXTERNAL_HOLDINGS
    else:
        primary_source = "holdings" if requested_source == "holdings" else "futu"

    return AccountPortfolioSourcePlan(
        account=account_key,
        account_type=account_type,
        requested_source=requested_source,
        primary_source=primary_source,
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


def _normalize_account_ids(raw: Mapping[str, Any] | Any, *, account: str | None) -> list[str]:
    if not account or not isinstance(raw, Mapping):
        return []
    want = str(account or "").strip().lower()
    out: list[str] = []
    for acc_id, mapped in raw.items():
        if str(mapped or "").strip().lower() != want:
            continue
        key = str(acc_id or "").strip()
        if key:
            out.append(key)
    return out


def _resolve_trade_intake_mapping_futu_account_ids(config: Mapping[str, Any] | Any, *, account: str | None) -> list[str]:
    if not isinstance(config, Mapping):
        return []
    trade_intake = config.get("trade_intake")
    if not isinstance(trade_intake, Mapping):
        return []
    account_mapping = trade_intake.get("account_mapping")
    if not isinstance(account_mapping, Mapping):
        return []
    futu_mapping = account_mapping.get("futu")
    return _normalize_account_ids(futu_mapping, account=account)


def resolve_trade_intake_futu_account_ids(config: Mapping[str, Any] | Any, *, account: str | None) -> list[str]:
    return resolve_futu_account_ids(config, account=account)


def build_account_config_view(config: dict[str, Any] | None, *, account: str) -> AccountConfigView:
    account_key = str(account or "").strip().lower()
    cfg = config if isinstance(config, dict) else {}
    source_plan = build_account_portfolio_source_plan(cfg, account=account_key)
    futu_acc_ids = resolve_futu_account_ids(cfg, account=account_key)
    runtime_plan = build_account_runtime_plan(cfg, account=account_key)
    return AccountConfigView(
        account=account_key,
        account_type=source_plan.account_type,
        futu_acc_ids=futu_acc_ids,
        holdings_account=source_plan.holdings_account,
        portfolio_source_plan=source_plan,
        runtime_plan=runtime_plan,
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
