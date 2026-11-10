from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger import decision_snapshot as snapshot_module
from src.application.ledger.manual_trades import (
    persist_manual_close_event,
    persist_manual_open_event,
)
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.queries import lifecycle_account_coherent_facts
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.void_repair import (
    lifecycle_effective_void_target_event_ids,
)
from src.application.ledger.writer_lifecycle_support import (
    _effective_void_target_ids,
)


def _contract_key() -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="AAPL",
        option_type="put",
        strike=150.0,
        expiration_ymd="2026-06-19",
    )


def _terminal_close(
    event_id: str, *, event_time_ms: int = 2000, **overrides: object
) -> TradeEvent:
    kwargs: dict[str, object] = {
        "event_id": event_id,
        "event_type": "close",
        "event_time_ms": event_time_ms,
        "contract_key": _contract_key(),
        "contracts": 1,
        "price": Decimal("0.5"),
        "currency": "USD",
        "source": "manual",
        "multiplier": 100,
        "target_lot_id": "lot_open-aapl",
        "raw_payload": {"side": "buy"},
    }
    kwargs.update(overrides)
    return TradeEvent(**kwargs)


def _repair_void(
    event_id: str,
    target_event_id: str,
    repair_event_id: str | None,
    *,
    event_time_ms: int = 4000,
) -> TradeEvent:
    raw_payload: dict[str, object] = {"side": "sell"}
    if repair_event_id is not None:
        raw_payload["repair_event_id"] = repair_event_id
    return TradeEvent(
        event_id=event_id,
        event_type="void",
        event_time_ms=event_time_ms,
        contract_key=_contract_key(),
        contracts=0,
        price=Decimal("0"),
        currency="USD",
        source="cli_trade_event_repair",
        multiplier=100,
        target_event_id=target_event_id,
        raw_payload=raw_payload,
    )


def test_excludes_a_close_reproduced_by_its_repair() -> None:
    original = _terminal_close("close-original")
    replacement = _terminal_close("close-repair", event_time_ms=3000)
    void = _repair_void("void-original", "close-original", "close-repair")

    assert lifecycle_effective_void_target_event_ids(
        [original.to_dict(), replacement.to_dict(), void.to_dict()]
    ) == ()


def test_keeps_a_void_without_a_live_replacement() -> None:
    original = _terminal_close("close-original")
    void = _repair_void("void-original", "close-original", "close-missing")

    assert lifecycle_effective_void_target_event_ids(
        [original.to_dict(), void.to_dict()]
    ) == ("close-original",)


def test_keeps_a_void_whose_original_is_absent() -> None:
    void = _repair_void("void-original", "close-missing", "close-repair")
    replacement = _terminal_close("close-repair", event_time_ms=3000)

    assert lifecycle_effective_void_target_event_ids(
        [void.to_dict(), replacement.to_dict()]
    ) == ("close-missing",)


@pytest.mark.parametrize(
    "replacement_overrides",
    [
        {"contracts": 2},
        {"target_lot_id": "lot-other"},
        {"event_type": "expire_close"},
        {"contract_key": replace(_contract_key(), strike=Decimal("155"))},
    ],
    ids=["contracts", "lot", "event_type", "contract"],
)
def test_keeps_a_void_whose_repair_differs(
    replacement_overrides: dict[str, object],
) -> None:
    original = _terminal_close("close-original")
    replacement = _terminal_close(
        "close-repair", event_time_ms=3000, **replacement_overrides
    )
    void = _repair_void("void-original", "close-original", "close-repair")

    assert lifecycle_effective_void_target_event_ids(
        [original.to_dict(), replacement.to_dict(), void.to_dict()]
    ) == ("close-original",)


def test_follows_a_repaired_repair_chain() -> None:
    original = _terminal_close("close-original")
    first_repair = _terminal_close("close-repair-1", event_time_ms=3000)
    second_repair = _terminal_close("close-repair-2", event_time_ms=5000)
    first_void = _repair_void("void-1", "close-original", "close-repair-1")
    second_void = _repair_void(
        "void-2", "close-repair-1", "close-repair-2", event_time_ms=6000
    )

    assert lifecycle_effective_void_target_event_ids(
        [
            original.to_dict(),
            first_repair.to_dict(),
            first_void.to_dict(),
            second_void.to_dict(),
            second_repair.to_dict(),
        ]
    ) == ()


def test_keeps_a_chain_whose_repair_is_itself_dead() -> None:
    original = _terminal_close("close-original")
    first_repair = _terminal_close("close-repair-1", event_time_ms=3000)
    first_void = _repair_void("void-1", "close-original", "close-repair-1")
    second_void = _repair_void("void-2", "close-repair-1", None, event_time_ms=6000)

    assert lifecycle_effective_void_target_event_ids(
        [
            original.to_dict(),
            first_repair.to_dict(),
            first_void.to_dict(),
            second_void.to_dict(),
        ]
    ) == ("close-original", "close-repair-1")


def test_keeps_a_close_with_contradictory_repairs() -> None:
    original = _terminal_close("close-original")
    first_replacement = _terminal_close("close-repair-1", event_time_ms=3000)
    second_replacement = _terminal_close("close-repair-2", event_time_ms=5000)
    first_void = _repair_void("void-1", "close-original", "close-repair-1")
    second_void = _repair_void(
        "void-2", "close-original", "close-repair-2", event_time_ms=6000
    )

    assert lifecycle_effective_void_target_event_ids(
        [
            original.to_dict(),
            first_replacement.to_dict(),
            second_replacement.to_dict(),
            first_void.to_dict(),
            second_void.to_dict(),
        ]
    ) == ("close-original",)


