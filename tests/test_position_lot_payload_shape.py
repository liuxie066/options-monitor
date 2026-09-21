"""Slice `payload-shape-converge` (write side): the published shape is `to_dict()`.

The binding statement is `write-side-definition.md` §1 -- the write side's
production for `position_lots.fields_json` is **exactly** the key set and shape of
`PositionLot.to_dict()`: 17 keys for an option lot, plus `shares_opened` /
`shares_open` / `shares_closed` / `cost_basis_total` for a stock lot. Not one key
more, not one fewer.

Everything here is written through the real projection path
(`project_stored_trade_events_to_position_lots`, the same entry bootstrap, combo,
current-decision and `verify-projection` share) rather than hand-built records, so
the controls pin the producer and not a test-local copy of it.

The last test is the plan's pre-window read-only assertion: it needs the
production `.backup` copy and skips without it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
    expiration_timestamp_to_ymd,
)
from domain.domain.ledger import ContractKey, PositionLot, TradeEvent
from domain.domain.option_position_identity import parse_exp_to_ms
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.publisher import (
    project_stored_trade_events_to_position_lots,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.ledger.repository_common import (
    _position_lot_contract_scalars,
    _position_lot_storage_values,
)

#: The production read-only copy the pre-window assertion is defined against.
READ_ONLY_COPY = Path("/tmp/om-readonly-local.sqlite3")

#: `write-side-definition.md` §1's target key set for an option lot.
OPTION_TARGET_KEYS = frozenset(
    {
        "lot_id",
        "open_event_id",
        "contract_key",
        "position_side",
        "position_key",
        "opened_at_ms",
        "contracts_opened",
        "contracts_open",
        "contracts_closed",
        "status",
        "premium_open",
        "multiplier",
        "currency",
        "realized_pnl",
        "last_event_id",
        "close_event_ids",
        "asset_type",
    }
)

#: The four keys a stock lot adds on top (§7.3 shares vocabulary).
STOCK_TARGET_KEYS = OPTION_TARGET_KEYS | {
    "shares_opened",
    "shares_open",
    "shares_closed",
    "cost_basis_total",
}


def _option_key(
    *,
    strike: float = 100.0,
    expiration_ymd: str = "2026-06-19",
    option_type: str = "put",
) -> ContractKey:
    return ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type=option_type,
        strike=strike,
        expiration_ymd=expiration_ymd,
    )


def _open_event(
    *,
    event_id: str = "open-nvda",
    lot_id: str = "lot_open-nvda",
    raw_payload: dict[str, object] | None = None,
    **kwargs: object,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=1000,
        contract_key=_option_key(**kwargs),  # type: ignore[arg-type]
        contracts=2,
        price=1.23,
        currency="USD",
        source="cli_manual_open",
        multiplier=100,
        lot_id=lot_id,
        raw_payload={
            "source": "cli_manual_open",
            "source_type": "manual_trade_event",
            "side": "sell",
            **(raw_payload or {}),
        },
    )


def _published_fields(events: list[TradeEvent]) -> dict[str, object]:
    projection = project_stored_trade_events_to_position_lots(events)
    assert projection.diagnostics == []
    assert len(projection.lots) == 1
    return projection.lots[0].fields


def test_option_payload_keys_are_exactly_the_lot_key_set() -> None:
    """§1: the published option payload has `to_dict()`'s keys and no others."""
    fields = _published_fields([_open_event()])

    lot = PositionLot.from_open_event(_open_event(), lot_id="lot_open-nvda")

    assert set(fields) == set(lot.to_dict()) == OPTION_TARGET_KEYS
    # The keys the assembly used to carry and §2 retires: a read model key set on
    # top of the lot, the flat contract scalars, the close-patch family, the
    # note provenance line, and the strategy metadata family.
    for retired in (
        "account",
        "broker",
        "symbol",
        "option_type",
        "strike",
        "expiration",
        "expiration_ymd",
        "side",
        "contracts",
        "opened_at",
        "premium",
        "last_action_at",
        "source_event_id",
        "position_id",
        "note",
        "quantity_unit",
        "close_type",
        "close_reason",
        "close_price",
        "closed_at",
        "last_close_event_id",
        "auto_close_exp_src",
        "auto_close_grace_days",
        "cash_secured_amount",
        "underlying_share_locked",
        "event_source_type",
        "event_source_name",
        "strategy",
        "leg_role",
        "strategy_group_id",
        "strategy_snapshot",
        "yield_enhancement_mode",
    ):
        assert retired not in fields, retired


def test_option_payload_contract_scalars_are_nested() -> None:
    """The contract is one nested object, not six flat siblings."""
    fields = _published_fields([_open_event()])

    assert fields["contract_key"] == {
        "broker": "富途",
        "account": "lx",
        "underlying_symbol": "NVDA",
        "option_type": "put",
        "strike": "100",
        "expiration_ymd": "2026-06-19",
        "asset_type": "option",
    }
    assert isinstance(fields["contract_key"], dict)


