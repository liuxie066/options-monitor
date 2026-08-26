from __future__ import annotations

from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, fee_fact_for_event
from src.application.ledger.order_fee_migration import enrich_order_fees
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades import order_fee_sync as order_fee_sync_module
from src.application.trades.order_fee_sync import sync_order_fees


EVENT_MS = 1_780_000_000_000


def _repo_with_bare_option_event(tmp_path: Path) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = TradeEvent(
        event_id="event-1",
        event_type="open",
        event_time_ms=EVENT_MS,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2.5,
        currency="USD",
        source="broker",
        multiplier=100,
        lot_id="lot-1",
        raw_payload={"futu_account_id": "123", "order_id": "order-1"},
    )
    repo.upsert_trade_event(event)
    return repo


class _Provider:
    def __init__(self, *, fee_amount: str = "1.230000", dealt_qty: str = "1.000000") -> None:
        self.fee_amount = fee_amount
        self.dealt_qty = dealt_qty
        self.terminal_calls = 0
        self.fee_calls = 0

    def fetch_terminal_orders(self, **kwargs):  # type: ignore[no-untyped-def]
        self.terminal_calls += 1
        order_id = kwargs["order_ids"][0]
        return {
            order_id: {
                "status": "terminal_with_fill",
                "dealt_qty": self.dealt_qty,
                "currency": "USD",
            }
        }, {}

    def fetch_order_fees(self, **kwargs):  # type: ignore[no-untyped-def]
        self.fee_calls += 1
        order_id = kwargs["order_ids"][0]
        return {
            order_id: {
                "fee_amount": self.fee_amount,
                "fee_details": {"commission": self.fee_amount},
            }
        }, {}


def _expiry_event(
    *, order_id: str | None = None, broker_execution: bool = False
) -> TradeEvent:
    if order_id is not None or broker_execution:
        raw_payload = {
            "futu_account_id": "123",
            "source_type": (
                "system_trade_event" if broker_execution else "broker_trade_event"
            ),
            "source_deal_id": "expiry-deal-1",
        }
        if order_id is not None:
            raw_payload["order_id"] = order_id
        source = "opend_push"
    else:
        raw_payload = {"source_type": "system_trade_event"}
        source = "option_lifecycle_decision"
    return TradeEvent(
        event_id="expiry-1",
        event_type="expire_close",
        event_time_ms=EVENT_MS,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=0,
        currency="USD",
        source=source,
        multiplier=100,
        target_lot_id="lot-1",
        raw_payload=raw_payload,
    )


@pytest.mark.parametrize("fee_amount", ["0.000000", "1.230000"])
def test_fee_sync_dry_run_is_read_only_and_apply_persists_actual_fee(
    tmp_path: Path,
    fee_amount: str,
) -> None:
    repo = _repo_with_bare_option_event(tmp_path)
    provider = _Provider(fee_amount=fee_amount)
    kwargs = {
        "account": "lx",
        "start_ms": EVENT_MS - 1,
        "end_exclusive_ms": EVENT_MS + 1,
        "provider": provider,
        "observed_at_ms": EVENT_MS + 10,
        "futu_account_id": "123",
        "max_orders": 400,
    }

    preview = sync_order_fees(repo, apply=False, **kwargs)

    assert preview["actual_observation_count"] == 1
    assert fee_fact_for_event(TradeEvent.from_dict(repo.list_trade_events()[0])).basis.value == "missing"
    with repo._connect() as conn:  # noqa: SLF001 - dry-run schema proof
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_fee_enrichment_audit'"
        ).fetchone() is None

    applied = sync_order_fees(repo, apply=True, **kwargs)

    event = TradeEvent.from_dict(repo.list_trade_events()[0])
    fee = fee_fact_for_event(event)
    assert applied["migration"]["status_counts"] == {"committed": 1}
    assert fee.basis.value == "actual"
    assert str(fee.amount) == fee_amount
    assert event.fees == float(fee_amount)
    assert event.raw_payload["fee_provenance"]["source"] == "opend.order_fee_query"
    with repo._connect() as conn:  # noqa: SLF001 - audit proof
        assert conn.execute("SELECT COUNT(*) FROM broker_fee_enrichment_audit").fetchone()[0] == 1


