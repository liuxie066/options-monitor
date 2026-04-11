"""Sell-put threshold resolution and validation.

Single source of truth for min annualized net return:
symbol.sell_put > templates.sell_put > DEFAULT constant.
"""

from __future__ import annotations

import math


DEFAULT_MIN_ANNUALIZED_NET_RETURN = 0.07


def _to_float(value, *, source: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{source} must be a number within [0, 1], got bool")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{source} must be a number within [0, 1], got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"{source} must be a finite number within [0, 1], got {value!r}")
    return out


def validate_min_annualized_net_return(value, *, source: str) -> float:
    out = _to_float(value, source=source)
    if out < 0.0 or out > 1.0:
        raise ValueError(f"{source} must be within [0, 1], got {out}")
    return out


def resolve_min_annualized_net_return(*, symbol_cfg: dict, profiles: dict | None = None) -> float:
    sp = (symbol_cfg.get("sell_put") or {}) if isinstance(symbol_cfg, dict) else {}
    symbol_value = sp.get("min_annualized_net_return")
    if symbol_value is not None:
        return validate_min_annualized_net_return(
            symbol_value,
            source="symbol.sell_put.min_annualized_net_return",
        )

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
            v = ((p.get("sell_put") or {}).get("min_annualized_net_return"))
            if v is None:
                continue
            template_value = validate_min_annualized_net_return(
                v,
                source=f"templates.{name}.sell_put.min_annualized_net_return",
            )

    if template_value is not None:
        return template_value

    return validate_min_annualized_net_return(
        DEFAULT_MIN_ANNUALIZED_NET_RETURN,
        source="DEFAULT_MIN_ANNUALIZED_NET_RETURN",
    )
