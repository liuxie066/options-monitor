from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from domain.domain.daily_decision_brief import diff_daily_decision_briefs
from src.application.daily_decision_brief_service import _candidate_event_risk
from src.application.earnings_calendar import (
    annotate_candidates_with_earnings_evidence,
    fetch_market_earnings_calendar,
)
from test_earnings_calendar import _rehash
from test_daily_decision_brief_service import (
    _account_dir, _assemble, _call_row, _config, _fixture_dependencies, _put_row,
)


DAY = "2026-07-17"
EXPIRY = "2026-08-21"
NOW = datetime(2026, 7, 17, 14, tzinfo=timezone.utc)


def _annotated(base, *, event="2026-08-05", market="US", symbol="NVDA",
               expiry=EXPIRY, observed=NOW, failure=None, extra_scope=False, corrupt=None):
    code = "HK.00700" if market == "HK" else f"US.{symbol}"

    class Gateway:
        def get_earnings_calendar(self, *, begin_date, end_date, **kwargs):
            if failure == "all" or (failure == "soft" and begin_date == DAY):
                raise RuntimeError("isolated calendar interval unavailable")
            if event and begin_date <= event <= end_date:
                return [{"security": code, "earnings_date": event}]
            return []

    scope = {code: [expiry]}
    if extra_scope:
        scope["US.AAPL"] = ["2026-09-18"]
    snapshot = fetch_market_earnings_calendar(
        gateway=Gateway(), market=market, scan_date=date.fromisoformat(DAY),
        scan_at_utc=observed, expirations_by_underlier=scope, now_fn=lambda: observed,
    )
    if corrupt:
        snapshot.update(corrupt)
        if "snapshot_hash" not in corrupt:
            _rehash(snapshot)
    path = base / "earnings_calendar" / f"{market}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    row = _put_row(symbol=symbol)
    row.update(market=market, expiration=expiry)
    return annotate_candidates_with_earnings_evidence(
        pd.DataFrame([row]), input_root=base,
    ).to_dict("records")[0]


def _brief(base, row, *, family="sell_put", priority="P1", now=NOW, window_end="23:59"):
    account_dir = _account_dir(base)
    for name in ("portfolio_context.json", "option_positions_context.json"):
        path = account_dir / "state" / name
        context = json.loads(path.read_text(encoding="utf-8"))
        context["as_of_utc"] = now.isoformat()
        if isinstance(context.get("exchange_rates"), dict):
            context["exchange_rates"]["timestamp"] = now.isoformat()
        path.write_text(json.dumps(context), encoding="utf-8")
    row = dict(row, tier=priority)
    market = row["market"]
    config = _config(timezone_name="Asia/Hong_Kong" if market == "HK" else "America/New_York")
    config["schedule"]["run_window"]["end"] = window_end
    if family == "covered_call":
        row = {**_call_row(), **{k: v for k, v in row.items() if k.startswith("earnings_")},
               "market": market, "tier": priority}
        filename = "nvda_sell_call_candidates.csv"
    elif family == "combo_yield":
        row.update(
            candidate_pair_id="combo_yield:NVDA:NVDA_P100:NVDA_C125",
            put_contract_symbol="NVDA_P100", call_contract_symbol="NVDA_C125",
            put_expiration=EXPIRY, call_expiration=EXPIRY, put_strike=100, call_strike=125,
            annualized_net_credit_yield=0.2, cash_required_usd=10_000, cash_free_usd=20_000,
        )
        filename = "nvda_combo_yield_candidates.csv"
    elif family == "wheel":
        from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot

        row.update(_call_row())
        row.update(candidate_id="wheel-1", final_candidate_id="wheel-1", account="lx",
                   wheel_branch_id="call-branch-1", stock_lot_id="stock-1", direction="call", multiplier=100,
                   granted_contracts=2,
                   sell_limit=2.1, quote_observed_at_utc="2026-07-17T14:00:00Z")
        batch = {
            "account": "lx", "symbol": "NVDA", "wheel_branch_id": "call-branch-1", "stock_lot_id": "stock-1",
            "direction": "call", "batch_generation_hash": "1" * 64, "projection_hash": "2" * 64,
            "remaining_contracts": 2, "principal_anchor": 10_000, "currency": "USD",
            "requested_shares": 200, "granted_shares": 200,
            "phase": "ready", "reason_codes": [], "requested_contracts": 2,
            "granted_contracts": 2, "capacity_before": 200, "capacity_after": 0,
            "raw_candidates": [{k: v for k, v in row.items() if k != "final_candidate_id"}],
            "final_candidate": row,
        }
        seal_wheel_candidate_snapshot(
            base=base, run_id="run-1", account="lx", market=market,
            account_config_sha256="f" * 64, strategy_policy_sha256="1" * 64,
            dependencies=_fixture_dependencies(), batches=[batch],
            scope_results=[{"symbol": "NVDA", "direction": "call", "status": "completed",
                            "candidate_count": 1}],
            capacity_allocations=[], run_mode={"scan_mode": "standard", "executable": True},
            sealed_at="2026-07-17T13:59:59Z",
        )
        return _assemble(base, market=market, now_utc=now, config=config)
    else:
        filename = f"{row['symbol'].lower()}_sell_put_candidates_labeled.csv"
    for field in (key for key in row if key.startswith("earnings_")):
        if isinstance(row[field], (dict, list)):
            row[field] = json.dumps(row[field])
    pd.DataFrame([row]).to_csv(account_dir / filename, index=False)
    return _assemble(base, market=market, now_utc=now, config=config)


