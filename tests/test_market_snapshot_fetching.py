from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_market_snapshot_fetching import fetch_option_snapshots
from src.application.opend_market_snapshot_fetching import get_underlier_observation_opend
from src.application.opend_market_snapshot_fetching import get_underlier_observations_opend


SNAPSHOT_LIMIT = OpenDEndpointRateLimit(
    window_sec=30.0,
    max_calls=60,
    max_wait_sec=30.0,
)


def test_underlier_observation_binds_snapshot_and_market_state(tmp_path: Path) -> None:
    class Gateway:
        def get_snapshot(self, codes):  # type: ignore[no-untyped-def]
            assert codes == ["US.NVDA"]
            return pd.DataFrame(
                [
                    {
                        "code": "US.NVDA",
                        "last_price": 180.0,
                        "update_time": "2026-08-06 10:59:00",
                        "sec_status": "NORMAL",
                        "suspension": False,
                    }
                ]
            )

        def get_market_state(self, codes):  # type: ignore[no-untyped-def]
            assert codes == ["US.NVDA"]
            return pd.DataFrame(
                [{"code": "US.NVDA", "market_state": "MORNING"}]
            )

    observation = get_underlier_observation_opend(
        Gateway(),
        "US.NVDA",
        market="US",
        base_dir=tmp_path,
        rate_limited_call=lambda **kwargs: kwargs["call"](),
        now_utc=pd.Timestamp("2026-08-06T15:00:00Z").to_pydatetime(),
    )

    assert observation.status == "ready"
    assert observation.last_price == 180.0
    assert observation.market_state == "MORNING"


def test_underlier_observations_batch_and_reconcile_exact_codes(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, list[str]]] = []

    class Gateway:
        def get_snapshot(self, codes):  # type: ignore[no-untyped-def]
            calls.append(("snapshot", list(codes)))
            rows = [
                {
                    "code": code,
                    "last_price": 300.0,
                    "update_time": "2026-08-06 10:59:00",
                    "sec_status": "NORMAL",
                    "suspension": False,
                }
                for code in codes
            ]
            if "US.NVDA" in codes:
                rows = [
                    {
                        "code": "US.NVDA",
                        "last_price": 180.0,
                        "update_time": "2026-08-06 10:59:00",
                        "sec_status": "NORMAL",
                        "suspension": False,
                    },
                    {"code": "US.AAPL", "last_price": 200.0},
                    {"code": "US.AAPL", "last_price": 201.0},
                    {"code": "US.UNRELATED", "last_price": 999.0},
                ]
            return pd.DataFrame(rows)

        def get_market_state(self, codes):  # type: ignore[no-untyped-def]
            calls.append(("state", list(codes)))
            return pd.DataFrame(
                [{"code": code, "market_state": "MORNING"} for code in codes]
            )

    observations = get_underlier_observations_opend(
        Gateway(),
        ["US.NVDA", "US.AAPL", "US.MSFT"],
        market="US",
        base_dir=tmp_path,
        snapshot_limit=SNAPSHOT_LIMIT,
        snapshot_batch_size=2,
        rate_limited_call=lambda **kwargs: kwargs["call"](),
        now_utc=lambda: pd.Timestamp("2026-08-06T15:00:00Z").to_pydatetime(),
    )

    assert calls == [
        ("snapshot", ["US.NVDA", "US.AAPL"]),
        ("state", ["US.NVDA", "US.AAPL"]),
        ("snapshot", ["US.MSFT"]),
        ("state", ["US.MSFT"]),
    ]
    assert set(observations) == {"US.NVDA", "US.AAPL", "US.MSFT"}
    assert observations["US.NVDA"].status == "ready"
    assert observations["US.AAPL"].status == "data_unavailable"
    assert observations["US.MSFT"].status == "ready"


