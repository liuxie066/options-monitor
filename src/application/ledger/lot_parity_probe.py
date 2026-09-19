"""Tier-1 read-only parity comparator for ``trade_events -> position_lots``.

This is the instrument of gateflow slice ``lot-parity-probe``
(``docs/gateflow/order-unification-20260919/plan.md`` §"Slice 1 —— 判定器").
It answers one question: **does the replay reproduce the stored rows?**

Three faces are compared, and only these three:

* **Face A (payload)** — the stored ``fields_json`` object against the ``fields``
  the replay produced. **Neither side is healed**: the stored side is read as
  raw JSON, so the existing codec's column-heal (``position_lot_row_to_record``)
  is deliberately *not* applied. Healing one side only would make face A's
  red/green depend on the state of the columns, which is why the columns are
  compared separately on face B (``comparator-spec.md`` §2).
* **Face B (columns)** — the stored value of ``account`` / ``expiration`` /
  ``strike`` / ``multiplier`` / ``source_event_id`` against the value
  re-derived **from the stored payload** by the writer's own derivation
  (``repository_common._position_lot_contract_scalars`` and the two sibling
  expressions in ``_position_lot_storage_values``).
* **Face C (row set)** — the ``lot_id`` set, the row counts, and duplicate
  identities on either side.

``updated_at_ms`` is excluded from every face: it is a wall clock
(``repository_projection_tail.py`` stamps ``now_ms()`` per diff), not a fact, so
two runs can never agree on it.

C-face differences are attributed into four classes so that "the ledger is
missing an event" is never reported as "the projection is unfaithful":

* ``null_source_event_id`` — the stored row's ``source_event_id`` is NULL/empty
  (the column first, then the payload key, so a never-backfilled column does not
  masquerade as "no source event at all"; see ``resolve_row_source_event_id``);
* ``ledger_missing_row`` — non-empty ``source_event_id`` absent from
  ``trade_events.event_id``;
* ``projection_omission`` — the event *is* in the ledger but the replay
  produced no such row;
* ``other`` — every other differing lot (rows present on both sides whose
  difference is in payload keys/values or derived columns — counted on faces A
  and B; plus ``missing_in_store`` and duplicate-identity items). Per plan ④
  these are *not* ①②③.

The four classes are a **partition** of the differing lot ids: ①②③ own every row
the replay did not produce, ④ takes the rest, and a lot id is never counted in
two classes. ``c_attribution.differing_lot_count`` is the divisor the four counts
must add up to (the module asserts it).

Duplicate identities are a face-C fact (``duplicate_identity``). Faces A and B
compare the **first** row of each identity: the second row of a collapsed pair is
the same identity read twice, not a second opinion about the payload, so it is
*reported* on face C (``comparator-spec.md`` §2 asks for the collapse to be
detected) instead of silently entering the payload comparison.

**Read-only by construction**: this module opens its own read-only connection with
``PRAGMA query_only=ON`` (the same idiom as
``position_projection_migration._read_only_connection`` and
``read_only_evidence._connect``) and never touches a write-capable repository
object, so "zero writes" is a property of the connection, not an intention. The
connection is opened as ``mode=ro``, and only for a settled store with no
``-shm``/``-wal`` sidecars does it fall back to ``immutable=1`` (see
``_read_only_connection``); both reads then run inside **one** transaction
(``BEGIN``), so a concurrent commit cannot land between them.

**Green** means exactly one thing: faces A, B and C are all empty. That is the
criterion ``plan.md`` §"Slice 2" uses for its prerequisite (A/B empty *and* all
four C classes zero), and it is deliberately not ``comparator-spec.md`` §7's
allow-list — ``column_differs_known_dirty`` is a tier-2 concept and this tier-1
report has no exempting list, so a known-dirty column difference is red here.
Replay diagnostics (``projection_error_count``) are reported beside ``green`` and
are not part of the definition, for the same reason the plan states the criterion
as three faces.

The report lands outside ``output_shared/state/`` on purpose (plan ⑥): the
running state tree belongs to the runtime artifacts, and mixing an operator
diagnostic into it would make the two indistinguishable.

Pure comparison logic plus a thin module CLI::

    PYTHONPATH=. python3 -m src.application.ledger.lot_parity_probe \
        --db /tmp/om-readonly-<ts>.sqlite3 --out /tmp/lot-parity-<ts>.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from src.application.ledger.event_codec import trade_event_application_payload
from src.application.ledger.publisher import (
    project_stored_trade_events_to_position_lots,
)
# The derivation below is deliberately the writer's own: importing it (rather
# than restating it) is what makes face B "same derivation the writer uses"
# instead of "a second opinion that can drift".
from src.application.ledger.repository_common import (
    _position_lot_contract_scalars,
)

SCHEMA_KIND = "position_lot_parity_probe"
SCHEMA_VERSION = "1.0"
TIER = "tier-1"

#: ``green`` means this and nothing else (see the module docstring).
GREEN_CRITERION = "faces a_payload, b_columns and c_rows are all empty"

#: Wall clock, stamped per diff (``repository_projection_tail.py``); never comparable.
EXCLUDED_PAYLOAD_KEYS = ("updated_at_ms",)

#: Face B's column set: the five columns derived from the payload by the writer.
DERIVED_COLUMNS = ("account", "expiration", "strike", "multiplier", "source_event_id")

#: Extra key of a derivation result (not a column): the message of the writer's
#: fail-fast for a payload the writer would refuse.
WRITER_RAISES_KEY = "writer_raises"

C_ATTRIBUTION_CLASSES = (
    "null_source_event_id",
    "ledger_missing_row",
    "projection_omission",
    "other",
)

DEFAULT_SAMPLE_LIMIT = 20

#: How many detail items ``probe_summary`` carries into the CLI report.
CLI_SAMPLE_LIMIT = 3

#: ``output_shared/state`` is the runtime state tree; probe reports must not land there.
RUNTIME_STATE_PATH_PARTS = ("output_shared", "state")

#: The read-only URI modes, and what each one promises. A *live* store keeps
#: ``mode=ro``; a settled copy (no sidecars, no writer) may use ``immutable=1``.
LIVE_READ_MODE = "ro"
SETTLED_READ_MODE = "ro+immutable"

_TOLERANCE = 1e-9


def _has_wal_sidecars(resolved: Path) -> bool:
    return any(Path(f"{resolved}{suffix}").exists() for suffix in ("-wal", "-shm"))


def _connect_read_only(resolved: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"{resolved.as_uri()}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        # ``sqlite3.connect`` is lazy: it does not touch the file until the first
        # statement, and "unable to open database file" for a settled WAL store
        # arrives exactly there (not at connect time). Force the open now, or the
        # caller's fallback would be deciding on a connection that has not
        # happened yet.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _read_only_connection(path: Path) -> Iterator[tuple[sqlite3.Connection, str]]:
    """Open the store read-only, in one snapshot, and say which mode it took.

    Two modes, chosen by what the store *is*:

    * a store that may still have writers is opened ``mode=ro``;
    * a **settled** copy — the ``.backup`` file slice 1 is defined against, whose
      ``-shm``/``-wal`` are gone — is opened ``mode=ro&immutable=1``.

    The second mode is not a preference, it is the only way in: a WAL database
    cannot be opened read-only when SQLite is not allowed to create the
    shared-memory file, and a settled store has no ``-shm`` to reuse, so
    ``mode=ro`` fails with "unable to open database file" on exactly the input
    this probe exists to read.

    ``immutable=1`` is only ever applied after ``mode=ro`` failed *and* no
    ``-wal``/``-shm`` exists. Both halves of that guard matter: a writer attached
    to a WAL store holds its ``-shm``, so sidecar-free means no live writer to
    race; and a ``-wal`` file means committed data may live outside the main
    database file, which ``immutable=1`` would read straight past. When the guard
    does not hold, the original ``mode=ro`` failure is what propagates.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise ValueError(f"position lot parity probe store does not exist: {resolved}")
    try:
        conn = _connect_read_only(resolved, immutable=False)
        mode = LIVE_READ_MODE
    except sqlite3.OperationalError:
        if _has_wal_sidecars(resolved):
            raise
        conn = _connect_read_only(resolved, immutable=True)
        mode = SETTLED_READ_MODE
    try:
        # One snapshot for both reads, the idiom of ``read_only_evidence``:
        # without it each SELECT takes its own snapshot, and a write committed
        # between them fabricates a row-set difference (a stored row the replay
        # no longer produces) that is indistinguishable from a real projection
        # omission — the one class slice 2 treats as a stop condition.
        conn.execute("BEGIN")
        yield conn, mode
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(
        str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _decode_fields_json(raw: Any, *, lot_id: str) -> dict[str, Any]:
    if raw is None or str(raw).strip() == "":
        # ``sqlite_row_codec.position_lot_row_to_record`` reads a NULL payload as
        # an empty one; the probe reads it the same way so that "no payload" is
        # not a second, stricter vocabulary from the read side's own idiom.
        return {}
    try:
        fields = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        # Never read a malformed payload as "no difference".
        raise ValueError(f"stored position lot payload is not valid JSON: {lot_id}") from exc
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise ValueError(f"stored position lot payload must be an object: {lot_id}")
    return fields


def read_stored_position_lots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read the stored rows **raw** — no codec, no column heal.

    Identity follows the same carrier-or-fallback rule as
    ``sqlite_row_codec.position_lot_row_to_record`` and
    ``read_only_evidence._read_position_lots``: the ``lot_id`` column when the
    store has one (production does not yet), else the ``record_id`` column.

    Both identity columns are probed rather than assumed. ``record_id`` is the
    pre-slice-3 spelling of the carrier and slice 3 validates its ``RENAME
    COLUMN record_id TO lot_id`` by re-running this comparison — a hard-coded
    column name would make the probe die on the rename instead of describing the
    store on the other side of it.
    """
    if not _table_exists(conn, "position_lots"):
        raise ValueError("position lot parity probe requires a position_lots table")
    columns = _table_columns(conn, "position_lots")
    missing_columns = sorted(set(DERIVED_COLUMNS) - columns)
    if missing_columns:
        # Loud, not silent: a store without the columns face B compares would
        # otherwise look like a store with nothing to compare.
        raise ValueError(
            "position_lots is missing derived columns required by face B: "
            + ", ".join(missing_columns)
        )
    identity_columns = [name for name in ("lot_id", "record_id") if name in columns]
    if not identity_columns:
        raise ValueError(
            "position_lots has neither lot_id nor record_id: the probe cannot "
            "identify its rows"
        )
    carrier = "lot_id" if "lot_id" in columns else "NULL AS lot_id"
    record_carrier = "record_id" if "record_id" in columns else "NULL AS record_id"
    # The guard above guarantees one of the two is present, and ``lot_id`` wins
    # when both are: the same precedence the two existing read surfaces use.
    order_key = identity_columns[0]
    rows = conn.execute(
        f"""
        SELECT {record_carrier}, {carrier}, fields_json, account, expiration, strike,
               multiplier, source_event_id
        FROM position_lots
        ORDER BY {order_key} ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record_id = str(row["record_id"] or "")
        raw_carrier = row["lot_id"] if "lot_id" in row.keys() else None
        lot_id = str(raw_carrier).strip() if raw_carrier not in (None, "") else record_id
        out.append(
            {
                "record_id": record_id,
                "lot_id": lot_id,
                "fields": _decode_fields_json(row["fields_json"], lot_id=lot_id),
                "columns": {
                    column: row[column] for column in DERIVED_COLUMNS
                },
            }
        )
    return out


def _decode_event_json(raw: Any, *, event_id: str) -> dict[str, Any]:
    """Read one ledger payload, loudly.

    Symmetric with ``_decode_fields_json``: a ledger row the probe cannot read is
    a fact about the ledger, not evidence that the projection is unfaithful. The
    alternative — collect the id but drop the payload — was the worst of both:
    the id stayed in the ledger id set, so every stored row referencing it was
    attributed to ``projection_omission``, the one class slice 2 treats as a stop
    condition, on the strength of a corrupt ledger row.
    """
    try:
        item = json.loads(str(raw) or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stored trade event payload is not valid JSON: {event_id}") from exc
    if not isinstance(item, dict):
        raise ValueError(f"stored trade event payload must be an object: {event_id}")
    return item


def read_stored_events(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Read the ledger events in the replay's own order, plus the stored event-id set."""
    if not _table_exists(conn, "trade_events"):
        raise ValueError("position lot parity probe requires a trade_events table")
    rows = conn.execute(
        """
        SELECT event_id, event_json
        FROM trade_events
        ORDER BY trade_time_ms ASC, event_id ASC
        """
    ).fetchall()
    event_ids: set[str] = set()
    events: list[dict[str, Any]] = []
    for row in rows:
        event_id = str(row["event_id"] or "").strip()
        event_ids.add(event_id)
        events.append(trade_event_application_payload(_decode_event_json(row["event_json"], event_id=event_id)))
    return events, event_ids


def _comparable_keys(fields: dict[str, Any]) -> list[str]:
    return sorted(key for key in fields if key not in EXCLUDED_PAYLOAD_KEYS)


def compare_payload_face(
    *,
    stored_fields: dict[str, Any],
    projected_fields: dict[str, Any],
) -> dict[str, Any]:
    """Face A: key set and values, with neither side healed."""
    stored_keys = set(_comparable_keys(stored_fields))
    projected_keys = set(_comparable_keys(projected_fields))
    only_in_store = sorted(stored_keys - projected_keys)
    only_in_projection = sorted(projected_keys - stored_keys)
    value_differences: list[dict[str, Any]] = []
    for key in sorted(stored_keys & projected_keys):
        if _canonical(stored_fields[key]) != _canonical(projected_fields[key]):
            value_differences.append(
                {
                    "key": key,
                    "stored": stored_fields[key],
                    "projected": projected_fields[key],
                }
            )
    return {
        "keys_only_in_store": only_in_store,
        "keys_only_in_projection": only_in_projection,
        "value_differences": value_differences,
        "differs": bool(only_in_store or only_in_projection or value_differences),
    }


def derive_stored_row_columns(fields: dict[str, Any]) -> dict[str, Any]:
    """Re-derive the five columns from a payload the way the writer does.

    ``account`` and ``source_event_id`` come from the two sibling expressions in
    ``repository_common._position_lot_storage_values``; the other three come from
    ``_position_lot_contract_scalars``. A payload whose ``account`` is missing or
    not lowercase makes the *writer* raise ``ValueError``
    (``repository_common.py``:218-221) — the one derived column comparator-spec
    §4 calls "缺了就响".

    The probe must not raise (it has to keep walking the store) and it must not
    turn that fail-fast into equality either: a payload the writer refuses cannot
    be the payload the writer wrote, so the refusal travels back under
    ``WRITER_RAISES_KEY`` and ``compare_column_face`` counts it as a face-B
    difference. ``tests/test_lot_parity_probe.py`` binds this derivation to the
    writer's own, column by column, so the two cannot drift apart silently.
    """
    expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
    source_event_id = (
        str(fields.get("source_event_id")) if fields.get("source_event_id") else None
    )
    account = str(fields.get("account") or "").strip()
    writer_error: str | None = None
    if not account:
        writer_error = "position lot account is required"
    elif account != account.lower():
        writer_error = "position lot account must be lowercase"
    return {
        "account": account or None,
        "expiration": int(expiration_ms) if expiration_ms is not None else None,
        "strike": float(strike) if strike is not None else None,
        "multiplier": float(multiplier) if multiplier is not None else None,
        "source_event_id": source_event_id,
        WRITER_RAISES_KEY: writer_error,
    }


def compare_column_face(
    *,
    stored_columns: dict[str, Any],
    derived_columns: dict[str, Any],
) -> list[dict[str, Any]]:
    """Face B: stored column value vs the value re-derived from the stored payload."""
    writer_error = str(derived_columns.get(WRITER_RAISES_KEY) or "")
    differences: list[dict[str, Any]] = []
    for column in DERIVED_COLUMNS:
        if writer_error and column == "account":
            # One difference per root cause: the writer refuses the whole
            # payload, so comparing the column value as well would report the
            # same rejection twice.
            differences.append(
                {
                    "column": column,
                    "stored": stored_columns.get(column),
                    "derived_from_stored_payload": None,
                    "writer_raises": writer_error,
                }
            )
            continue
        stored = stored_columns.get(column)
        derived = derived_columns.get(column)
        if not _scalar_matches(stored, derived, column=column):
            differences.append(
                {
                    "column": column,
                    "stored": stored,
                    "derived_from_stored_payload": derived,
                }
            )
    return differences


def _scalar_matches(left: Any, right: Any, *, column: str) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if column in {"expiration", "strike", "multiplier"}:
        return abs(float(left) - float(right)) < _TOLERANCE
    return str(left) == str(right)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return repr(value)


def _index_by_identity(
    rows: Sequence[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, int]]]:
    """Index rows by identity, keeping the first row of a repeated identity.

    The repeats are returned as duplicate counts rather than folded into the
    index: faces A and B then compare the first row of each identity only, and
    the collapse itself is reported on face C, where a repeated identity is a
    fact about the row set rather than a second opinion about the payload.
    """
    index: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in rows:
        identity = str(row["lot_id"])
        counts[identity] = counts.get(identity, 0) + 1
        index.setdefault(identity, row)
    duplicates = sorted(
        (identity, count) for identity, count in counts.items() if count > 1
    )
    return index, duplicates


def resolve_row_source_event_id(row: dict[str, Any]) -> tuple[str | None, str]:
    """The row's own ``source_event_id``, and where it came from.

    The column is the writer's derivation of the payload key
    (``repository_common._position_lot_storage_values``:229), so it wins. The
    payload is consulted only when the column is NULL/empty — a row whose column
    was never backfilled must not be read as "no source event id at all", which
    would hide a real ledger gap behind ①.
    """
    column = str(row["columns"]["source_event_id"] or "").strip()
    if column:
        return column, "column"
    payload = str(row["fields"].get("source_event_id") or "").strip()
    if payload:
        return payload, "payload"
    return None, "absent"


def attribute_c_difference(
    *,
    source_event_id: Any,
    ledger_event_ids: set[str],
) -> str:
    """Classify a stored row the replay did not produce (plan ①②③)."""
    value = str(source_event_id or "").strip()
    if not value:
        return "null_source_event_id"
    if value not in ledger_event_ids:
        return "ledger_missing_row"
    return "projection_omission"


def _projected_lot_row(lot: Any) -> dict[str, Any]:
    payload = lot.to_dict() if hasattr(lot, "to_dict") else lot
    if not isinstance(payload, dict):
        raise ValueError("projected position lot must be a mapping")
    fields = payload.get("fields")
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        # The stored side raises for a non-object payload; the projected side
        # must not answer the same question with a shrug ({}).
        raise ValueError("projected position lot fields must be an object")
    return {
        "lot_id": str(payload.get("lot_id") or "").strip(),
        "fields": dict(fields),
    }


def run_lot_parity_probe(
    *,
    sqlite_path: str | Path,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Run the tier-1 comparison and return the report. Performs no writes.

    ``green`` is ``GREEN_CRITERION``: all three faces empty (see the module
    docstring). It is the probe's whole verdict — ``difference_count`` is the
    arithmetic behind it, not a second opinion.
    """
    limit = max(0, int(sample_limit))
    path = Path(str(sqlite_path))

    with _read_only_connection(path) as (conn, connection_mode):
        stored_rows = read_stored_position_lots(conn)
        events, ledger_event_ids = read_stored_events(conn)

    projection = project_stored_trade_events_to_position_lots(events)
    projected_rows = [_projected_lot_row(lot) for lot in projection.lots]

    stored_by_id, stored_duplicates = _index_by_identity(stored_rows)
    projected_by_id, projected_duplicates = _index_by_identity(projected_rows)

    aligned_ids = sorted(stored_by_id.keys() & projected_by_id.keys())
    extra_ids = sorted(stored_by_id.keys() - projected_by_id.keys())
    missing_ids = sorted(projected_by_id.keys() - stored_by_id.keys())

    a_items: list[dict[str, Any]] = []
    a_key_set_difference_lot_count = 0
    a_value_difference_count = 0
    b_items: list[dict[str, Any]] = []
    for lot_id in aligned_ids:
        stored_row = stored_by_id[lot_id]
        projected_row = projected_by_id[lot_id]
        payload_diff = compare_payload_face(
            stored_fields=stored_row["fields"],
            projected_fields=projected_row["fields"],
        )
        if payload_diff["differs"]:
            key_set_difference = bool(
                payload_diff["keys_only_in_store"]
                or payload_diff["keys_only_in_projection"]
            )
            if key_set_difference:
                a_key_set_difference_lot_count += 1
            a_value_difference_count += len(payload_diff["value_differences"])
            a_items.append({"lot_id": lot_id, **payload_diff})
        for difference in compare_column_face(
            stored_columns=stored_row["columns"],
            derived_columns=derive_stored_row_columns(stored_row["fields"]),
        ):
            b_items.append({"lot_id": lot_id, **difference})

    a_differing_lot_count = len(a_items)

    c_items: list[dict[str, Any]] = []
    for lot_id in extra_ids:
        c_items.append(
            {
                "status": "extra_in_store",
                "lot_id": lot_id,
                "record_id": stored_by_id[lot_id]["record_id"],
            }
        )
    for lot_id in missing_ids:
        c_items.append({"status": "missing_in_store", "lot_id": lot_id})
    for lot_id, occurrences in stored_duplicates:
        c_items.append(
            {
                "status": "duplicate_identity",
                "side": "store",
                "lot_id": lot_id,
                "occurrences": occurrences,
            }
        )
    for lot_id, occurrences in projected_duplicates:
        c_items.append(
            {
                "status": "duplicate_identity",
                "side": "projection",
                "lot_id": lot_id,
                "occurrences": occurrences,
            }
        )

    count_mismatch = len(stored_rows) != len(projected_rows)
    c_set_difference_count = len(extra_ids) + len(missing_ids)
    c_duplicate_count = len(stored_duplicates) + len(projected_duplicates)
    b_writer_raises_count = sum(1 for item in b_items if WRITER_RAISES_KEY in item)

    # Attribution runs over every differing lot: an extra_in_store row gets
    # exactly one of ①②③ (plan is explicit that the NULL branch must not be
    # read as a ledger gap), everything else is ④.
    attribution_counts = {name: 0 for name in C_ATTRIBUTION_CLASSES}
    attribution_items: list[dict[str, Any]] = []
    for lot_id in extra_ids:
        row = stored_by_id[lot_id]
        source_event_id, source_event_id_source = resolve_row_source_event_id(row)
        attribution = attribute_c_difference(
            source_event_id=source_event_id,
            ledger_event_ids=ledger_event_ids,
        )
        attribution_counts[attribution] += 1
        attribution_items.append(
            {
                "lot_id": lot_id,
                "record_id": row["record_id"],
                "attribution": attribution,
                "source_event_id": source_event_id,
                "source_event_id_source": source_event_id_source,
            }
        )
    differing_aligned_ids = {item["lot_id"] for item in a_items} | {
        item["lot_id"] for item in b_items
    }
    duplicate_ids = {lot_id for lot_id, _count in stored_duplicates} | {
        lot_id for lot_id, _count in projected_duplicates
    }
    # The four classes are a partition of the differing lot ids (plan ①–④): ①②③
    # own every row the replay did not produce, ④ takes the rest. A lot id that
    # ①②③ already attributed is not counted again as ④ — that overlap is what
    # made the four counts add up to more than the number of differing lots, and
    # ③ is a stop condition for slice 2, so inflating it by a repeated row reads
    # as a second, independent failure.
    differing_lot_ids = (
        set(extra_ids) | set(missing_ids) | differing_aligned_ids | duplicate_ids
    )
    other_ids = sorted(differing_lot_ids - set(extra_ids))
    for lot_id in other_ids:
        attribution_counts["other"] += 1
        attribution_items.append(
            {
                "lot_id": lot_id,
                "attribution": "other",
                "source_event_id": None,
            }
        )
    attributed_lot_count = sum(attribution_counts.values())
    if attributed_lot_count != len(differing_lot_ids):
        # A can't-happen guard on the one property the classes exist to have. Get
        # this wrong and ③ stops meaning what slice 2 reads it as.
        raise AssertionError(
            "lot parity probe C attribution is not a partition: "
            f"{attributed_lot_count} attributed lot ids for "
            f"{len(differing_lot_ids)} differing lot ids"
        )

    b_by_column = {column: 0 for column in DERIVED_COLUMNS}
    for item in b_items:
        b_by_column[item["column"]] += 1

    a_difference_count = a_key_set_difference_lot_count + a_value_difference_count
    b_difference_count = len(b_items)
    # ``count_mismatch`` is a restatement of the two counts above — a row count
    # can only differ because an id is extra, missing, or duplicated — so
    # counting it as a third difference made one differing row print
    # ``c_rows=2``. It stays in the report as the boolean comparator-spec §5
    # names; it is not a third difference.
    c_difference_count = c_set_difference_count + c_duplicate_count
    difference_count = a_difference_count + b_difference_count + c_difference_count

    return {
        "schema_kind": SCHEMA_KIND,
        "schema_version": SCHEMA_VERSION,
        "tier": TIER,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sqlite_path": str(path.resolve()),
        "connection_mode": connection_mode,
        "sample_limit": limit,
        "excluded_payload_keys": list(EXCLUDED_PAYLOAD_KEYS),
        "derived_columns": list(DERIVED_COLUMNS),
        "green_criterion": GREEN_CRITERION,
        "notes": [
            "face A compares raw stored fields_json against the replay payload with neither side healed",
            "face B compares each stored derived column against the value re-derived from the stored payload",
            "updated_at_ms is excluded from every face: it is a wall clock, not a fact",
            "green is exactly this: " + GREEN_CRITERION
            + " (plan.md slice 2 prerequisites); comparator-spec.md §7's allow-list is a tier-2 concept this report does not implement",
            "replay diagnostics are counted and are not part of 'green'",
            "c_attribution classes ①②③④ are a partition of c_attribution.differing_lot_count",
            "c_attribution class 'other' is not a row-set gap: it is every differing lot that is not ①②③",
            "c_attribution reads a row's source_event_id from its column first, then its payload key",
            "duplicate identities are a face-C fact; faces A and B compare the first row of each identity",
            "the read-only connection takes one snapshot (BEGIN), so a concurrent commit cannot land between the two reads",
            "the connection is mode=ro, or mode=ro&immutable=1 for a settled copy with no -wal/-shm (no writer to race)",
        ],
        "event_count": len(events),
        "stored_lot_count": len(stored_rows),
        "projected_lot_count": len(projected_rows),
        "projection_diagnostic_count": len(projection.diagnostics),
        "projection_error_count": sum(
            1 for item in projection.diagnostics if getattr(item, "severity", "") == "error"
        ),
        "green": difference_count == 0,
        "difference_count": difference_count,
        "faces": {
            "a_payload": {
                "difference_count": a_difference_count,
                "differing_lot_count": a_differing_lot_count,
                "key_set_difference_lot_count": a_key_set_difference_lot_count,
                "value_difference_count": a_value_difference_count,
                "items": a_items[:limit],
                "items_truncated": len(a_items) > limit,
            },
            "b_columns": {
                "difference_count": b_difference_count,
                "differing_lot_count": len({item["lot_id"] for item in b_items}),
                "writer_raises_count": b_writer_raises_count,
                "by_column": b_by_column,
                "items": b_items[:limit],
                "items_truncated": len(b_items) > limit,
            },
            "c_rows": {
                "difference_count": c_difference_count,
                "lot_id_set_difference_count": c_set_difference_count,
                "count_mismatch": count_mismatch,
                "duplicate_identity_count": c_duplicate_count,
                "extra_in_store_count": len(extra_ids),
                "missing_in_store_count": len(missing_ids),
                "items": c_items[:limit],
                "items_truncated": len(c_items) > limit,
            },
        },
        "c_attribution": {
            **attribution_counts,
            "differing_lot_count": len(differing_lot_ids),
            "items": attribution_items[:limit],
            "items_truncated": len(attribution_items) > limit,
        },
    }


def probe_summary(report: dict[str, Any]) -> dict[str, Any]:
    """The compact shape the CLI mounts into its own report (new key, never ``ok``).

    One verdict per run: ``green`` is the probe's own field, carried through
    unchanged. The CLI must not restate it as a second key — two spellings of one
    fact is how a receipt ends up disagreeing with itself.
    """
    faces = report.get("faces") or {}
    a = faces.get("a_payload") or {}
    b = faces.get("b_columns") or {}
    c = faces.get("c_rows") or {}
    attribution = report.get("c_attribution") or {}
    return {
        "schema_kind": report.get("schema_kind"),
        "schema_version": report.get("schema_version"),
        "tier": report.get("tier"),
        "connection_mode": report.get("connection_mode"),
        "green": bool(report.get("green")),
        "difference_count": int(report.get("difference_count") or 0),
        "a_payload_difference_count": int(a.get("difference_count") or 0),
        "b_columns_difference_count": int(b.get("difference_count") or 0),
        "b_writer_raises_count": int(b.get("writer_raises_count") or 0),
        "c_rows_difference_count": int(c.get("difference_count") or 0),
        "c_lot_id_set_difference_count": int(c.get("lot_id_set_difference_count") or 0),
        "c_duplicate_identity_count": int(c.get("duplicate_identity_count") or 0),
        "c_extra_in_store_count": int(c.get("extra_in_store_count") or 0),
        "c_missing_in_store_count": int(c.get("missing_in_store_count") or 0),
        "c_count_mismatch": bool(c.get("count_mismatch")),
        "c_attribution": {
            name: int(attribution.get(name) or 0) for name in C_ATTRIBUTION_CLASSES
        },
        # The counts say how much is wrong; the samples say what. A red run whose
        # summary carries no detail sends the reader back to the probe's own
        # report file, which is exactly the trip the enforced channel exists to
        # avoid.
        "samples": {
            "a_payload": list(a.get("items") or [])[:CLI_SAMPLE_LIMIT],
            "b_columns": list(b.get("items") or [])[:CLI_SAMPLE_LIMIT],
            "c_rows": list(c.get("items") or [])[:CLI_SAMPLE_LIMIT],
            "c_attribution": list(attribution.get("items") or [])[:CLI_SAMPLE_LIMIT],
        },
    }


def assert_report_path_outside_runtime_state(path: Path) -> Path:
    """Probe reports must not land in ``output_shared/state/`` (plan ⑥)."""
    resolved = Path(path).resolve()
    parts = resolved.parts
    for index in range(len(parts) - 1):
        if parts[index : index + 2] == RUNTIME_STATE_PATH_PARTS:
            raise ValueError(
                "lot parity probe reports must land outside output_shared/state/ "
                f"(would collide with the runtime state tree): {resolved}"
            )
    return resolved


def write_report(report: dict[str, Any], *, out_path: str | Path) -> Path:
    target = assert_report_path_outside_runtime_state(Path(str(out_path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lot_parity_probe",
        description="read-only tier-1 parity probe for trade_events -> position_lots",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="SQLite store path; opened read-only (mode=ro, or immutable=1 for a settled copy)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="report path; must be outside output_shared/state/ (default: print to stdout)",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=DEFAULT_SAMPLE_LIMIT,
        help="how many detail items each face keeps (counts are always totals)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)
    report = run_lot_parity_probe(
        sqlite_path=args.db,
        sample_limit=args.sample_limit,
    )
    if args.out:
        target = write_report(report, out_path=args.out)
        print(
            json.dumps(
                {
                    "report_path": str(target),
                    "green": bool(report["green"]),
                    "connection_mode": report.get("connection_mode"),
                    "difference_count": int(report["difference_count"]),
                    "c_attribution": probe_summary(report)["c_attribution"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "C_ATTRIBUTION_CLASSES",
    "CLI_SAMPLE_LIMIT",
    "DEFAULT_SAMPLE_LIMIT",
    "DERIVED_COLUMNS",
    "EXCLUDED_PAYLOAD_KEYS",
    "GREEN_CRITERION",
    "LIVE_READ_MODE",
    "RUNTIME_STATE_PATH_PARTS",
    "SCHEMA_KIND",
    "SETTLED_READ_MODE",
    "WRITER_RAISES_KEY",
    "attribute_c_difference",
    "assert_report_path_outside_runtime_state",
    "compare_column_face",
    "compare_payload_face",
    "derive_stored_row_columns",
    "main",
    "probe_summary",
    "read_stored_events",
    "read_stored_position_lots",
    "resolve_row_source_event_id",
    "run_lot_parity_probe",
    "write_report",
]
