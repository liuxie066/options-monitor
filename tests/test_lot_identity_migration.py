"""D1–D4 lot-identity migration (§13.3 slice 3).

Every fixture here is built through the real write paths and only then
degraded to the pre-migration shape. The point of the batch is a store that
already exists in production, so a hand-written ``fields_json`` would prove
that the migration handles *my* idea of the legacy payload, not the payload the
publisher actually produces.
"""

from __future__ import annotations

from tests.ledger_sqlite_test_support import connect_ledger_fixture

from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

import pytest

from domain.domain.ledger import ContractKey, PositionLot, TradeEvent
from domain.domain.ledger.position_fields import parse_exp_to_ms
from src.application.ledger import lot_identity_migration as module
from src.application.ledger import repository_common
from src.application.ledger.api import open_trade_reconciliation_evidence_repo
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.sqlite_row_codec import position_lot_row_to_record
from src.application.ledger.position_projection_runtime import (
    run_position_projection_forced_full,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


def _stock_open(
    event_id: str,
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    shares: int = 100,
    price: float = 100.0,
    event_time_ms: int = 2000,
) -> TradeEvent:
    """A stock acquisition in the shape the publisher's stock branch expects.

    §7.3: a stock lot carries ``shares_*``/``cost_basis_total`` instead of
    ``contracts``/``premium``, so the share count rides on ``contracts`` and the
    option vocabulary is empty. ``_stock_lot_fields`` is what turns this into a
    published payload.
    """

    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="富途",
            account=account,
            underlying_symbol=symbol,
            option_type="",
            strike=0.0,
            expiration_ymd="",
            asset_type="stock",
        ),
        contracts=shares,
        price=price,
        currency="USD",
        source="test",
        multiplier=1,
        asset_type="stock",
        quantity_unit="share",
        raw_payload={"side": "buy"},
    )


def _legacy_expiration_ms(exp_ymd: object) -> int | None:
    """The flat ``expiration`` mirror a pre-switch payload carried: ms.

    The retired sibling was a timestamp (``write-side-definition.md`` §2), and
    the domain's legacy reader converts it as one
    (``position_fields.effective_expiration``: ``exp_ms_to_datetime``). A ymd
    string in this slot is a value the vocabulary never wrote.
    """

    exp_ms = parse_exp_to_ms(exp_ymd if isinstance(exp_ymd, str) else None)
    return int(exp_ms) if exp_ms is not None else None


def _restore_legacy_flat_keys(path: Path) -> None:
    """Write the retired flat vocabulary back into every stored payload.

    The write path converges the payload onto ``contract_key``
    (``write-side-definition.md`` §2), so a store it built no longer carries the
    flat keys this module classifies. A pre-switch row does, and that row is the
    module's subject matter — this is the degradation the fixture's own
    ``position_id`` injection models, one vocabulary further back.
    """

    with closing(connect_ledger_fixture(path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT record_id, fields_json FROM position_lots"
        ).fetchall():
            fields = json.loads(row["fields_json"])
            contract_key = fields.get("contract_key")
            contract_key = contract_key if isinstance(contract_key, dict) else {}
            fields.update(
                {
                    "account": contract_key.get("account"),
                    "broker": contract_key.get("broker"),
                    "symbol": contract_key.get("underlying_symbol"),
                    "option_type": contract_key.get("option_type"),
                    "side": fields.get("position_side"),
                    "expiration": _legacy_expiration_ms(
                        contract_key.get("expiration_ymd")
                    ),
                    "expiration_ymd": contract_key.get("expiration_ymd"),
                    "strike": contract_key.get("strike"),
                    "contracts": fields.get("contracts_opened"),
                    "premium": fields.get("premium_open"),
                    "opened_at": fields.get("opened_at_ms"),
                    "source_event_id": fields.get("open_event_id"),
                }
            )
            if str(fields.get("status") or "").strip().lower() == "close":
                fields.update(
                    {
                        "close_type": "assign",
                        "close_reason": "assignment",
                        "close_price": 0.0,
                        "closed_at": 3_000,
                        "last_action_at": 3_000,
                    }
                )
            conn.execute(
                "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
                (
                    json.dumps(
                        fields,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    ),
                    row["record_id"],
                ),
            )
        conn.commit()


def _strip_converged_shape(fields: dict) -> None:
    """Leave the flat payload vocabulary present in the frozen legacy store."""

    for key in (
        "asset_type",
        "contract_key",
        "open_event_id",
        "lot_id",
        "last_event_id",
        "close_event_ids",
        "realized_pnl",
        "contracts_opened",
        "premium_open",
        "opened_at_ms",
        "position_side",
    ):
        fields.pop(key, None)


def _lot_option_type(fields: dict) -> str:
    """The contract's option type under the converged shape.

    The payload carries the contract under ``contract_key`` now
    (``write-side-definition.md`` §2); the retired flat sibling stays readable
    for a row written before the shape switch.
    """
    contract_key = fields.get("contract_key")
    contract_key = contract_key if isinstance(contract_key, dict) else {}
    return str(contract_key.get("option_type") or fields.get("option_type") or "")


def _legacy_store(
    tmp_path: Path, *, name: str = "ledger.sqlite3", degrade: bool = True
) -> Path:
    """A store holding both lot families, degraded to the pre-migration shape.

    Two option lots (one assigned, one still open) and two stock lots, so the
    shape-key dispatch, both scalar-carrier paths and the close-provenance keys
    are all exercised. The degradation is what an existing production store
    looks like to this batch: ``lot_id`` is present but NULL, and the retired
    ``position_id`` display key is still inside ``fields_json``.
    """

    path = tmp_path / name
    repo = SQLiteOptionPositionsRepository(path)

    persist_manual_open_event(
        repo, broker="富途", account="lx", symbol="NVDA", option_type="put",
        side="short", contracts=1, currency="USD", strike=100.0, multiplier=100,
        expiration_ymd="2026-06-19", premium_per_share=2.5, opened_at_ms=1000,
    )
    persist_manual_open_event(
        repo, broker="富途", account="sy", symbol="TSLA", option_type="call",
        side="short", contracts=2, currency="USD", strike=250.0, multiplier=100,
        expiration_ymd="2027-06-18", premium_per_share=4.0, opened_at_ms=1100,
    )
    run_position_projection_forced_full(
        repo,
        [
            _stock_open("assign-1"),
            _stock_open("buy-1", symbol="TSLA", shares=50, price=95.0, event_time_ms=2100),
        ],
    )
    put_lot = next(
        item for item in repo.list_position_lots()
        if _lot_option_type(item.get("fields") or {}) == "put"
    )
    record_manual_assignment(
        repo, lot_id=put_lot["record_id"], contracts_to_close=1,
        stock_side="buy", stock_qty=100, stock_price=100.0, as_of_ms=3000,
    )
    if not degrade:
        return path

    with closing(connect_ledger_fixture(path)) as conn, conn:
        conn.execute("ALTER TABLE position_lots RENAME COLUMN lot_id TO record_id")
        conn.execute("ALTER TABLE position_lots ADD COLUMN lot_id TEXT")
        conn.execute("ALTER TABLE position_lots ADD COLUMN expiration INTEGER")
        conn.execute("ALTER TABLE wheel_events RENAME COLUMN lot_id TO stock_lot_id")
        conn.execute("DROP INDEX idx_position_lots_account_lot")
        conn.execute(
            "CREATE INDEX idx_position_lots_account_record "
            "ON position_lots(account, record_id)"
        )
        conn.execute(
            "CREATE INDEX idx_position_lots_account_expiration "
            "ON position_lots(account, expiration, record_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_position_lots_lot_id ON position_lots(lot_id)"
        )
        for lot_id, raw in conn.execute(
            "SELECT record_id, fields_json FROM position_lots"
        ).fetchall():
            fields = json.loads(raw)
            fields["position_id"] = f"LEGACY-{lot_id}"
            expiration = _legacy_expiration_ms(
                (fields.get("contract_key") or {}).get("expiration_ymd")
            )
            conn.execute(
                "UPDATE position_lots SET fields_json = ?, expiration = ? "
                "WHERE record_id = ?",
                (
                    json.dumps(fields, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    expiration,
                    lot_id,
                ),
            )
        conn.commit()
    return path


def _edit_lot_fields(path: Path, record_id: str, mutate) -> None:
    """Rig one lot's ``fields_json`` the way a degraded store carries it."""

    with closing(connect_ledger_fixture(path)) as conn, conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(position_lots)")
        }
        identity = "record_id" if "record_id" in columns else "lot_id"
        raw = conn.execute(
            f"SELECT {identity}, fields_json FROM position_lots WHERE {identity} = ?",
            (record_id,),
        ).fetchone()
        fields = json.loads(raw[1])
        mutate(fields)
        conn.execute(
            f"UPDATE position_lots SET fields_json = ? WHERE {identity} = ?",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True), raw[0]),
        )
        conn.commit()


