"""Regression: scanners should not default missing multiplier to 100.

Requirement:
- multiplier missing/invalid => metrics None (row skipped)
"""

from __future__ import annotations



def test_sell_put_metrics_requires_multiplier() -> None:

    import pandas as pd
    from src.application.scan_sell_put import compute_metrics

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

    import pandas as pd
    from src.application.scan_sell_call import compute_metrics

    row = pd.Series({
        'mid': 1.0,
        'strike': 110.0,
        'spot': 100.0,
        'dte': 14,
        'currency': 'HKD',
        # multiplier intentionally missing
    })
    assert compute_metrics(row, avg_cost=80.0) is None
