"""Slice 1 (`lot-parity-probe`): red/green controls for the read-only comparator.

Every fixture is built through the real write path (``persist_manual_open_event``)
and only then degraded, so the "stored" side of every comparison is a payload the
publisher actually produces rather than one hand-written to match the probe.

The controls are deliberately one-per-face and one-per-C-class: a probe whose
classes can be conflated is worse than no probe, because it turns "the ledger is
missing an event" into "the projection is unfaithful".

Several controls exist for one reason only: the assertions they pin are invisible
on a store that still has its WAL sidecars, or that has never been degraded past
what the write path itself permits. Those fixtures (a settled copy with no
``-shm``/``-wal``, a row the account guard would refuse) are built by hand and
labelled as such at the point of use.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.repository_common import _position_lot_storage_values
from src.application.ledger.lot_parity_probe import (
    C_ATTRIBUTION_CLASSES,
    DERIVED_COLUMNS,
    LIVE_READ_MODE,
    SETTLED_READ_MODE,
    WRITER_RAISES_KEY,
    assert_report_path_outside_runtime_state,
    derive_stored_row_columns,
    main as probe_main,
    run_lot_parity_probe,
    write_report,
)


def _build_green_store(tmp_path: Path) -> tuple[Path, Path]:
    """A store written by the real path, where the replay reproduces every row."""
    sqlite_path = tmp_path / "option_positions.sqlite3"
    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}),
        encoding="utf-8",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        opened_at_ms=1000,
    )
    return sqlite_path, data_config


def _stored_lot_id(sqlite_path: Path) -> str:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT lot_id FROM position_lots").fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _stored_fields(sqlite_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT fields_json FROM position_lots").fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(str(row[0]))


def _settle_store(sqlite_path: Path) -> None:
    """Leave the store as a ``.backup`` copy: contents in the file, no sidecars.

    This is the input slice 1 is defined against (``production-readout.md``:
    ``.backup``, never a bare ``cp``) and the one the previous fixture could not
    produce: every write-path connection in this file left ``-shm``/``-wal``
    behind, so the probe never had to open a settled WAL store.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{sqlite_path}{suffix}").unlink(missing_ok=True)


def _sidecar_sizes(sqlite_path: Path) -> tuple[int, int]:
    wal = Path(f"{sqlite_path}-wal")
    shm = Path(f"{sqlite_path}-shm")
    return (
        wal.stat().st_size if wal.exists() else 0,
        shm.stat().st_size if shm.exists() else 0,
    )


def _drop_table_triggers(sqlite_path: Path, table: str) -> None:
    """Drop every trigger that guards a write to ``table``.

    The fixtures below have to describe store shapes the *current* write path
    refuses (a payload without ``account``, a corrupt ledger payload). Those
    shapes are reachable only on a store written before today's guards, or damaged
    on disk — and the guards sit at three separate layers per table, so dropping
    them by name would make each fixture depend on today's guard inventory.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                (table,),
            ).fetchall()
        ]
        for name in names:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


def _drop_account_guards(sqlite_path: Path) -> None:
    """Allow a legacy row whose column is NULL and whose payload predates the key.

    The write path raises for that payload (``repository_common.py``:218-221) and
    the DB guards reject it a second time, which is exactly why the probe's copy
    of the derivation has to notice it.
    """
    _drop_table_triggers(sqlite_path, "position_lots")


def _drop_trade_event_guards(sqlite_path: Path) -> None:
    """Allow a ledger row whose payload is not an object."""
    _drop_table_triggers(sqlite_path, "trade_events")


def _ledger_event_id(sqlite_path: Path) -> str:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute(
            "SELECT event_id FROM trade_events ORDER BY trade_time_ms ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _tamper(sqlite_path: Path, statements: list[tuple[str, tuple[object, ...]]]) -> None:
    conn = sqlite3.connect(sqlite_path)
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _mutate_fields(
    sqlite_path: Path,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute(
            "SELECT record_id, fields_json FROM position_lots"
        ).fetchone()
        assert row is not None
        fields = json.loads(str(row[1]))
        mutate(fields)
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = ?",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True), row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_stored_row(
    sqlite_path: Path,
    *,
    record_id: str,
    source_event_id: object,
    payload_source_event_id: object = None,
) -> None:
    """Insert a stored row the replay cannot produce, with a chosen source event id."""
    fields: dict[str, object] = {"account": "lx", "status": "open", "symbol": "TSLA"}
    if payload_source_event_id is not None:
        fields["source_event_id"] = payload_source_event_id
    _tamper(
        sqlite_path,
        [
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES (?, 'lx', ?, ?, NULL, NULL, NULL, 1, ?)
                """,
                (
                    record_id,
                    json.dumps(fields, ensure_ascii=False, sort_keys=True),
                    source_event_id,
                    record_id,
                ),
            )
        ],
    )