def _risk(row, *, family="sell_put", day=DAY):
    return _candidate_event_risk(row, family=family, market_date=day)


@pytest.mark.parametrize("market,priority,extra_scope,now", [
    ("US", "P1", False, NOW), ("US", "P0", False, NOW),
    ("HK", "P1", False, NOW),
    ("US", "P1", False, datetime(2026, 7, 18, 0, tzinfo=timezone.utc)),
    ("US", "P1", True, NOW),
])
def test_real_snapshot_to_same_day_removal(tmp_path, market, priority, extra_scope, now):
    symbol = "0700.HK" if market == "HK" else "NVDA"
    before_row = _annotated(tmp_path / "before", market=market, symbol=symbol)
    after_row = _annotated(tmp_path / "after", event=None, market=market, symbol=symbol,
                           observed=now, extra_scope=extra_scope)
    before = _brief(tmp_path / "before", before_row, priority=priority)
    after = _brief(tmp_path / "after", after_row, priority=priority, now=now)
    _assert_removal(before, after, before_row, after_row, priority)


def _assert_removal(before, after, before_row, after_row, priority="P1"):
    assert before["actionability"] == after["actionability"] == "live_actionable"
    prior = next(a for a in before["actions"] if a["action_type"] in {"open_candidate", "open_combo_yield"})
    current = next(a for a in after["actions"] if a["action_type"] in {"open_candidate", "open_combo_yield"})
    assert prior["state"] == current["state"] == "active"
    assert prior["priority"] == current["priority"] == priority
    assert prior["action_id"] == current["action_id"]
    assert prior["metrics"]["rank"] == current["metrics"]["rank"] == 1
    assert prior["metrics"].get("capacity") == current["metrics"].get("capacity")
    if prior["strategy_family"] != "combo_yield":
        assert current["metrics"]["capacity"]["contracts_available"] == 2
    assert before_row["earnings_snapshot_hash"] != after_row["earnings_snapshot_hash"]
    assert prior["event_risk"]["evidence_chain_id"] == current["event_risk"]["evidence_chain_id"]
    assert current["event_risk"]["evidence_chain_id"].startswith("earnings-comparable-v1:")
    changes = diff_daily_decision_briefs(before, after)["changes"]
    assert [c["change_type"] for c in changes] == ["candidate_event_removed"]
    assert changes[0]["before_event_risk"]["evidence_snapshot_hash"] == before_row["earnings_snapshot_hash"]
    assert changes[0]["after_event_risk"]["evidence_snapshot_hash"] == after_row["earnings_snapshot_hash"]
    for brief, row in ((before, before_row), (after, after_row)):
        _assert_projection_parity(brief, row, family=current["strategy_family"])
    from src.application.daily_decision_brief_renderer import render_full_brief
    rendered = render_full_brief(after)
    assert "earnings-comparable-v1:" not in rendered
    assert after_row["earnings_snapshot_hash"] not in rendered


def _assert_projection_parity(brief, row, *, family):
    view, = brief["candidates"][family]
    action, = (a for a in brief["actions"] if a["strategy_family"] == family)
    indexed, = (item for item in brief["candidate_index"] if item["strategy_family"] == family)
    risk = action["event_risk"]
    assert risk == view["event_risk"] == indexed["representative"]["event_risk"]
    assert risk["user_state"] == _risk(row, family=family)["user_state"]
    assert risk["evidence_snapshot_hash"] == row["earnings_snapshot_hash"]
    assert view["metrics"]["rank"] == action["metrics"]["rank"] == 1
    assert view["capacity"] == action["metrics"].get("capacity", {})
    assert indexed["contract_count"] == 1
    return risk


