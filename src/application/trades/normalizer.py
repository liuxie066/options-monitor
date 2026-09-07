from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.ledger.position_fields import normalize_option_type
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_execution import (
    canonical_decimal,
    canonical_utc_instant,
    epoch_milliseconds_instant,
    execution_economic_content,
    normalize_execution_input,
)
from src.application.multiplier_cache import resolve_multiplier_with_source_and_diagnostics
from domain.domain.trade_contract_identity import (
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from src.application.trades.account_mapping import resolve_internal_account
from domain.domain.trade_account_identity import (
    extract_primary_account_id,
    extract_visible_account_fields,
)
from domain.domain.symbol_identity import OPTION_CODE_RE, normalize_symbol_candidate, pick_first_normalized_symbol, symbol_market
from src.application.symbol_aliases import symbol_aliases_from_config
from src.application.trades.field_normalization import (
    normalize_optional_float,
    normalize_optional_int,
    normalize_optional_text,
)


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
            decimal_digits = strike_digits.zfill(4)
            strike_value = canonical_decimal(decimal_digits[:-3] + "." + decimal_digits[-3:])
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


def _normalize_side(value: Any) -> str | None:
    return normalize_trade_side(value)


def _normalize_position_effect(value: Any) -> str | None:
    return normalize_position_effect(value)


def _normalize_expiration(value: Any) -> str | None:
    return normalize_contract_expiration(value)


_FUTU_TRADE_TIME_ZONE = ZoneInfo("Asia/Shanghai")


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
    iso_raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_FUTU_TRADE_TIME_ZONE)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=_FUTU_TRADE_TIME_ZONE)
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
    asset_type: str | None = None
    execution_input: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _futu_asset_type(src: dict[str, Any], option_info: dict[str, Any]) -> str | None:
    raw_type = _pick(src, "asset_type", "security_type", "stock_type", "sec_type")
    if raw_type is not None:
        kind = str(raw_type).strip().lower().rsplit(".", 1)[-1]
        return {"stock": "stock", "etf": "stock", "equity": "stock", "option": "option"}.get(kind)
    if option_info or _pick(src, "option_type", "put_call", "call_or_put") is not None:
        return "option"
    code = str(_pick(src, "code", "stock_code") or "").strip().upper()
    if re.fullmatch(r"US\.[A-Z][A-Z.\-]{0,9}|HK\.\d{4,5}|\d{4,5}\.HK", code):
        return "stock"
    return None


def _source_multiplier_errors(src: dict[str, Any]) -> list[str]:
    supplied = {key: normalize_optional_int(src[key]) for key in ("multiplier", "contract_multiplier", "lot_size") if src.get(key) not in (None, "")}
    errors = [f"invalid:instrument_ref.multiplier:source_field:{key}" for key, value in supplied.items() if value is None or value <= 0]
    if len({value for value in supplied.values() if value is not None and value > 0}) > 1:
        errors.append("invalid:instrument_ref.multiplier:source_alias_conflict")
    return errors


