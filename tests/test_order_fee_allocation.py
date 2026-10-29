from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.performance.period import PeriodWindow
from domain.domain.trade_execution import execution_instant_milliseconds, futu_execution_time
from src.application.cash_conversion import build_cash_conversion
from src.application.ledger import order_fee_allocation as allocation
from src.application.ledger import order_fee_migration
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.service import build_option_period_performance


STAMP = "2026-05-26 10:30:00.123"
OLD_CODE = "US.NVDA260619P100000"
NEW_CODE = "US.NVDA260717P100000"
AT = execution_instant_milliseconds(futu_execution_time({"code": OLD_CODE, "create_time": STAMP})["occurred_at_utc"])


def _snapshot(repo):
    with sqlite3.connect(repo.db_path) as conn:
        return dict(conn.execute("SELECT event_id,event_json FROM trade_events"))


def _event(eid, kind, *, new=False, account="demo", order="combo-fixture", estimated=True):
    code = NEW_CODE if new else OLD_CODE
    side = "sell" if kind == "open" else "buy"
    return TradeEvent(event_id=eid, event_type=kind, event_time_ms=AT,
        contract_key=ContractKey.from_values(broker="富途", account=account, underlying_symbol="NVDA",
            option_type="put", strike=100, expiration_ymd="2026-07-17" if new else "2026-06-19"),
        contracts=1, price=5 if new else 1, currency="USD", multiplier=100,
        source="opend_push", fees=0 if estimated else 1,
        lot_id="lot-" + eid if kind == "open" else None,
        target_lot_id="lot-seed" if kind == "close" else None,
        raw_payload={"side": side, "code": code, "trd_side": "SELL_SHORT" if kind == "open" else "BUY_BACK",
            "futu_account_id": "123", "order_id": order, "source_deal_id": "deal-" + eid,
            "deal_id": "deal-" + eid, "create_time": STAMP,
            "fee_provenance": {"amount": "4" if estimated else "1", "basis": "estimated" if estimated else "actual", "source": "fixture"}})


