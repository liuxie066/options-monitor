from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable


def _warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)
        return
    print(message, file=sys.stderr)


def default_pm_root() -> Path:
    base_dir = Path(__file__).resolve().parents[2]
    return (base_dir / "portfolio-management").resolve()


def fetch_spot_from_portfolio_management(
    ticker: str,
    *,
    timeout_sec: int = 12,
    pm_root: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> float | None:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        _warn(log, "[WARN] pm spot fetch skipped: empty ticker")
        return None

    root = Path(pm_root).resolve() if pm_root is not None else default_pm_root()
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
