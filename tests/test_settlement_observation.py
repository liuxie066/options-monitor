from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_lifecycle import (
    expiration_observation_start_ms,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.ledger.current_decision_projection import (
    current_decision_projection_row,
    preview_current_decision_projection_oracle,
    read_current_decision_projection,
    write_lifecycle_case_decision_fact,
)
from src.application.ledger.api import (
    advance_lifecycle_case_state,
    LegacySettlementSemanticUnavailable,
    SettlementAdmissionStateIncoherent,
    SettlementSemanticUnavailable,
    attach_settlement_semantics,
    build_lifecycle_attempt_audit_envelope,
    lifecycle_case_coherent_facts,
    record_lifecycle_allocation,
    record_lifecycle_evidence_issue,
    settlement_evidence_id,
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
    lifecycle_case_read_models_for_account,
)
from src.application.trades.lifecycle import (
    reconcile_polled_stock_settlement_evidence,
)
from src.application.trades.settlement_attempts import (
    SETTLEMENT_OBSERVATION_CONTEXT_KEY,
    case_scope_fingerprint,
)
from src.application.trades.settlement_observation import (
    SettlementObservationDataError,
    build_settlement_observation_collector,
    collect_broker_settlement_observation,
)
from src.application.trades.lifecycle_runtime import (
    reconcile_due_lifecycle_cases_for_source,
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


def _bootstrap_current_decision_shadow(
    repo: SQLiteOptionPositionsRepository,
    *,
    now_ms: int,
) -> None:
    assigned_stock_report = {
        "_all_assigned_stock_lots": [],
        "covered_call_allocations": [],
        "assigned_stock_review_rows": [],
    }
    projection = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=now_ms,
        assigned_stock_report=assigned_stock_report,
    )
    with repo._connect() as conn:  # noqa: SLF001 - explicit shadow bootstrap
        for fact in projection["lifecycle"]["operational_cases"]:
            write_lifecycle_case_decision_fact(repo, fact=fact, conn=conn)
    projection = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=now_ms,
        assigned_stock_report=assigned_stock_report,
    )
    repo.upsert_current_decision_projection(
        current_decision_projection_row(projection)
    )


def _assert_current_lifecycle_matches_oracle(
    repo: SQLiteOptionPositionsRepository,
    *,
    now_ms: int,
) -> None:
    trusted = read_current_decision_projection(
        repo,
        account="lx",
        now_ms=now_ms,
    )
    oracle = preview_current_decision_projection_oracle(
        repo,
        account="lx",
        now_ms=now_ms,
        assigned_stock_report={
            "_all_assigned_stock_lots": [],
            "covered_call_allocations": [],
            "assigned_stock_review_rows": [],
        },
    )
    assert trusted["status"] == "trusted"
    assert trusted["payload"]["lifecycle"] == oracle["lifecycle"]


def _add_pending_case(
    repo: SQLiteOptionPositionsRepository,
    *,
    symbol: str,
    strike: int,
    suffix: str,
    option_code: str,
    anchor_time_ms: int,
) -> tuple[dict, dict]:
    lot_id = f"lot-{suffix}"
    contract = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol=symbol,
        option_type="put",
        position_side="short",
        strike=strike,
        expiration_ymd=EXPIRATION_YMD,
    )
    persist_trade_event_object(
        repo,
        TradeEvent(
            event_id=f"open-{suffix}",
            event_type="open",
            event_time_ms=1_700_000_000_000 + int(suffix),
            contract_key=contract,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=100,
            lot_id=lot_id,
            raw_payload={
                "fields": {
                    "broker": "futu",
                    "account": "lx",
                    "symbol": symbol,
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "currency": "USD",
                    "strike": strike,
                    "expiration_ymd": EXPIRATION_YMD,
                    "multiplier": 100,
                }
            },
        ),
    )
    observation_start_ms = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observation_start_ms is not None
    created = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start_ms,
    )["created_case_ids"]
    assert len(created) == 1
    case_id = str(created[0])
    source_key = f"futu:lx:1001:option-close-{suffix}"
    evidence = {
        "evidence_id": f"anchor-{suffix}",
        "case_id": case_id,
        "source_type": "futu_broker_deal",
        "source_event_id": source_key,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": symbol,
        "option_type": "put",
        "position_side": "short",
        "strike": strike,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "price": 0,
        "event_time_ms": anchor_time_ms,
        "received_at_ms": anchor_time_ms + 100,
        "order_id": f"option-order-{suffix}",
        "target_contracts_by_lot": {lot_id: 1},
        "raw": {"raw_payload": {"code": option_code}},
    }
    assert repo.insert_trade_lifecycle_evidence_once(evidence)
    assert repo.bind_trade_lifecycle_case_futu_account_once(
        case_id=case_id,
        futu_account_id="1001",
    )
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=source_key,
            case_id=case_id,
            owner_evidence_id=f"anchor-{suffix}",
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
    lifecycle_case = repo.get_trade_lifecycle_case(case_id)
    assert lifecycle_case is not None
    return lifecycle_case, policy


def _complete_receipt(rows: list[dict]) -> dict:
    return {
        "retcode": 0,
        "coverage_complete": True,
        "pagination_complete": True,
        "rows": rows,
    }


def test_discovery_replay_does_not_override_canonical_timing_policy(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    fallback_deadline_ms = int(lifecycle_case["pending_until_ms"])
    canonical_deadline_ms = int(policy["settlement_deadline_ms"])
    assert fallback_deadline_ms < canonical_deadline_ms

    before = repo.get_trade_lifecycle_case(case_id)
    assert before is not None
    canonical_before = lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=fallback_deadline_ms,
    )
    assert canonical_before["pending_until_ms"] == canonical_deadline_ms
    assert canonical_before["reason_state"] == "cause_pending"

    replay = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=fallback_deadline_ms,
    )

    assert replay["created_case_ids"] == []
    assert replay["refreshed_case_ids"] == []
    assert replay["would_refresh_case_ids"] == []
    assert repo.get_trade_lifecycle_case(case_id) == before
    canonical_after = lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=fallback_deadline_ms,
    )
    assert canonical_after == canonical_before


def test_due_reconciliation_routes_anchor_without_effective_pairing_to_close_reason(
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
                    "case_id": "anchor-without-timing",
                    "account": "lx",
                    "status": "waiting_settlement_evidence",
                    "target_contracts_by_lot": {"lot-1": 1},
                }
            ]

    monkeypatch.setattr(
        reconciliation,
        "lifecycle_case_read_model",
        lambda *_args, **_kwargs: {
            "pairing_until_ms": None,
            "pending_until_ms": 200,
            "reason_state": "needs_review",
            "lifecycle_evidence_status": "closure_observed_cause_pending",
        },
    )
    calls: list[dict] = []

    def _reconcile(*_args, **kwargs):
        calls.append(dict(kwargs))
        return {
            "case_id": "anchor-without-timing",
            "decision": {
                "status": "needs_review",
                "reason_codes": ["lifecycle_timing_policy_unavailable"],
            },
        }

    monkeypatch.setattr(
        reconciliation,
        "reconcile_lifecycle_close_reason",
        _reconcile,
    )

    result = reconcile_due_lifecycle_cases(
        _DueV2Repo(),
        account="lx",
        now_ms=200,
        apply_changes=False,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("no effective pairing must not poll provider")
        ),
    )

    assert result["results"][0]["decision"]["reason_codes"] == [
        "lifecycle_timing_policy_unavailable"
    ]
    assert calls == [
        {
            "case_id": "anchor-without-timing",
            "now_ms": 200,
            "apply_changes": False,
        }
    ]


def test_due_reconciliation_skips_conflict_without_effective_pairing(
    monkeypatch,
) -> None:
    import src.application.trades.close_reason_reconciliation as reconciliation

    class _ConflictRepo:
        def list_trade_lifecycle_cases(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return [
                {
                    "schema_version": "lifecycle_case.v2",
                    "case_id": "conflict-without-timing",
                    "account": "lx",
                    "status": "conflict",
                    "target_contracts_by_lot": {"lot-1": 1},
                }
            ]

    monkeypatch.setattr(
        reconciliation,
        "lifecycle_case_read_model",
        lambda *_args, **_kwargs: {
            "pairing_until_ms": None,
            "pending_until_ms": 200,
            "reason_state": "conflict",
            "lifecycle_evidence_status": "conflict",
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "reconcile_lifecycle_close_reason",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("conflict case is absorbing for due reconciliation")
        ),
    )

    result = reconcile_due_lifecycle_cases(
        _ConflictRepo(),
        account="lx",
        now_ms=200,
        apply_changes=False,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("conflict case must not poll provider")
        ),
    )

    assert result["case_count"] == 0
    assert result["results"] == []


