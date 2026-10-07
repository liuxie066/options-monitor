from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from domain.domain.engine.combo_yield import COMBO_LONG_MAX_DELTA, COMBO_LONG_MIN_DELTA

from src.application.strategy_policy import (
    INSURANCE_UNDERWRITING_PROFILE,
)


COMBO_YIELD_OBJECTIVES: set[str] = {"premium_funded_long_call"}
COMBO_YIELD_STRUCTURE_MODES: set[str] = {"same_expiry_pair"}
COMBO_YIELD_VARIANTS: set[str] = {"sp_lc", "cc_lp"}
COMBO_YIELD_LEGACY_OPTIMIZER_FIELDS: tuple[str, ...] = (
    "optimizer_enabled",
    "max_downside_worsen_pct",
    "min_scenario_score_lift",
    "min_annualized_scenario_score_lift",
    "min_lift_to_downside_ratio",
    "max_combo_spread_worsen_ratio",
)
COMBO_YIELD_LEGACY_CALL_BOUND_FIELDS: tuple[str, ...] = (
    "min_call_otm_pct",
    "max_call_otm_pct",
)
COMBO_YIELD_LEGACY_CALL_OTM_FIELDS: tuple[str, ...] = (
    "min_otm_pct",
    "max_otm_pct",
)
COMBO_YIELD_LEGACY_PUT_OTM_FIELDS: tuple[str, ...] = (
    "min_put_otm_pct",
)
COMBO_YIELD_LEGACY_SCENARIO_FIELDS: tuple[str, ...] = (
    "min_scenario_score",
    "min_annualized_scenario_score",
    "scenario_move_factors",
    "scenario_weights",
    "min_upside_lift_to_call_cost",
    "min_upside_lift_to_put_credit",
)
COMBO_YIELD_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "structure_mode": "same_expiry_pair",
    "objective": "premium_funded_long_call",
    "variant": "sp_lc",
    "min_combo_net_credit": None,
    "min_net_credit_annualized": 0.08,
    "min_net_credit_retention": 0.60,
    "min_open_interest": 100,
    "min_volume": 5,
    "max_spread_ratio": 0.35,
    "max_combo_spread_ratio": 0.50,
    "call": {
        "min_delta": COMBO_LONG_MIN_DELTA,
        "max_delta": COMBO_LONG_MAX_DELTA,
    },
    "put": {
        "min_delta": COMBO_LONG_MIN_DELTA,
        "max_delta": COMBO_LONG_MAX_DELTA,
    },
}
COMBO_YIELD_MARKET_DEFAULT_OVERRIDES: dict[str, dict[str, Any]] = {
    "hk": {
        "min_open_interest": 50,
        "min_volume": 0,
    },
}
COMBO_YIELD_CONFIG_KEY = "combo_yield"


