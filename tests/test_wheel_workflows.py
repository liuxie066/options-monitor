from __future__ import annotations
from src.application.trades.attribution import confirm_wheel_call_linkage

from cash_evidence_helpers import cash_portfolio, cash_config
from src.application.portfolio_context_service import cash_snapshot_evidence

from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.wheel.workflows as wheel_workflows
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application.ledger.api import decision_state_snapshot
from domain.domain.wheel import lot_strategy_metadata_for_lot
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.positions.workflows import execute_manual_assignment
from src.application.wheel import (
    build_wheel_read_model,
    cancel_wheel_call_intent,
    create_wheel_call_intent,
    end_wheel_lifecycle,
    reject_wheel_call_linkage,
)


def test_put_intent_preview_revalidates_capacity_inside_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "account_wheel_events": [],
        "trade_events": [],
        "account_position_lots": [],
    }
    position_lots = [{"record_id": "existing-put"}]

    class Repo:
        def read_lifecycle_account_rows(self, *, account, conn):
            assert account == "lx"
            assert conn is not None
            return rows

        def list_position_lots(self, *, conn):
            assert conn is not None
            return position_lots

        def get_current_wheel_activation_window(self, *, market, account, conn):
            assert (market, account) == ("us", "lx")
            assert conn is not None
            return {
                "market": "us",
                "account": "lx",
                "generation": 1,
                "activated_at_ms": 500,
                "deactivated_at_ms": None,
                "policy_sha256": "a" * 64,
            }

    repo = Repo()
    branch = {
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "lifecycle_status": "active",
        "integrity_status": "trusted",
        "phase": "ready",
        "active_option_lot_ids": [],
        "active_intent_ids": [],
        "batch_generation_hash": "generation-1",
    }
    candidate = {
        "final_candidate_id": "candidate-1",
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "batch_generation_hash": "generation-1",
        "capacity_identity_hash": "capacity-1",
        "granted_contracts": 1,
        "multiplier": 100,
        "strike": 100,
        "currency": "USD",
        "cash_reservation_currency": "USD",
        "expiration_ymd": "2027-01-15",
    }
    snapshot = {
        "account": "lx",
        "snapshot_hash": "snapshot-1",
        "strategy_policy_sha256": "b" * 64,
        "rows": [
            {
                "wheel_branch_id": "wheel-put-1",
                "direction": "put",
                "batch_generation_hash": "generation-1",
                "final_candidate": candidate,
            }
        ],
        "opening_put_candidates": [{"symbol": "MSFT"}],
    }
    allocation = {
        "account": "lx",
        "allocation_status": "allocated",
        "granted_contracts": 1,
        "capacity_identity_hash": "capacity-1",
        "cash_reservation_amount": 10_000,
        "cash_reservation_currency": "USD",
    }
    revalidations = []
    monkeypatch.setattr(
        wheel_workflows,
        "with_sqlite_repo_transaction",
        lambda active_repo, call, **_kwargs: call(active_repo, object()),
    )
    monkeypatch.setattr(wheel_workflows, "_wheel_branch", lambda *_args, **_kwargs: branch)
    monkeypatch.setattr(wheel_workflows, "project_wheel_intents", lambda *_args, **_kwargs: [])
    trusted_snapshot = {"snapshot_status": "trusted", "decision_state_fingerprint": "decision-1"}
    monkeypatch.setattr(
        wheel_workflows, "decision_state_snapshot_from_locked_rows",
        lambda *_args, **_kwargs: trusted_snapshot,
    )

    def _revalidate(**kwargs):
        assert kwargs["decision_snapshot"] is trusted_snapshot
        revalidations.append(kwargs)
        return allocation

    monkeypatch.setattr(
        wheel_workflows,
        "revalidate_selected_wheel_put_candidate_from_rows",
        _revalidate,
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(1, timezone.utc)
    monkeypatch.setattr(wheel_workflows, "datetime", Clock)
    original_create = wheel_workflows.create_wheel_intent
    request = {}
    def capture_request(active, **kwargs):
        if not request:
            request.update(kwargs)
        return original_create(active, **kwargs)
    monkeypatch.setattr(wheel_workflows, "create_wheel_intent", capture_request)
    result = wheel_workflows.create_wheel_intent(
        repo,
        candidate_snapshot=snapshot,
        current_strategy_policy_sha256="b" * 64,
        account="lx",
        wheel_branch_id="wheel-put-1",
        direction="put",
        final_candidate_id="candidate-1",
        expected_snapshot_hash="snapshot-1",
        expected_batch_generation_hash="generation-1",
        expires_at_ms=2_000,
        request_id="request-1",
        actor="tester",
        runtime_config=cash_config(),
        capacity_fact={
            "cash_evidence": cash_snapshot_evidence(cash_portfolio({"cash_by_currency": {"USD": 20_000}, "source_observed_at": "1970-01-01T00:00:01+00:00"})),
            "fx_snapshot": {"rates": {}},
        },
        new_intent_enabled=True,
        account_configured=True,
        market="us",
        activation_descriptor={
            "market": "us",
            "account": "lx",
            "generation": 1,
            "activated_at_ms": 500,
            "deactivated_at_ms": None,
            "policy_hash": "a" * 64,
        },
        policy_sha256="a" * 64,
        apply_changes=False,
        as_of_ms=1_000,
    )

    assert result["schema_version"] == "wheel_intent_result.v1"
    assert result["direction"] == "put"
    assert result["wheel_branch_id"] == "wheel-put-1"
    assert result["dry_run"] is True
    assert result["write_applied"] is False

    with pytest.raises(ValueError, match="repreview required"):
        wheel_workflows.create_wheel_intent(
            repo, candidate_snapshot=snapshot, current_strategy_policy_sha256="b" * 64,
            account="lx", wheel_branch_id="wheel-put-1", direction="put",
            final_candidate_id="candidate-1", expected_snapshot_hash="snapshot-1",
            expected_batch_generation_hash="generation-1", expires_at_ms=2_000,
            request_id="request-stale-broker", actor="tester",
            capacity_fact={"cash_authority": {"status": "available", "source_observed_at":
                (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}},
            new_intent_enabled=True, account_configured=True, market="us",
            activation_descriptor={
                "market": "us", "account": "lx", "generation": 1,
                "activated_at_ms": 500, "deactivated_at_ms": None,
                "policy_hash": "a" * 64,
            },
            policy_sha256="a" * 64, apply_changes=False, as_of_ms=1_000,
        )
    assert len(revalidations) == 1
    def delayed_transaction(active, call, **kwargs):
        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(902, timezone.utc)
        monkeypatch.setattr(wheel_workflows, "datetime", ExpiredClock)
        return call(active, object())
    monkeypatch.setattr(wheel_workflows, "with_sqlite_repo_transaction", delayed_transaction)
    with pytest.raises(ValueError, match="repreview required"):
        original_create(repo, **{**request, "apply_changes": True})
    assert len(revalidations) == 1

    monkeypatch.setattr(wheel_workflows, "datetime", Clock)
    monkeypatch.setattr(wheel_workflows, "with_sqlite_repo_transaction",
        lambda active, call, **kwargs: call(active, object()))
    candidate.pop("capacity_identity_hash")
    with pytest.raises(ValueError, match="capacity identity"):
        wheel_workflows.create_wheel_intent(
            repo, candidate_snapshot=snapshot, current_strategy_policy_sha256="b" * 64,
            account="lx", wheel_branch_id="wheel-put-1", direction="put",
            final_candidate_id="candidate-1", expected_snapshot_hash="snapshot-1",
            expected_batch_generation_hash="generation-1", expires_at_ms=2_000,
            request_id="request-missing-hash", actor="tester",
            runtime_config=cash_config(),
            capacity_fact={"cash_evidence": cash_snapshot_evidence(cash_portfolio({"cash_by_currency": {"USD": 20000}, "source_observed_at": "1970-01-01T00:00:01+00:00"}))},
            new_intent_enabled=True, account_configured=True, market="us",
            activation_descriptor={
                "market": "us", "account": "lx", "generation": 1,
                "activated_at_ms": 500, "deactivated_at_ms": None,
                "policy_hash": "a" * 64,
            },
            policy_sha256="a" * 64, apply_changes=False, as_of_ms=1_000,
        )
    candidate["capacity_identity_hash"] = "capacity-1"

    with pytest.raises(ValueError, match="wheel_disabled: account_not_configured"):
        wheel_workflows.create_wheel_intent(
            repo,
            candidate_snapshot=snapshot,
            current_strategy_policy_sha256="b" * 64,
            account="lx",
            wheel_branch_id="wheel-put-1",
            direction="put",
            final_candidate_id="candidate-1",
            expected_snapshot_hash="snapshot-1",
            expected_batch_generation_hash="generation-1",
            expires_at_ms=2_000,
            request_id="request-removed",
            actor="tester",
            capacity_fact={"cash_authority": {"status": "available"}},
            new_intent_enabled=True,
            account_configured=False,
            market="us",
            activation_descriptor={
                "market": "us", "account": "lx", "generation": 1,
                "activated_at_ms": 500, "deactivated_at_ms": None,
                "policy_hash": "a" * 64,
            },
            policy_sha256="a" * 64,
            apply_changes=False,
            as_of_ms=1_000,
        )
    assert revalidations[0]["lifecycle_rows"] is rows
    assert revalidations[0]["position_lots"] == position_lots
    assert revalidations[0]["opening_put_candidates"] == [{"symbol": "MSFT"}]

    previous_revalidations = len(revalidations)
    confirm = dict(
        candidate_snapshot=snapshot, current_strategy_policy_sha256="b" * 64,
        account="lx", wheel_branch_id="wheel-put-1", direction="put",
        final_candidate_id="candidate-1", expected_snapshot_hash="snapshot-1",
        expected_batch_generation_hash="generation-1", expires_at_ms=2_000_000,
        request_id="request-cash-contract", actor="tester", runtime_config=cash_config(),
        capacity_fact={"cash_evidence": cash_snapshot_evidence(cash_portfolio({
            "cash_by_currency": {"USD": 20000},
            "source_observed_at": "1970-01-01T00:00:01+00:00"})), "fx_snapshot": {"rates": {}}},
        new_intent_enabled=True, account_configured=True, market="us",
        activation_descriptor={"market": "us", "account": "lx", "generation": 1,
            "activated_at_ms": 500, "deactivated_at_ms": None, "policy_hash": "a" * 64},
        policy_sha256="a" * 64, apply_changes=False, as_of_ms=1_000,
    )
    for changed in (
        {"runtime_config": {**cash_config(), "runtime": {"portfolio_context_ttl_sec": 600}}},
        {"runtime_config": cash_config(account_id="other-account")},
    ):
        with pytest.raises(ValueError, match="repreview required"):
            wheel_workflows.create_wheel_intent(repo, **{**confirm, **changed})
    assert len(revalidations) == previous_revalidations
    rows["account_wheel_events"].append({
        "event_type": "wheel_put_intent_created", "event_id": "persisted-event",
        "intent_id": "persisted-intent", "stock_lot_id": None, "wheel_branch_id": "wheel-put-1",
        "payload": {"request_id": "request-cash-contract", "actor": "tester",
            "final_candidate_id": "candidate-1", "snapshot_hash": "snapshot-1",
            "batch_generation_hash": "generation-1", "expires_at_ms": 2_000_000, "market": "us"},
    })
    monkeypatch.setattr(wheel_workflows, "with_sqlite_repo_transaction", delayed_transaction)
    replay = wheel_workflows.create_wheel_intent(repo, **{**confirm, "as_of_ms": 3_000_000,
        "runtime_config": {}, "capacity_fact": {}, "new_intent_enabled": False})
    assert replay["status"] == "idempotent"
    assert replay["event_id"] == "persisted-event"
    assert replay["write_applied"] is False
    assert len(revalidations) == previous_revalidations


def test_put_linkage_rejection_preview_uses_canonical_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "account_wheel_events": [],
        "account_position_lots": [],
        "trade_events": [],
    }

    class Repo:
        def read_lifecycle_account_rows(self, *, account, conn):
            assert account == "lx"
            assert conn is not None
            return rows

    repo = Repo()
    branch = {
        "account": "lx",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "batch_generation_hash": "generation-1",
    }
    candidate = {
        "direction": "put",
        "wheel_branch_id": "wheel-put-1",
        "option_record_id": "put-lot-1",
        "option_open_event_id": "put-open-1",
        "linkage_candidate_id": "candidate-1",
        "input_snapshot_hash": "input-1",
        "batch_generation_hash": "generation-1",
    }
    monkeypatch.setattr(
        wheel_workflows,
        "with_sqlite_repo_transaction",
        lambda active_repo, call, **_kwargs: call(active_repo, object()),
    )
    monkeypatch.setattr(
        wheel_workflows,
        "build_wheel_read_model_from_rows",
        lambda *_args, **_kwargs: {"wheel_branches": [branch]},
    )
    monkeypatch.setattr(
        wheel_workflows,
        "project_wheel_linkage_candidates",
        lambda *_args, **_kwargs: [candidate],
    )

    result = wheel_workflows.reject_wheel_linkage(
        repo,
        account="lx",
        option_lot_id="put-lot-1",
        wheel_branch_id="wheel-put-1",
        direction="put",
        linkage_candidate_id="candidate-1",
        expected_input_hash="input-1",
        expected_batch_generation_hash="generation-1",
        request_id="request-1",
        actor="tester",
        reason="not this cycle",
        market="us",
        apply_changes=False,
        as_of_ms=1_000,
    )

    assert result["schema_version"] == "wheel_linkage_result.v1"
    assert result["direction"] == "put"
    assert result["wheel_branch_id"] == "wheel-put-1"
    assert result["option_record_id"] == "put-lot-1"
    assert result["status"] == "planned"
    assert result["dry_run"] is True








def test_wheel_intent_replay_uses_stable_request_and_preserves_accepted_capacity(tmp_path):
    repo, lot_id = _wheel_repo(tmp_path)
    created, coverage = _create_call_intent(repo, lot_id)
    before = repo.list_wheel_events(account="lx")
    original = next(event["payload"] for event in before if event["event_type"] == "wheel_call_intent_created")
    replay = create_wheel_call_intent(
        repo, candidate_snapshot={}, current_strategy_policy_sha256="b" * 64, account="lx", lot_id=lot_id,
        final_candidate_id=original["final_candidate_id"], expected_snapshot_hash=original["snapshot_hash"],
        expected_batch_generation_hash=original["batch_generation_hash"],
        expires_at_ms=original["expires_at_ms"], request_id=original["request_id"], actor=original["actor"],
        coverage_fact={**coverage, "capacity_identity_hash": "refreshed-capacity", "shares_available_for_cover": 0},
        new_intent_enabled=True, account_configured=False, market="us", activation_descriptor=None,
        policy_sha256="", apply_changes=True, as_of_ms=6_000,
    )
    assert replay["status"] == "idempotent"
    assert replay["event_id"] == created["event_id"]
    assert repo.list_wheel_events(account="lx") == before


def _assign_short_put(
    tmp_path: Path,
    *,
    wheel_start_enabled: bool,
    contracts: int = 1,
) -> tuple[SQLiteOptionPositionsRepository, str, str]:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    put_lot_id = "wheel-source-put-lot"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-source-put-open",
                event_type="open",
                event_time_ms=1_000,
                contract_key=_nvda_put_leg_key(),
                contracts=contracts,
                price=2.5,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=put_lot_id,
                # §9.2 step 3: the short put side now travels as the trade side.
                raw_payload=_trusted_multiplier_payload("wheel-source-put-open", side="sell"),
            )
        ],
    )
    if wheel_start_enabled:
        _open_test_activation(repo)
    assignment_event_id = "wheel-source-put-assignment"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=assignment_event_id,
                event_type="assignment",
                event_time_ms=2_000,
                contract_key=_nvda_put_leg_key(),
                contracts=contracts,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=put_lot_id,
                raw_payload={
                    "side": "buy",
                    "target_lot_id": put_lot_id,
                    "stock_settlement": {
                        "side": "buy",
                        "shares": contracts * 100,
                        "price": 100,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    }
                },
            )
        ],
    )
    return repo, put_lot_id, f"assigned-stock-{assignment_event_id}"