def _stored_rows(path: Path) -> dict[str, object]:
    with closing(connect_ledger_fixture(path)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            str(row["record_id"]): {
                "lot_id": row["lot_id"],
                "account": row["account"],
                "source_event_id": row["source_event_id"],
                "fields": json.loads(row["fields_json"]),
            }
            for row in conn.execute(
                "SELECT record_id, lot_id, account, source_event_id, fields_json"
                " FROM position_lots"
            ).fetchall()
        }














def _table_info(path: Path, table: str = "position_lots") -> list[sqlite3.Row]:
    with connect_ledger_fixture(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"PRAGMA table_info({table})").fetchall()












# --- the shape pin -----------------------------------------------------------


@pytest.mark.parametrize("asset_type", ["option", "stock"])
def test_target_shape_keys_are_exactly_position_lot_to_dict(asset_type: str) -> None:
    """§12.3 D3 says "以 ``to_dict()`` 为准"; pin the constant to the code.

    A hand-copied key list drifts silently — it would keep passing after
    ``to_dict()`` gained a field, and every row would then be reported as
    holding a dropped key the migration had never heard of.
    """

    stock = asset_type == "stock"
    emitted = set(
        PositionLot(
            lot_id="lot-1",
            open_event_id="open-1",
            contract_key=ContractKey.from_values(
                broker="futu", account="lx", underlying_symbol="NVDA",
                option_type="" if stock else "put",
                strike=0.0 if stock else 100.0,
                expiration_ymd="" if stock else "2028-12-15",
                asset_type=asset_type,
            ),
            position_side="short",
            opened_at_ms=1_000,
            contracts_opened=0 if stock else 1,
            contracts_open=0 if stock else 1,
            contracts_closed=0,
            status="open",
            premium_open=Decimal("0") if stock else Decimal("2.5"),
            multiplier=0 if stock else 100,
            currency="USD",
            realized_pnl=Decimal("0"),
            last_event_id="open-1",
            asset_type=asset_type,
            shares_opened=Decimal("100") if stock else None,
            shares_open=Decimal("100") if stock else None,
            shares_closed=Decimal("0") if stock else None,
            cost_basis_total=Decimal("10000") if stock else None,
        ).to_dict()
    )
    declared = module._lot_shape_keys(asset_type)
    assert declared == emitted
    assert set(module.LOT_SHAPE_KEYS_COMMON) | (
        set(module.LOT_SHAPE_KEYS_STOCK_EXTRA) if asset_type == "stock" else set()
    ) == emitted


def test_stock_extra_keys_are_not_expected_of_an_option_lot() -> None:
    """The dispatch is on ``asset_type``, so an option row must not owe them."""

    assert not module._lot_shape_keys("option") & set(module.LOT_SHAPE_KEYS_STOCK_EXTRA)
    assert module.LOT_SHAPE_KEYS_STOCK_EXTRA <= module._lot_shape_keys("stock")


