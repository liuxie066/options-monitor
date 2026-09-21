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
2. **D1/D2's rebuild is gated on this build's live SQL, not on the column
   contract.** The window runs on the release that keeps *both* shapes
   readable — the contract-tightening release (§9.5 M6 step 5) comes *after*
   the window — so on window day the contract is deliberately still dual-shape
   and answers the wrong question. What the rebuild must know is whether the
   build that will keep running can read the rebuilt shape back, and that is a
   statement-level property of its SQL: ``apply`` therefore reads the pinned
   retired-column registry (``docs/retired_column_sql_registry.json``, the
   ledger the repo-wide guardrail and its quality test pin against the tree)
   and defers the destructive half while any unreviewed live statement — read,
   write or DDL, since an open path that re-adds a retired column pushes the
   rebuilt store back to the old shape — still names a retired column. R1's
   exact old-shape exceptions require both-shape regression evidence; they are
   removed with those branches in R2. The
   deferred report names the statements that made it defer. §9.5 M2 also fixes
   the granularity — D1's
   ``drop_expiration`` and D2-step-2's
   ``switch_primary_key_to_lot_id_and_drop_record_id`` are *one* table rebuild,
   so both steps report that one traversal.
3. **D3's rewrite is gated on the same fact, and cannot drop a fact.**
   §12.4 D3 states the precondition itself — "写侧改为纯 lot 形状；读侧必须留
   兼容读窗口，否则存量行读不出" — and the read side leaves that window in the
   same release that repoints the SQL above (the payload readers move with it),
   so the one registry gate opens the column rebuild and the payload rewrite
   together. When it opens, a fresh full replay supplies the exact canonical
   ``PositionLot.to_dict()`` payload for every lot. Before any rebuild, the
   batch requires a replay without errors, exact lot-id set equality, and no
   key that ``_drop_disposition`` calls ``lost``. The classifier is therefore
   the rewrite's loss gate, while the canonical event replay is the write
   source; the old row is never used to synthesize a partial v2 payload.
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
_DEFERRED_REPOINT_REASON = "live_sql_repointing_precedes_rebuild"

#: ``apply`` refuses to run until the D1–D4 window batch is explicitly enabled.
#: The batch is a controlled-window operation: it writes the store even when the
#: manifest checks pass, so "the operator passed --apply --yes" is not
#: authorization enough between the release that lands this guard and the
#: window itself. Arming is a reviewed commit that sets this to the window's
#: authorization token. ``None`` — the value every such intermediate build
#: carries — means "not enabled", and the refusal fires before any connection
#: to the store is opened.
LOT_IDENTITY_WINDOW_ENABLEMENT: str | None = "liuxie-incus-2026-09-22-final-shape"

#: The two columns the rebuild retires: D1's derived ``expiration`` mirror and
#: D2's legacy identity name. §9.5 M2 makes their removal *one* rebuild.
RETIRED_LOT_COLUMNS = ("expiration", "record_id")

#: §12.3 step 3's temporary name. ``DROP TABLE IF EXISTS`` on it is what makes
#: a re-run after an interrupted window idempotent.
REBUILD_TEMP_TABLE = "position_lots_lot_identity_rebuild"

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


#: The statement-level ledger of live SQL that names the retired columns:
#: generated by ``scripts/retired_column_scan.py --write`` (line scan + window
#: scan — DDL column lists span lines) and pinned against the tree by the
#: repo-wide guardrail and its quality test. ``apply`` reads it as the build's
#: own answer to "can the running build read the rebuilt shape back?".
RETIRED_COLUMN_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "retired_column_sql_registry.json"
)

