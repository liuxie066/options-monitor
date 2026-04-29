from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.feishu_bitable import parse_note_kv, safe_float
from scripts.exchange_rates import CurrencyConverter, ExchangeRates
from scripts.multiplier_cache import resolve_multiplier
from scripts.option_positions_core.domain import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    effective_contracts_closed,
    effective_contracts,
    effective_multiplier,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_side,
    normalize_status,
    norm_symbol,
)


@dataclass(frozen=True)
class IncomeRow:
    record_id: str
    month: str
    account: str
    broker: str
    symbol: str
    currency: str
    contracts_closed: int
    premium: float
    close_price: float
    multiplier: int
    realized_gross: float
    close_type: str
    closed_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "month": self.month,
            "account": self.account,
            "broker": self.broker,
            "symbol": self.symbol,
            "currency": self.currency,
            "contracts_closed": self.contracts_closed,
            "premium": self.premium,
            "close_price": self.close_price,
            "multiplier": self.multiplier,
            "realized_gross": self.realized_gross,
            "close_type": self.close_type,
            "closed_at": self.closed_at,
        }


@dataclass(frozen=True)
class PremiumIncomeRow:
    record_id: str
    month: str
    account: str
    broker: str
    symbol: str
    currency: str
    contracts: int
    premium: float
    multiplier: int
    premium_received_gross: float
    opened_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "month": self.month,
            "account": self.account,
            "broker": self.broker,
            "symbol": self.symbol,
            "currency": self.currency,
            "contracts": self.contracts,
            "premium": self.premium,
            "multiplier": self.multiplier,
            "premium_received_gross": self.premium_received_gross,
            "opened_at": self.opened_at,
        }


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


def parse_closed_at_ms(value: Any) -> int | None:
    return parse_event_at_ms(value)


def month_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m")


def _read_premium(fields: dict[str, Any]) -> float | None:
    premium = safe_float(fields.get("premium"))
    if premium is not None:
        return premium
    return safe_float(parse_note_kv(fields.get("note") or "", "premium_per_share"))


def _read_multiplier(fields: dict[str, Any]) -> int | None:
    multiplier = effective_multiplier(fields)
    if multiplier is not None and int(multiplier) > 0:
        return int(multiplier)
    resolved = resolve_multiplier(
        repo_base=Path(__file__).resolve().parents[2],
        symbol=norm_symbol(fields.get("symbol") or ""),
        allow_opend_refresh=False,
    )
    return int(resolved) if resolved else None


def _build_exchange_rate_converter(rates: dict[str, Any] | None) -> CurrencyConverter:
    rates_map = rates.get("rates") if isinstance(rates, dict) and isinstance(rates.get("rates"), dict) else rates
    usdcny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    if isinstance(rates_map, dict):
        try:
            usdcny_exchange_rate = float(rates_map.get("USDCNY")) if rates_map.get("USDCNY") else None
        except Exception:
            usdcny_exchange_rate = None
        try:
            cny_per_hkd_exchange_rate = float(rates_map.get("HKDCNY")) if rates_map.get("HKDCNY") else None
        except Exception:
            cny_per_hkd_exchange_rate = None
    usd_per_cny_exchange_rate = (1.0 / usdcny_exchange_rate) if usdcny_exchange_rate and usdcny_exchange_rate > 0 else None
    return CurrencyConverter(
        ExchangeRates(
            usd_per_cny=usd_per_cny_exchange_rate,
            cny_per_hkd=cny_per_hkd_exchange_rate,
        )
    )


def _maybe_to_cny(converter: CurrencyConverter, amount: float, currency: str) -> float | None:
    out = converter.native_to_cny(float(amount), native_ccy=str(currency or "").upper())
    return round(float(out), 6) if out is not None else None


def build_income_row(record: dict[str, Any]) -> tuple[IncomeRow | None, str | None]:
    record_id = str(record.get("record_id") or record.get("id") or "").strip()
    fields = record.get("fields") or record
    if not isinstance(fields, dict):
        return None, f"{record_id or '(no record_id)'}: fields is not an object"

    status = normalize_status(fields.get("status"))
    if status != "close":
        return None, None

    closed_at = parse_closed_at_ms(fields.get("closed_at"))
    if closed_at is None:
        return None, f"{record_id or '(no record_id)'}: missing closed_at"

    contracts_closed = effective_contracts_closed(fields)
    if contracts_closed <= 0:
        return None, f"{record_id or '(no record_id)'}: contracts_closed <= 0"

    premium = _read_premium(fields)
    if premium is None:
        return None, f"{record_id or '(no record_id)'}: missing premium"

    multiplier = _read_multiplier(fields)
    if multiplier is None:
        return None, f"{record_id or '(no record_id)'}: missing multiplier"

    close_type = normalize_close_type(fields.get("close_type")) if fields.get("close_type") else ""
    close_price = safe_float(fields.get("close_price"))
    if close_price is None:
        if close_type == EXPIRE_AUTO_CLOSE:
            close_price = 0.0
        else:
            return None, f"{record_id or '(no record_id)'}: missing close_price"

    currency = normalize_currency(fields.get("currency")) or "USD"
    account = normalize_account(fields.get("account")) or "-"
    broker = normalize_broker(fields.get("broker")) or "-"
    symbol = norm_symbol(fields.get("symbol") or "-")
    realized_gross = (float(premium) - float(close_price)) * int(multiplier) * int(contracts_closed)

    return (
        IncomeRow(
            record_id=record_id,
            month=month_from_ms(closed_at),
            account=account,
            broker=broker,
            symbol=symbol,
            currency=currency,
            contracts_closed=int(contracts_closed),
            premium=float(premium),
            close_price=float(close_price),
            multiplier=int(multiplier),
            realized_gross=round(float(realized_gross), 6),
            close_type=close_type or BUY_TO_CLOSE,
            closed_at=int(closed_at),
        ),
        None,
    )


