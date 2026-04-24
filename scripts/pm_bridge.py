from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
        return
    print(message, file=sys.stderr)


def resolve_spot_fallback_enabled(
    fetch_cfg: Mapping[str, Any] | None,
    *,
    symbol: str,
) -> bool:
    return False


def fetch_spot_with_fallback(
    ticker: str,
    *,
    timeout_sec: int = 12,
    pm_root: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> float | None:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        _warn(log, "[WARN] spot fetch skipped: empty ticker")
        return None
    _warn(log, f"[WARN] external spot fallback removed: ticker={symbol}; OpenD is the only supported source")
    return None


def fetch_spot_from_portfolio_management(
    ticker: str,
    *,
    timeout_sec: int = 12,
    pm_root: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> float | None:
    return fetch_spot_with_fallback(
        ticker,
        timeout_sec=timeout_sec,
        pm_root=pm_root,
        log=log,
    )
