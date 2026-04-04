#!/usr/bin/env python3
from __future__ import annotations

"""Utilities for Futu OpenD integration (options-monitor).

Keep this module lightweight and dependency-minimal.

- Normalize underlying symbol -> Futu code (e.g. NVDA -> US.NVDA, 00700.HK -> HK.00700)
- Decide currency by market

NOTE: options-monitor currently assumes US options economics in downstream scans.
HK options chain support is possible, but may require multiplier/fee model changes.
"""

import re
from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Any


def market_to_futu_trade_date_market(market: str):
    """Map internal market label to futu TradeDateMarket enum.

    Returns None when mapping is unknown.
    """
    try:
        from futu import TradeDateMarket
    except Exception:
        return None

    m = (market or '').upper().strip()
    mapping = {
        'HK': 'HK',
        'US': 'US',
        'CN': 'CN',
    }
    key = mapping.get(m)
    return getattr(TradeDateMarket, key, None) if key else None


def is_trading_day_via_futu(ctx: Any, market: str) -> tuple[bool | None, str]:
    """Check whether today is a trading day via futu request_trading_days.

    Returns:
      (True/False, market_used) on API success;
      (None, market_used) when market mapping/API call fails.
    """
    market_used = (market or '').upper().strip()
    tm = market_to_futu_trade_date_market(market_used)
    if tm is None:
        return (None, market_used)

    d = get_trading_date(market_used)
    ds = d.strftime('%Y-%m-%d')
    try:
        ret, data = ctx.request_trading_days(market=tm, start=ds, end=ds)
    except Exception:
        return (None, market_used)

    # futu RET_OK is 0
    if ret != 0:
        return (None, market_used)

    rows = data if isinstance(data, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get('time') or '') != ds:
            continue
        t = str(row.get('trade_date_type') or '').upper()
        if t in ('WHOLE', 'MORNING', 'AFTERNOON', 'TRADING'):
            return (True, market_used)
    return (False, market_used)


def get_trading_date(market: str) -> date:
    """Market-convention trading date.

    Why: server may run in UTC; using date.today() can shift DTE by 1 around US after-hours.
    """
    m = (market or '').upper().strip()
    if m == 'US':
        return datetime.now(ZoneInfo('America/New_York')).date()
    if m == 'HK':
        return datetime.now(ZoneInfo('Asia/Hong_Kong')).date()
    if m == 'CN':
        return datetime.now(ZoneInfo('Asia/Shanghai')).date()
    return datetime.now(ZoneInfo('UTC')).date()


@dataclass
class Underlier:
    symbol: str        # input symbol (e.g. NVDA, 00700.HK)
    market: str        # US | HK | CN
    code: str          # futu code (e.g. US.NVDA, HK.00700)
    currency: str      # USD | HKD | CNY


_US_RE = re.compile(r"^[A-Z][A-Z0-9\.-]{0,10}$", re.I)


def normalize_underlier(symbol: str) -> Underlier:
    s = (symbol or '').strip()
    su = s.upper()

    # HK: allow 00700.HK / 700.HK / HK.00700 / 00700
    if su.endswith('.HK'):
        num = su[:-3]
        num = num.zfill(5)
        return Underlier(symbol=s, market='HK', code=f"HK.{num}", currency='HKD')
    if su.startswith('HK.'):
        num = su[3:]
        num = num.zfill(5)
        return Underlier(symbol=s, market='HK', code=f"HK.{num}", currency='HKD')
    if su.isdigit() and len(su) <= 5:
        # ambiguous: treat as HK code by default (most common in this repo if digits-only)
        num = su.zfill(5)
        return Underlier(symbol=s, market='HK', code=f"HK.{num}", currency='HKD')

    # CN A-share: SH/SZ.
    if su.startswith('SH.') or su.startswith('SZ.'):
        prefix = su[:2]
        num = su[3:]
        return Underlier(symbol=s, market='CN', code=f"{prefix}.{num}", currency='CNY')

    # Default: US ticker
    if _US_RE.match(su):
        return Underlier(symbol=s, market='US', code=f"US.{su}", currency='USD')

    raise ValueError(f"Unsupported underlier symbol format: {symbol!r}")
