"""Required-data fetch step.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

from scripts import pipeline_fetch_models
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
    state_dir: Path | None = None,
    fetch_source: str = 'yahoo',
    fetch_host: str = '127.0.0.1',
    fetch_port: int = 11111,
    spot_from_pm: bool | None = None,
    max_strike: float | None = None,
    min_dte: int | None = None,
    max_dte: int | None = None,
) -> None:
    sym = symbol
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if not (want_put or want_call):
        return

    src = str(fetch_source or 'yahoo').strip().lower()

    # In dev mode, keep fetch write/read model separated from pipeline orchestration:
    # - write model: fetch_required_data.events.jsonl + fetch_required_data.snapshots.json
    # - read model:  state/current/fetch_required_data.current.json
    # This keeps delivery/pipeline path from directly reading raw fetch artifacts.
    fetch_current = None
    if (not is_scheduled) and (state_dir is not None):
        try:
            fetch_current = pipeline_fetch_models.backfill_symbol_snapshot_from_raw(
                required_data_dir=required_data_dir,
                state_dir=state_dir,
                symbol=sym,
                source=src,
            )
        except Exception:
            fetch_current = None

    # Always fetch before scan if required_data missing.
    # Also refetch when:
    # - read-model shows previous fetch status=error
    # - min_dte is requested but existing required_data doesn't reach that DTE.
    if parsed.exists() and parsed.stat().st_size > 0:
        should_refetch = False
        if isinstance(fetch_current, dict):
            if str(fetch_current.get('status') or '').lower() == 'error':
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

    try:
        run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)
        if (not is_scheduled) and (state_dir is not None):
            pipeline_fetch_models.record_fetch_snapshot(
                state_dir=state_dir,
                symbol=sym,
                source=src,
                status='ok',
            )
    except BaseException as e:
        if (not is_scheduled) and (state_dir is not None):
            pipeline_fetch_models.record_fetch_snapshot(
                state_dir=state_dir,
                symbol=sym,
                source=src,
                status='error',
                reason=str(e),
            )
        raise
