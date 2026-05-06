"""Report summary helpers.

Stage 3 refactor target: make run_pipeline orchestration-only.

These functions are intentionally pure (DataFrame -> dict) and must not perform I/O.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.engine import rank_candidate_rows, rank_yield_enhancement_rows
from scripts.trade_symbol_identity import symbol_currency


COMMON_EMPTY_ROW = {
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

SELL_PUT_EMPTY_FIELDS = {
    'cash_secured_used_usd': 0.0,
    'cash_secured_used_usd_symbol': None,
    'cash_secured_used_cny': None,
    'cash_secured_used_cny_total': None,
    'cash_secured_used_cny_symbol': None,
    'cash_required_usd': None,
    'cash_available_usd': None,
    'cash_free_usd': None,
    'cash_available_usd_est': None,
    'cash_free_usd_est': None,
    'cash_available_cny': None,
    'cash_free_cny': None,
    'cash_available_total_cny': None,
    'cash_free_total_cny': None,
    'cash_required_cny': None,
    'linked_call_contract': None,
    'linked_call_contract_symbol': None,
    'linked_call_strike': None,
    'linked_call_ask': None,
    'linked_call_delta': None,
    'linked_call_iv': None,
    'linked_call_net_credit': None,
    'linked_call_expected_move': None,
    'linked_call_expected_move_iv': None,
    'linked_call_scenario_score': None,
    'linked_call_annualized_scenario_score': None,
    'linked_call_count': None,
}

YIELD_ENHANCEMENT_EMPTY_FIELDS = {
    'put_strike': None,
    'call_strike': None,
    'call_candidate_count': None,
    'put_bid': None,
    'call_ask': None,
    'put_delta': None,
    'call_delta': None,
    'net_credit': None,
    'net_debit': None,
    'funding_ratio': None,
    'cash_required': None,
    'downside_breakeven': None,
    'upside_breakeven': None,
    'max_loss_if_zero': None,
    'expected_move_iv': None,
    'expected_move': None,
    'scenario_score': None,
    'annualized_scenario_score': None,
    'put_otm_pct': None,
    'call_otm_pct': None,
    'gap_width_pct': None,
    'upside_breakeven_pct_above_spot': None,
    'combo_spread_ratio': None,
}

def _empty_summary_row(symbol: str, strategy: str, *, extra_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        'symbol': symbol,
        'strategy': strategy,
        **COMMON_EMPTY_ROW,
        **(extra_fields or {}),
    }


def _option_ccy(symbol: str) -> str:
    return symbol_currency(symbol) or 'USD'


def _safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


def _read_first_float(df: pd.DataFrame, column: str) -> float | None:
    try:
        if column not in df.columns or df.empty:
            return None
        value = df[column].iloc[0]
        if pd.notna(value):
            return float(value)
    except Exception:
        return None
    return None


def _format_top_contract(top: pd.Series, suffix: str) -> str:
    strike = float(top['strike'])
    strike_token = int(strike) if strike.is_integer() else strike
    return f"{top['expiration']} {strike_token}{suffix}"


def _format_combo_contract(top: pd.Series) -> str:
    put_strike = float(top['put_strike'])
    call_strike = float(top['call_strike'])
    put_token = int(put_strike) if put_strike.is_integer() else put_strike
    call_token = int(call_strike) if call_strike.is_integer() else call_strike
    return f"{top['expiration']} {put_token}P+{call_token}C"


def _build_ranked_row(
    *,
    symbol: str,
    strategy: str,
    df: pd.DataFrame,
    top: pd.Series,
    annualized_key: str,
    contract_suffix: str,
    note: str,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _empty_summary_row(symbol, strategy, extra_fields=extra_fields)
    row['candidate_count'] = len(df)
    row.update({
        'top_contract': _format_top_contract(top, contract_suffix),
        'expiration': top['expiration'],
        'strike': float(top['strike']),
        'dte': int(top['dte']),
        'net_income': float(top['net_income']),
        'annualized_return': float(top[annualized_key]),
        'risk_label': top.get('risk_label', ''),
        'delta': _safe_float(top.get('delta')) if 'delta' in top else None,
        'iv': _safe_float(top.get('implied_volatility')) if 'implied_volatility' in top else None,
        'mid': _safe_float(top.get('mid')) if 'mid' in top else None,
        'bid': _safe_float(top.get('bid')) if 'bid' in top else None,
        'ask': _safe_float(top.get('ask')) if 'ask' in top else None,
        'option_ccy': _option_ccy(symbol),
        'note': note,
    })
    return row


def _rank_top(df: pd.DataFrame, *, mode: str) -> pd.Series | None:
    ranked = rank_candidate_rows(df.to_dict('records'), mode=mode)
    if not ranked:
        return None
    return pd.Series(ranked[0])


def _sell_put_extras(df: pd.DataFrame, top: pd.Series) -> dict[str, Any]:
    cash_required_usd = None
    try:
        cash_required_usd = float(top['strike']) * 100.0
    except Exception:
        cash_required_usd = None

    return {
        'cash_secured_used_usd': (_read_first_float(df, 'cash_secured_used_usd') or 0.0),
        'cash_secured_used_usd_symbol': _read_first_float(df, 'cash_secured_used_usd_symbol'),
        'cash_secured_used_cny': _read_first_float(df, 'cash_secured_used_cny'),
        'cash_secured_used_cny_total': _read_first_float(df, 'cash_secured_used_cny_total'),
        'cash_secured_used_cny_symbol': _read_first_float(df, 'cash_secured_used_cny_symbol'),
        'cash_required_usd': cash_required_usd,
        'cash_available_usd': _read_first_float(df, 'cash_available_usd'),
        'cash_free_usd': _read_first_float(df, 'cash_free_usd'),
        'cash_available_usd_est': _read_first_float(df, 'cash_available_usd_est'),
        'cash_free_usd_est': _read_first_float(df, 'cash_free_usd_est'),
        'cash_available_cny': _read_first_float(df, 'cash_available_cny'),
        'cash_free_cny': _read_first_float(df, 'cash_free_cny'),
        'cash_available_total_cny': _read_first_float(df, 'cash_available_total_cny'),
        'cash_free_total_cny': _read_first_float(df, 'cash_free_total_cny'),
        'cash_required_cny': _read_first_float(df, 'cash_required_cny'),
        'linked_call_contract': top.get('linked_call_contract'),
        'linked_call_contract_symbol': top.get('linked_call_contract_symbol'),
        'linked_call_strike': _safe_float(top.get('linked_call_strike')),
        'linked_call_ask': _safe_float(top.get('linked_call_ask')),
        'linked_call_delta': _safe_float(top.get('linked_call_delta')),
        'linked_call_iv': _safe_float(top.get('linked_call_iv')),
        'linked_call_net_credit': _safe_float(top.get('linked_call_net_credit')),
        'linked_call_expected_move': _safe_float(top.get('linked_call_expected_move')),
        'linked_call_expected_move_iv': _safe_float(top.get('linked_call_expected_move_iv')),
        'linked_call_scenario_score': _safe_float(top.get('linked_call_scenario_score')),
        'linked_call_annualized_scenario_score': _safe_float(top.get('linked_call_annualized_scenario_score')),
        'linked_call_count': _safe_float(top.get('linked_call_count')),
    }


def summarize_sell_put(df: pd.DataFrame, symbol: str, *, symbol_cfg: dict | None = None) -> dict[str, Any]:
    _ = symbol_cfg or {}
    row = _empty_summary_row(symbol, 'sell_put', extra_fields=SELL_PUT_EMPTY_FIELDS)
    if df.empty:
        return row

    top = _rank_top(df, mode='put')
    if top is None:
        row['candidate_count'] = len(df)
        return row

    return _build_ranked_row(
        symbol=symbol,
        strategy='sell_put',
        df=df,
        top=top,
        annualized_key='annualized_net_return_on_cash_basis',
        contract_suffix='P',
        note='有候选',
        extra_fields=_sell_put_extras(df, top),
    )


def summarize_sell_call(df: pd.DataFrame, symbol: str, *, symbol_cfg: dict | None = None) -> dict[str, Any]:
    _ = symbol_cfg or {}
    row = _empty_summary_row(symbol, 'sell_call')
    if df.empty:
        return row

    top = _rank_top(df, mode='call')
    if top is None:
        row['candidate_count'] = len(df)
        return row

    try:
        cover_avail = int(top.get('covered_contracts_available', 0) or 0)
    except Exception:
        cover_avail = 0
    try:
        shares_total = int(top.get('shares_total', 0) or 0)
    except Exception:
        shares_total = 0
    try:
        shares_locked = int(top.get('shares_locked', 0) or 0)
    except Exception:
        shares_locked = 0

    return _build_ranked_row(
        symbol=symbol,
        strategy='sell_call',
        df=df,
        top=top,
        annualized_key='annualized_net_premium_return',
        contract_suffix='C',
        note=f'有候选 | cover_avail {cover_avail} | shares_total {shares_total} | shares_locked {shares_locked}',
    )


def summarize_yield_enhancement(df: pd.DataFrame, symbol: str, *, symbol_cfg: dict | None = None) -> dict[str, Any]:
    _ = symbol_cfg or {}
    row = _empty_summary_row(symbol, 'yield_enhancement', extra_fields=YIELD_ENHANCEMENT_EMPTY_FIELDS)
    if df.empty:
        return row

    ranked = rank_yield_enhancement_rows(df.to_dict('records'))
    if not ranked:
        row['candidate_count'] = len(df)
        return row

    top = pd.Series(ranked[0])
    row['candidate_count'] = len(df)
    dte = _safe_float(top.get('dte'))
    row.update({
        'top_contract': _format_combo_contract(top),
        'strike': _safe_float(top.get('put_strike')),
        'dte': int(dte) if dte is not None else None,
        'net_income': _safe_float(top.get('net_credit')),
        'annualized_return': _safe_float(top.get('annualized_scenario_score')),
        'risk_label': top.get('risk_label', ''),
        'delta': _safe_float(top.get('put_delta')),
        'iv': _safe_float(top.get('put_implied_volatility')),
        'mid': _safe_float(top.get('mid')),
        'bid': _safe_float(top.get('put_bid')),
        'ask': _safe_float(top.get('call_ask')),
        'option_ccy': top.get('option_ccy') or top.get('currency') or _option_ccy(symbol),
        'note': '已按组合收益筛出推荐Call',
    })
    for key in YIELD_ENHANCEMENT_EMPTY_FIELDS:
        row[key] = top.get(key)
    return row
