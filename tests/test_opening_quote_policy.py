from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

import pytest

from domain.domain.opening_quote_policy import resolve_closed_market_reference


def _observation(**changes: Any) -> dict[str, Any]:
    return {
        "code": "US.NVDA",
        "market": "US",
        "last_price": 120.5,
        "market_state": "CLOSED",
        "sec_status": "NORMAL",
        "suspension": False,
        "observed_at_utc": "2026-07-06T20:00:00+00:00",
        **changes,
    }


def _calendar(
    sessions: dict[str, str] | None = None,
    *,
    start: str = "2026-07-01",
    end: str = "2026-07-10",
    **changes: Any,
) -> dict[str, Any]:
    return {
        "request_start": start,
        "request_end": end,
        "sessions": sessions if sessions is not None else {
            "2026-07-02": "WHOLE",
            "2026-07-06": "WHOLE",
            "2026-07-07": "WHOLE",
            "2026-07-08": "WHOLE",
            "2026-07-09": "WHOLE",
            "2026-07-10": "WHOLE",
        },
        "coverage_complete": True,
        "pagination_complete": True,
        **changes,
    }


def _resolve(
    now: str = "2026-07-06T22:00:00+00:00",
    *,
    observation: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return resolve_closed_market_reference(
        observation=observation if observation is not None else _observation(),
        calendar=calendar if calendar is not None else _calendar(),
        now_utc=datetime.fromisoformat(now),
    )


def _hk_observation(**changes: Any) -> dict[str, Any]:
    return _observation(
        code="HK.09988",
        market="HK",
        observed_at_utc="2026-07-06T08:08:00+00:00",
        **changes,
    )


def test_after_close_returns_exact_reference_contract_and_preserves_inputs() -> None:
    observation = _observation(
        status="market_inactive",
        reason_code="market_not_continuous",
        age_seconds=7200,
        snapshot_received_at_utc="2026-07-06T21:59:59+00:00",
    )
    calendar = _calendar()
    before = deepcopy((observation, calendar))

    assert _resolve(observation=observation, calendar=calendar) == {
        "market": "US",
        "reference_date": "2026-07-06",
        "underlying_observed_at_utc": "2026-07-06T20:00:00+00:00",
        "valid_until_utc": "2026-07-07T13:30:00+00:00",
        "market_state": "CLOSED",
    }
    assert (observation, calendar) == before


@pytest.mark.parametrize("now", ["2026-07-11T18:00:00Z", "2026-07-12T23:00:00Z"])
def test_weekend_uses_last_trading_day_until_next_actual_open(now: str) -> None:
    result = _resolve(
        now,
        observation=_observation(observed_at_utc="2026-07-10T20:00:00Z"),
        calendar=_calendar(
            {"2026-07-10": "WHOLE", "2026-07-13": "WHOLE"},
            start="2026-07-09",
            end="2026-07-13",
        ),
    )
    assert result["reference_date"] == "2026-07-10"
    assert result["valid_until_utc"] == "2026-07-13T13:30:00+00:00"


@pytest.mark.parametrize("now", ["2026-07-03T20:00:00Z", "2026-07-05T20:00:00Z"])
def test_holiday_gap_uses_authoritative_calendar(now: str) -> None:
    result = _resolve(
        now,
        observation=_observation(observed_at_utc="2026-07-02T20:00:00Z"),
    )
    assert result["reference_date"] == "2026-07-02"
    assert result["valid_until_utc"] == "2026-07-06T13:30:00+00:00"


def test_provider_calendar_can_schedule_a_weekend_open() -> None:
    calendar = _calendar({"2026-07-06": "WHOLE", "2026-07-11": "WHOLE"}, end="2026-07-11")
    result = _resolve(calendar=calendar)
    assert result["valid_until_utc"] == "2026-07-11T13:30:00+00:00"
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve("2026-07-11T13:30:00Z", calendar=calendar)


def test_premarket_can_use_previous_reference_only_before_actual_open() -> None:
    observation = _observation(
        observed_at_utc="2026-07-02T20:00:00Z",
        market_state="PRE_MARKET_BEGIN",
    )
    result = _resolve("2026-07-06T13:29:59Z", observation=observation)
    assert result["reference_date"] == "2026-07-02"
    assert result["valid_until_utc"] == "2026-07-06T13:30:00+00:00"
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve("2026-07-06T13:30:00Z", observation=observation)


@pytest.mark.parametrize("now", ["2026-07-06T13:30:00Z", "2026-07-06T19:59:59Z"])
def test_closed_provider_state_cannot_override_regular_us_session(now: str) -> None:
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve(now, observation=_observation(observed_at_utc="2026-07-02T20:00:00Z"))


def test_exact_regular_close_is_inactive() -> None:
    result = _resolve("2026-07-06T20:00:00Z")
    assert result["reference_date"] == "2026-07-06"


def test_hk_lunch_allows_same_day_reference_and_expires_at_afternoon_open() -> None:
    observation = _observation(
        code="HK.09988", market="HK", market_state="REST", observed_at_utc="2026-07-06T04:00:00Z",
    )
    # A known afternoon open on the same date is sufficient future coverage.
    calendar = _calendar({"2026-07-06": "WHOLE"}, end="2026-07-06")
    result = _resolve("2026-07-06T04:30:00Z", observation=observation, calendar=calendar)
    assert result == {
        "market": "HK",
        "reference_date": "2026-07-06",
        "underlying_observed_at_utc": "2026-07-06T04:00:00+00:00",
        "valid_until_utc": "2026-07-06T05:00:00+00:00",
        "market_state": "REST",
    }
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve("2026-07-06T05:00:00Z", observation=observation, calendar=calendar)


@pytest.mark.parametrize(
    "now",
    ["2026-07-06T01:30:00Z", "2026-07-06T03:59:59Z", "2026-07-06T05:00:00Z", "2026-07-06T07:59:59Z"],
)
def test_closed_provider_state_cannot_override_either_hk_session(now: str) -> None:
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve(
            now,
            observation=_observation(code="HK.09988", market="HK", observed_at_utc="2026-07-02T08:00:00Z"),
        )


def test_hk_close_auction_source_update_is_valid_after_regular_close() -> None:
    result = _resolve("2026-07-06T09:00:00Z", observation=_hk_observation())
    assert result["reference_date"] == "2026-07-06"
    assert result["underlying_observed_at_utc"] == "2026-07-06T08:08:00+00:00"
    assert result["valid_until_utc"] == "2026-07-07T01:30:00+00:00"


def test_us_half_day_uses_one_pm_close_and_next_trading_day() -> None:
    calendar = _calendar(
        {"2026-11-27": "MORNING", "2026-11-30": "WHOLE"},
        start="2026-11-26", end="2026-11-30",
    )
    observation = _observation(observed_at_utc="2026-11-27T17:59:00Z")
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve("2026-11-27T17:59:59Z", observation=observation, calendar=calendar)
    result = _resolve("2026-11-27T18:00:00Z", observation=observation, calendar=calendar)
    assert result["reference_date"] == "2026-11-27"
    assert result["valid_until_utc"] == "2026-11-30T14:30:00+00:00"


def test_hk_morning_half_day_has_no_lunch_reopening() -> None:
    calendar = _calendar(
        {"2026-02-16": "MORNING", "2026-02-20": "WHOLE"},
        start="2026-02-13", end="2026-02-20",
    )
    observation = _observation(code="HK.09988", market="HK", observed_at_utc="2026-02-16T04:00:00Z")
    result = _resolve("2026-02-16T05:00:00Z", observation=observation, calendar=calendar)
    assert result["reference_date"] == "2026-02-16"
    assert result["valid_until_utc"] == "2026-02-20T01:30:00+00:00"


def test_hk_afternoon_half_day_uses_its_actual_first_open() -> None:
    calendar = _calendar({"2026-07-06": "WHOLE", "2026-07-07": "AFTERNOON", "2026-07-08": "WHOLE"})
    result = _resolve("2026-07-07T04:00:00Z", observation=_hk_observation(), calendar=calendar)
    assert result["reference_date"] == "2026-07-06"
    assert result["valid_until_utc"] == "2026-07-07T05:00:00+00:00"
    with pytest.raises(ValueError, match="^regular_session_active$"):
        _resolve("2026-07-07T05:00:00Z", observation=_hk_observation(), calendar=calendar)
    observation = _observation(code="HK.09988", market="HK", observed_at_utc="2026-07-07T08:00:00Z")
    result = _resolve("2026-07-07T09:00:00Z", observation=observation, calendar=calendar)
    assert result["reference_date"] == "2026-07-07"
    assert result["valid_until_utc"] == "2026-07-08T01:30:00+00:00"
    observation["observed_at_utc"] = "2026-07-07T04:59:59Z"
    with pytest.raises(ValueError, match="^observation_before_open$"):
        _resolve("2026-07-07T09:00:00Z", observation=observation, calendar=calendar)


def test_us_afternoon_day_type_is_unavailable_instead_of_guessed() -> None:
    with pytest.raises(ValueError, match="^calendar_day_type_unsupported$"):
        _resolve(calendar=_calendar({"2026-07-06": "AFTERNOON", "2026-07-07": "WHOLE"}))


@pytest.mark.parametrize(
    ("source", "now", "next_day", "expected_open"),
    [
        ("2026-03-06T21:00:00Z", "2026-03-08T16:00:00Z", "2026-03-09", "2026-03-09T13:30:00+00:00"),
        ("2026-10-30T20:00:00Z", "2026-11-01T17:00:00Z", "2026-11-02", "2026-11-02T14:30:00+00:00"),
    ],
)
def test_us_next_open_respects_dst_transitions(source: str, now: str, next_day: str, expected_open: str) -> None:
    source_day = source[:10]
    calendar = _calendar({next_day: "WHOLE", source_day: "WHOLE"}, start="2026-01-01", end=next_day)
    result = _resolve(now, observation=_observation(observed_at_utc=source), calendar=calendar)
    assert result["reference_date"] == source_day
    assert result["valid_until_utc"] == expected_open


def test_reference_dates_use_market_timezone_instead_of_utc_date() -> None:
    result = _resolve(
        "2026-07-07T01:00:00Z",
        observation=_observation(observed_at_utc="2026-07-07T00:00:00Z", market_state="AFTER_HOURS_BEGIN"),
    )
    assert result["reference_date"] == "2026-07-06"
    assert result["underlying_observed_at_utc"] == "2026-07-07T00:00:00+00:00"
    assert result["valid_until_utc"] == "2026-07-07T13:30:00+00:00"


def test_explicit_source_and_now_offsets_are_converted_to_utc() -> None:
    result = _resolve(
        "2026-07-06T18:00:00-04:00",
        observation=_observation(observed_at_utc="2026-07-06T16:00:00-04:00"),
    )
    assert result["underlying_observed_at_utc"] == "2026-07-06T20:00:00+00:00"
    assert result["valid_until_utc"] == "2026-07-07T13:30:00+00:00"


def test_prior_trading_day_source_is_stale_after_new_day_has_traded() -> None:
    with pytest.raises(ValueError, match="^reference_date_mismatch$"):
        _resolve(observation=_observation(observed_at_utc="2026-07-02T20:00:00Z"))


def test_hk_lunch_cannot_use_prior_day_close() -> None:
    with pytest.raises(ValueError, match="^reference_date_mismatch$"):
        _resolve(
            "2026-07-06T04:30:00Z",
            observation=_observation(code="HK.09988", market="HK", observed_at_utc="2026-07-02T08:00:00Z"),
        )


@pytest.mark.parametrize("source", ["2026-07-06T00:00:00Z", "2026-07-06T13:29:59Z"])
def test_source_must_be_on_reference_day_at_or_after_first_open(source: str) -> None:
    reason = "reference_date_mismatch" if source.endswith("00:00:00Z") else "observation_before_open"
    with pytest.raises(ValueError, match=f"^{reason}$"):
        _resolve(observation=_observation(observed_at_utc=source))


def test_source_at_first_open_is_eligible_as_historical_reference_after_close() -> None:
    result = _resolve(observation=_observation(observed_at_utc="2026-07-06T13:30:00Z"))
    assert result["underlying_observed_at_utc"] == "2026-07-06T13:30:00+00:00"


def test_future_source_is_rejected_without_replacing_source_with_receipt_time() -> None:
    observation = _observation(
        observed_at_utc="2026-07-06T22:00:01Z",
        snapshot_received_at_utc="2026-07-06T21:59:59Z",
    )
    with pytest.raises(ValueError, match="^observation_in_future$"):
        _resolve(observation=observation)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"observed_at_utc": None}, "observed_at_utc_invalid"),
        ({"observed_at_utc": "garbage"}, "observed_at_utc_invalid"),
        ({"observed_at_utc": "2026-07-06T20:00:00"}, "observed_at_utc_timezone_required"),
        ({"market": "SG"}, "market_invalid"),
        ({"market": []}, "market_invalid"),
        ({"code": "HK.09988"}, "underlier_code_invalid"),
        ({"code": "US."}, "underlier_code_invalid"),
        ({"code": None}, "underlier_code_invalid"),
    ],
)
def test_invalid_observation_metadata_uses_stable_reason_codes(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValueError, match=f"^{reason}$"):
        _resolve(observation=_observation(**changes))


