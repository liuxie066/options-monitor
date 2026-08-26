from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.trade_contract_identity import canonical_contract_symbol
from domain.domain.strategy_vocab import STRATEGY_COMBO_YIELD, canonical_strategy_id
from src.application.config_profiles import apply_profiles, deep_merge
from src.application.config_sections import resolve_templates_config
from src.application.config_defaults import DEFAULT_CONFIG


SELL_PUT_FAMILY = "sell_put"
SELL_CALL_FAMILY = "sell_call"
RETURN_FIRST_PROFILE = "return_first"
SHORT_VOL_PROFILE = "short_vol"
INSURANCE_UNDERWRITING_PROFILE = "insurance_underwriting"
COMBO_YIELD_STRATEGY = "combo_yield"
COMBO_YIELD_PUT_LEG_ROLES = {
    "funding_put",
    "sell_put",
    "enhancement_put",
    "yield_enhancement_put",
}
COMBO_YIELD_CALL_LEG_ROLES = {
    "participation_call",
    "enhancement_call",
    "long_call",
    "upside_call",
    "convexity_call",
}


@dataclass(frozen=True)
class StrategySemantics:
    strategy_family: str
    strategy_profile: str
    risk_model: str
    scan_strategy_profile: str
    scan_requires_rv: bool
    scan_uses_underwriting_gate: bool
    scan_uses_short_vol_gate: bool
    scan_uses_path_risk: bool
    close_advice_profile: str
    close_requires_rv: bool
    close_uses_short_vol_thesis: bool

    def to_fields(self) -> dict[str, Any]:
        return {
            "strategy_family": self.strategy_family,
            "strategy_profile": self.strategy_profile,
            "risk_model": self.risk_model,
            "scan_strategy_profile": self.scan_strategy_profile,
            "scan_requires_rv": bool(self.scan_requires_rv),
            "scan_uses_underwriting_gate": bool(self.scan_uses_underwriting_gate),
            "scan_uses_short_vol_gate": bool(self.scan_uses_short_vol_gate),
            "close_advice_profile": self.close_advice_profile,
            "close_requires_rv": bool(self.close_requires_rv),
            "close_uses_short_vol_thesis": bool(self.close_uses_short_vol_thesis),
        }


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

    def semantics(self) -> StrategySemantics:
        return strategy_semantics_for_profile(
            family=self.strategy_family,
            profile=self.strategy_profile,
        )


@dataclass(frozen=True)
class ComboYieldPositionRole:
    strategy: str
    leg_role: str
    strategy_group_id: str
    is_combo_yield_short_put: bool
    is_combo_yield_long_call: bool

    @property
    def is_combo_yield(self) -> bool:
        return self.is_combo_yield_short_put or self.is_combo_yield_long_call


def assert_strategy_config_resolved(symbol_cfg: dict[str, Any] | None) -> None:
    """Fail fast when a template-backed symbol reaches strategy planning unexpanded."""

    if not isinstance(symbol_cfg, dict) or not _has_template_refs(symbol_cfg):
        return
    unresolved: list[str] = []
    for family, key in ((SELL_PUT_FAMILY, "sell_put"), (SELL_CALL_FAMILY, "sell_call")):
        side_cfg = symbol_cfg.get(key)
        if not isinstance(side_cfg, dict) or not bool(side_cfg.get("enabled", False)):
            continue
        if not _has_strategy_profile(side_cfg):
            unresolved.append(family)
    if unresolved:
        symbol = str(symbol_cfg.get("symbol") or "").strip() or "<unknown>"
        raise ValueError(
            f"{symbol} strategy config is not resolved for: {', '.join(unresolved)}; "
            "apply templates/profiles before strategy planning"
        )


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
    if profile in {RETURN_FIRST_PROFILE, SHORT_VOL_PROFILE, INSURANCE_UNDERWRITING_PROFILE}:
        return profile
    return RETURN_FIRST_PROFILE


