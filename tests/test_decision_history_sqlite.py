from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from test_daily_decision_brief_repository_v2 import _brief, _action
from src.application.daily_decision_brief_repository import (
    DailyDecisionBriefStateError, persist_daily_decision_brief_success,
    persist_daily_decision_brief_failure, read_daily_decision_brief, read_latest_daily_decision_brief,
)
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, DecisionHistoryError, history_path


def test_replay_immutable_conflict_new_run_and_failure_current(tmp_path):
    source = _brief(run_id="first", actions=[_action()])
    first = persist_daily_decision_brief_success(base=tmp_path, brief=source)
    repeat = persist_daily_decision_brief_success(base=tmp_path, brief=source)
    assert repeat == first
    with pytest.raises(DailyDecisionBriefStateError, match="history_run_conflict"):
        persist_daily_decision_brief_success(base=tmp_path, brief={**source, "strategy_summary": "changed"})
    failure = persist_daily_decision_brief_failure(base=tmp_path, brief=_brief(run_id="failed", status="failed", actionability="blocked"))
    assert failure["revision"] == 1
    assert failure["actions"] == []
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"] == first["brief"]
    store = DecisionHistoryStore(history_path(tmp_path))
    assert store.get(account="lx", market="US", run_id="failed")["payload"] == failure
    with sqlite3.connect(history_path(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable_decision"):
            conn.execute("UPDATE decisions SET run_id='oops'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable_decision"):
            conn.execute("DELETE FROM decisions")


def test_json_is_disposable_not_authority_and_missing_sqlite_is_not_empty(tmp_path):
    saved = persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="first"))
    for name in ("current", "revision", "run_brief"):
        saved["paths"][name].unlink()
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"] == saved["brief"]
    saved["paths"]["current"].write_text('{"fake":true}')
    assert read_daily_decision_brief(base=tmp_path, account="lx", market="US", market_trading_date="2026-07-21", revision=0)["brief"] == saved["brief"]
    history_path(tmp_path).unlink()
    result = read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")
    assert not result["available"] and result["error"] == "history_missing"
    assert not history_path(tmp_path).exists()


def test_concurrent_failed_and_successful_runs_allocate_unique_revisions(tmp_path):
    def write(i):
        brief = _brief(run_id=f"run-{i}")
        if i % 2:
            return persist_daily_decision_brief_failure(base=tmp_path, brief={**brief, "status": "failed", "actionability": "blocked"})["revision"]
        return persist_daily_decision_brief_success(base=tmp_path, brief=brief)["current_revision"]
    with ThreadPoolExecutor(max_workers=4) as workers:
        revisions = list(workers.map(write, range(12)))
    assert sorted(revisions) == list(range(12))


def test_bootstrap_cannot_reuse_legacy_revision(tmp_path):
    path = tmp_path / 'output_accounts/lx/state/daily_decision_brief.US.2026-07-21.r0000.json'
    path.parent.mkdir(parents=True)
    path.write_text('{}')
    with pytest.raises(DailyDecisionBriefStateError, match="history_migration_required"):
        persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id="new"))
    assert path.read_text() == '{}'


def test_read_only_missing_and_bad_schema_never_repairs(tmp_path):
    path = history_path(tmp_path)
    with pytest.raises(DecisionHistoryError, match="history_missing"):
        DecisionHistoryStore(path).get(account="lx", market="US")
    assert not path.parent.exists()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(DecisionHistoryError, match="history_schema_invalid"):
        DecisionHistoryStore(path).get(account="lx", market="US")
    assert path.read_bytes() == before


def test_nonfinite_evidence_is_missing_and_replay_is_stable(tmp_path):
    source = _brief(run_id="nonfinite")
    source["candidates"] = {"csp": [{"symbol": "NVDA", "quote": float("nan"), "metrics": {"iv": float("inf")}}]}
    first = persist_daily_decision_brief_success(base=tmp_path, brief=source)
    stored = DecisionHistoryStore(history_path(tmp_path)).get(account="lx", market="US", run_id="nonfinite")
    assert stored["input"]["candidates"]["csp"][0]["quote"] is None
    assert stored["payload"]["candidates"]["csp"][0]["metrics"]["iv"] is None
    assert persist_daily_decision_brief_success(base=tmp_path, brief=source) == first
