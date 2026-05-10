from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
    expiration_timestamp_to_date,
    expiration_timestamp_to_ymd,
)
from scripts.exchange_rates import get_exchange_rates_or_fetch_latest
from scripts.config_loader import resolve_data_config_path
from scripts.feishu_bitable import parse_note_kv, safe_float
from scripts.option_positions_core.domain import (
    effective_contracts,
    effective_contracts_closed,
    effective_contracts_open,
    effective_multiplier,
    parse_exp_to_ms,
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
from src.application.option_positions_sync_config import apply_option_positions_runtime_config
from src.application.option_positions_v2_service import load_option_positions_v2_records


def resolve_option_positions_repo(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any]:
    resolved_data_config = resolve_data_config_path(base=base, data_config=data_config)
    return resolved_data_config, load_option_positions_repo(resolved_data_config)


def resolve_option_positions_repo_from_config(
    *,
    base: Path,
    cfg: dict[str, Any] | None,
    data_config: str | Path | None = None,
) -> tuple[Path, Any]:
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) and isinstance(cfg.get("portfolio"), dict) else {}
    data_config_ref = data_config
    if data_config_ref is None or not str(data_config_ref).strip():
        data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    resolved_data_config, repo = resolve_option_positions_repo(base=base, data_config=data_config_ref)
    apply_option_positions_runtime_config(repo, cfg)
    return resolved_data_config, repo


def canonicalize_option_position_fields(fields: dict[str, Any]) -> dict[str, Any]:
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


def canonicalize_option_position_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": item.get("record_id"),
        "fields": canonicalize_option_position_fields(item.get("fields") or {}),
    }


def load_canonical_option_position_records(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    return [canonicalize_option_position_record(item) for item in load_option_position_records(repo, base=base)]


def build_option_position_view(
    item: dict[str, Any],
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    record = canonicalize_option_position_record(item)
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


def load_option_position_records(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    resolved_v2_base = base
    if resolved_v2_base is None:
        data_config = getattr(repo, "data_config_path", None)
        db_path = getattr(getattr(repo, "primary_repo", repo), "db_path", None)
        resolved_v2_base = Path(str(data_config)).resolve().parent if data_config else (Path(str(db_path)).resolve().parent if db_path else None)
    if resolved_v2_base is not None:
        try:
            compat = load_option_positions_v2_records(base=resolved_v2_base, repo=repo)
            if compat.records:
                return compat.records
        except Exception:
            pass
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
    return resolved_data_config, repo, load_option_position_records(repo, base=base)


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
    for item in load_canonical_option_position_records(repo):
        view = build_option_position_view(item, as_of_date=resolved_as_of_date)
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


def build_option_positions_monthly_income_report(
    repo: Any,
    *,
    base: Path,
    broker: str,
    account: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    return build_monthly_income_report(
        load_canonical_option_position_records(repo, base=base),
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