def test_due_candidate_reader_excludes_absorbing_terminal_states(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, _policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    active_case_ids = {str(lifecycle_case["case_id"])}
    terminal_case_ids: set[str] = set()
    for status in (
        "waiting_settlement_evidence",
        "needs_review",
        "ledger_written",
        "conflict",
        "superseded",
    ):
        case_id = f"candidate-{status}"
        assert repo.upsert_trade_lifecycle_case(
            {
                **lifecycle_case,
                "case_id": case_id,
                "case_key": case_id,
                "status": status,
            }
        )
        if status in {
            "waiting_settlement_evidence",
            "needs_review",
        }:
            active_case_ids.add(case_id)
        else:
            terminal_case_ids.add(case_id)

    candidates = repo.list_trade_lifecycle_due_candidates(account="lx")
    returned_case_ids = {
        str(item["lifecycle_case"]["case_id"])
        for item in candidates
    }
    with repo._connect() as conn:  # noqa: SLF001 - query-plan contract
        query_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT case_id
            FROM trade_lifecycle_cases
            WHERE account = ?
              AND status NOT IN (
                'ledger_written', 'conflict', 'superseded'
              )
            ORDER BY updated_at_ms DESC, case_id DESC
            """,
            ("lx",),
        ).fetchall()

    assert returned_case_ids == active_case_ids
    assert returned_case_ids.isdisjoint(terminal_case_ids)
    assert any(
        "idx_trade_lifecycle_cases_due" in str(row["detail"])
        for row in query_plan
    )


def test_due_candidate_evidence_revision_is_constant_time_and_mutation_safe(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, _policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    first_case_id = str(lifecycle_case["case_id"])
    second_case_id = "candidate-revision-second"
    assert repo.upsert_trade_lifecycle_case(
        {
            **lifecycle_case,
            "case_id": second_case_id,
            "case_key": second_case_id,
        }
    )

    def revisions() -> dict[str, int]:
        return {
            str(item["lifecycle_case"]["case_id"]): int(
                item["evidence_revision"]
            )
            for item in repo.list_trade_lifecycle_due_candidates(
                account="lx"
            )
        }

    initial = revisions()
    evidence_id = "revision-backfill-evidence"
    with repo._connect() as conn:  # noqa: SLF001 - trigger contract
        traced_sql: list[str] = []
        conn.set_trace_callback(traced_sql.append)
        repo.list_trade_lifecycle_due_candidates(
            account="lx",
            conn=conn,
        )
        conn.set_trace_callback(None)
        conn.execute(
            """
            INSERT INTO trade_lifecycle_evidence (
              evidence_id, case_id, source_type, source_event_id,
              evidence_type, account, symbol, raw_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                first_case_id,
                "revision_test",
                evidence_id,
                "revision_test",
                "lx",
                str(lifecycle_case["symbol"]),
                json.dumps(
                    {
                        "evidence_id": evidence_id,
                        "case_id": first_case_id,
                    },
                    sort_keys=True,
                ),
                1,
            ),
        )
        conn.commit()

    compact_select = next(
        statement
        for statement in traced_sql
        if "FROM trade_lifecycle_cases AS lifecycle_case" in statement
    )
    assert "COUNT(" not in compact_select.upper()
    assert "trade_lifecycle_evidence AS" not in compact_select
    assert "trade_lifecycle_evidence_revisions" in compact_select
    after_insert = revisions()
    assert after_insert[first_case_id] == initial[first_case_id] + 1

    with repo._connect() as conn:  # noqa: SLF001 - trigger contract
        conn.execute(
            """
            UPDATE trade_lifecycle_evidence
            SET case_id = ?
            WHERE evidence_id = ?
            """,
            (second_case_id, evidence_id),
        )
        conn.commit()
    after_move = revisions()
    assert after_move[first_case_id] == after_insert[first_case_id] + 1
    assert after_move[second_case_id] == initial[second_case_id] + 1

    with repo._connect() as conn:  # noqa: SLF001 - trigger contract
        conn.execute(
            "DELETE FROM trade_lifecycle_evidence WHERE evidence_id = ?",
            (evidence_id,),
        )
        conn.commit()
    after_delete = revisions()
    assert after_delete[second_case_id] == after_move[second_case_id] + 1


