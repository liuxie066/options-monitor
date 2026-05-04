from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
from typing import Any

from scripts.option_positions_core.domain import normalize_option_type
from scripts.trade_symbol_identity import canonical_symbol
from src.application.expiration_normalization import normalize_expiration_ymd


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


def normalize_trade_side(value: Any) -> str | None:
    compact = _compact_choice(value)
    aliases = {
        "buy": "buy",
        "b": "buy",
        "1": "buy",
        "buy_to_open": "buy",
        "buytoopen": "buy",
        "bto": "buy",
        "buy_to_close": "buy",
        "buytoclose": "buy",
        "btc": "buy",
        "buy_back": "buy",
        "buyback": "buy",
        "买": "buy",
        "買": "buy",
        "买入": "buy",
        "買入": "buy",
        "买开": "buy",
        "買開": "buy",
        "买平": "buy",
        "買平": "buy",
        "sell": "sell",
        "s": "sell",
        "2": "sell",
        "sell_to_open": "sell",
        "selltoopen": "sell",
        "sto": "sell",
        "sell_to_close": "sell",
        "selltoclose": "sell",
        "sell_short": "sell",
        "short_sell": "sell",
        "卖": "sell",
        "賣": "sell",
        "卖出": "sell",
        "賣出": "sell",
        "卖开": "sell",
        "賣開": "sell",
    }
    return aliases.get(compact)


def normalize_position_effect(value: Any) -> str | None:
    compact = _compact_choice(value)
    aliases = {
        "open": "open",
        "opened": "open",
        "open_position": "open",
        "openposition": "open",
        "open_only": "open",
        "openonly": "open",
        "buy_to_open": "open",
        "buytoopen": "open",
        "bto": "open",
        "sell_to_open": "open",
        "selltoopen": "open",
        "sto": "open",
        "sell_short": "open",
        "short_sell": "open",
        "卖开": "open",
        "賣開": "open",
        "买开": "open",
        "買開": "open",
        "close": "close",
        "closed": "close",
        "close_position": "close",
        "closeposition": "close",
        "close_only": "close",
        "closeonly": "close",
        "buy_to_close": "close",
        "buytoclose": "close",
        "btc": "close",
        "buy_back": "close",
        "buyback": "close",
        "sell_to_close": "close",
        "selltoclose": "close",
        "买平": "close",
        "買平": "close",
        "卖平": "close",
        "賣平": "close",
        "void": "void",
        "voided": "void",
        "cancel": "void",
        "cancelled": "void",
        "canceled": "void",
        "adjust": "adjust",
        "adjusted": "adjust",
        "adjustment": "adjust",
    }
    return aliases.get(compact)


def normalize_contract_expiration(value: Any, *, fallback_raw: bool = False) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = normalize_expiration_ymd(raw)
    if normalized:
        return normalized
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 6:
        candidate = f"20{digits[:2]}-{digits[2:4]}-{digits[4:6]}"
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
        except Exception:
            return raw if fallback_raw else None
        return candidate
    return raw if fallback_raw else None


def normalize_contract_option_type(value: Any, *, fallback_raw: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        normalized = normalize_option_type(raw)
    except Exception:
        normalized = ""
    if normalized in {"put", "call"}:
        return normalized
    return raw.lower() if fallback_raw else ""


def canonical_contract_symbol(value: Any, *, base_dir: Path | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return canonical_symbol(raw, base_dir=base_dir) or raw.upper()


def contract_strike_key(value: Any) -> str:
    try:
        if value in (None, ""):
            return ""
        numeric = float(value)
        if math.isnan(numeric):
            return ""
        return f"{numeric:.6f}"
    except Exception:
        return ""


def contract_key(
    symbol: Any,
    option_type: Any,
    expiration: Any,
    strike: Any,
    *,
    base_dir: Path | None = None,
    option_type_fallback_raw: bool = False,
    expiration_fallback_raw: bool = False,
) -> tuple[str, str, str, str]:
    return (
        canonical_contract_symbol(symbol, base_dir=base_dir),
        normalize_contract_option_type(option_type, fallback_raw=option_type_fallback_raw),
        normalize_contract_expiration(expiration, fallback_raw=expiration_fallback_raw) or "",
        contract_strike_key(strike),
    )
