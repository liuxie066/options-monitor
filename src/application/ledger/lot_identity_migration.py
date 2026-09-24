"""D1–D4 lot-identity migration, read-only half: ``inventory`` / ``verify``.

R2 retired the destructive half — ``apply``, its ``--dry-run`` preview, the D1/D2
table rebuild, the D3/D4 payload rewrite, the ``wheel_events.stock_lot_id``
rename, and the statement-level registry gate that deferred them. The one-off
production window they existed for is complete (``CHANGELOG.md`` 3.6.5,
2026-09-22), ``LOT_IDENTITY_WINDOW_ENABLEMENT`` is gone, and the removed code is
recoverable from the released 3.6.x tags if a restored pre-window backup ever
has to be migrated again.

What is left answers the two questions a store still raises: what a D1–D4
migration would have to do to it (``inventory``), and whether the payload
classification behind that answer holds up against a fresh replay (``verify``).
Neither writes, and neither needs a manifest.

§13.2 row 8 fixes the conventions: this parent group reuses the *conventions* of
``om option-positions projection-migration`` (a read-only ``inventory``) but
**not** its subcommand names, because ``inventory`` / ``verify`` / ``apply``
already mean checkpoint/tail work there and ``activate`` / ``deactivate`` are
live in production.

The D1–D4 definitions live in §9.4; the execution design in §12; the rulings in
§9.5 (M1–M6). The assumptions below did not survive contact with the code and
are corrected here rather than silently implemented around:

1. **A dropped payload key is not automatically a defect.** §13.3 slice 3 says
   "凡 D3 计划丢弃的键在旧 payload 中非空即判 fail". The target shape
   (`PositionLot.to_dict()`) re-expresses the whole option vocabulary —
   ``broker``/``account``/``symbol``/``option_type`` move into ``contract_key``,
   ``side`` into ``position_side``, ``strike``/``expiration`` into
   ``contract_key``, ``premium`` into ``premium_open``. A blanket
   non-empty-implies-fail rule would report a dozen blocking keys on every real
   store, so ``verify`` would be permanently red and the documented go/no-go
   gate (§13.3 "``verify`` 是只读 dry-run，作为窗口 go/no-go 依据") would be
   void. ``verify`` therefore classifies each non-empty dropped key by whether
   its value survives — carried into the row's target shape, reconstructible
   from ``trade_events``, or lost — and fails only on ``lost``. A
   *reconstructible* verdict that rests on the event layer is measured there
   rather than declared, so a home that turns out empty reports ``lost``
   instead of certifying a loss (``EVENT_LAYER_MEASURED_DROPPED_KEYS``). The negative
   case §13.4 row 3 asks for (structured field empty, fact only in ``note`` KV)
   lands in ``lost``, which is the case this rule was written for.
2. **The checkpoint shortcut is weaker than "shape-only", but stronger than
   nothing.** §13.3 slice 3 asks that ``verify`` not take
   ``projection_verify``'s reuse shortcut. The shortcut is real — ``--mode
   auto`` can answer ``ok: True`` with synthesised ``matched`` items and no
   replay — but its precondition is a verify-state checkpoint whose event
   *and* ``position_lots`` fingerprints match the current store, and
   ``projection_verify`` writes one only after an ``ok`` full replay
   (``:280``). It therefore proves "unchanged since a verified point", not
   "the stored payloads equal a fresh replay": that comparison is
   stored-to-stored and cannot see a store that was already wrong when the
   checkpoint was written. The guarantee below is structural — the reuse entry
   point is never called — rather than a claim that the shortcut would have
   fired.
"""

from __future__ import annotations

from .sqlite_row_codec import FINAL_POSITION_LOT_COLUMNS, position_lots_use_lot_id, wheel_events_use_lot_id

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Mapping

# The ms → ``YYYY-MM-DD`` rule is the domain's, timezone included
# (``EXPIRATION_DATE_TZ``); re-deriving it here is how a migration invents an
# off-by-one-day expiration on the rows it rewrites.
from domain.domain.expiration_dates import expiration_timestamp_to_ymd
from domain.domain.wheel.projection import lot_strategy_metadata_from_trade_events
from src.application.ledger.position_projection_migration import (
    _assert_read_only_persistent_sizes,
    _column_names,
    _events,
    _fail,
    _file_sizes,
    _json_object,
    _load_event_rows,
    _loaded_implementation,
    _manifest,
    _now_iso,
    _read_only_connection,
    _schema_cookie,
    _sha256,
    _store_identity,
    _store_path,
    _table_exists,
    _validate_manifest,
    _write_connection,
)
from src.application.ledger.projection_verify import compare_projection_lots
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import (
    POSITION_LOTS_COLUMN_CLASSIFICATION,
)
# The note KV fallback lives in the infrastructure codec; the repository layer
# reads it through the same helper (``repository_common``'s ``:64``).
from src.infrastructure.feishu_bitable import parse_note_kv


INVENTORY_SCHEMA = "lot_identity_migration_inventory.v1"
VERIFY_SCHEMA = "lot_identity_migration_verify.v1"

