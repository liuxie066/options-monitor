from dataclasses import replace
from pathlib import Path

import pytest

from domain.domain.ledger import TradeEvent
from src.application.ledger.api import enrich_order_fees, execution_identity_from_input
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake
from src.application.trades.auto_intake import _process_payload
from src.application.trades.order_fee_sync import (
    fee_target_from_trusted_payload,
    recover_order_fee_targets,
    sync_order_fees,
)


_TARGET = ("富途", "lx", "123", "same-order")
_NOW_MS = 1_788_752_000_000


def _payload(identity: str, namespace: str = "futu.order") -> dict:
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "123",
                               "environment": "REAL", "broker_account_id": "futu:REAL:123",
                               "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": "NVDA",
                           "currency": "USD", "option_type": "put", "strike": "100",
                           "expiration_ymd": "2026-09-18", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": identity,
        "external_order_namespace": namespace, "external_order_id": "same-order",
        "side": "sell", "position_effect": "open", "quantity": "1", "price": "2.50",
        "currency": "USD", "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _intake(tmp_path: Path, repo, payload, *, replay=False):
    result = _process_payload(
        payload, repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], host="127.0.0.1", port=11111,
        apply_changes=True, allow_external_lookup=False, source="file",
    )
    assert result["status"] == "applied" or (replay and result["status"] == "skipped")


class _Provider:
    def __init__(self):
        self.terminal_calls = []
        self.fee_calls = []

    def fetch_terminal_orders(self, **kwargs):
        self.terminal_calls.append(kwargs)
        return {"same-order": {"status": "terminal_with_fill", "dealt_qty": "1", "currency": "USD"}}, {}

    def fetch_order_fees(self, **kwargs):
        self.fee_calls.append(kwargs)
        return {"same-order": {"fee_amount": "1.23"}}, {}


def _actual_fee(quantity: str = "1", namespace: str = "futu.order") -> dict:
    return {"broker": "富途", "account": "lx", "futu_account_id": "123", "order_id": "same-order",
            "external_order_namespace": namespace, "fee_amount": "1.23", "currency": "USD",
            "event_kind": "option_trade", "dealt_quantity": quantity, "observed_at_ms": _NOW_MS}


