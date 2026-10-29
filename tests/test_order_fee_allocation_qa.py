"""Independent synthetic acceptance tests for explicit order-fee allocation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.performance.cash_conversion import cash_conversion_id, cash_conversion_identity
from domain.domain.performance.period import normalize_performance_period
from src.application.ledger import api
from src.application.ledger import order_fee_allocation as allocation
from src.application.ledger import order_fee_migration
from src.application.ledger.event_codec import stored_trade_event_to_ledger_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.performance.service import build_option_period_performance
from src.interfaces.cli.trade_events import main as cli
from tests.ledger_sqlite_test_support import connect_ledger_fixture


TZ = ZoneInfo("Asia/Hong_Kong")
AT = int(datetime(2025, 9, 8, 11, 7, 9, 123000, tzinfo=TZ).timestamp() * 1000)
TARGETS = ("qa-roll-close", "qa-roll-open")


def _events(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT event_id,event_json FROM trade_events"))


def _business(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def _fixture(tmp_path: Path, *, additional_gap: bool = False, duplicate_source: bool = False, db_name: str = "ledger.sqlite3"):
    repo = SQLiteOptionPositionsRepository(tmp_path / db_name)

    def trade(identity, at, price, *, expiration="2025-09-29", account="qa", kind="open", order=None, deal=None, fee="3.2", basis="actual"):
        raw = {"side": "sell" if kind == "open" else "buy",
               "fee_provenance": {"amount": fee, "basis": basis, "source": "qa_fixture"}}
        if order:
            raw.update(futu_account_id="91001", order_id=order, source_deal_id=deal,
                       code=f"HK.QAT{expiration[2:].replace('-', '')}P80000",
                       trd_side="SELL_SHORT" if kind == "open" else "BUY_BACK", trd_env="REAL")
        event = TradeEvent(event_id=identity, event_type=kind, event_time_ms=at,
            contract_key=ContractKey.from_values(broker="富途", account=account,
                underlying_symbol="0005.HK", option_type="put", strike=80, expiration_ymd=expiration),
            contracts=1, price=Decimal(price), currency="HKD", source="qa_fixture",
            multiplier=100, fees=Decimal(fee) if basis == "actual" else 0,
            lot_id=f"lot-{identity}" if kind == "open" else None,
            target_lot_id="lot-qa-prior" if kind == "close" else None, raw_payload=raw)
        conversions = {}
        for fact in cash_facts_for_trade_event(event):
            identity_fields = cash_conversion_identity(cash_fact_id=fact.fact_id,
                native_amount=fact.amount, native_currency="HKD", fx_rate=Decimal("0.9"),
                amount_cny=fact.amount * Decimal("0.9"), rate_source_id=f"qa-rate:{at}", effective_at_ms=at)
            conversions[fact.fact_kind] = {**identity_fields, "schema_version": "cash_conversion.v1",
                "conversion_id": cash_conversion_id(identity_fields), "status": "observed", "quote_currency": "CNY",
                "method": "historical_fx_evidence_backfill", "rate_source": "pbc_central_parity",
                "rate_timestamp": datetime.fromtimestamp(at / 1000, TZ).isoformat(),
                "observed_at_ms": at + 1, "missing_reason": None}
        return replace(event, raw_payload={**raw, "cash_conversions": conversions})

    data = [trade("qa-prior", AT - 7 * 86400000, "2.2"),
            trade(TARGETS[0], AT, "1.8" if duplicate_source else "0.4", kind="open" if duplicate_source else "close", order="qa-order-11", deal="qa-deal-close", basis="estimated"),
            trade(TARGETS[1], AT, "1.8", expiration="2025-10-30", order="qa-order-11", deal="qa-deal-open", basis="estimated"),
            trade("qa-other-account", AT, "1", account="qb")]
    broker_data = deepcopy(data[1:3])
    if duplicate_source:
        raw = {**data[2].raw_payload, "source_deal_id": data[1].raw_payload["source_deal_id"], "code": data[1].raw_payload["code"]}
        data[2] = replace(data[2], raw_payload=raw)
    if additional_gap:
        data.append(trade("qa-other-gap", AT + 1000, "0.7", expiration="2025-12-30", basis="estimated"))
    for event in data:
        repo.upsert_trade_event(event)
    run_position_projection_forced_full(repo)
    stored = _events(repo.db_path)
    request = {"schema_version": "confirmed_order_fee_allocation.v1", "request_id": "qa-equal-request-11",
        "actor": "qa-operator", "authorization_ref": "qa-user-confirmation-11", "reason": "Synthetic explicit equal allocation",
        "method": "equal", "account": "qa", "futu_account_id": "91001", "order_id": "qa-order-11",
        "currency": "HKD", "expected_total_fee": "6.40",
        "targets": [{"event_id": eid, "before_sha256": hashlib.sha256(stored[eid].encode()).hexdigest()} for eid in TARGETS],
        "evidence": {"observed_at_ms": AT + 2000, "account": "qa", "futu_account_id": "91001", "environment": "REAL", "day": "2025-09-08",
            "order": {"order_id": "qa-order-11", "currency": "HKD", "order_status": "FILLED_ALL", "dealt_qty": "1",
                "combo_legs": [{"code": x.raw_payload["code"], "trd_side": x.raw_payload["trd_side"], "qty_ratio": "1"} for x in broker_data]},
            "deals": [{"deal_id": x.raw_payload["source_deal_id"], "order_id": "qa-order-11", "code": x.raw_payload["code"],
                "trd_side": x.raw_payload["trd_side"], "qty": "1", "price": str(x.price), "create_time": "2025-09-08 11:07:09.123"} for x in broker_data],
            "fee": {"order_id": "qa-order-11", "fee_amount": "6.40", "fee_details": [["qa-commission", "1.40"], ["qa-platform", "5"]]}}}
    return repo, request


def _report(repo):
    period = normalize_performance_period({"period": "month", "month": "2025-09"}, report_now_ms=AT + 40 * 86400000)
    return build_option_period_performance(repo, period=period, config_key="hk", configured_accounts=("qa", "qb"), account="qa", include_rows=True)


def _apply(repo, request, root):
    preview = api.prepare_confirmed_order_fee_allocation(repo, request=request)
    result = api.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=root / "qa-backup")
    return preview, result


def test_qa_actual_expense_has_manual_source_and_repairs_real_metrics_without_extra_cash(tmp_path, monkeypatch):
    repo, request = _fixture(tmp_path)
    before = _events(repo.db_path)
    other_lots = [row for row in _business(repo.db_path, "position_lots") if row[-1] == "qb"]
    old_report = _report(repo)
    assert "actual_fee_missing" in old_report["period_option_net_cashflow"]["missing"]
    forbidden = {"provider": 0, "fx_cache": 0}
    def forbid(kind):
        def denied(*args, **kwargs):
            forbidden[kind] += 1
            raise AssertionError(f"forbidden {kind} contact")
        return denied
    import src.infrastructure.futu_gateway as gateway
    monkeypatch.setattr(gateway, "build_futu_gateway", forbid("provider"))
    monkeypatch.setattr(order_fee_migration, "load_cash_fx_payload", forbid("fx_cache"))
    preview, result = _apply(repo, request, tmp_path)
    assert result["mode"] == "committed" and result["durable_readback"] is True
    assert preview["broker_leg_fees_observed"] is False
    after = _events(repo.db_path)
    assert before.keys() == after.keys()
    assert all(after[eid] == raw for eid, raw in before.items() if eid not in TARGETS)
    assert [row for row in _business(repo.db_path, "position_lots") if row[-1] == "qb"] == other_lots
    fee_amounts = []
    for eid in TARGETS:
        old, new = json.loads(before[eid]), json.loads(after[eid])
        assert new["raw_payload"]["cash_conversions"] == old["raw_payload"]["cash_conversions"]
        provenance = new["raw_payload"]["fee_provenance"]
        assert provenance["source"] == "operator_allocated_order_fee"
        assert provenance["broker_evidence_source"] == "operator_supplied_opend_snapshot"
        assert provenance["authorization_ref"] == request["authorization_ref"]
        assert provenance["broker_order_fee_total"] == "6.4"
        event, _ = stored_trade_event_to_ledger_event(new)
        facts = cash_facts_for_trade_event(event)
        assert len(facts) == 2
        fee_amounts.extend(f.amount for f in facts if f.fact_kind == "option_fee_cash")
    assert sum(fee_amounts) == Decimal("-6.4")
    new_report = _report(repo)
    assert new_report["period_option_net_cashflow"]["missing"] == []
    assert new_report["period_option_net_cashflow"]["fact_count"] == old_report["period_option_net_cashflow"]["fact_count"]
    assert new_report["period_option_net_cashflow"]["by_currency"]["HKD"]["total"]["amount"] == pytest.approx(350.4)
    assert all("fee_estimated" not in row["missing"] for row in new_report["rows"])
    assert forbidden == {"provider": 0, "fx_cache": 0}


def test_qa_other_fee_gap_is_not_hidden(tmp_path):
    repo, request = _fixture(tmp_path, additional_gap=True)
    _apply(repo, request, tmp_path)
    report = _report(repo)
    assert "actual_fee_missing" in report["period_option_net_cashflow"]["missing"]
    gaps = [row for row in report["rows"] if "fee_estimated" in row["missing"]]
    assert len(gaps) == 1 and gaps[0]["open_event_id"] == "qa-other-gap"


@pytest.mark.parametrize("change", ["account", "order", "currency", "total", "details", "deal", "code", "time", "side", "quantity", "extra", "zero", "fraction", "bool", "duplicate", "future_evidence"])
def test_qa_unproven_or_conflicting_request_is_readonly(tmp_path, change):
    repo, request = _fixture(tmp_path)
    before = _events(repo.db_path)
    r = deepcopy(request)
    if change == "account": r["evidence"]["account"] = "qb"
    elif change == "order": r["evidence"]["deals"][0]["order_id"] = "qa-wrong-order"
    elif change == "currency": r["evidence"]["order"]["currency"] = "USD"
    elif change == "total": r["expected_total_fee"] = "6.42"
    elif change == "details": r["evidence"]["fee"]["fee_details"][0][1] = "1.41"
    elif change == "deal": r["evidence"]["deals"][0]["deal_id"] = "qa-missing-deal"
    elif change == "code": r["evidence"]["deals"][0]["code"] = "HK.QAX250929P80000"
    elif change == "time": r["evidence"]["deals"][0]["create_time"] = "2025-09-08 11:07:09.124"
    elif change == "side": r["evidence"]["deals"][0]["trd_side"] = "SELL_SHORT"
    elif change == "quantity": r["evidence"]["deals"][0]["qty"] = "2"
    elif change == "extra": r["extra"] = True
    elif change == "zero": r["expected_total_fee"] = "0"
    elif change == "fraction": r["expected_total_fee"] = "6.41"
    elif change == "bool": r["expected_total_fee"] = True
    elif change == "duplicate": r["targets"][1] = deepcopy(r["targets"][0])
    else: r["evidence"]["observed_at_ms"] = AT - 1
    with pytest.raises((ValueError, TypeError)):
        api.prepare_confirmed_order_fee_allocation(repo, request=r)
    assert _events(repo.db_path) == before


def test_qa_repeated_apply_and_clock_change_do_not_duplicate_fees(tmp_path, monkeypatch):
    repo, request = _fixture(tmp_path)
    preview = api.prepare_confirmed_order_fee_allocation(repo, request=request)
    monkeypatch.setattr(allocation.time, "time", lambda: (AT + 9000000) / 1000)
    assert api.prepare_confirmed_order_fee_allocation(repo, request=request) == preview
    result = api.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "first-backup")
    assert result["durable_readback"]
    before_retry = _events(repo.db_path)
    audits = _business(repo.db_path, "broker_fee_enrichment_audit")
    retry = api.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "must-not-exist")
    assert retry["mode"] == "no_op" and retry["write_applied"] is False
    assert not (tmp_path / "must-not-exist").exists()
    assert _events(repo.db_path) == before_retry
    assert _business(repo.db_path, "broker_fee_enrichment_audit") == audits and len(audits) == 2
    for row in audits:
        assert row[6] == hashlib.sha256(before_retry[row[4]].encode()).hexdigest()


def test_qa_unrelated_projection_drift_blocks_before_backup(tmp_path):
    repo, request = _fixture(tmp_path)
    with connect_ledger_fixture(repo.db_path) as conn:
        conn.execute("UPDATE position_lots SET fields_json=json_set(fields_json,'$.premium',999) WHERE account='qb'")
    before = _events(repo.db_path)
    with pytest.raises(ValueError, match="projection"):
        api.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert _events(repo.db_path) == before
    assert not (tmp_path / "qa-backup").exists()


def test_qa_failure_after_first_write_rolls_back_both_and_retains_valid_backup(tmp_path, monkeypatch):
    repo, request = _fixture(tmp_path)
    before = _events(repo.db_path)
    insert = order_fee_migration._insert_audit
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        insert(*args, **kwargs)
        raise RuntimeError("qa-injected-after-first-row")
    monkeypatch.setattr(order_fee_migration, "_insert_audit", interrupted)
    _, result = _apply(repo, request, tmp_path)
    assert calls == 1 and result["mode"] == "not_applied" and result["write_applied"] is False
    assert _events(repo.db_path) == before
    backup = Path(result["backup"]["path"])
    assert _events(backup) == before
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_qa_committed_readback_failure_retries_without_reapplying(tmp_path, monkeypatch):
    repo, request = _fixture(tmp_path)
    preview = api.prepare_confirmed_order_fee_allocation(repo, request=request)
    original = allocation._readback
    def unavailable(*args, **kwargs):
        raise OSError("qa-readback-unavailable")
    monkeypatch.setattr(allocation, "_readback", unavailable)
    result = api.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "first-backup")
    assert result["mode"] == "committed_readback_unavailable" and result["write_applied"] is True
    monkeypatch.setattr(allocation, "_readback", original)
    retry = api.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "no-second-backup")
    assert retry["mode"] == "no_op" and len(_business(repo.db_path, "broker_fee_enrichment_audit")) == 2


def test_qa_real_cli_preserves_write_gate_and_hash_contract(tmp_path, capsys):
    runtime = tmp_path / "runtime"
    state = runtime / "output_shared" / "state"
    state.mkdir(parents=True)
    _repo, request = _fixture(state, db_name="option_positions.sqlite3")
    data_config = tmp_path / "portfolio.json"
    data_config.write_text("{}")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    args = ["--runtime-root", str(runtime), "--data-config", str(data_config), "fees-allocate", "--request", str(request_path)]
    assert cli(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True and preview["write_applied"] is False
    with pytest.raises(SystemExit, match="--confirm"):
        cli(args + ["--apply"])
    assert cli(args + ["--confirm", "--expected-input-hash", preview["input_hash"], "--backup-dir", str(tmp_path / "cli-backup")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["durable_readback"] and result["write_applied"]


def test_qa_each_ledger_target_must_consume_a_distinct_broker_deal(tmp_path):
    repo, request = _fixture(tmp_path, duplicate_source=True)
    before = _events(repo.db_path)
    with pytest.raises(ValueError):
        api.prepare_confirmed_order_fee_allocation(repo, request=request)
    assert _events(repo.db_path) == before


def test_qa_saved_fx_drift_invalidates_preview_before_backup(tmp_path):
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
    repo, request = _fixture(tmp_path)
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    def rate(identity, at):
        return {"base_currency": "HKD", "quote_currency": "CNY", "rate": "0.9", "rate_kind": "central_parity",
            "effective_at_ms": at, "observed_at_ms": AT + 3000, "source": "pbc_central_parity", "source_id": identity,
            "quality": {"backfill": True}, "raw": {}}
    evidence.import_envelope({"schema_version": "option_performance_evidence.v1", "valuation_marks": [], "fx_rates": [rate("qa-fx-first", AT)]}, apply=True, migrated_at_ms=AT + 4000)
    preview = api.prepare_confirmed_order_fee_allocation(repo, request=request)
    evidence.import_envelope({"schema_version": "option_performance_evidence.v1", "valuation_marks": [], "fx_rates": [rate("qa-fx-later", AT + 1000)]}, apply=True, migrated_at_ms=AT + 5000)
    before = _events(repo.db_path)
    with pytest.raises(ValueError, match="hash changed"):
        api.apply_confirmed_order_fee_allocation(repo, request=request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "must-not-exist")
    assert _events(repo.db_path) == before and not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("drift", ["request_reuse", "missing_audit"])
def test_qa_retry_requires_same_request_and_complete_durable_audit(tmp_path, drift):
    repo, request = _fixture(tmp_path)
    preview, _ = _apply(repo, request, tmp_path)
    retry_request = deepcopy(request)
    if drift == "request_reuse":
        retry_request["authorization_ref"] = "qa-different-approval"
    else:
        with connect_ledger_fixture(repo.db_path) as conn:
            conn.execute("DELETE FROM broker_fee_enrichment_audit WHERE event_id=?", (TARGETS[0],))
    before = _events(repo.db_path)
    with pytest.raises(ValueError, match="conflicts|drift"):
        api.apply_confirmed_order_fee_allocation(repo, request=retry_request, expected_input_hash=preview["input_hash"], backup_dir=tmp_path / "no-extra-backup")
    assert _events(repo.db_path) == before and not (tmp_path / "no-extra-backup").exists()