def test_stock_payload_keys_are_the_lot_key_set_plus_shares() -> None:
    """A stock lot publishes the §7.3 shares vocabulary and nothing else."""
    event = TradeEvent(
        event_id="open-stock",
        event_type="open",
        event_time_ms=1000,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="",
            strike=0.0,
            expiration_ymd="",
            asset_type="stock",
        ),
        contracts=10,
        price=150.0,
        currency="USD",
        source="cli_manual_open",
        multiplier=0,
        lot_id="lot_open-stock",
        asset_type="stock",
        raw_payload={"side": "buy", "source": "cli_manual_open"},
    )

    fields = _published_fields([event])

    lot = PositionLot.from_open_event(event, lot_id="lot_open-stock")
    assert set(fields) == set(lot.to_dict()) == STOCK_TARGET_KEYS
    for option_only in ("strike", "expiration", "expiration_ymd", "option_type"):
        assert option_only not in fields, option_only


def test_a_legacy_snapshot_cannot_leak_keys_into_the_published_payload() -> None:
    """§6: the seeding branch is closed, so a historical snapshot leaks nothing.

    The open event used to carry a verbatim copy of the legacy position-lot row in
    ``raw_payload["fields"]`` (``bootstrap.py:159`` / ``migration.py:143``) and the
    publisher seeded the published payload from it, so any legacy spelling it
    happened to hold rode into a converged payload. This is that snapshot, with
    the two spellings the plan calls reachable (``exp``, ``underlying_shares_locked``)
    plus the retired ``position_id``, and the payload is still the target key set.
    """
    legacy_snapshot = {
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "contracts": 2,
        "contracts_open": 2,
        "contracts_closed": 0,
        "status": "open",
        "strike": 100.0,
        "expiration": 1781827200000,
        "expiration_ymd": "2026-06-19",
        "multiplier": 100,
        "premium": 1.23,
        "opened_at": 1000,
        "last_action_at": 1000,
        "position_id": "NVDA_20260619_100P_short",
        "note": "source=legacy exp=2026-06-19; multiplier=100",
        "quantity_unit": "contract",
        "cash_secured_amount": 20000.0,
        "underlying_shares_locked": 200,
        "event_source_type": "manual_trade_event",
        "event_source_name": "legacy_import",
        "close_type": "assignment",
        "close_reason": "assignment",
        "close_price": 0.0,
        "closed_at": 2000,
        "last_close_event_id": "close-1",
        "auto_close_exp_src": "expiration_ymd",
        "auto_close_grace_days": 1,
        "strategy": "wheel",
        "leg_role": "short_put",
        "strategy_group_id": "grp-1",
        "source_stock_lot_id": "lot_stock",
        "source_wheel_branch_id": "branch-1",
        "strategy_snapshot": {"strategy": "wheel"},
        "yield_enhancement_mode": "return_first",
        "deliverable": "cash",
        "exp": "2026-06-19",
        "underlying_shares_locked": 200,
    }
    fields = _published_fields(
        [_open_event(raw_payload={"fields": legacy_snapshot})]
    )

    assert set(fields) == OPTION_TARGET_KEYS
    for leaked in ("exp", "position_id", "deliverable", "note", "expiration"):
        assert leaked not in fields, leaked


def test_the_seeding_producers_do_not_write_a_legacy_fields_snapshot() -> None:
    """§6 option (b): the two seeding producers stop copying the legacy row.

    ``bootstrap._bootstrap_trade_event`` and ``migration.position_lot_snapshot_to_open_event``
    each carry a verbatim copy of the legacy position-lot row in the event payload
    they write (``bootstrap.py:159`` / ``migration.py:143``). That copy is the open
    set §6 warns about -- it is where ``exp`` and ``underlying_shares_locked`` were
    reachable from -- so the converged producer writes ``contract_key`` and the
    quantities and no snapshot.
    """
    from src.application.ledger.bootstrap import _bootstrap_trade_event
    from src.application.ledger.migration import position_lot_snapshot_to_open_event

    legacy_row = {
        "record_id": "lot_legacy-1",
        "fields": {
            "account": "lx",
            "broker": "富途",
            "symbol": "0700.HK",
            "option_type": "put",
            "side": "short",
            "status": "open",
            "contracts": 2,
            "contracts_open": 2,
            "strike": 470.0,
            "expiration_ymd": "2026-06-30",
            "expiration": 1782691200000,
            "multiplier": 100,
            "premium": 3.93,
            "opened_at": 1000,
            "exp": "2026-06-30",
            "underlying_shares_locked": 200,
        },
    }

    bootstrapped = _bootstrap_trade_event(legacy_row, source_name="sqlite_position_lots")
    assert bootstrapped is not None
    assert "fields" not in bootstrapped.raw_payload

    migrated, diagnostics = position_lot_snapshot_to_open_event(
        {"lot_id": "lot_legacy-1", "fields": dict(legacy_row["fields"])},
        source="legacy_position_lots",
    )
    assert diagnostics == []
    assert migrated is not None
    assert "fields" not in migrated.raw_payload


