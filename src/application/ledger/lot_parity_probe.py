"""Tier-1 read-only parity comparator for ``trade_events -> position_lots``.

This is the instrument of gateflow slice ``lot-parity-probe``
(``docs/gateflow/order-unification-20260919/plan.md`` §"Slice 1 —— 判定器").
It answers one question: **does the replay reproduce the stored rows?**

Three faces are compared, and only these three:

* **Face A (payload)** — the stored ``fields_json`` object against the ``fields``
  the replay produced. Same-shape payloads are compared raw. During R1, a
  v3.5 flat payload and a v2 nested replay compare the shared business facts
  through their two published spellings; migration inventory separately proves
  that no old-only fact is discarded. The stored side is still read as raw JSON,
  so ``position_lot_row_to_record`` is deliberately *not* applied.
  (That codec used to heal ``expiration``/``strike``/``multiplier`` back into the
  decoded payload; slice 2 deleted the heal, which is the comparator-spec §2
  convention "两侧都不 heal, 列单独进 B 面比" made real. This reader never went
  through it either way.) Healing one side only would make face A's red/green
  depend on the state of the columns, which is why the columns are compared
  separately on face B (``comparator-spec.md`` §2).
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
the replay did not produce, ④ takes every remaining differing lot, and a lot id is
never counted in two classes. Each class count is incremented exactly once per
attributed item, so ``c_attribution.differing_lot_count`` — the number of distinct
ids ①②③④ cover — is what the four counts add up to. The module asserts the two
facts that make that arithmetic hold: the attributed ids cover every differing id,
and none is attributed twice (``run_lot_parity_probe``). Both sides of that check
are built from the same expressions the attribution walks, so **no store shape can
trip it**; it fires when an edit to the attribution code breaks the partition
(proved by injecting such an edit — it names the source and the missing ids), and
for the same reason the test module can bind it no more tightly than the fixtures
of the four classes do.

Duplicate identities are a face-C fact (``duplicate_identity``). Faces A and B
compare the **first** row of each identity: the second row of a collapsed pair is
the same identity read twice, not a second opinion about the payload, so it is
*reported* on face C (``comparator-spec.md`` §2 asks for the collapse to be
detected) instead of silently entering the payload comparison.

**Read-only by construction**: this module opens its own connection with
``mode=ro`` (the same idiom as
``position_projection_migration._read_only_connection`` and
``read_only_evidence._connect``), sets ``PRAGMA query_only=ON`` on it, and never
touches a write-capable repository object, so "zero writes" is a property of the
connection, not an intention. It falls back to ``immutable=1`` only when that
open failed with "unable to open database file" *and* the store has no
``-shm``/``-wal`` sidecar. That guard is a **filename test and nothing more** — it
does not detect writers, and no clause of it judges a ``-journal`` (see
``_read_only_connection``: a hot journal is refused by SQLite's own error, and a
live WAL writer whose sidecars are gone is *not* refused at all — measured). Every
other failure — a lock, a rollback SQLite may not perform read-only — propagates,
and the run is reported as "cannot run" rather than answered with a verdict. Both
reads then run inside **one** transaction (``BEGIN``), so a concurrent commit
cannot land between them.

**Green** means exactly one thing: faces A, B and C are all empty. That is the
criterion ``plan.md`` §"Slice 2" uses for its prerequisite (A/B empty *and* all
four C classes zero), and it is deliberately not ``comparator-spec.md`` §7's
allow-list — ``column_differs_known_dirty`` is a tier-2 concept and this module
implements no exempting list, so any column difference is a difference and red
here (bound: ``test_probe_face_b_reports_a_column_difference``). Replay
diagnostics (``projection_error_count``) are reported beside ``green`` and are
not part of the definition — a run whose replay emitted an error but reproduced
every row is green (bound:
``test_probe_reports_replay_diagnostics_beside_green_without_changing_it``).

The report lands outside ``output_shared/state/`` on purpose (plan ⑥): the
running state tree belongs to the runtime artifacts, and mixing an operator
diagnostic into it would make the two indistinguishable.

Pure comparison logic plus a thin module CLI::

    PYTHONPATH=. python3 -m src.application.ledger.lot_parity_probe \
        --db /tmp/om-readonly-<ts>.sqlite3 --out /tmp/lot-parity-<ts>.json
"""