def test_evidence_revision_upgrade_does_not_require_history_backfill(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, _policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    with repo._connect() as conn:  # noqa: SLF001 - migration fixture
        for trigger_name in (
            "trg_trade_lifecycle_evidence_revision_insert",
            "trg_trade_lifecycle_evidence_revision_update_old",
            "trg_trade_lifecycle_evidence_revision_update_new",
            "trg_trade_lifecycle_evidence_revision_delete",
        ):
            conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute("DROP TABLE trade_lifecycle_evidence_revisions")
        conn.execute(
            """
            INSERT INTO trade_lifecycle_evidence (
              evidence_id, case_id, source_type, source_event_id,
              evidence_type, account, symbol, raw_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-before-revision-migration",
                case_id,
                "revision_test",
                "legacy-before-revision-migration",
                "revision_test",
                "lx",
                str(lifecycle_case["symbol"]),
                json.dumps(
                    {
                        "evidence_id": "legacy-before-revision-migration",
                        "case_id": case_id,
                    },
                    sort_keys=True,
                ),
                1,
            ),
        )
        conn.commit()

    upgraded = SQLiteOptionPositionsRepository(repo.db_path)
    before = next(
        candidate
        for candidate in upgraded.list_trade_lifecycle_due_candidates(
            account="lx"
        )
        if candidate["lifecycle_case"]["case_id"] == case_id
    )
    assert before["evidence_revision"] == 0

    assert upgraded.insert_trade_lifecycle_evidence_once(
        {
            "evidence_id": "first-after-revision-migration",
            "case_id": case_id,
            "source_type": "revision_test",
            "source_event_id": "first-after-revision-migration",
            "evidence_type": "revision_test",
            "account": "lx",
            "symbol": str(lifecycle_case["symbol"]),
        }
    )
    after = next(
        candidate
        for candidate in upgraded.list_trade_lifecycle_due_candidates(
            account="lx"
        )
        if candidate["lifecycle_case"]["case_id"] == case_id
    )
    assert after["evidence_revision"] == 1
    assert case_scope_fingerprint(after) != case_scope_fingerprint(before)


class _Gateway:
    def __init__(
        self,
        *,
        calendar_rows: list[dict[str, str]] | None = None,
        history_deals: list[dict] | None = None,
    ) -> None:
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
        self.history_order_queries: list[dict] = []
        self.position_queries: list[dict] = []
        self.calendar_queries: list[dict] = []

    def get_history_deals(self, **kwargs: object) -> dict:
        self.history_deal_queries.append(dict(kwargs))
        return _complete_receipt(self.history_deal_rows)

    def get_history_orders(self, **kwargs: object) -> dict:
        self.history_order_queries.append(dict(kwargs))
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
        self.position_queries.append(dict(kwargs))
        return _complete_receipt([])

    def get_trading_days_with_receipt(
        self,
        **kwargs: object,
    ) -> dict:
        self.calendar_queries.append(dict(kwargs))
        return _complete_receipt(self.calendar_rows)


class _NativeFutuExpiryOrderGateway(_Gateway):
    def get_history_orders(self, **kwargs: object) -> dict:
        from src.infrastructure.futu_gateway import (
            _annotate_futu_history_order_receipt,
        )

        return _annotate_futu_history_order_receipt(_complete_receipt(
            [
                {
                    "order_id": "option-order-1",
                    "code": OPTION_CODE,
                    "trd_side": "BUY_BACK",
                    "order_type": "NORMAL",
                    "order_status": "FILLED_ALL",
                    "qty": 1.0,
                    "price": 0.0,
                    "dealt_qty": 1.0,
                    "dealt_avg_price": 0.0,
                    "last_err_msg": "",
                    "remark": "",
                    "create_time": "2026-08-21 16:00:00",
                }
            ]
        ))


class _NativeFutuPositiveOrderGateway(
    _NativeFutuExpiryOrderGateway
):
    def get_history_orders(self, **kwargs: object) -> dict:
        from src.infrastructure.futu_gateway import (
            _annotate_futu_history_order_receipt,
        )

        receipt = super().get_history_orders(**kwargs)
        receipt["rows"][0].pop("order_origin", None)
        receipt["rows"][0].pop("order_origin_evidence", None)
        receipt["rows"][0]["price"] = 0.01
        receipt["rows"][0]["dealt_avg_price"] = 0.01
        return _annotate_futu_history_order_receipt(receipt)


class _IncompleteGateway(_Gateway):
    def get_positions_with_receipt(
        self,
        **kwargs: object,
    ) -> dict:
        self.position_queries.append(dict(kwargs))
        return _complete_receipt(
            [
                {
                    "acc_id": "1001",
                    "code": OPTION_CODE,
                    "qty": 1,
                }
            ]
        )


class _RateLimitedGateway(_Gateway):
    def get_history_deals(self, **kwargs: object) -> dict:
        self.history_deal_queries.append(dict(kwargs))

        class _RateLimitError(RuntimeError):
            code = "RATE_LIMIT"
            retry_after_ms = 420_000

        raise _RateLimitError("diagnostic text")


class _UntypedFailedReceiptGateway(_Gateway):
    def get_history_deals(self, **kwargs: object) -> dict:
        self.history_deal_queries.append(dict(kwargs))
        return {
            "retcode": -1,
            "coverage_complete": False,
            "pagination_complete": False,
            "rows": [],
            "error": "query failed",
        }


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
    assert "account_cash_flows" not in observation["source_receipts"]


def test_complete_observation_classifies_native_futu_zero_price_expiry_order(
    tmp_path: Path,
) -> None:
    observation = _collect(tmp_path, _NativeFutuExpiryOrderGateway())

    assert observation["complete"] is True
    assert observation["normal_order_present"] is False
    assert observation["incomplete_reason_codes"] == []


def test_native_futu_positive_price_order_remains_ambiguous(
    tmp_path: Path,
) -> None:
    observation = _collect(tmp_path, _NativeFutuPositiveOrderGateway())

    assert observation["complete"] is False
    assert observation["normal_order_present"] is False
    assert observation["incomplete_reason_codes"] == [
        "anchor_order_classification_ambiguous"
    ]


def test_provider_collection_and_reconciliation_reuse_account_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    account_reads = 0
    original_account_reader = repo.read_lifecycle_account_rows

    def counted_account_reader(*args, **kwargs):
        nonlocal account_reads
        account_reads += 1
        return original_account_reader(*args, **kwargs)

    monkeypatch.setattr(
        repo,
        "read_lifecycle_account_rows",
        counted_account_reader,
    )
    models = lifecycle_case_read_models_for_account(
        repo,
        account="lx",
        now_ms=now_ms,
        settlement_context_case_ids=(case_id,),
    )
    read_model = models[case_id]
    context = read_model[SETTLEMENT_OBSERVATION_CONTEXT_KEY]
    monkeypatch.setattr(
        repo,
        "read_lifecycle_case_rows",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("prepared provider path reread lifecycle account")
        ),
    )
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=_Gateway(),
        futu_account_ids=["1001"],
        now_ms_fn=lambda: now_ms,
        source_id="lx",
    )

    outcome = collector.collect_outcome(lifecycle_case, read_model)
    assert outcome.kind == "observed_complete"
    assert isinstance(outcome.observation, dict)
    preview = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=outcome.observation,
        apply_changes=False,
        coherent_facts=context,
    )

    assert account_reads == 1
    assert preview["decision"]["status"] == "resolved"


def test_provider_start_callback_failure_queries_nothing(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    gateway = _Gateway()
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=gateway,
        futu_account_ids=["1001"],
        now_ms_fn=lambda: now_ms,
        source_id="lx",
    )

    def reject_provider_start() -> None:
        raise RuntimeError("provider marker compare-and-set failed")

    with pytest.raises(
        RuntimeError,
        match="provider marker compare-and-set failed",
    ):
        collector.collect_outcome(
            lifecycle_case,
            lifecycle_case_read_model(
                repo,
                case_id=str(lifecycle_case["case_id"]),
                now_ms=now_ms,
            ),
            before_first_provider_io=reject_provider_start,
        )

    assert gateway.history_deal_queries == []
    assert gateway.history_order_queries == []
    assert gateway.position_queries == []
    assert gateway.calendar_queries == []


def test_provider_start_callback_runs_once_per_collection(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    gateway = _Gateway()
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=gateway,
        futu_account_ids=["1001"],
        now_ms_fn=lambda: now_ms,
        source_id="lx",
    )
    provider_starts: list[None] = []
    read_model = lifecycle_case_read_model(
        repo,
        case_id=str(lifecycle_case["case_id"]),
        now_ms=now_ms,
    )

    for expected_count in (1, 2):
        outcome = collector.collect_outcome(
            lifecycle_case,
            read_model,
            before_first_provider_io=lambda: provider_starts.append(None),
        )

        assert outcome.kind == "observed_complete"
        assert len(provider_starts) == expected_count
        assert len(gateway.history_deal_queries) == expected_count
        assert len(gateway.history_order_queries) == expected_count
        assert len(gateway.position_queries) == expected_count
        assert len(gateway.calendar_queries) == expected_count


def test_generation_mismatch_runs_no_provider_start_callback(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    gateway = _Gateway()
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=gateway,
        futu_account_ids=["1001"],
        now_ms_fn=lambda: now_ms,
        source_id="lx",
    )
    read_model = lifecycle_case_read_model(
        repo,
        case_id=str(lifecycle_case["case_id"]),
        now_ms=now_ms,
    )
    read_model["lifecycle_generation_token"] = "stale"
    provider_starts: list[None] = []

    outcome = collector.collect_outcome(
        lifecycle_case,
        read_model,
        before_first_provider_io=lambda: provider_starts.append(None),
    )

    assert outcome.kind == "stale_generation"
    assert provider_starts == []
    assert gateway.history_deal_queries == []
    assert gateway.history_order_queries == []
    assert gateway.position_queries == []
    assert gateway.calendar_queries == []


def test_missing_quote_dependency_preserves_broker_receipts_and_trade_environment(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(tmp_path)
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    broker = _Gateway()

    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=str(lifecycle_case["case_id"]),
            now_ms=now_ms,
        ),
        broker_gateway=broker,
        quote_gateway=None,
        quote_dependency_error="canonical route conflict",
        futu_account_id="1001",
        trd_env="SIMULATE",
        now_ms=now_ms,
    )

    assert broker.history_deal_queries
    assert broker.history_deal_queries[0]["trd_env"] == "SIMULATE"
    assert broker.position_queries[0]["trd_env"] == "SIMULATE"
    calendar = observation["source_receipts"]["trading_calendar"]
    assert calendar["status"] == "incomplete"
    assert calendar["error"] == "canonical route conflict"
    assert observation["complete"] is False


def test_provider_receipt_preserves_typed_retry_metadata(
    tmp_path: Path,
) -> None:
    observation = _collect(tmp_path, _RateLimitedGateway())
    receipt = observation["source_receipts"]["history_deals"]

    assert receipt["status"] == "incomplete"
    assert receipt["provider_code"] == "RATE_LIMIT"
    assert receipt["error_class"] == "rate_limit"
    assert receipt["retry_after_ms"] == 420_000
    assert "diagnostic text" in receipt["error"]


def test_untyped_failed_receipt_is_not_business_observation(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=_UntypedFailedReceiptGateway(),
        futu_account_ids=["1001"],
        now_ms_fn=lambda: now_ms,
        source_id="lx",
    )

    outcome = collector.collect_outcome(
        lifecycle_case,
        lifecycle_case_read_model(
            repo,
            case_id=str(lifecycle_case["case_id"]),
            now_ms=now_ms,
        ),
    )

    assert outcome.kind == "unknown_error"
    assert outcome.error_class == "unknown"
    assert outcome.observation is None


@pytest.mark.parametrize(
    ("capability_key", "method_name"),
    [
        ("legacy.cash_flow_query", "get_account_cash_flows"),
        ("synthetic.secondary_query", "get_secondary_query"),
    ],
)
def test_test_only_missing_capabilities_share_generic_static_block(
    tmp_path: Path,
    capability_key: str,
    method_name: str,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    gateway = _Gateway()
    provider_starts: list[None] = []
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=gateway,
        futu_account_ids=["1001"],
        now_ms_fn=lambda: now_ms,
        source_id="lx",
        required_capability_keys=(capability_key,),
        additional_capability_requirements={
            capability_key: ("broker", method_name)
        },
    )

    outcome = collector.collect_outcome(
        lifecycle_case,
        lifecycle_case_read_model(
            repo,
            case_id=str(lifecycle_case["case_id"]),
            now_ms=now_ms,
        ),
        before_first_provider_io=lambda: provider_starts.append(None),
    )

    assert collector.capability.missing_keys == (capability_key,)
    assert outcome.kind == "blocked_static"
    assert outcome.reason_code == "missing_static_capability"
    assert provider_starts == []
    assert gateway.history_deal_queries == []
    assert gateway.history_order_queries == []
    assert gateway.position_queries == []
    assert gateway.calendar_queries == []


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


def _collect_stock_settlement_observation(
    repo: SQLiteOptionPositionsRepository,
    *,
    lifecycle_case: dict,
    policy: dict,
    stock_deal_id: str,
) -> tuple[dict, int]:
    stock_time_ms = int(policy["settlement_deadline_ms"]) - 1
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=str(lifecycle_case["case_id"]),
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
                    "deal_id": stock_deal_id,
                    "acc_id": "1001",
                    "code": "US.NVDA",
                    "price": "100",
                    "qty": 100,
                    "trd_side": "BUY",
                    "trade_time_ms": stock_time_ms,
                    "order_id": f"order:{stock_deal_id}",
                },
            ]
        ),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    assert observation["stock_settlement_present"] is True
    assert len(observation["stock_settlement_candidates"]) == 1
    return observation, now_ms


def _mismatched_polled_observation(observation: dict) -> dict:
    mismatched = deepcopy(observation)
    mismatched["stock_settlement_candidates"][0][
        "observed_case_id"
    ] = "other-case"
    return mismatched


def _polled_attempt_fixture(
    tmp_path: Path,
    *,
    invocation_id: str,
    mismatched: bool,
) -> tuple[
    SQLiteOptionPositionsRepository,
    str,
    int,
    dict,
    dict,
    object,
]:
    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    observation, now_ms = _collect_stock_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        policy=policy,
        stock_deal_id=f"stock-{invocation_id[-3:]}",
    )
    if mismatched:
        observation = _mismatched_polled_observation(observation)
    case_id = str(lifecycle_case["case_id"])
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id=invocation_id,
        attempted_at_ms=now_ms,
        outcome_kind="observed_complete",
        observation=observation,
    )
    return (
        repo,
        case_id,
        now_ms,
        observation,
        dict(observation["stock_settlement_candidates"][0]),
        envelope,
    )


def test_polled_preview_rejects_attempt_and_writes_nothing(
    tmp_path: Path,
) -> None:
    repo, case_id, _now_ms, observation, candidate, envelope = (
        _polled_attempt_fixture(
            tmp_path,
            invocation_id="123e4567-e89b-42d3-a456-426614174402",
            mismatched=False,
        )
    )
    before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        ValueError,
        match="polled settlement preview cannot consume an attempt",
    ):
        reconcile_polled_stock_settlement_evidence(
            repo,
            evidence=candidate,
            apply_changes=False,
            attempt_evidence=_settlement_issue_evidence(observation),
            attempt_audit=envelope,
        )

    preview = reconcile_polled_stock_settlement_evidence(
        repo,
        evidence=candidate,
        apply_changes=False,
    )

    assert preview.status == "dry_run"
    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == before
    assert repo.get_trade_lifecycle_evidence(
        str(candidate["evidence_id"])
    ) is None
    assert repo.list_trade_lifecycle_attempt_audits(case_id=case_id) == []
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None


def test_unresolved_polled_attempt_writes_direct_and_observation_atomically(
    tmp_path: Path,
) -> None:
    repo, case_id, now_ms, observation, candidate, envelope = (
        _polled_attempt_fixture(
            tmp_path,
            invocation_id="123e4567-e89b-42d3-a456-426614174403",
            mismatched=True,
        )
    )
    _bootstrap_current_decision_shadow(repo, now_ms=now_ms)

    resolution = reconcile_polled_stock_settlement_evidence(
        repo,
        evidence=candidate,
        apply_changes=True,
        expected_lifecycle_generation_token=str(
            observation["expected_lifecycle_generation_token"]
        ),
        attempt_evidence=_settlement_issue_evidence(observation),
        attempt_audit=envelope,
    )

    assert resolution.status == "unresolved"
    assert resolution.reason == "polled_settlement_case_mismatch"
    assert resolution.diagnostics["attempt_result"]["audit_ordinal"] == 1
    assert resolution.diagnostics["attempt_result"]["decision_projection"][
        "statuses"
    ] == {"lx": "published"}
    assert (
        resolution.diagnostics["attempt_result"]["admission_status"]
        == "admitted_semantic"
    )
    assert repo.get_trade_lifecycle_evidence(
        str(candidate["evidence_id"])
    ) == candidate
    admitted = repo.get_trade_lifecycle_evidence(
        str(observation["observation_id"])
    )
    assert admitted is not None
    assert admitted["observation"] == observation
    assert admitted["semantic_fingerprint"] == (
        observation["semantic_fingerprint"]
    )
    assert len(
        repo.list_trade_lifecycle_attempt_audits(case_id=case_id)
    ) == 1
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"
    _assert_current_lifecycle_matches_oracle(repo, now_ms=now_ms)


def test_unresolved_polled_close_reason_falls_back_to_one_attempt_owner(
    tmp_path: Path,
) -> None:
    repo, case_id, now_ms, observation, candidate, envelope = (
        _polled_attempt_fixture(
            tmp_path,
            invocation_id="123e4567-e89b-42d3-a456-426614174404",
            mismatched=True,
        )
    )

    result = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
        attempt_audit=envelope,
    )

    assert result["poll_settlement_results"][0]["status"] == "unresolved"
    assert (
        result["poll_settlement_results"][0]["reason"]
        == "polled_settlement_case_mismatch"
    )
    assert result["write_result"]["audit_ordinal"] == 1
    assert repo.get_trade_lifecycle_evidence(
        str(candidate["evidence_id"])
    ) is None
    admitted = repo.get_trade_lifecycle_evidence(
        str(observation["observation_id"])
    )
    assert admitted is not None
    assert admitted["observation"] == observation
    assert len(
        repo.list_trade_lifecycle_attempt_audits(case_id=case_id)
    ) == 1
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"


def test_unresolved_polled_attempt_rolls_back_direct_and_observation(
    tmp_path: Path,
) -> None:
    repo, case_id, _now_ms, observation, candidate, envelope = (
        _polled_attempt_fixture(
            tmp_path,
            invocation_id="123e4567-e89b-42d3-a456-426614174405",
            mismatched=True,
        )
    )
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)
    with repo._connect() as conn:  # noqa: SLF001 - atomic rollback fixture
        conn.execute(
            """
            CREATE TRIGGER injected_polled_attempt_audit_failure
            BEFORE INSERT ON trade_lifecycle_attempt_audits
            BEGIN
              SELECT RAISE(ABORT, 'injected polled audit failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected polled audit failure",
    ):
        reconcile_polled_stock_settlement_evidence(
            repo,
            evidence=candidate,
            apply_changes=True,
            expected_lifecycle_generation_token=str(
                observation["expected_lifecycle_generation_token"]
            ),
            attempt_evidence=_settlement_issue_evidence(observation),
            attempt_audit=envelope,
        )

    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == evidence_before
    assert repo.get_trade_lifecycle_evidence(
        str(candidate["evidence_id"])
    ) is None
    assert repo.get_trade_lifecycle_evidence(
        str(observation["observation_id"])
    ) is None
    assert repo.list_trade_lifecycle_attempt_audits(case_id=case_id) == []
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None


def test_poll_stock_settlement_uses_canonical_lifecycle_writer(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = (
        _repo_with_pending_case(tmp_path)
    )
    observation, now_ms = _collect_stock_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        policy=policy,
        stock_deal_id="stock-settlement-1",
    )

    case_id = str(lifecycle_case["case_id"])
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id="123e4567-e89b-42d3-a456-426614174401",
        attempted_at_ms=now_ms,
        outcome_kind="observed_complete",
        observation=observation,
    )
    result = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
        attempt_audit=envelope,
    )

    assert result["poll_settlement_results"][0]["status"] == "applied"
    assert (
        result["lifecycle_read_model"]["close_reason"]
        == "assignment"
    )
    assert result["write_result"] is None
    attempt_result = result["poll_settlement_results"][0][
        "diagnostics"
    ]["attempt_result"]
    assert attempt_result["audit_ordinal"] == 1
    evidence_rows = repo.list_trade_lifecycle_evidence(case_id=case_id)
    pair = next(
        item
        for item in evidence_rows
        if item.get("source_type") == "broker_settlement_pair"
    )
    admitted = next(
        item
        for item in evidence_rows
        if item.get("evidence_id") == observation["observation_id"]
    )
    assert pair["evidence_type"] == "assignment"
    assert set(pair["source_evidence_ids"]) == {
        "anchor-1",
        observation["stock_settlement_candidates"][0]["evidence_id"],
    }
    assert admitted["observation"] == observation
    assert admitted["semantic_fingerprint"] == (
        observation["semantic_fingerprint"]
    )
    assert len(
        repo.list_trade_lifecycle_attempt_audits(case_id=case_id)
    ) == 1
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"


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


