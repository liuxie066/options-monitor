#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import pandas as pd

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
]


def calc_futu_us_option_fee(order_price: float, contracts: int = 1, is_sell: bool = True) -> float:
    """富途美股单腿期权费用简化模型。"""
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
    """港股期权固定费用模型（HKD）。"""
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
    min_otm_pct: float = 0.05,
    min_annualized_net_return: float | None = None,
    min_net_income: float = 50.0,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_open_interest: float = 100,
    min_volume: float = 10,
    max_spread_ratio: float = 0.30,
    min_iv: float | None = None,
    max_iv: float | None = None,
    require_bid_ask: bool = False,
    min_abs_delta: float | None = None,
    max_abs_delta: float | None = None,
    quiet: bool = False,
) -> pd.DataFrame:
    """执行卖出看跌期权扫描并写出候选 CSV。"""
    threshold = validate_min_annualized_net_return(
        min_annualized_net_return,
        source="--min-annualized-net-return",
    )

    out_path = Path(output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

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
            vol = safe_float(row.get("volume")) or 0.0
            if oi < min_open_interest:
                continue
            if vol < min_volume:
                continue

            bid = safe_float(row.get("bid"))
            ask = safe_float(row.get("ask"))
            mid = safe_float(row.get("mid"))
            spot = safe_float(row.get("spot"))
            if mid is None or strike is None or spot is None:
                continue

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

            try:
                d = safe_float(row.get("delta"))
                if (min_abs_delta is not None) or (max_abs_delta is not None):
                    if d is None:
                        continue
                    ad = abs(float(d))
                    if min_abs_delta is not None and ad < float(min_abs_delta):
                        continue
                    if max_abs_delta is not None and ad > float(max_abs_delta):
                        continue
            except Exception:
                pass

            metrics = compute_metrics(row)
            if not metrics:
                continue
            if metrics["otm_pct"] < min_otm_pct:
                continue
            if metrics["net_income"] < min_net_income:
                continue
            if metrics["annualized_net_return_on_cash_basis"] < threshold:
                continue
            if spread_ratio is not None and spread_ratio > max_spread_ratio:
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
    if not out.empty:
        out = out.sort_values(by=["annualized_net_return_on_cash_basis", "net_income"], ascending=[False, False])
    if out.empty:
        pd.DataFrame(columns=SELL_PUT_EMPTY_OUTPUT_COLUMNS).to_csv(out_path, index=False)
    else:
        out.to_csv(out_path, index=False)

    if not quiet:
        print(f"[DONE] sell put scan -> {out_path}")
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