def test_standard_execution_namespace_never_joins_futu_provider_order(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _intake(tmp_path, repo, _payload("foreign", "external-file.order"))
    _intake(tmp_path, repo, _payload("futu"))
    foreign = next(row for row in repo.list_trade_events() if row["event_id"].endswith(":foreign"))
    recovered = recover_order_fee_targets(repo, account="lx", allowed_futu_account_ids=["123"])
    assert recovered["targets"] == [_TARGET]
    assert any(issue["reason"] == "unsupported_order_namespace" for issue in recovered["issues"])

    provider = _Provider()
    result = sync_order_fees(repo, account="lx", provider=provider, apply=True,
                            observed_at_ms=_NOW_MS, target_identity=_TARGET)
    assert result["actual_observation_count"] == 1
    assert len(provider.terminal_calls) == len(provider.fee_calls) == 1
    assert provider.terminal_calls[0]["futu_account_id"] == "123"
    rows = repo.list_trade_events()
    assert next(row for row in rows if row["event_id"] == foreign["event_id"]) == foreign
    actual = next(row for row in rows if row["event_id"].endswith(":futu"))
    assert actual["fees"] == 1.23
    assert actual["raw_payload"]["fee_provenance"]["basis"] == "actual"


@pytest.mark.parametrize(("namespace", "account_overrides", "reason"), [
    (None, {}, "order_namespace_missing"),
    ("external-file.order", {}, "unsupported_order_namespace"),
    ("futu.order", {"broker_id": "ibkr"}, "unsupported_order_broker"),
    ("futu.order", {"environment": "SIMULATE"}, "unsupported_order_environment"),
    ("futu.order", {"external_account_id": "999"}, "order_physical_account_mismatch"),
    ("futu.order", {"external_account_id": None}, "order_account_identity_missing"),
])
def test_unverified_namespace_is_not_queried_or_publicly_enriched(tmp_path, namespace, account_overrides, reason):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _intake(tmp_path, repo, _payload("template"))
    original = TradeEvent.from_dict(repo.list_trade_events()[0])
    execution = _payload("unverified")
    execution["external_order_namespace"] = namespace
    execution["external_order_id"] = "unverified-order"
    execution["broker_account_ref"].update(account_overrides)
    raw = {**execution, "execution_input": execution,
           "execution_id": execution_identity_from_input(execution),
           "futu_account_id": "123", "order_id": "unverified-order"}
    repo.upsert_trade_event(replace(original, event_id="unverified", lot_id="unverified-lot", raw_payload=raw))
    before = next(row for row in repo.list_trade_events() if row["event_id"] == "unverified")
    target = ("富途", "lx", "123", "unverified-order")
    provider = _Provider()
    result = sync_order_fees(repo, account="lx", provider=provider, apply=True,
                            observed_at_ms=_NOW_MS, target_identity=target)
    assert result["reason_counts"][reason] == 1
    assert provider.terminal_calls == provider.fee_calls == []
    observation = {**_actual_fee(), "order_id": "unverified-order"}
    direct = enrich_order_fees(repo, account="lx", actual_fees=[observation], apply=True,
                              applied_at_ms=_NOW_MS, target_identity=target)
    assert not direct["status_counts"].get("committed")
    assert any(issue["reason"] == reason for issue in direct["unresolved"])
    assert next(row for row in repo.list_trade_events() if row["event_id"] == "unverified") == before
    legitimate = sync_order_fees(repo, account="lx", provider=provider, apply=True,
                                observed_at_ms=_NOW_MS, target_identity=_TARGET)
    assert legitimate["actual_observation_count"] == 1
    assert reason not in legitimate["reason_counts"]


def test_public_fee_migration_rechecks_namespace_before_allocation(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _intake(tmp_path, repo, _payload("foreign", "external-file.order"))
    _intake(tmp_path, repo, _payload("futu"))
    before = repo.list_trade_events()
    stale = enrich_order_fees(repo, account="lx", actual_fees=[_actual_fee("2")], apply=True,
                             applied_at_ms=_NOW_MS, target_identity=_TARGET)
    assert any(issue["reason"] == "order_quantity_changed_after_admission" for issue in stale["unresolved"])
    assert repo.list_trade_events() == before
    with pytest.raises(ValueError, match="unsupported_order_namespace"):
        enrich_order_fees(repo, account="lx", actual_fees=[_actual_fee(namespace="external-file.order")],
                          apply=True, applied_at_ms=_NOW_MS, target_identity=_TARGET)
    assert repo.list_trade_events() == before
    admitted = enrich_order_fees(repo, account="lx", actual_fees=[_actual_fee()], apply=True,
                                applied_at_ms=_NOW_MS, target_identity=_TARGET)
    assert admitted["status_counts"] == {"committed": 1}
    foreign = next(row for row in before if row["event_id"].endswith(":foreign"))
    assert next(row for row in repo.list_trade_events() if row["event_id"] == foreign["event_id"]) == foreign


@pytest.mark.parametrize("namespace", [None, "external-file.order", "futu.order"])
def test_trusted_payload_requires_standard_order_namespace(namespace):
    payload = _payload("source")
    payload["external_order_namespace"] = namespace
    payload["_trade_intake_source"] = {"schema_version": "trade_intake_source.v1",
                                       "account": "lx", "futu_account_id": "123"}
    assert fee_target_from_trusted_payload(payload) == (_TARGET if namespace == "futu.order" else None)
    legacy = {"order_id": "same-order", "_trade_intake_source": payload["_trade_intake_source"]}
    assert fee_target_from_trusted_payload(legacy) == _TARGET


@pytest.mark.parametrize("namespace", [None, "external-file.order"])
def test_assigned_stock_sale_namespace_blocks_query_and_public_allocation(tmp_path, namespace):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    execution = _payload("foreign-stock")
    execution["external_order_namespace"] = namespace
    execution["instrument_ref"] = {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"}
    execution.update(side="sell", position_effect="close", quantity="100", price="100")
    sale = {"stock_event_id": "foreign-stock", "event_type": "sale", "trade_time_ms": _NOW_MS - 1,
            "account": "lx", "broker": "富途", "symbol": "NVDA", "currency": "USD",
            "shares": 100, "price": 100, "fees": 0, "futu_account_id": "123", "order_id": "same-order",
            "external_order_namespace": namespace, "execution_input": execution,
            "execution_id": execution_identity_from_input(execution)}
    repo.upsert_assigned_stock_event(sale)
    before = repo.list_assigned_stock_events()
    recovery = recover_order_fee_targets(repo, account="lx")
    assert recovery["targets"] == []
    provider = _Provider()
    queried = sync_order_fees(repo, account="lx", provider=provider, apply=True,
                              observed_at_ms=_NOW_MS, target_identity=_TARGET)
    reason = "order_namespace_missing" if namespace is None else "unsupported_order_namespace"
    assert queried["reason_counts"][reason] == 1
    assert provider.terminal_calls == provider.fee_calls == []
    observation = {**_actual_fee("100"), "event_kind": "assigned_stock_sale"}
    direct = enrich_order_fees(repo, account="lx", actual_fees=[observation], apply=True,
                              applied_at_ms=_NOW_MS, target_identity=_TARGET)
    assert not direct["status_counts"].get("committed")
    assert any(issue["reason"] == reason for issue in direct["unresolved"])
    assert repo.list_assigned_stock_events() == before


@pytest.mark.parametrize("overrides", [
    {"broker_id": "ibkr"}, {"environment": "SIMULATE"},
    {"external_account_id": "999"}, {"external_account_id": None},
])
def test_trusted_payload_does_not_override_standard_account_reference(overrides):
    payload = _payload("source")
    payload["broker_account_ref"].update(overrides)
    payload["_trade_intake_source"] = {"schema_version": "trade_intake_source.v1",
                                       "account": "lx", "futu_account_id": "123"}
    assert fee_target_from_trusted_payload(payload) is None


@pytest.mark.parametrize("crash_before_state", [False, True])
@pytest.mark.parametrize("namespace", ["futu.order", "external-file.order"])
def test_late_order_association_recovers_fee_target_after_ledger_commit(tmp_path, monkeypatch, crash_before_state, namespace):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    unknown = _payload("late-order")
    unknown.pop("external_order_id")
    unknown.pop("external_order_namespace")
    if crash_before_state:
        with monkeypatch.context() as interrupted:
            interrupted.setattr(auto_intake, "update_trade_intake_state_entries", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline crash")))
            with pytest.raises(RuntimeError, match="offline crash"):
                _intake(tmp_path, repo, unknown)
    else:
        _intake(tmp_path, repo, unknown)
    before = repo.list_trade_events()[0]
    lots_before = repo.list_position_lots()
    assert recover_order_fee_targets(repo, account="lx")["targets"] == []

    reopened = SQLiteOptionPositionsRepository(repo.db_path)
    _intake(tmp_path, reopened, _payload("late-order", namespace), replay=True)
    after = reopened.list_trade_events()[0]
    assert {key: value for key, value in after.items() if key != "raw_payload"} == {
        key: value for key, value in before.items() if key != "raw_payload"
    }
    assert reopened.list_position_lots() == lots_before
    assert after["raw_payload"]["order_id"] == "same-order"
    assert after["raw_payload"]["external_order_namespace"] == namespace
    for key in ("cash_conversions", "fee_provenance"):
        assert after["raw_payload"].get(key) == before["raw_payload"].get(key)
    recovered = recover_order_fee_targets(reopened, account="lx", allowed_futu_account_ids=["123"])
    if namespace == "futu.order":
        assert recovered["targets"] == [_TARGET]
    else:
        assert recovered["targets"] == []
        assert any(issue["reason"] == "unsupported_order_namespace" for issue in recovered["issues"])
        _intake(tmp_path, reopened, _payload("different-futu"))

    provider = _Provider()
    synced = sync_order_fees(reopened, account="lx", provider=provider, apply=True,
                            observed_at_ms=_NOW_MS, target_identity=_TARGET)
    assert synced["actual_observation_count"] == 1
    assert len(provider.terminal_calls) == len(provider.fee_calls) == 1
    original = next(row for row in reopened.list_trade_events() if row["event_id"] == before["event_id"])
    if namespace != "futu.order":
        assert original == after
    assert recover_order_fee_targets(reopened, account="lx")["targets"] == []
