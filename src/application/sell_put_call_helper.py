"""Attach linked call suggestions to confirmed sell-put candidates."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.engine import (
    YieldEnhancementFundingDecision,
    YieldEnhancementLeg,
    compute_yield_enhancement_funding_decision,
    compute_yield_enhancement_metrics,
    rank_yield_enhancement_call_lottery_rows,
    rank_yield_enhancement_rows,
    rank_yield_enhancement_shadow_rows,
    validate_yield_enhancement_pair,
)
from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_LIQUIDITY,
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from domain.domain.fee_calc import calc_futu_option_fee
from domain.domain.combo_yield_identity import (
    combo_yield_pair_fingerprint,
    combo_yield_strategy_group_id,
)
from domain.domain.sell_put_risk_bands import classify_sell_put_risk
from domain.domain.symbol_identity import symbol_market
from src.application.candidate_models import CandidateContractInput
from src.application.strategy_policy import SELL_PUT_FAMILY, strategy_semantics_for_side_config
from src.application.yield_enhancement_config import derive_yield_enhancement_policy


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
    out = _safe_float(value)
    return int(out) if out is not None else None


def _merged_dict(*items: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            out.update(item)
    return out


def _format_contract(expiration: str, strike: float, option_suffix: str) -> str:
    token = int(strike) if float(strike).is_integer() else strike
    return f"{expiration} {token}{option_suffix}"


def _combo_pair_fingerprint(put_leg: YieldEnhancementLeg, call_leg: YieldEnhancementLeg) -> str:
    return combo_yield_pair_fingerprint(
        symbol=put_leg.symbol,
        put_contract_symbol=put_leg.contract_symbol,
        call_contract_symbol=call_leg.contract_symbol,
    )


def _expiration_gap_days(put_leg: YieldEnhancementLeg, call_leg: YieldEnhancementLeg) -> int | None:
    try:
        return (date.fromisoformat(call_leg.expiration) - date.fromisoformat(put_leg.expiration)).days
    except Exception:
        return None


def _spread_values(contract: CandidateContractInput) -> tuple[float | None, float | None]:
    bid = contract.bid
    ask = contract.ask
    mid = contract.mid
    if bid is None or ask is None or ask < bid:
        return None, None
    spread = ask - bid
    if mid is None or mid <= 0:
        return spread, None
    return spread, spread / mid


def _call_leg_from_required_data(row: pd.Series) -> YieldEnhancementLeg | None:
    contract = CandidateContractInput.from_row(row, mode="call")
    dte = contract.dte
    strike = contract.strike
    spot = contract.spot
    bid = contract.bid
    ask = contract.ask
    mid = contract.mid
    multiplier = contract.multiplier
    if None in (dte, strike, spot, bid, ask, mid, multiplier):
        return None
    if dte <= 0 or strike <= 0 or spot <= 0 or bid <= 0 or ask <= 0 or mid <= 0 or multiplier <= 0:
        return None
    spread, spread_ratio = _spread_values(contract)
    return YieldEnhancementLeg(
        symbol=contract.symbol,
        option_type="call",
        expiration=contract.expiration,
        contract_symbol=contract.contract_symbol,
        currency=contract.currency,
        dte=int(dte),
        strike=float(strike),
        spot=float(spot),
        bid=float(bid),
        ask=float(ask),
        mid=float(mid),
        multiplier=float(multiplier),
        open_interest=contract.open_interest,
        volume=contract.volume,
        implied_volatility=contract.implied_volatility,
        delta=contract.delta,
        spread=spread,
        spread_ratio=spread_ratio,
    )


def _put_leg_from_sell_put_row(row: pd.Series) -> YieldEnhancementLeg | None:
    contract_symbol = str(row.get("contract_symbol") or "").strip()
    expiration = str(row.get("expiration") or "").strip()
    currency = str(row.get("currency") or row.get("option_ccy") or "").strip().upper()
    symbol = str(row.get("symbol") or "").strip().upper()
    dte = _safe_int(row.get("dte"))
    strike = _safe_float(row.get("strike"))
    spot = _safe_float(row.get("spot"))
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    mid = _safe_float(row.get("mid"))
    multiplier = _safe_float(row.get("multiplier"))
    if not contract_symbol or not expiration or not currency or not symbol:
        return None
    if None in (dte, strike, spot, bid, ask, mid, multiplier):
        return None
    if dte <= 0 or strike <= 0 or spot <= 0 or bid <= 0 or ask <= 0 or mid <= 0 or multiplier <= 0:
        return None
    spread = ask - bid if ask >= bid else None
    spread_ratio = (spread / mid) if spread is not None and mid > 0 else None
    return YieldEnhancementLeg(
        symbol=symbol,
        option_type="put",
        expiration=expiration,
        contract_symbol=contract_symbol,
        currency=currency,
        dte=int(dte),
        strike=float(strike),
        spot=float(spot),
        bid=float(bid),
        ask=float(ask),
        mid=float(mid),
        multiplier=float(multiplier),
        open_interest=_safe_float(row.get("open_interest")),
        volume=_safe_float(row.get("volume")),
        implied_volatility=_safe_float(row.get("implied_volatility")),
        delta=_safe_float(row.get("delta")),
        spread=spread,
        spread_ratio=spread_ratio,
    )


def _passes_range(value: float, min_value: float | None, max_value: float | None) -> bool:
    if min_value is not None and value < float(min_value):
        return False
    if max_value is not None and value > float(max_value):
        return False
    return True


def _liquidity_reject_reason(
    leg: YieldEnhancementLeg,
    *,
    min_open_interest: float,
    min_volume: float,
    max_spread_ratio: float | None,
) -> str | None:
    oi = _safe_float(leg.open_interest) or 0.0
    volume = _safe_float(leg.volume) or 0.0
    spread_ratio = _safe_float(leg.spread_ratio)
    if oi < float(min_open_interest):
        return "call_open_interest_below_min"
    if volume < float(min_volume):
        return "call_volume_below_min"
    if max_spread_ratio is not None and spread_ratio is not None and spread_ratio > float(max_spread_ratio):
        return "call_spread_ratio_above_max"
    return None


def _normalized_iv(*values: Any) -> float | None:
    parsed: list[float] = []
    for value in values:
        out = _safe_float(value)
        if out is None or out <= 0:
            continue
        if out > 3.0:
            out = out / 100.0
        if out > 0:
            parsed.append(float(out))
    if not parsed:
        return None
    return sum(parsed) / float(len(parsed))


def _funding_decision_row_fields(decision: YieldEnhancementFundingDecision) -> dict[str, Any]:
    components = ";".join(
        f"{name}={value:.6f}"
        for name, value in sorted(decision.score_components.items())
    )
    net_credit_retention = None
    if decision.put_net_credit > 0:
        net_credit_retention = decision.combo_net_credit / decision.put_net_credit
    return {
        "funding_accepted": bool(decision.accepted),
        "funding_reject_reasons": "|".join(decision.reject_reasons),
        "put_net_credit": decision.put_net_credit,
        "call_total_cost": decision.call_total_cost,
        "combo_net_credit": decision.combo_net_credit,
        "net_credit_yield": decision.net_credit_yield,
        "annualized_net_credit_yield": decision.annualized_net_credit_yield,
        "net_credit_retention": round(float(net_credit_retention), 6) if net_credit_retention is not None else None,
        "call_cost_to_put_credit": decision.call_cost_ratio,
        "upside_scenario_price": decision.upside_scenario_price,
        "upside_lift": decision.upside_lift,
        "upside_net_lift": decision.upside_net_lift,
        "upside_lift_to_call_cost": decision.upside_lift_to_call_cost,
        "upside_lift_to_put_credit": decision.upside_lift_to_put_credit,
        "premium_funding_score": decision.premium_funding_score,
        "funding_score_components": components,
    }


def _build_pair_row(
    *,
    put_leg: YieldEnhancementLeg,
    call_leg: YieldEnhancementLeg,
    expected_move_iv: float | None,
    min_combo_notional_floor: float,
    enhancement_cfg: dict[str, Any],
    sell_put_cfg: dict[str, Any] | None,
    account: str | None = None,
) -> dict[str, Any]:
    expiry_structure = str(enhancement_cfg.get("expiry_structure") or "same_expiry").strip().lower()
    min_expiry_gap_days = int(_safe_int(enhancement_cfg.get("min_expiry_gap_days")) or 1)
    multiplier = int(put_leg.multiplier)
    put_sell_fee = calc_futu_option_fee(put_leg.currency, put_leg.bid, contracts=1, multiplier=multiplier, is_sell=True)
    call_buy_fee = calc_futu_option_fee(call_leg.currency, call_leg.ask, contracts=1, multiplier=multiplier, is_sell=False)
    metrics = compute_yield_enhancement_metrics(
        put_leg=put_leg,
        call_leg=call_leg,
        put_sell_fee=put_sell_fee,
        call_buy_fee=call_buy_fee,
        expected_move_iv=expected_move_iv,
        min_combo_notional_floor=min_combo_notional_floor,
        expiry_structure=expiry_structure,
        min_expiry_gap_days=min_expiry_gap_days,
    )
    risk = classify_sell_put_risk(metrics.put_otm_pct)
    max_strike = _safe_float((sell_put_cfg or {}).get("max_strike")) if isinstance(sell_put_cfg, dict) else None
    put_assignment_margin_pct = None
    if max_strike is not None and max_strike > 0:
        put_assignment_margin_pct = (max_strike - put_leg.strike) / max_strike
    expiry_gap_days = _expiration_gap_days(put_leg, call_leg)
    fingerprint = _combo_pair_fingerprint(put_leg, call_leg)
    normalized_account = str(account or "").strip().lower()
    row = {
        "symbol": put_leg.symbol,
        "expiration": put_leg.expiration,
        "dte": put_leg.dte,
        "expiry_structure": expiry_structure,
        "put_expiration": put_leg.expiration,
        "call_expiration": call_leg.expiration,
        "put_dte": put_leg.dte,
        "call_dte": call_leg.dte,
        "expiry_gap_days": expiry_gap_days,
        "terminal_metrics_status": (
            "not_evaluable_diagonal" if expiry_structure == "diagonal" else "evaluable_same_expiry"
        ),
        "terminal_metrics_unsupported_reason": (
            "future_call_residual_value_not_modeled" if expiry_structure == "diagonal" else None
        ),
        "combo_pair_fingerprint": fingerprint,
        "spot": put_leg.spot,
        "currency": put_leg.currency,
        "option_ccy": put_leg.currency,
        "multiplier": put_leg.multiplier,
        "put_contract_symbol": put_leg.contract_symbol,
        "put_strike": put_leg.strike,
        "put_bid": put_leg.bid,
        "put_ask": put_leg.ask,
        "put_mid": put_leg.mid,
        "put_delta": put_leg.delta,
        "put_implied_volatility": put_leg.implied_volatility,
        "put_open_interest": put_leg.open_interest,
        "put_volume": put_leg.volume,
        "put_spread_ratio": put_leg.spread_ratio,
        "call_contract_symbol": call_leg.contract_symbol,
        "call_strike": call_leg.strike,
        "call_bid": call_leg.bid,
        "call_ask": call_leg.ask,
        "call_mid": call_leg.mid,
        "call_delta": call_leg.delta,
        "call_implied_volatility": call_leg.implied_volatility,
        "call_open_interest": call_leg.open_interest,
        "call_volume": call_leg.volume,
        "call_spread_ratio": call_leg.spread_ratio,
        "put_sell_fee": put_sell_fee,
        "call_buy_fee": call_buy_fee,
        "net_credit": metrics.net_credit,
        "net_debit": metrics.net_debit,
        "put_only_net_credit": metrics.put_only_net_credit,
        "net_credit_yield": metrics.net_credit_yield,
        "annualized_net_credit_yield": metrics.annualized_net_credit_yield,
        "funding_ratio": metrics.funding_ratio,
        "net_income": metrics.net_credit,
        "cash_required": metrics.cash_required,
        "put_only_breakeven": metrics.put_only_breakeven,
        "combo_breakeven": metrics.combo_breakeven,
        "downside_breakeven_penalty": metrics.downside_breakeven_penalty,
        "lottery_budget_ratio": metrics.lottery_budget_ratio,
        "residual_premium_ratio": metrics.residual_premium_ratio,
        "downside_breakeven": metrics.downside_breakeven,
        "upside_breakeven": metrics.upside_breakeven,
        "max_loss_if_zero": metrics.max_loss_if_zero,
        "annualized_return": metrics.annualized_net_credit_yield,
        "expected_move_iv": metrics.expected_move_iv,
        "expected_move": metrics.expected_move,
        "call_payoff_multiple_at_1_5_sigma": metrics.call_payoff_multiple_at_1_5_sigma,
        "call_payoff_multiple_at_2_0_sigma": metrics.call_payoff_multiple_at_2_0_sigma,
        "scenario_score": metrics.scenario_score,
        "annualized_scenario_score": metrics.annualized_scenario_score,
        "put_otm_pct": metrics.put_otm_pct,
        "call_otm_pct": metrics.call_otm_pct,
        "put_assignment_margin_pct": round(float(put_assignment_margin_pct), 6) if put_assignment_margin_pct is not None else None,
        "gap_width_pct": metrics.gap_width_pct,
        "upside_breakeven_pct_above_spot": metrics.upside_breakeven_pct_above_spot,
        "combo_spread_ratio": metrics.combo_spread_ratio,
        "strike": put_leg.strike,
        "mid": metrics.net_credit / put_leg.multiplier,
        "bid": put_leg.bid,
        "ask": call_leg.ask,
        "delta": put_leg.delta,
        "iv": put_leg.implied_volatility,
        "risk_label": risk.risk_label,
    }
    funding_mode = str(enhancement_cfg.get("funding_mode") or "credit_or_even").strip().lower()
    min_combo_net_credit = _safe_float(enhancement_cfg.get("min_combo_net_credit"))
    if funding_mode == "max_debit":
        min_combo_net_credit = None
    max_call_cost_to_put_credit = _safe_float(enhancement_cfg.get("max_call_cost_to_put_credit"))
    if funding_mode == "max_debit" and not bool(enhancement_cfg.get("_max_call_cost_to_put_credit_explicit")):
        max_call_cost_to_put_credit = None
    decision = compute_yield_enhancement_funding_decision(
        put_leg=put_leg,
        call_leg=call_leg,
        put_sell_fee=put_sell_fee,
        call_buy_fee=call_buy_fee,
        combo_metrics=metrics,
        min_combo_net_credit=min_combo_net_credit,
        min_net_credit_annualized=_safe_float(enhancement_cfg.get("min_net_credit_annualized")),
        max_call_cost_to_put_credit=max_call_cost_to_put_credit,
        max_combo_spread_ratio=_safe_float(enhancement_cfg.get("max_combo_spread_ratio")),
        expiry_structure=expiry_structure,
        min_expiry_gap_days=min_expiry_gap_days,
    )
    row.update(_funding_decision_row_fields(decision))
    row["strategy_group_id"] = combo_yield_strategy_group_id(
        account=normalized_account,
        pair_fingerprint=fingerprint,
    )
    return row


def _empty_pairs_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "symbol",
            "expiration",
            "dte",
            "expiry_structure",
            "put_expiration",
            "call_expiration",
            "put_dte",
            "call_dte",
            "expiry_gap_days",
            "terminal_metrics_status",
            "terminal_metrics_unsupported_reason",
            "combo_pair_fingerprint",
            "strategy_group_id",
            "put_contract_symbol",
            "call_contract_symbol",
            "call_strike",
            "call_ask",
            "net_credit",
            "put_only_net_credit",
            "net_credit_yield",
            "annualized_net_credit_yield",
            "expected_move_iv",
            "expected_move",
            "put_only_breakeven",
            "combo_breakeven",
            "downside_breakeven_penalty",
            "lottery_budget_ratio",
            "residual_premium_ratio",
            "call_payoff_multiple_at_1_5_sigma",
            "call_payoff_multiple_at_2_0_sigma",
            "scenario_score",
            "annualized_scenario_score",
            "call_candidate_count",
            "funding_accepted",
            "funding_reject_reasons",
            "put_net_credit",
            "call_total_cost",
            "combo_net_credit",
            "net_credit_retention",
            "call_cost_to_put_credit",
            "upside_scenario_price",
            "upside_lift",
            "upside_net_lift",
            "upside_lift_to_call_cost",
            "upside_lift_to_put_credit",
            "premium_funding_score",
            "funding_score_components",
            "yield_enhancement_mode",
            "derived_from_sell_put_strategy",
            "put_strategy_profile",
            "put_strategy_source",
            "put_risk_model",
        ]
    )


def _sell_put_strategy_fields(sell_put_cfg: dict[str, Any] | None) -> dict[str, Any]:
    semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sell_put_cfg)
    source = "current_config" if isinstance(sell_put_cfg, dict) and (
        "strategy" in sell_put_cfg or "strategy_profile" in sell_put_cfg
    ) else "template_default"
    return {
        "put_strategy_profile": semantics.strategy_profile,
        "put_strategy_source": source,
        "put_risk_model": semantics.risk_model,
    }


def _put_risk_fields(row: pd.Series) -> dict[str, Any]:
    fields = (
        "funding_put_eligible",
        "funding_put_min_annualized_return",
        "put_only_annualized_net_return",
        "annualized_net_return_on_cash_basis",
        "short_vol_thesis_status",
        "short_vol_reason",
        "short_vol_mode",
        "short_gamma_profile",
        "short_vega_profile",
        "implied_volatility",
        "realized_volatility_estimate",
        "iv_rv_ratio",
        "iv_minus_rv",
        "abs_delta",
        "equity_delta_equivalent",
        "delta_target_score",
        "vol_edge_score",
        "event_risk_flag",
        "event_risk_types",
        "event_risk_dates",
        "event_source_status",
        "event_source_error",
        "path_stress_status",
        "path_stress_evaluable",
        "path_stress_unavailable_reason",
        "stress_sigma_move_pct",
        "put_stress_down_loss_nav_pct",
        "put_gap_down_loss_nav_pct",
        "data_quality_flags",
    )
    return {key: row.get(key) for key in fields if key in row}


def _put_leg_passes_assignment_bounds(put_leg: YieldEnhancementLeg, sell_put_cfg: dict[str, Any] | None) -> bool:
    cfg = sell_put_cfg if isinstance(sell_put_cfg, dict) else {}
    min_strike = _safe_float(cfg.get("min_strike"))
    max_strike = _safe_float(cfg.get("max_strike"))
    effective_max_strike = min(value for value in (max_strike, put_leg.spot) if value is not None)
    if min_strike is not None and put_leg.strike < min_strike:
        return False
    return put_leg.strike <= effective_max_strike


def _load_required_data_calls(*, input_root: Path, symbol: str) -> pd.DataFrame:
    path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if df.empty or "option_type" not in df.columns:
        return pd.DataFrame()
    mask = df["option_type"].astype(str).str.strip().str.lower() == "call"
    return df.loc[mask].copy()


def _write_pairs_df(pairs_df: pd.DataFrame, output_path: Path | None) -> None:
    if output_path is None:
        return
    try:
        pairs_df.to_csv(output_path, index=False)
    except Exception:
        pass


_PAIR_DIAGNOSTIC_COLUMNS = (
    "run_id account diagnostic_scope diagnostic_stage accepted reject_reasons "
    "symbol expiration dte expiry_structure put_expiration call_expiration put_dte call_dte expiry_gap_days "
    "terminal_metrics_status terminal_metrics_unsupported_reason combo_pair_fingerprint strategy_group_id "
    "spot currency multiplier "
    "put_contract_symbol put_strike put_bid put_ask put_mid put_delta put_open_interest put_volume put_spread_ratio "
    "call_contract_symbol call_strike call_bid call_ask call_mid call_delta call_open_interest call_volume "
    "call_spread_ratio put_only_net_credit put_net_credit call_total_cost combo_net_credit net_credit net_debit "
    "net_credit_retention call_cost_to_put_credit annualized_net_credit_yield combo_spread_ratio funding_accepted "
    "funding_reject_reasons expected_move lottery_budget_ratio residual_premium_ratio "
    "call_payoff_multiple_at_1_5_sigma call_payoff_multiple_at_2_0_sigma funding_put_min_annualized_return "
    "put_only_annualized_net_return yield_enhancement_mode put_strategy_profile "
    "policy_call_min_delta policy_call_max_delta policy_call_min_strike policy_call_max_strike "
    "policy_call_min_open_interest policy_call_min_volume policy_call_max_spread_ratio policy_funding_mode "
    "policy_max_debit_native policy_min_net_credit_retention policy_min_net_credit_annualized "
    "policy_max_combo_spread_ratio"
).split()


def _leg_diagnostic_fields(leg: YieldEnhancementLeg, *, prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_expiration": leg.expiration,
        f"{prefix}_dte": leg.dte,
        f"{prefix}_contract_symbol": leg.contract_symbol,
        f"{prefix}_strike": leg.strike,
        f"{prefix}_bid": leg.bid,
        f"{prefix}_ask": leg.ask,
        f"{prefix}_mid": leg.mid,
        f"{prefix}_delta": leg.delta,
        f"{prefix}_open_interest": leg.open_interest,
        f"{prefix}_volume": leg.volume,
        f"{prefix}_spread_ratio": leg.spread_ratio,
    }


def _diagnostic_row(
    *,
    scope: str,
    stage: str,
    accepted: bool,
    reject_reasons: tuple[str, ...] = (),
    put_leg: YieldEnhancementLeg | None = None,
    call_leg: YieldEnhancementLeg | None = None,
    candidate: dict[str, Any] | None = None,
    raw_call: pd.Series | None = None,
) -> dict[str, Any]:
    row = dict(candidate or {})
    source_leg = put_leg or call_leg
    if source_leg is not None:
        row.setdefault("symbol", source_leg.symbol)
        row.setdefault("expiration", source_leg.expiration)
        row.setdefault("dte", source_leg.dte)
        row.setdefault("spot", source_leg.spot)
        row.setdefault("currency", source_leg.currency)
        row.setdefault("multiplier", source_leg.multiplier)
    if put_leg is not None:
        for key, value in _leg_diagnostic_fields(put_leg, prefix="put").items():
            row.setdefault(key, value)
    if call_leg is not None:
        for key, value in _leg_diagnostic_fields(call_leg, prefix="call").items():
            row.setdefault(key, value)
    if raw_call is not None:
        row.setdefault("symbol", raw_call.get("symbol"))
        row.setdefault("expiration", raw_call.get("expiration"))
        row.setdefault("dte", raw_call.get("dte"))
        row.setdefault("spot", raw_call.get("spot"))
        row.setdefault("currency", raw_call.get("currency") or raw_call.get("option_ccy"))
        row.setdefault("multiplier", raw_call.get("multiplier"))
        for source, target in (
            ("contract_symbol", "call_contract_symbol"),
            ("strike", "call_strike"),
            ("bid", "call_bid"),
            ("ask", "call_ask"),
            ("mid", "call_mid"),
            ("delta", "call_delta"),
            ("open_interest", "call_open_interest"),
            ("volume", "call_volume"),
            ("spread_ratio", "call_spread_ratio"),
        ):
            row.setdefault(target, raw_call.get(source))
    row.update(
        {
            "diagnostic_scope": scope,
            "diagnostic_stage": stage,
            "accepted": bool(accepted),
            "reject_reasons": "|".join(dict.fromkeys(reject_reasons)),
        }
    )
    return row


def _pair_diagnostics_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_PAIR_DIAGNOSTIC_COLUMNS)
    diagnostics = pd.DataFrame(rows)
    columns = [
        *_PAIR_DIAGNOSTIC_COLUMNS,
        *(column for column in diagnostics.columns if column not in _PAIR_DIAGNOSTIC_COLUMNS),
    ]
    return diagnostics.reindex(columns=columns)


def get_yield_enhancement_pair_diagnostics(pairs_df: pd.DataFrame) -> pd.DataFrame:
    diagnostics = pairs_df.attrs.get("pair_diagnostics")
    if isinstance(diagnostics, pd.DataFrame):
        return diagnostics.copy()
    return _pair_diagnostics_df([])


def _load_yield_enhancement_call_legs_by_expiration(
    *,
    input_root: Path,
    symbol: str,
    call_cfg: dict[str, Any],
    window: Any,
    liquidity: Any,
    diagnostics: list[dict[str, Any]],
) -> tuple[dict[str, list[YieldEnhancementLeg]], Counter[str]]:
    raw_calls = _load_required_data_calls(input_root=input_root, symbol=symbol)
    call_legs_by_expiration: dict[str, list[YieldEnhancementLeg]] = {}
    reject_counts: Counter[str] = Counter()
    if raw_calls.empty:
        reject_counts["call_universe_empty"] += 1
        diagnostics.append(
            _diagnostic_row(
                scope="call",
                stage="call_filter",
                accepted=False,
                reject_reasons=("call_universe_empty",),
                candidate={"symbol": symbol},
            )
        )
        return call_legs_by_expiration, reject_counts
    min_call_delta = _safe_float(call_cfg.get("min_delta"))
    max_call_delta = _safe_float(call_cfg.get("max_delta"))
    configured_min_strike = _safe_float(call_cfg.get("min_strike"))
    configured_max_strike = _safe_float(call_cfg.get("max_strike"))
    diagnostic_policy = {
        "policy_call_min_delta": min_call_delta,
        "policy_call_max_delta": max_call_delta,
        "policy_call_min_strike": configured_min_strike,
        "policy_call_max_strike": configured_max_strike,
        "policy_call_min_open_interest": liquidity.min_open_interest,
        "policy_call_min_volume": liquidity.min_volume,
        "policy_call_max_spread_ratio": liquidity.max_spread_ratio,
    }
    for _, raw in raw_calls.iterrows():
        leg = _call_leg_from_required_data(raw)
        if leg is None:
            reject_counts["call_leg_invalid"] += 1
            diagnostics.append(
                _diagnostic_row(
                    scope="call",
                    stage="call_filter",
                    accepted=False,
                    reject_reasons=("call_leg_invalid",),
                    candidate=diagnostic_policy,
                    raw_call=raw,
                )
            )
            continue
        reject_reason: str | None = None
        if not _passes_range(leg.dte, int(window.min_dte), int(window.max_dte)):
            reject_reason = "call_dte_out_of_range"
        effective_min_strike = max(value for value in (configured_min_strike, leg.spot) if value is not None)
        if reject_reason is None and leg.strike < effective_min_strike:
            reject_reason = "call_strike_below_min"
        if reject_reason is None and configured_max_strike is not None and leg.strike > configured_max_strike:
            reject_reason = "call_strike_above_max"
        call_delta = _safe_float(leg.delta)
        if reject_reason is None and call_delta is None and (min_call_delta is not None or max_call_delta is not None):
            reject_reason = "call_delta_missing"
        if reject_reason is None and min_call_delta is not None and call_delta < float(min_call_delta):
            reject_reason = "call_delta_below_min"
        if reject_reason is None and max_call_delta is not None and call_delta > float(max_call_delta):
            reject_reason = "call_delta_above_max"
        if reject_reason is None:
            reject_reason = _liquidity_reject_reason(
                leg,
                min_open_interest=liquidity.min_open_interest,
                min_volume=liquidity.min_volume,
                max_spread_ratio=liquidity.max_spread_ratio,
            )
        if reject_reason:
            reject_counts[reject_reason] += 1
            diagnostics.append(
                _diagnostic_row(
                    scope="call",
                    stage="call_filter",
                    accepted=False,
                    reject_reasons=(reject_reason,),
                    call_leg=leg,
                    candidate=diagnostic_policy,
                )
            )
            continue
        diagnostics.append(
            _diagnostic_row(
                scope="call",
                stage="call_filter",
                accepted=True,
                call_leg=leg,
                candidate=diagnostic_policy,
            )
        )
        call_legs_by_expiration.setdefault(leg.expiration, []).append(leg)
    return call_legs_by_expiration, reject_counts


def _candidate_pair_reject_reasons(
    candidate: dict[str, Any],
    *,
    funding_mode: str,
    max_debit_native: float | None,
    min_net_credit_retention: float | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not bool(candidate.get("funding_accepted")):
        reasons.extend(
            reason
            for reason in str(candidate.get("funding_reject_reasons") or "").split("|")
            if reason
        )
        if not reasons:
            reasons.append("funding_rejected")
    if funding_mode == "credit_or_even" and float(candidate["net_credit"]) < 0:
        reasons.append("funding_mode_credit_or_even")
    if funding_mode == "max_debit" and max_debit_native is not None and float(candidate["net_debit"]) > float(max_debit_native):
        reasons.append("max_debit_native")
    net_credit_retention = _safe_float(candidate.get("net_credit_retention"))
    if min_net_credit_retention is not None and (
        net_credit_retention is None or net_credit_retention < float(min_net_credit_retention)
    ):
        reasons.append("min_net_credit_retention")
    return tuple(dict.fromkeys(reasons))


def _build_yield_enhancement_pair_rows(
    *,
    df: pd.DataFrame,
    call_legs_by_expiration: dict[str, list[YieldEnhancementLeg]],
    window: Any,
    sell_put_cfg: dict[str, Any] | None,
    min_combo_notional_floor: float,
    cfg: dict[str, Any],
    put_strategy_fields: dict[str, Any],
    policy_fields: dict[str, Any],
    funding_mode: str,
    max_debit_native: float | None,
    min_net_credit_retention: float | None,
    reject_counts: Counter[str],
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pair_rows: list[dict[str, Any]] = []
    expiry_structure = str(cfg.get("expiry_structure") or "same_expiry").strip().lower()
    min_expiry_gap_days = int(_safe_int(cfg.get("min_expiry_gap_days")) or 1)
    diagnostic_policy = {
        "policy_funding_mode": funding_mode,
        "policy_max_debit_native": max_debit_native,
        "policy_min_net_credit_retention": min_net_credit_retention,
        "policy_min_net_credit_annualized": _safe_float(cfg.get("min_net_credit_annualized")),
        "policy_max_combo_spread_ratio": _safe_float(cfg.get("max_combo_spread_ratio")),
    }
    for _, raw in df.iterrows():
        put_leg = _put_leg_from_sell_put_row(raw)
        if put_leg is None:
            reject_counts["put_leg_invalid"] += 1
            diagnostics.append(
                _diagnostic_row(
                    scope="put",
                    stage="put_filter",
                    accepted=False,
                    reject_reasons=("put_leg_invalid",),
                    candidate={
                        "symbol": raw.get("symbol"),
                        "expiration": raw.get("expiration"),
                        "dte": raw.get("dte"),
                        "put_contract_symbol": raw.get("contract_symbol"),
                        "put_strike": raw.get("strike"),
                    },
                )
            )
            continue
        if not _passes_range(put_leg.dte, int(window.min_dte), int(window.max_dte)):
            reject_counts["put_dte_out_of_range"] += 1
            diagnostics.append(
                _diagnostic_row(
                    scope="put",
                    stage="put_filter",
                    accepted=False,
                    reject_reasons=("put_dte_out_of_range",),
                    put_leg=put_leg,
                )
            )
            continue
        if not _put_leg_passes_assignment_bounds(put_leg, sell_put_cfg):
            reject_counts["put_assignment_bounds"] += 1
            diagnostics.append(
                _diagnostic_row(
                    scope="put",
                    stage="put_filter",
                    accepted=False,
                    reject_reasons=("put_assignment_bounds",),
                    put_leg=put_leg,
                )
            )
            continue

        if expiry_structure == "diagonal":
            call_legs = [
                leg
                for expiration in sorted(call_legs_by_expiration)
                for leg in call_legs_by_expiration[expiration]
            ]
        else:
            call_legs = call_legs_by_expiration.get(put_leg.expiration, [])
        if not call_legs:
            unavailable_reason = (
                "later_call_expiration_unavailable"
                if expiry_structure == "diagonal"
                else "call_expiration_unavailable"
            )
            reject_counts[unavailable_reason] += 1
            diagnostics.append(
                _diagnostic_row(
                    scope="put",
                    stage="pair_join",
                    accepted=False,
                    reject_reasons=(unavailable_reason,),
                    put_leg=put_leg,
                )
            )
            continue
        for call_leg in call_legs:
            pair_rejects = validate_yield_enhancement_pair(
                put_leg,
                call_leg,
                expiry_structure=expiry_structure,
                min_expiry_gap_days=min_expiry_gap_days,
            )
            if pair_rejects:
                reject_counts.update(pair_rejects)
                diagnostics.append(
                    _diagnostic_row(
                        scope="pair",
                        stage="pair_structure",
                        accepted=False,
                        reject_reasons=pair_rejects,
                        put_leg=put_leg,
                        call_leg=call_leg,
                        candidate=diagnostic_policy,
                    )
                )
                continue
            expected_iv = _normalized_iv(put_leg.implied_volatility, call_leg.implied_volatility)
            try:
                candidate = _build_pair_row(
                    put_leg=put_leg,
                    call_leg=call_leg,
                    expected_move_iv=expected_iv,
                    min_combo_notional_floor=min_combo_notional_floor,
                    enhancement_cfg=cfg,
                    sell_put_cfg=sell_put_cfg,
                    account=(
                        None
                        if pd.isna(raw.get("account"))
                        else str(raw.get("account") or "").strip() or None
                    ),
                )
            except Exception:
                reject_counts["pair_metrics_error"] += 1
                diagnostics.append(
                    _diagnostic_row(
                        scope="pair",
                        stage="pair_metrics",
                        accepted=False,
                        reject_reasons=("pair_metrics_error",),
                        put_leg=put_leg,
                        call_leg=call_leg,
                        candidate=diagnostic_policy,
                    )
                )
                continue
            candidate.update(put_strategy_fields)
            candidate.update(policy_fields)
            candidate.update(_put_risk_fields(raw))
            pair_rejects = _candidate_pair_reject_reasons(
                candidate,
                funding_mode=funding_mode,
                max_debit_native=max_debit_native,
                min_net_credit_retention=min_net_credit_retention,
            )
            if pair_rejects:
                reject_counts.update(pair_rejects)
                diagnostics.append(
                    _diagnostic_row(
                        scope="pair",
                        stage="pair_filter",
                        accepted=False,
                        reject_reasons=pair_rejects,
                        put_leg=put_leg,
                        call_leg=call_leg,
                        candidate={**candidate, **diagnostic_policy},
                    )
                )
                continue
            diagnostics.append(
                _diagnostic_row(
                    scope="pair",
                    stage="pair_filter",
                    accepted=True,
                    put_leg=put_leg,
                    call_leg=call_leg,
                    candidate={**candidate, **diagnostic_policy},
                )
            )
            pair_rows.append(candidate)
    return pair_rows


def find_sell_put_yield_enhancement_pairs(
    *,
    df_candidates: pd.DataFrame,
    symbol: str,
    input_root: Path,
    yield_enhancement_cfg: dict[str, Any] | None,
    sell_put_cfg: dict[str, Any] | None = None,
    global_yield_enhancement_liquidity: dict[str, Any] | None = None,
    output_path: Path | None = None,
) -> pd.DataFrame:
    df = df_candidates.copy()
    policy = derive_yield_enhancement_policy(
        yield_enhancement_cfg,
        sell_put_cfg,
        market=symbol_market(symbol),
    )
    cfg = policy.to_config()
    cfg["_max_call_cost_to_put_credit_explicit"] = "max_call_cost_to_put_credit" in set(policy.explicit_fields)
    diagnostics: list[dict[str, Any]] = []
    if df.empty or not policy.enabled:
        pairs_df = _empty_pairs_df()
        pairs_df.attrs["pair_diagnostics"] = _pair_diagnostics_df(diagnostics)
        _write_pairs_df(pairs_df, output_path)
        return pairs_df

    call_cfg = dict(cfg.get("call") or {})
    liquidity_cfg = _merged_dict(global_yield_enhancement_liquidity, cfg)
    liquidity = resolve_candidate_liquidity(liquidity_cfg, defaults=DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_LIQUIDITY)
    put_strategy_fields = _sell_put_strategy_fields(sell_put_cfg)
    window = resolve_candidate_window(
        sell_put_cfg if sell_put_cfg is not None else cfg,
        defaults=DEFAULT_SELL_PUT_WINDOW if sell_put_cfg is not None else DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    )
    expiry_structure = str(cfg.get("expiry_structure") or "same_expiry").strip().lower()
    call_window = (
        resolve_candidate_window(call_cfg, defaults=window)
        if expiry_structure == "diagonal"
        else window
    )

    funding_mode = str(cfg.get("funding_mode") or "credit_or_even").strip().lower()
    max_debit_native = _safe_float(cfg.get("max_debit_native"))
    if max_debit_native is None:
        max_debit_native = _safe_float(cfg.get("max_debit"))
    min_net_credit_retention = _safe_float(cfg.get("min_net_credit_retention"))
    if funding_mode == "max_debit" and "min_net_credit_retention" not in set(policy.explicit_fields):
        min_net_credit_retention = None
    min_combo_notional_floor = 1.0

    call_legs_by_expiration, reject_counts = _load_yield_enhancement_call_legs_by_expiration(
        input_root=Path(input_root),
        symbol=symbol,
        call_cfg=call_cfg,
        window=call_window,
        liquidity=liquidity,
        diagnostics=diagnostics,
    )

    pair_rows = _build_yield_enhancement_pair_rows(
        df=df,
        call_legs_by_expiration=call_legs_by_expiration,
        window=window,
        sell_put_cfg=sell_put_cfg,
        min_combo_notional_floor=min_combo_notional_floor,
        cfg=cfg,
        put_strategy_fields=put_strategy_fields,
        policy_fields=policy.to_fields(),
        funding_mode=funding_mode,
        max_debit_native=max_debit_native,
        min_net_credit_retention=min_net_credit_retention,
        reject_counts=reject_counts,
        diagnostics=diagnostics,
    )

    ranked_pairs = rank_yield_enhancement_rows(pair_rows)
    pairs_df = pd.DataFrame(ranked_pairs) if ranked_pairs else _empty_pairs_df()
    pairs_df.attrs["reject_counts"] = dict(sorted(reject_counts.items()))
    pairs_df.attrs["pair_diagnostics"] = _pair_diagnostics_df(diagnostics)
    _write_pairs_df(pairs_df, output_path)
    return pairs_df


def select_best_yield_enhancement_pairs(
    pairs_df: pd.DataFrame,
) -> pd.DataFrame:
    if pairs_df.empty:
        return _empty_pairs_df()

    selected_rows: list[dict[str, Any]] = []
    for _put_contract_symbol, group in pairs_df.groupby("put_contract_symbol", sort=False):
        top = rank_yield_enhancement_rows(group.to_dict("records"))[0]
        selected = dict(top)
        selected["call_candidate_count"] = int(len(group))
        selected_rows.append(selected)

    ranked_selected = rank_yield_enhancement_rows(selected_rows)
    return pd.DataFrame(ranked_selected) if ranked_selected else _empty_pairs_df()


def build_yield_enhancement_rank_shadow(pairs_df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        *pairs_df.columns,
        "baseline_rank",
        "shadow_rank",
        "baseline_selected",
        "shadow_selected",
        "rank_changed",
    ]
    if pairs_df.empty:
        return pd.DataFrame(columns=list(dict.fromkeys(columns)))

    rows = pairs_df.to_dict("records")

    def pair_key(row: dict[str, Any]) -> tuple[str, str]:
        return (
            str(row.get("put_contract_symbol") or ""),
            str(row.get("call_contract_symbol") or ""),
        )

    baseline_selected_rows: list[dict[str, Any]] = []
    shadow_selected_rows: list[dict[str, Any]] = []
    source = pd.DataFrame(rows)
    for _put_contract_symbol, group in source.groupby("put_contract_symbol", sort=False):
        group_rows = group.to_dict("records")
        baseline_selected_rows.append(rank_yield_enhancement_rows(group_rows)[0])
        shadow_selected_rows.append(rank_yield_enhancement_call_lottery_rows(group_rows)[0])

    ranked_baseline_selected = rank_yield_enhancement_rows(baseline_selected_rows)
    ranked_shadow_selected = rank_yield_enhancement_shadow_rows(shadow_selected_rows)
    baseline_rank = {pair_key(row): index for index, row in enumerate(ranked_baseline_selected, start=1)}
    shadow_rank = {pair_key(row): index for index, row in enumerate(ranked_shadow_selected, start=1)}
    baseline_selected = set(baseline_rank)
    shadow_selected = set(shadow_rank)

    out: list[dict[str, Any]] = []
    for row in rank_yield_enhancement_rows(rows):
        key = pair_key(row)
        baseline_is_selected = key in baseline_selected
        shadow_is_selected = key in shadow_selected
        baseline_position = baseline_rank.get(key)
        shadow_position = shadow_rank.get(key)
        out.append(
            {
                **row,
                "baseline_rank": baseline_position,
                "shadow_rank": shadow_position,
                "baseline_selected": baseline_is_selected,
                "shadow_selected": shadow_is_selected,
                "rank_changed": (
                    baseline_is_selected != shadow_is_selected
                    or (
                        baseline_position is not None
                        and shadow_position is not None
                        and baseline_position != shadow_position
                    )
                ),
            }
        )
    return pd.DataFrame(out)


def _ensure_selected_yield_enhancement_pairs(pairs_df: pd.DataFrame) -> pd.DataFrame:
    if pairs_df.empty:
        return _empty_pairs_df()
    if "put_contract_symbol" in pairs_df.columns and "call_candidate_count" in pairs_df.columns:
        try:
            if not pairs_df["put_contract_symbol"].duplicated().any():
                ranked_rows = rank_yield_enhancement_rows(pairs_df.to_dict("records"))
                return pd.DataFrame(ranked_rows) if ranked_rows else _empty_pairs_df()
        except Exception:
            pass
    return select_best_yield_enhancement_pairs(pairs_df)


def attach_best_linked_calls(
    *,
    df_candidates: pd.DataFrame,
    pairs_df: pd.DataFrame,
    out_path: Path | None = None,
) -> pd.DataFrame:
    df = df_candidates.copy()
    if df.empty or pairs_df.empty:
        if out_path is not None:
            try:
                df.to_csv(out_path, index=False)
            except Exception:
                pass
        return df

    selected_pairs = _ensure_selected_yield_enhancement_pairs(pairs_df)
    best_by_put: list[dict[str, Any]] = []
    for _, top_row in selected_pairs.iterrows():
        put_contract_symbol = str(top_row["put_contract_symbol"])
        top = dict(top_row)
        call_strike = float(top["call_strike"])
        best_by_put.append(
            {
                "contract_symbol": put_contract_symbol,
                "linked_call_contract": _format_contract(
                    str(top.get("call_expiration") or top["expiration"]),
                    call_strike,
                    "C",
                ),
                "linked_call_contract_symbol": top["call_contract_symbol"],
                "linked_call_strike": call_strike,
                "linked_call_ask": _safe_float(top.get("call_ask")),
                "linked_call_delta": _safe_float(top.get("call_delta")),
                "linked_call_iv": _safe_float(top.get("call_implied_volatility")),
                "linked_call_net_credit": _safe_float(top.get("net_credit")),
                "linked_call_net_credit_yield": _safe_float(top.get("net_credit_yield")),
                "linked_call_annualized_net_credit_yield": _safe_float(top.get("annualized_net_credit_yield")),
                "linked_call_expected_move": _safe_float(top.get("expected_move")),
                "linked_call_expected_move_iv": _safe_float(top.get("expected_move_iv")),
                "linked_call_put_only_net_credit": _safe_float(top.get("put_only_net_credit")),
                "linked_call_put_only_breakeven": _safe_float(top.get("put_only_breakeven")),
                "linked_call_combo_breakeven": _safe_float(top.get("combo_breakeven")),
                "linked_call_downside_breakeven_penalty": _safe_float(top.get("downside_breakeven_penalty")),
                "linked_call_lottery_budget_ratio": _safe_float(top.get("lottery_budget_ratio")),
                "linked_call_residual_premium_ratio": _safe_float(top.get("residual_premium_ratio")),
                "linked_call_payoff_multiple_at_1_5_sigma": _safe_float(
                    top.get("call_payoff_multiple_at_1_5_sigma")
                ),
                "linked_call_payoff_multiple_at_2_0_sigma": _safe_float(
                    top.get("call_payoff_multiple_at_2_0_sigma")
                ),
                "linked_call_scenario_score": _safe_float(top.get("scenario_score")),
                "linked_call_annualized_scenario_score": _safe_float(top.get("annualized_scenario_score")),
                "linked_call_count": int(_safe_float(top.get("call_candidate_count")) or 1),
                "linked_call_cost_to_put_credit": _safe_float(top.get("call_cost_to_put_credit")),
                "linked_call_upside_lift": _safe_float(top.get("upside_lift")),
                "linked_call_upside_lift_to_call_cost": _safe_float(top.get("upside_lift_to_call_cost")),
                "linked_call_upside_lift_to_put_credit": _safe_float(top.get("upside_lift_to_put_credit")),
                "linked_call_premium_funding_score": _safe_float(top.get("premium_funding_score")),
            }
        )

    merged = df.merge(pd.DataFrame(best_by_put), on="contract_symbol", how="left")
    if out_path is not None:
        try:
            merged.to_csv(out_path, index=False)
        except Exception:
            pass
    return merged
