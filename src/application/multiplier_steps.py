"""Multiplier cache helpers.

Extracted from pipeline_symbol.py (Stage 3).

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd



def apply_multiplier_cache_to_required_data_csv(*, base: Path, required_data_dir: Path, symbol: str) -> None:
    """Best-effort: fill missing multiplier in required_data.csv based on local cache."""
    try:
        from src.application import multiplier_cache

        m = multiplier_cache.resolve_multiplier(
            repo_base=base,
            symbol=symbol,
            allow_opend_refresh=False,
        )
        if not m:
            return

        parsed = (required_data_dir / 'parsed' / f"{symbol}_required_data.csv").resolve()
        if not parsed.exists() or parsed.stat().st_size <= 0:
            return

        df = pd.read_csv(parsed, converters={field: str for field in ("multiplier", "chain_multiplier", "snapshot_multiplier")})
        if df.empty:
            return

        if 'multiplier' not in df.columns:
            df['multiplier'] = str(float(m))
        else:
            # CSV empty cells encode absent provider values. Preserve explicit
            # invalid tokens for the shared multiplier validator to reject.
            missing = df['multiplier'].eq('')
            if not missing.any():
                return
            df.loc[missing, 'multiplier'] = str(float(m))

        df.to_csv(parsed, index=False)
    except Exception:
        pass
