from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_STRONG_REMAINING_ANNUALIZED_MAX = 0.08
DEFAULT_MEDIUM_REMAINING_ANNUALIZED_MAX = 0.12

TIER_LABELS = {
    "strong": "强烈建议平仓",
    "medium": "建议平仓",
    "weak": "可观察平仓",
    "optional": "低价买回可选",
    "none": "不提醒",
}

TIER_PRIORITY = {
    "strong": 0,
    "medium": 1,
    "optional": 2,
    "weak": 3,
    "none": 9,
}


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


@dataclass(frozen=True)
class CloseAdviceConfig:
    max_spread_ratio: float | None = 0.4
    strong_remaining_annualized_max: float = DEFAULT_STRONG_REMAINING_ANNUALIZED_MAX
    medium_remaining_annualized_max: float = DEFAULT_MEDIUM_REMAINING_ANNUALIZED_MAX

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "CloseAdviceConfig":
        src = raw or {}
        max_spread = safe_float(src.get("max_spread_ratio"))
        strong_max = safe_float(src.get("strong_remaining_annualized_max"))
        medium_max = safe_float(src.get("medium_remaining_annualized_max"))
        return cls(
            max_spread_ratio=max_spread if max_spread is not None else 0.4,
            strong_remaining_annualized_max=(
                strong_max if strong_max is not None else DEFAULT_STRONG_REMAINING_ANNUALIZED_MAX
            ),
            medium_remaining_annualized_max=(
                medium_max if medium_max is not None else DEFAULT_MEDIUM_REMAINING_ANNUALIZED_MAX
            ),
        )


@dataclass(frozen=True)
class CloseAdviceTierRule:
    level: str
    reason: str
    min_capture: float
    min_dte: int | None = None
    max_dte: int | None = None
    remaining_annualized_attr: str | None = None

    def matches(
        self,
        *,
        capture_ratio: float,
        dte: int,
        remaining_annualized_return: float | None,
        config: CloseAdviceConfig,
    ) -> bool:
        if self.min_dte is not None and dte < self.min_dte:
            return False
        if self.max_dte is not None and dte > self.max_dte:
            return False
        if capture_ratio < self.min_capture:
            return False
        if self.remaining_annualized_attr:
            limit = safe_float(getattr(config, self.remaining_annualized_attr, None))
            if limit is None or remaining_annualized_return is None:
                return False
            if remaining_annualized_return > limit:
                return False
        return True


DEFAULT_TIER_RULES: tuple[CloseAdviceTierRule, ...] = (
    CloseAdviceTierRule(
        level="strong",
        min_dte=7,
        max_dte=13,
        min_capture=0.90,
        remaining_annualized_attr="strong_remaining_annualized_max",
        reason="已锁定大部分收益，剩余时间仍长，继续持有的边际收益偏低",
    ),
    CloseAdviceTierRule(
        level="strong",
        min_dte=14,
        max_dte=29,
        min_capture=0.85,
        remaining_annualized_attr="strong_remaining_annualized_max",
        reason="已锁定大部分收益，剩余时间仍长，继续持有的边际收益偏低",
    ),
    CloseAdviceTierRule(
        level="strong",
        min_dte=30,
        min_capture=0.80,
        remaining_annualized_attr="strong_remaining_annualized_max",
        reason="已锁定大部分收益，剩余时间仍长，继续持有的边际收益偏低",
    ),
    CloseAdviceTierRule(
        level="medium",
        min_dte=14,
        min_capture=0.70,
        remaining_annualized_attr="medium_remaining_annualized_max",
        reason="已锁定较多收益，剩余时间仍较长，值得认真考虑买回",
    ),
    CloseAdviceTierRule(
        level="optional",
        min_dte=1,
        max_dte=6,
        min_capture=0.90,
        reason="临近到期且平仓成本较低，低价买回可选",
    ),
    CloseAdviceTierRule(
        level="weak",
        min_dte=30,
        min_capture=0.50,
        reason="已锁定部分收益且剩余时间较长，适合进入观察",
    ),
)


@dataclass(frozen=True)
class CloseAdviceInput:
    account: str
    symbol: str
    option_type: str
    side: str
    expiration: str | None
    strike: float | None
    contracts_open: int | None
    premium: float | None
    close_mid: float | None
    bid: float | None = None
    ask: float | None = None
    dte: int | None = None
    multiplier: float | None = None
    spot: float | None = None
    currency: str | None = None


def _remaining_annualized_return(inp: CloseAdviceInput) -> float | None:
    mid = safe_float(inp.close_mid)
    dte = safe_int(inp.dte)
    if mid is None or dte is None or dte <= 0:
        return None
    if str(inp.option_type).lower() == "put":
        denominator = safe_float(inp.strike)
    elif str(inp.option_type).lower() == "call":
        denominator = safe_float(inp.spot)
    else:
        denominator = None
    if denominator is None or denominator <= 0:
        return None
    return (float(mid) / float(denominator)) * (365.0 / float(dte))


