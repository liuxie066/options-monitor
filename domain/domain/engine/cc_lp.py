from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .yield_enhancement import YieldEnhancementLeg


CC_LP_DEFAULT_MIN_PUT_DELTA = 0.10
CC_LP_DEFAULT_MAX_PUT_DELTA = 0.25
CC_LP_DEFAULT_MIN_RETENTION = 0.20
CC_LP_DEFAULT_TARGET_PUT_DELTA = 0.12


@dataclass(frozen=True)
class CcLpMetrics:
    """CC+LP combo metrics.

    Funding leg is a short call (premium received), reversal leg is a long put
    (premium paid). Covered notional is passed in from the stock position and
    is NOT reduced by net credit (the stock remains fully held).
    """

    call_net_credit: float
    put_total_cost: float
    net_credit: float
    net_debit: float
    retention: float
    net_return: float
    annualized_net_return: float | None
    call_otm_pct: float
    put_otm_pct: float
    gap_width_pct: float
    call_spread_ratio: float | None
    put_spread_ratio: float | None
    combo_spread_ratio: float | None


def validate_cc_lp_pair(
    call_leg: YieldEnhancementLeg,
    put_leg: YieldEnhancementLeg,
    *,
    min_put_delta: float = CC_LP_DEFAULT_MIN_PUT_DELTA,
    max_put_delta: float = CC_LP_DEFAULT_MAX_PUT_DELTA,
) -> list[str]:
    """Validate a same-expiry CC+LP pair.

    The funding leg is the short call; the reversal leg is the long put.
    Structure direction requires ``call_strike > put_strike``.
    """

    rejects: list[str] = []
    if str(call_leg.option_type).lower() != "call":
        rejects.append("call_leg_option_type")
    if str(put_leg.option_type).lower() != "put":
        rejects.append("put_leg_option_type")
    if call_leg.symbol.upper() != put_leg.symbol.upper():
        rejects.append("symbol_mismatch")
    if call_leg.expiration != put_leg.expiration:
        rejects.append("expiration_mismatch")
    if call_leg.currency.upper() != put_leg.currency.upper():
        rejects.append("currency_mismatch")
    if float(call_leg.multiplier) != float(put_leg.multiplier):
        rejects.append("multiplier_mismatch")
    if float(call_leg.strike) <= float(put_leg.strike):
        rejects.append("strike_order")
    if call_leg.spot <= 0 or put_leg.spot <= 0:
        rejects.append("spot")
    if call_leg.dte <= 0 or put_leg.dte <= 0:
        rejects.append("dte")
    if call_leg.bid <= 0 or put_leg.ask <= 0:
        rejects.append("execution_price")
    put_delta = abs(_safe_float(put_leg.delta) or 0.0)
    if put_delta < float(min_put_delta):
        rejects.append("put_delta_below_min")
    if put_delta > float(max_put_delta):
        rejects.append("put_delta_above_max")
    return rejects


def compute_cc_lp_metrics(
    *,
    call_leg: YieldEnhancementLeg,
    put_leg: YieldEnhancementLeg,
    call_sell_fee: float,
    put_buy_fee: float,
    covered_notional: float,
    dte: int | None = None,
) -> CcLpMetrics:
    """Compute CC+LP combo metrics.

    ``covered_notional`` is the current market value of the held shares
    (spot * shares) and is NOT reduced by net credit.
    """

    rejects = validate_cc_lp_pair(call_leg, put_leg)
    if rejects:
        raise ValueError(f"invalid cc_lp pair: {', '.join(rejects)}")
    multiplier = float(call_leg.multiplier)
    spot = float(call_leg.spot)
    call_net_credit = float(call_leg.bid) * multiplier - float(call_sell_fee)
    put_total_cost = float(put_leg.ask) * multiplier + float(put_buy_fee)
    if call_net_credit <= 0:
        raise ValueError("call_net_credit must be > 0")
    net_credit = call_net_credit - put_total_cost
    net_debit = max(-net_credit, 0.0)
    retention = net_credit / call_net_credit
    if covered_notional <= 0:
        raise ValueError("covered_notional must be > 0")
    net_return = net_credit / covered_notional
    resolved_dte = int(dte if dte is not None else min(call_leg.dte, put_leg.dte))
    annualized_net_return = net_return * (365.0 / float(resolved_dte)) if resolved_dte > 0 else None
    call_otm_pct = _pct_distance(float(call_leg.strike) - spot, spot)
    put_otm_pct = _pct_distance(spot - float(put_leg.strike), spot)
    gap_width_pct = _pct_distance(float(call_leg.strike) - float(put_leg.strike), spot)
    call_spread = _safe_float(call_leg.spread_ratio)
    put_spread = _safe_float(put_leg.spread_ratio)
    combo_spread_ratio = None
    if call_spread is not None and put_spread is not None:
        combo_spread_ratio = call_spread + put_spread
    return CcLpMetrics(
        call_net_credit=round(call_net_credit, 6),
        put_total_cost=round(put_total_cost, 6),
        net_credit=round(net_credit, 6),
        net_debit=round(net_debit, 6),
        retention=round(retention, 6),
        net_return=round(net_return, 6),
        annualized_net_return=(
            round(annualized_net_return, 6) if annualized_net_return is not None else None
        ),
        call_otm_pct=round(call_otm_pct, 6),
        put_otm_pct=round(put_otm_pct, 6),
        gap_width_pct=round(gap_width_pct, 6),
        call_spread_ratio=call_spread,
        put_spread_ratio=put_spread,
        combo_spread_ratio=(
            round(combo_spread_ratio, 6) if combo_spread_ratio is not None else None
        ),
    )


def cc_lp_rank_key(
    row: dict[str, Any],
    *,
    target_put_delta: float = CC_LP_DEFAULT_TARGET_PUT_DELTA,
) -> tuple[Any, ...]:
    """Rank CC+LP candidates: retention primary, reversal-put delta closeness secondary."""

    retention = _safe_float(row.get("net_credit_retention")) or _safe_float(row.get("retention")) or 0.0
    put_delta = abs(_safe_float(row.get("put_delta")) or 0.0)
    call_spread = _safe_float(row.get("call_spread_ratio")) or 999.0
    put_spread = _safe_float(row.get("put_spread_ratio")) or 999.0
    call_oi = _safe_float(row.get("call_open_interest")) or 0.0
    put_oi = _safe_float(row.get("put_open_interest")) or 0.0
    return (
        -float(retention),
        abs(float(put_delta) - float(target_put_delta)),
        max(float(call_spread), float(put_spread)),
        -min(float(call_oi), float(put_oi)),
        str(row.get("call_contract_symbol") or ""),
        str(row.get("put_contract_symbol") or ""),
    )


def rank_cc_lp_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(row) for row in rows), key=cc_lp_rank_key)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    try:
        if out != out:
            return None
    except Exception:
        return None
    return out


def _pct_distance(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator
