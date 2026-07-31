from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_lifecycle import (
    expiration_observation_start_ms,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.close_reason_evidence import (
    build_lifecycle_timing_policy,
)
from src.application.trades.lifecycle_reconciliation import (
    discover_lifecycle_cases,
    lifecycle_case_read_model,
)
from src.application.trades.settlement_observation import (
    SettlementObservationDataError,
    collect_broker_settlement_observation,
)
from src.application.trades.close_reason_reconciliation import (
    reconcile_due_lifecycle_cases,
    reconcile_lifecycle_close_reason,
)


EXPIRATION_YMD = "2026-08-21"
OPTION_CODE = "US.NVDA260821P100000"


def _calendar_rows() -> list[dict[str, str]]:
    start = date(2026, 8, 20)
    end = date(2026, 9, 4)
    return [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "type": (
                "TRADING"
                if (start + timedelta(days=offset)).weekday() < 5
                else "REST"
            ),
        }
        for offset in range((end - start).days + 1)
    ]


def _repo_with_pending_case(
    tmp_path: Path,
) -> tuple[
    SQLiteOptionPositionsRepository,
    dict,
    dict,
    int,
]:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    contract = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd=EXPIRATION_YMD,
    )
    persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="open-1",
            event_type="open",
            event_time_ms=1_700_000_000_000,
            contract_key=contract,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=100,
            lot_id="lot-1",
            raw_payload={
                "fields": {
                    "broker": "futu",
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "currency": "USD",
                    "strike": 100,
                    "expiration_ymd": EXPIRATION_YMD,
                    "multiplier": 100,
                }
            },
        ),
    )
    anchor_time_ms = int(
        datetime(
            2026,
            8,
            21,
            16,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        ).timestamp()
        * 1000
    )
    observation_start_ms = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observation_start_ms is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start_ms,
    )["created_case_ids"][0]
    lifecycle_case = repo.get_trade_lifecycle_case(case_id)
    assert lifecycle_case is not None
    source_key = "futu:lx:1001:option-close-1"
    evidence = {
        "evidence_id": "anchor-1",
        "case_id": case_id,
        "source_type": "futu_broker_deal",
        "source_event_id": source_key,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "price": 0,
        "event_time_ms": anchor_time_ms,
        "received_at_ms": anchor_time_ms + 100,
        "order_id": "option-order-1",
        "target_contracts_by_lot": {"lot-1": 1},
        "raw": {"raw_payload": {"code": OPTION_CODE}},
    }
    assert repo.insert_trade_lifecycle_evidence_once(evidence)
    assert repo.bind_trade_lifecycle_case_futu_account_once(
        case_id=case_id,
        futu_account_id="1001",
    )
    lifecycle_case = repo.get_trade_lifecycle_case(case_id)
    assert lifecycle_case is not None
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=source_key,
            case_id=case_id,
            owner_evidence_id="anchor-1",
            source_role="option_anchor",
            economic_payload=evidence,
        )
    )
    policy = build_lifecycle_timing_policy(
        case_id=case_id,
        market="US",
        expiration_ymd=EXPIRATION_YMD,
        contract_metadata={
            "settlement_style": "physical",
            "underlying_security_type": "equity",
            "last_trade_cutoff_ms": anchor_time_ms,
            "last_trade_cutoff_source": (
                "instrument_policy_registry"
            ),
        },
        trading_days=_calendar_rows(),
        calendar_source="futu_request_trading_days",
        calendar_observed_at_ms=anchor_time_ms,
    )
    assert repo.insert_trade_lifecycle_timing_policy_once(policy)
    return repo, lifecycle_case, policy, anchor_time_ms


def _complete_receipt(rows: list[dict]) -> dict:
    return {
        "retcode": 0,
        "coverage_complete": True,
        "pagination_complete": True,
        "rows": rows,
    }


class _Gateway:
    def __init__(
        self,
        *,
        cash_failure_date: str | None = None,
        calendar_rows: list[dict[str, str]] | None = None,
        history_deals: list[dict] | None = None,
    ) -> None:
        self.cash_failure_date = cash_failure_date
        self.calendar_rows = (
            list(calendar_rows)
            if calendar_rows is not None
            else _calendar_rows()
        )
        self.history_deal_rows = (
            list(history_deals)
            if history_deals is not None
            else [
                {
                    "deal_id": "option-close-1",
                    "acc_id": "1001",
                    "code": OPTION_CODE,
                    "price": "0",
                    "qty": 1,
                }
            ]
        )
        self.history_deal_queries: list[dict] = []
        self.calendar_queries: list[dict] = []

    def get_history_deals(self, **kwargs: object) -> dict:
        self.history_deal_queries.append(dict(kwargs))
        return _complete_receipt(self.history_deal_rows)

    def get_history_orders(self, **kwargs: object) -> dict:
        return _complete_receipt(
            [
                {
                    "order_id": "option-order-1",
                    "is_broker_auto": True,
                }
            ]
        )

    def get_positions_with_receipt(
        self,
        **kwargs: object,
    ) -> dict:
        return _complete_receipt([])

    def get_account_cash_flows(
        self,
        *,
        clearing_date: str,
        **kwargs: object,
    ) -> dict:
        if clearing_date == self.cash_failure_date:
            return {
                "retcode": -1,
                "coverage_complete": False,
                "pagination_complete": False,
                "rows": [],
                "error": "cash query failed",
            }
        return _complete_receipt([])

    def get_trading_days_with_receipt(
        self,
        **kwargs: object,
    ) -> dict:
        self.calendar_queries.append(dict(kwargs))
        return _complete_receipt(self.calendar_rows)


def _collect(
    tmp_path: Path,
    gateway: _Gateway,
) -> dict:
    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    return collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=str(lifecycle_case["case_id"]),
            now_ms=now_ms,
        ),
        gateway=gateway,
        futu_account_id="1001",
        now_ms=now_ms,
    )


def test_complete_observation_revalidates_frozen_calendar_window(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    observation = _collect(tmp_path, gateway)

    assert observation["complete"] is True
    assert observation["incomplete_reason_codes"] == []
    assert "code" not in gateway.history_deal_queries[0]
    assert gateway.calendar_queries == [
        {
            "market": "US",
            "start": "2026-08-20",
            "end": "2026-09-04",
        }
    ]
    date_receipts = observation["source_receipts"][
        "account_cash_flows"
    ]["date_receipts"]
    assert len(date_receipts) == 5
    assert all(item["status"] == "complete" for item in date_receipts)


def test_cash_date_failure_preserves_each_date_receipt_and_blocks(
    tmp_path: Path,
) -> None:
    gateway = _Gateway(cash_failure_date="2026-08-23")
    observation = _collect(tmp_path, gateway)

    assert observation["complete"] is False
    assert (
        "account_cash_flows_incomplete"
        in observation["incomplete_reason_codes"]
    )
    failed = [
        item
        for item in observation["source_receipts"][
            "account_cash_flows"
        ]["date_receipts"]
        if item["query_input"]["clearing_date"] == "2026-08-23"
    ]
    assert len(failed) == 1
    assert failed[0]["status"] == "incomplete"
    assert failed[0]["error"] == "cash query failed"


def test_calendar_or_anchor_history_mismatch_blocks_observation(
    tmp_path: Path,
) -> None:
    altered_calendar = _calendar_rows()
    altered_calendar[0] = {
        **altered_calendar[0],
        "type": "REST",
    }
    gateway = _Gateway(
        calendar_rows=altered_calendar,
        history_deals=[],
    )
    observation = _collect(tmp_path, gateway)

    assert observation["complete"] is False
    assert {
        "calendar_hash_mismatch",
        "option_anchor_history_deal_missing",
    }.issubset(observation["incomplete_reason_codes"])


def test_poll_stock_settlement_uses_canonical_lifecycle_writer(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    stock_time_ms = int(policy["settlement_deadline_ms"]) - 1
    gateway = _Gateway(
        history_deals=[
            {
                "deal_id": "option-close-1",
                "acc_id": "1001",
                "code": OPTION_CODE,
                "price": "0",
                "qty": 1,
            },
            {
                "deal_id": "stock-settlement-1",
                "acc_id": "1001",
                "code": "US.NVDA",
                "price": "100",
                "qty": 100,
                "trd_side": "BUY",
                "trade_time_ms": stock_time_ms,
                "order_id": "stock-order-1",
            },
        ]
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=str(lifecycle_case["case_id"]),
            now_ms=now_ms,
        ),
        gateway=gateway,
        futu_account_id="1001",
        now_ms=now_ms,
    )

    assert observation["stock_settlement_present"] is True
    assert len(observation["stock_settlement_candidates"]) == 1
    result = reconcile_lifecycle_close_reason(
        repo,
        case_id=str(lifecycle_case["case_id"]),
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
    )

    assert result["poll_settlement_results"][0]["status"] == "applied"
    assert (
        result["lifecycle_read_model"]["close_reason"]
        == "assignment"
    )


def test_duplicate_polled_stock_settlement_is_applied_once(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    case_id = str(lifecycle_case["case_id"])
    stock_time_ms = int(policy["settlement_deadline_ms"]) - 1
    stock_row = {
        "deal_id": "stock-settlement-duplicate",
        "acc_id": "1001",
        "code": "US.NVDA",
        "price": "100",
        "qty": 100,
        "trd_side": "BUY",
        "trade_time_ms": stock_time_ms,
        "order_id": "stock-order-duplicate",
    }
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_Gateway(
            history_deals=[
                {
                    "deal_id": "option-close-1",
                    "acc_id": "1001",
                    "code": OPTION_CODE,
                    "price": "0",
                    "qty": 1,
                },
                stock_row,
                dict(stock_row),
            ]
        ),
        futu_account_id="1001",
        now_ms=now_ms,
    )

    assert len(observation["stock_settlement_candidates"]) == 2
    result = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
    )

    assert len(result["poll_settlement_results"]) == 1
    assert result["poll_settlement_results"][0]["status"] == "applied"
    assert len(repo.list_trade_lifecycle_allocations(case_id=case_id)) == 1
    assert len(repo.list_trade_events()) == 2


