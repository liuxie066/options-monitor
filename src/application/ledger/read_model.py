from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
    expiration_timestamp_to_date,
    expiration_timestamp_to_ymd,
)
from domain.domain.ledger.position_fields import (
    effective_contracts,
    effective_contracts_closed,
    effective_contracts_open,
    effective_multiplier,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
    parse_exp_to_ms,
)
from src.application.config_loader import resolve_data_config_path
from src.application.positions.reporting import build_monthly_income_report
from src.application.ledger.bootstrap import load_option_positions_repo
from src.application.ledger.repository import require_option_positions_read_repo
from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest
from src.infrastructure.feishu_bitable import parse_note_kv, safe_float


def _resolve_data_config_for_config_path(
    *,
    base: Path,
    data_config: str | Path | None,
    config_path: str | Path | None = None,
) -> Path:
    if config_path is None or not str(config_path).strip():
        return resolve_data_config_path(base=base, data_config=data_config)
    resolved_config = Path(config_path).expanduser()
    if not resolved_config.is_absolute():
        resolved_config = resolved_config.resolve()
    if data_config is not None and str(data_config).strip():
        path = Path(data_config).expanduser()
        if not path.is_absolute():
            path = (resolved_config.parent / path).resolve()
        return path
    env_ref = str(os.environ.get("OM_DATA_CONFIG") or "").strip()
    if env_ref:
        return Path(env_ref).expanduser().resolve()
    return (resolved_config.parent / "portfolio.runtime.json").resolve()


def resolve_position_repo(
    *,
    base: Path,
    data_config: str | Path | None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> tuple[Path, Any]:
    resolved_data_config = _resolve_data_config_for_config_path(
        base=base,
        data_config=data_config,
        config_path=config_path,
    )
    return resolved_data_config, load_option_positions_repo(
        resolved_data_config,
        config_path=config_path,
        runtime_root=runtime_root,
    )


def resolve_position_repo_from_config(
    *,
    base: Path,
    cfg: dict[str, Any] | None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> tuple[Path, Any]:
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) and isinstance(cfg.get("portfolio"), dict) else {}
    data_config_ref = data_config
    if data_config_ref is None or not str(data_config_ref).strip():
        data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    return resolve_position_repo(
        base=base,
        data_config=data_config_ref,
        config_path=config_path,
        runtime_root=runtime_root,
    )


def resolve_position_data_config_path(
    *,
    base: Path,
    cfg: dict[str, Any] | None = None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) and isinstance(cfg.get("portfolio"), dict) else {}
    data_config_ref = data_config
    if data_config_ref is None or not str(data_config_ref).strip():
        data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    return _resolve_data_config_for_config_path(
        base=base,
        data_config=data_config_ref,
        config_path=config_path,
    )


def canonicalize_position_lot_fields(fields: dict[str, Any]) -> dict[str, Any]:
    raw = dict(fields or {})
    note = str(raw.get("note") or "")
    expiration = raw.get("expiration")
    expiration_ymd = (
        str(raw.get("expiration_ymd") or raw.get("exp") or expiration_timestamp_to_ymd(expiration) or "").strip() or None
    )
    locked_shares = safe_float(raw.get("underlying_share_locked"))
    if locked_shares is None:
        locked_shares = safe_float(raw.get("underlying_shares_locked"))

    normalized = dict(raw)
    normalized.update(
        {
            "broker": normalize_broker(raw.get("broker")) or None,
            "account": normalize_account(raw.get("account")) or raw.get("account"),
            "symbol": (str(raw.get("symbol") or "").strip().upper() or None),
            "option_type": normalize_option_type(raw.get("option_type") or parse_note_kv(note, "option_type")) or None,
            "side": normalize_side(raw.get("side") or parse_note_kv(note, "side")) or None,
            "status": normalize_status(raw.get("status") or parse_note_kv(note, "status")) or None,
            "currency": normalize_currency(raw.get("currency")) or raw.get("currency") or None,
            "contracts": effective_contracts(raw),
            "contracts_open": effective_contracts_open(raw),
            "contracts_closed": effective_contracts_closed(raw),
            "multiplier": effective_multiplier(raw),
            "premium": raw.get("premium") if raw.get("premium") is not None else parse_note_kv(note, "premium_per_share"),
            "underlying_share_locked": locked_shares,
            "cash_secured_amount": safe_float(raw.get("cash_secured_amount")),
            "close_type": normalize_close_type(raw.get("close_type")) if raw.get("close_type") else None,
            "position_id": (str(raw.get("position_id") or raw.get("position_key") or "").strip() or None),
            "source_event_id": (str(raw.get("source_event_id") or "").strip() or None),
            "last_close_event_id": (str(raw.get("last_close_event_id") or "").strip() or None),
            "expiration_ymd": expiration_ymd,
        }
    )
    strike = safe_float(raw.get("strike"))
    if strike is not None:
        normalized["strike"] = strike
    if normalized.get("expiration") in (None, "") and expiration_ymd:
        normalized["expiration"] = parse_exp_to_ms(expiration_ymd)
    return normalized


def canonicalize_position_lot_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": item.get("record_id"),
        "fields": canonicalize_position_lot_fields(item.get("fields") or {}),
    }


