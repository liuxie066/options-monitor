from __future__ import annotations

"""Append account cash summary footers to notification text."""

import json
from pathlib import Path
from typing import Any

from src.application.account_config import cash_footer_accounts_from_config
from src.application.cash_headroom_query import query_sell_put_cash
from src.application.config_loader import resolve_data_config_path
from src.infrastructure.io_utils import money_cny


CASH_FOOTER_HEADERS = {
    "现金结余:",
    "现金（holding表，CNY）:",
    "现金（CNY）:",
}


def strip_existing_cash_footer(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    lines = raw.splitlines()
    cut = len(lines)
    for idx, line in enumerate(lines):
        if line.strip() in CASH_FOOTER_HEADERS:
            cut = idx
            break
    return "\n".join(lines[:cut]).rstrip()


def load_cash_footer_config(*, base: Path, config_path: str | Path | None) -> tuple[dict[str, Any], Path | None]:
    if not config_path:
        return {}, None
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    try:
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return (payload if isinstance(payload, dict) else {}), cfg_path


def resolve_cash_footer_accounts(*, cfg: dict[str, Any], accounts: list[str] | None) -> list[str]:
    if accounts is None:
        return cash_footer_accounts_from_config(cfg)
    return cash_footer_accounts_from_config({"notifications": {"cash_footer_accounts": accounts}})


def build_cash_summary_footer(
    *,
    base: Path,
    cfg: dict[str, Any],
    cfg_path: Path | None,
    data_config: str | Path | None,
    market: str,
    accounts: list[str],
) -> str:
    data_config_path = resolve_data_config_path(base=base, data_config=data_config)

    rows: list[tuple[str, Any, Any]] = []
    for account in accounts:
        payload = query_sell_put_cash(
            config=(str(cfg_path) if cfg_path is not None else None),
            data_config=str(data_config_path),
            market=str(market),
            account=str(account),
            output_format="json",
            out_dir="output/state",
            base_dir=base,
        )
        rows.append(
            (
                str(account).upper(),
                payload.get("cash_available_cny"),
                payload.get("cash_free_cny"),
            )
        )

    footer = ["现金（CNY）:"]
    for account, available_cny, free_cny in rows:
        footer.append(f"{account}: holding {money_cny(available_cny)} | free {money_cny(free_cny)}")
    return "\n".join(footer).strip()


def append_cash_summary_footer(
    *,
    base: Path,
    notification: str | Path,
    config: str | Path | None = None,
    data_config: str | Path | None = None,
    market: str = "富途",
    accounts: list[str] | None = None,
) -> Path:
    base_path = Path(base).resolve()
    cfg, cfg_path = load_cash_footer_config(base=base_path, config_path=config)
    resolved_accounts = resolve_cash_footer_accounts(cfg=cfg, accounts=accounts)

    notif_path = Path(notification)
    if not notif_path.is_absolute():
        notif_path = (base_path / notif_path).resolve()

    text = notif_path.read_text(encoding="utf-8").strip() if notif_path.exists() else ""
    text = strip_existing_cash_footer(text)
    footer = build_cash_summary_footer(
        base=base_path,
        cfg=cfg,
        cfg_path=cfg_path,
        data_config=data_config,
        market=str(market),
        accounts=resolved_accounts,
    )
    new_text = (text + "\n\n" + footer + "\n").strip() + "\n"
    notif_path.write_text(new_text, encoding="utf-8")
    return notif_path


__all__ = [
    "CASH_FOOTER_HEADERS",
    "append_cash_summary_footer",
    "build_cash_summary_footer",
    "load_cash_footer_config",
    "resolve_cash_footer_accounts",
    "strip_existing_cash_footer",
]