@pytest.mark.parametrize("field", ["market", "code", "sec_status", "suspension", "last_price", "market_state", "observed_at_utc"])
def test_observation_requires_explicit_fields(field: str) -> None:
    observation = _observation()
    del observation[field]
    with pytest.raises(ValueError):
        _resolve(observation=observation)


@pytest.mark.parametrize(
    "changes",
    [
        {"sec_status": None},
        {"sec_status": "SUSPENDED"},
        {"sec_status": "UNKNOWN"},
        {"suspension": True},
        {"suspension": None},
        {"suspension": 0},
        {"suspension": "False"},
    ],
)
def test_security_must_be_explicitly_normal_and_unsuspended(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="^security_not_normal$"):
        _resolve(observation=_observation(**changes))


@pytest.mark.parametrize("price", [None, True, False, "120.5", 0, -1, float("nan"), float("inf"), float("-inf"), 10**1000])
def test_price_must_be_positive_finite_number(price: Any) -> None:
    with pytest.raises(ValueError, match="^underlier_price_invalid$"):
        _resolve(observation=_observation(last_price=price))


@pytest.mark.parametrize(
    "state",
    [
        "CLOSED", "REST", "WAITING_OPEN", "PRE_MARKET_BEGIN", "PRE_MARKET_END", "AFTER_HOURS_BEGIN",
        "AFTER_HOURS_END", "AFTERNOON_END", "NIGHT_OPEN", "NIGHT_END", "NIGHT", "OVERNIGHT_BEGIN", "OVERNIGHT_END", "OVERNIGHT",
    ],
)
def test_known_inactive_provider_states_allow_reference(state: str) -> None:
    assert _resolve(observation=_observation(market_state=state))["market_state"] == state


