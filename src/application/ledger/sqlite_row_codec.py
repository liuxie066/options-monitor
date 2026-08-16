from __future__ import annotations

import json
import sqlite3
from typing import Any

from domain.domain.ledger.position_fingerprint import (
    ordered_position_lots_fingerprint,
)


def position_lot_row_to_record(row: Any) -> dict[str, Any]:
    fields = json.loads(str(row["fields_json"]) or "{}")
    if not isinstance(fields, dict):
        fields = {}
    if fields.get("expiration") in (None, "") and row["expiration"] not in (None, ""):
        fields["expiration"] = int(row["expiration"])
    if fields.get("strike") is None and row["strike"] is not None:
        fields["strike"] = float(row["strike"])
    if fields.get("multiplier") is None and row["multiplier"] is not None:
        raw_multiplier = float(row["multiplier"])
        fields["multiplier"] = int(raw_multiplier) if raw_multiplier.is_integer() else raw_multiplier
    return {
        "record_id": str(row["record_id"]),
        "fields": fields,
    }


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
    lots = [
        position_lot_row_to_record(row)
        for row in conn.execute(
            """
            SELECT record_id, fields_json, expiration, strike, multiplier
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