def load_position_lot_records(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    _ = base
    try:
        primary_repo = require_option_positions_read_repo(repo)
    except Exception:
        return []
    try:
        projected = primary_repo.list_position_lots()
    except Exception:
        return []
    if isinstance(projected, list):
        return projected
    return []


def load_canonical_position_lot_records(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    return [canonicalize_position_lot_record(item) for item in load_position_lot_records(repo, base=base)]


def resolve_position_lot_records(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any, list[dict[str, Any]]]:
    resolved_data_config, repo = resolve_position_repo(base=base, data_config=data_config)
    return resolved_data_config, repo, load_position_lot_records(repo, base=base)


def build_position_lot_view(
    item: dict[str, Any],
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    record = canonicalize_position_lot_record(item)
    fields = record.get("fields") or {}
    expiration_date = expiration_timestamp_to_date(fields.get("expiration"))
    resolved_as_of_date = as_of_date or datetime.now(EXPIRATION_DATE_TZ).date()
    days_to_expiration = (expiration_date - resolved_as_of_date).days if expiration_date is not None else None
    return {
        "record_id": record.get("record_id"),
        "fields": fields,
        "position_id": fields.get("position_id"),
        "broker": fields.get("broker"),
        "account": fields.get("account"),
        "symbol": fields.get("symbol"),
        "option_type": fields.get("option_type"),
        "side": fields.get("side"),
        "status": fields.get("status"),
        "strike": fields.get("strike"),
        "multiplier": fields.get("multiplier"),
        "expiration": fields.get("expiration"),
        "expiration_ymd": fields.get("expiration_ymd"),
        "expiration_date": expiration_date,
        "days_to_expiration": days_to_expiration,
        "contracts": fields.get("contracts"),
        "contracts_open": fields.get("contracts_open"),
        "contracts_closed": fields.get("contracts_closed"),
        "currency": fields.get("currency"),
        "cash_secured_amount": fields.get("cash_secured_amount"),
        "underlying_share_locked": fields.get("underlying_share_locked"),
        "premium": fields.get("premium"),
        "opened_at": fields.get("opened_at"),
        "closed_at": fields.get("closed_at"),
        "last_action_at": fields.get("last_action_at"),
        "close_type": fields.get("close_type"),
        "close_reason": fields.get("close_reason"),
        "note": fields.get("note"),
    }


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
    resolved_as_of_date = (
        datetime.fromtimestamp(int(as_of_ms) / 1000, tz=EXPIRATION_DATE_TZ).date()
        if as_of_ms is not None
        else datetime.now(EXPIRATION_DATE_TZ).date()
    )
    for item in load_canonical_position_lot_records(repo):
        view = build_position_lot_view(item, as_of_date=resolved_as_of_date)
        if normalized_broker and view.get("broker") != normalized_broker:
            continue
        if normalized_account and view.get("account") != normalized_account:
            continue
        normalized_status = view.get("status")
        if status != "all" and normalized_status != status:
            continue
        days_to_expiration = view.get("days_to_expiration")
        if expiration_within_days is not None:
            if days_to_expiration is None or days_to_expiration < 0 or days_to_expiration > int(expiration_within_days):
                continue
        rows.append(
            {
                "record_id": view.get("record_id"),
                "broker": view.get("broker"),
                "account": view.get("account"),
                "symbol": view.get("symbol"),
                "option_type": view.get("option_type"),
                "side": view.get("side"),
                "strike": view.get("strike"),
                "multiplier": view.get("multiplier"),
                "expiration": view.get("expiration"),
                "expiration_ymd": view.get("expiration_ymd"),
                "days_to_expiration": days_to_expiration,
                "contracts": view.get("contracts"),
                "contracts_open": view.get("contracts_open"),
                "contracts_closed": view.get("contracts_closed"),
                "currency": view.get("currency"),
                "cash_secured_amount": view.get("cash_secured_amount"),
                "underlying_share_locked": view.get("underlying_share_locked"),
                "close_type": view.get("close_type"),
                "close_reason": view.get("close_reason"),
                "status": normalized_status,
                "note": view.get("note"),
            }
        )
    return rows[: max(limit, 1)]


def build_position_monthly_income_report(
    repo: Any,
    *,
    base: Path,
    broker: str,
    account: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    primary_repo = getattr(repo, "primary_repo", repo)
    list_trade_events = getattr(primary_repo, "list_trade_events", None)
    raw_trade_events = list_trade_events() if callable(list_trade_events) else None
    trade_events = raw_trade_events if isinstance(raw_trade_events, list) else None
    return build_monthly_income_report(
        load_canonical_position_lot_records(repo, base=base),
        account=account,
        broker=broker,
        month=month,
        rates=get_exchange_rates_or_fetch_latest(
            cache_path=(base / "output" / "state" / "rate_cache.json").resolve(),
        ),
        trade_events=trade_events,
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
