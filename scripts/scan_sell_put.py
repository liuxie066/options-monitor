#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import math
import pandas as pd


MULTIPLIER = 100


def calc_futu_us_option_fee(order_price: float, contracts: int = 1, is_sell: bool = True) -> float:
    """
    Simplified Futu US option fee model for single-leg option order.
    Assumptions based on current public schedule discussed in the project:
    - Commission: $0.65/contract if premium > 0.1, else $0.15/contract; minimum $1.99/order
    - Platform fee: $0.30/contract
    - TAF (sell only): $0.00329/contract, minimum $0.01/order
    - ORF: $0.013/contract
    - OCC clearing fee: $0.02/contract
    - Settlement fee: $0.18/contract
    """
    commission_per_contract = 0.65 if order_price > 0.1 else 0.15
    commission = max(commission_per_contract * contracts, 1.99)
    platform_fee = 0.30 * contracts
    taf = max(0.00329 * contracts, 0.01) if is_sell else 0.0
    orf = 0.013 * contracts
    occ = 0.02 * contracts
    settlement = 0.18 * contracts
    total = commission + platform_fee + taf + orf + occ + settlement
    return round(total, 6)


def safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def compute_metrics(row: pd.Series) -> dict | None:
    mid = safe_float(row.get("mid"))
    strike = safe_float(row.get("strike"))
    spot = safe_float(row.get("spot"))
    dte = int(row.get("dte"))

    if mid is None or strike is None or spot is None or dte <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
        return None
    if strike >= spot:
        return None

    gross_income = mid * MULTIPLIER
    fee = calc_futu_us_option_fee(mid, contracts=1, is_sell=True)
    net_income = gross_income - fee
    if net_income <= 0:
        return None

    otm_pct = (spot - strike) / spot
    cash_basis = strike * MULTIPLIER - net_income
    if cash_basis <= 0:
        return None

    annualized_net_return_on_cash_basis = (net_income / cash_basis) * (365 / dte)
    annualized_net_return_on_strike = (net_income / (strike * MULTIPLIER)) * (365 / dte)
    breakeven = strike - net_income / MULTIPLIER

    return {
        "gross_income": round(gross_income, 6),
        "futu_fee": round(fee, 6),
        "net_income": round(net_income, 6),
        "otm_pct": round(otm_pct, 6),
        "cash_basis": round(cash_basis, 6),
        "breakeven": round(breakeven, 6),
        "annualized_net_return_on_strike": round(annualized_net_return_on_strike, 6),
        "annualized_net_return_on_cash_basis": round(annualized_net_return_on_cash_basis, 6),
    }


def main():
    parser = argparse.ArgumentParser(description="Run Sell Put scan on Yahoo required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--min-otm-pct", type=float, default=0.05)
    parser.add_argument("--min-annualized-net-return", type=float, default=0.10)
    parser.add_argument("--min-net-income", type=float, default=50.0)
    parser.add_argument("--min-strike", type=float, default=None)
    parser.add_argument("--max-strike", type=float, default=None)
    parser.add_argument("--min-open-interest", type=float, default=100)
    parser.add_argument("--min-volume", type=float, default=10)
    parser.add_argument("--max-spread-ratio", type=float, default=0.30)
    parser.add_argument("--min-iv", type=float, default=None, help="min implied volatility (decimal, e.g. 0.15)")
    parser.add_argument("--max-iv", type=float, default=None, help="max implied volatility (decimal, e.g. 2.0)")
    parser.add_argument("--require-bid-ask", action="store_true", help="require bid>0 and ask>0 (better fillability)")
    parser.add_argument("--min-abs-delta", type=float, default=None, help="min abs(delta) (e.g. 0.15)")
    parser.add_argument("--max-abs-delta", type=float, default=None, help="max abs(delta) (e.g. 0.28)")
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    out_dir = base / "output" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for symbol in args.symbols:
        path = base / "output" / "parsed" / f"{symbol}_required_data.csv"
        df = pd.read_csv(path)
        df = df[df["option_type"] == "put"].copy()

        for _, row in df.iterrows():
            dte = int(row["dte"])
            if dte < args.min_dte or dte > args.max_dte:
                continue

            strike = safe_float(row.get("strike"))
            if strike is None:
                continue
            if args.min_strike is not None and strike < args.min_strike:
                continue
            if args.max_strike is not None and strike > args.max_strike:
                continue

            oi = safe_float(row.get("open_interest")) or 0.0
            vol = safe_float(row.get("volume")) or 0.0
            if oi < args.min_open_interest:
                continue
            if vol < args.min_volume:
                continue

            bid = safe_float(row.get("bid"))
            ask = safe_float(row.get("ask"))
            mid = safe_float(row.get("mid"))

            # Data-quality gate: require a real market (avoid Yahoo rows with 0/0 quotes)
            if args.require_bid_ask:
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue

            iv = safe_float(row.get("implied_volatility"))
            if iv is not None:
                # yfinance sometimes yields IV in percent; normalize heuristically
                if iv > 3.0:
                    iv = iv / 100.0
            if args.min_iv is not None:
                if iv is None or iv < float(args.min_iv):
                    continue
            if args.max_iv is not None and iv is not None:
                if iv > float(args.max_iv):
                    continue
            spread = None
            spread_ratio = None
            if bid is not None and ask is not None and ask >= bid:
                spread = ask - bid
                if mid is not None and mid > 0:
                    spread_ratio = spread / mid

            # Delta filter (optional)
            try:
                d = safe_float(row.get('delta'))
                if (args.min_abs_delta is not None) or (args.max_abs_delta is not None):
                    # If user requests delta gating, missing delta => skip.
                    if d is None:
                        continue
                    ad = abs(float(d))
                    if args.min_abs_delta is not None and ad < float(args.min_abs_delta):
                        continue
                    if args.max_abs_delta is not None and ad > float(args.max_abs_delta):
                        continue
            except Exception:
                pass

            metrics = compute_metrics(row)
            if not metrics:
                continue
            if metrics["otm_pct"] < args.min_otm_pct:
                continue
            if metrics["net_income"] < args.min_net_income:
                continue
            if metrics["annualized_net_return_on_cash_basis"] < args.min_annualized_net_return:
                continue
            if spread_ratio is not None and spread_ratio > args.max_spread_ratio:
                continue

            rows.append({
                "symbol": row["symbol"],
                "expiration": row["expiration"],
                "dte": dte,
                "contract_symbol": row.get("contract_symbol"),
                "strike": safe_float(row.get("strike")),
                "spot": safe_float(row.get("spot")),
                "bid": safe_float(row.get("bid")),
                "ask": safe_float(row.get("ask")),
                "last_price": safe_float(row.get("last_price")),
                "mid": safe_float(row.get("mid")),
                "open_interest": oi,
                "volume": vol,
                "implied_volatility": safe_float(row.get("implied_volatility")),
                "delta": safe_float(row.get("delta")),
                "spread": spread,
                "spread_ratio": spread_ratio,
                **metrics,
            })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(by=["annualized_net_return_on_cash_basis", "net_income"], ascending=[False, False])
    out_path = out_dir / "sell_put_candidates.csv"
    out.to_csv(out_path, index=False)

    if not args.quiet:
        print(f"[DONE] sell put scan -> {out_path}")
        print(f"[DONE] candidates: {len(out)}")
        if not out.empty:
            display_cols = [
                "symbol", "expiration", "dte", "strike", "spot", "mid",
                "futu_fee", "net_income", "otm_pct", "annualized_net_return_on_cash_basis"
            ]
            print(out[display_cols].head(20).to_string(index=False))


if __name__ == "__main__":
    main()
