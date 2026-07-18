from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.feishu_bitable import safe_float
from domain.domain.assigned_stock import project_assigned_stock_lifecycle
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from domain.domain.fee_calc import (
    FUTU_HK_FEE_SCHEDULE_URL,
    FUTU_US_FEE_SCHEDULE_URL,
    calc_futu_option_fee,
    calc_futu_stock_fee,
    extract_actual_fees,
)
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.events import validate_trade_event
from domain.domain.ledger.position_fields import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_status,
    norm_symbol,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import normalize_trade_side


def parse_event_at_ms(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(float(value))
    except Exception:
        pass
    try:
        s = str(value).strip()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def month_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m")


def _build_exchange_rate_converter(rates: dict[str, Any] | None) -> CurrencyConverter:
    rates_map = rates.get("rates") if isinstance(rates, dict) and isinstance(rates.get("rates"), dict) else rates
    usdcny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    if isinstance(rates_map, dict):
        try:
            raw_usdcny = rates_map.get("USDCNY")
            usdcny_exchange_rate = float(raw_usdcny) if raw_usdcny not in (None, "") else None
        except Exception:
            usdcny_exchange_rate = None
        try:
            raw_hkdcny = rates_map.get("HKDCNY")
            cny_per_hkd_exchange_rate = float(raw_hkdcny) if raw_hkdcny not in (None, "") else None
        except Exception:
            cny_per_hkd_exchange_rate = None
    usd_per_cny_exchange_rate = (
        (1.0 / usdcny_exchange_rate) if usdcny_exchange_rate and usdcny_exchange_rate > 0 else None
    )
    return CurrencyConverter(
        ExchangeRates(
            usd_per_cny=usd_per_cny_exchange_rate,
            cny_per_hkd=cny_per_hkd_exchange_rate,
        )
    )


def _maybe_to_cny(converter: CurrencyConverter, amount: float, currency: str) -> float | None:
    out = converter.native_to_cny(float(amount), native_ccy=str(currency or "").upper())
    return round(float(out), 6) if out is not None else None


def _round_money(value: float | int | None) -> float:
    return round(float(value or 0.0), 6)


def _amount(price: Any, multiplier: Any, contracts: Any) -> float:
    return _round_money(float(price or 0.0) * int(float(multiplier or 0)) * int(float(contracts or 0)))


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload")
    return payload if isinstance(payload, dict) else {}


def _event_ts(event: dict[str, Any]) -> int | None:
    return parse_event_at_ms(event.get("trade_time_ms"))


def _event_month(event: dict[str, Any]) -> str | None:
    ts = _event_ts(event)
    return month_from_ms(ts) if ts is not None else None


def _event_position_side(event: dict[str, Any]) -> str | None:
    side = str(event.get("side") or "").strip().lower()
    effect = str(event.get("position_effect") or "").strip().lower()
    if effect == "open":
        if side == "sell":
            return "short"
        if side == "buy":
            return "long"
    if effect == "close":
        if side == "buy":
            return "short"
        if side == "sell":
            return "long"
    return None


def _is_expire_close_event(event: dict[str, Any]) -> bool:
    payload = _event_payload(event)
    tokens = {
        str(event.get("event_type") or "").strip().lower(),
        str(payload.get("mode") or "").strip().lower(),
        str(payload.get("close_type") or "").strip().lower(),
        str(payload.get("close_reason") or "").strip().lower(),
        str(event.get("source_name") or "").strip().lower(),
    }
    return EXPIRE_AUTO_CLOSE in tokens or "expired" in tokens or "auto_close_expired_positions" in tokens


def _event_close_type(event: dict[str, Any]) -> str:
    if _is_expire_close_event(event):
        return EXPIRE_AUTO_CLOSE
    payload = _event_payload(event)
    for value in (
        event.get("event_type"),
        payload.get("close_type"),
        payload.get("close_reason"),
        payload.get("mode"),
    ):
        token = str(value or "").strip().lower()
        if token in {"assignment", "exercise"}:
            return token
        if token in {BUY_TO_CLOSE, "sell_to_close"}:
            return token
    return ""


def _event_stock_settlement(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    raw = payload.get("stock_settlement")
    return raw if isinstance(raw, dict) else {}


def _event_key(event: dict[str, Any], position_side: str | None = None) -> tuple[Any, ...]:
    return (
        normalize_broker(event.get("broker")),
        normalize_account(event.get("account")),
        norm_symbol(event.get("symbol") or ""),
        normalize_option_type(event.get("option_type")),
        position_side or _event_position_side(event),
        event.get("strike"),
        str(event.get("expiration_ymd") or "").strip() or None,
        normalize_currency(event.get("currency")) or "USD",
    )


def _voided_event_ids(events: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for event in events:
        target = _valid_void_target_event_id(event)
        if target:
            out.add(target)
    return out


def _valid_void_target_event_id(event: dict[str, Any]) -> str | None:
    if str(event.get("event_type") or "").strip().lower() != "void":
        return None
    target = str(event.get("target_event_id") or "").strip()
    if not target:
        return None
    raw_contract_key = event.get("contract_key")
    if not isinstance(raw_contract_key, dict) or event.get("event_time_ms") in (None, ""):
        return None
    try:
        decoded = TradeEvent(
            event_id=str(event.get("event_id") or "").strip(),
            event_type="void",
            event_time_ms=int(event.get("event_time_ms") or 0),
            contract_key=ContractKey.from_values(
                broker=raw_contract_key.get("broker"),
                account=raw_contract_key.get("account"),
                underlying_symbol=raw_contract_key.get("underlying_symbol") or raw_contract_key.get("symbol"),
                option_type=raw_contract_key.get("option_type"),
                position_side=raw_contract_key.get("position_side") or raw_contract_key.get("side"),
                strike=raw_contract_key.get("strike"),
                expiration_ymd=raw_contract_key.get("expiration_ymd") or raw_contract_key.get("expiration"),
            ),
            contracts=int(event.get("contracts") or 0),
            price=float(event.get("price") or 0.0),
            currency=str(event.get("currency") or ""),
            source=str(event.get("source") or event.get("source_name") or ""),
            multiplier=float(event.get("multiplier") or 0.0),
            fees=float(event.get("fees") or 0.0),
            target_event_id=target,
            raw_payload=dict(event.get("raw_payload") or {}),
        )
    except Exception:
        return None
    if any(item.severity == "error" for item in validate_trade_event(decoded)):
        return None
    return target


def _trade_events_as_of(events: list[dict[str, Any]], as_of_ms: int | None) -> list[dict[str, Any]]:
    if as_of_ms is None:
        return [dict(event) for event in events]
    cutoff = int(as_of_ms)
    return [
        dict(event)
        for event in events
        if (event_time := _event_ts(event)) is not None and event_time <= cutoff
    ]


def _active_trade_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    voided = _voided_event_ids(events)
    out: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        if str(event.get("position_effect") or "").strip().lower() == "void":
            continue
        if event_id and event_id in voided:
            continue
        out.append(dict(event))
    return sorted(out, key=lambda x: (int(_event_ts(x) or 0), str(x.get("event_id") or "")))


def _event_strategy(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return str(payload.get("strategy") or event.get("strategy") or "").strip().lower()


def _event_leg_role(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return str(payload.get("leg_role") or event.get("leg_role") or "").strip().lower()


def _event_group_id(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return str(
        payload.get("strategy_group_id")
        or payload.get("group_id")
        or event.get("strategy_group_id")
        or event.get("group_id")
        or ""
    ).strip()


def _empty_summary_bucket(month: str, account: str, currency: str) -> dict[str, Any]:
    return {
        "month": month,
        "account": account,
        "currency": currency,
        "cash_in_gross": 0.0,
        "cash_in_gross_cny": 0.0,
        "cash_in_gross_cny_missing": False,
        "cash_out_gross": 0.0,
        "cash_out_gross_cny": 0.0,
        "cash_out_gross_cny_missing": False,
        "net_cashflow_gross": 0.0,
        "net_cashflow_gross_cny": 0.0,
        "net_cashflow_gross_cny_missing": False,
        "assignment_stock_cash_in_gross": 0.0,
        "assignment_stock_cash_in_gross_cny": 0.0,
        "assignment_stock_cash_in_gross_cny_missing": False,
        "assignment_stock_cash_out_gross": 0.0,
        "assignment_stock_cash_out_gross_cny": 0.0,
        "assignment_stock_cash_out_gross_cny_missing": False,
        "assignment_stock_net_cashflow_gross": 0.0,
        "assignment_stock_net_cashflow_gross_cny": 0.0,
        "assignment_stock_net_cashflow_gross_cny_missing": False,
        "assignment_stock_shares_bought": 0,
        "assignment_stock_shares_sold": 0,
        "realized_pnl_gross": 0.0,
        "realized_pnl_gross_cny": 0.0,
        "realized_pnl_gross_cny_missing": False,
        "realized_short_pnl_gross": 0.0,
        "realized_long_pnl_gross": 0.0,
        "yield_enhancement_realized_pnl_gross": 0.0,
        "yield_enhancement_realized_pnl_gross_cny": 0.0,
        "yield_enhancement_realized_pnl_gross_cny_missing": False,
        "open_basis_lifecycle_pnl_gross": 0.0,
        "open_basis_lifecycle_pnl_gross_cny": 0.0,
        "open_basis_lifecycle_pnl_gross_cny_missing": False,
        "short_open_premium_gross": 0.0,
        "long_open_cost_gross": 0.0,
        "close_cost_gross": 0.0,
        "close_proceeds_gross": 0.0,
        "realized_gross": 0.0,
        "realized_gross_cny": 0.0,
        "realized_gross_cny_missing": False,
        "closed_contracts": 0,
        "positions": 0,
        "premium_received_gross": 0.0,
        "premium_received_gross_cny": 0.0,
        "premium_received_gross_cny_missing": False,
        "premium_contracts": 0,
        "premium_positions": 0,
    }


def _summary_bucket(summary: dict[str, dict[str, Any]], month: str, account: str, currency: str) -> dict[str, Any]:
    key = f"{month}|{account}|{currency}"
    return summary.setdefault(key, _empty_summary_bucket(month, account, currency))


def _add_money(
    bucket: dict[str, Any],
    field: str,
    amount: float,
    *,
    converter: CurrencyConverter,
    currency: str,
) -> None:
    bucket[field] = _round_money(float(bucket.get(field, 0.0) or 0.0) + amount)
    cny_field = f"{field}_cny"
    missing_field = f"{cny_field}_missing"
    if cny_field not in bucket:
        return
    converted = _maybe_to_cny(converter, amount, currency)
    if converted is None:
        bucket[missing_field] = True
    elif not bucket.get(missing_field):
        bucket[cny_field] = _round_money(float(bucket.get(cny_field, 0.0) or 0.0) + converted)


def _finalize_summary_rows(summary: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = sorted(summary.values(), key=lambda x: (str(x["month"]), str(x["account"]), str(x["currency"])))
    for row in rows:
        for key in list(row.keys()):
            if not key.endswith("_missing"):
                continue
            value_key = key.removesuffix("_missing")
            row[value_key] = None if row.pop(key) else _round_money(row.get(value_key, 0.0))
    return rows


def _month_elapsed_days(month: str, *, now_fn: Any = None) -> int:
    try:
        year_s, month_s = str(month).split("-", 1)
        year = int(year_s)
        month_num = int(month_s)
        _, days_in_month = calendar.monthrange(year, month_num)
    except Exception:
        return 0
    now = (now_fn or (lambda: datetime.now(ZoneInfo("Asia/Shanghai"))))()
    if isinstance(now, datetime):
        now_bj = now.astimezone(ZoneInfo("Asia/Shanghai")) if now.tzinfo else now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        current_year = now_bj.year
        current_month = now_bj.month
        current_day = now_bj.day
    else:
        current_year = int(getattr(now, "year", 0) or 0)
        current_month = int(getattr(now, "month", 0) or 0)
        current_day = int(getattr(now, "day", 0) or 0)
    if (year, month_num) < (current_year, current_month):
        return int(days_in_month)
    if (year, month_num) == (current_year, current_month):
        return max(1, min(int(current_day), int(days_in_month)))
    return 0


def _add_ccy_amount(bucket: dict[str, float], currency: str, amount: Any) -> None:
    value = safe_float(amount)
    if value is None:
        return
    ccy = normalize_currency(currency) or str(currency or "").upper()
    if not ccy:
        return
    bucket[ccy] = _round_money(float(bucket.get(ccy, 0.0) or 0.0) + float(value))


def _current_cash_secured_by_account_from_records(
    records: list[dict[str, Any]],
    *,
    account_norm: str | None,
    broker_norm: str | None,
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for rec in records:
        fields = rec.get("fields") or rec
        if not isinstance(fields, dict):
            continue
        if account_norm and normalize_account(fields.get("account")) != account_norm:
            continue
        if broker_norm and normalize_broker(fields.get("broker")) != broker_norm:
            continue
        if normalize_status(fields.get("status")) != "open":
            continue
        amount = safe_float(fields.get("cash_secured_amount"))
        if amount is None or amount <= 0:
            continue
        account = normalize_account(fields.get("account")) or "-"
        currency = normalize_currency(fields.get("currency")) or "USD"
        _add_ccy_amount(out.setdefault(account, {}), currency, amount)
    return out


def _current_cash_secured_by_account_from_event_lots(
    open_lots: list[dict[str, Any]],
    *,
    account_norm: str | None,
    broker_norm: str | None,
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for lot in open_lots:
        if account_norm and normalize_account(lot.get("account")) != account_norm:
            continue
        if broker_norm and normalize_broker(lot.get("broker")) != broker_norm:
            continue
        if str(lot.get("position_side") or "").strip().lower() != "short":
            continue
        if normalize_option_type(lot.get("option_type")) != "put":
            continue
        remaining = int(float(lot.get("remaining") or 0))
        if remaining <= 0:
            continue
        strike = safe_float(lot.get("strike"))
        multiplier = safe_float(lot.get("multiplier"))
        if strike is None or multiplier is None or strike <= 0 or multiplier <= 0:
            continue
        account = normalize_account(lot.get("account")) or "-"
        currency = normalize_currency(lot.get("currency")) or "USD"
        _add_ccy_amount(out.setdefault(account, {}), currency, float(strike) * float(multiplier) * remaining)
    return out


def _matching_record_fields(
    records: list[dict[str, Any]],
    *,
    account_norm: str | None,
    broker_norm: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        fields = rec.get("fields") or rec
        if not isinstance(fields, dict):
            continue
        if account_norm and normalize_account(fields.get("account")) != account_norm:
            continue
        if broker_norm and normalize_broker(fields.get("broker")) != broker_norm:
            continue
        out.append(fields)
    return out


def _month_range_payload(month: str | None) -> dict[str, Any]:
    if not month:
        return {"month": None, "start": None, "end": None}
    try:
        year_s, month_s = str(month).split("-", 1)
        year = int(year_s)
        month_num = int(month_s)
        _, days = calendar.monthrange(year, month_num)
    except Exception:
        return {"month": month, "start": None, "end": None}
    return {"month": month, "start": f"{year:04d}-{month_num:02d}-01", "end": f"{year:04d}-{month_num:02d}-{days:02d}"}


def _return_row_is_calculable(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    if safe_float(row.get("cash_secured_cny")) is None or float(row.get("cash_secured_cny") or 0.0) <= 0:
        return False
    return any(
        safe_float(row.get(key)) is not None
        for key in (
            "net_return_rate",
            "premium_return_rate",
            "realized_return_rate",
            "net_income_cny",
            "premium_income_cny",
            "realized_pnl_cny",
        )
    )


def _missing_fields_from_warnings(warnings: list[str]) -> set[str]:
    missing: set[str] = set()
    for warning in warnings:
        text = str(warning or "").lower()
        if "missing premium" in text:
            missing.add("premium")
        if "missing closed_at" in text or "missing trade_time_ms" in text:
            missing.add("month_range")
        if "missing close_price" in text:
            missing.add("close_price")
        if "missing contracts" in text or "contracts_closed <= 0" in text or "contracts <= 0" in text:
            missing.add("contracts")
        if "missing multiplier" in text:
            missing.add("multiplier")
        if "missing cny exchange rate" in text or "exchange rate" in text:
            missing.add("currency_conversion")
        if "no matching open lot" in text or "close contracts exceed" in text:
            missing.add("closed_lots")
    return missing


def _build_monthly_income_diagnostics(
    *,
    account_norm: str | None,
    broker_norm: str | None,
    month: str | None,
    records: list[dict[str, Any]],
    trade_events: list[dict[str, Any]] | None,
    summary_rows: list[dict[str, Any]],
    return_summary: list[dict[str, Any]],
    realized_rows: list[dict[str, Any]],
    premium_rows: list[dict[str, Any]],
    cash_secured_by_account: dict[str, dict[str, float]],
    warnings: list[str],
    calculation_method: str,
) -> list[dict[str, Any]]:
    matching_fields = _matching_record_fields(records, account_norm=account_norm, broker_norm=broker_norm)
    accounts: set[str] = {
        str(row.get("account") or "-")
        for row in [*summary_rows, *return_summary]
        if isinstance(row, dict) and str(row.get("account") or "").strip()
    }
    if account_norm:
        accounts.add(account_norm)
    if not accounts:
        accounts.update(
            normalize_account(fields.get("account")) or "-"
            for fields in matching_fields
            if normalize_account(fields.get("account"))
        )
    if not accounts:
        accounts.add("-")

    months: set[str | None] = {
        str(row.get("month") or "")
        for row in [*summary_rows, *return_summary]
        if isinstance(row, dict) and str(row.get("month") or "").strip()
    }
    if month:
        months.add(month)
    if not months:
        months.add(None)

    return_by_key = {
        (str(row.get("month") or ""), str(row.get("account") or "-")): row
        for row in return_summary
        if isinstance(row, dict)
    }
    summary_keys = {
        (str(row.get("month") or ""), str(row.get("account") or "-"))
        for row in summary_rows
        if isinstance(row, dict)
    }
    active_events = _active_trade_events(trade_events or []) if trade_events is not None else []

    diagnostics: list[dict[str, Any]] = []
    for account in sorted(accounts):
        cash_by_ccy = dict(sorted((cash_secured_by_account.get(account) or {}).items()))
        cash_secured_available = any(float(value or 0.0) > 0 for value in cash_by_ccy.values())
        matched_lots_count = sum(1 for fields in matching_fields if (normalize_account(fields.get("account")) or "-") == account)
        for diag_month in sorted(months, key=lambda value: str(value or "")):
            month_key = str(diag_month or "")
            return_row = return_by_key.get((month_key, account))
            matched_events_count = 0
            if trade_events is not None:
                for event in active_events:
                    if not _passes_report_filter(event, account, broker_norm):
                        continue
                    if str(event.get("position_effect") or "").strip().lower() not in {"open", "close"}:
                        continue
                    event_month = _event_month(event)
                    if diag_month and event_month != diag_month:
                        continue
                    matched_events_count += 1
            closed_lots_count = sum(
                1
                for row in realized_rows
                if str(row.get("account") or "-") == account and (not diag_month or row.get("month") == diag_month)
            )
            premium_rows_count = sum(
                1
                for row in premium_rows
                if str(row.get("account") or "-") == account and (not diag_month or row.get("month") == diag_month)
            )
            missing_fields = _missing_fields_from_warnings(warnings)
            if (month_key, account) not in summary_keys:
                missing_fields.add("income_rows")
            if trade_events is not None and matched_events_count == 0:
                missing_fields.add("trade_events")
            if closed_lots_count == 0:
                missing_fields.add("closed_lots")
            if premium_rows_count == 0:
                missing_fields.add("premium")
            if not cash_secured_available:
                missing_fields.add("cash_secured")
            cash_secured_conversion_missing = False
            currency_conversion_missing = "currency_conversion" in missing_fields
            missing_cny_currencies: set[str] = set()
            if isinstance(return_row, dict):
                row_cash_by_ccy = return_row.get("cash_secured_by_ccy")
                if isinstance(row_cash_by_ccy, dict) and row_cash_by_ccy:
                    next_cash_by_ccy: dict[str, float] = {}
                    for key, value in row_cash_by_ccy.items():
                        amount = safe_float(value)
                        if amount is not None:
                            next_cash_by_ccy[str(key)] = float(amount)
                    cash_by_ccy = dict(sorted(next_cash_by_ccy.items()))
                    cash_secured_available = any(float(value or 0.0) > 0 for value in cash_by_ccy.values())
                if return_row.get("cash_secured_cny") is None and cash_secured_available:
                    cash_secured_conversion_missing = True
                    currency_conversion_missing = True
                    missing_cny_currencies.update(str(currency) for currency in cash_by_ccy)
                    missing_fields.discard("cash_secured")
                elif return_row.get("cash_secured_cny") is None:
                    missing_fields.add("cash_secured")
                for cny_key, by_ccy_key in (
                    ("net_income_cny", "net_income_by_ccy"),
                    ("premium_income_cny", "premium_income_by_ccy"),
                    ("realized_pnl_cny", "realized_pnl_by_ccy"),
                ):
                    values = return_row.get(by_ccy_key)
                    if return_row.get(cny_key) is None and isinstance(values, dict) and values:
                        currency_conversion_missing = True
                        missing_cny_currencies.update(str(currency) for currency in values)
            if closed_lots_count == 0 and premium_rows_count > 0 and "closed_lots" in missing_fields:
                missing_fields.discard("closed_lots")
            if currency_conversion_missing:
                missing_fields.add("currency_conversion")

            has_income_row = (month_key, account) in summary_keys
            status = "ok" if _return_row_is_calculable(return_row) else ("empty" if not has_income_row else "incomplete")
            diagnostics.append(
                {
                    "account": account,
                    "month": diag_month,
                    "month_range": _month_range_payload(diag_month),
                    "status": status,
                    "income_record_status": "recorded" if has_income_row else "no_recorded_rows",
                    "income_amount_status": "reported" if has_income_row else "not_reported",
                    "calculation_method": calculation_method,
                    "matched_trade_events_count": matched_events_count,
                    "position_lot_snapshots_count": matched_lots_count,
                    "closed_lots_count": closed_lots_count,
                    "premium_rows_count": premium_rows_count,
                    "cash_secured_collateral_status": "reported" if cash_secured_available else "not_reported",
                    "cash_secured_collateral_conversion_missing": cash_secured_conversion_missing,
                    "currency_conversion_missing": currency_conversion_missing,
                    "missing_cny_currencies": sorted(currency for currency in missing_cny_currencies if currency),
                    "cash_secured_collateral_by_ccy": cash_by_ccy,
                    "missing_fields": sorted(missing_fields),
                    "warnings": [str(item) for item in warnings if str(item).strip()],
                }
            )
    return diagnostics


def _cny_total_or_none(
    by_ccy: dict[str, float],
    *,
    converter: CurrencyConverter,
    warnings: list[str],
    warning_prefix: str,
) -> float | None:
    total = 0.0
    missing: list[str] = []
    for currency, amount in sorted(by_ccy.items()):
        converted = _maybe_to_cny(converter, amount, currency)
        if converted is None:
            missing.append(currency)
            continue
        total += float(converted)
    if missing:
        warnings.append(f"{warning_prefix}: missing CNY exchange rate for {', '.join(missing)}")
        return None
    return _round_money(total)


def _return_income_cashflow(row: dict[str, Any]) -> float | None:
    net_cashflow = safe_float(row.get("net_cashflow_gross"))
    if net_cashflow is None:
        return None
    assignment_stock_cashflow = safe_float(row.get("assignment_stock_net_cashflow_gross")) or 0.0
    return _round_money(float(net_cashflow) - float(assignment_stock_cashflow))


def _return_income_cashflow_cny(row: dict[str, Any]) -> float | None:
    raw_income = _return_income_cashflow(row)
    if raw_income is not None and abs(float(raw_income)) < 1e-9:
        return 0.0
    if bool(row.get("net_cashflow_gross_cny_missing")) or bool(row.get("assignment_stock_net_cashflow_gross_cny_missing")):
        return None
    net_cashflow_cny = safe_float(row.get("net_cashflow_gross_cny"))
    if net_cashflow_cny is None:
        return None
    assignment_stock_cashflow_cny = safe_float(row.get("assignment_stock_net_cashflow_gross_cny")) or 0.0
    return _round_money(float(net_cashflow_cny) - float(assignment_stock_cashflow_cny))


def _rate(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or float(denominator) <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _rate_by_ccy(numerator_by_ccy: dict[str, float], denominator_by_ccy: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for currency, numerator in sorted(numerator_by_ccy.items()):
        denominator = safe_float(denominator_by_ccy.get(currency))
        rate = _rate(safe_float(numerator), denominator)
        if rate is not None:
            out[currency] = rate
    return out


def _annualized(rate: float | None, days: int) -> float | None:
    if rate is None or days <= 0:
        return None
    return round(float(rate) * 365.0 / float(days), 6)


def _build_return_summary(
    summary_rows: list[dict[str, Any]],
    *,
    cash_secured_by_account: dict[str, dict[str, float]],
    converter: CurrencyConverter,
    warnings: list[str],
    now_fn: Any = None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in summary_rows:
        month = str(row.get("month") or "").strip()
        account = normalize_account(row.get("account")) or "-"
        currency = normalize_currency(row.get("currency")) or str(row.get("currency") or "").upper()
        if not month or not currency:
            continue
        bucket = grouped.setdefault(
            (month, account),
            {
                "month": month,
                "account": account,
                "net_income_by_ccy": {},
                "premium_income_by_ccy": {},
                "realized_pnl_by_ccy": {},
                "net_income_cny": 0.0,
                "premium_income_cny": 0.0,
                "realized_pnl_cny": 0.0,
                "_net_income_cny_missing": False,
                "_premium_income_cny_missing": False,
                "_realized_pnl_cny_missing": False,
            },
        )
        _add_ccy_amount(bucket["net_income_by_ccy"], currency, _return_income_cashflow(row))
        _add_ccy_amount(bucket["premium_income_by_ccy"], currency, row.get("premium_received_gross"))
        _add_ccy_amount(bucket["realized_pnl_by_ccy"], currency, row.get("realized_pnl_gross"))
        net_income_cny = _return_income_cashflow_cny(row)
        if net_income_cny is None:
            bucket["_net_income_cny_missing"] = True
        elif not bucket.get("_net_income_cny_missing"):
            bucket["net_income_cny"] = _round_money(float(bucket.get("net_income_cny") or 0.0) + net_income_cny)
        for source_key, target_key, missing_key in (
            ("premium_received_gross_cny", "premium_income_cny", "_premium_income_cny_missing"),
            ("realized_pnl_gross_cny", "realized_pnl_cny", "_realized_pnl_cny_missing"),
        ):
            cny = safe_float(row.get(source_key))
            if cny is None:
                bucket[missing_key] = True
            elif not bucket.get(missing_key):
                bucket[target_key] = _round_money(float(bucket.get(target_key) or 0.0) + float(cny))

    out: list[dict[str, Any]] = []
    for (month, account), bucket in sorted(grouped.items(), key=lambda item: item[0]):
        cash_by_ccy = dict(sorted((cash_secured_by_account.get(account) or {}).items()))
        cash_secured_cny = _cny_total_or_none(
            cash_by_ccy,
            converter=converter,
            warnings=warnings,
            warning_prefix=f"return_summary {month} {account} cash_secured_cny",
        )
        net_income_cny = None if bucket.pop("_net_income_cny_missing") else _round_money(bucket["net_income_cny"])
        premium_income_cny = (
            None if bucket.pop("_premium_income_cny_missing") else _round_money(bucket["premium_income_cny"])
        )
        realized_pnl_cny = (
            None if bucket.pop("_realized_pnl_cny_missing") else _round_money(bucket["realized_pnl_cny"])
        )
        if net_income_cny is None:
            warnings.append(f"return_summary {month} {account} net_income_cny: missing CNY exchange rate")
        if premium_income_cny is None:
            warnings.append(f"return_summary {month} {account} premium_income_cny: missing CNY exchange rate")
        if realized_pnl_cny is None:
            warnings.append(f"return_summary {month} {account} realized_pnl_cny: missing CNY exchange rate")
        net_return_rate = _rate(net_income_cny, cash_secured_cny)
        premium_return_rate = _rate(premium_income_cny, cash_secured_cny)
        realized_return_rate = _rate(realized_pnl_cny, cash_secured_cny)
        annualized_basis_days = _month_elapsed_days(month, now_fn=now_fn)
        cash_by_ccy_sorted = dict(sorted(cash_by_ccy.items()))
        net_by_ccy = dict(sorted(bucket["net_income_by_ccy"].items()))
        premium_by_ccy = dict(sorted(bucket["premium_income_by_ccy"].items()))
        realized_by_ccy = dict(sorted(bucket["realized_pnl_by_ccy"].items()))
        out.append(
            {
                "month": month,
                "account": account,
                "cash_secured_by_ccy": cash_by_ccy_sorted,
                "cash_secured_cny": cash_secured_cny,
                "net_income_by_ccy": net_by_ccy,
                "net_income_cny": net_income_cny,
                "premium_income_by_ccy": premium_by_ccy,
                "premium_income_cny": premium_income_cny,
                "realized_pnl_by_ccy": realized_by_ccy,
                "realized_pnl_cny": realized_pnl_cny,
                "net_return_rate": net_return_rate,
                "premium_return_rate": premium_return_rate,
                "realized_return_rate": realized_return_rate,
                "net_return_rate_by_ccy": _rate_by_ccy(net_by_ccy, cash_by_ccy_sorted),
                "premium_return_rate_by_ccy": _rate_by_ccy(premium_by_ccy, cash_by_ccy_sorted),
                "realized_return_rate_by_ccy": _rate_by_ccy(realized_by_ccy, cash_by_ccy_sorted),
                "annualized_net_return_rate": _annualized(net_return_rate, annualized_basis_days),
                "annualized_premium_return_rate": _annualized(premium_return_rate, annualized_basis_days),
                "annualized_realized_return_rate": _annualized(realized_return_rate, annualized_basis_days),
                "annualized_basis_days": annualized_basis_days,
                "return_basis": "current_cash_secured",
                "calculation_method": "income_cashflow_ex_assignment_stock_cny / current_open_cash_secured_cny",
            }
        )
    return out


def _sum_optional(rows: list[dict[str, Any]], key: str) -> float | None:
    total = 0.0
    for row in rows:
        value = safe_float(row.get(key))
        if value is None:
            return None
        total += float(value)
    return _round_money(total)


def _build_combined_return_summary(return_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in return_summary:
        if not isinstance(row, dict):
            continue
        month = str(row.get("month") or "").strip()
        if not month:
            continue
        grouped.setdefault(month, []).append(row)

    out: list[dict[str, Any]] = []
    for month, rows in sorted(grouped.items()):
        cash_by_ccy: dict[str, float] = {}
        net_by_ccy: dict[str, float] = {}
        premium_by_ccy: dict[str, float] = {}
        realized_by_ccy: dict[str, float] = {}
        accounts: list[str] = []
        annualized_basis_days = 0
        for row in rows:
            account = normalize_account(row.get("account")) or str(row.get("account") or "").strip()
            if account and account not in accounts:
                accounts.append(account)
            cash_by_ccy_row = row.get("cash_secured_by_ccy") if isinstance(row.get("cash_secured_by_ccy"), dict) else {}
            net_by_ccy_row = row.get("net_income_by_ccy") if isinstance(row.get("net_income_by_ccy"), dict) else {}
            premium_by_ccy_row = (
                row.get("premium_income_by_ccy") if isinstance(row.get("premium_income_by_ccy"), dict) else {}
            )
            realized_by_ccy_row = row.get("realized_pnl_by_ccy") if isinstance(row.get("realized_pnl_by_ccy"), dict) else {}
            for currency, amount in cash_by_ccy_row.items():
                _add_ccy_amount(cash_by_ccy, str(currency), amount)
            for currency, amount in net_by_ccy_row.items():
                _add_ccy_amount(net_by_ccy, str(currency), amount)
            for currency, amount in premium_by_ccy_row.items():
                _add_ccy_amount(premium_by_ccy, str(currency), amount)
            for currency, amount in realized_by_ccy_row.items():
                _add_ccy_amount(realized_by_ccy, str(currency), amount)
            annualized_basis_days = max(annualized_basis_days, int(row.get("annualized_basis_days") or 0))

        cash_secured_cny = _sum_optional(rows, "cash_secured_cny")
        net_income_cny = _sum_optional(rows, "net_income_cny")
        premium_income_cny = _sum_optional(rows, "premium_income_cny")
        realized_pnl_cny = _sum_optional(rows, "realized_pnl_cny")
        net_return_rate = _rate(net_income_cny, cash_secured_cny)
        premium_return_rate = _rate(premium_income_cny, cash_secured_cny)
        realized_return_rate = _rate(realized_pnl_cny, cash_secured_cny)
        out.append(
            {
                "month": month,
                "account": "all",
                "account_scope": "all",
                "accounts": sorted(accounts),
                "cash_secured_by_ccy": dict(sorted(cash_by_ccy.items())),
                "cash_secured_cny": cash_secured_cny,
                "net_income_by_ccy": dict(sorted(net_by_ccy.items())),
                "net_income_cny": net_income_cny,
                "premium_income_by_ccy": dict(sorted(premium_by_ccy.items())),
                "premium_income_cny": premium_income_cny,
                "realized_pnl_by_ccy": dict(sorted(realized_by_ccy.items())),
                "realized_pnl_cny": realized_pnl_cny,
                "net_return_rate": net_return_rate,
                "premium_return_rate": premium_return_rate,
                "realized_return_rate": realized_return_rate,
                "net_return_rate_by_ccy": _rate_by_ccy(net_by_ccy, cash_by_ccy),
                "premium_return_rate_by_ccy": _rate_by_ccy(premium_by_ccy, cash_by_ccy),
                "realized_return_rate_by_ccy": _rate_by_ccy(realized_by_ccy, cash_by_ccy),
                "annualized_net_return_rate": _annualized(net_return_rate, annualized_basis_days),
                "annualized_premium_return_rate": _annualized(premium_return_rate, annualized_basis_days),
                "annualized_realized_return_rate": _annualized(realized_return_rate, annualized_basis_days),
                "annualized_basis_days": annualized_basis_days,
                "return_basis": "combined_current_cash_secured",
                "calculation_method": "sum(income_cashflow_ex_assignment_stock_cny) / sum(current_open_cash_secured_cny)",
            }
        )
    return out


def _event_detail_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("month") or ""),
        str(row.get("account") or ""),
        str(row.get("currency") or ""),
        int(row.get("event_at") or 0),
        str(row.get("event_id") or row.get("record_id") or ""),
    )


def _passes_report_filter(event: dict[str, Any], account_norm: str | None, broker_norm: str | None) -> bool:
    if account_norm and normalize_account(event.get("account")) != account_norm:
        return False
    if broker_norm and normalize_broker(event.get("broker")) != broker_norm:
        return False
    return True


def _apply_adjust_event(open_lots: list[dict[str, Any]], event: dict[str, Any]) -> None:
    payload = _event_payload(event)
    patch = payload.get("patch")
    if not isinstance(patch, dict):
        return
    target_source_event_id = str(payload.get("adjust_target_source_event_id") or "").strip()
    target_record_id = str(payload.get("record_id") or "").strip()
    for lot in open_lots:
        if target_source_event_id and str(lot.get("open_event_id") or "") != target_source_event_id:
            continue
        if not target_source_event_id and target_record_id and str(lot.get("record_id") or "") != target_record_id:
            continue
        if "premium" in patch:
            premium = safe_float(patch.get("premium"))
            if premium is not None:
                lot["price"] = float(premium)
        if "opened_at" in patch:
            opened_at = parse_event_at_ms(patch.get("opened_at"))
            if opened_at is not None:
                lot["opened_at"] = opened_at
                lot["open_month"] = month_from_ms(opened_at)
        if "contracts" in patch:
            next_contracts = int(float(patch.get("contracts") or lot.get("contracts") or 0))
            delta = next_contracts - int(lot.get("contracts") or 0)
            lot["contracts"] = next_contracts
            lot["remaining"] = max(0, int(lot.get("remaining") or 0) + delta)
        if "multiplier" in patch:
            multiplier = safe_float(patch.get("multiplier"))
            if multiplier is not None and multiplier > 0:
                lot["multiplier"] = int(multiplier) if float(multiplier).is_integer() else float(multiplier)
        if "strike" in patch:
            lot["strike"] = patch.get("strike")
        if "expiration" in patch:
            exp = parse_event_at_ms(patch.get("expiration"))
            if exp is not None:
                lot["expiration_ymd"] = datetime.fromtimestamp(exp / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        return


def _matching_open_lots(
    open_lots: list[dict[str, Any]],
    event: dict[str, Any],
    position_side: str,
) -> list[dict[str, Any]]:
    payload = _event_payload(event)
    target_source_event_id = str(payload.get("close_target_source_event_id") or "").strip()
    target_record_id = str(payload.get("record_id") or "").strip()
    candidates = [lot for lot in open_lots if int(lot.get("remaining") or 0) > 0]
    if target_source_event_id:
        explicit = [lot for lot in candidates if str(lot.get("open_event_id") or "") == target_source_event_id]
        if explicit:
            return explicit
    if target_record_id:
        explicit = [lot for lot in candidates if str(lot.get("record_id") or "") == target_record_id]
        if explicit:
            return explicit
    key = _event_key(event, position_side)
    return [lot for lot in candidates if lot.get("match_key") == key]


def _stock_settlement_cashflow_row(
    event: dict[str, Any],
    *,
    event_id: str,
    event_month: str,
    event_at: int,
    account: str,
    broker: str,
    symbol: str,
    option_type: str,
    position_side: str,
    currency: str,
    contracts: int,
    multiplier: int,
    strike: Any,
    expiration_ymd: str | None,
    strategy: str,
    leg_role: str,
    strategy_group_id: str,
    close_type: str,
) -> dict[str, Any] | None:
    if close_type != "assignment":
        return None
    stock = _event_stock_settlement(event)
    if not stock:
        return None
    stock_side = normalize_trade_side(stock.get("side") or stock.get("stock_side"))
    if stock_side not in {"buy", "sell"}:
        return None
    raw_shares = stock.get("shares") if stock.get("shares") not in (None, "") else stock.get("stock_qty")
    raw_price = stock.get("price") if stock.get("price") not in (None, "") else stock.get("stock_price")
    shares = int(abs(float(raw_shares or 0)))
    price = safe_float(raw_price)
    if shares <= 0 or price is None:
        return None
    fees = safe_float(stock.get("fees") if stock.get("fees") not in (None, "") else stock.get("fee"))
    amount = _round_money(float(price) * shares)
    cash_in = amount if stock_side == "sell" else 0.0
    cash_out = amount if stock_side == "buy" else 0.0
    settlement_currency = normalize_currency(stock.get("currency")) or currency
    return {
        "event_id": event_id,
        "event_at": event_at,
        "month": event_month,
        "account": account,
        "broker": broker,
        "symbol": symbol,
        "option_type": option_type,
        "position_side": position_side,
        "trade_action": f"{close_type}_stock_{stock_side}",
        "currency": settlement_currency,
        "contracts": contracts,
        "shares": shares,
        "price": float(price),
        "fees": _round_money(fees),
        "multiplier": multiplier,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "cash_in_gross": cash_in,
        "cash_out_gross": cash_out,
        "net_cashflow_gross": _round_money(cash_in - cash_out),
        "strategy": strategy,
        "leg_role": leg_role,
        "strategy_group_id": strategy_group_id,
        "close_type": close_type,
    }


def _stock_event_time_ms(event: dict[str, Any]) -> int | None:
    for key in ("trade_time_ms", "event_time_ms", "time_ms", "trade_time", "event_time"):
        ts = parse_event_at_ms(event.get(key))
        if ts is not None:
            return ts
    return None


def _normalize_assigned_stock_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _assigned_stock_events_as_of(value: Any, as_of_ms: int | None) -> list[dict[str, Any]]:
    events = _normalize_assigned_stock_events(value)
    if as_of_ms is None:
        return events
    cutoff = int(as_of_ms)
    return [
        event
        for event in events
        if (event_time := _stock_event_time_ms(event)) is not None and event_time <= cutoff
    ]


def _build_assigned_stock_lifecycle_report(
    trade_events: list[dict[str, Any]],
    *,
    assignment_option_rows: list[dict[str, Any]],
    option_open_lots: list[dict[str, Any]],
    assigned_stock_events: list[dict[str, Any]] | None,
    quote_snapshots: Any = None,
    stock_holdings: list[dict[str, Any]] | None = None,
    account_norm: str | None,
    broker_norm: str | None,
    month: str | None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return project_assigned_stock_lifecycle(
        trade_events,
        assignment_option_rows=assignment_option_rows,
        option_open_lots=option_open_lots,
        assigned_stock_events=assigned_stock_events,
        quote_snapshots=quote_snapshots,
        stock_holdings=stock_holdings,
        account_norm=account_norm,
        broker_norm=broker_norm,
        month=month,
        as_of_ms=as_of_ms,
    )


def _build_open_basis_rows(open_lots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for lot in open_lots:
        group_id = str(lot.get("strategy_group_id") or "").strip()
        if group_id:
            key = (
                lot.get("open_month"),
                lot.get("account"),
                lot.get("broker"),
                lot.get("currency"),
                group_id,
            )
        else:
            key = (
                lot.get("open_month"),
                lot.get("account"),
                lot.get("broker"),
                lot.get("currency"),
                lot.get("open_event_id"),
            )
        row = grouped.setdefault(
            key,
            {
                "month": lot.get("open_month"),
                "account": lot.get("account"),
                "broker": lot.get("broker"),
                "symbol": lot.get("symbol"),
                "currency": lot.get("currency"),
                "strategy": lot.get("strategy") or "",
                "strategy_group_id": group_id,
                "sell_open_premium": 0.0,
                "sell_close_cost_actual": 0.0,
                "enhancement_call_buy_cost": 0.0,
                "enhancement_call_sell_proceeds_actual": 0.0,
                "open_basis_lifecycle_pnl_gross": 0.0,
                "open_contracts": 0,
                "remaining_contracts": 0,
                "is_final": True,
                "open_event_ids": [],
            },
        )
        row["open_event_ids"].append(lot.get("open_event_id"))
        side = str(lot.get("position_side") or "")
        leg_role = str(lot.get("leg_role") or "")
        open_amount = _amount(lot.get("price"), lot.get("multiplier"), lot.get("contracts"))
        close_amount = _round_money(lot.get("close_amount"))
        remaining = int(lot.get("remaining") or 0)
        row["open_contracts"] = int(row["open_contracts"]) + int(lot.get("contracts") or 0)
        row["remaining_contracts"] = int(row["remaining_contracts"]) + remaining
        row["is_final"] = bool(row["is_final"]) and remaining == 0
        if side == "short":
            row["sell_open_premium"] = _round_money(row["sell_open_premium"] + open_amount)
            row["sell_close_cost_actual"] = _round_money(row["sell_close_cost_actual"] + close_amount)
        elif leg_role == "enhancement_call" or str(lot.get("strategy") or "") in {"combo_yield", "yield_enhancement"}:
            row["enhancement_call_buy_cost"] = _round_money(row["enhancement_call_buy_cost"] + open_amount)
            row["enhancement_call_sell_proceeds_actual"] = _round_money(
                row["enhancement_call_sell_proceeds_actual"] + close_amount
            )
        else:
            # Standalone long option attribution uses the same lifecycle field.
            row["enhancement_call_buy_cost"] = _round_money(row["enhancement_call_buy_cost"] + open_amount)
            row["enhancement_call_sell_proceeds_actual"] = _round_money(
                row["enhancement_call_sell_proceeds_actual"] + close_amount
            )
        row["open_basis_lifecycle_pnl_gross"] = _round_money(
            row["sell_open_premium"]
            - row["sell_close_cost_actual"]
            - row["enhancement_call_buy_cost"]
            + row["enhancement_call_sell_proceeds_actual"]
        )
    return sorted(
        grouped.values(),
        key=lambda x: (
            str(x.get("month")),
            str(x.get("account")),
            str(x.get("strategy_group_id")),
            str(x.get("symbol")),
        ),
    )


def _build_monthly_income_report_from_events(
    trade_events: list[dict[str, Any]],
    *,
    records: list[dict[str, Any]],
    account_norm: str | None,
    broker_norm: str | None,
    month: str | None,
    converter: CurrencyConverter,
    assigned_stock_events: list[dict[str, Any]] | None = None,
    quote_snapshots: Any = None,
    assigned_stock_holdings: list[dict[str, Any]] | None = None,
    as_of_ms: int | None = None,
    now_fn: Any = None,
) -> dict[str, Any]:
    events = _active_trade_events(trade_events)
    open_lots: list[dict[str, Any]] = []
    cashflow_rows: list[dict[str, Any]] = []
    stock_settlement_rows: list[dict[str, Any]] = []
    realized_rows: list[dict[str, Any]] = []
    premium_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for event in events:
        effect = str(event.get("position_effect") or "").strip().lower()
        if effect == "adjust":
            _apply_adjust_event(open_lots, event)
            continue
        if effect not in {"open", "close"}:
            continue
        if not _passes_report_filter(event, account_norm, broker_norm):
            continue
        event_month = _event_month(event)
        if event_month is None:
            warnings.append(f"{event.get('event_id') or '(no event_id)'}: missing trade_time_ms")
            continue
        position_side = _event_position_side(event)
        if position_side not in {"short", "long"}:
            continue
        contracts = int(float(event.get("contracts") or 0))
        multiplier = int(float(event.get("multiplier") or 0))
        if contracts <= 0 or multiplier <= 0:
            warnings.append(f"{event.get('event_id') or '(no event_id)'}: missing contracts or multiplier")
            continue
        price = float(event.get("price") or 0.0)
        currency = normalize_currency(event.get("currency")) or "USD"
        account = normalize_account(event.get("account")) or "-"
        broker = normalize_broker(event.get("broker")) or "-"
        symbol = norm_symbol(event.get("symbol") or "-")
        option_type = normalize_option_type(event.get("option_type")) or "-"
        amount = _amount(price, multiplier, contracts)
        strategy = _event_strategy(event)
        leg_role = _event_leg_role(event)
        strategy_group_id = _event_group_id(event)
        event_id = str(event.get("event_id") or "").strip()

        if effect == "open":
            open_lots.append(
                {
                    "record_id": event_id,
                    "open_event_id": event_id,
                    "match_key": _event_key(event, position_side),
                    "open_month": event_month,
                    "opened_at": int(_event_ts(event) or 0),
                    "account": account,
                    "broker": broker,
                    "symbol": symbol,
                    "option_type": option_type,
                    "position_side": position_side,
                    "currency": currency,
                    "contracts": contracts,
                    "remaining": contracts,
                    "price": price,
                    "multiplier": multiplier,
                    "strike": event.get("strike"),
                    "expiration_ymd": str(event.get("expiration_ymd") or "").strip() or None,
                    "strategy": strategy,
                    "leg_role": leg_role,
                    "strategy_group_id": strategy_group_id,
                    "close_amount": 0.0,
                    "realized_pnl": 0.0,
                    "closed_contracts": 0,
                }
            )
            continue

        close_type = _event_close_type(event)
        is_expire = close_type == EXPIRE_AUTO_CLOSE
        close_cash_amount = 0.0 if is_expire else amount
        cash_in = close_cash_amount if position_side == "long" else 0.0
        cash_out = close_cash_amount if position_side == "short" else 0.0
        close_trade_action = "expire" if is_expire else ("buy_close" if position_side == "short" else "sell_close")
        if close_type == "assignment":
            close_trade_action = "assignment_option_close"
        elif close_type == "exercise":
            close_trade_action = "exercise_option_close"
        event_at = int(_event_ts(event) or 0)
        expiration_ymd = str(event.get("expiration_ymd") or "").strip() or None
        cashflow_rows.append(
            {
                "event_id": event_id,
                "event_at": event_at,
                "month": event_month,
                "account": account,
                "broker": broker,
                "symbol": symbol,
                "option_type": option_type,
                "position_side": position_side,
                "trade_action": close_trade_action,
                "currency": currency,
                "contracts": contracts,
                "price": price,
                "multiplier": multiplier,
                "strike": event.get("strike"),
                "expiration_ymd": expiration_ymd,
                "cash_in_gross": cash_in,
                "cash_out_gross": cash_out,
                "net_cashflow_gross": _round_money(cash_in - cash_out),
                "strategy": strategy,
                "leg_role": leg_role,
                "strategy_group_id": strategy_group_id,
            }
        )
        stock_cashflow_row = _stock_settlement_cashflow_row(
            event,
            event_id=event_id,
            event_month=event_month,
            event_at=event_at,
            account=account,
            broker=broker,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            currency=currency,
            contracts=contracts,
            multiplier=multiplier,
            strike=event.get("strike"),
            expiration_ymd=expiration_ymd,
            strategy=strategy,
            leg_role=leg_role,
            strategy_group_id=strategy_group_id,
            close_type=close_type,
        )
        if stock_cashflow_row is not None:
            stock_settlement_rows.append(stock_cashflow_row)
            cashflow_rows.append(stock_cashflow_row)
        remaining_to_close = contracts
        matches = _matching_open_lots(open_lots, event, position_side)
        if not matches:
            warnings.append(f"{event_id or '(no event_id)'}: close event has no matching open lot")
            continue
        for lot in matches:
            if remaining_to_close <= 0:
                break
            qty = min(remaining_to_close, int(lot.get("remaining") or 0))
            if qty <= 0:
                continue
            open_amount = _amount(lot.get("price"), lot.get("multiplier"), qty)
            close_amount = 0.0 if is_expire else _amount(price, multiplier, qty)
            realized_pnl = (
                _round_money(open_amount - close_amount)
                if position_side == "short"
                else _round_money(close_amount - open_amount)
            )
            lot["remaining"] = int(lot.get("remaining") or 0) - qty
            lot["close_amount"] = _round_money(float(lot.get("close_amount") or 0.0) + close_amount)
            lot["realized_pnl"] = _round_money(float(lot.get("realized_pnl") or 0.0) + realized_pnl)
            lot["closed_contracts"] = int(lot.get("closed_contracts") or 0) + qty
            row_strategy = strategy or str(lot.get("strategy") or "")
            row_leg_role = leg_role or str(lot.get("leg_role") or "")
            row_group_id = strategy_group_id or str(lot.get("strategy_group_id") or "")
            realized_rows.append(
                {
                    "record_id": event_id,
                    "event_id": event_id,
                    "event_at": int(_event_ts(event) or 0),
                    "open_event_id": lot.get("open_event_id"),
                    "month": event_month,
                    "account": account,
                    "broker": broker,
                    "symbol": symbol,
                    "option_type": option_type,
                    "position_side": position_side,
                    "currency": currency,
                    "contracts_closed": qty,
                    "premium": float(lot.get("price") or 0.0),
                    "close_price": 0.0 if is_expire else price,
                    "multiplier": multiplier,
                    "strike": event.get("strike") if event.get("strike") is not None else lot.get("strike"),
                    "expiration_ymd": str(event.get("expiration_ymd") or lot.get("expiration_ymd") or "").strip() or None,
                    "open_amount_gross": open_amount,
                    "close_amount_gross": close_amount,
                    "realized_pnl_gross": realized_pnl,
                    "realized_gross": realized_pnl,
                    "close_type": (
                        close_type
                        if close_type in {EXPIRE_AUTO_CLOSE, "assignment", "exercise"}
                        else (BUY_TO_CLOSE if position_side == "short" else "sell_to_close")
                    ),
                    "closed_at": event_at,
                    "strategy": row_strategy,
                    "leg_role": row_leg_role,
                    "strategy_group_id": row_group_id,
                }
            )
            remaining_to_close -= qty
        if remaining_to_close > 0:
            warnings.append(
                f"{event_id or '(no event_id)'}: close contracts exceed matching open lots by {remaining_to_close}"
            )

    for lot in open_lots:
        open_month = str(lot.get("open_month") or "").strip()
        if not open_month:
            continue
        position_side = str(lot.get("position_side") or "").strip().lower()
        contracts = int(lot.get("contracts") or 0)
        multiplier = int(float(lot.get("multiplier") or 0))
        price = float(lot.get("price") or 0.0)
        if position_side not in {"short", "long"} or contracts <= 0 or multiplier <= 0:
            continue
        amount = _amount(price, multiplier, contracts)
        cash_in = amount if position_side == "short" else 0.0
        cash_out = amount if position_side == "long" else 0.0
        event_id = str(lot.get("open_event_id") or lot.get("record_id") or "").strip()
        cashflow_rows.append(
            {
                "event_id": event_id,
                "event_at": int(lot.get("opened_at") or 0),
                "month": open_month,
                "account": lot.get("account"),
                "broker": lot.get("broker"),
                "symbol": lot.get("symbol"),
                "option_type": lot.get("option_type"),
                "position_side": position_side,
                "trade_action": "sell_open" if position_side == "short" else "buy_open",
                "currency": lot.get("currency"),
                "contracts": contracts,
                "price": price,
                "multiplier": multiplier,
                "strike": lot.get("strike"),
                "expiration_ymd": lot.get("expiration_ymd"),
                "cash_in_gross": cash_in,
                "cash_out_gross": cash_out,
                "net_cashflow_gross": _round_money(cash_in - cash_out),
                "strategy": lot.get("strategy"),
                "leg_role": lot.get("leg_role"),
                "strategy_group_id": lot.get("strategy_group_id"),
            }
        )
        if position_side == "short":
            premium_rows.append(
                {
                    "record_id": event_id,
                    "event_id": event_id,
                    "event_at": int(lot.get("opened_at") or 0),
                    "month": open_month,
                    "account": lot.get("account"),
                    "broker": lot.get("broker"),
                    "symbol": lot.get("symbol"),
                    "currency": lot.get("currency"),
                    "contracts": contracts,
                    "premium": price,
                    "multiplier": multiplier,
                    "strike": lot.get("strike"),
                    "expiration_ymd": lot.get("expiration_ymd"),
                    "premium_received_gross": amount,
                    "opened_at": int(lot.get("opened_at") or 0),
                }
            )

    open_basis_rows = _build_open_basis_rows(open_lots)
    assigned_stock_report = _build_assigned_stock_lifecycle_report(
        events,
        assignment_option_rows=realized_rows,
        option_open_lots=open_lots,
        assigned_stock_events=assigned_stock_events,
        quote_snapshots=quote_snapshots,
        stock_holdings=assigned_stock_holdings,
        account_norm=account_norm,
        broker_norm=broker_norm,
        month=month,
        as_of_ms=as_of_ms,
    )
    warnings.extend(str(item) for item in (assigned_stock_report.get("warnings") or []) if str(item).strip())
    summary: dict[str, dict[str, Any]] = {}

    for row in cashflow_rows:
        if month and row["month"] != month:
            continue
        bucket = _summary_bucket(summary, row["month"], row["account"], row["currency"])
        _add_money(bucket, "cash_in_gross", row["cash_in_gross"], converter=converter, currency=row["currency"])
        _add_money(bucket, "cash_out_gross", row["cash_out_gross"], converter=converter, currency=row["currency"])
        _add_money(
            bucket,
            "net_cashflow_gross",
            row["net_cashflow_gross"],
            converter=converter,
            currency=row["currency"],
        )
        if row["trade_action"] == "sell_open":
            _add_money(
                bucket,
                "short_open_premium_gross",
                row["cash_in_gross"],
                converter=converter,
                currency=row["currency"],
            )
            _add_money(
                bucket,
                "premium_received_gross",
                row["cash_in_gross"],
                converter=converter,
                currency=row["currency"],
            )
            bucket["premium_contracts"] = int(bucket["premium_contracts"]) + int(row["contracts"])
            bucket["premium_positions"] = int(bucket["premium_positions"]) + 1
        elif row["trade_action"] == "buy_open":
            _add_money(
                bucket,
                "long_open_cost_gross",
                row["cash_out_gross"],
                converter=converter,
                currency=row["currency"],
            )
        elif row["trade_action"] == "buy_close":
            _add_money(bucket, "close_cost_gross", row["cash_out_gross"], converter=converter, currency=row["currency"])
        elif row["trade_action"] == "sell_close":
            _add_money(
                bucket,
                "close_proceeds_gross",
                row["cash_in_gross"],
                converter=converter,
                currency=row["currency"],
            )
        elif str(row["trade_action"]).startswith("assignment_stock_"):
            _add_money(
                bucket,
                "assignment_stock_cash_in_gross",
                row["cash_in_gross"],
                converter=converter,
                currency=row["currency"],
            )
            _add_money(
                bucket,
                "assignment_stock_cash_out_gross",
                row["cash_out_gross"],
                converter=converter,
                currency=row["currency"],
            )
            _add_money(
                bucket,
                "assignment_stock_net_cashflow_gross",
                row["net_cashflow_gross"],
                converter=converter,
                currency=row["currency"],
            )
            if row["trade_action"] == "assignment_stock_buy":
                bucket["assignment_stock_shares_bought"] = int(bucket["assignment_stock_shares_bought"]) + int(
                    row.get("shares") or 0
                )
            elif row["trade_action"] == "assignment_stock_sell":
                bucket["assignment_stock_shares_sold"] = int(bucket["assignment_stock_shares_sold"]) + int(
                    row.get("shares") or 0
                )

    for row in realized_rows:
        if month and row["month"] != month:
            continue
        bucket = _summary_bucket(summary, row["month"], row["account"], row["currency"])
        realized_pnl = float(row["realized_pnl_gross"])
        _add_money(bucket, "realized_pnl_gross", realized_pnl, converter=converter, currency=row["currency"])
        _add_money(bucket, "realized_gross", realized_pnl, converter=converter, currency=row["currency"])
        if row["position_side"] == "short":
            bucket["realized_short_pnl_gross"] = _round_money(bucket["realized_short_pnl_gross"] + realized_pnl)
        else:
            bucket["realized_long_pnl_gross"] = _round_money(bucket["realized_long_pnl_gross"] + realized_pnl)
        is_enhancement_call = row.get("leg_role") == "enhancement_call" or (
            row.get("strategy") in {"combo_yield", "yield_enhancement"} and row.get("position_side") == "long"
        )
        if is_enhancement_call:
            _add_money(
                bucket,
                "yield_enhancement_realized_pnl_gross",
                realized_pnl,
                converter=converter,
                currency=row["currency"],
            )
        bucket["closed_contracts"] = int(bucket["closed_contracts"]) + int(row["contracts_closed"])
        bucket["positions"] = int(bucket["positions"]) + 1

    for row in open_basis_rows:
        if month and row["month"] != month:
            continue
        bucket = _summary_bucket(summary, row["month"], row["account"], row["currency"])
        _add_money(
            bucket,
            "open_basis_lifecycle_pnl_gross",
            float(row["open_basis_lifecycle_pnl_gross"]),
            converter=converter,
            currency=row["currency"],
        )

    filtered_cashflow_rows = [row for row in cashflow_rows if not month or row["month"] == month]
    filtered_stock_settlement_rows = [row for row in stock_settlement_rows if not month or row["month"] == month]
    filtered_realized_rows = [row for row in realized_rows if not month or row["month"] == month]
    filtered_premium_rows = [row for row in premium_rows if not month or row["month"] == month]
    filtered_open_basis_rows = [row for row in open_basis_rows if not month or row["month"] == month]
    enhancement_rows = [
        row
        for row in filtered_realized_rows
        if row.get("leg_role") == "enhancement_call"
        or (row.get("strategy") in {"combo_yield", "yield_enhancement"} and row.get("position_side") == "long")
    ]
    summary_rows = _finalize_summary_rows(summary)
    cash_secured_by_account = _current_cash_secured_by_account_from_records(
        records,
        account_norm=account_norm,
        broker_norm=broker_norm,
    )
    if not cash_secured_by_account:
        cash_secured_by_account = _current_cash_secured_by_account_from_event_lots(
            open_lots,
            account_norm=account_norm,
            broker_norm=broker_norm,
        )
    return_summary = _build_return_summary(
        summary_rows,
        cash_secured_by_account=cash_secured_by_account,
        converter=converter,
        warnings=warnings,
        now_fn=now_fn,
    )
    combined_return_summary = _build_combined_return_summary(return_summary) if account_norm is None else []
    return {
        "summary": summary_rows,
        "return_summary": return_summary,
        "combined_return_summary": combined_return_summary,
        "diagnostics": _build_monthly_income_diagnostics(
            account_norm=account_norm,
            broker_norm=broker_norm,
            month=month,
            records=records,
            trade_events=trade_events,
            summary_rows=summary_rows,
            return_summary=return_summary,
            realized_rows=filtered_realized_rows,
            premium_rows=filtered_premium_rows,
            cash_secured_by_account=cash_secured_by_account,
            warnings=warnings,
            calculation_method="trade_events",
        ),
        "rows": sorted(filtered_realized_rows, key=_event_detail_sort_key),
        "premium_rows": sorted(filtered_premium_rows, key=_event_detail_sort_key),
        "cashflow_rows": sorted(filtered_cashflow_rows, key=_event_detail_sort_key),
        "stock_settlement_rows": sorted(filtered_stock_settlement_rows, key=_event_detail_sort_key),
        "assigned_stock_lots": assigned_stock_report.get("assigned_stock_lots") or [],
        "assignment_lifecycle_rows": assigned_stock_report.get("assignment_lifecycle_rows") or [],
        "lifecycle_efficiency_rows": assigned_stock_report.get("lifecycle_efficiency_rows") or [],
        "lifecycle_efficiency_summary": assigned_stock_report.get("lifecycle_efficiency_summary") or [],
        "assigned_stock_sale_rows": assigned_stock_report.get("assigned_stock_sale_rows") or [],
        "assigned_stock_review_rows": assigned_stock_report.get("assigned_stock_review_rows") or [],
        "realized_rows": sorted(filtered_realized_rows, key=_event_detail_sort_key),
        "open_basis_rows": filtered_open_basis_rows,
        "enhancement_rows": enhancement_rows,
        "warnings": warnings,
        "calculation_method": "trade_events",
    }


def build_monthly_income_report(
    records: list[dict[str, Any]],
    *,
    account: str | None = None,
    broker: str | None = None,
    month: str | None = None,
    rates: dict[str, Any] | None = None,
    trade_events: list[dict[str, Any]] | None = None,
    assigned_stock_events: list[dict[str, Any]] | None = None,
    quote_snapshots: Any = None,
    assigned_stock_holdings: list[dict[str, Any]] | None = None,
    as_of_ms: int | None = None,
    now_fn: Any = None,
) -> dict[str, Any]:
    account_norm = normalize_account(account) if account else None
    broker_norm = normalize_broker(broker) if broker else None
    converter = _build_exchange_rate_converter(rates)
    cutoff_trade_events = _trade_events_as_of(
        trade_events if isinstance(trade_events, list) else [],
        as_of_ms,
    )
    cutoff_assigned_stock_events = _assigned_stock_events_as_of(assigned_stock_events, as_of_ms)
    report = _build_monthly_income_report_from_events(
        cutoff_trade_events,
        records=records,
        account_norm=account_norm,
        broker_norm=broker_norm,
        month=month,
        converter=converter,
        assigned_stock_events=cutoff_assigned_stock_events,
        quote_snapshots=quote_snapshots,
        assigned_stock_holdings=assigned_stock_holdings,
        as_of_ms=as_of_ms,
        now_fn=now_fn,
    )
    report["filters"] = {
        "account": account_norm,
        "broker": broker_norm,
        "month": month,
    }
    return report
