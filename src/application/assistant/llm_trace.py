from __future__ import annotations

from typing import Any


def skipped_llm_trace(settings: Any, *, reason: str) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings, "enabled", False)),
        "attempted": False,
        "reason": str(reason),
        "provider": str(getattr(settings, "provider", "")),
        "base_url": str(getattr(settings, "base_url", "")),
        "model": str(getattr(settings, "model", "")),
        "api_key_env": str(getattr(settings, "api_key_env", "")),
        "confidence_min": float(getattr(settings, "confidence_min", 0.0)),
        "timeout_seconds": int(getattr(settings, "timeout_seconds", 0)),
        "max_output_tokens": int(getattr(settings, "max_output_tokens", 0)),
    }


__all__ = ["skipped_llm_trace"]
