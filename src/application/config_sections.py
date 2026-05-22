from __future__ import annotations


def resolve_templates_config(cfg: dict | None) -> dict:
    data = cfg if isinstance(cfg, dict) else {}
    templates = data.get("templates")
    if isinstance(templates, dict):
        return templates
    return {}


def resolve_watchlist_config(cfg: dict | None) -> list[dict]:
    data = cfg if isinstance(cfg, dict) else {}
    symbols = data.get("symbols")
    if isinstance(symbols, list):
        out: list[dict] = []
        for item in symbols:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            broker = str(item.get("broker") or "").strip()
            if not broker:
                broker = str(item.get("market") or "").strip()
            if broker:
                normalized["broker"] = broker
            normalized.pop("market", None)
            out.append(normalized)
        return out
    return []


def set_watchlist_config(cfg: dict | None, items: list[dict]) -> dict:
    data = cfg if isinstance(cfg, dict) else {}
    normalized: list[dict] = []
    for item in items or []:
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
    data["symbols"] = normalized
    return data


__all__ = ["resolve_templates_config", "resolve_watchlist_config", "set_watchlist_config"]
