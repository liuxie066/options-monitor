"""Report summary helpers.

Stage 3 refactor target: make run_pipeline orchestration-only.

These functions are intentionally pure (DataFrame -> dict) and must not perform I/O.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from domain.domain.engine import rank_candidate_rows, rank_yield_enhancement_rows
from domain.domain.strategy_vocab import STRATEGY_COMBO_YIELD
from domain.domain.symbol_identity import symbol_currency


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
    'earnings_evidence_status': '',
    'earnings_has_event': False,
    'earnings_event_dates': '',
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
    'cash_requirement_unavailable_reason': None,
    'cash_secured_unavailable_reason': None,
    'term_matched_rv': None,
    'iv_rv_ratio': None,
    'iv_minus_rv': None,
    'abs_delta': None,
    'single_trade_concentration': None,
    'symbol_concentration_after': None,
    'total_short_put_concentration_after': None,
}

YIELD_ENHANCEMENT_EMPTY_FIELDS = {
    'structure_mode': None,
    'put_expiration': None,
    'put_dte': None,
    'call_expiration': None,
    'call_dte': None,
    'expiry_gap_days': None,
    'expiration_scope': None,
    'dte_scope': None,
    'put_contracts': None,
    'call_contracts': None,
    'put_strike': None,
    'call_strike': None,
    'call_candidate_count': None,
    'put_bid': None,
    'call_ask': None,
    'put_delta': None,
    'call_delta': None,
    'net_credit': None,
    'net_debit': None,
    'net_credit_yield': None,
    'annualized_net_credit_yield': None,
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
    'funding_accepted': None,
    'funding_reject_reasons': None,
    'put_net_credit': None,
    'call_total_cost': None,
    'combo_net_credit': None,
    'call_cost_to_put_credit': None,
    'upside_scenario_price': None,
    'upside_lift': None,
    'upside_net_lift': None,
    'upside_lift_to_call_cost': None,
    'upside_lift_to_put_credit': None,
    'premium_funding_score': None,
    'funding_score_components': None,
    'net_credit_retention': None,
    'strike_safety_margin_pct': None,
    'premium_edge_score': None,
    'cash_required_usd': None,
    'cash_required_cny': None,
    'max_leg_spread_ratio': None,
    'fee_basis': None,
}

def _empty_summary_row(symbol: str, strategy: str, *, extra_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        'symbol': symbol,
        'strategy': strategy,
        **COMMON_EMPTY_ROW,
        **(extra_fields or {}),
    }


def _option_ccy(symbol: str) -> str | None:
    return symbol_currency(symbol)


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


def _safe_int(value: Any) -> int | None:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except Exception:
        return None


def _safe_bool(value: Any) -> bool:
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y'}
    return bool(value)


def _safe_text(value: Any) -> str:
    try:
        if pd.isna(value):
            return ''
    except Exception:
        pass
    return str(value or '').strip()


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


def _strike_token(value: Any) -> str:
    strike = _safe_float(value)
    if strike is None:
        return ''
    return str(int(strike)) if float(strike).is_integer() else str(strike)


def _format_top_contract(top: pd.Series, suffix: str) -> str:
    expiration = _safe_text(top.get('expiration'))
    strike_token = _strike_token(top.get('strike'))
    if expiration and strike_token:
        return f"{expiration} {strike_token}{suffix}"
    return _safe_text(top.get('contract_symbol') or top.get('option_symbol') or top.get('top_contract'))


def _format_combo_contract(top: pd.Series) -> str:
    put_expiration = _safe_text(top.get('put_expiration') or top.get('expiration'))
    call_expiration = _safe_text(top.get('call_expiration') or top.get('expiration'))
    put_token = _strike_token(top.get('put_strike'))
    call_token = _strike_token(top.get('call_strike'))
    if put_expiration and call_expiration and put_token and call_token:
        if put_expiration != call_expiration:
            return f"{put_expiration} {put_token}P + {call_expiration} {call_token}C"
        return f"{put_expiration} {put_token}P+{call_token}C"
    return _safe_text(top.get('combo_contract') or top.get('contract_symbol') or top.get('top_contract'))


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
        'expiration': _safe_text(top.get('expiration')),
        'strike': _safe_float(top.get('strike')),
        'dte': _safe_int(top.get('dte')),
        'net_income': _safe_float(top.get('net_income')),
        'annualized_return': _safe_float(top.get(annualized_key)),
        'risk_label': _safe_text(top.get('risk_label')),
        'delta': _safe_float(top.get('delta')) if 'delta' in top else None,
        'iv': _safe_float(top.get('implied_volatility')) if 'implied_volatility' in top else None,
        'mid': _safe_float(top.get('mid')) if 'mid' in top else None,
        'bid': _safe_float(top.get('bid')) if 'bid' in top else None,
        'ask': _safe_float(top.get('ask')) if 'ask' in top else None,
        'option_ccy': _option_ccy(symbol),
        'note': note,
    })
    return row


def _first_top(df: pd.DataFrame) -> pd.Series | None:
    if df.empty:
        return None
    return df.iloc[0]


def _sell_put_extras(df: pd.DataFrame, top: pd.Series) -> dict[str, Any]:
    return {
        'earnings_evidence_status': _safe_text(top.get('earnings_evidence_status')),
        'earnings_has_event': _safe_bool(top.get('earnings_has_event')),
        'earnings_event_dates': _safe_text(top.get('earnings_event_dates')),
        'cash_secured_used_usd': _read_first_float(df, 'cash_secured_used_usd'),
        'cash_secured_used_usd_symbol': _read_first_float(df, 'cash_secured_used_usd_symbol'),
        'cash_secured_used_cny': _read_first_float(df, 'cash_secured_used_cny'),
        'cash_secured_used_cny_total': _read_first_float(df, 'cash_secured_used_cny_total'),
        'cash_secured_used_cny_symbol': _read_first_float(df, 'cash_secured_used_cny_symbol'),
        'cash_required_usd': _safe_float(top.get('cash_required_usd')),
        'cash_available_usd': _read_first_float(df, 'cash_available_usd'),
        'cash_free_usd': _read_first_float(df, 'cash_free_usd'),
        'cash_available_usd_est': _read_first_float(df, 'cash_available_usd_est'),
        'cash_free_usd_est': _read_first_float(df, 'cash_free_usd_est'),
        'cash_available_cny': _read_first_float(df, 'cash_available_cny'),
        'cash_free_cny': _read_first_float(df, 'cash_free_cny'),
        'cash_available_total_cny': _read_first_float(df, 'cash_available_total_cny'),
        'cash_free_total_cny': _read_first_float(df, 'cash_free_total_cny'),
        'cash_required_cny': _read_first_float(df, 'cash_required_cny'),
        'cash_requirement_unavailable_reason': top.get('cash_requirement_unavailable_reason'),
        'cash_secured_unavailable_reason': top.get('cash_secured_unavailable_reason'),
        'term_matched_rv': _safe_float(top.get('term_matched_rv')),
        'iv_rv_ratio': _safe_float(top.get('iv_rv_ratio')),
        'iv_minus_rv': _safe_float(top.get('iv_minus_rv')),
        'abs_delta': _safe_float(top.get('abs_delta')),
        'single_trade_concentration': _safe_float(top.get('single_trade_concentration')),
        'symbol_concentration_after': _safe_float(top.get('symbol_concentration_after')),
        'total_short_put_concentration_after': _safe_float(top.get('total_short_put_concentration_after')),
    }


def summarize_sell_put(df: pd.DataFrame, symbol: str, *, symbol_cfg: dict | None = None) -> dict[str, Any]:
    _ = symbol_cfg or {}
    row = _empty_summary_row(symbol, 'sell_put', extra_fields=SELL_PUT_EMPTY_FIELDS)
    if df.empty:
        return row

    ranked = rank_candidate_rows(df.to_dict("records"), mode="put")
    top = pd.Series(ranked[0]) if ranked else None
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

    ranked = rank_candidate_rows(df.to_dict("records"), mode="call")
    top = pd.Series(ranked[0]) if ranked else None
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
    row = _empty_summary_row(symbol, STRATEGY_COMBO_YIELD, extra_fields=YIELD_ENHANCEMENT_EMPTY_FIELDS)
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
        'annualized_return': _safe_float(top.get('annualized_net_credit_yield')),
        'risk_label': top.get('risk_label', ''),
        'delta': _safe_float(top.get('put_delta')),
        'iv': _safe_float(top.get('put_implied_volatility')),
        'mid': _safe_float(top.get('mid')),
        'bid': _safe_float(top.get('put_bid')),
        'ask': _safe_float(top.get('call_ask')),
        'option_ccy': top.get('option_ccy') or top.get('currency') or _option_ccy(symbol),
        'note': (
            'Put已独立通过接货、现金、事件、收益和流动性门槛'
            if str(top.get('structure_mode') or '').strip().lower() == 'staggered_expiry_pair'
            else '已按组合收益筛出推荐Call'
        ),
    })
    for key in YIELD_ENHANCEMENT_EMPTY_FIELDS:
        row[key] = top.get(key)
    return row
