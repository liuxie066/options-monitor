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
    max_spread_ratio: float = 0.40


DEFAULT_SELL_PUT_WINDOW = CandidateWindowDefaults(min_dte=7, max_dte=60)
DEFAULT_SELL_CALL_WINDOW = CandidateWindowDefaults(min_dte=7, max_dte=60)
DEFAULT_SELL_PUT_COMBO_YIELD_WINDOW = CandidateWindowDefaults(min_dte=7, max_dte=90)
DEFAULT_CANDIDATE_LIQUIDITY = CandidateLiquidityDefaults()
DEFAULT_SELL_PUT_COMBO_YIELD_LIQUIDITY = CandidateLiquidityDefaults(
    min_open_interest=100.0,
    min_volume=5.0,
    max_spread_ratio=0.35,
)


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


def resolve_candidate_liquidity(
    raw: dict | None,
    *,
    defaults: CandidateLiquidityDefaults = DEFAULT_CANDIDATE_LIQUIDITY,
) -> CandidateLiquidityDefaults:
    src = raw or {}
    return CandidateLiquidityDefaults(
        min_open_interest=_coerce_float(
            src.get("min_open_interest"),
            default=defaults.min_open_interest,
        ),
        min_volume=_coerce_float(
            src.get("min_volume"),
            default=defaults.min_volume,
        ),
        max_spread_ratio=_coerce_float(
            src.get("max_spread_ratio"),
            default=defaults.max_spread_ratio,
        ),
    )
