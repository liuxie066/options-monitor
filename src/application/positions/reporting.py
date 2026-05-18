from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.infrastructure.feishu_bitable import parse_note_kv, safe_float
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from src.infrastructure.multiplier_cache import resolve_multiplier
from domain.domain.ledger.position_fields import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    effective_contracts_closed,
    effective_contracts,
    effective_multiplier,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
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
    position_side: str
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
            "position_side": self.position_side,
            "contracts_closed": self.contracts_closed,
            "premium": self.premium,
            "close_price": self.close_price,
            "multiplier": self.multiplier,
            "realized_gross": self.realized_gross,
            "close_type": self.close_type,
            "closed_at": self.closed_at,
        }


@dataclass(frozen=True)
class OpenCashflowRow:
    record_id: str
    month: str
    account: str
    broker: str
    symbol: str
    option_type: str
    currency: str
    position_side: str
    trade_action: str
    contracts: int
    price: float
    multiplier: int
    cash_in_gross: float
    cash_out_gross: float
    net_cashflow_gross: float
    opened_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "event_id": self.record_id,
            "event_at": self.opened_at,
            "month": self.month,
            "account": self.account,
            "broker": self.broker,
            "symbol": self.symbol,
            "option_type": self.option_type,
            "position_side": self.position_side,
            "trade_action": self.trade_action,
            "currency": self.currency,
            "contracts": self.contracts,
            "price": self.price,
            "multiplier": self.multiplier,
            "cash_in_gross": self.cash_in_gross,
            "cash_out_gross": self.cash_out_gross,
            "net_cashflow_gross": self.net_cashflow_gross,
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
        repo_base=Path(__file__).resolve().parents[3],
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
        str(payload.get("mode") or "").strip().lower(),
        str(payload.get("close_type") or "").strip().lower(),
        str(payload.get("close_reason") or "").strip().lower(),
        str(event.get("source_name") or "").strip().lower(),
    }
    return EXPIRE_AUTO_CLOSE in tokens or "expired" in tokens or "auto_close_expired_positions" in tokens


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
        if str(event.get("position_effect") or "").strip().lower() != "void":
            continue
        target = str(_event_payload(event).get("void_target_event_id") or "").strip()
        if target:
            out.add(target)
    return out


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


