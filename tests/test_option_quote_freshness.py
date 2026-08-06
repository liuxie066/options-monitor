from __future__ import annotations

from datetime import datetime, timezone

from domain.domain.quote_freshness import evaluate_option_quote_freshness


def test_us_quote_within_five_minutes_is_rank_eligible() -> None:
    decision = evaluate_option_quote_freshness(
        market="US",
        update_time="2026-04-01 10:56:00",
        now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
    )

    assert decision.rank_eligible
    assert decision.status == "fresh_active_session"
    assert decision.age_seconds == 240


def test_us_quote_older_than_five_minutes_fails_during_active_session() -> None:
    decision = evaluate_option_quote_freshness(
        market="US",
        update_time="2026-04-01 10:54:59",
        now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
    )

    assert not decision.rank_eligible
    assert decision.status == "stale_during_active_session"


def test_closed_market_quote_is_observation_only() -> None:
    decision = evaluate_option_quote_freshness(
        market="US",
        update_time="2026-04-01 15:59:00",
        now_utc=datetime(2026, 4, 1, 21, 0, tzinfo=timezone.utc),
    )

    assert not decision.rank_eligible
    assert decision.status == "latest_closed_session_observation"


def test_hk_lunch_break_keeps_morning_quote_as_observation_only() -> None:
    decision = evaluate_option_quote_freshness(
        market="HK",
        update_time="2026-04-01 11:59:00",
        now_utc=datetime(2026, 4, 1, 4, 30, tzinfo=timezone.utc),
    )

    assert not decision.rank_eligible
    assert decision.status == "latest_closed_session_observation"


def test_missing_raw_opend_update_time_fails_closed() -> None:
    decision = evaluate_option_quote_freshness(
        market="US",
        update_time=None,
        now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
    )

    assert not decision.rank_eligible
    assert decision.status == "quote_update_time_missing_or_invalid"
