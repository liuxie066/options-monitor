from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.application.ledger import api
from src.application.ledger.bootstrap import apply_bootstrap_snapshot
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.migration import import_position_lot_snapshot
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.adapters import ledger_performance_inputs_from_rows
from tests.test_ledger_sqlite_workflows import _deal, _open_kwargs, _seed_fields, _trade_event


def _dump(repo):
    with repo._connect() as conn:
        return list(conn.iterdump())


def _open(repo, multiplier=500):
    result = api.record_manual_position_open(repo, **_open_kwargs(multiplier=multiplier))
    return result.result.lot_id


def _adjust_kwargs(lot_id, **overrides):
    return dict(lot_id=lot_id, contracts=None, strike=None, expiration_ymd=None,
                premium_per_share=3.0, opened_at_ms=None, **overrides)


def _corrupt_lot_multiplier(repo, lot_id):
    fields = repo.get_record_fields(lot_id)
    fields.pop("multiplier")
    with repo._connect() as conn:
        conn.execute("UPDATE position_lots SET fields_json=? WHERE lot_id=?",
                     (json.dumps(fields), lot_id))


@pytest.mark.parametrize("multiplier", [None, "", True, False, 0, -1, 100.5,
                                        "100.00000000000000001", "NaN", "Infinity"])
def test_manual_open_invalid_original_multiplier_has_no_writes(tmp_path, multiplier):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    before = _dump(repo)
    for command in (api.preview_manual_position_open, api.record_manual_position_open):
        with pytest.raises(ValueError, match="multiplier"):
            command(repo, **_open_kwargs(multiplier=multiplier))
        assert _dump(repo) == before


@pytest.mark.parametrize("multiplier", [500, 1000])
def test_manual_open_adjust_close_retains_actual_multiplier_and_retry(tmp_path, multiplier):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo, multiplier)
    preview = api.preview_manual_position_adjust(repo, **_adjust_kwargs(lot_id))
    assert preview.fields["multiplier"] == multiplier
    api.record_manual_position_adjust(repo, **_adjust_kwargs(lot_id))
    assert repo.get_record_fields(lot_id)["multiplier"] == multiplier
    close = dict(lot_id=lot_id, contracts_to_close=1, close_price=1.0,
                 close_reason="manual_buy_to_close", as_of_ms=4102444800000)
    api.preview_manual_position_close(repo, **close)
    result = api.record_manual_position_close(repo, **close)
    assert result.result.created is True
    events = repo.list_trade_events()
    assert len(events) == 3
    assert {event["multiplier"] for event in events} == {multiplier}
    fields = repo.get_record_fields(lot_id)
    assert fields["contracts_open"] == 0
    assert fields["contracts_closed"] == 1
    allocations = api.trade_event_economic_allocations(repo)
    assert len(allocations) == 1
    assert allocations[0].realized_pnl_gross == Decimal(2 * multiplier)
    before = _dump(repo)
    retry = api.record_manual_position_close(repo, **close)
    assert retry.result.created is False
    assert _dump(repo) == before


@pytest.mark.parametrize("multiplier", [None, True, "100.00000000000000001"])
def test_adjust_explicit_invalid_multiplier_rejected_before_writes(tmp_path, multiplier):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    before = _dump(repo)
    for command in (api.preview_manual_position_adjust, api.record_manual_position_adjust):
        with pytest.raises(ValueError, match="multiplier"):
            command(repo, **_adjust_kwargs(lot_id, multiplier=multiplier))
        assert _dump(repo) == before