# --- inventory ---------------------------------------------------------------


def test_inventory_reports_the_pending_work_without_writing(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    before = path.stat().st_size

    inventory = module.build_lot_identity_migration_inventory(path)

    assert inventory["schema_version"] == module.INVENTORY_SCHEMA
    assert inventory["read_only"] is True
    assert path.stat().st_size == before
    assert inventory["readiness"] == "not_ready"
    assert inventory["readiness_reasons"] == ["lot_id_backfill_pending"]
    pending = inventory["pending"]
    assert pending["d2_lot_id_column"] == {"column_present": True, "rows_null": 4}
    assert pending["d2_record_id_column"]["column_present"] is True
    assert pending["d1_expiration_column"]["rows_non_null"] == 2
    assert pending["d4_position_id_rows"] == 4
    assert pending["rows"] == inventory["counts"]["position_lots"] == 4


def test_inventory_classifies_every_dropped_key_and_loses_nothing(tmp_path: Path) -> None:
    """No real payload key is unaccounted for on a store the write path built.

    This is the assertion that would have caught the §13.3 "非空即 fail" rule:
    under it, ``carried`` and ``reconstructible`` would both be empty and every
    real store would report ~15 blocking keys.

    The fixture's payload is the converged shape, so the flat contract
    vocabulary is not a dropped key on it at all; the retired pre-switch
    vocabulary is what the rest of this test writes back in, which is exactly the
    row this module migrates.
    """

    path = _legacy_store(tmp_path)
    _restore_legacy_flat_keys(path)
    inventory = module.build_lot_identity_migration_inventory(path)
    classification = inventory["dropped_key_classification"]

    assert classification["lost"] == {}
    assert {"account", "broker", "symbol", "option_type", "side", "position_id"} <= set(
        classification["carried"]
    )
    # The close patch writes these off the closing trade event, so they are
    # reconstructible rather than lost.
    assert {
        "close_type", "close_reason", "close_price", "closed_at", "last_action_at",
    } <= set(classification["reconstructible"])
    assert classification["carried"]["position_id"]["reason"] == "position_key"
    assert classification["reconstructible"]["closed_at"]["reason"] == (
        "the closing trade_event's event_time_ms"
    )


def test_the_strategy_family_is_classified_as_one_group() -> None:
    """The table must not lag ``POSITION_LOT_STRATEGY_PATCH_FIELDS``.

    ``publisher`` writes that whole family in one pass off the same open-event
    payload, so a member needs a disposition for exactly the reason its siblings
    have one. Pinning the boundary here fails loudly when the family grows,
    instead of shipping a blocking ``no_declared_carrier`` key on the project's
    principal strategy.
    """

    from domain.domain.ledger.position_fields import POSITION_LOT_STRATEGY_PATCH_FIELDS

    assert POSITION_LOT_STRATEGY_PATCH_FIELDS  # the pin is vacuous if the family is gone
    unclassified = sorted(
        key
        for key in POSITION_LOT_STRATEGY_PATCH_FIELDS
        if key not in module.RECONSTRUCTIBLE_DROPPED_KEYS
        and key not in module.CARRIED_DROPPED_KEYS
        and key not in module._lot_shape_keys("option")
    )
    assert unclassified == []


def _put_lot_record_id(path: Path) -> str:
    return next(
        record_id
        for record_id, stored in _stored_rows(path).items()
        if _lot_option_type(stored["fields"]) == "put"
    )


def test_a_family_key_no_event_carries_is_reported_lost(tmp_path: Path) -> None:
    """The event layer is a home only where it actually holds the fact.

    ``RECONSTRUCTIBLE_DROPPED_KEYS`` says where the family's home is; it cannot
    say whether this store's open event ever put it there. An import that never
    seeded the family leaves the row's own copy as the only one, so the gate has
    to report the loss rather than certify it as reconstructible — the verdict
    that kept this instrument green on a fact nothing here can bring back.
    """

    path = _legacy_store(tmp_path)
    record_id = _put_lot_record_id(path)
    _edit_lot_fields(
        path,
        record_id,
        lambda fields: fields.update({"strategy": "wheel", "leg_role": "short_put"}),
    )

    inventory = module.build_lot_identity_migration_inventory(path)
    classification = inventory["dropped_key_classification"]

    assert set(classification["lost"]) == {"strategy", "leg_role"}
    assert classification["lost"]["strategy"] == {
        "rows_non_empty": 1,
        "disposition": "lost",
        "reason": "event_layer_carrier_absent",
        "sample_lot_ids": [record_id],
    }
    assert "strategy" not in classification["reconstructible"]
    report = module.verify_lot_identity_migration(path)
    assert report["blocking_keys"] == ["leg_role", "strategy"]
    assert report["ok"] is False
    assert "dropped_payload_keys_would_lose_facts" in report["readiness_reasons"]


def test_a_family_key_the_open_event_carries_is_reconstructible(tmp_path: Path) -> None:
    """Measured, not looked up: the same key answers per lot and per fact.

    Only ``strategy``/``leg_role`` ride the open event here — ``strategy_group_id``
    was never written by it — so one row must report two different verdicts. A
    table cannot express that split, which is the whole point of measuring.
    """

    path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(path)
    persist_manual_open_event(
        repo, broker="futu", account="lx", symbol="NVDA", option_type="put",
        side="short", contracts=1, currency="USD", strike=100.0, multiplier=100,
        expiration_ymd="2026-06-19", premium_per_share=2.5, opened_at_ms=1_000,
        strategy_snapshot={"strategy": "wheel", "leg_role": "short_put"},
    )

    with connect_ledger_fixture(path) as conn:
        conn.row_factory = sqlite3.Row
        record_id = str(conn.execute("SELECT lot_id FROM position_lots").fetchone()["lot_id"])
    _edit_lot_fields(
        path,
        record_id,
        lambda fields: fields.update(
            {
                "strategy": "wheel",
                "leg_role": "short_put",
                "strategy_group_id": "group-a",
            }
        ),
    )

    classification = module.build_lot_identity_migration_inventory(path)[
        "dropped_key_classification"
    ]

    assert set(classification["reconstructible"]) & {"strategy", "leg_role"} == {
        "strategy",
        "leg_role",
    }
    assert classification["reconstructible"]["strategy"]["reason"] == (
        "the open event payload's strategy metadata"
    )
    assert set(classification["lost"]) == {"strategy_group_id"}
    assert classification["lost"]["strategy_group_id"]["reason"] == (
        "event_layer_carrier_absent"
    )


def test_the_measured_family_is_exactly_the_strategy_patch_family() -> None:
    """The measurement must cover the family, no more and no less.

    ``POSITION_LOT_STRATEGY_PATCH_FIELDS`` is what ``strategy_metadata_fields_from_payload``
    can hand back, so those are exactly the keys a measured verdict has an
    answer for. A family member outside the measured set would silently return
    to being declared, which is the defect this pin exists to prevent.
    """

    from domain.domain.ledger.position_fields import POSITION_LOT_STRATEGY_PATCH_FIELDS

    assert set(module.EVENT_LAYER_MEASURED_DROPPED_KEYS) == {
        *POSITION_LOT_STRATEGY_PATCH_FIELDS,
        "yield_enhancement_mode",
    }
    assert set(module.EVENT_LAYER_MEASURED_DROPPED_KEYS) <= set(
        module.RECONSTRUCTIBLE_DROPPED_KEYS
    )


def test_yield_mode_requires_the_same_open_event_fact(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    lot_id = _put_lot_record_id(path)
    mode = "vol_convexity_enhancement"
    _edit_lot_fields(path, lot_id, lambda fields: fields.update({"yield_enhancement_mode": mode}))
    absent = module.build_lot_identity_migration_inventory(path)["dropped_key_classification"]
    assert absent["lost"]["yield_enhancement_mode"]["reason"] == "event_layer_carrier_absent"
    event_id = _stored_rows(path)[lot_id]["source_event_id"]

    with connect_ledger_fixture(path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'trade_events'"
        ):
            conn.execute(f"DROP TRIGGER {row['name']}")
        raw = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id = ?", (event_id,)
        ).fetchone()["event_json"]
        event = json.loads(raw)
        event["raw_payload"]["yield_enhancement_mode"] = mode
        conn.execute(
            "UPDATE trade_events SET event_json = ? WHERE event_id = ?",
            (json.dumps(event, ensure_ascii=False, sort_keys=True), event_id),
        )
        conn.commit()

    classification = module.build_lot_identity_migration_inventory(path)["dropped_key_classification"]
    assert classification["reconstructible"]["yield_enhancement_mode"]["rows_non_empty"] == 1

    with connect_ledger_fixture(path) as conn:
        raw = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        event = json.loads(raw)
        event["raw_payload"]["yield_enhancement_mode"] = "income_upside_enhancement"
        conn.execute(
            "UPDATE trade_events SET event_json = ? WHERE event_id = ?",
            (json.dumps(event, ensure_ascii=False, sort_keys=True), event_id),
        )
        conn.commit()
    conflict = module.build_lot_identity_migration_inventory(path)["dropped_key_classification"]
    assert conflict["lost"]["yield_enhancement_mode"]["reason"] == "event_layer_carrier_conflict"


def test_yield_mode_is_reconstructed_from_an_adjust_event(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    lot_id = _put_lot_record_id(path)
    mode = "income_upside_enhancement"
    _edit_lot_fields(path, lot_id, lambda fields: fields.update({"yield_enhancement_mode": mode}))
    from src.application.ledger.position_projection_migration import (
        _repository,
        _write_connection,
    )

    repo = _repository(path)
    event = TradeEvent(
            event_id="adjust-yield-mode",
            event_type="adjust",
            event_time_ms=4_000,
            contract_key=ContractKey.from_values(
                broker="富途", account="lx", underlying_symbol="NVDA",
                option_type="put", strike=100.0, expiration_ymd="2026-06-19",
            ),
            contracts=0,
            price=Decimal("0"),
            currency="USD",
            source="test",
            target_lot_id=lot_id,
            raw_payload={"target_lot_id": lot_id, "patch": {"yield_enhancement_mode": mode}},
    )
    with _write_connection(path) as conn:
        repo.upsert_trade_event(event, conn=conn)
        conn.commit()

    classification = module.build_lot_identity_migration_inventory(path)["dropped_key_classification"]
    assert classification["reconstructible"]["yield_enhancement_mode"]["rows_non_empty"] == 1


def test_a_note_only_scalar_blocks_even_with_a_populated_column(tmp_path: Path) -> None:
    """A note-only scalar no longer survives anywhere (write-side-definition §4).

    The note fallbacks that used to fill the ``multiplier``/``strike`` columns
    (``repository_common._position_lot_contract_scalars``,
    ``position_fields.effective_*``) are retired -- a note is display text,
    never a fact source -- so ``NOTE_KV_SURVIVING_COLUMNS`` declares no column
    home: a rebuild would not refill the column from the note, and a value
    still sitting in it is a stale copy the retired fallback once wrote, not a
    surviving fact. This test's previous shape asserted that column rescue;
    the rescue is the retired behavior, pinned from the other side by
    ``test_verify_fails_when_a_fact_lives_only_in_the_note``.
    """

    path = _legacy_store(tmp_path)
    lot_id = next(
        key
        for key, value in _stored_rows(path).items()
        if _lot_option_type(value["fields"] or {}) == "put"
    )
    with connect_ledger_fixture(path) as conn:
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            "SELECT fields_json, multiplier FROM position_lots WHERE record_id = ?",
            (lot_id,),
        ).fetchone()
        # The column still holds the value the retired fallback wrote...
        assert raw["multiplier"] == 100.0
        fields = json.loads(raw["fields_json"])
        assert fields["multiplier"] == 100
        fields.pop("multiplier")
        # ...and the note is written whole (the converged payload carries no
        # ``note`` key, ``write-side-definition.md`` §2).
        fields["note"] = "multiplier=100"
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True), lot_id),
        )
        conn.commit()

    classification = module.build_lot_identity_migration_inventory(path)[
        "dropped_key_classification"
    ]
    # The populated column does not rescue the fact: nothing refills it after a
    # rebuild, so the note's copy is the only live one and D3 cannot drop it.
    assert classification["lost"]["note"]["reason"] == "note_kv_only:multiplier"
    # No key declares a surviving column anymore. ``exp``'s column is the one
    # D1 drops, and the same retirement removed the others' refills.
    assert module.NOTE_KV_SURVIVING_COLUMNS == {}

    # Nulling the column changes nothing: the classification already ignores it.
    with connect_ledger_fixture(path) as conn:
        conn.execute(
            "UPDATE position_lots SET multiplier = NULL WHERE record_id = ?",
            (lot_id,),
        )
        conn.commit()

    without_column = module.build_lot_identity_migration_inventory(path)[
        "dropped_key_classification"
    ]
    assert without_column["lost"]["note"]["reason"] == "note_kv_only:multiplier"




