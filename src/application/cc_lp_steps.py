from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from domain.domain.engine.cc_lp import (
    CC_LP_DEFAULT_MAX_PUT_DELTA,
    CC_LP_DEFAULT_MIN_PUT_DELTA,
    CC_LP_DEFAULT_MIN_RETENTION,
    compute_cc_lp_metrics,
    rank_cc_lp_rows,
    validate_cc_lp_pair,
)
from domain.domain.engine.yield_enhancement import YieldEnhancementLeg
from domain.domain.fee_calc import calc_futu_option_fee
from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.symbol_identity import symbol_market
from src.application.scan_sell_call import run_sell_call_scan
from src.application.sell_call_steps import _optional_float
from src.application.sell_put_call_helper import (
    _call_leg_from_required_data,
    _put_leg_from_sell_put_row,
)
from src.application.strategy_policy import SELL_CALL_FAMILY
from src.infrastructure.io_utils import atomic_write_text


CC_LP_FAMILY = "combo_yield"
CC_LP_VARIANT = "cc_lp"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_required_data_puts(*, input_root: Path, symbol: str) -> pd.DataFrame:
    path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if df.empty or "option_type" not in df.columns:
        return pd.DataFrame()
    mask = df["option_type"].astype(str).str.strip().str.lower() == "put"
    return df.loc[mask].copy()


def _put_leg_from_required_data(row: pd.Series) -> YieldEnhancementLeg | None:
    """Build a YieldEnhancementLeg for a long-put reversal leg from required-data row."""

    return _put_leg_from_sell_put_row(row)


