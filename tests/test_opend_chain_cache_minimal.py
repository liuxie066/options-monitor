"""Minimal tests for OpenD option_chain day-cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

def test_chain_cache_helpers_roundtrip(tmp_path: Path) -> None:

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    td = tmp_path
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


def test_chain_cache_fresh_check(tmp_path: Path) -> None:

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    td = tmp_path
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


def test_chain_fetch_uses_stale_cache_on_rate_limit(tmp_path: Path) -> None:

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("获取期权链频率太高，请求失败，每30秒最多10次。")

    td = tmp_path
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


def test_chain_fetch_force_refresh_does_not_use_stale_cache_on_rate_limit(tmp_path: Path) -> None:

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("获取期权链频率太高，请求失败，每30秒最多10次。")

    td = tmp_path
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


def test_chain_fetch_ignores_stale_cache_older_than_cache_horizon_on_rate_limit(tmp_path: Path) -> None:

    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("获取期权链频率太高，请求失败，每30秒最多10次。")

    td = tmp_path
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

    from src.application.option_chain_fetching import option_chain_shard_cache_path

    root = Path("/tmp/cache-root")
    assert option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28") != option_chain_shard_cache_path(root, "US.NVDA", "2026-04-29")


def test_chain_cache_separates_single_side_option_type_scope() -> None:

    from src.application.option_chain_fetching import option_chain_shard_cache_path

    root = Path("/tmp/cache-root")
    all_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28")
    put_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28", option_type_scope="put")
    call_path = option_chain_shard_cache_path(root, "US.NVDA", "2026-05-28", option_type_scope="call")
    assert all_path != put_path
    assert all_path != call_path
    assert put_path != call_path


def test_chain_cache_does_not_trust_declared_expirations_without_rows(tmp_path: Path) -> None:

    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
    )

    td = tmp_path
    root = Path(td)
    declared_only = option_chain_shard_cache_path(root, "HK.TEST", "2026-06-29")
    declared_only.parent.mkdir(parents=True, exist_ok=True)
    assert load_option_chain_shard(declared_only, asof_date="2026-04-28") is None


def test_chain_cache_prune_by_mtime(tmp_path: Path) -> None:
    import time


    from src.application.opend_symbol_chain_fetching import prune_chain_cache
    from src.application.option_chain_fetching import option_chain_shard_cache_path, save_option_chain_shard

    td = tmp_path
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


def test_option_chain_shard_cache_hit_does_not_call_opend(tmp_path: Path) -> None:


    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
        option_chain_shard_cache_path,
        save_option_chain_shard,
    )

    td = tmp_path
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


def test_option_chain_single_side_request_passes_option_type_to_opend(tmp_path: Path) -> None:


    from src.application.option_chain_fetching import OptionChainFetchRequest, fetch_option_chains

    td = tmp_path
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


def test_partitioned_incident_plan_makes_17_calls_for_21_side_date_scopes(
    tmp_path: Path,
) -> None:
    import pandas as pd

    from src.application.opend_symbol_fetching import fetch_symbol
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        StrikeWindowPlan,
        _merge_side_plans,
    )

    calls: list[dict[str, Any]] = []
    snapshot_calls: list[list[str]] = []

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN001, ANN201
            calls.append(dict(kwargs))
            underlier = str(kwargs["code"])
            expiration = str(kwargs["start"])
            requested = kwargs.get("option_type")
            sides = [str(requested)] if requested else ["PUT", "CALL"]
            return pd.DataFrame([
                {
                    "code": f"{underlier}.{expiration}.{side[0]}100",
                    "strike_time": expiration,
                    "strike_price": 100,
                    "option_type": side,
                    "option_standard_type": "STANDARD",
                    "stock_owner": underlier,
                    "stock_type": "DRVT",
                    "suspension": False,
                    "lot_size": 100,
                }
                for side in sides
            ])

        def get_snapshot(self, codes):  # noqa: ANN001, ANN201
            snapshot_calls.append(list(codes))
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "last_price": 1.0,
                        "bid_price": 0.9,
                        "ask_price": 1.1,
                        "bid_vol": 10,
                        "ask_vol": 12,
                        "price_spread": 0.01,
                        "sec_status": "NORMAL",
                        "suspension": False,
                        "option_contract_multiplier": 100,
                    }
                    for code in codes
                ]
            )

    def side_plan(option_type: str, expirations: list[str]) -> OptionSideFetchPlan:
        return OptionSideFetchPlan(
            option_type=cast(Any, option_type),
            min_dte=1,
            max_dte=365,
            explicit_expirations=expirations,
            strike_window=StrikeWindowPlan(
                min_strike=80.0,
                max_strike=120.0,
                source=f"fixture.{option_type}",
            ),
            planning_reason=f"fixture {option_type}",
        )

    shared = [
        "2026-08-21",
        "2026-09-18",
        "2026-10-16",
        "2026-11-20",
    ]
    symbol_plans = [
        (
            "0700.HK",
            [side_plan("put", [*shared, "2026-12-18"]), side_plan("call", shared)],
        ),
        ("9988.HK", [side_plan("put", ["2026-08-28", "2026-09-25", "2026-10-30", "2026-11-27"])]),
        ("9999.HK", [side_plan("put", ["2026-08-14", "2026-09-11", "2026-10-09", "2026-11-13"])]),
        ("1211.HK", [side_plan("call", ["2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06"])]),
    ]
    gateway = _Gateway()
    covered_scopes: set[tuple[str, str, str]] = set()
    for symbol, side_plans in symbol_plans:
        specs = _merge_side_plans(
            symbol=symbol,
            limit_expirations=0,
            host="127.0.0.1",
            port=11111,
            side_plans=side_plans,
        )
        for spec in specs:
            result = fetch_symbol(
                symbol,
                host="127.0.0.1",
                port=11111,
                spot_override=100.0,
                fetch_spot_if_missing=False,
                base_dir=tmp_path,
                option_types=",".join(spec.option_types),
                side_strike_windows=spec.side_strike_windows,
                min_dte=spec.min_dte,
                max_dte=spec.max_dte,
                explicit_expirations=spec.explicit_expirations,
                trading_date="2026-07-01",
                no_retry=True,
                chain_cache=False,
                max_wait_sec=1,
                option_chain_window_sec=0.01,
                option_chain_max_calls=100,
                snapshot_max_wait_sec=1,
                snapshot_window_sec=0.01,
                snapshot_max_calls=100,
                gateway=gateway,
            )
            assert result["meta"]["status"] == "ok"
            assert result["meta"]["snapshot_complete"] is True
            request_scopes = {
                (symbol, option_type, expiration)
                for option_type in spec.option_types
                for expiration in spec.explicit_expirations
            }
            result_scopes = {
                (
                    symbol,
                    str(row["option_type"]).lower(),
                    str(row["expiration"]),
                )
                for row in result["rows"]
            }
            assert result_scopes == request_scopes
            covered_scopes.update(result_scopes)

    expected_scopes = {
        (symbol, plan.option_type, expiration)
        for symbol, side_plans in symbol_plans
        for plan in side_plans
        for expiration in plan.explicit_expirations
    }
    assert len(expected_scopes) == 21
    assert covered_scopes == expected_scopes
    assert len(calls) == 17
    assert sum(len(codes) for codes in snapshot_calls) == 21


def test_put_only_warm_cache_has_full_or_partial_formal_reuse_by_scope(
    tmp_path: Path,
) -> None:
    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
    )

    calls: list[dict[str, Any]] = []

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN001, ANN201
            calls.append(dict(kwargs))
            expiration = str(kwargs["start"])
            requested = kwargs.get("option_type")
            sides = [str(requested)] if requested else ["PUT", "CALL"]
            return [
                {
                    "code": f"HK.00700.{expiration}.{side[0]}100",
                    "strike_time": expiration,
                    "strike_price": 100,
                    "option_type": side,
                }
                for side in sides
            ]

    expirations = [
        "2026-08-21",
        "2026-09-18",
        "2026-10-16",
        "2026-11-20",
        "2026-12-18",
    ]

    def fetch(requested_expirations: list[str], option_types: str):
        return fetch_option_chains(
            gateway=_Gateway(),
            request=OptionChainFetchRequest(
                symbol="0700.HK",
                underlier_code="HK.00700",
                expirations=requested_expirations,
                option_types=option_types,
                base_dir=tmp_path,
                asof_date="2026-07-27",
                chain_cache=True,
                max_calls=100,
                no_retry=True,
            ),
            retry_call=lambda _name, fn, **_kwargs: fn(),
        )

    warm = fetch(expirations, "put")
    matching_formal = fetch(expirations, "put")
    mismatched_formal = fetch(expirations[:4], "put,call")
    exclusive_formal = fetch(expirations[4:], "put")

    assert warm.opend_call_count == 5
    assert matching_formal.opend_call_count == 0
    assert matching_formal.from_cache_expirations == expirations
    assert mismatched_formal.opend_call_count == 4
    assert exclusive_formal.opend_call_count == 0
    assert len(calls) == 9


def test_option_chain_legacy_option_type_fallback_records_rate_limit() -> None:

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
    with pytest.raises(RuntimeError) as _caught:
        _fetch_one_chain(
            _Gateway(),
            OptionChainFetchRequest(symbol="PDD", underlier_code="US.PDD", option_types="put"),
            cast(Any, limiter),
            "2026-05-15",
        )
    exc = _caught.value
    assert "rate limit" in str(exc)

    assert limiter.recorded == 1


def test_option_chain_error_shard_does_not_count_as_cache_coverage(tmp_path: Path) -> None:


    from src.application.option_chain_fetching import (
        load_option_chain_shard,
        option_chain_shard_cache_path,
    )

    td = tmp_path
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


def test_file_rate_limiter_coordinates_instances_through_state_file(tmp_path: Path) -> None:
    import time


    from src.application.option_chain_fetching import FileRateLimiter

    td = tmp_path
    state_path = Path(td) / "limiter.json"
    one = FileRateLimiter(state_path=state_path, max_calls=2, window_sec=0.05, max_wait_sec=2.0, clock=time.monotonic)
    two = FileRateLimiter(state_path=state_path, max_calls=2, window_sec=0.05, max_wait_sec=2.0, clock=time.monotonic)

    started = time.monotonic()
    one.acquire()
    two.acquire()
    one.acquire()

    assert time.monotonic() - started >= 0.045


def test_save_outputs_preserves_existing_parsed_csv_on_fetch_error(tmp_path: Path) -> None:


    import src.application.opend_symbol_outputs as m

    td = tmp_path
    root = Path(td)
    parsed = root / "parsed"
    parsed.mkdir(parents=True)
    csv_path = parsed / "PDD_required_data.csv"
    csv_path.write_text("symbol,option_type,expiration,strike,mid\nPDD,put,2026-05-15,100,1.0\n", encoding="utf-8")

    m.save_outputs(
        Path(__file__).resolve().parents[1],
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


def test_save_outputs_preserves_existing_parsed_csv_on_nonempty_fetch_error(tmp_path: Path) -> None:


    import src.application.opend_symbol_outputs as m

    td = tmp_path
    root = Path(td)
    parsed = root / "parsed"
    parsed.mkdir(parents=True)
    csv_path = parsed / "PDD_required_data.csv"
    csv_path.write_text("symbol,option_type,expiration,strike,mid\nPDD,put,2026-05-15,100,1.0\n", encoding="utf-8")

    m.save_outputs(
        Path(__file__).resolve().parents[1],
        "PDD",
        {
            "symbol": "PDD",
            "rows": [
                {
                    "symbol": "PDD",
                    "option_type": "put",
                    "expiration": "2026-05-15",
                    "dte": 15,
                    "contract_symbol": "US.PDD.2026-05-15.P100",
                    "strike": 100,
                    "spot": 110,
                }
            ],
            "meta": {"source": "opend", "status": "error", "error_code": "RATE_LIMIT", "error": "snapshot failed"},
        },
        output_root=root,
    )

    assert "PDD,put,2026-05-15,100,1.0" in csv_path.read_text(encoding="utf-8")
    raw = json.loads((root / "raw" / "PDD_required_data.json").read_text(encoding="utf-8"))
    assert raw["rows"][0]["contract_symbol"] == "US.PDD.2026-05-15.P100"
    assert raw["meta"]["error_code"] == "RATE_LIMIT"


def test_explicit_option_chain_all_empty_is_success_empty(tmp_path: Path) -> None:
    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            return []

    td = tmp_path
    result = fetch_option_chains(
        gateway=_Gateway(),
        request=OptionChainFetchRequest(
            symbol="NVDA",
            underlier_code="US.NVDA",
            expirations=["2026-08-21", "2026-09-18"],
            base_dir=Path(td),
            chain_cache=False,
        ),
        retry_call=lambda _name, fn, **kwargs: fn(),
    )

    assert result.status == "ok"
    assert result.source_outcome == "success_empty"
    assert result.reason_code == "no_contract_rows"
    assert result.errors == []
    assert set(result.expiration_statuses.values()) == {"empty"}
    assert len(result.diagnostics) == 2


def test_explicit_option_chain_empty_plus_error_fails_closed(tmp_path: Path) -> None:
    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs.get("start") == "2026-09-18":
                raise RuntimeError("provider unavailable")
            return []

    td = tmp_path
    result = fetch_option_chains(
        gateway=_Gateway(),
        request=OptionChainFetchRequest(
            symbol="NVDA",
            underlier_code="US.NVDA",
            expirations=["2026-08-21", "2026-09-18"],
            base_dir=Path(td),
            chain_cache=False,
        ),
        retry_call=lambda _name, fn, **kwargs: fn(),
    )

    assert result.status == "error"
    assert result.source_outcome == "provider_error"
    assert result.expiration_statuses == {
        "2026-08-21": "empty",
        "2026-09-18": "error",
    }


def test_explicit_option_chain_rows_plus_error_fails_closed(tmp_path: Path) -> None:
    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs.get("start") == "2026-09-18":
                raise RuntimeError("provider unavailable")
            return [
                {
                    "code": "US.NVDA.2026-08-21.P100",
                    "strike_time": "2026-08-21",
                    "strike_price": 100,
                    "option_type": "PUT",
                }
            ]

    td = tmp_path
    result = fetch_option_chains(
        gateway=_Gateway(),
        request=OptionChainFetchRequest(
            symbol="NVDA",
            underlier_code="US.NVDA",
            expirations=["2026-08-21", "2026-09-18"],
            base_dir=Path(td),
            chain_cache=False,
        ),
        retry_call=lambda _name, fn, **kwargs: fn(),
    )

    assert result.status == "partial"
    assert result.rows
    assert result.source_outcome == "provider_error"


def test_explicit_option_chain_invalid_response_is_parse_error(tmp_path: Path) -> None:
    from src.application.option_chain_fetching import (
        OptionChainFetchRequest,
        fetch_option_chains,
    )

    class _Gateway:
        def get_option_chain(self, **kwargs):  # noqa: ANN003, ANN201
            return None

    td = tmp_path
    result = fetch_option_chains(
        gateway=_Gateway(),
        request=OptionChainFetchRequest(
            symbol="NVDA",
            underlier_code="US.NVDA",
            expirations=["2026-08-21"],
            base_dir=Path(td),
            chain_cache=False,
        ),
        retry_call=lambda _name, fn, **kwargs: fn(),
    )

    assert result.status == "error"
    assert result.source_outcome == "parse_error"
    assert result.reason_code == "chain_response_invalid"
    assert result.error_code == "PARSE_ERROR"


def test_save_outputs_marks_row_validation_exception_as_parse_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_outputs as outputs
    import src.application.required_data_validation as validation

    monkeypatch.setattr(
        validation,
        "validate_required_rows",
        lambda rows: (_ for _ in ()).throw(ValueError("invalid row")),
    )
    payload = {
        "rows": [{"symbol": "NVDA"}],
        "meta": {
            "status": "ok",
            "source_outcome": "success_rows",
        },
    }

    td = tmp_path
    root = Path(td)
    outputs.save_outputs(
        Path(__file__).resolve().parents[1],
        "NVDA",
        payload,
        output_root=root,
    )
    raw = json.loads(
        (
            root
            / "raw"
            / "NVDA_required_data.json"
        ).read_text(encoding="utf-8")
    )

    assert raw["rows"] == []
    assert raw["meta"]["status"] == "error"
    assert raw["meta"]["source_outcome"] == "parse_error"
    assert raw["meta"]["error_code"] == "ROW_VALIDATION_ERROR"
