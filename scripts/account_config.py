from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_ACCOUNTS = ("lx", "sy")


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
