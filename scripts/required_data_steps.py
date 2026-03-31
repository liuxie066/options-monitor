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
) -> None:
    sym = symbol
    raw = (required_data_dir / 'raw' / f"{sym}_required_data.json").resolve()
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if not (want_put or want_call):
        return

    # Always fetch before scan if required_data missing.
    if raw.exists() and raw.stat().st_size > 0 and parsed.exists() and parsed.stat().st_size > 0:
        return

    cmd = [
        py, 'scripts/fetch_required_data.py',
        '--symbols', sym,
        '--output-root', str(required_data_dir),
        '--limit-expirations', str(limit_expirations),
    ]
    if is_scheduled:
        cmd.append('--quiet')

    run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)
