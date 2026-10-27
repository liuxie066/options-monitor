from datetime import date

import pytest

from src.application import earnings_calendar as earnings
from src.infrastructure.futu_gateway import FutuGatewayCapabilityUnavailableError
from test_earnings_calendar import _Gateway, _SCAN_AT_UTC, _event


def _request():
    return dict(
        market="US", scan_date=date(2026, 8, 6), scan_at_utc=_SCAN_AT_UTC,
        expirations_by_underlier={"NVDA": ["2026-08-12", "2026-08-21"]},
    )


def _fetch(gateway, **kwargs):
    return earnings.fetch_market_earnings_calendar(
        gateway=gateway, **_request(), now_fn=lambda: _SCAN_AT_UTC, **kwargs,
    )


def test_handoffs_are_immutable_target_only_and_preserve_default_snapshot():
    responses = [[_event("NVDA", "2026-08-10"), _event("AAPL", "2026-08-11")], [], []]
    handoffs = []
    snapshot = _fetch(_Gateway(responses), on_interval=handoffs.append)
    assert snapshot == _fetch(_Gateway(responses))
    assert {row["security"] for row in snapshot["events"]} == {"US.NVDA", "US.AAPL"}
    first = handoffs[0]
    assert (first["source"], first["market"], first["scan_date"]) == ("opend", "US", "2026-08-06")
    assert isinstance(first["events"], tuple)
    assert first["row_count"] == 1
    assert first["events"][0]["security"] == "US.NVDA"
    with pytest.raises(TypeError):
        first["status"] = "changed"
    with pytest.raises(TypeError):
        first["events"][0]["earnings_date"] = "2026-08-09"
    partial = earnings.earnings_calendar_snapshot_from_intervals(**_request(), intervals=handoffs)
    earnings.validate_earnings_calendar_snapshot(partial)
    assert partial["status"] == "ready"
    assert partial["evidence_by_underlier"] == snapshot["evidence_by_underlier"]


@pytest.mark.parametrize("response", [
    RuntimeError("provider failed"),
    [_event("HK.00700", "2026-08-10")],
    [_event("NVDA", "2026-08-20")],
    [{"security": "NVDA"}],
])
def test_failed_or_invalid_intervals_are_handed_off_without_events(response):
    handoffs = []
    snapshot = _fetch(_Gateway([response, [], []]), on_interval=handoffs.append)
    assert handoffs[0]["status"] == "data_unavailable"
    assert handoffs[0]["events"] == ()
    assert handoffs[0]["result_hash"] is None
    assert earnings.earnings_calendar_snapshot_from_intervals(
        **_request(), intervals=handoffs,
    ) == snapshot


def test_unsupported_capability_hands_off_failures_without_more_calls():
    gateway = _Gateway([FutuGatewayCapabilityUnavailableError(
        "unsupported",
        capability="get_earnings_calendar", reason_code="opend_earnings_calendar_unsupported",
    )])
    handoffs = []
    _fetch(gateway, on_interval=handoffs.append)
    assert len(gateway.calls) == 1
    assert len(handoffs) == 3
    assert all(row["status"] == "data_unavailable" for row in handoffs)


@pytest.mark.parametrize("fail_late", [False, True])
def test_late_second_interval_preserves_first_and_short_expiry(monkeypatch, fail_late):
    clock = [1.0]
    monkeypatch.setattr(earnings, "monotonic", lambda: clock[0])
    handoffs = []

    class Gateway(_Gateway):
        def get_earnings_calendar(self, **kwargs):
            result = super().get_earnings_calendar(**kwargs)
            if len(self.calls) == 2:
                clock[0] = 5.0
                if fail_late:
                    raise RuntimeError("late failure")
            return result

    gateway = Gateway([[_event("NVDA", "2026-08-10")], [], []])
    with pytest.raises(TimeoutError, match="deadline"):
        _fetch(gateway, deadline=5.0, on_interval=handoffs.append)
    assert len(gateway.calls) == 2
    assert len(handoffs) == 1
    snapshot = earnings.earnings_calendar_snapshot_from_intervals(**_request(), intervals=handoffs)
    assert [row["status"] for row in snapshot["intervals"]] == ["ok", "data_unavailable", "data_unavailable"]
    short = earnings.project_earnings_for_expiry(snapshot, underlier_code="NVDA", expiration="2026-08-12")
    long = earnings.project_earnings_for_expiry(snapshot, underlier_code="NVDA", expiration="2026-08-21")
    assert short["hard_coverage_status"] == "complete"
    assert long["hard_coverage_status"] == "partial"
    assert long["events"][0]["earnings_date"] == "2026-08-10"
    assert long["events"][0]["days_before_expiration"] == 11


def test_cancel_in_callback_stops_next_call_and_callback_errors_propagate():
    stopped = [False]
    gateway = _Gateway([[], [], []])

    def handoff(_interval):
        stopped[0] = True

    with pytest.raises(TimeoutError, match="cancelled"):
        _fetch(gateway, cancelled=lambda: stopped[0], on_interval=handoff)
    assert len(gateway.calls) == 1

    def broken_callback(_interval):
        raise TimeoutError("collector closed")

    gateway = _Gateway([[], [], []])
    with pytest.raises(TimeoutError, match="collector closed"):
        _fetch(gateway, on_interval=broken_callback)
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("control", [{"deadline": 0.0}, {"cancelled": lambda: True}])
def test_stopped_request_makes_no_provider_calls(control):
    gateway = _Gateway([])
    with pytest.raises(TimeoutError):
        _fetch(gateway, **control)
    assert not gateway.calls


def test_empty_collection_and_duplicate_handoffs_preserve_coverage():
    missing = earnings.earnings_calendar_snapshot_from_intervals(**_request(), intervals=[])
    assert missing["status"] == "data_unavailable"
    assert all(row["status"] == "data_unavailable" for row in missing["intervals"])
    successes, failures = [], []
    _fetch(_Gateway([[_event("NVDA", "2026-08-10")], [], []]), on_interval=successes.append)
    _fetch(_Gateway([RuntimeError("failed"), [], []]), on_interval=failures.append)
    one = earnings.earnings_calendar_snapshot_from_intervals(**_request(), intervals=successes)
    duplicate = earnings.earnings_calendar_snapshot_from_intervals(
        **_request(), intervals=[*successes, *failures, *successes],
    )
    assert one == duplicate
    with pytest.raises(ValueError, match="identity"):
        earnings.earnings_calendar_snapshot_from_intervals(
            **_request(), intervals=[{**successes[0], "market": "HK"}],
        )
    with pytest.raises(ValueError, match="result identity"):
        earnings.earnings_calendar_snapshot_from_intervals(
            **_request(), intervals=[{**successes[0], "events": ()}],
        )


def test_cancel_after_provider_return_and_normalization_never_hands_off(monkeypatch):
    stopped = [False]
    handoffs = []
    original = earnings._normalize_provider_rows

    def normalize(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped[0] = True
        return result

    monkeypatch.setattr(earnings, "_normalize_provider_rows", normalize)
    gateway = _Gateway([[_event("NVDA", "2026-08-10")], [], []])
    with pytest.raises(TimeoutError, match="cancelled"):
        _fetch(gateway, cancelled=lambda: stopped[0], on_interval=handoffs.append)
    assert len(gateway.calls) == 1
    assert not handoffs