def test_inventory_reports_where_each_contract_scalar_lives(tmp_path: Path) -> None:
    """§13.5 R6's asymmetry, measured: the column is not the only carrier."""

    carriers = module.build_lot_identity_migration_inventory(
        _legacy_store(tmp_path)
    )["contract_scalar_carriers"]

    # Both stock rows legitimately have no expiration at all. Their converged
    # ``contract_key`` does carry the option vocabulary's placeholders — one of
    # which is the canonical ``"0"`` a stock lot's ``strike`` renders as, so a
    # value *is* present there even though no strike is.
    assert carriers["expiration"]["structured"] == 2
    assert carriers["expiration"]["absent"] == 2
    assert carriers["expiration"]["rows"] == 4
    assert carriers["strike"]["structured"] == 4
    assert carriers["multiplier"]["structured"] == 4


# --- verify ------------------------------------------------------------------


def test_verify_replays_and_never_uses_the_checkpoint_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is structural: the reuse entry point is never even imported.

    ``projection_verify``'s ``--mode auto`` can return ``ok: True`` with
    synthesised ``matched`` items and no replay. Asserting on a boolean in the
    payload would prove nothing about whether the replay ran, so this makes the
    shortcut's entry point raise and shows ``verify`` completing anyway.
    """

    from src.application.ledger import commands as commands_module
    import src.application.ledger.projection_verify as projection_verify_module

    def _forbidden(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("verify must not enter the checkpoint reuse path")

    monkeypatch.setattr(projection_verify_module, "verify_position_projection", _forbidden)
    monkeypatch.setattr(commands_module, "verify_position_lot_projection", _forbidden)

    report = module.verify_lot_identity_migration(_legacy_store(tmp_path))

    assert report["mode_used"] == "full_replay"
    assert report["checkpoint_reuse_forbidden"] is True
    assert report["checkpoint"]["contract"] == "never_reuse"
    # The read-only surface cannot see the shortcut's precondition, and says so
    # rather than reporting a store-side table that the shortcut does not read.
    assert report["checkpoint"]["shortcut_precondition_observable_read_only"] is False
    assert "projection_verify.checkpoint.json" not in json.dumps(report["checkpoint"])
    assert report["projection"]["event_count"] == 5


def test_verify_is_content_side_not_shape_side(tmp_path: Path) -> None:
    """A payload that only *looks* right must still fail.

    Flipping ``shares_open``/``shares_closed`` keeps every key and every count,
    so a shape-only check would pass it. ``compare_projection_lots`` compares
    the payloads themselves. The store is built in the final shape (the window's
    migration is not needed to reach it), so the corruption is the only thing
    that differs from a fresh replay.
    """

    path = _legacy_store(tmp_path, degrade=False)
    assert module.verify_lot_identity_migration(path)["projection"]["summary"] == {
        "matched": 4
    }

    def _rig_shares(fields: dict) -> None:
        fields["shares_open"] = "1"
        fields["shares_closed"] = "99"

    _edit_lot_fields(path, "lot_assign-1", _rig_shares)

    report = module.verify_lot_identity_migration(path)
    assert report["projection"]["ok"] is False
    assert report["projection"]["summary"] == {"field_mismatch": 1, "matched": 3}
    assert report["projection"]["mismatch_items"][0]["record_id"] == "lot_assign-1"


def test_verify_does_not_blame_the_lots_for_events_it_cannot_read(
    tmp_path: Path,
) -> None:
    """A store the replay cannot read is not a store whose lots disagree.

    The sampled pre-canonical store carries flat ``position_effect`` event
    payloads, which the codec rejects (``non_canonical_trade_event_schema``: no
    ``contract_key``/``event_type``/``event_time_ms``). The replay then projects
    no lots at all, so every stored lot reads as ``extra_in_position_lots`` —
    an artifact of the unreadable input, not a verdict about the rows. The
    reasons are exclusive for that reason: with the events unreadable, a
    ``projection_replay_mismatch`` label would send the operator to the wrong
    surface.
    """

    path = _legacy_store(tmp_path)
    with connect_ledger_fixture(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT event_id, event_json FROM trade_events").fetchall()
        assert rows
        # The current writer cannot produce this shape — that is the whole point
        # of the guard triggers, which forbid the transition field by field.
        # Building the degraded store means lifting the guard the way the
        # hand-built post-rebuild table lifts the schema, and the test only
        # reads afterwards.
        for trigger in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'trade_events'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {trigger['name']}")
        for row in rows:
            payload = json.loads(row["event_json"])
            for key in ("contract_key", "event_type", "event_time_ms"):
                payload.pop(key, None)
            conn.execute(
                "UPDATE trade_events SET event_json = ? WHERE event_id = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["event_id"]),
            )
        conn.commit()

    report = module.verify_lot_identity_migration(path)

    assert report["projection"]["event_count"] == len(rows)
    assert report["projection"]["projection_error_count"] == len(rows)
    assert report["projection"]["summary"] == {
        "projection_error": len(rows),
        "extra_in_position_lots": report["projection"]["position_lot_count"],
        # §9.3: the two counts were always reported and never compared until A3.
        # Nothing projected against N stored rows is a count mismatch as well as N
        # extra rows, and the report says both instead of letting the reader infer.
        "count_mismatch": 1,
    }
    # The lot-level count is not hidden, it is attributed: every stored lot is
    # flagged extra *because* nothing was projected, so it equals the lot count
    # exactly. Reporting that as a lot verdict is the misattribution.
    assert report["projection"]["lot_mismatch_count"] == len(_stored_rows(path))
    assert report["projection"]["ok"] is False
    assert "trade_events_not_replayable" in report["readiness_reasons"]
    assert "projection_replay_mismatch" not in report["readiness_reasons"]

    # The contrast that makes the split meaningful: the same store with readable
    # events. Its lots do disagree with the replay — the pre-migration payloads
    # still carry ``position_id`` — and that is exactly the case the other reason
    # names, so the two labels track the two causes rather than the store.
    pristine = module.verify_lot_identity_migration(_legacy_store(tmp_path, name="clean.sqlite3"))
    assert pristine["projection"]["projection_error_count"] == 0
    assert pristine["projection"]["summary"] == {"field_mismatch": 4}
    assert pristine["projection"]["lot_mismatch_count"] == 4
    assert "trade_events_not_replayable" not in pristine["readiness_reasons"]
    assert "projection_replay_mismatch" in pristine["readiness_reasons"]


def test_verify_is_clean_on_a_strategy_tagged_store(tmp_path: Path) -> None:
    """The project's principal strategy must not make the gate permanently red.

    ``publisher`` writes the whole ``POSITION_LOT_STRATEGY_PATCH_FIELDS`` family
    off the open event's strategy metadata in one pass, so ``strategy`` and
    ``leg_role`` share ``strategy_snapshot``'s home. Reporting them as
    ``no_declared_carrier`` — a blocking key — put a permanent red on every
    wheel-tagged store, which is the population this window targets.
    """

    path = tmp_path / "ledger.sqlite3"
    repo = SQLiteOptionPositionsRepository(path)
    persist_manual_open_event(
        repo,
        broker="futu",
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100,
        expiration_ymd="2026-06-19",
        premium_per_share=2.5,
        opened_at_ms=1_000,
        strategy_snapshot={"strategy": "wheel", "leg_role": "short_put"},
    )

    with connect_ledger_fixture(path) as conn:
        conn.row_factory = sqlite3.Row
        fields = json.loads(
            conn.execute("SELECT fields_json FROM position_lots").fetchone()["fields_json"]
        )

    # The fixture must actually carry the family, or this test proves nothing.
    # The converged write path no longer puts it in the payload
    # (``write-side-definition.md`` §2/§7 moves it to the strategy/event side), so
    # the carrier asserted here is the open event's ``raw_payload``.
    assert not {"strategy", "leg_role", "strategy_snapshot"} & set(fields)
    event_payload = json.loads(
        conn.execute(
            "SELECT event_json FROM trade_events ORDER BY event_id LIMIT 1"
        ).fetchone()["event_json"]
    )["raw_payload"]
    assert event_payload["strategy"] == "wheel"
    assert event_payload["leg_role"] == "short_put"
    assert event_payload["strategy_snapshot"]

    report = module.verify_lot_identity_migration(path)

    assert report["blocking_keys"] == []
    assert report["ok"] is True
    # The family is never a fact nobody vouched for. On the converged payload it
    # is not a dropped key at all, and the declaration that would cover a
    # pre-switch row is pinned by
    # ``test_the_strategy_family_is_classified_as_one_group``.
    assert report["payload_keys"]["lost"] == {}
    assert "dropped_payload_keys_would_lose_facts" not in report["readiness_reasons"]


def test_verify_fails_when_a_fact_lives_only_in_the_note(tmp_path: Path) -> None:
    """§13.4 row 3's negative case, and the reason D3 is gated on it.

    ``exp`` in a note is the *fallback* source for ``expiration``. When the
    structured field is present the note is redundant and D3 may drop it; when
    it is the only copy, dropping the note destroys the fact.
    """

    path = _legacy_store(tmp_path)
    def _note_only_expiration(fields: dict) -> None:
        # ``expiration`` moved under ``contract_key`` (``write-side-definition.md``
        # §2), so the structured copy is removed there; the flat key is popped too
        # so a legacy spelling cannot rescue the fact. The converged payload
        # carries no ``note`` key either, so the note is written whole.
        fields.pop("expiration", None)
        fields.pop("expiration_ymd", None)
        contract_key = fields.get("contract_key")
        if isinstance(contract_key, dict):
            contract_key.pop("expiration_ymd", None)
        fields["note"] = "exp=2026-06-19"

    _edit_lot_fields(path, "lot_assign-1", _note_only_expiration)

    report = module.verify_lot_identity_migration(path)

    assert report["ok"] is False
    assert "dropped_payload_keys_would_lose_facts" in report["readiness_reasons"]
    assert report["blocking_keys"] == ["note"]
    assert report["payload_keys"]["lost"]["note"]["reason"] == "note_kv_only:exp"


@pytest.mark.parametrize(
    "note",
    [
        # ``merge_note``'s separator.
        "source=test;event_id=assign-1;multiplier_source=",
        # ``_base_fields_for_lot``'s separator, which is what the publisher
        # actually writes. ``parse_note_kv`` cannot see past the first key here,
        # which is why the classifier tokenizes on whitespace as well.
        "source=test event_id=assign-1 order_id= multiplier_source=",
    ],
)
def test_note_kv_is_read_in_both_writer_formats(note: str) -> None:
    """D3's verdict on ``note`` must not depend on which writer wrote it."""

    prose, pairs = module._note_parts(note)
    assert prose == []
    assert dict(pairs[:2]) == {"source": "test", "event_id": "assign-1"}
    # Every one of those facts is carried by the open event, so the note is a
    # convenience copy and D3 may drop it — whichever writer wrote it.
    assert module._note_disposition(note, {"note": note}, {}) is None


