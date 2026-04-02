"""Regression: scanners should not default missing multiplier to 100.

Requirement:
- multiplier missing/invalid => metrics None (row skipped)
"""

from __future__ import annotations

import sys
from pathlib import Path


def test_sell_put_metrics_requires_multiplier() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import pandas as pd
    from scripts.scan_sell_put import compute_metrics

    row = pd.Series({
        'mid': 1.0,
        'strike': 90.0,
        'spot': 100.0,
        'dte': 14,
        'currency': 'HKD',
        # multiplier intentionally missing
    })
    assert compute_metrics(row) is None


def test_sell_call_metrics_requires_multiplier() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import pandas as pd
    from scripts.scan_sell_call import compute_metrics

    row = pd.Series({
        'mid': 1.0,
        'strike': 110.0,
        'spot': 100.0,
        'dte': 14,
        'currency': 'HKD',
        # multiplier intentionally missing
    })
    assert compute_metrics(row, avg_cost=80.0) is None
