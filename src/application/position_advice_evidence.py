"""Current-observation qualification shared by scheduled and active advice.

The caller supplies only sealed rows/requirements. This module never fetches or
reinterprets last-trade activity as a bid/ask update timestamp.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.expiration_dates import expiration_market_date
from src.application.opening_quote_evidence import OPENING_QUOTE_MAX_AGE_SECONDS
from src.application.position_advice_inputs import aware_time


QUOTE_RIGHTS_FIELDS = (
    "quote_rights_status", "quote_rights_market", "quote_rights_underlier",
    "quote_rights_option", "quote_rights_received_at_utc",
)


def current_position_advice_evidence(
    *, position: Mapping[str, Any], quote: Mapping[str, Any] | None,
    requirement: Mapping[str, Any] | None, market: str, now_utc: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """Require coherent fresh continuous-session evidence and both rights."""
    if now_utc.tzinfo is None:
        raise ValueError("position advice decision time must be timezone-aware")
    now_utc = now_utc.astimezone(timezone.utc)
    quote = quote or {}
    requirement = requirement or {}
    snapshot = aware_time(quote.get("snapshot_received_at_utc"))
    evidence = {
        "assessment_mode": "unavailable", "current_actionable": False,
        "assessment_as_of_utc": snapshot.isoformat() if snapshot else None,
        **{key: requirement.get(key) for key in QUOTE_RIGHTS_FIELDS},
    }
    flags: list[str] = []
    max_age = OPENING_QUOTE_MAX_AGE_SECONDS

    def recent(value: datetime | None) -> bool:
        return value is not None and 0 <= (now_utc - value).total_seconds() <= max_age

    spot_time = aware_time(quote.get("spot_observed_at_utc"))
    requested = aware_time(quote.get("snapshot_requested_at_utc"))
    state_time = aware_time(requirement.get("market_state_received_at_utc"))
    rights_time = aware_time(requirement.get("quote_rights_received_at_utc"))
    if not recent(snapshot):
        flags.append("option_snapshot_not_current")
    if requested is None or snapshot is None or not requested <= snapshot or not recent(requested):
        flags.append("option_snapshot_interval_invalid")
    if (not recent(spot_time) or quote.get("underlier_observation_status") != "ready"
        or str(quote.get("underlier_sec_status") or "").upper() != "NORMAL"
        or str(quote.get("underlier_suspension")).strip().lower() == "true"):
        flags.append("underlier_observation_not_current")
    if (str(quote.get("option_sec_status") or "").strip().upper() != "NORMAL"
        or str(quote.get("option_suspension")).strip().lower() != "false"):
        flags.append("option_security_not_tradable")
    if (spot_time is None or snapshot is None or spot_time > snapshot
        or (snapshot - spot_time).total_seconds() > max_age):
        flags.append("underlier_option_time_incoherent")
    times = (snapshot, spot_time, state_time, rights_time)
    if any(value is None or expiration_market_date(value, market) != expiration_market_date(now_utc, market)
           for value in times):
        flags.append("quote_session_date_mismatch")
    state = str(requirement.get("market_state_after_snapshot") or "").upper()
    snapshot_state = str(quote.get("market_state") or "").upper()
    if state not in {"MORNING", "AFTERNOON"} or snapshot_state != state:
        flags.append("market_not_continuous")
    if not recent(state_time) or snapshot is None or state_time is None or state_time < snapshot:
        flags.append("post_snapshot_state_unproven")
    if (requirement.get("quote_rights_status") != "ok"
        or requirement.get("quote_rights_market") != market
        or requirement.get("quote_rights_underlier") not in {"LV1", "LV2", "LV3"}
        or requirement.get("quote_rights_option") not in {"LV1", "LV2", "LV3"}):
        flags.append("realtime_quote_rights_unproven")
    if not recent(rights_time) or snapshot is None or rights_time is None or rights_time < snapshot:
        flags.append("quote_rights_time_unproven")
    if position.get("input_source") == "user_provided":
        input_time = aware_time(position.get("input_as_of_utc"))
        if input_time is None or snapshot is None or input_time > snapshot or input_time > now_utc:
            flags.append("manual_input_time_invalid")
    if not flags:
        evidence["assessment_mode"] = "current"
        evidence["current_actionable"] = True
    return evidence, flags
