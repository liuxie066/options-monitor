from __future__ import annotations

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
    normalize_option_type,
    normalize_side,
    normalize_status,
    parse_exp_to_ms,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import canonical_symbol
from src.application.config_loader import resolve_data_config_path
from src.application.settings import build_effective_env
from src.application.ledger.bootstrap import load_option_positions_repo
from src.application.ledger.repository import require_option_positions_read_repo
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
    env_ref = str(build_effective_env().get("OM_DATA_CONFIG") or "").strip()
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


def open_performance_evidence_repository(repo: Any) -> Any:
    """Open the performance-evidence repository that shares the ledger SQLite file."""
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    db_path = getattr(repo, "db_path", None)
    if db_path in (None, ""):
        ledger_store = getattr(repo, "ledger_store", None)
        db_path = getattr(ledger_store, "sqlite_path", None)
    if db_path in (None, ""):
        raise ValueError("position ledger does not expose its SQLite path")
    return PerformanceEvidenceSQLiteRepository(Path(db_path))


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
    primary_repo = require_option_positions_read_repo(repo)
    projected = primary_repo.list_position_lots()
    if not isinstance(projected, list):
        raise TypeError("position lot repository returned a non-list payload")
    return projected


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
    status = str(fields.get("status") or "").strip().lower()
    expiration_state = "unknown" if days_to_expiration is None else ("expired" if days_to_expiration < 0 else "active")
    state_warning = "expired_position_marked_open" if expiration_state == "expired" and status == "open" else None
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
        "expiration_state": expiration_state,
        "state_warning": state_warning,
        "contracts": fields.get("contracts"),
        "contracts_open": fields.get("contracts_open"),
        "contracts_closed": fields.get("contracts_closed"),
        "currency": fields.get("currency"),
        "cash_secured_amount": fields.get("cash_secured_amount"),
        "cash_secured_amount_role": "assignment_collateral" if fields.get("cash_secured_amount") not in (None, "") else None,
        "underlying_share_locked": fields.get("underlying_share_locked"),
        "premium": fields.get("premium"),
        "opened_at": fields.get("opened_at"),
        "closed_at": fields.get("closed_at"),
        "last_action_at": fields.get("last_action_at"),
        "close_type": fields.get("close_type"),
        "close_reason": fields.get("close_reason"),
        "note": fields.get("note"),
    }


def _position_row_from_view(view: dict[str, Any]) -> dict[str, Any]:
    return {
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
        "days_to_expiration": view.get("days_to_expiration"),
        "expiration_state": view.get("expiration_state"),
        "state_warning": view.get("state_warning"),
        "contracts": view.get("contracts"),
        "contracts_open": view.get("contracts_open"),
        "contracts_closed": view.get("contracts_closed"),
        "currency": view.get("currency"),
        "cash_secured_amount": view.get("cash_secured_amount"),
        "cash_secured_amount_role": view.get("cash_secured_amount_role"),
        "underlying_share_locked": view.get("underlying_share_locked"),
        "close_type": view.get("close_type"),
        "close_reason": view.get("close_reason"),
        "status": view.get("status"),
        "note": view.get("note"),
    }


def list_open_short_assignment_rows(
    repo: Any,
    *,
    accounts: list[str],
) -> list[dict[str, Any]]:
    """Strictly read every open short put/call needed by the stress scenario."""

    primary_repo = require_option_positions_read_repo(repo)
    projected = primary_repo.list_position_lots()
    if not isinstance(projected, list):
        raise TypeError("position lot repository must return a list")
    normalized_accounts = {
        normalize_account(account)
        for account in accounts
        if normalize_account(account)
    }
    rows: list[dict[str, Any]] = []
    for item in projected:
        view = build_position_lot_view(item)
        if normalize_account(view.get("account")) not in normalized_accounts:
            continue
        if view.get("status") != "open" or view.get("side") != "short":
            continue
        if view.get("option_type") not in {"put", "call"}:
            continue
        rows.append(_position_row_from_view(view))
    rows.sort(key=_position_row_sort_key)
    return rows


def list_position_rows(
    repo: Any,
    *,
    broker: str,
    account: str | None = None,
    status: str = "open",
    limit: int = 50,
    expiration_within_days: int | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    side: str | None = None,
    strike: float | None = None,
    expiration_exact: str | None = None,
    expiration_month: str | None = None,
    expiration_before: str | None = None,
    expiration_after: str | None = None,
    as_of_ms: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized_broker = normalize_broker(broker)
    normalized_account = normalize_account(account) if account else None
    normalized_symbol = canonical_symbol(symbol) if symbol else None
    normalized_option_type = normalize_option_type(option_type) if option_type else None
    normalized_side = normalize_side(side) if side else None
    normalized_strike = float(strike) if strike is not None else None
    exact_expiration = _parse_filter_date(expiration_exact)
    before_expiration = _parse_filter_date(expiration_before)
    after_expiration = _parse_filter_date(expiration_after)
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
        if normalized_symbol and canonical_symbol(view.get("symbol")) != normalized_symbol:
            continue
        if normalized_option_type and view.get("option_type") != normalized_option_type:
            continue
        if normalized_side and view.get("side") != normalized_side:
            continue
        if normalized_strike is not None:
            raw_strike = view.get("strike")
            if raw_strike is None:
                continue
            try:
                if float(raw_strike) != normalized_strike:
                    continue
            except Exception:
                continue
        normalized_status = view.get("status")
        if status != "all" and normalized_status != status:
            continue
        expiration_ymd = _parse_filter_date(view.get("expiration_ymd") or view.get("expiration"))
        if exact_expiration is not None and expiration_ymd != exact_expiration:
            continue
        if expiration_month and not str(view.get("expiration_ymd") or view.get("expiration") or "").startswith(expiration_month):
            continue
        if before_expiration is not None and (expiration_ymd is None or expiration_ymd > before_expiration):
            continue
        if after_expiration is not None and (expiration_ymd is None or expiration_ymd < after_expiration):
            continue
        days_to_expiration = view.get("days_to_expiration")
        if expiration_within_days is not None:
            if days_to_expiration is None or days_to_expiration < 0 or days_to_expiration > int(expiration_within_days):
                continue
        rows.append(_position_row_from_view(view))
    rows.sort(key=_position_row_sort_key)
    return rows[: max(limit, 1)]


def _parse_filter_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _position_row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    expiration_date = _parse_filter_date(row.get("expiration_ymd")) or expiration_timestamp_to_date(row.get("expiration"))
    strike = safe_float(row.get("strike"))
    return (
        expiration_date is None,
        expiration_date or date.max,
        str(row.get("account") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        str(row.get("option_type") or ""),
        strike is None,
        strike if strike is not None else float("inf"),
        str(row.get("record_id") or ""),
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