def _event_detail_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("month") or ""),
        str(row.get("account") or ""),
        str(row.get("currency") or ""),
        int(row.get("event_at") or 0),
        str(row.get("event_id") or row.get("record_id") or ""),
    )


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
    side = normalize_side(fields.get("side"))
    position_side = "long" if side == "long" else "short"
    if position_side == "long":
        realized_gross = (float(close_price) - float(premium)) * int(multiplier) * int(contracts_closed)
    else:
        realized_gross = (float(premium) - float(close_price)) * int(multiplier) * int(contracts_closed)

    return (
        IncomeRow(
            record_id=record_id,
            month=month_from_ms(closed_at),
            account=account,
            broker=broker,
            symbol=symbol,
            currency=currency,
            position_side=position_side,
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


def build_open_cashflow_row(record: dict[str, Any]) -> tuple[OpenCashflowRow | None, str | None]:
    record_id = str(record.get("record_id") or record.get("id") or "").strip()
    fields = record.get("fields") or record
    if not isinstance(fields, dict):
        return None, f"{record_id or '(no record_id)'}: fields is not an object"

    side = normalize_side(fields.get("side"))
    if side not in {"short", "long"}:
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

    amount = _amount(premium, multiplier, contracts)
    currency = normalize_currency(fields.get("currency")) or "USD"
    account = normalize_account(fields.get("account")) or "-"
    broker = normalize_broker(fields.get("broker")) or "-"
    symbol = norm_symbol(fields.get("symbol") or "-")
    option_type = normalize_option_type(fields.get("option_type")) or "-"
    cash_in = amount if side == "short" else 0.0
    cash_out = amount if side == "long" else 0.0

    return (
        OpenCashflowRow(
            record_id=record_id,
            month=month_from_ms(opened_at),
            account=account,
            broker=broker,
            symbol=symbol,
            option_type=option_type,
            currency=currency,
            position_side=side,
            trade_action="sell_open" if side == "short" else "buy_open",
            contracts=int(contracts),
            price=float(premium),
            multiplier=int(multiplier),
            cash_in_gross=cash_in,
            cash_out_gross=cash_out,
            net_cashflow_gross=_round_money(cash_in - cash_out),
            opened_at=int(opened_at),
        ),
        None,
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
        elif leg_role == "enhancement_call" or str(lot.get("strategy") or "") == "yield_enhancement":
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


def _legacy_close_cashflow_row(row: IncomeRow) -> dict[str, Any]:
    close_amount = _amount(row.close_price, row.multiplier, row.contracts_closed)
    cash_in = close_amount if row.position_side == "long" else 0.0
    cash_out = close_amount if row.position_side == "short" else 0.0
    return {
        "record_id": row.record_id,
        "event_id": row.record_id,
        "event_at": row.closed_at,
        "month": row.month,
        "account": row.account,
        "broker": row.broker,
        "symbol": row.symbol,
        "option_type": "-",
        "position_side": row.position_side,
        "trade_action": "buy_close" if row.position_side == "short" else "sell_close",
        "currency": row.currency,
        "contracts": row.contracts_closed,
        "price": row.close_price,
        "multiplier": row.multiplier,
        "cash_in_gross": cash_in,
        "cash_out_gross": cash_out,
        "net_cashflow_gross": _round_money(cash_in - cash_out),
    }


def _build_legacy_open_basis_rows(
    open_cashflow_rows: list[OpenCashflowRow],
    close_rows: list[IncomeRow],
) -> list[dict[str, Any]]:
    close_by_record_id = {row.record_id: row for row in close_rows}
    out: list[dict[str, Any]] = []
    for open_row in open_cashflow_rows:
        close_row = close_by_record_id.get(open_row.record_id)
        close_amount = (
            _amount(close_row.close_price, close_row.multiplier, close_row.contracts_closed)
            if close_row is not None
            else 0.0
        )
        is_short = open_row.position_side == "short"
        sell_open_premium = open_row.cash_in_gross if is_short else 0.0
        sell_close_cost_actual = close_amount if is_short else 0.0
        enhancement_call_buy_cost = open_row.cash_out_gross if not is_short else 0.0
        enhancement_call_sell_proceeds_actual = close_amount if not is_short else 0.0
        out.append(
            {
                "month": open_row.month,
                "account": open_row.account,
                "broker": open_row.broker,
                "symbol": open_row.symbol,
                "currency": open_row.currency,
                "strategy": "",
                "strategy_group_id": "",
                "sell_open_premium": sell_open_premium,
                "sell_close_cost_actual": sell_close_cost_actual,
                "enhancement_call_buy_cost": enhancement_call_buy_cost,
                "enhancement_call_sell_proceeds_actual": enhancement_call_sell_proceeds_actual,
                "open_basis_lifecycle_pnl_gross": _round_money(
                    sell_open_premium
                    - sell_close_cost_actual
                    - enhancement_call_buy_cost
                    + enhancement_call_sell_proceeds_actual
                ),
                "open_contracts": open_row.contracts,
                "remaining_contracts": max(
                    0,
                    open_row.contracts - (close_row.contracts_closed if close_row is not None else 0),
                ),
                "is_final": bool(close_row is not None and close_row.contracts_closed >= open_row.contracts),
                "open_event_ids": [open_row.record_id],
            }
        )
    return sorted(
        out,
        key=lambda x: (
            str(x.get("month")),
            str(x.get("account")),
            str(x.get("symbol")),
            str(x.get("open_event_ids")),
        ),
    )


def _build_monthly_income_report_from_events(
    trade_events: list[dict[str, Any]],
    *,
    account_norm: str | None,
    broker_norm: str | None,
    month: str | None,
    converter: CurrencyConverter,
) -> dict[str, Any]:
    events = _active_trade_events(trade_events)
    open_lots: list[dict[str, Any]] = []
    cashflow_rows: list[dict[str, Any]] = []
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
            cash_in = amount if position_side == "short" else 0.0
            cash_out = amount if position_side == "long" else 0.0
            cashflow_row = {
                "event_id": event_id,
                "event_at": int(_event_ts(event) or 0),
                "month": event_month,
                "account": account,
                "broker": broker,
                "symbol": symbol,
                "option_type": option_type,
                "position_side": position_side,
                "trade_action": "sell_open" if position_side == "short" else "buy_open",
                "currency": currency,
                "contracts": contracts,
                "price": price,
                "multiplier": multiplier,
                "cash_in_gross": cash_in,
                "cash_out_gross": cash_out,
                "net_cashflow_gross": _round_money(cash_in - cash_out),
                "strategy": strategy,
                "leg_role": leg_role,
                "strategy_group_id": strategy_group_id,
            }
            cashflow_rows.append(cashflow_row)
            if position_side == "short":
                premium_rows.append(
                    {
                        "record_id": event_id,
                        "event_id": event_id,
                        "event_at": int(_event_ts(event) or 0),
                        "month": event_month,
                        "account": account,
                        "broker": broker,
                        "symbol": symbol,
                        "currency": currency,
                        "contracts": contracts,
                        "premium": price,
                        "multiplier": multiplier,
                        "premium_received_gross": amount,
                        "opened_at": int(_event_ts(event) or 0),
                    }
                )
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

        is_expire = _is_expire_close_event(event)
        close_cash_amount = 0.0 if is_expire else amount
        cash_in = close_cash_amount if position_side == "long" else 0.0
        cash_out = close_cash_amount if position_side == "short" else 0.0
        cashflow_rows.append(
            {
                "event_id": event_id,
                "event_at": int(_event_ts(event) or 0),
                "month": event_month,
                "account": account,
                "broker": broker,
                "symbol": symbol,
                "option_type": option_type,
                "position_side": position_side,
                "trade_action": "expire" if is_expire else ("buy_close" if position_side == "short" else "sell_close"),
                "currency": currency,
                "contracts": contracts,
                "price": price,
                "multiplier": multiplier,
                "cash_in_gross": cash_in,
                "cash_out_gross": cash_out,
                "net_cashflow_gross": _round_money(cash_in - cash_out),
                "strategy": strategy,
                "leg_role": leg_role,
                "strategy_group_id": strategy_group_id,
            }
        )
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
                    "open_amount_gross": open_amount,
                    "close_amount_gross": close_amount,
                    "realized_pnl_gross": realized_pnl,
                    "realized_gross": realized_pnl,
                    "close_type": (
                        EXPIRE_AUTO_CLOSE
                        if is_expire
                        else (BUY_TO_CLOSE if position_side == "short" else "sell_to_close")
                    ),
                    "closed_at": int(_event_ts(event) or 0),
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

    open_basis_rows = _build_open_basis_rows(open_lots)
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
            row.get("strategy") == "yield_enhancement" and row.get("position_side") == "long"
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
    filtered_realized_rows = [row for row in realized_rows if not month or row["month"] == month]
    filtered_premium_rows = [row for row in premium_rows if not month or row["month"] == month]
    filtered_open_basis_rows = [row for row in open_basis_rows if not month or row["month"] == month]
    enhancement_rows = [
        row
        for row in filtered_realized_rows
        if row.get("leg_role") == "enhancement_call"
        or (row.get("strategy") == "yield_enhancement" and row.get("position_side") == "long")
    ]
    return {
        "summary": _finalize_summary_rows(summary),
        "rows": sorted(filtered_realized_rows, key=_event_detail_sort_key),
        "premium_rows": sorted(filtered_premium_rows, key=_event_detail_sort_key),
        "cashflow_rows": sorted(filtered_cashflow_rows, key=_event_detail_sort_key),
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
) -> dict[str, Any]:
    account_norm = normalize_account(account) if account else None
    broker_norm = normalize_broker(broker) if broker else None
    converter = _build_exchange_rate_converter(rates)
    if trade_events:
        report = _build_monthly_income_report_from_events(
            trade_events,
            account_norm=account_norm,
            broker_norm=broker_norm,
            month=month,
            converter=converter,
        )
        report["filters"] = {
            "account": account_norm,
            "broker": broker_norm,
            "month": month,
        }
        return report

    all_rows: list[IncomeRow] = []
    rows: list[IncomeRow] = []
    all_open_cashflow_rows: list[OpenCashflowRow] = []
    premium_rows: list[PremiumIncomeRow] = []
    open_cashflow_rows: list[OpenCashflowRow] = []
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
        if row is not None:
            all_rows.append(row)
            if not month or row.month == month:
                rows.append(row)

        open_cashflow_row, open_cashflow_warning = build_open_cashflow_row(rec)
        if open_cashflow_warning:
            warnings.append(open_cashflow_warning)
        if open_cashflow_row is not None:
            all_open_cashflow_rows.append(open_cashflow_row)
            if not month or open_cashflow_row.month == month:
                open_cashflow_rows.append(open_cashflow_row)
            if open_cashflow_row.position_side == "short" and (not month or open_cashflow_row.month == month):
                premium_rows.append(
                    PremiumIncomeRow(
                        record_id=open_cashflow_row.record_id,
                        month=open_cashflow_row.month,
                        account=open_cashflow_row.account,
                        broker=open_cashflow_row.broker,
                        symbol=open_cashflow_row.symbol,
                        currency=open_cashflow_row.currency,
                        contracts=open_cashflow_row.contracts,
                        premium=open_cashflow_row.price,
                        multiplier=open_cashflow_row.multiplier,
                        premium_received_gross=open_cashflow_row.cash_in_gross,
                        opened_at=open_cashflow_row.opened_at,
                    )
                )

    legacy_open_basis_rows = _build_legacy_open_basis_rows(
        [row for row in all_open_cashflow_rows if not month or row.month == month],
        all_rows,
    )
    close_cashflow_rows = [_legacy_close_cashflow_row(row) for row in rows]

    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = _summary_bucket(summary, row.month, row.account, row.currency)
        _add_money(bucket, "realized_gross", row.realized_gross, converter=converter, currency=row.currency)
        _add_money(bucket, "realized_pnl_gross", row.realized_gross, converter=converter, currency=row.currency)
        close_cashflow_row = _legacy_close_cashflow_row(row)
        _add_money(
            bucket,
            "cash_in_gross",
            close_cashflow_row["cash_in_gross"],
            converter=converter,
            currency=row.currency,
        )
        _add_money(
            bucket,
            "cash_out_gross",
            close_cashflow_row["cash_out_gross"],
            converter=converter,
            currency=row.currency,
        )
        _add_money(
            bucket,
            "net_cashflow_gross",
            close_cashflow_row["net_cashflow_gross"],
            converter=converter,
            currency=row.currency,
        )
        if row.position_side == "short":
            bucket["realized_short_pnl_gross"] = _round_money(bucket["realized_short_pnl_gross"] + row.realized_gross)
            _add_money(
                bucket,
                "close_cost_gross",
                close_cashflow_row["cash_out_gross"],
                converter=converter,
                currency=row.currency,
            )
        else:
            bucket["realized_long_pnl_gross"] = _round_money(bucket["realized_long_pnl_gross"] + row.realized_gross)
            _add_money(
                bucket,
                "close_proceeds_gross",
                close_cashflow_row["cash_in_gross"],
                converter=converter,
                currency=row.currency,
            )
        bucket["closed_contracts"] = int(bucket["closed_contracts"]) + row.contracts_closed
        bucket["positions"] = int(bucket["positions"]) + 1

    for row in open_cashflow_rows:
        bucket = _summary_bucket(summary, row.month, row.account, row.currency)
        _add_money(bucket, "cash_in_gross", row.cash_in_gross, converter=converter, currency=row.currency)
        _add_money(bucket, "cash_out_gross", row.cash_out_gross, converter=converter, currency=row.currency)
        _add_money(bucket, "net_cashflow_gross", row.net_cashflow_gross, converter=converter, currency=row.currency)
        if row.position_side == "short":
            _add_money(bucket, "premium_received_gross", row.cash_in_gross, converter=converter, currency=row.currency)
            _add_money(
                bucket,
                "short_open_premium_gross",
                row.cash_in_gross,
                converter=converter,
                currency=row.currency,
            )
            bucket["premium_contracts"] = int(bucket["premium_contracts"]) + row.contracts
            bucket["premium_positions"] = int(bucket["premium_positions"]) + 1
        else:
            _add_money(bucket, "long_open_cost_gross", row.cash_out_gross, converter=converter, currency=row.currency)

    for row in legacy_open_basis_rows:
        bucket = _summary_bucket(summary, row["month"], row["account"], row["currency"])
        _add_money(
            bucket,
            "open_basis_lifecycle_pnl_gross",
            float(row["open_basis_lifecycle_pnl_gross"]),
            converter=converter,
            currency=row["currency"],
        )

    return {
        "summary": _finalize_summary_rows(summary),
        "rows": [
            r.as_dict()
            for r in sorted(rows, key=lambda x: (x.month, x.account, x.currency, x.symbol, x.record_id))
        ],
        "premium_rows": [
            r.as_dict()
            for r in sorted(
                premium_rows,
                key=lambda x: (x.month, x.account, x.currency, x.symbol, x.record_id),
            )
        ],
        "cashflow_rows": sorted(
            [r.as_dict() for r in open_cashflow_rows] + close_cashflow_rows,
            key=_event_detail_sort_key,
        ),
        "realized_rows": [
            r.as_dict()
            for r in sorted(rows, key=lambda x: (x.month, x.account, x.currency, x.symbol, x.record_id))
        ],
        "open_basis_rows": legacy_open_basis_rows,
        "enhancement_rows": [],
        "warnings": warnings,
        "calculation_method": "position_lots_legacy",
        "filters": {
            "account": account_norm,
            "broker": broker_norm,
            "month": month,
        },
    }
