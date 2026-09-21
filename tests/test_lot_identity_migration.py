"""D1–D4 lot-identity migration (§13.3 slice 3).

Every fixture here is built through the real write paths and only then
degraded to the pre-migration shape. The point of the batch is a store that
already exists in production, so a hand-written ``fields_json`` would prove
that the migration handles *my* idea of the legacy payload, not the payload the
publisher actually produces.
"""

from __future__ import annotations

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

    with sqlite3.connect(path) as conn:
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
                    "strike": contract_key.get("strike"),
                    "contracts": fields.get("contracts_opened"),
                    "premium": fields.get("premium_open"),
                    "opened_at": fields.get("opened_at_ms"),
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


def _lot_option_type(fields: dict) -> str:
    """The contract's option type under the converged shape.

    The payload carries the contract under ``contract_key`` now
    (``write-side-definition.md`` §2); the retired flat sibling stays readable
    for a row written before the shape switch.
    """
    contract_key = fields.get("contract_key")
    contract_key = contract_key if isinstance(contract_key, dict) else {}
    return str(contract_key.get("option_type") or fields.get("option_type") or "")


def _legacy_store(tmp_path: Path, *, name: str = "ledger.sqlite3") -> Path:
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

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE position_lots SET lot_id = NULL")
        for lot_id, raw in conn.execute(
            "SELECT record_id, fields_json FROM position_lots"
        ).fetchall():
            fields = json.loads(raw)
            fields["position_id"] = f"LEGACY-{lot_id}"
            conn.execute(
                "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
                (
                    json.dumps(fields, ensure_ascii=False, sort_keys=True, allow_nan=False),
                    lot_id,
                ),
            )
        conn.commit()
    return path


def _edit_lot_fields(path: Path, record_id: str, mutate) -> None:
    """Rig one lot's ``fields_json`` the way a degraded store carries it."""

    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            "SELECT record_id, fields_json FROM position_lots WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        fields = json.loads(raw[1])
        mutate(fields)
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True), raw[0]),
        )
        conn.commit()


def _stored_rows(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
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


def _run_apply(path: Path, **kwargs: object) -> dict[str, object]:
    inventory = module.build_lot_identity_migration_inventory(path)
    return module.apply_lot_identity_migration(path, inventory, **kwargs)


@pytest.fixture(autouse=True)
def _lot_identity_window_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Carry the window token the machinery tests run behind.

    ``apply`` refuses to run while ``LOT_IDENTITY_WINDOW_ENABLEMENT`` is unset;
    the rest of this module exercises the migration machinery itself, so it
    runs armed. The unarmed refusal is pinned by
    ``test_apply_refuses_while_the_window_is_not_enabled``, which clears the
    token again.
    """

    monkeypatch.setattr(module, "LOT_IDENTITY_WINDOW_ENABLEMENT", "test-window-token")


def test_apply_refuses_while_the_window_is_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No window token on the build: refuse before opening the store.

    The manifest checks all pass on this store — the refusal must not depend
    on them, and must not so much as open a connection: the file keeps the
    exact bytes it had.
    """

    monkeypatch.setattr(module, "LOT_IDENTITY_WINDOW_ENABLEMENT", None)
    path = _legacy_store(tmp_path)
    inventory = module.build_lot_identity_migration_inventory(path)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="not enabled on this build"):
        module.apply_lot_identity_migration(path, inventory)

    assert path.read_bytes() == before


@pytest.fixture
def repointed_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """An R1 build: its live SQL no longer names any retired column.

    On window day the release that runs ``apply`` is the one whose statements
    have been repointed, and the gate reads the pinned registry to prove it.
    This tree is pre-R1 — its ledger names live statements — so the window's
    own answer is supplied here; the real read is pinned by
    ``test_apply_defers_the_destructive_half_until_the_sql_is_repointed``.
    """

    monkeypatch.setattr(module, "_live_sql_naming_retired_columns", lambda: ())


def _table_info(path: Path, table: str = "position_lots") -> list[sqlite3.Row]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _object_sql(path: Path, kind: str, table: str = "position_lots") -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            str(row["name"]): " ".join(str(row["sql"]).split())
            for row in conn.execute(
                f"SELECT name, sql FROM sqlite_master WHERE type=? AND tbl_name=?"
                " AND sql IS NOT NULL ORDER BY name",
                (kind, table),
            )
        }


def _columns(path: Path, table: str = "position_lots") -> list[str]:
    return [str(row["name"]) for row in _table_info(path, table)]


def _primary_key(path: Path, table: str = "position_lots") -> str | None:
    return next(
        (str(row["name"]) for row in _table_info(path, table) if int(row["pk"]) == 1),
        None,
    )


def _rebuilt_rows(path: Path) -> dict[str, dict[str, object]]:
    """Every lot row of a store on the rebuilt shape, keyed by ``lot_id``."""

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            str(row["lot_id"]): {
                "account": row["account"],
                "source_event_id": row["source_event_id"],
                "fields": json.loads(row["fields_json"]),
                "fields_json": row["fields_json"],
            }
            for row in conn.execute(
                "SELECT lot_id, account, source_event_id, fields_json FROM position_lots"
            )
        }