def _settlement_issue_evidence(observation: dict) -> dict:
    observation_id = str(observation["observation_id"])
    return {
        "evidence_id": observation_id,
        "case_id": observation["case_id"],
        "source_type": "broker_settlement_observation",
        "source_event_id": observation_id,
        "evidence_type": "settlement_observation",
        "account": observation["account"],
        "symbol": observation["contract_identity"]["symbol"],
        "contracts": sum(
            int(value)
            for value in observation[
                "frozen_preterminal_remaining_by_lot"
            ].values()
        ),
        "observation_hash": observation["semantic_fingerprint"],
        "semantic_schema": observation["semantic_schema"],
        "semantic_fingerprint": observation[
            "semantic_fingerprint"
        ],
        "semantic_projection": observation["semantic_projection"],
        "previous_settlement_evidence_id": observation.get(
            "previous_settlement_evidence_id"
        ),
        "observation": observation,
    }


def _settlement_terminal_evidence(observation: dict) -> dict:
    return {
        **_settlement_issue_evidence(observation),
        "evidence_type": "expire_close",
        "terminal_type": "expire_close",
        "option_type": observation["contract_identity"][
            "option_type"
        ],
        "position_side": observation["contract_identity"][
            "position_side"
        ],
        "strike": observation["contract_identity"]["strike"],
        "expiration_ymd": observation["contract_identity"][
            "expiration_ymd"
        ],
        "event_time_ms": observation["observed_at_ms"],
    }


def _rebase_observation(
    repo: SQLiteOptionPositionsRepository,
    *,
    case_id: str,
    observation: dict,
    previous_evidence_id: str,
    normal_order_present: bool | None = None,
) -> dict:
    payload = deepcopy(observation)
    for key in (
        "observation_id",
        "semantic_schema",
        "semantic_fingerprint",
        "semantic_projection",
    ):
        payload.pop(key, None)
    if normal_order_present is not None:
        payload["normal_order_present"] = normal_order_present
        reasons = {
            str(item)
            for item in payload.get("incomplete_reason_codes") or []
        }
        if normal_order_present:
            reasons.add("normal_order_present")
        else:
            reasons.discard("normal_order_present")
        payload["incomplete_reason_codes"] = sorted(reasons)
    generation = str(
        lifecycle_case_coherent_facts(repo, case_id=case_id)[
            "generation_token"
        ]["generation_token"]
    )
    payload["expected_lifecycle_generation_token"] = generation
    payload["previous_settlement_evidence_id"] = previous_evidence_id
    semantic = attach_settlement_semantics(payload)
    observation_id = settlement_evidence_id(
        case_id=case_id,
        semantic_fingerprint=str(semantic["semantic_fingerprint"]),
        expected_generation_token=generation,
        previous_evidence_id=previous_evidence_id,
    )
    return {
        "observation_id": observation_id,
        "previous_settlement_evidence_id": previous_evidence_id,
        **semantic,
    }


def _record_issue(
    repo: SQLiteOptionPositionsRepository,
    *,
    observation: dict,
) -> dict:
    return record_lifecycle_evidence_issue(
        repo,
        case_id=str(observation["case_id"]),
        evidence=_settlement_issue_evidence(observation),
        status="needs_review",
        reason_codes=list(observation["incomplete_reason_codes"]),
        expected_lifecycle_generation_token=str(
            observation["expected_lifecycle_generation_token"]
        ),
    )


