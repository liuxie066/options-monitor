from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.feishu_bitable import safe_float
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
    effective_multiplier,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
    normalize_status,
    norm_symbol,
)
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


def _assigned_stock_lot_id(event_id: str) -> str:
    stable = str(event_id or "").strip()
    return f"assigned-stock-{stable}" if stable else "assigned-stock-unknown"


def _stock_event_id(event: dict[str, Any], *, fallback_index: int) -> str:
    for key in ("stock_event_id", "event_id", "source_deal_id", "deal_id"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return f"assigned-stock-event-{fallback_index}"


def _stock_event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "").strip().lower()


def _stock_event_time_ms(event: dict[str, Any]) -> int | None:
    for key in ("trade_time_ms", "event_time_ms", "time_ms", "trade_time", "event_time"):
        ts = parse_event_at_ms(event.get(key))
        if ts is not None:
            return ts
    return None


def _stock_event_month(event: dict[str, Any]) -> str | None:
    ts = _stock_event_time_ms(event)
    return month_from_ms(ts) if ts is not None else None


def _stock_event_shares(event: dict[str, Any]) -> int:
    raw = event.get("shares") if event.get("shares") not in (None, "") else event.get("quantity")
    try:
        return int(abs(float(raw or 0)))
    except Exception:
        return 0


def _stock_event_price(event: dict[str, Any]) -> float | None:
    return safe_float(event.get("price") if event.get("price") not in (None, "") else event.get("avg_price"))


def _stock_event_fees(event: dict[str, Any]) -> float:
    value = safe_float(event.get("fees") if event.get("fees") not in (None, "") else event.get("fee"))
    return _round_money(value)