def test_multiple_polled_stock_settlements_require_review_before_writes(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    case_id = str(lifecycle_case["case_id"])
    stock_time_ms = int(policy["settlement_deadline_ms"]) - 1
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_Gateway(
            history_deals=[
                {
                    "deal_id": "option-close-1",
                    "acc_id": "1001",
                    "code": OPTION_CODE,
                    "price": "0",
                    "qty": 1,
                },
                {
                    "deal_id": "stock-settlement-a",
                    "acc_id": "1001",
                    "code": "US.NVDA",
                    "price": "100",
                    "qty": 100,
                    "trd_side": "BUY",
                    "trade_time_ms": stock_time_ms,
                },
                {
                    "deal_id": "stock-settlement-b",
                    "acc_id": "1001",
                    "code": "US.NVDA",
                    "price": "100",
                    "qty": 100,
                    "trd_side": "BUY",
                    "trade_time_ms": stock_time_ms,
                },
            ]
        ),
        futu_account_id="1001",
        now_ms=now_ms,
    )

    assert len(observation["stock_settlement_candidates"]) == 2
    result = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
    )

    assert result["decision"]["status"] == "needs_review"
    assert result["decision"]["reason_codes"] == [
        "stock_settlement_multiple_candidates_unresolved"
    ]
    assert result["write_status"] == "not_attempted"
    assert result["poll_settlement_results"] == []
    assert repo.list_trade_lifecycle_allocations(case_id=case_id) == []
    assert len(repo.list_trade_events()) == 1
    assert repo.list_trade_lifecycle_notifications() == []


def test_polled_settlement_generation_drift_writes_no_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle as lifecycle_module

    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    case_id = str(lifecycle_case["case_id"])
    stock_time_ms = int(policy["settlement_deadline_ms"]) - 1
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_Gateway(
            history_deals=[
                {
                    "deal_id": "option-close-1",
                    "acc_id": "1001",
                    "code": OPTION_CODE,
                    "price": "0",
                    "qty": 1,
                },
                {
                    "deal_id": "stock-settlement-stale",
                    "acc_id": "1001",
                    "code": "US.NVDA",
                    "price": "100",
                    "qty": 100,
                    "trd_side": "BUY",
                    "trade_time_ms": stock_time_ms,
                },
            ]
        ),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    candidate = observation["stock_settlement_candidates"][0]
    original_find = lifecycle_module._find_matching_option_cases
    changed = False

    def _change_generation_then_find(*args, **kwargs):
        nonlocal changed
        if not changed:
            current = repo.get_trade_lifecycle_case(case_id)
            assert current is not None
            assert repo.update_trade_lifecycle_case_derived_status(
                case_id=case_id,
                status=str(current["status"]),
                derived_summary={"concurrent_marker": "changed"},
            )
            changed = True
        return original_find(*args, **kwargs)

    monkeypatch.setattr(
        lifecycle_module,
        "_find_matching_option_cases",
        _change_generation_then_find,
    )

    with pytest.raises(
        ValueError,
        match="lifecycle generation compare-and-set failed",
    ):
        reconcile_lifecycle_close_reason(
            repo,
            case_id=case_id,
            now_ms=now_ms,
            observation=observation,
            apply_changes=True,
        )

    assert repo.get_trade_lifecycle_evidence(
        str(candidate["evidence_id"])
    ) is None
    assert repo.list_trade_lifecycle_allocations(case_id=case_id) == []
    assert len(repo.list_trade_events()) == 1
    assert repo.list_trade_lifecycle_notifications() == []