def _wheel_repo(
    tmp_path: Path,
    *,
    contracts: int = 1,
) -> tuple[SQLiteOptionPositionsRepository, str]:
    """A repository holding the canonical assigned short put, plus its stock lot id."""
    repo, _put_lot_id, lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
        contracts=contracts,
    )
    return repo, lot_id


def _open_test_activation(repo: SQLiteOptionPositionsRepository) -> None:
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.open_wheel_activation_window(
            market="us",
            account="lx",
            expected_current_generation=0,
            policy_hash="a" * 64,
            request_id="test-wheel-activation",
            request_hash="b" * 64,
            conn=conn,
        )


def _trusted_multiplier_payload(event_id: str, **extra: object) -> dict[str, object]:
    return {
        "source_type": "broker_trade_event",
        "source_deal_id": event_id,
        "external_event_key": f"futu:{event_id}",
        "multiplier_source": "payload",
        **extra,
    }


def _lot_row_for_option_type(repo, option_type: str) -> dict:
    """The stored lot row for one option type (contract is nested now)."""
    return next(
        row
        for row in repo.list_position_lots()
        if (row["fields"].get("contract_key") or {}).get("option_type") == option_type
    )


def _lot_strategy_metadata(repo, lot_id: str, account: str = "lx") -> dict:
    """A lot's strategy metadata, resolved from the event layer (design §7.5).

    The family left ``fields_json`` with the converged payload shape, so the
    assertions that used to read it off the lot read it off the events.
    """
    rows = repo.read_lifecycle_account_rows(account=account)
    return lot_strategy_metadata_for_lot(lot_id, rows.get("trade_events") or [])


