from __future__ import annotations

from datetime import datetime, timezone
import json
from unittest.mock import patch

import pandas as pd
import pytest

from conftest import phase2_opening_row
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_contract_identity import derive_trade_side
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel.read_model import build_wheel_read_model
from src.application.wheel.scanning import run_wheel_put_scan
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


def _open_activation(repo: SQLiteOptionPositionsRepository) -> None:
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.open_wheel_activation_window(
            market="us",
            account="lx",
            expected_current_generation=0,
            policy_hash="a" * 64,
            request_id="test-activation",
            request_hash="b" * 64,
            conn=conn,
        )


def _put_event(
    *,
    event_id: str,
    event_type: str,
    multiplier: int | float,
    raw_payload: dict,
    contracts: int = 1,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=1_000 if event_type == "open" else 2_000,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            strike=100,
            expiration_ymd="2026-08-21",
        ),
        contracts=contracts,
        price=2.5 if event_type == "open" else 0,
        currency="USD",
        source="test",
        multiplier=multiplier,
        lot_id="put-lot" if event_type == "open" else None,
        target_lot_id="put-lot" if event_type == "assignment" else None,
        # §9.2 step 3: the contract key no longer carries the position side, so the
        # short put side travels as the trade side instead.
        raw_payload={**raw_payload, "side": derive_trade_side(event_type, "short") or ""},
    )


def _assignment_payload(
    multiplier: int | float,
    *,
    contracts: int = 1,
    actual_fee: bool = True,
) -> dict:
    settlement = {
        "side": "buy",
        "shares": int(multiplier * contracts),
        "price": 100,
        "fees": 0,
        "currency": "USD",
    }
    if actual_fee:
        settlement["fee_provenance"] = {"basis": "actual", "source": "test"}
    return {"target_lot_id": "put-lot", "stock_settlement": settlement}


def _broker_assignment_payload(
    multiplier: int,
    *,
    actual_fee: bool,
) -> dict:
    payload = _assignment_payload(multiplier, actual_fee=actual_fee)
    payload.update(
        source_type="broker_settlement_pair",
        source_event_id="option-close|stock-settlement",
    )
    payload["stock_settlement"].update(
        source_event_id="stock-settlement",
        futu_account_id="1001",
        order_id="stock-order",
        symbol="NVDA",
    )
    return payload


def _trusted_multiplier_payload(event_id: str, **extra: object) -> dict[str, object]:
    return {
        "source_type": "broker_trade_event",
        "source_deal_id": event_id,
        "external_event_key": f"futu:{event_id}",
        "multiplier_source": "payload",
        **extra,
    }


def _resolver_multiplier_payload(
    event_id: str,
    *,
    evidence_multiplier: int = 10,
    include_receipt: bool = True,
) -> dict[str, object]:
    evidence = {
        "schema_version": "contract_multiplier_evidence.v1",
        "source": "opend",
        "canonical_symbol": "NVDA",
        "multiplier": evidence_multiplier,
        **(
            {"source_receipt_sha256": canonical_sha256({"rows": ["NVDA"]})}
            if include_receipt
            else {}
        ),
    }
    return _trusted_multiplier_payload(
        event_id,
        multiplier_source="opend",
        multiplier_evidence=evidence,
        multiplier_evidence_hash=canonical_sha256(evidence),
    )


def test_actual_nonstandard_multiplier_creates_exact_wheel_child(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload=_trusted_multiplier_payload("put-open"),
        )],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_assignment_payload(10),
        )],
    )[0].to_dict()

    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert result["wheel_event_id"]
    assert "wheel_manual_review_reason" not in result
    assert branch["multiplier"] == 10
    assert branch["shares_opened"] == 10


