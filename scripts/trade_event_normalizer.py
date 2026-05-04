from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from scripts.option_positions_core.domain import normalize_currency, normalize_option_type
from scripts.multiplier_cache import resolve_multiplier_with_source_and_diagnostics
from scripts.parse_option_message import parse_exp
from scripts.trade_account_mapping import resolve_internal_account
from scripts.trade_account_identity import (
    ACCOUNT_ID_KEYS,
    extract_primary_account_id,
    extract_visible_account_fields,
)
from scripts.trade_symbol_identity import OPTION_CODE_RE, normalize_symbol_candidate, pick_first_normalized_symbol


def _pick(src: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in src and src.get(key) not in (None, ""):
            return src.get(key)
    return None


def _parse_futu_option_code(code: Any) -> dict[str, Any]:
    raw = str(code or "").strip().upper()
    match = OPTION_CODE_RE.match(raw)
    if not match:
        return {}
    strike_digits = match.group("strike")
    strike_value = None
    if strike_digits:
        try:
            strike_value = int(strike_digits) / 1000.0
        except Exception:
            strike_value = None
    expiration = f"20{match.group('yy')}-{match.group('mm')}-{match.group('dd')}"
    option_type = "call" if match.group("cp") == "C" else "put"
    return {
        "option_code_market": match.group("market"),
        "option_code_root": match.group("root"),
        "option_type": option_type,
        "expiration_ymd": expiration,
        "strike": strike_value,
        "currency": ("HKD" if match.group("market") == "HK" else "USD" if match.group("market") == "US" else None),
    }


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
    if raw in ("buy_back", "buyback"):
        return "buy"
    if raw in ("sell", "sell_to_open", "sell_to_close", "sell_short", "short_sell", "s", "2"):
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
        "sell_short": "open",
        "short_sell": "open",
        "close": "close",
        "close_position": "close",
        "close_only": "close",
        "buy_to_close": "close",
        "sell_to_close": "close",
        "buy_back": "close",
        "buyback": "close",
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


_SYMBOL_KEYS = (
    "symbol",
    "underlying_symbol",
    "owner_symbol",
    "owner_stock_code",
    "owner_stock_code_full",
    "underlying_stock_code",
    "owner_code",
    "underlying_code",
    "stock_code",
    "code",
    "owner_stock_name",
    "underlying_stock_name",
    "owner_name",
    "stock_name",
    "name",
    "underlying",
)


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
    visible_account_fields: dict[str, str] = field(default_factory=dict)
    account_mapping_keys: list[str] = field(default_factory=list)
    normalization_diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_trade_deal(
    payload: dict[str, Any] | Any,
    *,
    futu_account_mapping: dict[str, str] | None = None,
    repo_base: Path | None = None,
    config: dict[str, Any] | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    opend_fetch_config: dict[str, float | int] | None = None,
) -> NormalizedTradeDeal:
    src = payload if isinstance(payload, dict) else {}
    visible_account_fields = extract_visible_account_fields(src)
    futu_account_id = extract_primary_account_id(src)
    option_code_info = _parse_futu_option_code(_pick(src, "code", "stock_code", "symbol"))
    raw_symbol_fields = {
        key: src.get(key)
        for key in _SYMBOL_KEYS
        if key in src and src.get(key) not in (None, "")
    }
    symbol = pick_first_normalized_symbol(src, *_SYMBOL_KEYS)
    if symbol is None:
        root_symbol = normalize_symbol_candidate(option_code_info.get("option_code_root"))
        if root_symbol:
            symbol = root_symbol

    option_type_raw = _pick(src, "option_type", "put_call", "call_or_put")
    option_type = None
    if option_type_raw not in (None, ""):
        try:
            option_type = normalize_option_type(option_type_raw)
        except Exception:
            option_type = None
    if option_type is None:
        option_type = str(option_code_info.get("option_type") or "").strip() or None

    currency_raw = _pick(src, "currency", "currency_code", "ccy")
    currency = None
    if currency_raw not in (None, ""):
        try:
            currency = normalize_currency(currency_raw)
        except Exception:
            currency = None
    if currency is None:
        fallback_currency = option_code_info.get("currency")
        if fallback_currency not in (None, ""):
            try:
                currency = normalize_currency(fallback_currency)
            except Exception:
                currency = None

    position_effect = _normalize_position_effect(
        _pick(src, "position_effect", "position_side", "offset_type", "open_close", "trd_side", "trade_side", "side")
    )
    base = Path(repo_base).resolve() if repo_base is not None else Path(__file__).resolve().parents[1]
    multiplier = _norm_int(_pick(src, "multiplier", "contract_multiplier", "lot_size"))
    multiplier, multiplier_source, multiplier_diagnostics = resolve_multiplier_with_source_and_diagnostics(
        repo_base=base,
        symbol=symbol,
        multiplier=multiplier,
        allow_opend_refresh=True,
        host=host,
        port=port,
        opend_fetch_config=opend_fetch_config,
        config=config,
    )

    strike = _norm_float(_pick(src, "strike", "strike_price"))
    if strike is None and option_code_info.get("strike") is not None:
        strike = float(option_code_info["strike"])
    expiration_ymd = _normalize_expiration(_pick(src, "expiration", "expiration_ymd", "expiry", "expiry_date"))
    if expiration_ymd is None:
        expiration_ymd = str(option_code_info.get("expiration_ymd") or "").strip() or None

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
        strike=strike,
        multiplier=multiplier,
        multiplier_source=multiplier_source,
        expiration_ymd=expiration_ymd,
        currency=currency,
        trade_time_ms=_normalize_trade_time_ms(_pick(src, "trade_time_ms", "create_time", "updated_time")),
        raw_payload=dict(src),
        visible_account_fields=visible_account_fields,
        account_mapping_keys=sorted(str(key).strip() for key in (futu_account_mapping or {}).keys() if str(key).strip()),
        normalization_diagnostics={
            "symbol": {
                "canonical": symbol,
                "raw_fields": raw_symbol_fields,
                "option_code": option_code_info,
            },
            "multiplier_resolution": multiplier_diagnostics,
        },
    )
