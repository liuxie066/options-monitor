from dataclasses import replace
from decimal import Decimal

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, fee_fact_for_event
from src.application.ledger import order_fee_migration as migration
from src.application.ledger.api import enrich_order_fees
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.order_fee_sync import sync_order_fees


TIME = 1_780_000_000_000
WINDOW = dict(account="account-a", start_ms=TIME, end_exclusive_ms=TIME + 10)
KEY = ContractKey.from_values(
    broker="富途", account="account-a", underlying_symbol="0700.HK",
    option_type="put", strike=400, expiration_ymd="2026-06-19",
)


def _event(event_id="fill-1", *, event_type="open", physical="physical-a", **raw):
    return TradeEvent(
        event_id=event_id, event_type=event_type, event_time_ms=TIME, contract_key=KEY,
        contracts=1, price=Decimal("2.5"), currency="HKD", multiplier=100,
        source="broker", lot_id=event_id if event_type == "open" else None,
        target_lot_id="seed" if event_type == "close" else None,
        raw_payload={"side": "sell" if event_type == "open" else "buy",
                     "futu_account_id": physical, "order_id": "order-1", **raw},
    )


def _repo(tmp_path):
    return SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")


def _audit_count(repo):
    with repo._connect() as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='broker_fee_enrichment_audit'"
        ).fetchone():
            return 0
        return conn.execute("SELECT count(*) FROM broker_fee_enrichment_audit").fetchone()[0]


def _fees(repo):
    return {row["event_id"]: fee_fact_for_event(TradeEvent.from_dict(row))
            for row in repo.list_trade_events() if row["event_type"] in {"open", "close"}}


@pytest.mark.parametrize("event_type", ["open", "close"])
@pytest.mark.parametrize("same_physical", [False, True])
def test_formula_deal_identity_and_split_sqlite(tmp_path, event_type, same_physical):
    repo = _repo(tmp_path)
    if event_type == "close":
        repo.upsert_trade_event(replace(_event("seed"), contracts=2, event_time_ms=TIME - 1))
    for index, physical in enumerate(("physical-a", "physical-a" if same_physical else "physical-b")):
        repo.upsert_trade_event(_event(
            f"fill-{index}", event_type=event_type, physical=physical,
            source_deal_id="same-deal", broker_deal_completion={"expected_contracts": 2 if same_physical else 1},
        ))
    before = repo.list_trade_events()
    preview = enrich_order_fees(repo, **WINDOW, applied_at_ms=TIME + 100)
    assert preview["status_counts"] == {"preview": 1 if same_physical else 2}
    assert repo.list_trade_events() == before
    assert _audit_count(repo) == 0
    receipt = enrich_order_fees(repo, **WINDOW, apply=True, applied_at_ms=TIME + 100)
    assert receipt["status_counts"] == {"committed": 1 if same_physical else 2}
    fees = _fees(repo)
    assert [fees[f"fill-{i}"].amount for i in range(2)] == [Decimal(12 if same_physical else 21)] * 2
    assert all(fees[f"fill-{i}"].basis.value == "estimated" for i in range(2))
    assert _audit_count(repo) == 2
    retry = enrich_order_fees(repo, **WINDOW, apply=True, applied_at_ms=TIME + 200)
    assert retry["unit_count"] == 0
    assert _audit_count(repo) == 2


@pytest.mark.parametrize("event_type", ["open", "close"])
@pytest.mark.parametrize("missing", ["physical", "deal"])
def test_formula_unproven_deal_identity_stays_unresolved(tmp_path, event_type, missing):
    repo = _repo(tmp_path)
    repo.upsert_trade_event(_event(
        event_type=event_type, physical=None if missing == "physical" else "physical-a",
        source_deal_id="same-deal" if missing == "physical" else None,
        broker_deal_completion={"expected_contracts": 1},
    ))
    before = repo.list_trade_events()
    receipt = enrich_order_fees(repo, **WINDOW, apply=True, applied_at_ms=TIME + 100)
    assert receipt["unit_count"] == 0
    assert receipt["reason_counts"] == {"source_deal_fee_identity_missing": 1}
    assert repo.list_trade_events() == before
    assert _audit_count(repo) == 0


