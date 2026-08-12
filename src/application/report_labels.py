"""Pure report labeling helpers."""

from __future__ import annotations

import pandas as pd

from domain.domain.sell_put_risk_bands import classify_sell_put_risk


def label_sell_put_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Add deterministic Sell Put display bands without file I/O."""

    out = df.copy()
    if 'otm_pct' in out.columns:
        risk_series = out['otm_pct'].apply(
            lambda v: classify_sell_put_risk(None if pd.isna(v) else float(v))
        )
        out['otm_band'] = risk_series.apply(lambda r: r.band)
        out['risk_label'] = risk_series.apply(lambda r: r.risk_label)
    return out