def _create_call_intent(
    repo: SQLiteOptionPositionsRepository,
    lot_id: str,
    *,
    new_intent_enabled: bool = True,
    account_configured: bool = True,
    contracts: int = 1,
    broker_order_id: str | None = None,
    market: str = "us",
    broker_observed_at: str | None = None,
    include_decision_identity: bool = True,
    include_capacity_identity: bool = True,
) -> tuple[dict, dict]:
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]
    snapshot = {
        "account": "lx",
        "snapshot_hash": "snapshot-1",
        "strategy_policy_sha256": "b" * 64,
        "batches": [
            {
                "stock_lot_id": lot_id,
                "batch_generation_hash": batch["batch_generation_hash"],
                "final_candidate": {
                    "final_candidate_id": "candidate-1",
                    "symbol": "NVDA",
                    "stock_lot_id": lot_id,
                    "strike": 110,
                    "expiration_ymd": "2026-08-21",
                    "granted_contracts": contracts,
                    "multiplier": 100,
                    **({"capacity_identity_hash": "capacity-1"} if include_capacity_identity else {}),
                },
            }
        ],
    }
    observed_at = (
        datetime.now(timezone.utc).isoformat()
        if broker_observed_at is None else broker_observed_at
    )
    decision = decision_state_snapshot(
        repo, account="lx", portfolio_scope_id=portfolio_scope_id("lx"),
        source_observed_at=observed_at, current_decision_now_ms=4_000,
    )
    coverage = {
        "account": "lx",
        "symbol": "NVDA",
        "source_observed_at": observed_at,
        **({"decision_state_fingerprint": decision["decision_state_fingerprint"]}
           if include_decision_identity else {}),
        "capacity_identity_hash": "capacity-1",
        "status": "available",
        "shares_eligible": contracts * 100,
        "shares_locked": 0,
        "shares_reserved": 0,
        "shares_available_for_cover": contracts * 100,
    }
    created = create_wheel_call_intent(
        repo,
        candidate_snapshot=snapshot,
        current_strategy_policy_sha256="b" * 64,
        account="lx",
        lot_id=lot_id,
        final_candidate_id="candidate-1",
        expected_snapshot_hash="snapshot-1",
        expected_batch_generation_hash=batch["batch_generation_hash"],
        expires_at_ms=10_000,
        request_id="intent-create-1",
        actor="tester",
        coverage_fact=coverage,
        new_intent_enabled=new_intent_enabled,
        account_configured=account_configured,
        market=market,
        activation_descriptor={
            "market": market,
            "account": "lx",
            "generation": 1,
            "activated_at_ms": 500,
            "deactivated_at_ms": None,
            "policy_hash": "a" * 64,
        },
        policy_sha256="a" * 64,
        broker_order_id=broker_order_id,
        apply_changes=True,
        as_of_ms=4_000,
    )
    return created, coverage


