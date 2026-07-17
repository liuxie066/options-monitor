from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.combo_pairing import execute_staggered_combo_yield_pairing


def _open_lot(
    repo: SQLiteOptionPositionsRepository,
    *,
    option_type: str,
    side: str,
    strike: float,
    expiration_ymd: str,
    symbol: str = "NVDA",
    account: str = "lx",
    contracts: int = 1,
    multiplier: int = 100,
    currency: str = "USD",
) -> str:
    result = persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account=account,
            symbol=symbol,
            option_type=option_type,
            side=side,
            contracts=contracts,
            currency=currency,
            strike=strike,
            multiplier=multiplier,
            expiration_ymd=expiration_ymd,
            premium_per_share=2.5 if option_type == "put" else 1.0,
            opened_at_ms=1000 if option_type == "put" else 2000,
        ),
    )
    record_id = str(result.record_id or "")
    assert record_id
    return record_id


def _pair_lots(repo: SQLiteOptionPositionsRepository, **overrides: Any) -> tuple[str, str]:
    put_kwargs = {
        "option_type": "put",
        "side": "short",
        "strike": 100.0,
        "expiration_ymd": "2026-08-21",
    }
    call_kwargs = {
        "option_type": "call",
        "side": "long",
        "strike": 140.0,
        "expiration_ymd": "2026-12-18",
    }
    put_kwargs.update(overrides.get("put", {}))
    call_kwargs.update(overrides.get("call", {}))
    return _open_lot(repo, **put_kwargs), _open_lot(repo, **call_kwargs)


def test_staggered_combo_yield_pairing_dry_run_writes_nothing(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    put_id, call_id = _pair_lots(repo)
    before_events = repo.list_trade_events()

    out = execute_staggered_combo_yield_pairing(
        repo,
        put_record_id=put_id,
        call_record_id=call_id,
        pair_intent_id="intent-nvda-20260717-1",
        dry_run=True,
    )

    assert out["mode"] == "dry_run"
    assert out["strategy_group_id"] == "combo_yield:lx:intent-nvda-20260717-1"
    assert out["put"]["patch"]["leg_role"] == "funding_put"
    assert out["call"]["patch"]["leg_role"] == "participation_call"
    assert repo.list_trade_events() == before_events


def test_staggered_combo_yield_pairing_applies_both_lots_atomically(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    put_id, call_id = _pair_lots(repo)

    out = execute_staggered_combo_yield_pairing(
        repo,
        put_record_id=put_id,
        call_record_id=call_id,
        pair_intent_id="intent-nvda-20260717-1",
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert len(repo.list_trade_events()) == 4
    lots = {str(item["record_id"]): item["fields"] for item in repo.list_position_lots()}
    group_id = "combo_yield:lx:intent-nvda-20260717-1"
    assert lots[put_id]["strategy"] == "combo_yield"
    assert lots[put_id]["leg_role"] == "funding_put"
    assert lots[put_id]["strategy_group_id"] == group_id
    assert lots[put_id]["strategy_snapshot"]["pair_intent_id"] == "intent-nvda-20260717-1"
    assert lots[put_id]["strategy_snapshot"]["counterpart_record_id"] == call_id
    assert lots[call_id]["strategy"] == "combo_yield"
    assert lots[call_id]["leg_role"] == "participation_call"
    assert lots[call_id]["strategy_group_id"] == group_id
    assert lots[call_id]["strategy_snapshot"]["counterpart_record_id"] == put_id


def test_staggered_combo_yield_pairing_is_idempotent_after_confirmation(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    put_id, call_id = _pair_lots(repo)
    execute_staggered_combo_yield_pairing(
        repo,
        put_record_id=put_id,
        call_record_id=call_id,
        pair_intent_id="intent-idempotent",
        dry_run=False,
    )
    before_events = repo.list_trade_events()

    out = execute_staggered_combo_yield_pairing(
        repo,
        put_record_id=put_id,
        call_record_id=call_id,
        pair_intent_id="intent-idempotent",
        dry_run=False,
    )

    assert out["mode"] == "already_paired"
    assert out["put"]["patch"] == {}
    assert out["call"]["patch"] == {}
    assert repo.list_trade_events() == before_events


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"call": {"symbol": "AMD"}}, "same canonical symbol"),
        ({"call": {"account": "sy"}}, "same account"),
        ({"put": {"side": "long"}}, "funding Put must be an open short put"),
        ({"call": {"side": "short"}}, "participation Call must be an open long call"),
        ({"put": {"expiration_ymd": "2026-12-18"}}, "Put expiration earlier"),
        ({"put": {"strike": 150.0}}, "Put strike lower"),
        ({"call": {"contracts": 2}}, "exactly 1 open Put contract and 1 open Call contract"),
        ({"call": {"multiplier": 10}}, "multipliers must match"),
    ],
)
def test_staggered_combo_yield_pairing_rejects_invalid_relation(
    tmp_path: Path,
    overrides: dict[str, dict[str, Any]],
    expected: str,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    put_id, call_id = _pair_lots(repo, **overrides)

    with pytest.raises(ValueError, match=expected):
        execute_staggered_combo_yield_pairing(
            repo,
            put_record_id=put_id,
            call_record_id=call_id,
            pair_intent_id="intent-invalid",
            dry_run=True,
        )


def test_staggered_combo_yield_pairing_rolls_back_if_projection_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    put_id, call_id = _pair_lots(repo)
    before_events = repo.list_trade_events()

    def fail_replace(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("projection write failed")

    monkeypatch.setattr(repo, "replace_position_lots", fail_replace)

    with pytest.raises(RuntimeError, match="projection write failed"):
        execute_staggered_combo_yield_pairing(
            repo,
            put_record_id=put_id,
            call_record_id=call_id,
            pair_intent_id="intent-rollback",
            dry_run=False,
        )

    assert repo.list_trade_events() == before_events


def test_staggered_combo_yield_pairing_rejects_reusing_intent_for_other_lots(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "positions.sqlite3")
    first_put_id, first_call_id = _pair_lots(repo)
    execute_staggered_combo_yield_pairing(
        repo,
        put_record_id=first_put_id,
        call_record_id=first_call_id,
        pair_intent_id="intent-shared",
        dry_run=False,
    )
    second_put_id = _open_lot(
        repo,
        option_type="put",
        side="short",
        strike=95.0,
        expiration_ymd="2026-09-18",
    )
    second_call_id = _open_lot(
        repo,
        option_type="call",
        side="long",
        strike=150.0,
        expiration_ymd="2027-01-15",
    )

    with pytest.raises(ValueError, match="already assigned to another position lot"):
        execute_staggered_combo_yield_pairing(
            repo,
            put_record_id=second_put_id,
            call_record_id=second_call_id,
            pair_intent_id="intent-shared",
            dry_run=True,
        )