#: The D3 target shape: exactly the keys ``PositionLot.to_dict()`` emits
#: (``domain/domain/ledger/lots.py``). Pinned to the domain object by
#: ``tests/test_lot_identity_migration.py`` so drift in ``to_dict`` breaks that
#: test instead of silently mis-classifying a migration.
LOT_SHAPE_KEYS_COMMON = frozenset(
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
LOT_SHAPE_KEYS_STOCK_EXTRA = frozenset(
    {"shares_opened", "shares_open", "shares_closed", "cost_basis_total"}
)

#: D1's three contract scalars and the payload key each is derived from. The
#: §13.5 R6 carrier distribution is reported against these.
CONTRACT_SCALARS = ("expiration", "strike", "multiplier")

#: Dropped keys whose value D3 carries into the row's own target shape. The
#: option vocabulary moves wholesale: ``broker``/``account``/``symbol``/
#: ``option_type``/``strike``/``expiration`` into ``contract_key``, ``side``
#: into ``position_side``, ``premium`` into ``premium_open``. ``position_id`` is
#: the retired display id superseded by the derived ``position_key``.
CARRIED_DROPPED_KEYS = {
    "broker": "contract_key.broker",
    "account": "contract_key.account",
    "symbol": "contract_key.underlying_symbol",
    "option_type": "contract_key.option_type",
    "side": "position_side",
    "contracts": "contracts_opened",
    "contracts_open": "contracts_open",
    "contracts_closed": "contracts_closed",
    "currency": "currency",
    "status": "status",
    "opened_at": "opened_at_ms",
    "strike": "contract_key.strike",
    "expiration": "contract_key.expiration_ymd",
    "expiration_ymd": "contract_key.expiration_ymd",
    "premium": "premium_open",
    "position_id": "position_key",
    "last_close_event_id": "close_event_ids / last_event_id",
    # Not a target payload key, but a surviving column on the rebuilt table.
    "source_event_id": "position_lots.source_event_id (column)",
}

#: Dropped keys whose carrier exists for one asset type only, keyed by the same
#: ``asset_type`` value the shape oracle dispatches on. ``quantity_unit`` is the
#: stock branch's unit: ``PositionLot.to_dict()`` emits ``shares_*`` for
#: ``asset_type == "stock"`` and nothing for an option lot, so on an option row
#: this claim would name a carrier that does not exist — a factual error that
#: reads as "already has a home" while the quantity unit disappears with D3.
#: A non-stock row therefore declares no carrier and the key lands in ``lost``,
#: which is the module's verdict for any fact nobody has vouched for.
CARRIED_DROPPED_KEYS_BY_ASSET_TYPE = {
    "quantity_unit": {"stock": "asset_type + shares_*"},
}

#: Dropped keys that are not in the row but are derivable from the ledger's
#: source of truth (``trade_events``) or from the row's own carried values.
RECONSTRUCTIBLE_DROPPED_KEYS = {
    "cash_secured_amount": "strike * multiplier * contracts, all of which are carried",
    "underlying_share_locked": "contracts * multiplier for a short call",
    "last_action_at": "latest trade_event for the lot; trade_events is the source of truth",
    "event_source_type": "the open event's source_type",
    "event_source_name": "the open event's source_name",
    "strategy_snapshot": "the open event payload's strategy metadata",
    # The rest of ``POSITION_LOT_STRATEGY_PATCH_FIELDS``. ``publisher`` writes
    # that whole family in one pass off the same open-event payload
    # (``strategy_metadata_fields_from_payload``), so these share
    # ``strategy_snapshot``'s home rather than being a second, unrelated fact.
    # They are not decoration: leaving them unmapped would report
    # ``no_declared_carrier`` — and a blocking key — on the project's principal
    # strategy, i.e. a permanently red gate on the stores the window targets.
    # Whether this declared home actually holds the family is a property of the
    # store, and it is settled per row by ``EVENT_LAYER_MEASURED_DROPPED_KEYS``
    # below rather than asserted here.
    "strategy": "the open event payload's strategy metadata",
    "leg_role": "the open event payload's strategy metadata",
    "strategy_group_id": "the open event payload's strategy metadata",
    "source_stock_lot_id": "the open event payload's strategy metadata",
    "source_wheel_branch_id": "the open event payload's strategy metadata",
    "yield_enhancement_mode": "the open or adjust event payload's legacy strategy metadata",
    # The close patch (``publisher._close_fields``) writes each of these straight
    # off the closing trade event — ``event.event_id``/``event.price``/
    # ``event.event_time_ms`` and the event payload's own ``close_type`` and
    # ``close_reason`` — so the closing event is their source of truth, exactly
    # as it is for ``last_action_at``.
    "close_type": "the closing trade_event: its payload's close_type",
    "close_reason": "the closing trade_event: its payload's close_reason",
    "close_price": "the closing trade_event's price",
    "closed_at": "the closing trade_event's event_time_ms",
    "auto_close_exp_src": "the closing trade_event payload's auto_close_exp_src",
    "auto_close_grace_days": "the closing trade_event payload's auto_close_grace_days",
}

#: The entries of ``RECONSTRUCTIBLE_DROPPED_KEYS`` whose verdict this module
#: **measures** rather than looks up: the whole strategy family, whose declared
#: carrier is the open event's payload. A table can say where a fact's home is;
#: whether that home holds it is a property of the store, and for this family it
#: is decided by whoever wrote the open event — an import event that never
#: carried the family leaves the fact with no carrier at all once ``fields_json``
#: stops carrying it. Reporting that as ``reconstructible`` is what kept the
#: go/no-go gate green on a loss nothing here can repair. The remaining entries
#: stay declarations: their carriers are the row's own carried values, or a
#: closing event this surface does not replay.
#: Pinned against ``POSITION_LOT_STRATEGY_PATCH_FIELDS`` by the test module.
EVENT_LAYER_MEASURED_DROPPED_KEYS = frozenset(
    {
        "strategy",
        "leg_role",
        "strategy_group_id",
        "source_stock_lot_id",
        "source_wheel_branch_id",
        "strategy_snapshot",
        "yield_enhancement_mode",
    }
)

#: ``note`` KV vocabulary: every key the codebase writes into or reads out of a
#: note, and where its fact lives besides the note. A note ``k=v`` whose fact is
#: *not* in the row's own structured fields is the §13.5 R6 blocker: ``note`` is
#: the fallback source, so dropping the note drops the only copy.
NOTE_KV_DISPOSITIONS = {
    # ``effective_expiration``/``_position_lot_contract_scalars`` read these
    # three as fallbacks, so each maps onto the payload key it falls back from.
    "exp": ("structured", "expiration"),
    "strike": ("structured", "strike"),
    "multiplier": ("structured", "multiplier"),
    # Read-model fallbacks (``read_model.py``'s ``:142``-``:150``).
    "option_type": ("structured", "option_type"),
    "side": ("structured", "side"),
    "status": ("structured", "status"),
    "premium_per_share": ("structured", "premium"),
    # Provenance written by the publisher's ``_base_fields_for_lot``; the same
    # facts are carried by ``open_event_id`` and by the open event itself.
    "source": ("external", "the open event's source_name"),
    "event_id": ("external", "open_event_id"),
    "order_id": ("external", "the open event payload's order_id"),
    "multiplier_source": ("external", "the open event payload's multiplier_source"),
    # Written by the expire auto-close patch. The close facts survive in
    # ``status``/``close_event_ids``/``last_event_id`` and in the close event.
    "auto_close_at": ("external", "the closing trade_event's trade_time_ms"),
    "auto_close_reason": ("external", "the closing trade_event's close_reason"),
    "close_reason": ("external", "the closing trade_event's close_reason"),
    "auto_close_grace_days": ("external", "the closing trade_event's grace_days"),
    "auto_close_exp_src": ("external", "the closing trade_event's expiration source"),
}

#: ``structured`` note keys whose fact also survives in a table column that the
#: rebuild keeps, so an empty *payload* field is not by itself the loss the
#: §13.5 R6 rule is looking for. This set is now empty: the note fallback that
#: used to fill the ``multiplier``/``strike`` columns was retired
#: (``repository_common._position_lot_contract_scalars`` reads the payload key
#: and ``contract_key`` only), so a note-only scalar no longer survives the
#: rebuild and is a genuine R6 blocker. ``exp`` is absent for the same reason:
#: its column is the one D1 drops.
NOTE_KV_SURVIVING_COLUMNS: dict[str, str] = {}

#: Why a note that blocks nothing is not reported as ``carried``: D3 drops the
#: ``note`` key itself, so no note text reaches the target shape. What survives
#: is each fact via its own home — the row's payload field or surviving column
#: for ``structured`` keys, the event that wrote it for ``external`` keys.
_NOTE_KV_RECONSTRUCTION = (
    "the trade_events that wrote the note (open/close/adjust); D3 drops the note key itself"
)

#: The destructive half of the recipe is deferred while this build's live SQL
#: still names a retired column: the rebuilt shape would be one the build that
#: keeps running cannot fully read back. §13.5 R4 / §9.5 M6 step 3, sequenced
#: by the window's R1/R2 split.

#: Dropped keys the D3 traversal must write somewhere, and where. Every entry is
#: a path into the target shape, and each path is exactly the carrier string
#: ``CARRIED_DROPPED_KEYS`` already declares for that key — the rewrite
#: *executes* the vocabulary rather than restating it, so a change to one that
#: is not made to the other fails ``test_the_rewrite_executes_only_declared_carriers``.
#: Keys whose declared carrier is not a path are deliberately absent: their fact
#: lives in a column the rebuild keeps (``source_event_id``), in the ledger's own
#: events (``last_close_event_id``), in the derived ``position_key`` that
#: supersedes the retired ``position_id``, or in the target's own
#: ``asset_type``/``shares_*`` emission (``quantity_unit``). ``contracts_open``/
#: ``contracts_closed``/``currency``/``status`` are target keys under the very
#: name they are dropped by, so the key filter already carries them.
CARRIER_TARGETS = {
    "broker": ("contract_key", "broker"),
    "account": ("contract_key", "account"),
    "symbol": ("contract_key", "underlying_symbol"),
    "option_type": ("contract_key", "option_type"),
    "strike": ("contract_key", "strike"),
    "expiration_ymd": ("contract_key", "expiration_ymd"),
    "expiration": ("contract_key", "expiration_ymd"),
    "side": ("position_side",),
    "contracts": ("contracts_opened",),
    "premium": ("premium_open",),
    "opened_at": ("opened_at_ms",),
}

#: The two flat keys that both describe D1's ``contract_key.expiration_ymd``.
#: They are classified together because they are the same fact in the ms and the
#: ``YYYY-MM-DD`` vocabulary, and the rewrite has to know which copy to trust.
_EXPIRATION_CARRIER_KEYS = ("expiration", "expiration_ymd")

#: The inventory keys that move when the *writer* open path adds the identity
#: carrier to a store that predates it (``repository_projection_schema``'s
#: ``_ensure_position_projection_schema``, reached from ``repository_core``'s
#: ``_init_db`` on an ordinary ``SQLiteOptionPositionsRepository(path)``). This
#: drift is this migration's own precondition being met rather than the store
#: diverging, so ``apply`` names it instead of leaving the operator to guess.


#: The statement-level ledger of live SQL that names the retired columns:
#: generated by ``scripts/retired_column_scan.py --write`` (line scan + window
#: scan — DDL column lists span lines) and pinned against the tree by the
#: repo-wide guardrail and its quality test. ``apply`` reads it as the build's
#: own answer to "can the running build read the rebuilt shape back?".


def _lot_shape_keys(asset_type: Any) -> frozenset[str]:
    """The keys ``PositionLot.to_dict()`` emits for this row.

    The comparison is *exactly* the oracle's (``lots.py``: ``if self.asset_type
    == "stock"``), with no normalizing. A looser test here would predict a stock
    shape for a row the rewrite will treat as an option, and the ``shares_*``
    keys it claimed to carry would then vanish from the gate's report.
    """

    keys = LOT_SHAPE_KEYS_COMMON
    if asset_type == "stock":
        keys = keys | LOT_SHAPE_KEYS_STOCK_EXTRA
    return keys


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _expiration_carrier(fields: Mapping[str, Any]) -> str | None:
    """The ``YYYY-MM-DD`` that carries this row's expiration fact, or None.

    D1 retires the ms mirror, so the only home D3 has for the fact is
    ``contract_key.expiration_ymd``. The writer publishes both keys off the same
    contract key (``publisher._apply_lot_state_fields``:
    ``expiration_ymd`` then ``int(parse_exp_to_ms(...))``), so the ymd is
    normally right there and is the authority; converting the ms is the fallback
    for a row where the mirror is the only copy. Both the classifier and the
    rewrite read this one helper, so the gate can never declare "carried" for a
    value the rewrite is unable to place — and the reverse.
    """

    ymd = fields.get("expiration_ymd")
    if _non_empty(ymd):
        return str(ymd).strip()
    if _non_empty(fields.get("expiration")):
        converted = expiration_timestamp_to_ymd(fields.get("expiration"))
        return converted or None
    return None


_NOTE_KV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _segment_pairs(segment: str) -> list[tuple[str, str]] | None:
    """Tokenize one ``;``/``,``-delimited segment, or None if it is not all KV.

    Two writers disagree about the separator. ``merge_note`` joins pairs with
    ``;``, while ``_base_fields_for_lot`` writes them space-separated
    (``"source=test event_id=deal-1 order_id= multiplier_source="``). The
    codebase's own reader, ``parse_note_kv``, splits only on ``;``/``,``, so it
    reads a publisher note as the single pair ``("source", "test event_id=…")``
    — the rest of the publisher's keys are invisible to it.

    Classification cannot inherit that blind spot: the question is whether a
    *fact* survives D3, not whether the current reader happens to see it. So a
    segment is accepted as KV only when every whitespace-separated token is
    ``key=``/``key=value``, which tokenizes the publisher's format without
    turning prose that merely contains an ``=`` into a phantom pair. Anything
    else stays prose, and prose has no other home — the conservative direction
    for a go/no-go gate.
    """

    tokens = segment.split()
    pairs: list[tuple[str, str]] = []
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator or not _NOTE_KV_KEY.match(key) or "=" in value:
            return None
        pairs.append((key, value))
    return pairs or None


def _note_parts(note: Any) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a stored note into free prose and ``key=value`` pairs."""

    prose: list[str] = []
    pairs: list[tuple[str, str]] = []
    for raw in str(note or "").replace(",", ";").split(";"):
        segment = raw.strip()
        if not segment:
            continue
        segment_pairs = _segment_pairs(segment)
        if segment_pairs is None:
            prose.append(segment)
            continue
        pairs.extend(segment_pairs)
    return prose, pairs


#: Where each ``NOTE_KV_DISPOSITIONS`` ``structured`` target lives in the
#: converged payload (``write-side-definition.md`` §2). The table's own names are
#: the pre-switch flat keys, which the row no longer carries; the retired sibling
#: stays readable for a row written before the shape switch.
#: ``(container, key)`` pairs, most-converged first: ``"contract"`` is the nested
#: ``contract_key``, ``"payload"`` the top level. The last entry of each tuple is
#: the retired flat key, so a row written before the shape switch still reads.
_CONVERGED_STRUCTURED_TARGETS: dict[str, tuple[tuple[str, str], ...]] = {
    "expiration": (("contract", "expiration_ymd"), ("payload", "expiration")),
    "strike": (("contract", "strike"), ("payload", "strike")),
    "option_type": (("contract", "option_type"), ("payload", "option_type")),
    "side": (("payload", "position_side"), ("payload", "side")),
    "status": (("payload", "status"),),
    "premium": (("payload", "premium_open"), ("payload", "premium")),
}


def _structured_value(fields: Mapping[str, Any], target: str) -> Any:
    """One ``structured`` target: the converged path first, the flat key after."""
    contract_key = fields.get("contract_key")
    contract_key = contract_key if isinstance(contract_key, Mapping) else {}
    candidates = _CONVERGED_STRUCTURED_TARGETS.get(
        target, (("payload", target),)
    )
    for container, key in candidates:
        value = (
            contract_key.get(key)
            if container == "contract"
            else fields.get(key)
        )
        if _non_empty(value):
            return value
    return None


def _note_disposition(
    note: Any,
    fields: Mapping[str, Any],
    surviving_columns: Mapping[str, Any],
) -> str | None:
    """Why a non-empty ``note`` would lose information, or None if it would not.

    D3 drops the whole ``note`` key, so it is only lossless when every fact the
    note carries is still reachable afterwards. ``NOTE_KV_DISPOSITIONS``
    declares where each key's fact lives besides the note:

    * ``structured`` — the row's own field is the primary source and ``note``
      is only its fallback, so the pair is redundant *unless* that field is
      empty. That is §13.5 R6 and the §13.4 negative case: the fact exists
      solely in the note. "The row's own field" is read as the payload field
      *or* a surviving table column (``NOTE_KV_SURVIVING_COLUMNS``), because
      the derived column is filled from this same note fallback and outlives
      the rebuild.
    * ``external`` — the fact is also written by the event that produced it, so
      dropping the note loses only a convenience copy. This is a declared
      home, not a checked one: nothing on this path can see the event, so it
      is reported as ``reconstructible`` rather than as carried into the row.

    Free prose has no other home at all, and an undeclared key is a fact whose
    only copy nobody has vouched for.
    """

    prose, pairs = _note_parts(note)
    if prose:
        return "note_prose_only_in_note"
    for key, value in pairs:
        if not value:
            continue
        disposition = NOTE_KV_DISPOSITIONS.get(key)
        if disposition is None:
            return f"note_kv_unmapped:{key}"
        kind, target = disposition
        if kind != "structured" or _non_empty(
            _structured_value(fields, target)
        ):
            continue
        column = NOTE_KV_SURVIVING_COLUMNS.get(key)
        if column is None or not _non_empty(surviving_columns.get(column)):
            return f"note_kv_only:{key}"
    return None


def _event_layer_strategy_families(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """The family each lot can still be read back out of the event layer.

    Deliberately the read model's own reconstruction
    (``read_model.attach_event_strategy_metadata`` ->
    ``wheel.lot_strategy_metadata_from_trade_events``) rather than a second
    derivation: the gate has to measure the claim with the reader that is
    supposed to honour it, or it certifies a home nobody can actually read.
    Empty when the store has no ``trade_events`` table at all, which is the
    honest answer — a payload-only store has no event layer to rebuild from.
    """

    if not _table_exists(conn, "trade_events"):
        return {}
    return lot_strategy_metadata_from_trade_events(_events(_load_event_rows(conn)))


def _family_for_row(
    row: Mapping[str, Any],
    families: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """The measured family for one scanned lot row.

    Keyed the way the reader keys it (the event's lot id, which the import path
    sets to the lot's ``record_id``); ``lot_id`` is probed second because D2
    backfills it, so a store on either side of that step resolves the same lot.
    """

    for candidate in (row.get("record_id"), row.get("lot_id")):
        text = str(candidate or "").strip()
        if text and text in families:
            return families[text]
    return {}


def _drop_disposition(
    key: str,
    value: Any,
    fields: Mapping[str, Any],
    surviving_columns: Mapping[str, Any],
    event_metadata: Mapping[str, Any],
) -> tuple[str, str]:
    """Classify one non-empty dropped key as carried / reconstructible / lost."""

    path = CARRIER_TARGETS.get(key)
    carried = _carried_value(key, fields) if path else None
    if path and carried is not None:
        existing: Any = fields
        for part in path:
            if not isinstance(existing, Mapping):
                if _non_empty(existing):
                    return "lost", "carrier_value_conflict"
                break
            existing = existing.get(part)
        else:
            if _non_empty(existing) and json.dumps(existing, sort_keys=True) != json.dumps(carried, sort_keys=True):
                return "lost", "carrier_value_conflict"
    if key == "note":
        reason = _note_disposition(value, fields, surviving_columns)
        if reason:
            return "lost", reason
        return "reconstructible", _NOTE_KV_RECONSTRUCTION
    if key in _EXPIRATION_CARRIER_KEYS:
        # Both flat keys are the same fact, and the carrier is whichever copy
        # the row still has. Without one, ``contract_key.expiration_ymd`` cannot
        # be filled from the row at all — a handful of ms in the payload is not
        # a carrier, and calling it one is how D3 would drop the expiration.
        if _expiration_carrier(fields):
            return "carried", "contract_key.expiration_ymd"
        return "lost", "no_declared_carrier"
    carrier = CARRIED_DROPPED_KEYS.get(key)
    if carrier is None:
        by_asset_type = CARRIED_DROPPED_KEYS_BY_ASSET_TYPE.get(key)
        if by_asset_type:
            # Same default as the shape dispatch: anything that is not the
            # oracle's exact ``"stock"`` takes the option branch.
            carrier = by_asset_type.get(str(fields.get("asset_type") or "option"))
    if carrier:
        return "carried", carrier
    derivation = RECONSTRUCTIBLE_DROPPED_KEYS.get(key)
    if key in EVENT_LAYER_MEASURED_DROPPED_KEYS:
        # Measured, not declared: ask this row's event layer whether the family
        # is actually there. An import event that never carried it, or a
        # payload-only row with no open event at all, leaves the fact with no
        # carrier — a loss the gate has to report instead of certifying.
        event_value = event_metadata.get(key)
        if _non_empty(event_value) and json.dumps(event_value, sort_keys=True) != json.dumps(value, sort_keys=True):
            return "lost", "event_layer_carrier_conflict"
        if _non_empty(event_value):
            return "reconstructible", derivation or "the open event payload's strategy metadata"
        return "lost", "event_layer_carrier_absent"
    if derivation:
        return "reconstructible", derivation
    return "lost", "no_declared_carrier"


def _parse_fields(raw: Any) -> dict[str, Any] | None:
    return _json_object(raw)


def _scan_lot_payloads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read every lot row through a column probe so an older store still reads.

    Same idiom as §13.2's existing probes (``position_projection_migration``
    ``:271``-``:273``): a store that predates the identity carrier must still be
    readable, so a missing column is selected as ``NULL AS <name>`` rather than
    crashing the inventory that is supposed to report it.
    """

    if not _table_exists(conn, "position_lots"):
        return []
    columns = set(_column_names(conn, "position_lots"))
    identity = "lot_id" if "lot_id" in columns else "NULL AS lot_id"
    # ``record_id`` is what D2's rebuild retires, and this module is the surface
    # that reports readiness for that shape — so it probes its own ordering key
    # as well, rather than crashing the inventory that is supposed to name the
    # missing column. ``lot_id`` becomes the key at that point.
    if "record_id" in columns:
        key_column: str | None = "record_id"
    elif "lot_id" in columns:
        key_column = "lot_id"
    else:
        key_column = None
    selected = [
        "record_id" if "record_id" in columns else "NULL AS record_id",
        identity,
        "fields_json",
    ]
    for name in (
        "account",
        "source_event_id",
        "expiration",
        "strike",
        "multiplier",
    ):
        selected.append(name if name in columns else f"NULL AS {name}")
    order = f" ORDER BY {key_column}" if key_column else ""
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        f"SELECT {','.join(selected)} FROM position_lots{order}"
    ):
        rows.append(
            {
                "record_id": str(row["record_id"] or ""),
                "lot_id": (
                    str(row["lot_id"]).strip()
                    if row["lot_id"] not in (None, "")
                    else None
                ),
                "fields_json": row["fields_json"],
                "fields": _parse_fields(row["fields_json"]),
                "account": row["account"],
                "source_event_id": row["source_event_id"],
                "expiration": row["expiration"],
                "strike": row["strike"],
                "multiplier": row["multiplier"],
            }
        )
    return rows


def _scalar_carrier_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """§13.5 R6: where each D1 contract scalar's value actually comes from.

    ``structured`` — the row's own payload field carries it.
    ``note_kv`` — only the ``note`` KV fallback carries it (§13.5 R6's blocker).
    ``column_only`` — neither payload nor note carries it, but the derived
    SQL column has a value, so a reader that only looks at the column still
    sees one.
    ``absent`` — no value anywhere.
    """

    distribution: dict[str, Any] = {}
    for name in CONTRACT_SCALARS:
        buckets = {"structured": 0, "note_kv": 0, "column_only": 0, "absent": 0}
        note_keys = {
            "expiration": "exp",
            "strike": "strike",
            "multiplier": "multiplier",
        }
        column_present = 0
        for row in rows:
            fields = row["fields"]
            if row["fields"] is None:
                fields = {}
            if _non_empty(row[name]):
                column_present += 1
            if _non_empty(_structured_value(fields, name)):
                buckets["structured"] += 1
            elif _non_empty(parse_note_kv(fields.get("note") or "", note_keys[name])):
                buckets["note_kv"] += 1
            elif _non_empty(row[name]):
                buckets["column_only"] += 1
            else:
                buckets["absent"] += 1
        distribution[name] = {
            **buckets,
            "rows": len(rows),
            "column_present_rows": column_present,
            "note_kv_keys": note_keys[name],
        }
    return distribution


def _pending_work(
    rows: list[dict[str, Any]],
    columns: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """D1/D2 pending column work and D3/D4 pending payload work, by row count.

    ``columns`` is the table's real column set (``PRAGMA table_info``), passed in
    rather than inferred from the rows: deriving it from ``rows[0]`` made an
    empty ``position_lots`` report every column as missing, contradicting the
    ``column_contract`` computed beside it in the same payload and inventing a
    ``lot_id_column_missing`` reason — and an ``ensure_lot_id_column: applied``
    step — on a store that has the column.
    """

    column_set = set(columns)
    expiration_non_null = sum(1 for row in rows if _non_empty(row["expiration"]))
    record_id_non_null = sum(1 for row in rows if _non_empty(row["record_id"]))
    lot_id_null = sum(1 for row in rows if row["lot_id"] is None)

    dropped_keys: dict[str, dict[str, Any]] = {}
    d4_rows = 0
    for row in rows:
        fields = row["fields"]
        if fields is None:
            continue
        if _non_empty(fields.get("position_id")):
            d4_rows += 1
        target = _lot_shape_keys(fields.get("asset_type"))
        for key in fields:
            if key in target or not _non_empty(fields[key]):
                continue
            bucket = dropped_keys.setdefault(
                key,
                {"rows_non_empty": 0, "asset_types": {}, "sample_lot_ids": []},
            )
            bucket["rows_non_empty"] += 1
            asset_type = str(fields.get("asset_type") or "option")
            bucket["asset_types"][asset_type] = (
                int(bucket["asset_types"].get(asset_type) or 0) + 1
            )
            if len(bucket["sample_lot_ids"]) < 5:
                bucket["sample_lot_ids"].append(row["record_id"])

    return {
        "d1_expiration_column": {
            "column_present": "expiration" in column_set,
            "rows_non_null": expiration_non_null,
        },
        "d2_record_id_column": {
            "column_present": "record_id" in column_set,
            "rows_non_null": record_id_non_null,
        },
        "d2_lot_id_column": {
            "column_present": "lot_id" in column_set,
            "rows_null": lot_id_null,
        },
        "d3_fields_json_keys_outside_lot_shape": {
            "rows_with_dropped_keys": sum(
                1
                for row in rows
                if row["fields"]
                and any(
                    key not in _lot_shape_keys(row["fields"].get("asset_type"))
                    for key in row["fields"]
                )
            ),
            "keys": dropped_keys,
        },
        "d4_position_id_rows": d4_rows,
        "rows": len(rows),
    }


def _classify_dropped_keys(
    rows: list[dict[str, Any]],
    event_families: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Per-key disposition of every non-empty dropped payload key.

    ``event_families`` is the measured event layer (see
    ``_event_layer_strategy_families``); the keys in
    ``EVENT_LAYER_MEASURED_DROPPED_KEYS`` are answered per row from it, so the
    same key can land in ``reconstructible`` for one lot and ``lost`` for
    another — which is the truth about a store where only some opens carried the
    family.
    """

    buckets: dict[str, dict[str, Any]] = {
        "carried": {},
        "reconstructible": {},
        "lost": {},
    }
    for row in rows:
        fields = row["fields"]
        if fields is None:
            continue
        target = _lot_shape_keys(fields.get("asset_type"))
        event_metadata = _family_for_row(row, event_families)
        for key, value in fields.items():
            if key in target or not _non_empty(value):
                continue
            disposition, reason = _drop_disposition(
                key, value, fields, row, event_metadata
            )
            bucket = buckets[disposition].setdefault(
                key,
                {
                    "rows_non_empty": 0,
                    "disposition": disposition,
                    "reason": reason,
                    "sample_lot_ids": [],
                },
            )
            bucket["rows_non_empty"] += 1
            if len(bucket["sample_lot_ids"]) < 5:
                bucket["sample_lot_ids"].append(row["record_id"])
    return buckets




def _inventory_from_conn(
    path: Path,
    conn: sqlite3.Connection,
    *,
    implementation: str,
) -> dict[str, Any]:
    rows = _scan_lot_payloads(conn)
    rows, asset_type_resolution, replay_match = _resolve_asset_types_from_replay(conn, rows)
    lot_columns = _column_names(conn, "position_lots")
    final_shape = set(lot_columns) == FINAL_POSITION_LOT_COLUMNS and position_lots_use_lot_id(conn)
    expected = set(POSITION_LOTS_COLUMN_CLASSIFICATION) - ({"record_id", "expiration"} if final_shape else set())
    columns = {
        "position_lots": {
            "missing": sorted(expected - set(lot_columns)),
            "unclassified": sorted(set(lot_columns) - expected),
        }
    }
    reasons: list[str] = []
    if not _table_exists(conn, "position_lots"):
        reasons.append("base_tables_missing")
    if any(details["missing"] or details["unclassified"] for details in columns.values()):
        reasons.append("column_contract_open")
    pending = _pending_work(rows, lot_columns)
    carrier = pending["d2_lot_id_column"]
    if not carrier["column_present"]:
        reasons.append("lot_id_column_missing")
    elif carrier["rows_null"]:
        reasons.append("lot_id_backfill_pending")
    if not final_shape and not pending["d2_record_id_column"]["column_present"]:
        # D2's rebuild already ran: the store is past this batch's entry state,
        # and the legacy-id backfill has no source column left to read.
        reasons.append("record_id_column_missing")
    if asset_type_resolution["status"] != "resolved":
        reasons.append("lot_asset_type_unresolved")
    if replay_match["status"] == "mismatch":
        reasons.append("projection_replay_mismatch")

    stable = {
        "store_identity": _store_identity(path),
        "sqlite_schema_cookie": _schema_cookie(conn),
        "loaded_projector_implementation_fingerprint": implementation,
        "counts": {
            "position_lots": len(rows),
            "trade_events": (
                int(
                    conn.execute("SELECT count(*) FROM trade_events").fetchone()[0]
                )
                if _table_exists(conn, "trade_events")
                else 0
            ),
        },
        "wheel_identity": _wheel_identity_inventory(conn),
        "column_contract": columns,
        "pending": pending,
        "asset_type_resolution": asset_type_resolution,
        "projection_replay_match": replay_match,
        "contract_scalar_carriers": _scalar_carrier_distribution(rows),
        "lot_shape_keys": {
            "common": sorted(LOT_SHAPE_KEYS_COMMON),
            "stock_extra": sorted(LOT_SHAPE_KEYS_STOCK_EXTRA),
        },
        "dropped_key_classification": _classify_dropped_keys(
            rows, _event_layer_strategy_families(conn)
        ),
    }
    return {
        **stable,
        "inventory_fingerprint": _sha256(stable),
        "readiness": "ready" if not reasons else "not_ready",
        "readiness_reasons": reasons,
    }


def _resolve_asset_types_from_replay(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Use the canonical event replay to classify legacy payload shapes."""

    try:
        replayed = _fresh_replay_payloads(conn)
    except RuntimeError:
        return (
            rows,
            {"status": "unresolved", "resolved_rows": 0, "blocked_rows": len(rows)},
            {"status": "unavailable", "mismatch_count": 0},
        )

    resolved: list[dict[str, Any]] = []
    blocked: list[str] = []
    for row in rows:
        lot_id = str(row["lot_id"] or row["record_id"] or "").strip()
        fields = row["fields"]
        canonical = replayed.get(lot_id, {}).get("asset_type")
        stored = fields.get("asset_type") if isinstance(fields, Mapping) else None
        if (
            not isinstance(fields, Mapping)
            or canonical not in {"option", "stock"}
            or not (stored is None or stored == "" or stored == canonical)
        ):
            blocked.append(lot_id)
            resolved.append(row)
            continue
        copied = dict(row)
        copied["fields"] = {**fields, "asset_type": canonical}
        resolved.append(copied)
    resolution = {
        "status": "resolved" if not blocked else "unresolved",
        "resolved_rows": len(rows) - len(blocked),
        "blocked_rows": len(blocked),
        "sample_blocked_lot_ids": blocked[:5],
    }
    if blocked:
        return resolved, resolution, {"status": "unavailable", "mismatch_count": 0}

    families = _event_layer_strategy_families(conn)
    comparable = []
    try:
        for original, row in zip(rows, resolved, strict=True):
            lot_id = str(row["lot_id"] or row["record_id"] or "").strip()
            fields = original["fields"] or {}
            if isinstance(fields, Mapping) and isinstance(
                fields.get("contract_key"), Mapping
            ):
                fields = _aligned_lot_payload(
                    row["fields"],
                    row,
                    _family_for_row(row, families),
                )
                if not _non_empty(fields.get("open_event_id")):
                    fields["open_event_id"] = row["source_event_id"]
                contract = fields.setdefault("contract_key", {})
                canonical_contract = replayed[lot_id].get("contract_key", {})
                for key in ("option_type", "expiration_ymd"):
                    if key not in contract and canonical_contract.get(key) in (None, ""):
                        contract[key] = canonical_contract.get(key)
            comparable.append(
                {
                    "record_id": lot_id,
                    "fields": fields,
                }
            )
    except RuntimeError:
        return resolved, resolution, {"status": "unavailable", "mismatch_count": 0}
    comparison = compare_projection_lots(
        projected_lots=[
            {"lot_id": lot_id, "fields": fields}
            for lot_id, fields in replayed.items()
        ],
        current_lots=comparable,
        diagnostics=[],
    )
    mismatches = sum(
        item.get("status") != "matched" for item in comparison["items"]
    )
    return resolved, resolution, {
        "status": "matched" if not mismatches else "mismatch",
        "mismatch_count": mismatches,
    }


def build_lot_identity_migration_inventory(sqlite_path: str | Path) -> dict[str, Any]:
    path = _store_path(sqlite_path)
    implementation, _timing = _loaded_implementation()
    before = _file_sizes(path)
    with _read_only_connection(path) as conn:
        inventory = _inventory_from_conn(path, conn, implementation=implementation)
    after = _file_sizes(path)
    _assert_read_only_persistent_sizes(before, after, operation="inventory")
    return _manifest(
        {
            "schema_version": INVENTORY_SCHEMA,
            "generated_at_utc": _now_iso(),
            "operation": "inventory",
            "read_only": True,
            **inventory,
        }
    )




def verify_lot_identity_migration(sqlite_path: str | Path) -> dict[str, Any]:
    """Read-only content-side verification, with checkpoint reuse forbidden.

    ``projection_verify``'s ``--mode auto`` can answer ``ok: True`` with
    ``mode_used: "checkpoint_reuse"`` and synthesised ``items=[{"status":
    "matched"}…]`` **without replaying anything** (``projection_verify.py``).
    The shortcut's precondition is a verify-state checkpoint file whose
    contract version and event/lot fingerprints match the current store, and
    ``projection_verify`` only writes one after an ``ok`` full replay
    (``:280``), so the guarantee it carries is *"unchanged since the checkpoint
    was taken"* — never *"the stored payloads equal a fresh replay"*. Whatever
    the checkpoint's own provenance, that comparison is stored-to-stored, so it
    cannot see a store that was already wrong when the checkpoint was written.

    This surface therefore never calls it. It replays the projection from
    ``trade_events`` and hands the result to ``compare_projection_lots``, which
    is the same content-side comparison the non-reuse branch of
    ``projection_verify`` performs, and reports ``mode_used: "full_replay"``
    unconditionally. The precondition is not observable from a read-only store
    handle — it lives in the verify-state directory, not in SQLite — so the
    report states the contract rather than pretending to have checked it.
    """

    path = _store_path(sqlite_path)
    implementation, _timing = _loaded_implementation()
    before = _file_sizes(path)
    with _read_only_connection(path) as conn:
        inventory = _inventory_from_conn(path, conn, implementation=implementation)
        rows = _scan_lot_payloads(conn)
        events = (
            _events(_load_event_rows(conn))
            if _table_exists(conn, "trade_events")
            else []
        )
        projection = project_stored_trade_events_to_position_lots(events)
        comparison = compare_projection_lots(
            projected_lots=list(projection.lots),
            current_lots=[
                {
                    "record_id": row["lot_id"] or row["record_id"],
                    "fields": row["fields"] or {},
                }
                for row in rows
            ],
            diagnostics=list(projection.diagnostics),
        )
        checkpoint = _checkpoint_state(conn)
    after = _file_sizes(path)
    _assert_read_only_persistent_sizes(before, after, operation="verification")

    summary = dict(comparison["summary"])
    replay_mismatches = sum(count for key, count in summary.items() if key != "matched")
    # ``compare_projection_lots`` returns a partition: one item per error
    # diagnostic plus one item per compared lot. The two families are counted
    # apart because they mean opposite things. ``projection_error`` is the replay
    # refusing an event it cannot decode (``non_canonical_trade_event_schema``),
    # so that event produced no lot at all and every stored lot then reads as
    # "extra". Counting those as lot mismatches reports a lot-identity verdict on
    # a store the replay never actually read — the state the precedent declines
    # to replay at all (``position_projection_migration._verify_from_conn`` runs
    # the replay only once the projection runtime state is present). The gate
    # stays exactly as closed: both counters feed the reason list, and any reason
    # closes ``ok``. Only the attribution is separated, so an operator can tell
    # "these events predate the canonical payload" from "these lots differ from
    # the replay".
    projection_errors = int(summary.get("projection_error") or 0)
    lot_mismatches = sum(
        int(summary.get(status) or 0)
        for status in ("field_mismatch", "extra_in_position_lots", "missing_in_position_lots")
    )
    classification = inventory["dropped_key_classification"]
    lost_keys = sorted(classification["lost"])
    # Mutually exclusive on purpose: with events the replay could not read, every
    # stored lot reads as ``extra_in_position_lots`` by construction, so those
    # counts are an artifact of the missing events rather than an independent
    # disagreement — reporting them as a lot mismatch is the misattribution this
    # split exists to remove. ``projection`` still carries both counts.
    reasons: list[str] = list(inventory["readiness_reasons"])
    if projection_errors:
        reasons.append("trade_events_not_replayable")
    elif replay_mismatches:
        reasons.append("projection_replay_mismatch")
    if lost_keys:
        reasons.append("dropped_payload_keys_would_lose_facts")

    return _manifest(
        {
            "schema_version": VERIFY_SCHEMA,
            "generated_at_utc": _now_iso(),
            "operation": "verify",
            "read_only": True,
            "store_identity": inventory["store_identity"],
            "inventory_fingerprint": inventory["inventory_fingerprint"],
            # Never "checkpoint_reuse": the replay above always ran.
            "mode_used": "full_replay",
            "checkpoint_reuse_forbidden": True,
            "checkpoint": checkpoint,
            "projection": {
                "ok": replay_mismatches == 0,
                "mismatch_count": replay_mismatches,
                "lot_mismatch_count": lot_mismatches,
                "projection_error_count": projection_errors,
                "summary": summary,
                "mismatch_items": [
                    item
                    for item in comparison["items"]
                    if item.get("status") != "matched"
                ][:10],
                "event_count": len(events),
                "position_lot_count": len(rows),
            },
            "payload_keys": {
                "target_keys_common": sorted(LOT_SHAPE_KEYS_COMMON),
                "target_keys_stock_extra": sorted(LOT_SHAPE_KEYS_STOCK_EXTRA),
                "carried": classification["carried"],
                "reconstructible": classification["reconstructible"],
                "lost": classification["lost"],
            },
            "pending": inventory["pending"],
            "contract_scalar_carriers": inventory["contract_scalar_carriers"],
            "blocking_keys": lost_keys,
            "ok": not reasons,
            "readiness_reasons": reasons,
        }
    )


def _checkpoint_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """What this read-only surface can and cannot say about the reuse shortcut.

    Two different artifacts are named "checkpoint" and they must not be
    conflated: ``projection_verify``'s shortcut reads
    ``<base>/current/projection_verify.checkpoint.json``, while
    ``position_projection_checkpoints`` is the runtime tail checkpoint
    activated by the Phase 3A migration. Only the latter is reachable from a
    store handle, and it is *not* the shortcut's precondition, so this reports
    the runtime table's presence as an observation and states the shortcut's
    real precondition as a contract rather than claiming to have checked it.
    """

    runtime_rows = 0
    if _table_exists(conn, "position_projection_checkpoints"):
        runtime_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM position_projection_checkpoints"
            ).fetchone()[0]
            or 0
        )
    return {
        "contract": "never_reuse",
        "mode_used": "full_replay",
        "shortcut_precondition": (
            "a verify-state checkpoint file whose projection_contract_version, "
            "event_fingerprint and position_lots_fingerprint all match the "
            "current store (projection_verify.py:210-215); it is written only "
            "after an ok full replay (:280)"
        ),
        "shortcut_precondition_observable_read_only": False,
        "runtime_tail_checkpoint_rows": runtime_rows,
    }




def _place_carried(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Write one carried fact at its declared path in the target shape."""

    target = payload
    for part in path[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[path[-1]] = value


def _carried_value(key: str, fields: Mapping[str, Any]) -> Any:
    """The value D3 places at ``CARRIER_TARGETS[key]``.

    ``None`` means "the authoritative copy already filled that target": the ms
    ``expiration`` and the ``expiration_ymd`` beside it are the same fact, and
    the ymd wins (``_expiration_carrier``).
    """

    if key == "expiration" and _non_empty(fields.get("expiration_ymd")):
        return None
    if key in _EXPIRATION_CARRIER_KEYS:
        return _expiration_carrier(fields)
    return fields.get(key)


def _aligned_lot_payload(
    fields: Mapping[str, Any],
    surviving_columns: Mapping[str, Any],
    event_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """D3: the row's payload restricted to the ``PositionLot.to_dict()`` shape.

    Every key outside the target shape is either carried into it (the path
    ``CARRIED_DROPPED_KEYS`` declares, materialized here) or dropped as
    reconstructible from the ledger. A key the classifier calls ``lost`` is not
    dropped at all — the whole ``apply`` aborts, because the alternative is a
    fact that disappears from the store while the gate reported it carried.
    """

    target_keys = _lot_shape_keys(fields.get("asset_type"))
    aligned = deepcopy({key: value for key, value in fields.items() if key in target_keys})
    for key, value in fields.items():
        if key in target_keys or not _non_empty(value):
            continue
        disposition, reason = _drop_disposition(
            key, value, fields, surviving_columns, event_metadata
        )
        if disposition == "lost":
            raise RuntimeError(
                "lot payload rewrite would lose a fact: "
                f"key {key!r} has no carrier ({reason})"
            )
        path = CARRIER_TARGETS.get(key)
        if path is None:
            continue
        carried = _carried_value(key, fields)
        if carried is None:
            continue
        _place_carried(aligned, path, carried)
    return aligned




def _fresh_replay_payloads(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    events = _events(_load_event_rows(conn)) if _table_exists(conn, "trade_events") else []
    projection = project_stored_trade_events_to_position_lots(events)
    errors = [
        item for item in projection.diagnostics
        if str(getattr(item, "severity", "") or "").lower() == "error"
    ]
    if errors:
        raise RuntimeError(f"fresh replay reported {len(errors)} projection error(s)")
    payloads: dict[str, dict[str, Any]] = {}
    for item in projection.lots:
        lot_id = str(item.lot_id or "").strip()
        if not lot_id or lot_id in payloads:
            raise RuntimeError(f"fresh replay produced invalid lot identity: {lot_id!r}")
        payloads[lot_id] = deepcopy(item.fields)
    rows = _scan_lot_payloads(conn)
    stored_ids = [str(row["lot_id"] or row["record_id"] or "").strip() for row in rows]
    if len(set(stored_ids)) != len(stored_ids) or set(stored_ids) != set(payloads):
        raise RuntimeError("fresh replay lot identities differ from position_lots")
    return payloads


# --- D1/D2: one rebuild ------------------------------------------------------

#: A stored ``CREATE TABLE`` is the only place the column declarations actually
#: live, so the rebuilt table's definitions come from there rather than from
#: ``PRAGMA table_info`` — which reports type/NOT NULL/DEFAULT but not CHECK,
#: COLLATE or generated-column clauses, i.e. it silently drops every constraint
#: it does not model.


def _wheel_identity_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "wheel_events"):
        return {"status": "absent", "rows": 0}
    uses_lot_id = wheel_events_use_lot_id(conn)
    rows = [tuple(row) for row in conn.execute("SELECT rowid, * FROM wheel_events ORDER BY event_id")]
    return {
        "status": "lot_id" if uses_lot_id else "stock_lot_id",
        "rows": len(rows),
        "content_fingerprint": _sha256(rows),
    }






__all__ = [
    "CARRIED_DROPPED_KEYS",
    "CARRIER_TARGETS",
    "INVENTORY_SCHEMA",
    "LOT_SHAPE_KEYS_COMMON",
    "LOT_SHAPE_KEYS_STOCK_EXTRA",
    "RECONSTRUCTIBLE_DROPPED_KEYS",
    "VERIFY_SCHEMA",
    "build_lot_identity_migration_inventory",
    "verify_lot_identity_migration",
]
