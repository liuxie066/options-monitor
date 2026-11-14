"""Operation-bounded history reads must reduce real I/O without hiding changes."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path

import pytest

from daily_brief_history_fixtures import seed_delivery_history
from daily_brief_preparation_fixtures import NOW, seed_preparation_inputs
from src.application import daily_decision_brief_repository as repository
from src.application.ledger.api import open_trade_reconciliation_evidence_repo, read_trade_attribution_snapshot
from src.application.trades import attribution
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, canonical_json, content_hash, history_path


def test_real_attribution_reads_delivery_once_and_uses_one_history_transaction(tmp_path, monkeypatch):
    seed_preparation_inputs(tmp_path, days=3, fill_dates=2, fills_per_date=1)
    ledger = open_trade_reconciliation_evidence_repo(tmp_path / "output_shared/state/option_positions.sqlite3")
    rows = read_trade_attribution_snapshot(ledger, account="lx", market="hk")
    reads, connections = [], []
    original_read, original_connect = Path.read_text, DecisionHistoryStore.connect

    def counted_read(path, *args, **kwargs):
        if path.name.endswith(".delivery.json"):
            reads.append(path)
        return original_read(path, *args, **kwargs)

    @contextmanager
    def counted_connect(store, **kwargs):
        with original_connect(store, **kwargs) as conn:
            connections.append(store.path)
            yield conn

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr(DecisionHistoryStore, "connect", counted_connect)
    actual = attribution.read_attribution_combo_evidence(
        rows, account="lx", runtime_root=tmp_path, now_ms=int(NOW.timestamp() * 1000))
    assert actual == {"complete": False, "reads": [
        {"market": "HK", "market_date": "2026-09-01", "complete": False},
        {"market": "HK", "market_date": "2026-09-26", "complete": True},
        {"market": "HK", "market_date": "2026-09-27", "complete": True},
    ], "exposures": []}
    assert len(reads) == 1
    assert len(connections) == 1


@pytest.fixture
def history(tmp_path):
    delivery = seed_delivery_history(tmp_path, days=3)
    return tmp_path, delivery, ["2026-07-18", "2026-07-19", "2026-07-20"]


def batch(base, dates):
    return repository.read_combo_candidate_exposures_batch(
        base=base, account="lx", market="US", market_trading_dates=dates)


def independent(base, dates):
    return {day: repository.read_combo_candidate_exposures(
        base=base, account="lx", market="US", market_trading_date=day) for day in dates}


@pytest.mark.parametrize("reuse_scope", [False, True])
def test_each_independent_delivery_observation_uses_one_fresh_source_transaction(history, monkeypatch, reuse_scope):
    base, _, _ = history
    args = dict(base=base, account="lx", market="US")
    scope = repository.DailyBriefReadScope(**args) if reuse_scope else None
    original = DecisionHistoryStore.connect
    connections = []

    @contextmanager
    def counted(store, **kwargs):
        with original(store, **kwargs) as conn:
            connections.append(conn)
            yield conn

    monkeypatch.setattr(DecisionHistoryStore, "connect", counted)
    for _ in range(2):
        connections.clear()
        assert repository.read_daily_decision_brief_delivery_state(**args, read_scope=scope)["available"]
        assert len(connections) == 1


def test_batch_preserves_results_and_copies_and_observes_the_next_delivery(history):
    base, delivery, dates = history
    expected = independent(base, dates)
    actual = batch(base, dates + dates)
    assert actual == expected
    actual[dates[0]]["invalid_revisions"].append(999)
    assert batch(base, dates) == expected
    original = delivery.read_bytes()
    delivery.write_text("{", encoding="utf-8")
    with pytest.raises(repository.DailyDecisionBriefStateError, match="failed to read daily brief state"):
        batch(base, dates)
    with pytest.raises(repository.DailyDecisionBriefStateError, match="failed to read daily brief state"):
        independent(base, dates)
    delivery.unlink()
    assert batch(base, dates) == independent(base, dates)
    delivery.write_bytes(original)
    assert batch(base, dates) == expected


def test_batch_reobserves_changed_or_corrupt_referenced_source(history):
    base, delivery, dates = history
    before = batch(base, dates)
    original_stat = delivery.stat()
    day = dates[0]
    raw = repository.read_daily_decision_brief(
        base=base, account="lx", market="US", market_trading_date=day, revision=0)["brief"]
    changed = deepcopy(raw)
    changed["strategy_summary"] = "changed source behind unchanged delivery bytes"
    with DecisionHistoryStore(history_path(base)).connect(write=True) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE name='decisions_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER decisions_no_update")
        conn.execute("UPDATE decisions SET payload_json=?,payload_hash=? WHERE account='lx' AND market='US' AND market_date=?",
                     (canonical_json(changed), content_hash(changed), day))
        conn.execute(trigger)
    after = batch(base, dates)
    assert after == independent(base, dates)
    assert after != before
    assert delivery.stat().st_mtime_ns == original_stat.st_mtime_ns
    with DecisionHistoryStore(history_path(base)).connect(write=True) as conn:
        conn.execute("DROP TRIGGER decisions_no_update")
        conn.execute("UPDATE decisions SET payload_json='{' WHERE market_date=?", (day,))
    assert batch(base, dates) == independent(base, dates)
    assert all(not item["complete"] for item in batch(base, dates).values())


@pytest.mark.parametrize("other_account", [False, True])
def test_batch_does_not_decode_unrelated_corrupt_rows(history, other_account):
    base, _, dates = history
    expected = batch(base, dates)
    from test_daily_decision_brief_repository_v2 import _brief
    account = "other" if other_account else "lx"
    day = dates[0] if other_account else "2026-06-01"
    repository.persist_daily_decision_brief_success(
        base=base, brief=_brief(run_id="unrelated", account=account, market_date=day))
    with DecisionHistoryStore(history_path(base)).connect(write=True) as conn:
        conn.execute("DROP TRIGGER decisions_no_update")
        conn.execute("UPDATE decisions SET payload_json='{' WHERE run_id='unrelated'")
    assert batch(base, dates) == expected


@pytest.mark.parametrize("display_state", [None, "expanded", "summary_notified", "not_shown"])
def test_batch_preserves_nonempty_legacy_combo_exposure_and_confirmation(tmp_path, display_state):
    from domain.domain.combo_candidate_evidence import combo_exposure_render_context, derive_combo_candidate_exposures
    from src.application.notification_delivery_adapter import build_notification_transport_key
    from test_daily_decision_brief_repository_v2 import (
        MARKET_DATE, _install_legacy_combo_revision, _prepare, _record_candidates,
    )

    legacy, source_digest, _ = _install_legacy_combo_revision(tmp_path)
    identity = "candidate:v1:lx:US:NVDA:combo_yield"
    _record_candidates(tmp_path, revision=0, brief_digest=source_digest,
                       candidate_identities=[identity], observed_at_utc="2026-07-21T14:00:02+00:00")
    exposures = derive_combo_candidate_exposures(legacy, candidate_identities=[identity])
    envelope = _prepare(tmp_path, run_id="run-1", delivery_kind="candidate_alert",
        source_kind="successful_brief", revision=0, source_digest=source_digest,
        candidate_identities=[identity], rendered_message="# synthetic legacy Combo candidate",
        render_context={**combo_exposure_render_context(exposures), "rendered_combo_candidate_identities": [identity]},
        prepared_at_utc="2026-07-21T14:00:03+00:00",
        **({"display_evidence": [{"identity": identity, "state": display_state}]} if display_state else {}))["envelope"]
    repository.confirm_daily_decision_brief_delivery_v2(
        base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"], source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
        confirmed_at_utc="2026-07-21T14:00:04+00:00")
    expected = independent(tmp_path, [MARKET_DATE])
    assert len(expected[MARKET_DATE]["exposures"]) == 1
    assert expected[MARKET_DATE]["exposures"][0]["delivery_confirmed"] is (display_state in {None, "expanded"})
    assert batch(tmp_path, [MARKET_DATE]) == expected


def test_batch_missing_history_does_not_lease_absence(tmp_path):
    dates = ["2026-07-18", "2026-07-19", "2026-07-20"]
    assert batch(tmp_path, dates) == independent(tmp_path, dates)
    seed_delivery_history(tmp_path, days=3)
    assert all(item["complete"] for item in batch(tmp_path, dates).values())


def test_wheel_projection_reuses_only_identical_historical_instants(tmp_path, monkeypatch):
    from src.application import daily_decision_brief_service as service
    from src.application.ledger import trade_attribution as ledger_attribution
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.auto_intake import _process_payload
    from test_trade_attribution_meituan import _call_payload

    config = seed_preparation_inputs(tmp_path, days=3, fill_dates=2, fills_per_date=2)
    ledger = SQLiteOptionPositionsRepository(tmp_path / "output_shared/state/option_positions.sqlite3")
    payload = _call_payload(deal_id="same-day-distinct-instant", contracts=1)
    payload["occurred_at_utc"] = "2026-09-27T02:45:32Z"
    result = _process_payload(payload, repo=ledger, state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl", account_mapping={"1001": "lx"}, futu_account_ids=["1001"],
        apply_changes=True, host="127.0.0.1", port=11111, allow_external_lookup=False,
        config=config, runtime_root=tmp_path)
    assert result["status"] == "applied", result
    calls = []
    original = ledger_attribution.build_wheel_read_model_from_rows

    def counted(*args, **kwargs):
        calls.append(kwargs["as_of_ms"])
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger_attribution, "build_wheel_read_model_from_rows", counted)
    pending, error = service._pending_attribution_for_brief(
        base=tmp_path, config=config, account="lx", market="HK", now_ms=int(NOW.timestamp() * 1000))
    assert error is None and len(pending) == 5
    assert len(set(calls)) == 3
    assert len(calls) == 3


def test_batch_keeps_delivery_envelope_digest_validation(history):
    base, delivery, dates = history
    raw = json.loads(delivery.read_text(encoding="utf-8"))
    envelope = next(iter(raw["days"][dates[0]]["fixed_reports"].values()))
    envelope["message_sha256"] = "0" * 64
    delivery.write_text(json.dumps(raw), encoding="utf-8")
    actual = batch(base, dates)
    assert actual == independent(base, dates)
    assert all(not item["complete"] for item in actual.values())