def test_call_intent_rejects_stale_broker_fact_and_unbound_decision(
    tmp_path: Path,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    before = repo.list_wheel_events(account="lx")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="broker capacity observation"):
        _create_call_intent(repo, lot_id, broker_observed_at=old_time)
    assert repo.list_wheel_events(account="lx") == before
    with pytest.raises(ValueError, match="broker capacity observation"):
        _create_call_intent(repo, lot_id, broker_observed_at="")
    assert repo.list_wheel_events(account="lx") == before
    with pytest.raises(ValueError, match="coverage settlement decision"):
        _create_call_intent(repo, lot_id, include_decision_identity=False)
    assert repo.list_wheel_events(account="lx") == before
    with pytest.raises(ValueError, match="coverage settlement decision"):
        _create_call_intent(repo, lot_id, include_capacity_identity=False)
    assert repo.list_wheel_events(account="lx") == before


def test_call_intent_rechecks_source_conflict_inside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    before = repo.list_wheel_events(account="lx")
    monkeypatch.setattr(
        wheel_workflows, "decision_state_snapshot_from_locked_rows",
        lambda *_args, **_kwargs: {"snapshot_status": "source_untrusted"},
    )
    with pytest.raises(ValueError, match="coverage settlement decision is unavailable"):
        _create_call_intent(repo, lot_id)
    assert repo.list_wheel_events(account="lx") == before


def _open_unlinked_call(
    repo: SQLiteOptionPositionsRepository,
    *,
    event_time_ms: int = 3_000,
) -> str:
    call_lot_id = "unlinked-call-lot-1"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="unlinked-call-open-1",
                event_type="open",
                event_time_ms=event_time_ms,
                contract_key=_nvda_call_leg_key(),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload={"side": "sell"},
            )
        ],
    )
    return call_lot_id


def _nvda_put_leg_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        strike=100,
        expiration_ymd="2026-08-21",
    )


def _nvda_call_leg_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        strike=110,
        expiration_ymd="2026-08-21",
    )


def _persist_wheel_call_open(
    repo: SQLiteOptionPositionsRepository,
    *,
    event_id: str,
    lot_id: str,
    source_stock_lot_id: str,
) -> None:
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=event_id,
                event_type="open",
                event_time_ms=3_000,
                contract_key=_nvda_call_leg_key(),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=lot_id,
                raw_payload=_trusted_multiplier_payload(
                    event_id,
                    strategy="wheel",
                    leg_role="wheel_call",
                    source_stock_lot_id=source_stock_lot_id,
                    side="sell",
                ),
            )
        ],
    )


def _persist_wheel_call_assignment(
    repo: SQLiteOptionPositionsRepository,
    *,
    event_id: str,
    lot_id: str,
) -> None:
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=event_id,
                event_type="assignment",
                event_time_ms=4_000,
                contract_key=_nvda_call_leg_key(),
                contracts=1,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=lot_id,
                raw_payload={
                    "side": "buy",
                    "target_lot_id": lot_id,
                    "stock_settlement": {
                        "side": "sell",
                        "shares": 100,
                        "price": 110,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    },
                },
            )
        ],
    )


def test_assignment_starts_wheel_and_manual_end_is_cas_idempotent(
    tmp_path: Path,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]

    preview = end_wheel_lifecycle(
        repo,
        account="lx",
        lot_id=lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        market="us",
        as_of_ms=4_000,
    )
    applied = end_wheel_lifecycle(
        repo,
        account="lx",
        lot_id=lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        market="us",
        apply_changes=True,
        as_of_ms=4_000,
    )
    replay = end_wheel_lifecycle(
        repo,
        account="lx",
        lot_id=lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        market="us",
        apply_changes=True,
        as_of_ms=5_000,
    )

    assert preview["dry_run"] is True
    assert preview["lifecycle_status_after"] == "manual_ended"
    assert applied["write_applied"] is True
    assert replay["idempotent"] is True
    assert replay["write_applied"] is False
    events = repo.list_wheel_events(account="lx")
    assert len(events) == 2
    assert next(
        event for event in events if event["event_type"] == "wheel_manual_ended"
    )["event_schema_version"] == "wheel_event.v2"
    terminal = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert terminal["lifecycle_status"] == "manual_ended"
    assert terminal["phase"] is None


@pytest.mark.parametrize("apply_changes", [False, True])
def test_manual_end_rejects_cross_market_without_effects(
    tmp_path: Path,
    apply_changes: bool,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    batch = build_wheel_read_model(repo, "lx", 3_000, market="us")["batches"][0]
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="resolve uniquely"):
        end_wheel_lifecycle(
            repo,
            account="lx",
            lot_id=lot_id,
            expected_batch_generation_hash=batch["batch_generation_hash"],
            request_id=f"cross-market-end-{apply_changes}",
            actor="tester",
            market="hk",
            apply_changes=apply_changes,
            as_of_ms=4_000,
        )

    assert repo.list_wheel_events(account="lx") == before


