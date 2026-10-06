from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from unittest.mock import patch

import pandas as pd
import pytest

from conftest import phase2_opening_row
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.cash_facts import broker_settlement_multiplier_evidence
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


def _call_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        strike=110,
        expiration_ymd="2026-09-18",
    )


def _persist_put_open(
    repo: SQLiteOptionPositionsRepository,
    *,
    multiplier: int | float,
    raw_payload: dict,
    contracts: int = 1,
) -> None:
    persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-open",
            event_type="open",
            multiplier=multiplier,
            contracts=contracts,
            raw_payload=raw_payload,
        )],
    )


def _persist_put_assignment(
    repo: SQLiteOptionPositionsRepository,
    *,
    multiplier: int | float,
    raw_payload: dict,
    contracts: int = 1,
) -> dict:
    return persist_trade_event_objects_atomically(
        repo,
        [_put_event(
            event_id="put-assignment",
            event_type="assignment",
            multiplier=multiplier,
            contracts=contracts,
            raw_payload=raw_payload,
        )],
    )[0].to_dict()


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
    _persist_put_open(repo, multiplier=10, raw_payload=_trusted_multiplier_payload("put-open"))
    _open_activation(repo)

    result = _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10))

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
    lot_id = "assigned-stock-stock-source-assignment"
    call_key = _call_key()
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
                source_stock_lot_id=lot_id,
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


def _cc_lp_events(*, long_at: int = 1_100) -> tuple[TradeEvent, TradeEvent, TradeEvent]:
    group_id = "combo_yield:lx:cc-lp"
    call_key = _call_key()
    put_key = ContractKey.from_values(
        broker="富途", account="lx", underlying_symbol="NVDA",
        option_type="put", strike=100, expiration_ymd="2026-09-18",
    )
    metadata = {"strategy": "combo_yield", "strategy_group_id": group_id}
    call = TradeEvent(
        event_id="combo-call-open", event_type="open", event_time_ms=1_000,
        contract_key=call_key, contracts=1, price=2, currency="USD",
        source="test", multiplier=10, lot_id="combo-call-lot",
        raw_payload={**_trusted_multiplier_payload("combo-call-open"), **metadata,
                     "leg_role": "short_call", "side": "sell"},
    )
    put = TradeEvent(
        event_id="combo-put-open", event_type="open", event_time_ms=long_at,
        contract_key=put_key, contracts=1, price=1, currency="USD",
        source="test", multiplier=10, lot_id="combo-put-lot",
        raw_payload={**metadata, "leg_role": "long_put", "side": "buy"},
    )
    assignment = TradeEvent(
        event_id="combo-call-assignment", event_type="assignment", event_time_ms=2_000,
        contract_key=call_key, contracts=1, price=0, currency="USD", source="test",
        multiplier=10, target_lot_id="combo-call-lot",
        raw_payload={"side": "buy", "target_lot_id": "combo-call-lot", "stock_settlement": {
            "side": "sell", "shares": 10, "price": 110, "fees": 0,
            "currency": "USD", "fee_provenance": {"basis": "actual", "source": "test"},
        }},
    )
    return call, put, assignment


@pytest.mark.parametrize("batched", [False, True])
def test_external_cc_lp_short_call_assignment_starts_put_branch(tmp_path, batched):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_activation(repo)
    call, put, assignment = _cc_lp_events()
    if batched:
        persist_trade_event_objects_atomically(repo, [call, put, assignment])
    else:
        persist_trade_event_objects_atomically(repo, [call, put])
        persist_trade_event_objects_atomically(repo, [assignment])
    branches = repo.list_wheel_events(account="lx")
    assert len(branches) == 1
    assert branches[0]["source_trade_event_id"] == assignment.event_id
    model = build_wheel_read_model(repo, "lx", 3_000)
    assert model["wheel_branches"][0]["direction"] == "put"
    assert model["wheel_branches"][0]["lifecycle_status"] == "active"
    assert next(row for row in repo.list_position_lots() if row["record_id"] == "combo-put-lot")["fields"]["status"] == "open"
    assert persist_trade_event_objects_atomically(repo, [assignment])[0].created is False
    assert len(repo.list_wheel_events(account="lx")) == 1