def test_due_reconciliation_skips_superseded_legacy_empty_case() -> None:
    class _LegacyOnlyRepo:
        def list_trade_lifecycle_cases(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return [
                {
                    "case_id": "legacy-case",
                    "account": "lx",
                    "status": "superseded",
                    "target_contracts_by_lot": {},
                }
            ]

    result = reconcile_due_lifecycle_cases(
        _LegacyOnlyRepo(),
        account="lx",
        now_ms=1_800_000_000_000,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("superseded case must not be observed")
        ),
    )

    assert result["case_count"] == 0
    assert result["results"] == []


def test_due_reconciliation_reports_active_v2_empty_target() -> None:
    class _InvalidV2Repo:
        def list_trade_lifecycle_cases(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return [
                {
                    "schema_version": "lifecycle_case.v2",
                    "case_id": "v2-empty",
                    "account": "lx",
                    "status": "waiting_settlement_evidence",
                    "target_contracts_by_lot": {},
                }
            ]

    result = reconcile_due_lifecycle_cases(
        _InvalidV2Repo(),
        account="lx",
        now_ms=1_800_000_000_000,
    )

    assert result["case_count"] == 1
    assert result["results"] == [
        {
            "case_id": "v2-empty",
            "status": "needs_review",
            "reason_codes": ["lifecycle_target_manifest_empty"],
            "failure_class": "case_data",
            "failure_stage": "case_validation",
            "failure_field": "target_contracts_by_lot",
            "write_status": "not_attempted",
            "apply_changes": False,
        }
    ]


def test_due_reconciliation_isolates_observation_collector_error(
    monkeypatch,
) -> None:
    import src.application.trades.close_reason_reconciliation as reconciliation

    class _DueV2Repo:
        def list_trade_lifecycle_cases(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return [
                {
                    "schema_version": "lifecycle_case.v2",
                    "case_id": "v2-due",
                    "account": "lx",
                    "status": "waiting_settlement_evidence",
                    "target_contracts_by_lot": {"lot-1": 1},
                }
            ]

    monkeypatch.setattr(
        reconciliation,
        "lifecycle_case_read_model",
        lambda *_args, **_kwargs: {
            "pairing_until_ms": 100,
            "pending_until_ms": 200,
            "reason_state": "cause_pending",
        },
    )

    result = reconcile_due_lifecycle_cases(
        _DueV2Repo(),
        account="lx",
        now_ms=201,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            SettlementObservationDataError("malformed provider input")
        ),
    )

    assert result["results"] == [
        {
            "case_id": "v2-due",
            "status": "needs_review",
            "reason_codes": [
                "settlement_observation_data_invalid"
            ],
            "failure_class": "case_data",
            "failure_stage": "provider_observation_input",
            "failure_field": None,
            "write_status": "not_attempted",
            "apply_changes": False,
            "error": (
                "SettlementObservationDataError: "
                "malformed provider input"
            ),
        }
    ]


def test_due_reconciliation_propagates_unclassified_provider_error(
    monkeypatch,
) -> None:
    import src.application.trades.close_reason_reconciliation as reconciliation

    class _DueV2Repo:
        def list_trade_lifecycle_cases(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return [
                {
                    "schema_version": "lifecycle_case.v2",
                    "case_id": "v2-due",
                    "account": "lx",
                    "status": "waiting_settlement_evidence",
                    "target_contracts_by_lot": {"lot-1": 1},
                }
            ]

    monkeypatch.setattr(
        reconciliation,
        "lifecycle_case_read_model",
        lambda *_args, **_kwargs: {
            "pairing_until_ms": 100,
            "pending_until_ms": 200,
            "reason_state": "cause_pending",
        },
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        reconcile_due_lifecycle_cases(
            _DueV2Repo(),
            account="lx",
            now_ms=201,
            observation_collector=lambda *_args: (
                _ for _ in ()
            ).throw(RuntimeError("provider unavailable")),
        )


def test_due_reconciliation_continues_after_malformed_case(
    monkeypatch,
) -> None:
    import src.application.trades.close_reason_reconciliation as reconciliation

    class _MixedV2Repo:
        def list_trade_lifecycle_cases(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return [
                {
                    "schema_version": "lifecycle_case.v2",
                    "case_id": "v2-empty",
                    "account": "lx",
                    "status": "waiting_settlement_evidence",
                    "target_contracts_by_lot": {},
                },
                {
                    "schema_version": "lifecycle_case.v2",
                    "case_id": "v2-valid",
                    "account": "lx",
                    "status": "waiting_settlement_evidence",
                    "target_contracts_by_lot": {"lot-1": 1},
                },
            ]

    monkeypatch.setattr(
        reconciliation,
        "lifecycle_case_read_model",
        lambda *_args, **_kwargs: {
            "pairing_until_ms": 100,
            "pending_until_ms": 200,
            "reason_state": "cause_pending",
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "reconcile_lifecycle_close_reason",
        lambda *_args, **_kwargs: {
            "case_id": "v2-valid",
            "status": "dry_run",
        },
    )

    result = reconcile_due_lifecycle_cases(
        _MixedV2Repo(),
        account="lx",
        now_ms=201,
        observation_collector=lambda *_args: {},
    )

    assert result["schema_version"] == "due_lifecycle_reconciliation.v2"
    assert result["case_count"] == 2
    assert result["results"][0]["case_id"] == "v2-empty"
    assert result["results"][0]["failure_class"] == "case_data"
    assert result["results"][1] == {
        "case_id": "v2-valid",
        "status": "dry_run",
    }


def test_settlement_observation_uses_validated_legacy_bridge_anchor(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(tmp_path)
    case_id = str(lifecycle_case["case_id"])
    direct = repo.get_trade_lifecycle_evidence("anchor-1")
    assert direct is not None
    with repo._connect() as conn:  # noqa: SLF001 - migration fixture
        conn.execute(
            "DELETE FROM trade_lifecycle_source_consumptions WHERE owner_evidence_id = ?",
            ("anchor-1",),
        )
        conn.execute(
            "DELETE FROM trade_lifecycle_evidence WHERE evidence_id = ?",
            ("anchor-1",),
        )
        conn.commit()

    legacy_case_id = "legacy-anchor-case"
    assert repo.upsert_trade_lifecycle_case(
        {
            **lifecycle_case,
            "schema_version": "lifecycle_case.v1",
            "case_id": legacy_case_id,
            "case_key": legacy_case_id,
            "status": "superseded",
            "superseded_by_case_id": case_id,
        }
    )
    legacy_anchor = {
        **direct,
        "case_id": legacy_case_id,
    }
    assert repo.insert_trade_lifecycle_evidence_once(legacy_anchor)
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=str(direct["source_event_id"]),
            case_id=legacy_case_id,
            owner_evidence_id="anchor-1",
            source_role="option_anchor",
            economic_payload=legacy_anchor,
        )
    )
    assert repo.insert_trade_lifecycle_evidence_once(
        {
            "schema_version": "migration_bridge_evidence.v1",
            "evidence_id": "bridge-anchor-1",
            "case_id": case_id,
            "source_type": "lifecycle_migration",
            "source_event_id": None,
            "evidence_type": "migration_bridge",
            "account": "lx",
            "symbol": "NVDA",
            "referenced_legacy_case_id": legacy_case_id,
            "referenced_legacy_evidence_id": "anchor-1",
            "allocating": False,
        }
    )

    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_Gateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )

    assert observation["complete"] is True
    assert observation["anchor_option_deal_key"] == ("futu:lx:1001:option-close-1")
    assert repo.list_trade_lifecycle_source_consumptions(case_id=case_id) == []
    assert len(repo.list_trade_lifecycle_source_consumptions(case_id=legacy_case_id)) == 1