def test_combo_funding_put_without_persisted_identity_preserves_combo_tail_without_wheel(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    group_id = "combo_yield:lx:nvda-20260821"
    for option_type, side, strike, role, opened_at_ms in (
        ("put", "short", 100, "funding_put", 1_000),
        ("call", "long", 120, "participation_call", 1_100),
    ):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type=option_type,
            side=side,
            contracts=1,
            currency="USD",
            strike=strike,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=opened_at_ms,
            strategy_snapshot={
                "strategy": "combo_yield",
                "leg_role": role,
                "strategy_group_id": group_id,
            },
        )

    funding_put_row = _lot_row_for_option_type(repo, "put")
    funding_put_id = str(funding_put_row["record_id"])
    record_manual_assignment(
        repo,
        lot_id=funding_put_id,
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        as_of_ms=2_000,
        request_id="combo-funding-put-assignment-1",
        wheel_start_enabled=True,
    )
    model = build_wheel_read_model(repo, "lx", 3_000)
    assigned_stock = model["assigned_stock_projection"]["_all_assigned_stock_lots"][0]
    residual_call = _lot_row_for_option_type(repo, "call")["fields"]
    residual_metadata = _lot_strategy_metadata(repo, str(residual_call["lot_id"]))

    assert model["batches"] == []
    assert model["wheel_branches"] == []
    assert assigned_stock["strategy_group_id"] == group_id
    assert assigned_stock["leg_role"] == "assigned_stock"
    assert assigned_stock["source_option_leg_role"] == "funding_put"
    assert residual_call["status"] == "open"
    assert residual_metadata["strategy_group_id"] == group_id
    assert residual_metadata["leg_role"] == "participation_call"


def test_assignment_replay_does_not_backfill_wheel_start(tmp_path: Path) -> None:
    repo, _put_lot_id, _lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=False,
    )

    assignment = next(
        TradeEvent.from_dict(item)
        for item in repo.list_trade_events()
        if item["event_type"] == "assignment"
    )
    _open_test_activation(repo)
    replay = persist_trade_event_objects_atomically(repo, [assignment])

    assert replay[0].created is False
    assert repo.list_wheel_events(account="lx") == []


def test_manual_assignment_runtime_boolean_does_not_authorize_wheel_lifecycle(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        broker="富途",
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100,
        multiplier=100,
        expiration_ymd="2026-08-21",
        premium_per_share=2.5,
        opened_at_ms=1_000,
        request_id="manual-open-for-wheel-rollback",
    )

    execute_manual_assignment(
        repo,
        lot_id=str(repo.list_position_lots()[0]["record_id"]),
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        dry_run=False,
        as_of_ms=2_000,
        request_id="configured-assignment-1",
        runtime_config={"wheel": {"accounts": ["lx"]}},
    )

    assert build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"] == []


def test_intent_creation_revalidates_current_ledger_share_coverage(
    tmp_path: Path,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]
    prior_decision = decision_state_snapshot(
        repo, account="lx", portfolio_scope_id=portfolio_scope_id("lx"),
        source_observed_at=datetime.now(timezone.utc).isoformat(), current_decision_now_ms=3_000,
    )
    _open_unlinked_call(repo, event_time_ms=3_500)
    snapshot = {
        "account": "lx",
        "snapshot_hash": "snapshot-stale",
        "strategy_policy_sha256": "b" * 64,
        "batches": [
            {
                "stock_lot_id": lot_id,
                "batch_generation_hash": batch["batch_generation_hash"],
                "final_candidate": {
                    "final_candidate_id": "candidate-stale",
                    "symbol": "NVDA",
                    "stock_lot_id": lot_id,
                    "strike": 110,
                    "expiration_ymd": "2026-08-21",
                    "granted_contracts": 1,
                    "multiplier": 100,
                },
            }
        ],
    }

    with pytest.raises(
        ValueError,
        match=(
            "batch generation changed|coverage is unavailable|coverage is insufficient|"
            "not ready|coverage settlement decision is unavailable"
        ),
    ):
        create_wheel_call_intent(
            repo,
            candidate_snapshot=snapshot,
            current_strategy_policy_sha256="b" * 64,
            account="lx",
            lot_id=lot_id,
            final_candidate_id="candidate-stale",
            expected_snapshot_hash="snapshot-stale",
            expected_batch_generation_hash=batch["batch_generation_hash"],
            expires_at_ms=10_000,
            request_id="intent-stale-1",
            actor="tester",
            coverage_fact={
                "account": "lx",
                "symbol": "NVDA",
                "capacity_identity_hash": "capacity-before-race",
                "decision_state_fingerprint": prior_decision["decision_state_fingerprint"],
                "source_observed_at": datetime.now(timezone.utc).isoformat(),
                "status": "available",
                "shares_eligible": 100,
                "shares_locked": 0,
                "shares_reserved": 0,
                "shares_available_for_cover": 100,
            },
            new_intent_enabled=True,
            account_configured=True,
            market="us",
            activation_descriptor={
                "market": "us",
                "account": "lx",
                "generation": 1,
                "activated_at_ms": 500,
                "deactivated_at_ms": None,
                "policy_hash": "a" * 64,
            },
            policy_sha256="a" * 64,
            apply_changes=True,
            as_of_ms=4_000,
        )
    assert not any(
        event["event_type"] == "wheel_call_intent_created"
        for event in repo.list_wheel_events(account="lx")
    )


def test_intent_creation_rejects_disabled_wheel(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path)

    with pytest.raises(ValueError, match="wheel_disabled"):
        _create_call_intent(repo, lot_id, new_intent_enabled=False)

    assert not any(
        event["event_type"] == "wheel_call_intent_created"
        for event in repo.list_wheel_events(account="lx")
    )


def test_intent_creation_rejects_removed_account_with_open_window(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="wheel_disabled: account_not_configured"):
        _create_call_intent(repo, lot_id, account_configured=False)

    assert repo.list_wheel_events(account="lx") == before


def test_intent_creation_rejects_closed_activation_without_effects(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=3_500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.close_wheel_activation_window(
            market="us",
            account="lx",
            expected_current_generation=1,
            policy_hash="a" * 64,
            request_id="test-wheel-deactivation",
            request_hash="c" * 64,
            conn=conn,
        )
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="wheel_disabled"):
        _create_call_intent(repo, lot_id)

    assert repo.list_wheel_events(account="lx") == before


def test_intent_creation_rejects_cross_market_branch_without_effects(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="wheel_disabled"):
        _create_call_intent(repo, lot_id, market="hk")

    assert repo.list_wheel_events(account="lx") == before