def _source_option_lot_id(event: dict[str, Any], option_rows: list[dict[str, Any]]) -> str | None:
    explicit = str(event.get("target_lot_id") or "").strip()
    if explicit:
        return explicit
    payload = _event_payload(event)
    for key in ("target_lot_id", "record_id", "close_target_source_event_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    open_ids = sorted({str(row.get("open_event_id") or "").strip() for row in option_rows if row.get("open_event_id")})
    if len(open_ids) == 1:
        return open_ids[0]
    return None


def _source_option_open_event_id(event: dict[str, Any], option_rows: list[dict[str, Any]]) -> str | None:
    open_ids = sorted({str(row.get("open_event_id") or "").strip() for row in option_rows if row.get("open_event_id")})
    if len(open_ids) == 1:
        return open_ids[0]
    payload = _event_payload(event)
    value = str(payload.get("close_target_source_event_id") or "").strip()
    return value or None


def _option_premium_attribution(option_rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in option_rows:
        total += float(row.get("realized_pnl_gross") or 0.0)
    return _round_money(total)


def _quote_symbol(row: dict[str, Any]) -> str:
    return norm_symbol(row.get("symbol") or row.get("underlying_symbol") or "")


def _quote_time_ms(row: dict[str, Any]) -> int | None:
    for key in ("spot_time_ms", "quote_time_ms", "time_ms", "spot_time", "quote_time", "as_of_ms", "as_of"):
        ts = parse_event_at_ms(row.get(key))
        if ts is not None:
            return ts
    return None


def _quote_spot(row: dict[str, Any]) -> float | None:
    for key in ("spot", "last_price", "price", "underlying_price", "mark"):
        value = safe_float(row.get(key))
        if value is not None and value > 0:
            return float(value)
    return None


def _matching_quote(
    quote_snapshots: list[dict[str, Any]],
    lot: dict[str, Any],
    *,
    as_of_ms: int | None,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    lot_symbol = norm_symbol(lot.get("symbol") or "")
    lot_account = normalize_account(lot.get("account"))
    lot_broker = normalize_broker(lot.get("broker"))
    for idx, quote in enumerate(quote_snapshots):
        if _quote_symbol(quote) != lot_symbol:
            continue
        quote_account = normalize_account(quote.get("account")) if quote.get("account") not in (None, "") else None
        quote_broker = normalize_broker(quote.get("broker")) if quote.get("broker") not in (None, "") else None
        if quote_account and lot_account and quote_account != lot_account:
            continue
        if quote_broker and lot_broker and quote_broker != lot_broker:
            continue
        quote_time = _quote_time_ms(quote)
        if as_of_ms is not None and quote_time is None:
            continue
        if as_of_ms is not None and quote_time is not None and quote_time > int(as_of_ms):
            continue
        sort_time = int(quote_time or 0)
        candidates.append((sort_time, idx, quote))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]


def _normalize_quote_snapshots(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("symbol", key)
                rows.append(row)
            else:
                rows.append({"symbol": key, "spot": item})
        return rows
    return []


def _normalize_assigned_stock_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


_DAY_MS = 86_400_000


def _market_date(ms: int | None, *, symbol: str, currency: str) -> str | None:
    if ms is None or int(ms) <= 0:
        return None
    tz_name = "Asia/Hong_Kong" if str(symbol).endswith(".HK") or currency == "HKD" else "America/New_York"
    return datetime.fromtimestamp(int(ms) / 1000, tz=ZoneInfo(tz_name)).date().isoformat()


def _elapsed_days(start_ms: int | None, end_ms: int | None) -> float | None:
    if start_ms is None or end_ms is None or int(end_ms) < int(start_ms):
        return None
    return round((int(end_ms) - int(start_ms)) / _DAY_MS, 6)


def _actual_option_fee_fact(event: dict[str, Any], *, component: str) -> dict[str, Any] | None:
    payload = _event_payload(event)
    provenance = payload.get("fee_provenance") if isinstance(payload.get("fee_provenance"), dict) else {}
    if str(provenance.get("basis") or "").strip().lower() == "actual":
        amount = safe_float(event.get("fees"))
        if amount is not None and amount >= 0:
            return {
                "component": component,
                "basis": "actual",
                "amount": _round_money(amount),
                "source": str(provenance.get("source") or "event.fees"),
                "reason": "broker_reported_fee",
            }
    extracted = extract_actual_fees(payload)
    if extracted is None:
        return None
    return {
        "component": component,
        "basis": "actual",
        "amount": _round_money(extracted["amount"]),
        "source": str(extracted.get("source") or "broker_payload"),
        "reason": "broker_reported_fee",
        "components": list(extracted.get("components") or []),
    }


def _option_fee_fact(event: dict[str, Any], *, component: str) -> dict[str, Any]:
    actual = _actual_option_fee_fact(event, component=component)
    if actual is not None:
        return actual
    price = safe_float(event.get("price"))
    contracts = int(abs(float(event.get("contracts") or 0)))
    multiplier = int(abs(float(event.get("multiplier") or 0)))
    close_type = _event_close_type(event)
    if price == 0 and close_type in {EXPIRE_AUTO_CLOSE, "assignment", "exercise"}:
        return {
            "component": component,
            "basis": "estimated",
            "amount": 0.0,
            "source": "domain.domain.fee_calc.calc_futu_option_fee",
            "reason": "zero_price_lifecycle_option_leg",
        }
    if price is None or price <= 0 or contracts <= 0 or multiplier <= 0:
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "reason": "option_fee_inputs_incomplete",
        }
    try:
        amount = calc_futu_option_fee(
            normalize_currency(event.get("currency")) or "USD",
            price,
            contracts=contracts,
            multiplier=multiplier,
            is_sell=normalize_trade_side(event.get("side")) == "sell",
        )
    except Exception:
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "reason": "option_fee_estimate_failed",
        }
    currency = normalize_currency(event.get("currency")) or "USD"
    return {
        "component": component,
        "basis": "estimated",
        "amount": _round_money(amount),
        "source": FUTU_HK_FEE_SCHEDULE_URL if currency == "HKD" else FUTU_US_FEE_SCHEDULE_URL,
        "reason": "standard_option_fee_schedule_estimate",
    }


def _stock_fee_fact(
    value: dict[str, Any],
    *,
    component: str,
    transaction_kind: str,
) -> dict[str, Any]:
    provenance = value.get("fee_provenance") if isinstance(value.get("fee_provenance"), dict) else {}
    provenance_basis = str(provenance.get("basis") or "").strip().lower()
    explicit_amount = safe_float(value.get("fees") if value.get("fees") not in (None, "") else value.get("fee"))
    if provenance_basis in {"actual", "estimated", "missing"}:
        amount = _round_money(explicit_amount) if explicit_amount is not None and explicit_amount >= 0 else 0.0
        return {
            "component": component,
            "basis": provenance_basis,
            "amount": amount,
            "source": str(provenance.get("source") or "event.fee_provenance"),
            "reason": str(provenance.get("reason") or f"stored_{provenance_basis}_fee"),
        }

    extracted = extract_actual_fees(value)
    if extracted is not None and float(extracted.get("amount") or 0.0) > 0:
        return {
            "component": component,
            "basis": "actual",
            "amount": _round_money(extracted["amount"]),
            "source": str(extracted.get("source") or "broker_payload"),
            "reason": "broker_reported_fee",
            "components": list(extracted.get("components") or []),
        }

    broker = normalize_broker(value.get("broker"))
    if broker and broker != "富途":
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "reason": "unsupported_broker_fee_schedule",
        }
    currency = normalize_currency(value.get("currency"))
    shares = _stock_event_shares(value)
    price = _stock_event_price(value)
    source = FUTU_HK_FEE_SCHEDULE_URL if currency == "HKD" else FUTU_US_FEE_SCHEDULE_URL
    if transaction_kind == "assignment" and currency == "USD":
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "source": source,
            "reason": "us_assignment_fee_rule_not_explicit",
        }
    if currency not in {"USD", "HKD"} or shares <= 0 or price is None or price <= 0:
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "source": source if currency in {"USD", "HKD"} else None,
            "reason": "stock_fee_inputs_incomplete",
        }
    try:
        amount = calc_futu_stock_fee(currency, price, shares=shares, is_sell=transaction_kind == "sale")
    except Exception:
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "source": source,
            "reason": "stock_fee_estimate_failed",
        }
    return {
        "component": component,
        "basis": "estimated",
        "amount": _round_money(amount),
        "source": source,
        "reason": (
            "hk_assignment_stock_fee_excluding_assignment_exercise_fee"
            if transaction_kind == "assignment"
            else "standard_fixed_stock_fee_schedule_estimate"
        ),
    }


def _scale_fee_fact(fact: dict[str, Any], ratio: float) -> dict[str, Any]:
    return {**fact, "amount": _round_money(float(fact.get("amount") or 0.0) * max(0.0, ratio))}


def _summarize_fee_facts(facts: list[dict[str, Any]]) -> dict[str, Any]:
    actual = _round_money(sum(float(fact.get("amount") or 0.0) for fact in facts if fact.get("basis") == "actual"))
    estimated = _round_money(
        sum(float(fact.get("amount") or 0.0) for fact in facts if fact.get("basis") == "estimated")
    )
    missing = sorted({str(fact.get("component") or "unknown") for fact in facts if fact.get("basis") == "missing"})
    bases = {str(fact.get("basis") or "") for fact in facts if fact.get("basis") != "missing"}
    if missing and not bases:
        basis = "missing"
    elif missing or len(bases) > 1:
        basis = "mixed"
    elif bases:
        basis = next(iter(bases))
    else:
        basis = "missing"
    return {
        "actual_fees": actual,
        "estimated_fees": estimated,
        "fees_used": _round_money(actual + estimated),
        "fee_basis": basis,
        "fee_missing_components": missing,
        "fee_evidence": [
            {key: value for key, value in fact.items() if value not in (None, "", [])}
            for fact in facts
        ],
    }


