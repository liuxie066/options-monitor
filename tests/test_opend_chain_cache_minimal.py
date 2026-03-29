"""Minimal tests for OpenD option_chain day-cache.

No pytest dependency; run via tests/run_tests.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory


def test_chain_cache_helpers_roundtrip() -> None:
    # Ensure repo root on sys.path so `scripts.*` is importable when running tests/run_tests.py
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    # Import helpers from script (ok for minimal tests)
    import scripts.fetch_market_data_opend as m

    with TemporaryDirectory() as td:
        base = Path(td)
        p = m._chain_cache_path(base, "US.NVDA")
        payload = {"asof_date": "2099-01-01", "underlier_code": "US.NVDA", "rows": [{"x": 1}]}
        m._save_chain_cache(p, payload)
        obj = m._load_chain_cache(p)
        assert obj["underlier_code"] == "US.NVDA"
        assert obj["rows"][0]["x"] == 1


def test_chain_cache_fresh_check() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import scripts.fetch_market_data_opend as m
    from datetime import date

    assert m._is_chain_cache_fresh({"asof_date": "2026-03-29"}, date(2026, 3, 29)) is True
    assert m._is_chain_cache_fresh({"asof_date": "2026-03-28"}, date(2026, 3, 29)) is False
    assert m._is_chain_cache_fresh({}, date(2026, 3, 29)) is False
