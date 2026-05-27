from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.config_profiles import apply_profiles, deep_merge
from src.application.config_sections import resolve_templates_config
from src.application.config_defaults import DEFAULT_CONFIG


SELL_PUT_FAMILY = "sell_put"
SELL_CALL_FAMILY = "sell_call"
RETURN_FIRST_PROFILE = "return_first"
SHORT_VOL_PROFILE = "short_vol"


@dataclass(frozen=True)
class StrategyResolution:
    strategy_family: str
    strategy_profile: str
    strategy_source: str
    config_path: str | None
    risk_model: str

    def to_fields(self) -> dict[str, Any]:
        return {
            "strategy_family": self.strategy_family,
            "strategy_profile": self.strategy_profile,
            "strategy_source": self.strategy_source,
            "strategy_config_path": self.config_path,
            "risk_model": self.risk_model,
        }


def strategy_family_for_position(position: dict[str, Any]) -> str | None:
    option_type = str(position.get("option_type") or "").strip().lower()
    side = str(position.get("side") or "").strip().lower()
    if side != "short":
        return None
    if option_type == "put":
        return SELL_PUT_FAMILY
    if option_type == "call":
        return SELL_CALL_FAMILY
    return None


def normalize_strategy_profile(value: Any) -> str:
    profile = str(value or "").strip().lower()
    if profile in {"", "legacy", "yield_first", "return"}:
        return RETURN_FIRST_PROFILE
    if profile in {RETURN_FIRST_PROFILE, SHORT_VOL_PROFILE}:
        return profile
    return RETURN_FIRST_PROFILE


def risk_model_for_profile(profile: str) -> str:
    normalized = normalize_strategy_profile(profile)
    if normalized == SHORT_VOL_PROFILE:
        return SHORT_VOL_PROFILE
    return "return_first_legacy"


def resolve_position_strategy(
    *,
    position: dict[str, Any],
    config: dict[str, Any] | None,
) -> StrategyResolution:
    family = strategy_family_for_position(position) or ""
    snapshot_profile = _snapshot_strategy_profile(position, family=family)
    if snapshot_profile:
        profile = normalize_strategy_profile(snapshot_profile)
        return StrategyResolution(
            strategy_family=family,
            strategy_profile=profile,
            strategy_source="position_snapshot",
            config_path=None,
            risk_model=risk_model_for_profile(profile),
        )

    profile, path = _current_config_strategy_profile(position, config=config, family=family)
    if profile:
        normalized = normalize_strategy_profile(profile)
        return StrategyResolution(
            strategy_family=family,
            strategy_profile=normalized,
            strategy_source="current_config",
            config_path=path,
            risk_model=risk_model_for_profile(normalized),
        )

    fallback_profile, fallback_path = _template_default_strategy_profile(config=config, family=family)
    normalized = normalize_strategy_profile(fallback_profile)
    return StrategyResolution(
        strategy_family=family,
        strategy_profile=normalized,
        strategy_source="template_default",
        config_path=fallback_path,
        risk_model=risk_model_for_profile(normalized),
    )