def test_ordinary_covered_call_assignment_bootstraps_active_put_branch(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="stock-source-open",
            event_type="open",
            multiplier=100,
            raw_payload=_trusted_multiplier_payload("stock-source-open"),
        )],
    )
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="stock-source-assignment",
            event_type="assignment",
            multiplier=100,
            raw_payload=_assignment_payload(100),
        )],
    )
    stock_lot_id = "assigned-stock-stock-source-assignment"
    call_key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        strike=110,
        expiration_ymd="2026-09-18",
        )
    persist_trade_event_objects_atomically(
        repo,
        [TradeEvent(
            event_id="covered-call-open",
            event_type="open",
            event_time_ms=3_000,
            contract_key=call_key,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=100,
            lot_id="covered-call-lot",
            raw_payload=_trusted_multiplier_payload(
                "covered-call-open",
                strategy="cc",
                leg_role="covered_call",
                source_stock_lot_id=stock_lot_id,
                side="sell",
            ),
        )],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [TradeEvent(
            event_id="covered-call-assignment",
            event_type="assignment",
            event_time_ms=4_000,
            contract_key=call_key,
            contracts=1,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            target_lot_id="covered-call-lot",
            raw_payload={
                "side": "buy",
                "target_lot_id": "covered-call-lot",
                "stock_settlement": {
                    "side": "sell",
                    "shares": 100,
                    "price": 110,
                    "fees": 1,
                    "currency": "USD",
                    "fee_provenance": {"basis": "actual", "source": "test"},
                },
            },
        )],
    )[0].to_dict()

    assert result.get("wheel_manual_review_reason") is None
    model = build_wheel_read_model(
        repo,
        "lx",
        5_000,
        monitoring_readiness={"monitoring_gate": "enabled"},
    )
    branch = model["wheel_branches"][0]
    assert result["wheel_event_id"]
    assert branch["direction"] == "put"
    assert branch["lifecycle_status"] == "active"
    assert branch["principal_anchor"] == "10999.000000"
    assert branch["realized_put_net_pnl_in_current_stage"] == 0.0

    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-05-06",
            "dte": 35,
            "contract_symbol": "NVDA-PUT-99",
            "multiplier": 100,
            "currency": "USD",
            "strike": 99,
            "spot": 100,
            "bid": 2.0,
            "ask": 2.2,
            "last_price": 2.1,
            "mid": 2.1,
            "open_interest": 500,
            "volume": 50,
            "implied_volatility": 0.30,
            "term_matched_rv": 0.20,
            "delta": -0.30,
        }
    )
    scan = run_wheel_put_scan(
        model,
        {
            "enabled_for_new_lifecycle": True,
            "min_dte": 30,
            "max_dte": 45,
            "min_abs_delta": 0.25,
            "max_abs_delta": 0.35,
            "min_open_interest": 0,
            "min_volume": 0,
            "min_annualized_net_premium_return": 0.10,
            "min_net_premium_cny": 50,
            "max_spread_ratio": 0.40,
            "min_iv_rv_ratio": 1.10,
            "min_iv_minus_rv": 0.05,
        },
        {"frames": {"NVDA": pd.DataFrame([row])}},
        {
            "exchange_rate_converter": CurrencyConverter(
                ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
            ),
            "stock_assignment_fee_fact_fn": lambda *_args: {
                "basis": "actual",
                "amount": 0,
            },
        },
        decision_time_ms=int(
            datetime(2026, 4, 1, 15, tzinfo=timezone.utc).timestamp() * 1000
        ),
    )
    assert scan["scope_results"][0]["reason_code"] == "candidates_found", scan
    assert scan["capacity_claims"][0]["wheel_branch_id"] == branch["wheel_branch_id"]