@pytest.mark.parametrize("family", ["covered_call", "combo_yield", "wheel"])
def test_shared_service_callers_remove_comparable_event(tmp_path, family):
    before_row = _annotated(tmp_path / "before")
    after_row = _annotated(tmp_path / "after", event=None)
    before = _brief(tmp_path / "before", before_row, family=family)
    after = _brief(tmp_path / "after", after_row, family=family)
    _assert_removal(before, after, before_row, after_row)


@pytest.mark.parametrize("options,changes,state", [
    ({"failure": "all"}, {}, "unknown"),
    ({"failure": "soft"}, {}, "confirmed_none"),
    ({}, {"earnings_hard_coverage_status": "partial"}, "confirmed_none"),
    ({}, {"earnings_market_date": "2026-07-18"}, "confirmed_none"),
    ({}, {"earnings_evidence_status": None}, "unknown"),
])
def test_wheel_incomplete_evidence_preserves_projection_and_no_removal(tmp_path, options, changes, state):
    before_row = _annotated(tmp_path / "before")
    after_row = {**_annotated(tmp_path / "after", event=None, **options), **changes}
    before = _brief(tmp_path / "before", before_row, family="wheel")
    after = _brief(tmp_path / "after", after_row, family="wheel")
    _assert_projection_parity(before, before_row, family="wheel")
    risk = _assert_projection_parity(after, after_row, family="wheel")
    assert risk["user_state"] == state
    assert risk["evidence_chain_id"] == ""
    prior, = (a for a in before["actions"] if a["strategy_family"] == "wheel")
    current, = (a for a in after["actions"] if a["strategy_family"] == "wheel")
    assert prior["action_id"] == current["action_id"]
    assert prior["state"] == current["state"] == "active"
    assert prior["priority"] == current["priority"] == "P1"
    assert prior["metrics"]["capacity"] == current["metrics"]["capacity"]
    assert "candidate_event_removed" not in [c["change_type"] for c in diff_daily_decision_briefs(before, after)["changes"]]


@pytest.mark.parametrize("event,failure", [("2026-08-05", None), (None, None), (None, "all")])
def test_wheel_refresh_only_preserves_state_without_material_diff(tmp_path, event, failure):
    before_row = _annotated(tmp_path / "before", event=event, failure=failure)
    after_row = _annotated(tmp_path / "after", event=event, failure=failure,
                           observed=datetime(2026, 7, 17, 15, tzinfo=timezone.utc))
    assert before_row["earnings_snapshot_hash"] != after_row["earnings_snapshot_hash"]
    before = _brief(tmp_path / "before", before_row, family="wheel")
    after = _brief(tmp_path / "after", after_row, family="wheel")
    for brief, row in ((before, before_row), (after, after_row)):
        _assert_projection_parity(brief, row, family="wheel")
    assert diff_daily_decision_briefs(before, after)["changes"] == []


def test_wheel_legacy_first_pair_remains_conservative(tmp_path):
    before_row = _annotated(tmp_path / "before")
    after_row = _annotated(tmp_path / "after", event=None)
    before = _brief(tmp_path / "before", before_row, family="wheel")
    after = _brief(tmp_path / "after", after_row, family="wheel")
    for action in before["actions"]:
        if action["strategy_family"] == "wheel":
            action["event_risk"]["evidence_chain_id"] = before_row["earnings_snapshot_hash"]
            action["event_risk"].pop("evidence_snapshot_hash", None)
    assert diff_daily_decision_briefs(before, after)["changes"] == []


@pytest.mark.parametrize("before_event,after_event,expected", [
    ("2026-08-05", "2026-08-05", []), (None, None, []),
    (None, "2026-08-05", ["candidate_event_added"]),
    ("2026-08-05", "2026-08-06", ["candidate_event_date_changed"]),
])
def test_refresh_and_existing_transitions(tmp_path, before_event, after_event, expected):
    before_row = _annotated(tmp_path / "before", event=before_event)
    after_row = _annotated(tmp_path / "after", event=after_event,
                           observed=datetime(2026, 7, 17, 15, tzinfo=timezone.utc))
    assert before_row["earnings_snapshot_hash"] != after_row["earnings_snapshot_hash"]
    before = _brief(tmp_path / "before", before_row)
    after = _brief(tmp_path / "after", after_row)
    assert [c["change_type"] for c in diff_daily_decision_briefs(before, after)["changes"]] == expected