def _observation(quantity=1):
    return dict(broker="富途", account="account-a", futu_account_id="physical-a",
                order_id="order-1", fee_amount="21", currency="HKD", event_kind="option_trade",
                dealt_quantity=str(quantity), observed_at_ms=TIME + 100)


def test_actual_fee_overrides_only_its_physical_account_estimate(tmp_path):
    repo = _repo(tmp_path)
    for index, physical in enumerate(("physical-a", "physical-b")):
        repo.upsert_trade_event(_event(
            f"fill-{index}", physical=physical, source_deal_id="same-deal",
            broker_deal_completion={"expected_contracts": 1},
        ))
    enrich_order_fees(repo, **WINDOW, apply=True, applied_at_ms=TIME + 100)
    observation = {**_observation(), "fee_amount": "22"}
    receipt = enrich_order_fees(repo, **WINDOW, actual_fees=[observation], apply=True, applied_at_ms=TIME + 200)
    assert receipt["status_counts"] == {"committed": 1}
    fees = _fees(repo)
    assert (fees["fill-0"].basis.value, fees["fill-0"].amount) == ("actual", Decimal(22))
    assert (fees["fill-1"].basis.value, fees["fill-1"].amount) == ("estimated", Decimal(21))
    assert _audit_count(repo) == 3


class Provider:
    def fetch_terminal_orders(self, **kwargs):
        return {"order-1": {"status": "terminal_with_fill", "dealt_qty": "1", "currency": "HKD"}}, {}

    def fetch_order_fees(self, **kwargs):
        return {"order-1": {"fee_amount": "21"}}, {}


@pytest.mark.parametrize("change", ["new_fill", "void"])
@pytest.mark.parametrize("targeted", [False, True])
def test_actual_order_rechecks_membership_before_transaction(tmp_path, monkeypatch, change, targeted):
    repo = _repo(tmp_path)
    original_event = _event()
    repo.upsert_trade_event(original_event)
    original_transaction = migration.with_sqlite_repo_transaction

    def interleave(candidate, fn, **kwargs):
        if change == "new_fill":
            repo.upsert_trade_event(_event("fill-2"))
        else:
            repo.upsert_trade_event(replace(
                original_event, event_id="void-1", event_type="void", target_event_id="fill-1", lot_id=None,
            ))
        return original_transaction(candidate, fn, **kwargs)

    monkeypatch.setattr(migration, "with_sqlite_repo_transaction", interleave)
    scope = dict(account="account-a", target_identity=("富途", "account-a", "physical-a", "order-1")) if targeted else WINDOW
    receipt = sync_order_fees(repo, **scope, provider=Provider(), apply=True, observed_at_ms=TIME + 100)
    assert receipt["migration"]["status_counts"] == {"rolled_back": 1}
    assert all(fee.basis.value == "missing" for fee in _fees(repo).values())
    assert _audit_count(repo) == 0
    monkeypatch.setattr(migration, "with_sqlite_repo_transaction", original_transaction)
    retried = enrich_order_fees(repo, **WINDOW, actual_fees=[_observation()], apply=True, applied_at_ms=TIME + 200)
    assert retried["unit_count"] == 0
    assert retried["reason_counts"] == {
        "order_quantity_changed_after_admission" if change == "new_fill" else "order_group_missing": 1,
    }
    assert _audit_count(repo) == 0
    if change == "new_fill":
        refreshed = enrich_order_fees(
            repo, **WINDOW, actual_fees=[_observation(2)], apply=True, applied_at_ms=TIME + 300,
        )
        assert refreshed["status_counts"] == {"committed": 1}
        assert [fee.amount for fee in _fees(repo).values()] == [Decimal("10.5")] * 2
        assert _audit_count(repo) == 2


