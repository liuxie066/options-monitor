from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from src.application.strategy_policy import (
    RETURN_FIRST_PROFILE,
    SELL_PUT_FAMILY,
    YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
    YIELD_ENHANCEMENT_VOL_CONVEXITY_MODE,
    strategy_semantics_for_profile,
)


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
YIELD_ENHANCEMENT_LEGACY_CALL_OTM_FIELDS: tuple[str, ...] = (
    "min_otm_pct",
    "max_otm_pct",
)
YIELD_ENHANCEMENT_LEGACY_PUT_OTM_FIELDS: tuple[str, ...] = (
    "min_put_otm_pct",
)
YIELD_ENHANCEMENT_LEGACY_SCENARIO_FIELDS: tuple[str, ...] = (
    "min_scenario_score",
    "min_annualized_scenario_score",
    "scenario_move_factors",
    "scenario_weights",
    "min_upside_lift_to_call_cost",
    "min_upside_lift_to_put_credit",
)
YIELD_ENHANCEMENT_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "objective": "premium_funded_long_call",
    "output_mode": "separate",
    "funding_mode": "credit_or_even",
    "min_combo_net_credit": 0.0,
    "min_net_credit_annualized": 0.08,
    "max_call_cost_to_put_credit": 1.0,
    "min_open_interest": 100,
    "min_volume": 5,
    "max_spread_ratio": 0.35,
    "max_combo_spread_ratio": 0.50,
    "call": {
        "min_delta": 0.10,
        "max_delta": 0.45,
    },
}
YIELD_ENHANCEMENT_MARKET_DEFAULT_OVERRIDES: dict[str, dict[str, Any]] = {
    "hk": {
        "min_open_interest": 50,
        "min_volume": 0,
    },
}
YIELD_ENHANCEMENT_DERIVED_POLICY_DEFAULTS: dict[str, dict[str, Any]] = {
    YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE: {
        "funding_mode": "credit_or_even",
        "min_combo_net_credit": 0.0,
        "max_call_cost_to_put_credit": 0.20,
        "min_net_credit_retention": 0.75,
        "call": {
            "min_delta": 0.05,
            "max_delta": 0.20,
        },
    },
    YIELD_ENHANCEMENT_VOL_CONVEXITY_MODE: {
        "funding_mode": "credit_or_even",
        "min_combo_net_credit": 0.0,
        "max_call_cost_to_put_credit": 0.35,
        "min_net_credit_retention": None,
        "call": {
            "min_delta": 0.15,
            "max_delta": 0.30,
        },
    },
}
COMBO_YIELD_CONFIG_KEY = "combo_yield"
YIELD_ENHANCEMENT_LEGACY_CONFIG_KEY = "yield_enhancement"