def test_underlier_observations_index_each_response_once(tmp_path: Path) -> None:
    class CountingFrame:
        def __init__(self, rows):  # type: ignore[no-untyped-def]
            self.rows = rows
            self.to_dict_calls = 0

        def to_dict(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.to_dict_calls += 1
            return self.rows

    snapshot = CountingFrame(
        [
            {
                "code": code,
                "last_price": 180.0,
                "update_time": "2026-08-06 10:59:00",
                "sec_status": "NORMAL",
                "suspension": False,
            }
            for code in ("US.NVDA", "US.AAPL")
        ]
    )
    market_state = CountingFrame(
        [
            {"code": code, "market_state": "MORNING"}
            for code in ("US.NVDA", "US.AAPL")
        ]
    )

    class Gateway:
        def get_snapshot(self, _codes):  # type: ignore[no-untyped-def]
            return snapshot

        def get_market_state(self, _codes):  # type: ignore[no-untyped-def]
            return market_state

    observations = get_underlier_observations_opend(
        Gateway(),
        ["US.NVDA", "US.AAPL"],
        market="US",
        base_dir=tmp_path,
        snapshot_limit=SNAPSHOT_LIMIT,
        snapshot_batch_size=2,
        rate_limited_call=lambda **kwargs: kwargs["call"](),
        now_utc=lambda: pd.Timestamp("2026-08-06T15:00:00Z").to_pydatetime(),
    )

    assert set(observations) == {"US.NVDA", "US.AAPL"}
    assert snapshot.to_dict_calls == 1
    assert market_state.to_dict_calls == 1


def test_underlier_observations_attempt_endpoints_independently(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class Gateway:
        def get_snapshot(self, _codes):  # type: ignore[no-untyped-def]
            calls.append("snapshot")
            raise RuntimeError("snapshot unavailable")

        def get_market_state(self, _codes):  # type: ignore[no-untyped-def]
            calls.append("state")
            return pd.DataFrame(
                [{"code": "US.NVDA", "market_state": "MORNING"}]
            )

    observations = get_underlier_observations_opend(
        Gateway(),
        ["US.NVDA"],
        market="US",
        base_dir=tmp_path,
        snapshot_limit=SNAPSHOT_LIMIT,
        snapshot_batch_size=1,
        rate_limited_call=lambda **kwargs: kwargs["call"](),
    )

    assert calls == ["snapshot", "state"]
    assert observations["US.NVDA"].status == "data_unavailable"


def test_underlier_observations_stop_before_next_endpoint_or_chunk(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    max_waits: list[float] = []
    ticks = iter([0.0, 0.0, 0.0, 2.0, 2.0])

    class Gateway:
        def get_snapshot(self, codes):  # type: ignore[no-untyped-def]
            calls.append(("snapshot", list(codes)))
            return pd.DataFrame([{"code": codes[0], "last_price": 1.0}])

        def get_market_state(self, codes):  # type: ignore[no-untyped-def]
            calls.append(("state", list(codes)))
            return pd.DataFrame()

    observations = get_underlier_observations_opend(
        Gateway(),
        ["US.NVDA", "US.AAPL"],
        market="US",
        base_dir=tmp_path,
        snapshot_limit=SNAPSHOT_LIMIT,
        snapshot_batch_size=1,
        stop_monotonic=1.0,
        monotonic=lambda: next(ticks),
        rate_limited_call=lambda **kwargs: (
            max_waits.append(float(kwargs["max_wait_sec"])),
            kwargs["call"](),
        )[1],
    )

    assert calls == [("snapshot", ["US.NVDA"])]
    assert max_waits == [1.0]
    assert set(observations) == {"US.NVDA"}
    assert observations["US.NVDA"].status == "data_unavailable"


def test_underlier_observations_do_not_call_provider_after_rate_limit_deadline(
    tmp_path: Path,
) -> None:
    now = [0.0]
    calls: list[str] = []

    class Gateway:
        def get_snapshot(self, _codes):  # type: ignore[no-untyped-def]
            calls.append("snapshot")
            return pd.DataFrame()

        def get_market_state(self, _codes):  # type: ignore[no-untyped-def]
            calls.append("state")
            return pd.DataFrame()

    def cross_deadline_then_call(**kwargs):  # type: ignore[no-untyped-def]
        now[0] = 1.0
        return kwargs["call"]()

    observations = get_underlier_observations_opend(
        Gateway(),
        ["US.NVDA"],
        market="US",
        base_dir=tmp_path,
        snapshot_limit=SNAPSHOT_LIMIT,
        snapshot_batch_size=1,
        stop_monotonic=1.0,
        monotonic=lambda: now[0],
        rate_limited_call=cross_deadline_then_call,
    )

    assert calls == []
    assert observations["US.NVDA"].status == "data_unavailable"


def _fetch(*, tmp_path: Path, handler, fallback_max_codes: int):  # type: ignore[no-untyped-def]
    class Gateway:
        def get_snapshot(self, codes):  # type: ignore[no-untyped-def]
            return handler(list(codes))

    return fetch_option_snapshots(
        option_codes=["US.NVDA.A", "US.NVDA.B"],
        gateway=Gateway(),
        snapshot_limit=SNAPSHOT_LIMIT,
        base_dir=tmp_path,
        snapshot_batch_size=2,
        snapshot_fallback_max_codes=fallback_max_codes,
        snapshot_fallback_batch_size=1,
        retry_call=lambda _name, call, **_kwargs: call(),
        rate_limited_call=lambda **kwargs: kwargs["call"](),
        classify_error=lambda _exc: "PROVIDER_ERROR",
    )


def test_snapshot_result_reconciles_subset_and_discards_unexpected_codes(tmp_path: Path) -> None:
    result = _fetch(
        tmp_path=tmp_path,
        fallback_max_codes=0,
        handler=lambda _codes: pd.DataFrame(
            [
                {"code": "US.NVDA.A", "last_price": 1.0},
                {"code": "US.NVDA.UNRELATED", "last_price": 99.0},
            ]
        ),
    )

    assert result.requested_codes == frozenset({"US.NVDA.A", "US.NVDA.B"})
    assert result.returned_codes == frozenset({"US.NVDA.A", "US.NVDA.UNRELATED"})
    assert result.missing_codes == frozenset({"US.NVDA.B"})
    assert result.unexpected_codes == frozenset({"US.NVDA.UNRELATED"})
    assert result.complete is False
    assert set(result.snap_map) == {"US.NVDA.A"}
    assert any(error["error_code"] == "SNAPSHOT_COVERAGE_INCOMPLETE" for error in result.errors)


def test_snapshot_result_can_recover_fully_while_retaining_diagnostics(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def handler(codes: list[str]) -> pd.DataFrame:
        calls.append(codes)
        if len(codes) > 1:
            raise RuntimeError("batch provider failure")
        rows = [{"code": codes[0], "last_price": 1.0}]
        if codes[0] == "US.NVDA.A":
            rows.append({"code": "US.NVDA.UNRELATED", "last_price": 99.0})
        return pd.DataFrame(rows)

    result = _fetch(
        tmp_path=tmp_path,
        fallback_max_codes=2,
        handler=handler,
    )

    assert calls == [
        ["US.NVDA.A", "US.NVDA.B"],
        ["US.NVDA.A"],
        ["US.NVDA.B"],
    ]
    assert result.complete is True
    assert result.missing_codes == frozenset()
    assert result.unexpected_codes == frozenset({"US.NVDA.UNRELATED"})
    assert set(result.snap_map) == {"US.NVDA.A", "US.NVDA.B"}
    assert result.fallback_filled == 2
    assert result.fallback_failed == 0
    assert any(error["error_code"] == "PROVIDER_ERROR" for error in result.errors)
    assert any(error["error_code"] == "SNAPSHOT_UNEXPECTED_CODES" for error in result.errors)
    assert not any(error["error_code"] == "SNAPSHOT_COVERAGE_INCOMPLETE" for error in result.errors)


def test_snapshot_result_rejects_duplicate_code_rows_in_one_response(
    tmp_path: Path,
) -> None:
    result = _fetch(
        tmp_path=tmp_path,
        fallback_max_codes=0,
        handler=lambda _codes: pd.DataFrame(
            [
                {"code": "US.NVDA.A", "last_price": 1.0},
                {"code": "US.NVDA.A", "last_price": 9.0},
                {"code": "US.NVDA.B", "last_price": 2.0},
            ]
        ),
    )

    assert result.complete is False
    assert result.missing_codes == frozenset({"US.NVDA.A"})
    assert set(result.snap_map) == {"US.NVDA.B"}
    assert any(
        error["error_code"] == "SNAPSHOT_DUPLICATE_CODES"
        and error["duplicate_codes"] == ["US.NVDA.A"]
        for error in result.errors
    )


def test_snapshot_result_rejects_duplicate_code_across_responses(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    class Gateway:
        def get_snapshot(self, codes):  # type: ignore[no-untyped-def]
            calls.append(list(codes))
            return pd.DataFrame(
                [{"code": "US.NVDA.A", "last_price": float(len(calls))}]
            )

    result = fetch_option_snapshots(
        option_codes=["US.NVDA.A", "US.NVDA.B"],
        gateway=Gateway(),
        snapshot_limit=SNAPSHOT_LIMIT,
        base_dir=tmp_path,
        snapshot_batch_size=1,
        snapshot_fallback_max_codes=0,
        snapshot_fallback_batch_size=1,
        retry_call=lambda _name, call, **_kwargs: call(),
        rate_limited_call=lambda **kwargs: kwargs["call"](),
        classify_error=lambda _exc: "PROVIDER_ERROR",
    )

    assert calls == [["US.NVDA.A"], ["US.NVDA.B"]]
    assert result.complete is False
    assert result.missing_codes == frozenset(
        {"US.NVDA.A", "US.NVDA.B"}
    )
    assert result.snap_map == {}
    assert any(
        error["error_code"] == "SNAPSHOT_DUPLICATE_CODES"
        for error in result.errors
    )


def test_snapshot_result_rejects_duplicate_code_from_fallback(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def handler(codes: list[str]) -> pd.DataFrame:
        calls.append(codes)
        if len(codes) > 1:
            return pd.DataFrame(
                [{"code": "US.NVDA.A", "last_price": 1.0}]
            )
        return pd.DataFrame(
            [
                {"code": codes[0], "last_price": 2.0},
                {"code": codes[0], "last_price": 3.0},
            ]
        )

    result = _fetch(
        tmp_path=tmp_path,
        fallback_max_codes=2,
        handler=handler,
    )

    assert calls == [["US.NVDA.A", "US.NVDA.B"], ["US.NVDA.B"]]
    assert result.complete is False
    assert result.missing_codes == frozenset({"US.NVDA.B"})
    assert set(result.snap_map) == {"US.NVDA.A"}
    assert result.fallback_filled == 0
    assert result.fallback_failed == 1
    assert any(
        error["error_code"] == "SNAPSHOT_DUPLICATE_CODES"
        for error in result.errors
    )