def build_premium_income_row(record: dict[str, Any]) -> tuple[PremiumIncomeRow | None, str | None]:
    record_id = str(record.get("record_id") or record.get("id") or "").strip()
    fields = record.get("fields") or record
    if not isinstance(fields, dict):
        return None, f"{record_id or '(no record_id)'}: fields is not an object"

    side = normalize_side(fields.get("side"))
    if side != "short":
        return None, None

    opened_at = parse_event_at_ms(fields.get("opened_at"))
    if opened_at is None:
        return None, f"{record_id or '(no record_id)'}: missing opened_at"

    contracts = effective_contracts(fields)
    if contracts <= 0:
        return None, f"{record_id or '(no record_id)'}: contracts <= 0"

    premium = _read_premium(fields)
    if premium is None:
        return None, f"{record_id or '(no record_id)'}: missing premium"

    multiplier = _read_multiplier(fields)
    if multiplier is None:
        return None, f"{record_id or '(no record_id)'}: missing multiplier"

    currency = normalize_currency(fields.get("currency")) or "USD"
    account = normalize_account(fields.get("account")) or "-"
    broker = normalize_broker(fields.get("broker")) or "-"
    symbol = norm_symbol(fields.get("symbol") or "-")
    premium_received_gross = float(premium) * int(multiplier) * int(contracts)

    return (
        PremiumIncomeRow(
            record_id=record_id,
            month=month_from_ms(opened_at),
            account=account,
            broker=broker,
            symbol=symbol,
            currency=currency,
            contracts=int(contracts),
            premium=float(premium),
            multiplier=int(multiplier),
            premium_received_gross=round(float(premium_received_gross), 6),
            opened_at=int(opened_at),
        ),
        None,
    )


def build_monthly_income_report(
    records: list[dict[str, Any]],
    *,
    account: str | None = None,
    broker: str | None = None,
    month: str | None = None,
    rates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account_norm = normalize_account(account) if account else None
    broker_norm = normalize_broker(broker) if broker else None
    converter = _build_exchange_rate_converter(rates)
    rows: list[IncomeRow] = []
    premium_rows: list[PremiumIncomeRow] = []
    warnings: list[str] = []

    for rec in records:
        fields = rec.get("fields") or rec
        if not isinstance(fields, dict):
            continue
        if account_norm and normalize_account(fields.get("account")) != account_norm:
            continue
        if broker_norm and normalize_broker(fields.get("broker")) != broker_norm:
            continue

        row, warning = build_income_row(rec)
        if warning:
            warnings.append(warning)
        if row is not None and (not month or row.month == month):
            rows.append(row)

        premium_row, premium_warning = build_premium_income_row(rec)
        if premium_warning:
            warnings.append(premium_warning)
        if premium_row is not None and (not month or premium_row.month == month):
            premium_rows.append(premium_row)

    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row.month}|{row.account}|{row.currency}"
        bucket = summary.setdefault(
            key,
            {
                "month": row.month,
                "account": row.account,
                "currency": row.currency,
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
            },
        )
        bucket["realized_gross"] = round(float(bucket["realized_gross"]) + row.realized_gross, 6)
        realized_gross_cny = _maybe_to_cny(converter, row.realized_gross, row.currency)
        if realized_gross_cny is None:
            bucket["realized_gross_cny_missing"] = True
        elif not bucket["realized_gross_cny_missing"]:
            bucket["realized_gross_cny"] = round(float(bucket["realized_gross_cny"]) + realized_gross_cny, 6)
        bucket["closed_contracts"] = int(bucket["closed_contracts"]) + row.contracts_closed
        bucket["positions"] = int(bucket["positions"]) + 1

    for row in premium_rows:
        key = f"{row.month}|{row.account}|{row.currency}"
        bucket = summary.setdefault(
            key,
            {
                "month": row.month,
                "account": row.account,
                "currency": row.currency,
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
            },
        )
        bucket["premium_received_gross"] = round(
            float(bucket["premium_received_gross"]) + row.premium_received_gross,
            6,
        )
        premium_received_gross_cny = _maybe_to_cny(converter, row.premium_received_gross, row.currency)
        if premium_received_gross_cny is None:
            bucket["premium_received_gross_cny_missing"] = True
        elif not bucket["premium_received_gross_cny_missing"]:
            bucket["premium_received_gross_cny"] = round(
                float(bucket["premium_received_gross_cny"]) + premium_received_gross_cny,
                6,
            )
        bucket["premium_contracts"] = int(bucket["premium_contracts"]) + row.contracts
        bucket["premium_positions"] = int(bucket["premium_positions"]) + 1

    summary_rows = sorted(summary.values(), key=lambda x: (str(x["month"]), str(x["account"]), str(x["currency"])))
    for row in summary_rows:
        row["realized_gross_cny"] = None if row.pop("realized_gross_cny_missing") else round(float(row["realized_gross_cny"]), 6)
        row["premium_received_gross_cny"] = None if row.pop("premium_received_gross_cny_missing") else round(
            float(row["premium_received_gross_cny"]),
            6,
        )
    return {
        "summary": summary_rows,
        "rows": [r.as_dict() for r in sorted(rows, key=lambda x: (x.month, x.account, x.currency, x.symbol, x.record_id))],
        "premium_rows": [
            r.as_dict() for r in sorted(premium_rows, key=lambda x: (x.month, x.account, x.currency, x.symbol, x.record_id))
        ],
        "warnings": warnings,
        "filters": {
            "account": account_norm,
            "broker": broker_norm,
            "month": month,
        },
    }
