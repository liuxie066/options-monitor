"""Real intake reconciliation must reuse the public history batch boundary."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import json
import shutil
import subprocess
import sys

import pytest

from test_daily_decision_brief_repository_v2 import _brief
from trade_intake_combo_history_fixtures import seed_intake_combo_history
from src.application import daily_decision_brief_repository as history
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import combo_reconciliation as application
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore


@pytest.mark.parametrize("shared_validation", [False, True])
def test_real_intake_reconciliation_reads_delivery_once_per_market(tmp_path, monkeypatch, shared_validation):
    ledger, dates, now_ms = seed_intake_combo_history(tmp_path)
    reads, connections, validations = [], [], []
    original_read, original_connect = Path.read_text, DecisionHistoryStore.connect
    original_normalize = history._DeliveryValidation.normalize

    def counted_read(path, *args, **kwargs):
        if path.name.endswith(".delivery.json"):
            reads.append(path)
        return original_read(path, *args, **kwargs)

    @contextmanager
    def counted_connect(store, **kwargs):
        with original_connect(store, **kwargs) as conn:
            connections.append(store.path)
            yield conn

    def counted_normalize(validation, *args, **kwargs):
        validations.append(validation)
        return original_normalize(validation, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr(DecisionHistoryStore, "connect", counted_connect)
    monkeypatch.setattr(history._DeliveryValidation, "normalize", counted_normalize)
    scope = history.daily_brief_delivery_validation_scope() if shared_validation else nullcontext()
    with scope:
        ambient = history._DELIVERY_VALIDATION.get()
        result = application.reconcile_account_post_trade_combos(
            repo=ledger, runtime_root=tmp_path, account="lx",
            runtime_environment="opend:127.0.0.1:11111", mode="observe",
            effective_now_ms=now_ms)
    assert [r["market_date"] for r in result["evidence_reads"]] == dates
    assert all(r["complete"] and r["exposure_count"] == 1 for r in result["evidence_reads"])
    assert len(reads) == 1
    assert len(connections) == 1
    assert validations == ([ambient] if shared_validation else [])


def test_market_batches_preserve_date_order_and_per_date_completeness(tmp_path, monkeypatch):
    calls, persisted = [], []
    scopes = [("US", "2026-07-21"), ("HK", "2026-07-20"),
              ("US", "2026-07-20"), ("US", "2026-07-21")]

    def reconcile(**kwargs):
        if not kwargs["persist"]:
            return {"inferences": [dict(market=m, market_date=d) for m, d in scopes]}
        persisted.append(kwargs)
        return {"ok": True}

    def read_batch(**kwargs):
        calls.append(kwargs)
        return {date: {"available": True, "reason": "ok", "complete": date.endswith("21"),
                       "delivery_available": True, "exposures": []}
                for date in kwargs["market_trading_dates"]}

    monkeypatch.setattr(application, "reconcile_combo_pair_inferences", reconcile)
    monkeypatch.setattr(application, "read_combo_candidate_exposures_batch", read_batch, raising=False)
    result = application.reconcile_account_post_trade_combos(
        repo=object(), runtime_root=tmp_path, account="LX",
        runtime_environment="isolated", mode="observe", effective_now_ms=123)
    assert [(c["market"], list(c["market_trading_dates"])) for c in calls] == [
        ("HK", ["2026-07-20"]), ("US", ["2026-07-20", "2026-07-21"])]
    assert all(c["base"] == tmp_path.resolve() and c["account"] == "lx" for c in calls)
    assert [(r["market"], r["market_date"], r["complete"]) for r in result["evidence_reads"]] == [
        ("HK", "2026-07-20", False), ("US", "2026-07-20", False), ("US", "2026-07-21", True)]
    assert len(persisted) == 1 and persisted[0]["effective_now_ms"] == 123


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_batch_failure_or_cancellation_never_reaches_persistence(tmp_path, monkeypatch, failure):
    calls = []

    def reconcile(**kwargs):
        calls.append(kwargs["persist"])
        assert not kwargs["persist"]
        return {"waiting_for_counterpart": [{"market": "US", "market_date": "2026-07-20"}]}

    def fail(**kwargs):
        raise failure("isolated read failure")

    monkeypatch.setattr(application, "reconcile_combo_pair_inferences", reconcile)
    monkeypatch.setattr(application, "read_combo_candidate_exposures_batch", fail, raising=False)
    with pytest.raises(failure, match="isolated read failure"):
        application.reconcile_account_post_trade_combos(
            repo=object(), runtime_root=tmp_path, account="lx",
            runtime_environment="isolated", mode="observe")
    assert calls == [False]


def test_real_batch_matches_independent_reads_and_closes_scope_before_persist(tmp_path, monkeypatch):
    old_base, new_base = tmp_path / "independent", tmp_path / "batch"
    old_base.mkdir()
    old_ledger, dates, now_ms = seed_intake_combo_history(old_base)
    monkeypatch.setattr("src.application.ledger.repository_strategy_groups.now_ms", lambda: now_ms)
    shutil.copytree(old_base, new_base)
    new_ledger = SQLiteOptionPositionsRepository(new_base / "output_shared/state/option_positions.sqlite3")
    real_batch = application.read_combo_candidate_exposures_batch
    real_reconcile = application.reconcile_combo_pair_inferences

    def checked_reconcile(**kwargs):
        if kwargs["persist"]:
            assert history._BRIEF_HISTORY_OBSERVATION.get() is None
        return real_reconcile(**kwargs)

    def independent(*, market_trading_dates, **kwargs):
        return {day: history.read_combo_candidate_exposures(**kwargs, market_trading_date=day)
                for day in market_trading_dates}

    monkeypatch.setattr(application, "reconcile_combo_pair_inferences", checked_reconcile)
    monkeypatch.setattr(application, "read_combo_candidate_exposures_batch", independent)
    args = dict(account="lx", runtime_environment="opend:127.0.0.1:11111",
                mode="observe", effective_now_ms=now_ms)
    expected = application.reconcile_account_post_trade_combos(repo=old_ledger, runtime_root=old_base, **args)
    monkeypatch.setattr(application, "read_combo_candidate_exposures_batch", real_batch)
    actual = application.reconcile_account_post_trade_combos(repo=new_ledger, runtime_root=new_base, **args)
    assert actual == expected
    assert len(actual["evidence_reads"]) == len(dates)
    assert old_ledger.list_combo_pair_inferences(account="lx") == new_ledger.list_combo_pair_inferences(account="lx")
    assert old_ledger.list_trade_events() == new_ledger.list_trade_events()


def test_next_reconcile_revalidates_unrequested_delivery_history(tmp_path):
    ledger, dates, now_ms = seed_intake_combo_history(tmp_path, days=4, requested_dates=2)
    args = dict(repo=ledger, runtime_root=tmp_path, account="lx",
                runtime_environment="opend:127.0.0.1:11111", mode="observe", effective_now_ms=now_ms)
    assert all(row["complete"] for row in application.reconcile_account_post_trade_combos(**args)["evidence_reads"])
    path = tmp_path / "output_accounts/lx/state/daily_decision_brief.US.delivery.json"
    raw = json.loads(path.read_text())
    unrequested = next(day for day in raw["days"] if day not in dates)
    envelope = next(iter(raw["days"][unrequested]["fixed_reports"].values()))
    envelope["message_sha256"] = "0" * 64
    path.write_text(json.dumps(raw))
    result = application.reconcile_account_post_trade_combos(**args)
    assert result["evidence_reads"]
    assert all(not row["complete"] for row in result["evidence_reads"])


def test_no_scopes_never_opens_history(tmp_path, monkeypatch):
    writes = []

    def reconcile(**kwargs):
        if kwargs["persist"]:
            writes.append(kwargs["exposures"])
        return {}

    def forbidden(**kwargs):
        pytest.fail("empty scope collection must not read history")

    monkeypatch.setattr(application, "reconcile_combo_pair_inferences", reconcile)
    monkeypatch.setattr(application, "read_combo_candidate_exposures_batch", forbidden)
    result = application.reconcile_account_post_trade_combos(
        repo=object(), runtime_root=tmp_path, account="lx", runtime_environment="isolated", mode="observe")
    assert result["evidence_reads"] == [] and writes == [[]]


def test_delivery_change_between_dates_is_seen_by_next_batch(tmp_path, monkeypatch):
    ledger, _dates, now_ms = seed_intake_combo_history(tmp_path)
    path = tmp_path / "output_accounts/lx/state/daily_decision_brief.US.delivery.json"
    original = history.read_combo_candidate_exposures
    changed = False

    def interleaved(**kwargs):
        nonlocal changed
        result = original(**kwargs)
        if not changed:
            changed = True
            raw = json.loads(path.read_text())
            envelope = next(iter(next(iter(raw["days"].values()))["fixed_reports"].values()))
            envelope["message_sha256"] = "0" * 64
            replacement = path.with_suffix(".replacement")
            replacement.write_text(json.dumps(raw))
            replacement.replace(path)
        return result

    monkeypatch.setattr(history, "read_combo_candidate_exposures", interleaved)
    args = dict(repo=ledger, runtime_root=tmp_path, account="lx",
                runtime_environment="opend:127.0.0.1:11111", mode="observe", effective_now_ms=now_ms)
    current = application.reconcile_account_post_trade_combos(**args)
    assert all(row["complete"] for row in current["evidence_reads"])
    following = application.reconcile_account_post_trade_combos(**args)
    assert all(not row["complete"] for row in following["evidence_reads"])


@pytest.mark.parametrize("fail_read", [False, True])
def test_tick_commits_while_intake_decodes_its_history_snapshot(tmp_path, monkeypatch, fail_read):
    ledger, dates, now_ms = seed_intake_combo_history(tmp_path)
    original_decode = DecisionHistoryStore.decode
    committed = False
    run_id = "concurrent-tick"
    source = _brief(run_id=run_id, status="failed", actionability="blocked")
    write_brief = """
