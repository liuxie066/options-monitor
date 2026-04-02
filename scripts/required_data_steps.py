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
    min_dte: int | None = None,
    max_dte: int | None = None,
) -> None:
    sym = symbol
    raw = (required_data_dir / 'raw' / f"{sym}_required_data.json").resolve()
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if not (want_put or want_call):
        return

    # Always fetch before scan if required_data missing.
    # Also refetch when:
    # - raw meta.error exists (previous fetch failed but left header-only CSV)
    # - min_dte is requested but existing required_data doesn't reach that DTE.
    if raw.exists() and raw.stat().st_size > 0 and parsed.exists() and parsed.stat().st_size > 0:
        should_refetch = False
        try:
            import json

            obj = json.loads(raw.read_text(encoding='utf-8'))
            meta = obj.get('meta') if isinstance(obj, dict) else None
            err = (meta or {}).get('error') if isinstance(meta, dict) else None
            if err:
                should_refetch = True
        except Exception:
            # raw is unreadable => treat as invalid
            should_refetch = True

        if not should_refetch:
            if min_dte is not None:
                try:
                    import pandas as pd

                    df0 = pd.read_csv(parsed, usecols=['dte'])
                    mx = pd.to_numeric(df0['dte'], errors='coerce').max()
                    if mx is not None and mx >= float(min_dte):
                        return
                except Exception:
                    # On read/parse failure, refetch to be safe.
                    pass
            else:
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
        if min_dte is not None:
            cmd.extend(['--min-dte', str(int(min_dte))])
        if max_dte is not None:
            cmd.extend(['--max-dte', str(int(max_dte))])

        # US spot policy: OpenD often lacks US quote right; default to PM spot fetch unless explicitly disabled.
        if spot_from_pm is None:
            u = str(symbol).strip().upper()
            spot_from_pm = (not u.endswith('.HK'))
        if bool(spot_from_pm):
            cmd.append('--spot-from-pm')
        if (max_strike is not None) and want_put:
            cmd.extend(['--max-strike', str(max_strike)])
        # Cache option_chain daily to reduce OpenD calls (US/HK share the same OpenD limit).
        cmd.append('--chain-cache')
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
