from __future__ import annotations

import json
import sqlite3

import pytest

from domain.domain.ledger.position_fingerprint import ordered_position_lots_fingerprint
from src.application.ledger.sqlite_row_codec import position_lot_row_to_record

# The two identity keys are not the same fact: ``record_id`` is the stored column
# and ``lot_id`` is the carrier, which falls back to the column only while the
# gated backfill (``UPDATE position_lots SET lot_id = record_id``) is the only
# writer of the carrier. The fixtures below diverge the two columns so the codec
# has to prove each key carries its own value.
FIELDS_JSON = '{"account": "lx", "symbol": "0700.HK"}'


def _row(
    *,
    record_id: str | None,
    lot_id: str | None,
    carrier_column: bool = True,
    carrier_selected: bool = True,
) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    table = "record_id TEXT, fields_json TEXT, expiration INTEGER, strike REAL, multiplier REAL"
    if carrier_column:
        table = f"{table}, lot_id TEXT"
    conn.execute(f"CREATE TABLE position_lots ({table})")
    names = ["record_id", "fields_json", "expiration", "strike", "multiplier"]
    values: list[object] = [record_id, FIELDS_JSON, None, None, None]
    if carrier_column:
        names.append("lot_id")
        values.append(lot_id)
    placeholders = ", ".join("?" for _item in names)
    conn.execute(f"INSERT INTO position_lots ({', '.join(names)}) VALUES ({placeholders})", values)
    selected = ["record_id", "fields_json", "expiration", "strike", "multiplier"]
    if carrier_column and carrier_selected:
        selected.append("lot_id")
    return conn.execute(f"SELECT {', '.join(selected)} FROM position_lots").fetchone()


def test_position_lot_row_to_record_keeps_the_column_and_the_carrier_distinct() -> None:
    out = position_lot_row_to_record(_row(record_id="R1", lot_id="L1"))

    assert out["record_id"] == "R1"
    assert out["lot_id"] == "L1"


def test_position_lot_row_to_record_falls_back_to_record_id_for_a_null_carrier() -> None:
    out = position_lot_row_to_record(_row(record_id="R1", lot_id=None))

    assert out["record_id"] == "R1"
    assert out["lot_id"] == "R1"


def test_position_lot_row_to_record_tolerates_a_store_without_the_carrier_column() -> None:
    out = position_lot_row_to_record(_row(record_id="R1", lot_id=None, carrier_column=False))

    assert out["record_id"] == "R1"
    assert out["lot_id"] == "R1"


def test_position_lot_row_to_record_tolerates_a_narrower_select_without_the_carrier() -> None:
    out = position_lot_row_to_record(_row(record_id="R1", lot_id="L1", carrier_selected=False))

    assert out["record_id"] == "R1"
    assert out["lot_id"] == "R1"


def test_position_lot_row_to_record_does_not_derive_record_id_from_the_carrier() -> None:
    out = position_lot_row_to_record(_row(record_id=None, lot_id="L1"))

    assert out["record_id"] == ""
    assert out["lot_id"] == "L1"


def test_position_lot_row_to_record_fingerprints_the_record_id_column() -> None:
    record = position_lot_row_to_record(_row(record_id="R1", lot_id="L1"))

    # ``repository_projection_tail`` hands these dicts to the position
    # fingerprint, whose Mapping branch reads ``record.get("record_id")``. The
    # persisted fingerprint therefore tracks the stored column, and a merge that
    # emits the carrier under that key silently re-keys every stored row.
    assert ordered_position_lots_fingerprint([record]) == ordered_position_lots_fingerprint(
        [{"record_id": "R1", "fields": dict(record["fields"])}]
    )


@pytest.mark.parametrize("raw,expected", [
    (FIELDS_JSON, {"account": "lx", "symbol": "0700.HK"}),
    ("{}", {}),
    ("[]", {}),
    ("null", {}),
    ("1", {}),
    ("true", {}),
    ('"text"', {}),
    ("", {}),
])
def test_position_lot_row_to_record_preserves_default_decode(raw, expected) -> None:
    row = {"lot_id": "L1", "fields_json": raw}
    record = position_lot_row_to_record(row)

    assert record["fields"] == expected
    assert position_lot_row_to_record(row, fields=None) == record


def test_position_lot_row_to_record_default_decode_rejects_invalid_json() -> None:
    row = {"lot_id": "L1", "fields_json": "not json"}
    with pytest.raises(json.JSONDecodeError):
        position_lot_row_to_record(row)
    with pytest.raises(json.JSONDecodeError):
        position_lot_row_to_record(row, fields=None)


@pytest.mark.parametrize("fields", [{}, {"account": "lx", "symbol": "0700.HK"}])
def test_position_lot_row_to_record_uses_predecoded_fields(fields) -> None:
    row = {"lot_id": "L1", "fields_json": "not json"}
    before = dict(fields)

    record = position_lot_row_to_record(row, fields=fields)

    assert record == {"record_id": "L1", "lot_id": "L1", "fields": before}
    assert fields == before


@pytest.mark.parametrize("legacy", [False, True])
def test_position_lot_row_to_record_preserves_predecoded_metadata(legacy) -> None:
    fields = {"account": "lx", "symbol": "0700.HK"}
    columns = {"account": "lx", "source_event_id": "open", "strike": 100, "multiplier": 100}
    row = {"lot_id": "L1", "fields_json": json.dumps(fields), "updated_at_ms": 123, "rowid": 7, **columns}
    if legacy:
        row.update(record_id="R1", expiration=456)
        columns["expiration"] = 456
    else:
        columns["retired_columns"] = ["expiration"]

    record = position_lot_row_to_record(row, fields=fields)

    assert record == position_lot_row_to_record(row)
    assert record == {
        "record_id": "R1" if legacy else "L1", "lot_id": "L1",
        "fields": fields, "columns": columns, "rowid": 7,
    }