def _attribution(report: dict[str, object]) -> dict[str, int]:
    attribution = report["c_attribution"]
    assert isinstance(attribution, dict)
    return {name: int(attribution[name]) for name in C_ATTRIBUTION_CLASSES}


def _faces(report: dict[str, object]) -> dict[str, dict[str, object]]:
    faces = report["faces"]
    assert isinstance(faces, dict)
    return faces  # type: ignore[return-value]


# --- green baseline ----------------------------------------------------------


def test_probe_is_green_on_an_untouched_store(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert report["difference_count"] == 0
    assert report["tier"] == "tier-1"
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1
    assert report["event_count"] == 1
    faces = _faces(report)
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["difference_count"] == 0
    assert report["excluded_payload_keys"] == ["updated_at_ms"]
    assert _attribution(report) == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }


# --- face A: payload ---------------------------------------------------------


def test_probe_face_a_reports_a_key_set_difference(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    lot_id = _stored_lot_id(sqlite_path)
    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__("probe_only_key", "x"))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["key_set_difference_lot_count"] == 1
    assert faces["a_payload"]["value_difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["difference_count"] == 0
    item = faces["a_payload"]["items"][0]
    assert item["lot_id"] == lot_id
    assert item["keys_only_in_store"] == ["probe_only_key"]
    assert item["keys_only_in_projection"] == []


def test_probe_face_a_reports_a_value_difference(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)

    def _reprice(fields: dict[str, object]) -> None:
        fields["premium"] = "9.99"

    _mutate_fields(sqlite_path, _reprice)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["key_set_difference_lot_count"] == 0
    assert faces["a_payload"]["value_difference_count"] == 1
    assert faces["b_columns"]["difference_count"] == 0
    difference = faces["a_payload"]["items"][0]["value_differences"][0]
    assert difference["key"] == "premium"
    assert difference["stored"] == "9.99"
    assert difference["projected"] == "1.23"


def test_probe_face_a_does_not_heal_the_stored_payload(tmp_path: Path) -> None:
    """The existing codec's column-heal must not be applied to either side.

    ``strike`` leaves the payload while its column keeps the true value. A healed
    stored side would put ``strike=100.0`` back and call face A matched; the probe
    must instead report the key and leave the column to face B.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _mutate_fields(sqlite_path, lambda fields: fields.pop("strike"))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["a_payload"]["key_set_difference_lot_count"] == 1
    assert faces["a_payload"]["items"][0]["keys_only_in_projection"] == ["strike"]
    # And the same fact is *also* a face B difference, because the column was
    # never re-derived: this is comparator-spec §6.3's "both", counted twice.
    assert faces["b_columns"]["by_column"]["strike"] == 1
    assert faces["b_columns"]["by_column"]["multiplier"] == 0
    assert faces["b_columns"]["items"][0]["stored"] == 100.0
    assert faces["b_columns"]["items"][0]["derived_from_stored_payload"] is None


def test_probe_face_a_ignores_updated_at_ms(tmp_path: Path) -> None:
    """``updated_at_ms`` is a wall clock, so a difference on it is not a difference."""
    sqlite_path, _config = _build_green_store(tmp_path)
    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__("updated_at_ms", 42))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert _faces(report)["a_payload"]["difference_count"] == 0


def test_probe_refuses_a_stored_payload_that_is_not_an_object(tmp_path: Path) -> None:
    """An empty payload reads as empty; a non-object payload is loud.

    The read side (``sqlite_row_codec.position_lot_row_to_record``) reads a NULL /
    absent payload as ``{}``, so face A must not call the same shape "malformed" —
    and, symmetrically, the projected side must not answer a non-object payload
    with ``{}``. Both sides of one comparison, one vocabulary per shape.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_account_guards(sqlite_path)
    _tamper(sqlite_path, [("UPDATE position_lots SET fields_json = '[1,2]'", ())])

    with pytest.raises(ValueError, match="stored position lot payload must be an object"):
        run_lot_parity_probe(sqlite_path=sqlite_path)


def test_probe_does_not_shrug_at_a_projected_payload_that_is_not_an_object() -> None:
    """The projected side is the other half of the same asymmetry.

    ``_projected_lot_row`` used to coerce a non-dict ``fields`` to ``{}`` — the
    one silent answer in a comparison whose stored side raises for the same shape.
    """
    import src.application.ledger.lot_parity_probe as probe_module

    assert probe_module._projected_lot_row({"lot_id": "lot_x", "fields": None})["fields"] == {}
    with pytest.raises(ValueError, match="projected position lot fields must be an object"):
        probe_module._projected_lot_row({"lot_id": "lot_x", "fields": [1, 2]})
    assert probe_module._decode_fields_json(None, lot_id="lot_x") == {}
    assert probe_module._decode_fields_json("", lot_id="lot_x") == {}


# --- face B: derived columns -------------------------------------------------


def test_probe_face_b_reports_a_column_difference(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [("UPDATE position_lots SET multiplier = 7.0", ())],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 1
    assert faces["b_columns"]["by_column"]["multiplier"] == 1
    assert faces["b_columns"]["by_column"]["account"] == 0
    assert faces["b_columns"]["by_column"]["expiration"] == 0
    assert faces["b_columns"]["by_column"]["strike"] == 0
    assert faces["b_columns"]["by_column"]["source_event_id"] == 0
    assert faces["c_rows"]["difference_count"] == 0
    item = faces["b_columns"]["items"][0]
    assert item["column"] == "multiplier"
    assert item["stored"] == 7.0
    assert item["derived_from_stored_payload"] == 100.0


def _writer_columns(fields: dict[str, object], *, lot_id: str) -> dict[str, object]:
    """The five derived columns as **the writer** computes them, for the same payload."""
    values = _position_lot_storage_values(PositionLotRecord(lot_id=lot_id, fields=dict(fields)))
    # (lot_id, account, fields_json, source_event_id, expiration, strike,
    #  multiplier, carrier)
    return {
        "account": values[1],
        "source_event_id": values[3],
        "expiration": values[4],
        "strike": values[5],
        "multiplier": values[6],
    }


def test_probe_derivation_is_bound_to_the_writers_own_derivation(tmp_path: Path) -> None:
    """Face B must use the writer's derivation, not a second opinion that can drift.

    The probe restates ``account``/``source_event_id`` and the three casts because
    the shared function lives in ``repository_common.py`` (outside slice 1's
    allowed files). This test *is* the binding: for the same payload, the probe's
    derived columns must equal the values ``_position_lot_storage_values`` hands
    the INSERT. A writer-side change that the probe does not follow fails here
    instead of turning face B into either a wall of false differences or a column
    that silently stops discriminating.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    stored = _stored_fields(sqlite_path)
    lot_id = _stored_lot_id(sqlite_path)

    payloads: dict[str, dict[str, object]] = {"stored payload": stored}

    note_fallback = dict(stored)
    note_fallback["note"] = f"multiplier={note_fallback.pop('multiplier', 100)}"
    payloads["multiplier via the note fallback"] = note_fallback

    no_source_event = dict(stored)
    no_source_event.pop("source_event_id", None)
    payloads["no source_event_id"] = no_source_event

    for label, payload in payloads.items():
        derived = derive_stored_row_columns(payload)
        assert {column: derived[column] for column in DERIVED_COLUMNS} == _writer_columns(
            payload, lot_id=lot_id
        ), label
        assert derived[WRITER_RAISES_KEY] is None, label


def test_probe_derivation_reports_the_writers_fail_fast_instead_of_equality(
    tmp_path: Path,
) -> None:
    """``account`` is the one derived column the writer refuses to guess at.

    Bound to the writer's own rule in both directions: whenever the writer raises
    for ``account``, the probe's derivation must carry the refusal instead of
    returning ``None`` (which would compare equal to a NULL column).
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    stored = _stored_fields(sqlite_path)
    lot_id = _stored_lot_id(sqlite_path)

    missing = dict(stored)
    missing.pop("account", None)
    uppercase = dict(stored)
    uppercase["account"] = "LX"

    for payload in (missing, uppercase):
        with pytest.raises(ValueError):
            _position_lot_storage_values(PositionLotRecord(lot_id=lot_id, fields=payload))
        assert derive_stored_row_columns(payload)[WRITER_RAISES_KEY] is not None
    assert derive_stored_row_columns(stored)[WRITER_RAISES_KEY] is None


def test_probe_face_b_reports_a_payload_the_writer_would_refuse(tmp_path: Path) -> None:
    """A legacy row the write path would reject must not pass as "both are NULL".

    The column is NULL and the payload has no ``account``: the writer's
    derivation raises (``repository_common.py``:218-221), and the probe's copy of
    it used to answer ``None`` on both sides — a silent equality on a row the
    writer cannot reproduce. The guards are dropped because the *current* write
    path refuses to create this row; it is exactly the pre-guard shape the probe
    has to notice.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_account_guards(sqlite_path)
    fields = _stored_fields(sqlite_path)
    fields.pop("account")
    _tamper(
        sqlite_path,
        [
            (
                "UPDATE position_lots SET account = NULL, fields_json = ?",
                (json.dumps(fields, ensure_ascii=False, sort_keys=True),),
            )
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["b_columns"]["difference_count"] == 1
    assert faces["b_columns"]["writer_raises_count"] == 1
    item = faces["b_columns"]["items"][0]
    assert item["column"] == "account"
    assert item["stored"] is None
    assert item["derived_from_stored_payload"] is None
    assert item["writer_raises"] == "position lot account is required"


# --- face C: row set ---------------------------------------------------------


def test_probe_face_c_reports_an_extra_stored_row(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_extra",
        source_event_id=ledger_event_id,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["extra_in_store_count"] == 1
    assert faces["c_rows"]["missing_in_store_count"] == 0
    assert faces["c_rows"]["lot_id_set_difference_count"] == 1
    assert faces["c_rows"]["count_mismatch"] is True
    assert report["stored_lot_count"] == 2
    assert report["projected_lot_count"] == 1


def test_probe_face_c_reports_a_missing_stored_row(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(sqlite_path, [("DELETE FROM position_lots", ())])

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["c_rows"]["missing_in_store_count"] == 1
    assert faces["c_rows"]["extra_in_store_count"] == 0
    assert faces["c_rows"]["count_mismatch"] is True
    assert faces["c_rows"]["items"][0]["status"] == "missing_in_store"
    # Not a ledger gap: the projection produced the row, the store lost it.
    assert _attribution(report) == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 1,
    }


def test_probe_face_c_detects_a_duplicate_identity(tmp_path: Path) -> None:
    """Two rows collapsing onto one identity is what the old instrument silently lost.

    A unique index pins the ``lot_id`` column, so the collision has to come from
    the carrier-or-fallback rule the two existing read surfaces share: one row
    carries ``lot_probe_shared`` while a second, carrier-less row falls back to
    the same string through ``record_id``.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('probe_carrier_row', 'lx', '{"account":"lx"}', NULL,
                          NULL, NULL, NULL, 1, 'lot_probe_shared')
                """,
                (),
            ),
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('lot_probe_shared', 'lx', '{"account":"lx"}', NULL,
                          NULL, NULL, NULL, 1, NULL)
                """,
                (),
            ),
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["c_rows"]["duplicate_identity_count"] == 1
    duplicate = next(
        item
        for item in faces["c_rows"]["items"]
        if item["status"] == "duplicate_identity"
    )
    assert duplicate == {
        "status": "duplicate_identity",
        "side": "store",
        "lot_id": "lot_probe_shared",
        "occurrences": 2,
    }
    assert faces["c_rows"]["count_mismatch"] is True


# --- C-face attribution: four classes, never conflated -----------------------


def test_probe_attributes_an_empty_source_event_id_without_calling_it_a_ledger_gap(
    tmp_path: Path,
) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_no_source",
        source_event_id=None,
        payload_source_event_id=None,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert _attribution(report) == {
        "null_source_event_id": 1,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }
    item = report["c_attribution"]["items"][0]
    assert item["attribution"] == "null_source_event_id"
    assert item["source_event_id"] is None
    assert item["source_event_id_source"] == "absent"


def test_probe_attributes_a_missing_ledger_event_as_ledger_missing_row(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_no_event",
        source_event_id="manual-open-does-not-exist",
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["ledger_missing_row"] == 1
    # Not a NULL id, and not projection infidelity: that distinction is the fix.
    assert attribution["null_source_event_id"] == 0
    assert attribution["projection_omission"] == 0
    assert attribution["other"] == 0
    assert report["c_attribution"]["items"][0]["attribution"] == "ledger_missing_row"


def test_probe_attributes_an_existing_ledger_event_as_projection_omission(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_omitted",
        source_event_id=ledger_event_id,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["projection_omission"] == 1
    assert attribution["null_source_event_id"] == 0
    assert attribution["ledger_missing_row"] == 0
    assert attribution["other"] == 0
    item = report["c_attribution"]["items"][0]
    assert item["attribution"] == "projection_omission"
    assert item["source_event_id"] == ledger_event_id


def test_probe_attributes_a_payload_difference_as_other(tmp_path: Path) -> None:
    """The remainder class: a difference that is not a row-set gap at all."""
    sqlite_path, _config = _build_green_store(tmp_path)

    def _reprice(fields: dict[str, object]) -> None:
        fields["premium"] = "9.99"

    _mutate_fields(sqlite_path, _reprice)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["other"] == 1
    assert attribution["null_source_event_id"] == 0
    assert attribution["ledger_missing_row"] == 0
    assert attribution["projection_omission"] == 0
    assert report["c_attribution"]["items"][0]["attribution"] == "other"


def test_probe_reads_the_source_event_id_from_the_payload_when_the_column_is_empty(
    tmp_path: Path,
) -> None:
    """A never-backfilled column must not masquerade as "no source event at all"."""
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_column_null",
        source_event_id=None,
        payload_source_event_id=ledger_event_id,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["projection_omission"] == 1
    assert attribution["null_source_event_id"] == 0
    assert report["c_attribution"]["items"][0]["source_event_id_source"] == "payload"


def test_probe_c_attribution_counts_are_a_partition_of_the_differing_lots(
    tmp_path: Path,
) -> None:
    """One differing lot, one class — even when it is also a duplicated identity.

    Two stored rows collapse onto one ``lot_id`` whose ``source_event_id`` is in
    the ledger: one differing lot id that is ③. Counting it again as ④ made the
    four classes add up to more than the number of differing lots and read as two
    independent failures — and ③ is a stop condition for slice 2, so it must not
    be inflated by a repeated row.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _tamper(
        sqlite_path,
        [
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('probe_dup_carrier', 'lx', '{"account":"lx"}', ?,
                          NULL, NULL, NULL, 1, 'lot_probe_dup')
                """,
                (ledger_event_id,),
            ),
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('lot_probe_dup', 'lx', '{"account":"lx"}', ?,
                          NULL, NULL, NULL, 1, NULL)
                """,
                (ledger_event_id,),
            ),
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    attribution = _attribution(report)
    assert faces["c_rows"]["duplicate_identity_count"] == 1
    assert faces["c_rows"]["extra_in_store_count"] == 1
    assert attribution["projection_omission"] == 1
    # Not ③ + ④: the same lot id, counted once.
    assert attribution["other"] == 0
    assert report["c_attribution"]["differing_lot_count"] == 1
    assert sum(attribution.values()) == report["c_attribution"]["differing_lot_count"]


def test_probe_refuses_a_ledger_row_that_is_not_an_object(tmp_path: Path) -> None:
    """A corrupt ledger row is the ledger's fault, not the projection's.

    Keeping the id while dropping the payload (what the probe used to do) put the
    id in the ledger id set with nothing behind it, so every stored row
    referencing it was attributed to ``projection_omission`` — the class slice 2
    treats as a stop condition. The payload side already refuses this shape; the
    event side now does the same.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_trade_event_guards(sqlite_path)
    _tamper(sqlite_path, [("UPDATE trade_events SET event_json = ?", ("[1,2]",))])

    with pytest.raises(ValueError, match="stored trade event payload must be an object"):
        run_lot_parity_probe(sqlite_path=sqlite_path)


@pytest.mark.parametrize("raw", ["null", "0", '"a string"'])
def test_probe_refuses_other_non_object_ledger_payloads(tmp_path: Path, raw: str) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_trade_event_guards(sqlite_path)
    _tamper(sqlite_path, [("UPDATE trade_events SET event_json = ?", (raw,))])

    with pytest.raises(ValueError, match="stored trade event payload must be an object"):
        run_lot_parity_probe(sqlite_path=sqlite_path)


def test_probe_survives_the_record_id_rename(tmp_path: Path) -> None:
    """Slice 3 renames ``record_id`` to ``lot_id`` and re-runs this comparison.

    A hard-coded ``record_id`` in the SELECT or the ORDER BY would make the
    comparison die on the rename instead of describing the store on the other side
    of it — at the one step that has to prove "replay == stored" still holds.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [("ALTER TABLE position_lots RENAME COLUMN record_id TO record_id_legacy", ())],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert report["stored_lot_count"] == 1
    assert _faces(report)["c_rows"]["difference_count"] == 0


def test_probe_reads_a_store_that_only_has_record_id(tmp_path: Path) -> None:
    """The production store today: ``record_id`` and no carrier column at all.

    This is the shape of the acceptance input (``production-readout.md``'s
    ``.backup`` copy, whose ``position_lots`` predates the carrier), and the shape
    no fixture in this file produced: the write path creates both columns, so the
    carrier-or-fallback branch that production actually takes was only ever
    exercised by hand.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [
            ("DROP INDEX IF EXISTS idx_position_lots_lot_id", ()),
            ("ALTER TABLE position_lots DROP COLUMN lot_id", ()),
        ],
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(position_lots)").fetchall()
        }
    finally:
        conn.close()
    assert "lot_id" not in columns and "record_id" in columns

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1


# --- zero writes -------------------------------------------------------------


def _store_snapshot(sqlite_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(sqlite_path)
    try:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        lots = conn.execute("SELECT * FROM position_lots ORDER BY record_id").fetchall()
        events = conn.execute(
            "SELECT event_id, event_json, trade_time_ms FROM trade_events ORDER BY event_id"
        ).fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    # Hashed after the connection closes: closing the last writer checkpoints the
    # WAL, exactly as it does for the "after" snapshot. The WAL payload is part of
    # the comparison because it is part of the state: the repository's own
    # read-only criterion hashes db **and** wal bytes
    # (``position_projection_migration._assert_read_only_persistent_sizes``), and
    # a main-file-only hash would call "the probe wrote nothing" while a write sat
    # in the log. ``-shm`` is reported but not compared: SQLite may resize that
    # ephemeral coordination file on any connection, which is why the repository's
    # criterion leaves it out.
    wal_bytes, shm_bytes = _sidecar_sizes(sqlite_path)
    return {
        "sha256": hashlib.sha256(sqlite_path.read_bytes()).hexdigest(),
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "schema": schema,
        "lots": lots,
        "events": events,
        "integrity_check": integrity,
    }


#: The durable state of the store — everything except ``shm_bytes``.
PERSISTENT_SNAPSHOT_KEYS = (
    "sha256",
    "wal_bytes",
    "schema",
    "lots",
    "events",
    "integrity_check",
)


@pytest.mark.parametrize("tampered", [False, True])
@pytest.mark.parametrize("settled", [False, True])
def test_probe_performs_no_writes(tmp_path: Path, tampered: bool, settled: bool) -> None:
    """Zero writes on a live store **and** on a settled copy (the immutable mode).

    The snapshot compares the main file *and* the WAL payload: the repository's
    own read-only criterion is db + wal bytes
    (``position_projection_migration._assert_read_only_persistent_sizes``), and a
    main-file-only hash would call "the probe wrote nothing" while a write sat in
    the log. The connection mode itself is pinned by the two tests below.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    if tampered:
        _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    if settled:
        # The immutable fallback is the mode with the least right to write; run
        # the zero-write control against it too, not just against ``mode=ro``.
        _settle_store(sqlite_path)
    # Warm-up: settle any pending WAL checkpoint before the "before" snapshot.
    _store_snapshot(sqlite_path)
    before = _store_snapshot(sqlite_path)
    assert before["integrity_check"] == "ok"

    run_lot_parity_probe(sqlite_path=sqlite_path)

    after = _store_snapshot(sqlite_path)
    assert {key: after[key] for key in PERSISTENT_SNAPSHOT_KEYS} == {
        key: before[key] for key in PERSISTENT_SNAPSHOT_KEYS
    }
    # The WAL is part of the compared state, not an implementation detail.
    assert "wal_bytes" in after


def test_probe_reads_both_tables_from_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit landing between the two reads must not fabricate a C difference.

    The cadence runs against the live store (plan ⑤), so the window between the
    ``position_lots`` read and the ``trade_events`` read is real. A writer that
    commits inside it leaves the probe holding a stored row the replay — read a
    moment later — no longer produces: an ``extra_in_store`` row whose event *is*
    in the ledger, which is exactly ③ ``projection_omission``, the class slice 2
    reads as a stop condition. One transaction (``BEGIN``, the idiom of
    ``read_only_evidence``) makes the two reads one snapshot.

    The writer is the real write path, not a hand-written INSERT: this is the
    window a nightly run actually has.
    """
    import src.application.ledger.lot_parity_probe as probe_module

    sqlite_path, data_config = _build_green_store(tmp_path)
    real_read = probe_module.read_stored_position_lots

    def _read_then_commit(conn: sqlite3.Connection) -> list[dict[str, object]]:
        rows = real_read(conn)
        repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
        repo.data_config_path = data_config  # type: ignore[attr-defined]
        ledger_manual_trades.persist_manual_open_event(
            repo,
            broker="富途",
            account="lx",
            symbol="AMD",
            option_type="call",
            side="short",
            contracts=1,
            currency="USD",
            strike=50.0,
            multiplier=100,
            expiration_ymd="2026-07-17",
            premium_per_share=0.55,
            opened_at_ms=2000,
        )
        return rows

    monkeypatch.setattr(probe_module, "read_stored_position_lots", _read_then_commit)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    # The concurrent commit is invisible to both reads, so the comparison is
    # green instead of reporting a projection omission that never happened.
    assert report["event_count"] == 1
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1
    assert report["green"] is True
    assert _attribution(report) == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }


def test_probe_opens_a_settled_copy_and_still_produces_its_verdict(tmp_path: Path) -> None:
    """The acceptance input: a settled ``.backup`` copy with no ``-shm``/``-wal``.

    This is the case the probe could not open at all: a WAL database cannot be
    opened read-only when SQLite may not create the shared-memory file, and a
    settled copy has no ``-shm`` to reuse.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _settle_store(sqlite_path)
    assert not Path(f"{sqlite_path}-wal").exists()
    assert not Path(f"{sqlite_path}-shm").exists()

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["connection_mode"] == SETTLED_READ_MODE
    assert report["green"] is True
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1


def test_probe_keeps_mode_ro_when_a_writer_may_still_be_attached(tmp_path: Path) -> None:
    """``immutable=1`` is for a settled copy only — never for a store with writers.

    A writer attached to a WAL store holds its ``-shm``, which is what makes the
    fallback's guard meaningful: sidecars present means the store is opened
    ``mode=ro`` and the writer's committed state stays visible.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    writer = sqlite3.connect(sqlite_path)
    try:
        assert writer.execute("SELECT count(*) FROM position_lots").fetchone() is not None
        assert Path(f"{sqlite_path}-shm").exists()

        report = run_lot_parity_probe(sqlite_path=sqlite_path)
    finally:
        writer.close()

    assert report["connection_mode"] == LIVE_READ_MODE
    assert report["green"] is True


# --- report landing (plan ⑥: outside output_shared/state/) -------------------


def test_probe_refuses_to_land_a_report_inside_the_runtime_state_tree(tmp_path: Path) -> None:
    forbidden = tmp_path / "output_shared" / "state" / "option_positions" / "probe.json"
    with pytest.raises(ValueError, match="outside output_shared/state/"):
        assert_report_path_outside_runtime_state(forbidden)

    allowed = tmp_path / "artifacts" / "lot-parity.json"
    assert assert_report_path_outside_runtime_state(allowed) == allowed.resolve()
    assert write_report({"green": True}, out_path=allowed) == allowed.resolve()
    assert json.loads(allowed.read_text(encoding="utf-8")) == {"green": True}


def test_probe_module_cli_writes_a_report_and_exits_non_zero_when_red(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    green_out = tmp_path / "artifacts" / "green.json"
    assert probe_main(["--db", str(sqlite_path), "--out", str(green_out)]) == 0
    assert json.loads(green_out.read_text(encoding="utf-8"))["green"] is True

    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    red_out = tmp_path / "artifacts" / "red.json"
    assert probe_main(["--db", str(sqlite_path), "--out", str(red_out)]) == 1
    red = json.loads(red_out.read_text(encoding="utf-8"))
    assert red["green"] is False
    assert red["faces"]["b_columns"]["by_column"]["strike"] == 1


def test_probe_module_cli_keeps_its_report_out_of_the_runtime_state_tree(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    forbidden = tmp_path / "output_shared" / "state" / "probe.json"
    with pytest.raises(ValueError, match="outside output_shared/state/"):
        probe_main(["--db", str(sqlite_path), "--out", str(forbidden)])


# --- the enforcement channel mounted on verify-projection --------------------


def _mount_cli(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_path: Path,
    data_config: Path,
    *,
    fmt: str | None = "json",
    extra_args: tuple[str, ...] = (),
) -> None:
    """Mount ``verify-projection`` with the argv the cadence would use.

    ``fmt=None`` is the production shape (``service_deploy.py``'s ``verify_args``
    carries no ``--format``, so the handler takes its text branch) — the one
    enforcement path that runs unattended, and the one no test reached.
    """
    import src.interfaces.cli.option_positions as cli_mod

    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    argv = [
        "om option-positions",
        "--data-config",
        str(data_config),
        "verify-projection",
        *extra_args,
    ]
    if fmt is not None:
        argv += ["--format", fmt]
    monkeypatch.setattr(sys, "argv", argv)


def test_verify_projection_adds_the_probe_under_a_new_key_without_touching_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)

    assert cli_mod.main() == 0

    payload = json.loads(capsys.readouterr().out)
    # Existing semantics untouched.
    assert payload["ok"] is True
    assert payload["mode_used"] == "full_replay"
    assert payload["checkpoint_reused"] is False
    assert payload["summary"]["matched"] == 1
    assert payload["report_id"].startswith("projection-verify-")
    assert payload["source_of_truth"] == "trade_events"
    assert payload["projection"] == "position_lots"
    # ... and the probe rides beside them, never inside them. One key, one
    # verdict: the summary carries the probe's own ``green`` and the handler does
    # not restate it.
    assert payload["lot_parity_probe"]["green"] is True
    assert payload["lot_parity_probe"]["tier"] == "tier-1"
    assert payload["lot_parity_probe"]["connection_mode"] in {"ro", "ro+immutable"}
    assert "lot_parity_probe_green" not in payload
    assert "lot_parity_probe_error" not in payload


def test_verify_projection_exits_non_zero_when_the_probe_is_red(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    _mount_cli(monkeypatch, sqlite_path, data_config)

    assert cli_mod.main() == 1

    # The JSON still reaches stdout on a red run: the report is the evidence,
    # the exit code is the enforcement.
    payload = json.loads(capsys.readouterr().out)
    assert payload["lot_parity_probe"]["green"] is False
    assert payload["lot_parity_probe"]["b_columns_difference_count"] == 1
    # ``ok`` is deliberately unchanged: folding the probe into it would move the
    # checkpoint write and the ``--mode auto`` fast path.
    assert payload["ok"] is True
    # A red run still carries the detail: the counts say how much, the samples
    # say what.
    samples = payload["lot_parity_probe"]["samples"]
    assert samples["b_columns"][0]["column"] == "strike"
    assert samples["b_columns"][0]["stored"] == 999.0


def test_verify_projection_text_run_enforces_a_red_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production enforcement channel: no ``--format``, so the text branch.

    The cadence's ``verify_args`` carries no ``--format``, which makes this the
    only path that actually exits non-zero in production. Every other control
    here runs ``--format json``.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    _mount_cli(monkeypatch, sqlite_path, data_config, fmt=None)

    assert cli_mod.main() == 1

    captured = capsys.readouterr()
    assert "[DONE] verified trade_events projection against position_lots" in captured.out
    assert "matched=1" in captured.out
    probe_line = next(
        line for line in captured.out.splitlines() if "lot parity probe (tier-1)" in line
    )
    assert "green=False" in probe_line
    assert "b_columns=1" in probe_line
    assert "lot parity probe is red" in captured.err


def test_verify_projection_text_run_is_green_when_the_probe_is_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config, fmt=None)

    assert cli_mod.main() == 0

    captured = capsys.readouterr()
    probe_line = next(
        line for line in captured.out.splitlines() if "lot parity probe (tier-1)" in line
    )
    assert "green=True" in probe_line
    assert captured.err == ""


def test_verify_projection_red_run_leaves_the_existing_fields_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ok``/``summary``/``checkpoint_reused``/``report_id`` keep their meaning.

    Read off the same store before and after a face-B diff that the probe sees and
    the existing verifier does not: a red probe must not move any of the four
    fields the receipts' known readers depend on.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)
    assert cli_mod.main() == 0
    green = json.loads(capsys.readouterr().out)

    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    assert cli_mod.main() == 1
    red = json.loads(capsys.readouterr().out)

    assert red["ok"] == green["ok"] is True
    assert red["summary"] == green["summary"]
    assert red["checkpoint_reused"] == green["checkpoint_reused"] is False
    assert red["mode_used"] == green["mode_used"]
    assert red["report_id"].startswith("projection-verify-")
    # A new run, a new report: the red run's evidence is its own, not a reused one.
    assert red["report_id"] != green["report_id"]


def test_verify_projection_reuse_path_still_runs_the_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reused checkpoint says the *inputs* are unchanged, not that the store is.

    The probe runs unconditionally, on the reuse path too: "the replay reproduces
    the store" is the one thing the cadence is there to answer.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(
        monkeypatch,
        sqlite_path,
        data_config,
        extra_args=("--mode", "auto", "--publish-evidence"),
    )
    calls: list[dict[str, object]] = []
    real_probe = cli_mod.run_lot_parity_probe

    def _counted(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return real_probe(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_mod, "run_lot_parity_probe", _counted)

    assert cli_mod.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["checkpoint_reused"] is False

    assert cli_mod.main() == 0
    reused = json.loads(capsys.readouterr().out)

    assert reused["checkpoint_reused"] is True
    assert reused["mode_used"] == "checkpoint_reuse"
    assert len(calls) == 2
    assert reused["lot_parity_probe"]["green"] is True
    assert reused["lot_parity_probe"]["c_attribution"] == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }


def test_verify_projection_isolates_a_probe_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A probe that cannot run must not take the verification report with it.

    A store shape the probe refuses (here: a never-migrated ``position_lots``
    without ``source_event_id``, the pre-migration shape) is not the same fact as
    a probe that ran and came back red, and neither may cost the caller the report
    the existing consumers read. Hence a separate key and a separate exit code —
    and the error is written down, never swallowed.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [
            (
                "ALTER TABLE position_lots RENAME COLUMN source_event_id TO source_event_id_legacy",
                (),
            )
        ],
    )
    _mount_cli(monkeypatch, sqlite_path, data_config)

    assert cli_mod.main() == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["lot_parity_probe_error"].startswith("ValueError: ")
    assert "source_event_id" in payload["lot_parity_probe_error"]
    assert "lot_parity_probe" not in payload
    # The report the existing consumers read is intact.
    assert payload["ok"] is True
    assert payload["summary"]["matched"] == 1
    assert payload["report_id"].startswith("projection-verify-")


