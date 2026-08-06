from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_market_snapshot_fetching import fetch_option_snapshots
from src.application.opend_market_snapshot_fetching import get_underlier_observation_opend


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
