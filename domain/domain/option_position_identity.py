from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from domain.domain.expiration_dates import expiration_timestamp_to_ymd
from domain.domain.symbol_identity import resolve_underlier_alias, symbol_currency


BUY_TO_CLOSE = "buy_to_close"
SELL_TO_CLOSE = "sell_to_close"
EXPIRE_AUTO_CLOSE = "expire_auto_close"


def norm_symbol(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return resolve_underlier_alias(raw)


def _compact_choice(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
        .replace("-", "_")
        .lower()
    )


def _normalize_choice(value: Any, aliases: dict[str, str], field_name: str, *, strict: bool = False) -> str:
    raw = str(value or "").strip()
    compact = _compact_choice(value)
    if compact in aliases:
        return aliases[compact]
    if strict:
        allowed = sorted(set(aliases.values()))
        raise ValueError(f"{field_name} must be one of: {', '.join(allowed)}")
    return raw.lower()


def normalize_broker(value: str | None) -> str:
    raw = str(value or "").strip()
    compact = (
        raw.replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
        .lower()
    )
    futu_aliases = {
        "富途",
        "富途证券",
        "富途证券(香港)",
        "富途證券",
        "富途證券(香港)",
        "富途牛牛",
        "futu",
        "futuhk",
        "futusecurities",
        "futusecurities(hongkong)",
        "futusecuritieshk",
    }
    if compact in futu_aliases or compact.startswith("futu"):
        return "富途"
    return raw


def normalize_account(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_option_type(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            "put": "put",
            "p": "put",
            "认沽": "put",
            "認沽": "put",
            "沽": "put",
            "call": "call",
            "c": "call",
            "认购": "call",
            "認購": "call",
            "购": "call",
            "購": "call",
        },
        "option_type",
        strict=strict,
    )


def normalize_side(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            "short": "short",
            "sell": "short",
            "sell_to_open": "short",
            "selltoopen": "short",
            "sto": "short",
            "卖出": "short",
            "賣出": "short",
            "卖开": "short",
            "賣開": "short",
            "long": "long",
            "buy": "long",
            "buy_to_open": "long",
            "buytoopen": "long",
            "bto": "long",
            "买入": "long",
            "買入": "long",
            "买开": "long",
            "買開": "long",
        },
        "side",
        strict=strict,
    )


def normalize_status(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            "open": "open",
            "opened": "open",
            "active": "open",
            "持仓": "open",
            "未平": "open",
            "未平仓": "open",
            "未平倉": "open",
            "close": "close",
            "closed": "close",
            "平仓": "close",
            "平倉": "close",
            "已平": "close",
            "已平仓": "close",
            "已平倉": "close",
        },
        "status",
        strict=strict,
    )


def normalize_currency(value: Any, *, strict: bool = False) -> str:
    raw = str(value or "").strip().upper()
    compact = _compact_choice(raw)
    aliases = {
        "USD": "USD",
        "US$": "USD",
        "$": "USD",
        "美元": "USD",
        "HKD": "HKD",
        "HK$": "HKD",
        "港币": "HKD",
        "港幣": "HKD",
        "CNY": "CNY",
        "CNH": "CNY",
        "RMB": "CNY",
        "人民币": "CNY",
        "人民幣": "CNY",
    }
    if raw in aliases:
        return aliases[raw]
    if compact in aliases:
        return aliases[compact]
    if strict:
        raise ValueError("currency must be one of: CNY, HKD, USD")
    return raw


def infer_currency_from_symbol(symbol: Any) -> str | None:
    return symbol_currency(symbol)


def resolve_open_currency(symbol: Any, currency: Any) -> str:
    normalized = normalize_currency(currency)
    inferred = infer_currency_from_symbol(symbol)
    return normalize_currency(normalized or inferred, strict=True)


def normalize_close_type(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            BUY_TO_CLOSE: BUY_TO_CLOSE,
            "btc": BUY_TO_CLOSE,
            "buytoclose": BUY_TO_CLOSE,
            "buyclose": BUY_TO_CLOSE,
            "买入平仓": BUY_TO_CLOSE,
            "買入平倉": BUY_TO_CLOSE,
            "买平": BUY_TO_CLOSE,
            "買平": BUY_TO_CLOSE,
            EXPIRE_AUTO_CLOSE: EXPIRE_AUTO_CLOSE,
            "expireautoclose": EXPIRE_AUTO_CLOSE,
            "expired": EXPIRE_AUTO_CLOSE,
            "expire": EXPIRE_AUTO_CLOSE,
            "到期": EXPIRE_AUTO_CLOSE,
            "到期平仓": EXPIRE_AUTO_CLOSE,
            "到期平倉": EXPIRE_AUTO_CLOSE,
            "到期自动平仓": EXPIRE_AUTO_CLOSE,
            "到期自動平倉": EXPIRE_AUTO_CLOSE,
        },
        "close_type",
        strict=strict,
    )


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def parse_exp_to_ms(exp_ymd: str | None) -> int | None:
    try:
        if not exp_ymd:
            return None
        y, m, d = map(int, str(exp_ymd).split("-"))
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def exp_ms_to_datetime(value: Any) -> datetime | None:
    try:
        if value in (None, "", 0):
            return None
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except Exception:
        return None


def exp_ms_to_ymd(value: Any) -> str | None:
    return expiration_timestamp_to_ymd(value)
