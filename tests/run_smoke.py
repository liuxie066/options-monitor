#!/usr/bin/env python3
"""Small smoke checks (fast, no OpenD).

Usage:
  ./.venv/bin/python tests/run_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_repo_on_path() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_scanners_require_multiplier() -> None:
    _ensure_repo_on_path()

    import pandas as pd
    from scripts.scan_sell_put import compute_metrics as put_metrics
    from scripts.scan_sell_call import compute_metrics as call_metrics

    put_row = pd.Series({'mid': 1.0, 'strike': 90.0, 'spot': 100.0, 'dte': 14, 'currency': 'HKD'})
    assert put_metrics(put_row) is None

    call_row = pd.Series({'mid': 1.0, 'strike': 110.0, 'spot': 100.0, 'dte': 14, 'currency': 'HKD'})
    assert call_metrics(call_row, avg_cost=80.0) is None


def test_cash_cap_requires_multiplier_cache() -> None:
    _ensure_repo_on_path()

    from scripts.pipeline_steps import derive_put_max_strike_from_cash

    # minimal ctx with HKD cash; without cache multiplier should return None
    ctx = {
        'cash_by_currency': {'HKD': 100000.0},
        'option_ctx': {'cash_secured_total_by_ccy': {'HKD': 0.0}},
    }
    out = derive_put_max_strike_from_cash('0700.HK', ctx, None, None)
    assert out is None


def main() -> None:
    test_scanners_require_multiplier()
    test_cash_cap_requires_multiplier_cache()
    print('OK (smoke)')


if __name__ == '__main__':
    main()
