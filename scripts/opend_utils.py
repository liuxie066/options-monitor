#!/usr/bin/env python3
from __future__ import annotations

"""Utilities for Futu OpenD integration (options-monitor).

Keep this module lightweight and dependency-minimal.

- Normalize underlying symbol -> Futu code (e.g. NVDA -> US.NVDA, 00700.HK -> HK.00700)
- Decide currency by market

NOTE: options-monitor currently assumes US options economics in downstream scans.
HK options chain support is possible, but may require multiplier/fee model changes.
"""

from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from scripts.trade_symbol_identity import resolve_symbol_identity, resolve_underlier_alias as _resolve_underlier_alias


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

    rows = []
    if isinstance(data, list):
        rows = data
    elif hasattr(data, 'to_dict'):
        # Futu often returns a pandas DataFrame
        try:
            rows = data.to_dict('records')  # type: ignore[attr-defined]
        except Exception:
            rows = []
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


def resolve_underlier_alias(symbol: str, *, base_dir: Path | None = None) -> str:
    return _resolve_underlier_alias(symbol, base_dir=base_dir)


def normalize_underlier(symbol: str) -> Underlier:
    identity = resolve_symbol_identity(symbol)
    if identity is not None:
        return Underlier(
            symbol=identity.canonical,
            market=identity.market,
            code=identity.futu_code,
            currency=identity.currency,
        )
    raise ValueError(f"Unsupported underlier symbol format: {symbol!r}")