def test_ordinary_call_on_active_wheel_stock_requires_manual_review(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload=_trusted_multiplier_payload("put-open"),
        )],
    )
    _open_activation(repo)
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_assignment_payload(10),
        )],
    )
    call_key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        strike=110,
        expiration_ymd="2026-09-18",
        )
    persist_trade_event_objects_atomically(
        repo,
        [TradeEvent(
            event_id="ordinary-call-open",
            event_type="open",
            event_time_ms=3_000,
            contract_key=call_key,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=10,
            lot_id="ordinary-call-lot",
            raw_payload=_trusted_multiplier_payload(
                "ordinary-call-open",
                strategy="cc",
                leg_role="covered_call",
                source_stock_lot_id="assigned-stock-put-assignment",
                side="sell",
            ),
        )],
    )

    result = persist_trade_event_objects_atomically(
        repo,
        [TradeEvent(
            event_id="ordinary-call-assignment",
            event_type="assignment",
            event_time_ms=4_000,
            contract_key=call_key,
            contracts=1,
            price=0,
            currency="USD",
            source="test",
            multiplier=10,
            target_lot_id="ordinary-call-lot",
            raw_payload={
                "side": "buy",
                "target_lot_id": "ordinary-call-lot",
                "stock_settlement": {
                    "side": "sell",
                    "shares": 10,
                    "price": 110,
                    "fees": 1,
                    "currency": "USD",
                    "fee_provenance": {"basis": "actual", "source": "test"},
                },
            },
        )],
    )[0].to_dict()

    branches = build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
    assert result["created"] is True
    assert result["wheel_manual_review_reason"] == (
        "ordinary_cc_overlaps_active_wheel_stock"
    )
    assert [branch["direction"] for branch in branches] == ["call"]


def test_unproven_multiplier_creates_visible_blocked_child(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload={},
        )],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_assignment_payload(10),
        )],
    )[0].to_dict()

    assert result["created"] is True
    assert result["wheel_manual_review_reason"] == "multiplier_unproven"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]
    assert any(row["event_id"] == "put-assignment" for row in repo.list_trade_events())


@pytest.mark.parametrize(
    "payload",
    [
        _resolver_multiplier_payload("put-open", include_receipt=False),
        _resolver_multiplier_payload("put-open", evidence_multiplier=100),
    ],
)
def test_unbound_resolver_multiplier_creates_visible_blocked_child(
    tmp_path,
    payload,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload=payload,
        )],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_assignment_payload(10),
        )],
    )[0].to_dict()

    assert result["wheel_manual_review_reason"] == "multiplier_unproven"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]


def test_bound_resolver_multiplier_creates_wheel_child(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload=_resolver_multiplier_payload("put-open"),
        )],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_assignment_payload(10),
        )],
    )[0].to_dict()

    assert result["wheel_event_id"]
    assert "wheel_manual_review_reason" not in result


def test_arbitrary_multiplier_source_is_not_trusted(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [
            _put_event(
                event_id="put-open",
                event_type="open",
                multiplier=10,
                raw_payload=_trusted_multiplier_payload(
                    "put-open",
                    multiplier_source="broker",
                ),
            )
        ],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [
            _put_event(
                event_id="put-assignment",
                event_type="assignment",
                multiplier=10,
                raw_payload=_assignment_payload(10),
            )
        ],
    )[0].to_dict()

    assert result["wheel_manual_review_reason"] == "multiplier_unproven"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]


def test_fractional_multiplier_is_rejected_before_assignment(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError, match="whole number"):
        _put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10.5,
            raw_payload=_assignment_payload(10.5),
        )

    assert not any(
        row["event_type"] == "assignment" for row in repo.list_trade_events()
    )


