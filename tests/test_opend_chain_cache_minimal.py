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


def test_option_chain_shard_cache_hit_does_not_call_opend() -> None:
    import sys
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    with TemporaryDirectory() as td:
        root = Path(td)
        cache_path = option_chain_shard_cache_path(root, "US.PDD", "2026-05-15")
        save_option_chain_shard(
            cache_path,
            asof_date="2026-04-30",
            underlier_code="US.PDD",
            expiration="2026-05-15",
            rows=[{"code": "US.PDD.2026-05-15.P100", "strike_time": "2026-05-15"}],
        )

        class _Gateway:
            def get_option_chain(self, **kwargs):  # noqa: ANN001
                raise AssertionError(f"unexpected OpenD call: {kwargs}")

        result = fetch_option_chains(
            gateway=_Gateway(),
            request=OptionChainFetchRequest(
                symbol="PDD",
                underlier_code="US.PDD",
                expirations=["2026-05-15"],
                base_dir=root,
                asof_date="2026-04-30",
                chain_cache=True,
            ),
            retry_call=lambda _name, fn, **kwargs: fn(),
        )

        assert result.status == "ok"
        assert result.opend_call_count == 0
        assert result.from_cache_expirations == ["2026-05-15"]
        assert result.rows[0]["code"] == "US.PDD.2026-05-15.P100"


def test_option_chain_error_shard_does_not_count_as_cache_coverage() -> None:
    import sys
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
    )

    with TemporaryDirectory() as td:
        root = Path(td)
        path = option_chain_shard_cache_path(root, "US.PDD", "2026-05-15")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "asof_date": "2026-04-30",
                    "underlier_code": "US.PDD",
                    "expiration": "2026-05-15",
                    "status": "error",
                    "error_code": "RATE_LIMIT",
                    "rows": [{"code": "bad"}],
                }
            ),
            encoding="utf-8",
        )

        assert load_option_chain_shard(path, asof_date="2026-04-30") is None


def test_file_rate_limiter_coordinates_instances_through_state_file() -> None:
    import sys
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import FileRateLimiter

    with TemporaryDirectory() as td:
        now = [1000.0]
        sleeps: list[float] = []

        def _clock() -> float:
            return now[0]

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        state_path = Path(td) / "limiter.json"
        one = FileRateLimiter(state_path=state_path, max_calls=2, window_sec=1.0, max_wait_sec=5.0, clock=_clock, sleep=_sleep)
        two = FileRateLimiter(state_path=state_path, max_calls=2, window_sec=1.0, max_wait_sec=5.0, clock=_clock, sleep=_sleep)

        one.acquire()
        two.acquire()
        one.acquire()

        assert sleeps
        assert now[0] > 1000.0


def test_save_outputs_preserves_existing_parsed_csv_on_fetch_error() -> None:
    import sys
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import scripts.fetch_market_data_opend as m

    with TemporaryDirectory() as td:
        root = Path(td)
        parsed = root / "parsed"
        parsed.mkdir(parents=True)
        csv_path = parsed / "PDD_required_data.csv"
        csv_path.write_text("symbol,option_type,expiration,strike,mid\nPDD,put,2026-05-15,100,1.0\n", encoding="utf-8")

        m.save_outputs(
            base,
            "PDD",
            {
                "symbol": "PDD",
                "rows": [],
                "meta": {"source": "opend", "status": "error", "error_code": "RATE_LIMIT", "error": "最多10次"},
            },
            output_root=root,
        )

        assert "PDD,put,2026-05-15,100,1.0" in csv_path.read_text(encoding="utf-8")
        raw = json.loads((root / "raw" / "PDD_required_data.json").read_text(encoding="utf-8"))
        assert raw["meta"]["error_code"] == "RATE_LIMIT"
