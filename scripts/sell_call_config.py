"""Sell-call threshold resolution and validation.

Single source of truth for min annualized net premium return:
symbol.sell_call > templates.sell_call > DEFAULT constant.

Compatibility:
- Preferred field: min_annualized_net_premium_return
- Legacy field (still accepted): min_annualized_net_return
"""

from __future__ import annotations

import math


DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN = 0.07
PRIMARY_FIELD = "min_annualized_net_premium_return"
LEGACY_FIELD = "min_annualized_net_return"


def _to_float(value, *, source: str) -> float:
    if value is None:
        raise ValueError(f"{source} is required and must be within [0, 1]")
    if isinstance(value, bool):
        raise ValueError(f"{source} must be a number within [0, 1], got bool")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{source} must be a number within [0, 1], got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"{source} must be a finite number within [0, 1], got {value!r}")
    return out


def validate_min_annualized_net_premium_return(value, *, source: str) -> float:
    out = _to_float(value, source=source)
    if out < 0.0 or out > 1.0:
        raise ValueError(f"{source} must be within [0, 1], got {out}")
    return out


def _pick_compatible_field(section: dict, *, source_prefix: str) -> tuple[object | None, str | None]:
    if not isinstance(section, dict):
        return None, None

    v = section.get(PRIMARY_FIELD)
    if v is not None:
        return v, f"{source_prefix}.{PRIMARY_FIELD}"

    v = section.get(LEGACY_FIELD)
    if v is not None:
        return v, f"{source_prefix}.{LEGACY_FIELD}"

    return None, None


def resolve_min_annualized_net_premium_return(*, symbol_cfg: dict, profiles: dict | None = None) -> float:
    sc = (symbol_cfg.get("sell_call") or {}) if isinstance(symbol_cfg, dict) else {}

    symbol_value, symbol_source = _pick_compatible_field(sc, source_prefix="symbol.sell_call")
    if symbol_source is not None:
        return validate_min_annualized_net_premium_return(symbol_value, source=symbol_source)

    template_value = None
    use = symbol_cfg.get("use") if isinstance(symbol_cfg, dict) else None
    if isinstance(use, str):
        use_list = [use]
    elif isinstance(use, list):
        use_list = [x for x in use if isinstance(x, str)]
    else:
        use_list = []

    if isinstance(profiles, dict):
        for name in use_list:
            p = profiles.get(name)
            if not isinstance(p, dict):
                continue
            v, src = _pick_compatible_field(
                p.get("sell_call") or {},
                source_prefix=f"templates.{name}.sell_call",
            )
            if src is None:
                continue
            template_value = validate_min_annualized_net_premium_return(v, source=src)

    if template_value is not None:
        return template_value

    return validate_min_annualized_net_premium_return(
        DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN,
        source="DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN",
    )


def resolve_min_annualized_net_premium_return_from_sell_call_cfg(*, sell_call_cfg: dict, source_prefix: str) -> float:
    value, source = _pick_compatible_field(sell_call_cfg or {}, source_prefix=source_prefix)
    if source is not None:
        return validate_min_annualized_net_premium_return(value, source=source)
    return validate_min_annualized_net_premium_return(
        DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN,
        source="DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN",
    )