@pytest.mark.parametrize(
    "state",
    [None, "", "NONE", "N/A", "UNKNOWN", "UNRECOGNIZED", "MORNING", "AFTERNOON", "AUCTION", "HK_CAS", "CLOSE_AUCTION", "TRADE_AUCTION", "TRADE_AT_LAST", []],
)
def test_unknown_live_and_active_auction_states_cannot_authorize_reference(state: Any) -> None:
    with pytest.raises(ValueError, match="^market_state_not_inactive$"):
        _resolve(observation=_observation(market_state=state))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"coverage_complete": False}, "calendar_incomplete"),
        ({"coverage_complete": None}, "calendar_incomplete"),
        ({"coverage_complete": 1}, "calendar_incomplete"),
        ({"pagination_complete": False}, "calendar_incomplete"),
        ({"pagination_complete": "true"}, "calendar_incomplete"),
        ({"request_start": None}, "calendar_invalid"),
        ({"request_start": "20260701"}, "calendar_invalid"),
        ({"request_end": "2026-02-30"}, "calendar_invalid"),
        ({"request_start": "2026-07-11"}, "calendar_invalid"),
        ({"request_start": "2026-07-06"}, "calendar_coverage_insufficient"),
        ({"request_end": "2026-07-05"}, "calendar_coverage_insufficient"),
        ({"sessions": None}, "calendar_invalid"),
        ({"sessions": []}, "calendar_invalid"),
        ({"sessions": {"2026-07-11": "WHOLE"}}, "calendar_invalid"),
        ({"sessions": {"2026-07-00": "WHOLE"}}, "calendar_invalid"),
        ({"sessions": {"20260706": "WHOLE"}}, "calendar_invalid"),
        ({"sessions": {"2026-07-06": "HOLIDAY"}}, "calendar_invalid"),
        ({"sessions": {"2026-07-06": []}}, "calendar_invalid"),
    ],
)
def test_partial_or_invalid_calendar_is_unavailable(changes: dict[str, Any], reason: str) -> None:
    calendar = {**_calendar(), **changes}
    with pytest.raises(ValueError, match=f"^{reason}$"):
        _resolve(calendar=calendar)


