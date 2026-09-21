from __future__ import annotations

import json
import sqlite3
from typing import Any

from domain.domain.ledger.position_fingerprint import (
    ordered_position_lots_fingerprint,
)


#: The five columns the writer derives from a lot payload (``comparator-spec.md``
#: §4). Restated rather than imported: the probe's copy
#: (``lot_parity_probe.DERIVED_COLUMNS``) sits behind ``repository_common``, which
#: imports *this* module, so importing it here would close a cycle. The two tuples
#: are bound to each other by ``tests/test_projection_verify.py``.
DERIVED_COLUMN_NAMES = ("account", "expiration", "strike", "multiplier", "source_event_id")


def position_lots_reads_face_b(conn: sqlite3.Connection) -> bool:
    """Whether this ``position_lots`` carries all five derived columns.

    Not every store does: the rebuild and migration paths work on shapes that
    predate ``account``/``source_event_id``, and a SELECT naming a missing column
    fails before the key-presence guard below can say "this read carried no
    columns". Readers therefore pick their statement from this answer, and the
    caller sees ``store_face.columns_read == False`` instead of a guess.
    """
    present = {str(row[1]) for row in conn.execute("PRAGMA table_info(position_lots)")}
    return set(DERIVED_COLUMN_NAMES) <= present


def position_lot_row_to_record(row: Any) -> dict[str, Any]:
    # No column heal. ``comparator-spec.md`` §2 rules that both sides of the
    # parity comparison are normalized to one convention and that the columns are
    # compared on their own face, so re-injecting ``expiration``/``strike``/
    # ``multiplier`` from the row's columns made the store side carry two flat
    # keys ``PositionLot.to_dict()`` does not have -- and ``projection_verify``'s
    # exact dict comparison (``:153``) then reported ``field_mismatch`` for every
    # option row while none of the slice's completion legs could see it. The
    # columns still travel on face B (``lot_parity_probe`` reads them raw).
    fields = json.loads(str(row["fields_json"]) or "{}")
    if not isinstance(fields, dict):
        fields = {}
    # ``or ""`` rather than a bare ``str()``: a NULL record_id would otherwise
    # become the string "None", a fabricated identity that differs from the ""
    # the read-only evidence surface emits for the same row.
    stored_record_id = str(row["record_id"] or "")
    # Both identity keys are emitted so consumers can converge on lot_id without
    # a coupled rename. Two row shapes legitimately fall back to record_id: a
    # legacy row whose carrier is still NULL, and a narrower SELECT that predates
    # the carrier. The gated backfill fills the column in.
    #
    # The two keys stay separate facts: ``record_id`` is the stored column and
    # ``lot_id`` is the carrier-or-fallback. Today the carrier is only ever
    # backfilled from that column, which masks the difference; once the carrier
    # is independently authoritative the same row has two distinct identities,
    # and this key must not follow the carrier, because the read-only evidence
    # surface (``read_only_evidence._read_position_lots``) and the persisted
    # position fingerprint both read ``record_id`` from the column.
    raw_lot_id = row["lot_id"] if "lot_id" in row.keys() else None
    lot_id = str(raw_lot_id).strip() if raw_lot_id not in (None, "") else stored_record_id
    record = {
        "record_id": stored_record_id,
        "lot_id": lot_id,
        "fields": fields,
    }
    # Face B travels with the record: the five derived columns, so a comparison
    # can put each stored column next to the value re-derived from the stored
    # payload (``comparator-spec.md`` §1/§4, ``projection_verify``). Emitted only
    # when the read carried *all* five -- a narrower SELECT would otherwise look
    # like a store that agreed on whichever columns it happened to fetch, and the
    # column-face report has no other way to tell the two apart.
    columns = {
        column: row[column]
        for column in ("account", "expiration", "strike", "multiplier", "source_event_id")
        if column in row.keys()
    }
    if len(columns) == len(DERIVED_COLUMN_NAMES):
        record["columns"] = columns
    # ``rowid`` is not a column of the lot and not part of any face today: §3
    # compares it across the rewrite (pre store vs post store), which is a
    # two-snapshot job. It rides along so the read that will serve as the "pre"
    # snapshot carries it. Neither key changes ``position_lots_fingerprint`` /
    # ``ordered_position_lots_fingerprint``: ``position_fingerprint._record_parts``
    # reads only ``record_id`` and ``fields``.
    if "rowid" in row.keys():
        record["rowid"] = row["rowid"]
    return record


def read_current_decision_projection_inputs_from_conn(
    conn: sqlite3.Connection,
    account: str,
    *,
    include_identities: bool = True,
) -> dict[str, Any]:
    """Read bounded current-decision inputs from an existing snapshot."""

    account_value = str(account or "").strip()
    if not account_value or account_value != account_value.lower():
        raise ValueError("current decision account must be lowercase")
    source = conn.execute(
        "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
    ).fetchone()
    head = conn.execute(
        "SELECT * FROM position_projection_heads WHERE account = ?",
        (account_value,),
    ).fetchone()
    generation = conn.execute(
        "SELECT * FROM current_decision_input_generations WHERE account = ?",
        (account_value,),
    ).fetchone()
    projection = conn.execute(
        "SELECT * FROM current_decision_projections WHERE account = ?",
        (account_value,),
    ).fetchone()
    # This reader is reached both from the write path (where ``_init_db`` has
    # already ensured the carrier) and from the read-only evidence surface, which
    # by construction cannot add a column and must still serve a store that
    # predates it. Selecting ``lot_id`` unconditionally made the second case
    # raise ``no such column``, which the current-decision runtime converts into
    # a blanket ``data_unavailable``. Same probe idiom as
    # ``read_only_evidence._read_position_lots``.
    lot_columns = {
        str(item["name"])
        for item in conn.execute("PRAGMA table_info(position_lots)").fetchall()
    }
    carrier = "lot_id" if "lot_id" in lot_columns else "NULL AS lot_id"
    lots = [
        position_lot_row_to_record(row)
        for row in conn.execute(
            f"""
            SELECT record_id, {carrier}, fields_json, expiration, strike, multiplier
            FROM position_lots
            WHERE account = ?
            ORDER BY record_id ASC
            """,
            (account_value,),
        )
    ]
    identities = []
    if include_identities:
        for row in conn.execute(
            """
            SELECT raw_json FROM strategy_group_identities
            WHERE account = ?
            ORDER BY account ASC, symbol ASC, group_id ASC
            """,
            (account_value,),
        ):
            identity = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(identity, dict):
                raise ValueError("stored ledger JSON value must be an object")
            identities.append(identity)
    schema = conn.execute("PRAGMA schema_version").fetchone()
    return {
        "source": dict(source) if source is not None else None,
        "head": dict(head) if head is not None else None,
        "generation": dict(generation) if generation is not None else None,
        "projection": dict(projection) if projection is not None else None,
        "lots_fingerprint": ordered_position_lots_fingerprint(lots),
        "lots": lots,
        "lot_count": len(lots),
        "identities": identities,
        "schema_cookie": int(schema[0]) if schema is not None else 0,
    }
