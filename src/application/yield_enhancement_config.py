from __future__ import annotations

from copy import deepcopy
from typing import Any


YIELD_ENHANCEMENT_OUTPUT_MODES: set[str] = {"inline", "separate", "both"}
YIELD_ENHANCEMENT_OBJECTIVES: set[str] = {"premium_funded_long_call"}
YIELD_ENHANCEMENT_FUNDING_MODES: set[str] = {"credit_or_even", "max_debit"}
YIELD_ENHANCEMENT_LEGACY_OPTIMIZER_FIELDS: tuple[str, ...] = (
    "optimizer_enabled",
    "max_downside_worsen_pct",
    "min_scenario_score_lift",
    "min_annualized_scenario_score_lift",
    "min_lift_to_downside_ratio",
    "max_combo_spread_worsen_ratio",
)
YIELD_ENHANCEMENT_LEGACY_CALL_BOUND_FIELDS: tuple[str, ...] = (
    "min_call_otm_pct",
    "max_call_otm_pct",
)
YIELD_ENHANCEMENT_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "objective": "premium_funded_long_call",
    "output_mode": "separate",
    "funding_mode": "credit_or_even",
    "min_combo_net_credit": 0.0,
    "max_call_cost_to_put_credit": 1.0,
    "min_upside_lift_to_call_cost": 1.5,
    "min_upside_lift_to_put_credit": 0.5,
    "min_put_otm_pct": 0.05,
    "min_open_interest": 100,
    "min_volume": 5,
    "max_spread_ratio": 0.35,
    "max_combo_spread_ratio": 0.50,
    "call": {
        "min_otm_pct": 0.03,
        "max_otm_pct": 0.40,
        "min_delta": 0.10,
        "max_delta": 0.45,
    },
}
YIELD_ENHANCEMENT_MARKET_DEFAULT_OVERRIDES: dict[str, dict[str, Any]] = {
    "hk": {
        "min_volume": 0,
    },
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def yield_enhancement_defaults_for_market(market: str | None = None) -> dict[str, Any]:
    market_key = str(market or "").strip().lower()
    defaults = deepcopy(YIELD_ENHANCEMENT_DEFAULTS)
    override = YIELD_ENHANCEMENT_MARKET_DEFAULT_OVERRIDES.get(market_key)
    if override:
        defaults = _deep_merge_dict(defaults, override)
    return defaults


def apply_yield_enhancement_defaults(cfg: dict[str, Any] | None, *, market: str | None = None) -> dict[str, Any]:
    defaults = yield_enhancement_defaults_for_market(market)
    return _deep_merge_dict(defaults, _as_dict(cfg))


def resolve_yield_enhancement_cfg(symbol_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(symbol_cfg or {})
    top_level = _as_dict(cfg.get("yield_enhancement"))

    has_top_level = isinstance(cfg.get("yield_enhancement"), dict)
    if not has_top_level:
        return {}

    top_level = apply_yield_enhancement_defaults(top_level)
    output_mode = str(top_level.get("output_mode") or "").strip().lower()
    if not output_mode:
        output_mode = "separate"
    top_level["output_mode"] = output_mode

    if "enabled" in top_level:
        top_level["enabled"] = bool(top_level.get("enabled"))

    return top_level


def yield_enhancement_output_mode(cfg: dict[str, Any] | None, *, default: str = "separate") -> str:
    mode = str((cfg or {}).get("output_mode") or "").strip().lower()
    if mode in YIELD_ENHANCEMENT_OUTPUT_MODES:
        return mode
    return default


def wants_yield_enhancement_inline(cfg: dict[str, Any] | None) -> bool:
    if not bool((cfg or {}).get("enabled", False)):
        return False
    return yield_enhancement_output_mode(cfg) in {"inline", "both"}


def wants_yield_enhancement_separate(cfg: dict[str, Any] | None) -> bool:
    if not bool((cfg or {}).get("enabled", False)):
        return False
    return yield_enhancement_output_mode(cfg) in {"separate", "both"}