@dataclass(frozen=True)
class YieldEnhancementPolicy:
    enabled: bool
    mode: str
    derived_from_sell_put_strategy: str
    requires_realized_volatility: bool
    uses_short_vol_gate: bool
    config: dict[str, Any]
    explicit_fields: tuple[str, ...]

    def to_config(self) -> dict[str, Any]:
        cfg = deepcopy(self.config)
        cfg["enabled"] = bool(self.enabled)
        cfg["yield_enhancement_mode"] = self.mode
        cfg["derived_from_sell_put_strategy"] = self.derived_from_sell_put_strategy
        cfg["yield_enhancement_requires_rv"] = bool(self.requires_realized_volatility)
        cfg["yield_enhancement_uses_short_vol_gate"] = bool(self.uses_short_vol_gate)
        cfg["_explicit_fields"] = tuple(self.explicit_fields)
        return cfg

    def to_fields(self) -> dict[str, Any]:
        return {
            "yield_enhancement_mode": self.mode,
            "derived_from_sell_put_strategy": self.derived_from_sell_put_strategy,
            "yield_enhancement_requires_rv": bool(self.requires_realized_volatility),
            "yield_enhancement_uses_short_vol_gate": bool(self.uses_short_vol_gate),
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
            if key == "call" and isinstance(cfg.get("call"), dict):
                nested = cfg.get("_explicit_call_fields")
                if isinstance(nested, (list, tuple, set)):
                    call_cfg = _as_dict(cfg.get("call"))
                    out["call"] = {
                        str(child): deepcopy(call_cfg[str(child)])
                        for child in nested
                        if str(child) in call_cfg
                    }
                    continue
            out[key] = deepcopy(cfg[key])
    return out


def _normalize_sell_put_strategy(sell_put_cfg: dict[str, Any] | None) -> str:
    cfg = _as_dict(sell_put_cfg)
    return strategy_semantics_for_profile(
        family=SELL_PUT_FAMILY,
        profile=cfg.get("strategy") or cfg.get("strategy_profile"),
    ).strategy_profile


def yield_enhancement_mode_for_sell_put_strategy(strategy: Any) -> str:
    return str(
        strategy_semantics_for_profile(
            family=SELL_PUT_FAMILY,
            profile=strategy,
        ).yield_enhancement_mode
        or YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE
    )


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


def derive_yield_enhancement_policy(
    yield_enhancement_cfg: dict[str, Any] | None,
    sell_put_cfg: dict[str, Any] | None,
    *,
    market: str | None = None,
) -> YieldEnhancementPolicy:
    raw_cfg = _as_dict(yield_enhancement_cfg)
    explicit_fields = _explicit_fields(raw_cfg)
    enabled = bool(raw_cfg.get("enabled", False))
    combo_strategy = RETURN_FIRST_PROFILE
    mode = YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE

    base = yield_enhancement_defaults_for_market(market)
    derived_defaults = YIELD_ENHANCEMENT_DERIVED_POLICY_DEFAULTS.get(mode) or {}
    cfg = _deep_merge_dict(base, derived_defaults)
    cfg = _deep_merge_dict(cfg, _explicit_overrides(raw_cfg, explicit_fields))
    cfg["enabled"] = bool(enabled)
    cfg["yield_enhancement_mode"] = mode
    cfg["derived_from_sell_put_strategy"] = combo_strategy
    cfg["yield_enhancement_requires_rv"] = False
    cfg["yield_enhancement_uses_short_vol_gate"] = False
    output_mode = str(cfg.get("output_mode") or "").strip().lower()
    cfg["output_mode"] = output_mode if output_mode in YIELD_ENHANCEMENT_OUTPUT_MODES else "separate"
    return YieldEnhancementPolicy(
        enabled=bool(enabled),
        mode=mode,
        derived_from_sell_put_strategy=combo_strategy,
        requires_realized_volatility=False,
        uses_short_vol_gate=False,
        config=cfg,
        explicit_fields=explicit_fields,
    )


def resolve_yield_enhancement_cfg(symbol_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(symbol_cfg or {})
    raw_top_level = (
        cfg.get(COMBO_YIELD_CONFIG_KEY)
        if isinstance(cfg.get(COMBO_YIELD_CONFIG_KEY), dict)
        else cfg.get(YIELD_ENHANCEMENT_LEGACY_CONFIG_KEY)
    )
    top_level = _as_dict(raw_top_level)

    has_top_level = isinstance(raw_top_level, dict)
    if not has_top_level:
        return {}

    existing_explicit_fields = top_level.get("_explicit_fields")
    if isinstance(existing_explicit_fields, (list, tuple, set)):
        explicit_fields = tuple(str(key) for key in existing_explicit_fields)
    else:
        explicit_fields = tuple(str(key) for key in top_level.keys() if not str(key).startswith("_"))
    existing_call_explicit_fields = top_level.get("_explicit_call_fields")
    if isinstance(existing_call_explicit_fields, (list, tuple, set)):
        explicit_call_fields = tuple(str(key) for key in existing_call_explicit_fields)
    else:
        raw_call_cfg = top_level.get("call")
        explicit_call_fields = (
            tuple(str(key) for key in raw_call_cfg.keys() if not str(key).startswith("_"))
            if isinstance(raw_call_cfg, dict)
            else tuple()
        )
    top_level = apply_yield_enhancement_defaults(top_level)
    top_level["_explicit_fields"] = explicit_fields
    if explicit_call_fields:
        top_level["_explicit_call_fields"] = explicit_call_fields
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