# R1 only: exact old-shape statements selected by position_lots_use_lot_id.
# The core CREATE TABLE is a no-op on an existing table (IF NOT EXISTS).
# Quality checks bind every entry to the two-shape repository regression in
# test_r1_rebuilt_store_reopens_and_preserves_projection. They also reject
# stale entries; --write of the generated registry cannot extend this set.
# R2 removes these entries together with their old-shape branches after B7.
R1_POSITION_SQL_EXCEPTIONS: dict[str, frozenset[tuple[str, str, int]]] = {
    "src/application/ledger/current_decision_migration.py": frozenset({
        ("index_name", "sha256:4705ae2499089b72", 1),
    }),
    "src/application/ledger/current_decision_oracle.py": frozenset({
        ("index_name", "sha256:4705ae2499089b72", 1),
    }),
    "src/application/ledger/manual_trades.py": frozenset({
        ("read", "sha256:9808a49757695702", 1),
    }),
    "src/application/ledger/repository_assigned_stock.py": frozenset({
        ("write", "sha256:aa32ce63d24f0c0f", 1),
    }),
    "src/application/ledger/repository_projection_schema.py": frozenset({
        ("ddl", "sha256:1db55addc09d05cf", 1),
        ("ddl", "sha256:a90268930d130e48", 1),
        ("ddl", "sha256:b3ee5f2c21a78fdc", 1),
        ("ddl", "sha256:eb62e146516f2a69", 1),
        ("ddl", "sha256:fea50e0a430d6900", 1),
        ("index_name", "sha256:2533bf5b4afc0c98", 1),
        ("index_name", "sha256:4705ae2499089b72", 1),
        ("dynamic", "sha256:2e17af0755d5ce73", 1),
        ("dynamic", "sha256:d3f2b9942a10355b", 1),
        ("dynamic", "sha256:e55d96210a4ee8d6", 1),
    }),
    "src/application/ledger/position_projection_migration.py": frozenset({
        ("index_name", "sha256:2533bf5b4afc0c98", 2),
        ("index_name", "sha256:4705ae2499089b72", 2),
        ("read", "sha256:08a18e936798b71b", 1),
        ("read", "sha256:3f82fe90a41f58b1", 1),
        ("read", "sha256:fde0f8e8075836b8", 1),
    }),
    "src/application/ledger/read_only_evidence.py": frozenset({
        ("read", "sha256:112aa4a949f4e07e", 1),
    }),
    "src/application/ledger/repository_core.py": frozenset({
        ("ddl", "sha256:4318f3c4c19eb537", 1),
        ("ddl", "sha256:53a9f1cd00b07bf7", 1),
        ("ddl", "sha256:77c5cf64f84cc313", 1),
        ("index_name", "sha256:481b578c7f1b9f72", 1),
        ("schema_helper", "sha256:5570ac5c24e3838a", 1),
    }),
    "src/application/ledger/repository_projection.py": frozenset({
        ("ddl", "sha256:600ec5dd6f9829f9", 1),
        ("ddl", "sha256:e127ac6364f59574", 1),
        ("index_name", "sha256:2533bf5b4afc0c98", 1),
        ("index_name", "sha256:4705ae2499089b72", 1),
        ("read", "sha256:c4de6ba3dd25d0a7", 1),
        ("read", "sha256:fde0f8e8075836b8", 1),
        ("write", "sha256:85efd73292ea5dd6", 1),
        ("write", "sha256:bcbd848f39b3c9d4", 1),
    }),
    "src/application/ledger/repository_projection_tail.py": frozenset({
        ("index_name", "sha256:2533bf5b4afc0c98", 2),
        ("index_name", "sha256:4705ae2499089b72", 2),
        ("read", "sha256:0316be086ed9cbdb", 1),
        ("read", "sha256:0a21b814f917372f", 1),
        ("read", "sha256:1f77b6a43fa94ed3", 2),
        ("read", "sha256:2fbeb0c7d711b58f", 1),
        ("read", "sha256:6ecfabfa6adbce9b", 1),
        ("read", "sha256:906c26f8d69d736f", 1),
        ("read", "sha256:92f8e71eef34664e", 1),
        ("read", "sha256:ccf7e57f7a709379", 1),
        ("write", "sha256:2993be41278daa7a", 1),
        ("write", "sha256:bf3da6241e607360", 1),
        ("write", "sha256:c6b8c0f17fdfee97", 1),
    }),
    "src/application/ledger/sqlite_row_codec.py": frozenset({
        ("read", "sha256:24a86222a7c0b3cb", 1),
    }),
}


