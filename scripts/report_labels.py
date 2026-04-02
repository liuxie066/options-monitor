"""Report labeling helpers.

Stage 3 refactor target: keep run_pipeline orchestration-only.

These helpers may read/write report CSVs, but must remain small and predictable.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError


def _safe_read_csv(path: Path) -> pd.DataFrame:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return pd.DataFrame()
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def add_sell_put_labels(base: Path, input_path: Path, output_path: Path) -> None:
    """Add OTM risk labels to sell_put candidate CSV.

    Why: even when upstream scan yields 0 candidates, we must still overwrite the
    labeled output; otherwise stale labeled CSV from a previous symbol/run may be
    reused and cause "symbol串线".

    Note: `base` is kept for call-site compatibility; it is not used.
    """
    _ = base
    df = _safe_read_csv(input_path)

    def band(v):
        if pd.isna(v):
            return 'unknown'
        if v < 0.03:
            return '<3%'
        if v < 0.07:
            return '3%-7%'
        return '>=7%'

    def label(v):
        if pd.isna(v):
            return '未知'
        if v < 0.03:
            return '激进'
        if v < 0.07:
            return '中性'
        return '保守'

    if 'otm_pct' in df.columns:
        df['otm_band'] = df['otm_pct'].apply(band)
        df['risk_label'] = df['otm_pct'].apply(label)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
