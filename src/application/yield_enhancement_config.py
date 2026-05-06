from __future__ import annotations

from typing import Any


YIELD_ENHANCEMENT_OUTPUT_MODES = {"inline", "separate", "both"}


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def resolve_yield_enhancement_cfg(symbol_cfg: dict | None) -> dict:
    cfg = dict(symbol_cfg or {})
    top_level = _as_dict(cfg.get("yield_enhancement"))

    has_top_level = isinstance(cfg.get("yield_enhancement"), dict)
    if not has_top_level:
        return {}

    output_mode = str(top_level.get("output_mode") or "").strip().lower()
    if not output_mode:
        output_mode = "separate"
    top_level["output_mode"] = output_mode

    if "enabled" in top_level:
        top_level["enabled"] = bool(top_level.get("enabled"))

    return top_level


def yield_enhancement_output_mode(cfg: dict | None, *, default: str = "separate") -> str:
    mode = str((cfg or {}).get("output_mode") or "").strip().lower()
    if mode in YIELD_ENHANCEMENT_OUTPUT_MODES:
        return mode
    return default


def wants_yield_enhancement_inline(cfg: dict | None) -> bool:
    if not bool((cfg or {}).get("enabled", False)):
        return False
    return yield_enhancement_output_mode(cfg) in {"inline", "both"}


def wants_yield_enhancement_separate(cfg: dict | None) -> bool:
    if not bool((cfg or {}).get("enabled", False)):
        return False
    return yield_enhancement_output_mode(cfg) in {"separate", "both"}