def test_actual_order_unchanged_commits_and_retries_without_duplicate_audit(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert_trade_event(_event())
    args = dict(**WINDOW, actual_fees=[_observation()], apply=True, applied_at_ms=TIME + 100)
    assert enrich_order_fees(repo, **args)["status_counts"] == {"committed": 1}
    after = repo.list_trade_events()
    assert enrich_order_fees(repo, **args)["status_counts"] == {"no_op": 1}
    assert repo.list_trade_events() == after
    assert _fees(repo)["fill-1"].amount == Decimal(21)
    assert _audit_count(repo) == 1


@pytest.mark.parametrize("change", ["replacement", "quantity", "currency", "actual_fee"])
def test_actual_order_rechecks_already_actual_members(tmp_path, monkeypatch, change):
    repo = _repo(tmp_path)
    repo.upsert_trade_event(_event())
    actual = replace(_event("actual-fill"), fees=Decimal("10.5"), raw_payload={
        **_event().raw_payload,
        "fee_provenance": {"basis": "actual", "amount": "10.5", "source": "test"},
    })
    repo.upsert_trade_event(actual)
    original_transaction = migration.with_sqlite_repo_transaction

    def interleave(candidate, fn, **kwargs):
        if change == "replacement":
            repo.upsert_trade_event(replace(
                actual, event_id="void-actual", event_type="void", target_event_id=actual.event_id, lot_id=None,
            ))
            repo.upsert_trade_event(replace(actual, event_id="replacement", lot_id="replacement"))
        else:
            payload = actual.to_dict()
            if change == "quantity":
                payload["contracts"] = 2
            elif change == "currency":
                payload["currency"] = "USD"
            else:
                payload["fees"] = "11"
                payload["raw_payload"]["fee_provenance"]["amount"] = "11"
            # Simulate a concurrent correction of an already-actual member in this isolated DB.
            with repo._connect() as conn:
                conn.execute("UPDATE trade_events SET event_json=? WHERE event_id=?",
                             (migration._json(payload), actual.event_id))
                conn.commit()
        return original_transaction(candidate, fn, **kwargs)

    monkeypatch.setattr(migration, "with_sqlite_repo_transaction", interleave)
    receipt = enrich_order_fees(repo, **WINDOW, actual_fees=[_observation(2)], apply=True, applied_at_ms=TIME + 100)
    assert receipt["status_counts"] == {"rolled_back": 1}
    assert _fees(repo)["fill-1"].basis.value == "missing"
    assert _audit_count(repo) == 0


def test_actual_split_order_audit_failure_rolls_back_every_member(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    for event_id in ("fill-1", "fill-2"):
        repo.upsert_trade_event(_event(event_id))
    before = repo.list_trade_events()
    original_insert = migration._insert_audit

    def fail_second(conn, **kwargs):
        if kwargs["change"].event_id == "fill-2":
            raise RuntimeError("isolated audit failure")
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(migration, "_insert_audit", fail_second)
    args = dict(**WINDOW, actual_fees=[_observation(2)], apply=True, applied_at_ms=TIME + 100)
    assert enrich_order_fees(repo, **args)["status_counts"] == {"rolled_back": 1}
    assert repo.list_trade_events() == before
    assert _audit_count(repo) == 0
    monkeypatch.setattr(migration, "_insert_audit", original_insert)
    assert enrich_order_fees(repo, **args)["status_counts"] == {"committed": 1}
    assert [fee.amount for fee in _fees(repo).values()] == [Decimal("10.5")] * 2
    assert _audit_count(repo) == 2


def test_stock_sale_order_rechecks_added_member(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    sale = dict(stock_event_id="sale-1", event_type="sale", trade_time_ms=TIME,
                account="account-a", broker="富途", symbol="0700.HK", currency="HKD",
                shares=100, price=400, fees=0, futu_account_id="physical-a", order_id="order-1")
    repo.upsert_assigned_stock_event(sale)
    original_transaction = migration.with_sqlite_repo_transaction

    def interleave(candidate, fn, **kwargs):
        repo.upsert_assigned_stock_event({**sale, "stock_event_id": "sale-2"})
        return original_transaction(candidate, fn, **kwargs)

    monkeypatch.setattr(migration, "with_sqlite_repo_transaction", interleave)
    observation = {**_observation(100), "event_kind": "assigned_stock_sale"}
    receipt = enrich_order_fees(repo, **WINDOW, actual_fees=[observation], apply=True, applied_at_ms=TIME + 100)
    assert receipt["status_counts"] == {"rolled_back": 1}
    assert all(row["fees"] == 0 and "fee_provenance" not in row for row in repo.list_assigned_stock_events())
    assert _audit_count(repo) == 0