def test_order_backed_expiry_reaches_actual_fee_provider(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "expiry-order.sqlite3")
    repo.upsert_trade_event(_expiry_event(order_id="expiry-order-1"))
    provider = _Provider(fee_amount="0.50")

    receipt = sync_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        provider=provider,
        apply=False,
        observed_at_ms=EVENT_MS + 10,
        allowed_futu_account_ids=("123",),
    )

    assert receipt["actual_observation_count"] == 1
    assert provider.terminal_calls == provider.fee_calls == 1


def test_expiry_without_executed_order_is_frozen_as_actual_zero(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "expiry-writer.sqlite3")
    expiry = _expiry_event()
    persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="open-for-expiry",
            event_type="open",
            event_time_ms=EVENT_MS - 1,
            contract_key=expiry.contract_key,
            contracts=1,
            price=2.5,
            currency="USD",
            source="manual",
            multiplier=100,
            lot_id="lot-1",
        ),
    )

    persist_trade_event_object(repo, expiry)

    event = next(
        TradeEvent.from_dict(row)
        for row in repo.list_trade_events()
        if row["event_id"] == "expiry-1"
    )
    fee = fee_fact_for_event(event)
    assert fee.basis.value == "actual"
    assert fee.amount == 0
    provenance = event.raw_payload["fee_provenance"]
    assert provenance["source"] == "option_expiry_lifecycle"
    assert provenance["reason"] == "expired_without_executed_order"
    conversion = event.raw_payload["cash_conversions"]["option_fee_cash"]
    assert conversion["status"] == "observed"
    assert conversion["method"] == "zero_identity"


def test_legacy_expiry_without_executed_order_migrates_to_actual_zero(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "expiry-migration.sqlite3")
    expiry = _expiry_event()
    repo.upsert_trade_event(
        TradeEvent(
            event_id="legacy-open-for-expiry",
            event_type="open",
            event_time_ms=EVENT_MS - 2,
            contract_key=expiry.contract_key,
            contracts=1,
            price=2.5,
            currency="USD",
            source="legacy",
            multiplier=100,
            lot_id="lot-1",
            raw_payload={
                "fee_provenance": {
                    "basis": "actual",
                    "amount": "0",
                    "source": "test",
                }
            },
        )
    )
    repo.upsert_trade_event(expiry)

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        apply=True,
        applied_at_ms=EVENT_MS + 10,
    )

    assert receipt["status_counts"] == {"committed": 1}
    assert receipt["basis_counts"] == {"actual": 1, "estimated": 0, "missing": 0}
    assert receipt["reason_counts"] == {}
    event = next(
        TradeEvent.from_dict(row)
        for row in repo.list_trade_events()
        if row["event_id"] == "expiry-1"
    )
    assert fee_fact_for_event(event).basis.value == "actual"
    assert event.raw_payload["fee_provenance"]["reason"] == (
        "expired_without_executed_order"
    )
    assert event.raw_payload["cash_conversions"]["option_fee_cash"]["status"] == (
        "observed"
    )


def test_broker_expiry_without_order_identity_stays_missing(
    tmp_path: Path,
) -> None:
    expiry = _expiry_event(broker_execution=True)
    writer_repo = SQLiteOptionPositionsRepository(tmp_path / "broker-expiry-writer.sqlite3")
    writer_repo.upsert_trade_event(
        TradeEvent(
            event_id="broker-open-for-expiry",
            event_type="open",
            event_time_ms=EVENT_MS - 2,
            contract_key=expiry.contract_key,
            contracts=1,
            price=2.5,
            currency="USD",
            source="legacy",
            multiplier=100,
            lot_id="lot-1",
            raw_payload={
                "fee_provenance": {
                    "basis": "actual",
                    "amount": "0",
                    "source": "test",
                }
            },
        )
    )

    persist_trade_event_object(writer_repo, expiry)

    persisted = next(
        TradeEvent.from_dict(row)
        for row in writer_repo.list_trade_events()
        if row["event_id"] == "expiry-1"
    )
    fee = fee_fact_for_event(persisted)
    assert fee.basis.value == "missing"
    assert fee.reason == "broker_order_identity_missing"
    provider = _Provider()
    sync_receipt = sync_order_fees(
        writer_repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        provider=provider,
        apply=False,
        observed_at_ms=EVENT_MS + 10,
        allowed_futu_account_ids=("123",),
    )
    assert sync_receipt["reason_counts"] == {"order_identity_missing": 1}
    assert provider.terminal_calls == provider.fee_calls == 0

    migration_repo = SQLiteOptionPositionsRepository(
        tmp_path / "broker-expiry-migration.sqlite3"
    )
    migration_repo.upsert_trade_event(expiry)
    migration_receipt = enrich_order_fees(
        migration_repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        apply=True,
        applied_at_ms=EVENT_MS + 10,
    )
    assert migration_receipt["unit_count"] == 0
    migrated = TradeEvent.from_dict(migration_repo.list_trade_events()[0])
    assert fee_fact_for_event(migrated).basis.value == "missing"


