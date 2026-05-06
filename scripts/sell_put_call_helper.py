"""Attach linked call suggestions to confirmed sell-put candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.engine import (
    YieldEnhancementLeg,
    compute_yield_enhancement_metrics,
    rank_yield_enhancement_rows,
    validate_yield_enhancement_pair,
)
from scripts.candidate_defaults import (
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_LIQUIDITY,
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from scripts.fee_calc import calc_futu_option_fee
from scripts.sell_put_risk_bands import classify_sell_put_risk
from src.application.candidate_models import CandidateContractInput


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


def _merged_dict(*items: dict | None) -> dict:
    out: dict = {}
    for item in items:
        if isinstance(item, dict):
            out.update(item)
    return out


def _format_contract(expiration: str, strike: float, option_suffix: str) -> str:
    token = int(strike) if float(strike).is_integer() else strike
    return f"{expiration} {token}{option_suffix}"


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


def _passes_liquidity(
    leg: YieldEnhancementLeg,
    *,
    min_open_interest: float,
    min_volume: float,
    max_spread_ratio: float | None,
) -> bool:
    oi = _safe_float(leg.open_interest) or 0.0
    volume = _safe_float(leg.volume) or 0.0
    spread_ratio = _safe_float(leg.spread_ratio)
    if oi < float(min_open_interest):
        return False
    if volume < float(min_volume):
        return False
    if max_spread_ratio is not None and spread_ratio is not None and spread_ratio > float(max_spread_ratio):
        return False
    return True


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


def _float_list(value: Any, *, default: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, list):
        return default
    parsed: list[float] = []
    for item in value:
        out = _safe_float(item)
        if out is not None:
            parsed.append(float(out))
    return tuple(parsed) if parsed else default


def _build_pair_row(
    *,
    put_leg: YieldEnhancementLeg,
    call_leg: YieldEnhancementLeg,
    expected_move_iv: float | None,
    scenario_move_factors: tuple[float, ...],
    scenario_weights: tuple[float, ...],
    min_combo_notional_floor: float,
) -> dict[str, Any]:
    multiplier = int(put_leg.multiplier)
    put_sell_fee = calc_futu_option_fee(put_leg.currency, put_leg.bid, contracts=1, multiplier=multiplier, is_sell=True)
    call_buy_fee = calc_futu_option_fee(call_leg.currency, call_leg.ask, contracts=1, multiplier=multiplier, is_sell=False)
    metrics = compute_yield_enhancement_metrics(
        put_leg=put_leg,
        call_leg=call_leg,
        put_sell_fee=put_sell_fee,
        call_buy_fee=call_buy_fee,
        expected_move_iv=expected_move_iv,
        scenario_move_factors=scenario_move_factors,
        scenario_weights=scenario_weights,
        min_combo_notional_floor=min_combo_notional_floor,
    )
    risk = classify_sell_put_risk(metrics.put_otm_pct)
    return {
        "symbol": put_leg.symbol,
        "expiration": put_leg.expiration,
        "dte": min(put_leg.dte, call_leg.dte),
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
        "funding_ratio": metrics.funding_ratio,
        "net_income": metrics.net_credit,
        "cash_required": metrics.cash_required,
        "downside_breakeven": metrics.downside_breakeven,
        "upside_breakeven": metrics.upside_breakeven,
        "max_loss_if_zero": metrics.max_loss_if_zero,
        "annualized_return": metrics.annualized_scenario_score,
        "expected_move_iv": metrics.expected_move_iv,
        "expected_move": metrics.expected_move,
        "scenario_score": metrics.scenario_score,
        "annualized_scenario_score": metrics.annualized_scenario_score,
        "put_otm_pct": metrics.put_otm_pct,
        "call_otm_pct": metrics.call_otm_pct,
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


def _empty_pairs_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "symbol",
            "expiration",
            "dte",
            "put_contract_symbol",
            "call_contract_symbol",
            "call_strike",
            "call_ask",
            "net_credit",
            "expected_move_iv",
            "expected_move",
            "scenario_score",
            "annualized_scenario_score",
            "call_candidate_count",
        ]
    )


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


def find_sell_put_yield_enhancement_pairs(
    *,
    df_candidates: pd.DataFrame,
    symbol: str,
    input_root: Path,
    yield_enhancement_cfg: dict | None,
    global_yield_enhancement_liquidity: dict | None = None,
    output_path: Path | None = None,
) -> pd.DataFrame:
    df = df_candidates.copy()
    cfg = dict(yield_enhancement_cfg or {})
    if df.empty or not cfg.get("enabled"):
        pairs_df = _empty_pairs_df()
        if output_path is not None:
            try:
                pairs_df.to_csv(output_path, index=False)
            except Exception:
                pass
        return pairs_df

    call_cfg = dict(cfg.get("call") or {})
    liquidity_cfg = _merged_dict(global_yield_enhancement_liquidity, cfg)
    liquidity = resolve_candidate_liquidity(liquidity_cfg, defaults=DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_LIQUIDITY)
    window = resolve_candidate_window(cfg, defaults=DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW)

    min_put_otm_pct = float(cfg.get("min_put_otm_pct", 0.05) or 0.0)
    min_call_otm_pct = float(cfg.get("min_call_otm_pct", 0.03) or 0.0)
    max_call_otm_pct = _safe_float(cfg.get("max_call_otm_pct"))
    if max_call_otm_pct is None:
        max_call_otm_pct = 0.25
    scenario_move_factors = _float_list(
        cfg.get("scenario_move_factors"),
        default=(0.0, 0.5, 1.0, 1.5),
    )
    scenario_weights = _float_list(
        cfg.get("scenario_weights"),
        default=(0.2, 0.3, 0.4, 0.1),
    )
    min_scenario_score = float(cfg.get("min_scenario_score", 0.0) or 0.0)
    min_annualized_scenario_score = _safe_float(cfg.get("min_annualized_scenario_score"))
    funding_mode = str(cfg.get("funding_mode") or "credit_or_even").strip().lower()
    max_debit_native = _safe_float(cfg.get("max_debit_native"))
    if max_debit_native is None:
        max_debit_native = _safe_float(cfg.get("max_debit"))
    max_combo_spread_ratio = _safe_float(cfg.get("max_combo_spread_ratio", 0.50))
    min_combo_notional_floor = 1.0

    raw_calls = _load_required_data_calls(input_root=Path(input_root), symbol=symbol)
    call_legs_by_expiration: dict[str, list[YieldEnhancementLeg]] = {}
    if not raw_calls.empty:
        for _, raw in raw_calls.iterrows():
            leg = _call_leg_from_required_data(raw)
            if leg is None:
                continue
            if not _passes_range(leg.dte, int(window.min_dte), int(window.max_dte)):
                continue
            if not _passes_range(
                leg.strike,
                _safe_float(call_cfg.get("min_strike")),
                _safe_float(call_cfg.get("max_strike")),
            ):
                continue
            if leg.strike < leg.spot * (1.0 + float(min_call_otm_pct)):
                continue
            if max_call_otm_pct is not None and leg.strike > leg.spot * (1.0 + float(max_call_otm_pct)):
                continue
            if not _passes_liquidity(
                leg,
                min_open_interest=liquidity.min_open_interest,
                min_volume=liquidity.min_volume,
                max_spread_ratio=liquidity.max_spread_ratio,
            ):
                continue
            call_legs_by_expiration.setdefault(leg.expiration, []).append(leg)

    pair_rows: list[dict[str, Any]] = []
    for _, raw in df.iterrows():
        put_leg = _put_leg_from_sell_put_row(raw)
        if put_leg is None:
            continue
        if not _passes_range(put_leg.dte, int(window.min_dte), int(window.max_dte)):
            continue
        if put_leg.strike > put_leg.spot * (1.0 - float(min_put_otm_pct)):
            continue

        for call_leg in call_legs_by_expiration.get(put_leg.expiration, []):
            pair_rejects = validate_yield_enhancement_pair(put_leg, call_leg)
            if pair_rejects:
                continue
            expected_iv = _normalized_iv(put_leg.implied_volatility, call_leg.implied_volatility)
            if expected_iv is None:
                continue
            try:
                candidate = _build_pair_row(
                    put_leg=put_leg,
                    call_leg=call_leg,
                    expected_move_iv=expected_iv,
                    scenario_move_factors=scenario_move_factors,
                    scenario_weights=scenario_weights,
                    min_combo_notional_floor=min_combo_notional_floor,
                )
            except Exception:
                continue
            if funding_mode == "credit_or_even" and float(candidate["net_credit"]) < 0:
                continue
            if funding_mode == "max_debit" and max_debit_native is not None and float(candidate["net_debit"]) > float(max_debit_native):
                continue
            scenario_score = _safe_float(candidate["scenario_score"])
            if scenario_score is None or scenario_score < min_scenario_score:
                continue
            annualized_scenario = _safe_float(candidate["annualized_scenario_score"])
            if min_annualized_scenario_score is not None and (
                annualized_scenario is None or annualized_scenario < float(min_annualized_scenario_score)
            ):
                continue
            combo_spread = _safe_float(candidate["combo_spread_ratio"])
            if max_combo_spread_ratio is not None and combo_spread is not None and combo_spread > float(max_combo_spread_ratio):
                continue
            pair_rows.append(candidate)

    ranked_pairs = rank_yield_enhancement_rows(pair_rows)
    pairs_df = pd.DataFrame(ranked_pairs) if ranked_pairs else _empty_pairs_df()
    if output_path is not None:
        try:
            pairs_df.to_csv(output_path, index=False)
        except Exception:
            pass
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
                "linked_call_contract": _format_contract(str(top["expiration"]), call_strike, "C"),
                "linked_call_contract_symbol": top["call_contract_symbol"],
                "linked_call_strike": call_strike,
                "linked_call_ask": _safe_float(top.get("call_ask")),
                "linked_call_delta": _safe_float(top.get("call_delta")),
                "linked_call_iv": _safe_float(top.get("call_implied_volatility")),
                "linked_call_net_credit": _safe_float(top.get("net_credit")),
                "linked_call_expected_move": _safe_float(top.get("expected_move")),
                "linked_call_expected_move_iv": _safe_float(top.get("expected_move_iv")),
                "linked_call_scenario_score": _safe_float(top.get("scenario_score")),
                "linked_call_annualized_scenario_score": _safe_float(top.get("annualized_scenario_score")),
                "linked_call_count": int(_safe_float(top.get("call_candidate_count")) or 1),
            }
        )

    merged = df.merge(pd.DataFrame(best_by_put), on="contract_symbol", how="left")
    if out_path is not None:
        try:
            merged.to_csv(out_path, index=False)
        except Exception:
            pass
    return merged