def _new_shape_guard(sql: str) -> str:
    """The declared three edits the rebuild applies to a stored guard.

    Spelled out here rather than imported so the pin is independent of the
    implementation: whatever the migration does, the guards on the rebuilt table
    must be the store's own guards with the identity renamed, the retired
    column forgotten, and the account read where the target shape keeps it.
    """

    rewritten = re.sub(r"\brecord_id\b", "lot_id", sql)
    rewritten = re.sub(r"\s+OR\s+OLD\.expiration IS NOT NEW\.expiration", "", rewritten)
    rewritten = re.sub(r"\s*,\s*expiration\b", "", rewritten)
    return " ".join(rewritten.replace("'$.account'", "'$.contract_key.account'").split())


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

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        record_id = str(
            conn.execute("SELECT record_id FROM position_lots").fetchone()["record_id"]
        )
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

    assert set(module.EVENT_LAYER_MEASURED_DROPPED_KEYS) == set(
        POSITION_LOT_STRATEGY_PATCH_FIELDS
    )
    assert set(module.EVENT_LAYER_MEASURED_DROPPED_KEYS) <= set(
        module.RECONSTRUCTIBLE_DROPPED_KEYS
    )


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
    with sqlite3.connect(path) as conn:
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
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE position_lots SET multiplier = NULL WHERE record_id = ?",
            (lot_id,),
        )
        conn.commit()

    without_column = module.build_lot_identity_migration_inventory(path)[
        "dropped_key_classification"
    ]
    assert without_column["lost"]["note"]["reason"] == "note_kv_only:multiplier"


def test_an_empty_store_does_not_report_a_missing_column(tmp_path: Path) -> None:
    """Column presence comes from the table, not from the rows.

    Inferring it from ``rows[0]`` made an empty ``position_lots`` report every
    column as absent while the ``column_contract`` in the same payload said the
    opposite — a ``lot_id_column_missing`` reason on a store that has the column,
    and an ``ensure_lot_id_column: applied`` step for a DDL no-op.
    """

    path = tmp_path / "empty.sqlite3"
    SQLiteOptionPositionsRepository(path)

    inventory = module.build_lot_identity_migration_inventory(path)

    assert inventory["counts"]["position_lots"] == 0
    assert inventory["column_contract"]["position_lots"]["missing"] == []
    assert inventory["readiness_reasons"] == []
    assert inventory["pending"]["d2_lot_id_column"] == {
        "column_present": True, "rows_null": 0,
    }

    result = module.apply_lot_identity_migration(path, inventory)
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["ensure_lot_id_column"]["status"] == "already_present"
    assert result["write_applied"] is False


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
    the payloads themselves. ``apply`` first, so the corruption is the only
    thing left that differs from a fresh replay — and ``apply`` never touches
    ``shares_*``, so nothing about the rigged numbers is being repaired.
    """

    path = _legacy_store(tmp_path)
    _run_apply(path)
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
    with sqlite3.connect(path) as conn:
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

    with sqlite3.connect(path) as conn:
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


def test_end_to_end_inventory_verify_apply_verify(tmp_path: Path) -> None:
    """§13.4 row 3: the designed run, ending on a store that verifies clean."""

    path = _legacy_store(tmp_path)

    before = module.verify_lot_identity_migration(path)
    assert before["ok"] is False
    assert before["readiness_reasons"] == [
        "lot_id_backfill_pending",
        "projection_replay_mismatch",
    ]

    result = _run_apply(path)

    assert result["schema_version"] == module.APPLY_SCHEMA
    assert result["write_applied"] is True
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["backfill_lot_id"] == {
        "item": "D2", "step": "backfill_lot_id", "status": "applied", "rows_updated": 4,
    }
    assert steps["strip_position_id_from_fields_json"]["rows_updated"] == 4
    assert steps["ensure_lot_id_column"]["status"] == "already_present"
    for deferred in (
        "switch_primary_key_to_lot_id_and_drop_record_id",
        "drop_expiration_column",
        "rewrite_fields_json_to_lot_shape",
    ):
        assert steps[deferred]["status"] == "deferred"
    assert steps["drop_expiration_column"]["reason"] == "live_sql_repointing_precedes_rebuild"
    assert steps["rewrite_fields_json_to_lot_shape"]["reason"] == (
        "live_sql_repointing_precedes_rebuild"
    )
    assert result["deferred_rebuild_recipe"]

    # D4 rewrites fields_json, which is in the generation trigger's
    # AFTER UPDATE OF list, so the republish requirement is not optional.
    assert result["projection_heads_advanced"]["accounts"] == ["lx", "sy"]
    # A real entry point, not a placeholder name: the follow-up has to be
    # runnable, and the CLI is where this store is republished from.
    assert result["required_follow_up"] == ["om option-positions rebuild"]

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    rows = _stored_rows(path)
    assert len(rows) == 4
    assert all(row["lot_id"] == lot_id for lot_id, row in rows.items())
    assert all("position_id" not in row["fields"] for row in rows.values())

    after = module.verify_lot_identity_migration(path)
    assert after["ok"] is True
    assert after["readiness_reasons"] == []
    assert after["projection"]["summary"] == {"matched": 4}
    assert after["blocking_keys"] == []


def test_apply_rolls_back_everything_on_failure(tmp_path: Path) -> None:
    """§13.4 row 3's failure path, checked against the bytes that were there."""

    path = _legacy_store(tmp_path)
    before = _stored_rows(path)
    inventory = module.build_lot_identity_migration_inventory(path)

    stages: list[str] = []

    def _hook(stage: str) -> None:
        stages.append(stage)
        if stage == "before_commit":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        module.apply_lot_identity_migration(path, inventory, failure_hook=_hook)

    assert stages == [
        "after_manifest_recheck",
        "after_schema",
        "after_lot_id_backfill",
        "after_position_id_strip",
        "before_commit",
    ]
    assert _stored_rows(path) == before
    assert all(row["lot_id"] is None for row in _stored_rows(path).values())
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_apply_refuses_a_stale_or_foreign_manifest(tmp_path: Path) -> None:
    """The two refusals are distinct, because they need different recoveries.

    A shared message would leave the operator to guess whether the store is the
    wrong one or merely moved on, and the named drift is what makes the second
    case actionable.
    """

    path = _legacy_store(tmp_path)
    inventory = module.build_lot_identity_migration_inventory(path)

    # Re-signed, so the refusal below is the store-binding gate and not the
    # tamper check that runs first: a manifest can be internally consistent and
    # still describe a store that has moved on.
    stale = module._manifest(
        {
            **{k: v for k, v in inventory.items() if k != "manifest_hash"},
            "inventory_fingerprint": "0" * 64,
        }
    )
    with pytest.raises(ValueError, match="is stale: the store changed") as stale_error:
        module.apply_lot_identity_migration(path, stale)
    assert "another store" not in str(stale_error.value)

    other = _legacy_store(tmp_path, name="other.sqlite3")
    other_inventory = module.build_lot_identity_migration_inventory(other)
    with pytest.raises(ValueError, match="belongs to another store"):
        module.apply_lot_identity_migration(path, other_inventory)

    # The refusals above must not have half-applied anything.
    assert all(row["lot_id"] is None for row in _stored_rows(path).values())