def _live_sql_naming_retired_columns() -> tuple[str, ...]:
    """Which live statements of this build still name a retired column.

    The gate is *derived from the ledger in force*, never from a flag: the
    registry is what the ``--check`` guardrail and its quality test hold the
    tree to, so "the build still carries such a statement" and "the ledger
    lists one" are the same fact, checked on every PR and every production
    upgrade. The rebuild's question — "can this build read the rebuilt shape
    back?" — is a statement-level property of the SQL, and the registry answers
    it per statement, excluding only the exact R1 old-shape exceptions tested
    on both shapes; the column contract deliberately cannot, because on
    window day it is still dual-shape.

    Returns one ``"module (kind)"`` descriptor per live statement in the
    ledger's order; empty means the destructive half may run. An unreadable or
    malformed ledger reports a deferral too: this gate opens on evidence,
    never on a missing file.
    """

    try:
        registry = json.loads(RETIRED_COLUMN_REGISTRY_PATH.read_text(encoding="utf-8"))
        detail = registry["src"]["detail"]
        dynamic = registry["src"]["dynamic_sql"]
        if not isinstance(detail, list) or not isinstance(dynamic, list):
            raise ValueError("statement inventories must be lists")
        return tuple(
            f"{hit['module']} ({hit['kind']})"
            for hit in detail + dynamic
            if (hit["kind"], hit.get("digest"), hit.get("occurrences"))
            not in R1_POSITION_SQL_EXCEPTIONS.get(hit["module"], ())
        )
    except (OSError, ValueError, KeyError, TypeError):
        return (f"<registry unreadable: {RETIRED_COLUMN_REGISTRY_PATH.name}>",)


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


def _rewrite_lot_payloads(
    conn: sqlite3.Connection,
    *,
    align_to_lot_shape: bool,
    replayed_payloads: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    """D3 and D4 in one traversal over ``position_lots``.

    §9.4's D3 and D4 are two items with one edit: aligning a row to the lot
    shape and removing the retired ``position_id`` both rewrite the same
    ``fields_json``, so a second pass would only re-encode the first pass's
    output. ``align_to_lot_shape`` separates the halves the two items own:
    ``apply`` runs the alignment only once the gate's ledger says the read side
    has converged, and otherwise performs exactly the D4 cleanup the batch has
    always performed.

    ``fields_json`` is rewritten in the write path's own canonical form
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
        return {"rows_scanned": 0, "position_id_rows": 0, "rewritten_rows": 0}
    # ``NOTE_KV_SURVIVING_COLUMNS`` names the derived columns a note-only fact
    # may still live in, so the classification below needs their values, not
    # just their names.
    surviving = [
        name for name in NOTE_KV_SURVIVING_COLUMNS.values() if name in columns
    ]
    rows = conn.execute(
        f"SELECT {key_column} AS row_key, fields_json"
        f"{''.join(f', {name}' for name in surviving)}"
        f" FROM position_lots ORDER BY {key_column}"
    ).fetchall()
    # The event-layer keys are answered by measurement, not declaration (see
    # ``_drop_disposition``), so the rewrite asks the same question with the
    # same reader the inventory does — classifying against an empty event
    # layer here would abort the batch on rows whose family is really there.
    families = _event_layer_strategy_families(conn)
    scanned = position_id_rows = rewritten = 0
    for row in rows:
        scanned += 1
        fields = _parse_fields(row["fields_json"])
        if fields is None:
            continue
        has_position_id = _non_empty(fields.get("position_id"))
        if has_position_id:
            position_id_rows += 1
        if align_to_lot_shape:
            if replayed_payloads is not None:
                lot_id = str(row["row_key"])
                if lot_id not in replayed_payloads:
                    raise RuntimeError(f"fresh replay omitted position lot: {lot_id}")
                aligned = deepcopy(dict(replayed_payloads[lot_id]))
            else:
                aligned = _aligned_lot_payload(
                    fields, row, _family_for_row({key_column: row["row_key"]}, families)
                )
        elif has_position_id:
            aligned = {key: value for key, value in fields.items() if key != "position_id"}
        else:
            continue
        encoded = _canonical_fields_json(aligned)
        if encoded == _canonical_fields_json(fields):
            continue
        conn.execute(
            f"UPDATE position_lots SET fields_json = ? WHERE {key_column} = ?",
            (encoded, str(row["row_key"])),
        )
        rewritten += 1
    return {
        "rows_scanned": scanned,
        "position_id_rows": position_id_rows,
        "rewritten_rows": rewritten,
    }


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
_TABLE_BODY = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s*\((?P<body>.*)\)\s*(?:STRICT\s*)?$",
    re.IGNORECASE | re.DOTALL,
)

_INDEX_DDL = re.compile(
    r"^\s*CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>\S+)"
    r"\s+ON\s+(?P<table>\S+)\s*\((?P<columns>[^)]*)\)(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _primary_key_column(conn: sqlite3.Connection, table: str) -> str | None:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if int(row["pk"] or 0) == 1:
            return str(row["name"])
    return None


def _split_column_defs(body: str) -> list[str]:
    """Split a ``CREATE TABLE`` body on top-level commas only.

    A ``CHECK(...)``/``DEFAULT (...)`` clause contains commas of its own, so a
    plain ``split(",")`` would cut a column definition in half.
    """

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _column_def_name(definition: str) -> str:
    name = definition.strip().split(None, 1)[0]
    return name.strip('"`[]')


def _rebuild_column_defs(conn: sqlite3.Connection, retained: list[str]) -> list[str]:
    """The rebuilt table's column definitions, in ``retained`` order.

    Every surviving column keeps the declaration the store already had; the
    only authored change is D2's: the primary key moves from the retired
    ``record_id`` onto ``lot_id``.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='position_lots'"
    ).fetchone()
    if row is None or not str(row["sql"] or "").strip():
        raise RuntimeError("rebuild requires the stored CREATE TABLE for position_lots")
    match = _TABLE_BODY.match(str(row["sql"]).strip())
    if match is None:
        raise RuntimeError("position_lots CREATE TABLE is not in a parseable form")
    declared = {
        _column_def_name(definition): definition
        for definition in _split_column_defs(match.group("body"))
    }
    if set(declared) != set(_column_names(conn, "position_lots")):
        raise RuntimeError(
            "position_lots CREATE TABLE and PRAGMA table_info disagree about its columns"
        )
    missing = [name for name in retained if name not in declared]
    if missing:
        raise RuntimeError(f"rebuilt table would lose columns: {', '.join(missing)}")
    defs: list[str] = []
    for name in retained:
        definition = declared[name]
        if name == "lot_id":
            definition = f"{definition} NOT NULL PRIMARY KEY CHECK (length(trim(lot_id)) > 0)"
        defs.append(definition)
    return defs


