from __future__ import annotations

from dataclasses import replace

from domain.domain.option_position_lots import parse_exp_to_ms
from src.application.ledger.migration import (
    reconcile_position_lot_snapshot,
    shadow_replay_position_lot_snapshot,
)


def _position_lot(
    *,
    record_id: str,
    option_type: str,
    strike: float,
    expiration_ymd: str,
    contracts_open: int,
) -> dict:
    exp_ms = parse_exp_to_ms(expiration_ymd)
    assert exp_ms is not None
    return {
        "record_id": record_id,
        "fields": {
            "record_id": record_id,
            "position_id": record_id,
            "status": "open",
            "contracts": contracts_open,
            "contracts_open": contracts_open,
            "contracts_closed": 0,
            "broker": "futu",
            "account": "sy",
            "symbol": "0700.HK",
            "option_type": option_type,
            "side": "short",
            "currency": "HKD",
            "strike": strike,
            "multiplier": 100,
            "expiration": exp_ms,
            "opened_at": 1,
            "premium": 1.0,
            "note": "",
        },
    }


def test_shadow_replay_position_lot_snapshot_supports_0700_fixture() -> None:
    records = [
        _position_lot(record_id="lot_call_may", option_type="call", strike=510, expiration_ymd="2026-05-28", contracts_open=2),
        _position_lot(record_id="lot_put_may", option_type="put", strike=450, expiration_ymd="2026-05-28", contracts_open=6),
        _position_lot(record_id="lot_put_jun", option_type="put", strike=450, expiration_ymd="2026-06-29", contracts_open=3),
    ]

    result = shadow_replay_position_lot_snapshot(records)

    assert result.source_record_count == 3
    assert result.imported_event_count == 3
    assert result.import_diagnostics == []
    assert result.projection.diagnostics == []
    assert result.reconciliation is not None
    assert result.reconciliation.issues == []
    assert {lot.lot_id: lot.contracts_open for lot in result.projection.lots} == {
        "lot_call_may": 2,
        "lot_put_may": 6,
        "lot_put_jun": 3,
    }


def test_reconciliation_reports_quantity_mismatch() -> None:
    records = [
        _position_lot(record_id="lot_put_may", option_type="put", strike=450, expiration_ymd="2026-05-28", contracts_open=6)
    ]
    result = shadow_replay_position_lot_snapshot(records)
    mismatched_lot = replace(result.projection.lots[0], contracts_open=5)
    mutated_projection = result.projection.__class__(
        lots=[mismatched_lot],
        views=[],
        diagnostics=[],
    )

    report = reconcile_position_lot_snapshot(records, mutated_projection)

    assert [item.code for item in report.issues] == ["quantity_mismatch"]
