"""D1–D4 lot-identity migration: gated ``inventory`` / ``verify`` / ``apply``.

§13.2 row 8 fixes the conventions: this batch reuses the *conventions* of
``om option-positions projection-migration`` (read-only ``inventory``, a
required ``--manifest``, ``_add_local_write_flags(..., high_risk=True)`` for
writes) but **not** its subcommand names. ``inventory`` / ``verify`` / ``apply``
are already taken there and mean checkpoint/tail work, and ``activate`` /
``deactivate`` are live in production. So this batch hangs off its own parent
group (``lot-identity-migration``) and leaves the existing semantics alone.

Two ``apply`` commands now exist with the same name and the same parameters but
opposite meanings — the projection one lands a *disabled checkpoint* and is
non-destructive, this one rewrites lot payloads and backfills identity. §13.3
calls that "本批次唯一的操作者风险面"; the mitigation is the distinct parent
group plus the distinct ``schema_version`` carried in every payload below.

The D1–D4 definitions live in §9.4; the execution design in §12; the rulings in
§9.5 (M1–M6). Four of §13.3's slice-3 assumptions did not survive contact with
the code and are corrected here rather than silently implemented around:

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
2. **D1/D2's rebuild cannot be executed by this batch.** The column contract is
   exact-set equality (``repository_projection_schema.py``), so a rebuilt
   ``position_lots`` without ``expiration``/``record_id`` is
   ``column_contract_open`` → heads ``untrusted`` → tail publish
   ``RuntimeError`` for as long as the running code still declares those
   columns. §13.5 R4 already records that the release carrying the two-shapes
   contract is missing from M6 and asks only to *write that step in*, not to
   execute it; §9.5 M6 step 3 puts the rebuild in a window. ``apply`` therefore
   runs the safe half (identity backfill + the ``position_id`` cleanup) and
   reports the destructive half as a deferred, itemized step with its reason.
3. **D3's rewrite of existing rows is unsafe until the read side converges.**
   §12.4 D3 states the precondition itself — "写侧改为纯 lot 形状；读侧必须留
   兼容读窗口，否则存量行读不出" — and today's readers (``read_model``,
   ``views``, ``read_only_evidence``) still consume ``account``/``symbol``/
   ``option_type``/``side`` from the payload. Rewriting existing rows to the
   pure lot shape now would blank them, so that step is deferred too.
4. **The checkpoint shortcut is weaker than "shape-only", but stronger than
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
5. **The open path, not this batch, moves the store into its own precondition.**
   ``_ensure_position_projection_schema`` adds ``lot_id`` and its unique index
   from ``_init_db``, so the first ordinary writer open after the release lands
   flips a pre-carrier store to the post-carrier shape. That is the shape this
   batch's ``inventory`` reports as ``not_ready`` *by design*
   (``lot_id_column_missing`` / ``lot_id_backfill_pending``), and it also moves
   ``sqlite_schema_cookie``, ``column_contract`` and ``pending`` — i.e. the
   inventory fingerprint — while ``store_identity`` stays equal. An ``inventory``
   taken before that first open is therefore refused by ``apply`` even though it
   describes this very store, and a refusal that blamed "another store" would
   send the operator after the wrong problem. The transition fires once per
   store (the second open is a no-op), so ``apply`` names the drift and the
   recovery: re-inventory the store as it now is. Nothing else about the gate
   changes — a store that genuinely moved on is still refused.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Mapping

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
    _ensure_position_projection_schema,
)
# The note KV fallback lives in the infrastructure codec; the repository layer
# reads it through the same helper (``repository_common``'s ``:64``).
from src.infrastructure.feishu_bitable import parse_note_kv


INVENTORY_SCHEMA = "lot_identity_migration_inventory.v1"
VERIFY_SCHEMA = "lot_identity_migration_verify.v1"
APPLY_SCHEMA = "lot_identity_migration_apply.v1"

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

#: The destructive half of the recipe is deferred while the running code still
#: declares the retired columns. §13.5 R4 / §9.5 M6 step 3.
_DEFERRED_COLUMN_REBUILD_REASON = "column_contract_precedes_rebuild"
_DEFERRED_LOT_SHAPE_REASON = "read_side_compatibility_window_open"

#: The inventory keys that move when the *writer* open path adds the identity
#: carrier to a store that predates it (``repository_projection_schema``'s
#: ``_ensure_position_projection_schema``, reached from ``repository_core``'s
#: ``_init_db`` on an ordinary ``SQLiteOptionPositionsRepository(path)``). This
#: drift is this migration's own precondition being met rather than the store
#: diverging, so ``apply`` names it instead of leaving the operator to guess.
_SCHEMA_TRANSITION_KEYS = ("column_contract", "pending", "sqlite_schema_cookie")


def _schema_transition_hint(drift: list[str]) -> str:
    """Explain the one drift the open path causes, and only that one."""

    if set(drift) != set(_SCHEMA_TRANSITION_KEYS):
        return ""
    return (
        "; that is the lot_id carrier a writer's first open adds to a store that "
        "predates it, which happens once per store — re-run inventory against the "
        "store as it now is"
    )

#: §12.3's checked rebuild recipe, carried by ``apply`` for the window.
REBUILD_RECIPE = (
    "probe shape (PRAGMA table_info + expected index set); return if already new",
    "read every row and normalize in Python",
    "DROP TABLE IF EXISTS position_lots_lot_identity_rebuild",
    "CREATE TABLE position_lots_lot_identity_rebuild with lot_id as PRIMARY KEY and without expiration/record_id",
    "INSERT every normalized row",
    "assert row count equality",
    "assert read-back equality",
    "PRAGMA foreign_key_check(position_lots_lot_identity_rebuild)",
    "DROP TABLE position_lots",
    "ALTER TABLE position_lots_lot_identity_rebuild RENAME TO position_lots",
    "recreate guard triggers and indexes without expiration/record_id",
)


def _canonical_fields_json(fields: Mapping[str, Any]) -> str:
    """The write path's own encoding (``repository_common._position_lot_storage_values``)."""

    return json.dumps(fields, ensure_ascii=False, sort_keys=True, allow_nan=False)


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

    if key == "note":
        reason = _note_disposition(value, fields, surviving_columns)
        if reason:
            return "lost", reason
        return "reconstructible", _NOTE_KV_RECONSTRUCTION
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
        if _non_empty(event_metadata.get(key)):
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
    for name in ("account", "expiration", "strike", "multiplier"):
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