def test_open_path_schema_transition_needs_a_fresh_inventory(tmp_path: Path) -> None:
    """The one drift this migration causes itself, named rather than mislabelled.

    ``_ensure_position_projection_schema`` runs from the ordinary writer open
    path, so the first writer to touch a pre-carrier store adds ``lot_id`` and
    its index — which is exactly the state ``inventory`` reports as
    ``not_ready`` on purpose. That moves the fingerprint while ``store_identity``
    stays equal, so an inventory taken before that first open is refused. Blaming
    "another store" would send the operator after the wrong problem; the refusal
    has to say what moved, and re-inventorying has to be the whole recovery.
    """

    path = _legacy_store(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_position_lots_lot_id")
        conn.execute("ALTER TABLE position_lots DROP COLUMN lot_id")
        conn.commit()

    frozen = module.build_lot_identity_migration_inventory(path)
    assert "lot_id_column_missing" in frozen["readiness_reasons"]

    # An ordinary writer open that writes no data at all.
    SQLiteOptionPositionsRepository(path)

    with pytest.raises(ValueError, match="is stale: the store changed") as error:
        module.apply_lot_identity_migration(path, frozen)
    assert "column_contract" in str(error.value)
    assert "re-run inventory" in str(error.value)
    assert all(row["lot_id"] is None for row in _stored_rows(path).values())

    # Re-inventorying the store as it now is, is the whole recovery — and it does
    # not take a second writer open, because the transition is once per store.
    result = _run_apply(path)
    assert {step["step"]: step["status"] for step in result["steps"]} == {
        "ensure_lot_id_column": "already_present",
        "backfill_lot_id": "applied",
        "strip_position_id_from_fields_json": "applied",
        "switch_primary_key_to_lot_id_and_drop_record_id": "deferred",
        "drop_expiration_column": "deferred",
        "rewrite_fields_json_to_lot_shape": "deferred",
    }
    assert module.verify_lot_identity_migration(path)["ok"] is True


def test_apply_rejects_a_manifest_of_the_wrong_schema(tmp_path: Path) -> None:
    """Two ``apply`` commands share a name; the schema is the discriminator."""

    path = _legacy_store(tmp_path)
    report = module.verify_lot_identity_migration(path)

    with pytest.raises(ValueError):
        module.apply_lot_identity_migration(path, report)


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """A second run finds nothing to do, and *says so* instead of failing.

    The assertion is on the status, not only on the row count: a constant
    ``"applied"`` reports a re-run of a migrated store exactly like the migration
    itself, which leaves an operator or a follow-up automation unable to tell
    "already done" from "just did it".
    """

    path = _legacy_store(tmp_path)
    first = _run_apply(path)
    assert first["write_applied"] is True
    assert first["steps_changed"] == [
        "backfill_lot_id",
        "strip_position_id_from_fields_json",
    ]

    second = _run_apply(path)

    steps = {step["step"]: step for step in second["steps"]}
    assert steps["backfill_lot_id"] == {
        "item": "D2",
        "step": "backfill_lot_id",
        "status": "already_satisfied",
        "rows_updated": 0,
    }
    assert steps["strip_position_id_from_fields_json"]["status"] == "already_satisfied"
    assert steps["strip_position_id_from_fields_json"]["rows_updated"] == 0
    assert second["write_applied"] is False
    assert second["steps_changed"] == []
    assert module.verify_lot_identity_migration(path)["ok"] is True


def test_apply_backfills_the_carrier_on_a_store_that_never_had_the_column(
    tmp_path: Path,
) -> None:
    """§13.2 row 7's gated backfill: the column may be missing entirely."""

    path = _legacy_store(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_position_lots_lot_id")
        conn.execute("ALTER TABLE position_lots DROP COLUMN lot_id")
        conn.commit()

    inventory = module.build_lot_identity_migration_inventory(path)
    assert "lot_id_column_missing" in inventory["readiness_reasons"]
    assert inventory["pending"]["d2_lot_id_column"] == {
        "column_present": False, "rows_null": 4,
    }

    result = module.apply_lot_identity_migration(path, inventory)
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["ensure_lot_id_column"]["status"] == "applied"
    assert steps["backfill_lot_id"]["rows_updated"] == 4
    assert module.verify_lot_identity_migration(path)["ok"] is True


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


def test_both_lot_readers_agree_on_a_null_identity(tmp_path: Path) -> None:
    """``str(None)`` is the string "None" — a fabricated identity, not an absent one.

    Two readers emit the same row: ``position_lot_row_to_record`` on the write
    path and ``read_only_evidence._read_position_lots`` on the read-only evidence
    surface. ``record_id`` is a TEXT primary key, which SQLite permits to be NULL,
    and the post-rebuild shape reaches the same state by having no such column at
    all. A reader that fabricates ``"None"`` makes one row carry two different
    identities depending on who asks, and ``lot_id`` inherits the fallback, so it
    diverges in the same way.
    """

    path = tmp_path / "null-identity.sqlite3"
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO position_lots (record_id, account, fields_json, updated_at_ms)
            VALUES (NULL, 'lx', ?, 1)
            """,
            (json.dumps({"account": "lx"}),),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT record_id, lot_id, fields_json, expiration, strike, multiplier
            FROM position_lots
            """
        ).fetchone()

    codec_record = position_lot_row_to_record(row)
    [evidence_lot] = open_trade_reconciliation_evidence_repo(path).list_position_lots()

    assert codec_record["record_id"] == ""
    assert evidence_lot["record_id"] == ""
    # Both keys, because ``lot_id`` is the fallback for the same NULL.
    assert codec_record["lot_id"] == ""
    assert evidence_lot["lot_id"] == ""


def test_the_post_rebuild_shape_reports_instead_of_crashing(tmp_path: Path) -> None:
    """The shape this module is the readiness surface for must not crash it.

    ``REBUILD_RECIPE`` step 4 lands ``lot_id`` as the primary key and drops
    ``record_id``. No current write path can produce that shape, so it is built
    here by hand — and what is under test is the column probes, not the payload
    handling that every other fixture in this file exercises through the real
    writers.
    """

    path = tmp_path / "rebuilt.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE position_lots (
              lot_id TEXT PRIMARY KEY,
              account TEXT,
              fields_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO position_lots (lot_id, account, fields_json) VALUES (?, ?, ?)",
            ("lot-1", "lx", json.dumps({"account": "lx", "position_id": "LEGACY-1"})),
        )
        conn.commit()

    inventory = module.build_lot_identity_migration_inventory(path)

    assert inventory["counts"]["position_lots"] == 1
    assert inventory["readiness_reasons"] == [
        "column_contract_open",
        "record_id_column_missing",
    ]
    assert inventory["pending"]["d2_record_id_column"]["column_present"] is False
    assert inventory["dropped_key_classification"]["lost"] == {}

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        # Both writers behind ``apply`` key off ``record_id``, which is not
        # there, so they must fall back to the identity column that is — a
        # rebuild leaves exactly this shape and still runs them afterwards.
        assert module._backfill_lot_id(conn) == 0
        assert module._rewrite_lot_payloads(conn, align_to_lot_shape=False) == {
            "rows_scanned": 1,
            "position_id_rows": 1,
            "rewritten_rows": 1,
        }
        remaining = json.loads(
            conn.execute("SELECT fields_json FROM position_lots").fetchone()["fields_json"]
        )
    assert "position_id" not in remaining