def test_note_segment_that_is_not_all_kv_stays_prose() -> None:
    """Otherwise any prose containing an ``=`` would be read as a fact carrier."""

    prose, pairs = module._note_parts("rolled into the March cycle target=105")
    assert pairs == []
    assert prose == ["rolled into the March cycle target=105"]
    assert module._note_disposition(prose[0], {}, {}) == "note_prose_only_in_note"


def test_verify_rejects_free_prose_in_the_note(tmp_path: Path) -> None:
    """Prose has no other home, so no KV disposition can rescue it."""

    path = _legacy_store(tmp_path)
    def _prose_only_note(fields: dict) -> None:
        fields.clear()
        fields.update({"account": "lx", "note": "rolled into the March cycle"})

    _edit_lot_fields(path, "lot_assign-1", _prose_only_note)

    report = module.verify_lot_identity_migration(path)
    assert report["payload_keys"]["lost"]["note"]["reason"] == "note_prose_only_in_note"


# --- apply -------------------------------------------------------------------
















# --- module contract ---------------------------------------------------------


def test_lot_scalar_carriers_match_the_write_paths_reading_of_them() -> None:
    """The migration's carrier vocabulary and the writer's must not diverge.

    ``_position_lot_contract_scalars`` is what the contract columns are filled
    from; if it grew a fourth scalar or changed where it reads the scalars from,
    the inventory's ``contract_scalar_carriers`` would be describing a different
    derivation than the one the store was written with.
    """

    from domain.domain.option_position_identity import parse_exp_to_ms
    from src.application.ledger.repository_common import _position_lot_contract_scalars

    assert module.CONTRACT_SCALARS == ("expiration", "strike", "multiplier")
    # The converged shape: the contract under ``contract_key``, ``multiplier``
    # top level. ``expiration_ymd`` renders as midnight UTC (``parse_exp_to_ms``),
    # which is asserted against the round trip in the parity probe's own suite.
    structured = _position_lot_contract_scalars(
        {
            "contract_key": {
                "strike": 12.5,
                "expiration_ymd": "2026-06-19",
            },
            "multiplier": 100,
        }
    )
    assert structured == (
        parse_exp_to_ms("2026-06-19"),
        12.5,
        100.0,
    )

    # Every note fallback is retired (``write-side-definition.md`` §4: a note is
    # display text, never a fact source), so a note carrying all three carries
    # nothing the writer can still read.
    note_only = _position_lot_contract_scalars(
        {"note": "exp=2026-06-19;strike=12.5;multiplier=100"}
    )
    assert note_only == (None, None, None)