def run_cc_lp_scan(
    *,
    symbol: str,
    required_data_dir: Path,
    report_dir: Path,
    sell_call_cfg: dict[str, Any],
    stock: dict[str, Any] | None = None,
    min_put_delta: float = CC_LP_DEFAULT_MIN_PUT_DELTA,
    max_put_delta: float = CC_LP_DEFAULT_MAX_PUT_DELTA,
    min_retention: float = CC_LP_DEFAULT_MIN_RETENTION,
    global_sell_call_liquidity: dict[str, Any] | None = None,
    run_sell_call_scan_fn: Callable[..., pd.DataFrame] = run_sell_call_scan,
    now_utc_fn: Callable[[], datetime] = _utc_now,
    strategy_profile: str = "cc_lp_funding_call",
) -> pd.DataFrame:
    """Run the CC+LP opening scan: independent Sell Call scan + Long Put pairing."""

    if not stock:
        return pd.DataFrame()
    try:
        shares_raw = stock.get("shares")
        shares_can_sell_raw = stock.get("can_sell_qty")
        avg_cost_raw = stock.get("avg_cost")
        if shares_raw is None or shares_can_sell_raw is None or avg_cost_raw is None:
            return pd.DataFrame()
        shares_total = int(shares_raw)
        shares_can_sell = int(shares_can_sell_raw)
        avg_cost = float(avg_cost_raw)
    except Exception:
        return pd.DataFrame()
    if shares_total <= 0 or shares_can_sell < 0 or avg_cost <= 0:
        return pd.DataFrame()

    liquidity = resolve_candidate_liquidity(global_sell_call_liquidity)
    window = resolve_candidate_window(sell_call_cfg, defaults=DEFAULT_SELL_CALL_WINDOW)
    effective_min_strike = resolve_effective_sell_call_min_strike(
        min_strike=_optional_float(sell_call_cfg, "min_strike"),
        avg_cost=avg_cost,
        cost_multiplier=_optional_float(sell_call_cfg, "min_strike_cost_multiplier") or 1.02,
    )
    df_calls = run_sell_call_scan_fn(
        symbols=[symbol],
        input_root=required_data_dir,
        output=None,
        avg_cost=float(avg_cost),
        shares=int(shares_total),
        shares_can_sell=int(shares_can_sell),
        shares_locked=int(stock.get("shares_locked") or 0),
        shares_available_for_cover=int(stock.get("shares_available_for_cover") or 0),
        min_dte=window.min_dte,
        max_dte=window.max_dte,
        min_strike=effective_min_strike,
        max_strike=_optional_float(sell_call_cfg, "max_strike"),
        min_annualized_net_return=0.0,
        min_strike_cost_multiplier=float(
            _optional_float(sell_call_cfg, "min_strike_cost_multiplier") or 1.02
        ),
        min_net_income=0.0,
        min_open_interest=liquidity.min_open_interest,
        min_volume=liquidity.min_volume,
        max_spread_ratio=liquidity.max_spread_ratio,
        strategy_family=SELL_CALL_FAMILY,
        strategy_profile="cc_lp_funding_call",
        quiet=True,
    )
    if df_calls.empty:
        return pd.DataFrame()
    df_puts = _load_required_data_puts(input_root=required_data_dir, symbol=symbol)
    if df_puts.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for _, call_row in df_calls.iterrows():
        call_leg = _call_leg_from_required_data(call_row)
        if call_leg is None:
            continue
        for _, put_row in df_puts.iterrows():
            put_leg = _put_leg_from_required_data(put_row)
            if put_leg is None:
                continue
            rejects = validate_cc_lp_pair(
                call_leg,
                put_leg,
                min_put_delta=min_put_delta,
                max_put_delta=max_put_delta,
            )
            if rejects:
                continue
            try:
                covered_notional = float(call_leg.spot) * float(call_leg.multiplier)
                call_sell_fee = calc_futu_option_fee(
                    call_leg.currency,
                    call_leg.bid,
                    contracts=1,
                    multiplier=call_leg.multiplier,
                    is_sell=True,
                )
                put_buy_fee = calc_futu_option_fee(
                    put_leg.currency,
                    put_leg.ask,
                    contracts=1,
                    multiplier=put_leg.multiplier,
                    is_sell=False,
                )
                metrics = compute_cc_lp_metrics(
                    call_leg=call_leg,
                    put_leg=put_leg,
                    call_sell_fee=call_sell_fee,
                    put_buy_fee=put_buy_fee,
                    covered_notional=covered_notional,
                    dte=min(call_leg.dte, put_leg.dte),
                )
            except ValueError:
                continue
            if metrics.retention < min_retention:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "strategy_family": CC_LP_FAMILY,
                    "variant": CC_LP_VARIANT,
                    "strategy_profile": strategy_profile,
                    "market": symbol_market(symbol),
                    "candidate_pair_id": (
                        f"cc_lp:{symbol}:{call_leg.contract_symbol}:{put_leg.contract_symbol}"
                    ),
                    "call_contract_symbol": call_leg.contract_symbol,
                    "call_strike": call_leg.strike,
                    "call_expiration": call_leg.expiration,
                    "call_dte": call_leg.dte,
                    "call_bid": call_leg.bid,
                    "call_ask": call_leg.ask,
                    "call_delta": call_leg.delta,
                    "call_open_interest": call_leg.open_interest,
                    "call_volume": call_leg.volume,
                    "call_spread_ratio": call_leg.spread_ratio,
                    "put_contract_symbol": put_leg.contract_symbol,
                    "put_strike": put_leg.strike,
                    "put_expiration": put_leg.expiration,
                    "put_dte": put_leg.dte,
                    "put_bid": put_leg.bid,
                    "put_ask": put_leg.ask,
                    "put_delta": put_leg.delta,
                    "put_open_interest": put_leg.open_interest,
                    "put_volume": put_leg.volume,
                    "put_spread_ratio": put_leg.spread_ratio,
                    "spot": call_leg.spot,
                    "currency": call_leg.currency,
                    "multiplier": call_leg.multiplier,
                    "covered_notional": covered_notional,
                    "call_net_credit": metrics.call_net_credit,
                    "put_total_cost": metrics.put_total_cost,
                    "net_credit": metrics.net_credit,
                    "net_debit": metrics.net_debit,
                    "net_credit_retention": metrics.retention,
                    "net_return": metrics.net_return,
                    "annualized_net_return": metrics.annualized_net_return,
                    "call_otm_pct": metrics.call_otm_pct,
                    "put_otm_pct": metrics.put_otm_pct,
                    "gap_width_pct": metrics.gap_width_pct,
                    "combo_spread_ratio": metrics.combo_spread_ratio,
                    "generated_at_utc": now_utc_fn().isoformat(),
                }
            )
    ranked = rank_cc_lp_rows(rows)
    df = pd.DataFrame(ranked)
    if not df.empty:
        atomic_write_text(
            report_dir / f"{symbol.lower()}_cc_lp_candidates.csv",
            df.to_csv(index=False),
        )
    return df


def summarize_cc_lp_result(
    *,
    df: pd.DataFrame,
    symbol: str,
    status: str,
    reason: str = "",
) -> dict[str, Any]:
    """Summarize a CC+LP scan result for pipeline status tracking."""

    return {
        "strategy_family": CC_LP_FAMILY,
        "variant": CC_LP_VARIANT,
        "symbol": symbol,
        "candidate_count": int(len(df)),
        "status": status,
        "reason": reason,
        "generated_at_utc": _utc_now().isoformat(),
    }