def test_fee_sync_rejects_quantity_mismatch_before_fee_query(tmp_path: Path) -> None:
    repo = _repo_with_bare_option_event(tmp_path)
    provider = _Provider(dealt_qty="2.000000")

    receipt = sync_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        provider=provider,
        apply=False,
        observed_at_ms=EVENT_MS + 10,
        futu_account_id="123",
    )

    assert provider.terminal_calls == 1
    assert provider.fee_calls == 0
    assert receipt["actual_observation_count"] == 0
    assert receipt["reason_counts"]["option_order_quantity_mismatch"] == 1


def test_formula_migration_groups_split_close_by_writer_order_group() -> None:
    from src.application.ledger.order_fee_migration import _formula_option_groups

    key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-06-19",
    )
    events = tuple(
        TradeEvent(
            event_id=f"close-{index}",
            event_type="close",
            event_time_ms=EVENT_MS,
            contract_key=key,
            contracts=1,
            price=1,
            currency="USD",
            source="manual",
            multiplier=100,
            raw_payload={"fee_order_group_id": "close-both"},
        )
        for index in (1, 2)
    )

    assert _formula_option_groups(events) == {"deal:close-both": events}


def test_non_futu_event_stays_missing_and_never_reaches_provider(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "non-futu.sqlite3")
    event = TradeEvent(
        event_id="ibkr-event",
        event_type="open",
        event_time_ms=EVENT_MS,
        contract_key=ContractKey.from_values(
            broker="IBKR",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-06-19",
        ),
        contracts=1,
        price=2.5,
        currency="USD",
        source="broker",
        multiplier=100,
        lot_id="ibkr-lot",
        raw_payload={"futu_account_id": "123", "order_id": "order-1"},
    )
    persist_trade_event_object(repo, event)
    provider = _Provider()

    receipt = sync_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        provider=provider,
        apply=False,
        observed_at_ms=EVENT_MS + 10,
        allowed_futu_account_ids=("123",),
    )

    persisted = TradeEvent.from_dict(repo.list_trade_events()[0])
    assert fee_fact_for_event(persisted).basis.value == "missing"
    assert provider.terminal_calls == provider.fee_calls == 0
    assert receipt["reason_counts"] == {"unsupported_broker_fee_schedule": 1}


def test_fee_sync_only_queries_configured_provider_accounts(tmp_path: Path) -> None:
    repo = _repo_with_bare_option_event(tmp_path)
    provider = _Provider()

    receipt = sync_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        provider=provider,
        apply=False,
        observed_at_ms=EVENT_MS + 10,
        allowed_futu_account_ids=("999",),
    )

    assert receipt["selected_order_count"] == 0
    assert receipt["outside_scope_order_count"] == 1
    assert receipt["provider_call_count"] == 0
    assert provider.terminal_calls == provider.fee_calls == 0
    assert receipt["reason_counts"] == {"provider_account_outside_scope": 1}