def _new_shape_trigger_sql(sql: str) -> str:
    """Rewrite a stored ``position_lots`` trigger for the rebuilt shape.

    The guards are recreated from the definitions the store is actually running
    rather than from a copy of them kept here: a second copy of the guard DDL in
    a migration module is a guard that drifts every time the real one is
    touched, and the guards on the rebuilt table would then be the migration's
    idea of them. Two edits are declared and nothing else:

    * the identity column is renamed (``record_id`` → ``lot_id``, D2);
    * the retired ``expiration`` column stops being watched (D1), in both the
      ``AFTER UPDATE OF`` list and the change test; and
    * the account the guard reads moves to where the target shape keeps it
      (``$.account`` → ``$.contract_key.account``).

    The third edit is forced by the first two being landable at all: the guard
    the store is running (``repository_projection_schema``:
    ``trg_position_lots_account_*``, the three generation triggers) reads the
    *flat* ``$.account``, and ``PositionLot.to_dict()`` has no such key — it
    carries the account at ``contract_key.account``. A rebuilt table that kept a
    flat reading guard would reject every write of an aligned row, so the
    rebuild would produce a store that cannot be written to. It is one more
    reason the D3 rewrite runs after this function and not before: the guards
    have to be the new shape's before the payloads become the new shape.

    Anything left over that still names a retired column, or still reads the
    flat account path, raises — a body this rewrite does not understand stops
    the migration instead of silently installing a guard that cannot fire.
    """

    rewritten = re.sub(r"\brecord_id\b", "lot_id", sql)
    rewritten = re.sub(
        r"\s+OR\s+OLD\.expiration IS NOT NEW\.expiration", "", rewritten
    )
    rewritten = re.sub(r"\s*,\s*expiration\b", "", rewritten)
    rewritten = rewritten.replace("'$.account'", "'$.contract_key.account'")
    for retired in (*RETIRED_LOT_COLUMNS, "'$.account'"):
        if re.search(rf"\b{retired}\b" if retired.isidentifier() else re.escape(retired), rewritten):
            raise RuntimeError(
                f"cannot rebuild guard trigger {sql.split()[2] if len(sql.split()) > 2 else ''}"
                f": {retired} still named after the declared rewrite"
            )
    return rewritten