def _futu_execution_input(src: dict[str, Any]) -> dict[str, Any]:
    """Map Futu execution fields only; SDK enrichment belongs to the adapter caller."""
    code = _pick(src, "code", "stock_code", "symbol")
    option_info = _parse_futu_option_code(code)
    symbol = pick_first_normalized_symbol(src, *_SYMBOL_KEYS)
    asset_type = _futu_asset_type(src, option_info)
    raw_time = _pick(src, "occurred_at_utc", "trade_time_ms", "create_time", "updated_time")
    occurred_at = raw_time
    try:
        if "occurred_at_utc" in src:
            occurred_at = canonical_utc_instant(raw_time)
        elif "trade_time_ms" in src:
            occurred_at = epoch_milliseconds_instant(str(raw_time) if isinstance(raw_time, float) else raw_time)
        elif raw_time is not None:
            time_text = str(raw_time).strip().replace("/", "-")
            if not re.search(r"Z$|[+-]\d{2}:\d{2}$", time_text):
                time_text += "+08:00"
            occurred_at = canonical_utc_instant(time_text)
    except (ValueError, OverflowError):
        pass

    def decimal_source(*keys: str) -> Any:
        value = _pick(src, *keys)
        return str(value) if isinstance(value, float) else value

    option_type = _pick(src, "option_type", "put_call", "call_or_put") or option_info.get("option_type")
    currency = _pick(src, "currency", "currency_code", "ccy") or option_info.get("currency")
    data_type = str(_pick(src, "data_type", "record_type", "granularity") or "execution").strip().lower()
    if data_type in {"deal", "fill", "trade"}:
        data_type = "execution"
    if any(key in src for key in ("dealt_avg_price", "avg_price", "dealt_qty")) and not all(
        _pick(src, *keys) is not None for keys in (("qty", "quantity", "contracts"), ("price", "execution_price", "dealt_price"))
    ):
        data_type = "order_summary"
    execution = normalize_execution_input({
        "broker_account_ref": {
            "broker_account_id": src.get("broker_account_id"),
            "broker_id": "futu",
            "external_account_id": extract_primary_account_id(src),
            "environment": _pick(src, "environment", "trd_env"),
            "account_label": _pick(src, "internal_account", "account_label"),
        },
        "instrument_ref": {
            "asset_type": asset_type,
            "symbol": symbol,
            "market": src.get("market") or option_info.get("option_code_market") or symbol_market(symbol),
            "currency": currency,
            "source_code": code,
            "source_security_type": _pick(src, "security_type", "stock_type", "sec_type"),
            "option_type": option_type,
            "strike": decimal_source("strike", "strike_price") if _pick(src, "strike", "strike_price") is not None else option_info.get("strike"),
            "expiration_ymd": _normalize_expiration(_pick(src, "expiration", "expiration_ymd", "expiry", "expiry_date")) or option_info.get("expiration_ymd"),
            "multiplier": decimal_source("multiplier", "contract_multiplier", "lot_size"),
            "deliverable": src.get("deliverable"),
        },
        "data_type": data_type,
        "external_id_namespace": _pick(src, "external_id_namespace", "execution_id_namespace"),
        "external_execution_id": _pick(src, "external_execution_id", "deal_id", "dealID", "id"),
        "external_order_namespace": src.get("external_order_namespace"),
        "external_order_id": _pick(src, "external_order_id", "order_id", "orderID"),
        "side": _pick(src, "side", "trd_side", "trade_side"),
        "position_effect": _pick(src, "position_effect", "position_side", "offset_type", "open_close", "trd_side", "trade_side", "side"),
        "quantity": decimal_source("contracts", "qty", "quantity"),
        "price": decimal_source("price", "execution_price", "dealt_price"),
        "currency": currency,
        "occurred_at_utc": occurred_at,
        "source_time": raw_time,
        "source_timezone": src.get("source_timezone") or ("UTC" if "occurred_at_utc" in src or "trade_time_ms" in src else "Asia/Shanghai"),
        "evidence_refs": src.get("evidence_refs") or [],
    })
    if asset_type == "option":
        execution["errors"].extend(_source_multiplier_errors(src))
    return execution


