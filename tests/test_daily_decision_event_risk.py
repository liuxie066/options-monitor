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