def _new_shape_index_sql(sql: str) -> str | None:
    """Rewrite one stored ``position_lots`` index for the rebuilt shape.

    Its column list loses the retired columns; an index whose list becomes
    empty, or whose partial ``WHERE`` clause names a retired column, has no
    shape left and is dropped. The name and the UNIQUE flag are kept, so the
    store's index set stays recognisable.
    """

    match = _INDEX_DDL.match(sql.strip())
    if match is None:
        raise RuntimeError("position_lots index DDL is not in a parseable form")
    tail = match.group("tail") or ""
    for retired in RETIRED_LOT_COLUMNS:
        if re.search(rf"\b{retired}\b", tail):
            return None
    columns = [item.strip() for item in match.group("columns").split(",")]
    if match.group("name") in {"idx_position_lots_account_expiration", "idx_position_lots_account_record"}:
        if columns not in (["account", "expiration", "record_id"], ["account", "record_id"]):
            raise RuntimeError("position_lots account index has an unexpected definition")
        return "CREATE INDEX idx_position_lots_account_lot ON position_lots(account, lot_id)"
    kept = [item for item in columns if _column_def_name(item) not in RETIRED_LOT_COLUMNS]
    if not kept:
        return None
    unique = "UNIQUE " if match.group("unique") else ""
    return (
        f"CREATE {unique}INDEX {match.group('name')} ON {match.group('table')}"
        f"({', '.join(kept)}){tail}"
    )


def _new_shape_guard_sql(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """The triggers and indexes the rebuilt table must carry.

    Read before the old table is dropped, because dropping it drops both.
    Identical index shapes are collapsed to one — after the retirement an
    ``(expiration, record_id)`` and an ``(account, record_id)`` index can filter
    down to the same column list, and carrying two indexes over one list is
    write cost with no lookup to show for it. The UNIQUE entry wins, so the
    identity index the open path re-creates on every open (``CREATE UNIQUE
    INDEX IF NOT EXISTS idx_position_lots_lot_id``) is the one that survives and
    the next open stays a no-op.
    """

    triggers = [
        _new_shape_trigger_sql(str(row["sql"]))
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='position_lots'"
            " AND sql IS NOT NULL ORDER BY name"
        )
    ]
    parsed: list[tuple[bool, str, tuple[str, ...]]] = []
    for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='position_lots'"
        " AND sql IS NOT NULL ORDER BY name"
    ):
        rewritten = _new_shape_index_sql(str(row["sql"]))
        if rewritten is None:
            continue
        match = _INDEX_DDL.match(rewritten)
        assert match is not None  # _new_shape_index_sql rebuilt this exact form
        columns = tuple(
            _column_def_name(item).lower()
            for item in match.group("columns").split(",")
        )
        parsed.append((bool(match.group("unique")), rewritten, columns))
    # UNIQUE first, then first-come: dropping a UNIQUE index because a plain one
    # over the same columns was declared earlier would be a silent weakening.
    indexes: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for unique, rewritten, columns in sorted(parsed, key=lambda item: not item[0]):
        if columns in seen:
            continue
        seen.add(columns)
        indexes.append(rewritten)
    return triggers, indexes


def _row_key(values: Any) -> tuple[Any, ...]:
    # ``repr`` for floats so the comparison is on the stored bits rather than on
    # a arithmetic identity SQLite does not promise (and so a NaN, which is not
    # equal to itself, cannot make every row differ).
    return tuple(repr(value) if isinstance(value, float) else value for value in values)


def _normalized_rebuild_values(
    row: Mapping[str, Any],
    columns: list[str],
) -> list[Any]:
    """§12.3 step 2 for one row: copy it out, then normalize in Python.

    ``lot_id`` is the identity (D2); a row that still predates the gated
    backfill inherits it from ``record_id``, which is the same value under the
    old name (§9.5 M4). One implementation, because the read-back check has to
    expect the same values the copier writes — a normalization applied only on
    the way in reports every such row as a mismatch.
    """

    values = [row[name] for name in columns]
    if not values[0] and "record_id" in row.keys():
        values[0] = row["record_id"]
    return values