@pytest.mark.parametrize("options,state", [
    ({"failure": "soft"}, "confirmed_none"), ({"failure": "all"}, "unknown"),
    ({"symbol": "AAPL"}, "confirmed_none"), ({"expiry": "2026-08-28"}, "confirmed_none"),
    ({"corrupt": {"source": "other"}}, "unknown"),
    ({"corrupt": {"policy_version": "legacy"}}, "unknown"),
    ({"corrupt": {"window_days": 6.5}}, "unknown"),
    ({"corrupt": {"snapshot_hash": "invalid"}}, "unknown"),
])
def test_incomparable_real_evidence_never_removes(tmp_path, options, state):
    before_row = _annotated(tmp_path / "before")
    after_row = _annotated(tmp_path / "after", event=None, **options)
    assert _risk(after_row)["user_state"] == state
    before = _brief(tmp_path / "before", before_row)
    after = _brief(tmp_path / "after", after_row)
    assert "candidate_event_removed" not in [c["change_type"] for c in diff_daily_decision_briefs(before, after)["changes"]]


@pytest.mark.parametrize("changes", [
    {"earnings_market_date": "2026-07-18"}, {"earnings_market_date": "20260717"},
    {"earnings_market_date": "2026-02-30"}, {"earnings_market_date": None},
    {"earnings_window_days": True}, {"earnings_window_days": 7.5},
    {"earnings_window_days": 6.0}, {"earnings_window_days": "6e0"},
    {"earnings_window_days": "6.0"}, {"earnings_window_days": None},
    {"earnings_policy_version": "legacy"}, {"earnings_snapshot_hash": None},
    {"earnings_snapshot_hash": "z" * 64}, {"earnings_hard_coverage_status": "partial"},
    {"earnings_soft_coverage_status": "partial"}, {"earnings_soft_coverage_status": "unavailable"},
    {"earnings_hard_window_start": "2026-08-13"},
    {"earnings_hard_window_end": "2026-08-22"},
    {"earnings_soft_window_start": None}, {"earnings_soft_window_end": "2026-08-15"},
    {"expiration": "20260821"}, {"expiration": "2026-08-21T00:00:00"},
    {"expiration": "2026-07-16"}, {"symbol": "invalid!"},
    {"earnings_evidence_status": "data_unavailable"},
    {"earnings_has_event": True, "earnings_blocking_has_event": True},
])
def test_strict_scope_rejects_bad_shapes_without_relaxing_original_state(tmp_path, changes):
    row = _annotated(tmp_path, event=None)
    risk = _risk({**row, **changes})
    assert risk["evidence_chain_id"] == ""
    if changes.get("earnings_snapshot_hash", row["earnings_snapshot_hash"]):
        assert risk["evidence_snapshot_hash"] == changes.get("earnings_snapshot_hash", row["earnings_snapshot_hash"])
    else:
        assert "evidence_snapshot_hash" not in risk
    if "earnings_hard_coverage_status" in changes or "earnings_soft_coverage_status" in changes:
        assert risk["user_state"] == "confirmed_none"


@pytest.mark.parametrize("window", [6, "6", "006"])
def test_integer_window_has_one_comparable_identity(tmp_path, window):
    row = _annotated(tmp_path, event=None)
    assert _risk({**row, "earnings_window_days": window})["evidence_chain_id"] == _risk(row)["evidence_chain_id"]


def test_labelled_expiry_and_family_isolation(tmp_path):
    row = _annotated(tmp_path, event=None)
    chains = {_risk(row, family=family)["evidence_chain_id"] for family in ("sell_put", "covered_call", "wheel")}
    combo = {**row, "put_expiration": EXPIRY, "call_expiration": EXPIRY}
    chain = _risk(combo, family="combo_yield")["evidence_chain_id"]
    assert chain and chain not in chains and len(chains) == 3
    for changes in ({"call_expiration": None}, {"put_expiration": None},
                    {"call_expiration": "2026-09-18"}, {"call_expiration": "20260821"}):
        risk = _risk({**combo, **changes}, family="combo_yield")
        assert risk["user_state"] == "confirmed_none"
        assert risk["evidence_chain_id"] == ""
    assert _risk(row, family="unsupported")["evidence_chain_id"] == ""


