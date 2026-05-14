"""Minimal tests for OpenD option_chain day-cache.

No pytest dependency; run via tests/run_tests.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast


def test_chain_cache_helpers_roundtrip() -> None:
    # Ensure repo root on sys.path when running tests/run_tests.py directly.
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    with TemporaryDirectory() as td:
        base = Path(td)
        p = option_chain_shard_cache_path(base, "US.NVDA", "2099-01-15")
        save_option_chain_shard(
            p,
            asof_date="2099-01-01",
            underlier_code="US.NVDA",
            expiration="2099-01-15",
            rows=[{"x": 1}],
        )
        obj = load_option_chain_shard(p, asof_date="2099-01-01")
        assert obj is not None
        assert obj[0]["x"] == 1


def test_chain_cache_fresh_check() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    with TemporaryDirectory() as td:
        root = Path(td)
        path = option_chain_shard_cache_path(root, "US.NVDA", "2026-03-29")
        save_option_chain_shard(
            path,
            asof_date="2026-03-29",
            underlier_code="US.NVDA",
            expiration="2026-03-29",
            rows=[{"code": "US.NVDA.2026-03-29.P100"}],
        )

        assert load_option_chain_shard(path, asof_date="2026-03-29") is not None
        assert load_option_chain_shard(path, asof_date="2026-03-28") is None


def test_chain_fetch_uses_stale_cache_on_rate_limit() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("获取期权链频率太高，请求失败，每30秒最多10次。")

    with TemporaryDirectory() as td:
        root = Path(td)
        cache_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-09-18", option_type_scope="put")
        save_option_chain_shard(
            cache_path,
            asof_date="2026-05-13",
            underlier_code="US.NVDA",
            expiration="2026-09-18",
            rows=[
                {
                    "code": "US.NVDA.2026-09-18.P100",
                    "strike_time": "2026-09-18",
                    "strike_price": 100,
                    "option_type": "PUT",
                    "lot_size": 100,
                }
            ],
        )

        result = fetch_option_chains(
            gateway=_Gateway(),
            request=OptionChainFetchRequest(
                symbol="NVDA",
                underlier_code="US.NVDA",
                expirations=["2026-09-18"],
                option_types="put",
                base_dir=root,
                asof_date="2026-05-14",
                chain_cache=True,
                max_wait_sec=1,
            ),
            retry_call=lambda _name, fn, **kwargs: fn(),
        )

    assert result.status == "partial"
    assert result.error_code == "RATE_LIMIT"
    assert result.expiration_statuses["2026-09-18"] == "stale_cache"
    assert result.stale_cache_expirations == ["2026-09-18"]
    assert result.stale_cache_asof_dates == {"2026-09-18": "2026-05-13"}
    assert result.rows[0]["code"] == "US.NVDA.2026-09-18.P100"


def test_chain_fetch_force_refresh_does_not_use_stale_cache_on_rate_limit() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("获取期权链频率太高，请求失败，每30秒最多10次。")

    with TemporaryDirectory() as td:
        root = Path(td)
        cache_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-09-18", option_type_scope="put")
        save_option_chain_shard(
            cache_path,
            asof_date="2026-05-13",
            underlier_code="US.NVDA",
            expiration="2026-09-18",
            rows=[
                {
                    "code": "US.NVDA.2026-09-18.P100",
                    "strike_time": "2026-09-18",
                    "strike_price": 100,
                    "option_type": "PUT",
                    "lot_size": 100,
                }
            ],
        )

        result = fetch_option_chains(
            gateway=_Gateway(),
            request=OptionChainFetchRequest(
                symbol="NVDA",
                underlier_code="US.NVDA",
                expirations=["2026-09-18"],
                option_types="put",
                base_dir=root,
                asof_date="2026-05-14",
                chain_cache=True,
                is_force_refresh=True,
                max_wait_sec=1,
            ),
            retry_call=lambda _name, fn, **kwargs: fn(),
        )

    assert result.status == "error"
    assert result.error_code == "RATE_LIMIT"
    assert result.expiration_statuses["2026-09-18"] == "error"
    assert result.stale_cache_expirations == []
    assert result.rows == []


def test_chain_fetch_ignores_stale_cache_older_than_cache_horizon_on_rate_limit() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("获取期权链频率太高，请求失败，每30秒最多10次。")

    with TemporaryDirectory() as td:
        root = Path(td)
        cache_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-09-18", option_type_scope="put")
        save_option_chain_shard(
            cache_path,
            asof_date="2026-05-06",
            underlier_code="US.NVDA",
            expiration="2026-09-18",
            rows=[
                {
                    "code": "US.NVDA.2026-09-18.P100",
                    "strike_time": "2026-09-18",
                    "strike_price": 100,
                    "option_type": "PUT",
                    "lot_size": 100,
                }
            ],
        )

        result = fetch_option_chains(
            gateway=_Gateway(),
            request=OptionChainFetchRequest(
                symbol="NVDA",
                underlier_code="US.NVDA",
                expirations=["2026-09-18"],
                option_types="put",
                base_dir=root,
                asof_date="2026-05-14",
                chain_cache=True,
                max_wait_sec=1,
            ),
            retry_call=lambda _name, fn, **kwargs: fn(),
        )

    assert result.status == "error"
    assert result.error_code == "RATE_LIMIT"
    assert result.expiration_statuses["2026-09-18"] == "error"
    assert result.stale_cache_expirations == []
    assert result.rows == []


def test_chain_cache_must_cover_explicit_expirations() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import option_chain_shard_cache_path

    root = Path("/tmp/cache-root")
    assert option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28") != option_chain_shard_cache_path(root, "US.NVDA", "2026-04-29")


def test_chain_cache_separates_single_side_option_type_scope() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import option_chain_shard_cache_path

    root = Path("/tmp/cache-root")
    all_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28")
    put_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28", option_type_scope="put")
    call_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28", option_type_scope="call")
    assert all_path != put_path
    assert all_path != call_path
    assert put_path != call_path


def test_chain_cache_does_not_trust_declared_expirations_without_rows() -> None:
    import sys
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
    )

    with TemporaryDirectory() as td:
        root = Path(td)
        declared_only = option_chain_shard_cache_path(root, "HK.TEST", "2026-06-29")
        declared_only.parent.mkdir(parents=True, exist_ok=True)
        assert load_option_chain_shard(declared_only, asof_date="2026-04-28") is None


def test_chain_cache_prune_by_mtime() -> None:
    import sys
    import time
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.opend_symbol_chain_fetching import prune_chain_cache
    from src.application.option_chain_fetching import option_chain_shard_cache_path, save_option_chain_shard

    with TemporaryDirectory() as td:
        root = Path(td)
        # create fake cache files
        p1 = option_chain_shard_cache_path(root, "US.AAPL", "2026-05-15")
        p2 = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-15")
        save_option_chain_shard(
            p1,
            asof_date="2000-01-01",
            underlier_code="US.AAPL",
            expiration="2026-05-15",
            rows=[{"code": "US.AAPL.2026-05-15.P100"}],
        )
        save_option_chain_shard(
            p2,
            asof_date="2000-01-01",
            underlier_code="US.NVDA",
            expiration="2026-05-15",
            rows=[{"code": "US.NVDA.2026-05-15.P100"}],
        )
        # set p1 very old, p2 recent
        old = time.time() - 10 * 86400
        os_utime = __import__('os').utime
        os_utime(p1, (old, old))
        prune_chain_cache(root, keep_days=7)
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
        assert result.rate_gate_wait_sec == 0.0
        assert result.to_meta()["rate_gate_wait_sec"] == 0.0
        assert result.from_cache_expirations == ["2026-05-15"]
        assert result.rows[0]["code"] == "US.PDD.2026-05-15.P100"
        assert result.frame is not None
        assert result.frame.iloc[0]["code"] == "US.PDD.2026-05-15.P100"


def test_option_chain_single_side_request_passes_option_type_to_opend() -> None:
    import sys
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import OptionChainFetchRequest, fetch_option_chains

    with TemporaryDirectory() as td:
        root = Path(td)
        captured: list[dict[str, Any]] = []

        class _Gateway:
            def get_option_chain(self, **kwargs):  # noqa: ANN001
                captured.append(dict(kwargs))
                return [{"code": "US.PDD.2026-05-15.P100", "strike_time": "2026-05-15", "option_type": "PUT"}]

        result = fetch_option_chains(
            gateway=_Gateway(),
            request=OptionChainFetchRequest(
                symbol="PDD",
                underlier_code="US.PDD",
                expirations=["2026-05-15"],
                option_types="put",
                base_dir=root,
                asof_date="2026-04-30",
                chain_cache=False,
            ),
            retry_call=lambda _name, fn, **kwargs: fn(),
        )

        assert result.status == "ok"
        assert captured[0]["option_type"] == "PUT"


def test_option_chain_legacy_option_type_fallback_records_rate_limit() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import OptionChainFetchRequest, _fetch_one_chain

    class _Limiter:
        def __init__(self) -> None:
            self.recorded = 0

        def acquire(self) -> float:
            return 0.0

        def record_rate_limit(self) -> None:
            self.recorded += 1

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN001
            if "option_type" in kwargs:
                raise TypeError("got an unexpected keyword argument 'option_type'")
            raise RuntimeError("rate limit")

    limiter = _Limiter()
    try:
        _fetch_one_chain(
            _Gateway(),
            OptionChainFetchRequest(symbol="PDD", underlier_code="US.PDD", option_types="put"),
            cast(Any, limiter),
            "2026-05-15",
        )
    except RuntimeError as exc:
        assert "rate limit" in str(exc)
    else:
        raise AssertionError("expected fallback OpenD call to raise rate limit")

    assert limiter.recorded == 1


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
    import time
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.application.option_chain_fetching import FileRateLimiter

    with TemporaryDirectory() as td:
        state_path = Path(td) / "limiter.json"
        one = FileRateLimiter(state_path=state_path, max_calls=2, window_sec=0.05, max_wait_sec=2.0, clock=time.monotonic)
        two = FileRateLimiter(state_path=state_path, max_calls=2, window_sec=0.05, max_wait_sec=2.0, clock=time.monotonic)

        started = time.monotonic()
        one.acquire()
        two.acquire()
        one.acquire()

        assert time.monotonic() - started >= 0.045


def test_save_outputs_preserves_existing_parsed_csv_on_fetch_error() -> None:
    import sys
    from tempfile import TemporaryDirectory

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    import src.application.opend_symbol_outputs as m

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