def canonical_trade_execution_content(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("execution_input")
    if isinstance(nested, dict):
        execution = normalize_execution_input(nested)
    elif _is_standard_execution(payload):
        execution = normalize_execution_input(payload)
    else:
        execution = _futu_execution_input(payload)
    return execution_economic_content(execution)


def _is_standard_execution(src: dict[str, Any]) -> bool:
    broker = str(_pick(src, "broker_id", "broker") or "futu").strip().lower()
    return any(name in src for name in ("execution_input", "instrument_ref", "broker_account_ref")) or broker not in {"futu", "富途"}


def _standard_trade_deal(src: dict[str, Any]) -> NormalizedTradeDeal:
    execution = normalize_execution_input(src.get("execution_input") if isinstance(src.get("execution_input"), dict) else src)
    account = execution["broker_account_ref"]
    instrument = execution["instrument_ref"]
    return NormalizedTradeDeal(
        broker=account.get("broker_id") or "unknown",
        futu_account_id=account.get("external_account_id"),
        internal_account=account.get("account_label"),
        deal_id=execution["external_execution_id"],
        order_id=execution["external_order_id"],
        symbol=instrument.get("symbol"),
        option_type=instrument.get("option_type"),
        side=execution["side"],
        position_effect=execution["position_effect"],
        contracts=normalize_optional_int(execution["quantity"]),
        price=normalize_optional_float(execution["price"]),
        strike=normalize_optional_float(instrument.get("strike")),
        multiplier=normalize_optional_int(instrument.get("multiplier")),
        multiplier_source="input" if instrument.get("multiplier") is not None else None,
        expiration_ymd=instrument.get("expiration_ymd"),
        currency=execution["currency"],
        trade_time_ms=_normalize_trade_time_ms(execution["occurred_at_utc"]),
        raw_payload=dict(src),
        normalization_diagnostics={"execution_input": {"errors": execution["errors"]}},
        asset_type=instrument.get("asset_type"),
        execution_input=execution,
    )


def normalize_trade_deal(
    payload: dict[str, Any] | Any,
    *,
    futu_account_mapping: dict[str, str] | None = None,
    repo_base: Path | None = None,
    runtime_root: str | Path | None = None,
    config_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    opend_fetch_config: dict[str, float | int] | None = None,
    allow_opend_refresh: bool = True,
) -> NormalizedTradeDeal:
    src = payload if isinstance(payload, dict) else {}
    if _is_standard_execution(src):
        deal = _standard_trade_deal(src)
        if futu_account_mapping is not None:
            mapped = resolve_internal_account(deal.futu_account_id, futu_account_mapping)
            if not mapped or (deal.internal_account and deal.internal_account != mapped):
                deal.execution_input["errors"].append("invalid:broker_account_ref:configured_mapping_mismatch")
            else:
                from dataclasses import replace
                deal = replace(deal, internal_account=mapped)
        return deal
    visible_account_fields = extract_visible_account_fields(src)
    futu_account_id = extract_primary_account_id(src)
    option_code_info = _parse_futu_option_code(_pick(src, "code", "stock_code", "symbol"))
    asset_type = _futu_asset_type(src, option_code_info)
    symbol_aliases = symbol_aliases_from_config(config)
    raw_symbol_fields = {
        key: src.get(key)
        for key in _SYMBOL_KEYS
        if key in src and src.get(key) not in (None, "")
    }
    symbol = pick_first_normalized_symbol(src, *_SYMBOL_KEYS, symbol_aliases=symbol_aliases)
    if symbol is None:
        root_symbol = normalize_symbol_candidate(
            option_code_info.get("option_code_root"),
            symbol_aliases=symbol_aliases,
        )
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
    base = Path(repo_base).resolve() if repo_base is not None else Path(__file__).resolve().parents[3]
    raw_multiplier = _pick(src, "multiplier", "contract_multiplier", "lot_size")
    multiplier = normalize_optional_int(raw_multiplier)
    source_multiplier_errors = _source_multiplier_errors(src) if asset_type == "option" else []
    invalid_source_multiplier = bool(source_multiplier_errors)
    if invalid_source_multiplier:
        multiplier = None
    multiplier_source = "payload" if multiplier is not None else None
    multiplier_diagnostics: dict[str, Any] = {}
    if asset_type == "option" and invalid_source_multiplier:
        multiplier_diagnostics = {
            "canonical_symbol": symbol, "selected_source": None,
            "attempted_sources": [{"source": "payload", "status": "invalid"}],
            "message": "source multiplier must be a positive integer",
            "errors": source_multiplier_errors,
        }
    elif asset_type == "option":
        multiplier, multiplier_source, multiplier_diagnostics = resolve_multiplier_with_source_and_diagnostics(
            repo_base=base,
            symbol=symbol,
            multiplier=multiplier,
            runtime_root=runtime_root,
            config_path=config_path,
            allow_opend_refresh=bool(allow_opend_refresh),
            host=host,
            port=port,
            opend_fetch_config=opend_fetch_config,
            config=config,
        )

    strike = normalize_optional_float(_pick(src, "strike", "strike_price"))
    if strike is None and option_code_info.get("strike") is not None:
        strike = float(option_code_info["strike"])
    expiration_ymd = _normalize_expiration(_pick(src, "expiration", "expiration_ymd", "expiry", "expiry_date"))
    if expiration_ymd is None:
        expiration_ymd = str(option_code_info.get("expiration_ymd") or "").strip() or None

    execution = _futu_execution_input(
        {**src, "multiplier": multiplier} if raw_multiplier is None and multiplier is not None else src
    )

    return NormalizedTradeDeal(
        broker="富途",
        futu_account_id=futu_account_id,
        internal_account=resolve_internal_account(futu_account_id, futu_account_mapping),
        deal_id=normalize_optional_text(_pick(src, "deal_id", "dealID", "id")),
        order_id=normalize_optional_text(_pick(src, "order_id", "orderID")),
        symbol=symbol,
        option_type=option_type,
        side=_normalize_side(_pick(src, "side", "trd_side", "trade_side")),
        position_effect=position_effect,
        contracts=normalize_optional_int(_pick(src, "contracts", "qty", "quantity")),
        price=normalize_optional_float(_pick(src, "price", "execution_price", "dealt_price")),
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
            "execution_input": {"errors": execution["errors"]},
        },
        asset_type=asset_type,
        execution_input=execution,
    )
