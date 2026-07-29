"""Config profile helpers.

Extracted from run_pipeline.py (Stage 3): profile/template expansion via `use`.

Rules:
- Item overrides profile defaults.
- Only merges dict->dict recursively.
"""

from __future__ import annotations


class ConfigProfileError(ValueError):
    """Raised when a profile reference cannot be resolved exactly."""


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge two dicts. override wins."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_profiles(item: dict, profiles: dict | None) -> dict:
    if not isinstance(item, dict):
        return item

    use = item.get('use')
    if not use:
        return item

    use_list: list[str]
    if isinstance(use, str):
        name = use.strip()
        if not name:
            raise ConfigProfileError("profile use must not be empty")
        use_list = [name]
    elif isinstance(use, list):
        if not use:
            raise ConfigProfileError("profile use list must not be empty")
        if any(not isinstance(value, str) or not value.strip() for value in use):
            raise ConfigProfileError("profile use must contain only non-empty strings")
        use_list = [value.strip() for value in use]
    else:
        raise ConfigProfileError("profile use must be a string or list of strings")
    if len(set(use_list)) != len(use_list):
        raise ConfigProfileError("profile use must not contain duplicate references")
    if not isinstance(profiles, dict):
        raise ConfigProfileError("profile use requires a templates object")

    merged: dict = {}
    for name in use_list:
        p = profiles.get(name)
        if not isinstance(p, dict):
            raise ConfigProfileError(f"unknown profile reference: {name}")
        merged = deep_merge(merged, p)

    # Item overrides profile defaults
    item2 = dict(item)
    item2.pop('use', None)
    merged = deep_merge(merged, item2)
    return merged


__all__ = ["ConfigProfileError", "apply_profiles", "deep_merge"]
