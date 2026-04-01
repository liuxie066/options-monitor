"""Required-data fetch step.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

from scripts.subprocess_utils import run_cmd


def ensure_required_data(
    *,
    py: str,
    base: Path,
    symbol: str,
    required_data_dir: Path,
    limit_expirations: int,
    want_put: bool,
    want_call: bool,
    timeout_sec: int | None,
    is_scheduled: bool,
    fetch_source: str = 'yahoo',
    fetch_host: str = '127.0.0.1',
    fetch_port: int = 11111,
    spot_from_pm: bool | None = None,
    max_strike: float | None = None,
) -> None:
    sym = symbol
    raw = (required_data_dir / 'raw' / f"{sym}_required_data.json").resolve()
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if not (want_put or want_call):
        return

    # Always fetch before scan if required_data missing.
    if raw.exists() and raw.stat().st_size > 0 and parsed.exists() and parsed.stat().st_size > 0:
        return

    src = str(fetch_source or 'yahoo').strip().lower()

    # fetch_required_data.py no longer exists; use fetch_market_data(_opend).py directly.
    if src == 'opend':
        opt_types = ('put,call' if (want_put and want_call) else ('put' if want_put else 'call'))
        cmd = [
            py, 'scripts/fetch_market_data_opend.py',
            '--symbols', sym,
            '--limit-expirations', str(limit_expirations),
            '--host', str(fetch_host),
            '--port', str(int(fetch_port)),
            '--option-types', opt_types,
            '--output-root', str(required_data_dir),
        ]

        # US spot policy: OpenD often lacks US quote right; default to PM spot fetch unless explicitly disabled.
        if spot_from_pm is None:
            u = str(symbol).strip().upper()
            spot_from_pm = (not u.endswith('.HK'))
        if bool(spot_from_pm):
            cmd.append('--spot-from-pm')
        if (max_strike is not None) and want_put:
            cmd.extend(['--max-strike', str(max_strike)])
        if is_scheduled:
            cmd.append('--quiet')
    else:
        cmd = [
            py, 'scripts/fetch_market_data.py',
            '--symbols', sym,
            '--output-root', str(required_data_dir),
            '--limit-expirations', str(limit_expirations),
        ]

    run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)