def test_fee_sync_rate_limits_combined_provider_calls(tmp_path: Path, monkeypatch) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "rate-limit.sqlite3")
    candidates = [
        {
            "broker": "富途",
            "account": "lx",
            "futu_account_id": "123",
            "order_id": f"order-{index}",
            "identity_sha256": f"hash-{index}",
            "oldest_event_time_ms": EVENT_MS,
            "row_count": 1,
            "event_kind": "option_trade",
            "quantity": 1,
            "currency": "USD",
            "sort_key": f"{EVENT_MS:020d}:{index:05d}",
        }
        for index in range(2001)
    ]
    monkeypatch.setattr(
        order_fee_sync_module,
        "_select_candidates",
        lambda *_args, **_kwargs: (candidates, []),
    )

    class BatchProvider:
        def fetch_terminal_orders(self, **kwargs):  # type: ignore[no-untyped-def]
            return {
                order_id: {
                    "status": "terminal_with_fill",
                    "dealt_qty": "1",
                    "currency": "USD",
                }
                for order_id in kwargs["order_ids"]
            }, {}

        def fetch_order_fees(self, **kwargs):  # type: ignore[no-untyped-def]
            return {
                order_id: {"fee_amount": "1", "fee_details": {}}
                for order_id in kwargs["order_ids"]
            }, {}

    sleeps: list[float] = []
    receipt = sync_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        provider=BatchProvider(),
        apply=False,
        observed_at_ms=EVENT_MS + 10,
        allowed_futu_account_ids=("123",),
        sleep_fn=sleeps.append,
    )

    assert receipt["provider_call_count"] == 12
    assert receipt["provider_fee_call_count"] == 6
    assert sleeps == [30.0]


@pytest.mark.parametrize(
    ("event_kind", "dealt_quantity", "reason"),
    [
        ("assigned_stock_sale", "1", "order_event_kind_changed_after_admission"),
        ("option_trade", "2", "order_quantity_changed_after_admission"),
    ],
)
def test_migration_rechecks_admitted_event_kind_and_quantity(
    tmp_path: Path,
    event_kind: str,
    dealt_quantity: str,
    reason: str,
) -> None:
    repo = _repo_with_bare_option_event(tmp_path)
    observation = {
        "broker": "富途",
        "account": "lx",
        "futu_account_id": "123",
        "order_id": "order-1",
        "fee_amount": "1.23",
        "currency": "USD",
        "event_kind": event_kind,
        "dealt_quantity": dealt_quantity,
        "observed_at_ms": EVENT_MS + 10,
    }

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        actual_fees=(observation,),
        apply=False,
        applied_at_ms=EVENT_MS + 10,
    )

    assert receipt["reason_counts"][reason] == 1
    assert receipt["unit_count"] == 0
    event = TradeEvent.from_dict(repo.list_trade_events()[0])
    assert fee_fact_for_event(event).basis.value == "missing"


def test_migration_reports_idempotent_actual_noop_by_event_kind(tmp_path: Path) -> None:
    repo = _repo_with_bare_option_event(tmp_path)
    observation = {
        "broker": "富途",
        "account": "lx",
        "futu_account_id": "123",
        "order_id": "order-1",
        "fee_amount": "1.23",
        "currency": "USD",
        "event_kind": "option_trade",
        "dealt_quantity": "1",
        "observed_at_ms": EVENT_MS + 10,
    }
    enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        actual_fees=(observation,),
        apply=True,
        applied_at_ms=EVENT_MS + 10,
    )

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        actual_fees=(observation,),
        apply=False,
        applied_at_ms=EVENT_MS + 20,
    )

    assert receipt["status_counts"] == {"no_op": 1}
    assert receipt["status_counts_by_event_kind"] == {"option_trade": {"no_op": 1}}
    expected = {"actual": 1, "estimated": 0, "missing": 0}
    assert receipt["basis_counts"] == expected
    assert receipt["basis_counts_by_event_kind"] == {"option_trade": expected}
    assert receipt["fee_basis_event_counts_before"] == {
        "total": expected,
        "option_trade": expected,
    }
    assert receipt["fee_basis_event_counts_after"] == {
        "total": expected,
        "option_trade": expected,
    }
    assert receipt["newly_frozen_estimated_event_count"] == 0


def test_formula_preview_reports_effective_fee_basis_coverage(tmp_path: Path) -> None:
    repo = _repo_with_bare_option_event(tmp_path)

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        apply=False,
        applied_at_ms=EVENT_MS + 10,
    )

    assert receipt["fee_basis_event_counts_before"] == {
        "total": {"actual": 0, "estimated": 0, "missing": 1},
        "option_trade": {"actual": 0, "estimated": 0, "missing": 1},
    }
    assert receipt["fee_basis_event_counts_after"] == {
        "total": {"actual": 0, "estimated": 1, "missing": 0},
        "option_trade": {"actual": 0, "estimated": 1, "missing": 0},
    }
    assert receipt["basis_counts"] == {"actual": 0, "estimated": 1, "missing": 0}
    assert receipt["newly_frozen_estimated_event_count"] == 1