def _explicit_stock_lot_id(event: dict[str, Any]) -> str | None:
    payload = _event_payload(event)
    for source in (event, payload):
        for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None


def _lot_shares_at(lot: dict[str, Any], at_ms: int) -> int:
    shares = int(lot.get("shares_opened") or 0)
    for sale in lot.get("_sale_rows") or []:
        if int(sale.get("event_at") or 0) <= at_ms:
            shares -= int(sale.get("shares") or 0)
    return max(0, shares)


def _active_reserved_shares(
    reservations: dict[str, list[tuple[int, int, int]]],
    stock_lot_id: str,
    at_ms: int,
) -> int:
    return sum(shares for start, end, shares in reservations.get(stock_lot_id, []) if start <= at_ms < end)


def _mixed_assigned_stock_keys(
    lots_by_id: dict[str, dict[str, Any]],
    stock_holdings: list[dict[str, Any]] | None,
) -> set[tuple[str, str, str]]:
    assigned: dict[tuple[str, str, str], int] = {}
    for lot in lots_by_id.values():
        key = (str(lot.get("account") or ""), str(lot.get("broker") or ""), str(lot.get("symbol") or ""))
        assigned[key] = assigned.get(key, 0) + int(lot.get("shares_remaining") or 0)
    mixed: set[tuple[str, str, str]] = set()
    for holding in stock_holdings or []:
        key = (
            normalize_account(holding.get("account")) or "",
            normalize_broker(holding.get("broker")) or "",
            norm_symbol(holding.get("symbol") or ""),
        )
        shares = int(abs(float(holding.get("shares") or holding.get("quantity") or 0)))
        if shares > assigned.get(key, 0):
            mixed.add(key)
    return mixed


def _attribute_covered_calls(
    lots_by_id: dict[str, dict[str, Any]],
    *,
    trade_events: list[dict[str, Any]],
    option_open_lots: list[dict[str, Any]],
    assignment_option_rows: list[dict[str, Any]],
    stock_holdings: list[dict[str, Any]] | None,
    as_of_ms: int,
    review_rows: list[dict[str, Any]],
) -> None:
    event_by_id = {
        str(event.get("event_id") or "").strip(): event
        for event in trade_events
        if str(event.get("event_id") or "").strip()
    }
    realized_by_open: dict[str, list[dict[str, Any]]] = {}
    for row in assignment_option_rows:
        open_id = str(row.get("open_event_id") or "").strip()
        if open_id:
            realized_by_open.setdefault(open_id, []).append(row)
    mixed_keys = _mixed_assigned_stock_keys(lots_by_id, stock_holdings)
    reservations: dict[str, list[tuple[int, int, int]]] = {}

    calls = sorted(
        (
            lot
            for lot in option_open_lots
            if str(lot.get("position_side") or "").lower() == "short"
            and str(lot.get("option_type") or "").lower() == "call"
        ),
        key=lambda lot: (int(lot.get("opened_at") or 0), str(lot.get("open_event_id") or "")),
    )
    for call in calls:
        open_id = str(call.get("open_event_id") or "").strip()
        open_event = event_by_id.get(open_id)
        if open_event is None:
            continue
        opened_at = int(call.get("opened_at") or 0)
        contracts = int(call.get("contracts") or 0)
        multiplier = int(call.get("multiplier") or 0)
        required_shares = contracts * multiplier
        if opened_at <= 0 or required_shares <= 0:
            continue
        realized_rows = realized_by_open.get(open_id, [])
        gross_pnl = _round_money(sum(float(row.get("realized_pnl_gross") or 0.0) for row in realized_rows))
        remaining = int(call.get("remaining") or 0)
        complete = remaining == 0
        closed_times = [int(row.get("closed_at") or 0) for row in realized_rows if int(row.get("closed_at") or 0) > 0]
        reservation_end = max(closed_times) if complete and closed_times else as_of_ms
        if reservation_end <= opened_at:
            reservation_end = max(as_of_ms, opened_at + 1)
        fee_facts = [_option_fee_fact(open_event, component="covered_call_open_option_fee")]
        for row in realized_rows:
            close_event = event_by_id.get(str(row.get("event_id") or ""))
            if close_event is None:
                fee_facts.append({"component": "covered_call_close_option_fee", "basis": "missing", "amount": 0.0})
                continue
            event_contracts = max(1, int(abs(float(close_event.get("contracts") or 0))))
            fee_facts.append(
                _scale_fee_fact(
                    _option_fee_fact(close_event, component="covered_call_close_option_fee"),
                    int(row.get("contracts_closed") or 0) / event_contracts,
                )
            )

        key = (str(call.get("account") or ""), str(call.get("broker") or ""), str(call.get("symbol") or ""))
        explicit_id = _explicit_stock_lot_id(open_event)
        candidates = [
            lot
            for lot in lots_by_id.values()
            if (str(lot.get("account") or ""), str(lot.get("broker") or ""), str(lot.get("symbol") or "")) == key
            and int(lot.get("assigned_at_ms") or 0) <= opened_at
        ]
        if not candidates and not explicit_id:
            continue
        allocation_status = "explicit" if explicit_id else "derived_fifo"
        if explicit_id:
            candidates = [lot for lot in candidates if str(lot.get("stock_lot_id") or "") == explicit_id]
        elif key in mixed_keys:
            candidates = []
            allocation_status = "unallocated"
        candidates.sort(key=lambda lot: (int(lot.get("assigned_at_ms") or 0), str(lot.get("stock_lot_id") or "")))

        available: list[tuple[dict[str, Any], int]] = []
        for lot in candidates:
            lot_id = str(lot.get("stock_lot_id") or "")
            shares = _lot_shares_at(lot, opened_at) - _active_reserved_shares(reservations, lot_id, opened_at)
            if shares > 0:
                available.append((lot, shares))
        if sum(shares for _lot, shares in available) < required_shares:
            review_rows.append(
                _assigned_stock_review_row(
                    status="covered_call_unallocated",
                    event_id=open_id,
                    account=key[0],
                    broker=key[1],
                    symbol=key[2],
                    message="covered call cannot be attributed to sufficient assigned-stock shares",
                    details={"required_shares": required_shares, "explicit_stock_lot_id": explicit_id},
                )
            )
            continue

        remaining_shares = required_shares
        for lot, shares in available:
            if remaining_shares <= 0:
                break
            allocated = min(shares, remaining_shares)
            ratio = allocated / required_shares
            lot["_covered_call_pnl"] = _round_money(float(lot.get("_covered_call_pnl") or 0.0) + gross_pnl * ratio)
            lot["_covered_call_fee_facts"].extend(_scale_fee_fact(fact, ratio) for fact in fee_facts)
            lot["_covered_call_statuses"].add(allocation_status)
            lot["_covered_call_complete"] = bool(lot.get("_covered_call_complete")) and complete
            lot_id = str(lot.get("stock_lot_id") or "")
            reservations.setdefault(lot_id, []).append((opened_at, reservation_end, allocated))
            remaining_shares -= allocated