def test_wheel_call_intent_create_and_cancel(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    created, _coverage = _create_call_intent(repo, lot_id)
    pending = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms", return_value=5_500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.close_wheel_activation_window(
            market="us", account="lx", expected_current_generation=1,
            policy_hash="a" * 64, request_id="close-before-cancel",
            request_hash="c" * 64, conn=conn,
        )
    cancelled = cancel_wheel_call_intent(
        repo,
        account="lx",
        lot_id=lot_id,
        intent_id=created["intent_id"],
        expected_batch_generation_hash=pending["batch_generation_hash"],
        request_id="intent-cancel-1",
        actor="tester",
        broker_order_inactive_confirmed=True,
        reason="order cancelled",
        market="us",
        apply_changes=True,
        as_of_ms=6_000,
    )

    ready = build_wheel_read_model(repo, "lx", 7_000)["batches"][0]
    assert created["status"] == "created"
    assert pending["phase"] == "call_pending"
    assert pending["active_intent_reserved_shares"] == 100
    assert cancelled["status"] == "cancelled"
    assert ready["phase"] == "ready"
    assert ready["active_intent_ids"] == []
    intent_events = {
        event["event_type"]: event
        for event in repo.list_wheel_events(account="lx")
        if event["event_type"] in {
            "wheel_call_intent_created",
            "wheel_call_intent_cancelled",
        }
    }
    assert {
        event_type: event["event_schema_version"]
        for event_type, event in intent_events.items()
    } == {
        "wheel_call_intent_created": "wheel_event.v2",
        "wheel_call_intent_cancelled": "wheel_event.v2",
    }
    already_inactive = cancel_wheel_call_intent(
        repo,
        account="lx",
        lot_id=lot_id,
        intent_id=created["intent_id"],
        expected_batch_generation_hash=ready["batch_generation_hash"],
        request_id="intent-cancel-2",
        actor="tester",
        broker_order_inactive_confirmed=True,
        reason="already cancelled",
        market="us",
        apply_changes=True,
        as_of_ms=7_000,
    )
    assert already_inactive["status"] == "already_inactive"
    assert already_inactive["market"] == "us"


@pytest.mark.parametrize("apply_changes", [False, True])
def test_call_intent_cancel_rejects_cross_market_without_effects(
    tmp_path: Path,
    apply_changes: bool,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    created, _coverage = _create_call_intent(repo, lot_id)
    pending = build_wheel_read_model(repo, "lx", 5_000, market="us")["batches"][0]
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="resolve uniquely"):
        cancel_wheel_call_intent(
            repo,
            account="lx",
            lot_id=lot_id,
            intent_id=created["intent_id"],
            expected_batch_generation_hash=pending["batch_generation_hash"],
            request_id=f"cross-market-cancel-{apply_changes}",
            actor="tester",
            broker_order_inactive_confirmed=True,
            reason="wrong market",
            market="hk",
            apply_changes=apply_changes,
            as_of_ms=6_000,
        )

    assert repo.list_wheel_events(account="lx") == before






def _shared_linkage_scope(tmp_path, monkeypatch):
    from test_trade_attribution_view import _writable_call_scope
    from src.application.trades import attribution
    from src.application.ledger.api import read_trade_attribution_snapshot
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    context = dict(config=config, market="us", combo_evidence={"complete": True, "exposures": []},
                   capacity_observation={}, combo_mode="confirm")
    monkeypatch.setattr(attribution, "read_trade_attribution_context", lambda *a, **k: context)
    view = attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        account="lx", now_ms=4000, **context)
    fact = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "call")
    candidate = view["wheel_model"]["linkage_candidates"][0]
    args = dict(account="lx", call_lot_id=fact["lot_id"], lot_id=candidate["stock_lot_id"],
        linkage_candidate_id=candidate["linkage_candidate_id"], expected_input_hash=fact["input_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"], request_id="link-confirm",
        actor="tester", config=config, runtime_root=tmp_path)
    return repo, args, candidate


def test_manual_wheel_call_linkage_confirm_uses_narrow_adjust(tmp_path, monkeypatch):
    repo, args, candidate = _shared_linkage_scope(tmp_path, monkeypatch)
    before = repo.list_trade_events()
    assert not confirm_wheel_call_linkage(repo, **args)["write_applied"]
    assert repo.list_trade_events() == before
    result = confirm_wheel_call_linkage(repo, **args, apply_changes=True)
    assert result["status"] == "confirmed" and len(result["proof_event_ids"]) == 1
    assert not confirm_wheel_call_linkage(repo, **args, apply_changes=True)["write_applied"]
    adjust = next(row for row in repo.list_trade_events() if row["event_id"] in result["proof_event_ids"])
    patch = adjust["raw_payload"]["patch"]
    assert patch["strategy"] == "wheel" and patch["source_stock_lot_id"] == candidate["stock_lot_id"]
    assert adjust["contracts"] == 0 and float(adjust["price"]) == 0
    assert repo.list_trade_events()[:len(before)] == before
    assert build_wheel_read_model(repo, "lx", 6000)["batches"][0]["phase"] == "call_open"


def test_call_linkage_confirm_skips_put_candidates(tmp_path, monkeypatch):
    from src.application.trades import attribution

    repo, args, _candidate = _shared_linkage_scope(tmp_path, monkeypatch)
    build_view = attribution.build_trade_attribution_view
    put_candidate = {
        "direction": "put",
        "option_record_id": "unlinked-put-lot",
        "wheel_branch_id": "put-branch",
        "linkage_candidate_id": "put-candidate",
        "batch_generation_hash": "put-generation",
    }

    def mixed_view(*view_args, **view_kwargs):
        view = build_view(*view_args, **view_kwargs)
        view["wheel_model"]["linkage_candidates"] = [
            put_candidate,
            *view["wheel_model"]["linkage_candidates"],
        ]
        return view

    monkeypatch.setattr(attribution, "build_trade_attribution_view", mixed_view)

    result = confirm_wheel_call_linkage(repo, **args, apply_changes=True)

    assert result["status"] == "confirmed"
    assert result["write_applied"] is True


@pytest.mark.parametrize("failure", [None, "missing_candidate", "changed_input"])
def test_call_linkage_reject_skips_put_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    call_lot_id = _open_unlinked_call(repo)
    candidate = build_wheel_read_model(repo, "lx", 4_000)["linkage_candidates"][0]
    build_model = wheel_workflows.build_wheel_read_model_from_rows
    put_candidate = {
        "direction": "put",
        "option_record_id": "unlinked-put-lot",
        "wheel_branch_id": "put-branch",
        "linkage_candidate_id": "put-candidate",
        "batch_generation_hash": "put-generation",
    }

    def mixed_model(*model_args, **model_kwargs):
        model = build_model(*model_args, **model_kwargs)
        model["linkage_candidates"] = [
            put_candidate,
            *model["linkage_candidates"],
            {**put_candidate, "linkage_candidate_id": "put-candidate-2"},
        ]
        return model

    monkeypatch.setattr(wheel_workflows, "build_wheel_read_model_from_rows", mixed_model)
    before = repo.list_wheel_events(account="lx")
    kwargs = {
        "account": "lx",
        "call_lot_id": call_lot_id,
        "lot_id": lot_id,
        "linkage_candidate_id": (
            "missing-call-candidate"
            if failure == "missing_candidate"
            else candidate["linkage_candidate_id"]
        ),
        "expected_input_hash": (
            "old-input-hash"
            if failure == "changed_input"
            else candidate["input_snapshot_hash"]
        ),
        "expected_batch_generation_hash": candidate["batch_generation_hash"],
        "request_id": "mixed-linkage-reject",
        "actor": "tester",
        "reason": "not this Wheel batch",
        "market": "us",
        "apply_changes": True,
        "as_of_ms": 5_000,
    }

    if failure:
        message = (
            "candidate input changed"
            if failure == "changed_input"
            else "candidate is stale or unavailable"
        )
        with pytest.raises(ValueError, match=message):
            reject_wheel_call_linkage(repo, **kwargs)
        assert repo.list_wheel_events(account="lx") == before
    else:
        result = reject_wheel_call_linkage(repo, **kwargs)
        assert result["status"] == "rejected"
        assert result["write_applied"] is True
        assert len(repo.list_wheel_events(account="lx")) == len(before) + 1