def _spread_ratio(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None:
        return None
    if ask < bid or mid <= 0:
        return None
    return (ask - bid) / mid


def decide_tier(
    *,
    capture_ratio: float,
    dte: int,
    remaining_annualized_return: float | None,
    config: CloseAdviceConfig,
) -> tuple[str, str]:
    for rule in DEFAULT_TIER_RULES:
        if rule.matches(
            capture_ratio=capture_ratio,
            dte=dte,
            remaining_annualized_return=remaining_annualized_return,
            config=config,
        ):
            return rule.level, rule.reason

    return "none", "未达到平仓建议阈值"


def evaluate_close_advice(inp: CloseAdviceInput, config: CloseAdviceConfig | None = None) -> dict[str, Any]:
    cfg = config or CloseAdviceConfig()
    flags: list[str] = []

    option_type = str(inp.option_type or "").strip().lower()
    side = str(inp.side or "").strip().lower()
    if side != "short" or option_type not in {"put", "call"}:
        flags.append("unsupported_position")
        return _result(inp, tier="none", reason="首版仅支持 open short put/call", flags=flags)

    premium = safe_float(inp.premium)
    mid = safe_float(inp.close_mid)
    dte = safe_int(inp.dte)
    multiplier = safe_float(inp.multiplier)
    contracts_open = safe_int(inp.contracts_open)

    if premium is None:
        flags.append("missing_premium")
    elif premium <= 0:
        flags.append("invalid_premium")
    if mid is None:
        flags.append("missing_mid")
    elif mid <= 0:
        flags.append("invalid_mid")
    if dte is None:
        flags.append("missing_dte")
    elif dte <= 0:
        flags.append("invalid_dte")
    if multiplier is None:
        flags.append("missing_multiplier")
    elif multiplier <= 0:
        flags.append("invalid_multiplier")
    if contracts_open is None:
        flags.append("missing_contracts_open")
    elif contracts_open <= 0:
        flags.append("invalid_contracts_open")

    spread = _spread_ratio(inp.bid, inp.ask, mid)
    if inp.bid is not None and inp.ask is not None and inp.ask < inp.bid:
        flags.append("invalid_spread")
    if cfg.max_spread_ratio is not None and spread is not None and spread > cfg.max_spread_ratio:
        flags.append("spread_too_wide")

    blocking = [
        f
        for f in flags
        if f
        in {
            "missing_premium",
            "invalid_premium",
            "missing_mid",
            "invalid_mid",
            "missing_dte",
            "invalid_dte",
            "missing_multiplier",
            "invalid_multiplier",
            "missing_contracts_open",
            "invalid_contracts_open",
            "invalid_spread",
            "spread_too_wide",
        }
    ]
    if blocking:
        return _result(inp, tier="none", reason="数据不足或报价质量不足，暂不提醒", flags=flags, spread_ratio=spread)

    assert premium is not None and mid is not None and dte is not None
    assert multiplier is not None and contracts_open is not None

    capture_ratio = (premium - mid) / premium
    remaining_premium = mid * multiplier * contracts_open
    realized_if_close = (premium - mid) * multiplier * contracts_open
    remaining_annualized = _remaining_annualized_return(inp)

    if mid >= premium:
        flags.append("not_profitable_to_close")
        return _result(
            inp,
            tier="none",
            reason="当前平仓价不低于开仓权利金，不属于收益型买回建议",
            flags=flags,
            capture_ratio=capture_ratio,
            remaining_premium=remaining_premium,
            realized_if_close=realized_if_close,
            remaining_annualized_return=remaining_annualized,
            spread_ratio=spread,
        )

    if remaining_annualized is None:
        flags.append("missing_remaining_annualized_return")

    tier, reason = decide_tier(
        capture_ratio=capture_ratio,
        dte=dte,
        remaining_annualized_return=remaining_annualized,
        config=cfg,
    )
    return _result(
        inp,
        tier=tier,
        reason=reason,
        flags=flags,
        capture_ratio=capture_ratio,
        remaining_premium=remaining_premium,
        realized_if_close=realized_if_close,
        remaining_annualized_return=remaining_annualized,
        spread_ratio=spread,
    )


def _result(
    inp: CloseAdviceInput,
    *,
    tier: str,
    reason: str,
    flags: list[str],
    capture_ratio: float | None = None,
    remaining_premium: float | None = None,
    realized_if_close: float | None = None,
    remaining_annualized_return: float | None = None,
    spread_ratio: float | None = None,
) -> dict[str, Any]:
    return {
        "account": str(inp.account or "").strip().lower(),
        "symbol": str(inp.symbol or "").strip().upper(),
        "option_type": str(inp.option_type or "").strip().lower(),
        "expiration": inp.expiration,
        "strike": safe_float(inp.strike),
        "contracts_open": safe_int(inp.contracts_open),
        "premium": safe_float(inp.premium),
        "close_mid": safe_float(inp.close_mid),
        "bid": safe_float(inp.bid),
        "ask": safe_float(inp.ask),
        "dte": safe_int(inp.dte),
        "multiplier": safe_float(inp.multiplier),
        "capture_ratio": capture_ratio,
        "remaining_premium": remaining_premium,
        "realized_if_close": realized_if_close,
        "remaining_annualized_return": remaining_annualized_return,
        "spread_ratio": spread_ratio,
        "tier": tier,
        "tier_label": TIER_LABELS.get(tier, tier),
        "reason": reason,
        "data_quality_flags": ";".join(flags),
        "currency": (str(inp.currency or "").strip().upper() or None),
        "spot": safe_float(inp.spot),
    }


def sort_advice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows or [],
        key=lambda r: (
            TIER_PRIORITY.get(str(r.get("tier") or "none"), 9),
            -(safe_float(r.get("capture_ratio")) or 0.0),
            -(safe_float(r.get("remaining_premium")) or 0.0),
            str(r.get("symbol") or ""),
        ),
    )