@pytest.mark.parametrize(
    ("gateway", "outcome_kind", "nested_allocation"),
    [
        (_Gateway(), "observed_complete", True),
        (_IncompleteGateway(), "observed_incomplete", False),
    ],
)
def test_close_reason_threads_one_attempt_to_terminal_owner(
    tmp_path: Path,
    gateway: _Gateway,
    outcome_kind: str,
    nested_allocation: bool,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    _bootstrap_current_decision_shadow(repo, now_ms=now_ms)
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=gateway,
        futu_account_id="1001",
        now_ms=now_ms,
    )
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id=(
            "123e4567-e89b-42d3-a456-426614174301"
            if nested_allocation
            else "123e4567-e89b-42d3-a456-426614174302"
        ),
        attempted_at_ms=now_ms,
        outcome_kind=outcome_kind,
        observation=observation,
    )

    result = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
        attempt_audit=envelope,
    )
    owner_result = result["write_result"]
    if nested_allocation:
        owner_result = owner_result["ledger_result"]

    assert owner_result["audit_ordinal"] == 1
    assert owner_result["admission_status"] == "admitted_semantic"
    assert owner_result["decision_projection"]["statuses"] == {"lx": "published"}
    _assert_current_lifecycle_matches_oracle(repo, now_ms=now_ms)
    assert len(
        repo.list_trade_lifecycle_attempt_audits(case_id=case_id)
    ) == 1
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"