def test_quantity_unit_declares_a_carrier_only_where_one_exists() -> None:
    """``shares_*`` is a stock-only emission, so an option row has no such carrier.

    Claiming ``asset_type + shares_*`` for an option row names a carrier that
    ``PositionLot.to_dict()`` never emits for that shape, so the quantity unit
    would vanish with D3 while the gate read ``carried``.
    """

    assert module._drop_disposition(
        "quantity_unit", "share", {"asset_type": "stock"}, {}, {}
    ) == ("carried", "asset_type + shares_*")
    assert module._drop_disposition(
        "quantity_unit", "contract", {"asset_type": "option"}, {}, {}
    ) == ("lost", "no_declared_carrier")
    # Same dispatch the shape oracle uses, down to its case sensitivity and its
    # default for a payload that does not say.
    assert module._drop_disposition(
        "quantity_unit", "share", {"asset_type": "Stock"}, {}, {}
    ) == ("lost", "no_declared_carrier")
    assert module._drop_disposition("quantity_unit", "share", {}, {}, {}) == (
        "lost",
        "no_declared_carrier",
    )
    assert module._lot_shape_keys("Stock") == module.LOT_SHAPE_KEYS_COMMON


def test_final_lot_identity_rejects_null(tmp_path: Path) -> None:

    path = tmp_path / "null-identity.sqlite3"
    SQLiteOptionPositionsRepository(path)
    with connect_ledger_fixture(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            conn.execute(
            """
            INSERT INTO position_lots (lot_id, account, fields_json, updated_at_ms)
            VALUES (NULL, 'lx', ?, 1)
            """,
                (json.dumps({"contract_key": {"account": "lx"}}),),
            )




# --- the retired-column registry gate ------------------------------------------
#
# D1/D2's rebuild and D3/D4's payload rewrite are destructive, and §13.5 R7 fixes
# what makes them safe: the build that keeps running must be able to read the
# shape it leaves behind. On window day that build is R1 — its statements are
# repointed while the column contract is deliberately still dual-shape, because
# the contract-tightening release (§9.5 M6 step 5) is R2, after the window — so
# the gate is read from the pinned statement ledger, never from a flag: the
# release that repoints the SQL is the release that enables the migration.








# --- D1/D2: the rebuild -------------------------------------------------------
















def test_legacy_payload_without_asset_type_uses_replay_for_mixed_lots(
    tmp_path: Path,
) -> None:
    """Asset type is read off the replay when the payload no longer carries it.

    The inventory is the surface that has to answer for a store whose rows lost
    ``asset_type``; the shape-key dispatch cannot, so the answer comes from
    replaying the ledger instead of from a default.
    """
    path = _legacy_store(tmp_path)
    _restore_legacy_flat_keys(path)

    for record_id in _stored_rows(path):
        _edit_lot_fields(path, record_id, _strip_converged_shape)

    inventory = module.build_lot_identity_migration_inventory(path)
    assert inventory["asset_type_resolution"] == {
        "status": "resolved",
        "resolved_rows": 4,
        "blocked_rows": 0,
        "sample_blocked_lot_ids": [],
    }












# --- D3/D4: the payload rewrite ----------------------------------------------


def test_the_rewrite_executes_only_carriers_the_classifier_declares() -> None:
    """The rewrite runs the vocabulary instead of restating it.

    Every path the traversal writes into must be the carrier string
    ``CARRIED_DROPPED_KEYS`` already declared for that key. A key the
    classifier calls carried but the rewrite has no path for is a fact the
    rewrite would drop while the gate reported it carried — the one failure mode
    §13.5 R6 is about.
    """

    assert module.CARRIER_TARGETS  # vacuous if the traversal stopped carrying
    for key, path in module.CARRIER_TARGETS.items():
        declared = module.CARRIED_DROPPED_KEYS.get(key)
        assert declared == ".".join(path), key
        # The identical check the classifier runs, so the pair cannot be
        # "consistent" only for the keys nobody drops.
        assert module._drop_disposition(
            key, "x", {key: "x", "expiration_ymd": "2027-01-15"}, {}, {}
        )[0] in {"carried", "reconstructible"}

    # And the ones the traversal deliberately leaves alone, named rather than
    # left to look forgotten.
    unhandled = {
        key
        for key in module.CARRIED_DROPPED_KEYS
        if key not in module.CARRIER_TARGETS
        and key not in module._lot_shape_keys("option")
        and key not in module._lot_shape_keys("stock")
    }
    assert unhandled == {"position_id", "source_event_id", "last_close_event_id"}
    # ``quantity_unit`` is declared per asset type and is carried by the target's
    # own ``asset_type`` + ``shares_*`` emission, so it is not a key the
    # traversal has to write anywhere.
    assert module.CARRIED_DROPPED_KEYS_BY_ASSET_TYPE == {
        "quantity_unit": {"stock": "asset_type + shares_*"}
    }






def test_the_ms_expiration_mirror_is_carried_by_one_shared_rule() -> None:
    """One fact, two vocabularies, and the gate reads the same rule as the rewrite.

    D1 retires the ms mirror and the carrier is ``contract_key.expiration_ymd``,
    so the row's ymd is the authority and the mirror is converted only when it
    is the only copy. Both the classifier and the rewrite call this rule, so
    ``carried`` cannot mean one thing to the gate and another to the write — and
    a value that converts to nothing is ``lost`` rather than carried to a target
    the rewrite cannot fill.
    """

    assert module._drop_disposition(
        "expiration_ymd", "2026-06-19", {"expiration_ymd": "2026-06-19"}, {}, {}
    ) == ("carried", "contract_key.expiration_ymd")
    # The ymd beside the mirror is the authority, so the mirror is redundant.
    assert module._expiration_carrier(
        {"expiration_ymd": "2026-06-19", "expiration": 1}
    ) == "2026-06-19"
    # The mirror alone still carries the fact: the conversion is the domain's,
    # timezone included (``expiration_timestamp_to_ymd``).
    assert module._expiration_carrier({"expiration": 1_781_827_200_000}) == "2026-06-19"
    assert module._drop_disposition(
        "expiration", 1_781_827_200_000, {"expiration": 1_781_827_200_000}, {}, {}
    ) == ("carried", "contract_key.expiration_ymd")
    # A mirror that is not a timestamp at all carries nothing, and saying
    # "carried" about it is how D3 would drop the row's expiration.
    assert module._expiration_carrier({"expiration": 0}) is None
    assert module._drop_disposition("expiration", 0, {"expiration": 0}, {}, {}) == (
        "lost",
        "no_declared_carrier",
    )
    # The rewrite reads the same helper, so the two agree by construction rather
    # than by two rules that happen to match today.
    assert module._carried_value("expiration", {"expiration": 1_781_827_200_000}) == (
        "2026-06-19"
    )
    assert module._carried_value(
        "expiration", {"expiration_ymd": "2026-06-19", "expiration": 1}
    ) is None  # the ymd wrote that target already






def test_alignment_preserves_existing_nested_payload():
    fields = {"asset_type": "option", "account": "lx", "contract_key": {"strike": "100"}}
    aligned = module._aligned_lot_payload(fields, {}, {})
    assert aligned["contract_key"] == {"strike": "100", "account": "lx"}
    assert fields["contract_key"] == {"strike": "100"}
    fields["contract_key"] = "invalid-but-present"
    with pytest.raises(RuntimeError, match="carrier_value_conflict"):
        module._aligned_lot_payload(fields, {}, {})




@pytest.mark.parametrize("status", ["duplicate_lot_id", "empty_lot_id", "count_mismatch"])
def test_verify_propagates_every_comparator_blocker(tmp_path, monkeypatch, status):
    # Exercise the report boundary independently of the comparator: the latter
    # may add blocking statuses without extending the legacy lot-only counters.
    monkeypatch.setattr(
        module, "compare_projection_lots",
        lambda **kwargs: {"summary": {status: 1}, "items": [{"status": status}]},
    )
    report = module.verify_lot_identity_migration(_legacy_store(tmp_path))
    assert report["ok"] is False
    assert "projection_replay_mismatch" in report["readiness_reasons"]
    assert report["projection"]["mismatch_count"] == 1






