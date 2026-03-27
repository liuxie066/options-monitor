#!/usr/bin/env python3
from __future__ import annotations

"""Lightweight spot price fetcher for options-monitor.

Goal: keep options-monitor independent from portfolio-management.

Usage:
  from scripts.spot_fetcher import get_spot
  spot = get_spot("NVDA")

Sources:
- Finnhub (preferred; requires FINNHUB_API_KEY env)
- Yahoo (fallback; may rate-limit)

Returned price is a float (native currency). For US tickers it's USD.
"""

import os
import time
from typing import Optional

import requests


def _get_env_key() -> Optional[str]:
    return os.getenv("FINNHUB_API_KEY") or os.getenv("FINNHUB_TOKEN")


def get_us_spot_finnhub(ticker: str, timeout: float = 6.0) -> Optional[float]:
    key = _get_env_key()
    if not key:
        return None

    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": ticker.upper(), "token": key}
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json() or {}
        # finnhub quote: c=current, pc=prev close
        v = data.get("c")
        if v is None:
            return None
        v = float(v)
        if v <= 0:
            return None
        return v
    except Exception:
        return None


def get_us_spot_yahoo(ticker: str, timeout: float = 6.0) -> Optional[float]:
    """Best-effort Yahoo spot via public endpoint (no yfinance dependency).

    WARNING: may rate-limit / block.
    """
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ticker.upper()}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json() or {}
        res = (((data.get("quoteResponse") or {}).get("result") or [])[:1])
        if not res:
            return None
        q = res[0] or {}
        v = q.get("regularMarketPrice")
        if v is None:
            return None
        v = float(v)
        if v <= 0:
            return None
        return v
    except Exception:
        return None


def get_spot(symbol: str) -> Optional[float]:
    """Get spot for a ticker (currently US only; symbol should be like NVDA)."""
    s = (symbol or "").strip().upper()
    if not s:
        return None

    # Finnhub first
    v = get_us_spot_finnhub(s)
    if v is not None:
        return v

    # Yahoo fallback
    return get_us_spot_yahoo(s)
