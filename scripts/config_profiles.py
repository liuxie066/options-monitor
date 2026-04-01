"""Config profile helpers.

Extracted from run_pipeline.py (Stage 3): profile/template expansion via `use`.

Rules:
- Item overrides profile defaults.
- Only merges dict->dict recursively.
"""

from __future__ import annotations


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
    if not profiles or not isinstance(profiles, dict):
        return item

    use = item.get('use')
    if not use:
        return item

    use_list: list[str] = []
    if isinstance(use, str):
        use_list = [use]
    elif isinstance(use, list):
        use_list = [x for x in use if isinstance(x, str)]

    merged: dict = {}
    for name in use_list:
        p = profiles.get(name)
        if isinstance(p, dict):
            merged = deep_merge(merged, p)

    # Item overrides profile defaults
    item2 = dict(item)
    item2.pop('use', None)
    merged = deep_merge(merged, item2)
    return merged
