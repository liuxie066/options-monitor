from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateWindowDefaults:
    min_dte: int
    max_dte: int


@dataclass(frozen=True)
class CandidateLiquidityDefaults:
    min_open_interest: float = 300.0
    min_volume: float = 10.0
    max_spread_ratio: float = 0.30


DEFAULT_EVENT_RISK_CONFIG = {
    "enabled": True,
    "mode": "warn",
}

DEFAULT_SELL_PUT_WINDOW = CandidateWindowDefaults(min_dte=20, max_dte=60)
DEFAULT_SELL_CALL_WINDOW = CandidateWindowDefaults(min_dte=20, max_dte=90)
DEFAULT_CANDIDATE_LIQUIDITY = CandidateLiquidityDefaults()


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def resolve_candidate_window(raw: dict | None, *, defaults: CandidateWindowDefaults) -> CandidateWindowDefaults:
    src = raw or {}
    return CandidateWindowDefaults(
        min_dte=_coerce_int(src.get("min_dte"), default=defaults.min_dte),
        max_dte=_coerce_int(src.get("max_dte"), default=defaults.max_dte),
    )


def resolve_candidate_liquidity(raw: dict | None) -> CandidateLiquidityDefaults:
    src = raw or {}
    return CandidateLiquidityDefaults(
        min_open_interest=_coerce_float(
            src.get("min_open_interest"),
            default=DEFAULT_CANDIDATE_LIQUIDITY.min_open_interest,
        ),
        min_volume=_coerce_float(
            src.get("min_volume"),
            default=DEFAULT_CANDIDATE_LIQUIDITY.min_volume,
        ),
        max_spread_ratio=_coerce_float(
            src.get("max_spread_ratio"),
            default=DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio,
        ),
    )


def resolve_event_risk_config(raw: dict | None) -> dict[str, Any]:
    src = dict(DEFAULT_EVENT_RISK_CONFIG)
    if isinstance(raw, dict):
        src.update(raw)
    return {
        "enabled": bool(src.get("enabled", DEFAULT_EVENT_RISK_CONFIG["enabled"])),
        "mode": str(src.get("mode") or DEFAULT_EVENT_RISK_CONFIG["mode"]),
    }
