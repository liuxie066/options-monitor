from __future__ import annotations

import hashlib
import json

import pytest

from test_daily_decision_brief_repository_v2 import (
    IDENTITY_NVDA,
    MARKET_DATE,
    TARGET_1000,
    _action,
    _brief,
    _prepare,
    _read_state,
    _record_candidates,
)
from src.application import daily_decision_brief_repository as repo
from src.application.notification_delivery_adapter import build_notification_transport_key


SCOPE = {"account": "lx", "market": "US", "market_trading_date": MARKET_DATE}
CUTOFF = "2026-07-21T15:00:00+00:00"


def setup_recovery(tmp_path, *, route=None):
    artifact = tmp_path / "failure.json"
    artifact.write_text('{"reason":"failure"}')
    failure = _prepare(
        tmp_path,
        run_id="failure",
        delivery_kind="fixed_failure",
        source_kind="scan_failure",
        source_digest=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        source_reference="failure.json",
        scheduled_target_market=TARGET_1000,
        rendered_message="failed",
        prepared_at_utc="2026-07-21T14:00:01+00:00",
    )["envelope"]
    args = identity(tmp_path, failure)
    repo.confirm_daily_decision_brief_delivery_v2(**args, confirmed_at_utc="2026-07-21T14:00:02+00:00")
    assert _read_state(tmp_path)["state"]["schema_version"].endswith("v2")
    brief = _brief(run_id="success", actions=[_action()])
    brief["generated_at_utc"] = "2026-07-21T14:05:00+00:00"
    success = repo.persist_daily_decision_brief_success(base=tmp_path, brief=brief)
    _record_candidates(
        tmp_path,
        revision=success["current_revision"],
        brief_digest=success["current_brief_digest"],
        candidate_identities=[IDENTITY_NVDA],
    )
    bind = repo.bind_daily_decision_brief_recovery_report(
        base=tmp_path,
        **SCOPE,
        scheduled_target_market=TARGET_1000,
        cutoff_utc=CUTOFF,
        revision=success["current_revision"],
        brief_digest=success["current_brief_digest"],
        candidate_identities=[IDENTITY_NVDA],
        render_context={"projection": "recovery", **({"attribution_route_key": route} if route else {})},
        diff={"changed": True},
        render_limits={"limit": 1},
        now_utc="2026-07-21T14:06:00+00:00",
    )
    assert bind["available"]
    return failure, success, bind["recovery"]


def identity(tmp_path, envelope):
    return dict(
        base=tmp_path,
        **SCOPE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
    )


def prepare(tmp_path, success, when="2026-07-21T14:07:00+00:00", message="recovered"):
    return _prepare(
        tmp_path,
        run_id="rebuild",
        delivery_kind="fixed_recovery_report",
        source_kind="successful_brief",
        revision=success["current_revision"],
        source_digest=success["current_brief_digest"],
        scheduled_target_market=TARGET_1000,
        candidate_identities=[IDENTITY_NVDA],
        rendered_message=message,
        render_context={"projection": "recovery"},
        prepared_at_utc=when,
    )


def test_binding_freeze_confirm_and_later_writes(tmp_path):
    failure, success, recovery = setup_recovery(tmp_path)
    day = _read_state(tmp_path)["state"]["days"][MARKET_DATE]
    assert day["fixed_reports"][TARGET_1000] == failure | {
        "status": "confirmed",
        "last_attempt_at_utc": "2026-07-21T14:00:02+00:00",
        "confirmed_at_utc": "2026-07-21T14:00:02+00:00",
    }
    assert _read_state(tmp_path)["state"]["schema_version"].endswith("v3")
    assert (
        repo.read_daily_decision_brief_recovery_report(base=tmp_path, **SCOPE, now_utc="2026-07-21T14:07:00+00:00")[
            "recovery"
        ]
        == recovery
    )
    assert repo.read_daily_decision_brief_recovery_report(base=tmp_path, **SCOPE, now_utc=CUTOFF)["available"] is False
    rows = repo.query_daily_brief_receipts(base=tmp_path, account="lx", market="US", query={})
    assert len(rows) == 2
    assert (
        next(row for row in rows if row["event_ref"]["source_event_id"] == recovery["delivery_key"])["receipt_body"]
        is None
    )
    newer = _brief(run_id="newer", actions=[])
    newer["generated_at_utc"] = "2026-07-21T14:08:00+00:00"
    repo.persist_daily_decision_brief_success(base=tmp_path, brief=newer)
    envelope = prepare(tmp_path, success)["envelope"]
    assert envelope["diff"] == {"changed": True}
    with pytest.raises(repo.DailyDecisionBriefStateError):
        prepare(tmp_path, success, message="changed")
    args = identity(tmp_path, envelope)
    repo.begin_daily_decision_brief_recovery_attempt(**args, attempted_at_utc="2026-07-21T14:08:00+00:00")
    repo.confirm_daily_decision_brief_delivery_v2(**args, confirmed_at_utc="2026-07-21T14:09:00+00:00")
    state = _read_state(tmp_path)["state"]
    assert state["days"][MARKET_DATE]["alerted_candidates"][IDENTITY_NVDA]["via"] == "fixed_recovery_report"
    assert state["days"][MARKET_DATE]["recovery_reports"][TARGET_1000]["status"] == "confirmed"
    assert not repo.begin_daily_decision_brief_recovery_attempt(**args, attempted_at_utc=CUTOFF)["started"]