def test_state_only_owner_appends_once_and_exact_replay_reads_no_business(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    timing = lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=int(policy["settlement_deadline_ms"]),
    )
    now_ms = int(timing["pairing_until_ms"]) + 1
    assert now_ms < int(policy["settlement_deadline_ms"])
    _bootstrap_current_decision_shadow(repo, now_ms=now_ms)
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
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id="123e4567-e89b-42d3-a456-426614174303",
        attempted_at_ms=now_ms,
        outcome_kind="observed_complete",
        observation=observation,
    )

    first = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
        attempt_audit=envelope,
    )
    assert first["decision"]["status"] == "cause_pending"
    assert first["write_result"]["audit_ordinal"] == 1
    assert first["write_result"]["decision_projection"]["statuses"] == {
        "lx": "published"
    }
    _assert_current_lifecycle_matches_oracle(repo, now_ms=now_ms)
    before = {
        "case": repo.get_trade_lifecycle_case(case_id),
        "evidence": repo.list_trade_lifecycle_evidence(case_id=case_id),
        "audits": repo.list_trade_lifecycle_attempt_audits(case_id=case_id),
        "notifications": repo.list_trade_lifecycle_notifications(
            case_id=case_id
        ),
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact invocation replay reached business reads")

    with monkeypatch.context() as guarded:
        guarded.setattr(repo, "assert_foreign_keys_clean", forbidden)
        guarded.setattr(repo, "get_trade_lifecycle_case", forbidden)
        replay = advance_lifecycle_case_state(
            repo,
            case_id=case_id,
            status="waiting_settlement_evidence",
            derived_summary={},
            public_transition=None,
            expected_lifecycle_generation_token=str(
                observation["expected_lifecycle_generation_token"]
            ),
            evidence=_settlement_issue_evidence(observation),
            attempt_audit=envelope,
        )

    assert replay["audit_idempotent"] is True
    assert replay["audit_ordinal"] == 1
    assert "decision_projection" not in replay
    assert repo.get_trade_lifecycle_case(case_id) == before["case"]
    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == before["evidence"]
    assert repo.list_trade_lifecycle_attempt_audits(
        case_id=case_id
    ) == before["audits"]
    assert repo.list_trade_lifecycle_notifications(
        case_id=case_id
    ) == before["notifications"]


@pytest.mark.parametrize(
    ("gateway", "outcome_kind", "state_only"),
    [
        (_Gateway(), "observed_complete", False),
        (_IncompleteGateway(), "observed_incomplete", False),
        (_Gateway(), "observed_complete", True),
    ],
)
def test_terminal_owner_rolls_back_business_when_audit_append_fails(
    tmp_path: Path,
    gateway: _Gateway,
    outcome_kind: str,
    state_only: bool,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    if state_only:
        timing = lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=int(policy["settlement_deadline_ms"]),
        )
        now_ms = int(timing["pairing_until_ms"]) + 1
    else:
        now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=gateway,
        futu_account_id="1001",
        now_ms=now_ms,
    )
    before = {
        "case": repo.get_trade_lifecycle_case(case_id),
        "evidence": repo.list_trade_lifecycle_evidence(case_id=case_id),
        "events": repo.list_trade_events(),
        "allocations": repo.list_trade_lifecycle_allocations(
            case_id=case_id
        ),
        "notifications": repo.list_trade_lifecycle_notifications(
            case_id=case_id
        ),
    }
    with repo._connect() as conn:  # noqa: SLF001 - focused crash contract
        conn.execute(
            """
            CREATE TRIGGER injected_terminal_owner_audit_failure
            BEFORE INSERT ON trade_lifecycle_attempt_audits
            BEGIN
              SELECT RAISE(ABORT, 'injected terminal audit failure');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="injected terminal audit failure",
    ):
        reconcile_lifecycle_close_reason(
            repo,
            case_id=case_id,
            now_ms=now_ms,
            observation=observation,
            apply_changes=True,
            attempt_audit=build_lifecycle_attempt_audit_envelope(
                case_id=case_id,
                invocation_id=(
                    "123e4567-e89b-42d3-a456-426614174306"
                    if state_only
                    else (
                        "123e4567-e89b-42d3-a456-426614174304"
                        if outcome_kind == "observed_complete"
                        else "123e4567-e89b-42d3-a456-426614174305"
                    )
                ),
                attempted_at_ms=now_ms,
                outcome_kind=outcome_kind,
                observation=observation,
            ),
        )

    assert repo.get_trade_lifecycle_case(case_id) == before["case"]
    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == before["evidence"]
    assert repo.list_trade_events() == before["events"]
    assert repo.list_trade_lifecycle_allocations(
        case_id=case_id
    ) == before["allocations"]
    assert repo.list_trade_lifecycle_notifications(
        case_id=case_id
    ) == before["notifications"]
    assert repo.list_trade_lifecycle_attempt_audits(case_id=case_id) == []


def test_issue_writer_appends_same_semantic_attempts_before_business_dedupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    observed_at_ms = int(policy["settlement_deadline_ms"]) + 1
    _bootstrap_current_decision_shadow(repo, now_ms=observed_at_ms)
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=observed_at_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=observed_at_ms,
    )
    def write(observed: dict, invocation_id: str) -> dict:
        return record_lifecycle_evidence_issue(
            repo,
            case_id=case_id,
            evidence=_settlement_issue_evidence(observed),
            status="needs_review",
            reason_codes=list(observed["incomplete_reason_codes"]),
            expected_lifecycle_generation_token=str(
                observed["expected_lifecycle_generation_token"]
            ),
            attempt_audit=build_lifecycle_attempt_audit_envelope(
                case_id=case_id,
                invocation_id=invocation_id,
                attempted_at_ms=observed_at_ms,
                outcome_kind="observed_incomplete",
                observation=observed,
            ),
        )

    first = write(
        observation,
        "123e4567-e89b-42d3-a456-426614174101",
    )
    evidence_after_first = repo.list_trade_lifecycle_evidence(
        case_id=case_id
    )
    case_after_first = repo.get_trade_lifecycle_case(case_id)
    outbox_after_first = repo.list_trade_lifecycle_notifications(
        case_id=case_id
    )
    decision_storage_after_first = repo.read_current_decision_storage_state(
        "lx"
    )
    second_observation = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id=str(observation["observation_id"]),
    )
    second = write(
        second_observation,
        "123e4567-e89b-42d3-a456-426614174102",
    )

    assert first["audit_ordinal"] == 1
    assert first["decision_projection"]["statuses"] == {"lx": "published"}
    assert read_current_decision_projection(
        repo,
        account="lx",
        now_ms=observed_at_ms,
    )["status"] == "trusted"
    assert second["audit_ordinal"] == 2
    assert second["admission_status"] == "duplicate_semantic"
    assert second["decision_projection"]["projection_dml_count"] == 0
    assert second["decision_projection"]["statuses"] == {"lx": "not_required"}
    assert second["resolution_revision"] == first["resolution_revision"]
    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == evidence_after_first
    assert repo.get_trade_lifecycle_case(case_id) == case_after_first
    assert repo.list_trade_lifecycle_notifications(
        case_id=case_id
    ) == outbox_after_first
    assert repo.read_current_decision_storage_state(
        "lx"
    ) == decision_storage_after_first
    _assert_current_lifecycle_matches_oracle(
        repo,
        now_ms=observed_at_ms,
    )
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"

    counts_before = {
        table: len(rows)
        for table, rows in {
            "audit": repo.list_trade_lifecycle_attempt_audits(
                case_id=case_id
            ),
            "evidence": repo.list_trade_lifecycle_evidence(
                case_id=case_id
            ),
        }.items()
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact invocation replay reached business reads")

    monkeypatch.setattr(repo, "assert_foreign_keys_clean", forbidden)
    monkeypatch.setattr(repo, "get_trade_lifecycle_case", forbidden)
    replay = write(
        second_observation,
        "123e4567-e89b-42d3-a456-426614174102",
    )

    assert replay["audit_idempotent"] is True
    assert replay["audit_ordinal"] == 2
    assert "decision_projection" not in replay
    assert repo.read_current_decision_storage_state(
        "lx"
    ) == decision_storage_after_first
    assert len(repo.list_trade_lifecycle_attempt_audits(case_id=case_id)) == counts_before[
        "audit"
    ]


def test_issue_business_and_sidecar_roll_back_together_on_audit_failure(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    observed_at_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=observed_at_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=observed_at_ms,
    )
    case_before = repo.get_trade_lifecycle_case(case_id)
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)
    outbox_before = repo.list_trade_lifecycle_notifications(case_id=case_id)
    with repo._connect() as conn:  # noqa: SLF001 - focused crash contract
        conn.execute(
            """
            CREATE TRIGGER injected_owner_audit_failure
            BEFORE INSERT ON trade_lifecycle_attempt_audits
            BEGIN
              SELECT RAISE(ABORT, 'injected owner audit failure');
            END
            """
        )

    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="injected owner audit failure",
        ):
            record_lifecycle_evidence_issue(
                repo,
                case_id=case_id,
                evidence=_settlement_issue_evidence(observation),
                status="needs_review",
                reason_codes=list(observation["incomplete_reason_codes"]),
                expected_lifecycle_generation_token=str(
                    observation["expected_lifecycle_generation_token"]
                ),
                attempt_audit=build_lifecycle_attempt_audit_envelope(
                    case_id=case_id,
                    invocation_id="123e4567-e89b-42d3-a456-426614174111",
                    attempted_at_ms=observed_at_ms,
                    outcome_kind="observed_incomplete",
                    observation=observation,
                ),
            )
    finally:
        with repo._connect() as conn:  # noqa: SLF001 - focused crash cleanup
            conn.execute("DROP TRIGGER injected_owner_audit_failure")

    assert repo.get_trade_lifecycle_case(case_id) == case_before
    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == evidence_before
    assert repo.list_trade_lifecycle_notifications(case_id=case_id) == outbox_before
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None
    with repo._connect() as conn:  # noqa: SLF001 - focused rollback proof
        sidecar_count = sum(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "trade_lifecycle_attempt_audit_heads",
                "trade_lifecycle_attempt_audits",
                "trade_lifecycle_observation_spans",
                "trade_lifecycle_receipt_blobs",
            )
        )
    assert sidecar_count == 0


def test_cleanup_failure_warns_without_reclassifying_committed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    observed_at_ms = int(policy["settlement_deadline_ms"]) + 1
    observation_r0 = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=observed_at_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=observed_at_ms,
    )

    def write(observed: dict, invocation_id: str) -> dict:
        return record_lifecycle_evidence_issue(
            repo,
            case_id=case_id,
            evidence=_settlement_issue_evidence(observed),
            status="needs_review",
            reason_codes=list(observed["incomplete_reason_codes"]),
            expected_lifecycle_generation_token=str(
                observed["expected_lifecycle_generation_token"]
            ),
            attempt_audit=build_lifecycle_attempt_audit_envelope(
                case_id=case_id,
                invocation_id=invocation_id,
                attempted_at_ms=observed_at_ms,
                outcome_kind="observed_incomplete",
                observation=observed,
            ),
        )

    write(observation_r0, "123e4567-e89b-42d3-a456-426614174121")
    base_r1 = deepcopy(observation_r0)
    base_r1["receipt_note"] = "R1"
    observation_r1 = _rebase_observation(
        repo,
        case_id=case_id,
        observation=base_r1,
        previous_evidence_id=str(observation_r0["observation_id"]),
    )
    second = write(
        observation_r1,
        "123e4567-e89b-42d3-a456-426614174122",
    )
    assert "cleanup_warning" not in second
    receipt_r1 = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id="123e4567-e89b-42d3-a456-426614174122",
        attempted_at_ms=observed_at_ms,
        outcome_kind="observed_incomplete",
        observation=observation_r1,
    ).receipt_sha256
    assert receipt_r1 is not None
    base_r2 = deepcopy(observation_r0)
    base_r2["receipt_note"] = "R2"
    observation_r2 = _rebase_observation(
        repo,
        case_id=case_id,
        observation=base_r2,
        previous_evidence_id=str(observation_r0["observation_id"]),
    )
    case_before = repo.get_trade_lifecycle_case(case_id)
    outbox_before = repo.list_trade_lifecycle_notifications(case_id=case_id)

    def fail_cleanup(_receipt_hash):
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(
        repo,
        "delete_unreferenced_trade_lifecycle_receipt_blob",
        fail_cleanup,
    )
    third = write(
        observation_r2,
        "123e4567-e89b-42d3-a456-426614174123",
    )

    assert third["audit_ordinal"] == 3
    assert third["admission_status"] == "duplicate_semantic"
    assert third["cleanup_warning"] == {
        "code": "receipt_blob_cleanup_failed",
        "receipt_sha256": receipt_r1.hex(),
        "error_class": "RuntimeError",
    }
    assert len(repo.list_trade_lifecycle_attempt_audits(case_id=case_id)) == 3
    assert repo.get_trade_lifecycle_case(case_id) == case_before
    assert repo.list_trade_lifecycle_notifications(case_id=case_id) == outbox_before
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"


def test_issue_writer_rejects_tampered_frozen_semantic_projection(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    evidence = _settlement_issue_evidence(observation)
    evidence["semantic_projection"] = deepcopy(
        evidence["semantic_projection"]
    )
    evidence["semantic_projection"]["normal_order_present"] = True
    case_before = repo.get_trade_lifecycle_case(case_id)
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        SettlementSemanticUnavailable,
        match="projection mismatch in evidence",
    ):
        record_lifecycle_evidence_issue(
            repo,
            case_id=case_id,
            evidence=evidence,
            status="needs_review",
            reason_codes=list(observation["incomplete_reason_codes"]),
            expected_lifecycle_generation_token=str(
                observation["expected_lifecycle_generation_token"]
            ),
        )

    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == evidence_before
    assert repo.get_trade_lifecycle_case(case_id) == case_before
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None


def test_terminal_writer_rejects_tampered_embedded_semantic_projection(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
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
    evidence = _settlement_terminal_evidence(observation)
    evidence["semantic_projection"] = deepcopy(
        evidence["semantic_projection"]
    )
    evidence["observation"] = deepcopy(evidence["observation"])
    evidence["observation"]["semantic_projection"]["complete"] = False
    case_before = repo.get_trade_lifecycle_case(case_id)
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        SettlementSemanticUnavailable,
        match="projection mismatch in observation",
    ):
        record_lifecycle_allocation(
            repo,
            case_id=case_id,
            evidence=evidence,
            terminal_events=[],
            allocations=[],
            derived_status="ledger_written",
            derived_summary={},
            expected_lifecycle_generation_token=str(
                observation["expected_lifecycle_generation_token"]
            ),
        )

    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == evidence_before
    assert repo.get_trade_lifecycle_case(case_id) == case_before
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None


def test_settlement_writer_dedupes_latest_but_preserves_a_b_a(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation_a = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    assert observation_a["complete"] is False

    first = _record_issue(repo, observation=observation_a)
    assert first["admission_status"] == "admitted_semantic"
    after_first_evidence = repo.list_trade_lifecycle_evidence(
        case_id=case_id
    )
    after_first_revision = first["resolution_revision"]
    after_first_outbox = repo.list_trade_lifecycle_notifications(
        case_id=case_id
    )

    duplicate_a = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation_a,
        previous_evidence_id=str(observation_a["observation_id"]),
    )
    duplicate = _record_issue(repo, observation=duplicate_a)

    assert duplicate["admission_status"] == "duplicate_semantic"
    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == after_first_evidence
    assert duplicate["resolution_revision"] == after_first_revision
    assert repo.list_trade_lifecycle_notifications(
        case_id=case_id
    ) == after_first_outbox

    observation_b = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation_a,
        previous_evidence_id=str(observation_a["observation_id"]),
        normal_order_present=True,
    )
    second = _record_issue(repo, observation=observation_b)
    assert second["admission_status"] == "admitted_semantic"

    observation_a_again = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation_a,
        previous_evidence_id=str(observation_b["observation_id"]),
    )
    third = _record_issue(repo, observation=observation_a_again)

    assert third["admission_status"] == "admitted_semantic"
    semantic_rows = [
        item
        for item in repo.list_trade_lifecycle_evidence(case_id=case_id)
        if item.get("source_type")
        == "broker_settlement_observation"
        and isinstance(item.get("observation"), dict)
    ]
    assert len(semantic_rows) == 3
    assert (
        observation_a["semantic_fingerprint"]
        == observation_a_again["semantic_fingerprint"]
    )
    assert (
        observation_a["observation_id"]
        != observation_a_again["observation_id"]
    )
    latest = repo.get_latest_trade_lifecycle_settlement_evidence(
        case_id=case_id
    )
    assert latest is not None
    assert latest["evidence_id"] == observation_a_again["observation_id"]


def test_concurrent_same_generation_writers_admit_one_transition(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )

    def write_once() -> tuple[str, object]:
        try:
            return "result", _record_issue(repo, observation=observation)
        except ValueError as exc:
            return "value_error", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: write_once(), range(2)))

    admitted = [
        value
        for kind, value in outcomes
        if kind == "result"
        and isinstance(value, dict)
        and value.get("admission_status") == "admitted_semantic"
    ]
    losers = [
        value
        for kind, value in outcomes
        if kind == "value_error"
        or (
            kind == "result"
            and isinstance(value, dict)
            and value.get("admission_status") == "duplicate_semantic"
        )
    ]
    semantic_rows = [
        item
        for item in repo.list_trade_lifecycle_evidence(case_id=case_id)
        if item.get("source_type") == "broker_settlement_observation"
        and isinstance(item.get("observation"), dict)
    ]

    assert len(admitted) == 1
    assert len(losers) == 1
    assert len(semantic_rows) == 1
    if isinstance(losers[0], str):
        assert "generation compare-and-set failed" in losers[0]


def test_same_millisecond_rows_repair_head_by_rowid_without_regression(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.ledger.repository as repository_module

    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    observed_at_ms = int(policy["settlement_deadline_ms"]) + 1
    fixed_created_at_ms = 2_000_000_000_000
    monkeypatch.setattr(
        repository_module,
        "now_ms",
        lambda: fixed_created_at_ms,
    )
    observation_a = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=observed_at_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=observed_at_ms,
    )
    assert _record_issue(
        repo,
        observation=observation_a,
    )["admission_status"] == "admitted_semantic"
    observation_b = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation_a,
        previous_evidence_id=str(observation_a["observation_id"]),
        normal_order_present=True,
    )
    assert _record_issue(
        repo,
        observation=observation_b,
    )["admission_status"] == "admitted_semantic"
    with repo._connect() as conn:  # noqa: SLF001 - deterministic ordering fixture
        created_rows = conn.execute(
            """
            SELECT evidence_id, created_at_ms
            FROM trade_lifecycle_evidence
            WHERE evidence_id IN (?, ?)
            ORDER BY rowid
            """,
            (
                observation_a["observation_id"],
                observation_b["observation_id"],
            ),
        ).fetchall()
    assert [int(row["created_at_ms"]) for row in created_rows] == [
        fixed_created_at_ms,
        fixed_created_at_ms,
    ]

    repo.upsert_trade_lifecycle_settlement_admission_head(
        case_id=case_id,
        semantic_schema=str(observation_a["semantic_schema"]),
        semantic_fingerprint=str(
            observation_a["semantic_fingerprint"]
        ),
        evidence_id=str(observation_a["observation_id"]),
        evidence_created_at_ms=fixed_created_at_ms,
        updated_at_ms=fixed_created_at_ms,
    )
    retry_b = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation_b,
        previous_evidence_id=str(observation_b["observation_id"]),
    )
    before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    result = _record_issue(repo, observation=retry_b)
    head = repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    )

    assert result["admission_status"] == "duplicate_semantic"
    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == before
    assert head is not None
    assert head["evidence_id"] == observation_b["observation_id"]


def test_terminal_settlement_duplicate_does_not_advance_business_state(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
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

    first = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
    )
    assert (
        first["write_result"]["ledger_result"]["admission_status"]
        == "admitted_semantic"
    )
    evidence_before = repo.list_trade_lifecycle_evidence(
        case_id=case_id
    )
    notifications_before = repo.list_trade_lifecycle_notifications(
        case_id=case_id
    )
    case_before = repo.get_trade_lifecycle_case(case_id)
    assert case_before is not None

    duplicate_observation = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id=str(observation["observation_id"]),
    )
    duplicate = record_lifecycle_allocation(
        repo,
        case_id=case_id,
        evidence=_settlement_terminal_evidence(
            duplicate_observation
        ),
        terminal_events=[],
        allocations=[],
        derived_status="ledger_written",
        derived_summary=dict(case_before.get("derived_summary") or {}),
        expected_lifecycle_generation_token=str(
            duplicate_observation[
                "expected_lifecycle_generation_token"
            ]
        ),
    )

    assert duplicate["admission_status"] == "duplicate_semantic"
    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == evidence_before
    assert repo.list_trade_lifecycle_notifications(
        case_id=case_id
    ) == notifications_before
    assert repo.get_trade_lifecycle_case(case_id) == case_before


def test_terminal_duplicate_opens_span_from_admitted_r0_and_audits_r1(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    observed_at_ms = int(policy["settlement_deadline_ms"]) + 1
    observation_r0 = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=observed_at_ms,
        ),
        gateway=_Gateway(),
        futu_account_id="1001",
        now_ms=observed_at_ms,
    )
    first = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=observed_at_ms,
        observation=observation_r0,
        apply_changes=True,
    )
    assert (
        first["write_result"]["ledger_result"]["admission_status"]
        == "admitted_semantic"
    )
    case_before = repo.get_trade_lifecycle_case(case_id)
    assert case_before is not None
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)
    outbox_before = repo.list_trade_lifecycle_notifications(case_id=case_id)
    observation_r1 = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation_r0,
        previous_evidence_id=str(observation_r0["observation_id"]),
    )
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id=case_id,
        invocation_id="123e4567-e89b-42d3-a456-426614174201",
        attempted_at_ms=observed_at_ms + 1,
        outcome_kind="observed_complete",
        observation=observation_r1,
    )

    result = record_lifecycle_allocation(
        repo,
        case_id=case_id,
        evidence=_settlement_terminal_evidence(observation_r1),
        terminal_events=[],
        allocations=[],
        derived_status="ledger_written",
        derived_summary=dict(case_before.get("derived_summary") or {}),
        expected_lifecycle_generation_token=str(
            observation_r1["expected_lifecycle_generation_token"]
        ),
        attempt_audit=envelope,
    )

    with repo._connect() as conn:  # noqa: SLF001 - focused span contract
        span = dict(
            conn.execute(
                "SELECT * FROM trade_lifecycle_observation_spans"
            ).fetchone()
        )
    assert result["audit_ordinal"] == 1
    assert result["admission_status"] == "duplicate_semantic"
    assert span["first_evidence_id"] == observation_r0["observation_id"]
    assert span["last_receipt_sha256"] == envelope.receipt_sha256
    assert span["first_evidence_receipt_sha256"] != envelope.receipt_sha256
    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == evidence_before
    assert repo.list_trade_lifecycle_notifications(case_id=case_id) == outbox_before
    assert repo.get_trade_lifecycle_case(case_id) == case_before
    assert repo.verify_trade_lifecycle_attempt_audit_case(
        case_id=case_id
    )["status"] == "valid"


def test_terminal_settlement_duplicate_with_missing_allocation_fails_closed(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
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
    first = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=now_ms,
        observation=observation,
        apply_changes=True,
    )
    assert (
        first["write_result"]["ledger_result"]["admission_status"]
        == "admitted_semantic"
    )
    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        conn.execute(
            "DELETE FROM trade_lifecycle_allocations WHERE case_id = ?",
            (case_id,),
        )
        conn.commit()
    duplicate_observation = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id=str(observation["observation_id"]),
    )
    case_before = repo.get_trade_lifecycle_case(case_id)
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        SettlementAdmissionStateIncoherent,
        match="summary is incoherent|allocations are missing",
    ):
        record_lifecycle_allocation(
            repo,
            case_id=case_id,
            evidence=_settlement_terminal_evidence(
                duplicate_observation
            ),
            terminal_events=[],
            allocations=[],
            derived_status="ledger_written",
            derived_summary=dict(case_before["derived_summary"]),
            expected_lifecycle_generation_token=str(
                duplicate_observation[
                    "expected_lifecycle_generation_token"
                ]
            ),
        )

    assert repo.list_trade_lifecycle_evidence(
        case_id=case_id
    ) == evidence_before
    assert repo.get_trade_lifecycle_case(case_id) == case_before


def test_issue_duplicate_with_malformed_revision_fails_with_typed_state_error(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    assert _record_issue(
        repo,
        observation=observation,
    )["admission_status"] == "admitted_semantic"
    with repo._connect() as conn:  # noqa: SLF001 - corruption fixture
        row = conn.execute(
            "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        assert row is not None
        corrupted = json.loads(str(row["raw_json"] or "{}"))
        corrupted["derived_summary"]["resolution_revision"] = "bad"
        conn.execute(
            "UPDATE trade_lifecycle_cases SET raw_json = ? WHERE case_id = ?",
            (
                json.dumps(
                    corrupted,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                case_id,
            ),
        )
        conn.commit()
    duplicate_observation = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id=str(observation["observation_id"]),
    )
    evidence_before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        SettlementAdmissionStateIncoherent,
        match="revision is incoherent",
    ):
        _record_issue(repo, observation=duplicate_observation)

    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == (
        evidence_before
    )


def test_settlement_writer_types_foreign_key_violation_but_not_store_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )

    monkeypatch.setattr(
        repo,
        "assert_foreign_keys_clean",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SQLite foreign key check failed: 1 violation(s)")
        ),
    )
    with pytest.raises(
        SettlementAdmissionStateIncoherent,
        match="foreign keys are incoherent",
    ):
        _record_issue(repo, observation=observation)

    monkeypatch.setattr(
        repo,
        "assert_foreign_keys_clean",
        lambda **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _record_issue(repo, observation=observation)


def test_blocked_ticks_do_not_rematerialize_large_account_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    second_case, second_policy = _add_pending_case(
        repo,
        symbol="AAPL",
        strike=200,
        suffix="2",
        option_code="US.AAPL260821P200000",
        anchor_time_ms=_anchor_ms,
    )
    cases = [
        (
            lifecycle_case,
            policy,
            OPTION_CODE,
            "option-close-1",
        ),
        (
            second_case,
            second_policy,
            "US.AAPL260821P200000",
            "option-close-2",
        ),
    ]
    legacy_rows: list[tuple] = []
    for case_index, (
        case,
        case_policy,
        option_code,
        anchor_deal_id,
    ) in enumerate(cases):
        case_id = str(case["case_id"])
        observed_at_ms = int(case_policy["settlement_deadline_ms"]) + 1
        observation = collect_broker_settlement_observation(
            repo,
            lifecycle_case=case,
            read_model=lifecycle_case_read_model(
                repo,
                case_id=case_id,
                now_ms=observed_at_ms,
            ),
            gateway=_Gateway(
                history_deals=[
                    {
                        "deal_id": anchor_deal_id,
                        "acc_id": "1001",
                        "code": option_code,
                        "price": "0",
                        "qty": 1,
                    }
                ]
            ),
            futu_account_id="1001",
            now_ms=observed_at_ms,
        )
        legacy_observation = deepcopy(observation)
        for key in (
            "observation_id",
            "semantic_schema",
            "semantic_fingerprint",
            "semantic_projection",
        ):
            legacy_observation.pop(key, None)
        legacy_observation["complete"] = False
        legacy_observation["broker_option_position_absent"] = False
        legacy_observation["incomplete_reason_codes"] = [
            "broker_option_position_present"
        ]
        for index in range(1_050):
            evidence_id = (
                f"legacy-settlement-{case_index}-{index}"
            )
            row_observation = {
                **legacy_observation,
                "observation_id": evidence_id,
                "observed_at_ms": observed_at_ms + index,
            }
            evidence = {
                "evidence_id": evidence_id,
                "case_id": case_id,
                "source_type": "broker_settlement_observation",
                "source_event_id": evidence_id,
                "evidence_type": "settlement_observation",
                "account": "lx",
                "symbol": str(case["symbol"]),
                "contracts": 1,
                "observation": row_observation,
            }
            legacy_rows.append(
                (
                    evidence_id,
                    case_id,
                    "broker_settlement_observation",
                    evidence_id,
                    "settlement_observation",
                    "lx",
                    str(case["symbol"]),
                    json.dumps(evidence, sort_keys=True),
                    1_700_000_000_000 + index,
                )
            )
    with repo._connect() as conn:  # noqa: SLF001 - resource fixture
        conn.executemany(
            """
            INSERT INTO trade_lifecycle_evidence (
              evidence_id, case_id, source_type, source_event_id,
              evidence_type, account, symbol, raw_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            legacy_rows,
        )
        conn.commit()

    account_reads = 0
    original_reader = repo.read_lifecycle_account_rows

    def counted_reader(*args, **kwargs):
        nonlocal account_reads
        account_reads += 1
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(repo, "read_lifecycle_account_rows", counted_reader)
    gateway = _Gateway()
    collector = build_settlement_observation_collector(
        repo=repo,
        broker_gateway=gateway,
        quote_gateway=gateway,
        futu_account_ids=["1001"],
        now_ms_fn=lambda: int(policy["settlement_deadline_ms"]) + 1,
        source_id="lx",
        required_capability_keys=("synthetic.missing",),
    )
    source = {
        "id": "lx",
        "account": "lx",
        "futu_account_ids": ["1001"],
        "inbox_path": tmp_path / "inbox.sqlite3",
        "settlement_observation": {"enabled": True},
    }
    start_ms = int(policy["settlement_deadline_ms"]) + 1
    case_ids = [str(case[0]["case_id"]) for case in cases]

    def evidence_ids() -> dict[str, tuple[str, ...]]:
        with repo._connect() as conn:  # noqa: SLF001 - exact no-write proof
            return {
                case_id: tuple(
                    str(row["evidence_id"])
                    for row in conn.execute(
                        """
                        SELECT evidence_id
                        FROM trade_lifecycle_evidence
                        WHERE case_id = ?
                        ORDER BY rowid
                        """,
                        (case_id,),
                    ).fetchall()
                )
                for case_id in case_ids
            }

    evidence_before = evidence_ids()
    seals: list[dict] = []
    cases_before = {
        case_id: repo.get_trade_lifecycle_case(case_id)
        for case_id in case_ids
    }
    allocations_before = {
        case_id: repo.list_trade_lifecycle_allocations(
            case_id=case_id
        )
        for case_id in case_ids
    }
    notifications_before = {
        case_id: repo.list_trade_lifecycle_notifications(
            case_id=case_id
        )
        for case_id in case_ids
    }

    first = reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=start_ms,
        apply_changes=True,
        settlement_collector=collector,
        seal_sink=seals.append,
    )
    for tick in range(1, 11):
        result = reconcile_due_lifecycle_cases_for_source(
            repo,
            source=source,
            now_ms=start_ms + tick * 60_000,
            apply_changes=True,
            settlement_collector=collector,
            seal_sink=seals.append,
        )

    assert first["planned_case_count"] == 2
    assert result["planned_case_count"] == 0
    assert account_reads == 1
    assert result["skipped_counts"]["blocked"] == 2
    assert gateway.history_deal_queries == []
    assert gateway.position_queries == []
    assert gateway.calendar_queries == []
    assert evidence_ids() == evidence_before
    assert {
        case_id: repo.get_trade_lifecycle_case(case_id)
        for case_id in case_ids
    } == cases_before
    assert {
        case_id: repo.list_trade_lifecycle_allocations(
            case_id=case_id
        )
        for case_id in case_ids
    } == allocations_before
    assert {
        case_id: repo.list_trade_lifecycle_notifications(
            case_id=case_id
        )
        for case_id in case_ids
    } == notifications_before

    import src.application.ledger.queries as lifecycle_queries

    index_builds = 0
    original_indexer = lifecycle_queries._index_lifecycle_account_snapshot

    def counted_indexer(*args, **kwargs):
        nonlocal index_builds
        index_builds += 1
        return original_indexer(*args, **kwargs)

    monkeypatch.setattr(
        lifecycle_queries,
        "_index_lifecycle_account_snapshot",
        counted_indexer,
    )
    supported_gateway = _Gateway(
        history_deals=[
            {
                "deal_id": "option-close-1",
                "acc_id": "1001",
                "code": OPTION_CODE,
                "price": "0",
                "qty": 1,
                "order_id": "option-order-1",
            },
            {
                "deal_id": "option-close-2",
                "acc_id": "1001",
                "code": "US.AAPL260821P200000",
                "price": "0",
                "qty": 1,
                "order_id": "option-order-2",
            },
        ]
    )
    supported_collector = build_settlement_observation_collector(
        repo=repo,
        broker_gateway=supported_gateway,
        quote_gateway=supported_gateway,
        futu_account_ids=["1001"],
        now_ms_fn=lambda: start_ms + 11 * 60_000,
        source_id="lx",
    )

    supported = reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=start_ms + 11 * 60_000,
        apply_changes=True,
        settlement_collector=supported_collector,
        seal_sink=seals.append,
    )

    assert supported["provider_attempt_count"] == 2
    assert index_builds == 1
    assert account_reads == 2 + supported["provider_attempt_count"]
    assert len(supported_gateway.history_deal_queries) == 2
    assert len(supported_gateway.position_queries) == 2
    assert len(supported_gateway.calendar_queries) == 2
    assert len(seals) == 1
    assert seals[0]["head_count"] == 2


