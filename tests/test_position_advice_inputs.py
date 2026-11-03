from datetime import datetime, timezone

import pytest

from src.application.position_advice_inputs import advice_position_key, normalize_manual_positions, position_economics_sha256, select_ledger_positions

NOW = datetime(2026, 7, 29, 15, tzinfo=timezone.utc)


def manual(**overrides):
    return {"input_id": "user-1", "input_as_of_utc": "2026-07-29T14:00:00Z", "symbol": "NVDA", "option_type": "put", "expiration": "2026-08-28", "strike": 100, "contracts_open": 1, "premium": 2, "currency": "USD", "multiplier": 100, **overrides}


def normalize(rows):
    return normalize_manual_positions(rows, account="sample", broker="富途", now_utc=NOW)


def test_manual_multiple_preserves_distinct_identity_and_missing_cost():
    rows = normalize([manual(), manual(input_id="user-2", symbol="AAPL", premium=None)])
    assert len(rows) == 2
    assert rows[1]["premium"] is None
    assert rows[0]["input_source"] == "user_provided"
    assert advice_position_key(rows[0]) == "user:user-1"
    assert all("lot_id" not in row and "record_id" not in row for row in rows)
    assert position_economics_sha256(rows[0]) != position_economics_sha256(rows[1])


@pytest.mark.parametrize("fields", [
    {"input_as_of_utc": "2026-07-30T00:00:00Z"}, {"input_as_of_utc": "2026-07-29T14:00:00"},
    {"lot_id": "fake"}, {"account": "other"}, {"broker": "长桥"}, {"currency": "HKD"},
    {"side": "long"}, {"option_type": "stock"}, {"strike": True}, {"contracts_open": 0}, {"multiplier": 1.5},
    {"premium": float("nan")}, {"input_id": ""}, {"expiration": "not-a-date"},
])
def test_manual_rejects_unsafe_or_invalid_identity(fields):
    with pytest.raises(ValueError):
        normalize([manual(**fields)])


def test_duplicate_manual_ids_rejected():
    with pytest.raises(ValueError, match="unique"):
        normalize([manual(), manual()])


def test_ledger_selection_intersection_and_account_broker_isolation():
    from test_close_advice_required_data import _position
    records = [_position(account="sample", lot_id="wanted"), _position(account="other", lot_id="other"), _position(account="sample", lot_id="lb", broker="长桥")]
    positions, selected = select_ledger_positions({"symbols": ["NVDA"], "lot_ids": ["wanted"], "contracts": [{"symbol": "NVDA", "option_type": "put", "expiration": "2026-08-28", "strike": 100}]}, records=records, account="sample", broker="富途", now_utc=NOW)
    assert [row["lot_id"] for row in positions] == ["wanted"]
    assert selected == [records[0]]
    assert positions[0]["input_source"] == "ledger"
    with pytest.raises(ValueError, match="scope"):
        select_ledger_positions({"lot_ids": ["other"]}, records=records, account="sample", broker="富途", now_utc=NOW)
