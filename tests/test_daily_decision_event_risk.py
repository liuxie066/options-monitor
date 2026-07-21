from __future__ import annotations

import pytest

from domain.domain.daily_decision_event_risk import build_candidate_event_risk


def _item(*, status: str = "ok", events=None, coverage=None, **extra):
    return {
        "symbol": "NVDA",
        "selected_provider": "futu",
        "source_status": status,
        "events": [] if events is None else events,
        "coverage": coverage
        or {
            "earnings": {"status": "complete"},
            "ex_dividend": {"status": "complete"},
            "split": {"status": "complete"},
        },
        **extra,
    }


def _build(item, *, reason: str = ""):
    return build_candidate_event_risk(
        symbol="NVDA",
        market_trading_date="2026-07-17",
        expirations={"contract": "2026-08-21"},
        snapshot_item=item,
        snapshot_reason=reason,
    )


@pytest.mark.parametrize(
    ("item", "reason", "expected_reason"),
    [
        (None, "event_snapshot_missing", "event_snapshot_missing"),
        ({"symbol": "NVDA"}, "", "event_source_unavailable"),
        (_item(status="stale"), "", "event_source_stale"),
        (_item(status="error"), "", "event_source_error"),
        (
            _item(
                coverage={
                    "earnings": {"status": "complete"},
                    "ex_dividend": {"status": "partial"},
                    "split": {"status": "complete"},
                }
            ),
            "",
            "event_evidence_incomplete",
        ),
        (_item(status="ok_with_fallback"), "", "fallback_absence_unconfirmed"),
        (_item(events="not-a-list"), "", "event_evidence_malformed"),
    ],
)
def test_unreliable_event_evidence_is_unknown(item, reason: str, expected_reason: str) -> None:
    result = _build(item, reason=reason)

    assert result["user_state"] == "unknown"
    assert result["reason_code"] == expected_reason
    assert result["reliable"] is False


def test_complete_fallback_can_confirm_a_concrete_event() -> None:
    result = _build(
        _item(
            status="ok_with_fallback",
            events=[{"type": "earnings", "date": "2026-08-05"}],
        )
    )

    assert result["user_state"] == "confirmed_event"
    assert result["reliable"] is True


def test_complete_provider_disagreement_is_unknown_conflict() -> None:
    coverage = {
        "earnings": {"status": "complete"},
        "ex_dividend": {"status": "complete"},
        "split": {"status": "complete"},
    }
    result = _build(
        _item(
            events=[{"type": "earnings", "date": "2026-08-05"}],
            source_results={
                "futu": {
                    "source_status": "ok",
                    "events": [{"type": "earnings", "date": "2026-08-05"}],
                    "coverage": coverage,
                },
                "other": {
                    "source_status": "ok",
                    "events": [],
                    "coverage": coverage,
                },
            },
        )
    )

    assert result["user_state"] == "unknown"
    assert result["reason_code"] == "event_evidence_conflict"


def _risk(
    state: str,
    *,
    event_date: str | None = None,
    relation: str = "before_expiration",
    chain: str = "event-chain-futu",
    event_id: str = "event-q2",
    series_id: str = "event-series-earnings",
    anchored: bool = True,
    events: list[dict] | None = None,
):
    event = (
        {
            "event_id": event_id,
            "event_series_id": series_id,
            "event_type": "earnings",
            "event_date": event_date,
            "occurrence_anchor": "2026|Q2" if anchored else "",
            "anchored": anchored,
        }
        if event_date
        else None
    )
    return {
        "user_state": state,
        "reason_code": state,
        "reliable": state != "unknown",
        "evidence_chain_id": chain,
        "nearest_event": event,
        "events": events if events is not None else ([event] if event else []),
        "expiration_relations": (
            {
                "contract": {
                    "expiration": "2026-08-21",
                    "relation": relation,
                    "days_before_expiration": 16,
                }
            }
            if event
            else {}
        ),
        "in_attention_window": relation in {"before_expiration", "on_expiration"} if event else False,
    }


def test_candidate_event_risk_transition_matrix() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    none = _risk("confirmed_none")
    event = _risk("confirmed_event", event_date="2026-08-05")
    unknown = _risk("unknown")

    assert [item["change_type"] for item in candidate_event_risk_transitions(
        none, event, market_trading_date="2026-07-21"
    )] == ["candidate_event_added"]
    assert [item["change_type"] for item in candidate_event_risk_transitions(
        event, unknown, market_trading_date="2026-07-21"
    )] == ["candidate_event_evidence_degraded"]
    assert [item["change_type"] for item in candidate_event_risk_transitions(
        unknown, event, market_trading_date="2026-07-21"
    )] == ["candidate_event_evidence_recovered"]
    assert [item["change_type"] for item in candidate_event_risk_transitions(
        event, none, market_trading_date="2026-07-21"
    )] == ["candidate_event_removed"]


