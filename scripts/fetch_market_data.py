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