def test_external_cc_lp_late_long_put_does_not_backdate_identity(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_activation(repo)
    call, put, assignment = _cc_lp_events(long_at=3_000)
    persist_trade_event_objects_atomically(repo, [call, put])
    result = persist_trade_event_objects_atomically(repo, [assignment])[0].to_dict()
    assert not repo.list_wheel_events(account="lx")
    assert result["wheel_manual_review_reason"] == "combo_assignment_membership_unresolved"


def test_external_cc_lp_same_batch_known_void_blocks_assignment(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_activation(repo)
    call, put, assignment = _cc_lp_events()
    void = TradeEvent(
        multiplier=put.multiplier,
        event_id="void-combo-put", event_type="void", event_time_ms=3_000,
        contract_key=put.contract_key, contracts=0, price=0, currency="USD",
        source="test", target_event_id=put.event_id,
    )
    result = persist_trade_event_objects_atomically(
        repo, [call, put, assignment, void],
    )
    assert not repo.list_wheel_events(account="lx")
    assert result[2].to_dict()["wheel_manual_review_reason"] == "combo_assignment_membership_unresolved"


def test_external_cc_lp_does_not_reuse_active_wheel_stock(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10, raw_payload=_trusted_multiplier_payload("put-open"))
    _open_activation(repo)
    _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10))
    call, put, assignment = _cc_lp_events()
    call = replace(call, event_time_ms=3_000)
    put = replace(put, event_time_ms=3_100)
    assignment = replace(assignment, event_time_ms=4_000)
    persist_trade_event_objects_atomically(repo, [call, put])
    result = persist_trade_event_objects_atomically(repo, [assignment])[0].to_dict()
    assert result["wheel_manual_review_reason"] == "combo_cc_overlaps_active_wheel_stock"
    assert len(repo.list_wheel_events(account="lx")) == 1


@pytest.mark.parametrize("reverse_input", [False, True])
def test_cc_lp_same_batch_checks_earlier_assignment_wheel_stock(tmp_path, reverse_input):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_activation(repo)
    put_open = _put_event(
        event_id="put-open", event_type="open", multiplier=10,
        raw_payload=_trusted_multiplier_payload("put-open"),
    )
    put_assignment = _put_event(
        event_id="put-assignment", event_type="assignment", multiplier=10,
        raw_payload=_assignment_payload(10),
    )
    call, long_put, call_assignment = _cc_lp_events()
    call = replace(call, event_time_ms=3_000)
    long_put = replace(long_put, event_time_ms=3_100)
    call_assignment = replace(call_assignment, event_time_ms=4_000)
    events = [put_open, put_assignment, call, long_put, call_assignment]
    results = persist_trade_event_objects_atomically(
        repo, list(reversed(events)) if reverse_input else events,
    )
    reasons = {item.event_id: item.to_dict().get("wheel_manual_review_reason")
               for item in results}
    assert reasons[call_assignment.event_id] == "combo_cc_overlaps_active_wheel_stock"
    assert len(repo.list_wheel_events(account="lx")) == 1


def test_external_cc_lp_rejects_wrong_stock_side_even_without_fee(tmp_path):
    from src.application.ledger.wheel_trade_companions import plan_wheel_assignment_companion

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_activation(repo)
    call, put, assignment = _cc_lp_events()
    persist_trade_event_objects_atomically(repo, [call, put])
    raw = dict(assignment.raw_payload)
    raw["stock_settlement"] = {**raw["stock_settlement"], "side": "buy", "fees": None}
    assignment = replace(assignment, raw_payload=raw)
    rows = repo.read_lifecycle_account_rows(account="lx")
    fields = repo.get_position_lot_fields("combo-call-lot")
    window = repo.get_wheel_activation_window_for_event(market="us", account="lx", occurred_at_ms=2_000)
    assert plan_wheel_assignment_companion(
        assignment, fields, rows, window, recorded_at_ms=2_100,
    ) == (None, "stock_settlement_side_mismatch")


def test_ordinary_call_on_active_wheel_stock_requires_manual_review(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10, raw_payload=_trusted_multiplier_payload("put-open"))
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
    call_key = _call_key()
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
    _persist_put_open(repo, multiplier=10, raw_payload={})
    _open_activation(repo)

    result = _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10))

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
    _persist_put_open(repo, multiplier=10, raw_payload=payload)
    _open_activation(repo)

    result = _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10))

    assert result["wheel_manual_review_reason"] == "multiplier_unproven"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]


