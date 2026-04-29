from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.exchange_rates import get_exchange_rates_or_fetch_latest
from scripts.config_loader import resolve_data_config_path
from scripts.feishu_bitable import safe_float
from scripts.option_positions_core.domain import (
    exp_ms_to_datetime,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
)
from scripts.option_positions_core.reporting import build_monthly_income_report
from scripts.option_positions_core.service import load_option_positions_repo, require_option_positions_read_repo


def resolve_option_positions_repo(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any]:
    resolved_data_config = resolve_data_config_path(base=base, data_config=data_config)
    return resolved_data_config, load_option_positions_repo(resolved_data_config)


def load_option_position_records(repo: Any) -> list[dict[str, Any]]:
    try:
        primary_repo = require_option_positions_read_repo(repo)
    except Exception:
        primary_repo = getattr(repo, "primary_repo", repo)
    list_position_lots = getattr(primary_repo, "list_position_lots", None)
    if callable(list_position_lots):
        try:
            projected = list_position_lots()
        except Exception:
            projected = []
        if isinstance(projected, list) and projected:
            return projected
    list_records = getattr(repo, "list_records", None)
    if callable(list_records):
        rows = list_records(page_size=500)
        return rows if isinstance(rows, list) else []
    return []


def resolve_option_position_records(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any, list[dict[str, Any]]]:
    resolved_data_config, repo = resolve_option_positions_repo(base=base, data_config=data_config)
    return resolved_data_config, repo, load_option_position_records(repo)


def list_position_rows(
    repo: Any,
    *,
    broker: str,
    account: str | None = None,
    status: str = "open",
    limit: int = 50,
    expiration_within_days: int | None = None,
    as_of_ms: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized_broker = normalize_broker(broker)
    normalized_account = normalize_account(account) if account else None
    as_of_date = datetime.fromtimestamp(int(as_of_ms) / 1000).date() if as_of_ms is not None else datetime.now().date()
    for item in repo.list_records(page_size=200):
        record_id = item.get("record_id")
        fields = item.get("fields") or {}
        if normalized_broker and normalize_broker(fields.get("broker")) != normalized_broker:
            continue
        if normalized_account and normalize_account(fields.get("account")) != normalized_account:
            continue
        normalized_status = normalize_status(fields.get("status"))
        if status != "all" and normalized_status != status:
            continue
        exp_dt = exp_ms_to_datetime(fields.get("expiration"))
        expiration_ymd = exp_dt.date().isoformat() if exp_dt is not None else None
        days_to_expiration = (exp_dt.date() - as_of_date).days if exp_dt is not None else None
        if expiration_within_days is not None:
            if days_to_expiration is None or days_to_expiration < 0 or days_to_expiration > int(expiration_within_days):
                continue
        rows.append(
            {
                "record_id": record_id,
                "broker": normalize_broker(fields.get("broker")),
                "account": normalize_account(fields.get("account")) or fields.get("account"),
                "symbol": fields.get("symbol"),
                "option_type": normalize_option_type(fields.get("option_type")),
                "side": normalize_side(fields.get("side")),
                "strike": safe_float(fields.get("strike")),
                "multiplier": safe_float(fields.get("multiplier")),
                "expiration": fields.get("expiration"),
                "expiration_ymd": expiration_ymd,
                "days_to_expiration": days_to_expiration,
                "contracts": fields.get("contracts"),
                "contracts_open": fields.get("contracts_open"),
                "contracts_closed": fields.get("contracts_closed"),
                "currency": normalize_currency(fields.get("currency")),
                "cash_secured_amount": fields.get("cash_secured_amount"),
                "underlying_share_locked": fields.get("underlying_share_locked"),
                "close_type": normalize_close_type(fields.get("close_type")) if fields.get("close_type") else None,
                "close_reason": fields.get("close_reason"),
                "status": normalized_status,
                "note": fields.get("note"),
            }
        )
    return rows[: max(limit, 1)]


def build_option_positions_monthly_income_report(
    repo: Any,
    *,
    base: Path,
    broker: str,
    account: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    return build_monthly_income_report(
        repo.list_records(page_size=500),
        account=account,
        broker=broker,
        month=month,
        rates=get_exchange_rates_or_fetch_latest(
            cache_path=(base / "output" / "state" / "rate_cache.json").resolve(),
        ),
    )


def format_position_money(value: float | int | None, currency: str) -> str:
    if value is None:
        return "-"
    amount = float(value)
    normalized_currency = str(currency or "").upper()
    if normalized_currency == "USD":
        return f"${amount:,.2f}"
    if normalized_currency == "HKD":
        return f"HKD {amount:,.2f}"
    if normalized_currency == "CNY":
        return f"¥{amount:,.2f}"
    return f"{amount:,.2f} {normalized_currency}"


def format_cash_secured_amount(value: Any, currency: str) -> str:
    amount = safe_float(value)
    return format_position_money(amount, currency) if amount is not None else "-"
