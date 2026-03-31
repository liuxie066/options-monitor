"""Multiplier cache helpers.

Extracted from pipeline_symbol.py (Stage 3).

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.io_utils import safe_read_csv


def apply_multiplier_cache_to_required_data_csv(*, base: Path, required_data_dir: Path, symbol: str) -> None:
    """Best-effort: fill missing/invalid multiplier in required_data.csv based on local cache."""
    try:
        from scripts import multiplier_cache

        cache_path = multiplier_cache.default_cache_path(base)
        cache = multiplier_cache.load_cache(cache_path)
        m = multiplier_cache.get_cached_multiplier(cache, symbol)
        if not m:
            return

        parsed = (required_data_dir / 'parsed' / f"{symbol}_required_data.csv").resolve()
        if not parsed.exists() or parsed.stat().st_size <= 0:
            return

        df = safe_read_csv(parsed)
        if df.empty:
            return

        if 'multiplier' not in df.columns:
            df['multiplier'] = float(m)
        else:
            try:
                mm = pd.to_numeric(df['multiplier'], errors='coerce')
                bad = mm.isna() | (mm <= 0)
                if bad.any():
                    df.loc[bad, 'multiplier'] = float(m)
            except Exception:
                df['multiplier'] = float(m)

        df.to_csv(parsed, index=False)
    except Exception:
        pass