def _projection_heads(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "position_projection_heads"):
        return {}
    return {
        str(row["account"]): int(row["lots_generation"] or 0)
        for row in conn.execute(
            "SELECT account, lots_generation FROM position_projection_heads"
        )
    }


def _inventory_from_conn(
    path: Path,
    conn: sqlite3.Connection,
    *,
    implementation: str,
) -> dict[str, Any]:
    rows = _scan_lot_payloads(conn)
    lot_columns = _column_names(conn, "position_lots")
    columns = {
        "position_lots": {
            "missing": sorted(set(POSITION_LOTS_COLUMN_CLASSIFICATION) - set(lot_columns)),
            "unclassified": sorted(set(lot_columns) - set(POSITION_LOTS_COLUMN_CLASSIFICATION)),
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
    if not pending["d2_record_id_column"]["column_present"]:
        # D2's rebuild already ran: the store is past this batch's entry state,
        # and the legacy-id backfill has no source column left to read.
        reasons.append("record_id_column_missing")

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
        "column_contract": columns,
        "pending": pending,
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
    elif lot_mismatches:
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


def _backfill_lot_id(conn: sqlite3.Connection) -> int:
    """Fill the identity carrier for rows that predate it.

    §13.2 row 7's gated form: same shape as
    ``backfill_position_lot_contract_columns`` — an explicit ``UPDATE`` inside
    the gated ``apply`` transaction, never on the open path. ``record_id`` is
    the primary key, so the assignment cannot produce a duplicate and the
    ``idx_position_lots_lot_id`` unique index cannot trip.
    """

    if "record_id" not in set(_column_names(conn, "position_lots")):
        # Past D2's rebuild the carrier *is* the primary key, so there is no
        # legacy column left to read an identity from and nothing to fill.
        return 0
    updated = conn.execute(
        "UPDATE position_lots SET lot_id = record_id WHERE lot_id IS NULL"
    ).rowcount
    return int(updated or 0)


def _strip_position_id(conn: sqlite3.Connection) -> int:
    """D4: remove the retired ``position_id`` from stored lot payloads.

    ``publisher._apply_lot_state_fields`` already pops it on every republish;
    this is the one-time traversal for rows that were never republished.
    ``fields_json`` is rewritten in the same canonical form the write path uses
    (``sort_keys``, ``ensure_ascii=False``, ``allow_nan=False``), so a later
    republish of an unchanged lot produces identical bytes.

    The row key is probed like every other column here: ``record_id`` is what
    D2's rebuild retires, and ``lot_id`` is the key after it.
    """

    columns = set(_column_names(conn, "position_lots"))
    if "record_id" in columns:
        key_column = "record_id"
    elif "lot_id" in columns:
        key_column = "lot_id"
    else:
        return 0
    updated = 0
    rows = conn.execute(
        f"SELECT {key_column} AS row_key, fields_json FROM position_lots ORDER BY {key_column}"
    ).fetchall()
    for row in rows:
        fields = _parse_fields(row["fields_json"])
        if fields is None or "position_id" not in fields:
            continue
        cleaned = {key: value for key, value in fields.items() if key != "position_id"}
        conn.execute(
            f"UPDATE position_lots SET fields_json = ? WHERE {key_column} = ?",
            (_canonical_fields_json(cleaned), str(row["row_key"])),
        )
        updated += 1
    return updated


def apply_lot_identity_migration(
    sqlite_path: str | Path,
    manifest: Mapping[str, Any],
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the gated part of D1–D4 and itemize the part this batch may not run."""

    supplied = _validate_manifest(manifest, schema=INVENTORY_SCHEMA)
    path = _store_path(sqlite_path)
    implementation, _timing = _loaded_implementation()
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    with _write_connection(path) as conn:
        before_sizes = _file_sizes(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _inventory_from_conn(path, conn, implementation=implementation)
            # Identity first: a fingerprint alone cannot tell "a different store"
            # from "this store moved on", and the two need different recoveries.
            if current["store_identity"] != supplied.get("store_identity"):
                raise ValueError("migration manifest belongs to another store")
            if current["inventory_fingerprint"] != supplied.get("inventory_fingerprint"):
                # Every key of ``current`` outside these three *is* a fingerprint
                # input (``_inventory_from_conn`` derives the other three from
                # them), so this names what actually moved.
                drift = sorted(
                    key
                    for key, value in current.items()
                    if key not in ("inventory_fingerprint", "readiness", "readiness_reasons")
                    and supplied.get(key) != value
                )
                raise ValueError(
                    "migration manifest is stale: the store changed after the inventory "
                    f"was taken (changed: {', '.join(drift) or 'unknown'})"
                    f"{_schema_transition_hint(drift)}"
                )
            if "base_tables_missing" in current["readiness_reasons"]:
                raise ValueError("migration requires an existing position_lots table")
            _fail(failure_hook, "after_manifest_recheck")

            heads_before = _projection_heads(conn)
            lot_id_present_before = current["pending"]["d2_lot_id_column"][
                "column_present"
            ]
            _ensure_position_projection_schema(conn)
            _fail(failure_hook, "after_schema")

            backfilled = _backfill_lot_id(conn)
            _fail(failure_hook, "after_lot_id_backfill")

            stripped = _strip_position_id(conn)
            _fail(failure_hook, "after_position_id_strip")

            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if str(integrity) != "ok":
                raise RuntimeError(f"integrity_check failed: {integrity}")
            lot_count = int(
                conn.execute("SELECT count(*) FROM position_lots").fetchone()[0]
            )
            if lot_count != current["counts"]["position_lots"]:
                raise RuntimeError("lot count changed during the identity backfill")
            null_lot_ids = int(
                conn.execute(
                    "SELECT count(*) FROM position_lots WHERE lot_id IS NULL"
                ).fetchone()[0]
            )
            if null_lot_ids:
                raise RuntimeError("lot identity backfill left NULL carriers")
            heads_after = _projection_heads(conn)
            _fail(failure_hook, "before_commit")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    advanced = sorted(
        account
        for account, generation in heads_after.items()
        if generation != heads_before.get(account)
    )
    # Each status is derived from what the step actually changed, so a re-run on
    # a migrated store reads as "already satisfied" instead of as the same
    # "applied" a first migration reports. The step names are the surface an
    # operator or a follow-up automation keys on, so a constant here would make
    # "migrated" and "nothing happened" indistinguishable.
    ensure_status = "already_present" if lot_id_present_before else "applied"
    steps_changed = [
        name
        for name, count in (
            ("ensure_lot_id_column", int(not lot_id_present_before)),
            ("backfill_lot_id", backfilled),
            ("strip_position_id_from_fields_json", stripped),
        )
        if count
    ]
    result = {
        "schema_version": APPLY_SCHEMA,
        "generated_at_utc": _now_iso(),
        "operation": "apply",
        "write_applied": bool(steps_changed),
        "steps_changed": steps_changed,
        "store_identity": _store_identity(path),
        "source_manifest_hash": supplied["manifest_hash"],
        "steps": [
            {
                "item": "D2",
                "step": "ensure_lot_id_column",
                "status": ensure_status,
            },
            {
                "item": "D2",
                "step": "backfill_lot_id",
                "status": "applied" if backfilled else "already_satisfied",
                "rows_updated": backfilled,
            },
            {
                "item": "D4",
                "step": "strip_position_id_from_fields_json",
                "status": "applied" if stripped else "already_satisfied",
                "rows_updated": stripped,
            },
            {
                "item": "D2",
                "step": "switch_primary_key_to_lot_id_and_drop_record_id",
                "status": "deferred",
                "reason": _DEFERRED_COLUMN_REBUILD_REASON,
            },
            {
                "item": "D1",
                "step": "drop_expiration_column",
                "status": "deferred",
                "reason": _DEFERRED_COLUMN_REBUILD_REASON,
            },
            {
                "item": "D3",
                "step": "rewrite_fields_json_to_lot_shape",
                "status": "deferred",
                "reason": _DEFERRED_LOT_SHAPE_REASON,
            },
        ],
        "deferred_rebuild_recipe": list(REBUILD_RECIPE),
        "pending_before": {
            key: current["pending"][key]
            for key in (
                "d1_expiration_column",
                "d2_record_id_column",
                "d2_lot_id_column",
                "d4_position_id_rows",
            )
        },
        "projection_heads_advanced": {
            "accounts": advanced,
            "reason": "fields_json is in the generation trigger's AFTER UPDATE OF list, so the D4 rewrite bumps lots_generation",
        },
        # A real entry point, not a placeholder: D4 rewrites ``fields_json``,
        # which the generation trigger watches, so the head's ``lots_generation``
        # moves ahead of what the tail built and readers gate on the pair.
        # ``om option-positions rebuild`` republishes ``position_lots`` from
        # canonical ``trade_events``; the next ordinary tail publish heals it too.
        "required_follow_up": (
            ["om option-positions rebuild"] if advanced else []
        ),
        "sqlite_bytes": {
            "before": before_sizes,
            "after": _file_sizes(path),
        },
        "timing": {
            "wall_ns": time.perf_counter_ns() - wall_start,
            "cpu_ns": time.process_time_ns() - cpu_start,
        },
    }
    return _manifest(result)


__all__ = [
    "APPLY_SCHEMA",
    "CARRIED_DROPPED_KEYS",
    "INVENTORY_SCHEMA",
    "LOT_SHAPE_KEYS_COMMON",
    "LOT_SHAPE_KEYS_STOCK_EXTRA",
    "REBUILD_RECIPE",
    "RECONSTRUCTIBLE_DROPPED_KEYS",
    "VERIFY_SCHEMA",
    "apply_lot_identity_migration",
    "build_lot_identity_migration_inventory",
    "verify_lot_identity_migration",
]