def test_candidate_event_date_change_and_expiry_window_entry_are_material() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    before = _risk("confirmed_event", event_date="2026-08-25", relation="after_expiration")
    after = _risk("confirmed_event", event_date="2026-08-05", relation="before_expiration")

    assert [item["change_type"] for item in candidate_event_risk_transitions(
        before, after, market_trading_date="2026-07-21"
    )] == ["candidate_event_date_changed", "candidate_event_entered_expiry_window"]


def test_candidate_event_removal_requires_same_evidence_chain() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    event = _risk("confirmed_event", event_date="2026-08-05", chain="event-chain-futu")
    none_from_other_chain = _risk("confirmed_none", chain="event-chain-other")

    assert candidate_event_risk_transitions(
        event, none_from_other_chain, market_trading_date="2026-07-21"
    ) == []


def test_unanchored_future_date_correction_requires_unique_occurrence() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    before = _risk(
        "confirmed_event",
        event_date="2026-08-06",
        event_id="event-aug-06",
        anchored=False,
    )
    after = _risk(
        "confirmed_event",
        event_date="2026-08-05",
        event_id="event-aug-05",
        anchored=False,
    )

    assert [item["change_type"] for item in candidate_event_risk_transitions(
        before, after, market_trading_date="2026-07-21"
    )] == ["candidate_event_date_changed"]


def test_elapsed_recurring_event_is_not_misclassified_as_date_correction() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    elapsed = _risk(
        "confirmed_event",
        event_date="2026-07-20",
        event_id="event-q2-date",
        anchored=False,
    )
    next_quarter = _risk(
        "confirmed_event",
        event_date="2026-10-20",
        event_id="event-q3-date",
        anchored=False,
    )

    assert [item["change_type"] for item in candidate_event_risk_transitions(
        elapsed, next_quarter, market_trading_date="2026-07-21"
    )] == ["candidate_event_added"]


def test_freshness_only_event_updates_are_non_material() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    before = _risk("confirmed_event", event_date="2026-08-05")
    after = {**before, "fetched_at": "2026-07-21T15:00:00+00:00", "cache_status": "fetched"}

    assert candidate_event_risk_transitions(
        before, after, market_trading_date="2026-07-21"
    ) == []


def test_multiple_unanchored_dates_in_one_series_are_conflict_unknown() -> None:
    result = _build(
        _item(
            events=[
                {"type": "earnings", "date": "2026-08-05"},
                {"type": "earnings", "date": "2026-08-06"},
            ]
        )
    )

    assert result["user_state"] == "unknown"
    assert result["reason_code"] == "event_evidence_conflict"


def test_old_brief_without_event_risk_does_not_fabricate_evidence_recovery() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    current = _risk("confirmed_event", event_date="2026-08-05")

    assert candidate_event_risk_transitions(
        None, current, market_trading_date="2026-07-21"
    ) == []


def test_same_chain_removal_is_detected_when_a_later_known_event_remains() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    removed = _risk(
        "confirmed_event",
        event_date="2026-08-05",
        event_id="event-earnings-q2",
        series_id="series-earnings",
    )["nearest_event"]
    later = {
        "event_id": "event-dividend-september",
        "event_series_id": "series-dividend",
        "event_type": "ex_dividend",
        "event_date": "2026-09-01",
        "occurrence_anchor": "2026-09-01",
        "anchored": True,
    }
    before = _risk(
        "confirmed_event",
        event_date="2026-08-05",
        event_id="event-earnings-q2",
        series_id="series-earnings",
        events=[removed, later],
    )
    after = _risk(
        "confirmed_event",
        event_date="2026-09-01",
        event_id="event-dividend-september",
        series_id="series-dividend",
        events=[later],
    )

    assert [item["change_type"] for item in candidate_event_risk_transitions(
        before, after, market_trading_date="2026-07-21"
    )] == ["candidate_event_removed"]


def test_combo_event_entry_is_detected_per_leg_expiration() -> None:
    from domain.domain.daily_decision_event_risk import candidate_event_risk_transitions

    before = _risk("confirmed_event", event_date="2026-08-25")
    after = _risk("confirmed_event", event_date="2026-08-25")
    before["expiration_relations"] = {
        "put": {"expiration": "2026-08-21", "relation": "after_expiration"},
        "call": {"expiration": "2026-09-18", "relation": "before_expiration"},
    }
    after["expiration_relations"] = {
        "put": {"expiration": "2026-09-04", "relation": "before_expiration"},
        "call": {"expiration": "2026-09-18", "relation": "before_expiration"},
    }

    assert [item["change_type"] for item in candidate_event_risk_transitions(
        before, after, market_trading_date="2026-07-21"
    )] == ["candidate_event_entered_expiry_window"]
