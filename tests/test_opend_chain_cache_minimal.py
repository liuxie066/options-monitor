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


def test_chain_cache_must_cover_explicit_expirations() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import scripts.fetch_market_data_opend as m

    cached = {
        "asof_date": "2026-04-28",
        "expirations_all": ["2026-05-28"],
        "rows": [{"strike_time": "2026-05-28", "code": "HK.TEST"}],
    }
    assert m._chain_cache_covers_explicit_expirations(cached, ["2026-05-28"]) is True
    assert m._chain_cache_covers_explicit_expirations(cached, ["2026-04-29"]) is False


def test_chain_cache_does_not_trust_declared_expirations_without_rows() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import scripts.fetch_market_data_opend as m

    cached = {
        "asof_date": "2026-04-28",
        "expirations_all": ["2026-04-29", "2026-06-29"],
        "expirations_pick": ["2026-04-29", "2026-06-29"],
        "rows": [{"strike_time": "2026-04-29", "code": "HK.TEST.2026-04-29.P135"}],
    }
    assert m._chain_cache_covers_explicit_expirations(cached, ["2026-04-29"]) is True
    assert m._chain_cache_covers_explicit_expirations(cached, ["2026-06-29"]) is False


def test_chain_cache_prune_by_mtime() -> None:
    import sys
    import time
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import scripts.fetch_market_data_opend as m

    with TemporaryDirectory() as td:
        root = Path(td)
        # create fake cache files
        p1 = m._chain_cache_path(root, "US.AAPL")
        p2 = m._chain_cache_path(root, "US.NVDA")
        m._save_chain_cache(p1, {"asof_date": "2000-01-01", "underlier_code": "US.AAPL", "rows": []})
        m._save_chain_cache(p2, {"asof_date": "2000-01-01", "underlier_code": "US.NVDA", "rows": []})
        # set p1 very old, p2 recent
        old = time.time() - 10 * 86400
        os_utime = __import__('os').utime
        os_utime(p1, (old, old))
        m._prune_chain_cache(root, keep_days=7)
        assert not p1.exists()
        assert p2.exists()