def _lifecycle_efficiency_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("account") or ""),
            str(row.get("currency") or ""),
            str(row.get("lifecycle_quality") or "unclassified"),
        )
        bucket = buckets.setdefault(
            key,
            {"account": key[0], "currency": key[1], "lifecycle_quality": key[2], "lifecycle_count": 0, "lifecycle_pnl_net": 0.0, "capital_days": 0.0},
        )
        bucket["lifecycle_count"] += 1
        if row.get("lifecycle_pnl_net") is not None:
            bucket["lifecycle_pnl_net"] += float(row["lifecycle_pnl_net"])
        if row.get("capital_days") is not None:
            bucket["capital_days"] += float(row["capital_days"])
    out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        net = _round_money(bucket["lifecycle_pnl_net"])
        capital_days = round(float(bucket["capital_days"]), 6)
        out.append(
            {
                **bucket,
                "lifecycle_pnl_net": net,
                "capital_days": capital_days,
                "annualized_capital_efficiency": round(net * 365 / capital_days, 8) if capital_days > 0 else None,
            }
        )
    return sorted(out, key=lambda row: (row["account"], row["currency"], row["lifecycle_quality"]))


def _lot_basis_per_share_with_fees(lot: dict[str, Any]) -> float:
    shares_opened = int(lot.get("shares_opened") or 0)
    if shares_opened <= 0:
        return 0.0
    return float(lot.get("stock_cost_basis_total") or 0.0) / float(shares_opened)


def _assigned_stock_review_row(
    *,
    status: str,
    event_id: str | None = None,
    stock_lot_id: str | None = None,
    stock_event_id: str | None = None,
    month: str | None = None,
    account: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "event_id": event_id,
        "stock_lot_id": stock_lot_id,
        "stock_event_id": stock_event_id,
        "month": month,
        "account": account,
        "broker": broker,
        "symbol": symbol,
        "message": message,
        "details": dict(details or {}),
    }


def _assigned_stock_row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("month") or row.get("opened_month") or ""),
        str(row.get("account") or ""),
        str(row.get("symbol") or ""),
        int(row.get("opened_at_ms") or row.get("event_at") or 0),
        str(row.get("stock_lot_id") or row.get("event_id") or ""),
    )


def _lifecycle_row_in_month(row: dict[str, Any], month: str | None) -> bool:
    if not month:
        return True
    if row.get("opened_month") == month or row.get("month") == month:
        return True
    sale_months = row.get("sale_months")
    return isinstance(sale_months, list) and month in sale_months


def _sale_row_in_month(row: dict[str, Any], month: str | None) -> bool:
    return not month or row.get("month") == month