def test_attempt_window_and_ambiguity(tmp_path):
    _, success, _ = setup_recovery(tmp_path)
    envelope = prepare(tmp_path, success)["envelope"]
    args = identity(tmp_path, envelope)
    repo.begin_daily_decision_brief_recovery_attempt(**args, attempted_at_utc="2026-07-21T14:08:00+00:00")
    assert (
        repo.read_retryable_daily_decision_brief_delivery(base=tmp_path, **SCOPE, now_utc=CUTOFF)["reason"]
        == "ambiguous"
    )
    repo.record_daily_decision_brief_delivery_attempt(
        **args, ambiguous=False, attempted_at_utc="2026-07-21T14:09:00+00:00"
    )
    assert (
        repo.read_retryable_daily_decision_brief_delivery(base=tmp_path, **SCOPE, now_utc="2026-07-21T14:10:00+00:00")[
            "reason"
        ]
        == "pending_recovery"
    )
    repo.begin_daily_decision_brief_recovery_attempt(**args, attempted_at_utc="2026-07-21T14:11:00+00:00")
    repo.begin_daily_decision_brief_recovery_attempt(**args, attempted_at_utc=CUTOFF)
    repo.record_daily_decision_brief_delivery_attempt(**args, ambiguous=False, attempted_at_utc=CUTOFF)
    assert repo.expire_daily_decision_brief_recovery_reports(base=tmp_path, **SCOPE, now_utc=CUTOFF)["updated"] is False
    assert _read_state(tmp_path)["state"]["days"][MARKET_DATE]["recovery_reports"][TARGET_1000]["status"] == "ambiguous"
    with pytest.raises(repo.DailyDecisionBriefStateError):
        repo.begin_daily_decision_brief_recovery_attempt(**(args | {"account": "other"}), attempted_at_utc=CUTOFF)


def test_unrendered_expires_at_cutoff(tmp_path):
    _, success, _ = setup_recovery(tmp_path)
    assert prepare(tmp_path, success, when=CUTOFF)["reason"] == "expired"
    assert repo.expire_daily_decision_brief_recovery_reports(base=tmp_path, **SCOPE, now_utc=CUTOFF)["updated"]
    assert (
        _read_state(tmp_path)["state"]["days"][MARKET_DATE]["recovery_reports"][TARGET_1000]["status"]
        == "expired_unconfirmed"
    )


def test_binding_stays_on_first_revision_and_survives_other_writes(tmp_path):
    _, success, recovery = setup_recovery(tmp_path)
    newer = _brief(run_id="newer", actions=[])
    newer["generated_at_utc"] = "2026-07-21T14:08:00+00:00"
    current = repo.persist_daily_decision_brief_success(base=tmp_path, brief=newer)
    _record_candidates(
        tmp_path,
        revision=current["current_revision"],
        brief_digest=current["current_brief_digest"],
        candidate_identities=[],
    )
    duplicate = repo.bind_daily_decision_brief_recovery_report(
        base=tmp_path,
        **SCOPE,
        scheduled_target_market=TARGET_1000,
        cutoff_utc=CUTOFF,
        revision=current["current_revision"],
        brief_digest=current["current_brief_digest"],
        now_utc="2026-07-21T14:10:00+00:00",
    )
    assert duplicate["reason"] == "already_bound"
    assert duplicate["recovery"] == recovery
    assert _read_state(tmp_path)["state"]["days"][MARKET_DATE]["recovery_reports"][TARGET_1000] == recovery
    assert prepare(tmp_path, success)["envelope"]["revision"] == success["current_revision"]


def test_recovery_receipts_and_confirmed_attribution(tmp_path):
    route = "a" * 64
    _, success, _ = setup_recovery(tmp_path, route=route)
    envelope = _prepare(
        tmp_path,
        run_id="rebuild",
        delivery_kind="fixed_recovery_report",
        source_kind="successful_brief",
        revision=success["current_revision"],
        source_digest=success["current_brief_digest"],
        scheduled_target_market=TARGET_1000,
        candidate_identities=[IDENTITY_NVDA],
        rendered_message="recovered",
        render_context={"projection": "recovery", "attribution_route_key": route},
        prepared_at_utc="2026-07-21T14:07:00+00:00",
    )["envelope"]
    args = identity(tmp_path, envelope)
    repo.begin_daily_decision_brief_recovery_attempt(**args, attempted_at_utc="2026-07-21T14:08:00+00:00")
    repo.confirm_daily_decision_brief_delivery_v2(
        **args, confirmed_at_utc="2026-07-21T14:09:00+00:00", attribution_route_key=route
    )
    assert (
        repo.read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")[
            "attribution_route_key"
        ]
        == route
    )
    rows = repo.query_daily_brief_receipts(base=tmp_path, account="lx", market="US", query={"run_id": "rebuild"})
    assert len(rows) == 1 and rows[0]["receipt_body"] == "recovered" and rows[0]["delivery_state"] == "confirmed"


def test_corrupt_recovery_identity_fails_closed(tmp_path):
    setup_recovery(tmp_path)
    state_path = _read_state(tmp_path)["path"]
    raw = json.loads(state_path.read_text())
    raw["days"][MARKET_DATE]["recovery_reports"][TARGET_1000]["original_failure_digest"] = "0" * 64
    state_path.write_text(json.dumps(raw))
    assert _read_state(tmp_path)["reason"] == "state_invalid"
    with pytest.raises(repo.DailyDecisionBriefStateError):
        repo.expire_daily_decision_brief_recovery_reports(base=tmp_path, **SCOPE, now_utc=CUTOFF)