def test_bound_resolver_multiplier_creates_wheel_child(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10, raw_payload=_resolver_multiplier_payload("put-open"))
    _open_activation(repo)

    result = _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10))

    assert result["wheel_event_id"]
    assert "wheel_manual_review_reason" not in result


def test_arbitrary_multiplier_source_is_not_trusted(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10,
                      raw_payload=_trusted_multiplier_payload( "put-open", multiplier_source="broker", ))
    _open_activation(repo)

    result = _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10))

    assert result["wheel_manual_review_reason"] == "multiplier_unproven"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]


def test_fractional_multiplier_is_rejected_before_assignment(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = _put_event(
        event_id="put-assignment",
        event_type="assignment",
        multiplier=10.5,
        raw_payload=_assignment_payload(10.5),
    )
    assert event.multiplier == 10.5  # Historical nodes retain the invalid source.
    with pytest.raises(ValueError, match="event_multiplier_invalid"):
        persist_trade_event_objects_atomically(repo, [event])

    assert not any(
        row["event_type"] == "assignment" for row in repo.list_trade_events()
    )


def test_batched_legacy_call_assignments_use_rolling_stock_state(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=100, raw_payload=_trusted_multiplier_payload("put-open"), contracts=2)
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
    lot_id = "assigned-stock-put-assignment"
    call_key = _call_key()
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
                    source_stock_lot_id=lot_id,
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
    called_away = [
        event
        for event in repo.list_wheel_events(account="lx")
        if event["event_type"] == "wheel_called_away"
    ]
    assert len(called_away) == 1
    assert {event["event_schema_version"] for event in called_away} == {
        "wheel_event.v1"
    }


def test_unproven_assignment_fee_creates_visible_blocked_child(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10, raw_payload=_trusted_multiplier_payload("put-open"))
    _open_activation(repo)

    result = _persist_put_assignment(repo, multiplier=10, raw_payload=_assignment_payload(10, actual_fee=False))

    assert result["created"] is True
    assert result["wheel_manual_review_reason"] == "assignment_cash_facts_unavailable"
    assert len(repo.list_wheel_events(account="lx")) == 1
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert branch["phase"] == "data_unavailable"
    assert result["wheel_manual_review_reason"] in branch["reason_codes"]


def test_broker_assignment_refreshes_wheel_evidence_after_fee_sync(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10, raw_payload={})
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


def test_broker_assignment_without_stock_order_id_keeps_multiplier_proof(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _persist_put_open(repo, multiplier=10, raw_payload={})
    _open_activation(repo)
    payload = _broker_assignment_payload(10, actual_fee=True)
    del payload["stock_settlement"]["order_id"]
    assignment = _put_event(
        event_id="put-assignment",
        event_type="assignment",
        multiplier=10,
        raw_payload=payload,
    )
    evidence = broker_settlement_multiplier_evidence(assignment)
    assert evidence is not None
    assert evidence["order_id"] is None
    assert evidence["multiplier"] == 10
    assert broker_settlement_multiplier_evidence(
        replace(
            assignment,
            raw_payload={
                **assignment.raw_payload,
                "stock_settlement": {
                    **payload["stock_settlement"],
                    "order_id": " stock-order ",
                },
            },
        )
    )["order_id"] == " stock-order "
    assert broker_settlement_multiplier_evidence(
        replace(assignment, raw_payload={**assignment.raw_payload, "source_event_id": "other"})
    ) is None

    result = persist_trade_event_objects_atomically(repo, [assignment])[0].to_dict()
    branch = build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"][0]
    assert result["wheel_event_id"]
    assert branch["phase"] == "ready"
    assert branch["multiplier"] == 10


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


def test_lifecycle_allocation_writer_enters_wheel_from_cc_lp_assignment(tmp_path):
    from tests.test_settlement_observation import (
        _Gateway, _collect_broker_observation, _repo_with_pending_case,
        reconcile_lifecycle_close_reason,
    )
    from src.application.trades.settlement_observation import (
        LifecycleObservationGenerationChanged,
    )
    from src.application.trades.manual_lifecycle_resolution import resolve_lifecycle_manually

    call, put, _ = _cc_lp_events()
    call = replace(
        call, event_time_ms=1_700_000_000_000, multiplier=100,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="call", strike=110, expiration_ymd="2026-08-21",
        ),
    )
    put = replace(
        put, event_time_ms=1_700_000_000_100, multiplier=100,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="put", strike=100, expiration_ymd="2026-08-21",
        ),
    )
    repo, lifecycle_case, policy, _ = _repo_with_pending_case(
        tmp_path, opening_event=call, option_code="US.NVDA260821C110000",
    )
    persist_trade_event_objects_atomically(repo, [put])
    _open_activation(repo)
    stock_time_ms = int(policy["settlement_deadline_ms"]) - 1
    now_ms = stock_time_ms + 2
    observation = _collect_broker_observation(
        repo, lifecycle_case=lifecycle_case, case_id=lifecycle_case["case_id"],
        now_ms=now_ms,
        gateway=_Gateway(history_deals=[
            {"deal_id": "option-close-1", "acc_id": "1001",
             "code": "US.NVDA260821C110000", "price": "0", "qty": 1},
            {"deal_id": "combo-cc-stock-settlement", "acc_id": "1001",
             "code": "US.NVDA", "price": "110", "qty": 100,
             "trd_side": "SELL", "trade_time_ms": stock_time_ms,
             "order_id": "combo-cc-stock-order"},
        ]),
    )
    assert observation["stock_settlement_present"] is True
    result = reconcile_lifecycle_close_reason(
        repo, case_id=lifecycle_case["case_id"], now_ms=now_ms,
        observation=observation, apply_changes=True,
    )
    assert result["poll_settlement_results"][0]["status"] == "applied"
    wheel_events = repo.list_wheel_events(account="lx")
    assert len(wheel_events) == 1
    assert wheel_events[0]["event_type"] == "wheel_branch_created"
    assignment = next(
        event for event in repo.list_trade_events()
        if event["event_id"] == wheel_events[0]["source_trade_event_id"]
    )
    assert assignment["event_type"] == "assignment"
    assert assignment["contract_key"]["option_type"] == "call"
    branch = build_wheel_read_model(repo, "lx", now_ms)["wheel_branches"][0]
    assert branch["direction"] == "put"
    assert branch["lifecycle_status"] == "active"
    assert next(row for row in repo.list_position_lots()
                if row["record_id"] == "combo-put-lot")["fields"]["status"] == "open"
    with pytest.raises(LifecycleObservationGenerationChanged):
        reconcile_lifecycle_close_reason(
            repo, case_id=lifecycle_case["case_id"], now_ms=now_ms,
            observation=observation, apply_changes=True,
        )
    assert len(repo.list_wheel_events(account="lx")) == 1

    replacement_ref = "futu:lx:1001:replacement-stock"
    repo.insert_trade_lifecycle_evidence_once({
        "evidence_id": "replacement-stock-evidence",
        "case_id": None,
        "source_type": "futu_broker_deal",
        "source_event_id": replacement_ref,
        "evidence_type": "stock_settlement_leg",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "side": "sell",
        "stock_qty": 100,
        "stock_price": 110,
        "trade_time_ms": stock_time_ms + 10,
        "order_id": "replacement-stock-order",
    })
    revision = repo.get_trade_lifecycle_case(lifecycle_case["case_id"])["derived_summary"]["resolution_revision"]
    corrected = resolve_lifecycle_manually(
        repo, case_id=lifecycle_case["case_id"], expected_revision=revision,
        reason="assignment", broker_ref=replacement_ref, note="correct stock deal",
        void_terminal_event_id=assignment["event_id"], apply_changes=True,
        now_ms=stock_time_ms + 20,
    )
    assert corrected["status"] == "applied"
    assert len(repo.list_wheel_events(account="lx")) == 2
    branches = build_wheel_read_model(repo, "lx", stock_time_ms + 20)["wheel_branches"]
    assert len([branch for branch in branches
                if branch["lifecycle_status"] == "active"
                and "wheel_branch_source_invalid" not in branch["reason_codes"]]) == 1
    assert resolve_lifecycle_manually(
        repo, case_id=lifecycle_case["case_id"], expected_revision=revision,
        reason="assignment", broker_ref=replacement_ref, note="correct stock deal",
        void_terminal_event_id=assignment["event_id"], apply_changes=True,
        now_ms=stock_time_ms + 20,
    )["status"] == "idempotent"
    assert len(repo.list_wheel_events(account="lx")) == 2