def test_soft_not_applicable_requires_absent_endpoints(tmp_path):
    row = _annotated(tmp_path, event=None, expiry="2026-07-21")
    assert _risk(row)["evidence_chain_id"]
    for changes in ({"earnings_soft_window_start": DAY}, {"earnings_soft_window_end": DAY},
                    {"earnings_soft_coverage_status": "complete"}):
        assert _risk({**row, **changes})["evidence_chain_id"] == ""


@pytest.mark.parametrize("field,value", [("account", "sy"), ("market", "HK"),
                                          ("market_trading_date", "2026-07-18")])
def test_existing_brief_identity_guard_remains(tmp_path, field, value):
    row = _annotated(tmp_path)
    before = _brief(tmp_path, row)
    after = deepcopy(before)
    after[field] = value
    with pytest.raises(ValueError):
        diff_daily_decision_briefs(before, after)


@pytest.mark.parametrize("priority,state", [("P2", "active"), ("P1", "observe")])
def test_event_removal_retains_active_high_priority_gate(tmp_path, priority, state):
    before_row = _annotated(tmp_path / "before")
    after_row = _annotated(tmp_path / "after", event=None)
    before = _brief(tmp_path / "before", before_row)
    after = _brief(tmp_path / "after", after_row)
    for brief in (before, after):
        for action in brief["actions"]:
            action.update(priority=priority, state=state)
    assert "candidate_event_removed" not in [c["change_type"] for c in diff_daily_decision_briefs(before, after)["changes"]]


@pytest.mark.parametrize("field,value", [
    ("symbol", "AAPL"), ("expiration", "2026-08-28"),
])
def test_unregistered_calendar_candidate_fails_closed(tmp_path, field, value):
    from src.application.earnings_calendar import load_earnings_evidence_for_candidate

    row = _annotated(tmp_path)
    identity = {"symbol": "NVDA", "expiration": EXPIRY, field: value}
    evidence = load_earnings_evidence_for_candidate(input_root=tmp_path, market="US", **identity)
    risk = _risk({**row, **identity, **evidence})
    assert risk["user_state"] == "unknown"
    assert risk["evidence_chain_id"] == ""


def test_invalid_market_date_and_legacy_first_pair_are_conservative(tmp_path):
    before_row = _annotated(tmp_path / "before")
    after_row = _annotated(tmp_path / "after", event=None)
    assert _risk(before_row, day="20260717")["evidence_chain_id"] == ""
    before = _brief(tmp_path / "before", before_row)
    after = _brief(tmp_path / "after", after_row)
    for action in before["actions"]:
        if "event_risk" in action:
            action["event_risk"]["evidence_chain_id"] = before_row["earnings_snapshot_hash"]
            action["event_risk"].pop("evidence_snapshot_hash", None)
    assert diff_daily_decision_briefs(before, after)["changes"] == []


@pytest.mark.parametrize("family", ["sell_put", "covered_call", "combo_yield", "wheel"])
def test_planning_history_does_not_restore_candidates_or_hide_comparable_removal(tmp_path, family):
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_success, record_daily_decision_brief_candidates,
    )

    before_row = _annotated(tmp_path / "before")
    after_row = _annotated(tmp_path / "after", event=None)
    before = _brief(tmp_path / "before", before_row, family=family)
    planning = _brief(tmp_path / "planning", after_row, family=family, window_end="09:59")
    after = _brief(tmp_path / "after", after_row, family=family)
    assert before["actionability"] == after["actionability"] == "live_actionable"
    assert planning["actionability"] == "planning_only"
    assert any(action["state"] == "active" for action in planning["actions"])
    history = tmp_path / "history"
    persisted = []
    for index, brief in enumerate((before, planning, after)):
        brief["run_id"] = f"combined-run-{index}"
        result = persist_daily_decision_brief_success(base=history, brief=brief)
        recorded = record_daily_decision_brief_candidates(
            base=history, account=brief["account"], market=brief["market"],
            market_trading_date=brief["market_trading_date"],
            revision=result["current_revision"], brief_digest=result["current_brief_digest"],
            candidate_identities=result["current_candidate_identities"],
        )
        assert recorded["applied"]
        if index == 1:
            assert result["current_candidate_identities"] == []
            assert result["newly_detected_candidate_identities"] == []
            assert recorded["pending_candidate_identities"] == []
        else:
            assert result["current_candidate_identities"]
        persisted.append(result["brief"])
    _assert_removal(persisted[0], persisted[2], before_row, after_row)
