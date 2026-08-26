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

from domain.domain.symbol_identity import resolve_symbol_identity, resolve_underlier_alias as _resolve_underlier_alias
from src.application.symbol_aliases import load_runtime_symbol_aliases


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
    aliases = load_runtime_symbol_aliases(base_dir) if base_dir is not None else None
    return _resolve_underlier_alias(symbol, symbol_aliases=aliases)


def normalize_underlier(symbol: str, *, base_dir: Path | None = None) -> Underlier:
    aliases = load_runtime_symbol_aliases(base_dir) if base_dir is not None else None
    identity = resolve_symbol_identity(symbol, symbol_aliases=aliases)
    if identity is not None:
        return Underlier(
            symbol=identity.canonical,
            market=identity.market,
            code=identity.futu_code,
            currency=identity.currency,
        )
    raise ValueError(f"Unsupported underlier symbol format: {symbol!r}")
