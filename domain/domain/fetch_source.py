from __future__ import annotations

from typing import Any


FUTU_INTERNAL_SOURCE = "opend"
YAHOO_INTERNAL_SOURCE = "yahoo"

_FUTU_ALIASES = {
    "futu",
    "futuapi",
    "futu_api",
    "futuopend",
    "futu_opend",
    "opend",
    "open_d",
}

_YAHOO_ALIASES = {
    "yahoo",
    "yfinance",
    "yahoo_finance",
}


def normalize_fetch_source(value: Any, *, default: str = YAHOO_INTERNAL_SOURCE) -> str:
    """Normalize user-facing fetch source names to internal source ids."""
    raw = str(value if value is not None else default).strip().lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    compact = raw.replace("_", "")
    if raw in _FUTU_ALIASES or compact in _FUTU_ALIASES:
        return FUTU_INTERNAL_SOURCE
    if raw in _YAHOO_ALIASES or compact in _YAHOO_ALIASES:
        return YAHOO_INTERNAL_SOURCE
    return raw or str(default or YAHOO_INTERNAL_SOURCE)


def is_futu_fetch_source(value: Any) -> bool:
    return normalize_fetch_source(value) == FUTU_INTERNAL_SOURCE