def _review_row_in_month(row: dict[str, Any], month: str | None) -> bool:
    return not month or row.get("month") in (None, month)


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
    event_by_id = {
        str(event.get("event_id") or "").strip(): event
        for event in _active_trade_events(trade_events)
        if str(event.get("event_id") or "").strip()
    }
    option_rows_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in assignment_option_rows:
        if str(row.get("close_type") or "").strip().lower() != "assignment":
            continue
        event_id = str(row.get("event_id") or row.get("record_id") or "").strip()
        if event_id:
            option_rows_by_event.setdefault(event_id, []).append(row)

    lots_by_id: dict[str, dict[str, Any]] = {}
    review_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for event in _active_trade_events(trade_events):
        if not _passes_report_filter(event, account_norm, broker_norm):
            continue
        if _event_close_type(event) != "assignment":
            continue
        event_id = str(event.get("event_id") or "").strip()
        event_month = _event_month(event)
        event_at = int(_event_ts(event) or 0)
        account = normalize_account(event.get("account")) or "-"
        broker = normalize_broker(event.get("broker")) or "-"
        symbol = norm_symbol(event.get("symbol") or "-")
        option_type = normalize_option_type(event.get("option_type")) or "-"
        position_side = _event_position_side(event) or str(event.get("position_side") or "").strip().lower()
        currency = normalize_currency(event.get("currency")) or "USD"
        if option_type != "put" or position_side != "short":
            review_rows.append(
                _assigned_stock_review_row(
                    status="manual_review_required",
                    event_id=event_id,
                    month=event_month,
                    account=account,
                    broker=broker,
                    symbol=symbol,
                    message="assignment stock lifecycle first version supports short put assignment only",
                    details={"option_type": option_type, "position_side": position_side},
                )
            )
            continue
        stock = _event_stock_settlement(event)
        stock_side = normalize_trade_side(stock.get("side") or stock.get("stock_side")) if stock else ""
        raw_shares = stock.get("shares") if stock.get("shares") not in (None, "") else stock.get("stock_qty")
        raw_price = stock.get("price") if stock.get("price") not in (None, "") else stock.get("stock_price")
        try:
            shares_opened = int(abs(float(raw_shares or 0)))
        except Exception:
            shares_opened = 0
        assignment_price = safe_float(raw_price)
        if not stock or stock_side != "buy" or shares_opened <= 0 or assignment_price is None:
            review_rows.append(
                _assigned_stock_review_row(
                    status="missing_stock_settlement",
                    event_id=event_id,
                    month=event_month,
                    account=account,
                    broker=broker,
                    symbol=symbol,
                    message="assignment event is missing confirmed buy-side stock settlement facts",
                    details={"stock_settlement": dict(stock or {})},
                )
            )
            continue
        option_rows = option_rows_by_event.get(event_id, [])
        option_premium_attribution = _option_premium_attribution(option_rows)
        if not option_rows:
            warnings.append(f"{event_id or '(no event_id)'}: assignment has no option premium attribution row")
        stock_lot_id = _assigned_stock_lot_id(event_id)
        source_option_lot_id = _source_option_lot_id(event, option_rows)
        assigned_contracts = sum(int(row.get("contracts_closed") or 0) for row in option_rows)
        fee_facts: list[dict[str, Any]] = []
        source_open_event = event_by_id.get(str(_source_option_open_event_id(event, option_rows) or ""))
        if source_open_event is not None:
            open_contracts = max(1, int(abs(float(source_open_event.get("contracts") or 0))))
            fee_facts.append(
                _scale_fee_fact(
                    _option_fee_fact(source_open_event, component="put_open_option_fee"),
                    assigned_contracts / open_contracts,
                )
            )
        else:
            fee_facts.append({"component": "put_open_option_fee", "basis": "missing", "amount": 0.0})
        close_contracts = max(1, int(abs(float(event.get("contracts") or 0))))
        fee_facts.append(
            _scale_fee_fact(
                _option_fee_fact(event, component="put_assignment_option_fee"),
                assigned_contracts / close_contracts,
            )
        )
        assignment_stock_fee = _stock_fee_fact(
            {
                **stock,
                "account": account,
                "broker": broker,
                "symbol": symbol,
                "currency": normalize_currency(stock.get("currency")) or currency,
            },
            component="assignment_stock_fee",
            transaction_kind="assignment",
        )
        fee_facts.append(assignment_stock_fee)
        assignment_fees = _round_money(assignment_stock_fee.get("amount"))
        assignment_notional = _round_money(float(assignment_price) * shares_opened)
        lots_by_id[stock_lot_id] = {
            "stock_lot_id": stock_lot_id,
            "source_assignment_event_id": event_id,
            "source_option_lot_id": source_option_lot_id,
            "account": account,
            "broker": broker,
            "symbol": symbol,
            "currency": normalize_currency(stock.get("currency")) or currency,
            "opened_at_ms": event_at,
            "assigned_at_ms": event_at,
            "assigned_date": _market_date(event_at, symbol=symbol, currency=normalize_currency(stock.get("currency")) or currency),
            "opened_month": event_month,
            "month": event_month,
            "shares_opened": shares_opened,
            "shares_remaining": shares_opened,
            "shares_sold": 0,
            "assignment_price": float(assignment_price),
            "assignment_notional": assignment_notional,
            "assignment_fees": assignment_fees,
            "stock_cost_per_share": float(assignment_price),
            "stock_cost_basis_total": _round_money(assignment_notional + assignment_fees),
            "basis_policy": "assignment_stock_cost_basis",
            "option_premium_attribution": option_premium_attribution,
            "stock_sale_cash_in_net": 0.0,
            "stock_sale_cash_in_gross": 0.0,
            "stock_sale_fees": 0.0,
            "stock_cost_basis_sold": 0.0,
            "assigned_stock_realized_pnl": 0.0,
            "sale_event_ids": [],
            "sale_months": [],
            "_sale_rows": [],
            "_fee_facts": fee_facts,
            "_assigned_contracts": assigned_contracts,
            "_option_open_event": source_open_event,
            "_covered_call_pnl": 0.0,
            "_covered_call_fee_facts": [],
            "_covered_call_statuses": set(),
            "_covered_call_complete": True,
        }

    seen_stock_events: set[str] = set()
    for idx, sale in enumerate(_normalize_assigned_stock_events(assigned_stock_events), start=1):
        if _stock_event_type(sale) != "sale":
            continue
        stock_event_id = _stock_event_id(sale, fallback_index=idx)
        if stock_event_id in seen_stock_events:
            review_rows.append(
                _assigned_stock_review_row(
                    status="manual_review_required",
                    stock_event_id=stock_event_id,
                    month=_stock_event_month(sale),
                    account=normalize_account(sale.get("account")),
                    broker=normalize_broker(sale.get("broker")),
                    symbol=norm_symbol(sale.get("symbol") or ""),
                    message="duplicate assigned stock sale event id",
                )
            )
            continue
        seen_stock_events.add(stock_event_id)
        target_stock_lot_id = str(sale.get("target_stock_lot_id") or "").strip()
        sale_account_filter = normalize_account(sale.get("account")) if sale.get("account") not in (None, "") else None
        sale_broker_filter = normalize_broker(sale.get("broker")) if sale.get("broker") not in (None, "") else None
        if account_norm and sale_account_filter != account_norm:
            continue
        if broker_norm and sale_broker_filter != broker_norm:
            continue
        lot = lots_by_id.get(target_stock_lot_id)
        sale_month = _stock_event_month(sale)
        sale_at = _stock_event_time_ms(sale)
        if lot is None:
            review_rows.append(
                _assigned_stock_review_row(
                    status="manual_review_required",
                    stock_event_id=stock_event_id,
                    stock_lot_id=target_stock_lot_id or None,
                    month=sale_month,
                    account=normalize_account(sale.get("account")),
                    broker=normalize_broker(sale.get("broker")),
                    symbol=norm_symbol(sale.get("symbol") or ""),
                    message="assigned stock sale must target an existing assigned stock lot",
                )
            )
            continue
        sale_side = normalize_trade_side(sale.get("side"))
        sale_account = normalize_account(sale.get("account")) or lot.get("account")
        sale_broker = normalize_broker(sale.get("broker")) or lot.get("broker")
        sale_symbol = norm_symbol(sale.get("symbol") or lot.get("symbol") or "")
        sale_currency = normalize_currency(sale.get("currency")) or lot.get("currency")
        shares = _stock_event_shares(sale)
        price = _stock_event_price(sale)
        sale_fee_fact = _stock_fee_fact(sale, component="stock_sale_fee", transaction_kind="sale")
        fees = _round_money(sale_fee_fact.get("amount"))
        mismatch_fields = []
        if sale_side != "sell":
            mismatch_fields.append("side")
        if sale_account != lot.get("account"):
            mismatch_fields.append("account")
        if sale_broker != lot.get("broker"):
            mismatch_fields.append("broker")
        if sale_symbol != lot.get("symbol"):
            mismatch_fields.append("symbol")
        if sale_currency != lot.get("currency"):
            mismatch_fields.append("currency")
        if sale_at is None or int(sale_at) < int(lot.get("opened_at_ms") or 0):
            mismatch_fields.append("trade_time_ms")
        if shares <= 0:
            mismatch_fields.append("shares")
        if price is None or price < 0:
            mismatch_fields.append("price")
        if fees < 0:
            mismatch_fields.append("fees")
        if shares > int(lot.get("shares_remaining") or 0):
            mismatch_fields.append("shares_remaining")
        if mismatch_fields:
            review_rows.append(
                _assigned_stock_review_row(
                    status="source_conflict",
                    stock_event_id=stock_event_id,
                    stock_lot_id=target_stock_lot_id,
                    month=sale_month,
                    account=sale_account,
                    broker=sale_broker,
                    symbol=sale_symbol,
                    message="assigned stock sale event failed validation",
                    details={"fields": mismatch_fields, "shares_remaining": lot.get("shares_remaining")},
                )
            )
            continue
        proceeds_gross = _round_money(float(price) * shares)
        proceeds_net = _round_money(proceeds_gross - fees)
        cost_basis_sold = _round_money(_lot_basis_per_share_with_fees(lot) * shares)
        realized_pnl = _round_money(proceeds_net - cost_basis_sold)
        lot["shares_remaining"] = int(lot.get("shares_remaining") or 0) - shares
        lot["shares_sold"] = int(lot.get("shares_sold") or 0) + shares
        lot["stock_sale_cash_in_net"] = _round_money(float(lot.get("stock_sale_cash_in_net") or 0.0) + proceeds_net)
        lot["stock_sale_cash_in_gross"] = _round_money(
            float(lot.get("stock_sale_cash_in_gross") or 0.0) + proceeds_gross
        )
        lot["stock_sale_fees"] = _round_money(float(lot.get("stock_sale_fees") or 0.0) + fees)
        lot["stock_cost_basis_sold"] = _round_money(float(lot.get("stock_cost_basis_sold") or 0.0) + cost_basis_sold)
        lot["assigned_stock_realized_pnl"] = _round_money(
            float(lot.get("assigned_stock_realized_pnl") or 0.0) + realized_pnl
        )
        lot["sale_event_ids"].append(stock_event_id)
        if sale_month and sale_month not in lot["sale_months"]:
            lot["sale_months"].append(sale_month)
        lot["_fee_facts"].append(sale_fee_fact)
        sale_row = {
            "stock_event_id": stock_event_id,
            "stock_lot_id": target_stock_lot_id,
            "source_assignment_event_id": lot.get("source_assignment_event_id"),
            "account": sale_account,
            "broker": sale_broker,
            "symbol": sale_symbol,
            "currency": sale_currency,
            "month": sale_month,
            "event_at": int(sale_at or 0),
            "shares": shares,
            "price": float(price),
            "fees": fees,
            "fee_basis": sale_fee_fact.get("basis"),
            "fee_source": sale_fee_fact.get("source"),
            "fee_reason": sale_fee_fact.get("reason"),
            "cash_in_gross": proceeds_gross,
            "stock_sale_cash_in_net": proceeds_net,
            "stock_cost_basis_sold": cost_basis_sold,
            "assigned_stock_realized_pnl": realized_pnl,
            "source": str(sale.get("source") or "").strip() or None,
            "source_deal_id": str(sale.get("source_deal_id") or "").strip() or None,
        }
        lot["_sale_rows"].append(sale_row)

    effective_as_of_ms = int(as_of_ms) if as_of_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    _attribute_covered_calls(
        lots_by_id,
        trade_events=_active_trade_events(trade_events),
        option_open_lots=option_open_lots,
        assignment_option_rows=assignment_option_rows,
        stock_holdings=stock_holdings,
        as_of_ms=effective_as_of_ms,
        review_rows=review_rows,
    )

    quote_rows = _normalize_quote_snapshots(quote_snapshots)
    lifecycle_rows: list[dict[str, Any]] = []
    sale_rows: list[dict[str, Any]] = []
    lot_rows: list[dict[str, Any]] = []
    for lot in lots_by_id.values():
        shares_remaining = int(lot.get("shares_remaining") or 0)
        status = "closed" if shares_remaining == 0 else ("partially_sold" if int(lot.get("shares_sold") or 0) > 0 else "open")
        remaining_stock_cost_basis = _round_money(_lot_basis_per_share_with_fees(lot) * shares_remaining)
        quote = _matching_quote(quote_rows, lot, as_of_ms=as_of_ms) if shares_remaining > 0 else None
        spot = _quote_spot(quote or {}) if quote is not None else None
        quote_time = _quote_time_ms(quote or {}) if quote is not None else None
        quote_source = str((quote or {}).get("quote_source") or (quote or {}).get("source") or "").strip() or None
        quote_status = (
            str((quote or {}).get("quote_status") or "").strip().lower()
            if quote is not None
            else ("not_required" if shares_remaining == 0 else "missing_quote")
        )
        if quote is not None and quote_status not in {"fresh", "stale", "missing_quote"}:
            quote_status = "fresh" if spot is not None else "missing_quote"
        remaining_market_value = _round_money(spot * shares_remaining) if spot is not None and shares_remaining > 0 else None
        assigned_stock_unrealized_pnl = (
            _round_money(float(remaining_market_value) - remaining_stock_cost_basis)
            if remaining_market_value is not None
            else None
        )
        lifecycle_pnl = None
        if shares_remaining == 0 or remaining_market_value is not None:
            lifecycle_pnl = _round_money(
                float(lot.get("option_premium_attribution") or 0.0)
                - float(lot.get("stock_cost_basis_total") or 0.0)
                + float(lot.get("stock_sale_cash_in_net") or 0.0)
                + float(remaining_market_value or 0.0)
            )
        sale_rows_sorted = sorted(lot.get("_sale_rows") or [], key=lambda item: int(item.get("event_at") or 0))
        assigned_at_ms = int(lot.get("assigned_at_ms") or 0) or None
        inventory_end_at_ms = (
            max((int(item.get("event_at") or 0) for item in sale_rows_sorted), default=0) or assigned_at_ms
            if shares_remaining == 0
            else effective_as_of_ms
        )
        inventory_days = _elapsed_days(assigned_at_ms, inventory_end_at_ms)

        open_event = lot.get("_option_open_event") if isinstance(lot.get("_option_open_event"), dict) else None
        put_days = _elapsed_days(_event_ts(open_event or {}), assigned_at_ms)
        assigned_contracts = int(lot.get("_assigned_contracts") or 0)
        put_strike = safe_float((open_event or {}).get("strike"))
        put_multiplier = safe_float((open_event or {}).get("multiplier"))
        put_capital_days = (
            round(put_strike * put_multiplier * assigned_contracts * put_days, 6)
            if put_days is not None and put_strike is not None and put_multiplier is not None and assigned_contracts > 0
            else None
        )

        stock_capital_days = 0.0
        stock_capital_known = assigned_at_ms is not None and inventory_end_at_ms is not None
        capital_cursor = assigned_at_ms
        remaining_basis_for_days = float(lot.get("stock_cost_basis_total") or 0.0)
        if stock_capital_known:
            for sale_row in sale_rows_sorted:
                sale_at = int(sale_row.get("event_at") or 0)
                interval = _elapsed_days(capital_cursor, sale_at)
                if interval is None:
                    stock_capital_known = False
                    break
                stock_capital_days += remaining_basis_for_days * interval
                remaining_basis_for_days = max(
                    0.0,
                    remaining_basis_for_days - float(sale_row.get("stock_cost_basis_sold") or 0.0),
                )
                capital_cursor = sale_at
            if stock_capital_known:
                final_interval = _elapsed_days(capital_cursor, inventory_end_at_ms)
                if final_interval is None:
                    stock_capital_known = False
                else:
                    stock_capital_days += remaining_basis_for_days * final_interval
        stock_capital_days_value = round(stock_capital_days, 6) if stock_capital_known else None
        capital_days = (
            round(float(put_capital_days) + float(stock_capital_days_value), 6)
            if put_capital_days is not None and stock_capital_days_value is not None
            else None
        )

        stock_pnl_gross = None
        if shares_remaining == 0 or remaining_market_value is not None:
            stock_pnl_gross = _round_money(
                float(lot.get("stock_sale_cash_in_gross") or 0.0)
                + float(remaining_market_value or 0.0)
                - float(lot.get("assignment_notional") or 0.0)
            )
        covered_call_pnl = _round_money(lot.get("_covered_call_pnl"))
        fee_facts = list(lot.get("_fee_facts") or []) + list(lot.get("_covered_call_fee_facts") or [])
        fee_summary = _summarize_fee_facts(fee_facts)
        lifecycle_pnl_gross = (
            _round_money(float(lot.get("option_premium_attribution") or 0.0) + stock_pnl_gross + covered_call_pnl)
            if stock_pnl_gross is not None
            else None
        )
        lifecycle_pnl_net = (
            _round_money(lifecycle_pnl_gross - float(fee_summary["fees_used"]))
            if lifecycle_pnl_gross is not None
            else None
        )
        annualized_capital_efficiency = (
            round(lifecycle_pnl_net * 365 / capital_days, 8)
            if lifecycle_pnl_net is not None and capital_days is not None and capital_days > 0
            else None
        )
        covered_call_statuses = set(lot.get("_covered_call_statuses") or set())
        covered_call_allocation_status = (
            "none"
            if not covered_call_statuses
            else next(iter(covered_call_statuses))
            if len(covered_call_statuses) == 1
            else "mixed"
        )
        if status == "closed":
            lifecycle_quality = (
                "complete_closed"
                if not fee_summary["fee_missing_components"]
                and bool(lot.get("_covered_call_complete"))
                and capital_days is not None
                else "closed_incomplete"
            )
        elif remaining_market_value is not None:
            lifecycle_quality = "open_marked"
        else:
            lifecycle_quality = None
        review_status = "ready"
        if shares_remaining > 0 and remaining_market_value is None:
            review_status = "missing_quote"
            review_rows.append(
                _assigned_stock_review_row(
                    status="missing_quote",
                    event_id=str(lot.get("source_assignment_event_id") or ""),
                    stock_lot_id=str(lot.get("stock_lot_id") or ""),
                    month=str(lot.get("opened_month") or ""),
                    account=str(lot.get("account") or ""),
                    broker=str(lot.get("broker") or ""),
                    symbol=str(lot.get("symbol") or ""),
                    message="open assigned stock lot has no usable as-of quote",
                )
            )
        row = {
            **{key: value for key, value in lot.items() if not str(key).startswith("_")},
            "status": status,
            "review_status": review_status,
            "remaining_stock_cost_basis": remaining_stock_cost_basis,
            "spot": spot,
            "spot_time": quote_time,
            "quote_source": quote_source,
            "quote_status": quote_status,
            "remaining_market_value": remaining_market_value,
            "assigned_stock_unrealized_pnl": assigned_stock_unrealized_pnl,
            "assignment_lifecycle_pnl": lifecycle_pnl,
            "inventory_end_at_ms": inventory_end_at_ms,
            "inventory_days": inventory_days,
            **fee_summary,
            "covered_call_pnl": covered_call_pnl,
            "covered_call_allocation_status": covered_call_allocation_status,
            "put_capital_days": put_capital_days,
            "stock_capital_days": stock_capital_days_value,
            "capital_days": capital_days,
            "lifecycle_pnl_gross": lifecycle_pnl_gross,
            "lifecycle_pnl_net": lifecycle_pnl_net,
            "annualized_capital_efficiency": annualized_capital_efficiency,
            "lifecycle_quality": lifecycle_quality,
        }
        lot_rows.append(row)
        lifecycle_rows.append(row)
        sale_rows.extend(lot.get("_sale_rows") or [])

    _append_holding_reconciliation_reviews(
        review_rows,
        lot_rows,
        stock_holdings=stock_holdings,
        month=month,
    )

    filtered_lifecycle_rows = sorted(
        [row for row in lifecycle_rows if _lifecycle_row_in_month(row, month)],
        key=_assigned_stock_row_sort_key,
    )
    return {
        "assigned_stock_lots": sorted(
            [row for row in lot_rows if _lifecycle_row_in_month(row, month)],
            key=_assigned_stock_row_sort_key,
        ),
        "assignment_lifecycle_rows": filtered_lifecycle_rows,
        "lifecycle_efficiency_rows": filtered_lifecycle_rows,
        "lifecycle_efficiency_summary": _lifecycle_efficiency_summary(filtered_lifecycle_rows),
        "assigned_stock_sale_rows": sorted(
            [row for row in sale_rows if _sale_row_in_month(row, month)],
            key=_event_detail_sort_key,
        ),
        "assigned_stock_review_rows": sorted(
            [row for row in review_rows if _review_row_in_month(row, month)],
            key=_assigned_stock_row_sort_key,
        ),
        "warnings": warnings,
    }