def test_verify_projection_text_run_reports_a_probe_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same isolation on the channel production actually uses, with exit code 2."""
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [
            (
                "ALTER TABLE position_lots RENAME COLUMN source_event_id TO source_event_id_legacy",
                (),
            )
        ],
    )
    _mount_cli(monkeypatch, sqlite_path, data_config, fmt=None)

    assert cli_mod.main() == 2

    captured = capsys.readouterr()
    assert "[DONE] verified trade_events projection against position_lots" in captured.out
    # "cannot run" (2) is not "red" (1): the message says which, and it is loud.
    assert "lot parity probe could not run: ValueError" in captured.err
    assert "lot parity probe is red" not in captured.err


def test_verify_projection_hands_the_probe_only_a_store_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never the shared write-capable repo object — a path, so the probe owns its handle."""
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)
    seen: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return run_lot_parity_probe(sqlite_path=kwargs["sqlite_path"])  # type: ignore[arg-type]

    monkeypatch.setattr(cli_mod, "run_lot_parity_probe", _capture)

    assert cli_mod.main() == 0
    capsys.readouterr()
    assert seen == [{"sqlite_path": str(sqlite_path.resolve())}]


def test_probe_opens_its_own_connection_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.ledger.lot_parity_probe as probe_module

    sqlite_path, _config = _build_green_store(tmp_path)
    real_connect = sqlite3.connect
    uris: list[str] = []

    def _spy(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        uris.append(str(database))
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(probe_module.sqlite3, "connect", _spy)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert uris == [f"{sqlite_path.resolve().as_uri()}?mode=ro"]