@pytest.mark.parametrize("entry", ["cli", "tool_call", "tool_neutral"])
@pytest.mark.parametrize("runtime_source", ["argument", "environment"])
def test_public_linkage_entry_uses_complete_shared_decision(tmp_path, monkeypatch, entry, runtime_source):
    import sqlite3
    from src.application.trades import attribution
    from src.application.agent_tools import positions as position_tools
    from src.interfaces.cli import wheel as wheel_cli
    repo, args, candidate = _shared_linkage_scope(tmp_path, monkeypatch)
    runtime_root = tmp_path / "active-runtime"
    sqlite_path = runtime_root / "output_shared/state/option_positions.sqlite3"
    sqlite_path.parent.mkdir(parents=True)
    with sqlite3.connect(repo.db_path) as source, sqlite3.connect(sqlite_path) as target:
        source.backup(target)
    repo = SQLiteOptionPositionsRepository(sqlite_path)
    config_path = tmp_path / "configuration/config.us.json"
    data_path = tmp_path / "data-config/data.json"
    data_path.parent.mkdir()
    data_path.write_text("{}")
    args["config"]["portfolio"] = {"data_config": str(data_path)}
    monkeypatch.setattr(wheel_cli, "load_runtime_config", lambda **_: (config_path, args["config"]))
    monkeypatch.setattr(position_tools, "load_runtime_config", lambda **_: (config_path, args["config"]))
    context = attribution.read_trade_attribution_context(repo)
    observed_roots = []

    def read_context(*a, **kwargs):
        observed_roots.append(kwargs["runtime_root"])
        assert kwargs["runtime_root"] == runtime_root
        return context

    monkeypatch.setattr(attribution, "read_trade_attribution_context", read_context)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")
    payload = dict(config_key="us", account="lx", action="confirm", direction="call",
        wheel_branch_id=candidate["wheel_branch_id"], option_record_id=args["call_lot_id"],
        linkage_candidate_id=args["linkage_candidate_id"], expected_input_hash=args["expected_input_hash"],
        expected_batch_generation_hash=args["expected_batch_generation_hash"], request_id=args["request_id"], actor=args["actor"])
    if runtime_source == "argument":
        payload["runtime_root"] = str(runtime_root)
    else:
        monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))

    def invoke(apply):
        if entry == "cli":
            argv = ["linkage", "confirm"]
            for key, value in payload.items():
                if key != "action":
                    argv.extend(["--" + key.replace("_", "-"), str(value)])
            return wheel_cli.execute(wheel_cli.parse_args(argv + (["--apply", "--confirm"] if apply else [])))
        tool = position_tools.WHEEL_LINKAGE_TOOL
        selected = payload.copy()
        if entry == "tool_call":
            tool = position_tools.WHEEL_CALL_LINKAGE_TOOL
            for key in ("direction", "wheel_branch_id", "option_record_id"):
                selected.pop(key)
            selected.update(stock_lot_id=args["lot_id"], call_record_id=args["call_lot_id"])
        return tool.call({**selected, "apply": apply, "confirm": apply})[0]

    before = repo.list_trade_events()
    preview = invoke(False)
    assert preview["dry_run"] and not preview["write_applied"]
    assert repo.list_trade_events() == before
    applied = invoke(True)
    assert observed_roots
    assert applied["write_applied"] and applied["proof_event_ids"] == preview["proof_event_ids"]
    assert len(repo.list_trade_events()) == len(before) + 1
    monkeypatch.setattr(attribution, "read_trade_attribution_context", lambda *a, **k: pytest.fail("recovery must use persisted proof"))
    retried = invoke(True)
    assert not retried["write_applied"] and retried["proof_event_ids"] == applied["proof_event_ids"]
    assert len(repo.list_trade_events()) == len(before) + 1


@pytest.mark.parametrize("broken", ["local_hash", "capacity", "generation"])
def test_manual_linkage_uses_shared_fresh_evidence(tmp_path, monkeypatch, broken):
    from src.application.trades import attribution
    repo, args, candidate = _shared_linkage_scope(tmp_path, monkeypatch)
    before = repo.list_trade_events()
    if broken == "local_hash":
        args["expected_input_hash"] = candidate["input_snapshot_hash"]
    elif broken == "generation":
        args["expected_batch_generation_hash"] = "stale"
    else:
        monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
            lambda **_: {"status": "unavailable", "reason_codes": ["capacity_evidence_missing"]})
    with pytest.raises(ValueError):
        confirm_wheel_call_linkage(repo, **args, apply_changes=True)
    assert repo.list_trade_events() == before


