#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import pandas as pd

from scripts.option_candidate_strategy import (
    build_strategy_config,
    filter_candidates_with_reject_log,
    rank_candidates,
    score_candidates,
)
from scripts.d3_event_filter import annotate_candidates_with_d3_events
from scripts.sell_call_config import validate_min_annualized_net_premium_return

SELL_CALL_EMPTY_OUTPUT_COLUMNS = [
    "symbol",
    "expiration",
    "dte",
    "contract_symbol",
    "multiplier",
    "currency",
    "strike",
    "spot",
    "avg_cost",
    "shares_total",
    "shares_locked",
    "shares_available_for_cover",
    "covered_contracts_available",
    "is_fully_covered_available",
    "shares",
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
    "annualized_net_premium_return",
    "if_exercised_total_return",
    "strike_above_spot_pct",
    "strike_above_cost_pct",
    "cc_band",
    "risk_label",
    "event_flag",
    "event_types",
    "event_dates",
    "reject_stage_candidate",
]


def calc_futu_us_option_fee(order_price: float, contracts: int = 1, is_sell: bool = True) -> float:
    commission_per_contract = 0.65 if order_price > 0.1 else 0.15
    commission = max(commission_per_contract * contracts, 1.99)
    platform_fee = 0.30 * contracts
    taf = max(0.00329 * contracts, 0.01) if is_sell else 0.0
    orf = 0.013 * contracts
    occ = 0.02 * contracts
    settlement = 0.18 * contracts
    return round(commission + platform_fee + taf + orf + occ + settlement, 6)


def calc_futu_hk_option_fee_static(order_price: float, contracts: int = 1, is_sell: bool = True, *, base_dir: Path | None = None) -> float:
    platform_fee_per_order = 15.0
    commission_per_order = 0.0
    other_per_order = 0.0
    try:
        if base_dir is not None:
            import json

            cfg = None
            for cfg_name in ("config.hk.json", "config.us.json"):
                cfg_path = base_dir / cfg_name
                if cfg_path.exists() and cfg_path.stat().st_size > 0:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    break
            if isinstance(cfg, dict):
                hk = ((cfg.get("fees") or {}).get("hk_static") or {})
                platform_fee_per_order = float(hk.get("platform_fee_per_order_hkd", platform_fee_per_order))
                commission_per_order = float(hk.get("commission_per_order_hkd", commission_per_order))
                other_per_order = float(hk.get("other_fees_per_order_hkd", other_per_order))
    except Exception:
        pass
    return round(platform_fee_per_order + commission_per_order + other_per_order, 6)


def calc_futu_option_fee(currency: str | None, order_price: float, contracts: int = 1, is_sell: bool = True, *, base_dir: Path | None = None) -> float:
    ccy = (currency or "USD").upper()
    if ccy == "HKD":
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


def strike_band(strike_above_spot_pct: float) -> str:
    if strike_above_spot_pct < 0.03:
        return "<3%"
    if strike_above_spot_pct < 0.08:
        return "3%-8%"
    return ">=8%"


def risk_label(strike_above_spot_pct: float) -> str:
    if strike_above_spot_pct < 0.03:
        return "激进"
    if strike_above_spot_pct < 0.08:
        return "中性"
    return "保守"


def compute_metrics(row: pd.Series, avg_cost: float):
    mid = safe_float(row.get("mid"))
    strike = safe_float(row.get("strike"))
    spot = safe_float(row.get("spot"))
    try:
        dte = int(row.get("dte"))
    except Exception:
        return None
    if None in (mid, strike, spot) or dte <= 0 or avg_cost <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
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

    annualized_net_premium_return = (net_income / (avg_cost * m)) * (365 / dte)
    if_exercised_total_return = (((strike - avg_cost) * m) + net_income) / (avg_cost * m)
    strike_above_spot_pct = (strike - spot) / spot
    strike_above_cost_pct = (strike - avg_cost) / avg_cost

    return {
        "gross_income": round(gross_income, 6),
        "futu_fee": round(fee, 6),
        "net_income": round(net_income, 6),
        "annualized_net_premium_return": round(annualized_net_premium_return, 6),
        "if_exercised_total_return": round(if_exercised_total_return, 6),
        "strike_above_spot_pct": round(strike_above_spot_pct, 6),
        "strike_above_cost_pct": round(strike_above_cost_pct, 6),
        "cc_band": strike_band(strike_above_spot_pct),
        "risk_label": risk_label(strike_above_spot_pct),
    }