def test_adjust_bad_target_cannot_be_repaired_by_replacement_or_stale_preview(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    preview = api.preview_manual_position_adjust(repo, **_adjust_kwargs(lot_id))
    _corrupt_lot_multiplier(repo, lot_id)
    before = _dump(repo)
    for command in (api.preview_manual_position_adjust, api.record_manual_position_adjust):
        with pytest.raises(ValueError, match="multiplier"):
            command(repo, **_adjust_kwargs(lot_id, multiplier=1000))
        assert _dump(repo) == before
    with pytest.raises(LedgerPreflightError) as exc:
        api.record_manual_position_adjust(repo, fields=preview.fields, **_adjust_kwargs(lot_id))
    assert exc.value.code == "target_fields_mismatch"
    assert "multiplier" in exc.value.details["mismatches"]
    assert _dump(repo) == before


@pytest.mark.parametrize("operation", ["void", "repair"])
def test_intervention_bad_source_target_rejected_without_writes(tmp_path, operation):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open(repo)
    event = repo.list_trade_events()[0]
    event["multiplier"] = None
    with repo._connect() as conn:
        conn.execute("UPDATE trade_events SET event_json=? WHERE event_id=?",
                     (json.dumps(event), event["event_id"]))
    before = _dump(repo)
    kwargs = dict(event_id=event["event_id"], reason="test invalid target")
    if operation == "repair":
        kwargs["overrides"] = {"multiplier": 1000}
    for prefix in ("preview", "record"):
        with pytest.raises(ValueError, match="multiplier"):
            getattr(api, f"{prefix}_trade_event_{operation}")(repo, **kwargs)
        assert _dump(repo) == before


def test_repair_valid_source_allows_explicit_actual_replacement(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open(repo)
    event = repo.list_trade_events()[0]
    kwargs = dict(event_id=event["event_id"], overrides={"multiplier": 1000}, reason="actual contract size")
    preview = api.preview_trade_event_repair(repo, **kwargs)
    result = api.record_trade_event_repair(repo, **kwargs, expected_input_hash=preview["expected_input_hash"])
    assert result["mode"] == "applied"
    assert repo.list_position_lots()[0]["fields"]["multiplier"] == 1000
    assert [row["multiplier"] for row in repo.list_trade_events() if row["event_type"] == "open"] == [500, 1000]


@pytest.mark.parametrize("multiplier", [None, True, "100.00000000000000001"])
def test_bootstrap_invalid_second_row_has_no_partial_economic_writes(tmp_path, multiplier):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    records = [dict(record_id="good", fields=_seed_fields(multiplier=500)),
               dict(record_id="bad", fields=_seed_fields(multiplier=multiplier))]
    before = _dump(repo)
    applied = apply_bootstrap_snapshot(
        repo, records=records, source_name="local_fixture", success_status="ok",
        success_message="imported {count}", failure_status="failed",
        failure_message="failed: {error}", failure_log_prefix="fixture bootstrap")
    assert applied is False
    assert repo.bootstrap_status == "failed"
    assert "multiplier" in repo.bootstrap_message
    assert _dump(repo) == before
    events, diagnostics = import_position_lot_snapshot(records)
    assert len(events) == 1
    assert events[0].multiplier == 500
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "snapshot_import_failed"
    assert diagnostics[0].details["record_id"] == "bad"
    assert "multiplier" in diagnostics[0].details["error"]


def test_manual_close_rechecks_target_after_preview(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    close = dict(lot_id=lot_id, contracts_to_close=1, close_price=1.0,
                 close_reason="manual_buy_to_close", as_of_ms=2000)
    preview = api.preview_manual_position_close(repo, **close)
    _corrupt_lot_multiplier(repo, lot_id)
    before = _dump(repo)
    with pytest.raises(ValueError):
        api.record_manual_position_close(repo, fields=preview.fields, **close)
    assert _dump(repo) == before


def test_expiry_maintenance_rejects_invalid_original_multiplier(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    lot_id = _open(repo)
    _corrupt_lot_multiplier(repo, lot_id)
    fields = repo.get_record_fields(lot_id)
    fields.update(record_id=lot_id, underlying_spot=200)
    before = _dump(repo)
    decisions = api.plan_expired_position_closes([fields], as_of_ms=4102444800000, grace_days=0)
    assert len(decisions) == 1
    assert decisions[0].should_close is False
    assert decisions[0].skip_reason == "invalid_multiplier"
    assert "multiplier" in decisions[0].reason
    assert decisions[0].patch is None
    applied = api.record_expired_position_closes(repo, [fields], as_of_ms=4102444800000,
                                                grace_days=0, max_close=1, projection_refresh=None)
    assert applied.applied == []
    assert applied.decisions[0].should_close is False
    assert applied.decisions[0].skip_reason == "invalid_multiplier"
    assert _dump(repo) == before


@pytest.mark.parametrize("multiplier", [True, "100.00000000000000001", 500, 1000])
def test_performance_input_adapter_preserves_original_multiplier_and_blocks_bad_economics(multiplier):
    opening = _trade_event(event_id="performance-open", lot_id="performance-lot", multiplier=multiplier,
                           contracts=2, price=3).to_dict()
    closing = _trade_event(event_id="performance-close", event_type="close", event_time_ms=2000,
                           target_lot_id="performance-lot", lot_id=None, multiplier=multiplier,
                           contracts=1, price=1, raw_payload={"side": "buy"}).to_dict()
    inputs = ledger_performance_inputs_from_rows({"trade_events": [opening, closing]})
    assert len(inputs.rows) == len(inputs.events) == 2
    for row, event in zip(inputs.rows, inputs.events):
        assert row["multiplier"] == event.multiplier == multiplier
        assert type(event.multiplier) is type(multiplier)
    if isinstance(multiplier, bool) or isinstance(multiplier, str):
        assert inputs.allocations == ()
        assert inputs.position_lots == ()
        invalid_ids = {item["event_id"] for item in inputs.diagnostics
                       if item["code"] == "event_multiplier_invalid" and item["severity"] == "error"}
        assert invalid_ids == {"performance-open", "performance-close"}
    else:
        assert inputs.diagnostics == ()
        assert len(inputs.allocations) == len(inputs.position_lots) == 1
        assert inputs.allocations[0].realized_pnl_gross == Decimal(2 * multiplier)
        assert inputs.position_lots[0].multiplier == multiplier
        assert inputs.position_lots[0].contracts_open == 1


@pytest.mark.parametrize("multiplier", [None, True, "100.00000000000000001", 500, 1000])
def test_broker_open_preview_validates_original_multiplier(multiplier):
    deal = _deal(multiplier=multiplier)
    if multiplier is None or isinstance(multiplier, (bool, str)):
        with pytest.raises(ValueError, match="multiplier"):
            api.preview_broker_trade_open(deal)
    else:
        preview = api.preview_broker_trade_open(deal)
        assert preview.command["multiplier"] == multiplier
        assert preview.fields["multiplier"] == multiplier