def opening_profile_for_strategy(*, family: str, profile: Any) -> str:
    normalized_family = str(family or "").strip().lower()
    normalized_profile = normalize_strategy_profile(profile)
    if normalized_family in {SELL_PUT_FAMILY, SELL_CALL_FAMILY} and normalized_profile == INSURANCE_UNDERWRITING_PROFILE:
        return INSURANCE_UNDERWRITING_PROFILE
    return normalized_profile


def risk_model_for_profile(profile: str) -> str:
    normalized = normalize_strategy_profile(profile)
    if normalized in {SHORT_VOL_PROFILE, INSURANCE_UNDERWRITING_PROFILE}:
        return SHORT_VOL_PROFILE
    return "return_first_legacy"


def strategy_semantics_for_profile(*, family: str, profile: Any) -> StrategySemantics:
    normalized_family = str(family or "").strip().lower()
    normalized_profile = normalize_strategy_profile(profile)
    scan_profile = opening_profile_for_strategy(family=normalized_family, profile=normalized_profile)
    uses_underwriting = (
        normalized_family in {SELL_PUT_FAMILY, SELL_CALL_FAMILY}
        and scan_profile == INSURANCE_UNDERWRITING_PROFILE
    )
    uses_short_vol_thesis = normalized_profile in {SHORT_VOL_PROFILE, INSURANCE_UNDERWRITING_PROFILE}
    close_profile_token = SHORT_VOL_PROFILE if uses_short_vol_thesis else normalized_profile
    if normalized_family == SELL_CALL_FAMILY:
        close_profile = f"covered_call_{close_profile_token}"
    elif normalized_family == SELL_PUT_FAMILY:
        close_profile = f"sell_put_{close_profile_token}"
    else:
        close_profile = normalized_profile
    return StrategySemantics(
        strategy_family=normalized_family,
        strategy_profile=normalized_profile,
        risk_model=risk_model_for_profile(normalized_profile),
        scan_strategy_profile=scan_profile,
        scan_requires_rv=uses_underwriting,
        scan_uses_underwriting_gate=uses_underwriting,
        scan_uses_short_vol_gate=False,
        scan_uses_path_risk=False,
        close_advice_profile=close_profile,
        close_requires_rv=uses_short_vol_thesis,
        close_uses_short_vol_thesis=uses_short_vol_thesis,
    )


def strategy_semantics_for_side_config(
    *,
    family: str,
    side_cfg: dict[str, Any] | None,
) -> StrategySemantics:
    cfg = side_cfg if isinstance(side_cfg, dict) else {}
    raw_profile = cfg.get("strategy") or cfg.get("strategy_profile")
    profile = str(raw_profile or INSURANCE_UNDERWRITING_PROFILE).strip().lower()
    if profile != INSURANCE_UNDERWRITING_PROFILE:
        raise ValueError(
            f"{family} opening strategy only supports {INSURANCE_UNDERWRITING_PROFILE}; "
            f"got {profile or '<empty>'}"
        )
    return strategy_semantics_for_profile(
        family=family,
        profile=INSURANCE_UNDERWRITING_PROFILE,
    )


def resolve_combo_yield_position_role(position: dict[str, Any]) -> ComboYieldPositionRole:
    option_type = str(position.get("option_type") or "").strip().lower()
    side = str(position.get("side") or position.get("position_side") or "").strip().lower()
    strategy = _first_position_strategy_field(position, "strategy").lower()
    leg_role = _first_position_strategy_field(position, "leg_role").lower()
    group_id = _first_position_strategy_field(position, "strategy_group_id")
    has_yield_marker = _is_combo_yield_strategy_token(strategy) or bool(
        _legacy_combo_yield_mode(position)
    )
    is_short_put = (
        option_type == "put"
        and side in {"", "short"}
        and (
            has_yield_marker
            or leg_role == "funding_put"
            or (leg_role in COMBO_YIELD_PUT_LEG_ROLES and bool(group_id))
        )
    )
    is_long_call = (
        option_type == "call"
        and side == "long"
        and (
            has_yield_marker
            or leg_role in COMBO_YIELD_CALL_LEG_ROLES
        )
    )
    return ComboYieldPositionRole(
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=group_id,
        is_combo_yield_short_put=is_short_put,
        is_combo_yield_long_call=is_long_call,
    )


