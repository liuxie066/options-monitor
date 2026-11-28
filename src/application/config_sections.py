from __future__ import annotations

from collections.abc import Iterable


def resolve_templates_config(cfg: dict | None) -> dict:
    data = cfg if isinstance(cfg, dict) else {}
    templates = data.get("templates")
    if isinstance(templates, dict):
        return templates
    return {}


def _normalize_watchlist_items(items: Iterable[object]) -> list[dict]:
    normalized: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        broker = str(item.get("broker") or "").strip()
        if not broker:
            broker = str(item.get("market") or "").strip()
        if broker:
            row["broker"] = broker
        row.pop("market", None)
        normalized.append(row)
    return normalized


def resolve_watchlist_config(cfg: dict | None) -> list[dict]:
    data = cfg if isinstance(cfg, dict) else {}
    symbols = data.get("symbols")
    return _normalize_watchlist_items(symbols) if isinstance(symbols, list) else []


def set_watchlist_config(cfg: dict | None, items: list[dict]) -> dict:
    data = cfg if isinstance(cfg, dict) else {}
    data["symbols"] = _normalize_watchlist_items(items or [])
    return data


def normalize_portfolio_broker_config(cfg: dict | None) -> dict:
    data = dict(cfg or {}) if isinstance(cfg, dict) else {}
    portfolio = data.get('portfolio')
    if not isinstance(portfolio, dict):
        return data

    normalized = {k: v for k, v in portfolio.items() if k != 'market'}

    data_config = str(portfolio.get('data_config') or '').strip()
    if data_config:
        normalized['data_config'] = data_config

    broker = str(portfolio.get('broker') or '').strip()
    if not broker:
        broker = str(portfolio.get('market') or '').strip()
    if broker:
        normalized['broker'] = broker

    data['portfolio'] = normalized
    return data


__all__ = ["resolve_templates_config", "resolve_watchlist_config", "set_watchlist_config", "normalize_portfolio_broker_config"]
