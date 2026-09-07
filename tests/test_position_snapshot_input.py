from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from domain.domain.position_snapshot import normalize_position_snapshot_input, position_snapshot_scope_errors
from src.application.futu_portfolio_context import build_futu_portfolio_context, build_futu_position_snapshot
from src.application.quality.opend_position_adapter import OpenDOptionSnapshot
from src.application.quality.position_checks import build_position_dataset


NOW = datetime(2026, 9, 7, 2, tzinfo=timezone.utc)
ACCOUNT = {"broker_account_id": "futu:REAL:123", "broker_id": "futu", "external_account_id": "123", "environment": "REAL", "account_label": "lx"}


def _snapshot(*, stock: bool = False, rows: list | None = None) -> dict:
    return normalize_position_snapshot_input({
        "snapshot_id": "snapshot-1", "source_id": "test.positions",
        "broker_account_ref": ACCOUNT,
        "scope": {"markets": ["US"], "asset_types": ["stock" if stock else "option"], "filtered": False},
        "observed_at_utc": NOW.isoformat(), "completeness": "complete",
        "quality": {"status": "ready"}, "rows": [] if rows is None else rows,
    })


def _scope_errors(snapshot: dict, *, asset: str = "option") -> list[str]:
    return position_snapshot_scope_errors(snapshot, account_label="lx", environment="REAL", market="US", asset_type=asset, external_account_id="123", now_utc=NOW)


def _dataset(snapshot_input: dict, *, local_lots: list | None = None) -> dict:
    snapshot = OpenDOptionSnapshot(
        account="lx", market="us", environment="REAL", account_fingerprint="sha256:" + hashlib.sha256(b"123").hexdigest(),
        observed_at_utc=NOW.isoformat(), snapshot_id="snapshot-1", complete=True,
        refresh_cache=True, rows=[], trading_days=[], snapshot_input=snapshot_input,
    )
    return build_position_dataset(snapshot=snapshot, local_lots=local_lots or [], account="lx", market="us", observed_at_utc=NOW.isoformat(), now=NOW, control_state={})[0]


@pytest.mark.parametrize("change", [
    {"completeness": "partial"}, {"completeness": "unknown"},
    {"scope": {"markets": ["US"], "asset_types": ["option"], "filtered": True}},
    {"scope": {"markets": ["HK"], "asset_types": ["option"], "filtered": False}},
    {"scope": {"markets": ["US"], "asset_types": ["stock"], "filtered": False}},
    {"broker_account_ref": {**ACCOUNT, "environment": "SIMULATE"}},
    {"broker_account_ref": {**ACCOUNT, "account_label": "sy"}},
    {"source_as_of_utc": (NOW - timedelta(seconds=301)).isoformat()},
    {"observed_at_utc": (NOW - timedelta(seconds=301)).isoformat()},
    {"quality": {"status": "stale"}},
    {"broker_account_ref": {**ACCOUNT, "external_account_id": "456"}},
    {"observed_at_utc": (NOW - timedelta(seconds=10)).isoformat(), "source_as_of_utc": NOW.isoformat()},
])
def test_incomplete_or_wrong_scope_empty_positions_never_reconcile_to_zero(change: dict) -> None:
    snapshot = {**_snapshot(), **change}
    assert _scope_errors(snapshot)
    result = _dataset(snapshot)
    assert result["status"] == "unavailable"
    assert result["checks"][1]["reason_code"] == "POSITION_CONVERGENCE_SOURCE_UNAVAILABLE"


def test_only_complete_same_scope_empty_snapshot_proves_zero() -> None:
    assert _scope_errors(_snapshot()) == []
    assert _dataset(_snapshot())["status"] == "trusted"
    wrong_account = deepcopy(_snapshot())
    wrong_account["broker_account_ref"]["external_account_id"] = "456"
    assert "snapshot_physical_account_mismatch" in _scope_errors(wrong_account)