def strategy_side_config_for_resolution(
    *,
    resolution: StrategyResolution,
    position: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    family = resolution.strategy_family
    if family not in {SELL_PUT_FAMILY, SELL_CALL_FAMILY}:
        return {}
    template_cfg = _default_template_side_config(config=config, family=family)
    symbol_cfg = _resolved_symbol_config(position, config=config)
    side_cfg = symbol_cfg.get(family) if isinstance(symbol_cfg, dict) else None
    if isinstance(side_cfg, dict) and side_cfg:
        return deep_merge(template_cfg, side_cfg)
    return dict(template_cfg) if isinstance(template_cfg, dict) else {}


def _snapshot_strategy_profile(position: dict[str, Any], *, family: str) -> str | None:
    for key in ("strategy_snapshot", "open_strategy_snapshot", "strategy_resolution"):
        raw = position.get(key)
        if not isinstance(raw, dict):
            continue
        raw_family = str(raw.get("strategy_family") or raw.get("family") or "").strip()
        if raw_family and raw_family != family:
            continue
        profile = raw.get("strategy_profile") or raw.get("profile") or raw.get("strategy")
        if profile:
            return str(profile)

    raw_family = str(position.get("strategy_family") or "").strip()
    if raw_family and raw_family != family:
        return None
    profile = position.get("strategy_profile") or position.get("strategy")
    return str(profile) if profile else None


def _current_config_strategy_profile(
    position: dict[str, Any],
    *,
    config: dict[str, Any] | None,
    family: str,
) -> tuple[str | None, str | None]:
    symbol_cfg = _resolved_symbol_config(position, config=config)
    if not isinstance(symbol_cfg, dict) or not symbol_cfg:
        return None, None
    side_cfg = symbol_cfg.get(family)
    if not isinstance(side_cfg, dict):
        return None, None
    profile = side_cfg.get("strategy") or side_cfg.get("strategy_profile")
    if profile is None:
        return None, None
    symbol = _norm_symbol(position.get("symbol")) or str(position.get("symbol") or "").strip()
    return str(profile), f"symbols.{symbol}.{family}.strategy"


def _template_default_strategy_profile(
    *,
    config: dict[str, Any] | None,
    family: str,
) -> tuple[str, str | None]:
    side_cfg = _default_template_side_config(config=config, family=family)
    if isinstance(side_cfg, dict):
        profile = side_cfg.get("strategy") or side_cfg.get("strategy_profile")
        if profile is not None:
            template = "put_base" if family == SELL_PUT_FAMILY else "call_base"
            return str(profile), f"templates.{template}.{family}.strategy"
    if not _has_runtime_strategy_context(config):
        return RETURN_FIRST_PROFILE, None
    return RETURN_FIRST_PROFILE, None


def _resolved_symbol_config(position: dict[str, Any], *, config: dict[str, Any] | None) -> dict[str, Any]:
    raw = _symbol_config(position, config=config)
    if not raw:
        return {}
    profiles = _templates_with_defaults(config)
    if profiles:
        return apply_profiles(raw, profiles)
    return raw


def _symbol_config(position: dict[str, Any], *, config: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _norm_symbol(position.get("symbol"))
    items = config.get("symbols") if isinstance(config, dict) else []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if _norm_symbol(item.get("symbol")) == symbol:
            return dict(item)
    return {}


def _default_template_side_config(*, config: dict[str, Any] | None, family: str) -> dict[str, Any]:
    if not _has_runtime_strategy_context(config):
        return {}
    template = "put_base" if family == SELL_PUT_FAMILY else "call_base"
    return _side_from_templates(_templates_with_defaults(config), template=template, family=family)


def _templates_with_defaults(config: dict[str, Any] | None) -> dict[str, Any]:
    if not _has_runtime_strategy_context(config):
        return {}
    merged: dict[str, Any] = {}
    system_defaults = DEFAULT_CONFIG.get("defaults", {})
    system_templates = system_defaults.get("templates") if isinstance(system_defaults, dict) else None
    if isinstance(system_templates, dict):
        merged = deep_merge(merged, system_templates)

    defaults = config.get("defaults") if isinstance(config, dict) else None
    default_templates = defaults.get("templates") if isinstance(defaults, dict) else None
    if isinstance(default_templates, dict):
        merged = deep_merge(merged, default_templates)

    runtime_templates = resolve_templates_config(config)
    if isinstance(runtime_templates, dict):
        merged = deep_merge(merged, runtime_templates)
    return merged


def _has_runtime_strategy_context(config: dict[str, Any] | None) -> bool:
    if not isinstance(config, dict):
        return False
    return any(key in config for key in ("symbols", "templates", "defaults"))


def _side_from_templates(templates: Any, *, template: str, family: str) -> dict[str, Any]:
    if not isinstance(templates, dict):
        return {}
    profile = templates.get(template)
    if not isinstance(profile, dict):
        return {}
    side_cfg = profile.get(family)
    return dict(side_cfg) if isinstance(side_cfg, dict) else {}


def _norm_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)