def resolve_position_strategy_semantics(
    *,
    position: dict[str, Any],
    config: dict[str, Any] | None,
) -> tuple[StrategyResolution, StrategySemantics]:
    resolution = resolve_position_strategy(position=position, config=config)
    return resolution, resolution.semantics()


def resolve_position_strategy(
    *,
    position: dict[str, Any],
    config: dict[str, Any] | None,
) -> StrategyResolution:
    family = strategy_family_for_position(position) or ""
    legacy_combo_profile = _legacy_combo_yield_strategy_profile(position, family=family)
    if legacy_combo_profile:
        profile = normalize_strategy_profile(legacy_combo_profile)
        return StrategyResolution(
            strategy_family=family,
            strategy_profile=profile,
            strategy_source="position_legacy_combo_yield_mode",
            config_path=None,
            risk_model=risk_model_for_profile(profile),
        )

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

    if family == SELL_PUT_FAMILY and _is_combo_yield_position(position):
        return StrategyResolution(
            strategy_family=family,
            strategy_profile=RETURN_FIRST_PROFILE,
            strategy_source="position_combo_yield_identity",
            config_path=None,
            risk_model=risk_model_for_profile(RETURN_FIRST_PROFILE),
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
            if _is_combo_yield_strategy_token(profile):
                continue
            return str(profile)

    raw_family = str(position.get("strategy_family") or "").strip()
    if raw_family and raw_family != family:
        return None
    profile = position.get("strategy_profile") or position.get("strategy")
    if _is_combo_yield_strategy_token(profile):
        return None
    return str(profile) if profile else None


def _is_combo_yield_strategy_token(value: Any) -> bool:
    return canonical_strategy_id(str(value or "")) == STRATEGY_COMBO_YIELD


def _legacy_combo_yield_strategy_profile(position: dict[str, Any], *, family: str) -> str | None:
    if family != SELL_PUT_FAMILY:
        return None
    legacy_mode = _legacy_combo_yield_mode(position)
    if legacy_mode == "vol_convexity_enhancement":
        return SHORT_VOL_PROFILE
    if legacy_mode == "income_upside_enhancement":
        return RETURN_FIRST_PROFILE
    return None


def _legacy_combo_yield_mode(position: dict[str, Any]) -> str:
    """Read retired ledger facts; active code must never emit this field."""

    for source in (position, *_position_strategy_snapshots(position)):
        if not isinstance(source, dict):
            continue
        mode = str(source.get("yield_enhancement_mode") or "").strip().lower()
        if mode:
            return mode
    return ""


def _position_strategy_snapshots(position: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for key in ("strategy_snapshot", "open_strategy_snapshot", "strategy_resolution"):
        raw = position.get(key)
        if isinstance(raw, dict):
            out.append(raw)
    return tuple(out)


def _is_combo_yield_position(position: dict[str, Any]) -> bool:
    return resolve_combo_yield_position_role(position).is_combo_yield_short_put


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


def _has_template_refs(symbol_cfg: dict[str, Any]) -> bool:
    raw = symbol_cfg.get("use")
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, (list, tuple, set)):
        return any(str(item or "").strip() for item in raw)
    return False


def _has_strategy_profile(side_cfg: dict[str, Any]) -> bool:
    return side_cfg.get("strategy") is not None or side_cfg.get("strategy_profile") is not None


def _first_position_strategy_field(position: dict[str, Any], key: str) -> str:
    for source in (position, *_position_strategy_snapshots(position)):
        if not isinstance(source, dict):
            continue
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""
