from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from src.application.earnings_calendar import (
    earnings_calendar_intervals,
    earnings_calendar_scan_date,
    fetch_market_earnings_calendar,
    prefetch_market_earnings_calendars,
    project_earnings_for_expiry,
    validate_earnings_calendar_snapshot,
)
from src.infrastructure.futu_gateway import (
    FutuGatewayCapabilityUnavailableError,
)


def _us_timestamp(day: int, hour: int) -> float:
    from zoneinfo import ZoneInfo

    return datetime(
        2026,
        8,
        day,
        hour,
        tzinfo=ZoneInfo("America/New_York"),
    ).timestamp()


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    def get_earnings_calendar(self, **kwargs):
        self.calls.append(dict(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.close_calls += 1


def _fetch(gateway: _Gateway, *, expiry: str = "2026-08-21"):
    return fetch_market_earnings_calendar(
        gateway=gateway,
        market="US",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        expirations_by_underlier={"US.NVDA": [expiry]},
        now_fn=lambda: datetime(2026, 8, 6, 16, 1, tzinfo=timezone.utc),
    )


def _rehash(snapshot: dict) -> None:
    payload = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    snapshot["snapshot_hash"] = sha256(encoded).hexdigest()


def test_intervals_are_inclusive_non_overlapping_and_at_most_seven_days() -> None:
    assert earnings_calendar_intervals(
        date(2026, 8, 6),
        date(2026, 8, 21),
    ) == [
        (date(2026, 8, 6), date(2026, 8, 12)),
        (date(2026, 8, 13), date(2026, 8, 19)),
        (date(2026, 8, 20), date(2026, 8, 21)),
    ]


def test_scan_date_uses_each_market_local_calendar_date() -> None:
    scan_at = datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc)
    assert earnings_calendar_scan_date("US", scan_at) == date(2026, 8, 5)
    assert earnings_calendar_scan_date("HK", scan_at) == date(2026, 8, 6)


def test_complete_empty_results_make_absence_authoritative() -> None:
    snapshot = _fetch(_Gateway([[], [], []]))

    evidence = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-21"]
    assert snapshot["status"] == "ready"
    assert snapshot["absence_authoritative"] is True
    assert evidence["status"] == "ready"
    assert evidence["has_earnings_event"] is False
    assert all(item["result_hash"] for item in snapshot["intervals"])


def test_interval_failure_only_blocks_expiries_that_need_it() -> None:
    gateway = _Gateway([[], RuntimeError("interval unavailable"), []])
    snapshot = fetch_market_earnings_calendar(
        gateway=gateway,
        market="US",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        expirations_by_underlier={
            "US.NVDA": ["2026-08-12", "2026-08-21"],
        },
        now_fn=lambda: datetime(2026, 8, 6, 16, 1, tzinfo=timezone.utc),
    )

    short = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-12"]
    long = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-21"]
    assert short["status"] == "ready"
    assert short["absence_authoritative"] is True
    assert long["status"] == "data_unavailable"
    assert len(gateway.calls) == 3


def test_distant_event_does_not_hide_unresolved_hard_window_coverage() -> None:
    event = {
        "security": "US.NVDA",
        "earnings_date": "2026-08-09",
        "earnings_timestamp": _us_timestamp(9, 16),
    }
    snapshot = _fetch(_Gateway([[event], RuntimeError("later failure"), []]))
    evidence = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-21"]

    assert evidence["status"] == "data_unavailable"
    assert evidence["has_earnings_event"] is True
    assert evidence["has_blocking_earnings_event"] is False
    assert evidence["absence_authoritative"] is False
    assert evidence["hard_failed_intervals"]


def test_known_blocking_event_resolves_outcome_despite_other_hard_window_gap() -> None:
    event = {
        "security": "US.NVDA",
        "earnings_date": "2026-08-21",
        "earnings_timestamp": _us_timestamp(21, 16),
    }
    snapshot = _fetch(
        _Gateway([[], RuntimeError("hard-window gap"), [event]])
    )
    evidence = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-21"]

    assert evidence["status"] == "ready"
    assert evidence["has_blocking_earnings_event"] is True
    assert evidence["hard_coverage_status"] == "partial"
    assert evidence["absence_authoritative"] is False


def test_expiry_date_earnings_is_inside_holding_period() -> None:
    event = {
        "security": "US.NVDA",
        "earnings_date": "2026-08-21",
        "earnings_timestamp": _us_timestamp(21, 16),
        "pub_type": "AFTER",
    }
    snapshot = _fetch(_Gateway([[], [], [event]]))
    evidence = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-21"]

    assert evidence["status"] == "ready"
    assert evidence["has_earnings_event"] is True
    assert evidence["has_blocking_earnings_event"] is True
    assert evidence["events"][0]["earnings_date"] == "2026-08-21"


def test_scan_day_is_pending_for_the_full_market_day_regardless_of_timestamp() -> None:
    released = {
        "security": "US.NVDA",
        "earnings_date": "2026-08-06",
        "earnings_timestamp": _us_timestamp(6, 8),
        "pub_type": "BEFORE",
    }
    upcoming = {
        "security": "US.AAPL",
        "earnings_date": "2026-08-06",
        "earnings_timestamp": _us_timestamp(6, 16),
        "pub_type": "AFTER",
    }
    snapshot = fetch_market_earnings_calendar(
        gateway=_Gateway([[released, upcoming]]),
        market="US",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        expirations_by_underlier={
            "US.NVDA": ["2026-08-12"],
            "US.AAPL": ["2026-08-12"],
        },
        now_fn=lambda: datetime(2026, 8, 6, 16, 1, tzinfo=timezone.utc),
    )

    nvda = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-12"]
    aapl = snapshot["evidence_by_underlier"]["US.AAPL"]["2026-08-12"]
    assert nvda["has_earnings_event"] is True
    assert aapl["has_earnings_event"] is True
    assert nvda["has_blocking_earnings_event"] is True
    assert aapl["has_blocking_earnings_event"] is True


def test_scan_day_date_only_is_pending_and_does_not_require_timestamp() -> None:
    snapshot = fetch_market_earnings_calendar(
        gateway=_Gateway(
            [[{"security": "US.NVDA", "earnings_date": "2026-08-06"}]]
        ),
        market="US",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        expirations_by_underlier={
            "US.NVDA": ["2026-08-12"],
            "US.AAPL": ["2026-08-12"],
        },
        now_fn=lambda: datetime(2026, 8, 6, 16, 1, tzinfo=timezone.utc),
    )

    nvda = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-12"]
    aapl = snapshot["evidence_by_underlier"]["US.AAPL"]["2026-08-12"]
    assert nvda["status"] == "ready"
    assert nvda["has_blocking_earnings_event"] is True
    assert aapl["status"] == "ready"


def test_hk_underliers_share_inclusive_day_six_and_nonblocking_day_seven_policy() -> None:
    events = [
        {"security": "HK.00700", "earnings_date": "2026-08-08"},
        {"security": "HK.09992", "earnings_date": "2026-08-07"},
    ]
    snapshot = fetch_market_earnings_calendar(
        gateway=_Gateway([events, []]),
        market="HK",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 6, 0, tzinfo=timezone.utc),
        expirations_by_underlier={
            "HK.00700": ["2026-08-14"],
            "HK.09992": ["2026-08-14"],
        },
    )

    tencent = snapshot["evidence_by_underlier"]["HK.00700"]["2026-08-14"]
    pop_mart = snapshot["evidence_by_underlier"]["HK.09992"]["2026-08-14"]
    assert tencent["blocking_events"][0]["days_before_expiration"] == 6
    assert pop_mart["blocking_events"] == []
    assert pop_mart["nonblocking_events"][0]["days_before_expiration"] == 7


