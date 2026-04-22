from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.option_positions_core.domain import normalize_currency, normalize_option_type
from scripts.parse_option_message import infer_multiplier_with_source, normalize_symbol, parse_exp
from scripts.trade_account_mapping import resolve_internal_account


def _pick(src: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in src and src.get(key) not in (None, ""):
            return src.get(key)
    return None


def _norm_str(value: Any) -> str | None:
    s = str(value or "").strip()
    return s or None


def _norm_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _norm_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _normalize_side(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in ("buy", "buy_to_close", "buy_to_open", "b", "1"):
        return "buy"
    if raw in ("sell", "sell_to_open", "sell_to_close", "s", "2"):
        return "sell"
    return None


def _normalize_position_effect(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "open": "open",
        "open_position": "open",
        "open_only": "open",
        "buy_to_open": "open",
        "sell_to_open": "open",
        "close": "close",
        "close_position": "close",
        "close_only": "close",
        "buy_to_close": "close",
        "sell_to_close": "close",
    }
    return aliases.get(raw)


def _normalize_expiration(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) in (6, 8):
        return parse_exp(digits)
    return None


def _normalize_trade_time_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        num = int(value)
        if num > 10_000_000_000:
            return num
        return int(num * 1000)
    raw = str(value).strip()
    if raw.isdigit():
        return _normalize_trade_time_ms(int(raw))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class NormalizedTradeDeal:
    broker: str
    futu_account_id: str | None
    internal_account: str | None
    deal_id: str | None
    order_id: str | None
    symbol: str | None
    option_type: str | None
    side: str | None
    position_effect: str | None
    contracts: int | None
    price: float | None
    strike: float | None
    multiplier: int | None
    multiplier_source: str | None
    expiration_ymd: str | None
    currency: str | None
    trade_time_ms: int | None
    raw_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_trade_deal(
    payload: dict[str, Any] | Any,
    *,
    futu_account_mapping: dict[str, str] | None = None,
) -> NormalizedTradeDeal:
    src = payload if isinstance(payload, dict) else {}

    futu_account_id = _norm_str(
        _pick(src, "futu_account_id", "trd_acc_id", "account_id", "account")
    )
    symbol = normalize_symbol(str(_pick(src, "symbol", "stock_code", "code", "underlying") or ""))

    option_type_raw = _pick(src, "option_type", "put_call", "call_or_put")
    option_type = None
    if option_type_raw not in (None, ""):
        try:
            option_type = normalize_option_type(option_type_raw)
        except Exception:
            option_type = None

    currency_raw = _pick(src, "currency", "currency_code", "ccy")
    currency = None
    if currency_raw not in (None, ""):
        try:
            currency = normalize_currency(currency_raw)
        except Exception:
            currency = None

    position_effect = _normalize_position_effect(
        _pick(src, "position_effect", "position_side", "offset_type", "open_close")
    )
    repo_base = Path(__file__).resolve().parents[1]
    multiplier = _norm_int(_pick(src, "multiplier", "contract_multiplier", "lot_size"))
    multiplier, multiplier_source = infer_multiplier_with_source(
        symbol=symbol,
        multiplier=multiplier,
        repo_base=repo_base,
    )

    return NormalizedTradeDeal(
        broker="富途",
        futu_account_id=futu_account_id,
        internal_account=resolve_internal_account(futu_account_id, futu_account_mapping),
        deal_id=_norm_str(_pick(src, "deal_id", "dealID", "id")),
        order_id=_norm_str(_pick(src, "order_id", "orderID")),
        symbol=symbol,
        option_type=option_type,
        side=_normalize_side(_pick(src, "side", "trd_side", "trade_side")),
        position_effect=position_effect,
        contracts=_norm_int(_pick(src, "contracts", "qty", "quantity", "dealt_qty")),
        price=_norm_float(_pick(src, "price", "dealt_avg_price", "dealt_price", "avg_price")),
        strike=_norm_float(_pick(src, "strike", "strike_price")),
        multiplier=multiplier,
        multiplier_source=multiplier_source,
        expiration_ymd=_normalize_expiration(_pick(src, "expiration", "expiration_ymd", "expiry", "expiry_date")),
        currency=currency,
        trade_time_ms=_normalize_trade_time_ms(_pick(src, "trade_time_ms", "create_time", "updated_time")),
        raw_payload=dict(src),
    )