import json
from pathlib import Path
import sys
from src.application.daily_decision_brief_repository import persist_daily_decision_brief_failure

saved = persist_daily_decision_brief_failure(base=Path(sys.argv[1]), brief=json.loads(sys.argv[2]))
assert saved["revision"] == 1
"""

    def observed(row, **kwargs):
        nonlocal committed
        if not committed:
            result = subprocess.run(
                [sys.executable, "-c", write_brief, str(tmp_path), json.dumps(source)],
                capture_output=True, text=True, timeout=5, check=False,
            )
            assert result.returncode == 0, result.stderr
            committed = True
            assert history._BRIEF_HISTORY_OBSERVATION.get().revisions(dates[-1]) == [0]
            if fail_read:
                raise RuntimeError("isolated mid-batch failure")
        return original_decode(row, **kwargs)

    monkeypatch.setattr(DecisionHistoryStore, "decode", staticmethod(observed))
    args = dict(repo=ledger, runtime_root=tmp_path, account="lx",
                runtime_environment="opend:127.0.0.1:11111", mode="observe", effective_now_ms=now_ms)
    if fail_read:
        with pytest.raises(RuntimeError, match="isolated mid-batch failure"):
            application.reconcile_account_post_trade_combos(**args)
        assert ledger.list_combo_pair_inferences(account="lx") == []
    else:
        result = application.reconcile_account_post_trade_combos(**args)
        assert all(r["complete"] and r["exposure_count"] == 1 for r in result["evidence_reads"])
    assert committed
    assert history._BRIEF_HISTORY_OBSERVATION.get() is None
    assert history.list_daily_decision_brief_revisions(
        base=tmp_path, account="lx", market="US", market_trading_date=dates[-1],
    )["revisions"] == [0, 1]
