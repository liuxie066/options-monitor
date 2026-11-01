from datetime import datetime, timedelta, timezone

import pytest

from src.application.position_advice_evidence import current_position_advice_evidence


NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def _evidence(*, quote_changes=None, requirement_changes=None, position=None, now=NOW):
    quote = {
        "snapshot_requested_at_utc": (NOW - timedelta(seconds=3)).isoformat(),
        "snapshot_received_at_utc": (NOW - timedelta(seconds=2)).isoformat(),
        "spot_observed_at_utc": (NOW - timedelta(seconds=4)).isoformat(),
        "underlier_observation_status": "ready", "underlier_sec_status": "NORMAL",
        "option_sec_status": "NORMAL", "option_suspension": False,
        "market_state": "MORNING", **(quote_changes or {}),
    }
    requirement = {
        "market_state_after_snapshot": "MORNING",
        "market_state_received_at_utc": (NOW - timedelta(seconds=1)).isoformat(),
        "quote_rights_received_at_utc": (NOW - timedelta(seconds=1)).isoformat(),
        "quote_rights_status": "ok", "quote_rights_market": "US",
        "quote_rights_underlier": "LV1", "quote_rights_option": "LV2",
        **(requirement_changes or {}),
    }
    return current_position_advice_evidence(
        position=position or {}, quote=quote, requirement=requirement, market="US", now_utc=now,
    )


def test_current_evidence_uses_snapshot_receipt_not_last_trade_time():
    evidence, flags = _evidence(quote_changes={"last_price_update_time": "1999-01-01 10:00:00"})
    assert not flags
    assert evidence["assessment_mode"] == "current"
    assert evidence["current_actionable"] is True
    assert evidence["assessment_as_of_utc"] == "2026-11-02T14:59:58+00:00"


@pytest.mark.parametrize("scope", ["underlier", "option"])
@pytest.mark.parametrize("right", [None, "UNKNOWN", "BMP", "NO", "SF", "DELAYED"])
def test_both_scopes_need_positive_realtime_rights(scope, right):
    evidence, flags = _evidence(requirement_changes={f"quote_rights_{scope}": right})
    assert "realtime_quote_rights_unproven" in flags
    assert evidence["assessment_mode"] == "unavailable"
    assert evidence["current_actionable"] is False


@pytest.mark.parametrize("state", ["CLOSED", "REST", "PRE_MARKET_BEGIN", "AFTER_HOURS_BEGIN", None])
def test_noncontinuous_or_unknown_post_snapshot_state_blocks_current(state):
    evidence, flags = _evidence(requirement_changes={"market_state_after_snapshot": state})
    assert "market_not_continuous" in flags
    assert evidence["current_actionable"] is False


@pytest.mark.parametrize("field,flag", [
    ("snapshot_received_at_utc", "option_snapshot_not_current"),
    ("spot_observed_at_utc", "underlier_observation_not_current"),
    ("snapshot_requested_at_utc", "option_snapshot_interval_invalid"),
])
def test_missing_freshness_proofs_are_not_replaced_by_recent_trade(field, flag):
    evidence, flags = _evidence(quote_changes={field: None, "last_price_observed_at_utc": NOW.isoformat()})
    assert flag in flags
    assert evidence["current_actionable"] is False


@pytest.mark.parametrize("overrides", [
    {"market_state_received_at_utc": "2026-11-02T14:59:50Z"},
    {"quote_rights_received_at_utc": "2026-11-02T14:59:50Z"},
    {"quote_rights_market": "HK"},
    {"quote_rights_status": "unavailable"},
])
def test_proofs_bind_market_and_post_snapshot_time(overrides):
    evidence, flags = _evidence(requirement_changes=overrides)
    assert flags
    assert evidence["assessment_mode"] == "unavailable"


def test_stale_and_previous_session_evidence_cannot_be_current():
    evidence, flags = _evidence(now=NOW + timedelta(days=1))
    assert "option_snapshot_not_current" in flags
    assert "quote_session_date_mismatch" in flags
    assert evidence["assessment_mode"] == "unavailable"


def test_underlier_cannot_be_newer_than_option_snapshot():
    evidence, flags = _evidence(quote_changes={"spot_observed_at_utc": NOW.isoformat()})
    assert "underlier_option_time_incoherent" in flags
    assert evidence["current_actionable"] is False


def test_manual_holding_facts_cannot_postdate_the_quote_snapshot():
    evidence, flags = _evidence(position={"input_source": "user_provided", "input_as_of_utc": NOW.isoformat()})
    assert "manual_input_time_invalid" in flags
    assert evidence["current_actionable"] is False


def test_inconsistent_underlier_ready_and_suspended_is_rejected():
    evidence, flags = _evidence(quote_changes={"underlier_suspension": True})
    assert "underlier_observation_not_current" in flags
    assert evidence["current_actionable"] is False
