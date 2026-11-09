from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from domain.domain.lifecycle_timing import canonical_hash, derive_effective_lifecycle_timing
from src.application.trades.close_reason_evidence import build_lifecycle_timing_policy
from src.application.trades.inbox import get_settlement_attempt_state, upsert_settlement_attempt_state
from src.application.trades.lifecycle_reconciliation import lifecycle_case_read_model
from src.application.trades.lifecycle_runtime import reconcile_due_lifecycle_cases_for_source
from src.application.trades.settlement_observation import build_settlement_observation_collector
from tests import test_settlement_observation as fixtures


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _policy(market: str, expiration: str, days: list[dict[str, str]]) -> dict:
    zone = ZoneInfo("America/New_York" if market == "US" else "Asia/Hong_Kong")
    cutoff = int(datetime.fromisoformat(expiration).replace(hour=16, tzinfo=zone).timestamp() * 1000)
    return build_lifecycle_timing_policy(
        case_id="fixture", market=market, expiration_ymd=expiration,
        contract_metadata={
            "settlement_style": "physical", "underlying_security_type": "equity",
            "last_trade_cutoff_ms": cutoff,
            "last_trade_cutoff_source": "instrument_policy_registry",
        },
        trading_days=days, calendar_source="fixture", calendar_observed_at_ms=cutoff,
    )


def _effective(policy: dict, received: int) -> dict:
    return derive_effective_lifecycle_timing(
        policy=policy,
        option_close_evidence=[{"evidence_type": "option_zero_price_close", "received_at_ms": received}],
    )


def test_pdd_friday_review_is_saturday_without_advancing_final_expiration_gate() -> None:
    policy = _policy("US", "2026-11-06", [
        {"date": "2026-11-06", "type": "WHOLE"},
        {"date": "2026-11-09", "type": "WHOLE"},
    ])
    original = deepcopy(policy)
    received = _ms("2026-11-07T13:21:49+08:00")
    effective = _effective(policy, received)
    assert effective["last_trade_cutoff_ms"] == _ms("2026-11-07T05:00:00+08:00")
    assert effective["review_after_ms"] == _ms("2026-11-07T13:36:49+08:00")
    assert effective["settlement_deadline_ms"] == _ms("2026-11-10T05:00:00+08:00")
    assert effective["timing_policy_hash"] == canonical_hash(original)
    assert policy == original


@pytest.mark.parametrize(("market", "expiration", "next_day", "cutoff"), [
    ("US", "2026-11-27", "2026-11-30", "2026-11-27T13:00:00-05:00"),
    ("HK", "2026-12-24", "2026-12-28", "2026-12-24T12:00:00+08:00"),
])
def test_expiration_morning_session_corrects_new_and_immutable_legacy_cutoff(
    market: str, expiration: str, next_day: str, cutoff: str,
) -> None:
    policy = _policy(market, expiration, [
        {"date": expiration, "type": "MORNING"},
        {"date": next_day, "type": "WHOLE"},
    ])
    assert policy["last_trade_cutoff_ms"] == _ms(cutoff)
    legacy = deepcopy(policy)
    zone = ZoneInfo(policy["timezone"])
    legacy["last_trade_cutoff_ms"] = int(
        datetime.fromisoformat(expiration).replace(hour=16, tzinfo=zone).timestamp() * 1000
    )
    original = deepcopy(legacy)
    effective = _effective(legacy, _ms(cutoff) - 60 * 60_000)
    assert effective["last_trade_cutoff_ms"] == _ms(cutoff)
    assert effective["review_after_ms"] == _ms(cutoff)
    assert effective["settlement_deadline_ms"] == legacy["settlement_deadline_ms"]
    assert legacy == original


def test_closed_expiration_day_is_a_metadata_conflict_not_silently_rolled() -> None:
    with pytest.raises(ValueError, match="expiration.*trading"):
        _policy("US", "2026-04-03", [
            {"date": "2026-04-03", "type": "REST"},
            {"date": "2026-04-06", "type": "WHOLE"},
        ])


def test_holiday_adjusted_contract_date_keeps_local_expiration_and_dst() -> None:
    policy = _policy("US", "2026-04-02", [
        {"date": "2026-04-02", "type": "WHOLE"},
        {"date": "2026-04-03", "type": "REST"},
        {"date": "2026-04-06", "type": "WHOLE"},
    ])
    effective = _effective(policy, _ms("2026-04-03T08:00:00+08:00"))
    assert effective["last_trade_cutoff_ms"] == _ms("2026-04-03T04:00:00+08:00")
    assert effective["review_after_ms"] == _ms("2026-04-03T08:15:00+08:00")
    assert effective["settlement_deadline_ms"] == _ms("2026-04-07T04:00:00+08:00")