def _insert_rebuild_rows(
    conn: sqlite3.Connection,
    columns: list[str],
    rows: list[Mapping[str, Any]],
) -> int:
    """§12.3 step 5. Rows go in column by column, so nothing a column holds is
    dropped on the way through — including columns this batch has never heard
    of."""

    placeholders = ",".join("?" for _ in columns)
    statement = (
        f"INSERT INTO {REBUILD_TEMP_TABLE} ({','.join(columns)}) VALUES ({placeholders})"
    )
    inserted = 0
    for row in rows:
        conn.execute(statement, _normalized_rebuild_values(row, columns))
        inserted += 1
    return inserted


def _assert_retirement_preserves_facts(conn: sqlite3.Connection) -> None:
    from domain.domain.ledger.position_fields import parse_exp_to_ms

    for row in conn.execute("SELECT * FROM position_lots"):
        fields = _parse_fields(row["fields_json"])
        contract = fields.get("contract_key") or {}
        asset_type = fields.get("asset_type")
        if asset_type not in {"option", "stock"}:
            raise RuntimeError("retirement requires an explicit option or stock asset_type")
        if not row["account"] or row["account"] != contract.get("account"):
            raise RuntimeError("retirement account disagrees with contract_key")
        if not row["source_event_id"] or row["source_event_id"] != fields.get("open_event_id"):
            raise RuntimeError("retirement source_event_id disagrees with open_event_id")
        legacy_source = fields.get("source_event_id")
        if legacy_source not in (None, "", fields.get("open_event_id")):
            raise RuntimeError("retirement payload source_event_id disagrees with open_event_id")
        if asset_type == "option":
            original = row["expiration"]
            if original is None or row["strike"] is None:
                raise RuntimeError("retirement option requires expiration and strike")
            # Dropping milliseconds is only safe when the date round trip is exact.
            ymd = expiration_timestamp_to_ymd(original)
            if parse_exp_to_ms(ymd) != original or parse_exp_to_ms(contract.get("expiration_ymd")) != original:
                raise RuntimeError("retirement expiration millisecond round trip differs")