def test_latest_legacy_observation_bootstraps_admission_head_without_duplicate(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    assert _record_issue(
        repo,
        observation=observation,
    )["admission_status"] == "admitted_semantic"
    with repo._connect() as conn:  # noqa: SLF001 - legacy upgrade fixture
        row = conn.execute(
            """
            SELECT raw_json
            FROM trade_lifecycle_evidence
            WHERE evidence_id = ?
            """,
            (observation["observation_id"],),
        ).fetchone()
        assert row is not None
        legacy_evidence = json.loads(str(row["raw_json"] or "{}"))
        for container in (
            legacy_evidence,
            legacy_evidence["observation"],
        ):
            for key in (
                "semantic_schema",
                "semantic_fingerprint",
                "semantic_projection",
            ):
                container.pop(key, None)
        conn.execute(
            """
            UPDATE trade_lifecycle_evidence
            SET raw_json = ?
            WHERE evidence_id = ?
            """,
            (
                json.dumps(
                    legacy_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                observation["observation_id"],
            ),
        )
        conn.execute(
            """
            DELETE FROM trade_lifecycle_settlement_admission_heads
            WHERE case_id = ?
            """,
            (case_id,),
        )
        conn.commit()

    retry = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id=str(observation["observation_id"]),
    )
    before = repo.list_trade_lifecycle_evidence(case_id=case_id)
    result = _record_issue(repo, observation=retry)

    assert result["admission_status"] == "duplicate_semantic"
    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == before
    head = repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    )
    assert head is not None
    assert head["evidence_id"] == observation["observation_id"]


