"""Regression coverage for pure CSP display labeling."""

from __future__ import annotations

import pandas as pd


def test_label_sell_put_candidates_preserves_empty_frame_without_file_io() -> None:
    from src.application.report_labels import label_sell_put_candidates

    source = pd.DataFrame(columns=["symbol", "strike", "otm_pct"])

    labeled = label_sell_put_candidates(source)

    assert labeled.empty
    assert list(labeled.columns) == [
        "symbol",
        "strike",
        "otm_pct",
        "otm_band",
        "risk_label",
    ]
    assert list(source.columns) == ["symbol", "strike", "otm_pct"]