def test_old_local_attempt_wakes_before_final_deadline_and_keeps_repolling(
    tmp_path: Path,
) -> None:
    repo, case, policy, anchor_ms = fixtures._repo_with_pending_case(tmp_path)
    case_id = case["case_id"]
    now = max(anchor_ms + 16 * 60_000, int(case["observation_start_ms"]))
    gateway = fixtures._Gateway()
    collector = build_settlement_observation_collector(
        repo=repo, gateway=gateway, futu_account_id="1001", now_ms_fn=lambda: now, source_id="lx",
    )
    inbox = tmp_path / "inbox.sqlite3"
    source = {
        "id": "lx", "account": "lx", "futu_account_ids": ["1001"],
        "inbox_path": inbox, "settlement_observation": {"enabled": True},
    }
    fixtures._bootstrap_current_decision_shadow(repo, now_ms=now)
    from src.application.ledger.api import list_trade_lifecycle_due_candidates

    candidate = list_trade_lifecycle_due_candidates(repo, account="lx")[0]
    timing = candidate["timing_policy"]
    lifecycle_case = candidate["lifecycle_case"]
    # Exact old v2 fingerprint and Monday local wakeup, persisted before the upgrade.
    old_fingerprint = canonical_hash({
        "schema_version": "case_scope_fingerprint.v2",
        "case": {
            key: lifecycle_case.get(key)
            for key in ("case_id", "account", "status", "decision_type", "target_contracts_by_lot",
                        "observation_start_ms", "pending_until_ms")
        } | {"derived_reason_state": (lifecycle_case.get("derived_summary") or {}).get("reason_state")},
        "case_updated_at_ms": candidate.get("case_updated_at_ms"),
        "timing": {key: timing.get(key) for key in ("policy_schema", "settlement_deadline_ms", "calendar_hash")},
        "evidence_revision": int(candidate.get("evidence_revision") or 0),
    })
    upsert_settlement_attempt_state(inbox, state={
        "source_id": "lx", "account": "lx", "case_id": case_id,
        "case_scope_fingerprint": old_fingerprint, "classification": "local",
        "collector_contract_version": collector.contract.contract_version,
        "capability_fingerprint": collector.capability.capability_fingerprint,
        "next_attempt_at_ms": policy["settlement_deadline_ms"], "updated_at_ms": now - 1,
    })
    raw_policy = deepcopy(repo.get_trade_lifecycle_timing_policy(case_id))
    events = repo.list_trade_events()
    seals: list[dict] = []

    def run() -> dict:
        return reconcile_due_lifecycle_cases_for_source(
            repo, source=source, now_ms=now, apply_changes=True,
            settlement_collector=collector, settlement_control_now_ms_fn=lambda: now,
            seal_sink=seals.append,
        )

    result = run()
    assert result["provider_attempt_count"] == 1
    assert len(gateway.history_deal_queries) == 1
    assert lifecycle_case_read_model(repo, case_id=case_id, now_ms=now)["reason_state"] == "cause_pending"
    state = get_settlement_attempt_state(inbox, source_id="lx", account="lx", case_id=case_id)
    assert now < state["next_attempt_at_ms"] < policy["settlement_deadline_ms"]
    now = state["next_attempt_at_ms"]
    assert run()["provider_attempt_count"] == 1
    assert len(gateway.history_deal_queries) == 2
    assert repo.get_trade_lifecycle_timing_policy(case_id) == raw_policy
    assert repo.list_trade_events() == events


def test_late_stock_leg_remains_eligible_after_early_review(tmp_path: Path) -> None:
    repo, case, policy, anchor_ms = fixtures._repo_with_pending_case(tmp_path)
    now = max(anchor_ms + 16 * 60_000, int(case["observation_start_ms"]))
    early = fixtures._collect_broker_observation(
        repo, lifecycle_case=case, case_id=case["case_id"], gateway=fixtures._Gateway(), now_ms=now,
    )
    assert not early["complete"]
    assert "settlement_deadline_not_reached" in early["incomplete_reason_codes"]
    stock = fixtures._stock_settlement_deal("late-stock", trade_time_ms=now + 60 * 60_000)
    gateway = fixtures._settlement_gateway(stock)
    late = fixtures._collect_broker_observation(
        repo, lifecycle_case=case, case_id=case["case_id"], gateway=gateway,
        now_ms=policy["settlement_deadline_ms"],
    )
    assert len(late["stock_settlement_candidates"]) == 1


@pytest.mark.parametrize("after_deadline", [False, True])
def test_early_incomplete_retry_is_bounded_only_before_final_deadline(after_deadline: bool) -> None:
    from src.application.trades.settlement_attempts import (
        SettlementAttemptOutcome, settlement_attempt_updates_after_outcome,
    )

    deadline = _ms("2026-11-10T05:00:00+08:00")
    now = deadline + 1 if after_deadline else deadline - 1
    outcome = SettlementAttemptOutcome(
        kind="observed_incomplete", source_id="lx", account="lx", case_id="fixture",
        contract_version="fixture", capability_fingerprint="fixture",
    )
    updates = settlement_attempt_updates_after_outcome(
        current={}, outcome=outcome, now_ms=now, retry_deadline_ms=deadline,
        case_scope_fingerprint_value="fixture", provider_input_scope_fingerprint_value="fixture",
    )
    assert updates["next_attempt_at_ms"] == (now + 5 * 60_000 if after_deadline else deadline)