def run_sell_call_scan(
    *,
    symbols: list[str],
    input_root: Path,
    output: Path,
    avg_cost: float,
    shares: int = 100,
    shares_locked: int = 0,
    shares_available_for_cover: int | None = None,
    min_dte: int = 7,
    max_dte: int = 90,
    min_otm_pct: float = 0.0,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_annualized_net_return: float | None = None,
    min_if_exercised_total_return: float = 0.0,
    min_open_interest: float = 100,
    min_volume: float = 10,
    max_spread_ratio: float | None = 0.30,
    min_iv: float | None = None,
    max_iv: float | None = None,
    require_bid_ask: bool = False,
    min_delta: float | None = None,
    max_delta: float | None = None,
    d3_event_cfg: dict | None = None,
    reject_log_output: Path | None = None,
    quiet: bool = False,
) -> pd.DataFrame:
    """执行卖出看涨期权扫描并写出候选 CSV。"""
    if shares < 100:
        raise ValueError("shares 必须至少 100，sell call 才有意义。")

    threshold = validate_min_annualized_net_premium_return(
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
        df = df[df["option_type"] == "call"].copy() if (not df.empty and ("option_type" in df.columns)) else pd.DataFrame()

        for _, row in df.iterrows():
            dte = safe_int(row.get("dte"))
            if dte is None:
                continue
            if dte < min_dte or dte > max_dte:
                continue

            strike = safe_float(row.get("strike"))
            if strike is None:
                continue

            if min_otm_pct is not None and float(min_otm_pct) > 0:
                spot = safe_float(row.get("spot"))
                if spot is None or spot <= 0:
                    continue
                otm_pct = (strike - spot) / spot
                if otm_pct < float(min_otm_pct):
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
            if require_bid_ask and (bid is None or ask is None or bid <= 0 or ask <= 0):
                continue

            iv = safe_float(row.get("implied_volatility"))
            if iv is not None and iv > 3.0:
                iv = iv / 100.0
            if min_iv is not None and (iv is None or iv < float(min_iv)):
                continue
            if max_iv is not None and iv is not None and iv > float(max_iv):
                continue

            spread = None
            spread_ratio = None
            if bid is not None and ask is not None and ask >= bid:
                spread = ask - bid
                if mid is not None and mid > 0:
                    spread_ratio = spread / mid
            if max_spread_ratio is not None and spread_ratio is not None and spread_ratio > float(max_spread_ratio):
                continue
            try:
                d = safe_float(row.get("delta"))
                if (min_delta is not None) or (max_delta is not None):
                    if d is None:
                        continue
                    d = float(d)
                    if min_delta is not None and d < float(min_delta):
                        continue
                    if max_delta is not None and d > float(max_delta):
                        continue
            except Exception:
                pass

            metrics = compute_metrics(row, avg_cost)
            if not metrics:
                continue

            m = safe_float(row.get("multiplier"))
            m_int = int(m) if m is not None and m > 0 else 0

            shares_total = int(shares)
            shares_locked_value = int(shares_locked or 0)
            available = shares_available_for_cover
            try:
                if available is not None:
                    available = int(available)
            except Exception:
                available = None
            if available is None:
                available = max(0, shares_total - shares_locked_value)

            covered_contracts_available = 0
            is_fully_covered_available = False
            try:
                if m_int > 0:
                    covered_contracts_available = max(0, available) // m_int
                    is_fully_covered_available = covered_contracts_available >= 1
            except Exception:
                covered_contracts_available = 0
                is_fully_covered_available = False

            if covered_contracts_available < 1:
                continue

            rows.append(
                {
                    "symbol": row["symbol"],
                    "expiration": row["expiration"],
                    "dte": dte,
                    "contract_symbol": row.get("contract_symbol"),
                    "multiplier": m,
                    "currency": row.get("currency"),
                    "strike": strike,
                    "spot": safe_float(row.get("spot")),
                    "avg_cost": avg_cost,
                    "shares_total": shares_total,
                    "shares_locked": shares_locked_value,
                    "shares_available_for_cover": available,
                    "covered_contracts_available": covered_contracts_available,
                    "is_fully_covered_available": is_fully_covered_available,
                    "shares": shares_total,
                    "bid": bid,
                    "ask": ask,
                    "last_price": safe_float(row.get("last_price")),
                    "mid": mid,
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
            "call",
            min_annualized_return=threshold,
            min_if_exercised_total_return=min_if_exercised_total_return,
            max_spread_ratio=max_spread_ratio,
        )
        out, reject_log = filter_candidates_with_reject_log(out, strategy_cfg, reject_stage="step3_risk_gate")
        out = score_candidates(out, strategy_cfg)
        out = rank_candidates(out, strategy_cfg, layered=False)
        out = annotate_candidates_with_d3_events(
            out,
            base_dir=Path(__file__).resolve().parents[1],
            d3_event_cfg=d3_event_cfg,
        )
        if "_strategy_score" in out.columns:
            out = out.drop(columns=["_strategy_score"])
    if out.empty:
        pd.DataFrame(columns=SELL_CALL_EMPTY_OUTPUT_COLUMNS).to_csv(out_path, index=False)
    else:
        out.to_csv(out_path, index=False)
    if reject_log.empty:
        pd.DataFrame(
            columns=[
                "reject_stage",
                "reject_rule",
                "metric_value",
                "threshold",
                "symbol",
                "contract_symbol",
                "expiration",
                "strike",
                "mode",
            ]
        ).to_csv(reject_out_path, index=False)
    else:
        reject_log.to_csv(reject_out_path, index=False)

    if not quiet:
        print(f"[DONE] sell call scan -> {out_path}")
        print(f"[DONE] reject log -> {reject_out_path}")
        print(f"[DONE] candidates: {len(out)}")
        if not out.empty:
            cols = [
                "symbol",
                "expiration",
                "dte",
                "strike",
                "spot",
                "avg_cost",
                "mid",
                "net_income",
                "annualized_net_premium_return",
                "if_exercised_total_return",
                "strike_above_spot_pct",
                "risk_label",
            ]
            print(out[cols].head(20).to_string(index=False))

    return out