# --- the retired-column registry gate ------------------------------------------
#
# D1/D2's rebuild and D3/D4's payload rewrite are destructive, and §13.5 R7 fixes
# what makes them safe: the build that keeps running must be able to read the
# shape it leaves behind. On window day that build is R1 — its statements are
# repointed while the column contract is deliberately still dual-shape, because
# the contract-tightening release (§9.5 M6 step 5) is R2, after the window — so
# the gate is read from the pinned statement ledger, never from a flag: the
# release that repoints the SQL is the release that enables the migration.


def test_the_destructive_steps_are_deferred_while_the_sql_names_them(
    tmp_path: Path,
) -> None:
    """Both directions of the gate, from one store, with nothing but the gate changed.

    The reasons are asserted by name, not only the statuses: they are what an
    operator reads to learn *why* the window has not opened, and a status that
    became ``deferred`` for a different reason is a different instruction.
    """

    path = _legacy_store(tmp_path)
    # This tree is pre-R1, so its own ledger still lists live statements and
    # that — not the column contract, which the window release deliberately
    # keeps dual-shape — is what the gate reads.
    live = module._live_sql_naming_retired_columns()
    assert live

    result = _run_apply(path)

    steps = {step["step"]: step for step in result["steps"]}
    for deferred in (
        "switch_primary_key_to_lot_id_and_drop_record_id",
        "drop_expiration_column",
    ):
        assert steps[deferred]["status"] == "deferred"
        assert steps[deferred]["reason"] == "live_sql_repointing_precedes_rebuild"
    assert steps["rewrite_fields_json_to_lot_shape"]["status"] == "deferred"
    assert steps["rewrite_fields_json_to_lot_shape"]["reason"] == (
        "live_sql_repointing_precedes_rebuild"
    )
    # The receipt names the cause statement by statement, so an operator can
    # read which repointing is missing instead of only that something is.
    assert result["rebuild_gate"]["live_statements"] == list(live)
    # The store keeps the shape it had: the gate closed the destructive half,
    # not just its report.
    assert "record_id" in _columns(path)
    assert "expiration" in _columns(path)
    assert _primary_key(path) == "record_id"


