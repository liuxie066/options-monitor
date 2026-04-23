from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Any


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
        return
    print(message, file=sys.stderr)


def default_pm_root() -> Path | None:
    raw = str(os.environ.get("OM_PM_ROOT") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _fetch_spot_from_yahoo(
    ticker: str,
    *,
    log: Callable[[str], None] | None = None,
) -> float | None:
    try:
        import yfinance as yf

        from scripts.fetch_market_data import get_spot_price

        price = float(get_spot_price(yf.Ticker(ticker)))
        if price <= 0:
            raise ValueError(f"non-positive price: {price}")
        return price
    except Exception as exc:
        _warn(log, f"[WARN] yahoo spot fetch failed: ticker={ticker} error={exc}")
        return None


def default_spot_fallback_enabled(symbol: str) -> bool:
    u = str(symbol or "").strip().upper()
    return not (u.endswith(".HK") or u.startswith("HK."))


def resolve_spot_fallback_enabled(
    fetch_cfg: Mapping[str, Any] | None,
    *,
    symbol: str,
) -> bool:
    cfg = fetch_cfg if isinstance(fetch_cfg, Mapping) else {}
    if "spot_from_yahoo" in cfg:
        return bool(cfg.get("spot_from_yahoo"))
    if "spot_from_portfolio_management" in cfg:
        return bool(cfg.get("spot_from_portfolio_management"))
    return default_spot_fallback_enabled(symbol)


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

    yahoo_spot = _fetch_spot_from_yahoo(symbol, log=log)
    if yahoo_spot is not None:
        return yahoo_spot

    root = Path(pm_root).resolve() if pm_root is not None else default_pm_root()
    if root is None:
        _warn(log, "[WARN] legacy pm spot fallback disabled: set OM_PM_ROOT to enable external fallback")
        return None
    if not root.exists():
        _warn(log, f"[WARN] pm spot fetch unavailable: portfolio-management not found at {root}")
        return None

    pm_python = root / ".venv" / "bin" / "python"
    if not pm_python.exists():
        _warn(log, f"[WARN] pm spot fetch unavailable: python not found at {pm_python}")
        return None

    code = (
        "import sys, json; "
        "sys.path.insert(0, '.'); "
        "from src.price_fetcher import PriceFetcher; "
        f"r=PriceFetcher().fetch({symbol!r}); "
        "print(json.dumps(r, ensure_ascii=False))"
    )

    try:
        out = subprocess.check_output(
            [str(pm_python), "-c", code],
            cwd=str(root),
            timeout=int(timeout_sec),
        )
    except Exception as exc:
        _warn(log, f"[WARN] pm spot fetch failed: ticker={symbol} error={exc}")
        return None

    try:
        text = out.decode("utf-8", errors="ignore")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        payload_line = next((line for line in reversed(lines) if line.startswith("{") and line.endswith("}")), None)
        payload = json.loads(payload_line or "{}")
    except Exception as exc:
        _warn(log, f"[WARN] pm spot payload invalid: ticker={symbol} error={exc}")
        return None

    price = payload.get("price") if isinstance(payload, dict) else None
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None

    if price is None or price <= 0:
        _warn(log, f"[WARN] pm spot payload missing positive price: ticker={symbol}")
        return None
    return price


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