from __future__ import annotations

from .sqlite_row_codec import position_lots_use_lot_id

import argparse
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from domain.domain.money import canonical_decimal_text, quantize_money
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

#: Extra key of a derivation result (not a column): the derived columns that
#: refusal covers, one entry per column the writer's own message names. The
#: writer's messages name one column (``account``) or several
#: (``expiration``/``strike``); ``compare_column_face`` skips every one of them
#: (they have no derivation to compare against) and reports the refusal once.
WRITER_RAISES_COLUMNS_KEY = "writer_raises_columns"

C_ATTRIBUTION_CLASSES = (
    "null_source_event_id",
    "ledger_missing_row",
    "projection_omission",
    "other",
)

DEFAULT_SAMPLE_LIMIT = 20

#: How many detail items ``probe_summary`` carries into the CLI report — a display
#: budget, and nothing asserts the number (the faces' own counts are the totals).
CLI_SAMPLE_LIMIT = 3

#: ``output_shared/state`` is the runtime state tree; probe reports must not land there.
RUNTIME_STATE_PATH_PARTS = ("output_shared", "state")

#: The read-only URI modes, and what each one promises. A *live* store keeps
#: ``mode=ro``; a copy with nothing outstanding to recover may use ``immutable=1``.
LIVE_READ_MODE = "ro"
SETTLED_READ_MODE = "ro+immutable"

#: The one ``mode=ro`` failure the immutable fallback exists for: a settled WAL
#: store this build will not open read-only. SQLite refuses that open when it may
#: not create the shared-memory file — no readable sidecar and a directory that
#: does not allow the creation (sqlite.org/wal.html, "Read-Only Databases", the
#: rule relaxed in 3.22.0) — so the refusal belongs to the **build and the
#: directory**, not to the store: measured, the CI runner opens the very same
#: settled copy ``mode=ro``. Every other ``OperationalError`` is a fact about the
#: store — a writer holding a lock, a rollback the read-only connection cannot
#: perform — and must reach the caller instead of being answered by a lock-free
#: read.
_CANNOT_OPEN_MARKER = "unable to open database file"

_TOLERANCE = 1e-9


