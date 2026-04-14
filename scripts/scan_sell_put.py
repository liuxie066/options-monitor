#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import pandas as pd

from domain.domain.engine import (
    empty_reject_log_dataframe,
    build_strategy_config,
    filter_rank_candidates_with_reject_log,
)
from scripts.d3_event_filter import annotate_candidates_with_d3_events
from scripts.sell_put_config import validate_min_annualized_net_return

SELL_PUT_EMPTY_OUTPUT_COLUMNS = [
    "symbol",
    "expiration",
    "dte",
    "contract_symbol",
    "multiplier",
    "currency",
    "strike",
    "spot",
    "bid",
    "ask",
    "last_price",
    "mid",
    "open_interest",
    "volume",
    "implied_volatility",
    "delta",
    "spread",
    "spread_ratio",
    "gross_income",
    "futu_fee",
    "net_income",
    "otm_pct",
    "cash_basis",
    "breakeven",
    "annualized_net_return_on_strike",
    "annualized_net_return_on_cash_basis",
    "event_flag",
    "event_types",
    "event_dates",
    "reject_stage_candidate",
]

from scripts.fee_calc import calc_futu_us_option_fee, calc_futu_hk_option_fee_static, calc_futu_option_fee, safe_float, safe_int


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
    fee = calc_futu_option_fee(row.get("currency"), mid, contracts=1, is_sell=True, base_dir=base_dir)
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


def run_sell_put_scan(
    *,
    symbols: list[str],
    input_root: Path,
    output: Path,
    min_dte: int = 7,
    max_dte: int = 45,
    min_annualized_net_return: float | None = None,
    min_net_income: float = 50.0,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_open_interest: float = 100,
    min_volume: float = 10,
    max_spread_ratio: float | None = 0.30,
    d3_event_cfg: dict | None = None,
    reject_log_output: Path | None = None,
    quiet: bool = False,
) -> pd.DataFrame:
    """执行卖出看跌期权扫描并写出候选 CSV。"""
    threshold = validate_min_annualized_net_return(
        min_annualized_net_return,
        source="--min-annualized-net-return",
    )

    out_path = Path(output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    reject_out_path = (
        Path(reject_log_output).resolve()
        if reject_log_output is not None
        else out_path.with_name(f"{out_path.stem}_reject_log.csv")
    )
    reject_out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for symbol in symbols:
        path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
        try:
            df = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            # 空 required_data 视为无候选，避免中断整轮扫描。
            df = pd.DataFrame()
        df = df[df["option_type"] == "put"].copy() if (not df.empty and ("option_type" in df.columns)) else pd.DataFrame()

        for _, row in df.iterrows():
            dte = safe_int(row.get("dte"))
            if dte is None or dte <= 0:
                continue
            if dte < min_dte or dte > max_dte:
                continue

            strike = safe_float(row.get("strike"))
            if strike is None:
                continue
            if min_strike is not None and strike < min_strike:
                continue
            if max_strike is not None and strike > max_strike:
                continue

            oi = safe_float(row.get("open_interest")) or 0.0
            if oi < min_open_interest:
                continue
            vol = safe_float(row.get("volume")) or 0.0
            if vol < min_volume:
                continue

            bid = safe_float(row.get("bid"))
            ask = safe_float(row.get("ask"))
            mid = safe_float(row.get("mid"))
            spot = safe_float(row.get("spot"))
            if mid is None or strike is None or spot is None:
                continue

            spread = None
            spread_ratio = None
            if bid is not None and ask is not None and ask >= bid:
                spread = ask - bid
                if mid is not None and mid > 0:
                    spread_ratio = spread / mid
            if max_spread_ratio is not None and spread_ratio is not None and spread_ratio > float(max_spread_ratio):
                continue

            metrics = compute_metrics(row)
            if not metrics:
                continue
            rows.append(
                {
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
                }
            )

    out = pd.DataFrame(rows)
    reject_log = pd.DataFrame()
    if not out.empty:
        strategy_cfg = build_strategy_config(
            "put",
            min_annualized_return=threshold,
            min_net_income=min_net_income,
            max_spread_ratio=max_spread_ratio,
        )
        out, reject_log = filter_rank_candidates_with_reject_log(
            out,
            strategy_cfg,
            reject_stage="step3_risk_gate",
            layered=False,
        )
        out = annotate_candidates_with_d3_events(
            out,
            base_dir=Path(__file__).resolve().parents[1],
            d3_event_cfg=d3_event_cfg,
        )
        if "_strategy_score" in out.columns:
            out = out.drop(columns=["_strategy_score"])
    if out.empty:
        pd.DataFrame(columns=SELL_PUT_EMPTY_OUTPUT_COLUMNS).to_csv(out_path, index=False)
    else:
        out.to_csv(out_path, index=False)
    if reject_log.empty:
        empty_reject_log_dataframe().to_csv(reject_out_path, index=False)
    else:
        reject_log.to_csv(reject_out_path, index=False)

    if not quiet:
        print(f"[DONE] sell put scan -> {out_path}")
        print(f"[DONE] reject log -> {reject_out_path}")
        print(f"[DONE] candidates: {len(out)}")
        if not out.empty:
            display_cols = [
                "symbol",
                "expiration",
                "dte",
                "strike",
                "spot",
                "mid",
                "futu_fee",
                "net_income",
                "otm_pct",
                "annualized_net_return_on_cash_basis",
            ]
            print(out[display_cols].head(20).to_string(index=False))

    return out
