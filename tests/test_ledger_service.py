from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _position_fields(
    *,
    record_id: str = "lot_put_may",
    strike: float = 450.0,
    expiration_ymd: str = "2026-05-28",
    contracts_open: int = 6,
) -> dict[str, Any]:
    from domain.domain.option_position_lots import parse_exp_to_ms

    exp_ms = parse_exp_to_ms(expiration_ymd)
    assert exp_ms is not None
    return {
        "record_id": record_id,
        "position_id": record_id,
        "status": "open",
        "contracts": contracts_open,
        "contracts_open": contracts_open,
        "contracts_closed": 0,
        "broker": "富途",
        "account": "sy",
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "short",
        "currency": "HKD",
        "strike": strike,
        "multiplier": 100,
        "expiration": exp_ms,
        "source_event_id": "open-put-may",
    }


def _open_event_from_fields(fields: dict[str, Any], *, event_id: str = "open-put-may") -> dict[str, Any]:
    from domain.domain.ledger.position_fields import effective_expiration_ymd

    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=1000,
        contract_key=ContractKey.from_values(
            broker=fields.get("broker"),
            account=fields.get("account"),
            underlying_symbol=fields.get("symbol"),
            option_type=fields.get("option_type"),
            position_side=fields.get("side"),
            strike=fields.get("strike"),
            expiration_ymd=effective_expiration_ymd(fields),
        ),
        contracts=int(fields.get("contracts") or fields.get("contracts_open") or 0),
        price=1.0,
        currency=str(fields.get("currency") or "HKD"),
        source="test_seed_open_lot",
        multiplier=float(fields.get("multiplier") or 100),
        lot_id=str(fields.get("record_id") or ""),
        raw_payload={"source_type": "test_seed"},
    ).to_dict()


def test_manual_open_ledger_service_projects_new_lot(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.commands import persist_manual_open_event_with_ledger

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    result = persist_manual_open_event_with_ledger(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="sy",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=6,
            currency="HKD",
            strike=450.0,
            multiplier=100,
            expiration_ymd="2026-05-28",
            premium_per_share=8.0,
            opened_at_ms=1000,
        ),
    )

    assert result.ledger_preflight["status"] == "ok"
    assert result.ledger_preflight["event_type"] == "open"
    assert result.ledger_preflight["contracts_open_before"] == 0
    assert result.ledger_preflight["contracts_open_after"] == 6
    assert result.ledger_preflight["position_contracts_open_after"] == 6
    assert result.result["created"] is True
    assert result.result["record_id"] == result.ledger_preflight["target_lot_id"]
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["record_id"] == result.ledger_preflight["target_lot_id"]
    assert lots[0]["fields"]["contracts_open"] == 6


def test_manual_close_ledger_service_closes_exact_lot(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.commands import persist_manual_close_event_with_ledger

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="sy",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=6,
            currency="HKD",
            strike=450.0,
            multiplier=100,
            expiration_ymd="2026-05-28",
            premium_per_share=8.0,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    result = persist_manual_close_event_with_ledger(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=2,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )

    assert result.ledger_preflight["status"] == "ok"
    assert result.ledger_preflight["target_lot_id"] == lot["record_id"]
    assert result.ledger_preflight["contracts_open_before"] == 6
    assert result.ledger_preflight["contracts_open_after"] == 4
    assert result.result["created"] is True
    lots = repo.list_position_lots()
    assert lots[0]["record_id"] == lot["record_id"]
    assert lots[0]["fields"]["contracts_open"] == 4
    assert repo.list_trade_events()[-1]["raw_payload"]["record_id"] == lot["record_id"]


def test_manual_adjust_ledger_service_targets_exact_lot(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.commands import persist_manual_adjust_event_with_ledger

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="sy",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=6,
            currency="HKD",
            strike=450.0,
            multiplier=100,
            expiration_ymd="2026-05-28",
            premium_per_share=8.0,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    result = persist_manual_adjust_event_with_ledger(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts=5,
        premium_per_share=8.5,
        as_of_ms=2000,
    )

    assert result.ledger_preflight["status"] == "ok"
    assert result.ledger_preflight["event_type"] == "adjust"
    assert result.ledger_preflight["target_lot_id"] == lot["record_id"]
    assert result.ledger_preflight["contracts_open_before"] == 6
    assert result.ledger_preflight["contracts_open_after"] == 5
    assert result.result["created"] is True
    adjusted = repo.get_record_fields(lot["record_id"])
    assert adjusted["contracts"] == 5
    assert adjusted["contracts_open"] == 5
    assert adjusted["premium"] == 8.5
    assert repo.list_trade_events()[-1]["raw_payload"]["record_id"] == lot["record_id"]


def test_manual_close_ledger_preflight_rejects_target_identity_mismatch() -> None:
    from src.application.ledger.errors import LedgerPreflightError
    from src.application.ledger.preflight import preflight_manual_close

    current_fields = _position_fields(strike=450.0)
    projected_fields = _position_fields(strike=451.0)

    class MismatchedRepo:
        def get_record_fields(self, record_id: str) -> dict[str, Any]:
            assert record_id == "lot_put_may"
            return dict(current_fields)

        def list_position_lots(self) -> list[dict[str, Any]]:
            return [{"record_id": "lot_put_may", "fields": dict(projected_fields)}]

        def list_trade_events(self) -> list[dict[str, Any]]:
            return [_open_event_from_fields(projected_fields)]

    with pytest.raises(LedgerPreflightError) as exc_info:
        preflight_manual_close(
            MismatchedRepo(),
            record_id="lot_put_may",
            contracts_to_close=1,
            close_price=1.2,
            close_reason="manual_buy_to_close",
            as_of_ms=2000,
        )

    assert exc_info.value.code == "target_contract_mismatch"
    assert exc_info.value.details["record_id"] == "lot_put_may"


def test_manual_close_ledger_preflight_rejects_duplicate_lot_snapshot() -> None:
    from src.application.ledger.errors import LedgerPreflightError
    from src.application.ledger.preflight import preflight_manual_close

    fields = _position_fields()

    class DuplicateRepo:
        def get_record_fields(self, record_id: str) -> dict[str, Any]:
            assert record_id == "lot_put_may"
            return dict(fields)

        def list_position_lots(self) -> list[dict[str, Any]]:
            return [
                {"record_id": "lot_put_may", "fields": dict(fields)},
                {"record_id": "lot_put_may", "fields": dict(fields)},
            ]

        def list_trade_events(self) -> list[dict[str, Any]]:
            return [
                _open_event_from_fields(fields, event_id="open-put-may"),
                _open_event_from_fields(fields, event_id="open-put-may"),
            ]

    with pytest.raises(LedgerPreflightError) as exc_info:
        preflight_manual_close(
            DuplicateRepo(),
            record_id="lot_put_may",
            contracts_to_close=1,
            close_price=1.2,
            close_reason="manual_buy_to_close",
            as_of_ms=2000,
    )

    assert exc_info.value.code == "ledger_shadow_invalid"
    error_items = [
        *exc_info.value.details.get("import_errors", []),
        *exc_info.value.details.get("projection_errors", []),
    ]
    error_codes = {item["code"] for item in error_items}
    assert "duplicate_event_id" in error_codes or "duplicate_lot_id" in error_codes