def test_repointing_the_sql_is_what_enables_the_destructive_steps(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """The gate is derived state, so nothing else has to be flipped with it.

    The same store, the same code, the same command: only the build's statements
    moved — which is exactly the difference between this tree and R1. The
    contract-tightening release (§9.5 M6 step 5) is a later edit this gate does
    not wait for, and a flag would be the wrong shape for it.
    """

    path = _legacy_store(tmp_path)
    assert "record_id" in _columns(path)

    result = _run_apply(path)

    steps = {step["step"]: step for step in result["steps"]}
    for applied in (
        "switch_primary_key_to_lot_id_and_drop_record_id",
        "drop_expiration_column",
        "rewrite_fields_json_to_lot_shape",
    ):
        assert steps[applied]["status"] == "applied"
        assert "reason" not in steps[applied]
    assert result["rebuild"]["rebuilt"] is True
    assert "record_id" not in _columns(path)
    assert _primary_key(path) == "lot_id"


def test_the_closed_gates_receipt_carries_the_pinned_key_set(
    tmp_path: Path,
) -> None:
    """The pre-window receipt's key set is a contract this batch must not drop.

    The whole result is pinned, key set included: an operator's follow-up
    automation keys on these names, and a window that ships a differently shaped
    receipt has changed the surface it promised not to touch. The destructive
    three are ``deferred`` with the repointing reason, ``rebuild_gate`` names
    what held them back, ``write_applied`` still means "something changed", and
    no rebuild report appears at all.
    """

    path = _legacy_store(tmp_path)
    result = _run_apply(path)

    assert sorted(result) == [
        "deferred_rebuild_recipe",
        "generated_at_utc",
        "manifest_hash",
        "operation",
        "pending_before",
        "projection_heads_advanced",
        "rebuild_gate",
        "required_follow_up",
        "schema_version",
        "source_manifest_hash",
        "sqlite_bytes",
        "steps",
        "steps_changed",
        "store_identity",
        "timing",
        "write_applied",
    ]
    assert result["steps"] == [
        {"item": "D2", "step": "ensure_lot_id_column", "status": "already_present"},
        {
            "item": "D2",
            "step": "backfill_lot_id",
            "status": "applied",
            "rows_updated": 4,
        },
        {
            "item": "D4",
            "step": "strip_position_id_from_fields_json",
            "status": "applied",
            "rows_updated": 4,
        },
        {
            "item": "D2",
            "step": "switch_primary_key_to_lot_id_and_drop_record_id",
            "status": "deferred",
            "reason": "live_sql_repointing_precedes_rebuild",
        },
        {
            "item": "D1",
            "step": "drop_expiration_column",
            "status": "deferred",
            "reason": "live_sql_repointing_precedes_rebuild",
        },
        {
            "item": "D3",
            "step": "rewrite_fields_json_to_lot_shape",
            "status": "deferred",
            "reason": "live_sql_repointing_precedes_rebuild",
        },
    ]
    assert result["write_applied"] is True
    assert result["steps_changed"] == [
        "backfill_lot_id",
        "strip_position_id_from_fields_json",
    ]
    # The report is the visible half; this is the other half. While the gate is
    # closed the payload keeps the converged shape the readers still consume —
    # the D4 cleanup is the only edit — and ``record_id`` is still the key.
    for row in _stored_rows(path).values():
        assert "position_id" not in row["fields"]
        assert row["fields"]["contract_key"]["account"]
        assert row["fields"]["contract_key"]["underlying_symbol"]
    assert _primary_key(path) == "record_id"
    assert _columns(path) == [
        "record_id",
        "account",
        "fields_json",
        "source_event_id",
        "expiration",
        "strike",
        "multiplier",
        "updated_at_ms",
        "lot_id",
    ]


# --- D1/D2: the rebuild -------------------------------------------------------


def test_the_rebuild_leaves_the_store_on_the_contracted_shape(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """§13.4 A8: the rebuilt store's column set *is* the contract minus the retired
    columns.

    The expectations come from ``POSITION_LOTS_COLUMN_CLASSIFICATION`` minus
    ``RETIRED_LOT_COLUMNS`` — the shape R1 must keep able to read while its
    contract is still dual-shape — so the shape the rebuild produces and the
    shape the publish path gates on cannot drift apart. On this tree the check
    still describes the pre-window shape and reports exactly the retired columns
    as the delta; A2's repointing batch relaxes it to accept both shapes, and it
    owns that half of the pin from there.
    """

    path = _legacy_store(tmp_path)
    before = _stored_rows(path)
    _run_apply(path)

    # The store's own column order, with the identity promoted to the front and
    # nothing else moved: a rebuild that reshuffled the table would show up here
    # as a difference in a SELECT * row rather than only in a set.
    assert _columns(path) == [
        "lot_id",
        "account",
        "fields_json",
        "source_event_id",
        "strike",
        "multiplier",
        "updated_at_ms",
    ]
    assert set(_columns(path)) == set(module.POSITION_LOTS_COLUMN_CLASSIFICATION) - set(
        module.RETIRED_LOT_COLUMNS
    )
    assert _primary_key(path) == "lot_id"
    assert set(module.RETIRED_LOT_COLUMNS) & set(_columns(path)) == set()

    rows = _rebuilt_rows(path)
    assert len(rows) == len(before) == 4
    assert sorted(rows) == sorted(before)
    # Read-back equality of the columns that carry facts: the identity moved to
    # lot_id, and everything else is the byte the old row held — compared
    # against the old row's own columns, which is where both facts live now
    # that the payload is converged.
    for lot_id, row in rows.items():
        assert row["source_event_id"] == before[lot_id]["source_event_id"]
        assert row["account"] == before[lot_id]["account"]

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check(position_lots)").fetchall() == []
        # The contract check the publish path runs reads the pre-window shape on
        # this tree, so the rebuilt store shows up as exactly the retired
        # columns missing and nothing unclassified: the rebuilt store *is* the
        # contract minus them. A2's repointing batch is what relaxes this check
        # to accept both shapes (R1 stays dual-shape by design), and it owns
        # this assertion from there.
        from src.application.ledger.repository_projection_schema import (
            _position_projection_column_contract,
        )

        contract = _position_projection_column_contract(conn)["position_lots"]
        assert set(contract["missing"]) == set(module.RETIRED_LOT_COLUMNS)
        assert contract["unclassified"] == ()


def test_the_rebuild_recreates_the_stores_own_guards_on_the_new_shape(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """Step 11 recreates the guards the store is running, not a copy of them.

    The pin is a derivation: take the triggers and indexes the store had before
    the rebuild, apply the three declared edits, and require the rebuilt store's
    to be exactly that. A hand-written guard set in the migration would fail
    here the moment the real one moved, which is the drift the derivation exists
    to prevent.
    """

    path = _legacy_store(tmp_path)
    guards_before = _object_sql(path, "trigger")
    indexes_before = _object_sql(path, "index")
    assert len(guards_before) == 7
    assert set(guards_before) == {
        "trg_position_lots_account_insert_guard",
        "trg_position_lots_account_update_guard",
        "trg_position_lots_generation_insert",
        "trg_position_lots_generation_delete",
        "trg_position_lots_generation_update_same",
        "trg_position_lots_generation_update_old",
        "trg_position_lots_generation_update_new",
    }

    result = _run_apply(path)

    guards_after = _object_sql(path, "trigger")
    assert set(guards_after) == set(guards_before)
    for name, sql in guards_after.items():
        assert sql == _new_shape_guard(guards_before[name]), name
        assert "record_id" not in sql
        assert "expiration" not in sql
        assert "'$.account'" not in sql
    assert result["rebuild"]["triggers_recreated"] == 7

    # The identity index survives — the open path re-creates it with
    # ``CREATE UNIQUE INDEX IF NOT EXISTS`` on every open, so a rebuilt store
    # that dropped it would churn its schema cookie on the next open.
    assert "UNIQUE" in _object_sql(path, "index")["idx_position_lots_lot_id"]
    # An index whose column list was only ``(expiration, record_id)`` has no
    # shape left; the account lookup index keeps its name and loses the retired
    # column, and its duplicate (``(account, record_id)``) collapses into it.
    assert set(_object_sql(path, "index")) == {
        "idx_position_lots_lot_id",
        "idx_position_lots_account_expiration",
    }
    assert _object_sql(path, "index")["idx_position_lots_account_expiration"].endswith(
        "ON position_lots(account)"
    )


def test_the_recreated_guards_accept_the_new_shape_and_reject_a_conflict(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """A guard that cannot fire on the shape it guards is not a guard.

    The old bodies read the *flat* ``$.account``; the target shape carries the
    account at ``contract_key.account``, so a verbatim recreation would reject
    every write of an aligned row — the rebuilt store would be unwritable. The
    pairs below are what "recreated on the new shape" has to mean.
    """

    path = _legacy_store(tmp_path)
    _run_apply(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        generation = conn.execute(
            "SELECT lots_generation FROM position_projection_heads WHERE account='lx'"
        ).fetchone()["lots_generation"]
        fields = json.dumps(
            {
                "contract_key": {"account": "lx", "underlying_symbol": "AMD"},
                "position_key": "富途|lx|AMD|stock|long",
            }
        )
        conn.execute(
            "INSERT INTO position_lots (lot_id, account, fields_json, updated_at_ms)"
            " VALUES (?, ?, ?, ?)",
            ("lot-new", "lx", fields, 4000),
        )
        conn.commit()
        assert [
            int(row["lots_generation"])
            for row in conn.execute(
                "SELECT lots_generation FROM position_projection_heads WHERE account='lx'"
            )
        ] == [int(generation) + 1]

        # The account guard still has teeth on the new shape: the column and the
        # payload must agree, and the payload must carry one at all.
        with pytest.raises(sqlite3.IntegrityError, match="must be lowercase"):
            conn.execute(
                "INSERT INTO position_lots (lot_id, account, fields_json, updated_at_ms)"
                " VALUES (?, ?, ?, ?)",
                (
                    "lot-bad",
                    "lx",
                    json.dumps({"contract_key": {"account": "LX"}}),
                    4001,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="account is required"):
            conn.execute(
                "INSERT INTO position_lots (lot_id, account, fields_json, updated_at_ms)"
                " VALUES (?, ?, ?, ?)",
                ("lot-empty", "lx", json.dumps({"position_key": "x"}), 4002),
            )


@pytest.mark.parametrize(
    "stage",
    [
        "after_rebuild_rows_read",
        "after_rebuild_insert",
        "after_rebuild_row_count_check",
        "after_rebuild_read_back_check",
        "after_rebuild_foreign_key_check",
        "after_rebuild_drop_old_table",
        "after_rebuild_rename",
        "after_rebuild_guards",
        "after_rebuild",
    ],
)
def test_a_failure_at_any_rebuild_check_leaves_the_store_byte_identical(
    tmp_path: Path,
    repointed_build: None,
    stage: str,
) -> None:
    """§13.4 A9: no half-migrated table survives a failure, at any checkpoint.

    The last stages are the ones worth having: by then the old table is dropped
    and the new one renamed, so anything short of a real transaction would leave
    a store that is neither shape. The comparison is on the file's bytes, since
    "the old shape and the old rows are still there" is weaker than "nothing was
    written".
    """

    path = _legacy_store(tmp_path)
    before = path.read_bytes()
    original_columns = _columns(path)
    original_rows = _stored_rows(path)

    def _hook(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        _run_apply(path, failure_hook=_hook)

    assert path.read_bytes() == before
    assert _columns(path) == original_columns
    assert _stored_rows(path) == original_rows
    assert _primary_key(path) == "record_id"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_rebuilds_row_count_check_is_not_a_pro_forma(
    tmp_path: Path,
    repointed_build: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checks are the migration's own, so they are shown failing on their own.

    Injected through the row copier rather than through the failure hook: the
    hook proves the transaction rolls back, this proves the assertion in front
    of it actually catches an incomplete copy instead of trusting the loop that
    fed it.
    """

    path = _legacy_store(tmp_path)
    before = path.read_bytes()
    copied = module._insert_rebuild_rows

    def short_copy(conn: sqlite3.Connection, columns: list[str], rows: list[object]) -> int:
        return copied(conn, columns, rows[:-1])

    monkeypatch.setattr(module, "_insert_rebuild_rows", short_copy)

    with pytest.raises(RuntimeError, match="rebuild row count differs"):
        _run_apply(path)

    assert path.read_bytes() == before
    assert "record_id" in _columns(path)


def test_the_rebuilds_read_back_check_catches_a_mutated_row(
    tmp_path: Path,
    repointed_build: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy that comes back different is a copy that did not happen.

    The row count is made to match, so the count check passes it; only the
    read-back comparison can see that a value changed on the way through.
    """

    path = _legacy_store(tmp_path)
    before = path.read_bytes()
    copied = module._insert_rebuild_rows

    def mutant_copy(conn: sqlite3.Connection, columns: list[str], rows: list[object]) -> int:
        inserted = copied(conn, columns, rows[:-1])
        values = [rows[-1][name] for name in columns]
        values[columns.index("account")] = "zz"
        placeholders = ",".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO {module.REBUILD_TEMP_TABLE} ({','.join(columns)})"
            f" VALUES ({placeholders})",
            values,
        )
        return inserted + 1

    monkeypatch.setattr(module, "_insert_rebuild_rows", mutant_copy)

    with pytest.raises(RuntimeError, match="read-back differs"):
        _run_apply(path)

    assert path.read_bytes() == before
    assert "record_id" in _columns(path)


def test_the_rebuild_is_idempotent_and_second_run_writes_nothing(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """A rebuilt store re-opened by the same code finds nothing to do.

    The shape probe is what the second run leans on, so its verdict is asserted
    by name (``shape_already_new``) rather than inferred from an unchanged byte
    count: a probe that answered "rebuilt" and copied the table again would pass
    any assertion made only on the file.
    """

    path = _legacy_store(tmp_path)
    first = _run_apply(path)
    assert first["rebuild"]["rebuilt"] is True
    settled = path.read_bytes()
    rows = _rebuilt_rows(path)

    second = _run_apply(path)

    assert second["rebuild"] == {
        "rebuilt": False,
        "reason": "shape_already_new",
        "rows": 4,
    }
    assert second["write_applied"] is False
    assert second["steps_changed"] == []
    assert {step["status"] for step in second["steps"]} <= {
        "already_present",
        "already_satisfied",
    }
    assert path.read_bytes() == settled
    assert _rebuilt_rows(path) == rows


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


def test_the_rewrite_aligns_every_row_and_loses_nothing(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """D3+D4 on a store the real write path built: nothing unaccounted for.

    ``lost`` is asserted empty on this store for the same reason ``verify``
    fails only on that bucket: it is the verdict that means "a fact has no
    home", and on the payload the publisher actually produces there must be
    none. The key set is then checked against the target shape's, which is what
    "aligned to ``PositionLot.to_dict()``" can be asserted on without inventing
    the keys ``to_dict()`` derives from the events (``open_event_id``,
    ``realized_pnl``, ``close_event_ids``) and that no payload has ever carried.
    """

    path = _legacy_store(tmp_path)
    inventory = module.build_lot_identity_migration_inventory(path)
    assert inventory["dropped_key_classification"]["lost"] == {}

    _run_apply(path)

    for lot_id, row in _rebuilt_rows(path).items():
        fields = row["fields"]
        assert "position_id" not in fields, lot_id
        assert "note" not in fields, lot_id
        assert set(fields) <= module._lot_shape_keys(fields.get("asset_type"))
        # Every carried fact is at the target the classifier named for it.
        assert fields["contract_key"]["account"] == row["account"]
        assert "contract_key" in fields and fields["contract_key"]["broker"]
        assert "premium_open" in fields or fields.get("asset_type") == "stock"
        assert isinstance(fields.get("opened_at_ms"), int)
    option = next(
        row for row in _rebuilt_rows(path).values()
        if row["fields"].get("contract_key", {}).get("option_type") == "put"
    )
    assert option["fields"]["contract_key"]["expiration_ymd"] == "2026-06-19"
    assert option["fields"]["contract_key"]["strike"] == "100"
    assert option["fields"]["position_side"] == "short"
    assert option["fields"]["contracts_opened"] == 1


def test_the_rewrite_blocks_a_note_only_fact_instead_of_dropping_it(
    tmp_path: Path,
    repointed_build: None,
) -> None:
    """§13.4 row 3's negative case, now on the write side as well as the report.

    ``verify`` has always reported this row as blocking; ``apply`` must refuse it
    too, because a dry-run gate that the write path does not honour is a
    suggestion. The refusal happens before anything is written, so the store is
    left exactly as it was.
    """

    path = _legacy_store(tmp_path)
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            "SELECT record_id, fields_json FROM position_lots WHERE record_id = ?",
            ("lot_assign-1",),
        ).fetchone()
        fields = json.loads(raw[1])
        fields.pop("expiration", None)
        fields.pop("expiration_ymd", None)
        # A converged payload carries no ``note`` at all, so the note-only fact
        # is written whole here: with the structured copies popped above (and
        # the row's ``expiration`` column NULL), the note's ``exp`` is the
        # fact's only copy — §13.5 R6's shape.
        fields.get("contract_key", {}).pop("expiration_ymd", None)
        fields["note"] = "exp=2026-06-19"
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True), raw[0]),
        )
        conn.commit()

    before = path.read_bytes()
    inventory = module.build_lot_identity_migration_inventory(path)
    assert "dropped_payload_keys_would_lose_facts" in module.verify_lot_identity_migration(
        path
    )["readiness_reasons"]

    with pytest.raises(RuntimeError, match="would lose a fact: key 'note'"):
        module.apply_lot_identity_migration(path, inventory)

    assert path.read_bytes() == before
    assert "record_id" in _columns(path)


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


def test_rebuild_gate_blocks_dynamic_sql_and_malformed_inventory(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    monkeypatch.setattr(module, "RETIRED_COLUMN_REGISTRY_PATH", path)
    hit = {"module": "src/live.py", "kind": "dynamic"}
    for src in (
        {"detail": [], "dynamic_sql": [hit]},
        {"detail": {}, "dynamic_sql": []},
        {"detail": [], "dynamic_sql": {}},
        {"detail": []},
    ):
        path.write_text(json.dumps({"src": src}), encoding="utf-8")
        assert module._live_sql_naming_retired_columns()
    path.write_text(json.dumps({"src": {"detail": [], "dynamic_sql": []}}), encoding="utf-8")
    assert module._live_sql_naming_retired_columns() == ()


def test_carrier_conflict_blocks_inventory_and_apply(tmp_path, repointed_build):
    path = _legacy_store(tmp_path)
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            "SELECT fields_json FROM position_lots WHERE record_id = ?", ("lot_assign-1",)
        ).fetchone()
        fields = json.loads(raw[0])
        fields["strike"] = "200"
        fields["contract_key"]["strike"] = "100"
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields), "lot_assign-1"),
        )
    before = path.read_bytes()
    inventory = module.build_lot_identity_migration_inventory(path)
    assert inventory["dropped_key_classification"]["lost"]["strike"]["reason"] == "carrier_value_conflict"
    with pytest.raises(RuntimeError, match="carrier_value_conflict"):
        module.apply_lot_identity_migration(path, inventory)
    assert path.read_bytes() == before


def test_alignment_preserves_existing_nested_payload():
    fields = {"asset_type": "option", "account": "lx", "contract_key": {"strike": "100"}}
    aligned = module._aligned_lot_payload(fields, {}, {})
    assert aligned["contract_key"] == {"strike": "100", "account": "lx"}
    assert fields["contract_key"] == {"strike": "100"}
    fields["contract_key"] = "invalid-but-present"
    with pytest.raises(RuntimeError, match="carrier_value_conflict"):
        module._aligned_lot_payload(fields, {}, {})


def test_rebuilt_identity_rejects_null_empty_and_duplicates(tmp_path, repointed_build):
    path = _legacy_store(tmp_path)
    _run_apply(path)
    with sqlite3.connect(path) as conn:
        identity, = conn.execute("SELECT lot_id FROM position_lots LIMIT 1").fetchone()
        for invalid in (None, "", "   "):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE position_lots SET lot_id = ? WHERE lot_id = ?", (invalid, identity))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE position_lots SET lot_id = ?", (identity,))