def test_keeps_a_close_with_a_plain_void_beside_its_repair() -> None:
    original = _terminal_close("close-original")
    replacement = _terminal_close("close-repair", event_time_ms=3000)
    plain_void = _repair_void("void-plain", "close-original", None)
    repair_void = _repair_void(
        "void-repair", "close-original", "close-repair", event_time_ms=5000
    )

    assert lifecycle_effective_void_target_event_ids(
        [
            original.to_dict(),
            replacement.to_dict(),
            plain_void.to_dict(),
            repair_void.to_dict(),
        ]
    ) == ("close-original",)


def test_accepts_two_voids_naming_the_same_repair() -> None:
    original = _terminal_close("close-original")
    replacement = _terminal_close("close-repair", event_time_ms=3000)
    first_void = _repair_void("void-1", "close-original", "close-repair")
    second_void = _repair_void(
        "void-2", "close-original", "close-repair", event_time_ms=5000
    )

    assert lifecycle_effective_void_target_event_ids(
        [original.to_dict(), replacement.to_dict(), first_void.to_dict(), second_void.to_dict()]
    ) == ()


def test_keeps_a_self_referential_repair() -> None:
    original = _terminal_close("close-original")
    void = _repair_void("void-original", "close-original", "void-original")

    assert lifecycle_effective_void_target_event_ids(
        [original.to_dict(), void.to_dict()]
    ) == ("close-original",)


def test_ignores_rows_that_cannot_be_decoded() -> None:
    original = _terminal_close("close-original")
    replacement = _terminal_close("close-repair", event_time_ms=3000)
    void = _repair_void("void-original", "close-original", "close-repair")

    assert lifecycle_effective_void_target_event_ids(
        [{"event_id": "not-canonical"}, original.to_dict(), replacement.to_dict(), void.to_dict()]
    ) == ()


class _EventListRepo:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def list_trade_events(self, *, conn: object | None = None) -> list[dict]:
        return list(self._events)


def test_writer_void_targets_share_the_repair_rule() -> None:
    original = _terminal_close("close-original")
    replacement = _terminal_close("close-repair", event_time_ms=3000)
    void = _repair_void("void-original", "close-original", "close-repair")
    events = [original.to_dict(), replacement.to_dict(), void.to_dict()]

    assert _effective_void_target_ids(_EventListRepo(events), conn=object()) == ()


def _open_and_repaired_close(
    tmp_path: Path,
) -> tuple[SQLiteOptionPositionsRepository, str]:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    persist_manual_open_event(
        repo,
        broker="futu",
        account="lx",
        symbol="0700.HK",
        option_type="put",
        side="short",
        contracts=2,
        currency="HKD",
        strike=440,
        multiplier=100,
        expiration_ymd="2026-07-30",
        premium_per_share=3,
        opened_at_ms=1000,
        request_id="fixture-open",
    )
    lot = repo.list_position_lots()[0]
    persist_manual_close_event(
        repo,
        lot_id=lot["record_id"],
        fields=dict(lot["fields"]),
        contracts_to_close=1,
        close_price=1.0,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    original = repo.list_trade_events()[-1]
    original_id = str(original["event_id"])
    replacement_payload = dict(original)
    replacement_payload["event_id"] = "close-repair-1"
    assert repo.upsert_trade_event(TradeEvent.from_dict(replacement_payload))
    void = TradeEvent.from_dict(
        {
            **original,
            "event_id": "void-1",
            "event_type": "void",
            "contracts": 0,
            "price": "0",
            "target_event_id": original_id,
            "raw_payload": {"side": "sell", "repair_event_id": "close-repair-1"},
        }
    )
    assert repo.upsert_trade_event(void)
    return repo, original_id


def test_coherent_account_facts_drop_a_repaired_close(
    tmp_path: Path,
) -> None:
    repo, original_id = _open_and_repaired_close(tmp_path)

    facts = lifecycle_account_coherent_facts(repo, account="lx")

    assert original_id not in facts["effective_void_event_ids"]
    assert facts["effective_void_event_ids"] == []


def test_decision_snapshot_drops_a_repaired_close(
    tmp_path: Path,
) -> None:
    repo, original_id = _open_and_repaired_close(tmp_path)
    rows = snapshot_module.read_decision_state_rows_many(
        repo, accounts=["lx"]
    )["lx"]
    # The raw repair writes bypass the publisher, so align the stored lot
    # snapshot with the reprojected events before taking the snapshot.
    projected = {
        lot.lot_id: lot.fields
        for lot in project_stored_trade_events_to_position_lots(
            repo.list_trade_events()
        ).lots
    }
    for stored in rows["stored_position_lots"]:
        if stored["record_id"] in projected:
            stored["fields"] = dict(projected[stored["record_id"]])
    rows["account_lifecycle_allocations"] = [
        {
            "case_id": "case-x",
            "allocation_id": "alloc-1",
            "evidence_id": "ev-1",
            "target_lot_id": rows["stored_position_lots"][0]["record_id"],
            "canonical_terminal_event_id": original_id,
            "terminal_type": "expire_close",
            "contracts_allocated": 1,
        }
    ]

    snapshot = snapshot_module.decision_state_snapshot_from_rows(
        rows,
        account="lx",
        portfolio_scope_id="lx",
        source_observed_at="2026-08-10T03:00:00+00:00",
    )

    assert snapshot["snapshot_status"] == "trusted"
    assert original_id not in snapshot["effective_void_event_ids"]
    assert snapshot["effective_void_event_ids"] == []
