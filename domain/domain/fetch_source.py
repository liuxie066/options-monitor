from __future__ import annotations

from typing import Any


FUTU_INTERNAL_SOURCE = "opend"

_FUTU_ALIASES = {
    "futu",
    "futuapi",
    "futu_api",
    "futuopend",
    "futu_opend",
    "opend",
    "open_d",
}

def normalize_fetch_source(value: Any, *, default: str = FUTU_INTERNAL_SOURCE) -> str:
    """Normalize user-facing fetch source names to the only supported source id."""
    raw = str(value if value is not None else default).strip().lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    compact = raw.replace("_", "")
    if raw in _FUTU_ALIASES or compact in _FUTU_ALIASES:
        return FUTU_INTERNAL_SOURCE
    return str(default or FUTU_INTERNAL_SOURCE)


def is_futu_fetch_source(value: Any) -> bool:
    return normalize_fetch_source(value) == FUTU_INTERNAL_SOURCE


def resolve_symbol_fetch_source(fetch_cfg: Any, *, default: str = FUTU_INTERNAL_SOURCE) -> tuple[str, str]:
    """Resolve symbol fetch source plus its audit-friendly decision label."""
    cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}

    raw_source = cfg.get("source")
    if raw_source is not None and str(raw_source).strip():
        return (normalize_fetch_source(raw_source, default=default), "configured_opend")

    src = normalize_fetch_source(default, default=default)
    return (src, f"default_{src}")