@dataclass(frozen=True)
class ComboYieldPolicy:
    enabled: bool
    derived_from_sell_put_strategy: str
    requires_realized_volatility: bool
    config: dict[str, Any]
    explicit_fields: tuple[str, ...]

    def to_config(self) -> dict[str, Any]:
        cfg = deepcopy(self.config)
        cfg["enabled"] = bool(self.enabled)
        cfg["derived_from_sell_put_strategy"] = self.derived_from_sell_put_strategy
        cfg["requires_realized_volatility"] = bool(self.requires_realized_volatility)
        cfg["_explicit_fields"] = tuple(self.explicit_fields)
        return cfg

    def to_fields(self) -> dict[str, Any]:
        return {
            "derived_from_sell_put_strategy": self.derived_from_sell_put_strategy,
            "requires_realized_volatility": bool(self.requires_realized_volatility),
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


def _explicit_fields(cfg: dict[str, Any]) -> tuple[str, ...]:
    raw = cfg.get("_explicit_fields")
    if isinstance(raw, (list, tuple, set)):
        return tuple(str(key) for key in raw if str(key).strip())
    return tuple(str(key) for key in cfg.keys() if not str(key).startswith("_"))


def _explicit_overrides(cfg: dict[str, Any], explicit_fields: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in explicit_fields:
        if key in {"strategy", "strategy_profile"} or key.startswith("_"):
            continue
        if key in cfg:
            if key in {"call", "put"} and isinstance(cfg.get(key), dict):
                nested = cfg.get(f"_explicit_{key}_fields")
                if isinstance(nested, (list, tuple, set)):
                    leg_cfg = _as_dict(cfg.get(key))
                    out[key] = {
                        str(child): deepcopy(leg_cfg[str(child)])
                        for child in nested
                        if str(child) in leg_cfg
                    }
                    continue
            out[key] = deepcopy(cfg[key])
    return out


def combo_yield_defaults_for_market(market: str | None = None) -> dict[str, Any]:
    market_key = str(market or "").strip().lower()
    defaults = deepcopy(COMBO_YIELD_DEFAULTS)
    override = COMBO_YIELD_MARKET_DEFAULT_OVERRIDES.get(market_key)
    if override:
        defaults = _deep_merge_dict(defaults, override)
    return defaults


def apply_combo_yield_defaults(cfg: dict[str, Any] | None, *, market: str | None = None) -> dict[str, Any]:
    defaults = combo_yield_defaults_for_market(market)
    raw_cfg = _as_dict(cfg)
    return _deep_merge_dict(defaults, raw_cfg)


def derive_combo_yield_policy(
    combo_yield_cfg: dict[str, Any] | None,
    *,
    market: str | None = None,
) -> ComboYieldPolicy:
    raw_cfg = _as_dict(combo_yield_cfg)
    explicit_fields = _explicit_fields(raw_cfg)
    enabled = bool(raw_cfg.get("enabled", False))
    combo_strategy = INSURANCE_UNDERWRITING_PROFILE

    cfg = combo_yield_defaults_for_market(market)
    structure_mode = str(raw_cfg.get("structure_mode") or cfg.get("structure_mode") or "same_expiry_pair").strip().lower()
    cfg = _deep_merge_dict(cfg, _explicit_overrides(raw_cfg, explicit_fields))
    cfg["structure_mode"] = structure_mode
    cfg["enabled"] = bool(enabled)
    variant = str(raw_cfg.get("variant") or cfg.get("variant") or "sp_lc").strip().lower()
    cfg["variant"] = variant
    cfg["derived_from_sell_put_strategy"] = combo_strategy
    # Combo Yield owns its Funding Put scan even when the standalone CSP
    # step is disabled. That leg still runs the canonical insurance
    # underwriting gate, so realized volatility is required by the strategy.
    cfg["requires_realized_volatility"] = True
    return ComboYieldPolicy(
        enabled=bool(enabled),
        derived_from_sell_put_strategy=combo_strategy,
        requires_realized_volatility=True,
        config=cfg,
        explicit_fields=explicit_fields,
    )


def resolve_combo_yield_cfg(symbol_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(symbol_cfg or {})
    raw_top_level = cfg.get(COMBO_YIELD_CONFIG_KEY)
    top_level = _as_dict(raw_top_level)

    has_top_level = isinstance(raw_top_level, dict)
    if not has_top_level:
        return {}

    existing_explicit_fields = top_level.get("_explicit_fields")
    if isinstance(existing_explicit_fields, (list, tuple, set)):
        explicit_fields = tuple(str(key) for key in existing_explicit_fields)
    else:
        explicit_fields = tuple(str(key) for key in top_level.keys() if not str(key).startswith("_"))
    explicit_leg_fields = {}
    for leg in ("call", "put"):
        existing = top_level.get(f"_explicit_{leg}_fields")
        raw_leg = top_level.get(leg)
        explicit_leg_fields[leg] = (
            tuple(str(key) for key in existing)
            if isinstance(existing, (list, tuple, set))
            else tuple(str(key) for key in raw_leg if not str(key).startswith("_"))
            if isinstance(raw_leg, dict)
            else tuple()
        )
    top_level = apply_combo_yield_defaults(top_level)
    top_level["_explicit_fields"] = explicit_fields
    for leg, fields in explicit_leg_fields.items():
        if fields:
            top_level[f"_explicit_{leg}_fields"] = fields
    if "enabled" in top_level:
        top_level["enabled"] = bool(top_level.get("enabled"))

    return top_level