def test_the_derived_columns_come_from_the_nested_sources() -> None:
    """§4: `account` from `contract_key`, `source_event_id` from `open_event_id`."""
    fields = _published_fields([_open_event()])
    values = _position_lot_storage_values(
        PositionLotRecord(lot_id="lot_open-nvda", fields=fields)
    )

    assert values[1] == "lx"  # account <- contract_key.account
    assert values[3] == "open-nvda"  # source_event_id <- open_event_id
    assert values[4] == parse_exp_to_ms("2026-06-19")  # expiration <- ymd, midnight UTC
    assert values[5] == 100.0  # strike <- contract_key.strike
    assert values[6] == 100.0  # multiplier stays top level


def test_the_expiration_column_is_midnight_utc_and_not_the_expiry_timezone() -> None:
    """The ymd->ms conversion is `parse_exp_to_ms`, not `EXPIRATION_DATE_TZ`.

    `expiration_timestamp_to_ymd` renders a stored ms in **UTC+8**
    (`expiration_dates.py:7`), so using it as the inverse would place every
    expiration eight hours early: `"2026-06-19"` would become
    `2026-06-18T16:00Z`. `expiration` is the input to the expiry ordering and the
    auto-close scan, so the shift is load-bearing, and it is invisible to the row
    set and to the parity re-run.
    """
    ymd = "2026-06-19"
    midnight_utc = parse_exp_to_ms(ymd)
    assert midnight_utc is not None

    shifted = int(datetime(2026, 6, 19, tzinfo=EXPIRATION_DATE_TZ).timestamp() * 1000)
    assert shifted != midnight_utc
    assert shifted == midnight_utc - 8 * 60 * 60 * 1000

    fields = _published_fields([_open_event(expiration_ymd=ymd)])
    values = _position_lot_storage_values(
        PositionLotRecord(lot_id="lot_open-nvda", fields=fields)
    )

    assert values[4] == midnight_utc
    assert values[4] != shifted


def test_the_scalar_derivation_reads_the_nested_contract_and_not_the_note() -> None:
    """§4: all three note fallbacks are retired on the write side.

    `_position_lot_contract_scalars` used to fall back to `note` for `expiration`
    (via `effective_expiration`) and for `multiplier`; a note is display text, not
    a fact source, and the converged payload carries every scalar structurally.
    """
    nested = {
        "contract_key": {
            "account": "lx",
            "option_type": "put",
            "strike": 12.5,
            "expiration_ymd": "2026-06-19",
        },
        "multiplier": 100,
    }
    assert _position_lot_contract_scalars(nested) == (
        parse_exp_to_ms("2026-06-19"),
        12.5,
        100.0,
    )

    # A note that names all three scalars contributes none of them: the payload
    # has no structured contract, so there is nothing to derive.
    note_only = {"note": "exp=2026-06-19;strike=12.5;multiplier=100"}
    assert _position_lot_contract_scalars(note_only) == (None, None, None)


def test_a_missing_nested_account_is_still_a_loud_refusal() -> None:
    """§4: `account` missing still raises; it is the one column never guessed."""
    fields = _published_fields([_open_event()])
    fields["contract_key"].pop("account")

    with pytest.raises(ValueError, match="position lot account is required"):
        _position_lot_storage_values(
            PositionLotRecord(lot_id="lot_open-nvda", fields=fields)
        )


def test_an_option_payload_missing_the_nested_contract_is_still_refused() -> None:
    """`error handling`: a silent NULL for a missing derived column is not allowed."""
    fields = _published_fields([_open_event()])
    fields["contract_key"].pop("expiration_ymd")

    with pytest.raises(ValueError, match="missing expiration"):
        _position_lot_storage_values(
            PositionLotRecord(lot_id="lot_open-nvda", fields=fields)
        )


def test_the_stock_expiration_column_is_null() -> None:
    """§4: an option row's `expiration` is non-NULL; a stock row's is allowed NULL."""
    event = TradeEvent(
        event_id="open-stock",
        event_type="open",
        event_time_ms=1000,
        contract_key=ContractKey.from_values(
            broker="富途",
            account="lx",
            underlying_symbol="NVDA",
            option_type="",
            strike=0.0,
            expiration_ymd="",
            asset_type="stock",
        ),
        contracts=10,
        price=150.0,
        currency="USD",
        source="cli_manual_open",
        multiplier=0,
        lot_id="lot_open-stock",
        asset_type="stock",
        raw_payload={"side": "buy", "source": "cli_manual_open"},
    )
    fields = _published_fields([event])
    values = _position_lot_storage_values(
        PositionLotRecord(lot_id="lot_open-stock", fields=fields)
    )

    assert values[4] is None  # expiration