def test_position_contract_retains_exact_quantity_and_rejects_missing_or_nonfinite() -> None:
    row = {"instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "market": "US", "currency": "USD"}, "position_side": "long", "quantity": "100.000000000000000001"}
    assert _snapshot(stock=True, rows=[row])["rows"][0]["quantity"] == "100.000000000000000001"
    for bad in (None, "NaN", "Infinity", True, 1.1):
        normalized = _snapshot(stock=True, rows=[{**row, "quantity": bad}])
        assert normalized["errors"]
        assert normalized["rows"][0]["quantity"] is None
    option = {**row, "instrument_ref": {**row["instrument_ref"], "asset_type": "option", "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": "10"}, "quantity": "1.5"}
    assert "invalid:rows.0.quantity:integer_required" in _snapshot(rows=[option])["errors"]


def test_standard_rows_drive_option_comparison_without_futu_code_parsing() -> None:
    standard = _snapshot(rows=[{
        "instrument_ref": {"asset_type": "option", "symbol": "NVDA", "market": "US", "currency": "USD", "option_type": "put", "strike": "99.5", "expiration_ymd": "2026-09-18", "multiplier": "10"},
        "position_side": "short", "quantity": "1",
    }])
    result = _dataset(standard)
    assert result["status"] == "partial"
    assert result["checks"][1]["reason_code"] == "POSITION_DIVERGENCE_TRANSIENT"


@pytest.mark.parametrize(("deliverable", "error_kind"), [
    (None, None), ({}, None),
    ({"symbol": "NVDA", "quantity": "10", "cash": "9000"}, "unsupported"),
    ("NVDA:10", "invalid"), ([], "invalid"),
])
def test_standard_snapshot_deliverable_blocks_consumers_and_preserves_evidence(deliverable, error_kind) -> None:
    instrument = {
        "asset_type": "option", "symbol": "NVDA", "market": "US", "currency": "USD",
        "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": "100",
    }
    if deliverable is not None:
        instrument["deliverable"] = deliverable
    standard = {**_snapshot(), "rows": [{"instrument_ref": instrument, "position_side": "short", "quantity": "1"}]}
    original = deepcopy(standard)
    normalized = normalize_position_snapshot_input(standard)
    local = {"record_id": "ordinary-lot", "fields": {
        "account": "lx", "broker": "富途", "symbol": "NVDA", "option_type": "put",
        "side": "short", "contracts": 1, "contracts_open": 1, "strike": 100,
        "multiplier": 100, "expiration_ymd": "2026-09-18", "status": "open",
    }}
    result = _dataset(standard, local_lots=[local])
    assert standard == original
    assert normalized["rows"][0]["instrument_ref"].get("deliverable") == deliverable
    if error_kind:
        reason = f"{error_kind}:rows.0.instrument_ref.deliverable"
        assert reason in normalized["errors"]
        assert reason in result["checks"][0]["observed"]["snapshot_errors"]
        assert result["status"] == "unavailable"
        assert result["usable_for"] == []
        assert set(result["blocked_consumers"]) == {"option_position_report", "lifecycle", "close_advice"}
    else:
        assert normalized["errors"] == []
        assert result["status"] == "trusted"
        assert set(result["usable_for"]) == {"option_position_report", "lifecycle", "close_advice"}


@pytest.mark.parametrize("invalid", ["partial", "filtered", "stale_source", "wrong_account", "wrong_environment"])
def test_stock_capacity_uses_snapshot_scope_and_preserves_source_cost(monkeypatch, invalid: str) -> None:
    import src.application.futu_portfolio_context as module

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    raw_rows = [{"code": "US.NVDA", "qty": 100, "can_sell_qty": 100, "average_cost": 20, "cost_price": 1, "sec_type": "STOCK"}]
    snapshot = build_futu_position_snapshot(rows=raw_rows, broker_account_ref=ACCOUNT, markets=["US"], asset_types=["stock"], observed_at_utc=NOW.isoformat(), completeness="complete")
    if invalid == "partial":
        snapshot["completeness"] = "partial"
    elif invalid == "filtered":
        snapshot["scope"]["filtered"] = True
    elif invalid == "stale_source":
        snapshot["source_as_of_utc"] = (NOW - timedelta(days=1)).isoformat()
    elif invalid == "wrong_account":
        snapshot["broker_account_ref"]["external_account_id"] = "456"
    else:
        snapshot["broker_account_ref"]["environment"] = "SIMULATE"
    context = build_futu_portfolio_context(
        balance_rows=[], position_rows=[{**raw_rows[0], "qty": 1000}], account="lx",
        futu_account_id="123", broker_account_identifiers=["123"], trd_env="REAL",
        capacity_market="us", position_snapshot_input=snapshot,
    )
    stock = context["stocks_by_symbol"]["NVDA"]
    assert stock["shares"] == 100
    assert stock["avg_cost"] == 20
    assert stock["can_sell_qty"] is None
    assert stock["eligible_underlying_shares"] is None
    assert stock["capacity_authority_status"] == "unavailable"


def test_futu_snapshot_identifies_quantity_and_never_combines_physical_accounts() -> None:
    kwargs = dict(broker_account_ref=ACCOUNT, markets=["US"], asset_types=["stock"], observed_at_utc=NOW.isoformat(), completeness="complete")
    first = build_futu_position_snapshot(
        rows=[{"code": "US.NVDA", "qty": 100, "provider_time": NOW}],
        **kwargs,
    )
    second = build_futu_position_snapshot(rows=[{"code": "US.NVDA", "qty": 200}], **kwargs)
    assert first["snapshot_id"] != second["snapshot_id"]
    evidence = first["source_evidence"]
    assert first["evidence_refs"] == [evidence[0]["evidence_id"]]
    assert evidence[0]["schema_version"] == "source_evidence.v1"
    assert evidence[0]["source"] == "opend"
    assert evidence[0]["source_id"] == "futu-opend.positions"
    assert evidence[0]["account"] == "lx"
    assert evidence[0]["data_type"] == "position"
    assert evidence[0]["source_record_identity"] == first["snapshot_id"]
    assert evidence[0]["adapter_version"] == "om.futu-opend-position.v1"
    mixed = build_futu_position_snapshot(rows=[{"code": "US.NVDA", "qty": 100, "acc_id": "123"}, {"code": "US.NVDA", "qty": 200, "acc_id": "456"}], **kwargs)
    assert mixed["errors"] == ["position_row_account_mismatch:1"]
    assert mixed["rows"][0]["quantity"] == "100"
    assert len(mixed["source_payload"]["rows"]) == 2


@pytest.mark.parametrize(("quantity", "whole_shares"), [("100", 100), ("99.999999999999999999", 99)])
def test_complete_stock_snapshot_supplies_only_proven_whole_share_capacity(monkeypatch, quantity: str, whole_shares: int) -> None:
    import src.application.futu_portfolio_context as module

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    snapshot = build_futu_position_snapshot(
        rows=[{"code": "US.NVDA", "qty": quantity, "can_sell_qty": quantity, "average_cost": 20}],
        broker_account_ref=ACCOUNT, markets=["US"], asset_types=["stock"],
        observed_at_utc=NOW.isoformat(), completeness="complete",
    )
    context = build_futu_portfolio_context(
        balance_rows=[], position_rows=[], account="lx", futu_account_id="123",
        broker_account_identifiers=["123"], trd_env="REAL", capacity_market="us",
        position_snapshot_input=snapshot,
    )
    assert context["position_snapshot_input"]["rows"][0]["quantity"] == quantity
    stock = context["stocks_by_symbol"]["NVDA"]
    assert stock["capacity_authority_status"] == "available"
    assert stock["eligible_underlying_shares"] == whole_shares
    assert stock["avg_cost"] == 20