def _append_holding_reconciliation_reviews(
    review_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
    *,
    stock_holdings: list[dict[str, Any]] | None,
    month: str | None,
) -> None:
    if not isinstance(stock_holdings, list):
        return
    expected_by_key: dict[tuple[str, str, str, str], float] = {}
    for lot in lot_rows:
        key = (
            normalize_account(lot.get("account")) or "-",
            normalize_broker(lot.get("broker")) or "-",
            norm_symbol(lot.get("symbol") or ""),
            normalize_currency(lot.get("currency")) or "",
        )
        expected_by_key[key] = expected_by_key.get(key, 0.0) + float(lot.get("shares_remaining") or 0.0)

    actual_by_key: dict[tuple[str, str, str, str], float] = {}
    for holding in stock_holdings:
        if not isinstance(holding, dict):
            continue
        shares = safe_float(holding.get("shares") if holding.get("shares") not in (None, "") else holding.get("quantity"))
        if shares is None:
            continue
        key = (
            normalize_account(holding.get("account")) or "-",
            normalize_broker(holding.get("broker")) or "-",
            norm_symbol(holding.get("symbol") or holding.get("underlying_symbol") or ""),
            normalize_currency(holding.get("currency")) or "",
        )
        actual_by_key[key] = actual_by_key.get(key, 0.0) + float(shares)

    for key, expected in sorted(expected_by_key.items()):
        actual = actual_by_key.get(key, 0.0)
        if abs(actual - expected) < 1e-9:
            continue
        account, broker, symbol, currency = key
        status = "missing_stock_sale" if actual < expected else "source_conflict"
        review_rows.append(
            _assigned_stock_review_row(
                status=status,
                month=month,
                account=account,
                broker=broker,
                symbol=symbol,
                message="assigned stock lots and external holdings disagree; holdings are reconciliation evidence only",
                details={"currency": currency, "assigned_stock_shares_remaining": expected, "holding_shares": actual},
            )
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
    report = _build_monthly_income_report_from_events(
        trade_events if isinstance(trade_events, list) else [],
        records=records,
        account_norm=account_norm,
        broker_norm=broker_norm,
        month=month,
        converter=converter,
        assigned_stock_events=assigned_stock_events,
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