@pytest.fixture
def fixture(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    seed = replace(_event("seed", "open", order="seed-order", estimated=False), event_time_ms=AT - 1000, price=2)
    close = _event("close", "close")
    opening = _event("new", "open", new=True)
    unrelated = _event("other", "open", account="other", order="other-order", estimated=False)
    for event in (seed, close, opening, unrelated):
        repo.upsert_trade_event(event)
    run_position_projection_forced_full(repo)
    raw = _snapshot(repo)
    request = {"schema_version": allocation.REQUEST_SCHEMA, "request_id": "fixture-allocation", "actor": "operator",
        "authorization_ref": "fixture-user-approval", "reason": "Confirmed equal allocation", "method": "equal",
        "account": "demo", "futu_account_id": "123", "order_id": "combo-fixture", "currency": "USD", "expected_total_fee": "8",
        "targets": [{"event_id": eid, "before_sha256": hashlib.sha256(raw[eid].encode()).hexdigest()} for eid in ("close", "new")],
        "evidence": {"observed_at_ms": AT + 500, "account": "demo", "futu_account_id": "123", "environment": "REAL", "day": "2026-05-26",
            "order": {"order_id": "combo-fixture", "currency": "USD", "order_status": "FILLED_ALL", "dealt_qty": "1",
                "combo_legs": [{"code": OLD_CODE, "trd_side": "BUY_BACK", "qty_ratio": "1"}, {"code": NEW_CODE, "trd_side": "SELL_SHORT", "qty_ratio": "1"}]},
            "deals": [{"deal_id": "deal-close", "order_id": "combo-fixture", "code": OLD_CODE, "trd_side": "BUY_BACK", "qty": "1", "price": "1", "create_time": STAMP},
                {"deal_id": "deal-new", "order_id": "combo-fixture", "code": NEW_CODE, "trd_side": "SELL_SHORT", "qty": "1", "price": "5", "create_time": STAMP}],
            "fee": {"order_id": "combo-fixture", "fee_amount": "8", "fee_details": [["commission", "3"], ["platform", "5"]]}}}
    return repo, request


def _apply(repo, request, tmp_path):
    preview = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    return allocation.apply_confirmed_order_fee_allocation(repo, request=request,
        expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")


def _report(repo):
    period = PeriodWindow("month", "UTC", "2026-05-01", "2026-05-31", AT - 86400000, AT + 86400000,
                          AT - 86400000, AT + 86400000 - 1, "complete")
    return build_option_period_performance(repo, period=period, config_key="us",
        configured_accounts=["demo", "other"], account="demo", include_rows=True)


def test_preview_apply_has_durable_provenance_conservation_backup_and_performance(fixture, tmp_path):
    repo, request = fixture
    before = _snapshot(repo)
    with sqlite3.connect(repo.db_path) as conn:
        other_lot = conn.execute("SELECT * FROM position_lots WHERE lot_id='lot-other'").fetchone()
    report_before = _report(repo)
    assert report_before["period_option_net_cashflow"]["by_currency"]["USD"]["total"]["missing"] == ["actual_fee_missing"]
    assert "fee_estimated" in report_before["option_net_cashflow"]["by_currency"]["USD"]["total"]["missing"]
    preview = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert preview["broker_leg_fees_observed"] is False
    assert preview["allocations"] == [{"event_id": "close", "amount": "4"}, {"event_id": "new", "amount": "4"}]
    assert _snapshot(repo) == before
    result = allocation.apply_confirmed_order_fee_allocation(repo, request=request,
        expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")
    assert result["mode"] == "committed" and result["durable_readback"] is True
    after = _snapshot(repo)
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT * FROM position_lots WHERE lot_id='lot-other'").fetchone() == other_lot
    assert {k for k in before if before[k] != after[k]} == {"close", "new"}
    for eid in ("close", "new"):
        original, updated = json.loads(before[eid]), json.loads(after[eid])
        assert Decimal(str(updated["fees"])) == 4
        provenance = updated["raw_payload"]["fee_provenance"]
        assert provenance["source"] == allocation.SOURCE
        assert provenance["broker_evidence"] == request["evidence"]
        assert provenance["authorization_ref"] == request["authorization_ref"]
        assert provenance["allocation_basis"] == "user_confirmed_equal"
        for field in ("fees", "raw_payload"):
            original.pop(field); updated.pop(field)
        assert original == updated
        event = TradeEvent.from_dict(json.loads(after[eid]))
        facts = cash_facts_for_trade_event(event)
        assert sum(f.fact_kind == "option_fee_cash" for f in facts) == 1
        assert next(f.amount for f in facts if f.fact_kind == "option_fee_cash") == -4
    with sqlite3.connect(result["backup"]["path"]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert dict(backup.execute("SELECT event_id,event_json FROM trade_events")) == before
    assert hashlib.sha256(Path(result["backup"]["path"]).read_bytes()).hexdigest() == result["backup"]["sha256"]
    assert Path(result["backup"]["path"]).stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM broker_fee_enrichment_audit").fetchone()[0] == 2
    report_after = _report(repo)
    assert report_after["period_option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == 591
    assert report_after["option_net_cashflow"]["by_currency"]["USD"]["total"]["missing"] == []
    assert "actual_fee_missing" not in report_after["period_option_net_cashflow"]["missing"]
    # Only the native fee gap is repaired; lack of historical FX remains visible.
    assert report_after["period_option_net_cashflow"]["cny_total"]["status"] == "partial"
    repeated = allocation.apply_confirmed_order_fee_allocation(repo, request=request,
        expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")
    assert repeated["mode"] == "no_op" and repeated["write_applied"] is False
    assert _snapshot(repo) == after
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM broker_fee_enrichment_audit").fetchone()[0] == 2


@pytest.mark.parametrize("path,value", [
    (("expected_total_fee",), "8.01"), (("expected_total_fee",), "NaN"), (("expected_total_fee",), True),
    (("expected_total_fee",), "0"), (("expected_total_fee",), "-2"), (("expected_total_fee",), "8.000001"),
    (("method",), "weighted"), (("account",), "DEMO"), (("authorization_ref",), ""),
    (("evidence", "account"), "other"), (("evidence", "environment"), "SIMULATE"),
    (("evidence", "order", "currency"), "HKD"), (("evidence", "order", "order_status"), "SUBMITTED"),
    (("evidence", "order", "dealt_qty"), "2"), (("evidence", "order", "combo_legs", 0, "qty_ratio"), "2"),
    (("evidence", "deals", 0, "order_id"), "other-order"), (("evidence", "deals", 0, "deal_id"), "missing"),
    (("evidence", "deals", 0, "price"), "3"), (("evidence", "deals", 0, "create_time"), "2026-05-26 10:30:01"),
    (("evidence", "fee", "fee_amount"), "9"), (("evidence", "fee", "fee_details"), [["commission", "7"]]),
])
def test_invalid_evidence_rejected_before_any_write(fixture, path, value):
    repo, request = fixture
    before = _snapshot(repo)
    target = request
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert _snapshot(repo) == before


def test_duplicate_missing_unknown_and_incomplete_group_rejected(fixture):
    repo, request = fixture
    for mutate in (lambda r: r.update(extra=True), lambda r: r["targets"].pop(),
                   lambda r: r["targets"].__setitem__(1, deepcopy(r["targets"][0])),
                   lambda r: r["evidence"]["deals"].pop()):
        changed = deepcopy(request); mutate(changed)
        with pytest.raises(ValueError):
            allocation.prepare_confirmed_order_fee_allocation(repo, request=changed)
    repo.upsert_trade_event(_event("extra", "open", new=True))
    run_position_projection_forced_full(repo)
    with pytest.raises(ValueError, match="complete active order group"):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=request)


def test_stale_input_and_backup_collision_fail_without_fee_write(fixture, tmp_path):
    repo, request = fixture
    preview = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    before = _snapshot(repo)
    with pytest.raises(ValueError, match="input hash changed"):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash="0" * 64, backup_dir=tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    (tmp_path / "collision").mkdir()
    with pytest.raises(FileExistsError):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "collision")
    assert _snapshot(repo) == before


def test_unrelated_projection_drift_blocks_before_backup(fixture, tmp_path):
    repo, request = fixture
    preview = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    with repo._writer_connection(begin_immediate=True) as conn:
        row = conn.execute("SELECT fields_json FROM position_lots WHERE lot_id='lot-other'").fetchone()
        payload = json.loads(row[0]); payload["note"] = "unrelated drift"
        conn.execute("UPDATE position_lots SET fields_json=? WHERE lot_id='lot-other'", (json.dumps(payload),))
    before = _snapshot(repo)
    with pytest.raises(ValueError, match="projection is not current"):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    assert _snapshot(repo) == before


def test_rollback_after_first_target_preserves_backup_and_original_fees(fixture, tmp_path, monkeypatch):
    repo, request = fixture
    before = _snapshot(repo)
    real_insert = order_fee_migration._insert_audit
    calls = []
    def fail_after_one(*args, **kwargs):
        calls.append(kwargs["change"].event_id)
        real_insert(*args, **kwargs)
        if len(calls) == 1:
            raise ValueError("injected after first update")
    monkeypatch.setattr(order_fee_migration, "_insert_audit", fail_after_one)
    receipt = _apply(repo, request, tmp_path)
    assert receipt["mode"] == "not_applied" and receipt["write_applied"] is False
    assert calls and _snapshot(repo) == before
    assert Path(receipt["backup"]["path"]).is_file()
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='broker_fee_enrichment_audit'").fetchone() is None


def test_committed_readback_failure_can_safely_retry(fixture, tmp_path, monkeypatch):
    repo, request = fixture
    real_readback = allocation._readback
    def fail_readback(*args, **kwargs):
        raise sqlite3.OperationalError("injected readback failure")
    monkeypatch.setattr(allocation, "_readback", fail_readback)
    receipt = _apply(repo, request, tmp_path)
    assert receipt["mode"] == "committed_readback_unavailable" and receipt["write_applied"] is True
    monkeypatch.setattr(allocation, "_readback", real_readback)
    retry = allocation.apply_confirmed_order_fee_allocation(repo, request=request,
        expected_input_hash=receipt["input_hash"], backup_dir=tmp_path / "backup")
    assert retry["mode"] == "no_op" and retry["durable_readback"] is True


def test_valid_historical_fx_is_preserved(fixture, tmp_path):
    repo, request = fixture
    fx_repo = SQLiteOptionPositionsRepository(tmp_path / "with-fx.sqlite3")
    for eid, encoded in _snapshot(repo).items():
        event = TradeEvent.from_dict(json.loads(encoded))
        if eid in ("close", "new"):
            conversion = build_cash_conversion(cash_fact_id="option_fee_cash:" + eid, amount=Decimal("-4"), currency="USD",
                fx_payload={"rates": {"USDCNY": "7.2"}, "timestamp": "2026-05-26T14:30:00.123+00:00"},
                effective_at_ms=AT, observed_at_ms=AT + 1, rate_source="pbc_central_parity",
                rate_source_id="fixture-pbc", rate_evidence_fact_id="fixture-fx", method="historical_fx_evidence_backfill")
            event = replace(event, raw_payload={**event.raw_payload, "cash_conversions": {"option_fee_cash": conversion}})
        fx_repo.upsert_trade_event(event)
    repo = fx_repo
    run_position_projection_forced_full(repo)
    before = _snapshot(repo)
    request["targets"] = [{"event_id": eid, "before_sha256": hashlib.sha256(before[eid].encode()).hexdigest()} for eid in ("close", "new")]
    assert _apply(repo, request, tmp_path)["mode"] == "committed"
    after = _snapshot(repo)
    for eid in ("close", "new"):
        assert json.loads(before[eid])["raw_payload"]["cash_conversions"] == json.loads(after[eid])["raw_payload"]["cash_conversions"]


def test_cli_uses_existing_store_and_confirmation_gate(fixture, tmp_path, capsys, monkeypatch):
    from src.interfaces.cli import trade_events
    repo, request = fixture
    file = tmp_path / "request.json"; file.write_text(json.dumps(request))
    monkeypatch.setattr(trade_events, "open_order_fee_allocation_store", lambda **kwargs: (repo, {"sqlite_path": str(repo.db_path)}))
    calls = []
    def forbidden_provider(*args, **kwargs):
        calls.append("provider")
        raise AssertionError("no provider query allowed")
    monkeypatch.setattr(trade_events, "OpenDHistoryDealClient", forbidden_provider)
    assert trade_events.main(["fees-allocate", "--request", str(file)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] and calls == []
    with pytest.raises(SystemExit, match="use --confirm"):
        trade_events.main(["fees-allocate", "--request", str(file), "--apply"])
    monkeypatch.setattr(trade_events, "_guard_write", lambda **kwargs: {"ok": True})
    assert trade_events.main(["fees-allocate", "--request", str(file), "--expected-input-hash", preview["input_hash"],
        "--backup-dir", str(tmp_path / "backup"), "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["durable_readback"] is True
    assert calls == []


def _changed_fixture(fixture, tmp_path, mutate):
    previous, request = fixture
    repo = SQLiteOptionPositionsRepository(tmp_path / "changed.sqlite3")
    for eid, raw in _snapshot(previous).items():
        event = TradeEvent.from_dict(json.loads(raw))
        if eid == "new":
            event = mutate(event)
        repo.upsert_trade_event(event)
    run_position_projection_forced_full(repo)
    rows = _snapshot(repo)
    request["targets"] = [{"event_id": eid, "before_sha256": hashlib.sha256(rows[eid].encode()).hexdigest()} for eid in ("close", "new")]
    return repo, request


@pytest.mark.parametrize("mutation,match", [
    (lambda e: replace(e, fees=4, raw_payload={**e.raw_payload, "fee_provenance": {"amount": "4", "basis": "actual", "source": "fixture"}}), "existing actual"),
    (lambda e: replace(e, raw_payload={**e.raw_payload, "acc_id": "999"}), "account/currency/environment"),
    (lambda e: replace(e, raw_payload={**e.raw_payload, "_trade_intake_source": {"account": "other", "futu_account_id": "123"}}), "intake account"),
    (lambda e: replace(e, raw_payload={**e.raw_payload, "broker_deal_completion": {"split_count": 2, "split_index": 1, "expected_contracts": 1}}), "split broker"),
    (lambda e: replace(e, raw_payload={**e.raw_payload, "external_order_namespace": "other.order"}), "account/currency/environment"),
])
def test_target_actual_scope_and_split_conflicts(fixture, tmp_path, mutation, match):
    repo, request = _changed_fixture(fixture, tmp_path, mutation)
    before = _snapshot(repo)
    with pytest.raises(ValueError, match=match):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert _snapshot(repo) == before


def test_full_readset_change_and_voided_target_reject(fixture, tmp_path):
    repo, request = fixture
    preview = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("UPDATE trade_events SET updated_at_ms=updated_at_ms+1 WHERE event_id='other'")
    with pytest.raises(ValueError, match="input hash changed"):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request,
            expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")
    repo.upsert_trade_event(TradeEvent(event_id="void-new", event_type="void", event_time_ms=AT+1,
        contract_key=_event("new", "open", new=True).contract_key, contracts=0, price=0, currency="USD",
        source="fixture", multiplier=100, target_event_id="new"))
    run_position_projection_forced_full(repo)
    with pytest.raises(ValueError, match="complete active order group"):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=request)


def test_same_request_id_conflict_and_postcommit_drift_are_not_noops(fixture, tmp_path):
    repo, request = fixture
    result = _apply(repo, request, tmp_path)
    changed = deepcopy(request); changed["reason"] = "different approval"
    with pytest.raises(ValueError, match="request_id conflicts"):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=changed)
    with repo._writer_connection(begin_immediate=True) as conn:
        row = json.loads(conn.execute("SELECT event_json FROM trade_events WHERE event_id='new'").fetchone()[0])
        row["raw_payload"]["unreviewed_note"] = "drift"
        conn.execute("UPDATE trade_events SET event_json=? WHERE event_id='new'", (json.dumps(row),))
    with pytest.raises(ValueError, match="audit/readback drift"):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request,
            expected_input_hash=result["input_hash"], backup_dir=tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_saved_fx_is_bound_and_pending_resolution_is_deterministic(fixture, tmp_path, monkeypatch):
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
    repo, request = fixture
    stale = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    evidence.import_envelope({"schema_version": "option_performance_evidence.v1", "valuation_marks": [], "fx_rates": [{
        "base_currency": "USD", "quote_currency": "CNY", "rate": "7.2", "rate_kind": "central_parity",
        "effective_at_ms": AT, "observed_at_ms": AT+1, "source": "pbc_central_parity", "source_id": "fixture-rate",
        "revision": 1, "supersedes_fact_id": None, "quality": {}, "raw": {}}]}, apply=True, migrated_at_ms=AT+2)
    with pytest.raises(ValueError, match="input hash changed"):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request,
            expected_input_hash=stale["input_hash"], backup_dir=tmp_path / "stale")
    first = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    second = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert first == second
    def forbidden_fx_load(*args, **kwargs):
        raise AssertionError("manual apply must consume planned FX without loading/persisting cache")
    monkeypatch.setattr(order_fee_migration, "load_cash_fx_payload", forbidden_fx_load)
    result = allocation.apply_confirmed_order_fee_allocation(repo, request=request,
        expected_input_hash=first["input_hash"], backup_dir=tmp_path / "backup")
    assert result["mode"] == "committed"
    for row in first["events"]:
        after = json.loads(_snapshot(repo)[row["event_id"]])
        assert after == row["after"]
        assert after["raw_payload"]["cash_conversions"]["option_fee_cash"]["status"] == "observed"


def test_unrelated_missing_fee_stays_missing(fixture, tmp_path):
    repo, request = fixture
    extra = replace(_event("missing", "open", order="missing-order"),
                    raw_payload={"side": "sell", "fee_provenance": {"basis": "missing", "source": "fixture"}})
    repo.upsert_trade_event(extra)
    run_position_projection_forced_full(repo)
    assert _apply(repo, request, tmp_path)["mode"] == "committed"
    report = _report(repo)
    assert "actual_fee_missing" in report["period_option_net_cashflow"]["missing"]
    assert any(row["open_event_id"] == "missing" and row["status"] == "partial" for row in report["rows"])


def test_backup_failure_and_cancellation_never_change_events(fixture, tmp_path, monkeypatch):
    repo, request = fixture
    preview = allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    before = _snapshot(repo)
    original_backup = allocation._backup
    def fail_backup(*args, **kwargs):
        raise OSError("injected backup failure")
    monkeypatch.setattr(allocation, "_backup", fail_backup)
    with pytest.raises(OSError, match="backup failure"):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request,
            expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")
    assert _snapshot(repo) == before
    monkeypatch.setattr(allocation, "_backup", original_backup)
    def cancel_after_update(*args, **kwargs):
        raise KeyboardInterrupt("injected cancellation before commit")
    monkeypatch.setattr(order_fee_migration, "_insert_audit", cancel_after_update)
    with pytest.raises(KeyboardInterrupt):
        allocation.apply_confirmed_order_fee_allocation(repo, request=request,
            expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "backup")
    assert _snapshot(repo) == before
    assert (tmp_path / "backup" / "ledger.sqlite3").is_file()


def test_malformed_present_fx_table_fails_instead_of_becoming_empty(fixture):
    repo, request = fixture
    before = _snapshot(repo)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("CREATE TABLE performance_fx_rate_facts (invalid_column TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert _snapshot(repo) == before


def test_same_physical_order_across_account_labels_is_ambiguous(fixture):
    repo, request = fixture
    repo.upsert_trade_event(_event("wrong-account", "open", account="other", new=True))
    run_position_projection_forced_full(repo)
    with pytest.raises(ValueError, match="conflicting account labels"):
        allocation.prepare_confirmed_order_fee_allocation(repo, request=request)
