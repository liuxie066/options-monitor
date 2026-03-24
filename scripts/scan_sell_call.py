#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd

MULTIPLIER = 100


def calc_futu_us_option_fee(order_price: float, contracts: int = 1, is_sell: bool = True) -> float:
    commission_per_contract = 0.65 if order_price > 0.1 else 0.15
    commission = max(commission_per_contract * contracts, 1.99)
    platform_fee = 0.30 * contracts
    taf = max(0.00329 * contracts, 0.01) if is_sell else 0.0
    orf = 0.013 * contracts
    occ = 0.02 * contracts
    settlement = 0.18 * contracts
    return round(commission + platform_fee + taf + orf + occ + settlement, 6)


def safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def strike_band(strike_above_spot_pct: float) -> str:
    if strike_above_spot_pct < 0.03:
        return '<3%'
    if strike_above_spot_pct < 0.08:
        return '3%-8%'
    return '>=8%'


def risk_label(strike_above_spot_pct: float) -> str:
    if strike_above_spot_pct < 0.03:
        return '激进'
    if strike_above_spot_pct < 0.08:
        return '中性'
    return '保守'


def compute_metrics(row: pd.Series, avg_cost: float):
    mid = safe_float(row.get('mid'))
    strike = safe_float(row.get('strike'))
    spot = safe_float(row.get('spot'))
    dte = int(row.get('dte'))
    if None in (mid, strike, spot) or dte <= 0 or avg_cost <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
        return None

    gross_income = mid * MULTIPLIER
    fee = calc_futu_us_option_fee(mid, contracts=1, is_sell=True)
    net_income = gross_income - fee
    if net_income <= 0:
        return None

    annualized_net_premium_return = (net_income / (avg_cost * MULTIPLIER)) * (365 / dte)
    if_exercised_total_return = (((strike - avg_cost) * MULTIPLIER) + net_income) / (avg_cost * MULTIPLIER)
    strike_above_spot_pct = (strike - spot) / spot
    strike_above_cost_pct = (strike - avg_cost) / avg_cost

    return {
        'gross_income': round(gross_income, 6),
        'futu_fee': round(fee, 6),
        'net_income': round(net_income, 6),
        'annualized_net_premium_return': round(annualized_net_premium_return, 6),
        'if_exercised_total_return': round(if_exercised_total_return, 6),
        'strike_above_spot_pct': round(strike_above_spot_pct, 6),
        'strike_above_cost_pct': round(strike_above_cost_pct, 6),
        'cc_band': strike_band(strike_above_spot_pct),
        'risk_label': risk_label(strike_above_spot_pct),
    }


def main():
    parser = argparse.ArgumentParser(description='Run Sell Call scan on Yahoo required_data CSV files')
    parser.add_argument('--symbols', nargs='+', required=True)
    parser.add_argument('--avg-cost', type=float, required=True, help='Average holding cost per share')
    parser.add_argument('--shares', type=int, default=100)
    parser.add_argument('--min-dte', type=int, default=7)
    parser.add_argument('--max-dte', type=int, default=90)
    parser.add_argument('--min-strike', type=float, default=None)
    parser.add_argument('--max-strike', type=float, default=None)
    parser.add_argument('--min-annualized-net-return', type=float, default=0.03)
    parser.add_argument('--min-if-exercised-total-return', type=float, default=0.0)
    parser.add_argument('--min-open-interest', type=float, default=100)
    parser.add_argument('--min-volume', type=float, default=10)
    parser.add_argument('--max-spread-ratio', type=float, default=0.30)
    parser.add_argument('--min-iv', type=float, default=None, help='min implied volatility (decimal, e.g. 0.15)')
    parser.add_argument('--max-iv', type=float, default=None, help='max implied volatility (decimal, e.g. 2.0)')
    parser.add_argument('--require-bid-ask', action='store_true', help='require bid>0 and ask>0 (better fillability)')
    parser.add_argument('--min-delta', type=float, default=None, help='min call delta (e.g. 0.20)')
    parser.add_argument('--max-delta', type=float, default=None, help='max call delta (e.g. 0.35)')
    args = parser.parse_args()

    if args.shares < 100:
        raise SystemExit('shares 必须至少 100，sell call 才有意义。')

    base = Path(__file__).resolve().parents[1]
    out_dir = base / 'output' / 'reports'
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for symbol in args.symbols:
        path = base / 'output' / 'parsed' / f'{symbol}_required_data.csv'
        df = pd.read_csv(path)
        df = df[df['option_type'] == 'call'].copy()

        for _, row in df.iterrows():
            dte = int(row['dte'])
            if dte < args.min_dte or dte > args.max_dte:
                continue

            strike = safe_float(row.get('strike'))
            if strike is None:
                continue
            if args.min_strike is not None and strike < args.min_strike:
                continue
            if args.max_strike is not None and strike > args.max_strike:
                continue

            oi = safe_float(row.get('open_interest')) or 0.0
            vol = safe_float(row.get('volume')) or 0.0
            if oi < args.min_open_interest or vol < args.min_volume:
                continue

            bid = safe_float(row.get('bid'))
            ask = safe_float(row.get('ask'))
            mid = safe_float(row.get('mid'))

            if args.require_bid_ask:
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue

            iv = safe_float(row.get('implied_volatility'))
            if iv is not None and iv > 3.0:
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
            if spread_ratio is not None and spread_ratio > args.max_spread_ratio:
                continue

            # Delta filter (optional)
            try:
                d = safe_float(row.get('delta'))
                if (args.min_delta is not None) or (args.max_delta is not None):
                    if d is None:
                        continue
                    d = float(d)
                    if args.min_delta is not None and d < float(args.min_delta):
                        continue
                    if args.max_delta is not None and d > float(args.max_delta):
                        continue
            except Exception:
                pass

            metrics = compute_metrics(row, args.avg_cost)
            if not metrics:
                continue
            if metrics['annualized_net_premium_return'] < args.min_annualized_net_return:
                continue
            if metrics['if_exercised_total_return'] < args.min_if_exercised_total_return:
                continue

            rows.append({
                'symbol': row['symbol'],
                'expiration': row['expiration'],
                'dte': dte,
                'contract_symbol': row.get('contract_symbol'),
                'strike': strike,
                'spot': safe_float(row.get('spot')),
                'avg_cost': args.avg_cost,
                'shares': args.shares,
                'bid': bid,
                'ask': ask,
                'last_price': safe_float(row.get('last_price')),
                'mid': mid,
                'open_interest': oi,
                'volume': vol,
                'implied_volatility': safe_float(row.get('implied_volatility')),
                'delta': safe_float(row.get('delta')),
                'spread': spread,
                'spread_ratio': spread_ratio,
                **metrics,
            })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(by=['annualized_net_premium_return', 'if_exercised_total_return'], ascending=[False, False])
    out_path = out_dir / 'sell_call_candidates.csv'
    out.to_csv(out_path, index=False)

    print(f'[DONE] sell call scan -> {out_path}')
    print(f'[DONE] candidates: {len(out)}')
    if not out.empty:
        cols = [
            'symbol','expiration','dte','strike','spot','avg_cost','mid','net_income',
            'annualized_net_premium_return','if_exercised_total_return','strike_above_spot_pct','risk_label'
        ]
        print(out[cols].head(20).to_string(index=False))


if __name__ == '__main__':
    main()