def test_snapshot_validation_rejects_partition_gaps_even_with_fresh_hash() -> None:
    snapshot = _fetch(_Gateway([[], [], []]))
    snapshot["intervals"][1]["start"] = "2026-08-14"
    _rehash(snapshot)

    with pytest.raises(ValueError, match="interval partition"):
        validate_earnings_calendar_snapshot(snapshot)


def test_snapshot_validation_rejects_stored_projection_drift() -> None:
    snapshot = _fetch(_Gateway([[], [], []]))
    projection = snapshot["evidence_by_underlier"]["US.NVDA"]["2026-08-21"]
    projection["hard_window_start"] = "2026-08-14"
    _rehash(snapshot)

    with pytest.raises(ValueError, match="stored projection mismatch"):
        validate_earnings_calendar_snapshot(snapshot)


def test_unsupported_sdk_is_typed_and_not_retried_for_every_interval() -> None:
    error = FutuGatewayCapabilityUnavailableError(
        "unsupported",
        capability="get_earnings_calendar",
        reason_code="opend_earnings_calendar_unsupported",
    )
    gateway = _Gateway([error])
    snapshot = _fetch(gateway)

    assert snapshot["status"] == "data_unavailable"
    assert len(gateway.calls) == 1
    assert {
        item["reason_code"] for item in snapshot["intervals"]
    } == {"opend_earnings_calendar_unsupported"}


def test_provider_dataframe_is_normalized_and_out_of_interval_fails_closed() -> None:
    valid = pd.DataFrame(
        [
            {
                "security": "US.NVDA",
                "earnings_date": "2026-08-09",
                "earnings_timestamp": _us_timestamp(9, 16),
                "pub_type": "AFTER",
                "predict_eps": 999,
            }
        ]
    )
    snapshot = fetch_market_earnings_calendar(
        gateway=_Gateway([valid]),
        market="US",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        expirations_by_underlier={"US.NVDA": ["2026-08-12"]},
    )
    assert snapshot["events"][0].keys() == {
        "security",
        "earnings_date",
        "earnings_timestamp",
        "pub_type",
    }

    unavailable = fetch_market_earnings_calendar(
        gateway=_Gateway(
            [[{"security": "US.NVDA", "earnings_date": "2026-08-13"}]]
        ),
        market="US",
        scan_date=date(2026, 8, 6),
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        expirations_by_underlier={"US.NVDA": ["2026-08-12"]},
    )
    assert unavailable["status"] == "data_unavailable"


def test_prefetch_publishes_one_shared_snapshot_per_market(tmp_path: Path) -> None:
    gateways: list[_Gateway] = []

    def build_gateway(**kwargs):
        gateway = _Gateway([[], []])
        gateways.append(gateway)
        return gateway

    result = prefetch_market_earnings_calendars(
        market_requests={
            "US": {
                "host": "127.0.0.1",
                "port": 11111,
                "scan_date": "2026-08-06",
                "expirations_by_underlier": {
                    "US.NVDA": ["2026-08-13"],
                    "US.AAPL": ["2026-08-13"],
                },
            }
        },
        output_dir=tmp_path / "earnings_calendar",
        scan_at_utc=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
        gateway_builder=build_gateway,
    )

    artifact = json.loads(
        (tmp_path / "earnings_calendar" / "US.json").read_text(encoding="utf-8")
    )
    assert result["market_count"] == 1
    assert len(gateways) == 1
    assert len(gateways[0].calls) == 2
    assert gateways[0].close_calls == 1
    assert set(artifact["evidence_by_underlier"]) == {"US.NVDA", "US.AAPL"}
    assert project_earnings_for_expiry(
        artifact,
        underlier_code="US.NVDA",
        expiration="2026-08-13",
    )["status"] == "ready"