def test_batched_legacy_call_assignments_use_rolling_stock_state(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [
            _put_event(
                event_id="put-open",
                event_type="open",
                multiplier=100,
                contracts=2,
                raw_payload=_trusted_multiplier_payload("put-open"),
            )
        ],
    )
    _open_activation(repo)
    persist_trade_event_objects_atomically(
        repo,
        [
            _put_event(
                event_id="put-assignment",
                event_type="assignment",
                multiplier=100,
                contracts=2,
                raw_payload=_assignment_payload(100, contracts=2),
            )
        ],
    )
    stock_lot_id = "assigned-stock-put-assignment"
    call_key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        strike=110,
        expiration_ymd="2026-09-18",
        )
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=f"call-open-{index}",
                event_type="open",
                event_time_ms=3_000 + index,
                contract_key=call_key,
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=f"call-lot-{index}",
                raw_payload=_trusted_multiplier_payload(
                    f"call-open-{index}",
                    strategy="wheel",
                    leg_role="wheel_call",
                    source_stock_lot_id=stock_lot_id,
                    side="sell",
                ),
            )
            for index in (1, 2)
        ],
    )

    results = persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=f"call-assignment-{index}",
                event_type="assignment",
                event_time_ms=4_000 + index,
                contract_key=call_key,
                contracts=1,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=f"call-lot-{index}",
                raw_payload={
                    "case_id": "call-assignment-case",
                    "evidence_id": "call-assignment-evidence",
                    "side": "buy",
                    "target_lot_id": f"call-lot-{index}",
                    "stock_settlement_source": {
                        "side": "sell",
                        "shares": 200,
                        "price": 110,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    },
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
            for index in (1, 2)
        ],
    )

    model = build_wheel_read_model(repo, "lx", 5_000)
    parent = next(branch for branch in model["wheel_branches"] if branch["direction"] == "call")
    children = [branch for branch in model["wheel_branches"] if branch["direction"] == "put"]
    assert all(result.to_dict().get("wheel_event_id") for result in results)
    assert parent["lifecycle_status"] == "converted"
    assert parent["shares_remaining"] == 0
    assert len(children) == 2
    assert {child["lifecycle_status"] for child in children} == {"pending_decision"}


def test_unproven_assignment_fee_creates_visible_blocked_child(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload=_trusted_multiplier_payload("put-open"),
        )],
    )
    _open_activation(repo)

    result = persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_assignment_payload(10, actual_fee=False),
        )],
    )[0].to_dict()

    assert result["created"] is True
    assert result["wheel_manual_review_reason"] == "assignment_cash_facts_unavailable"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]


def test_broker_assignment_refreshes_wheel_evidence_after_fee_sync(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=10,
            raw_payload={},
        )],
    )
    _open_activation(repo)
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=10,
            raw_payload=_broker_assignment_payload(10, actual_fee=False),
        )],
    )

    blocked = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert blocked["reason_codes"] == ["assignment_cash_facts_unavailable"]
    assert (
        repo.list_wheel_events(account="lx")[0]["payload"]["principal_anchor"]
        is None
    )

    with repo._writer_connection(begin_immediate=True) as conn:
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id = ?",
            ("put-assignment",),
        ).fetchone()
        event = json.loads(row["event_json"])
        event["raw_payload"]["stock_settlement"]["fee_provenance"] = {
            "basis": "actual",
            "source": "test fee sync",
        }
        conn.execute(
            "UPDATE trade_events SET event_json = ? WHERE event_id = ?",
            (json.dumps(event, ensure_ascii=False, sort_keys=True), "put-assignment"),
        )

    ready = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert ready["reason_codes"] == []
    assert ready["phase"] == "ready"
    assert ready["multiplier"] == 10
    assert ready["principal_anchor"] == "1000.000000"


def test_lifecycle_allocation_writer_creates_blocked_assignment_branch(tmp_path):
    from tests.test_settlement_observation import (
        _repo_with_pending_case, _collect_stock_settlement_observation,
        reconcile_lifecycle_close_reason,
    )
    repo, lifecycle_case, policy, _anchor = _repo_with_pending_case(tmp_path)
    _open_activation(repo)
    observation, now_ms = _collect_stock_settlement_observation(
        repo, lifecycle_case=lifecycle_case, policy=policy, stock_deal_id="wheel-stock-settlement",
    )
    result = reconcile_lifecycle_close_reason(
        repo, case_id=lifecycle_case["case_id"], now_ms=now_ms,
        observation=observation, apply_changes=True,
    )
    assert result["poll_settlement_results"][0]["status"] == "applied"
    branch = build_wheel_read_model(repo, "lx", now_ms)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert branch["reason_codes"] == ["assignment_cash_facts_unavailable"]
    assert len(repo.list_wheel_events()) == 1