@pytest.mark.parametrize("field", ["coverage_complete", "pagination_complete", "request_start", "request_end", "sessions"])
def test_calendar_requires_all_normalized_coverage_fields(field: str) -> None:
    calendar = _calendar()
    del calendar[field]
    with pytest.raises(ValueError):
        _resolve(calendar=calendar)


@pytest.mark.parametrize("calendar", [None, [], {}])
def test_missing_calendar_cannot_fall_back_to_weekend_guess(calendar: Any) -> None:
    reason = "calendar_incomplete" if calendar == {} else "calendar_invalid"
    with pytest.raises(ValueError, match=f"^{reason}$"):
        resolve_closed_market_reference(
            observation=_observation(), calendar=calendar, now_utc=datetime.fromisoformat("2026-07-06T22:00:00Z"),
        )


def test_empty_calendar_cannot_supply_a_reference_session() -> None:
    with pytest.raises(ValueError, match="^reference_session_unavailable$"):
        _resolve(calendar=_calendar({}))


def test_calendar_must_include_a_known_future_opening() -> None:
    with pytest.raises(ValueError, match="^next_open_unavailable$"):
        _resolve(calendar=_calendar({"2026-07-06": "WHOLE"}))


def test_calendar_end_before_now_is_insufficient_even_over_nontrading_days() -> None:
    calendar = _calendar({"2026-07-06": "WHOLE"}, end="2026-07-06")
    with pytest.raises(ValueError, match="^calendar_coverage_insufficient$"):
        _resolve("2026-07-07T05:00:00Z", calendar=calendar)


def test_calendar_start_must_precede_source_date_even_across_holiday_gap() -> None:
    with pytest.raises(ValueError, match="^calendar_coverage_insufficient$"):
        _resolve(
            "2026-07-05T20:00:00Z",
            observation=_observation(observed_at_utc="2026-07-02T20:00:00Z"),
            calendar=_calendar(start="2026-07-02"),
        )


def test_now_requires_an_explicit_timezone() -> None:
    with pytest.raises(ValueError, match="^now_utc_timezone_required$"):
        _resolve("2026-07-06T22:00:00")


@pytest.mark.parametrize("now", [None, "2026-07-06T22:00:00Z"])
def test_now_must_be_a_datetime(now: Any) -> None:
    with pytest.raises(ValueError, match="^now_utc_invalid$"):
        resolve_closed_market_reference(observation=_observation(), calendar=_calendar(), now_utc=now)


def test_observation_must_be_a_mapping() -> None:
    with pytest.raises(ValueError, match="^observation_invalid$"):
        resolve_closed_market_reference(
            observation=None, calendar=_calendar(), now_utc=datetime.fromisoformat("2026-07-06T22:00:00Z"),
        )
