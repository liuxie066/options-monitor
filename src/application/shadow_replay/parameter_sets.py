from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CURRENT_UNDERWRITING_PROFILE = "insurance_underwriting"
LEGACY_SHORT_VOL_PROFILE = "short_vol"

ALLOWED_PROFILES = {CURRENT_UNDERWRITING_PROFILE}
EXPERIMENT_ONLY_PARAMETERS = {
    "min_iv_rv_percentile",
    "min_iv_rv_history_samples",
}
ALLOWED_PARAMETERS = {
    CURRENT_UNDERWRITING_PROFILE: {
        "min_iv_rv_ratio",
        "min_iv_minus_rv",
        "min_iv_rv_percentile",
        "min_iv_rv_history_samples",
        "min_dte",
        "max_dte",
        "min_annualized_return",
    }
}


@dataclass(frozen=True)
class ParameterVariant:
    name: str
    profiles: dict[str, dict[str, float]]
    strategy_family: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "profiles": self.profiles}
        if self.strategy_family:
            payload["strategy_family"] = self.strategy_family
        return payload


@dataclass(frozen=True)
class ParameterSet:
    baseline: str
    variants: tuple[ParameterVariant, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "variants": [variant.to_payload() for variant in self.variants],
            "allowed_profiles": sorted(ALLOWED_PROFILES),
            "allowed_parameters": {profile: sorted(keys) for profile, keys in sorted(ALLOWED_PARAMETERS.items())},
        }


def load_parameter_set(params: str | Path | dict[str, Any] | ParameterSet) -> ParameterSet:
    if isinstance(params, ParameterSet):
        return params
    if isinstance(params, dict):
        return parse_parameter_set(params)
    path = Path(params).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid parameter JSON: {exc}") from exc
    return parse_parameter_set(payload)


def parse_parameter_set(payload: dict[str, Any]) -> ParameterSet:
    if not isinstance(payload, dict):
        raise ValueError("parameter set must be a JSON object")
    baseline = str(payload.get("baseline") or "production").strip() or "production"
    variants_raw = payload.get("variants")
    if not isinstance(variants_raw, list) or not variants_raw:
        raise ValueError("parameter set requires a non-empty variants list")

    variants: list[ParameterVariant] = []
    names: set[str] = set()
    for idx, raw in enumerate(variants_raw, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"variant #{idx} must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"variant #{idx} requires name")
        if name in names:
            raise ValueError(f"duplicate variant name: {name}")
        names.add(name)
        strategy_family = str(raw.get("strategy_family") or "").strip().lower() or None
        if strategy_family not in {None, "sell_put", "covered_call"}:
            raise ValueError(f"variant {name} has unsupported strategy_family: {strategy_family}")
        profiles = _variant_profiles(raw, variant_name=name)
        variants.append(ParameterVariant(name=name, profiles=profiles, strategy_family=strategy_family))
    return ParameterSet(baseline=baseline, variants=tuple(variants))


def _variant_profiles(raw: dict[str, Any], *, variant_name: str) -> dict[str, dict[str, float]]:
    reserved = {"name", "description", "strategy_family"}
    profile_names = [_normalize_profile_key(key) for key in raw if key not in reserved]
    if not profile_names:
        raise ValueError(f"variant {variant_name} requires at least one profile block")
    unknown_profiles = sorted(set(profile_names) - ALLOWED_PROFILES)
    if unknown_profiles:
        raise ValueError(f"variant {variant_name} has unsupported profile blocks: {', '.join(unknown_profiles)}")

    profiles: dict[str, dict[str, float]] = {}
    for raw_profile in (key for key in raw if key not in reserved):
        profile = _normalize_profile_key(raw_profile)
        block = raw.get(raw_profile)
        if not isinstance(block, dict) or not block:
            raise ValueError(f"variant {variant_name}.{profile} must be a non-empty object")
        allowed = ALLOWED_PARAMETERS[profile]
        unknown = sorted(set(block) - allowed)
        if unknown:
            raise ValueError(
                f"variant {variant_name}.{profile} has non-tunable parameters: {', '.join(unknown)}"
            )
        params: dict[str, float] = {}
        for key, value in block.items():
            params[key] = _number(value, label=f"{variant_name}.{profile}.{key}")
        _validate_range(params, variant_name=variant_name, profile=profile)
        profiles[profile] = params
    return profiles


def _normalize_profile_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == CURRENT_UNDERWRITING_PROFILE:
        return CURRENT_UNDERWRITING_PROFILE
    return text


def _number(value: Any, *, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        parsed = float(value)
    except Exception as exc:
        raise ValueError(f"{label} must be a number") from exc
    if parsed != parsed:
        raise ValueError(f"{label} must be a finite number")
    return parsed


def _validate_range(params: dict[str, float], *, variant_name: str, profile: str) -> None:
    percentile = params.get("min_iv_rv_percentile")
    if percentile is not None and not 0.0 <= percentile <= 1.0:
        raise ValueError(f"variant {variant_name}.{profile} min_iv_rv_percentile must be between 0 and 1")
    if percentile is not None and "min_iv_rv_ratio" not in params:
        raise ValueError(
            f"variant {variant_name}.{profile} min_iv_rv_percentile requires min_iv_rv_ratio absolute floor"
        )
    history_samples = params.get("min_iv_rv_history_samples")
    if history_samples is not None and (history_samples < 1 or not float(history_samples).is_integer()):
        raise ValueError(f"variant {variant_name}.{profile} min_iv_rv_history_samples must be a positive integer")
    if "min_dte" in params and "max_dte" in params and params["min_dte"] > params["max_dte"]:
        raise ValueError(f"variant {variant_name}.{profile} min_dte cannot exceed max_dte")
