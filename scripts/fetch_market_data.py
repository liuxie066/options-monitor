#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf


def to_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def calc_mid(bid, ask, last_price=None):
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return round((bid + ask) / 2, 6)
    if last_price is not None and last_price > 0:
        return round(last_price, 6)
    return None


def get_spot_price(ticker: yf.Ticker) -> float:
    fast = ticker.fast_info or {}
    for key in ("lastPrice", "last_price", "regularMarketPrice"):
        value = fast.get(key)
        if value is not None:
            return float(value)

    hist = ticker.history(period="1d")
    if not hist.empty:
        return float(hist["Close"].iloc[-1])

    raise RuntimeError("Could not determine underlying spot price")


def _norm_cdf(x: float) -> float:
    # Standard normal CDF via erf (no scipy dependency)
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def calc_bs_delta(spot: float, strike: float, dte: int, iv: float, option_type: str, r: float = 0.0, q: float = 0.0) -> float | None:
    """Black-Scholes delta using implied volatility.

    iv is expected as decimal (e.g. 0.5 for 50%). We clamp to a reasonable range.
    """
    try:
        import math
        if spot <= 0 or strike <= 0 or dte <= 0:
            return None
        if iv is None or math.isnan(iv) or iv <= 0:
            return None

        # Some data sources may output IV in percent (e.g. 50 for 50%); normalize heuristically.
        sigma = float(iv)
        if sigma > 3.0:
            sigma = sigma / 100.0
        sigma = max(1e-6, min(sigma, 5.0))

        T = float(dte) / 365.0
        if T <= 0:
            return None

        d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        if option_type == 'call':
            return math.exp(-q * T) * _norm_cdf(d1)
        if option_type == 'put':
            return math.exp(-q * T) * (_norm_cdf(d1) - 1.0)
        return None
    except Exception:
        return None


def normalize_option_rows(symbol: str, expiration: str, option_type: str, df: pd.DataFrame, spot: float) -> list[dict[str, Any]]:
    today = date.today()
    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    dte = (exp_date - today).days
    rows: list[dict[str, Any]] = []

    for _, r in df.iterrows():
        bid = to_float(r.get("bid"))
        ask = to_float(r.get("ask"))
        last_price = to_float(r.get("lastPrice"))
        strike = to_float(r.get("strike"))
        iv = to_float(r.get("impliedVolatility"))

        row = {
            "symbol": symbol,
            "option_type": option_type,
            "expiration": expiration,
            "dte": dte,
            "contract_symbol": r.get("contractSymbol"),
            "strike": strike,
            "spot": spot,
            "bid": bid,
            "ask": ask,
            "last_price": last_price,
            "mid": calc_mid(bid, ask, last_price),
            "volume": to_float(r.get("volume")),
            "open_interest": to_float(r.get("openInterest")),
            "implied_volatility": iv,
            "in_the_money": bool(r.get("inTheMoney")) if pd.notna(r.get("inTheMoney")) else None,
            "currency": "USD",
        }

        if strike is not None and spot is not None and spot > 0:
            if option_type == "put":
                row["otm_pct"] = (spot - strike) / spot
            else:
                row["otm_pct"] = (strike - spot) / spot
        else:
            row["otm_pct"] = None

        # Greeks (best-effort): delta from BS using implied vol
        try:
            iv = float(row.get('implied_volatility')) if row.get('implied_volatility') is not None else None
        except Exception:
            iv = None
        try:
            dte = int(row.get('dte'))
        except Exception:
            dte = 0
        try:
            strike = float(row.get('strike'))
        except Exception:
            strike = None

        if strike is not None:
            row['delta'] = calc_bs_delta(float(spot), float(strike), int(dte), iv, str(option_type))
        else:
            row['delta'] = None

        rows.append(row)

    return rows


def fetch_symbol(symbol: str, limit_expirations: int | None = None) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    spot = get_spot_price(ticker)
    expirations = list(ticker.options or [])
    if limit_expirations:
        expirations = expirations[:limit_expirations]

    all_rows: list[dict[str, Any]] = []
    for exp in expirations:
        chain = ticker.option_chain(exp)
        if chain.calls is not None and not chain.calls.empty:
            all_rows.extend(normalize_option_rows(symbol, exp, "call", chain.calls, spot))
        if chain.puts is not None and not chain.puts.empty:
            all_rows.extend(normalize_option_rows(symbol, exp, "put", chain.puts, spot))

    return {
        "symbol": symbol,
        "spot": spot,
        "expiration_count": len(expirations),
        "expirations": expirations,
        "rows": all_rows,
    }


def save_outputs(base: Path, symbol: str, payload: dict[str, Any]):
    raw_dir = base / "output" / "raw"
    parsed_dir = base / "output" / "parsed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{symbol}_required_data.json"
    csv_path = parsed_dir / f"{symbol}_required_data.csv"

    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(payload["rows"]).to_csv(csv_path, index=False)
    return raw_path, csv_path


def main():
    parser = argparse.ArgumentParser(description="Fetch required US option data from Yahoo Finance via yfinance")
    parser.add_argument("--symbols", nargs="+", required=True, help="US tickers like AAPL TSLA SPY")
    parser.add_argument("--limit-expirations", type=int, default=2, help="Only fetch first N expirations for quick POC")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]

    for symbol in args.symbols:
        payload = fetch_symbol(symbol, limit_expirations=args.limit_expirations)
        raw_path, csv_path = save_outputs(base, symbol, payload)
        print(f"[OK] {symbol}")
        print(f"  spot={payload['spot']}")
        print(f"  expirations={payload['expiration_count']} fetched={len(payload['expirations'])}")
        print(f"  option_rows={len(payload['rows'])}")
        print(f"  raw={raw_path}")
        print(f"  csv={csv_path}")


if __name__ == "__main__":
    main()