def test_manual_wheel_call_linkage_rejects_only_selected_relation(
    tmp_path: Path,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    call_lot_id = _open_unlinked_call(repo)
    candidate = build_wheel_read_model(repo, "lx", 4_000)["linkage_candidates"][0]

    result = reject_wheel_call_linkage(
        repo,
        account="lx",
        call_lot_id=call_lot_id,
        lot_id=lot_id,
        linkage_candidate_id=candidate["linkage_candidate_id"],
        expected_input_hash=candidate["input_snapshot_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"],
        request_id="link-reject-1",
        actor="tester",
        reason="not this Wheel batch",
        market="us",
        apply_changes=True,
        as_of_ms=5_000,
    )

    model = build_wheel_read_model(repo, "lx", 6_000)
    assert result["status"] == "rejected"
    assert model["linkage_candidates"] == []
    assert repo.get_position_lot_fields(call_lot_id).get("strategy") is None
    assert model["batches"][0]["phase"] == "ready"
    rejection = next(
        event
        for event in repo.list_wheel_events(account="lx")
        if event["event_type"] == "wheel_call_linkage_rejected"
    )
    assert rejection["event_schema_version"] == "wheel_event.v2"


@pytest.mark.parametrize("apply_changes", [False, True])
@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_call_linkage_rejects_cross_market_without_effects(
    tmp_path: Path,
    action: str,
    apply_changes: bool,
    monkeypatch,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    call_lot_id = _open_unlinked_call(repo)
    candidate = build_wheel_read_model(repo, "lx", 4_000, market="us")[
        "linkage_candidates"
    ][0]
    before_events = repo.list_wheel_events(account="lx")
    before_trades = repo.list_trade_events()
    common = {
        "account": "lx",
        "call_lot_id": call_lot_id,
        "lot_id": lot_id,
        "linkage_candidate_id": candidate["linkage_candidate_id"],
        "expected_input_hash": candidate["input_snapshot_hash"],
        "expected_batch_generation_hash": candidate["batch_generation_hash"],
        "request_id": f"cross-market-linkage-{action}-{apply_changes}",
        "actor": "tester",
        "market": "hk",
        "apply_changes": apply_changes,
        "as_of_ms": 5_000,
    }

    with pytest.raises(ValueError, match="unavailable|no unique branch"):
        if action == "confirm":
            from src.application.trades import attribution
            monkeypatch.setattr(attribution, "read_trade_attribution_context", lambda *a, **k: {
                "market": "hk", "config": {"market": "hk"}, "combo_evidence": {"complete": True},
                "capacity_observation": {}, "combo_mode": "confirm"})
            confirm_wheel_call_linkage(repo,
                **{key: value for key, value in common.items() if key not in {"market", "as_of_ms"}},
                config={"market": "hk"}, runtime_root=tmp_path)
        else:
            reject_wheel_call_linkage(
                repo,
                **common,
                reason="wrong market",
            )

    assert repo.list_wheel_events(account="lx") == before_events
    assert repo.list_trade_events() == before_trades


def test_partial_wheel_call_assignment_keeps_batch_active(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path, contracts=2)
    call_lot_id = "wheel-call-lot-partial"
    _persist_wheel_call_open(
        repo,
        event_id="wheel-call-open-partial",
        lot_id=call_lot_id,
        source_stock_lot_id=lot_id,
    )
    _persist_wheel_call_assignment(
        repo,
        event_id="wheel-call-assignment-partial",
        lot_id=call_lot_id,
    )

    batch = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    branches = build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
    child = next(item for item in branches if item["direction"] == "put")
    assert batch["lifecycle_status"] == "active"
    assert batch["remaining_contracts"] == 1
    assert batch["shares_remaining"] == 100
    assert batch["phase"] == "ready"
    assert child["lifecycle_status"] == "pending_decision"


def test_wheel_call_assignment_closes_batch_in_same_transaction(
    tmp_path: Path,
) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    call_lot_id = "wheel-call-lot-1"
    _persist_wheel_call_open(
        repo,
        event_id="wheel-call-open-1",
        lot_id=call_lot_id,
        source_stock_lot_id=lot_id,
    )
    _persist_wheel_call_assignment(
        repo,
        event_id="call-assignment-1",
        lot_id=call_lot_id,
    )

    branches = build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
    parent = next(item for item in branches if item["direction"] == "call")
    child = next(item for item in branches if item["direction"] == "put")
    assert parent["lifecycle_status"] == "converted"
    assert parent["shares_remaining"] == 0
    assert parent["integrity_status"] == "trusted"
    assert child["lifecycle_status"] == "pending_decision"


def test_branch_start_rejects_removed_account_but_end_remains_available(tmp_path: Path) -> None:
    repo, lot_id = _wheel_repo(tmp_path)
    _persist_wheel_call_open(
        repo, event_id="wheel-call-open-for-decision", lot_id="wheel-call-for-decision",
        source_stock_lot_id=lot_id,
    )
    _persist_wheel_call_assignment(
        repo, event_id="wheel-call-assignment-for-decision", lot_id="wheel-call-for-decision",
    )
    child = next(
        branch for branch in build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
        if branch["direction"] == "put"
    )
    before = repo.list_wheel_events(account="lx")
    args = dict(
        account="lx", wheel_branch_id=child["wheel_branch_id"],
        expected_batch_generation_hash=child["batch_generation_hash"],
        request_id="removed-account-decision", actor="tester", market="us",
        activation_descriptor={
            "market": "us", "account": "lx", "generation": 1,
            "activated_at_ms": 500, "deactivated_at_ms": None,
            "policy_hash": "a" * 64,
        },
        account_configured=False, policy_sha256="a" * 64,
        apply_changes=False, as_of_ms=6_000,
    )

    preview = wheel_workflows.decide_wheel_branch(
        repo, decision="start", **{**args, "account_configured": True},
    )
    assert preview["status"] == "planned"
    with pytest.raises(ValueError, match="wheel_disabled: account_not_configured"):
        wheel_workflows.decide_wheel_branch(
            repo, decision="start", **{**args, "apply_changes": True},
        )
    assert repo.list_wheel_events(account="lx") == before

    ended = wheel_workflows.decide_wheel_branch(repo, decision="end", **args)
    assert ended["status"] == "planned"
    assert repo.list_wheel_events(account="lx") == before


def test_wheel_start_failure_rolls_back_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        broker="富途",
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100,
        multiplier=100,
        expiration_ymd="2026-08-21",
        premium_per_share=2.5,
        opened_at_ms=1_000,
        request_id="manual-open-for-wheel-rollback",
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    _open_test_activation(repo)

    def _fail(*_args: object, **_kwargs: object) -> bool:
        raise ValueError("forced Wheel companion failure")

    monkeypatch.setattr(repo, "append_wheel_event_once", _fail)
    with pytest.raises(ValueError, match="forced Wheel companion failure"):
        persist_trade_event_objects_atomically(
            repo,
            [
                TradeEvent(
                    event_id="put-assignment-rollback",
                    event_type="assignment",
                    event_time_ms=2_000,
                    contract_key=_nvda_put_leg_key(),
                    contracts=1,
                    price=0,
                    currency="USD",
                    source="test",
                    multiplier=100,
                    target_lot_id=put_lot_id,
                    raw_payload={
                        "side": "buy",
                        "target_lot_id": put_lot_id,
                        "stock_settlement": {
                            "side": "buy",
                            "shares": 100,
                            "price": 100,
                            "fees": 0,
                            "currency": "USD",
                            "fee_provenance": {
                                "basis": "actual",
                                "source": "test",
                            },
                        },
                    },
                )
            ],
        )

    assert [item["event_type"] for item in repo.list_trade_events()] == ["open"]
    assert repo.get_position_lot_fields(put_lot_id)["status"] == "open"
