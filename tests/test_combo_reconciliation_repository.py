from __future__ import annotations

import pytest

from domain.domain.combo_reconciliation import match_post_trade_combo_pairs
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository


BASE_TIME_MS = 1_785_312_000_000


def _lot(record_id: str, *, option_type: str, strike: int, trade_time_ms: int) -> dict:
    return {
        "record_id": record_id,
        "open_event_id": f"open-{record_id}",
        "account": "lx",
        "broker": "futu",
        "runtime_environment": "test",
        "market": "US",
        "market_date": "2026-07-31",
        "symbol": "NVDA",
        "option_type": option_type,
        "position_side": "short" if option_type == "put" else "long",
        "contracts_original": 1,
        "contracts_open": 1,
        "currency": "USD",
        "multiplier": 100,
        "strike": strike,
        "expiration_ymd": "2026-08-21",
        "trade_time_ms": trade_time_ms,
        "strategy": "",
        "strategy_group_id": "",
        "leg_role": "",
    }


def _event(lot: dict) -> TradeEvent:
    contract = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type=lot["option_type"],
        position_side=lot["position_side"],
        strike=lot["strike"],
        expiration_ymd=lot["expiration_ymd"],
    )
    return TradeEvent(
        event_id=lot["open_event_id"],
        event_type="open",
        event_time_ms=lot["trade_time_ms"],
        contract_key=contract,
        contracts=1,
        price=1,
        currency="USD",
        source="test",
        lot_id=lot["record_id"],
    )


def _proposal() -> tuple[list[dict], dict]:
    lots = [
        _lot(
            "put-1",
            option_type="put",
            strike=100,
            trade_time_ms=BASE_TIME_MS + 1_000,
        ),
        _lot(
            "call-1",
            option_type="call",
            strike=110,
            trade_time_ms=BASE_TIME_MS + 2_000,
        ),
    ]
    proposal = match_post_trade_combo_pairs(lots=lots)["inferences"][0]
    return lots, proposal


def test_inference_repository_upsert_is_idempotent_and_terminal_expiry_does_not_reopen(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    lots, proposal = _proposal()
    for lot in lots:
        assert repo.upsert_trade_event(_event(lot)) is True

    assert repo.upsert_combo_pair_inference(proposal) is True
    assert repo.upsert_combo_pair_inference(proposal) is False
    stored = repo.get_combo_pair_inference(proposal["inference_id"])
    assert stored is not None
    assert stored["status"] == "proposal_ready"
    assert stored["put_open_event_id"] == "open-put-1"
    assert stored["call_open_event_id"] == "open-call-1"

    expired = repo.expire_combo_pair_inferences(
        effective_now_ms=proposal["proposal_expires_at_ms"] + 1,
        account="lx",
    )
    assert expired == 1
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "expired_unresolved"

    assert repo.upsert_combo_pair_inference(proposal) is False
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "expired_unresolved"


def test_inference_repository_rejects_identity_drift(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    lots, proposal = _proposal()
    for lot in lots:
        repo.upsert_trade_event(_event(lot))
    repo.upsert_combo_pair_inference(proposal)

    drifted = {**proposal, "call_record_id": "different-call"}

    with pytest.raises(ValueError) as _caught:
        repo.upsert_combo_pair_inference(drifted)
    exc = _caught.value
    assert "identity conflict" in str(exc)