def _rebuild_position_lots(
    conn: sqlite3.Connection,
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """§12.3's checked rebuild: D1's dropped column and D2's renamed key in one.

    §9.5 M2 makes ``drop_expiration`` and
    ``switch_primary_key_to_lot_id_and_drop_record_id`` a single rebuild — one
    table pass, one set of checks, one rollback — so this performs both or
    neither. The order is the recipe's, and every check either passes or raises
    ``RuntimeError``: the caller's transaction rolls back and no half-rebuilt
    table survives. The hooks between the steps exist so that claim is testable
    at each point, including the ones after the old table is dropped.
    """

    columns = _column_names(conn, "position_lots")
    if not columns:
        raise RuntimeError("rebuild requires a position_lots table")
    probe = set(columns)
    if "lot_id" not in probe:
        raise RuntimeError("rebuild requires the lot_id identity column")
    retired_present = [name for name in RETIRED_LOT_COLUMNS if name in probe]
    if not retired_present and _primary_key_column(conn, "position_lots") == "lot_id":
        return {
            "rebuilt": False,
            "reason": "shape_already_new",
            "rows": int(conn.execute("SELECT count(*) FROM position_lots").fetchone()[0]),
        }

    _assert_retirement_preserves_facts(conn)
    retained = [
        "lot_id",
        *(name for name in columns if name != "lot_id" and name not in set(RETIRED_LOT_COLUMNS)),
    ]
    defs = _rebuild_column_defs(conn, retained)
    triggers, indexes = _new_shape_guard_sql(conn)
    audit = [*retained, *(["record_id"] if "record_id" in probe else [])]
    rows = conn.execute(
        f"SELECT {','.join(audit)} FROM position_lots ORDER BY rowid"
    ).fetchall()
    _fail(failure_hook, "after_rebuild_rows_read")

    conn.execute(f"DROP TABLE IF EXISTS {REBUILD_TEMP_TABLE}")
    conn.execute(f"CREATE TABLE {REBUILD_TEMP_TABLE} ({', '.join(defs)})")
    inserted = _insert_rebuild_rows(conn, retained, rows)
    _fail(failure_hook, "after_rebuild_insert")
    if inserted != len(rows):
        raise RuntimeError(
            f"rebuild row count differs: read {len(rows)}, inserted {inserted}"
        )
    _fail(failure_hook, "after_rebuild_row_count_check")

    readback = conn.execute(
        f"SELECT {','.join(retained)} FROM {REBUILD_TEMP_TABLE}"
    ).fetchall()
    written = Counter(
        _row_key(_normalized_rebuild_values(row, retained)) for row in rows
    )
    stored = Counter(_row_key(row) for row in readback)
    if written != stored:
        raise RuntimeError("rebuild read-back differs from the rows copied out")
    _fail(failure_hook, "after_rebuild_read_back_check")

    violations = conn.execute(f"PRAGMA foreign_key_check({REBUILD_TEMP_TABLE})").fetchall()
    if violations:
        raise RuntimeError(f"rebuild foreign_key_check reported {len(violations)} violation(s)")
    _fail(failure_hook, "after_rebuild_foreign_key_check")

    conn.execute("DROP TABLE position_lots")
    _fail(failure_hook, "after_rebuild_drop_old_table")
    conn.execute(f"ALTER TABLE {REBUILD_TEMP_TABLE} RENAME TO position_lots")
    _fail(failure_hook, "after_rebuild_rename")
    for statement in triggers:
        conn.execute(statement)
    for statement in indexes:
        conn.execute(statement)
    _fail(failure_hook, "after_rebuild_guards")

    return {
        "rebuilt": True,
        "temp_table": REBUILD_TEMP_TABLE,
        "rows": len(rows),
        "retired_columns": retired_present,
        "columns": retained,
        "primary_key": "lot_id",
        "triggers_recreated": len(triggers),
        "indexes_recreated": len(indexes),
        "foreign_key_check": "ok",
        "read_back": "equal",
    }


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


def _rename_wheel_lot_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    before = _wheel_identity_inventory(conn)
    if before["status"] != "stock_lot_id":
        return {**before, "renamed": False}
    # SQLite rewrites dependent indexes and checks. Event bytes, historical
    # hash keys, rowids and append-only triggers must survive unchanged.
    conn.execute("ALTER TABLE wheel_events RENAME COLUMN stock_lot_id TO lot_id")
    after = _wheel_identity_inventory(conn)
    if after != {**before, "status": "lot_id"}:
        raise RuntimeError("wheel identity rename changed stored event facts")
    if conn.execute("PRAGMA foreign_key_check(wheel_events)").fetchall():
        raise RuntimeError("wheel identity rename failed foreign_key_check")
    return {**after, "renamed": True, "read_back": "equal"}


def apply_lot_identity_migration(
    sqlite_path: str | Path,
    manifest: Mapping[str, Any],
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the gated part of D1–D4 and itemize the part this batch may not run."""

    if not LOT_IDENTITY_WINDOW_ENABLEMENT:
        raise RuntimeError(
            "lot-identity apply is not enabled on this build: the D1-D4 window "
            "batch runs only inside an authorized migration window. Arming it is "
            "a reviewed commit that sets "
            "lot_identity_migration.LOT_IDENTITY_WINDOW_ENABLEMENT to that "
            "window's authorization token; this build carries none."
        )
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

            # The gate reads the build's own ledger, not the store: the window
            # runs on the release whose live SQL has been repointed while the
            # column contract there is deliberately still dual-shape, so the
            # statements — not the contract — are what say the rebuilt shape
            # can be read back.
            deferred_by = _live_sql_naming_retired_columns()
            replayed_payloads: dict[str, dict[str, Any]] | None = None
            if not deferred_by:
                lost = current["dropped_key_classification"]["lost"]
                if lost:
                    key = sorted(lost)[0]
                    raise RuntimeError(
                        "lot payload rewrite would lose a fact: "
                        f"key {key!r} has no carrier ({lost[key]['reason']})"
                    )
                replayed_payloads = _fresh_replay_payloads(conn)
            # The rebuild goes first, and the payload rewrite follows it: the
            # rebuilt table's account guards read the account at the target
            # shape's path (``contract_key.account``), so they can only be
            # installed before the rows are aligned to it. Step order in the
            # report below is the *item* order (D1–D4 design order), not this
            # execution order.
            rebuild = (
                _rebuild_position_lots(conn, failure_hook=failure_hook)
                if not deferred_by
                else None
            )
            if rebuild is not None:
                _fail(failure_hook, "after_rebuild")
            wheel_rename = _rename_wheel_lot_identity(conn) if not deferred_by else None
            if wheel_rename is not None:
                _fail(failure_hook, "after_wheel_identity_rename")
            rewrite = _rewrite_lot_payloads(
                conn,
                align_to_lot_shape=not deferred_by,
                replayed_payloads=replayed_payloads,
            )
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
    rebuilt = int(bool(rebuild and rebuild["rebuilt"]))
    changed: list[tuple[str, int]] = [
        ("ensure_lot_id_column", int(not lot_id_present_before)),
        ("backfill_lot_id", backfilled),
        ("strip_position_id_from_fields_json", rewrite["position_id_rows"]),
    ]
    if not deferred_by:
        changed += [
            # D1 and D2 share one rebuild (§9.5 M2), so one table pass satisfies
            # both steps and both names are reported as changed — an operator
            # reading the ledger item by item should not have to know that D1's
            # line was carried by D2's.
            ("switch_primary_key_to_lot_id_and_drop_record_id", rebuilt),
            ("drop_expiration_column", rebuilt),
            ("rewrite_fields_json_to_lot_shape", rewrite["rewritten_rows"]),
            ("rename_wheel_stock_lot_id", int(bool(wheel_rename and wheel_rename["renamed"]))),
        ]
    steps_changed = [name for name, count in changed if count]
    # The three destructive steps are the same three entries in both states —
    # same items, same names, same order — so an operator's ledger keeps its
    # shape across the window. Only the status changes, and the one reason is
    # the one fact the gate reads: this build still owes the window the
    # repointing that lets the rebuilt shape be read back.
    if deferred_by:
        destructive_steps: list[dict[str, Any]] = [
            {
                "item": "D2",
                "step": "switch_primary_key_to_lot_id_and_drop_record_id",
                "status": "deferred",
                "reason": _DEFERRED_REPOINT_REASON,
            },
            {
                "item": "D1",
                "step": "drop_expiration_column",
                "status": "deferred",
                "reason": _DEFERRED_REPOINT_REASON,
            },
            {
                "item": "D3",
                "step": "rewrite_fields_json_to_lot_shape",
                "status": "deferred",
                "reason": _DEFERRED_REPOINT_REASON,
            },
        ]
        rebuild_report: dict[str, Any] = {
            # The deferral's cause, statement by statement: an operator reading
            # a window receipt sees which build-side repointing is still
            # missing rather than only that something is.
            "rebuild_gate": {
                "criterion": "no live SQL names a retired column",
                "ledger": RETIRED_COLUMN_REGISTRY_PATH.name,
                "live_statements": list(deferred_by),
            }
        }
    else:
        assert rebuild is not None  # the gate is what decides whether it runs
        rebuild_status = "applied" if rebuild["rebuilt"] else "already_satisfied"
        destructive_steps = [
            {
                "item": "D2",
                "step": "switch_primary_key_to_lot_id_and_drop_record_id",
                "status": rebuild_status,
            },
            {
                "item": "D1",
                "step": "drop_expiration_column",
                "status": rebuild_status,
            },
            {
                "item": "D3",
                "step": "rewrite_fields_json_to_lot_shape",
                "status": "applied" if rewrite["rewritten_rows"] else "already_satisfied",
                "rows_updated": rewrite["rewritten_rows"],
            },
        ]
        # The rebuild's own receipt, only in this state: the deferred receipt
        # carries ``rebuild_gate`` instead, because there the question an
        # operator asks is which statements held the rebuild back, not what a
        # rebuild that did not run returned.
        rebuild_report = {"rebuild": rebuild, "wheel_identity_rename": wheel_rename}
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
                "status": (
                    "applied" if rewrite["position_id_rows"] else "already_satisfied"
                ),
                "rows_updated": rewrite["position_id_rows"],
            },
            *destructive_steps,
        ],
        "deferred_rebuild_recipe": list(REBUILD_RECIPE),
        **rebuild_report,
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
    "CARRIER_TARGETS",
    "INVENTORY_SCHEMA",
    "LOT_SHAPE_KEYS_COMMON",
    "LOT_SHAPE_KEYS_STOCK_EXTRA",
    "REBUILD_RECIPE",
    "RECONSTRUCTIBLE_DROPPED_KEYS",
    "RETIRED_LOT_COLUMNS",
    "VERIFY_SCHEMA",
    "apply_lot_identity_migration",
    "build_lot_identity_migration_inventory",
    "verify_lot_identity_migration",
]