def _has_wal_sidecars(resolved: Path) -> bool:
    """Whether the store carries a ``-wal``/``-shm`` sidecar — a **filename test**.

    That is the whole guard the fallback has, and this docstring says so on
    purpose: it answers "does this store still have the two files a WAL writer's
    lock lives on", not "is a writer attached". What it does *not* test is a
    ``-journal``: a hot one is refused by SQLite's own error before this function
    is consulted (see ``_read_only_connection``).
    """
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

    Two modes, chosen by what the store *is* and by which one the build can open:

    * a store that may still have writers is opened ``mode=ro``;
    * a copy with nothing outstanding to recover — the ``.backup`` file slice 1 is
      defined against, whose ``-shm``/``-wal`` are gone — is opened ``mode=ro`` as
      well where the build can open one, and ``mode=ro&immutable=1`` where it
      cannot.

    The second mode is the way in for that input on a build that refuses the
    first. With no ``-shm`` to reuse, ``mode=ro`` has to create the shared-memory
    file, which SQLite allows when the directory permits it and refuses otherwise
    — refusing it with "unable to open database file", the one failure the
    fallback is for. Which of the two a settled copy gets is therefore the
    build's and the directory's decision rather than this module's preference
    (measured: the development build here is refused and the CI runner is not),
    and the report carries the mode that was taken.

    The fallback's trigger is a **condition, not a proof about writers**, and it
    has exactly two clauses:

    * only that one ``OperationalError`` falls through. "database is locked" (a
      writer holds the store) and "attempt to write a readonly database" (SQLite
      found a rollback it may not perform) are facts about the store, and a
      lock-free ``immutable=1`` read would answer them with a *verdict* built on
      state no writer would call settled;
    * and no ``-wal``/``-shm`` sidecar exists (``_has_wal_sidecars``). That is a
      file test: it does not detect writers, and it is **not** a test for a store
      being settled — measured, a live WAL writer whose sidecars were unlinked
      passes it and the run is answered with a verdict, because after that unlink
      ``mode=ro`` fails with cannot-open rather than with the lock.

    What the guard does *not* decide is a ``-journal``. A hot one is where SQLite
    refuses first, and it refuses with "attempt to write a readonly database" —
    not with the marker above — so that failure propagates and the journal is
    never consulted here (measured: with the journal judgment removed entirely the
    hot-journal control still refuses with the same error). The residual risk that
    leaves is explicit: *if* a SQLite version worded a hot-journal open failure as
    "unable to open database file", a store with a pending rollback would fall
    through to ``immutable=1`` and be answered with a verdict. Nothing in this
    module would catch that.

    When either clause does not hold, the original ``mode=ro`` failure is what
    propagates, and the caller reports "cannot run" instead of a judgement.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise ValueError(f"position lot parity probe store does not exist: {resolved}")
    try:
        conn = _connect_read_only(resolved, immutable=False)
        mode = LIVE_READ_MODE
    except sqlite3.OperationalError as exc:
        if _CANNOT_OPEN_MARKER not in str(exc):
            raise
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
    final_shape = "expiration" not in columns and position_lots_use_lot_id(conn)
    compared_columns = tuple(name for name in DERIVED_COLUMNS if not (final_shape and name == "expiration"))
    missing_columns = sorted(set(compared_columns) - columns)
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
    rows = conn.execute(
        "SELECT * FROM position_lots ORDER BY lot_id" if "lot_id" in columns else
        "SELECT * FROM position_lots ORDER BY record_id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record_id = str(row["record_id"] or "") if "record_id" in columns else ""
        raw_carrier = row["lot_id"] if "lot_id" in row.keys() else None
        lot_id = str(raw_carrier).strip() if raw_carrier not in (None, "") else record_id
        out.append(
            {
                "record_id": record_id,
                "lot_id": lot_id,
                "fields": _decode_fields_json(row["fields_json"], lot_id=lot_id),
                "columns": {
                    **{column: row[column] for column in compared_columns},
                    **({"retired_columns": ["expiration"]} if final_shape else {}),
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


_CROSS_SHAPE_FACTS = (
    "broker", "account", "symbol", "option_type", "side", "status", "currency",
    "contracts", "contracts_open", "contracts_closed", "strike", "expiration_ymd",
    "multiplier", "premium", "opened_at", "source_event_id", "position_key",
    "shares_opened", "shares_open", "shares_closed", "cost_basis_total",
)
_NUMERIC_FACTS = frozenset(
    {
        "contracts", "contracts_open", "contracts_closed", "strike", "multiplier",
        "premium", "opened_at", "shares_opened", "shares_open", "shares_closed",
        "cost_basis_total",
    }
)


def _cross_shape_payload(fields: dict[str, Any]) -> dict[str, Any]:
    contract = fields.get("contract_key")
    contract = contract if isinstance(contract, dict) else {}
    normalized = {
        "broker": contract.get("broker") or fields.get("broker"),
        "account": contract.get("account") or fields.get("account"),
        "symbol": contract.get("underlying_symbol") or fields.get("symbol"),
        "option_type": (
            contract.get("option_type")
            if "option_type" in contract
            else fields.get("option_type")
        ),
        "side": fields.get("position_side") or fields.get("side"),
        "status": fields.get("status"),
        "currency": fields.get("currency"),
        "contracts": fields.get("contracts_opened", fields.get("contracts")),
        "contracts_open": fields.get("contracts_open"),
        "contracts_closed": fields.get("contracts_closed"),
        "strike": contract.get("strike") if contract.get("strike") not in (None, "") else fields.get("strike"),
        "expiration_ymd": (
            contract.get("expiration_ymd")
            if "expiration_ymd" in contract
            else fields.get("expiration_ymd")
        ),
        "multiplier": fields.get("multiplier"),
        "premium": fields.get("premium_open", fields.get("premium")),
        "opened_at": fields.get("opened_at_ms", fields.get("opened_at")),
        "source_event_id": fields.get("open_event_id") or fields.get("source_event_id"),
        "position_key": fields.get("position_key"),
        "shares_opened": fields.get("shares_opened"),
        "shares_open": fields.get("shares_open"),
        "shares_closed": fields.get("shares_closed"),
        "cost_basis_total": fields.get("cost_basis_total"),
    }
    out: dict[str, Any] = {}
    for key in _CROSS_SHAPE_FACTS:
        value = normalized.get(key)
        if key in _NUMERIC_FACTS and value not in (None, ""):
            try:
                value = (
                    canonical_decimal_text(quantize_money(value))
                    if key == "premium"
                    else str(Decimal(str(value)).normalize())
                )
            except (InvalidOperation, ValueError):
                pass
        out[key] = value
    return out


def compare_payload_face(
    *,
    stored_fields: dict[str, Any],
    projected_fields: dict[str, Any],
) -> dict[str, Any]:
    """Face A: strict within a shape, shared business facts across R1 shapes."""
    # R1 changes the persisted representation, not these business facts. During
    # the migration window a v3.5 flat row and its v2 replay therefore compare
    # through the two published field mappings; same-shape rows remain byte-strict.
    if isinstance(stored_fields.get("contract_key"), dict) != isinstance(
        projected_fields.get("contract_key"), dict
    ):
        stored_fields = _cross_shape_payload(stored_fields)
        projected_fields = _cross_shape_payload(projected_fields)
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


def _safe_float(value: Any) -> float | None:
    """``safe_float`` as the writer's guard uses it, restated.

    Restated rather than imported: the writer reaches this helper through
    ``repository_common`` (which is outside this slice's allowed files, and whose
    copy comes from the Feishu infrastructure module), and this module's imports
    stay inside ``src.application.ledger`` on purpose. Unlike the casts in
    ``_position_lot_contract_scalars`` it has **no** note fallback, so a payload
    that loses its ``strike`` key is still refused when the note carries one —
    and ``tests/test_lot_parity_probe.py`` binds the two by comparing the probe's
    refusal against the writer's own ``ValueError``.
    """
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _contract_key(fields: dict[str, Any]) -> dict[str, Any]:
    """``repository_common._position_lot_contract_key``, restated.

    The converged payload carries the option contract under ``contract_key``
    instead of as flat siblings; the writer's guards read it from there, so the
    probe's copies have to as well.
    """
    contract_key = fields.get("contract_key")
    return contract_key if isinstance(contract_key, dict) else {}


def _missing_option_contract_fields(fields: dict[str, Any]) -> list[str]:
    """The writer guard for either payload shape in the R1 read window.

    The writer's **first** guard on a payload (``repository_common.py:216``,
    before the two ``account`` rules), so its refusal is the one the writer would
    hit first and the one this derivation has to carry.

    Only ``put``/``call`` payloads are validated; R1 accepts the v2 contract
    carrier first and the v3.5 flat carrier second. Notes never heal a fact.
    """
    contract_key = _contract_key(fields)
    option_type = str(contract_key.get("option_type") or fields.get("option_type") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return []
    missing: list[str] = []
    if contract_key.get("expiration_ymd") in (None, "") and fields.get("expiration_ymd") in (None, "") and fields.get("expiration") in (None, ""):
        missing.append("expiration")
    if _safe_float(contract_key.get("strike")) is None and _safe_float(fields.get("strike")) is None:
        missing.append("strike")
    return missing


def _writer_refusal(*, fields: dict[str, Any], account: str) -> tuple[str | None, tuple[str, ...]]:
    """The first refusal ``_position_lot_storage_values`` would raise, and its columns.

    The writer's order, restated: validate the option contract, then the
    ``account`` rules. Precedence matters as much as the rules — a payload that
    is both incomplete and account-less is refused for the incomplete contract,
    and the probe has to say the same thing.

    The writer embeds ``lot_id`` in both messages (``... lot {lot_id}: missing
    strike``, ``... required: record_id={lot_id}``) and this derivation has none:
    it is a function of a payload, and the row it belongs to already carries the
    lot id on the face-B item. So the copies below drop the id, exactly as the
    ``account`` copy always has.
    """
    missing = _missing_option_contract_fields(fields)
    if missing:
        return (
            "incomplete option position lot: missing " + ", ".join(missing),
            # The refusal names every missing field; the item is emitted once, on
            # the first of them, and the rest are skipped so one refusal is not
            # reported once per column it mentions.
            tuple(missing),
        )
    if not account:
        return "position lot account is required", ("account",)
    if account != account.lower():
        return "position lot account must be lowercase", ("account",)
    return None, ()


def derive_stored_row_columns(fields: dict[str, Any]) -> dict[str, Any]:
    """Re-derive the five columns from a payload the way the writer does.

    ``account`` and ``source_event_id`` are the two expressions that do **not**
    follow an imported helper. R1 reads their v2 carriers first
    (``contract_key.account`` / ``open_event_id``) and their v3.5 flat carriers
    second. The other three come from the imported
    ``_position_lot_contract_scalars``. A payload the
    *writer* refuses — the option contract first (``_validate_position_lot_fields``),
    then ``account`` (``repository_common.py``:217-221), the one derived column
    comparator-spec §4 calls "缺了就响" — has no columns to derive at all.

    The probe must not raise (it has to keep walking the store) and it must not
    turn that fail-fast into equality either: a payload the writer refuses cannot
    be the payload the writer wrote, so the refusal travels back under
    ``WRITER_RAISES_KEY`` — with the columns it covers, under
    ``WRITER_RAISES_COLUMNS_KEY`` — and ``compare_column_face`` counts it as one
    face-B difference. ``tests/test_lot_parity_probe.py`` binds this derivation to
    the writer's own for a payload set covering both guards, column by column, and
    compares each refusal's message *and* its covered-column tuple against the
    columns the writer's own ``ValueError`` names.

    Two refusals of ``_position_lot_storage_values`` are deliberately outside what
    this derivation models, and neither is reachable from a stored payload:

    * the ``allow_nan=False`` serialization (``ValueError: Out of range float
      values are not JSON compliant``) — SQLite's ``json_valid`` is 0 for a
      payload containing ``NaN``/``Infinity``, so no such row can be in
      ``fields_json`` for the probe to read;
    * the ``TypeError`` for a record that is not a ``PositionLotRecord`` — the
      probe holds a payload, not a record.

    A payload that reaches storage therefore cannot be refused by either, which is
    the sense in which "the writer refuses it" is modelled completely here.
    """
    expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
    if expiration_ms is None and fields.get("expiration_ymd") not in (None, ""):
        expiration_ms = _position_lot_contract_scalars(
            {**fields, "contract_key": {"expiration_ymd": fields["expiration_ymd"]}}
        )[0]
    source_event = fields.get("open_event_id") or fields.get("source_event_id")
    source_event_id = str(source_event) if source_event else None
    contract_key = _contract_key(fields)
    account = str(contract_key.get("account") or fields.get("account") or "").strip()
    writer_error, writer_columns = _writer_refusal(fields=fields, account=account)
    return {
        "account": account or None,
        "expiration": int(expiration_ms) if expiration_ms is not None else None,
        "strike": float(strike) if strike is not None else None,
        "multiplier": float(multiplier) if multiplier is not None else None,
        "source_event_id": source_event_id,
        WRITER_RAISES_KEY: writer_error,
        WRITER_RAISES_COLUMNS_KEY: writer_columns,
    }


def compare_column_face(
    *,
    stored_columns: dict[str, Any],
    derived_columns: dict[str, Any],
) -> list[dict[str, Any]]:
    """Face B: stored column value vs the value re-derived from the stored payload.

    A refusal is **one item** in the returned list — the columns its message names
    are skipped, not compared against a derivation that does not exist — and the
    item carries the whole ``WRITER_RAISES_COLUMNS_KEY`` tuple so the per-column
    histogram does not have to read the refusal's wording to know which columns it
    covers. Counting is therefore two different things on purpose: the items are
    one per root cause, and ``run_lot_parity_probe``'s ``by_column`` is one per
    column.
    """
    writer_error = str(derived_columns.get(WRITER_RAISES_KEY) or "")
    refused_columns = tuple(derived_columns.get(WRITER_RAISES_COLUMNS_KEY) or ())
    differences: list[dict[str, Any]] = []
    retired = stored_columns.get("retired_columns", [])
    if retired and (retired != ["expiration"] or "expiration" in stored_columns
                    or not (set(DERIVED_COLUMNS) - {"expiration"}) <= stored_columns.keys()):
        raise ValueError("invalid retired-column evidence")
    for column in DERIVED_COLUMNS:
        if column in retired:
            continue
        if writer_error and column in refused_columns:
            # One difference per root cause: the writer refuses the whole
            # payload, so the columns its message names are reported once — as
            # the refusal — instead of a second time as a column comparison
            # against a derivation that does not exist. The item still names
            # every column the refusal covers, because the histogram below counts
            # columns: a message naming two of them is two column differences.
            if column == refused_columns[0]:
                differences.append(
                    {
                        "column": column,
                        "stored": stored_columns.get(column),
                        "derived_from_stored_payload": None,
                        "writer_raises": writer_error,
                        WRITER_RAISES_COLUMNS_KEY: list(refused_columns),
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
        identity = str(row["lot_id"] or "").strip()
        counts[identity] = counts.get(identity, 0) + 1
        index.setdefault(identity, row)
    duplicates = sorted(
        (identity, count) for identity, count in counts.items() if count > 1
    )
    return index, duplicates


def resolve_row_source_event_id(row: dict[str, Any]) -> tuple[str | None, str]:
    """The row's own ``source_event_id``, and where it came from.

    The column is the writer's derivation of the payload key
    (``repository_common._position_lot_storage_values``); the payload key is
    ``open_event_id`` since the name converged. The payload is consulted only when
    the column is NULL/empty — a row whose column was never backfilled must not be
    read as "no source event id at all", which would hide a real ledger gap
    behind ①.
    """
    column = str(row["columns"]["source_event_id"] or "").strip()
    if column:
        return column, "column"
    payload = str(row["fields"].get("open_event_id") or "").strip()
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
        retired_columns = ["expiration"] if "expiration" not in _table_columns(conn, "position_lots") else []

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
    for side, rows in (("store", stored_rows), ("projection", projected_rows)):
        empty_count = sum(not str(row["lot_id"] or "").strip() for row in rows)
        if empty_count:
            c_items.append({"status": "empty_lot_id", "side": side, "lot_id": "", "occurrences": empty_count})
    c_empty_count = len(c_items)
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
        | ({""} if c_empty_count else set())
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
    # The guard the four classes exist for. It used to read
    # ``sum(attribution_counts.values()) != len(differing_lot_ids)`` — an
    # identity: ``other_ids`` is ``differing_lot_ids`` minus ``extra_ids``, so the
    # two sides agreed for every store shape, including ones where a whole input
    # (``duplicate_ids``) had been dropped from the union on the way in. The three
    # checks below are the ones that bite when the attribution code is edited: both
    # sides are built from the same expressions, so no store shape can trip them,
    # and an edit that drops an input from the union or appends an id twice does
    # (``uncovered`` names the source, ``repeated`` the id).
    #
    # ``repeated`` is not a restatement of the coverage check: coverage stays
    # intact when an id is appended by two different append points, and that is
    # exactly the double-count the counts must never have. It is vacuous for the
    # two append points as they stand (their id sets are disjoint by construction)
    # — it guards the append points, not the store.
    attributed_ids = [str(item["lot_id"]) for item in attribution_items]
    attributed_id_set = set(attributed_ids)
    uncovered = {
        source: sorted(ids - attributed_id_set)
        for source, ids in (
            ("extra_in_store", set(extra_ids)),
            ("missing_in_store", set(missing_ids)),
            ("a_or_b_difference", differing_aligned_ids),
            ("duplicate_identity", duplicate_ids),
        )
    }
    uncovered = {source: ids for source, ids in uncovered.items() if ids}
    repeated = sorted(
        lot_id for lot_id in attributed_id_set if attributed_ids.count(lot_id) > 1
    )
    if uncovered or repeated or attributed_id_set != differing_lot_ids:
        raise AssertionError(
            "lot parity probe C attribution is not a partition: "
            f"{len(attributed_id_set)} attributed lot ids for "
            f"{len(differing_lot_ids)} differing lot ids; "
            f"unattributed={uncovered}; in two classes={repeated}"
        )

    # The histogram counts **columns**, not root causes: a refusal whose message
    # names two columns is one face-B difference (one item) but one entry against
    # each of those two columns, so ``sum(by_column.values())`` can exceed
    # ``difference_count``. Reading the histogram as "how many differences" is the
    # mistake this comment exists to prevent; the counts that answer that question
    # are ``difference_count`` and ``differing_lot_count``.
    b_by_column = {column: 0 for column in DERIVED_COLUMNS}
    for item in b_items:
        covered = item.get(WRITER_RAISES_COLUMNS_KEY) or (item["column"],)
        for column in covered:
            b_by_column[column] += 1

    a_difference_count = a_key_set_difference_lot_count + a_value_difference_count
    b_difference_count = len(b_items)
    # ``count_mismatch`` is a restatement of the two counts above — a row count
    # can only differ because an id is extra, missing, or duplicated — so
    # counting it as a third difference made one differing row print
    # ``c_rows=2``. It stays in the report as the boolean comparator-spec §5
    # names; it is not a third difference.
    c_difference_count = c_set_difference_count + c_duplicate_count + c_empty_count
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
        "derived_columns": [name for name in DERIVED_COLUMNS if name not in retired_columns],
        "retired_columns": retired_columns,
        "green_criterion": GREEN_CRITERION,
        "notes": [
            "face A is raw and strict within one payload shape; v3.5-flat versus v2-nested compares their shared business facts",
            "face B compares each stored derived column against the value re-derived from the stored payload",
            "updated_at_ms is excluded from every face: it is a wall clock, not a fact",
            "green is exactly this: " + GREEN_CRITERION
            + " (plan.md slice 2 prerequisites); comparator-spec.md §7's allow-list is a tier-2 concept this report does not implement",
            "replay diagnostics are counted and are not part of 'green'",
            "c_attribution classes ①②③④ are a partition of c_attribution.differing_lot_count",
            "c_attribution class 'other' is not a row-set gap: it is every differing lot that is not ①②③",
            "c_attribution reads a row's source_event_id from its column first, then its payload key",
            "duplicate identities are a face-C fact; faces A and B compare the first row of each identity in the read order",
            "the read-only connection takes one snapshot (BEGIN), so a concurrent commit cannot land between the two reads",
            "the connection is mode=ro, or mode=ro&immutable=1 when mode=ro could not open a store that has no -wal/-shm sidecar (a filename test, not a test for writers; a -journal is not part of it)",
            "b_columns.by_column counts columns, not root causes: a refusal naming two columns is one item and two column entries, so the histogram can sum to more than b_columns.difference_count",
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
                "empty_identity_count": c_empty_count,
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
        help=(
            "SQLite store path; opened read-only (mode=ro, or immutable=1 for a "
            "copy with no -wal/-shm sidecar)"
        ),
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
    "WRITER_RAISES_COLUMNS_KEY",
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
