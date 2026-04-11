#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
import math
import pandas as pd

# Allow running as `python scripts/scan_sell_put.py` without installation.
repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.sell_put_config import validate_min_annualized_net_return



def calc_futu_us_option_fee(order_price: float, contracts: int = 1, is_sell: bool = True) -> float:
    """Simplified Futu US option fee model for single-leg option order."""
    commission_per_contract = 0.65 if order_price > 0.1 else 0.15
    commission = max(commission_per_contract * contracts, 1.99)
    platform_fee = 0.30 * contracts
    taf = max(0.00329 * contracts, 0.01) if is_sell else 0.0
    orf = 0.013 * contracts
    occ = 0.02 * contracts
    settlement = 0.18 * contracts
    total = commission + platform_fee + taf + orf + occ + settlement
    return round(total, 6)


def calc_futu_hk_option_fee_static(order_price: float, contracts: int = 1, is_sell: bool = True, *, base_dir: Path | None = None) -> float:
    """HK option static fee model (HKD).

    Configurable via runtime config (config.hk.json/config.us.json):
      fees.hk_static.platform_fee_per_order_hkd
      fees.hk_static.commission_per_order_hkd
      fees.hk_static.other_fees_per_order_hkd

    Note: this is per-order, not per-contract.
    """
    platform_fee_per_order = 15.0
    commission_per_order = 0.0
    other_per_order = 0.0

    try:
        if base_dir is not None:
            import json
            cfg = None
            for cfg_name in ('config.hk.json', 'config.us.json'):
                cfg_path = base_dir / cfg_name
                if cfg_path.exists() and cfg_path.stat().st_size > 0:
                    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
                    break
            if isinstance(cfg, dict):
                hk = ((cfg.get('fees') or {}).get('hk_static') or {})
                platform_fee_per_order = float(hk.get('platform_fee_per_order_hkd', platform_fee_per_order))
                commission_per_order = float(hk.get('commission_per_order_hkd', commission_per_order))
                other_per_order = float(hk.get('other_fees_per_order_hkd', other_per_order))
    except Exception:
        pass

    return round(platform_fee_per_order + commission_per_order + other_per_order, 6)


def calc_futu_option_fee(currency: str | None, order_price: float, contracts: int = 1, is_sell: bool = True, *, base_dir: Path | None = None) -> float:
    ccy = (currency or 'USD').upper()
    if ccy == 'HKD':
        return calc_futu_hk_option_fee_static(order_price, contracts=contracts, is_sell=is_sell, base_dir=base_dir)
    return calc_futu_us_option_fee(order_price, contracts=contracts, is_sell=is_sell)


def safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def safe_int(v):
    try:
        if pd.isna(v):
            return None
        return int(v)
    except Exception:
        return None


def compute_metrics(row: pd.Series) -> dict | None:
    mid = safe_float(row.get("mid"))
    strike = safe_float(row.get("strike"))
    spot = safe_float(row.get("spot"))
    try:
        dte = int(row.get("dte"))
    except Exception:
        return None

    if mid is None or strike is None or spot is None or dte <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
        return None
    if strike >= spot:
        return None

    multiplier = safe_float(row.get("multiplier"))
    m = int(multiplier) if multiplier and multiplier > 0 else None
    if not m:
        return None

    gross_income = mid * m
    base_dir = Path(__file__).resolve().parents[1]
    fee = calc_futu_option_fee(row.get('currency'), mid, contracts=1, is_sell=True, base_dir=base_dir)
    net_income = gross_income - fee
    if net_income <= 0:
        return None

    otm_pct = (spot - strike) / spot
    cash_basis = strike * m - net_income
    if cash_basis <= 0:
        return None

    annualized_net_return_on_cash_basis = (net_income / cash_basis) * (365 / dte)
    annualized_net_return_on_strike = (net_income / (strike * m)) * (365 / dte)
    breakeven = strike - net_income / m

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
    parser.add_argument("--min-annualized-net-return", type=float, default=None, help="required; min annualized net return in [0,1]")
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
    parser.add_argument("--output", default=None, help="Output CSV path (default: output/reports/sell_put_candidates.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: ./output)")
    args = parser.parse_args()

    try:
        min_annualized_net_return = validate_min_annualized_net_return(args.min_annualized_net_return, source="--min-annualized-net-return")
    except ValueError as e:
        raise SystemExit(f"[ARG_ERROR] {e}")

    base = Path(__file__).resolve().parents[1]
    input_root = (Path(args.input_root).resolve() if args.input_root else (base / "output").resolve())
    out_path = Path(args.output).resolve() if args.output else (base / "output" / "reports" / "sell_put_candidates.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for symbol in args.symbols:
        path = input_root / "parsed" / f"{symbol}_required_data.csv"
        try:
            df = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            # Keep consistent behavior with downstream: an empty required_data CSV should
            # produce an empty candidates CSV, not crash the whole tick.
            df = pd.DataFrame()
        df = df[df["option_type"] == "put"].copy() if (not df.empty and ("option_type" in df.columns)) else pd.DataFrame()

        for _, row in df.iterrows():
            # Critical compute gate: avoid NaN/None silently propagating into returns.
            dte = safe_int(row.get("dte"))
            if dte is None or dte <= 0:
                continue
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

            # Critical compute gate: mid/strike/spot must be valid; otherwise compute_metrics may
            # return None but NaN can still leak via later formatting.
            spot = safe_float(row.get("spot"))
            if mid is None or strike is None or spot is None:
                continue

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
            if metrics["annualized_net_return_on_cash_basis"] < min_annualized_net_return:
                continue
            if spread_ratio is not None and spread_ratio > args.max_spread_ratio:
                continue

            rows.append({
                "symbol": row["symbol"],
                "expiration": row["expiration"],
                "dte": dte,
                "contract_symbol": row.get("contract_symbol"),
                "multiplier": safe_float(row.get("multiplier")),
                "currency": row.get("currency"),
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
    if out.empty:
        # keep a valid header-only CSV to avoid downstream EmptyDataError
        cols = [
            "symbol","expiration","dte","contract_symbol","multiplier","currency","strike","spot","bid","ask","last_price","mid",
            "open_interest","volume","implied_volatility","delta","spread","spread_ratio",
            "gross_income","futu_fee","net_income","otm_pct","cash_basis","breakeven",
            "annualized_net_return_on_strike","annualized_net_return_on_cash_basis",
        ]
        pd.DataFrame(columns=cols).to_csv(out_path, index=False)
    else:
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
