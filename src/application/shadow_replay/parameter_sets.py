from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_PROFILES = {"short_vol"}
ALLOWED_PARAMETERS = {
    "short_vol": {
        "min_iv_rv_ratio",
        "min_iv_minus_rv",
        "min_abs_delta",
        "max_abs_delta",
        "min_dte",
        "max_dte",
        "min_annualized_return",
    }
}


@dataclass(frozen=True)
class ParameterVariant:
    name: str
    profiles: dict[str, dict[str, float]]

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "profiles": self.profiles}


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
        profiles = _variant_profiles(raw, variant_name=name)
        variants.append(ParameterVariant(name=name, profiles=profiles))
    return ParameterSet(baseline=baseline, variants=tuple(variants))


def _variant_profiles(raw: dict[str, Any], *, variant_name: str) -> dict[str, dict[str, float]]:
    reserved = {"name", "description"}
    profile_names = [key for key in raw if key not in reserved]
    if not profile_names:
        raise ValueError(f"variant {variant_name} requires at least one profile block")
    unknown_profiles = sorted(set(profile_names) - ALLOWED_PROFILES)
    if unknown_profiles:
        raise ValueError(f"variant {variant_name} has unsupported profile blocks: {', '.join(unknown_profiles)}")

    profiles: dict[str, dict[str, float]] = {}
    for profile in profile_names:
        block = raw.get(profile)
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
    if "min_abs_delta" in params and "max_abs_delta" in params and params["min_abs_delta"] > params["max_abs_delta"]:
        raise ValueError(f"variant {variant_name}.{profile} min_abs_delta cannot exceed max_abs_delta")
    if "min_dte" in params and "max_dte" in params and params["min_dte"] > params["max_dte"]:
        raise ValueError(f"variant {variant_name}.{profile} min_dte cannot exceed max_dte")