def test_legacy_observation_without_matching_business_state_fails_closed(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    legacy_evidence = _settlement_issue_evidence(observation)
    for container in (
        legacy_evidence,
        legacy_evidence["observation"],
    ):
        for key in (
            "semantic_schema",
            "semantic_fingerprint",
            "semantic_projection",
        ):
            container.pop(key, None)
    assert repo.insert_trade_lifecycle_evidence_once(legacy_evidence)
    retry = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id=str(observation["observation_id"]),
    )
    before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        SettlementAdmissionStateIncoherent,
        match="revision is incoherent",
    ):
        _record_issue(repo, observation=retry)

    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == before
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None


def test_malformed_latest_legacy_observation_fails_closed(
    tmp_path: Path,
) -> None:
    repo, lifecycle_case, policy, _anchor_ms = _repo_with_pending_case(
        tmp_path
    )
    case_id = str(lifecycle_case["case_id"])
    now_ms = int(policy["settlement_deadline_ms"]) + 1
    observation = collect_broker_settlement_observation(
        repo,
        lifecycle_case=lifecycle_case,
        read_model=lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
        gateway=_IncompleteGateway(),
        futu_account_id="1001",
        now_ms=now_ms,
    )
    malformed = _settlement_issue_evidence(observation)
    malformed["evidence_id"] = "legacy-malformed"
    malformed["source_event_id"] = "legacy-malformed"
    malformed["observation"] = deepcopy(observation)
    malformed["observation"].pop("contract_identity", None)
    for key in (
        "semantic_schema",
        "semantic_fingerprint",
        "semantic_projection",
    ):
        malformed.pop(key, None)
    assert repo.insert_trade_lifecycle_evidence_once(malformed)

    retry = _rebase_observation(
        repo,
        case_id=case_id,
        observation=observation,
        previous_evidence_id="legacy-malformed",
    )
    before = repo.list_trade_lifecycle_evidence(case_id=case_id)

    with pytest.raises(
        LegacySettlementSemanticUnavailable,
        match="legacy_semantic_unavailable",
    ):
        _record_issue(repo, observation=retry)

    assert repo.list_trade_lifecycle_evidence(case_id=case_id) == before
    assert repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id
    ) is None