def test_migration_receipt_counts_all_existing_fee_bases_by_event_kind(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "mixed-bases.sqlite3")
    key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-06-19",
    )
    for event_id, basis, fees in (
        ("actual-option", "actual", 1.0),
        ("estimated-option", "estimated", 0.0),
    ):
        repo.upsert_trade_event(
            TradeEvent(
                event_id=event_id,
                event_type="open",
                event_time_ms=EVENT_MS,
                contract_key=key,
                contracts=1,
                price=2.5,
                currency="USD",
                source="test",
                multiplier=100,
                fees=fees,
                lot_id=f"lot-{event_id}",
                raw_payload={
                    "fee_provenance": {
                        "basis": basis,
                        "amount": "1.000000",
                        "source": "test",
                    }
                },
            )
        )
    repo.upsert_assigned_stock_event(
        {
            "stock_event_id": "missing-stock-sale",
            "event_type": "sale",
            "trade_time_ms": EVENT_MS,
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "currency": "USD",
            "shares": 100,
            "price": 105,
            "fees": 0,
            "fee_provenance": {
                "basis": "missing",
                "source": "test",
                "reason": "provider_fee_unavailable",
            },
        }
    )

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        apply=False,
        applied_at_ms=EVENT_MS + 10,
    )

    expected = {
        "total": {"actual": 1, "estimated": 1, "missing": 1},
        "option_trade": {"actual": 1, "estimated": 1, "missing": 0},
        "assigned_stock_sale": {"actual": 0, "estimated": 0, "missing": 1},
    }
    assert receipt["fee_basis_event_counts_before"] == expected
    assert receipt["fee_basis_event_counts_after"] == expected
    assert receipt["basis_counts"] == expected["total"]
    assert receipt["basis_counts_by_event_kind"] == {
        key: value for key, value in expected.items() if key != "total"
    }


def test_formula_migration_reports_unsupported_broker_instead_of_silent_skip(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "unsupported-formula.sqlite3")
    repo.upsert_trade_event(
        TradeEvent(
            event_id="ibkr-bare",
            event_type="open",
            event_time_ms=EVENT_MS,
            contract_key=ContractKey.from_values(
                broker="IBKR",
                account="lx",
                underlying_symbol="NVDA",
                option_type="put",
                position_side="short",
                strike=100,
                expiration_ymd="2026-06-19",
            ),
            contracts=1,
            price=2.5,
            currency="USD",
            source="broker",
            multiplier=100,
            lot_id="ibkr-bare-lot",
        )
    )

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        apply=False,
        applied_at_ms=EVENT_MS + 10,
    )

    assert receipt["unit_count"] == 0
    assert receipt["reason_counts"] == {"unsupported_broker_fee_schedule": 1}


def test_migration_rollback_receipt_redacts_exception_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.ledger import order_fee_migration as migration

    repo = _repo_with_bare_option_event(tmp_path)
    monkeypatch.setattr(
        migration,
        "_apply_unit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret-order-id=/private/path")
        ),
    )

    receipt = enrich_order_fees(
        repo,
        account="lx",
        start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1,
        apply=True,
        applied_at_ms=EVENT_MS + 10,
    )

    outcome = receipt["outcomes"][0]
    assert outcome["reason"] == "fee_enrichment_unit_rolled_back"
    assert outcome["error_type"] == "RuntimeError"
    assert "secret-order-id" not in str(receipt)


def test_listener_fee_status_surfaces_latest_redacted_provider_error() -> None:
    from src.application.trades.auto_intake import _update_status_from_backfill

    status = _update_status_from_backfill(
        {},
        {
            "diagnostics": {
                "fee_sync": [
                    {
                        "selected_order_count": 1,
                        "actual_observation_count": 0,
                        "issues": [
                            {
                                "reason": "provider_fee_query_failed",
                                "error_type": "TimeoutError",
                            }
                        ],
                    }
                ]
            }
        },
    )

    assert status["last_fee_sync"]["last_error"] == "provider_fee_query_failed"
    assert status["last_fee_sync"]["last_error_type"] == "TimeoutError"