def test_the_codec_round_trip_does_not_reinject_the_retired_flat_scalars(
    tmp_path: Path,
) -> None:
    """The window-checklist leg that actually goes through the codec.

    The row is written by the converged writer, then read back through
    ``list_position_lots`` -> ``position_lot_row_to_record`` -- the decode path
    whose column backfill this slice deleted (``sqlite_row_codec.py``). The
    plan's other legs never touch this path, which is exactly how the old
    injection made the code's own parity comparison report ``field_mismatch``
    on every option lot while every completion signal stayed green. Re-introduce
    the injection (or any reader-side heal) and this goes red: the decoded
    payload is still exactly ``to_dict()``'s key set, contract nested.
    """
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    published = _published_fields([_open_event()])
    repo.replace_position_lots(
        [PositionLotRecord(lot_id="lot_open-nvda", fields=dict(published))]
    )

    listed = repo.list_position_lots()
    assert [row["lot_id"] for row in listed] == ["lot_open-nvda"]
    decoded = listed[0]["fields"]
    assert set(decoded) == OPTION_TARGET_KEYS
    # The three keys the deleted backfill used to inject, proven absent on the
    # decoded side (the columns still carry the scalars -- the codec just must
    # not heal them back into the payload).
    assert "expiration" not in decoded
    assert "strike" not in decoded
    assert decoded["contract_key"]["expiration_ymd"] == "2026-06-19"
    assert decoded["contract_key"]["strike"] == "100"
    with sqlite3.connect(tmp_path / "option_positions.sqlite3") as conn:
        columns = conn.execute("SELECT strike, multiplier FROM position_lots").fetchone()
        names = {row[1] for row in conn.execute("PRAGMA table_info(position_lots)")}
    assert columns == (100.0, 100.0)
    assert "expiration" not in names


def test_the_ymd_to_ms_round_trip_is_faithful() -> None:
    """§8: `ms(ymd(original_ms)) == original_ms` -- the conversion pair agrees."""
    for original_ms in (
        1777420800000,  # 2026-04-29T00:00:00Z, a value the production store holds
        1781827200000,  # 2026-06-19T00:00:00Z
        1767225600000,  # 2026-01-01T00:00:00Z
    ):
        ymd = expiration_timestamp_to_ymd(original_ms)
        assert ymd is not None
        assert parse_exp_to_ms(ymd) == original_ms


# --- the plan's pre-window read-only prerequisite ----------------------------


@pytest.mark.skipif(
    not READ_ONLY_COPY.exists(),
    reason="needs the production read-only .backup copy at /tmp/om-readonly-local.sqlite3",
)
def test_the_stored_expirations_are_midnight_utc() -> None:
    """Plan Slice 2, F5: `ms(ymd(expiration)) == expiration` on every stored row.

    The reason this is an assertion and not a comment: the 8-hour shift a wrong
    inverse would introduce is transparent to *every other* leg of this slice --
    the row-set assertions compare ids and counts, the parity re-run compares two
    sides that both use the new writer, and a round-trip unit test only proves the
    pair of helpers it carries agrees with itself -- while `expiration` is the
    input to the expiry ordering and the auto-close scan. So the equality becomes
    a testable premise: non-zero mismatches means stop.

    Read-only (`mode=ro&immutable=1`) and never written to; the copy is a settled
    ``.backup``, so it has no ``-wal``/``-shm`` sidecars to keep coherent.
    """
    conn = sqlite3.connect(f"file:{READ_ONLY_COPY}?mode=ro&immutable=1", uri=True)
    try:
        rows = conn.execute(
            "SELECT record_id, expiration FROM position_lots ORDER BY record_id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "the read-only copy has no position_lots rows to check"
    non_null = [(row[0], row[1]) for row in rows if row[1] not in (None, "")]
    assert non_null, "every stored expiration is NULL: nothing to prove"

    mismatches = [
        (record_id, expiration, expiration_timestamp_to_ymd(expiration), parse_exp_to_ms(expiration_timestamp_to_ymd(expiration)))
        for record_id, expiration in non_null
        if parse_exp_to_ms(expiration_timestamp_to_ymd(expiration)) != expiration
    ]
    assert mismatches == [], (
        f"{len(mismatches)} of {len(non_null)} stored expirations are not midnight "
        f"UTC; do not open the window. First: {mismatches[:3]}"
    )
