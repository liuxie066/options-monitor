"""Report summary helpers.

Stage 3 refactor target: make run_pipeline orchestration-only.

These functions are intentionally pure (DataFrame -> dict) and must not perform I/O.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def summarize_sell_put(df: pd.DataFrame, symbol: str, *, symbol_cfg: dict | None = None) -> dict[str, Any]:
    symbol_cfg = symbol_cfg or {}

    row: dict[str, Any] = {
        'symbol': symbol,
        'strategy': 'sell_put',
        'candidate_count': 0,
        'top_contract': '',
        'expiration': '',
        'strike': None,
        'dte': None,
        'net_income': None,
        'annualized_return': None,
        'risk_label': '',
        'delta': None,
        'iv': None,
        'cash_secured_used_usd': 0.0,
        'cash_required_usd': None,
        'cash_available_usd': None,
        'cash_free_usd': None,
        'cash_available_usd_est': None,
        'cash_free_usd_est': None,
        'cash_available_cny': None,
        'cash_free_cny': None,
        'cash_required_cny': None,
        'mid': None,
        'bid': None,
        'ask': None,
        'option_ccy': None,
        'note': '无候选',
    }
    if df.empty:
        return row

    row['candidate_count'] = len(df)

    # Pick the top contract with a more execution-friendly preference:
    # prefer abs(delta) close to target, then higher annualized return, then net income.
    target_abs_delta = 0.22
    try:
        target_abs_delta = float((symbol_cfg.get('sell_put') or {}).get('target_abs_delta') or target_abs_delta)
    except Exception:
        pass

    d = df.copy()
    try:
        if 'delta' in d.columns:
            d['_abs_delta'] = d['delta'].abs()
            d['_delta_dist'] = (d['_abs_delta'] - target_abs_delta).abs()
        else:
            d['_delta_dist'] = 999.0
    except Exception:
        d['_delta_dist'] = 999.0

    top = d.sort_values(
        ['_delta_dist', 'annualized_net_return_on_cash_basis', 'net_income'],
        ascending=[True, False, False],
    ).iloc[0]

    cash_secured_used = 0.0
    cash_avail = None
    cash_free = None
    cash_avail_est = None
    cash_free_est = None
    cash_avail_cny = None
    cash_free_cny = None
    cash_required_cny = None
    try:
        if 'cash_secured_used_usd' in df.columns and len(df) > 0:
            cash_secured_used = float(df['cash_secured_used_usd'].iloc[0] or 0.0)
        if 'cash_available_usd' in df.columns and len(df) > 0 and pd.notna(df['cash_available_usd'].iloc[0]):
            cash_avail = float(df['cash_available_usd'].iloc[0])
        if 'cash_free_usd' in df.columns and len(df) > 0 and pd.notna(df['cash_free_usd'].iloc[0]):
            cash_free = float(df['cash_free_usd'].iloc[0])
        if 'cash_available_usd_est' in df.columns and len(df) > 0 and pd.notna(df['cash_available_usd_est'].iloc[0]):
            cash_avail_est = float(df['cash_available_usd_est'].iloc[0])
        if 'cash_free_usd_est' in df.columns and len(df) > 0 and pd.notna(df['cash_free_usd_est'].iloc[0]):
            cash_free_est = float(df['cash_free_usd_est'].iloc[0])
        if 'cash_available_cny' in df.columns and len(df) > 0 and pd.notna(df['cash_available_cny'].iloc[0]):
            cash_avail_cny = float(df['cash_available_cny'].iloc[0])
        if 'cash_free_cny' in df.columns and len(df) > 0 and pd.notna(df['cash_free_cny'].iloc[0]):
            cash_free_cny = float(df['cash_free_cny'].iloc[0])
        if 'cash_required_cny' in df.columns and len(df) > 0 and pd.notna(df['cash_required_cny'].iloc[0]):
            cash_required_cny = float(df['cash_required_cny'].iloc[0])
    except Exception:
        cash_secured_used = 0.0
        cash_avail = None
        cash_free = None
        cash_avail_est = None
        cash_free_est = None

    cash_required = None
    try:
        cash_required = float(top['strike']) * 100.0
    except Exception:
        cash_required = None

    row.update({
        'top_contract': f"{top['expiration']} {int(top['strike']) if float(top['strike']).is_integer() else top['strike']}P",
        'expiration': top['expiration'],
        'strike': float(top['strike']),
        'dte': int(top['dte']),
        'net_income': float(top['net_income']),
        'annualized_return': float(top['annualized_net_return_on_cash_basis']),
        'risk_label': top.get('risk_label', ''),
        'delta': (float(top['delta']) if 'delta' in top and pd.notna(top['delta']) else None),
        'iv': (float(top['implied_volatility']) if 'implied_volatility' in top and pd.notna(top['implied_volatility']) else None),
        'cash_secured_used_usd': cash_secured_used,
        'cash_required_usd': cash_required,
        'cash_available_usd': cash_avail,
        'cash_free_usd': cash_free,
        'cash_available_usd_est': cash_avail_est,
        'cash_free_usd_est': cash_free_est,
        'cash_available_cny': cash_avail_cny,
        'cash_free_cny': cash_free_cny,
        'cash_required_cny': cash_required_cny,
        'mid': (float(top['mid']) if 'mid' in top else None),
        'bid': (float(top['bid']) if 'bid' in top and pd.notna(top['bid']) else None),
        'ask': (float(top['ask']) if 'ask' in top and pd.notna(top['ask']) else None),
        'option_ccy': ('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
        'note': '有候选',
    })
    return row


def summarize_sell_call(df: pd.DataFrame, symbol: str, *, symbol_cfg: dict | None = None) -> dict[str, Any]:
    symbol_cfg = symbol_cfg or {}

    row: dict[str, Any] = {
        'symbol': symbol,
        'strategy': 'sell_call',
        'candidate_count': 0,
        'top_contract': '',
        'expiration': '',
        'strike': None,
        'dte': None,
        'net_income': None,
        'annualized_return': None,
        'risk_label': '',
        'delta': None,
        'iv': None,
        'mid': None,
        'bid': None,
        'ask': None,
        'option_ccy': None,
        'note': '无候选',
    }
    if df.empty:
        return row

    row['candidate_count'] = len(df)

    # Prefer delta close to a steady target, then higher premium return.
    target_delta = 0.28
    try:
        target_delta = float((symbol_cfg.get('sell_call') or {}).get('target_delta') or target_delta)
    except Exception:
        pass

    d = df.copy()
    try:
        if 'delta' in d.columns:
            d['_delta_dist'] = (d['delta'] - target_delta).abs()
        else:
            d['_delta_dist'] = 999.0
    except Exception:
        d['_delta_dist'] = 999.0

    top = d.sort_values(
        ['_delta_dist', 'annualized_net_premium_return', 'if_exercised_total_return', 'net_income'],
        ascending=[True, False, False, False],
    ).iloc[0]

    cover_avail = 0
    try:
        cover_avail = int(top.get('covered_contracts_available', 0) or 0)
    except Exception:
        cover_avail = 0

    row.update({
        'top_contract': f"{top['expiration']} {int(top['strike']) if float(top['strike']).is_integer() else top['strike']}C",
        'expiration': top['expiration'],
        'strike': float(top['strike']),
        'dte': int(top['dte']),
        'net_income': float(top['net_income']),
        'annualized_return': float(top['annualized_net_premium_return']),
        'risk_label': top.get('risk_label', ''),
        'delta': (float(top['delta']) if 'delta' in top and pd.notna(top['delta']) else None),
        'iv': (float(top['implied_volatility']) if 'implied_volatility' in top and pd.notna(top['implied_volatility']) else None),
        'mid': (float(top['mid']) if 'mid' in top else None),
        'bid': (float(top['bid']) if 'bid' in top and pd.notna(top['bid']) else None),
        'ask': (float(top['ask']) if 'ask' in top and pd.notna(top['ask']) else None),
        'option_ccy': ('HKD' if str(symbol).upper().endswith('.HK') else 'USD'),
        'note': f"有候选 | cover_avail {cover_avail} | shares_total {int(top.get('shares_total', 0) or 0)} | shares_locked {int(top.get('shares_locked', 0) or 0)}",
    })
    return row
